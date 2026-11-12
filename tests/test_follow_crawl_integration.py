"""Adapter-level contracts for the main PixivFetcher follow-all mode."""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from types import SimpleNamespace

from core.pixiv_fetcher import FetchConfig, PixivFetcher


def test_fetcher_follow_all_creates_checkpoint_and_queues_download(tmp_path: Path) -> None:
    illust = SimpleNamespace(
        id=9001,
        title="original",
        total_bookmarks=10,
        illust_ai_type=0,
        create_date="2026-07-28T00:00:00+00:00",
        user=SimpleNamespace(id=100, name="Artist 100"),
        image_urls=SimpleNamespace(large="https://img/9001.jpg"),
    )

    class Api:
        user_id = "42"

        def user_following(self, *_args, **_kwargs):
            preview = SimpleNamespace(user=SimpleNamespace(id=100, name="Artist 100"))
            return SimpleNamespace(user_previews=[preview], next_url=None)

        def user_illusts(self, *_args, **_kwargs):
            return SimpleNamespace(illusts=[illust], next_url=None)

        def illust_detail(self, _illust_id):
            return SimpleNamespace(illust=illust)

        @staticmethod
        def parse_qs(_url):
            return {}

    stats = {
        "fetched": 0,
        "skipped": 0,
        "low_bookmarks": 0,
        "skipped_downloaded": 0,
        "requeued_incomplete": 0,
    }
    task_queue = queue.Queue()
    lock = threading.Lock()
    config = FetchConfig(
        save_dir=str(tmp_path / "images"),
        mode="follow_all",
        original=False,
        api_delay=0,
        max_count=10,
        follow_batch_artists=10,
        follow_api_budget=20,
        crawl_state_path=str(tmp_path / "follow_state.sqlite3"),
    )
    fetcher = PixivFetcher(Api(), config, stop_event=threading.Event())

    queued = fetcher.fetch_by_mode(
        "follow_all",
        task_queue,
        stats,
        lock,
        lambda: None,
        set(),
    )

    assert queued == 1
    assert task_queue.get_nowait().illust_id == 9001
    assert stats["follow_run_status"] == "completed"
    assert stats["follow_api_requests"] == 3
    assert (tmp_path / "follow_state.sqlite3").exists()

