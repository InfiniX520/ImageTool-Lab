"""RFB observation protocol: strict confirmation, resumable progress, page budget."""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

import test_u1a_recovery as h
import test_u1a_rfa_observation as o
from core.library_repository import LibraryError
from core.observation_snapshot import (
    MAX_PROGRESS_EVENTS,
    PROGRESS_EVENT_KIND,
    PageBudget,
    PreflightCancelled,
)
from core.task_contracts import canonical_json


class _Switch:
    """A cancellation flag a test can flip from inside an observation."""

    def __init__(self, value: bool = False):
        self.value = value

    def is_set(self) -> bool:
        return self.value


def links(repo, batch_id) -> int:
    """How many page links the acceptance wrote (pure reuse writes no delivery)."""
    return o.scalar(
        repo,
        "SELECT COUNT(*) FROM batch_item_pages WHERE batch_id = ?",
        (batch_id,),
    )


def accept(repo, job, page, budget=None):
    """Accept one page, returning the delivery ids or the control result."""
    if budget is None:
        return repo.accept_source_page(job["batch_id"], page)
    return repo.accept_source_page(job["batch_id"], page, page_budget=budget)


@pytest.mark.parametrize("restore_mtime", [False, True])
def test_an_in_place_equal_size_edit_is_held_after_a_resume(
    tmp_path, monkeypatch, restore_mtime
):
    """A cached hash is a hint: the commit must re-read the candidate."""
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = o.deliver(repo, tmp_path, pages=(0, 1))
        job = h.submit(repo, request_id="confirmed-reuse")
        with pytest.raises(PreflightCancelled):
            accept(repo, job, page, PageBudget(max_files=1, max_slices=1))
        path = h.target(tmp_path)
        before = path.stat()
        path.write_bytes(h.SAME_SIZE_PNG)
        if restore_mtime:
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        assert (before.st_dev, before.st_ino, before.st_size) == (
            after.st_dev, after.st_ino, after.st_size
        )
        o.settle(repo, job, False)
        job = o.resume(repo, job)
        calls = o.observe_counter(repo, monkeypatch)
        accept(repo, job, page, PageBudget())
        counts = h.pages(repo, job)
        assert len(calls) == 2, "both candidates are observed again in this attempt"
        assert counts["needs_verification"] == 1
        assert counts["completed"] == 1
        assert o.checkpoint(repo, job) is not None


def test_the_confirmation_pass_opens_each_reused_candidate_once(tmp_path, monkeypatch):
    """33 pages: 32 observed in the first call, then 1 new + 32 confirmed."""
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = o.deliver(repo, tmp_path, pages=tuple(range(33)))
        job = h.submit(repo, request_id="confirm-33")
        budget = PageBudget(max_files=32, max_slices=1)
        calls = o.observe_counter(repo, monkeypatch)
        with pytest.raises(PreflightCancelled) as caught:
            accept(repo, job, page, budget)
        assert caught.value.reason == "page_slice_limit"
        assert caught.value.details is not None
        assert caught.value.details["new"] == 32
        assert caught.value.details["remaining"] == 1
        assert len(calls) == 32
        assert o.checkpoint(repo, job) is None

        calls.clear()
        accept(repo, job, page, budget)
        assert len(calls) == 33, "one new observation plus 32 confirmations"
        assert len(set(calls)) == 33, "each cached candidate is confirmed exactly once"
        assert o.checkpoint(repo, job) is not None
        assert links(repo, job["batch_id"]) == 33


def test_a_failed_progress_write_finishes_the_page_in_one_call(tmp_path, monkeypatch):
    """Without a durable hint a slice budget could only repeat the same slice."""
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = o.deliver(repo, tmp_path, pages=tuple(range(6)))
        job = h.submit(repo, request_id="progress-refused")
        calls = o.observe_counter(repo, monkeypatch)

        def refuse(*args, **kwargs):
            raise LibraryError("synthetic progress write refusal")

        monkeypatch.setattr(repo, "record_page_observation", refuse)
        accept(repo, job, page, PageBudget(max_files=1, max_slices=2))
        assert len(calls) == 6, "the whole page is observed in this call"
        assert links(repo, job["batch_id"]) == 6
        assert o.checkpoint(repo, job) is not None


def test_a_truncated_progress_capacity_finishes_the_page_in_one_call(
    tmp_path, monkeypatch
):
    """A page larger than the retained capacity cannot be sliced."""
    import core.library_repository as lib

    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = o.deliver(repo, tmp_path, pages=tuple(range(6)))
        job = h.submit(repo, request_id="capacity-refused")
        monkeypatch.setattr(lib, "MAX_PROGRESS_RECORDS", 3)
        calls = o.observe_counter(repo, monkeypatch)
        accept(repo, job, page, PageBudget(max_files=1, max_slices=2))
        assert len(calls) == 6
        assert links(repo, job["batch_id"]) == 6
        assert o.checkpoint(repo, job) is not None


def test_a_single_file_over_the_page_byte_budget_is_refused_before_opening(
    tmp_path, monkeypatch
):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = o.deliver(repo, tmp_path)
        job = h.submit(repo, request_id="tiny-byte-budget")
        calls = o.observe_counter(repo, monkeypatch)
        with pytest.raises(PreflightCancelled, match="page_byte_budget"):
            accept(repo, job, page, PageBudget(max_bytes=1))
        assert calls == [], "a file the page cannot pay for is never opened"
        assert o.checkpoint(repo, job) is None


def test_the_last_file_cannot_overrun_the_page_byte_budget(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = o.deliver(repo, tmp_path, pages=(0, 1, 2))
        job = h.submit(repo, request_id="last-file-budget")
        calls = o.observe_counter(repo, monkeypatch)
        with pytest.raises(PreflightCancelled, match="page_byte_budget"):
            accept(repo, job, page, PageBudget(max_files=32, max_bytes=140))
        assert len(calls) == 2, "the third file would overrun the page budget"
        assert o.checkpoint(repo, job) is None


def test_a_pause_during_the_confirmation_pass_writes_nothing(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = o.deliver(repo, tmp_path, pages=(0, 1, 2))
        job = h.submit(repo, request_id="pause-during-confirm")
        budget = PageBudget(max_files=2, max_slices=1)
        with pytest.raises(PreflightCancelled):
            accept(repo, job, page, budget)
        stop = _Switch(False)
        opened = []
        original = repo._observe_artifact

        def counted(*args, **kwargs):
            opened.append(str(args[0]["artifact_id"]))
            if len(opened) >= 2:
                stop.value = True
            return original(*args, **kwargs)

        monkeypatch.setattr(repo, "_observe_artifact", counted)
        with pytest.raises(PreflightCancelled) as caught:
            repo.accept_source_page(
                job["batch_id"], page, cancel=stop, page_budget=PageBudget()
            )
        assert caught.value.reason == "cancelled"
        assert o.checkpoint(repo, job) is None
        assert links(repo, job["batch_id"]) == 0


def test_an_accepted_page_replays_without_opening_a_file(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = o.deliver(repo, tmp_path, pages=(0, 1))
        job = h.submit(repo, request_id="replay-free")
        first = accept(repo, job, page, PageBudget())
        calls = o.observe_counter(repo, monkeypatch)
        again = accept(repo, job, page, PageBudget())
        assert again == first
        assert calls == [], "a committed receipt replays with zero hashes"


def test_the_last_slice_commits_without_an_extra_call(tmp_path, monkeypatch):
    """Six files at one file per slice: the third call commits, not a fourth."""
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = o.deliver(repo, tmp_path, pages=tuple(range(6)))
        job = h.submit(repo, request_id="last-slice")
        budget = PageBudget(max_files=1, max_slices=2)
        calls = o.observe_counter(repo, monkeypatch)
        for attempt in range(2):
            with pytest.raises(PreflightCancelled) as caught:
                accept(repo, job, page, budget)
            assert caught.value.reason == "page_slice_limit"
            assert caught.value.details["remaining"] == 4 - 2 * attempt
            o.settle(repo, job, False)
            job = o.resume(repo, job, attempt)
        calls.clear()
        accept(repo, job, page, budget)
        # The last two files are observed, the four the earlier calls covered are
        # confirmed once, and the page commits in this call: the covering slice
        # must not ask for a fourth call (contract v4 §3.2).
        assert len(calls) == 6 and len(set(calls)) == 6
        assert o.checkpoint(repo, job) is not None
        assert links(repo, job["batch_id"]) == 6


def test_two_hundred_and_fifty_seven_pages_need_exactly_one_more_call(
    tmp_path, monkeypatch
):
    """The default budget covers 256 pages; the 257th costs one more call."""
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = o.deliver(repo, tmp_path, pages=tuple(range(257)))
        job = h.submit(repo, request_id="boundary-257")
        calls = o.observe_counter(repo, monkeypatch)
        with pytest.raises(PreflightCancelled) as caught:
            accept(repo, job, page, PageBudget())
        assert caught.value.reason == "page_slice_limit"
        assert caught.value.details["new"] == 32
        assert caught.value.details["remaining"] == 1
        assert len(calls) == 256
        assert o.checkpoint(repo, job) is None
        o.settle(repo, job, False)
        job = o.resume(repo, job, 0)
        calls.clear()
        accept(repo, job, page, PageBudget())
        assert len(calls) == 257, "one new page plus 256 confirmations"
        assert len(set(calls)) == 257
        assert links(repo, job["batch_id"]) == 257
        assert o.checkpoint(repo, job) is not None

def _pad_progress_window(repo, job, count: int = MAX_PROGRESS_EVENTS) -> None:
    """Push this page slice out of the newest-event window.

    The facts are valid progress payloads for another page of the same job: the
    reader must treat a full window as "older facts may be missing" instead of
    silently believing it saw the whole history.  No product code writes these.
    """
    now = datetime.now(timezone.utc).isoformat()
    payload = canonical_json(
        {
            "schema_version": 1,
            "page_key": {
                "job_id": job["job_id"],
                "source_id": "follow",
                "pass_id": "history",
                "entity_id": "source-1",
                "page_identity": "older-page",
                "page_digest": "0" * 64,
            },
            "records": [],
            "observed_files": 0,
            "observed_bytes": 0,
            "slice_seq": 1,
            "root_revisions": {"project": 1},
            "policy_version": 1,
        }
    )
    with sqlite3.connect(repo.database_path) as conn:
        for _ in range(count):
            conn.execute(
                "INSERT INTO events(event_uuid, job_id, batch_id, kind, safe_payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    job["job_id"],
                    job["batch_id"],
                    PROGRESS_EVENT_KIND,
                    payload,
                    now,
                ),
            )
        conn.commit()


def test_the_page_budget_covers_the_whole_page_content(tmp_path, monkeypatch):
    """max_bytes is the page ceiling: exactly the content commits, one less does not."""
    assert PageBudget().max_bytes == 8 * 1024 * 1024 * 1024
    assert PageBudget().max_bytes >= 1024 * 1024 * 1024
    assert PageBudget().max_bytes >= 660 * 1024 * 1024, (
        "a 660 MB work must fit the default page ceiling"
    )
    page_bytes = 3 * len(h.PNG_BYTES)

    exact_root = tmp_path / "exact"
    exact_root.mkdir()
    with h.LibraryRepository(tmp_path / "exact.sqlite3") as repo:
        _, page = o.deliver(repo, exact_root, pages=(0, 1, 2), request_id="exact")
        job = h.submit(repo, request_id="exact-budget")
        calls = o.observe_counter(repo, monkeypatch)
        accept(repo, job, page, PageBudget(max_bytes=page_bytes, max_seconds=60))
        assert len(calls) == 3
        assert links(repo, job["batch_id"]) == 3
        assert o.checkpoint(repo, job) is not None

    short_root = tmp_path / "short"
    short_root.mkdir()
    with h.LibraryRepository(tmp_path / "short.sqlite3") as repo:
        _, page = o.deliver(repo, short_root, pages=(0, 1, 2), request_id="short")
        job = h.submit(repo, request_id="short-budget")
        calls = o.observe_counter(repo, monkeypatch)
        with pytest.raises(PreflightCancelled, match="page_byte_budget"):
            accept(
                repo,
                job,
                page,
                PageBudget(max_bytes=page_bytes - 1, max_seconds=60),
            )
        assert len(calls) == 2, "the third file would overrun the page ceiling"
        assert o.checkpoint(repo, job) is None


def test_a_full_progress_event_window_is_treated_as_truncated(tmp_path, monkeypatch):
    """A window that pushed older facts out cannot be trusted as resumable."""
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = o.deliver(repo, tmp_path, pages=tuple(range(6)))
        job = h.submit(repo, request_id="window-full")
        calls = o.observe_counter(repo, monkeypatch)
        with pytest.raises(PreflightCancelled):
            accept(repo, job, page, PageBudget(max_files=1, max_slices=1))
        assert len(calls) == 1
        assert o.checkpoint(repo, job) is None

        _pad_progress_window(repo, job)
        o.settle(repo, job, False)
        job = o.resume(repo, job, 0)
        calls.clear()
        accept(repo, job, page, PageBudget(max_files=1, max_slices=2))
        assert len(calls) == 6, (
            "a full event window must finish the page in one attempt"
        )
        assert o.checkpoint(repo, job) is not None
        assert links(repo, job["batch_id"]) == 6


