"""Accept-path interleavings: a completion is never turned into a false hold.

Every test here is synthetic: temporary SQLite ledgers, temporary output roots,
synthetic PNG bytes and a fake transport.  No network, account, production
library or real gallery is touched.

The contract under test (core/observation_snapshot.py, UA-01/UA-09):
a preflight snapshot binds the completed generation it looked at; a writer that
sees newer evidence asks for a new preflight instead of writing
`artifact_unknown`; an idempotent page replay never rehashes; an exclusion
cancels every job's queued copy, and the claim/publish gates re-check it.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from core.library_repository import CorruptDataError, LibraryRepository
from core.observation_snapshot import PreflightSnapshot, PreflightUnavailable
from core.preservation_policy import LocalState

import test_u1a_recovery as h
from delivery_fixtures import Response, Transport


@pytest.fixture
def repo(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as value:
        yield value


def test_preflight_snapshot_binds_generation_and_delivery(repo, tmp_path):
    job = h.submit(repo, request_id="binds")
    (pending,) = repo.accept_source_page(job["batch_id"], h.source((h.candidate(),)))
    assert (
        h.run(repo, job, pending, h.executor(repo, tmp_path, Transport(Response()))).state
        == "completed"
    )
    snapshot = repo._preflight_page_health(
        job["batch_id"],
        h.source(
            (h.candidate(),), page_identity="page-2", before='"cursor-1"', after='"cursor-2"'
        ),
    )
    assert isinstance(snapshot, PreflightSnapshot)
    matching = [
        page
        for page in snapshot.pages.values()
        if page.key.illust_id == h.ARTWORK and page.key.page_index == 0
    ]
    assert len(matching) == 1
    page = matching[0]
    assert page.generation == 1
    assert page.delivery_id == pending
    assert page.observed and page.observed[0].state is LocalState.HASH_MATCH
    assert snapshot.degraded is False


def test_completion_before_preflight_links_the_completed_page(repo, tmp_path):
    job = h.submit(repo, request_id="already-done")
    (pending,) = repo.accept_source_page(job["batch_id"], h.source((h.candidate(),)))
    assert (
        h.run(repo, job, pending, h.executor(repo, tmp_path, Transport(Response()))).state
        == "completed"
    )
    ids = repo.accept_source_page(
        job["batch_id"],
        h.source(
            (h.candidate(),), page_identity="page-2", before='"cursor-1"', after='"cursor-2"'
        ),
    )
    assert ids == (pending,)
    counts = h.pages(repo, job)
    assert counts["needs_verification"] == 0
    assert counts["completed"] == 1
    assert h.target(tmp_path).read_bytes() == h.PNG_BYTES


def test_completion_after_preflight_retries_once_without_a_hold(repo, tmp_path):
    job = h.submit(repo, request_id="stale-snapshot")
    (pending,) = repo.accept_source_page(job["batch_id"], h.source((h.candidate(),)))
    transport = Transport(Response())
    service = h.executor(repo, tmp_path, transport)
    original = repo._preflight_page_health
    sizes = []

    def stale(batch_id, page):
        snapshot = original(batch_id, page)
        sizes.append(len(snapshot))
        if len(sizes) == 1:
            assert h.run(repo, job, pending, service).state == "completed"
        return snapshot

    repo._preflight_page_health = stale
    try:
        ids = repo.accept_source_page(
            job["batch_id"],
            h.source(
                (h.candidate(),), page_identity="page-2", before='"cursor-1"', after='"cursor-2"'
            ),
        )
    finally:
        repo._preflight_page_health = original
    assert ids == (pending,)
    assert sizes == [0, 1]
    counts = h.pages(repo, job)
    assert counts["needs_verification"] == 0
    assert counts["completed"] == 1
    assert len(transport.calls) == 1
    checkpoints = h.query(
        repo.database_path,
        "SELECT cursor_json FROM checkpoints WHERE job_id = ?",
        (job["job_id"],),
    )
    assert checkpoints and checkpoints[0]["cursor_json"] == '"cursor-2"'
    assert repo.budget_status(job["batch_id"])["used_works"] == 1


def test_completion_before_the_writer_retries_without_a_hold(repo, tmp_path):
    job = h.submit(repo, request_id="before-writer")
    (pending,) = repo.accept_source_page(job["batch_id"], h.source((h.candidate(),)))
    transport = Transport(Response())
    service = h.executor(repo, tmp_path, transport)
    ready, done = threading.Event(), threading.Event()
    completed = {}

    def complete_the_page():
        assert ready.wait(5)
        completed["state"] = h.run(repo, job, pending, service).state
        done.set()

    worker = threading.Thread(target=complete_the_page, name="complete-before-writer")
    worker.start()
    original_enqueue = repo._enqueue
    first_call = True

    def enqueue(kind, payload, **kwargs):
        nonlocal first_call
        if kind == "accept_source_page" and first_call:
            first_call = False
            ready.set()
            assert done.wait(5)
        return original_enqueue(kind, payload, **kwargs)

    repo._enqueue = enqueue
    try:
        ids = repo.accept_source_page(
            job["batch_id"],
            h.source(
                (h.candidate(),), page_identity="page-2", before='"cursor-1"', after='"cursor-2"'
            ),
        )
    finally:
        repo._enqueue = original_enqueue
        worker.join(5)
    assert completed["state"] == "completed"
    assert ids == (pending,)
    counts = h.pages(repo, job)
    assert counts["needs_verification"] == 0
    assert counts["completed"] == 1
    assert len(transport.calls) == 1


def test_another_task_completion_is_reused_without_downloading_again(repo, tmp_path):
    first, delivery = h.first_delivery(repo, tmp_path)
    job = h.submit(repo, request_id="cross-task")
    ids = repo.accept_source_page(
        job["batch_id"], h.source((h.candidate(),), page_identity="page-cross")
    )
    assert ids == ()
    counts = h.pages(repo, job)
    assert counts["needs_verification"] == 0
    links = h.query(
        repo.database_path,
        "SELECT delivery_id, artifact_id FROM batch_item_pages "
        "WHERE batch_id = ? AND illust_id = ? AND page_index = 0",
        (job["batch_id"], h.ARTWORK),
    )
    assert links and links[0]["artifact_id"] is not None
    assert len(h.delivery_rows(repo.database_path)) == 1
    assert repo.get_delivery(delivery)["state"] == "completed"


def test_replay_returns_the_same_ids_without_hashing_again(repo, tmp_path):
    h.first_delivery(repo, tmp_path)
    job = h.submit(repo, request_id="replay")
    page = h.source((h.candidate(),), page_identity="page-replay")
    calls = []
    original = repo._observe_artifact

    def count(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    repo._observe_artifact = count
    try:
        first = repo.accept_source_page(job["batch_id"], page)
        after_first = len(calls)
        second = repo.accept_source_page(job["batch_id"], page)
    finally:
        repo._observe_artifact = original
    assert first == second
    assert after_first == 1
    assert len(calls) == 1


def test_corrupt_intent_blocks_the_page_and_keeps_rows(repo, tmp_path):
    first, _ = h.first_delivery(repo, tmp_path)
    h.target(tmp_path).unlink()
    job = h.submit(repo, request_id="corrupt")
    repo.record_removal_intent(
        first["batch_id"], h.ARTWORK, 0, h.OUTPUT_REF,
        intent="excluded", command_id="intent-corrupt",
    )
    with sqlite3.connect(repo.database_path) as conn:
        conn.execute(
            "UPDATE events SET safe_payload = ? WHERE kind = 'removal_intent'",
            ("{broken",),
        )
    before = h.delivery_rows(repo.database_path)
    with pytest.raises(CorruptDataError):
        repo.accept_source_page(
            job["batch_id"], h.source((h.candidate(),), page_identity="page-corrupt")
        )
    assert h.delivery_rows(repo.database_path) == before
    assert repo.list_dispatchable(job["job_id"]) == ()


def test_exclusion_from_history_cancels_a_queued_repair_in_another_job(repo, tmp_path):
    first, _ = h.first_delivery(repo, tmp_path)
    h.target(tmp_path).unlink()
    second, (pending,) = h.second_task(repo, tmp_path)
    repo.record_removal_intent(
        first["batch_id"], h.ARTWORK, 0, h.OUTPUT_REF,
        intent="excluded", command_id="exclude-history",
    )
    cancelled = repo.get_delivery(pending)
    assert cancelled["state"] == "cancelled"
    assert cancelled["error_code"] == "intent_excluded"
    assert repo.list_dispatchable(second["job_id"]) == ()
    assert not h.target(tmp_path).exists()


def test_claim_refuses_an_intent_recorded_after_the_repair_was_queued(repo, tmp_path):
    first, _ = h.first_delivery(repo, tmp_path)
    h.target(tmp_path).unlink()
    second, (pending,) = h.second_task(repo, tmp_path)
    repo.record_removal_intent(
        second["batch_id"], h.ARTWORK, 0, h.OUTPUT_REF,
        intent="excluded", command_id="exclude-claim",
    )
    # Fault injection: pretend the cancellation half of that command never ran,
    # so only the claim gate can stop the download.
    with sqlite3.connect(repo.database_path) as conn:
        conn.execute(
            "UPDATE deliveries SET state = 'pending', error_code = NULL "
            "WHERE delivery_id = ?",
            (pending,),
        )
    assert repo.claim_delivery(pending, second["batch_id"]) is None
    blocked = repo.get_delivery(pending)
    assert blocked["state"] == "cancelled"
    assert blocked["error_code"] == "intent_excluded"
    assert repo.list_dispatchable(second["job_id"]) == ()


def test_publish_authorized_tracks_the_current_intent(repo, tmp_path):
    first, delivery = h.first_delivery(repo, tmp_path)
    assert repo.publish_authorized(delivery) == {
        "delivery_id": delivery, "allowed": True, "reason": "ok",
    }
    repo.record_removal_intent(
        first["batch_id"], h.ARTWORK, 0, h.OUTPUT_REF,
        intent="excluded", command_id="publish-exclude",
    )
    denied = repo.publish_authorized(delivery)
    assert denied["allowed"] is False
    assert denied["reason"] == "intent_excluded"
    with sqlite3.connect(repo.database_path) as conn:
        conn.execute(
            "UPDATE events SET safe_payload = ? WHERE kind = 'removal_intent'",
            ("{broken",),
        )
    unreadable = repo.publish_authorized(delivery)
    assert unreadable["allowed"] is False
    assert unreadable["reason"] == "intent_unreadable"


def test_revoked_exclusion_allows_publication_again(repo, tmp_path):
    first, delivery = h.first_delivery(repo, tmp_path)
    repo.record_removal_intent(
        first["batch_id"], h.ARTWORK, 0, h.OUTPUT_REF,
        intent="excluded", command_id="exclude-then-revoke-1",
    )
    assert repo.publish_authorized(delivery)["allowed"] is False
    repo.record_removal_intent(
        first["batch_id"], h.ARTWORK, 0, h.OUTPUT_REF,
        intent="none", command_id="exclude-then-revoke-2",
    )
    assert repo.publish_authorized(delivery)["allowed"] is True
def test_degraded_preflight_holds_with_an_honest_code(repo, tmp_path):
    job = h.submit(repo, request_id="degraded")
    (pending,) = repo.accept_source_page(job["batch_id"], h.source((h.candidate(),)))
    assert (
        h.run(repo, job, pending, h.executor(repo, tmp_path, Transport(Response()))).state
        == "completed"
    )
    original = repo._path_resolver_snapshot
    repo._path_resolver_snapshot = lambda: None
    try:
        ids = repo.accept_source_page(
            job["batch_id"],
            h.source(
                (h.candidate(),), page_identity="page-2", before='"cursor-1"', after='"cursor-2"'
            ),
        )
    finally:
        repo._path_resolver_snapshot = original
    held = repo.get_delivery(ids[0])
    assert held["state"] == "needs_verification"
    assert held["error_code"] == "artifact_health_unavailable"


def test_exhausted_retries_raise_and_write_nothing(repo, tmp_path):
    job = h.submit(repo, request_id="exhausted")
    (pending,) = repo.accept_source_page(job["batch_id"], h.source((h.candidate(),)))
    assert (
        h.run(repo, job, pending, h.executor(repo, tmp_path, Transport(Response()))).state
        == "completed"
    )
    original = repo._preflight_page_health
    calls = []

    def always_empty(batch_id, page):
        calls.append(1)
        return PreflightSnapshot(run_id="batch-" + batch_id, pages={})

    repo._preflight_page_health = always_empty
    before = h.delivery_rows(repo.database_path)
    try:
        with pytest.raises(PreflightUnavailable):
            repo.accept_source_page(
                job["batch_id"],
                h.source(
                    (h.candidate(),),
                    page_identity="page-2",
                    before='"cursor-1"',
                    after='"cursor-2"',
                ),
            )
    finally:
        repo._preflight_page_health = original
    assert len(calls) == 3
    assert h.delivery_rows(repo.database_path) == before
    assert repo.get_batch(job["batch_id"])["phase"] == "running"
