"""Persist explicit archive inventories in an already-open temporary schema-3 DB.

This module is deliberately not a repository factory: it never discovers or opens a
library.  A caller must provide a sqlite3 connection to an explicitly selected,
isolated schema-3 copy.  A bounded, segmented inventory document is the single
authoritative planned representation; planned package indexes are only an index.
Plans are not sealed objects, remote locations, verification evidence, or a
completion ledger.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone

from core.archive_scope import ScopeReceipt, ScopeSelectionKind
from core.archive_inventory import (
    ARCHIVE_INVENTORY_SCHEMA_VERSION,
    CAPACITY_PLAN_SCHEMA_VERSION,
    ArchiveEntry,
    ArchiveInventory,
    ArtworkGroup,
    CapacityComponent,
    CapacityPhase,
    CapacityPlan,
    CapacityPolicy,
    CapacityStatus,
    InventoryAsset,
    InventoryIssue,
    InventoryIssueCode,
    InventoryPolicy,
    InventoryStatus,
    PackagePlan,
    build_archive_inventory,
    plan_archive_capacity,
)
from core.archive_schema import (
    ARCHIVE_PLAN_DOCUMENT_LIMIT,
    ARCHIVE_PLAN_SEGMENT_COUNT,
    ARCHIVE_PLAN_SEGMENT_LIMIT,
    ARCHIVE_PROTOCOL_VERSION,
    ARCHIVE_SCHEMA_VERSION,
    ARCHIVE_TABLES,
    ArchiveSchemaError,
    _check_existing_objects,
    _content_key,
    bind_archive_path,
    ensure_archive_asset,
    read_plan_segments,
)
from core.local_observation import FileIdentity
from core.preservation_policy import ContentVersion
from core.task_contracts import ContractError, PathRef, canonical_json, normalize_json

_ARCHIVE_PLAN_FORMAT = "jpgpng-archive-plan"
_ARCHIVE_PLAN_VERSION = 3
_ARCHIVE_PLAN_SEGMENT_CHARS = 200_000
ARCHIVE_PLAN_MAX_BYTES = ARCHIVE_PLAN_DOCUMENT_LIMIT
ARCHIVE_PLAN_MAX_ENTRIES = 9_000
ARCHIVE_PLAN_MAX_SEGMENTS = ARCHIVE_PLAN_SEGMENT_COUNT
_BATCH_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class ArchivePlanStoreError(ArchiveSchemaError):
    """The connection, stored plan, or plan association fails closed validation."""


class ArchivePlanConflictError(ArchivePlanStoreError):
    """An immutable batch or package already has different facts."""


class ArchivePlanTooLargeError(ArchivePlanStoreError):
    """The prepared plan exceeds the bounded command/document budget."""

    def __init__(self, batch_id: str, payload_bytes: int):
        self.batch_id = batch_id
        self.payload_bytes = payload_bytes
        super().__init__(
            f"Archive plan for {batch_id} is {payload_bytes} bytes; "
            f"the bounded plan budget is {ARCHIVE_PLAN_MAX_BYTES} bytes"
        )


@dataclass(frozen=True, slots=True)
class ArchivePathConfirmation:
    """One path whose physical replacement needs an explicit human decision."""

    relative_path: str
    path_key: str
    existing_asset_id: str | None
    existing_revision: int | None
    proposed_asset_id: str
    proposed_revision: int
    reason: str
    next_action: str = "confirm_same_name_replacement"


class ArchiveContentConflictError(ArchivePlanStoreError):
    """A path already has different immutable facts for the same asset revision."""

    def __init__(self, asset_id: str, revision: int, relative_path: str, path_key: str, reason: str):
        self.asset_id = asset_id
        self.revision = revision
        self.relative_path = relative_path
        self.path_key = path_key
        self.reason = reason
        super().__init__(
            "Content conflict for %s at %s (asset %s revision %s): %s"
            % (relative_path, path_key, asset_id, revision, reason)
        )


class ArchiveConfirmationRequiredError(ArchivePlanStoreError):
    """A same-name physical replacement is not auto-accepted by this batch.

    The plan is not saved, the previous batch and path binding are untouched,
    and the caller receives the exact paths plus a retry action.  The range is
    explicitly not complete; an operator must confirm the replacement before a
    new plan can be prepared.
    """

    def __init__(self, conflicts: tuple[ArchivePathConfirmation, ...]):
        if not conflicts or not all(
                isinstance(item, ArchivePathConfirmation) for item in conflicts):
            raise TypeError("Expected one or more path confirmations")
        self.conflicts = tuple(conflicts)
        self.scope_complete = False
        super().__init__(
            "%d path(s) require replacement confirmation; the scope is not complete: %s"
            % (len(self.conflicts), ", ".join(item.relative_path for item in self.conflicts))
        )


class ArchivePlanEntryLimitError(ArchivePlanStoreError):
    """The prepared plan has more paths than the explicit entry budget."""

    def __init__(self, batch_id: str, entry_count: int):
        self.batch_id = batch_id
        self.entry_count = entry_count
        super().__init__(
            f"Archive plan for {batch_id} has {entry_count} entries; "
            f"the bounded entry budget is {ARCHIVE_PLAN_MAX_ENTRIES}"
        )


@dataclass(frozen=True, slots=True)
class PlanSegment:
    index: int
    payload: str
    byte_length: int


@dataclass(frozen=True, slots=True)
class PreparedArchivePlan:
    """A frozen, bounded plan ready for one transaction or one queue command."""

    batch_id: str
    inventory: ArchiveInventory
    capacity: CapacityPlan
    scope_json: str
    scope_receipt_json: str
    segments: tuple[PlanSegment, ...]
    capacity_json: str
    header_json: str
    payload_bytes: int

    def __post_init__(self):
        if not isinstance(self.batch_id, str) or _BATCH_ID.fullmatch(self.batch_id) is None:
            raise ArchivePlanStoreError("Invalid prepared batch ID")
        if not isinstance(self.inventory, ArchiveInventory) or not isinstance(self.capacity, CapacityPlan):
            raise ArchivePlanStoreError("Prepared plan lacks inventory or capacity")
        if not isinstance(self.scope_json, str):
            raise ArchivePlanStoreError("Prepared plan scope is not text")
        try:
            if not isinstance(json.loads(self.scope_json), dict):
                raise ValueError
        except (TypeError, ValueError, RecursionError) as exc:
            raise ArchivePlanStoreError("Prepared plan scope is not a JSON object") from exc
        if (
            type(self.payload_bytes) is not int
            or not 0 <= self.payload_bytes <= ARCHIVE_PLAN_MAX_BYTES
        ):
            raise ArchivePlanStoreError("Prepared plan payload size is out of bounds")
        if not isinstance(self.scope_receipt_json, str):
            raise ArchivePlanStoreError("Prepared plan receipt is required text")
        try:
            receipt = ScopeReceipt.from_json(self.scope_receipt_json)
            bound_receipt_json = _receipt_json(receipt, self.inventory)
            if bound_receipt_json != self.scope_receipt_json:
                raise ArchivePlanStoreError("Prepared plan receipt is not content-bound")
        except (ContractError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise ArchivePlanStoreError("Prepared plan receipt is invalid") from exc
        if not isinstance(self.segments, tuple) or not self.segments:
            raise ArchivePlanStoreError("Prepared plan has no segments")
        total = len(self.header_json.encode("utf-8")) + len(self.capacity_json.encode("utf-8"))
        for index, segment in enumerate(self.segments):
            if not isinstance(segment, PlanSegment) or segment.index != index:
                raise ArchivePlanStoreError("Prepared plan segments are not contiguous")
            if not isinstance(segment.payload, str) or len(segment.payload.encode("utf-8")) != segment.byte_length:
                raise ArchivePlanStoreError("Prepared plan segment length differs")
            total += segment.byte_length
        if total != self.payload_bytes:
            raise ArchivePlanStoreError("Prepared plan payload size differs from its parts")

    @property
    def scope(self) -> dict[str, object]:
        return json.loads(self.scope_json)


@dataclass(frozen=True, slots=True)
class StoredPackagePlan:
    """An index row only; planned rows never assert physical package bytes."""

    package_id: str
    batch_id: str
    package_revision: int
    format: str
    tool_version: str | None
    key_ref: str | None
    size_bytes: int | None
    sha256: str | None
    state: str


@dataclass(frozen=True, slots=True)
class StoredArchivePlan:
    """One complete immutable plan; an absent or malformed batch is an error."""

    batch_id: str
    inventory: ArchiveInventory
    capacity: CapacityPlan
    scope: dict[str, object]
    created_at: str
    packages: tuple[StoredPackagePlan, ...]
    scope_receipt: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ArchivePlanSaveResult:
    plan: StoredArchivePlan
    replayed: bool


def _exact(model: type, value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ContractError("Expected " + label)
    expected = {field.name for field in fields(model)}
    if set(value) != expected:
        raise ContractError("Unexpected or missing fields in " + label)
    return value


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ContractError("Invalid " + label)
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ContractError("Invalid " + label)
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContractError("Invalid " + label)
    return tuple(value)


def _optional_integer(value: object, label: str) -> int | None:
    return None if value is None else _integer(value, label)


def _content(value: object) -> ContentVersion:
    return ContentVersion(**_exact(ContentVersion, value, "archive content"))


def _path(value: object) -> PathRef:
    return PathRef(**_exact(PathRef, value, "archive path"))


def _asset(value: object) -> InventoryAsset:
    raw = _exact(InventoryAsset, value, "inventory asset")
    physical = raw["physical_identity"]
    if physical is not None:
        if not isinstance(physical, dict) or set(physical) != {"device", "file_index"}:
            raise ContractError("Invalid inventory physical identity")
        physical = FileIdentity(physical["device"], physical["file_index"])
    asset = InventoryAsset(
        content=_content(raw["content"]),
        path=_path(raw["path"]),
        work_id=raw["work_id"],
        page_index=raw["page_index"],
        path_key=raw["path_key"],
        content_key=raw["content_key"],
        physical_identity=physical,
    )
    if _TOKEN.fullmatch(asset.work_id) is None:
        raise ContractError("Invalid artwork ID")
    if asset.page_index is not None:
        _integer(asset.page_index, "page index")
        if asset.page_index > 9999:
            raise ContractError("Invalid page index")
    expected_content_key = (
        None if asset.content.sha256 is None
        else asset.content.sha256 + ":" + str(asset.content.size_bytes)
    )
    if (asset.path_key != asset.path.identity_key
            or asset.content_key != expected_content_key):
        raise ContractError("Inventory asset identity differs from its facts")
    return asset


def _issue(value: object) -> InventoryIssue:
    raw = _exact(InventoryIssue, value, "inventory issue")
    for name in ("message",):
        if not isinstance(raw[name], str) or not raw[name]:
            raise ContractError("Invalid issue " + name)
    for name in ("path_key", "path", "asset_id", "work_id"):
        if raw[name] is not None and not isinstance(raw[name], str):
            raise ContractError("Invalid issue " + name)
    if raw["page_index"] is not None:
        _integer(raw["page_index"], "issue page index")
    return InventoryIssue(
        code=InventoryIssueCode(raw["code"]),
        message=raw["message"],
        path_key=raw["path_key"],
        path=raw["path"],
        asset_id=raw["asset_id"],
        work_id=raw["work_id"],
        page_index=raw["page_index"],
    )


def _component(value: object) -> CapacityComponent:
    raw = _exact(CapacityComponent, value, "capacity component")
    if not isinstance(raw["name"], str) or not raw["name"]:
        raise ContractError("Invalid capacity component name")
    return CapacityComponent(raw["name"], _integer(raw["estimated_bytes"], "component bytes"))


def _phase(value: object) -> CapacityPhase:
    raw = _exact(CapacityPhase, value, "capacity phase")
    if not isinstance(raw["phase"], str) or not raw["phase"]:
        raise ContractError("Invalid capacity phase name")
    if not isinstance(raw["components"], list):
        raise ContractError("Invalid capacity components")
    return CapacityPhase(
        raw["phase"],
        _integer(raw["additional_bytes"], "phase bytes"),
        tuple(_component(item) for item in raw["components"]),
    )


def _inventory(
    raw_value: object, expected_batch_id: str | None = None, *,
    validate_consistency: bool = True,
) -> ArchiveInventory:
    raw = _exact(ArchiveInventory, raw_value, "archive inventory")
    if raw.get("schema_version") != ARCHIVE_INVENTORY_SCHEMA_VERSION or type(raw.get("schema_version")) is not int:
        raise ArchivePlanStoreError("Unsupported archive inventory schema")
    if not isinstance(raw["assets"], list) or not isinstance(raw["packages"], list):
        raise ContractError("Invalid inventory asset/package list")
    if not isinstance(raw["issues"], list):
        raise ContractError("Invalid inventory issue list")
    assets = tuple(_asset(item) for item in raw["assets"])
    asset_by_path = {asset.path_key: asset for asset in assets}
    if len(asset_by_path) != len(assets):
        raise ContractError("Duplicate inventory path identity")
    packages = []
    for item in raw["packages"]:
        package = _exact(PackagePlan, item, "package plan")
        if not isinstance(package["members"], list):
            raise ContractError("Invalid package members")
        members = tuple(_asset(member) for member in package["members"])
        if any(asset_by_path.get(member.path_key) != member for member in members):
            raise ContractError("Package member differs from the inventory asset")
        if len({member.path_key for member in members}) != len(members):
            raise ContractError("Duplicate package member")
        packages.append(PackagePlan(
            package_id=package["package_id"],
            sequence=_integer(package["sequence"], "package sequence", 1),
            payload_bytes=_integer(package["payload_bytes"], "package payload"),
            work_ids=_strings(package["work_ids"], "package works"),
            members=members,
            oversized_single_file=_boolean(package["oversized_single_file"], "oversized marker"),
            exception_codes=_strings(package["exception_codes"], "package exceptions"),
        ))
    inventory = ArchiveInventory(
        batch_id=raw["batch_id"],
        status=InventoryStatus(raw["status"]),
        scope_complete=_boolean(raw["scope_complete"], "scope completeness"),
        input_entry_count=_integer(raw["input_entry_count"], "input entry count"),
        duplicate_entry_count=_integer(raw["duplicate_entry_count"], "duplicate entry count"),
        asset_count=_integer(raw["asset_count"], "asset count"),
        path_count=_integer(raw["path_count"], "path count"),
        unique_content_count=_integer(raw["unique_content_count"], "content count"),
        member_payload_bytes=_integer(raw["member_payload_bytes"], "member bytes"),
        physical_member_payload_bytes=_integer(
            raw["physical_member_payload_bytes"], "physical member bytes",
        ),
        unique_content_bytes=_integer(raw["unique_content_bytes"], "unique bytes"),
        assets=assets,
        packages=tuple(packages),
        issues=tuple(_issue(item) for item in raw["issues"]),
        split_work_ids=_strings(raw["split_work_ids"], "split works"),
        adjustment_codes=_strings(raw["adjustment_codes"], "inventory adjustments"),
        target_package_bytes=_integer(raw["target_package_bytes"], "package target", 1),
        batch_budget_bytes=_integer(raw["batch_budget_bytes"], "batch budget"),
        schema_version=raw["schema_version"],
    )
    if (_BATCH_ID.fullmatch(inventory.batch_id) is None
            or (expected_batch_id is not None and inventory.batch_id != expected_batch_id)):
        raise ContractError("Archive plan batch identity mismatch")
    if validate_consistency:
        _validate_inventory(inventory)
    return inventory


def _validate_inventory(inventory: ArchiveInventory) -> None:
    if inventory.scope_complete != (not inventory.issues):
        raise ContractError("Scope completeness differs from inventory issues")
    expected_status = InventoryStatus.NEEDS_ADJUSTMENT if inventory.adjustment_codes else InventoryStatus.READY
    if inventory.status is not expected_status:
        raise ContractError("Inventory status differs from its adjustments")
    if not inventory.scope_complete:
        # Valid neighbours may remain visible, but never contribute to a partial plan.
        visible_contents = {asset.content_key: asset.content.size_bytes for asset in inventory.assets}
        if (inventory.packages or inventory.member_payload_bytes
                or inventory.physical_member_payload_bytes
                or "inventory_rejected_items" not in inventory.adjustment_codes):
            raise ContractError("Incomplete scope must not contain a package plan")
        if (
            inventory.path_count != len(inventory.assets)
            or inventory.asset_count != len({
                (asset.content.asset_id, asset.content.revision) for asset in inventory.assets
            })
            or inventory.unique_content_count != len(visible_contents)
            or inventory.unique_content_bytes != sum(visible_contents.values())
        ):
            raise ContractError("Incomplete scope counts differ from its visible assets")
        return
    if inventory.input_entry_count != inventory.path_count + inventory.duplicate_entry_count:
        raise ContractError("Inventory input and duplicate counts differ")
    by_work: dict[str, list[ArchiveEntry]] = {}
    for asset in inventory.assets:
        by_work.setdefault(asset.work_id, []).append(
            ArchiveEntry(asset.content, asset.path, asset.page_index, asset.physical_identity)
        )
    rebuilt = build_archive_inventory(
        (ArtworkGroup(work_id, tuple(entries)) for work_id, entries in by_work.items()),
        batch_id=inventory.batch_id,
        policy=InventoryPolicy(
            target_package_bytes=inventory.target_package_bytes,
            batch_budget_bytes=inventory.batch_budget_bytes,
        ),
    )
    expected = replace(
        rebuilt,
        input_entry_count=inventory.input_entry_count,
        duplicate_entry_count=inventory.duplicate_entry_count,
    )
    if inventory != expected:
        raise ContractError("Inventory totals/assets/packages are not internally consistent")


def _capacity(raw_value: object, inventory: ArchiveInventory) -> CapacityPlan:
    raw = _exact(CapacityPlan, raw_value, "capacity plan")
    if raw.get("schema_version") != CAPACITY_PLAN_SCHEMA_VERSION or type(raw.get("schema_version")) is not int:
        raise ArchivePlanStoreError("Unsupported capacity plan schema")
    if not isinstance(raw["peak_components"], list) or not isinstance(raw["phases"], list):
        raise ContractError("Invalid capacity component list")
    capacity = CapacityPlan(
        batch_id=raw["batch_id"],
        inventory_schema_version=_integer(raw["inventory_schema_version"], "inventory schema"),
        status=CapacityStatus(raw["status"]),
        scope_complete=_boolean(raw["scope_complete"], "capacity scope completeness"),
        staging_mode=raw["staging_mode"],
        source_member_bytes_excluded=_integer(raw["source_member_bytes_excluded"], "source bytes"),
        recovery_estimate_basis_points=_integer(raw["recovery_estimate_basis_points"], "recovery estimate"),
        peak_additional_bytes=_optional_integer(raw["peak_additional_bytes"], "peak bytes"),
        available_additional_bytes=_optional_integer(raw["available_additional_bytes"], "available bytes"),
        shortfall_bytes=_optional_integer(raw["shortfall_bytes"], "shortfall bytes"),
        peak_components=tuple(_component(item) for item in raw["peak_components"]),
        phases=tuple(_phase(item) for item in raw["phases"]),
        adjustment_codes=_strings(raw["adjustment_codes"], "capacity adjustments"),
        assumptions=_strings(raw["assumptions"], "capacity assumptions"),
        schema_version=raw["schema_version"],
    )
    if capacity.batch_id != inventory.batch_id:
        raise ContractError("Capacity and inventory batch identities differ")
    expected = plan_archive_capacity(
        inventory,
        available_additional_bytes=capacity.available_additional_bytes,
        policy=CapacityPolicy(
            staging_mode=capacity.staging_mode,
            recovery_estimate_basis_points=capacity.recovery_estimate_basis_points,
            control_bytes=next((item.estimated_bytes for item in capacity.peak_components
                                if item.name == "control"), 0),
            safety_margin_bytes=next((item.estimated_bytes for item in capacity.peak_components
                                      if item.name == "safety_margin"), 0),
            schema_version=capacity.schema_version,
        ),
    )
    if capacity != expected:
        raise ContractError("Capacity envelope differs from its inventory and policy")
    return capacity


def _strict_object(value: object, label: str) -> dict:
    if not isinstance(value, str):
        raise ArchivePlanStoreError("Stored " + label + " is not JSON text")
    try:
        return json.loads(normalize_json(value, object_only=True))
    except (ContractError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ArchivePlanStoreError("Invalid stored " + label) from exc


def _check_scope_root_revision(scope: dict[str, object], expected: int) -> None:
    """Refuse a scope header whose declared root generation differs from its receipt."""
    candidates = []
    if "root_revision" in scope:
        candidates.append(scope["root_revision"])
    root = scope.get("root")
    if isinstance(root, dict) and "root_revision" in root:
        candidates.append(root["root_revision"])
    for value in candidates:
        if type(value) is not int or value != expected:
            raise ArchivePlanStoreError("Archive plan scope root generation differs from its receipt")


def _canonical_scope(scope: dict[str, object] | None) -> str:
    if scope is None:
        scope = {"kind": "explicit_inventory"}
    if not isinstance(scope, dict):
        raise ArchivePlanStoreError("Archive plan scope must be a JSON object")
    try:
        value = json.loads(normalize_json(canonical_json(scope), object_only=True))
    except (ContractError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ArchivePlanStoreError("Archive plan scope is not canonical JSON") from exc
    if not isinstance(value, dict):
        raise ArchivePlanStoreError("Archive plan scope must be a JSON object")
    return canonical_json(value)


def _scope_entries(inventory: ArchiveInventory) -> tuple[ArchiveEntry, ...]:
    """Rebuild the scope-order entries for receipt comparison.

    `ScopeEnumerator` orders by its display key (NFC casefold) and then the raw
    relative path.  Plan assets are stored by artwork/page order, so they must be
    re-sorted here rather than assumed to be in page-consumption order.
    """
    entries = (
        ArchiveEntry(asset.content, asset.path, asset.page_index, asset.physical_identity)
        for asset in inventory.assets
    )
    return tuple(sorted(entries, key=lambda entry: (
        unicodedata.normalize("NFC", entry.path.relative_path).casefold(),
        entry.path.relative_path,
    )))


def _receipt_json(
    receipt: ScopeReceipt, inventory: ArchiveInventory, *,
    root_revision: int | None = None, bind_content: bool = True,
) -> str:
    if not isinstance(receipt, ScopeReceipt):
        raise ArchivePlanStoreError("Expected a ScopeReceipt")
    try:
        receipt.verify()
    except ContractError as exc:
        raise ArchivePlanStoreError("ScopeReceipt failed its digest verification") from exc
    if root_revision is not None:
        _integer(root_revision, "plan root mapping revision")
        if receipt.root_revision != root_revision:
            raise ArchivePlanStoreError(
                "ScopeReceipt root mapping revision differs from the plan"
            )
    # Completeness is bidirectional: a sub-batch receipt can never authorize a
    # whole inventory, and a complete snapshot receipt cannot hide an inventory
    # that still has rejected items.  A partial inventory may carry a partial
    # snapshot receipt, but a sub-batch still has to consume its own pages fully.
    if inventory.scope_complete:
        if (not receipt.consumption_complete
                or receipt.selection_kind is not ScopeSelectionKind.SNAPSHOT
                or not receipt.scope_complete):
            raise ArchivePlanStoreError(
                "A complete inventory requires a complete whole-snapshot receipt")
        if receipt.entries_in_snapshot != len(inventory.assets):
            raise ArchivePlanStoreError(
                "Complete inventory path count differs from the scope snapshot")
        if (inventory.input_entry_count
                != receipt.entries_in_snapshot + inventory.duplicate_entry_count):
            raise ArchivePlanStoreError(
                "Inventory input/duplicate counts disagree with the scope snapshot")
    else:
        if receipt.scope_complete:
            raise ArchivePlanStoreError(
                "An incomplete inventory cannot claim a complete scope")
        if (receipt.selection_kind is ScopeSelectionKind.SUB_BATCH
                and not receipt.consumption_complete):
            raise ArchivePlanStoreError(
                "A sub-batch receipt is not a complete page consumption")
    for asset in inventory.assets:
        if (asset.path.kind != receipt.kind
                or asset.path.root_id.casefold() != receipt.root_id.casefold()):
            raise ArchivePlanStoreError(
                "ScopeReceipt root does not match the plan paths"
            )
    entries = _scope_entries(inventory)
    if receipt.consumed_entries != len(entries):
        raise ArchivePlanStoreError("ScopeReceipt entry count differs from the plan")
    if not receipt.matches_entries(entries):
        if not receipt.matches_physical_entries(entries):
            raise ArchivePlanStoreError("ScopeReceipt entries differ from the plan")
        if not bind_content:
            raise ArchivePlanStoreError("Stored scope receipt is not content-bound")
        try:
            receipt = receipt.bind_content(entries)
        except ContractError as exc:
            raise ArchivePlanStoreError("ScopeReceipt entries differ from the plan") from exc
        if not receipt.matches_entries(entries):
            raise ArchivePlanStoreError("ScopeReceipt entries differ from the plan")
    try:
        return receipt.to_json()
    except (ContractError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ArchivePlanStoreError("ScopeReceipt is not canonical JSON") from exc


def _inventory_segments(
    inventory: ArchiveInventory, raw_inventory: dict | None = None,
) -> tuple[str, tuple[PlanSegment, ...]]:
    try:
        text = json.dumps(
            inventory.to_dict() if raw_inventory is None else raw_inventory,
            ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ArchivePlanStoreError("Archive plan inventory is not JSON serializable") from exc
    encoded_size = len(text.encode("utf-8"))
    if encoded_size > ARCHIVE_PLAN_MAX_BYTES:
        raise ArchivePlanTooLargeError(inventory.batch_id, encoded_size)
    segments = []
    for index, start in enumerate(range(0, len(text), _ARCHIVE_PLAN_SEGMENT_CHARS)):
        payload = text[start:start + _ARCHIVE_PLAN_SEGMENT_CHARS]
        byte_length = len(payload.encode("utf-8"))
        if byte_length > ARCHIVE_PLAN_SEGMENT_LIMIT:
            raise ArchivePlanStoreError("Archive plan segment exceeds its byte bound")
        segments.append(PlanSegment(index, payload, byte_length))
    return text, tuple(segments)


def prepare_archive_plan(
    inventory: ArchiveInventory,
    capacity: CapacityPlan,
    *,
    scope: dict[str, object] | None = None,
    receipt: ScopeReceipt | None = None,
    root_revision: int | None = None,
) -> PreparedArchivePlan:
    """Freeze and bound a plan without opening a database or reading image bytes.

    When a ScopeReceipt is supplied it must prove complete page consumption;
    its v2 entry facts, including physical identity, are compared with this
    exact plan before any write.  ``root_revision`` is the current root mapping
    generation: an old receipt from another generation is refused rather than
    silently replaying a stale candidate set.  A plan without a complete
    ScopeReceipt is never prepared.
    """
    if not isinstance(inventory, ArchiveInventory) or not isinstance(capacity, CapacityPlan):
        raise ArchivePlanStoreError("Expected ArchiveInventory and CapacityPlan")
    if inventory.batch_id != capacity.batch_id:
        raise ArchivePlanStoreError("Inventory and capacity batch IDs differ")
    entry_count = max(inventory.input_entry_count, len(inventory.assets))
    if entry_count > ARCHIVE_PLAN_MAX_ENTRIES:
        raise ArchivePlanEntryLimitError(inventory.batch_id, entry_count)
    if receipt is None:
        raise ArchivePlanStoreError("Archive plan requires a completed ScopeReceipt")
    scope_json = _canonical_scope(scope)
    try:
        _validate_inventory(inventory)
        raw_inventory = inventory.to_dict()
        checked_inventory = _inventory(
            raw_inventory, inventory.batch_id,
            validate_consistency=False,
        )
        inventory_json, segments = _inventory_segments(inventory, raw_inventory)
        receipt_json = None if receipt is None else _receipt_json(
            receipt, checked_inventory, root_revision=root_revision)
        if receipt is not None:
            _check_scope_root_revision(json.loads(scope_json), receipt.root_revision)
        capacity_json = canonical_json(capacity.to_dict())
        checked_capacity = _capacity(json.loads(capacity_json), checked_inventory)
        if checked_inventory != inventory or checked_capacity != capacity:
            raise ContractError("Archive plan does not survive strict serialization")
    except ArchivePlanTooLargeError:
        raise
    except ArchivePlanStoreError:
        raise
    except (ContractError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ArchivePlanStoreError("Invalid archive plan input") from exc
    digest = hashlib.sha256(inventory_json.encode("utf-8")).hexdigest()
    header_json = canonical_json({
        "format": _ARCHIVE_PLAN_FORMAT,
        "plan_schema_version": _ARCHIVE_PLAN_VERSION,
        "batch_id": inventory.batch_id,
        "scope": json.loads(scope_json),
        "inventory_sha256": digest,
        "inventory_bytes": len(inventory_json.encode("utf-8")),
        "segment_count": len(segments),
        "root_revision": None if receipt is None else receipt.root_revision,
        "scope_receipt": None if receipt_json is None else json.loads(receipt_json),
        "asset_sources_sha256": "0" * 64,
    })
    payload_bytes = (
        len(header_json.encode("utf-8")) + len(capacity_json.encode("utf-8"))
        + sum(segment.byte_length for segment in segments)
    )
    if payload_bytes > ARCHIVE_PLAN_MAX_BYTES:
        raise ArchivePlanTooLargeError(inventory.batch_id, payload_bytes)
    return PreparedArchivePlan(
        batch_id=inventory.batch_id,
        inventory=inventory,
        capacity=capacity,
        scope_json=scope_json,
        scope_receipt_json=receipt_json,
        segments=segments,
        capacity_json=capacity_json,
        header_json=header_json,
        payload_bytes=payload_bytes,
    )


def _decode_scope(
    conn: sqlite3.Connection, value: object, expected_batch_id: str,
) -> tuple[ArchiveInventory, dict[str, object], dict[str, object] | None, str]:

    raw = _strict_object(value, "scope_json")
    if raw.get("format") != _ARCHIVE_PLAN_FORMAT:
        raise ArchivePlanStoreError("Stored scope JSON is not an archive plan")
    if (
        raw.get("plan_schema_version") != _ARCHIVE_PLAN_VERSION
        or type(raw.get("plan_schema_version")) is not int
    ):
        raise ArchivePlanStoreError("Unsupported stored archive plan version")
    expected_keys = {
        "format", "plan_schema_version", "batch_id", "scope",
        "inventory_sha256", "inventory_bytes", "segment_count", "root_revision",
        "scope_receipt", "asset_sources_sha256",
    }
    if set(raw) != expected_keys:
        raise ArchivePlanStoreError("Stored scope JSON has missing or unknown fields")
    if (
        raw["batch_id"] != expected_batch_id
        or not isinstance(raw["scope"], dict)
        or not isinstance(raw["inventory_sha256"], str)
        or type(raw["inventory_bytes"]) is not int
        or type(raw["segment_count"]) is not int
        or not 1 <= raw["segment_count"] <= ARCHIVE_PLAN_MAX_SEGMENTS
        or (raw["root_revision"] is not None and type(raw["root_revision"]) is not int)
        or type(raw["inventory_bytes"]) is not int
        or not 0 <= raw["inventory_bytes"] <= ARCHIVE_PLAN_MAX_BYTES
    ):
        raise ArchivePlanStoreError("Stored scope identity or bound is invalid")
    if not isinstance(raw["scope_receipt"], dict):
        raise ArchivePlanStoreError("Stored archive plan requires a scope receipt")
    try:
        inventory_json = read_plan_segments(
            conn, expected_batch_id, raw["segment_count"],
            raw["inventory_bytes"], raw["inventory_sha256"],
        )
    except ArchiveSchemaError as exc:
        raise ArchivePlanStoreError(str(exc)) from exc
    try:
        inventory = _inventory(
            json.loads(normalize_json(inventory_json, object_only=True)),
            expected_batch_id,
        )
    except (ValueError, TypeError, KeyError, ContractError) as exc:
        raise ArchivePlanStoreError("Stored plan inventory is invalid") from exc
    if max(inventory.input_entry_count, len(inventory.assets)) > ARCHIVE_PLAN_MAX_ENTRIES:
        raise ArchivePlanStoreError("Stored plan exceeds the entry budget")
    scope_receipt = raw["scope_receipt"]
    if scope_receipt is None:
        raise ArchivePlanStoreError("Stored archive plan requires a scope receipt")
    try:
        receipt = ScopeReceipt.from_json(canonical_json(scope_receipt))
        if raw["root_revision"] != receipt.root_revision:
            raise ArchivePlanStoreError("Stored plan root generation differs from its receipt")
        _check_scope_root_revision(raw["scope"], receipt.root_revision)
        _receipt_json(receipt, inventory, bind_content=False)
    except (ContractError, TypeError, ValueError, UnicodeError) as exc:
        raise ArchivePlanStoreError("Stored scope receipt is invalid") from exc
    return inventory, raw["scope"], scope_receipt, raw["asset_sources_sha256"]


def _expected_packages(inventory: ArchiveInventory) -> tuple[StoredPackagePlan, ...]:
    return tuple(
        StoredPackagePlan(
            package.package_id, inventory.batch_id, 1, "7z",
            None, None, None, None, "planned",
        )
        for package in inventory.packages
    )


def _packages(conn: sqlite3.Connection, batch_id: str) -> tuple[StoredPackagePlan, ...]:
    rows = conn.execute(
        """SELECT package_id,batch_id,package_revision,format,tool_version,key_ref,
                  size_bytes,sha256,state FROM archive_packages
           WHERE batch_id=? ORDER BY package_id""", (batch_id,),
    ).fetchall()
    return tuple(StoredPackagePlan(*row) for row in rows)


def _check_package_index(
    stored: tuple[StoredPackagePlan, ...], expected: tuple[StoredPackagePlan, ...],
) -> None:
    if any(package.state == "sealed" for package in stored):
        raise ArchivePlanConflictError("A sealed package cannot be adapted as a planned package")
    if sorted(stored, key=lambda item: item.package_id) != sorted(expected, key=lambda item: item.package_id):
        raise ArchivePlanStoreError("Package index differs from the immutable plan JSON")


def _check_assets(
    conn: sqlite3.Connection, inventory: ArchiveInventory,
    asset_sources_sha256: object,
) -> None:
    if (not isinstance(asset_sources_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", asset_sources_sha256) is None):
        raise ArchivePlanStoreError("Stored plan asset source digest is invalid")
    source_facts = []
    for asset in inventory.assets:
        content = asset.content
        expected = (
            _content_key(content), content.asset_id, content.revision, asset.path_key,
            canonical_json({
                "kind": asset.path.kind,
                "root_id": asset.path.root_id,
                "relative_path": asset.path.relative_path,
            }),
            content.size_bytes, content.sha256,
        )
        row = conn.execute(
            """SELECT content_key,asset_id,revision,path_key,path_json,size_bytes,sha256,
                      source_kind,artifact_id FROM archive_assets
               WHERE asset_id=? AND revision=? AND path_key=?""",
            (content.asset_id, content.revision, asset.path_key),
        ).fetchone()
        if row is None or tuple(row[:7]) != expected:
            raise ArchivePlanStoreError("Content/path facts differ from the immutable plan JSON")
        source_facts.append([asset.path_key, row[7]])
        content_row = conn.execute(
            "SELECT content_key,size_bytes,sha256 FROM archive_contents "
            "WHERE asset_id=? AND revision=?",
            (content.asset_id, content.revision),
        ).fetchone()
        if content_row is None or tuple(content_row) != (
            expected[0], content.size_bytes, content.sha256,
        ):
            raise ArchivePlanStoreError("Referenced content facts differ from the plan")
        source_kind, artifact_id = row[7], row[8]
        if source_kind == "download_receipt":
            receipt = conn.execute(
                "SELECT output_ref_json,size,sha256,verification FROM artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if artifact_id is None or receipt is None:
                raise ArchivePlanStoreError("Download receipt asset has no exact artifact")
            try:
                receipt_path = PathRef(**json.loads(
                    normalize_json(receipt[0], object_only=True)))
            except (KeyError, TypeError, ValueError) as exc:
                raise ArchivePlanStoreError("Download receipt path is invalid") from exc
            if (
                receipt_path.identity_key != asset.path_key
                or receipt[1] != content.size_bytes
                or receipt[2] != content.sha256
                or receipt[3] != "structure_checked"
            ):
                raise ArchivePlanStoreError("Download receipt does not prove this exact content")
        elif artifact_id is not None or source_kind not in {"observed_existing", "user_selected"}:
            raise ArchivePlanStoreError("Asset source or artifact binding is invalid")
    if hashlib.sha256(canonical_json(sorted(source_facts)).encode("utf-8")).hexdigest() \
            != asset_sources_sha256:
        raise ArchivePlanStoreError("Asset source facts differ from the immutable plan JSON")


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ContractError("Invalid archive plan timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("Archive plan timestamp requires a timezone")
    return value


def _load_in_transaction(conn: sqlite3.Connection, batch_id: str) -> StoredArchivePlan:
    row = conn.execute(
        """SELECT protocol_version,scope_json,capacity_json,state,pause_reason,created_at
           FROM archive_batches WHERE batch_id=?""", (batch_id,),
    ).fetchone()
    if row is None:
        raise ArchivePlanStoreError("Archive plan batch not found")
    protocol, scope_json, capacity_json, state, pause_reason, created_at = row
    if protocol != ARCHIVE_PROTOCOL_VERSION or state != "planned" or pause_reason is not None:
        raise ArchivePlanConflictError("Existing batch is not an unchanged planned archive batch")
    inventory, scope, scope_receipt, asset_sources_sha256 = _decode_scope(
        conn, scope_json, batch_id)
    capacity = _capacity(_strict_object(capacity_json, "capacity_json"), inventory)
    stored_packages = _packages(conn, batch_id)
    _check_package_index(stored_packages, _expected_packages(inventory))
    _check_assets(conn, inventory, asset_sources_sha256)
    return StoredArchivePlan(
        batch_id, inventory, capacity, scope, _timestamp(created_at), stored_packages,
        scope_receipt,
    )


def _require_connection(conn: object) -> sqlite3.Connection:
    if not isinstance(conn, sqlite3.Connection):
        raise ArchivePlanStoreError("An explicit sqlite3 connection is required; paths are never opened")
    return conn


def _require_schema3(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "schema_meta" not in tables or not ARCHIVE_TABLES <= tables:
        raise ArchivePlanStoreError("Archive plan persistence requires the complete schema 3")
    metadata = dict(conn.execute('SELECT "key","value" FROM schema_meta'))
    if metadata.get("format") != "jpgpng-library":
        raise ArchivePlanStoreError("Connection belongs to a different product")
    if metadata.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
        raise ArchivePlanStoreError("Unsupported archive schema version; exactly schema 3 is required")
    _check_existing_objects(conn)


@contextmanager
def _deferred_writes(conn: sqlite3.Connection):
    """Restore writable PRAGMAs without opening a post-read transaction."""
    isolation_level = conn.isolation_level
    conn.isolation_level = None
    try:
        yield conn.execute
    finally:
        conn.isolation_level = isolation_level


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def _replacement_conflicts(
    conn: sqlite3.Connection, inventory: ArchiveInventory,
) -> tuple[ArchivePathConfirmation, ...]:
    """Find paths whose current owner is a different logical asset.

    This runs inside the writer transaction before the new batch row is
    inserted.  A same-asset revision advance is allowed; a different asset at
    the same path is a replacement the caller did not confirm.
    """
    conflicts = []
    for asset in inventory.assets:
        binding = conn.execute(
            "SELECT asset_id,revision FROM archive_path_bindings WHERE path_key=?",
            (asset.path_key,),
        ).fetchone()
        if binding is not None and binding[0] != asset.content.asset_id:
            conflicts.append(ArchivePathConfirmation(
                relative_path=asset.path.relative_path,
                path_key=asset.path_key,
                existing_asset_id=binding[0],
                existing_revision=binding[1],
                proposed_asset_id=asset.content.asset_id,
                proposed_revision=asset.content.revision,
                reason="physical_object_belongs_to_another_asset",
            ))
            continue
        owners = [row[0] for row in conn.execute(
            "SELECT DISTINCT asset_id FROM archive_assets WHERE path_key=?",
            (asset.path_key,),
        )]
        foreign = [owner for owner in owners if owner != asset.content.asset_id]
        if foreign:
            conflicts.append(ArchivePathConfirmation(
                relative_path=asset.path.relative_path,
                path_key=asset.path_key,
                existing_asset_id=foreign[0],
                existing_revision=None,
                proposed_asset_id=asset.content.asset_id,
                proposed_revision=asset.content.revision,
                reason="path_owned_by_another_asset",
                next_action="confirm_or_choose_new_path",
            ))
    return tuple(conflicts)


def save_prepared_plan_in_transaction(
    conn: sqlite3.Connection, prepared: PreparedArchivePlan,
) -> ArchivePlanSaveResult:
    """Execute only SQL inside a transaction owned by the caller or the wrapper."""
    conn = _require_connection(conn)
    if not isinstance(prepared, PreparedArchivePlan):
        raise ArchivePlanStoreError("Expected a prepared archive plan")
    if not conn.in_transaction:
        raise ArchivePlanStoreError("Prepared plan save requires a caller-owned transaction")
    _require_schema3(conn)
    existing = conn.execute(
        "SELECT 1 FROM archive_batches WHERE batch_id=?", (prepared.batch_id,),
    ).fetchone()
    if existing is not None:
        stored = _load_in_transaction(conn, prepared.batch_id)
        stored_receipt = (
            None if stored.scope_receipt is None else canonical_json(stored.scope_receipt)
        )
        if (
            stored.inventory != prepared.inventory
            or stored.capacity != prepared.capacity
            or stored.scope != prepared.scope
            or stored_receipt != prepared.scope_receipt_json
        ):
            raise ArchivePlanConflictError("Batch ID already contains a different immutable plan")
        return ArchivePlanSaveResult(stored, replayed=True)

    conflicts = _replacement_conflicts(conn, prepared.inventory)
    if conflicts:
        raise ArchiveConfirmationRequiredError(conflicts)

    expected_packages = _expected_packages(prepared.inventory)
    package_ids = [package.package_id for package in expected_packages]
    if package_ids:
        placeholders = ",".join("?" for _ in package_ids)
        collision = conn.execute(
            "SELECT package_id FROM archive_packages WHERE package_id IN (" + placeholders + ") LIMIT 1",
            package_ids,
        ).fetchone()
        if collision is not None:
            raise ArchivePlanConflictError("A historical package ID cannot be overwritten or reused")

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO archive_batches (
               batch_id,protocol_version,scope_json,capacity_json,state,pause_reason,created_at
           ) VALUES (?,?,?,?,'planned',NULL,?)""",
        (
            prepared.batch_id, ARCHIVE_PROTOCOL_VERSION, prepared.header_json,
            prepared.capacity_json, created_at,
        ),
    )
    conn.executemany(
        """INSERT INTO archive_plan_segments (
               batch_id,segment_index,payload,byte_length
           ) VALUES (?,?,?,?)""",
        [
            (prepared.batch_id, segment.index, segment.payload, segment.byte_length)
            for segment in prepared.segments
        ],
    )
    source_facts: list[list[str]] = []
    for asset in prepared.inventory.assets:
        content = asset.content
        existing_asset = conn.execute(
            """SELECT 1 FROM archive_assets
               WHERE asset_id=? AND revision=? AND path_key=? AND content_key=?
                 AND path_json=? AND size_bytes=? AND sha256 IS ?""",
            (
                content.asset_id, content.revision, asset.path_key, _content_key(content),
                canonical_json({
                    "kind": asset.path.kind, "root_id": asset.path.root_id,
                    "relative_path": asset.path.relative_path,
                }),
                content.size_bytes, content.sha256,
            ),
        ).fetchone()
        if existing_asset is None:
            try:
                ensure_archive_asset(
                    conn, content, asset.path, source_kind="user_selected", artifact_id=None,
                )
            except ContractError as exc:
                raise ArchiveContentConflictError(
                    content.asset_id, content.revision, asset.path.relative_path,
                    asset.path_key, str(exc),
                ) from exc
        current_binding = conn.execute(
            "SELECT asset_id,revision FROM archive_path_bindings WHERE path_key=?",
            (asset.path_key,),
        ).fetchone()
        if current_binding is None:
            bind_archive_path(conn, content, asset.path, bound_at=created_at)
        else:
            current_content = conn.execute(
                """SELECT content_key,asset_id,revision FROM archive_assets
                   WHERE asset_id=? AND revision=? AND path_key=?""",
                (current_binding[0], current_binding[1], asset.path_key),
            ).fetchone()
            if current_content is None:
                raise ArchivePlanConflictError("Current path binding has no exact asset")
            new_key = _content_key(content)
            if current_content[0] != new_key:
                if (
                    current_content[1] != content.asset_id
                    or current_content[2] >= content.revision
                ):
                    raise ArchivePlanConflictError(
                        "Path binding cannot move to a different asset or an older revision"
                    )
                bind_archive_path(
                    conn, content, asset.path, bound_at=created_at,
                    expected_content_key=current_content[0],
                )
        source_row = conn.execute(
            """SELECT source_kind FROM archive_assets
               WHERE asset_id=? AND revision=? AND path_key=?""",
            (content.asset_id, content.revision, asset.path_key),
        ).fetchone()
        if source_row is None:
            raise ArchivePlanStoreError("Saved asset has no source fact")
        source_facts.append([asset.path_key, source_row[0]])
    header = json.loads(prepared.header_json)
    header["asset_sources_sha256"] = hashlib.sha256(
        canonical_json(sorted(source_facts)).encode("utf-8")
    ).hexdigest()
    conn.execute(
        "UPDATE archive_batches SET scope_json=? WHERE batch_id=?",
        (canonical_json(header), prepared.batch_id),
    )
    conn.executemany(
        """INSERT INTO archive_packages (
               package_id,batch_id,package_revision,format,tool_version,key_ref,
               size_bytes,sha256,state
           ) VALUES (?,?,?,?,?,?,?,?,?)""",
        [(
            package.package_id, package.batch_id, package.package_revision, package.format,
            package.tool_version, package.key_ref, package.size_bytes, package.sha256, package.state,
        ) for package in expected_packages],
    )
    stored = _load_in_transaction(conn, prepared.batch_id)
    stored_receipt = None if stored.scope_receipt is None else canonical_json(stored.scope_receipt)
    if (
        (stored.inventory, stored.capacity, stored.scope, stored_receipt)
        != (
            prepared.inventory, prepared.capacity, prepared.scope,
            prepared.scope_receipt_json,
        )
    ):
        raise ArchivePlanStoreError("Persisted archive plan failed read-back verification")
    return ArchivePlanSaveResult(stored, replayed=False)


def save_archive_plan(
    conn: sqlite3.Connection,
    inventory: ArchiveInventory,
    capacity: CapacityPlan,
    *,
    scope: dict[str, object] | None = None,
    receipt: ScopeReceipt | None = None,
    root_revision: int | None = None,
) -> ArchivePlanSaveResult:
    """Prepare outside any transaction, then save on this standalone connection."""
    conn = _require_connection(conn)
    prepared = prepare_archive_plan(
        inventory, capacity, scope=scope, receipt=receipt,
        root_revision=root_revision,
    )
    try:
        if conn.in_transaction:
            raise ArchivePlanStoreError("Archive plan save requires ownership of a new transaction")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise ArchivePlanStoreError("Archive plan save requires foreign keys enabled")
        if conn.execute("PRAGMA query_only").fetchone()[0] != 0:
            raise ArchivePlanStoreError("Archive plan save requires a writable explicit connection")
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = save_prepared_plan_in_transaction(conn, prepared)
            conn.execute("COMMIT")
            return result
        except BaseException:
            _rollback(conn)
            raise
    except ArchivePlanStoreError:
        raise
    except (sqlite3.Error, ContractError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise ArchivePlanStoreError("Archive plan save failed; its transaction was rolled back") from exc


def load_archive_plan_in_transaction(
    conn: sqlite3.Connection, batch_id: str,
) -> StoredArchivePlan:
    """Reload one plan inside a caller-owned transaction without nested BEGIN/COMMIT."""
    conn = _require_connection(conn)
    if not isinstance(batch_id, str) or _BATCH_ID.fullmatch(batch_id) is None:
        raise ArchivePlanStoreError("Invalid archive batch ID")
    if not conn.in_transaction:
        raise ArchivePlanStoreError("Archive plan reload requires a caller-owned transaction")
    try:
        _require_schema3(conn)
        return _load_in_transaction(conn, batch_id)
    except ArchivePlanStoreError:
        raise
    except (sqlite3.Error, ContractError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ArchivePlanStoreError("Stored archive plan failed strict validation") from exc


def load_archive_plan(conn: sqlite3.Connection, batch_id: str) -> StoredArchivePlan:
    """Read one fully validated plan without writing or defaulting missing data to empty.

    A read transaction is always rolled back.  query_only and foreign_keys settings are
    restored before returning or raising; the caller retains ownership of the connection.
    """
    conn = _require_connection(conn)
    if not isinstance(batch_id, str) or _BATCH_ID.fullmatch(batch_id) is None:
        raise ArchivePlanStoreError("Invalid archive batch ID")
    try:
        if conn.in_transaction:
            raise ArchivePlanStoreError("Archive plan reload requires no open caller transaction")
        old_foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        old_query_only = conn.execute("PRAGMA query_only").fetchone()[0]
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            try:
                _require_schema3(conn)
                return _load_in_transaction(conn, batch_id)
            except ArchivePlanStoreError:
                raise
            except (sqlite3.Error, ContractError, ValueError, TypeError, KeyError, OverflowError) as exc:
                raise ArchivePlanStoreError("Stored archive plan failed strict validation") from exc
            finally:
                _rollback(conn)
        finally:
            # Assigning the deferred isolation_level property starts a Python sqlite
            # transaction on a plain PRAGMA.  Restore the original isolation level
            # only after temporarily entering autocommit; rollback already ended it.
            with _deferred_writes(conn) as execute:
                execute("PRAGMA query_only=" + ("ON" if old_query_only else "OFF"))
                execute("PRAGMA foreign_keys=" + ("ON" if old_foreign_keys else "OFF"))
    except ArchivePlanStoreError:
        raise
    except sqlite3.Error as exc:
        raise ArchivePlanStoreError("Archive plan read-only reload failed") from exc
