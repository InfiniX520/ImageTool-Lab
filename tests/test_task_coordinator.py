"""Offline integration of real SQLite state, queues, threads and controls."""
import threading
import time
import sqlite3

import pytest

from core.library_repository import LibraryRepository
from core.task_contracts import BatchBudget, CandidateDecision, CollectionRequest, PageSpec, PathRef, SourcePage
from core.task_coordinator import CommandQueueFull, CoordinatorClosed, DeliveryOutcome, TaskCoordinator


def request(number="1"):
    return CollectionRequest(f"request-{number}", "account")


def source_page(size=8):
    return SourcePage("follow", "history", "artist-7", "page-1", tuple(
        CandidateDecision(str(100 + i), "download", artist_id="7", page_count=1, pages=(
            PageSpec(0, PathRef(relative_path=f"downl_Pixiv/7/{100+i}_p0.png"), f"https://i.pximg.net/{100+i}_p0.png"),))
        for i in range(size)), cursor_after_json='{"offset":8}')


def until(probe, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail("Expected coordinator state did not arrive")


def test_bounded_queue_pause_persists_all_ids_and_resume_has_new_batch(tmp_path):
    database = tmp_path / "library.sqlite3"
    entered, returned = threading.Event(), threading.Event()
    calls = []

    def executor(task, stop):
        calls.append(task["delivery_id"])
        entered.set()
        assert stop.wait(8)
        returned.set()
        return DeliveryOutcome("suspended")

    repo = LibraryRepository(database)
    with TaskCoordinator(repo, executor, workers=1, queue_capacity=1) as coordinator:
        submitted = coordinator.submit(request(), "submit-1").result(5)
        assert coordinator.active_batch_id is None
        coordinator.run_queued().result(5)
        until(lambda: coordinator.active_batch_id == submitted["batch_id"])
        ids = coordinator.accept_page(submitted["batch_id"], source_page()).result(5)
        assert len(ids) == 8 and entered.wait(5)
        accepted = coordinator.pause(submitted["job_id"], "pause-1").result(5)
        assert accepted["phase"] == "pausing"
        until(lambda: repo.get_batch(submitted["batch_id"])["phase"] == "paused")
        assert returned.is_set() and len(calls) == 1
        assert all(repo.get_delivery(item)["state"] == "suspended" for item in ids)
        assert repo.get_checkpoint(submitted["job_id"], "follow", "history", "artist-7")["cursor_json"] == '{"offset":8}'
        continued = coordinator.resume(submitted["job_id"], "resume-1").result(5)
        assert continued["batch_id"] != submitted["batch_id"]
        assert repo.get_batch(submitted["batch_id"])["phase"] == "paused"
        assert coordinator.active_batch_id is None
    with LibraryRepository(database) as reopened:
        assert len(reopened.list_dispatchable(submitted["job_id"])) == 8
        assert reopened.get_request(submitted["job_id"]) == request()


def test_reopening_recovers_locally_without_calling_executor(tmp_path):
    database = tmp_path / "library.sqlite3"
    with LibraryRepository(database) as before:
        submitted = before.submit(request(), "submit")
        before.activate_next()
        delivery_id = before.accept_source_page(submitted["batch_id"], source_page(1))[0]
        assert before.claim_delivery(delivery_id, submitted["batch_id"])
    calls = []
    repo = LibraryRepository(database)
    with TaskCoordinator(repo, lambda task, stop: calls.append(task), workers=1) as coordinator:
        assert coordinator.active_batch_id is None
        assert calls == []
        assert repo.get_batch(submitted["batch_id"])["phase"] == "blocked"
        assert repo.get_delivery(delivery_id)["state"] == "suspended"
        resumed = coordinator.resume(submitted["job_id"], "resume").result(5)
        assert resumed["batch_id"] != submitted["batch_id"]
        assert coordinator.active_batch_id is None and calls == []


@pytest.mark.parametrize("outcome", [True, DeliveryOutcome("completed")])
def test_executor_cannot_turn_uncommitted_file_into_success(tmp_path, outcome):
    done = threading.Event()

    def executor(task, stop):
        done.set()
        return outcome

    repo = LibraryRepository(tmp_path / "library.sqlite3")
    with TaskCoordinator(repo, executor, workers=1, queue_capacity=1) as coordinator:
        submitted = coordinator.submit(request(), "submit").result(5)
        coordinator.run_queued().result(5)
        until(lambda: coordinator.active_batch_id)
        delivery_id = coordinator.accept_page(submitted["batch_id"], source_page(1)).result(5)[0]
        assert done.wait(5)
        until(lambda: repo.get_delivery(delivery_id)["state"] == "failed")
        assert repo.get_delivery(delivery_id)["attempts"] == 1
        coordinator.pause(submitted["job_id"], "pause").result(5)
        until(lambda: repo.get_batch(submitted["batch_id"])["phase"] == "paused")


def test_second_job_stays_queued_and_close_preserves_it(tmp_path):
    database = tmp_path / "library.sqlite3"
    repo = LibraryRepository(database)
    with TaskCoordinator(repo, lambda task, stop: DeliveryOutcome(), workers=1) as coordinator:
        first = coordinator.submit(request("1"), "first").result(5)
        second = coordinator.submit(request("2"), "second").result(5)
        coordinator.run_queued().result(5)
        until(lambda: coordinator.active_batch_id == first["batch_id"])
        assert repo.get_batch(second["batch_id"])["phase"] == "queued"
        coordinator.cancel(first["job_id"], "cancel").result(5)
        until(lambda: repo.get_batch(first["batch_id"])["phase"] == "cancelled")
        assert repo.get_batch(second["batch_id"])["phase"] == "queued"
        with pytest.raises(Exception):
            coordinator.resume(first["job_id"], "bad-resume").result(5)
    with LibraryRepository(database) as reopened:
        assert reopened.get_batch(first["batch_id"])["phase"] == "cancelled"
        assert reopened.get_batch(second["batch_id"])["phase"] == "queued"


def test_worker_exception_is_a_safe_failed_page_and_shutdown_closes_writer(tmp_path):
    database = tmp_path / "library.sqlite3"

    def executor(task, stop):
        raise RuntimeError("private HTTP headers must not be persisted")

    repo = LibraryRepository(database)
    coordinator = TaskCoordinator(repo, executor, workers=1)
    coordinator.start().result(5)
    submitted = coordinator.submit(request(), "submit").result(5)
    coordinator.run_queued().result(5)
    until(lambda: coordinator.active_batch_id)
    delivery_id = coordinator.accept_page(submitted["batch_id"], source_page(1)).result(5)[0]
    until(lambda: repo.get_delivery(delivery_id)["state"] == "failed")
    coordinator.close()
    with pytest.raises(CoordinatorClosed):
        coordinator.submit(request("2"), "after-close").result(5)
    with LibraryRepository(database) as reopened:
        row = reopened.get_delivery(delivery_id)
        assert row["state"] == "failed"
        assert "private HTTP" not in str(row)


def test_resume_work_limit_drains_admitted_work_and_preserves_the_rest(tmp_path):
    database = tmp_path / "library.sqlite3"
    with LibraryRepository(database) as before:
        submitted = before.submit(request(), "submit")
        before.activate_next()
        ids = before.accept_source_page(submitted["batch_id"], source_page(3))
        before.request_stop(submitted["job_id"], "pause")
        before.settle_stop(submitted["batch_id"])
    calls = []

    def executor(task, stop):
        calls.append(task["delivery_id"])
        assert not stop.is_set()
        return DeliveryOutcome("failed", "simulated")

    repo = LibraryRepository(database)
    with TaskCoordinator(repo, executor, workers=3, queue_capacity=2) as coordinator:
        resumed = coordinator.resume(submitted["job_id"], "resume", BatchBudget(max_works=1)).result(5)
        coordinator.run_queued().result(5)
        until(lambda: repo.get_batch(resumed["batch_id"])["phase"] == "paused")
        assert len(calls) == 1
        assert repo.get_batch(resumed["batch_id"])["stop_reason"] == "work_budget"
        states = [repo.get_delivery(item)["state"] for item in ids]
        assert states.count("failed") == 1 and states.count("suspended") == 2


def test_full_normal_command_queue_keeps_pause_nonblocking(tmp_path):
    database = tmp_path / "library.sqlite3"
    entered, returned = threading.Event(), threading.Event()

    def executor(task, stop):
        entered.set()
        assert stop.wait(8)
        returned.set()
        return DeliveryOutcome("suspended")

    repo = LibraryRepository(database)
    with TaskCoordinator(repo, executor, workers=1, command_capacity=1) as coordinator:
        submitted = coordinator.submit(request(), "submit").result(5)
        coordinator.run_queued().result(5)
        until(lambda: coordinator.active_batch_id)
        coordinator.accept_page(submitted["batch_id"], source_page(1)).result(5)
        assert entered.wait(5)
        blocker = sqlite3.connect(database)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            waiting = coordinator.submit(request("2"), "second")
            until(waiting.running)
            queued = coordinator.submit(request("3"), "third")
            refused = coordinator.submit(request("4"), "fourth")
            with pytest.raises(CommandQueueFull):
                refused.result(1)
            started = time.monotonic()
            paused = coordinator.pause(submitted["job_id"], "pause")
            assert time.monotonic() - started < 0.2
            assert returned.wait(2)
        finally:
            blocker.rollback()
            blocker.close()
        assert waiting.result(5)
        assert queued.result(5)
        assert paused.result(5)["phase"] == "pausing"
        until(lambda: repo.get_batch(submitted["batch_id"])["phase"] == "paused")
