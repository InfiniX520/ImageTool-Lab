"""Pure contract tests for the U1-A artifact current-health layer.

No production library, network or account is touched.  These tests exercise
the state machine and the writer-facing payload without relying on the
repository's reuse paths.
"""
from __future__ import annotations

import hashlib

import pytest

from core.artifact_health import (
    ArtifactExpectation,
    ArtifactHealth,
    ArtifactHealthPolicy,
    HealthPolicyMode,
    artifact_health_allows_reuse,
    assess_observation,
    unobserved_health,
)
from core.local_observation import (
    LocalObservation,
    LocalObserver,
    ObservationRequest,
    ObservationStatus,
)
from core.preservation_policy import ContentVersion, LocalEvidence, LocalState
from core.task_contracts import ContractError, PathRef
from image_fixtures import PNG_BYTES

TIME = "2026-11-13T10:00:00+08:00"
PATH = PathRef(relative_path="downl_Pixiv/123_p0.png")
SHA = hashlib.sha256(PNG_BYTES).hexdigest()


def expectation(path=PATH, *, sha=SHA, size=len(PNG_BYTES)):
    return ArtifactExpectation(
        artifact_id="a" * 32,
        delivery_id="d" * 32,
        job_id="j" * 32,
        illust_id="123",
        page_index=0,
        output_key=path.target_key,
        path=path,
        root_revision=1,
        size_bytes=size,
        sha256=sha,
        media_type="image/png",
        quality_key="q" * 64,
    )


def row(exp=None, **changes):
    exp = expectation() if exp is None else exp
    value = {
        "artifact_id": exp.artifact_id,
        "delivery_id": exp.delivery_id,
        "job_id": exp.job_id,
        "illust_id": exp.illust_id,
        "page_index": exp.page_index,
        "output_key": exp.output_key,
        "size": exp.size_bytes,
        "sha256": exp.sha256,
        "media_type": exp.media_type,
    }
    value.update(changes)
    return value


def observer(root, *, revision=1):
    return LocalObserver(root, root_id="project", root_revision=revision, _clock=lambda: TIME)


def observe(root, exp=None, *, data=None):
    exp = expectation() if exp is None else exp
    target = root / exp.path.relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if data is not None:
        target.write_bytes(data)
    return observer(root).observe(
        ObservationRequest(exp.content, exp.path, run_id="run-1", check_id="check-1")
    )


def test_hash_match_round_trips_and_is_reusable(tmp_path):
    exp = expectation()
    target = tmp_path / exp.path.relative_path
    target.parent.mkdir(parents=True)
    target.write_bytes(PNG_BYTES)
    health = assess_observation(exp, observe(tmp_path, exp))
    assert health.state is LocalState.HASH_MATCH
    assert health.reusable
    assert health.observed_size == len(PNG_BYTES)
    assert health.observed_sha256 == SHA
    assert health.identity is not None
    again = ArtifactHealth.from_payload(health.to_payload())
    assert again == health


def test_missing_changed_and_root_unavailable_are_not_reusable(tmp_path):
    exp = expectation()
    target = tmp_path / exp.path.relative_path
    target.parent.mkdir(parents=True)
    missing = assess_observation(exp, observer(tmp_path).observe(
        ObservationRequest(exp.content, exp.path, run_id="run-1", check_id="check-1")
    ))
    assert missing.state is LocalState.MISSING
    assert not missing.reusable

    target.write_bytes(b"X" * len(PNG_BYTES))
    changed = assess_observation(exp, observe(tmp_path, exp))
    assert changed.state is LocalState.CHANGED
    assert changed.observed_size == len(PNG_BYTES)
    assert changed.observed_sha256 == hashlib.sha256(b"X" * len(PNG_BYTES)).hexdigest()
    assert not changed.reusable

    other = tmp_path / "absent-root"
    offline = assess_observation(exp, observer(other).observe(
        ObservationRequest(exp.content, exp.path, run_id="run-1", check_id="check-1")
    ))
    assert offline.state is LocalState.ROOT_UNAVAILABLE
    assert not offline.reusable


def _evidence(state, *, size=None, sha=None):
    return LocalEvidence(
        content=ContentVersion("a" * 32, 1, len(PNG_BYTES), SHA),
        path=PATH,
        root_revision=1,
        run_id="run-1",
        check_id="check-1",
        checked_at=TIME,
        state=state,
        observed_size=size,
        observed_sha256=sha,
    )


@pytest.mark.parametrize(
    "status,state",
    [
        (ObservationStatus.UNREADABLE, LocalState.UNREADABLE),
        (ObservationStatus.CHANGED_DURING_CHECK, LocalState.CHANGED_DURING_CHECK),
        (ObservationStatus.PATH_REJECTED, LocalState.UNKNOWN),
        (ObservationStatus.TIME_BUDGET_EXHAUSTED, LocalState.UNKNOWN),
        (ObservationStatus.BYTE_BUDGET_EXHAUSTED, LocalState.UNKNOWN),
        (ObservationStatus.CANCELLED, LocalState.UNKNOWN),
    ],
)
def test_incomplete_observations_never_become_reusable(status, state):
    exp = expectation()
    evidence_state = (
        LocalState.UNKNOWN
        if status is ObservationStatus.BASELINE_OBSERVED
        else {
            ObservationStatus.UNREADABLE: LocalState.UNREADABLE,
            ObservationStatus.CHANGED_DURING_CHECK: LocalState.CHANGED_DURING_CHECK,
            ObservationStatus.PATH_REJECTED: LocalState.UNKNOWN,
            ObservationStatus.TIME_BUDGET_EXHAUSTED: LocalState.UNKNOWN,
            ObservationStatus.BYTE_BUDGET_EXHAUSTED: LocalState.UNKNOWN,
            ObservationStatus.CANCELLED: LocalState.UNKNOWN,
        }[status]
    )
    observation = LocalObservation(
        status=status,
        reason="synthetic_reason",
        evidence=_evidence(evidence_state),
    )
    health = assess_observation(exp, observation)
    assert health.state is state
    assert not health.reusable
    assert health.observed_sha256 is None


def test_scope_mismatch_is_unknown_even_if_the_file_matches(tmp_path):
    exp = expectation()
    target = tmp_path / exp.path.relative_path
    target.parent.mkdir(parents=True)
    target.write_bytes(PNG_BYTES)
    other = expectation(PathRef(relative_path="downl_Pixiv/other.png"))
    health = assess_observation(other, observe(tmp_path, exp))
    assert health.state is LocalState.UNKNOWN
    assert health.reason == "observation_scope_mismatch"
    assert not health.reusable


def test_quick_match_only_works_under_an_explicit_quick_policy(tmp_path):
    exp = expectation()
    quick = ArtifactHealth(
        expectation=exp,
        policy=ArtifactHealthPolicy(mode=HealthPolicyMode.QUICK),
        state=LocalState.QUICK_MATCH,
        checked_at=TIME,
        reason="quick_metadata",
        observed_size=exp.size_bytes,
    )
    assert quick.reusable
    strict = ArtifactHealth(
        expectation=exp,
        policy=ArtifactHealthPolicy(),
        state=LocalState.QUICK_MATCH,
        checked_at=TIME,
        reason="quick_metadata",
        observed_size=exp.size_bytes,
    )
    assert not strict.reusable


def test_unobserved_health_cannot_claim_a_match():
    with pytest.raises(ContractError):
        unobserved_health(
            expectation(), state=LocalState.HASH_MATCH, reason="fake", checked_at=TIME
        )
    health = unobserved_health(
        expectation(), state=LocalState.ROOT_UNAVAILABLE, reason="root_missing", checked_at=TIME
    )
    assert not health.reusable


def test_health_gate_requires_fresh_matching_row_and_root_revision():
    exp = expectation()
    health = ArtifactHealth(
        expectation=exp,
        policy=ArtifactHealthPolicy(max_age_seconds=60),
        state=LocalState.HASH_MATCH,
        checked_at=TIME,
        reason="full_sha256_match",
        observed_size=exp.size_bytes,
        observed_sha256=exp.sha256,
    )
    assert artifact_health_allows_reuse(
        health, row(exp), now="2026-11-13T10:00:30+08:00", root_revision=1
    )
    assert not artifact_health_allows_reuse(
        health, row(exp), now="2026-11-13T10:02:00+08:00", root_revision=1
    )
    assert not artifact_health_allows_reuse(
        health, row(exp, sha256="b" * 64), now="2026-11-13T10:00:30+08:00", root_revision=1
    )
    assert not artifact_health_allows_reuse(
        health, row(exp), now="2026-11-13T10:00:30+08:00", root_revision=2
    )


def test_payload_is_strict_about_shape_and_identity():
    health = ArtifactHealth(
        expectation=expectation(),
        policy=ArtifactHealthPolicy(),
        state=LocalState.MISSING,
        checked_at=TIME,
        reason="path_not_found",
    )
    payload = health.to_payload()
    assert ArtifactHealth.from_payload(payload) == health
    with pytest.raises(ContractError):
        ArtifactHealth.from_payload({key: value for key, value in payload.items() if key != "state"})
    with pytest.raises(ContractError):
        ArtifactHealth.from_payload({**payload, "extra": True})
    with pytest.raises(ContractError):
        ArtifactHealth.from_payload({**payload, "device": 1, "file_index": None})


@pytest.mark.parametrize("field,value", [
    ("size_bytes", 0),
    ("sha256", "0" * 63),
    ("sha256", "Z" * 64),
    ("media_type", "text/plain"),
    ("root_revision", -1),
    ("page_index", -1),
])
def test_expectation_rejects_invalid_evidence(field, value):
    values = {
        "artifact_id": "a" * 32, "delivery_id": "d" * 32, "job_id": "j" * 32,
        "illust_id": "123", "page_index": 0, "output_key": PATH.target_key,
        "path": PATH, "root_revision": 1, "size_bytes": len(PNG_BYTES),
        "sha256": SHA, "media_type": "image/png", "quality_key": "q" * 64,
    }
    values[field] = value
    with pytest.raises(ContractError):
        ArtifactExpectation(**values)


def test_policy_rejects_unknown_version_and_mode():
    with pytest.raises(ContractError):
        ArtifactHealthPolicy(version=2)
    with pytest.raises(ContractError):
        ArtifactHealthPolicy(mode="full")
    with pytest.raises(ContractError):
        ArtifactHealthPolicy(max_age_seconds=0)
