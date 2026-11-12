"""Persist explicit archive inventories in an already-open temporary schema-2 DB.

This module is deliberately not a repository factory: it never discovers or opens a
library.  A caller must provide a sqlite3 connection to an explicitly selected,
isolated schema-2 copy.  Planned package indexes and their immutable JSON plans are
not sealed objects, remote locations, verification evidence, or a completion ledger.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone

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
    ARCHIVE_PROTOCOL_VERSION,
    ARCHIVE_SCHEMA_VERSION,
    ARCHIVE_TABLES,
    ArchiveSchemaError,
    _content_key,
    ensure_archive_asset,
    validate_archive_facts,
)
from core.preservation_policy import ContentVersion
from core.task_contracts import ContractError, PathRef, canonical_json, normalize_json

_ARCHIVE_PLAN_FORMAT = "jpgpng-archive-plan"
_ARCHIVE_PLAN_VERSION = 1
_BATCH_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class ArchivePlanStoreError(ArchiveSchemaError):
    """The connection, stored plan, or plan association fails closed validation."""


class ArchivePlanConflictError(ArchivePlanStoreError):
    """An immutable batch or package already has different facts."""


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
    asset = InventoryAsset(
        content=_content(raw["content"]),
        path=_path(raw["path"]),
        work_id=raw["work_id"],
        page_index=raw["page_index"],
        path_key=raw["path_key"],
        content_key=raw["content_key"],
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
    if (asset.path_key != asset.path.target_key
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


def _inventory(raw_value: object, expected_batch_id: str | None = None) -> ArchiveInventory:
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
        if (inventory.packages or inventory.path_count or inventory.asset_count
                or inventory.unique_content_count or inventory.member_payload_bytes
                or inventory.unique_content_bytes
                or "inventory_rejected_items" not in inventory.adjustment_codes):
            raise ContractError("Incomplete scope must not contain a package plan")
        return
    if inventory.input_entry_count != inventory.path_count + inventory.duplicate_entry_count:
        raise ContractError("Inventory input and duplicate counts differ")
    by_work: dict[str, list[ArchiveEntry]] = {}
    for asset in inventory.assets:
        by_work.setdefault(asset.work_id, []).append(
            ArchiveEntry(asset.content, asset.path, asset.page_index)
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


def _encode_scope(inventory: ArchiveInventory, scope: dict[str, object]) -> str:
    return canonical_json({
        "format": _ARCHIVE_PLAN_FORMAT,
        "plan_schema_version": _ARCHIVE_PLAN_VERSION,
        "batch_id": inventory.batch_id,
        "inventory": inventory.to_dict(),
        "scope": scope,
    })


def _decode_scope(value: object, expected_batch_id: str) -> tuple[ArchiveInventory, dict[str, object]]:
    raw = _strict_object(value, "scope JSON")
    if raw.get("format") != _ARCHIVE_PLAN_FORMAT:
        raise ArchivePlanStoreError("Stored scope JSON is not an archive plan")
    if raw.get("plan_schema_version") != _ARCHIVE_PLAN_VERSION or type(raw.get("plan_schema_version")) is not int:
        raise ArchivePlanStoreError("Unsupported stored archive plan version")
    if set(raw) != {"format", "plan_schema_version", "batch_id", "inventory", "scope"}:
        raise ArchivePlanStoreError("Stored scope JSON has missing or unknown fields")
    if raw["batch_id"] != expected_batch_id or not isinstance(raw["scope"], dict):
        raise ArchivePlanStoreError("Stored scope identity or scope value is invalid")
    inventory = _inventory(raw["inventory"], expected_batch_id)
    return inventory, raw["scope"]


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


def _check_assets(conn: sqlite3.Connection, inventory: ArchiveInventory) -> None:
    for asset in inventory.assets:
        content = asset.content
        expected = (
            _content_key(content), content.asset_id, content.revision, asset.path_key,
            canonical_json({
                "kind": asset.path.kind,
                "root_id": asset.path.root_id,
                "relative_path": asset.path.relative_path,
            }),
            None, "user_selected", content.size_bytes, content.sha256,
        )
        row = conn.execute(
            """SELECT content_key,asset_id,revision,path_key,path_json,artifact_id,
                      source_kind,size_bytes,sha256 FROM archive_assets WHERE path_key=?""",
            (asset.path_key,),
        ).fetchone()
        if row is None or tuple(row) != expected:
            raise ArchivePlanStoreError("Content/path facts differ from the immutable plan JSON")


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
    inventory, scope = _decode_scope(scope_json, batch_id)
    capacity = _capacity(_strict_object(capacity_json, "capacity JSON"), inventory)
    stored_packages = _packages(conn, batch_id)
    _check_package_index(stored_packages, _expected_packages(inventory))
    _check_assets(conn, inventory)
    return StoredArchivePlan(
        batch_id, inventory, capacity, scope, _timestamp(created_at), stored_packages,
    )


def _require_connection(conn: object) -> sqlite3.Connection:
    if not isinstance(conn, sqlite3.Connection):
        raise ArchivePlanStoreError("An explicit sqlite3 connection is required; paths are never opened")
    return conn


def _require_schema2(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "schema_meta" not in tables or not ARCHIVE_TABLES <= tables:
        raise ArchivePlanStoreError("Archive plan persistence requires the complete schema 2")
    metadata = dict(conn.execute('SELECT "key","value" FROM schema_meta'))
    if metadata.get("format") != "jpgpng-library":
        raise ArchivePlanStoreError("Connection belongs to a different product")
    if metadata.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
        raise ArchivePlanStoreError("Unsupported archive schema version; exactly schema 2 is required")


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


def save_archive_plan(
    conn: sqlite3.Connection,
    inventory: ArchiveInventory,
    capacity: CapacityPlan,
    *,
    scope: dict[str, object] | None = None,
) -> ArchivePlanSaveResult:
    """Atomically insert or exactly replay one plan on a caller-owned schema-2 copy.

    The connection must have foreign_keys=ON, query_only=OFF and no open transaction.
    This function owns and commits/rolls back only the transaction it starts.  Package
    members stay in scope_json: archive_members is reserved for sealed facts.
    """
    conn = _require_connection(conn)
    if not isinstance(inventory, ArchiveInventory) or not isinstance(capacity, CapacityPlan):
        raise ArchivePlanStoreError("Expected ArchiveInventory and CapacityPlan")
    if scope is None:
        scope = {"kind": "explicit_inventory"}
    if not isinstance(scope, dict):
        raise ArchivePlanStoreError("Archive plan scope must be a JSON object")
    try:
        scope_json = _encode_scope(inventory, scope)
        # Round-trip caller objects too; dataclass construction alone is not validation.
        checked_inventory, checked_scope = _decode_scope(scope_json, inventory.batch_id)
        capacity_json = canonical_json(capacity.to_dict())
        checked_capacity = _capacity(json.loads(capacity_json), checked_inventory)
        if checked_inventory != inventory or checked_capacity != capacity or checked_scope != scope:
            raise ContractError("Archive plan does not survive strict serialization")
        if capacity.batch_id != inventory.batch_id:
            raise ContractError("Capacity and inventory batch identities differ")
    except ArchivePlanStoreError:
        raise
    except (ContractError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ArchivePlanStoreError("Invalid archive plan input") from exc

    try:
        if conn.in_transaction:
            raise ArchivePlanStoreError("Archive plan save requires ownership of a new transaction")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise ArchivePlanStoreError("Archive plan save requires foreign keys enabled")
        if conn.execute("PRAGMA query_only").fetchone()[0] != 0:
            raise ArchivePlanStoreError("Archive plan save requires a writable explicit connection")
        conn.execute("BEGIN IMMEDIATE")
        try:
            _require_schema2(conn)
            try:
                validate_archive_facts(conn)
            except ArchiveSchemaError as exc:
                raise ArchivePlanStoreError(
                    f"Stored archive facts failed schema validation: {exc}"
                ) from exc
            existing = conn.execute(
                "SELECT 1 FROM archive_batches WHERE batch_id=?", (inventory.batch_id,),
            ).fetchone()
            if existing is not None:
                stored = _load_in_transaction(conn, inventory.batch_id)
                if (stored.inventory != checked_inventory or stored.capacity != checked_capacity
                        or stored.scope != checked_scope):
                    raise ArchivePlanConflictError("Batch ID already contains a different immutable plan")
                conn.execute("COMMIT")
                return ArchivePlanSaveResult(stored, replayed=True)

            expected_packages = _expected_packages(inventory)
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
                (inventory.batch_id, ARCHIVE_PROTOCOL_VERSION, scope_json, capacity_json, created_at),
            )
            for asset in inventory.assets:
                ensure_archive_asset(
                    conn, asset.content, asset.path, source_kind="user_selected", artifact_id=None,
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
            try:
                validate_archive_facts(conn)
            except ArchiveSchemaError as exc:
                raise ArchivePlanStoreError(
                    f"Persisted archive facts failed schema validation: {exc}"
                ) from exc
            stored = _load_in_transaction(conn, inventory.batch_id)
            if (stored.inventory, stored.capacity, stored.scope) != (
                checked_inventory, checked_capacity, checked_scope,
            ):
                raise ArchivePlanStoreError("Persisted archive plan failed read-back verification")
            conn.execute("COMMIT")
            return ArchivePlanSaveResult(stored, replayed=False)
        except BaseException:
            _rollback(conn)
            raise
    except ArchivePlanStoreError:
        raise
    except (sqlite3.Error, ContractError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise ArchivePlanStoreError("Archive plan save failed; its transaction was rolled back") from exc


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
                _require_schema2(conn)
                try:
                    validate_archive_facts(conn)
                except ArchiveSchemaError as exc:
                    raise ArchivePlanStoreError(
                    f"Stored archive facts failed schema validation: {exc}"
                ) from exc
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
