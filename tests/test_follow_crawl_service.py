"""End-to-end contracts for followed-artist scheduling and continuation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.follow_crawl_service import (
    START_INDEX,
    START_RESUME,
    FollowCrawlOptions,
    FollowCrawlService,
)
from core.follow_crawl_state import FollowCrawlStateRepository
from core.pixiv_api_policy import PixivApiRequestExecutor


class FakeApi:
    user_id = "42"

    def __init__(self, artist_works):
        self.artist_works = artist_works
        self.following_calls = 0
        self.user_calls = []

    def user_following(self, *_args, **_kwargs):
        self.following_calls += 1
        previews = [
            SimpleNamespace(user=SimpleNamespace(id=artist_id, name=f"Artist {artist_id}"))
            for artist_id in self.artist_works
        ]
        return SimpleNamespace(user_previews=previews, next_url=None)

    def user_illusts(self, user_id=None, **kwargs):
        artist_id = str(user_id or kwargs.get("user_id"))
        self.user_calls.append(artist_id)
        works = [SimpleNamespace(id=value) for value in self.artist_works[artist_id]]
        return SimpleNamespace(illusts=works, next_url=None)

    @staticmethod
    def parse_qs(_url):
        return {}


def _executor(budget=100):
    return PixivApiRequestExecutor(
        request_budget=budget,
        min_interval=0,
        wait=lambda _seconds: True,
        should_stop=lambda: False,
        log=lambda _message: None,
        jitter=0,
        clock=lambda: 0,
        random_uniform=lambda _low, _high: 0,
    )


def _service(api, repository, executor, options, processed):
    def process(illust, _fetched):
        processed.append(illust.id)
        return SimpleNamespace(queued=1)

    return FollowCrawlService(
        api=api,
        repository=repository,
        request_executor=executor,
        options=options,
        process_illust=process,
        should_stop=lambda: False,
        log=lambda _message: None,
    )


def test_smart_resume_uses_saved_snapshot_instead_of_starting_over(tmp_path: Path) -> None:
    api = FakeApi({"100": [1], "200": [2], "300": [3]})
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    processed = []
    options = FollowCrawlOptions(
        start_mode=START_RESUME,
        max_artists_per_batch=1,
        max_queued_items=100,
    )

    first = _service(api, repository, _executor(), options, processed).run(
        account_id="42", config_key="cfg"
    )
    second = _service(api, repository, _executor(), options, processed).run(
        account_id="42", config_key="cfg"
    )
    third = _service(api, repository, _executor(), options, processed).run(
        account_id="42", config_key="cfg"
    )

    assert first.status == "paused"
    assert second.status == "paused"
    assert third.status == "completed"
    assert processed == [1, 2, 3]
    # Each resume re-syncs the follow list (unfollow detection + new
    # follows), so the snapshot endpoint is hit once per batch.
    assert api.following_calls == 3
    # The third batch fully crawls artist 300, then incrementally sweeps
    # artists 100/200 (page-1 probe each) without reprocessing any work.
    assert api.user_calls == ["100", "200", "300", "100", "200"]
    repository.close()


def test_manual_index_starts_from_requested_artist(tmp_path: Path) -> None:
    api = FakeApi({"100": [1], "200": [2], "300": [3]})
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    processed = []
    options = FollowCrawlOptions(
        start_mode=START_INDEX,
        start_value="2",
        max_artists_per_batch=10,
        max_queued_items=100,
    )
    summary = _service(api, repository, _executor(), options, processed).run(
        account_id="42", config_key="cfg"
    )

    assert summary.status == "completed"
    assert processed == [2, 3]
    assert api.user_calls == ["200", "300"]
    repository.close()


class MutableFakeApi:
    """FakeApi variant whose follow list can change between batches."""

    user_id = "42"

    def __init__(self, artist_works, following=None):
        self.artist_works = artist_works
        self.following = list(following if following is not None else artist_works.keys())
        self.user_calls = []

    def user_following(self, *_args, **_kwargs):
        previews = [
            SimpleNamespace(user=SimpleNamespace(id=artist_id, name=f"Artist {artist_id}"))
            for artist_id in self.following
        ]
        return SimpleNamespace(user_previews=previews, next_url=None)

    def user_illusts(self, user_id=None, **kwargs):
        artist_id = str(user_id or kwargs.get("user_id"))
        self.user_calls.append(artist_id)
        works = [SimpleNamespace(id=value) for value in self.artist_works[artist_id]]
        return SimpleNamespace(illusts=works, next_url=None)

    @staticmethod
    def parse_qs(_url):
        return {}


def test_resume_parks_unfollowed_artists_and_appends_new_follows(tmp_path: Path) -> None:
    api = MutableFakeApi({"100": [1], "200": [2]})
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    processed = []
    options = FollowCrawlOptions(
        start_mode=START_RESUME,
        max_artists_per_batch=10,
        max_queued_items=100,
    )

    first = _service(api, repository, _executor(), options, processed).run(
        account_id="42", config_key="cfg"
    )
    assert first.status == "completed"
    assert processed == [1, 2]

    # Unfollow artist 200 and follow artist 300 before the next batch.
    api.following = ["100", "300"]
    api.artist_works["300"] = [3]

    second = _service(api, repository, _executor(), options, processed).run(
        account_id="42", config_key="cfg"
    )
    assert second.status == "completed"
    assert processed == [1, 2, 3]
    # Artist 200 was crawled once before the unfollow and never again after.
    assert api.user_calls.count("200") == 1

    run = repository.find_latest_run("42", "cfg")
    states = {artist.artist_id: artist.state for artist in repository.list_artists(run.run_id)}
    assert states["200"] == "unfollowed"
    repository.close()


def test_resume_completed_run_sweeps_new_works_without_recrawl(tmp_path: Path) -> None:
    api = MutableFakeApi({"100": [1, 2]})
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    processed = []
    options = FollowCrawlOptions(
        start_mode=START_RESUME,
        max_artists_per_batch=10,
        max_queued_items=100,
    )

    first = _service(api, repository, _executor(), options, processed).run(
        account_id="42", config_key="cfg"
    )
    assert first.status == "completed"
    assert processed == [1, 2]

    # The artist publishes a new work; artist pages are newest-first.
    api.artist_works["100"] = [3, 1, 2]

    second = _service(api, repository, _executor(), options, processed).run(
        account_id="42", config_key="cfg"
    )
    assert second.status == "completed"
    assert processed == [1, 2, 3]
    # One full crawl plus one incremental page-1 probe — never a re-crawl.
    assert api.user_calls == ["100", "100"]
    repository.close()


def test_budget_pause_resumes_inside_artist_page_without_reprocessing(tmp_path: Path) -> None:
    api = FakeApi({"100": [1, 2, 3]})
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    processed = []
    first_executor = _executor(budget=4)

    def first_process(illust, _fetched):
        first_executor.call(f"detail {illust.id}", lambda: {"ok": True})
        processed.append(illust.id)
        return SimpleNamespace(queued=1)

    first_service = FollowCrawlService(
        api=api,
        repository=repository,
        request_executor=first_executor,
        options=FollowCrawlOptions(max_artists_per_batch=10, max_queued_items=100),
        process_illust=first_process,
        should_stop=lambda: False,
        log=lambda _message: None,
    )
    first = first_service.run(account_id="42", config_key="cfg")
    assert first.status == "paused"
    assert processed == [1, 2]

    second_executor = _executor()

    def second_process(illust, _fetched):
        second_executor.call(f"detail {illust.id}", lambda: {"ok": True})
        processed.append(illust.id)
        return SimpleNamespace(queued=1)

    second_service = FollowCrawlService(
        api=api,
        repository=repository,
        request_executor=second_executor,
        options=FollowCrawlOptions(max_artists_per_batch=10, max_queued_items=100),
        process_illust=second_process,
        should_stop=lambda: False,
        log=lambda _message: None,
    )
    second = second_service.run(account_id="42", config_key="cfg")

    assert second.status == "completed"
    assert processed == [1, 2, 3]
    # The resume re-syncs the follow list once; no artist is re-crawled.
    assert api.following_calls == 2
    repository.close()

