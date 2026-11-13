"""UA-03/04/05/06: recovery selects one observation and honours ownership.

The lane contract lives in docs/optimization/2026-11-13/U1A_STATE_CONTRACT.md.
Every scenario is synthetic: one temporary SQLite ledger, one temporary output
root, real PNG bytes served by a fake transport.  No network, account, gallery
or production library is used.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import test_u1a_recovery as h
import test_u1a_reuse_gate as g

ARTWORK = h.ARTWORK


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _settle(repo, job):
    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"])


def _artifact_row(repo, artifact_id):
    rows = h.query(
        repo.database_path,
        "SELECT * FROM artifacts WHERE artifact_id = ?",
        (artifact_id,),
    )
    assert len(rows) == 1
    return rows[0]


def _delivery_row(repo, delivery_id):
    rows = h.query(
        repo.database_path,
        "SELECT * FROM deliveries WHERE delivery_id = ?",
        (delivery_id,),
    )
    assert len(rows) == 1
    return rows[0]


def _page_link(repo, batch_id):
    rows = h.query(
        repo.database_path,
        "SELECT * FROM batch_item_pages WHERE batch_id = ? AND illust_id = ? "
        "AND page_index = 0",
        (batch_id, ARTWORK),
    )
    assert len(rows) == 1
    return rows[0]


def _table_snapshot(repo):
    return {
        "deliveries": h.query(repo.database_path, "SELECT * FROM deliveries ORDER BY rowid"),
        "artifacts": h.query(repo.database_path, "SELECT * FROM artifacts ORDER BY rowid"),
        "pages": h.query(repo.database_path, "SELECT * FROM batch_item_pages ORDER BY rowid"),
        "items": h.query(repo.database_path, "SELECT * FROM batch_items ORDER BY rowid"),
    }


def _two_generations(repo, root):
    """Artifact A (original bytes) and a later artifact B (same-size PNG)."""
    first_job, first_delivery = h.first_delivery(repo, root)
    artifact_a = repo.get_artifact(first_delivery)["artifact_id"]
    h.target(root).unlink()
    second_job, (repair,) = h.second_task(repo, root)
    outcome = h.run(
        repo, second_job, repair, h.executor(repo, root, h.Transport(h.Response(h.SAME_SIZE_PNG)))
    )
    assert outcome.state == "completed", outcome
    _settle(repo, second_job)
    artifact_b = repo.get_artifact(repair)["artifact_id"]
    # Nothing matches now: the third task parks the page as a real hold.
    h.target(root).write_bytes(h.NOT_AN_IMAGE)
    third_job, (hold,) = h.second_task(repo, root, request_id="third")
    assert repo.get_delivery(hold)["state"] == "needs_verification"
    return first_job, artifact_a, artifact_b, third_job, hold


def _reuse_payload(repo, batch_id, hold_id, artifact_id, health, *, page_key=None):
    delivery = repo.get_delivery(hold_id)
    return {
        "batch_id": batch_id,
        "illust_id": ARTWORK,
        "page_index": 0,
        "command_id": None,
        "previous_delivery_id": hold_id,
        "previous_state": delivery["state"],
        "generation": delivery["generation"],
        "page_key": page_key
        or {
            "illust_id": delivery["illust_id"],
            "page_index": delivery["page_index"],
            "output_key": delivery["output_key"],
            "quality_key": delivery["quality_key"],
        },
        "action": "reuse",
        "artifact_id": artifact_id,
        "selected_state": "hash_match",
        "health": [health],
        "draft": {"outcome": "reused", "reason": "hash_match"},
    }


def _health_for(repo, artifact_id):
    row = _artifact_row(repo, artifact_id)
    resolver = repo._path_resolver_snapshot()
    assert resolver is not None
    delivery = _delivery_row(repo, row["delivery_id"])
    record = repo._observe_artifact(
        row, resolver=resolver, run_id="ownership-probe", quality_key=delivery["quality_key"]
    )
    assert record is not None
    return record


def _stale(record):
    return replace(
        record,
        checked_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )


# ---------------------------------------------------------------------------
# UA-04: the decision, the digest and the artifact id are one record
# ---------------------------------------------------------------------------


def test_reuse_selects_the_matching_older_generation(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, artifact_a, artifact_b, job, hold = _two_generations(repo, root)
        h.target(root).write_bytes(h.PNG_BYTES)

        result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

        assert result["outcome"] == "reused", result
        assert result["artifact_id"] == artifact_a
        assert result["artifact_id"] != artifact_b
        assert result["observed_sha256"] == _sha(h.PNG_BYTES)
        assert result["observed_size"] == len(h.PNG_BYTES)
        assert result["health"]["artifact_id"] == artifact_a
        assert result["health"]["sha256"] == _sha(h.PNG_BYTES)
        assert result["checked_at"] == result["health"]["checked_at"]
        link = _page_link(repo, job["batch_id"])
        assert link["artifact_id"] == artifact_a
        assert link["delivery_id"] is None  # the artifact belongs to another job
        assert repo.get_delivery(hold)["state"] == "cancelled"
        assert repo.get_delivery(hold)["error_code"] == "recovered_hash_match"
        assert repo.list_dispatchable(job["job_id"]) == ()


def test_reuse_selects_the_matching_newer_generation(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, artifact_a, artifact_b, job, hold = _two_generations(repo, root)
        h.target(root).write_bytes(h.SAME_SIZE_PNG)

        result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

        assert result["outcome"] == "reused", result
        assert result["artifact_id"] == artifact_b
        assert result["artifact_id"] != artifact_a
        assert result["observed_sha256"] == _sha(h.SAME_SIZE_PNG)
        assert result["health"]["artifact_id"] == artifact_b


def test_no_matching_generation_keeps_the_hold_and_claims_no_artifact(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, artifact_a, artifact_b, job, hold = _two_generations(repo, root)
        before = _table_snapshot(repo)
        before_row = _delivery_row(repo, hold)

        result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

        assert result["outcome"] == "held", result
        assert result["reason"] == "artifact_changed"
        assert result["artifact_id"] is None
        assert result["observed_state"] == "changed"
        assert result["observed_sha256"] == _sha(h.NOT_AN_IMAGE)
        assert result["health"]["artifact_id"] in {artifact_a, artifact_b}
        assert result["health"]["state"] == "changed"
        assert result["state"] == "needs_verification"
        assert _delivery_row(repo, hold) == before_row
        assert _table_snapshot(repo) == before


# ---------------------------------------------------------------------------
# UA-06: reuse, repair and park share one freshness/root gate
# ---------------------------------------------------------------------------


def test_stale_missing_evidence_cannot_queue_a_repair(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, hold, _ = h.parked_hold(repo, root)
        h.target(root).unlink()
        before = _table_snapshot(repo)
        observe = repo._observe_artifact

        def stale(*args, **kwargs):
            record = observe(*args, **kwargs)
            return None if record is None else _stale(record)

        monkeypatch.setattr(repo, "_observe_artifact", stale)
        result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

        assert result["outcome"] == "refused", result
        assert result["reason"] == "stale_observation"
        assert repo.get_delivery(hold)["state"] == "needs_verification"
        assert repo.list_dispatchable(job["job_id"]) == ()
        assert _table_snapshot(repo) == before


def test_stale_changed_evidence_cannot_park_a_queued_repair(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, repair, _ = h.queued_repair(repo, root)
        h.target(root).write_bytes(h.NOT_AN_IMAGE)
        before = _table_snapshot(repo)
        observe = repo._observe_artifact

        def stale(*args, **kwargs):
            record = observe(*args, **kwargs)
            return None if record is None else _stale(record)

        monkeypatch.setattr(repo, "_observe_artifact", stale)
        result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

        assert result["outcome"] == "refused", result
        assert result["reason"] == "stale_observation"
        assert repo.get_delivery(repair)["state"] == "pending"
        assert _table_snapshot(repo) == before


def test_a_root_revision_change_between_observation_and_write_is_refused(
    tmp_path, monkeypatch
):
    root = tmp_path / "library"
    root.mkdir()
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, hold, _ = h.parked_hold(repo, root)
        h.target(root).write_bytes(h.PNG_BYTES)
        before = _table_snapshot(repo)
        observe = repo._observe_artifact

        def remapped(*args, **kwargs):
            record = observe(*args, **kwargs)
            if record is None:
                return None
            expectation = replace(
                record.expectation,
                root_revision=record.expectation.root_revision + 1,
            )
            return replace(record, expectation=expectation)

        monkeypatch.setattr(repo, "_observe_artifact", remapped)
        result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

        assert result["outcome"] == "refused", result
        assert result["reason"] == "stale_preflight"
        assert repo.get_delivery(hold)["state"] == "needs_verification"
        assert _table_snapshot(repo) == before


def test_an_observation_for_another_page_cannot_authorize_reuse(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, hold, first = h.parked_hold(repo, root)
        h.target(root).write_bytes(h.PNG_BYTES)
        record = _health_for(repo, repo.get_artifact(first)["artifact_id"])
        assert record.state.value == "hash_match"
        before = _table_snapshot(repo)
        hold_row = _delivery_row(repo, hold)
        payload = _reuse_payload(
            repo,
            job["batch_id"],
            hold,
            record.expectation.artifact_id,
            record.to_payload(),
            page_key={
                "illust_id": "456",
                "page_index": 0,
                "output_key": hold_row["output_key"],
                "quality_key": hold_row["quality_key"],
            },
        )

        result = repo._enqueue("recover_verification_page", payload)

        assert result["outcome"] == "refused", result
        assert result["reason"] == "observation_page_mismatch"
        assert _table_snapshot(repo) == before


def test_parking_an_offline_repair_removes_it_from_dispatch(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    offline = tmp_path / "library-offline"
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, repair, _ = h.queued_repair(repo, root)
        assert repo.list_dispatchable(job["job_id"]) == (repair,)
        root.rename(offline)

        result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

        assert result["outcome"] == "held", result
        assert result["reason"] == "artifact_root_unavailable"
        assert result["state"] == "needs_verification"
        assert repo.get_delivery(repair)["state"] == "needs_verification"
        assert repo.get_delivery(repair)["error_code"] == "artifact_root_unavailable"
        assert repo.list_dispatchable(job["job_id"]) == ()
        assert not root.exists()
        offline.rename(root)
        assert h.target(root).exists() is False


# ---------------------------------------------------------------------------
# UA-05: a carried foreign hold becomes recoverable, with an explicit handover
# ---------------------------------------------------------------------------


def _foreign_hold_setup(repo, root):
    first = g.submit(repo, request_id="first-two")
    ids = g.accept(repo, first["batch_id"], g.source((g.candidate(pages=(0, 1)),)))
    transport = h.Transport(h.Response(), h.Response(status=404))
    assert h.run(repo, first, ids[0], h.executor(repo, root, transport)).state == "completed"
    assert h.run(repo, first, ids[1], h.executor(repo, root, transport)).state == "failed"
    _settle(repo, first)
    h.target(root).write_bytes(h.SAME_SIZE_PNG)
    second, (foreign_hold,) = h.second_task(repo, root)
    _settle(repo, second)
    resumed = repo.resume(first["job_id"], "resume-first")
    assert repo.activate_next()["batch_id"] == resumed["batch_id"]
    assert repo.carry_unresolved_items(resumed["batch_id"])["count"] == 1
    link = _page_link(repo, resumed["batch_id"])
    assert link["delivery_id"] == foreign_hold
    return first, second, resumed, foreign_hold


def test_a_carried_foreign_hold_is_superseded_on_reuse(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        first, second, resumed, foreign_hold = _foreign_hold_setup(repo, root)
        artifact = h.query(
            repo.database_path,
            "SELECT * FROM artifacts WHERE job_id = ? AND illust_id = ? AND page_index = 0",
            (first["job_id"], ARTWORK),
        )[0]
        artifact_before = _artifact_row(repo, artifact["artifact_id"])
        h.target(root).write_bytes(h.PNG_BYTES)

        result = repo.recover_verification_page(resumed["batch_id"], ARTWORK, 0)

        assert result["outcome"] == "reused", result
        assert result["artifact_id"] == artifact["artifact_id"]
        foreign = repo.get_delivery(foreign_hold)
        assert foreign["state"] == "cancelled", foreign
        assert foreign["error_code"] == "superseded_by_recovery_reuse"
        assert _artifact_row(repo, artifact["artifact_id"]) == artifact_before
        link = _page_link(repo, resumed["batch_id"])
        assert link["artifact_id"] == artifact["artifact_id"]
        assert link["delivery_id"] == artifact["delivery_id"]
        transfer = h.query(
            repo.database_path,
            "SELECT safe_payload FROM events WHERE kind = 'recovery_owner_transfer' "
            "ORDER BY event_seq DESC LIMIT 1",
        )
        assert len(transfer) == 1
        payload = json.loads(transfer[0]["safe_payload"])
        assert payload["delivery_id"] == foreign_hold
        assert payload["from_job_id"] == second["job_id"]
        assert payload["to_job_id"] == first["job_id"]
        assert payload["artifact_id"] == artifact["artifact_id"]
        assert payload["action"] == "reuse"
        assert repo.list_dispatchable(first["job_id"]) == ()


def test_a_carried_foreign_hold_is_superseded_on_repair(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        first, second, resumed, foreign_hold = _foreign_hold_setup(repo, root)
        h.target(root).unlink()

        result = repo.recover_verification_page(resumed["batch_id"], ARTWORK, 0)

        assert result["outcome"] == "repair_queued", result
        assert result["reason"] == "artifact_missing"
        repair_id = result["repair_delivery_id"]
        assert repair_id and repair_id != foreign_hold
        foreign = repo.get_delivery(foreign_hold)
        assert foreign["state"] == "cancelled"
        assert foreign["error_code"] == "superseded_by_recovery_repair"
        queued = repo.get_delivery(repair_id)
        assert queued["state"] == "pending"
        assert queued["job_id"] == first["job_id"]
        link = _page_link(repo, resumed["batch_id"])
        assert link["delivery_id"] == repair_id
        assert repo.list_dispatchable(first["job_id"]) == (repair_id,)
        transfer = h.query(
            repo.database_path,
            "SELECT safe_payload FROM events WHERE kind = 'recovery_owner_transfer' "
            "ORDER BY event_seq DESC LIMIT 1",
        )
        assert len(transfer) == 1
        payload = json.loads(transfer[0]["safe_payload"])
        assert payload["delivery_id"] == foreign_hold
        assert payload["from_job_id"] == second["job_id"]
        assert payload["to_job_id"] == first["job_id"]
        assert payload["artifact_id"] is None
        assert payload["action"] == "repair"


def test_a_live_foreign_transfer_is_never_stolen(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        first, second, resumed, foreign_hold = _foreign_hold_setup(repo, root)
        # Simulate another task holding a live slot on the same output path.
        rows = h.query(
            repo.database_path,
            "SELECT delivery_id FROM deliveries WHERE delivery_id = ?",
            (foreign_hold,),
        )
        assert rows
        with sqlite3.connect(repo.database_path) as conn:
            conn.execute(
                "UPDATE deliveries SET state = 'transferring' WHERE delivery_id = ?",
                (foreign_hold,),
            )
        h.target(root).write_bytes(h.PNG_BYTES)

        result = repo.recover_verification_page(resumed["batch_id"], ARTWORK, 0)

        assert result["outcome"] == "refused", result
        assert result["reason"] in {"output_blocked_by_other_task", "state_changed"}
        assert repo.get_delivery(foreign_hold)["state"] == "transferring"

# ---------------------------------------------------------------------------
# UA-02 / UA-10 (recovery side): the current global intent is a write gate
#
# Events are appended directly so the gate is tested in isolation from the
# record-path cascade (lane D makes record_removal_intent cancel every task's
# queued rows; that cascade has its own tests).  The gate must still refuse
# when the row survived, which is exactly the historical-task case.
# ---------------------------------------------------------------------------


def _raw_intent(repo, job_id, payload):
    with sqlite3.connect(repo.database_path) as conn:
        cursor = conn.execute(
            "INSERT INTO events(event_uuid, accepted_command_id, job_id, batch_id, "
            "kind, safe_payload, created_at) "
            "VALUES (?, NULL, ?, NULL, 'removal_intent', ?, ?)",
            (uuid.uuid4().hex, job_id, payload, datetime.now(timezone.utc).isoformat()),
        )
        return int(cursor.lastrowid)


def _intent_event(repo, job_id, output_key, intent):
    return _raw_intent(
        repo,
        job_id,
        json.dumps(
            {
                "illust_id": ARTWORK,
                "page_index": 0,
                "output_key": output_key,
                "root_revision": 1,
                "intent": intent,
            }
        ),
    )


def test_an_excluded_page_is_never_reused_by_recovery(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, hold, first = h.parked_hold(repo, root)
        h.target(root).write_bytes(h.PNG_BYTES)
        row = _delivery_row(repo, hold)
        _intent_event(
            repo, repo.get_delivery(first)["job_id"], row["output_key"], "excluded"
        )
        after_exclusion = _table_snapshot(repo)

        result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

        assert result["outcome"] == "refused", result
        assert result["reason"] == "intent_excluded"
        assert repo.get_delivery(hold)["state"] == "needs_verification"
        assert repo.list_dispatchable(job["job_id"]) == ()
        assert _table_snapshot(repo) == after_exclusion


def test_a_corrupt_intent_blocks_a_repair_recovery(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, hold, first = h.parked_hold(repo, root)
        h.target(root).unlink()
        _raw_intent(repo, repo.get_delivery(first)["job_id"], "{broken")
        before = _table_snapshot(repo)

        result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

        assert result["outcome"] == "refused", result
        assert result["reason"] == "intent_unreadable"
        assert repo.get_delivery(hold)["state"] == "needs_verification"
        assert repo.list_dispatchable(job["job_id"]) == ()
        assert _table_snapshot(repo) == before


def test_revoking_the_exclusion_allows_reuse_again(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, hold, first = h.parked_hold(repo, root)
        h.target(root).write_bytes(h.PNG_BYTES)
        job_id = repo.get_delivery(first)["job_id"]
        output_key = _delivery_row(repo, hold)["output_key"]
        _intent_event(repo, job_id, output_key, "excluded")
        assert repo.recover_verification_page(job["batch_id"], ARTWORK, 0)["reason"] == (
            "intent_excluded"
        )

        _intent_event(repo, job_id, output_key, "none")

        result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

        assert result["outcome"] == "reused", result
        assert repo.get_delivery(hold)["state"] == "cancelled"
        assert repo.get_delivery(hold)["error_code"] == "recovered_hash_match"
        assert _page_link(repo, job["batch_id"])["artifact_id"] == result["artifact_id"]
