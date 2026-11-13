"""Control-protocol regressions for the RF round (RF-03/06/07/08).

Every test uses an in-memory or temporary SQLite ledger, synthetic pages and a
fake transport.  Ordering is established with Events, never with sleeps; the
only wall-clock waits are response bounds (a pause must answer, a close must
not hang) and they are generous.
"""
from __future__ import annotations

import threading
import time

import pytest

import test_u1a_recovery as h
from core.library_repository import InvalidStateError, LibraryRepository
from core.task_coordinator import CommandQueueFull, TaskCoordinator


def until(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _active(service, request_id="rf-control"):
    job = service.submit(h.request(request_id), "submit-" + request_id).result(5)
    service.run_queued().result(5)
    assert until(lambda: service.active_batch_id == job["batch_id"])
    return job


def _seed_completed(repo, tmp_path):
    """One completed artifact so the next page has something to observe."""
    job = h.submit(repo, request_id="rf-seed")
    ids = h.accept(repo, job["batch_id"], h.source((h.candidate(),)))
    outcome = h.run(
        repo, job, ids[0], h.executor(repo, tmp_path, h.Transport(h.Response()))
    )
    assert outcome.state == "completed"
    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"])


def _coordinator(repo, tmp_path):
    service = TaskCoordinator(repo, h.executor(repo, tmp_path, h.Transport()), workers=1)
    service.start().result(5)
    return service


def test_pause_before_admission_rejects_the_page_without_queueing(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        service = _coordinator(repo, tmp_path)
        try:
            job = _active(service)
            service.pause(job["job_id"], "pause-before-admission").result(5)
            future = service.accept_page(job["batch_id"], h.source((h.candidate(),)))
            with pytest.raises(InvalidStateError):
                future.result(5)
            assert service._observations.qsize() == 0
            assert service._outstanding == 0
        finally:
            service.close(10)


def test_a_ticket_taken_before_pause_may_finish(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _seed_completed(repo, tmp_path)
        service = _coordinator(repo, tmp_path)
        entered, release = threading.Event(), threading.Event()
        observe = repo._observe_artifact

        def blocked(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return observe(*args, **kwargs)

        monkeypatch.setattr(repo, "_observe_artifact", blocked)
        try:
            job = _active(service)
            future = service.accept_page(job["batch_id"], h.source((h.candidate(),)))
            assert entered.wait(5)
            service.pause(job["job_id"], "pause-after-admission").result(5)
            assert repo.get_batch(job["batch_id"])["phase"] == "pausing"
            release.set()
            ids = future.result(10)
            assert isinstance(ids, tuple)
            # A trusted reuse links the artifact without a new delivery id;
            # either outcome is a committed page.
            assert until(
                lambda: repo.batch_results(job["batch_id"])["pages"]["completed"] == 1
            )
            assert until(lambda: repo.get_batch(job["batch_id"])["phase"] == "paused")
        finally:
            release.set()
            service.close(10)


def test_settle_waits_for_a_ticket_whose_observation_is_still_running(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _seed_completed(repo, tmp_path)
        service = _coordinator(repo, tmp_path)
        entered, release = threading.Event(), threading.Event()
        observe = repo._observe_artifact

        def blocked(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return observe(*args, **kwargs)

        monkeypatch.setattr(repo, "_observe_artifact", blocked)
        try:
            job = _active(service)
            future = service.accept_page(job["batch_id"], h.source((h.candidate(),)))
            assert entered.wait(5)
            service.pause(job["job_id"], "pause-wait-ticket").result(5)
            # The durable stop is visible, but the outstanding ticket keeps the
            # batch from settling to 'paused'.
            assert until(lambda: repo.get_batch(job["batch_id"])["phase"] == "pausing")
            # The ticket is still outstanding, so settle cannot have run even
            # though the durable stop is already visible.
            assert service._outstanding == 1
            assert repo.get_batch(job["batch_id"])["phase"] == "pausing"
            release.set()
            future.result(10)
            assert until(lambda: repo.get_batch(job["batch_id"])["phase"] == "paused")
        finally:
            release.set()
            service.close(10)


def test_the_observation_queue_refuses_a_page_when_full(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        service = _coordinator(repo, tmp_path)
        entered, release = threading.Event(), threading.Event()
        original = repo.accept_source_page

        def blocked(batch_id, page, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(batch_id, page, **kwargs)

        monkeypatch.setattr(repo, "accept_source_page", blocked)
        try:
            job = _active(service)
            first = service.accept_page(job["batch_id"], h.source((h.candidate(),)))
            assert entered.wait(5)
            extra = [
                service.accept_page(job["batch_id"], h.source((h.candidate(),)))
                for _ in range(17)
            ]
            with pytest.raises(CommandQueueFull):
                extra[-1].result(5)
            assert service._outstanding <= 17
            release.set()
            first.result(10)
            service.close(10)
        finally:
            release.set()


def test_a_pre_cancelled_accept_releases_its_ticket(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        service = _coordinator(repo, tmp_path)
        entered, release = threading.Event(), threading.Event()
        original = repo.accept_source_page

        def blocked(batch_id, page, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(batch_id, page, **kwargs)

        monkeypatch.setattr(repo, "accept_source_page", blocked)
        try:
            job = _active(service)
            first = service.accept_page(job["batch_id"], h.source((h.candidate(),)))
            assert entered.wait(5)
            cancelled = service.accept_page(job["batch_id"], h.source((h.candidate(),)))
            assert cancelled.cancel()
            release.set()
            first.result(10)
            assert until(lambda: service._outstanding == 0)
            service.pause(job["job_id"], "pause-after-cancel").result(5)
            assert until(lambda: repo.get_batch(job["batch_id"])["phase"] == "paused")
        finally:
            release.set()
            service.close(10)


def test_an_owner_failure_stops_every_service_thread(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        service = _coordinator(repo, tmp_path)
        try:
            job = _active(service)
            original = repo.get_batch
            calls = []

            def failing(batch_id):
                calls.append(batch_id)
                raise RuntimeError("synthetic-owner-failure")

            monkeypatch.setattr(repo, "get_batch", failing)
            assert until(lambda: service._shutdown.is_set(), 5)
            service._owner.join(2)
            service._observer.join(2)
            service._control_thread.join(2)
            assert not service._observer.is_alive()
            assert not service._control_thread.is_alive()
            assert calls
            monkeypatch.setattr(repo, "get_batch", original)
            assert job["batch_id"]
        finally:
            try:
                service.close(10)
            except RuntimeError:
                pass


def test_close_is_idempotent_and_can_retry_after_a_timeout(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        service = _coordinator(repo, tmp_path)
        entered, release = threading.Event(), threading.Event()
        original = repo.accept_source_page

        def blocked(batch_id, page, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(batch_id, page, **kwargs)

        monkeypatch.setattr(repo, "accept_source_page", blocked)
        job = _active(service)
        try:
            future = service.accept_page(job["batch_id"], h.source((h.candidate(),)))
            assert entered.wait(5)
            with pytest.raises(TimeoutError):
                service.close(0.2)
            release.set()
            with pytest.raises(Exception):
                future.result(10)
        finally:
            release.set()
        service.close(10)
        service.close(10)
        assert service._closed
