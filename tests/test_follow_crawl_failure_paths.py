"""Failure-path contracts: rate-limit deferral and cancellation safety."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.follow_crawl_service import FollowCrawlOptions, FollowCrawlService
from core.follow_crawl_state import FollowCrawlStateRepository
from core.pixiv_api_policy import PixivApiRequestExecutor


def _executor(*, budget=100, max_attempts=2):
    return PixivApiRequestExecutor(
        request_budget=budget,
        min_interval=0,
        wait=lambda _seconds: True,
        should_stop=lambda: False,
        log=lambda _message: None,
        max_attempts=max_attempts,
        rate_limit_delays=(0, 0),
        transient_delays=(0, 0),
        jitter=0,
        clock=lambda: 0,
        random_uniform=lambda _low, _high: 0,
    )


def test_repeated_429_defers_artist_and_preserves_run(tmp_path: Path) -> None:
    class RateLimited(RuntimeError):
        status_code = 429

    class Api:
        user_id = "42"

        def user_following(self, *_args, **_kwargs):
            preview = SimpleNamespace(user=SimpleNamespace(id=100, name="A"))
            return SimpleNamespace(user_previews=[preview], next_url=None)

        def user_illusts(self, *_args, **_kwargs):
            raise RateLimited("429 Too Many Requests")

        @staticmethod
        def parse_qs(_url):
            return {}

    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    summary = FollowCrawlService(
        api=Api(),
        repository=repository,
        request_executor=_executor(),
        options=FollowCrawlOptions(),
        process_illust=lambda _illust, _fetched: SimpleNamespace(queued=0),
        should_stop=lambda: False,
        log=lambda _message: None,
    ).run(account_id="42", config_key="cfg")

    assert summary.status == "paused"
    assert summary.stop_reason
    run = repository.find_resumable_run("42", "cfg")
    assert run is not None
    artist = repository.list_artists(run.run_id)[0]
    assert artist.state == "deferred"
    assert artist.last_error_kind == "rate_limited"
    repository.close()


def test_budget_pause_reports_items_queued_before_checkpoint(tmp_path: Path) -> None:
    class Api:
        user_id = "42"

        def user_following(self, *_args, **_kwargs):
            preview = SimpleNamespace(user=SimpleNamespace(id=100, name="A"))
            return SimpleNamespace(user_previews=[preview], next_url=None)

        def user_illusts(self, *_args, **_kwargs):
            works = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
            return SimpleNamespace(illusts=works, next_url=None)

        @staticmethod
        def parse_qs(_url):
            return {}

    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    executor = _executor(budget=3)

    def process(illust, _fetched):
        executor.call(f"detail {illust.id}", lambda: {"ok": True})
        return SimpleNamespace(queued=1)

    summary = FollowCrawlService(
        api=Api(),
        repository=repository,
        request_executor=executor,
        options=FollowCrawlOptions(max_queued_items=100),
        process_illust=process,
        should_stop=lambda: False,
        log=lambda _message: None,
    ).run(account_id="42", config_key="cfg")

    assert summary.status == "paused"
    assert summary.queued_items == 1
    repository.close()

