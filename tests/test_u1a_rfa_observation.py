"""R-A resumable page observation: slices, progress facts and reuse.

Every ledger, root, PNG byte and HTTP response here is synthetic.  No network,
account, real gallery, production database or Tk widget is touched.

What is under test (contract v3 §1):

* one budget is one *slice*; a page with more candidates than one slice keeps
  advancing instead of failing on the same first N files forever;
* the observations a slice produced are an append-only, artifact-bound fact
  keyed by page content (not by batch), so a resume or a second repository
  instance continues where the last slice stopped;
* a recorded observation is reused only while it still matches the row read for
  this attempt: artifact identity, bytes, quality decision, root revision,
  policy version and age; otherwise the file is observed again;
* nothing is committed, no receipt is written and no cursor moves until the
  whole page is observed and accepted atomically.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import test_u1a_recovery as h
from core.library_repository import LibraryError
from core.observation_snapshot import (
    PROGRESS_EVENT_KIND,
    PageBudget,
    PreflightCancelled,
)
from core.task_contracts import CandidateDecision, PageSpec, PathRef
from delivery_fixtures import Response, Transport

ARTWORK = h.ARTWORK


class _Switch:
    """A cancellation flag a test can flip from inside an observation."""

    def __init__(self, value: bool = False):
        self.value = value

    def is_set(self) -> bool:
        return self.value


def candidate(illust_id: str = ARTWORK, *, pages=(0,)) -> CandidateDecision:
    specs = tuple(
        PageSpec(
            index,
            PathRef(relative_path=f"downl_Pixiv/{illust_id}_p{index}.png"),
            f"https://i.pximg.net/img-original/2026/{illust_id}_p{index}.png",
        )
        for index in pages
    )
    return CandidateDecision(
        illust_id,
        "download",
        artist_id="7",
        page_count=len(specs),
        pages=specs,
    )


def source(candidates, *, page_identity="page-1"):
    return h.source(candidates, page_identity=page_identity)


def settle(repo, job, complete: bool = True) -> None:
    repo.finish_source(job["batch_id"], coverage_complete=complete)
    assert repo.settle_batch(job["batch_id"])


def resume(repo, job, attempt: int = 0):
    job = repo.resume(job["job_id"], f"resume-{attempt}")
    assert repo.activate_next()["batch_id"] == job["batch_id"]
    return job


def deliver(repo, root, *, pages=(0,), illust_id=ARTWORK, request_id="seed"):
    """Deliver one synthetic work through the real executor, then settle."""
    job = h.submit(repo, request_id=request_id)
    page = source((candidate(illust_id, pages=pages),))
    ids = h.accept(repo, job["batch_id"], page)
    service = h.executor(repo, root, h.Transport(*(h.Response() for _ in ids)))
    for delivery in ids:
        assert h.run(repo, job, delivery, service).state == "completed"
    settle(repo, job)
    return job, page


def observe_counter(repo, monkeypatch):
    """Record which artifacts the preflight actually opened."""
    calls: list[str] = []
    original = repo._observe_artifact

    def counted(*args, **kwargs):
        calls.append(str(args[0]["artifact_id"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(repo, "_observe_artifact", counted)
    return calls


def progress_events(repo) -> list[dict]:
    with sqlite3.connect(repo.database_path) as conn:
        rows = conn.execute(
            "SELECT safe_payload FROM events WHERE kind = ? ORDER BY event_seq",
            (PROGRESS_EVENT_KIND,),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def recorded_ids(events) -> list[str]:
    return [record["artifact_id"] for event in events for record in event["records"]]


def scalar(repo, query: str, parameters=()):
    with sqlite3.connect(repo.database_path) as conn:
        return conn.execute(query, parameters).fetchone()[0]


def checkpoint(repo, job):
    return repo.get_checkpoint(job["job_id"], "follow", "history", "source-1")


def artifact_id_for(repo, illust_id=ARTWORK, page_index=0) -> str:
    return scalar(
        repo,
        "SELECT artifact_id FROM artifacts WHERE illust_id = ? AND page_index = ? "
        "ORDER BY completed_at DESC, artifact_id DESC LIMIT 1",
        (illust_id, page_index),
    )


def linked_artifact(repo, batch_id, illust_id=ARTWORK, page_index=0):
    return scalar(
        repo,
        "SELECT artifact_id FROM batch_item_pages WHERE batch_id = ? AND illust_id = ? "
        "AND page_index = ?",
        (batch_id, illust_id, page_index),
    )


def test_a_33_image_work_advances_across_slice_limited_calls(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = deliver(repo, tmp_path, pages=tuple(range(33)))
        job = h.submit(repo, request_id="reuse-33")
        calls = observe_counter(repo, monkeypatch)
        budget = PageBudget(max_files=16, max_slices=1)

        for attempt in range(2):
            with pytest.raises(PreflightCancelled) as caught:
                repo.accept_source_page(job["batch_id"], page, page_budget=budget)
            assert caught.value.reason == "page_slice_limit"
            # A partial slice never looks committed.
            assert checkpoint(repo, job) is None
            assert len(calls) == 16 * (attempt + 1)
            settle(repo, job, complete=False)
            job = resume(repo, job, attempt)

        assert repo.accept_source_page(job["batch_id"], page, page_budget=budget) == ()
        assert len(calls) == 33 and len(set(calls)) == 33
        assert checkpoint(repo, job) is not None
        events = progress_events(repo)
        # Two full slices, then the single file the last slice could not hold.
        assert [len(event["records"]) for event in events] == [16, 16, 1]
        assert len(set(recorded_ids(events))) == 33
        assert all(event["page_key"]["page_digest"] for event in events)
        assert [event["observed_files"] for event in events] == [16, 16, 1]


def test_a_64_image_work_commits_in_one_call_across_two_slices(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = deliver(repo, tmp_path, pages=tuple(range(64)))
        job = h.submit(repo, request_id="reuse-64")
        calls = observe_counter(repo, monkeypatch)

        assert repo.accept_source_page(job["batch_id"], page, page_budget=PageBudget()) == ()
        assert len(calls) == 64 and len(set(calls)) == 64
        assert checkpoint(repo, job) is not None
        assert [len(event["records"]) for event in progress_events(repo)] == [32, 32]


def test_twenty_multi_page_works_share_one_source_page(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job = h.submit(repo, request_id="works", max_works=25)
        candidates = tuple(
            candidate(str(1000 + index), pages=(0, 1, 2)) for index in range(20)
        )
        page = source(candidates)
        ids = h.accept(repo, job["batch_id"], page)
        assert len(ids) == 60
        service = h.executor(repo, tmp_path, h.Transport(*(h.Response() for _ in ids)))
        for delivery in ids:
            assert h.run(repo, job, delivery, service).state == "completed"
        settle(repo, job)

        job = h.submit(repo, request_id="reuse-works", max_works=25)
        calls = observe_counter(repo, monkeypatch)
        assert repo.accept_source_page(job["batch_id"], page, page_budget=PageBudget()) == ()
        assert len(calls) == 60 and len(set(calls)) == 60
        assert len(recorded_ids(progress_events(repo))) == 60


def test_two_historical_versions_of_one_file_are_both_observed(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        deliver(repo, tmp_path, request_id="seed-a")
        h.target(tmp_path).unlink()
        deliver(repo, tmp_path, request_id="seed-b")
        newest = artifact_id_for(repo)

        job = h.submit(repo, request_id="reuse-versions")
        page = source((candidate(),))
        calls = observe_counter(repo, monkeypatch)
        assert repo.accept_source_page(
            job["batch_id"], page, page_budget=PageBudget(max_files=1)
        ) == ()
        assert len(set(calls)) == 2
        assert len(calls) == 2
        assert linked_artifact(repo, job["batch_id"]) == newest
        assert checkpoint(repo, job) is not None


def test_a_pause_at_a_slice_boundary_keeps_the_slice(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = deliver(repo, tmp_path, pages=tuple(range(9)))
        job = h.submit(repo, request_id="pause-9")
        switch = _Switch()
        calls: list[str] = []
        original = repo._observe_artifact

        def counted(*args, **kwargs):
            calls.append(str(args[0]["artifact_id"]))
            if len(calls) == 4:
                switch.value = True
            return original(*args, **kwargs)

        monkeypatch.setattr(repo, "_observe_artifact", counted)
        with pytest.raises(PreflightCancelled) as caught:
            repo.accept_source_page(
                job["batch_id"],
                page,
                cancel=switch,
                page_budget=PageBudget(max_files=4),
            )
        assert caught.value.reason == "cancelled"
        assert checkpoint(repo, job) is None
        assert [len(event["records"]) for event in progress_events(repo)] == [4]

        calls.clear()
        switch.value = False
        assert repo.accept_source_page(job["batch_id"], page, page_budget=PageBudget(max_files=4)) == ()
        # Only the five files no slice has observed yet are opened again.
        assert len(calls) == 5
        assert len(recorded_ids(progress_events(repo))) == 9


def test_cancelling_before_the_first_file_leaves_no_trace(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = deliver(repo, tmp_path, pages=(0, 1))
        job = h.submit(repo, request_id="cancel-early")
        calls = observe_counter(repo, monkeypatch)
        with pytest.raises(PreflightCancelled) as caught:
            repo.accept_source_page(
                job["batch_id"],
                page,
                cancel=_Switch(True),
                page_budget=PageBudget(max_files=1),
            )
        assert caught.value.reason == "cancelled"
        assert calls == []
        assert progress_events(repo) == []
        assert checkpoint(repo, job) is None


def test_re_accepting_the_same_page_replays_without_observing(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job = h.submit(repo, request_id="duplicate")
        page = source((candidate(),))
        first = repo.accept_source_page(job["batch_id"], page)
        calls = observe_counter(repo, monkeypatch)
        assert repo.accept_source_page(job["batch_id"], page) == first
        assert calls == []
        assert progress_events(repo) == []


def test_a_candidate_that_appears_between_slices_is_observed(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = deliver(repo, tmp_path, pages=(0, 1, 2), request_id="seed")
        settled = artifact_id_for(repo, page_index=0)
        moved = artifact_id_for(repo, page_index=1)
        job = h.submit(repo, request_id="reuse-race")
        budget = PageBudget(max_files=2, max_slices=1)
        calls = observe_counter(repo, monkeypatch)
        with pytest.raises(PreflightCancelled) as caught:
            repo.accept_source_page(job["batch_id"], page, page_budget=budget)
        assert caught.value.reason == "page_slice_limit"
        assert set(calls) == {settled, moved}
        settle(repo, job, complete=False)

        # The row that page one's record points at no longer describes the
        # same bytes, so that record is history, not evidence.
        with sqlite3.connect(repo.database_path) as conn:
            conn.execute(
                "UPDATE artifacts SET sha256 = ? WHERE artifact_id = ?",
                ("f" * 64, moved),
            )
            conn.commit()
        job = resume(repo, job, 0)

        calls.clear()
        repo.accept_source_page(job["batch_id"], page, page_budget=PageBudget(max_files=4))
        assert moved in calls
        assert settled not in calls
        assert len(calls) == 2


def test_a_file_replaced_between_slices_is_observed_again(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = deliver(repo, tmp_path, pages=(0, 1, 2), request_id="seed")
        first_page = artifact_id_for(repo, page_index=0)
        second_page = artifact_id_for(repo, page_index=1)
        job = h.submit(repo, request_id="reuse-replaced")
        budget = PageBudget(max_files=2, max_slices=1)
        calls = observe_counter(repo, monkeypatch)
        with pytest.raises(PreflightCancelled):
            repo.accept_source_page(job["batch_id"], page, page_budget=budget)
        assert set(calls) == {first_page, second_page}
        settle(repo, job, complete=False)

        # Same bytes, new object: the recorded identity no longer describes the
        # file at that path, so the record must not be reused.
        swapped = tmp_path / "swapped.bin"
        swapped.write_bytes(h.target(tmp_path).read_bytes())
        h.target(tmp_path).unlink()
        swapped.replace(h.target(tmp_path))
        job = resume(repo, job, 0)

        calls.clear()
        assert repo.accept_source_page(
            job["batch_id"], page, page_budget=PageBudget(max_files=2)
        ) == ()
        assert first_page in calls
        assert second_page not in calls
        assert len(calls) == 2


def test_a_matching_identity_costs_no_slice_slot(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = deliver(repo, tmp_path, pages=(0, 1, 2), request_id="seed")
        job = h.submit(repo, request_id="reuse-identity-ok")
        calls = observe_counter(repo, monkeypatch)
        with pytest.raises(PreflightCancelled):
            repo.accept_source_page(
                job["batch_id"], page, page_budget=PageBudget(max_files=2, max_slices=1)
            )
        assert len(calls) == 2
        settle(repo, job, complete=False)
        job = resume(repo, job, 0)

        # Two stat rechecks plus one open still fit one file of budget.
        calls.clear()
        assert repo.accept_source_page(
            job["batch_id"], page, page_budget=PageBudget(max_files=1)
        ) == ()
        assert len(calls) == 1
        assert checkpoint(repo, job) is not None


def test_expired_progress_is_observed_again(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = deliver(repo, tmp_path, pages=(0, 1, 2), request_id="seed")
        job = h.submit(repo, request_id="reuse-stale")
        budget = PageBudget(max_files=2, max_slices=1)
        calls = observe_counter(repo, monkeypatch)
        with pytest.raises(PreflightCancelled):
            repo.accept_source_page(job["batch_id"], page, page_budget=budget)
        recorded = list(calls)
        assert len(recorded) == 2

        # Age the stored observations beyond the 900 s policy window.
        with sqlite3.connect(repo.database_path) as conn:
            for (payload,) in conn.execute(
                "SELECT safe_payload FROM events WHERE kind = ?", (PROGRESS_EVENT_KIND,)
            ).fetchall():
                event = json.loads(payload)
                for record in event["records"]:
                    record["health"]["checked_at"] = "2026-01-01T00:00:00.000000+00:00"
                conn.execute(
                    "UPDATE events SET safe_payload = ? WHERE kind = ? AND safe_payload = ?",
                    (json.dumps(event, sort_keys=True, separators=(",", ":")), PROGRESS_EVENT_KIND, payload),
                )
            conn.commit()

        settle(repo, job, complete=False)
        job = resume(repo, job, 0)
        calls.clear()
        with pytest.raises(PreflightCancelled):
            repo.accept_source_page(job["batch_id"], page, page_budget=budget)
        assert calls == recorded  # stale evidence is never reused


def test_progress_survives_a_new_repository_instance(tmp_path, monkeypatch):
    database = tmp_path / "ledger.sqlite3"
    with h.LibraryRepository(database) as repo:
        _, page = deliver(repo, tmp_path, pages=tuple(range(5)), request_id="seed")
        job = h.submit(repo, request_id="reuse-restart")
        budget = PageBudget(max_files=2, max_slices=1)
        calls = observe_counter(repo, monkeypatch)
        with pytest.raises(PreflightCancelled):
            repo.accept_source_page(job["batch_id"], page, page_budget=budget)
        first_slice = list(calls)
        assert len(first_slice) == 2
        settle(repo, job, complete=False)

    with h.LibraryRepository(database) as repo:
        h.executor(repo, tmp_path, h.Transport())  # rebind the synthetic root
        calls = observe_counter(repo, monkeypatch)
        job = resume(repo, job, 0)
        with pytest.raises(PreflightCancelled):
            repo.accept_source_page(job["batch_id"], page, page_budget=budget)
        assert calls and not set(calls) & set(first_slice)


def test_no_receipt_or_link_exists_before_the_page_commits(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = deliver(repo, tmp_path, pages=(0, 1, 2), request_id="seed")
        job = h.submit(repo, request_id="atomic")
        with pytest.raises(PreflightCancelled):
            repo.accept_source_page(
                job["batch_id"], page, page_budget=PageBudget(max_files=1, max_slices=1)
            )
        assert checkpoint(repo, job) is None
        assert scalar(
            repo,
            "SELECT COUNT(*) FROM source_batch_pages WHERE batch_id = ?",
            (job["batch_id"],),
        ) == 0
        assert scalar(
            repo,
            "SELECT COUNT(*) FROM batch_item_pages WHERE batch_id = ?",
            (job["batch_id"],),
        ) == 0
        assert repo.accept_source_page(job["batch_id"], page) == ()
        assert checkpoint(repo, job) is not None
        assert scalar(
            repo,
            "SELECT COUNT(*) FROM source_batch_pages WHERE batch_id = ?",
            (job["batch_id"],),
        ) == 1
        assert scalar(
            repo,
            "SELECT COUNT(*) FROM batch_item_pages WHERE batch_id = ?",
            (job["batch_id"],),
        ) == 3


def test_a_changed_page_content_does_not_reuse_the_old_progress(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = deliver(repo, tmp_path, pages=(0, 1, 2), request_id="seed")
        job = h.submit(repo, request_id="reuse-changed")
        budget = PageBudget(max_files=1, max_slices=1)
        calls = observe_counter(repo, monkeypatch)
        with pytest.raises(PreflightCancelled):
            repo.accept_source_page(job["batch_id"], page, page_budget=budget)
        recorded = list(calls)
        assert len(recorded) == 1
        settle(repo, job, complete=False)
        job = resume(repo, job, 0)

        # The same page identity now carries one more required page: the old
        # progress belongs to different page content and must not be reused.
        changed = source((candidate(pages=(0, 1, 2, 3)),))
        calls.clear()
        with pytest.raises(PreflightCancelled):
            repo.accept_source_page(job["batch_id"], changed, page_budget=budget)
        assert calls == recorded


def test_a_rejected_progress_write_never_fails_the_page(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, page = deliver(repo, tmp_path, pages=(0, 1, 2), request_id="seed")
        job = h.submit(repo, request_id="no-progress")

        def broken(*args, **kwargs):
            raise LibraryError("synthetic progress failure")

        monkeypatch.setattr(repo, "record_page_observation", broken)
        assert repo.accept_source_page(
            job["batch_id"], page, page_budget=PageBudget(max_files=2)
        ) == ()
        assert repo._progress_write_error == "LibraryError"
        assert checkpoint(repo, job) is not None
        assert progress_events(repo) == []
