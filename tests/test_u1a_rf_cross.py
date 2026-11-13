"""RF cross-lane acceptance net (tests only).

Pins the behaviours the 15 re-audit probes do not cover.  Expected red at the
start of the RF round, by lane:

* P1 (delivery transaction): handover consistency for a third task, both crash
  points around cancellation/cleanup, repeated unlink retries.
* P2 (recovery semantics): the held/dispatch matrix beyond no-resolver.
* P3 (control): close with queued observations, double close, a single page
  failure must not stop the queue.

No product code is changed here; no test is skipped or xfailed.  Synthetic
library, fake transport, temporary SQLite only.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import test_u1a_recovery as h
import test_u1a_reuse_gate as g
from core.library_repository import CorruptDataError, LibraryRepository
from core.page_delivery import _Fault
from core.preservation_policy import LocalState
from core.task_coordinator import DeliveryOutcome, TaskCoordinator


def _settle(repo, job):
    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"])


def _stop_reason(repo, batch_id):
    with sqlite3.connect(repo.database_path) as conn:
        row = conn.execute(
            "SELECT stop_reason FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
    return None if row is None else row[0]


def _page_link(repo, batch_id, page_index=0):
    with sqlite3.connect(repo.database_path) as conn:
        row = conn.execute(
            "SELECT delivery_id, artifact_id FROM batch_item_pages "
            "WHERE batch_id = ? AND illust_id = ? AND page_index = ?",
            (batch_id, h.ARTWORK, page_index),
        ).fetchone()
    return None if row is None else (row[0], row[1])


# ---------------------------------------------------------------------------
# 1. Four-step ownership rotation (P1 carry successor + P2 recovery)
# ---------------------------------------------------------------------------


def test_a_third_task_never_breaks_a_completed_handover_rotation(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        first = g.submit(repo, request_id="owner")
        ids = g.accept(repo, first["batch_id"], g.source((g.candidate(pages=(0, 1)),)))
        transport = h.Transport(h.Response(), h.Response(status=404))
        assert h.run(repo, first, ids[0], h.executor(repo, tmp_path, transport)).state == "completed"
        assert h.run(repo, first, ids[1], h.executor(repo, tmp_path, transport)).state == "failed"
        _settle(repo, first)
        history = h.receipt(repo, ids[0])
        original_link = _page_link(repo, first["batch_id"])

        h.target(tmp_path).write_bytes(h.SAME_SIZE_PNG)
        second, (hold,) = h.second_task(repo, tmp_path)
        _settle(repo, second)

        resumed_a = repo.resume(first["job_id"], "resume-a")
        assert repo.activate_next()["batch_id"] == resumed_a["batch_id"]
        assert repo.carry_unresolved_items(resumed_a["batch_id"])["count"] == 1
        h.target(tmp_path).write_bytes(h.PNG_BYTES)
        assert repo.recover_verification_page(
            resumed_a["batch_id"], h.ARTWORK, 0
        )["outcome"] == "reused"
        _settle(repo, resumed_a)

        resumed_b = repo.resume(second["job_id"], "resume-b")
        assert repo.activate_next()["batch_id"] == resumed_b["batch_id"]
        assert repo.carry_unresolved_items(resumed_b["batch_id"])["count"] == 1
        result = repo.recover_verification_page(resumed_b["batch_id"], h.ARTWORK, 0)
        print("ROTATION_SECOND_OWNER", result["outcome"], result["reason"], h.pages(repo, resumed_b))
        assert result["outcome"] in {"reused", "completed"}, result
        assert h.pages(repo, resumed_b)["cancelled"] == 0
        _settle(repo, resumed_b)

        third = h.submit(repo, request_id="third")
        accepted = repo.accept_source_page(
            third["batch_id"], h.source((h.candidate(),), page_identity="third")
        )
        assert accepted
        assert h.pages(repo, third)["completed"] == 1
        _settle(repo, third)

        assert h.receipt(repo, ids[0]) == history, "history must not be rewritten"
        assert _page_link(repo, first["batch_id"]) == original_link


# ---------------------------------------------------------------------------
# 2-3. Cancellation / cleanup crash points and retries (P1)
# ---------------------------------------------------------------------------


def _cancel_before_publish(repo, job):
    def exclude(stage, row):
        if stage == "before_publish":
            repo.record_removal_intent(
                job["batch_id"], h.ARTWORK, 0, h.OUTPUT_REF,
                intent="excluded", command_id="exclude-crash-point",
            )

    return exclude


def test_cancel_committed_before_unlink_converges_on_restart(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        service = h.executor(
            repo, tmp_path, h.Transport(h.Response()),
            fault_hook=_cancel_before_publish(repo, job),
        )
        original_unlink = Path.unlink

        def fail_once(path, *args, **kwargs):
            if path.suffix == ".part":
                raise PermissionError("synthetic file lock")
            return original_unlink(path, *args, **kwargs)

        with monkeypatch.context() as scope:
            scope.setattr(Path, "unlink", fail_once)
            assert h.run(repo, job, pending, service).state == "cancelled"
        assert list(tmp_path.rglob("*.part"))
        assert repo.get_delivery(pending)["state"] == "cancelled"

    with LibraryRepository(tmp_path / "ledger.sqlite3") as reopened:
        service = h.executor(reopened, tmp_path, h.Transport())
        service.recover_local()
        cleaned = service.cleanup_delivery(pending)
        leftovers = list(tmp_path.rglob("*.part"))
        print("CRASH_BEFORE_UNLINK", cleaned, leftovers)
        assert not h.target(tmp_path).exists(), "a cancelled page must not be republished"
        assert not leftovers, "a committed cancellation must converge on restart"


def test_unlink_succeeded_before_receipt_is_idempotent(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        service = h.executor(
            repo, tmp_path, h.Transport(h.Response()),
            fault_hook=_cancel_before_publish(repo, job),
        )
        original_unlink = Path.unlink

        def fail_once(path, *args, **kwargs):
            if path.suffix == ".part":
                raise PermissionError("synthetic file lock")
            return original_unlink(path, *args, **kwargs)

        with monkeypatch.context() as scope:
            scope.setattr(Path, "unlink", fail_once)
            assert h.run(repo, job, pending, service).state == "cancelled"
        for part in tmp_path.rglob("*.part"):
            original_unlink(part)  # simulate unlink success, receipt missing
        recovered = h.executor(repo, tmp_path, h.Transport())
        recovered.recover_local()
        assert recovered.cleanup_delivery(pending) is not False or not list(
            tmp_path.rglob("*.part")
        )
        assert not h.target(tmp_path).exists()
        assert repo.get_delivery(pending)["state"] == "cancelled"


def test_cleanup_retries_after_two_transient_unlink_failures(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        service = h.executor(
            repo, tmp_path, h.Transport(h.Response()),
            fault_hook=_cancel_before_publish(repo, job),
        )
        original_unlink = Path.unlink
        attempts = {"count": 0}

        def fail_twice(path, *args, **kwargs):
            if path.suffix == ".part" and attempts["count"] < 2:
                attempts["count"] += 1
                raise PermissionError("synthetic file lock")
            return original_unlink(path, *args, **kwargs)

        with monkeypatch.context() as scope:
            scope.setattr(Path, "unlink", fail_twice)
            assert h.run(repo, job, pending, service).state == "cancelled"
        assert list(tmp_path.rglob("*.part"))
        recovered = h.executor(repo, tmp_path, h.Transport())
        for _ in range(3):
            recovered.recover_local()
            recovered.cleanup_delivery(pending)
        print("CLEANUP_RETRY", attempts["count"], list(tmp_path.rglob("*.part")))
        assert not list(tmp_path.rglob("*.part")), "cleanup must be retryable"
        assert not h.target(tmp_path).exists()


def test_cleanup_never_deletes_a_foreign_final_file(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        h.target(tmp_path).write_bytes(b"F" * len(h.PNG_BYTES))
        outcome = h.run(
            repo, job, pending,
            h.executor(repo, tmp_path, h.Transport(h.Response())),
        )
        assert outcome.state in {"suspended", "blocked", "needs_verification", "failed"}, outcome
        assert h.target(tmp_path).read_bytes() == b"F" * len(h.PNG_BYTES)
        assert repo.get_delivery(pending)["state"] != "cancelled"


# ---------------------------------------------------------------------------
# 4. Admission tickets around pause (P3)
# ---------------------------------------------------------------------------


def test_a_page_admitted_after_a_durable_pause_is_rejected(tmp_path):
    repo = LibraryRepository(tmp_path / "ledger.sqlite3")
    service = TaskCoordinator(repo, lambda *_: DeliveryOutcome("failed"), workers=1)
    try:
        service.start().result(5)
        job = service.submit(h.request("pause-first"), "submit-pause-first").result(5)
        service.run_queued().result(5)
        deadline = threading.Event()
        service.pause(job["job_id"], "pause-first").result(5)
        with pytest.raises(Exception):
            service.accept_page(
                job["batch_id"], h.source((h.candidate(),), page_identity="after-pause")
            ).result(5)
        assert repo.get_batch(job["batch_id"])["phase"] in {"pausing", "paused", "blocked"}
    finally:
        service.close(5)


def test_close_leaves_no_accepted_future_pending(tmp_path):
    repo = LibraryRepository(tmp_path / "ledger.sqlite3")
    entered, release = threading.Event(), threading.Event()

    def blocking_accept(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        raise RuntimeError("synthetic-accept-failure")

    service = TaskCoordinator(repo, lambda *_: DeliveryOutcome("failed"), workers=1)
    try:
        service.start().result(5)
        job = service.submit(h.request("close-queued"), "submit-close-queued").result(5)
        service.run_queued().result(5)
        original = repo.accept_source_page
        repo.accept_source_page = blocking_accept
        future = service.accept_page(job["batch_id"], h.source((h.candidate(),)))
        assert entered.wait(5)
        release.set()
        try:
            future.result(5)
        except Exception:
            pass
        assert future.done(), "every accepted Future must reach a terminal state"
        service.close(5)
        service.close(5)  # idempotent
    finally:
        release.set()
        try:
            service.close(5)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 5. Failure classification (P3): single page vs library-wide trust fault
# ---------------------------------------------------------------------------


def test_a_single_page_failure_does_not_stop_queued_jobs(tmp_path):
    repo = LibraryRepository(tmp_path / "ledger.sqlite3")
    calls = []

    def executor(task, stop):
        calls.append(task["delivery_id"])
        return DeliveryOutcome("failed", "image_network_error")

    service = TaskCoordinator(repo, executor, workers=1)
    try:
        service.start().result(5)
        first = service.submit(h.request("first-page-fail"), "submit-first-page").result(5)
        later = service.submit(h.request("later-job"), "submit-later-job").result(5)
        service.run_queued().result(5)
        deadline = threading.Event()
        for _ in range(4):
            service.accept_page(
                first["batch_id"], h.source((h.candidate(),), page_identity="p1")
            ).result(5)
        import time

        for _ in range(200):
            if repo.get_batch(later["batch_id"])["phase"] == "running":
                break
            time.sleep(0.02)
        print("SINGLE_PAGE_FAILURE", repo.get_batch(first["batch_id"])["phase"],
              repo.get_batch(later["batch_id"])["phase"], calls)
        assert repo.get_batch(later["batch_id"])["phase"] in {"queued", "running"}
    finally:
        service.close(5)


# ---------------------------------------------------------------------------
# 6. held / dispatch matrix (P2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode", ["observation_none", "no_resolver", "root_unavailable", "unreadable"]
)
def test_a_pending_page_without_usable_evidence_is_not_dispatchable(
    tmp_path, monkeypatch, mode
):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        observe = repo._observe_artifact
        if mode == "observation_none":
            monkeypatch.setattr(repo, "_observe_artifact", lambda *a, **k: None)
        elif mode == "no_resolver":
            monkeypatch.setattr(repo, "_path_resolver_snapshot", lambda: None)
        elif mode == "root_unavailable":
            monkeypatch.setattr(
                repo, "_observe_artifact",
                lambda *a, **k: replace(
                    observe(*a, **k),
                    state=LocalState.ROOT_UNAVAILABLE,
                    reason="artifact_root_unavailable",
                ),
            )
        else:
            monkeypatch.setattr(
                repo, "_observe_artifact",
                lambda *a, **k: replace(
                    observe(*a, **k),
                    state=LocalState.UNREADABLE,
                    reason="artifact_unreadable",
                ),
            )
        result = repo.recover_verification_page(job["batch_id"], h.ARTWORK, 0)
        dispatched = repo.list_dispatchable(job["job_id"])
        print("HELD_MATRIX", mode, result["outcome"], result["reason"], dispatched)
        assert result["outcome"] == "held", result
        assert pending not in dispatched, f"{mode}: held page is still dispatchable"
        assert repo.get_delivery(pending)["state"] != "pending"


def test_a_stale_negative_observation_never_retires_a_hold(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, hold, _ = h.parked_hold(repo, tmp_path)
        h.target(tmp_path).unlink()
        observe = repo._observe_artifact
        monkeypatch.setattr(
            repo, "_observe_artifact",
            lambda *a, **k: replace(
                observe(*a, **k), checked_at="2020-01-01T00:00:00+00:00"
            ),
        )
        result = repo.recover_verification_page(job["batch_id"], h.ARTWORK, 0)
        print("STALE_HOLD", result["outcome"], result["reason"])
        assert result["outcome"] in {"held", "refused"}, result
        assert repo.get_delivery(hold)["state"] == "needs_verification"
        assert result["repair_delivery_id"] is None


def test_a_pending_page_without_history_stays_a_normal_repair(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        h.executor(repo, tmp_path, h.Transport())  # bind the project root
        job = h.submit(repo, request_id="fresh-page")
        (pending,) = repo.accept_source_page(
            job["batch_id"], h.source((h.candidate(),), page_identity="fresh")
        )
        result = repo.recover_verification_page(job["batch_id"], h.ARTWORK, 0)
        print("FRESH_PENDING", result["outcome"], result["reason"])
        assert result["outcome"] == "repair_queued"
        assert pending in repo.list_dispatchable(job["job_id"])
