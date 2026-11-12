"""Deterministic archive inventory and additional-space estimates.

Callers supply explicit artwork groups containing ContentVersion/PathRef pairs.
There is no filesystem, database, network, archive-tool, or release operation.
content=None means unresolved identity, revision, or size; it never causes a
scan. Unknown and conflicting items are reported instead of becoming a silent
partial archive. Configuration values are estimates, not platform guarantees.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import StrEnum

from core.preservation_policy import ContentVersion
from core.task_contracts import ContractError, PathRef, canonical_json


ARCHIVE_INVENTORY_SCHEMA_VERSION = 1
CAPACITY_PLAN_SCHEMA_VERSION = 1
DEFAULT_PACKAGE_TARGET_BYTES = 2 * 1024**3
DEFAULT_BATCH_BUDGET_BYTES = 20 * 1024**3
DEFAULT_RECOVERY_ESTIMATE_BASIS_POINTS = 1500
DEFAULT_CONTROL_BYTES = 64 * 1024**2
DEFAULT_SAFETY_MARGIN_BYTES = 10 * 1024**3


class InventoryStatus(StrEnum):
    READY = "ready"
    NEEDS_ADJUSTMENT = "needs_adjustment"


class CapacityStatus(StrEnum):
    READY = "ready"
    NEEDS_ADJUSTMENT = "needs_adjustment"
    INSUFFICIENT_SPACE = "insufficient_space"


class InventoryIssueCode(StrEnum):
    EMPTY_GROUP = "empty_group"
    INVALID_PATH = "invalid_path"
    UNKNOWN_CONTENT_VERSION = "unknown_content_version"
    MISSING_CONTENT_DIGEST = "missing_content_digest"
    PATH_COLLISION = "windows_path_collision"
    ASSET_CONFLICT = "conflicting_asset_version"
    PAGE_CONFLICT = "conflicting_artwork_page"


def _integer(value: object, name: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ContractError("Invalid " + name)
    return value


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ContractError("Invalid " + name)
    return value


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """One path observation; None denotes unknown content identity/size."""

    content: ContentVersion | None
    path: PathRef | str
    page_index: int | None = None

    def __post_init__(self):
        if self.content is not None and not isinstance(self.content, ContentVersion):
            raise ContractError("Invalid archive content")
        if not isinstance(self.path, (PathRef, str)):
            raise ContractError("Invalid archive path")
        if self.page_index is not None:
            _integer(self.page_index, "archive page index")
            if self.page_index > 9999:
                raise ContractError("Invalid archive page index")


@dataclass(frozen=True, slots=True)
class ArtworkGroup:
    """Explicit grouping; empty groups remain visible for review."""

    work_id: str
    entries: tuple[ArchiveEntry, ...]

    def __post_init__(self):
        _token(self.work_id, "artwork ID")
        entries = tuple(self.entries)
        if not all(isinstance(entry, ArchiveEntry) for entry in entries):
            raise ContractError("Invalid archive entries")
        object.__setattr__(self, "entries", entries)


@dataclass(frozen=True, slots=True)
class InventoryPolicy:
    target_package_bytes: int = DEFAULT_PACKAGE_TARGET_BYTES
    batch_budget_bytes: int = DEFAULT_BATCH_BUDGET_BYTES
    schema_version: int = ARCHIVE_INVENTORY_SCHEMA_VERSION

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError("Unsupported archive inventory schema")
        _integer(self.target_package_bytes, "package target", 1)
        _integer(self.batch_budget_bytes, "batch payload budget")


@dataclass(frozen=True, slots=True)
class InventoryIssue:
    code: InventoryIssueCode
    message: str
    path_key: str | None = None
    path: str | None = None
    asset_id: str | None = None
    work_id: str | None = None
    page_index: int | None = None


@dataclass(frozen=True, slots=True)
class InventoryAsset:
    content: ContentVersion
    path: PathRef
    work_id: str
    page_index: int | None
    path_key: str
    content_key: str


@dataclass(frozen=True, slots=True)
class PackagePlan:
    package_id: str
    sequence: int
    payload_bytes: int
    work_ids: tuple[str, ...]
    members: tuple[InventoryAsset, ...]
    oversized_single_file: bool = False
    exception_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArchiveInventory:
    batch_id: str
    status: InventoryStatus
    scope_complete: bool
    input_entry_count: int
    duplicate_entry_count: int
    asset_count: int
    path_count: int
    unique_content_count: int
    member_payload_bytes: int
    unique_content_bytes: int
    assets: tuple[InventoryAsset, ...]
    packages: tuple[PackagePlan, ...]
    issues: tuple[InventoryIssue, ...]
    split_work_ids: tuple[str, ...]
    adjustment_codes: tuple[str, ...]
    target_package_bytes: int
    batch_budget_bytes: int
    schema_version: int = ARCHIVE_INVENTORY_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self)))

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class CapacityPolicy:
    """Estimation knobs, not tool, filesystem, recovery, or provider proofs."""

    staging_mode: str = "full_batch"
    recovery_estimate_basis_points: int = DEFAULT_RECOVERY_ESTIMATE_BASIS_POINTS
    control_bytes: int = DEFAULT_CONTROL_BYTES
    safety_margin_bytes: int = DEFAULT_SAFETY_MARGIN_BYTES
    schema_version: int = CAPACITY_PLAN_SCHEMA_VERSION

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError("Unsupported capacity plan schema")
        if self.staging_mode not in {"full_batch", "single_package"}:
            raise ContractError("Invalid staging mode")
        _integer(self.recovery_estimate_basis_points, "recovery estimate basis points")
        _integer(self.control_bytes, "control byte estimate")
        _integer(self.safety_margin_bytes, "safety margin")


@dataclass(frozen=True, slots=True)
class CapacityComponent:
    name: str
    estimated_bytes: int


@dataclass(frozen=True, slots=True)
class CapacityPhase:
    """Additional-space envelope; original source bytes are not a component."""

    phase: str
    additional_bytes: int
    components: tuple[CapacityComponent, ...]


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    batch_id: str
    inventory_schema_version: int
    status: CapacityStatus
    scope_complete: bool
    staging_mode: str
    # Informational source total only; never add it to an allocation envelope.
    # Identical content at distinct paths is not proof of shared disk extents.
    source_member_bytes_excluded: int
    recovery_estimate_basis_points: int
    peak_additional_bytes: int | None
    available_additional_bytes: int | None
    shortfall_bytes: int | None
    peak_components: tuple[CapacityComponent, ...]
    phases: tuple[CapacityPhase, ...]
    adjustment_codes: tuple[str, ...]
    assumptions: tuple[str, ...]
    schema_version: int = CAPACITY_PLAN_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self)))

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class _RawEntry:
    content: ContentVersion | None
    path: PathRef | None
    raw_path: str
    work_id: str
    page_index: int | None


def _content_key(content: ContentVersion) -> str:
    return content.sha256 + ":" + str(content.size_bytes)


def _raw_identity(entry: _RawEntry) -> tuple:
    path_identity = (
        entry.path.target_key if entry.path is not None
        else "invalid:" + entry.raw_path.replace("\\", "/").casefold()
    )
    return path_identity, entry.work_id, entry.page_index, entry.content


def _raw_sort_key(entry: _RawEntry) -> tuple:
    content = entry.content
    return (
        entry.work_id,
        entry.path.target_key if entry.path is not None else "",
        entry.raw_path.casefold(), entry.raw_path,
        -1 if entry.page_index is None else entry.page_index,
        "" if content is None else content.asset_id,
        -1 if content is None else content.revision,
        -1 if content is None else content.size_bytes,
        "" if content is None or content.sha256 is None else content.sha256,
    )


def _issue(
    code: InventoryIssueCode, message: str, entry: _RawEntry,
) -> InventoryIssue:
    return InventoryIssue(
        code=code,
        message=message,
        path_key=entry.path.target_key if entry.path is not None else None,
        path=entry.path.relative_path if entry.path is not None else entry.raw_path,
        asset_id=entry.content.asset_id if entry.content is not None else None,
        work_id=entry.work_id,
        page_index=entry.page_index,
    )


def _issue_sort_key(issue: InventoryIssue) -> tuple:
    return (
        issue.code.value, issue.path_key or "", issue.path or "",
        issue.asset_id or "", issue.work_id or "",
        -1 if issue.page_index is None else issue.page_index, issue.message,
    )


def _asset_sort_key(asset: InventoryAsset) -> tuple:
    return (
        asset.path_key, asset.path.relative_path.casefold(), asset.path.relative_path,
        asset.content.asset_id, asset.content.revision, asset.work_id,
        -1 if asset.page_index is None else asset.page_index,
    )


def _plan_packages(
    assets: tuple[InventoryAsset, ...], batch_id: str, target: int,
) -> tuple[PackagePlan, ...]:
    by_work: dict[str, list[InventoryAsset]] = {}
    for asset in assets:
        by_work.setdefault(asset.work_id, []).append(asset)

    chunks: list[list[InventoryAsset]] = []
    current: list[InventoryAsset] = []
    current_bytes = 0

    def flush() -> None:
        nonlocal current, current_bytes
        if current:
            chunks.append(current)
        current = []
        current_bytes = 0

    for work_id in sorted(by_work):
        members = sorted(by_work[work_id], key=_asset_sort_key)
        work_bytes = sum(member.content.size_bytes for member in members)
        if work_bytes <= target:
            if current and current_bytes + work_bytes > target:
                flush()
            current.extend(members)
            current_bytes += work_bytes
            continue

        # Artwork affinity cannot override the target. Only individual files
        # may exceed it, and each does so alone; no file is divided.
        flush()
        for member in members:
            size = member.content.size_bytes
            if size > target:
                flush()
                chunks.append([member])
            else:
                if current and current_bytes + size > target:
                    flush()
                current.append(member)
                current_bytes += size
    flush()

    work_package_counts: dict[str, int] = {}
    for chunk in chunks:
        for work_id in {member.work_id for member in chunk}:
            work_package_counts[work_id] = work_package_counts.get(work_id, 0) + 1
    split_works = {work_id for work_id, count in work_package_counts.items() if count > 1}

    packages = []
    for sequence, chunk in enumerate(chunks, 1):
        work_ids = tuple(sorted({member.work_id for member in chunk}))
        oversized = len(chunk) == 1 and chunk[0].content.size_bytes > target
        exceptions = []
        if oversized:
            exceptions.append("single_file_over_target")
        if any(work_id in split_works for work_id in work_ids):
            exceptions.append("work_split_for_target")
        packages.append(PackagePlan(
            package_id="a_" + batch_id + "_" + str(sequence).zfill(6),
            sequence=sequence,
            payload_bytes=sum(member.content.size_bytes for member in chunk),
            work_ids=work_ids,
            members=tuple(chunk),
            oversized_single_file=oversized,
            exception_codes=tuple(exceptions),
        ))
    return tuple(packages)


def build_archive_inventory(
    groups: Iterable[ArtworkGroup], *, batch_id: str,
    policy: InventoryPolicy = InventoryPolicy(),
) -> ArchiveInventory:
    """Validate an explicit scope and build stable, whole-file package plans."""
    if not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", batch_id):
        raise ContractError("Invalid archive batch ID for a Windows-safe package name")
    if not isinstance(policy, InventoryPolicy):
        raise ContractError("Invalid inventory policy")

    raw_entries: list[_RawEntry] = []
    issues: list[InventoryIssue] = []
    input_entry_count = 0
    for group in groups:
        if not isinstance(group, ArtworkGroup):
            raise ContractError("Invalid artwork group")
        if not group.entries:
            issues.append(InventoryIssue(
                InventoryIssueCode.EMPTY_GROUP,
                "An explicitly selected empty artwork needs review",
                work_id=group.work_id,
            ))
        for entry in group.entries:
            input_entry_count += 1
            path = entry.path
            if isinstance(path, PathRef):
                normalized = None if path.relative_path == "." else path
                raw_path = path.relative_path
            else:
                raw_path = path
                try:
                    normalized = PathRef(relative_path=path)
                    if normalized.relative_path == ".":
                        raise ContractError("An archive member must name a file")
                except ContractError:
                    normalized = None
            raw_entries.append(_RawEntry(
                entry.content, normalized, raw_path, group.work_id, entry.page_index,
            ))

    # Exact repeat observations collapse. Case/slash aliases of one Windows
    # path share identity; a stable display spelling is selected explicitly.
    unique_raw: dict[tuple, _RawEntry] = {}
    for entry in raw_entries:
        identity = _raw_identity(entry)
        previous = unique_raw.get(identity)
        if previous is None or _raw_sort_key(entry) < _raw_sort_key(previous):
            unique_raw[identity] = entry
    ordered_raw = sorted(unique_raw.values(), key=_raw_sort_key)
    duplicate_entry_count = input_entry_count - len(ordered_raw)

    candidates: list[InventoryAsset] = []
    for entry in ordered_raw:
        if entry.path is None:
            issues.append(_issue(
                InventoryIssueCode.INVALID_PATH,
                "Expected a safe, non-empty relative file path without ADS",
                entry,
            ))
            continue
        if entry.content is None:
            issues.append(_issue(
                InventoryIssueCode.UNKNOWN_CONTENT_VERSION,
                "Content identity, revision, and size must be supplied before planning",
                entry,
            ))
            continue
        if entry.content.sha256 is None:
            issues.append(_issue(
                InventoryIssueCode.MISSING_CONTENT_DIGEST,
                "A current full SHA-256 is required before archive planning",
                entry,
            ))
            continue
        candidates.append(InventoryAsset(
            content=entry.content,
            path=entry.path,
            work_id=entry.work_id,
            page_index=entry.page_index,
            path_key=entry.path.target_key,
            content_key=_content_key(entry.content),
        ))

    by_path: dict[str, list[InventoryAsset]] = {}
    by_asset: dict[tuple[str, int], list[InventoryAsset]] = {}
    by_page: dict[tuple[str, int], list[InventoryAsset]] = {}
    for asset in candidates:
        by_path.setdefault(asset.path_key, []).append(asset)
        by_asset.setdefault((asset.content.asset_id, asset.content.revision), []).append(asset)
        if asset.page_index is not None:
            by_page.setdefault((asset.work_id, asset.page_index), []).append(asset)

    rejected_paths = {key for key, items in by_path.items() if len(items) > 1}
    rejected_assets = {
        key for key, items in by_asset.items()
        if len({_content_key(item.content) for item in items}) > 1
    }
    rejected_pages = {
        key for key, items in by_page.items()
        if len({item.content_key for item in items}) > 1
    }
    raw_by_path_key = {entry.path.target_key: entry for entry in ordered_raw if entry.path is not None}

    assets = []
    for asset in candidates:
        raw = raw_by_path_key[asset.path_key]
        asset_key = (asset.content.asset_id, asset.content.revision)
        page_key = (asset.work_id, asset.page_index)
        if asset.path_key in rejected_paths:
            issues.append(_issue(
                InventoryIssueCode.PATH_COLLISION,
                "Distinct asset, page, or artwork records collide on one Windows path",
                raw,
            ))
        elif asset_key in rejected_assets:
            issues.append(_issue(
                InventoryIssueCode.ASSET_CONFLICT,
                "One asset revision has conflicting size or content digest",
                raw,
            ))
        elif asset.page_index is not None and page_key in rejected_pages:
            issues.append(_issue(
                InventoryIssueCode.PAGE_CONFLICT,
                "One artwork page refers to different content versions",
                raw,
            ))
        else:
            assets.append(asset)

    ordered_assets = tuple(sorted(assets, key=_asset_sort_key))
    ordered_issues = tuple(sorted(issues, key=_issue_sort_key))
    scope_complete = not ordered_issues
    packages = (
        _plan_packages(ordered_assets, batch_id, policy.target_package_bytes)
        if scope_complete else ()
    )
    split_work_ids = tuple(sorted({
        work_id for package in packages for work_id in package.work_ids
        if "work_split_for_target" in package.exception_codes
    }))
    # Only the full accepted scope feeds totals or capacity; valid-looking
    # neighbours must not make a partial result look like the complete set.
    counted_assets = ordered_assets if scope_complete else ()
    member_bytes = sum(asset.content.size_bytes for asset in counted_assets)
    unique_contents = {asset.content_key: asset.content.size_bytes for asset in counted_assets}
    adjustment_codes = []
    if not scope_complete:
        adjustment_codes.append("inventory_rejected_items")
    if member_bytes > policy.batch_budget_bytes:
        adjustment_codes.append("batch_budget_exceeded")
    status = InventoryStatus.READY if not adjustment_codes else InventoryStatus.NEEDS_ADJUSTMENT

    return ArchiveInventory(
        batch_id=batch_id,
        status=status,
        scope_complete=scope_complete,
        input_entry_count=input_entry_count,
        duplicate_entry_count=duplicate_entry_count,
        asset_count=len({(asset.content.asset_id, asset.content.revision) for asset in counted_assets}),
        path_count=len(counted_assets),
        unique_content_count=len(unique_contents),
        member_payload_bytes=member_bytes,
        unique_content_bytes=sum(unique_contents.values()),
        assets=ordered_assets,
        packages=packages,
        issues=ordered_issues,
        split_work_ids=split_work_ids,
        adjustment_codes=tuple(adjustment_codes),
        target_package_bytes=policy.target_package_bytes,
        batch_budget_bytes=policy.batch_budget_bytes,
    )


_CAPACITY_ASSUMPTIONS = (
    "source_files_already_consume_space_and_are_not_added_again",
    "package_payload_is_estimated_without_claiming_compression_savings",
    "recovery_percentage_is_an_estimate_not_a_capability_proof",
    "current_readback_and_current_unpack_each_use_the_largest_package_payload",
    "selected_staging_envelope_remains_budgeted_through_current_readback",
    "actual_free_space_and_tool_overhead_must_be_rechecked_before_any_io",
    "repair_drills_and_full_restores_need_a_separate_space_plan",
)


def _phase(
    phase: str, components: dict[str, int], names: tuple[str, ...],
) -> CapacityPhase:
    selected = tuple(CapacityComponent(name, components[name]) for name in names)
    return CapacityPhase(phase, sum(item.estimated_bytes for item in selected), selected)


def plan_archive_capacity(
    inventory: ArchiveInventory, *, available_additional_bytes: int | None,
    policy: CapacityPolicy = CapacityPolicy(),
) -> CapacityPlan:
    """Estimate additional peak space; never delete or allocate anything."""
    if not isinstance(inventory, ArchiveInventory) or not isinstance(policy, CapacityPolicy):
        raise ContractError("Invalid capacity planning input")
    if available_additional_bytes is not None:
        _integer(available_additional_bytes, "available additional bytes")

    codes = list(inventory.adjustment_codes)
    if available_additional_bytes is None:
        codes.append("available_space_unknown")
    if not inventory.scope_complete:
        return CapacityPlan(
            batch_id=inventory.batch_id,
            inventory_schema_version=inventory.schema_version,
            status=CapacityStatus.NEEDS_ADJUSTMENT,
            scope_complete=False,
            staging_mode=policy.staging_mode,
            source_member_bytes_excluded=inventory.member_payload_bytes,
            recovery_estimate_basis_points=policy.recovery_estimate_basis_points,
            peak_additional_bytes=None,
            available_additional_bytes=available_additional_bytes,
            shortfall_bytes=None,
            peak_components=(),
            phases=(),
            adjustment_codes=tuple(codes),
            assumptions=_CAPACITY_ASSUMPTIONS,
        )

    payload = inventory.member_payload_bytes
    largest = max((package.payload_bytes for package in inventory.packages), default=0)
    staging = payload if policy.staging_mode == "full_batch" else largest
    recovery = (payload * policy.recovery_estimate_basis_points + 9999) // 10000
    components = {
        "frozen_staging": staging,
        "packages": payload,
        "recovery_estimate": recovery,
        "current_readback": largest,
        "current_unpack": largest,
        "control": policy.control_bytes,
        "safety_margin": policy.safety_margin_bytes,
    }
    names = tuple(components)
    phases = (
        _phase(
            "freeze_and_package", components,
            ("frozen_staging", "packages", "recovery_estimate", "control", "safety_margin"),
        ),
        # Staging remains budgeted through current readback. Later release of
        # verified staging is separate and never entails deleting source files.
        _phase("readback_and_unpack", components, names),
        _phase(
            "after_staging_release", components,
            ("packages", "recovery_estimate", "control", "safety_margin"),
        ),
    )
    peak = max(phase.additional_bytes for phase in phases)
    shortfall = (
        None if available_additional_bytes is None
        else max(0, peak - available_additional_bytes)
    )
    if shortfall:
        codes.append("insufficient_space")
    if codes:
        status = (
            CapacityStatus.INSUFFICIENT_SPACE
            if codes == ["insufficient_space"] else CapacityStatus.NEEDS_ADJUSTMENT
        )
    else:
        status = CapacityStatus.READY
    return CapacityPlan(
        batch_id=inventory.batch_id,
        inventory_schema_version=inventory.schema_version,
        status=status,
        scope_complete=True,
        staging_mode=policy.staging_mode,
        source_member_bytes_excluded=payload,
        recovery_estimate_basis_points=policy.recovery_estimate_basis_points,
        peak_additional_bytes=peak,
        available_additional_bytes=available_additional_bytes,
        shortfall_bytes=shortfall,
        peak_components=phases[1].components,
        phases=phases,
        adjustment_codes=tuple(codes),
        assumptions=_CAPACITY_ASSUMPTIONS,
    )
