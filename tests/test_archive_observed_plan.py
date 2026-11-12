"""Offline tests for the observed-plan entry: no filesystem, database or network.

Every candidate, decision and scope receipt here is built explicitly in memory;
the tests assert only what the entry and the reused builders can prove.
"""
from __future__ import annotations

import json

import pytest

from core.archive_observed_plan import (
    ObservedPlanEntryError,
    ObservedPlanIssueCode,
    ObservedPlanItem,
    archive_entries_from_resolutions,
    prepare_observed_plan,
)
from core.archive_inventory import ArchiveEntry, CapacityPolicy, CapacityStatus
from core.archive_plan_store import ArchivePlanStoreError
from core.archive_scope import (
    SCOPE_RECEIPT_SCHEMA_VERSION, CandidateSnapshot, ContentResolution, ResolutionKind,
    ScopeEntryFact, ScopeReceipt, ScopeSelectionKind, ScopeState, _digest_text, _facts_digest,
)
from core.local_observation import FileIdentity
from core.preservation_policy import ContentVersion
from core.task_contracts import ContractError

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
CAPACITY = CapacityPolicy(control_bytes=0, safety_margin_bytes=0)


def candidate(
    relative_path: str = "gallery/art.png", *, size: int = 4, device: int = 1,
    file_index: int = 2, root_revision: int = 1, kind: str = "external",
    root_id: str = "synthetic", physical: bool = True,
) -> CandidateSnapshot:
    return CandidateSnapshot(
        kind, root_id, root_revision, relative_path,
        FileIdentity(device, file_index) if physical else None,
        size, 0, "snapshot-1", "run-1",
    )


def decision(
    kind: ResolutionKind, snapshot: CandidateSnapshot, content: ContentVersion | None,
    previous: ContentVersion | None = None, *, reason: str = "resolved",
    next_action: str = "none", requires_confirmation: bool = False,
    scope_complete: bool = True,
) -> ContentResolution:
    return ContentResolution(
        kind, snapshot, content, previous, reason, next_action,
        requires_confirmation, scope_complete,
    )


def baseline(snapshot: CandidateSnapshot, *, content: ContentVersion | None = None):
    resolved = content or ContentVersion("asset-1", 1, snapshot.size_bytes, SHA_A)
    return decision(
        ResolutionKind.BASELINE, snapshot, resolved,
        ContentVersion("asset-1", 0, snapshot.size_bytes, None),
        reason="first_observation_baseline",
    )


def reuse(snapshot: CandidateSnapshot, content: ContentVersion):
    return decision(
        ResolutionKind.REUSE, snapshot, content, content,
        reason="same_physical_object_same_content",
    )


def new_revision(snapshot: CandidateSnapshot, previous: ContentVersion, content: ContentVersion):
    return decision(
        ResolutionKind.NEW_REVISION, snapshot, content, previous,
        reason="same_physical_object_content_changed",
    )


def scope_fact(entry: ArchiveEntry, *, root_revision: int = 1) -> ScopeEntryFact:
    identity = entry.physical_identity
    return ScopeEntryFact(
        entry.path.kind, entry.path.root_id, root_revision, entry.path.relative_path,
        None if identity is None else identity.device,
        None if identity is None else identity.file_index,
        entry.content.size_bytes, 0,
    )


def make_receipt(entries, *, root_revision: int = 1, extra: int = 0) -> ScopeReceipt:
    facts = tuple(scope_fact(entry, root_revision=root_revision) for entry in entries)
    count = len(facts)
    kind = entries[0].path.kind if entries else "external"
    root_id = entries[0].path.root_id if entries else "synthetic"
    return ScopeReceipt(
        schema_version=SCOPE_RECEIPT_SCHEMA_VERSION,
        kind=kind, root_id=root_id, root_revision=root_revision,
        rules_digest="0" * 64, selection_digest="0" * 64,
        selection_kind=ScopeSelectionKind.SNAPSHOT,
        sub_batch_limit=None, sub_batch_description=None,
        snapshot_id="snapshot-1", snapshot_digest="0" * 64,
        root_identity=FileIdentity(1, 1),
        entries_in_snapshot=count + extra, exclusions_in_snapshot=0, issues_in_snapshot=0,
        enumeration_state=ScopeState.COMPLETE,
        page_count=1, first_page_index=0, last_page_index=0, expected_page_count=1,
        last_page_has_next=False, consumed_entries=count,
        consumed_exclusions=0, consumed_issues=0, remaining_entries=extra,
        aborted=False, entries_digest=_facts_digest(facts), entry_facts=facts,
        exclusions_digest=_digest_text([]), snapshot_verified=True,
    )


def plan_for(items, receipt, *, batch_id: str = "batch-1", **kwargs):
    kwargs.setdefault("available_additional_bytes", 10**9)
    kwargs.setdefault("capacity_policy", CAPACITY)
    return prepare_observed_plan(
        items, batch_id=batch_id, receipt=receipt, **kwargs,
    )


def test_baseline_decision_becomes_one_entry_inventory_capacity_and_plan():
    snapshot = candidate()
    resolution = baseline(snapshot)
    content = resolution.content
    entry = ArchiveEntry(content, snapshot.path, 0, snapshot.physical_identity)
    plan = plan_for([ObservedPlanItem("work-1", 0, resolution)], make_receipt([entry]))

    assert plan.batch_id == "batch-1"
    assert plan.entries == (entry,)
    assert plan.inventory.scope_complete and plan.inventory.packages
    assert plan.inventory.asset_count == 1 and plan.inventory.path_count == 1
    assert plan.inventory.member_payload_bytes == content.size_bytes == 4
    assert plan.inventory.packages[0].members[0].content == content
    assert plan.capacity.status is CapacityStatus.READY
    assert plan.capacity.peak_additional_bytes is not None
    assert plan.prepared.inventory == plan.inventory
    assert plan.prepared.capacity == plan.capacity
    receipt_json = json.loads(plan.prepared.scope_receipt_json)
    assert receipt_json["entry_facts"][0]["content_sha256"] == SHA_A
    assert receipt_json["entry_facts"][0]["content_revision"] == content.revision


def test_reuse_keeps_one_content_for_two_paths_of_one_artwork():
    first = candidate("gallery/a.png", size=6)
    second = candidate("gallery/b.png", size=6, file_index=3)
    content = ContentVersion("asset-1", 5, 6, SHA_B)
    items = [
        ObservedPlanItem("work-1", 0, reuse(first, content)),
        ObservedPlanItem("work-1", 1, reuse(second, content)),
    ]
    receipt = make_receipt([
        ArchiveEntry(content, first.path, 0, first.physical_identity),
        ArchiveEntry(content, second.path, 1, second.physical_identity),
    ])
    plan = plan_for(items, receipt, batch_id="batch-reuse")

    planned_paths = [entry.path.relative_path for entry in plan.entries]
    assert planned_paths == ["gallery/a.png", "gallery/b.png"]
    assert plan.inventory.asset_count == 1 and plan.inventory.path_count == 2
    assert plan.inventory.unique_content_count == 1
    assert plan.inventory.unique_content_bytes == 6 and plan.inventory.member_payload_bytes == 12
    assert {asset.content for asset in plan.inventory.assets} == {content}


def test_new_revision_entry_carries_the_higher_revision_and_its_digest():
    snapshot = candidate(size=8)
    previous = ContentVersion("asset-1", 5, 8, SHA_B)
    content = ContentVersion("asset-1", 7, 8, SHA_C)
    resolution = new_revision(snapshot, previous, content)
    receipt = make_receipt([ArchiveEntry(content, snapshot.path, 3, snapshot.physical_identity)])

    plan = plan_for([ObservedPlanItem("work-2", 3, resolution)], receipt)

    asset = plan.inventory.assets[0]
    assert asset.content == content and asset.content.revision == 7
    assert asset.content.sha256 == SHA_C and asset.page_index == 3
    assert asset.physical_identity == snapshot.physical_identity
    assert plan.capacity.peak_additional_bytes >= content.size_bytes


def test_conflicting_decision_is_refused_before_any_builder_runs():
    snapshot = candidate()
    conflict = decision(
        ResolutionKind.CONFLICT, snapshot, None,
        reason="physical_identity_changed_during_observation", next_action="reobserve",
        requires_confirmation=True, scope_complete=False,
    )
    with pytest.raises(ObservedPlanEntryError) as rejected:
        prepare_observed_plan(
            [ObservedPlanItem("work-1", 0, conflict)], batch_id="batch-1", receipt=None,
        )

    assert rejected.value.codes == (ObservedPlanIssueCode.CONTENT_CONFLICT,)
    issue = rejected.value.issues[0]
    assert issue.work_id == "work-1" and issue.page_index == 0
    assert issue.relative_path == "gallery/art.png"
    assert issue.reason == "physical_identity_changed_during_observation"


def test_unconfirmed_same_name_replacement_is_refused_with_the_retry_action():
    snapshot = candidate()
    unconfirmed = decision(
        ResolutionKind.NEEDS_CONFIRMATION, snapshot, None,
        reason="physical_object_replaced", next_action="confirm_same_name_replacement",
        requires_confirmation=True, scope_complete=False,
    )
    with pytest.raises(ObservedPlanEntryError) as rejected:
        archive_entries_from_resolutions([ObservedPlanItem("work-1", 0, unconfirmed)])

    assert rejected.value.codes == (ObservedPlanIssueCode.UNCONFIRMED_REPLACEMENT,)
    assert rejected.value.issues[0].reason.endswith(":confirm_same_name_replacement")


def test_an_unresolved_decision_cannot_carry_content_or_a_complete_scope():
    snapshot = candidate()
    with pytest.raises(ContractError, match="complete"):
        decision(
            ResolutionKind.BASELINE, snapshot, None,
            reason="first_observation_baseline",
        )
    with pytest.raises(ContractError, match="complete scope"):
        decision(
            ResolutionKind.NEEDS_CONFIRMATION, snapshot,
            ContentVersion("asset-1", 1, 4, SHA_A),
            reason="physical_object_replaced",
            next_action="confirm_same_name_replacement",
        )


def test_one_path_may_enter_a_batch_once_even_as_a_case_alias():
    first = candidate("gallery/a.png")
    alias = candidate("gallery/A.png", file_index=3)
    content = ContentVersion("asset-1", 1, 4, SHA_A)
    items = [
        ObservedPlanItem("work-1", 0, reuse(first, content)),
        ObservedPlanItem("work-2", 0, reuse(alias, content)),
    ]
    with pytest.raises(ObservedPlanEntryError) as rejected:
        archive_entries_from_resolutions(items)

    assert rejected.value.codes == (ObservedPlanIssueCode.DUPLICATE_PATH,)
    assert len(rejected.value.issues) == 2
    assert {issue.relative_path for issue in rejected.value.issues} == {
        "gallery/a.png", "gallery/A.png"}


def test_observation_without_physical_identity_is_refused():
    snapshot = candidate(physical=False)
    with pytest.raises(ObservedPlanEntryError) as rejected:
        archive_entries_from_resolutions([ObservedPlanItem("work-1", 0, baseline(snapshot))])

    assert rejected.value.codes == (ObservedPlanIssueCode.MISSING_PHYSICAL_IDENTITY,)


def test_content_size_that_differs_from_the_enumerated_object_is_refused():
    snapshot = candidate(size=4)
    content = ContentVersion("asset-1", 1, 5, SHA_A)
    resolution = baseline(snapshot, content=content)
    with pytest.raises(ObservedPlanEntryError) as rejected:
        archive_entries_from_resolutions([ObservedPlanItem("work-1", 0, resolution)])

    assert rejected.value.codes == (ObservedPlanIssueCode.CONTENT_SIZE_MISMATCH,)
    assert "5" in rejected.value.issues[0].message


def test_two_root_generations_cannot_share_one_batch():
    first, second = candidate("gallery/a.png"), candidate("gallery/b.png", root_revision=2)
    content = ContentVersion("asset-1", 1, 4, SHA_A)
    items = [
        ObservedPlanItem("work-1", 0, reuse(first, content)),
        ObservedPlanItem("work-1", 1, reuse(second, content)),
    ]
    with pytest.raises(ObservedPlanEntryError) as rejected:
        archive_entries_from_resolutions(items)

    assert rejected.value.codes == (ObservedPlanIssueCode.MIXED_ROOT,)
    assert rejected.value.issues[0].relative_path == "gallery/b.png"


def test_decision_without_a_current_digest_is_refused_by_the_inventory_rules():
    snapshot = candidate()
    content = ContentVersion("asset-1", 1, 4, None)
    resolution = baseline(snapshot, content=content)
    receipt = make_receipt([ArchiveEntry(content, snapshot.path, 0, snapshot.physical_identity)])
    with pytest.raises(ObservedPlanEntryError) as rejected:
        plan_for([ObservedPlanItem("work-1", 0, resolution)], receipt)

    assert rejected.value.codes == (ObservedPlanIssueCode.INCOMPLETE_INVENTORY,)
    assert rejected.value.issues[0].reason == "missing_content_digest"


def test_receipt_from_another_root_generation_is_refused_by_the_plan_store():
    snapshot = candidate(root_revision=2)
    content = ContentVersion("asset-1", 1, 4, SHA_A)
    receipt = make_receipt(
        [ArchiveEntry(content, snapshot.path, 0, snapshot.physical_identity)], root_revision=1,
    )
    with pytest.raises(ArchivePlanStoreError, match="root mapping revision"):
        plan_for([ObservedPlanItem("work-1", 0, baseline(snapshot, content=content))], receipt)


def test_receipt_that_names_more_paths_than_the_batch_is_refused():
    snapshot = candidate()
    other = candidate("gallery/other.png", file_index=3)
    content = ContentVersion("asset-1", 1, 4, SHA_A)
    receipt = make_receipt([
        ArchiveEntry(content, snapshot.path, 0, snapshot.physical_identity),
        ArchiveEntry(content, other.path, 1, other.physical_identity),
    ])
    with pytest.raises(ArchivePlanStoreError, match="path count"):
        plan_for([ObservedPlanItem("work-1", 0, baseline(snapshot, content=content))], receipt)


def test_empty_selection_prepares_an_empty_complete_plan():
    receipt = make_receipt([])
    plan = plan_for([], receipt, batch_id="batch-empty")

    assert plan.entries == ()
    assert plan.inventory.scope_complete and plan.inventory.asset_count == 0
    assert plan.inventory.input_entry_count == 0 and plan.inventory.packages == ()
    assert plan.capacity.peak_additional_bytes == 0


def test_illegal_work_or_page_fails_the_core_objects_own_checks():
    snapshot = candidate()
    resolution = baseline(snapshot)
    receipt = make_receipt([
        ArchiveEntry(resolution.content, snapshot.path, 0, snapshot.physical_identity)])
    with pytest.raises(ContractError, match="artwork ID"):
        plan_for([ObservedPlanItem("bad work", 0, resolution)], receipt)
    with pytest.raises(ContractError, match="page index"):
        plan_for([ObservedPlanItem("work-1", -1, resolution)], receipt)
    with pytest.raises(ContractError, match="page index"):
        plan_for([ObservedPlanItem("work-1", 10000, resolution)], receipt)
