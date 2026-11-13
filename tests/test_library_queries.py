#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline P4 tests for the read-only manifest queries and Markdown export.

Only temporary SQLite ledgers/files and synthetic contracts are used.  No HTTP,
GUI, production database or image file outside ``tmp_path`` is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from datetime import datetime

import pytest

from core.app_paths import AppPaths
from core.library_paths import PathResolver
from core.library_queries import (
    ExportResult,
    LibraryQueries,
    LibraryQuery,
)
from core.library_repository import (
    CorruptDataError,
    LibraryClosedError,
    LibraryError,
    LibraryRepository,
)
from core.task_contracts import (
    BatchBudget,
    CandidateDecision,
    CollectionRequest,
    ContractError,
    OutputSpec,
    PageSpec,
    PathRef,
    QualitySpec,
    SourcePage,
)

OUTPUT_ROOT = "downl_Pixiv"
EXPORT_REF = PathRef(relative_path="exports/list.md")


# ---------------------------------------------------------------------------
# Synthetic ledger helpers
# ---------------------------------------------------------------------------


def _request(request_id="req-1", *, budget=None, all_pages=True):
    return CollectionRequest(
        request_id,
        "account-test",
        output=OutputSpec(root_ref=PathRef(relative_path=OUTPUT_ROOT)),
        quality=QualitySpec(original=True, all_pages=all_pages),
        budget=budget
        or BatchBudget(max_works=300, max_requests=400, max_artists=300),
        dedup_policy="trusted",
    )


def _candidate(
    illust_id,
    pages=(0,),
    *,
    page_count=None,
    decision="download",
    title="Title",
    artist_id="7001",
    artist_name="Artist",
):
    specs = tuple(
        PageSpec(
            index,
            PathRef(relative_path=f"{OUTPUT_ROOT}/{illust_id}_p{index}.png"),
            f"https://i.pximg.net/img-original/{illust_id}_p{index}.png",
        )
        for index in pages
    )
    if decision == "download" and page_count is None:
        page_count = max(pages) + 1
    return CandidateDecision(
        illust_id,
        decision,
        artist_id=artist_id,
        artist_name=artist_name,
        title=title,
        page_count=page_count,
        pages=specs,
    )


def _source(
    candidates,
    *,
    page_identity="page-1",
    before="null",
    after='"cursor-1"',
    entity_id="artist-1",
):
    return SourcePage(
        "follow",
        "history",
        entity_id,
        page_identity,
        tuple(candidates),
        cursor_before_json=before,
        cursor_after_json=after,
        exhausted=True,
        actual_sort="date_desc",
        coverage_json='{"seen":1}',
    )


def _open(repo, candidates, *, request=None, page_identity="page-1",
          before="null", after='"cursor-1"'):
    request = request or _request()
    job = repo.submit(request, "submit-" + request.request_id)
    activated = repo.activate_next()
    assert activated is not None and activated["batch_id"] == job["batch_id"]
    ids = repo.accept_source_page(
        job["batch_id"],
        _source(
            candidates,
            page_identity=page_identity,
            before=before,
            after=after,
        ),
    )
    return job, ids


def _claim(repo, delivery_id, batch_id):
    row = repo.claim_delivery(delivery_id, batch_id)
    assert row is not None
    return row


def _complete(repo, delivery_id, batch_id, *, size=1000, prefix="b" * 64):
    delivery = repo.get_delivery(delivery_id)
    assert repo.start_transfer_attempt(delivery_id, batch_id) == 1
    checkpoint = {
        "schema_version": 1,
        "delivery_id": delivery_id,
        "temp_ref": {
            "kind": "project",
            "root_id": "project",
            "relative_path": f"{OUTPUT_ROOT}/.{delivery_id}.part",
        },
        "file_id": {"device": 1, "inode": 42},
        "source_identity": "a" * 64,
        "strong_etag": '"etag-1"',
        "expected_total": size,
        "verified_bytes": size,
        "prefix_sha256": prefix,
        "media_type": "image/png",
        "validation_policy_version": 1,
    }
    repo.save_transfer_checkpoint(delivery_id, batch_id, checkpoint)
    repo.begin_validation(delivery_id, batch_id)
    evidence = {
        "schema_version": 1,
        "delivery_id": delivery_id,
        "temp_ref": checkpoint["temp_ref"],
        "final_ref": delivery["output_ref"],
        "file_id": checkpoint["file_id"],
        "source_identity": "a" * 64,
        "size": size,
        "sha256": prefix,
        "media_type": "image/png",
        "verification": "structure_checked",
        "validation_policy_version": 1,
        "width": 8,
        "height": 8,
        "frames": 1,
    }
    repo.prepare_delivery(delivery_id, batch_id, evidence)
    return repo.confirm_delivery(delivery_id)


def _finish_failed(repo, delivery_id, batch_id):
    _claim(repo, delivery_id, batch_id)
    assert repo.start_transfer_attempt(delivery_id, batch_id) == 1
    repo.release_delivery(
        delivery_id, batch_id, state="failed", error_code="http_transient"
    )


def _sql(db_path, statement, params=()):
    with closing(sqlite3.connect(str(db_path), timeout=10)) as raw:
        raw.execute(statement, params)
        raw.commit()


def _scalar(db_path, statement, params=()):
    with closing(sqlite3.connect(str(db_path), timeout=10)) as raw:
        return raw.execute(statement, params).fetchone()[0]


def _resolver(tmp_path):
    return PathResolver(AppPaths(tmp_path))


@pytest.fixture
def ledger(tmp_path):
    database = tmp_path / "library.sqlite3"
    with LibraryRepository(database) as repo:
        yield repo, database


def _build_manifest(repo):
    """Two download works (3 partly done + 1 done) and four non-downloads."""
    job, ids = _open(
        repo,
        (
            _candidate(
                "100",
                (0, 1, 2),
                page_count=3,
                title="Alpha 100%",
                artist_id="7001",
                artist_name="Alpha Artist",
            ),
            _candidate(
                "101",
                (0,),
                title="Beta_under",
                artist_id="7002",
                artist_name="Beta Artist",
            ),
            _candidate("102", (), decision="filtered", page_count=None, title="Gamma", artist_id="7003"),
            _candidate("103", (), decision="metadata_unknown", page_count=None, title="Delta", artist_id="7003"),
            _candidate("104", (), decision="unavailable", page_count=None, title="Epsilon", artist_id="7003"),
            _candidate("105", (), decision="unsupported", page_count=None, title="Zeta", artist_id="7003"),
        ),
    )
    batch_id = job["batch_id"]
    _claim(repo, ids[0], batch_id)
    _complete(repo, ids[0], batch_id)
    _claim(repo, ids[1], batch_id)
    _complete(repo, ids[1], batch_id)
    _finish_failed(repo, ids[2], batch_id)
    _claim(repo, ids[3], batch_id)
    _complete(repo, ids[3], batch_id)
    return job, ids


def _batch_rows(queries, batch_id):
    page = queries.fetch(LibraryQuery(kind="batch", batch_id=batch_id), page_size=200)
    assert page.next_cursor is None
    return {row["illust_id"]: row for row in page.rows}


# ---------------------------------------------------------------------------
# Query description validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"kind": "nope"}, "kind"),
        ({"kind": "batch"}, "batch id"),
        ({"kind": "pages", "batch_id": "b"}, "illust id"),
        ({"kind": "pages", "batch_id": "b", "illust_id": "1", "artist_id": "2"}, "artist"),
        ({"kind": "pages", "batch_id": "b", "illust_id": "1", "text": "x"}, "text"),
        ({"kind": "history", "batch_id": "b"}, "batch id"),
        ({"kind": "history", "states": ("failed",)}, "state filter"),
        ({"kind": "batch", "batch_id": "b", "states": ("bogus",)}, "state filter"),
        ({"kind": "batch", "batch_id": ""}, "batch id"),
        ({"kind": "batch", "batch_id": "b", "illust_id": "x1"}, "illust id"),
        ({"kind": "batch", "batch_id": "b", "artist_id": "abc"}, "artist id"),
        ({"kind": "batch", "batch_id": "b", "text": 5}, "text"),
        ({"kind": "batch", "batch_id": "b", "text": "x" * 2000}, "text"),
        ({"kind": "batch", "batch_id": "b", "text": "a\nb"}, "control"),
        ({"kind": "batch", "batch_id": "b", "after": "2026-01-01T00:00:00"}, "UTC"),
        ({"kind": "batch", "batch_id": "b", "after": "2026-01-01T00:00:00+08:00"}, "UTC"),
        ({"kind": "batch", "batch_id": "b", "after": "not-a-time"}, "after"),
        (
            {
                "kind": "batch",
                "batch_id": "b",
                "after": "2026-02-01T00:00:00+00:00",
                "before": "2026-01-01T00:00:00+00:00",
            },
            "after",
        ),
        ({"kind": "batch", "batch_id": "b", "states": "completed"}, "states"),
        ({"kind": "batch", "batch_id": "b", "states": None}, "states"),
    ],
)
def test_query_rejects_invalid_or_inapplicable_fields(kwargs, message):
    with pytest.raises(LibraryError, match=message):
        LibraryQuery(**kwargs)


def test_query_normalizes_equivalent_inputs_without_silent_ignoring():
    query = LibraryQuery(kind="history", after="2026-01-01T00:00:00Z")
    assert query.after == "2026-01-01T00:00:00.000000+00:00"
    query = LibraryQuery(kind="batch", batch_id="b", states=("partial", "failed", "partial"))
    assert query.states == ("failed", "partial")
    assert LibraryQuery(kind="history", states=("completed",)).states == ("completed",)
    assert LibraryQuery(kind="pages", batch_id="b", illust_id=123).illust_id == "123"
    assert LibraryQuery(kind="batch", batch_id="b", illust_id="100", artist_id=7001)
    assert isinstance(query, LibraryQuery) and query.kind == "batch"


def test_queries_constructor_rejects_unsupported_setup(ledger):
    repo, _database = ledger
    with pytest.raises(LibraryError):
        LibraryQueries(object())
    with pytest.raises(LibraryError):
        LibraryQueries(repo, clock=5)
    for limit in (0, -1, True, "300", 10**9):
        with pytest.raises(LibraryError):
            LibraryQueries(repo, export_time_limit=limit)


@pytest.mark.parametrize("page_size", (0, 1, 51, 99, 100.0, True, "100", 201))
def test_fetch_rejects_unsupported_page_sizes(ledger, page_size):
    repo, _database = ledger
    job, _ids = _open(repo, (_candidate("100"),))
    queries = LibraryQueries(repo)
    with pytest.raises(LibraryError):
        queries.fetch(LibraryQuery(kind="batch", batch_id=job["batch_id"]), page_size=page_size)


# ---------------------------------------------------------------------------
# Batch / history / pages separation and attribution
# ---------------------------------------------------------------------------


def test_batch_rows_count_frozen_pages_and_keep_filtered_conclusions(ledger):
    repo, _database = ledger
    job, _ids = _build_manifest(repo)
    rows = _batch_rows(LibraryQueries(repo), job["batch_id"])

    assert set(rows) == {"100", "101", "102", "103", "104", "105"}
    work = rows["100"]
    assert work["kind"] == "batch"
    assert work["decision"] == "download"
    assert work["state"] == "partial"
    assert work["result_state"] == "partial"
    assert work["required"] == 3
    assert work["completed"] == 2
    assert work["new_completed"] == 2
    assert work["existing"] == 0
    assert work["failed"] == 1
    assert work["needs_verification"] == 0
    assert work["pending"] == 0
    assert work["cancelled"] == 0
    assert work["title"] == "Alpha 100%"
    assert work["artist_id"] == "7001"
    assert work["artist_name"] == "Alpha Artist"

    done = rows["101"]
    assert done["required"] == 1
    assert done["completed"] == 1
    assert done["new_completed"] == 1
    assert done["state"] == "completed"

    for illust_id, decision in (
        ("102", "filtered"),
        ("103", "metadata_unknown"),
        ("104", "unavailable"),
        ("105", "unsupported"),
    ):
        row = rows[illust_id]
        assert row["decision"] == decision
        assert row["state"] == decision
        assert row["required"] == 0
        assert row["completed"] == 0


def test_history_contains_only_artifact_pages(ledger):
    repo, _database = ledger
    job, ids = _build_manifest(repo)
    queries = LibraryQueries(repo)
    page = queries.fetch(LibraryQuery(kind="history"), page_size=200)
    assert page.next_cursor is None
    assert len(page.rows) == 3
    assert {row["illust_id"] for row in page.rows} == {"100", "101"}
    assert {row["page_index"] for row in page.rows if row["illust_id"] == "100"} == {0, 1}
    assert all(row["verification"] == "structure_checked" for row in page.rows)
    assert all(row["completed_batch_id"] == job["batch_id"] for row in page.rows)
    assert all(row["historical"] is False for row in page.rows)
    assert all(row["legacy"] is False for row in page.rows)
    # The failed page never becomes history.
    failed_artifact = repo.get_artifact(ids[2])
    assert failed_artifact is None


def test_rows_expose_only_safe_fields_without_urls_or_payloads(ledger):
    repo, _database = ledger
    job, ids = _build_manifest(repo)
    queries = LibraryQueries(repo)
    batch = queries.fetch(LibraryQuery(kind="batch", batch_id=job["batch_id"])).rows
    history = queries.fetch(LibraryQuery(kind="history")).rows
    pages = queries.fetch(
        LibraryQuery(kind="pages", batch_id=job["batch_id"], illust_id="100")
    ).rows

    assert set(batch[0]) == {
        "kind", "batch_id", "illust_id", "artist_id", "artist_name", "title",
        "page_count", "decision", "state", "result_state", "required",
        "completed", "new_completed", "existing", "failed",
        "needs_verification", "cancelled", "pending", "reason_codes",
        "bookmarks", "ai_flag", "created_at", "updated_at",
    }
    assert set(history[0]) == {
        "kind", "artifact_id", "delivery_id", "job_id", "illust_id", "page_index",
        "title", "artist_id", "artist_name", "output_key", "output_ref",
        "quality", "media_type", "size", "sha256", "verification",
        "completed_at", "legacy_origin", "legacy", "historical",
        "completed_batch_id", "origin_batch_id",
    }
    assert set(pages[0]) == {
        "kind", "batch_id", "illust_id", "page_index", "delivery_id",
        "artifact_id", "status", "delivery_state", "error_code", "output_key",
        "output_ref", "quality", "verification", "size", "sha256",
        "media_type", "completed_at", "legacy_origin", "completed_batch_id",
    }
    serialized = json.dumps([batch, history, pages], ensure_ascii=False).lower()
    for forbidden in ("http://", "https://", "pximg", "cookie", "token", "payload", "authorization"):
        assert forbidden not in serialized
    assert all(set(row["quality"]) == {"original", "all_pages", "validation_policy_version"}
               for row in history + pages)
    for row in pages:
        assert row["output_ref"]["relative_path"].startswith(OUTPUT_ROOT + "/")


def test_pages_view_reports_status_from_valid_artifact(ledger):
    repo, _database = ledger
    job, ids = _build_manifest(repo)
    queries = LibraryQueries(repo)
    page = queries.fetch(
        LibraryQuery(kind="pages", batch_id=job["batch_id"], illust_id="100"),
        page_size=50,
    )
    assert page.next_cursor is None
    assert [row["page_index"] for row in page.rows] == [0, 1, 2]
    assert [row["status"] for row in page.rows] == ["completed", "completed", "failed"]
    failed = page.rows[2]
    assert failed["error_code"] == "http_transient"
    assert failed["delivery_state"] == "failed"
    assert failed["sha256"] is None and failed["completed_at"] is None
    completed = page.rows[0]
    assert completed["delivery_id"] == ids[0]
    assert completed["artifact_id"] is not None
    assert completed["verification"] == "structure_checked"
    assert len(completed["sha256"]) == 64
    assert completed["completed_batch_id"] == job["batch_id"]
    failed_only = queries.fetch(
        LibraryQuery(
            kind="pages", batch_id=job["batch_id"], illust_id="100", states=("failed",)
        )
    ).rows
    assert len(failed_only) == 1 and failed_only[0]["page_index"] == 2


def test_text_filter_is_literal_and_never_sql_syntax(ledger):
    repo, _database = ledger
    job, _ids = _build_manifest(repo)
    queries = LibraryQueries(repo)

    def matches(text):
        return {
            row["illust_id"]
            for row in queries.fetch(
                LibraryQuery(kind="batch", batch_id=job["batch_id"], text=text)
            ).rows
        }

    assert matches("100%") == {"100"}
    assert matches("%") == {"100"}
    assert matches("Beta_under") == {"101"}
    assert matches("x_under") == set()
    assert matches("_") == {"101"}
    assert matches("Alpha") == {"100"}
    assert matches("Beta Artist") == {"101"}
    assert matches("' OR 1=1 --") == set()
    assert matches("%' OR 1=1 --") == set()
    assert matches("100\\%") == set()
    assert matches("artist") == {"100", "101", "102", "103", "104", "105"}


def test_states_artist_illust_and_time_filters(ledger):
    repo, _database = ledger
    job, _ids = _build_manifest(repo)
    batch_id = job["batch_id"]
    queries = LibraryQueries(repo)

    partial = queries.fetch(
        LibraryQuery(kind="batch", batch_id=batch_id, states=("partial",))
    ).rows
    assert [row["illust_id"] for row in partial] == ["100"]
    stopped = queries.fetch(
        LibraryQuery(
            kind="batch", batch_id=batch_id, states=("filtered", "metadata_unknown")
        )
    ).rows
    assert {row["illust_id"] for row in stopped} == {"102", "103"}
    assert [
        row["illust_id"]
        for row in queries.fetch(
            LibraryQuery(kind="batch", batch_id=batch_id, artist_id="7001")
        ).rows
    ] == ["100"]
    assert [
        row["illust_id"]
        for row in queries.fetch(
            LibraryQuery(kind="batch", batch_id=batch_id, illust_id="101")
        ).rows
    ] == ["101"]

    created_at = _scalar(
        _database,
        "SELECT created_at FROM batch_items WHERE batch_id = ? AND illust_id = '100'",
        (batch_id,),
    )
    inclusive = queries.fetch(
        LibraryQuery(kind="batch", batch_id=batch_id, after=created_at)
    ).rows
    assert len(inclusive) == 6
    exclusive = queries.fetch(
        LibraryQuery(kind="batch", batch_id=batch_id, before=created_at)
    ).rows
    assert exclusive == ()


def test_multi_batch_existing_and_new_attribution(ledger):
    repo, _database = ledger
    job, ids = _open(repo, (_candidate("300", (0, 1, 2), page_count=3),))
    first_batch = job["batch_id"]
    payload = b"x" * 1000
    digest = hashlib.sha256(payload).hexdigest()
    _claim(repo, ids[0], first_batch)
    _complete(repo, ids[0], first_batch, size=len(payload), prefix=digest)
    _claim(repo, ids[1], first_batch)
    _complete(repo, ids[1], first_batch, size=len(payload), prefix=digest)
    for index in (0, 1):
        target = _database.parent / OUTPUT_ROOT / f"300_p{index}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    repo.bind_path_resolver(PathResolver(AppPaths(_database.parent)))
    _finish_failed(repo, ids[2], first_batch)

    repo.request_stop(job["job_id"], "pause-p4")
    assert repo.settle_stop(first_batch) is True
    resumed = repo.resume(job["job_id"], "resume-p4", retry_failed=True)
    second_batch = resumed["batch_id"]
    assert repo.activate_next()["batch_id"] == second_batch
    assert repo.claim_delivery(ids[2], second_batch) is not None

    queries = LibraryQueries(repo)
    rows = _batch_rows(queries, second_batch)
    work = rows["300"]
    assert work["required"] == 3
    assert work["completed"] == 2
    assert work["existing"] == 2
    assert work["new_completed"] == 0
    assert work["pending"] == 1
    assert work["failed"] == 0
    assert work["state"] == "partial"

    _complete(repo, ids[2], second_batch)
    work = _batch_rows(queries, second_batch)["300"]
    assert work["required"] == 3
    assert work["completed"] == 3
    assert work["new_completed"] == 1
    assert work["existing"] == 2
    assert work["pending"] == 0
    assert work["state"] == "completed"

    historical = _batch_rows(queries, first_batch)["300"]
    assert historical["completed"] == 3
    assert historical["new_completed"] == 2
    assert historical["existing"] == 1
    assert historical["state"] == "completed"

    history = queries.fetch(LibraryQuery(kind="history"), page_size=200).rows
    attribution = {
        row["page_index"]: row["completed_batch_id"] for row in history
    }
    assert attribution == {0: first_batch, 1: first_batch, 2: second_batch}


def test_history_time_boundaries_are_inclusive_after_exclusive_before(ledger):
    repo, database = ledger
    job, ids = _open(
        repo, (_candidate("400", (0, 1, 2), page_count=3),)
    )
    for delivery_id in ids:
        _claim(repo, delivery_id, job["batch_id"])
        _complete(repo, delivery_id, job["batch_id"])
    times = (
        "2026-01-01T00:00:00.000000+00:00",
        "2026-01-02T00:00:00.000000+00:00",
        "2026-01-03T00:00:00.000000+00:00",
    )
    for delivery_id, completed_at in zip(ids, times):
        _sql(
            database,
            "UPDATE artifacts SET completed_at = ? WHERE delivery_id = ?",
            (completed_at, delivery_id),
        )
    queries = LibraryQueries(repo)

    def pages_with(**kwargs):
        rows = queries.fetch(
            LibraryQuery(kind="history", **kwargs), page_size=200
        ).rows
        return [row["completed_at"] for row in rows]

    assert pages_with(after=times[1]) == [times[2], times[1]]
    assert pages_with(before=times[1]) == [times[0]]
    assert pages_with(after=times[0], before=times[2]) == [times[1], times[0]]
    assert pages_with(after="2026-01-02T00:00:00Z") == [times[2], times[1]]
    assert pages_with(before="2026-01-02T00:00:00Z") == [times[0]]


def test_legacy_artifact_is_history_source_not_invented(ledger):
    repo, database = ledger
    job, ids = _open(repo, (_candidate("600"),))
    _claim(repo, ids[0], job["batch_id"])
    artifact = _complete(repo, ids[0], job["batch_id"])
    _sql(
        database,
        "UPDATE artifacts SET verification = 'legacy_unknown', size = NULL, "
        "legacy_origin = 'legacy_v2', completed_at = '2020-01-01T00:00:00+00:00' "
        "WHERE artifact_id = ?",
        (artifact["artifact_id"],),
    )
    _sql(
        database,
        "UPDATE deliveries SET completed_batch_id = NULL WHERE delivery_id = ?",
        (ids[0],),
    )
    queries = LibraryQueries(repo)
    rows = queries.fetch(LibraryQuery(kind="history")).rows
    assert len(rows) == 1
    row = rows[0]
    assert row["legacy"] is True
    assert row["legacy_origin"] == "legacy_v2"
    assert row["historical"] is True
    assert row["completed_batch_id"] is None
    assert row["verification"] == "legacy_unknown"
    assert row["size"] is None
    assert row["completed_at"] == "2020-01-01T00:00:00+00:00"
    assert row["title"] == "Title"
    assert row["artist_id"] == "7001"

    # A time filter never guesses an unprovable legacy timestamp into range.
    assert queries.fetch(
        LibraryQuery(kind="history", after="2021-01-01T00:00:00+00:00")
    ).rows == ()
    # The batch view never counts an unusable artifact as success, and the
    # visible result is derived from the frozen page links, not the stale column.
    batch_rows = _batch_rows(queries, job["batch_id"])
    assert batch_rows["600"]["completed"] == 0
    assert batch_rows["600"]["pending"] == 1
    assert batch_rows["600"]["state"] == "pending"
    assert batch_rows["600"]["result_state"] == "completed"


def test_unknown_identifiers_return_empty_pages(ledger):
    repo, _database = ledger
    queries = LibraryQueries(repo)
    page = queries.fetch(LibraryQuery(kind="batch", batch_id="unknown-batch"))
    assert page.rows == () and page.next_cursor is None
    assert isinstance(page.watermark, int)
    assert queries.fetch(
        LibraryQuery(kind="pages", batch_id="unknown-batch", illust_id="1")
    ).rows == ()
    assert queries.fetch(
        LibraryQuery(kind="history", illust_id="999")
    ).rows == ()


# ---------------------------------------------------------------------------
# Bounded keyset pagination and query isolation
# ---------------------------------------------------------------------------


def _pagination_fixture(repo):
    illust_ids = [str(1000 + index) for index in range(120)]
    job, _ids = _open(repo, tuple(_candidate(value) for value in illust_ids))
    return job["batch_id"], illust_ids


def _collect(queries, query, page_size):
    collected = []
    page = queries.fetch(query, page_size=page_size)
    assert len(page.rows) <= page_size
    collected.extend(page.rows)
    while page.next_cursor is not None:
        page = queries.fetch(query, cursor=page.next_cursor, page_size=page_size)
        assert len(page.rows) <= page_size
        collected.extend(page.rows)
    return collected


def test_keyset_pagination_is_bounded_and_duplicate_free(ledger):
    repo, _database = ledger
    batch_id, illust_ids = _pagination_fixture(repo)
    queries = LibraryQueries(repo)
    query = LibraryQuery(kind="batch", batch_id=batch_id)

    for page_size, expected_pages in ((50, [50, 50, 20]), (100, [100, 20]), (200, [120])):
        collected = _collect(queries, query, page_size)
        assert len(collected) == 120
        assert len({row["illust_id"] for row in collected}) == 120
        assert [row["illust_id"] for row in collected] == sorted(illust_ids, reverse=True)
        sizes = []
        page = queries.fetch(query, page_size=page_size)
        sizes.append(len(page.rows))
        while page.next_cursor is not None:
            page = queries.fetch(query, cursor=page.next_cursor, page_size=page_size)
            sizes.append(len(page.rows))
        assert sizes == expected_pages

    first = queries.fetch(query, page_size=50)
    cursor = first.next_cursor
    assert cursor
    with pytest.raises(LibraryError, match="different query"):
        queries.fetch(replace(query, states=("failed",)), cursor=cursor, page_size=50)
    with pytest.raises(LibraryError, match="different query"):
        queries.fetch(replace(query, text="alpha"), cursor=cursor, page_size=50)
    with pytest.raises(LibraryError, match="different query"):
        queries.fetch(replace(query, artist_id="7001"), cursor=cursor, page_size=50)
    with pytest.raises(LibraryError, match="different query"):
        queries.fetch(
            LibraryQuery(kind="pages", batch_id=batch_id, illust_id="1000"),
            cursor=cursor,
            page_size=50,
        )
    with pytest.raises(LibraryError, match="different query"):
        queries.fetch(
            LibraryQuery(kind="batch", batch_id="other-batch"),
            cursor=cursor,
            page_size=50,
        )
    assert len(queries.fetch(query, cursor=cursor, page_size=50).rows) == 50

    for bad in ("", "not-a-cursor", cursor + "x", "A" * 5000, 42, b"bytes"):
        with pytest.raises(LibraryError):
            queries.fetch(query, cursor=bad, page_size=50)
    with pytest.raises(LibraryError):
        queries.fetch(query, cursor=_tampered(cursor), page_size=50)


def test_cursor_is_stable_for_equivalent_time_spellings(ledger):
    repo, _database = ledger
    job, ids = _open(repo, (_candidate("500", tuple(range(51)), page_count=51),))
    for delivery_id in ids:
        _claim(repo, delivery_id, job["batch_id"])
        _complete(repo, delivery_id, job["batch_id"])
    queries = LibraryQueries(repo)
    every_row = queries.fetch(LibraryQuery(kind="history"), page_size=200).rows
    assert len(every_row) == 51
    bound = min(row["completed_at"] for row in every_row)
    first = queries.fetch(LibraryQuery(kind="history", after=bound), page_size=50)
    assert first.next_cursor is not None
    cursor = first.next_cursor
    zulu = bound.replace("+00:00", "Z")
    assert zulu != bound
    page = queries.fetch(LibraryQuery(kind="history", after=zulu), cursor=cursor, page_size=50)
    assert len(page.rows) == 1
    assert page.next_cursor is None


def _tampered(cursor: str) -> str:
    import base64

    payload = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_"))
    payload["f"] = "0" * 64
    return base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def test_keyset_cursor_survives_concurrent_inserts_without_duplicates(ledger):
    repo, database = ledger
    job, ids = _open(
        repo, tuple(_candidate(str(2000 + index)) for index in range(60))
    )
    batch_id = job["batch_id"]
    queries = LibraryQueries(repo)
    query = LibraryQuery(kind="batch", batch_id=batch_id)
    first = queries.fetch(query, page_size=50)
    assert len(first.rows) == 50 and first.next_cursor
    _sql(
        database,
        "INSERT INTO batch_items("
        "batch_id, illust_id, artist_id, artist_name, title, page_count, decision, "
        "reason_codes_json, required_pages_json, result_state, bookmarks, ai_flag, "
        "provenance_json, created_at, updated_at) "
        "SELECT batch_id, '9001', artist_id, artist_name, title, page_count, decision, "
        "reason_codes_json, required_pages_json, result_state, bookmarks, ai_flag, "
        "provenance_json, '2000-01-01T00:00:00.000000+00:00', updated_at "
        "FROM batch_items WHERE batch_id = ? AND illust_id = '2000'",
        (batch_id,),
    )
    collected = list(first.rows)
    page = first
    while page.next_cursor is not None:
        page = queries.fetch(query, cursor=page.next_cursor, page_size=50)
        collected.extend(page.rows)
    assert len(collected) == 61
    assert len({row["illust_id"] for row in collected}) == 61
    assert collected[-1]["illust_id"] == "9001"


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------


def _table_cells(line):
    assert line.startswith("|") and line.endswith("|")
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]]


def test_export_batch_markdown_has_conditions_watermark_and_links(ledger, tmp_path):
    repo, database = ledger
    job, _ids = _build_manifest(repo)
    queries = LibraryQueries(repo)
    watermark = _scalar(database, "SELECT COALESCE(MAX(event_seq), 0) FROM events")
    target = tmp_path / "exports" / "list.md"
    result = queries.export_markdown(
        LibraryQuery(kind="batch", batch_id=job["batch_id"]),
        EXPORT_REF,
        _resolver(tmp_path),
    )
    assert isinstance(result, ExportResult)
    assert result.output_ref == EXPORT_REF
    assert result.row_count == 6
    assert result.watermark == watermark
    assert result.bytes_written == target.stat().st_size
    datetime.fromisoformat(result.generated_at)
    text = target.read_text(encoding="utf-8")

    assert "# 下载名单" in text
    assert f"事件水位: {watermark}" in text
    assert job["batch_id"] in text
    assert "运行中（名单可能仍在变化）" in text
    assert "[100](https://www.pixiv.net/artworks/100)" in text
    assert "[101](https://www.pixiv.net/artworks/101)" in text
    urls = re.findall(r"https?://[^\s)]+", text)
    assert urls and all(
        re.fullmatch(r"https://www\.pixiv\.net/artworks/[0-9]+", url)
        for url in urls
    )
    assert str(tmp_path) not in text

    table_lines = [line for line in text.splitlines() if line.startswith("|")]
    assert len(table_lines) == 2 + 6
    assert [len(_table_cells(line)) for line in table_lines] == [13] * 8
    assert "共 6 行" in text


def test_export_escapes_hostile_title_newlines_and_links(ledger, tmp_path):
    repo, database = ledger
    job, ids = _open(repo, (_candidate("777", title="Plain"),))
    _claim(repo, ids[0], job["batch_id"])
    _complete(repo, ids[0], job["batch_id"])
    hostile = (
        "Bad | pipe\n# heading <script>alert('x')</script> "
        "[click](https://evil.example/x.png) ![img](https://evil.example/i.png) "
        "`tick` *bold* _under_ ~strike~"
    )
    _sql(
        database,
        "UPDATE batch_items SET title = ?, artist_name = ? "
        "WHERE batch_id = ? AND illust_id = '777'",
        (hostile, hostile, job["batch_id"]),
    )
    target = tmp_path / "exports" / "hostile.md"
    LibraryQueries(repo).export_markdown(
        LibraryQuery(kind="batch", batch_id=job["batch_id"]),
        PathRef(relative_path="exports/hostile.md"),
        _resolver(tmp_path),
    )
    text = target.read_text(encoding="utf-8")

    assert "<script>" not in text
    assert "\\<script\\>" in text
    assert re.search(r"(?<!\\)!\[", text) is None
    assert "\\[click\\]" in text
    assert "<br>" in text
    assert "\\|" in text
    links = re.findall(r"(?<!\\)\]\((https?://[^)\s]+)\)", text)
    assert links == ["https://www.pixiv.net/artworks/777"]
    assert "https://evil.example" in text  # plain escaped text, never a link
    table_lines = [line for line in text.splitlines() if line.startswith("|")]
    assert [len(_table_cells(line)) for line in table_lines] == [13, 13, 13]


def test_export_history_and_pages_include_safe_facts(ledger, tmp_path):
    repo, _database = ledger
    job, ids = _build_manifest(repo)
    queries = LibraryQueries(repo)

    history_target = tmp_path / "exports" / "history.md"
    history_result = queries.export_markdown(
        LibraryQuery(kind="history"),
        PathRef(relative_path="exports/history.md"),
        _resolver(tmp_path),
    )
    history_text = history_target.read_text(encoding="utf-8")
    assert history_result.row_count == 3
    assert "| 作品 | 页 |" in history_text
    assert "structure\\_checked" in history_text
    assert re.search(r"[0-9a-f]{64}", history_text)
    assert "project:project/downl\\_Pixiv/100\\_p0.png" in history_text
    assert str(tmp_path) not in history_text
    history_lines = [line for line in history_text.splitlines() if line.startswith("|")]
    assert [len(_table_cells(line)) for line in history_lines] == [11] * 5

    pages_target = tmp_path / "exports" / "pages.md"
    pages_result = queries.export_markdown(
        LibraryQuery(kind="pages", batch_id=job["batch_id"], illust_id="100"),
        PathRef(relative_path="exports/pages.md"),
        _resolver(tmp_path),
    )
    pages_text = pages_target.read_text(encoding="utf-8")
    assert pages_result.row_count == 3
    assert "| 页 | 状态 |" in pages_text
    assert "http\\_transient" in pages_text
    pages_lines = [line for line in pages_text.splitlines() if line.startswith("|")]
    assert [len(_table_cells(line)) for line in pages_lines] == [9] * 5
    assert "https://i.pximg.net" not in pages_text


def test_export_target_is_limited_to_markdown_inside_resolver_root(ledger, tmp_path):
    repo, _database = ledger
    job, _ids = _open(repo, (_candidate("100"),))
    queries = LibraryQueries(repo)
    resolver = _resolver(tmp_path)
    query = LibraryQuery(kind="batch", batch_id=job["batch_id"])

    for relative_path in ("exports/list.sqlite3", "exports/list.png", "exports/list"):
        with pytest.raises(LibraryError, match=".md"):
            queries.export_markdown(
                query, PathRef(relative_path=relative_path), resolver
            )
    with pytest.raises(LibraryError, match="PathRef"):
        queries.export_markdown(query, "exports/list.md", resolver)
    with pytest.raises(LibraryError, match="PathResolver"):
        queries.export_markdown(query, EXPORT_REF, object())
    with pytest.raises(LibraryError):
        queries.export_markdown("not-a-query", EXPORT_REF, resolver)
    with pytest.raises(LibraryError):
        queries.export_markdown(query, EXPORT_REF, resolver, stop_event=object())
    external = PathRef(kind="external", root_id="archive", relative_path="list.md")
    with pytest.raises(ContractError, match="not been mapped"):
        queries.export_markdown(query, external, resolver)


def test_export_replaces_old_product_atomically(ledger, tmp_path):
    repo, _database = ledger
    job, _ids = _open(repo, (_candidate("100"),))
    target = tmp_path / "exports" / "list.md"
    target.parent.mkdir(parents=True)
    target.write_text("OLD PRODUCT", encoding="utf-8")
    result = LibraryQueries(repo).export_markdown(
        LibraryQuery(kind="batch", batch_id=job["batch_id"]),
        EXPORT_REF,
        _resolver(tmp_path),
    )
    assert result.row_count == 1
    assert target.read_text(encoding="utf-8") != "OLD PRODUCT"
    assert sorted(os.listdir(target.parent)) == ["list.md"]


def test_export_cancel_and_deadline_keep_old_product(ledger, tmp_path):
    repo, _database = ledger
    job, _ids = _open(repo, (_candidate("100"),))
    target = tmp_path / "exports" / "list.md"
    target.parent.mkdir(parents=True)
    target.write_text("OLD PRODUCT", encoding="utf-8")
    resolver = _resolver(tmp_path)
    query = LibraryQuery(kind="batch", batch_id=job["batch_id"])

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(LibraryError, match="cancel"):
        LibraryQueries(repo).export_markdown(
            query, EXPORT_REF, resolver, stop_event=cancelled
        )
    assert target.read_text(encoding="utf-8") == "OLD PRODUCT"
    assert sorted(os.listdir(target.parent)) == ["list.md"]

    class _FastClock:
        def __init__(self):
            self.now = 0.0

        def __call__(self):
            self.now += 10.0
            return self.now

    with pytest.raises(LibraryError, match="time limit"):
        LibraryQueries(repo, clock=_FastClock(), export_time_limit=1).export_markdown(
            query, EXPORT_REF, resolver
        )
    assert target.read_text(encoding="utf-8") == "OLD PRODUCT"
    assert sorted(os.listdir(target.parent)) == ["list.md"]


def test_export_publish_failure_keeps_old_product(ledger, tmp_path, monkeypatch):
    import core.library_queries as queries_module

    repo, _database = ledger
    job, _ids = _open(repo, (_candidate("100"),))
    target = tmp_path / "exports" / "list.md"
    target.parent.mkdir(parents=True)
    target.write_text("OLD PRODUCT", encoding="utf-8")

    def _boom(source, target_path):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(queries_module, "_replace_file", _boom)
    with pytest.raises(OSError, match="publish"):
        LibraryQueries(repo).export_markdown(
            LibraryQuery(kind="batch", batch_id=job["batch_id"]),
            EXPORT_REF,
            _resolver(tmp_path),
        )
    assert target.read_text(encoding="utf-8") == "OLD PRODUCT"
    assert sorted(os.listdir(target.parent)) == ["list.md"]


def test_export_read_failure_after_header_keeps_old_product(ledger, tmp_path):
    repo, database = ledger
    job, ids = _open(repo, (_candidate("100", (0, 1), page_count=2),))
    target = tmp_path / "exports" / "list.md"
    target.parent.mkdir(parents=True)
    target.write_text("OLD PRODUCT", encoding="utf-8")
    _sql(
        database,
        "UPDATE deliveries SET output_ref_json = '{broken' WHERE delivery_id = ?",
        (ids[1],),
    )
    with pytest.raises(CorruptDataError):
        LibraryQueries(repo).export_markdown(
            LibraryQuery(kind="pages", batch_id=job["batch_id"], illust_id="100"),
            EXPORT_REF,
            _resolver(tmp_path),
        )
    assert target.read_text(encoding="utf-8") == "OLD PRODUCT"
    assert sorted(os.listdir(target.parent)) == ["list.md"]


class _WriteOnce:
    """A stop_event stand-in that commits one writer command while exporting."""

    def __init__(self, action):
        self.action = action
        self.done = False

    def is_set(self):
        if not self.done:
            self.done = True
            self.action()
        return False


def test_export_snapshot_ignores_concurrent_commit(ledger, tmp_path):
    repo, database = ledger
    job, _ids = _open(
        repo, tuple(_candidate(str(3000 + index)) for index in range(5))
    )
    batch_id = job["batch_id"]
    watermark = _scalar(database, "SELECT COALESCE(MAX(event_seq), 0) FROM events")
    writes = []

    def write_more():
        writes.append(
            repo.accept_source_page(
                batch_id,
                _source(
                    (_candidate("3999"),),
                    page_identity="page-2",
                    before='"cursor-1"',
                    after='"cursor-2"',
                ),
            )
        )

    result = LibraryQueries(repo).export_markdown(
        LibraryQuery(kind="batch", batch_id=batch_id),
        EXPORT_REF,
        _resolver(tmp_path),
        stop_event=_WriteOnce(write_more),
    )
    assert writes, "the concurrent commit must have happened during the export"
    assert result.row_count == 5
    assert result.watermark == watermark
    text = (tmp_path / "exports" / "list.md").read_text(encoding="utf-8")
    assert "3999" not in text
    assert "共 5 行" in text
    # The writer stayed usable and the new work is visible afterwards.
    after = LibraryQueries(repo).fetch(
        LibraryQuery(kind="batch", batch_id=batch_id), page_size=200
    )
    assert len(after.rows) == 6


def test_export_watermark_and_running_status_track_ledger(ledger, tmp_path):
    repo, database = ledger
    job, ids = _open(repo, (_candidate("100"),))
    queries = LibraryQueries(repo)
    batch_id = job["batch_id"]
    running_result = queries.export_markdown(
        LibraryQuery(kind="batch", batch_id=batch_id),
        PathRef(relative_path="exports/running.md"),
        _resolver(tmp_path),
    )
    running_text = (tmp_path / "exports" / "running.md").read_text(encoding="utf-8")
    assert "运行中" in running_text
    assert running_result.watermark == _scalar(
        database, "SELECT COALESCE(MAX(event_seq), 0) FROM events"
    )

    _claim(repo, ids[0], batch_id)
    _complete(repo, ids[0], batch_id)
    repo.finish_source(batch_id, coverage_complete=True)
    assert repo.settle_batch(batch_id) is True
    settled = queries.export_markdown(
        LibraryQuery(kind="batch", batch_id=batch_id),
        PathRef(relative_path="exports/settled.md"),
        _resolver(tmp_path),
    )
    settled_text = (tmp_path / "exports" / "settled.md").read_text(encoding="utf-8")
    assert "已结束（快照）" in settled_text
    assert settled.watermark > running_result.watermark
    assert "共 1 行" in settled_text


def test_export_empty_result_publishes_bounded_manifest(ledger, tmp_path):
    repo, _database = ledger
    _open(repo, (_candidate("100", (), decision="filtered", page_count=None),))
    target = tmp_path / "exports" / "empty.md"
    result = LibraryQueries(repo).export_markdown(
        LibraryQuery(kind="history"),
        PathRef(relative_path="exports/empty.md"),
        _resolver(tmp_path),
    )
    assert result.row_count == 0
    text = target.read_text(encoding="utf-8")
    assert "共 0 行" in text
    assert "| 作品 | 页 |" in text
    assert sorted(os.listdir(target.parent)) == ["empty.md"]


def test_queries_never_change_ledger_rows(ledger, tmp_path):
    repo, database = ledger
    job, _ids = _open(repo, (_candidate("100"),))

    def counts():
        return tuple(
            _scalar(database, f"SELECT COUNT(*) FROM {table}")
            for table in ("events", "artifacts", "deliveries", "batch_items")
        )

    before = counts()
    queries = LibraryQueries(repo)
    queries.fetch(LibraryQuery(kind="batch", batch_id=job["batch_id"]))
    queries.fetch(
        LibraryQuery(kind="pages", batch_id=job["batch_id"], illust_id="100")
    )
    queries.fetch(LibraryQuery(kind="history"))
    queries.export_markdown(
        LibraryQuery(kind="batch", batch_id=job["batch_id"]),
        EXPORT_REF,
        _resolver(tmp_path),
    )
    assert counts() == before


def test_queries_open_no_write_transaction_and_release_after_close(tmp_path):
    repo = LibraryRepository(tmp_path / "library.sqlite3")
    try:
        job, ids = _open(repo, (_candidate("100"),))
        queries = LibraryQueries(repo)
        queries.fetch(LibraryQuery(kind="batch", batch_id=job["batch_id"]))
        queries.export_markdown(
            LibraryQuery(kind="batch", batch_id=job["batch_id"]),
            EXPORT_REF,
            _resolver(tmp_path),
        )
        # Read-only connections do not block the single writer.
        second = repo.submit(_request("req-2"), "submit-req-2")
        assert second["batch_id"]
    finally:
        repo.close(timeout=5)
    with pytest.raises(LibraryClosedError):
        LibraryQueries(repo).fetch(LibraryQuery(kind="history"))


def test_delivery_files_are_never_needed_for_manifest_queries(tmp_path):
    from delivery_fixtures import Response, Transport, executor, make_job, run
    from image_fixtures import PNG_BYTES

    database = tmp_path / "library.sqlite3"
    with LibraryRepository(database) as repo:
        job, ids, _pages = make_job(repo)
        result = run(repo, job, ids[0], executor(repo, tmp_path, Transport(Response())))
        assert result.state == "completed"
        final_file = tmp_path / "downl_Pixiv" / "123_p0.png"
        assert final_file.read_bytes() == PNG_BYTES
        moved = tmp_path / "moved-away.bin"
        os.replace(final_file, moved)
        assert not final_file.exists()

        queries = LibraryQueries(repo)
        history = queries.fetch(LibraryQuery(kind="history")).rows
        assert len(history) == 1
        assert history[0]["illust_id"] == "123"
        assert history[0]["output_ref"]["relative_path"] == "downl_Pixiv/123_p0.png"
        pages = queries.fetch(
            LibraryQuery(kind="pages", batch_id=job["batch_id"], illust_id="123")
        ).rows
        assert len(pages) == 1 and pages[0]["status"] == "completed"
        batch_rows = _batch_rows(queries, job["batch_id"])
        assert batch_rows["123"]["completed"] == 1

        export = queries.export_markdown(
            LibraryQuery(kind="history"),
            PathRef(relative_path="exports/history.md"),
            _resolver(tmp_path),
        )
        assert export.row_count == 1
        text = (tmp_path / "exports" / "history.md").read_text(encoding="utf-8")
        assert "downl\\_Pixiv/123\\_p0.png" in text
        assert str(tmp_path) not in text
