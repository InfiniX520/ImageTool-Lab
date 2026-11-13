"""UA-08: a carry command parses its health map once, on both carry paths.

Only temporary SQLite ledgers, synthetic PNG bytes and a fake HTTP transport are
used.  The tests count `_health_map` calls; they do not time anything, because a
timing threshold would hide a per-item reparse on a fast machine.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

import test_u1a_recovery as h
from core.library_repository import LibraryError, LibraryRepository
from core.task_contracts import (
    BatchBudget,
    CollectionRequest,
    OutputSpec,
    QualitySpec,
    QuerySpec,
)


def _settle(repo, job):
    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"]) is True


def _count_parses(repo, monkeypatch):
    parse = repo._health_map
    sizes: list[int] = []

    def count(value):
        sizes.append(len(value) if isinstance(value, list) else -1)
        return parse(value)

    monkeypatch.setattr(repo, "_health_map", count)
    return sizes


def _three_work_candidates():
    candidates = []
    for illust in ("123", "456", "789"):
        first = h.candidate(illust, page_index=0, page_count=2)
        second = h.candidate(illust, page_index=1, page_count=2)
        candidates.append(replace(first, pages=first.pages + second.pages))
    return candidates


def _carry_ready(repo, tmp_path, request):
    """Run one page per work to completion/failure, settle, then resume."""
    submitted = repo.submit(request, "submit-" + request.request_id)
    batch = repo.activate_next()
    assert batch is not None and batch["batch_id"] == submitted["batch_id"]
    job = {"job_id": submitted["job_id"], "batch_id": submitted["batch_id"]}
    candidates = _three_work_candidates()
    ids = repo.accept_source_page(batch["batch_id"], h.source(candidates))
    transport = h.Transport(
        *[response for _ in candidates for response in (h.Response(), h.Response(status=404))]
    )
    service = h.executor(repo, tmp_path, transport)
    for index, delivery in enumerate(ids):
        expected = "completed" if index % 2 == 0 else "failed"
        assert h.run(repo, job, delivery, service).state == expected
    _settle(repo, job)
    resumed = repo.resume(job["job_id"], "resume-" + request.request_id)
    assert repo.activate_next()["batch_id"] == resumed["batch_id"]
    return job, resumed


def _query_request(request_id="query-carry"):
    return CollectionRequest(
        request_id,
        "offline-account-synthetic",
        output=OutputSpec(folder_by_artist=False),
        quality=QualitySpec(all_pages=True),
        budget=BatchBudget(max_works=10, max_requests=10, max_artists=5),
        dedup_policy="trusted",
        query=QuerySpec(source="ranking", mode="query"),
    )


def test_follow_carry_parses_the_health_map_once(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, resumed = _carry_ready(repo, tmp_path, h.request())
        sizes = _count_parses(repo, monkeypatch)
        result = repo.carry_unresolved_items(resumed["batch_id"])
        assert result["count"] == 3
        assert sizes == [3], f"health map parsed {len(sizes)} times: {sizes}"
        counts = repo.batch_results(resumed["batch_id"])["pages"]
        assert counts["completed"] == 3
        assert counts["needs_verification"] == 0
        assert counts["new_completed"] == 0


def test_each_carried_page_links_its_own_artifact_from_one_map(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, resumed = _carry_ready(repo, tmp_path, h.request())
        source_links = h.query(
            repo.database_path,
            "SELECT illust_id, page_index, artifact_id FROM batch_item_pages "
            "WHERE batch_id=? ORDER BY illust_id, page_index",
            (job["batch_id"],),
        )
        expected = {
            (row["illust_id"], int(row["page_index"])): row["artifact_id"]
            for row in source_links
            if row["artifact_id"] is not None
        }
        sizes = _count_parses(repo, monkeypatch)
        assert repo.carry_unresolved_items(resumed["batch_id"])["count"] == 3
        assert sizes == [3]
        carried = h.query(
            repo.database_path,
            "SELECT illust_id, page_index, artifact_id FROM batch_item_pages "
            "WHERE batch_id=? ORDER BY illust_id, page_index",
            (resumed["batch_id"],),
        )
        actual = {
            (row["illust_id"], int(row["page_index"])): row["artifact_id"]
            for row in carried
            if row["artifact_id"] is not None
        }
        assert actual == expected
        assert all(key[1] == 0 for key in actual), actual


def test_an_empty_health_map_is_parsed_once_and_still_fails_closed(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, resumed = _carry_ready(repo, tmp_path, h.request())
        sizes = _count_parses(repo, monkeypatch)
        result = repo._enqueue(
            "carry_unresolved_items",
            {
                "batch_id": resumed["batch_id"],
                "after": "",
                "limit": 100,
                "enforce_health": True,
                "health": [],
            },
        )
        assert result["count"] == 3
        assert sizes == [0], f"health map parsed {len(sizes)} times: {sizes}"
        counts = repo.batch_results(resumed["batch_id"])["pages"]
        assert counts["completed"] == 0
        assert counts["needs_verification"] == 3


def test_carry_health_verification_cannot_be_disabled(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, resumed = _carry_ready(repo, tmp_path, h.request())
        with pytest.raises(LibraryError):
            repo.carry_unresolved_items(resumed["batch_id"], enforce_health=False)
        with pytest.raises(LibraryError):
            repo.carry_query_unresolved_items(resumed["batch_id"], enforce_health=False)


def test_query_carry_parses_the_health_map_once(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, resumed = _carry_ready(repo, tmp_path, _query_request())
        sizes = _count_parses(repo, monkeypatch)
        result = repo.carry_unresolved_items(resumed["batch_id"])
        assert result["count"] == 3
        assert sizes == [3], f"health map parsed {len(sizes)} times: {sizes}"
        counts = repo.batch_results(resumed["batch_id"])["pages"]
        assert counts["completed"] == 3
        assert counts["needs_verification"] == 0
        source = {
            (row["illust_id"], int(row["page_index"])): row["artifact_id"]
            for row in h.query(
                repo.database_path,
                "SELECT illust_id, page_index, artifact_id FROM batch_item_pages "
                "WHERE batch_id=?",
                (job["batch_id"],),
            )
            if row["artifact_id"] is not None
        }
        carried = {
            (row["illust_id"], int(row["page_index"])): row["artifact_id"]
            for row in h.query(
                repo.database_path,
                "SELECT illust_id, page_index, artifact_id FROM batch_item_pages "
                "WHERE batch_id=?",
                (resumed["batch_id"],),
            )
            if row["artifact_id"] is not None
        }
        assert carried == source
