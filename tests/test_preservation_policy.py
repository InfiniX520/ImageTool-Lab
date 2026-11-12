"""Preservation scenarios: local verification and intentional cloud offloading."""
from dataclasses import FrozenInstanceError, replace

import pytest

from core.preservation_policy import (
    ArchiveEvidence, ArchiveState, ContentVersion, LocalEvidence, LocalState,
    PreservationOutcome as Outcome, PreservationPolicy, RemovalIntent, assess_preservation,
)
from core.task_contracts import ContractError, PathRef


CONTENT = ContentVersion("asset-42-p0", 1, 2048, "a" * 64)
PATH = PathRef(relative_path="downl_Pixiv/artist/42_p0.png")
TIME = "2026-11-11T10:00:00+08:00"


def local(state, **overrides):
    values = dict(content=CONTENT, path=PATH, root_revision=3, run_id="run-1",
                  check_id="local-1", checked_at=TIME, state=state)
    if state in {LocalState.HASH_MATCH, LocalState.QUICK_MATCH}:
        values["observed_size"] = CONTENT.size_bytes
    if state is LocalState.HASH_MATCH:
        values["observed_sha256"] = CONTENT.sha256
    values.update(overrides)
    return LocalEvidence(**values)


def archived(state=ArchiveState.USER_RETURNED_VERIFIED, **overrides):
    values = dict(content=CONTENT, state=state, check_id="archive-1",
                  checked_at=TIME, location_ref="quark-package-1")
    values.update(overrides)
    return ArchiveEvidence(**values)


def assess(**kwargs):
    values = dict(content=CONTENT, path=PATH, root_revision=3, run_id="run-1")
    values.update(kwargs)
    return assess_preservation(**values)


def test_intentional_offload_reuses_archive_without_claiming_local_file_exists():
    result = assess(local=local(LocalState.MISSING), archive=archived(),
                    intent=RemovalIntent.OFFLOADED)
    assert result.outcome is Outcome.SKIPPED_ARCHIVED
    assert result.archive_verified and not result.local_hash_verified
    assert result.local_state is LocalState.MISSING
    assert result.archive_state is ArchiveState.USER_RETURNED_VERIFIED


def test_user_deleted_file_after_verified_archiving_does_not_require_fabricated_release_event():
    assert assess(local=local(LocalState.MISSING), archive=archived()).outcome is Outcome.SKIPPED_ARCHIVED


@pytest.mark.parametrize("evidence,intent", [
    (None, RemovalIntent.OFFLOADED),
    (archived(ArchiveState.UPLOAD_REPORTED), RemovalIntent.NONE),
])
def test_historical_transfer_or_upload_report_never_becomes_verified(evidence, intent):
    result = assess(local=local(LocalState.MISSING), archive=evidence, intent=intent)
    assert result.outcome is Outcome.OFFLOADED_UNVERIFIED
    assert not result.archive_verified and not result.local_hash_verified


def test_missing_without_transfer_evidence_requires_location_or_user_choice():
    assert assess(local=local(LocalState.MISSING)).outcome is Outcome.LOCATE_MISSING


def test_local_copy_policy_offers_restore_without_marking_local_complete():
    result = assess(local=local(LocalState.MISSING), archive=archived(),
                    policy=PreservationPolicy(required_copy="local"))
    assert result.outcome is Outcome.RESTORE_OFFERED
    assert result.archive_verified and not result.local_hash_verified


def test_excluded_missing_file_is_not_proposed_for_automatic_restore():
    result = assess(local=local(LocalState.MISSING), archive=archived(),
                    policy=PreservationPolicy(required_copy="local"),
                    intent=RemovalIntent.EXCLUDED)
    assert result.outcome is Outcome.EXCLUDED


@pytest.mark.parametrize("state,expected", [
    (LocalState.CHANGED, Outcome.REVIEW_CONFLICT),
    (LocalState.CHANGED_DURING_CHECK, Outcome.WAIT_STABLE),
])
def test_modified_local_file_is_preserved_even_with_verified_archive(state, expected):
    assert assess(local=local(state), archive=archived()).outcome is expected


@pytest.mark.parametrize("state", [LocalState.ROOT_UNAVAILABLE, LocalState.UNREADABLE])
def test_inaccessible_root_is_not_missing_and_restore_is_not_scheduled(state):
    result = assess(local=local(state), archive=archived(),
                    policy=PreservationPolicy(required_copy="local"))
    assert result.outcome is Outcome.CHECK_LOCAL_ACCESS
    assert result.local_state is state
    saved = assess(local=local(state), archive=archived())
    assert saved.outcome is Outcome.SKIPPED_ARCHIVED
    assert saved.local_state is state  # Archive availability does not change local facts.


def test_quick_check_never_satisfies_full_hash_policy():
    observation = local(LocalState.QUICK_MATCH)
    assert assess(local=observation).outcome is Outcome.VERIFY_LOCAL
    quick = assess(local=observation, policy=PreservationPolicy(local_check="quick"))
    assert quick.outcome is Outcome.REUSE_LOCAL_QUICK and not quick.local_hash_verified


def test_matching_local_digest_and_manual_archive_are_independent_proofs():
    result = assess(local=local(LocalState.HASH_MATCH), archive=archived(ArchiveState.ACCESS_FAILED))
    assert result.outcome is Outcome.REUSE_LOCAL and result.local_hash_verified
    assert not result.archive_verified


def test_failed_latest_archive_access_does_not_hide_behind_offload_intent():
    result = assess(local=local(LocalState.MISSING), archive=archived(ArchiveState.ACCESS_FAILED),
                    intent=RemovalIntent.OFFLOADED)
    assert result.outcome is Outcome.CHECK_ARCHIVE_ACCESS and not result.archive_verified


@pytest.mark.parametrize("overrides", [
    {"run_id": "run-2"}, {"root_revision": 4},
    {"path": PathRef(relative_path="elsewhere/42_p0.png")},
    {"path": PathRef(kind="external", root_id="other", relative_path=PATH.relative_path)},
    {"content": replace(CONTENT, revision=2)},
    {"content": replace(CONTENT, asset_id="asset-43-p0")},
])
def test_local_evidence_from_another_run_root_path_or_content_cannot_be_reused(overrides):
    result = assess(local=local(LocalState.HASH_MATCH), **overrides)
    assert result.outcome is Outcome.VERIFY_LOCAL
    assert result.ignored_local_evidence and not result.local_hash_verified


def test_windows_case_and_slash_variations_do_not_create_another_path_identity():
    result = assess(local=local(LocalState.HASH_MATCH),
                    path=PathRef(relative_path="DOWNL_PIXIV\\ARTIST\\42_P0.PNG"))
    assert result.outcome is Outcome.REUSE_LOCAL


@pytest.mark.parametrize("content", [
    replace(CONTENT, revision=2), replace(CONTENT, asset_id="another"),
    replace(CONTENT, sha256="b" * 64), replace(CONTENT, size_bytes=2049),
])
def test_archive_evidence_must_match_exact_content_version(content):
    result = assess(local=local(LocalState.MISSING), archive=archived(content=content))
    assert result.outcome is Outcome.LOCATE_MISSING
    assert result.ignored_archive_evidence and not result.archive_verified


def test_verified_archive_survives_local_directory_remapping():
    result = assess(archive=archived(), root_revision=99,
                    path=PathRef(relative_path="new_root/42_p0.png"))
    assert result.outcome is Outcome.SKIPPED_ARCHIVED


def test_unknown_historical_hash_cannot_be_promoted_to_hash_match_or_verified_archive():
    legacy = replace(CONTENT, sha256=None)
    with pytest.raises(ContractError):
        local(LocalState.HASH_MATCH, content=legacy)
    with pytest.raises(ContractError):
        archived(content=legacy)
    result = assess(content=legacy, intent=RemovalIntent.OFFLOADED)
    assert result.outcome is Outcome.OFFLOADED_UNVERIFIED


def test_same_size_different_hash_cannot_claim_matching_bytes():
    with pytest.raises(ContractError):
        local(LocalState.HASH_MATCH, observed_sha256="b" * 64)


def test_manual_and_adapter_provenance_remain_distinct():
    for state in (ArchiveState.USER_RETURNED_VERIFIED, ArchiveState.ADAPTER_READBACK_VERIFIED):
        result = assess(archive=archived(state))
        assert result.outcome is Outcome.SKIPPED_ARCHIVED
        assert result.archive_state is state


@pytest.mark.parametrize("factory", [
    lambda: ContentVersion("asset", True, 1),
    lambda: replace(CONTENT, size_bytes=-1),
    lambda: replace(CONTENT, sha256="not-a-digest"),
    lambda: PreservationPolicy(schema_version=True),
    lambda: PreservationPolicy(schema_version=2),
    lambda: PreservationPolicy(local_check="trust_anything"),
    lambda: local(LocalState.HASH_MATCH, checked_at="2026-11-11T10:00:00"),
    lambda: local(LocalState.HASH_MATCH, observed_size=True),
    lambda: local(LocalState.HASH_MATCH, observed_size=None),
    lambda: replace(local(LocalState.HASH_MATCH), state="hash_match"),
    lambda: local(LocalState.QUICK_MATCH, observed_sha256="a" * 64),
    lambda: archived(location_ref=None),
    lambda: archived(check_id=""),
    lambda: assess(intent="offloaded"),
])
def test_malformed_evidence_or_policy_fails_closed(factory):
    with pytest.raises(ContractError):
        factory()


def test_decision_does_not_mutate_historical_facts_or_observations():
    observation = local(LocalState.MISSING)
    record = archived()
    result = assess(local=observation, archive=record)
    assert result.outcome is Outcome.SKIPPED_ARCHIVED
    assert observation.state is LocalState.MISSING and record.content is CONTENT
    with pytest.raises(FrozenInstanceError):
        observation.state = LocalState.HASH_MATCH
