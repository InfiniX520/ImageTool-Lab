"""U1-A state contract: the pure decisions every repair lane shares.

These tests bind only the frozen contract modules; they never open a library
database, a real file or the network.  They exist so the five parallel repair
lanes cannot drift back into "absent evidence equals unhealthy evidence".
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from core.artifact_health import (
    ArtifactExpectation,
    ArtifactHealthPolicy,
    unobserved_health,
)
from core.observation_snapshot import (
    PageKey,
    PageSnapshot,
    PreflightSnapshot,
    PreflightUnavailable,
    RetryPreflight,
    SnapshotError,
    empty_snapshot,
)
from core.preservation_policy import LocalState, RemovalIntent
from core.removal_scope import (
    CANCELLABLE_STATES,
    PUBLISHED_STATES,
    CorruptIntentError,
    IntentKey,
    IntentProjection,
    IntentRecord,
    read_projection,
)
from core.task_contracts import PathRef


def _ref(illust: str = "100", page: int = 0) -> PathRef:
    return PathRef(
        kind="project", root_id="project", relative_path=f"downloads/{illust}/{page}.png"
    )


def _health(
    state: LocalState = LocalState.MISSING,
    *,
    illust: str = "100",
    page: int = 0,
    artifact: str = "a" * 32,
    revision: int = 1,
    quality: str = "q" * 64,
):
    path = _ref(illust, page)
    expectation = ArtifactExpectation(
        artifact_id=artifact,
        delivery_id="d" * 32,
        job_id="j" * 32,
        illust_id=illust,
        page_index=page,
        output_key=path.target_key,
        path=path,
        root_revision=revision,
        size_bytes=10,
        sha256="0" * 64,
        media_type="image/png",
        quality_key=quality,
        policy_version=1,
    )
    return unobserved_health(
        expectation,
        state=state,
        reason="artifact_missing",
        checked_at=datetime.now(timezone.utc).isoformat(),
        policy=ArtifactHealthPolicy(),
    )


def _key(illust: str = "100", page: int = 0, revision: int = 1) -> PageKey:
    return PageKey(
        illust_id=illust,
        page_index=page,
        output_key=_ref(illust, page).target_key,
        quality_key="q" * 64,
    )


def _page(**overrides) -> PageSnapshot:
    fields = dict(
        key=_key(),
        generation=1,
        delivery_id="d" * 32,
        observed=(_health(),),
        unobserved=(),
        degraded=False,
        reason="ok",
    )
    fields.update(overrides)
    return PageSnapshot(**fields)


def _snapshot(*pages: PageSnapshot, degraded: bool = False) -> PreflightSnapshot:
    return PreflightSnapshot(
        run_id="batch-test",
        pages={page.key.storage_key: page for page in pages},
        degraded=degraded,
        reason="no_candidates" if degraded else "ok",
    )


def test_page_snapshot_rejects_a_foreign_health_record():
    with pytest.raises(SnapshotError):
        _page(observed=(_health(illust="999"),))


def test_page_snapshot_rejects_an_artifact_both_observed_and_unobserved():
    with pytest.raises(SnapshotError):
        _page(observed=(_health(),), unobserved=("a" * 32,))


def test_snapshot_round_trip_keeps_evidence_and_identity():
    snapshot = _snapshot(_page(unobserved=("b" * 32,)))
    restored = PreflightSnapshot.from_payload(snapshot.to_payload())
    page = restored.page(_key())
    assert page is not None
    assert page.generation == 1 and page.delivery_id == "d" * 32
    assert page.health_for("a" * 32).state is LocalState.MISSING
    assert page.health_for("b" * 32) is None
    assert restored.policy == ArtifactHealthPolicy()


def test_degraded_snapshot_claims_no_page():
    snapshot = empty_snapshot("batch-degraded")
    assert snapshot.degraded is True
    assert snapshot.page(_key()) is None


def test_writer_must_repreflight_when_the_completed_generation_moved():
    page = _page()
    assert page.stale_for(1, "d" * 32) is False
    assert page.stale_for(2, "e" * 32) is True
    assert page.stale_for(1, "e" * 32) is True


def test_unobserved_candidate_never_becomes_evidence():
    page = _page(observed=(), unobserved=("c" * 32,))
    assert page.covers("c" * 32) is False
    assert page.health_for("c" * 32) is None


def test_retry_and_failure_carry_machine_readable_reasons():
    assert RetryPreflight("generation_advanced").reason == "generation_advanced"
    assert PreflightUnavailable("preflight_unavailable").reason == "preflight_unavailable"
    with pytest.raises(SnapshotError):
        RetryPreflight("Not A Reason")


def _intent_db(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE events(event_seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, "
        "safe_payload TEXT NOT NULL, accepted_command_id TEXT)"
    )
    for seq, payload in rows:
        conn.execute(
            "INSERT INTO events(event_seq, kind, safe_payload, accepted_command_id) "
            "VALUES (?, 'removal_intent', ?, ?)",
            (seq, payload, f"cmd-{seq}"),
        )
    return conn


def _payload(illust="100", page=0, key=None, intent="excluded", revision=1):
    import json

    return json.dumps(
        {
            "illust_id": illust,
            "page_index": page,
            "output_key": key or _ref(illust, page).target_key,
            "root_revision": revision,
            "intent": intent,
        }
    )


def test_latest_intent_wins_and_the_root_revision_is_part_of_the_key():
    key = _ref().target_key
    conn = _intent_db(
        [
            (1, _payload(intent="excluded", revision=1)),
            (2, _payload(intent="none", revision=1)),
            (3, _payload(intent="excluded", revision=2)),
        ]
    )
    projection = read_projection(conn)
    old = IntentKey("100", 0, key, 1)
    new = IntentKey("100", 0, key, 2)
    assert projection.state(old) is RemovalIntent.NONE
    assert projection.state(new) is RemovalIntent.EXCLUDED
    assert projection.is_excluded(new) is True
    assert projection.publish_allowed(old) is True
    assert projection.publish_allowed(new) is False


def test_corrupt_intent_blocks_claim_publish_and_repair():
    key = _ref().target_key
    conn = _intent_db([(1, _payload(intent="excluded")), (2, "{broken")])
    projection = read_projection(conn)
    assert projection.corrupt_events == (2,)
    with pytest.raises(CorruptIntentError) as caught:
        projection.require_clean()
    assert caught.value.event_seq == 2
    intent_key = IntentKey("100", 0, key, 1)
    assert projection.blocks_claim(intent_key) is True
    assert projection.blocks_repair(intent_key) is True
    assert projection.publish_allowed(intent_key) is False


def test_unknown_intent_value_is_corrupt_not_none():
    conn = _intent_db([(1, _payload(intent="deleted_by_user"))])
    projection = read_projection(conn)
    assert projection.corrupt_events == (1,)


def test_offloaded_intent_is_recorded_but_never_blocks_publication():
    conn = _intent_db([(1, _payload(intent="offloaded"))])
    projection = read_projection(conn)
    key = IntentKey("100", 0, _ref().target_key, 1)
    assert projection.require_clean() is None
    assert projection.state(key) is RemovalIntent.OFFLOADED
    assert projection.blocks_claim(key) is False
    assert projection.publish_allowed(key) is True


def test_intent_state_defaults_to_none_for_an_unrecorded_page():
    projection = read_projection(_intent_db([]))
    key = IntentKey("100", 0, _ref().target_key, 1)
    assert projection.state(key) is RemovalIntent.NONE
    assert projection.blocks_claim(key) is False
    assert projection.publish_allowed(key) is True


def test_published_and_transferring_states_are_never_cancelled_by_intent():
    for state in ("prepared", "completed", "transferring", "validating", "recovery_needed"):
        assert state not in CANCELLABLE_STATES
    assert set(PUBLISHED_STATES) == {"prepared", "completed"}
    assert set(CANCELLABLE_STATES) == {
        "pending",
        "suspended",
        "retry_wait",
        "needs_verification",
    }


def test_intent_key_rejects_a_negative_root_revision():
    with pytest.raises(Exception):
        IntentKey("100", 0, _ref().target_key, -1)


def test_projection_can_be_built_from_records_without_sql():
    key = IntentKey("100", 0, _ref().target_key, 1)
    projection = IntentProjection(
        records={key.storage_key: IntentRecord(RemovalIntent.EXCLUDED, 4, "cmd")},
        corrupt_events=(),
        max_seq=4,
    )
    assert projection.is_excluded(key) is True
    assert projection.blocks_claim(key) is True
