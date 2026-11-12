#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline P2 tests for the single-library persistence boundary.

These tests use only tmp_path databases and synthetic contracts.  They never
open the legacy databases, the GUI, HTTP sessions or production files.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from core.library_paths import InstanceLock
from core.library_repository import (
    BudgetExceeded,
    CommandConflict,
    CorruptDataError,
    CursorConflict,
    InvalidStateError,
    LibraryClosedError,
    LibraryError,
    LibraryRepository,
    NotFoundError,
    OutputConflict,
    PageCoverageError,
    SchemaVersionError,
)
from core.task_contracts import (
    BatchBudget,
    CandidateDecision,
    CollectionRequest,
    OutputSpec,
    PageSpec,
    PathRef,
    QualitySpec,
    QuerySpec,
    SourcePage,
)


def _request(
    request_id: str = "req-1",
    *,
    root: str = "library/out",
    all_pages: bool = True,
    budget: BatchBudget | None = None,
    dedup_policy: str = "trusted",
) -> CollectionRequest:
    return CollectionRequest(
        request_id=request_id,
        account_ref="account-test",
        query=QuerySpec(),
        output=OutputSpec(root_ref=PathRef(relative_path=root)),
        quality=QualitySpec(original=True, all_pages=all_pages),
        budget=budget
        or BatchBudget(max_works=10, max_requests=10, max_artists=5),
        dedup_policy=dedup_policy,
    )


def _candidate(
    illust_id: str,
    page_indices: tuple[int, ...] = (0,),
    *,
    root: str = "library/out",
    decision: str = "download",
    page_count: int | None = None,
    artist_id: str = "artist-1",
    title: str = "Title",
) -> CandidateDecision:
    pages = tuple(
        PageSpec(
            page_index=index,
            output_ref=PathRef(
                relative_path=f"{root}/{illust_id}_p{index}.jpg"
            ),
            url=f"https://i.pximg.net/img-original/{illust_id}_p{index}.jpg",
            expected_size=123,
        )
        for index in page_indices
    )
    if decision == "download" and page_count is None:
        page_count = max(page_indices) + 1
    return CandidateDecision(
        illust_id=illust_id,
        decision=decision,
        artist_id=artist_id,
        artist_name="Artist",
        title=title,
        page_count=page_count,
        pages=pages,
    )


def _candidate_at_output(
    illust_id: str, relative_path: str, *, page_index: int = 0
) -> CandidateDecision:
    page = PageSpec(
        page_index=page_index,
        output_ref=PathRef(relative_path=relative_path),
        url=f"https://i.pximg.net/img-original/{illust_id}_p{page_index}.jpg",
        expected_size=123,
    )
    return CandidateDecision(
        illust_id=illust_id,
        decision="download",
        artist_id="artist-1",
        artist_name="Artist",
        title="Title",
        page_count=max(page_index + 1, 1),
        pages=(page,),
    )


def _source_page(
    candidates: tuple[CandidateDecision, ...],
    *,
    page_identity: str = "page-1",
    before: str = "null",
    after: str = '"cursor-1"',
    source_id: str = "follow",
    pass_id: str = "pass-1",
    entity_id: str = "artist-1",
    exhausted: bool = False,
) -> SourcePage:
    return SourcePage(
        source_id=source_id,
        pass_id=pass_id,
        entity_id=entity_id,
        page_identity=page_identity,
        candidates=candidates,
        cursor_before_json=before,
        cursor_after_json=after,
        exhausted=exhausted,
        actual_sort="date_desc",
        coverage_json='{"seen":1}',
    )


def _submit_and_activate(repo: LibraryRepository, request: CollectionRequest) -> dict[str, str]:
    result = repo.submit(request, f"submit-{request.request_id}")
    activated = repo.activate_next()
    assert activated is not None
    assert activated["batch_id"] == result["batch_id"]
    return result


def _raw(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Commands and batch lifecycle
# ---------------------------------------------------------------------------


def test_submit_command_replay_and_payload_conflict(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    request = _request()
    with LibraryRepository(db_path) as repo:
        first = repo.submit(request, "cmd-submit")
        assert repo.submit(request, "cmd-submit") == first
        assert repo.get_request(first["job_id"]) == request
        batch = repo.get_batch(first["batch_id"])
        assert batch["phase"] == "queued"
        assert batch["budget"]["max_works"] == 10

        with pytest.raises(CommandConflict):
            repo.submit(_request("req-other"), "cmd-submit")
        with pytest.raises(CommandConflict):
            repo.resume(first["job_id"], "cmd-submit")
        with pytest.raises(NotFoundError):
            repo.get_request("missing-job")
        with pytest.raises(NotFoundError):
            repo.get_batch("missing-batch")
        with pytest.raises(NotFoundError):
            repo.get_delivery("missing-delivery")
        assert repo.get_checkpoint("missing-job", "s", "p", "e") is None


def test_single_active_batch_and_activation_order(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        first = repo.submit(_request("req-1"), "submit-1")
        second = repo.submit(_request("req-2"), "submit-2")
        active = repo.activate_next()
        assert active is not None
        assert active["batch_id"] == first["batch_id"]
        assert active["phase"] == "running"
        assert repo.activate_next() is None

        stop = repo.request_stop(first["job_id"], "stop-1", cancel=False)
        assert stop == {
            "job_id": first["job_id"],
            "batch_id": first["batch_id"],
            "phase": "pausing",
        }
        assert repo.get_batch(first["batch_id"])["phase"] == "pausing"
        assert repo.settle_stop(first["batch_id"]) is True
        assert repo.get_batch(first["batch_id"])["phase"] == "paused"

        next_active = repo.activate_next()
        assert next_active is not None
        assert next_active["batch_id"] == second["batch_id"]


def test_stopping_a_queued_batch_is_direct_and_resumable(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        submitted = repo.submit(_request(), "submit-queued")
        stop = repo.request_stop(submitted["job_id"], "stop-queued", cancel=False)
        assert stop["phase"] == "paused"
        assert repo.get_batch(submitted["batch_id"])["phase"] == "paused"
        assert repo.activate_next() is None

        resumed = repo.resume(submitted["job_id"], "resume-queued")
        assert resumed["batch_id"] != submitted["batch_id"]
        assert repo.get_batch(resumed["batch_id"])["phase"] == "queued"
        assert repo.activate_next()["batch_id"] == resumed["batch_id"]

        # A running job does not get a duplicate batch from another resume.
        assert (
            repo.resume(submitted["job_id"], "resume-running")["batch_id"]
            == resumed["batch_id"]
        )


# ---------------------------------------------------------------------------
# Source page acceptance, cursor and idempotency
# ---------------------------------------------------------------------------


def test_accept_page_persists_deliveries_checkpoint_and_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    request = _request()
    page = _source_page(
        (
            _candidate("100", (0, 1), page_count=2),
            _candidate("200", (), decision="filtered", page_count=None),
        )
    )
    with LibraryRepository(db_path) as repo:
        submitted = _submit_and_activate(repo, request)
        ids = repo.accept_source_page(submitted["batch_id"], page)
        assert len(ids) == 2

        checkpoint = repo.get_checkpoint(
            submitted["job_id"], "follow", "pass-1", "artist-1"
        )
        assert checkpoint is not None
        assert checkpoint["cursor_json"] == '"cursor-1"'
        assert checkpoint["exhausted"] is False
        assert checkpoint["coverage_json"] == '{"seen":1}'
        assert checkpoint["actual_sort"] == "date_desc"

        snapshot = repo.snapshot(submitted["job_id"])
        assert snapshot["batch_id"] == submitted["batch_id"]
        assert snapshot["phase"] == "running"
        assert snapshot["revision"] >= 1
        assert snapshot["counts"]["works"]["total"] == 2
        assert snapshot["counts"]["works"]["download"] == 1
        assert snapshot["counts"]["works"]["filtered"] == 1
        assert snapshot["counts"]["pages"]["total"] == 2
        assert snapshot["counts"]["pages"]["pending"] == 2

        delivery = repo.get_delivery(ids[0])
        for key in (
            "delivery_id",
            "job_id",
            "origin_batch_id",
            "current_batch_id",
            "state",
            "attempts",
            "payload_json",
            "output_key",
            "page_index",
            "illust_id",
            "quality_json",
            "generation",
        ):
            assert key in delivery
        payload = json.loads(delivery["payload_json"])
        assert set(payload) == {
            "illust_id",
            "artist_id",
            "artist_name",
            "title",
            "page",
            "quality",
        }
        assert payload["page"]["page_index"] == 0
        assert payload["quality"]["all_pages"] is True
        assert delivery["state"] == "pending"
        assert delivery["attempts"] == 0
        assert delivery["generation"] == 1

        assert repo.accept_source_page(submitted["batch_id"], page) == ids

        changed = _source_page(
            (_candidate("100", (0, 1), page_count=2),),
            page_identity="page-1",
            before='"cursor-1"',
            after='"cursor-2"',
        )
        with pytest.raises(CursorConflict):
            repo.accept_source_page(submitted["batch_id"], changed)


def test_cursor_mismatch_and_duplicate_work_across_sources(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    request = _request(budget=BatchBudget(max_works=1, max_requests=5, max_artists=2))
    first_page = _source_page(
        (_candidate("100", (0, 1), page_count=2),),
        page_identity="page-1",
        before="null",
        after='"cursor-1"',
    )
    second_page = _source_page(
        (_candidate("100", (0, 1), page_count=2),),
        page_identity="page-2",
        before="null",
        after='"cursor-2"',
        source_id="ranking",
        pass_id="pass-2",
        entity_id="rank-1",
    )
    with LibraryRepository(db_path) as repo:
        submitted = _submit_and_activate(repo, request)
        first_ids = repo.accept_source_page(submitted["batch_id"], first_page)
        second_ids = repo.accept_source_page(submitted["batch_id"], second_page)
        assert first_ids == second_ids
        with closing(_raw(db_path)) as raw:
            assert raw.execute("SELECT COUNT(*) FROM batch_items").fetchone()[0] == 1
            assert raw.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 2
            assert raw.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 2
        snapshot = repo.snapshot(submitted["job_id"])
        assert snapshot["counts"]["works"]["total"] == 1
        assert snapshot["counts"]["works"]["download"] == 1
        assert snapshot["counts"]["pages"]["total"] == 2

        bad_cursor = _source_page(
            (_candidate("101", (0,), page_count=1),),
            page_identity="page-3",
            before="null",
            after='"cursor-3"',
        )
        with pytest.raises(CursorConflict):
            repo.accept_source_page(submitted["batch_id"], bad_cursor)


def test_accept_page_transaction_rolls_back_on_trigger_failure(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    request = _request()
    page = _source_page((_candidate("300", (0,), page_count=1),))

    repo = LibraryRepository(db_path)
    try:
        submitted = _submit_and_activate(repo, request)
    finally:
        repo.close()

    with closing(_raw(db_path)) as raw:
        raw.execute(
            "CREATE TRIGGER fail_delivery BEFORE INSERT ON deliveries "
            "BEGIN SELECT RAISE(ABORT, 'forced delivery failure'); END"
        )
        raw.commit()

    with LibraryRepository(db_path) as repo:
        with pytest.raises(sqlite3.IntegrityError):
            repo.accept_source_page(submitted["batch_id"], page)
        assert (
            repo.get_checkpoint(
                submitted["job_id"], "follow", "pass-1", "artist-1"
            )
            is None
        )
        snapshot = repo.snapshot(submitted["job_id"])
        assert snapshot["counts"]["works"]["total"] == 0
        assert snapshot["counts"]["pages"]["total"] == 0


# ---------------------------------------------------------------------------
# Page policy, budget and output identity
# ---------------------------------------------------------------------------


def test_budget_exceeded_rolls_back_page_and_does_not_advance_cursor(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    request = _request(budget=BatchBudget(max_works=1, max_requests=5, max_artists=2))
    too_many = _source_page(
        (
            _candidate("100", (0,), page_count=1),
            _candidate("101", (0,), page_count=1),
        ),
        page_identity="too-many",
    )
    accepted = _source_page(
        (
            _candidate("100", (0,), page_count=1),
            _candidate("101", (), decision="filtered", page_count=None),
        ),
        page_identity="accepted",
        after='"cursor-1"',
    )
    with LibraryRepository(db_path) as repo:
        submitted = _submit_and_activate(repo, request)
        with pytest.raises(BudgetExceeded):
            repo.accept_source_page(submitted["batch_id"], too_many)
        assert (
            repo.get_checkpoint(
                submitted["job_id"], "follow", "pass-1", "artist-1"
            )
            is None
        )
        ids = repo.accept_source_page(submitted["batch_id"], accepted)
        assert len(ids) == 1
        snapshot = repo.snapshot(submitted["job_id"])
        assert snapshot["counts"]["works"]["total"] == 2
        assert snapshot["counts"]["works"]["download"] == 1
        assert snapshot["counts"]["works"]["filtered"] == 1
        assert snapshot["counts"]["pages"]["total"] == 1


def test_all_pages_and_selected_page_policies(tmp_path: Path) -> None:
    all_pages_db = tmp_path / "all-pages.sqlite3"
    incomplete = _source_page(
        (_candidate("100", (0,), page_count=2),),
        page_identity="incomplete",
    )
    with LibraryRepository(all_pages_db) as repo:
        submitted = _submit_and_activate(repo, _request(all_pages=True))
        with pytest.raises(PageCoverageError):
            repo.accept_source_page(submitted["batch_id"], incomplete)
        assert (
            repo.get_checkpoint(
                submitted["job_id"], "follow", "pass-1", "artist-1"
            )
            is None
        )

    selected_db = tmp_path / "selected.sqlite3"
    wrong_page = _source_page(
        (_candidate("100", (1,), page_count=2),),
        page_identity="wrong-page",
    )
    page_zero = _source_page(
        (_candidate("100", (0,), page_count=2),),
        page_identity="page-zero",
        after='"cursor-1"',
    )
    with LibraryRepository(selected_db) as repo:
        submitted = _submit_and_activate(repo, _request(all_pages=False))
        with pytest.raises(PageCoverageError):
            repo.accept_source_page(submitted["batch_id"], wrong_page)
        assert len(repo.accept_source_page(submitted["batch_id"], page_zero)) == 1


def test_output_path_must_be_inside_root_case_insensitively(tmp_path: Path) -> None:
    outside_db = tmp_path / "outside.sqlite3"
    outside = _source_page(
        (_candidate_at_output("100", "other/100_p0.jpg"),),
        page_identity="outside",
    )
    with LibraryRepository(outside_db) as repo:
        submitted = _submit_and_activate(repo, _request(root="library/out"))
        with pytest.raises(OutputConflict):
            repo.accept_source_page(submitted["batch_id"], outside)
        assert (
            repo.get_checkpoint(
                submitted["job_id"], "follow", "pass-1", "artist-1"
            )
            is None
        )

    case_db = tmp_path / "case.sqlite3"
    case_page = _source_page(
        (_candidate_at_output("100", "library/out/100_p0.jpg"),),
        page_identity="case",
        after='"cursor-1"',
    )
    with LibraryRepository(case_db) as repo:
        submitted = _submit_and_activate(repo, _request(root="Library/Out"))
        assert len(repo.accept_source_page(submitted["batch_id"], case_page)) == 1


def test_output_conflict_with_another_job_rolls_back(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    shared_output = "library/out/shared.jpg"
    first_request = _request("req-1")
    second_request = _request("req-2")
    first_page = _source_page(
        (_candidate_at_output("100", shared_output),),
        page_identity="first",
        after='"cursor-1"',
    )
    second_page = _source_page(
        (_candidate_at_output("200", shared_output),),
        page_identity="second",
    )
    with LibraryRepository(db_path) as repo:
        first = _submit_and_activate(repo, first_request)
        repo.accept_source_page(first["batch_id"], first_page)
        repo.request_stop(first["job_id"], "stop-first", cancel=False)
        assert repo.settle_stop(first["batch_id"]) is True

        second = _submit_and_activate(repo, second_request)
        with pytest.raises(OutputConflict):
            repo.accept_source_page(second["batch_id"], second_page)
        assert (
            repo.get_checkpoint(second["job_id"], "follow", "pass-1", "artist-1")
            is None
        )
        snapshot = repo.snapshot(second["job_id"])
        assert snapshot["counts"]["works"]["total"] == 0
        assert snapshot["counts"]["pages"]["total"] == 0


# ---------------------------------------------------------------------------
# Dispatch, claims and releases
# ---------------------------------------------------------------------------


def test_dispatch_listing_claim_and_release(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    page = _source_page(
        (
            _candidate("100", (0,), page_count=1),
            _candidate("101", (0,), page_count=1),
            _candidate("102", (0,), page_count=1),
        )
    )
    with LibraryRepository(db_path) as repo:
        submitted = _submit_and_activate(repo, _request())
        ids = repo.accept_source_page(submitted["batch_id"], page)
        assert len(ids) == 3

        first_page = repo.list_dispatchable(submitted["job_id"], limit=2)
        assert len(first_page) == 2
        excluded_page = repo.list_dispatchable(
            submitted["job_id"], limit=2, exclude_ids=(ids[0],)
        )
        assert ids[0] not in excluded_page
        assert len(excluded_page) == 2

        delivery_id = first_page[0]
        claimed = repo.claim_delivery(delivery_id, submitted["batch_id"])
        assert claimed is not None
        assert claimed["state"] == "transferring"
        assert claimed["attempts"] == 1
        assert claimed["current_batch_id"] == submitted["batch_id"]
        assert repo.claim_delivery(delivery_id, submitted["batch_id"]) is None

        repo.release_delivery(
            delivery_id, submitted["batch_id"], state="suspended", error_code="network"
        )
        suspended = repo.get_delivery(delivery_id)
        assert suspended["state"] == "suspended"
        assert suspended["error_code"] == "network"
        assert delivery_id in repo.list_dispatchable(submitted["job_id"])

        with pytest.raises(LibraryError):
            repo.release_delivery(
                delivery_id, submitted["batch_id"], state="completed"
            )
        with pytest.raises(LibraryError):
            repo.release_delivery(
                delivery_id, submitted["batch_id"], error_code="bad code"
            )

        assert repo.claim_delivery(delivery_id, submitted["batch_id"]) is not None
        repo.release_delivery(
            delivery_id, submitted["batch_id"], state="retry_wait"
        )
        retry = repo.get_delivery(delivery_id)
        assert retry["state"] == "retry_wait"
        assert retry["retry_at"] is not None
        assert delivery_id in repo.list_dispatchable(submitted["job_id"])

        assert repo.claim_delivery(delivery_id, submitted["batch_id"]) is not None
        repo.release_delivery(
            delivery_id, submitted["batch_id"], state="failed", error_code="forced"
        )
        assert repo.get_delivery(delivery_id)["state"] == "failed"
        assert delivery_id not in repo.list_dispatchable(submitted["job_id"])


# ---------------------------------------------------------------------------
# Pause, resume and cancellation
# ---------------------------------------------------------------------------


def test_pause_resume_cancel_keep_old_batches_and_ownership(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    request = _request()
    page = _source_page((_candidate("100", (0,), page_count=1),), after='"cursor-1"')
    with LibraryRepository(db_path) as repo:
        submitted = _submit_and_activate(repo, request)
        delivery_id = repo.accept_source_page(submitted["batch_id"], page)[0]
        assert repo.claim_delivery(delivery_id, submitted["batch_id"]) is not None

        stop = repo.request_stop(submitted["job_id"], "stop-1", cancel=False)
        assert stop["phase"] == "pausing"
        assert repo.request_stop(
            submitted["job_id"], "stop-1", cancel=False
        ) == stop
        with pytest.raises(CommandConflict):
            repo.request_stop(submitted["job_id"], "stop-1", cancel=True)
        assert repo.settle_stop(submitted["batch_id"]) is False
        repo.release_delivery(
            delivery_id, submitted["batch_id"], state="suspended"
        )
        assert repo.settle_stop(submitted["batch_id"]) is True
        assert repo.get_batch(submitted["batch_id"])["phase"] == "paused"
        assert repo.get_delivery(delivery_id)["state"] == "suspended"

        original_request = repo.get_request(submitted["job_id"])
        resumed = repo.resume(
            submitted["job_id"],
            "resume-1",
            budget=BatchBudget(max_works=2, max_requests=2, max_artists=1),
        )
        assert resumed["batch_id"] != submitted["batch_id"]
        resumed_batch = repo.get_batch(resumed["batch_id"])
        assert resumed_batch["phase"] == "queued"
        assert resumed_batch["budget"]["max_works"] == 2
        assert repo.get_batch(submitted["batch_id"])["phase"] == "paused"
        assert repo.get_request(submitted["job_id"]) == original_request
        assert (
            repo.resume(submitted["job_id"], "resume-duplicate")["batch_id"]
            == resumed["batch_id"]
        )

        activated = repo.activate_next()
        assert activated is not None
        assert activated["batch_id"] == resumed["batch_id"]
        assert delivery_id in repo.list_dispatchable(submitted["job_id"])
        claimed = repo.claim_delivery(delivery_id, resumed["batch_id"])
        assert claimed is not None
        assert claimed["attempts"] == 2
        assert claimed["current_batch_id"] == resumed["batch_id"]

        repo.release_delivery(
            delivery_id, submitted["batch_id"], state="suspended"
        )
        still_new = repo.get_delivery(delivery_id)
        assert still_new["state"] == "transferring"
        assert still_new["current_batch_id"] == resumed["batch_id"]

        stop_cancel = repo.request_stop(
            submitted["job_id"], "stop-2", cancel=True
        )
        assert stop_cancel["phase"] == "cancelling"
        stop_pause_late = repo.request_stop(
            submitted["job_id"], "stop-3", cancel=False
        )
        assert stop_pause_late["phase"] == "cancelling"
        assert repo.settle_stop(resumed["batch_id"]) is False
        repo.release_delivery(
            delivery_id, resumed["batch_id"], state="failed", error_code="forced"
        )
        assert repo.settle_stop(resumed["batch_id"]) is True
        assert repo.get_batch(resumed["batch_id"])["phase"] == "cancelled"
        assert repo.get_delivery(delivery_id)["state"] == "failed"
        with pytest.raises(InvalidStateError):
            repo.resume(submitted["job_id"], "resume-cancelled")


# ---------------------------------------------------------------------------
# Recovery, schema safety, corrupt data and close
# ---------------------------------------------------------------------------


def test_recover_interrupted_marks_blocked_and_preserves_prepared(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    page = _source_page(
        (_candidate("100", (0, 1), page_count=2),), after='"cursor-1"'
    )
    repo = LibraryRepository(db_path)
    try:
        submitted = _submit_and_activate(repo, _request())
        ids = repo.accept_source_page(submitted["batch_id"], page)
        assert len(ids) == 2
        assert repo.claim_delivery(ids[0], submitted["batch_id"]) is not None
    finally:
        repo.close()

    with closing(_raw(db_path)) as raw:
        raw.execute(
            "UPDATE deliveries SET state = 'prepared' WHERE delivery_id = ?",
            (ids[1],),
        )
        raw.commit()

    with LibraryRepository(db_path) as repo:
        recovered = repo.recover_interrupted()
        assert recovered == (submitted["batch_id"],)
        batch = repo.get_batch(submitted["batch_id"])
        assert batch["phase"] == "blocked"
        assert batch["stop_reason"] == "interrupted"
        assert repo.get_delivery(ids[0])["state"] == "suspended"
        assert repo.get_delivery(ids[1])["state"] == "prepared"
        assert repo.recover_interrupted() == ()
        assert repo.snapshot(submitted["job_id"])["phase"] == "blocked"


def test_unknown_schema_is_rejected_without_file_change_or_lock_leak(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "library.sqlite3"
    with closing(sqlite3.connect(db_path)) as raw:
        raw.execute(
            'CREATE TABLE schema_meta ("key" TEXT PRIMARY KEY, "value" TEXT NOT NULL)'
        )
        raw.execute(
            "INSERT INTO schema_meta(\"key\", \"value\") VALUES ('format', 'other-product')"
        )
        raw.execute(
            "INSERT INTO schema_meta(\"key\", \"value\") VALUES ('schema_version', '1')"
        )
        raw.commit()
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    with pytest.raises(SchemaVersionError):
        LibraryRepository(db_path)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before

    lock = InstanceLock(tmp_path / ".writer.lock")
    lock.acquire()
    lock.release()


def test_corrupt_stored_json_raises_instead_of_empty_values(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    page = _source_page((_candidate("100", (0,), page_count=1),))
    repo = LibraryRepository(db_path)
    try:
        submitted = _submit_and_activate(repo, _request())
        delivery_id = repo.accept_source_page(submitted["batch_id"], page)[0]
    finally:
        repo.close()

    with closing(_raw(db_path)) as raw:
        raw.execute(
            "UPDATE jobs SET request_json = '{broken' WHERE job_id = ?",
            (submitted["job_id"],),
        )
        raw.execute(
            "UPDATE batches SET budget_json = '{broken' WHERE batch_id = ?",
            (submitted["batch_id"],),
        )
        raw.execute(
            "UPDATE deliveries SET payload_json = '{broken' WHERE delivery_id = ?",
            (delivery_id,),
        )
        raw.commit()

    with LibraryRepository(db_path) as repo:
        with pytest.raises(CorruptDataError):
            repo.get_request(submitted["job_id"])
        with pytest.raises(CorruptDataError):
            repo.get_batch(submitted["batch_id"])
        with pytest.raises(CorruptDataError):
            repo.get_delivery(delivery_id)


def test_close_waits_and_rejects_afterward_and_releases_lock(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    repo = LibraryRepository(db_path)
    try:
        submitted = repo.submit(_request(), "submit-close")
    finally:
        repo.close()
    with pytest.raises(LibraryClosedError):
        repo.get_request(submitted["job_id"])
    with pytest.raises(LibraryClosedError):
        repo.submit(_request("req-after-close"), "submit-after-close")
    repo.close()

    lock = InstanceLock(tmp_path / ".writer.lock")
    lock.acquire()
    lock.release()


def test_bounded_queue_does_not_drop_accepted_submissions(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    repo = LibraryRepository(db_path, queue_capacity=1)
    try:
        def submit_one(index: int) -> dict[str, str]:
            return repo.submit(_request(f"req-{index}"), f"cmd-{index}")

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(submit_one, range(12)))
        assert len({item["job_id"] for item in results}) == 12
        for item in results:
            assert repo.get_request(item["job_id"]).request_id.startswith("req-")
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# Second-round boundary regressions
# ---------------------------------------------------------------------------


def test_page_receipt_and_source_items_are_entity_scoped(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    page = _source_page((_candidate("100", (0,), page_count=1),))
    with LibraryRepository(db_path) as repo:
        submitted = _submit_and_activate(repo, _request())
        first_ids = repo.accept_source_page(submitted["batch_id"], page)
        second_page = replace(
            page,
            entity_id="artist-2",
            candidates=(_candidate("200", (0,), page_count=1, artist_id="artist-2"),),
        )
        second_ids = repo.accept_source_page(submitted["batch_id"], second_page)
        assert first_ids and second_ids
        assert first_ids != second_ids
        assert repo.get_checkpoint(
            submitted["job_id"], "follow", "pass-1", "artist-1"
        )
        assert repo.get_checkpoint(
            submitted["job_id"], "follow", "pass-1", "artist-2"
        )
        assert repo.accept_source_page(submitted["batch_id"], second_page) == second_ids
        with closing(_raw(db_path)) as raw:
            keys = [
                row[0]
                for row in raw.execute(
                    "SELECT occurrence_key FROM source_items ORDER BY occurrence_key"
                ).fetchall()
            ]
            assert len(keys) == 2 and keys[0] != keys[1]
            assert all("artist-" in key for key in keys)


def test_resume_claim_admits_work_once_and_checks_budget_before_state(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    request = _request(budget=BatchBudget(max_works=2, max_requests=5, max_artists=2))
    page = _source_page(
        (
            _candidate("100", (0, 1), page_count=2),
            _candidate("101", (0,), page_count=1),
        ),
        after='"cursor-1"',
    )
    with LibraryRepository(db_path) as repo:
        submitted = _submit_and_activate(repo, request)
        ids = repo.accept_source_page(submitted["batch_id"], page)
        assert len(ids) == 3
        repo.request_stop(submitted["job_id"], "pause")
        assert repo.settle_stop(submitted["batch_id"])
        resumed = repo.resume(
            submitted["job_id"],
            "resume",
            BatchBudget(max_works=1, max_requests=2, max_artists=1),
        )
        assert repo.activate_next()["batch_id"] == resumed["batch_id"]

        assert repo.claim_delivery(ids[0], resumed["batch_id"]) is not None
        status = repo.budget_status(resumed["batch_id"])
        assert status == {"used_works": 1, "max_works": 1, "deferred_works": 1}
        repo.release_delivery(ids[0], resumed["batch_id"], state="suspended")

        # The second page of the same admitted work does not consume another work slot.
        assert repo.claim_delivery(ids[1], resumed["batch_id"]) is not None
        repo.release_delivery(ids[1], resumed["batch_id"], state="suspended")

        with pytest.raises(BudgetExceeded):
            repo.claim_delivery(ids[2], resumed["batch_id"])
        exhausted = repo.get_delivery(ids[2])
        assert exhausted["state"] == "suspended"
        assert exhausted["attempts"] == 0
        assert repo.budget_status(resumed["batch_id"]) == {
            "used_works": 1,
            "max_works": 1,
            "deferred_works": 1,
        }


def test_cancel_new_batch_cancels_job_pending_scope_without_touching_other_job(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        other = _submit_and_activate(repo, _request("other-job"))
        other_ids = repo.accept_source_page(
            other["batch_id"],
            _source_page(
                (_candidate("200", (0,), page_count=1),),
                after='"cursor-1"',
            ),
        )
        repo.request_stop(other["job_id"], "pause-other")
        assert repo.settle_stop(other["batch_id"])

        submitted = _submit_and_activate(repo, _request("main-job"))
        ids = repo.accept_source_page(
            submitted["batch_id"],
            _source_page(
                (
                    _candidate("100", (0,), page_count=1),
                    _candidate("101", (0,), page_count=1),
                ),
                after='"cursor-1"',
            ),
        )
        assert len(ids) == 2
        repo.request_stop(submitted["job_id"], "pause-main")
        assert repo.settle_stop(submitted["batch_id"])
        resumed = repo.resume(submitted["job_id"], "resume-main")
        assert repo.activate_next()["batch_id"] == resumed["batch_id"]

        repo.request_stop(submitted["job_id"], "cancel-main", cancel=True)
        assert repo.settle_stop(resumed["batch_id"]) is True
        assert all(repo.get_delivery(item)["state"] == "cancelled" for item in ids)
        assert repo.get_batch(submitted["batch_id"])["phase"] == "paused"
        assert repo.list_dispatchable(submitted["job_id"]) == ()
        assert repo.get_delivery(other_ids[0])["state"] == "suspended"
        assert repo.list_dispatchable(other["job_id"]) == tuple(other_ids)
        assert repo.get_batch(other["batch_id"])["phase"] == "paused"


def test_pause_blocks_on_prepared_left_by_an_old_batch(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    page = _source_page(
        (_candidate("100", (0, 1), page_count=2),),
        after='"cursor-1"',
    )
    with LibraryRepository(db_path) as repo:
        submitted = _submit_and_activate(repo, _request())
        ids = repo.accept_source_page(submitted["batch_id"], page)
        repo.request_stop(submitted["job_id"], "pause-old")
        assert repo.settle_stop(submitted["batch_id"])
        with closing(_raw(db_path)) as raw:
            raw.execute(
                "UPDATE deliveries SET state='prepared' WHERE delivery_id = ?",
                (ids[1],),
            )
            raw.commit()

        resumed = repo.resume(submitted["job_id"], "resume-new")
        assert repo.activate_next()["batch_id"] == resumed["batch_id"]
        repo.request_stop(submitted["job_id"], "pause-new")
        assert repo.settle_stop(resumed["batch_id"]) is True
        assert repo.get_batch(resumed["batch_id"])["phase"] == "blocked"
        assert repo.get_delivery(ids[1])["state"] == "prepared"
        assert repo.get_delivery(ids[0])["state"] == "suspended"


def test_dispatch_sql_filters_future_retries_before_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    request = _request(budget=BatchBudget(max_works=201, max_requests=201, max_artists=1))
    candidates = tuple(_candidate(str(1000 + index), (0,), page_count=1) for index in range(201))
    with LibraryRepository(db_path) as repo:
        submitted = _submit_and_activate(repo, request)
        repo.accept_source_page(
            submitted["batch_id"],
            _source_page(candidates, after='"cursor-1"'),
        )
        with closing(_raw(db_path)) as raw:
            ids = [
                row[0]
                for row in raw.execute(
                    "SELECT delivery_id FROM deliveries "
                    "ORDER BY created_at, delivery_id"
                ).fetchall()
            ]
            raw.executemany(
                "UPDATE deliveries SET state='retry_wait', "
                "retry_at='2099-01-01T00:00:00+00:00' WHERE delivery_id = ?",
                [(item,) for item in ids[:200]],
            )
            raw.commit()
        assert repo.list_dispatchable(submitted["job_id"], limit=1) == (ids[-1],)


def test_dispatch_excludes_before_limit_and_budget_gate(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    request = _request(budget=BatchBudget(max_works=2, max_requests=5, max_artists=2))
    page = _source_page(
        (
            _candidate("100", (0, 1), page_count=2),
            _candidate("101", (0,), page_count=1),
        ),
        after='"cursor-1"',
    )
    with LibraryRepository(db_path) as repo:
        submitted = _submit_and_activate(repo, request)
        ids = repo.accept_source_page(submitted["batch_id"], page)
        assert len(ids) == 3
        repo.request_stop(submitted["job_id"], "pause-old")
        assert repo.settle_stop(submitted["batch_id"])
        assert repo.list_dispatchable(submitted["job_id"]) == tuple(ids)

        resumed = repo.resume(
            submitted["job_id"],
            "resume",
            BatchBudget(max_works=1, max_requests=2, max_artists=1),
        )
        assert repo.activate_next()["batch_id"] == resumed["batch_id"]
        assert repo.claim_delivery(ids[0], resumed["batch_id"]) is not None
        repo.release_delivery(ids[0], resumed["batch_id"], state="suspended")

        # The current batch is full; only its admitted work remains dispatchable.
        assert repo.list_dispatchable(
            submitted["job_id"], limit=1, exclude_ids=(ids[0],)
        ) == (ids[1],)
        assert ids[2] not in repo.list_dispatchable(submitted["job_id"], limit=200)
        assert repo.budget_status(resumed["batch_id"]) == {
            "used_works": 1,
            "max_works": 1,
            "deferred_works": 1,
        }
        with pytest.raises(LibraryError):
            repo.list_dispatchable(
                submitted["job_id"],
                limit=1,
                exclude_ids=tuple("id" for _ in range(201)),
            )

        repo.request_stop(submitted["job_id"], "pause-new")
        assert repo.settle_stop(resumed["batch_id"])
        # No active running batch: all persistent work is visible for recovery.
        assert repo.list_dispatchable(submitted["job_id"]) == tuple(ids)


def test_request_stop_persists_safe_reason_and_handles_settling(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        submitted = _submit_and_activate(repo, _request("reason-job"))
        first = repo.request_stop(
            submitted["job_id"], "budget-stop", reason_code="work_budget"
        )
        assert first["phase"] == "pausing"
        assert repo.get_batch(submitted["batch_id"])["stop_reason"] == "work_budget"
        assert repo.request_stop(
            submitted["job_id"], "budget-stop", reason_code="work_budget"
        ) == first
        with pytest.raises(CommandConflict):
            repo.request_stop(
                submitted["job_id"], "budget-stop", reason_code="closing"
            )
        assert repo.settle_stop(submitted["batch_id"])
        assert repo.get_batch(submitted["batch_id"])["stop_reason"] == "work_budget"

        resumed = repo.resume(
            submitted["job_id"],
            "resume-settling",
            BatchBudget(max_works=1, max_requests=2, max_artists=1),
        )
        assert repo.activate_next()["batch_id"] == resumed["batch_id"]
        with closing(_raw(db_path)) as raw:
            raw.execute(
                "UPDATE batches SET phase='settling' WHERE batch_id = ?",
                (resumed["batch_id"],),
            )
            raw.execute(
                "UPDATE jobs SET state='settling' WHERE job_id = ?",
                (submitted["job_id"],),
            )
            raw.commit()

        stopped = repo.request_stop(
            submitted["job_id"], "closing-stop", cancel=True, reason_code="closing"
        )
        assert stopped["phase"] == "cancelling"
        assert repo.get_batch(resumed["batch_id"])["stop_reason"] == "closing"
        assert repo.settle_stop(resumed["batch_id"]) is True
        assert repo.get_batch(resumed["batch_id"])["phase"] == "cancelled"
        assert repo.get_batch(resumed["batch_id"])["stop_reason"] == "closing"

        default_job = repo.submit(_request("default-reason-job"), "default-submit")
        repo.activate_next()
        assert repo.request_stop(
            default_job["job_id"], "default-stop"
        )["phase"] == "pausing"
        assert repo.get_batch(default_job["batch_id"])["stop_reason"] == "user_pause"
        assert repo.settle_stop(default_job["batch_id"])


def test_close_rejects_waiting_producers_and_drains_admitted_commands(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "library.sqlite3"
    repo = LibraryRepository(db_path, queue_capacity=1)
    blocker = sqlite3.connect(str(db_path), timeout=10)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        first_result: list[object] = []
        second_result: list[object] = []
        third_result: list[object] = []
        close_result: list[object] = []

        def run_submit(request_id: str, command_id: str, target: list[object]) -> None:
            try:
                target.append(repo.submit(_request(request_id), command_id))
            except BaseException as exc:
                target.append(exc)

        first = threading.Thread(
            target=run_submit, args=("req-blocked", "cmd-blocked", first_result)
        )
        first.start()
        time.sleep(0.5)
        second = threading.Thread(
            target=run_submit, args=("req-queued", "cmd-queued", second_result)
        )
        second.start()
        time.sleep(0.3)
        third = threading.Thread(
            target=run_submit, args=("req-waiting", "cmd-waiting", third_result)
        )
        third.start()
        time.sleep(0.3)

        def run_close() -> None:
            try:
                repo.close(timeout=10)
                close_result.append("closed")
            except BaseException as exc:
                close_result.append(exc)

        closer = threading.Thread(target=run_close)
        closer.start()
        time.sleep(0.3)
        blocker.rollback()
        for thread in (first, second, third, closer):
            thread.join(10)
        assert close_result == ["closed"]
        assert first_result
        assert second_result
        assert third_result
        assert any(
            isinstance(item[0], dict)
            for item in (first_result, second_result, third_result)
        )
        assert any(
            isinstance(item[0], LibraryClosedError)
            for item in (second_result, third_result)
        )
    finally:
        try:
            blocker.rollback()
        finally:
            blocker.close()
        repo.close()


def test_ddl_allows_partial_batches_and_historical_null_artifact_fields(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "library.sqlite3"
    page = _source_page((_candidate("100", (0,), page_count=1),))
    with LibraryRepository(db_path) as repo:
        submitted = _submit_and_activate(repo, _request())
        delivery_id = repo.accept_source_page(submitted["batch_id"], page)[0]
        delivery = repo.get_delivery(delivery_id)
        with closing(_raw(db_path)) as raw:
            raw.execute(
                "INSERT INTO batches("
                "batch_id, job_id, command_id, budget_json, phase, stop_reason, "
                "active_slot, created_at, started_at, ended_at"
                ") VALUES (?, ?, ?, ?, 'partial', NULL, NULL, ?, NULL, NULL)",
                (
                    "partial-batch",
                    submitted["job_id"],
                    "partial-command",
                    '{"max_works":1,"max_requests":1,"max_artists":1,"max_seconds":null}',
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            raw.execute(
                "INSERT INTO artifacts("
                "artifact_id, delivery_id, job_id, illust_id, page_index, artist_id, "
                "output_key, output_ref_json, quality_json, media_type, size, sha256, "
                "verification, completed_at, legacy_origin, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
                (
                    "legacy-artifact",
                    delivery_id,
                    delivery["job_id"],
                    delivery["illust_id"],
                    delivery["page_index"],
                    None,
                    delivery["output_key"],
                    '{"kind":"project","root_id":"project","relative_path":"downl_Pixiv/100_p0.jpg"}',
                    '{"original":true,"all_pages":true,"validation_policy_version":1}',
                    None,
                    "legacy_unknown",
                    "2026-01-01T00:00:00+00:00",
                    "legacy",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            raw.commit()
        assert repo.get_batch("partial-batch")["phase"] == "partial"


def test_total_admission_waiters_are_woken_by_close(tmp_path: Path, monkeypatch) -> None:
    import core.library_repository as repository_module

    monkeypatch.setattr(repository_module, "_MAX_COMMAND_BYTES", 1_000_000)
    monkeypatch.setattr(repository_module, "_MAX_PENDING_BYTES", 1)
    db_path = tmp_path / "library.sqlite3"
    repo = LibraryRepository(db_path)
    waiter_result: list[object] = []

    def submit_waiter() -> None:
        try:
            waiter_result.append(repo.submit(_request("too-large-for-total"), "cmd-total"))
        except BaseException as exc:
            waiter_result.append(exc)

    waiter = threading.Thread(target=submit_waiter)
    try:
        waiter.start()
        time.sleep(0.25)
        assert waiter.is_alive()
        repo.close(timeout=5)
        waiter.join(5)
        assert waiter_result and isinstance(waiter_result[0], LibraryClosedError)
    finally:
        repo.close()
        waiter.join(5)


def test_per_command_bound_rejects_without_enqueueing(tmp_path: Path, monkeypatch) -> None:
    import core.library_repository as repository_module

    monkeypatch.setattr(repository_module, "_MAX_COMMAND_BYTES", 64)
    repo = LibraryRepository(tmp_path / "library.sqlite3")
    try:
        with pytest.raises(LibraryError):
            repo.submit(_request("oversized-command"), "cmd-oversized")
    finally:
        repo.close()


def test_database_path_rejects_unc_and_linked_locations(tmp_path: Path) -> None:
    with pytest.raises(LibraryError):
        LibraryRepository(r"\\server\share\library.sqlite3")

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("Directory symlinks are not available in this environment")
        import _winapi
        _winapi.CreateJunction(str(real), str(link))
        assert link.is_junction()
    with pytest.raises(LibraryError):
        LibraryRepository(link / "library.sqlite3")
