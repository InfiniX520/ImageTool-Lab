"""Durable download outbox contracts."""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import gui.pixiv_downloader_runtime_mixin as runtime_mixin
from core.download_task_store import DownloadTaskStore, DurableDownloadQueue
from core.models import DownloadTask


def _task(tmp_path, illust_id: str = "42") -> DownloadTask:
    return DownloadTask(
        url=f"https://i.pximg.net/img-original/{illust_id}_p0.jpg",
        save_path=str(tmp_path / f"{illust_id}_p0.jpg"),
        illust_id=illust_id,
        log_dir=str(tmp_path),
        is_ai=False,
        bookmarks=10,
        create_date="2026-07-29",
        page_index=0,
        title="title",
        artist_id="7",
        artist_name="artist",
        original_urls=(
            f"https://i.pximg.net/img-original/{illust_id}_p0.jpg",
            f"https://i.pximg.net/img-original/{illust_id}_p1.jpg",
        ),
        known_page_count=2,
        download_root=str(tmp_path),
        folder_by_user=True,
        original_enabled=True,
        multi_page=True,
    )


def test_failed_task_survives_reopen_and_completed_task_is_removed(tmp_path) -> None:
    task = _task(tmp_path)
    store = DownloadTaskStore(tmp_path / "logs")
    store.enqueue(task)
    store.mark_running(task)
    store.mark_failed(task, "temporary network error")
    assert store.count() == 1
    assert store.integrity_check() == "ok"
    store.close()

    reopened = DownloadTaskStore(tmp_path / "logs")
    recovered = reopened.list_retryable()
    assert len(recovered) == 1
    assert recovered[0].original_urls == task.original_urls
    assert recovered[0].known_page_count == 2
    assert recovered[0].download_root == str(tmp_path)
    assert recovered[0].original_enabled is True
    reopened.complete(recovered[0])
    assert reopened.count() == 0
    reopened.close()


def test_durable_queue_persists_before_publish_and_deduplicates(tmp_path) -> None:
    store = DownloadTaskStore(tmp_path / "logs")
    queue = DurableDownloadQueue(maxsize=2, store=store)
    task = _task(tmp_path)

    assert queue.put(task) is True
    assert store.count() == 1
    assert queue.put(task) is False
    assert queue.qsize() == 1
    assert queue.get_nowait() == task
    queue.task_done()
    store.close()


def test_gui_failure_is_recovered_before_next_fetch(monkeypatch, tmp_path) -> None:
    task = _task(tmp_path)
    calls: list[str] = []

    class Root:
        @staticmethod
        def after(_delay, callback):
            callback()

    class Gui(runtime_mixin.PixivDownloaderRuntimeMixin):
        def __init__(self):
            self.root = Root()
            self.stop_event = threading.Event()
            self.log_queue = queue.Queue()
            self.status_label = SimpleNamespace(config=lambda **_kwargs: None)
            self.api = object()
            self.finished = False

        def _downloaded_ids_path(self):
            return tmp_path / "downloaded_ids.txt"

        def _finish(self):
            self.finished = True

    class FirstFetcher:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def fetch_by_mode(_mode, download_queue, *_args):
            download_queue.put(task)

    class EmptyFetcher:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def fetch_by_mode(*_args):
            return 0

    config = SimpleNamespace(
        save_dir=str(tmp_path),
        crawl_state_path=str(tmp_path / "logs" / "follow.sqlite3"),
        folder_by_user=False,
        original=True,
        multi_page=True,
        auth_cookie="session=test",
        user_agent="test",
    )
    monkeypatch.setattr(runtime_mixin, "close_current_http_session", lambda: None)
    monkeypatch.setattr(runtime_mixin, "flush_pending_markdown", lambda: None)
    monkeypatch.setattr(runtime_mixin, "clear_record_cache", lambda: None)
    monkeypatch.setattr(runtime_mixin, "PixivFetcher", FirstFetcher)
    monkeypatch.setattr(runtime_mixin, "download_image", lambda *_args, **_kwargs: False)

    Gui()._download_task(config, max_workers=1, delay=0, skip_downloaded=False, mode="follow_all")
    store = DownloadTaskStore(tmp_path / "logs")
    assert store.count() == 1
    store.close()

    monkeypatch.setattr(runtime_mixin, "PixivFetcher", EmptyFetcher)

    def succeed(*_args, **kwargs):
        calls.append(str(kwargs["illust_id"]))
        return True

    monkeypatch.setattr(runtime_mixin, "download_image", succeed)
    Gui()._download_task(config, max_workers=1, delay=0, skip_downloaded=False, mode="follow_all")
    assert calls == ["42"]
    reopened = DownloadTaskStore(tmp_path / "logs")
    assert reopened.count() == 0
    reopened.close()
