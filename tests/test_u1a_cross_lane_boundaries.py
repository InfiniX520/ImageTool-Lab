"""Cross-lane invariants for the U1-A repair (merge acceptance net).

Every test names the repair lane that must turn it green and the audit item it
guards.  These are deliberately *not* copies of the audit's 14 repros: each one
covers a boundary the audit left open, usually the second half of a rule
("blocked" is not enough; the cursor and the dispatch queue must agree too).

Lane / audit map (green = already true at 8d8c62c, red = expected to fail until
the named lane lands):

* 1a same-task exclusion before claim .......... D / UA-02  (green guard)
* 1b cross-task exclusion before claim ......... D+A / UA-02 (red)
* 2  exclusion while validating ................ C+D / UA-11 (red)
* 3  prepared is not cancelled by the recorder .. D / UA-11  (green guard)
* 4  carried foreign hold + recovery consistency  B+D / UA-05 (green guard)
* 5  two tasks, one output slot ................. A+D / UA-01 (green guard)
* 6  changed file between identical replays ..... A / UA-09   (red)
* 7a corrupt intent blocks claim ................ D+A / UA-10 (red)
* 7b corrupt intent blocks publication .......... C+D / UA-10 (red)
* 7c corrupt intent blocks repair ............... B+D / UA-10 (red)
* 8a a rejected acceptance writes no cursor/budget A / UA-10 (red)
* 8b the completion-race retry counts once ...... A / UA-01   (red)
* 9  a dispatched root outage recreates nothing .. C / UA-03  (red)

Only synthetic PNG bytes, temporary SQLite ledgers and a fake transport are
used.  No real gallery, account, network, upload, delete or migration.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace

import pytest

from core.library_repository import (
    CorruptDataError,
    LibraryError,
    LibraryRepository,
    OutputConflict,
)
from core.task_contracts import PageSpec, PathRef

import test_u1a_recovery as h


@pytest.fixture
def repo(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as value:
        yield value

OTHER_REF = PathRef(relative_path=f"downl_Pixiv/{h.OTHER_ARTWORK}_p0.png")


def _exclude(repo, batch_id, *, illust=h.ARTWORK, output_ref=h.OUTPUT_REF, command):
    return repo.record_removal_intent(
        batch_id, illust, 0, output_ref, intent="excluded", command_id=command
    )


def _corrupt_removal_intents(repo, seed_batch_id):
    """One readable intent, then break its payload: the projection must block."""
    _exclude(repo, seed_batch_id, illust=h.OTHER_ARTWORK, output_ref=OTHER_REF,
             command="corrupt-seed")
    with sqlite3.connect(repo.database_path) as connection:
        changed = connection.execute(
            "UPDATE events SET safe_payload = ? WHERE kind = 'removal_intent'",
            ("{broken",),
        ).rowcount
    assert changed == 1


def _settle(repo, job):
    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"])


# ---------------------------------------------------------------------------
# 1. Exclusion before claim
# ---------------------------------------------------------------------------


def test_exclusion_before_claim_cancels_the_pending_delivery_same_task(repo, tmp_path):
    """D / UA-02 guard: the local path keeps working after the scope widens."""
    job, pending, _ = h.queued_repair(repo, tmp_path)
    _exclude(repo, job["batch_id"], command="same-task-exclusion")
    assert repo.get_delivery(pending)["state"] == "cancelled"
    assert repo.claim_delivery(pending, job["batch_id"]) is None
    assert repo.list_dispatchable(job["job_id"]) == ()


def test_exclusion_from_a_historical_task_stops_a_claim_in_another_task(repo, tmp_path):
    """D+A / UA-02: the intent is global, so the claim must see it."""
    job, pending, first = h.queued_repair(repo, tmp_path)
    first_row = repo.get_delivery(first)
    _exclude(repo, first_row["completed_batch_id"], command="historical-exclusion")
    assert repo.get_delivery(pending)["state"] == "cancelled"
    assert repo.claim_delivery(pending, job["batch_id"]) is None
    assert repo.list_dispatchable(job["job_id"]) == ()


# ---------------------------------------------------------------------------
# 2-3. Exclusion while the transfer is already past the queue
# ---------------------------------------------------------------------------


def test_exclusion_while_validating_never_publishes_the_final_file(repo, tmp_path):
    """C+D / UA-11: validating is not transferring; the publish gate must hold."""
    job, pending, _ = h.queued_repair(repo, tmp_path)
    transport = h.Transport(h.Response())

    def hook(stage, row):
        if stage == "before_prepare":
            _exclude(repo, row["current_batch_id"], command="exclude-validating")

    outcome = h.run(
        repo, job, pending,
        h.executor(repo, tmp_path, transport, fault_hook=hook),
    )
    assert outcome.state != "completed"
    assert not h.target(tmp_path).exists(), "an excluded page was published anyway"
    assert repo.get_delivery(pending)["state"] != "completed"


def test_prepared_is_never_cancelled_by_the_intent_recorder(repo, tmp_path):
    """D / UA-11 guard: prepared rows are not UPDATE'd to cancelled."""
    job, pending, _ = h.queued_repair(repo, tmp_path)
    seen = {}

    def hook(stage, row):
        if stage == "after_prepare":
            _exclude(repo, row["current_batch_id"], command="exclude-prepared")
            seen["state"] = repo.get_delivery(pending)["state"]

    outcome = h.run(
        repo, job, pending,
        h.executor(repo, tmp_path, h.Transport(h.Response()), fault_hook=hook),
    )
    assert seen["state"] == "prepared"
    final = repo.get_delivery(pending)["state"]
    assert final in {"completed", "cancelled"}
    if final == "completed":
        assert outcome.state == "completed"
        assert h.target(tmp_path).exists()
    else:
        assert not h.target(tmp_path).exists()


# ---------------------------------------------------------------------------
# 4-5. Ownership, carry and the single active slot
# ---------------------------------------------------------------------------


def test_carried_foreign_hold_is_not_dispatchable_and_recovery_agrees(repo, tmp_path):
    """B+D / UA-05 guard: carry may link a foreign hold, but never a pending one."""
    job = h.submit(repo, request_id="carry-owner")
    one = h.candidate(h.ARTWORK, page_index=0, page_count=2)
    two = h.candidate(h.ARTWORK, page_index=1, page_count=2)
    ids = h.accept(
        repo, job["batch_id"], h.source((replace(one, pages=one.pages + two.pages),))
    )
    transport = h.Transport(h.Response(), h.Response(status=404))
    assert h.run(repo, job, ids[0], h.executor(repo, tmp_path, transport)).state == "completed"
    assert h.run(repo, job, ids[1], h.executor(repo, tmp_path, transport)).state == "failed"
    _settle(repo, job)

    h.target(tmp_path).write_bytes(h.SAME_SIZE_PNG)
    second, (foreign_hold,) = h.second_task(repo, tmp_path)
    _settle(repo, second)

    resumed = repo.resume(job["job_id"], "resume-owner")
    assert repo.activate_next()["batch_id"] == resumed["batch_id"]
    assert repo.carry_unresolved_items(resumed["batch_id"])["count"] == 1
    assert repo.list_dispatchable(job["job_id"]) == ()

    result = repo.recover_verification_page(resumed["batch_id"], h.ARTWORK, 0)
    state = repo.get_delivery(foreign_hold)["state"]
    assert result["state"] == state
    assert state not in {"pending", "transferring", "completed"}
    assert result["outcome"] != "repair_queued"
    assert repo.list_dispatchable(job["job_id"]) == ()


def test_a_second_task_never_opens_a_second_active_slot(repo, tmp_path):
    """A+D / UA-01 guard: no duplicate active writer, no history rewrite."""
    first_job = h.submit(repo, request_id="slot-first")
    (first_delivery,) = h.accept(repo, first_job["batch_id"], h.source((h.candidate(),)))
    assert h.run(
        repo, first_job, first_delivery,
        h.executor(repo, tmp_path, h.Transport(h.Response())),
    ).state == "completed"
    _settle(repo, first_job)

    second_job = h.submit(repo, request_id="slot-second")
    h.accept(
        repo, second_job["batch_id"],
        h.source((h.candidate(),), page_identity="slot-second"),
    )
    assert repo.get_delivery(first_delivery)["state"] == "completed"
    assert h.active_output_rows(repo.database_path, h.OUTPUT_REF.target_key) == []
    _settle(repo, second_job)

    third_job = h.submit(repo, request_id="slot-third", dedup_policy="redownload")
    (pending,) = h.accept(
        repo, third_job["batch_id"],
        h.source((h.candidate(),), page_identity="slot-third"),
    )
    impostor = replace(
        h.candidate(h.OTHER_ARTWORK, page_index=0, page_count=1),
        pages=(PageSpec(0, h.OUTPUT_REF, h.ORIGINAL_URL),),
    )
    with pytest.raises(OutputConflict):
        h.accept(
            repo, third_job["batch_id"],
            h.source(
                (impostor,), page_identity="slot-conflict",
                before='"cursor-1"', after='"cursor-2"',
            ),
        )
    assert repo.get_delivery(pending)["state"] == "pending"
    active = h.active_output_rows(repo.database_path, h.OUTPUT_REF.target_key)
    assert len(active) == 1 and active[0]["delivery_id"] == pending


# ---------------------------------------------------------------------------
# 6. Idempotent replay must not skip a changed file
# ---------------------------------------------------------------------------


def test_a_changed_file_between_identical_replays_is_observed_again(repo, tmp_path, monkeypatch):
    """A / UA-09: the receipt fast path may skip hashing only while bytes match."""
    h.first_delivery(repo, tmp_path)
    job = h.submit(repo, request_id="replay-changed")
    page = h.source((h.candidate(),), page_identity="replay-changed")
    h.accept(repo, job["batch_id"], page)
    assert h.pages(repo, job)["completed"] == 1

    observe = repo._observe_artifact
    calls = []

    def counting(*args, **kwargs):
        calls.append(str(args[0]["artifact_id"]))
        return observe(*args, **kwargs)

    monkeypatch.setattr(repo, "_observe_artifact", counting)
    h.target(tmp_path).write_bytes(h.SAME_SIZE_PNG)
    failed = False
    try:
        h.accept(repo, job["batch_id"], page)
    except LibraryError:
        failed = True

    assert calls, "the replayed page reused a receipt without observing the file"
    if not failed:
        result = h.pages(repo, job)
        assert result["needs_verification"] == 1
        assert result["completed"] == 0


# ---------------------------------------------------------------------------
# 7. A corrupt intent blocks all three write paths
# ---------------------------------------------------------------------------


def test_corrupt_intent_blocks_the_claim(repo, tmp_path):
    """D+A / UA-10: unreadable intent is not permission to download."""
    job, pending, _ = h.queued_repair(repo, tmp_path)
    _corrupt_removal_intents(repo, job["batch_id"])
    row = None
    try:
        row = repo.claim_delivery(pending, job["batch_id"])
    except CorruptDataError:
        row = None
    assert row is None
    assert repo.get_delivery(pending)["state"] not in {"transferring", "completed"}


def test_corrupt_intent_blocks_publication(repo, tmp_path):
    """C+D / UA-10: the publish gate fails closed on unreadable evidence."""
    job, pending, _ = h.queued_repair(repo, tmp_path)
    row = repo.claim_delivery(pending, job["batch_id"])
    assert row is not None
    _corrupt_removal_intents(repo, job["batch_id"])
    outcome = h.executor(repo, tmp_path, h.Transport(h.Response()))(row, threading.Event())
    assert outcome.state != "completed"
    assert not h.target(tmp_path).exists()
    assert repo.get_delivery(pending)["state"] != "completed"


def test_corrupt_intent_blocks_recovery_repair(repo, tmp_path):
    """B+D / UA-10: recovery must not queue a repair from unreadable intent."""
    job, hold, _ = h.parked_hold(repo, tmp_path)
    h.target(tmp_path).unlink()
    _corrupt_removal_intents(repo, job["batch_id"])
    result = repo.recover_verification_page(job["batch_id"], h.ARTWORK, 0)
    assert result["outcome"] != "repair_queued"
    assert repo.get_delivery(hold)["state"] == "needs_verification"
    assert repo.list_dispatchable(job["job_id"]) == ()


# ---------------------------------------------------------------------------
# 8. A rejected or retried acceptance must not move the cursor or the budget
# ---------------------------------------------------------------------------


def test_a_rejected_acceptance_writes_no_cursor_and_no_work(repo, tmp_path):
    """A / UA-10: blocking happens before any durable source progress."""
    seed_job, _ = h.first_delivery(repo, tmp_path)
    _corrupt_removal_intents(repo, seed_job["batch_id"])
    job = h.submit(repo, request_id="rejected-accept")
    blocked = False
    try:
        h.accept(repo, job["batch_id"], h.source((h.candidate(),), page_identity="rejected"))
    except LibraryError:
        blocked = True
    assert blocked, "the acceptance proceeded while the intent projection was corrupt"
    assert h.query(
        repo.database_path,
        "SELECT COUNT(*) AS n FROM deliveries WHERE job_id = ?",
        (job["job_id"],),
    )[0]["n"] == 0
    assert h.query(
        repo.database_path,
        "SELECT COUNT(*) AS n FROM checkpoints WHERE job_id = ?",
        (job["job_id"],),
    )[0]["n"] == 0
    assert repo.budget_status(job["batch_id"])["used_works"] == 0


def test_the_completion_race_retry_counts_one_work_and_one_cursor_step(
    repo, tmp_path, monkeypatch
):
    """A / UA-01: a retried acceptance is one page, one work, one cursor step."""
    job = h.submit(repo, request_id="retry-budget")
    (pending,) = h.accept(repo, job["batch_id"], h.source((h.candidate(),)))
    service = h.executor(repo, tmp_path, h.Transport(h.Response()))
    preflight = repo._preflight_page_health

    def finish_after_snapshot(batch_id, page):
        health = preflight(batch_id, page)
        assert h.run(repo, job, pending, service).state == "completed"
        return health

    monkeypatch.setattr(repo, "_preflight_page_health", finish_after_snapshot)
    h.accept(
        repo, job["batch_id"],
        h.source(
            (h.candidate(),), page_identity="retry-second",
            before='"cursor-1"', after='"cursor-2"',
        ),
    )
    _settle(repo, job)

    result = h.pages(repo, job)
    assert result["needs_verification"] == 0
    assert result["completed"] == 1
    rows = h.delivery_rows(repo.database_path)
    assert len(rows) == 1 and rows[0]["state"] == "completed"
    assert repo.budget_status(job["batch_id"])["used_works"] == 1
    assert h.query(
        repo.database_path,
        "SELECT cursor_json FROM checkpoints WHERE job_id = ?",
        (job["job_id"],),
    ) == [{"cursor_json": '"cursor-2"'}]


# ---------------------------------------------------------------------------
# 9. A dispatched page must not rebuild an offline root
# ---------------------------------------------------------------------------


def test_a_dispatched_root_outage_recreates_nothing_and_requests_nothing(repo, tmp_path):
    """C / UA-03: the executor guard, not only the recovery park, protects bytes."""
    root = tmp_path / "library"
    root.mkdir()
    job, pending, _ = h.queued_repair(repo, root)
    service = h.executor(repo, root, h.Transport(h.Response()))
    row = repo.claim_delivery(pending, job["batch_id"])
    assert row is not None

    offline = tmp_path / "library-offline"
    root.rename(offline)

    outcome = service(row, threading.Event())
    assert outcome.state != "completed"
    assert not root.exists(), "a missing bound root was recreated by mkdir"
    assert service.get.calls == [], "a GET was sent into an unavailable root"
    assert repo.get_delivery(pending)["state"] != "completed"