"""Library-level production entry: explicit content resolutions -> archive plan.

This is the one public seam between per-file content resolution
(`resolve_content_version` / `ContentResolution`) and the archive plan builders.
Each explicit decision becomes exactly one `ArchiveEntry`; the inventory, the
capacity envelope and the frozen plan are then produced by
`build_archive_inventory`, `plan_archive_capacity` and
`prepare_archive_plan`.  Those validations are reused, never restated here.

What this layer adds is only the decision gate the builders cannot express:

* one item names its artwork and page explicitly and carries one resolved
  decision; the candidate inside it is the authority for the path and the
  physical object, the content is the authority for asset/revision/size/SHA-256;
* an unresolved, conflicting or unconfirmed decision is refused as data - all of
  them at once - instead of quietly shrinking the batch;
* one path may enter a batch once, and one batch describes one root generation,
  because the plan's root revision is derived from the candidates.

There is no filesystem, database, network, observer or archive-tool call here,
and nothing is saved: a `PreparedArchivePlan` stays the caller's to hand to
`save_archive_plan` inside its own transaction.  Work/page validity, inventory
rules, receipt binding and plan bounds stay with the core objects.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from core.archive_inventory import (
    ArchiveEntry,
    ArchiveInventory,
    ArtworkGroup,
    CapacityPlan,
    CapacityPolicy,
    InventoryPolicy,
    build_archive_inventory,
    plan_archive_capacity,
)
from core.archive_plan_store import PreparedArchivePlan, prepare_archive_plan
from core.archive_scope import ContentResolution, ResolutionKind, ScopeReceipt
from core.task_contracts import ContractError

__all__ = [
    "ObservedArchivePlan",
    "ObservedPlanEntryError",
    "ObservedPlanIssue",
    "ObservedPlanIssueCode",
    "ObservedPlanItem",
    "archive_entries_from_resolutions",
    "prepare_observed_plan",
]


class ObservedPlanIssueCode(StrEnum):
    """Why one explicit decision cannot become a planned archive entry."""

    MISSING_CONTENT = "missing_content"
    CONTENT_CONFLICT = "content_conflict"
    UNCONFIRMED_REPLACEMENT = "unconfirmed_replacement"
    INCOMPLETE_RESOLUTION = "incomplete_resolution"
    MISSING_PHYSICAL_IDENTITY = "missing_physical_identity"
    CONTENT_SIZE_MISMATCH = "content_size_mismatch"
    DUPLICATE_PATH = "duplicate_path"
    MIXED_ROOT = "mixed_root"
    INCOMPLETE_INVENTORY = "incomplete_inventory"
    INVALID_ITEM = "invalid_item"


@dataclass(frozen=True, slots=True)
class ObservedPlanIssue:
    """One refusal with the exact artwork, page and path it applies to."""

    code: ObservedPlanIssueCode
    message: str
    work_id: str | None = None
    page_index: int | None = None
    relative_path: str | None = None
    reason: str | None = None


class ObservedPlanEntryError(ContractError):
    """One or more explicit decisions cannot become a plan entry.

    The batch is not built, no builder ran on the rejected set, and the caller
    receives every refusal instead of the first one.
    """

    def __init__(self, issues: tuple[ObservedPlanIssue, ...]):
        items = tuple(issues)
        if not items or not all(isinstance(item, ObservedPlanIssue) for item in items):
            raise TypeError("Expected one or more observed plan issues")
        self.issues = tuple(sorted(items, key=_issue_sort_key))
        codes = tuple(dict.fromkeys(issue.code.value for issue in self.issues))
        super().__init__(
            "%d archive plan item(s) rejected: %s" % (len(self.issues), ", ".join(codes)))

    @property
    def codes(self) -> tuple[ObservedPlanIssueCode, ...]:
        return tuple(dict.fromkeys(issue.code for issue in self.issues))


@dataclass(frozen=True, slots=True)
class ObservedPlanItem:
    """One artwork page and the resolved content decision for its file.

    `page_index` is explicit: `None` means the artwork is not page-addressed,
    exactly as `ArchiveEntry` defines it.  The path and the physical object come
    from `resolution.candidate`; they are never supplied separately, so a plan
    entry cannot name a file its observation did not observe.
    """

    work_id: str
    page_index: int | None
    resolution: ContentResolution

    def __post_init__(self):
        if not isinstance(self.work_id, str):
            raise ContractError("Invalid artwork ID")
        if not isinstance(self.resolution, ContentResolution):
            raise ContractError("Expected a resolved content decision")


@dataclass(frozen=True, slots=True)
class ObservedArchivePlan:
    """One prepared plan together with the explicit entries it was built from."""

    batch_id: str
    entries: tuple[ArchiveEntry, ...]
    inventory: ArchiveInventory
    capacity: CapacityPlan
    prepared: PreparedArchivePlan

    def __post_init__(self):
        if not isinstance(self.entries, tuple) or not all(
                isinstance(entry, ArchiveEntry) for entry in self.entries):
            raise ContractError("Invalid planned archive entries")
        if (
            self.batch_id != self.inventory.batch_id
            or self.batch_id != self.capacity.batch_id
            or self.inventory != self.prepared.inventory
            or self.capacity != self.prepared.capacity
        ):
            raise ContractError("Planned archive parts disagree with each other")


@dataclass(frozen=True, slots=True)
class _Selection:
    entries: tuple[ArchiveEntry, ...]
    groups: tuple[ArtworkGroup, ...]
    root_revision: int | None


def _issue_sort_key(issue: ObservedPlanIssue) -> tuple:
    return (
        issue.work_id or "",
        -1 if issue.page_index is None else issue.page_index,
        issue.relative_path or "", issue.code.value, issue.reason or "", issue.message,
    )


def _issue(
    code: ObservedPlanIssueCode, message: str, *, item: ObservedPlanItem | None = None,
    relative_path: str | None = None, work_id: str | None = None,
    page_index: int | None = None, reason: str | None = None,
) -> ObservedPlanIssue:
    if item is not None:
        candidate = item.resolution.candidate
        return ObservedPlanIssue(
            code, message, item.work_id, item.page_index,
            relative_path or candidate.relative_path, reason,
        )
    return ObservedPlanIssue(code, message, work_id, page_index, relative_path, reason)


def _root_key(item: ObservedPlanItem) -> tuple[str, str, int]:
    candidate = item.resolution.candidate
    return candidate.kind, candidate.root_id.casefold(), candidate.root_revision


def _decision_issues(item: ObservedPlanItem) -> tuple[ObservedPlanIssue, ...]:
    """Refuse a decision that is not one complete, observed content revision."""
    resolution = item.resolution
    content = resolution.content
    if content is None:
        if resolution.kind is ResolutionKind.CONFLICT:
            return (_issue(
                ObservedPlanIssueCode.CONTENT_CONFLICT,
                "A conflicting content decision cannot become a planned entry",
                item=item, reason=resolution.reason,
            ),)
        if resolution.kind is ResolutionKind.NEEDS_CONFIRMATION:
            return (_issue(
                ObservedPlanIssueCode.UNCONFIRMED_REPLACEMENT,
                "A content decision that still needs confirmation cannot be planned",
                item=item, reason=resolution.reason + ":" + resolution.next_action,
            ),)
        return (_issue(
            ObservedPlanIssueCode.MISSING_CONTENT,
            "Every planned entry needs one resolved content revision",
            item=item, reason=resolution.reason,
        ),)
    issues = []
    if resolution.requires_confirmation or not resolution.scope_complete:
        issues.append(_issue(
            ObservedPlanIssueCode.INCOMPLETE_RESOLUTION,
            "A resolved decision must cover a complete scope and need no confirmation",
            item=item, reason=resolution.next_action,
        ))
    candidate = resolution.candidate
    if candidate.physical_identity is None:
        issues.append(_issue(
            ObservedPlanIssueCode.MISSING_PHYSICAL_IDENTITY,
            "An observation without a device/file identity cannot bind a plan entry",
            item=item,
        ))
    if content.size_bytes != candidate.size_bytes:
        issues.append(_issue(
            ObservedPlanIssueCode.CONTENT_SIZE_MISMATCH,
            "Content size %d differs from the enumerated physical size %d"
            % (content.size_bytes, candidate.size_bytes),
            item=item,
        ))
    return tuple(issues)


def _checked_selection(items: Iterable[ObservedPlanItem]) -> _Selection:
    supplied = tuple(items)
    issues: list[ObservedPlanIssue] = []
    accepted: list[ObservedPlanItem] = []
    batch_root: tuple[str, str, int] | None = None
    for item in supplied:
        if not isinstance(item, ObservedPlanItem):
            issues.append(_issue(
                ObservedPlanIssueCode.INVALID_ITEM,
                "Expected an explicit artwork page with its content decision",
            ))
            continue
        decision_issues = _decision_issues(item)
        if decision_issues:
            issues.extend(decision_issues)
            continue
        root_key = _root_key(item)
        if batch_root is None:
            batch_root = root_key
        elif root_key != batch_root:
            issues.append(_issue(
                ObservedPlanIssueCode.MIXED_ROOT,
                "One batch describes one root generation; this item belongs to %s revision %d"
                % (root_key[1], root_key[2]),
                item=item, reason="root_mapping_generation_differs",
            ))
            continue
        accepted.append(item)

    by_path: dict[str, list[ObservedPlanItem]] = {}
    for item in accepted:
        by_path.setdefault(item.resolution.candidate.path.identity_key, []).append(item)
    for shared in by_path.values():
        if len(shared) < 2:
            continue
        for item in shared:
            issues.append(_issue(
                ObservedPlanIssueCode.DUPLICATE_PATH,
                "One Windows path may enter a batch once; %d items name it" % len(shared),
                item=item, reason="path_identity_repeated",
            ))
    if issues:
        raise ObservedPlanEntryError(tuple(issues))

    entries: list[ArchiveEntry] = []
    groups: dict[str, list[ArchiveEntry]] = {}
    for item in accepted:
        candidate = item.resolution.candidate
        entry = ArchiveEntry(
            item.resolution.content, candidate.path, item.page_index,
            candidate.physical_identity,
        )
        entries.append(entry)
        groups.setdefault(item.work_id, []).append(entry)
    return _Selection(
        tuple(entries),
        tuple(ArtworkGroup(work_id, tuple(group)) for work_id, group in groups.items()),
        None if batch_root is None else batch_root[2],
    )


def archive_entries_from_resolutions(
    items: Iterable[ObservedPlanItem],
) -> tuple[ArchiveEntry, ...]:
    """Convert explicit decisions into plan entries, in the supplied order.

    Every refusal is raised as one `ObservedPlanEntryError` before any plan
    builder runs; a returned tuple names each path exactly once and carries the
    candidate's physical object.
    """
    return _checked_selection(items).entries


def prepare_observed_plan(
    items: Iterable[ObservedPlanItem],
    *,
    batch_id: str,
    receipt: ScopeReceipt,
    scope: dict[str, object] | None = None,
    inventory_policy: InventoryPolicy = InventoryPolicy(),
    capacity_policy: CapacityPolicy = CapacityPolicy(),
    available_additional_bytes: int | None = None,
) -> ObservedArchivePlan:
    """Prepare one frozen archive plan from explicit per-page decisions.

    The scope receipt proves which snapshot was consumed; `prepare_archive_plan`
    binds it to these exact entries, so a receipt from another enumeration, root
    generation or path set is refused there rather than here.  Work/page
    legality, inventory rules, capacity estimates and plan bounds are likewise
    the core objects' own validation.  Nothing is written and nothing is sealed.
    """
    selection = _checked_selection(items)
    inventory = build_archive_inventory(
        selection.groups, batch_id=batch_id, policy=inventory_policy,
    )
    if not inventory.scope_complete:
        raise ObservedPlanEntryError(tuple(
            _issue(
                ObservedPlanIssueCode.INCOMPLETE_INVENTORY, issue.message,
                work_id=issue.work_id, page_index=issue.page_index,
                relative_path=issue.path, reason=issue.code.value,
            )
            for issue in inventory.issues
        ))
    capacity = plan_archive_capacity(
        inventory, available_additional_bytes=available_additional_bytes,
        policy=capacity_policy,
    )
    root_revision = selection.root_revision
    if root_revision is None:
        root_revision = getattr(receipt, "root_revision", None)
    prepared = prepare_archive_plan(
        inventory, capacity, scope=scope, receipt=receipt, root_revision=root_revision,
    )
    return ObservedArchivePlan(
        batch_id, selection.entries, inventory, capacity, prepared,
    )
