"""The whole-page observation budget stops before opening another file."""
from __future__ import annotations

import pytest

import test_u1a_recovery as h
from core.library_repository import LibraryRepository
from core.observation_snapshot import PageBudget, PreflightCancelled


def test_page_budget_stops_before_the_second_file(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        h.first_delivery(repo, tmp_path)
        other = h.submit(repo, request_id="other-artwork")
        (other_id,) = h.accept(
            repo,
            other["batch_id"],
            h.source((h.candidate(h.OTHER_ARTWORK),), page_identity="other"),
        )
        assert h.run(
            repo, other, other_id,
            h.executor(repo, tmp_path, h.Transport(h.Response())),
        ).state == "completed"
        repo.finish_source(other["batch_id"], coverage_complete=True)
        assert repo.settle_batch(other["batch_id"])

        job = h.submit(repo, request_id="budget-page")
        page = h.source(
            (h.candidate(h.ARTWORK), h.candidate(h.OTHER_ARTWORK)),
            page_identity="budget-page",
        )
        observe = repo._observe_artifact
        calls = []

        def counted(*args, **kwargs):
            calls.append(str(args[0]["artifact_id"]))
            return observe(*args, **kwargs)

        monkeypatch.setattr(repo, "_observe_artifact", counted)
        repo._accept_context.budget = PageBudget(max_files=1)
        repo._accept_context.cancel = None
        try:
            with pytest.raises(PreflightCancelled) as caught:
                repo._preflight_page_health(job["batch_id"], page)
        finally:
            repo._accept_context.budget = None
        assert caught.value.reason == "page_file_budget"
        assert len(calls) == 1, "the budget must be checked before the next file"

class _FailingFuture:
    def __init__(self, exc):
        self.exc = exc

    def result(self, timeout=None):
        raise self.exc


class _Repo:
    def get_batch(self, batch_id):
        return {"phase": "running"}


class _Coordinator:
    def __init__(self, exc):
        self.repository = _Repo()
        self.exc = exc

    def accept_page(self, batch_id, page):
        return _FailingFuture(self.exc)


def _collector(exc):
    from core.follow_collection import FollowCollection

    collector = FollowCollection(_Coordinator(exc), source=object())
    collector.batch_id = "batch"
    collector.stop = None
    collector.deadline = 10**9
    collector.calls = 0
    return collector


def test_pause_cancellation_is_a_stop_not_a_source_failure():
    from core.follow_collection import _Stopped

    with pytest.raises(_Stopped):
        _collector(PreflightCancelled("cancelled"))._accept(None)


def test_page_budget_is_a_budget_result():
    from core.follow_collection import _Budget

    with pytest.raises(_Budget):
        _collector(PreflightCancelled("page_byte_budget"))._accept(None)
