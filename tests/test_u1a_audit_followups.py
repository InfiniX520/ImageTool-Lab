"""Follow-ups from the read-only review of the U1-A repair.

Each test pins one audited boundary that the first repair round could still
reach: a corrupt projection that silently idled the coordinator, a transient
observation failure that became a sticky hold, an unobservable trusted
candidate reported as "unknown", and an unreadable intent that discarded
staged bytes.  Synthetic library, fake transport, no real data.
"""
from __future__ import annotations

import sqlite3

import pytest

import test_u1a_recovery as h
from core.library_repository import CorruptDataError, LibraryRepository
from core.observation_snapshot import PreflightSnapshot, PreflightUnavailable
from core.page_delivery import _Fault
from core.task_coordinator import TaskCoordinator


def _corrupt_removal_intents(repo):
    with sqlite3.connect(repo.database_path) as conn:
        conn.execute("UPDATE events SET safe_payload = '{broken' WHERE kind = 'removal_intent'")


def _cursor_snapshot(repo, job_id):
    with sqlite3.connect(repo.database_path) as conn:
        return conn.execute(
            "SELECT source_id, pass_id, entity_id, cursor_json, exhausted "
            "FROM checkpoints WHERE job_id = ? ORDER BY source_id, pass_id, entity_id",
            (job_id,),
        ).fetchall()


def _stop_reason(repo, batch_id):
    with sqlite3.connect(repo.database_path) as conn:
        row = conn.execute(
            "SELECT stop_reason FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
    return None if row is None else row[0]


def test_a_corrupt_projection_blocks_the_batch_instead_of_idling(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        repo.record_removal_intent(
            job["batch_id"], h.ARTWORK, 0, h.OUTPUT_REF,
            intent="none", command_id="mark-none",
        )
        _corrupt_removal_intents(repo)
        with pytest.raises(CorruptDataError):
            repo.claim_delivery(pending, job["batch_id"])
        assert repo.get_batch(job["batch_id"])["phase"] == "pausing"
        assert _stop_reason(repo, job["batch_id"]) == "intent_unreadable"
        # The block is durable and the page is not silently claimed.
        assert repo.get_delivery(pending)["state"] == "pending"


def test_a_transient_preflight_failure_retries_instead_of_holding(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        h.first_delivery(repo, tmp_path)
        calls = []

        def failed(batch_id, page):
            calls.append(batch_id)
            return PreflightSnapshot(
                run_id=f"batch-{batch_id}", pages={}, degraded=True,
                reason="preflight_failed",
            )

        monkeypatch.setattr(repo, "_preflight_page_health", failed)
        job = h.submit(repo, request_id="transient-preflight")
        before = _cursor_snapshot(repo, job["job_id"])
        with pytest.raises(PreflightUnavailable):
            repo.accept_source_page(
                job["batch_id"], h.source((h.candidate(),), page_identity="transient")
            )
        assert len(calls) >= 2, "a transient failure must be retried"
        assert repo.list_dispatchable(job["job_id"]) == ()
        after = _cursor_snapshot(repo, job["job_id"])
        assert before == after, "a failed preflight must not advance the cursor"


def test_an_unobservable_trusted_candidate_is_retried_not_called_unknown(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        h.first_delivery(repo, tmp_path)
        monkeypatch.setattr(repo, "_observe_artifact", lambda *args, **kwargs: None)
        job = h.submit(repo, request_id="unobserved-candidate")
        with pytest.raises(PreflightUnavailable):
            repo.accept_source_page(
                job["batch_id"], h.source((h.candidate(),), page_identity="unobserved")
            )
        assert repo.list_dispatchable(job["job_id"]) == ()
        assert repo.get_batch(job["batch_id"])["phase"] == "running"


def test_a_paused_batch_does_not_create_a_health_hold(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        h.first_delivery(repo, tmp_path)
        monkeypatch.setattr(
            repo,
            "_preflight_page_health",
            lambda batch_id, page: PreflightSnapshot(
                run_id=f"batch-{batch_id}", pages={}, degraded=True,
                reason="batch_not_running",
            ),
        )
        job = h.submit(repo, request_id="paused-preflight")
        repo.activate_next()
        with pytest.raises(PreflightUnavailable):
            repo.accept_source_page(
                job["batch_id"], h.source((h.candidate(),), page_identity="paused")
            )
        with sqlite3.connect(repo.database_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE job_id = ?", (job["job_id"],)
            ).fetchone()[0]
        assert count == 0, "a paused batch must not turn a completed page into a hold"


def test_a_blocked_batch_survives_restart_and_keeps_the_next_job_queued(tmp_path):
    database = tmp_path / "ledger.sqlite3"
    with LibraryRepository(database) as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        repo.record_removal_intent(
            job["batch_id"], h.ARTWORK, 0, h.OUTPUT_REF,
            intent="none", command_id="mark-none-3",
        )
        _corrupt_removal_intents(repo)
        with pytest.raises(CorruptDataError):
            repo.claim_delivery(pending, job["batch_id"])
        second = repo.submit(h.request("queued-behind"), "submit-behind")

    with LibraryRepository(database) as repo:
        service = h.executor(repo, tmp_path, h.Transport())
        with TaskCoordinator(repo, service, workers=1) as coordinator:
            coordinator.start().result(10)
            assert repo.get_batch(job["batch_id"])["phase"] == "blocked"
            assert _stop_reason(repo, job["batch_id"]) == "intent_unreadable"
            assert repo.get_batch(second["batch_id"])["phase"] == "queued"


def test_unreadable_intent_keeps_staged_bytes_and_parks_the_delivery(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, pending, _ = h.queued_repair(repo, root)

        def stop_before_publish(stage, row):
            if stage == "before_publish":
                raise _Fault("probe_stop")

        service = h.executor(repo, root, h.Transport(h.Response()), fault_hook=stop_before_publish)
        outcome = h.run(repo, job, pending, service)
        assert outcome.state == "recovery_needed"
        assert not h.target(root).exists()
        parts = list(root.rglob("*.part"))
        assert parts, "the staged bytes must survive a probe stop"

        repo.record_removal_intent(
            job["batch_id"], h.ARTWORK, 0, h.OUTPUT_REF,
            intent="none", command_id="mark-none-2",
        )
        _corrupt_removal_intents(repo)

        results = service.recover_local()
        assert (pending, "recovery_needed") in results
        assert repo.get_delivery(pending)["state"] == "recovery_needed"
        assert not h.target(root).exists(), "no final file may appear"
        assert list(root.rglob("*.part")), "unreadable intent must not delete staged bytes"
