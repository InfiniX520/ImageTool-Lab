"""Characterization tests for strategy dispatch and queued task contracts."""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import core.download_service as download_service
import gui.pixiv_downloader_runtime_mixin as runtime_mixin
import gui.pixiv_multi_filter_runtime_mixin as multi_runtime_mixin
from core.download_engine import (
    APICallCounter,
    DownloadEngine,
    DownloadRequest,
    build_save_path_for_page,
    download_file,
    is_original_image_url,
)
from core.fetch_strategies import MODE_STRATEGIES
from core.models import DownloadTask
from core.multi_filter_fetch import fetch_source
from core.pixiv_fetcher import FetchConfig, PixivFetcher
from core.pixiv_fetcher_queue_mixin import _IllustOutcome

try:  # pragma: no cover - pytest prepend mode puts ``tests`` on sys.path
    from image_fixtures import PNG_BYTES
except ImportError:  # pragma: no cover - importlib/package execution fallback
    from tests.image_fixtures import PNG_BYTES


def _context():
    return queue.Queue(), {}, threading.Lock(), lambda: None


def test_download_task_keeps_legacy_tuple_unpacking() -> None:
    task = DownloadTask(
        "https://img/1.jpg", "out/1.jpg", 1, "out", False, 2,
        "2026-01-01", 0, "title", "7", "artist",
    )
    assert tuple(task) == (
        "https://img/1.jpg", "out/1.jpg", 1, "out", False, 2,
        "2026-01-01", 0, "title", "7", "artist",
    )
    assert len(task) == 11
    assert task[0] == "https://img/1.jpg"
    assert task.original_urls == ()
    assert task.known_page_count == 0
    assert task.download_root == ""
    assert task.original_enabled is None
    config = FetchConfig()
    config.integration_marker = "legacy-dynamic-field"
    assert config.integration_marker == "legacy-dynamic-field"


def test_download_engine_keeps_module_level_download_seam(monkeypatch) -> None:
    called = {}

    def fake_download(**kwargs):
        called.update(kwargs)
        return True

    import core.download_engine as engine_module
    monkeypatch.setattr(engine_module, "download_image", fake_download)
    request = DownloadRequest("url", "path", 1, 0)
    assert DownloadEngine().download(request, retries=1)
    assert called["url"] == "url"


def test_fetcher_dispatches_related_strategy_without_branch_side_effects(monkeypatch) -> None:
    class Api:
        def __init__(self):
            self.calls = []

        def illust_related(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return object()

    api = Api()
    fetcher = PixivFetcher(api, FetchConfig(related_illust_id="123"), stop_event=threading.Event())
    called = {}

    def fake_fetch(*args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs
        return 4

    monkeypatch.setattr(fetcher, "_fetch_and_queue", fake_fetch)
    result = fetcher.fetch_by_mode("related", *_context(), downloaded_ids=set())

    assert result == 4
    assert callable(called["args"][0])
    assert callable(called["args"][1])
    assert "related" in MODE_STRATEGIES


def test_queue_pipeline_emits_named_task_and_updates_stats() -> None:
    illust = SimpleNamespace(
        id=42,
        title="demo",
        total_bookmarks=10,
        illust_ai_type=0,
        create_date="2026-01-01T00:00:00+00:00",
        user=SimpleNamespace(id=7, name="artist"),
        image_urls=SimpleNamespace(large="https://img/42.jpg"),
    )

    class Result:
        illusts = [illust]
        next_url = None

    class Api:
        def illust_follow(self, **kwargs):
            return Result()

        def illust_detail(self, illust_id):
            return SimpleNamespace(illust=illust)

    config = FetchConfig(save_dir="out", mode="follow", original=False, api_delay=0)
    fetcher = PixivFetcher(Api(), config, stop_event=threading.Event())
    task_queue, stats, lock, update = _context()

    result = fetcher.fetch_by_mode("follow", task_queue, stats, lock, update, set())

    assert result == 1
    task = task_queue.get_nowait()
    assert isinstance(task, DownloadTask)
    assert task.illust_id == 42
    assert task.artist_name == "artist"
    assert stats["fetched"] == 1


def test_fetcher_queues_one_artwork_task_for_download_layer_page_expansion() -> None:
    illust = SimpleNamespace(
        id=42,
        meta_pages=[
            SimpleNamespace(image_urls=SimpleNamespace(original="https://img/42_p0.jpg")),
            SimpleNamespace(image_urls=SimpleNamespace(original="https://img/42_p1.jpg")),
        ],
        meta_single_page={},
        image_urls=SimpleNamespace(large="https://img/42-large.jpg"),
    )

    original_fetcher = PixivFetcher(
        object(),
        FetchConfig(original=True, multi_page=True),
        stop_event=threading.Event(),
    )
    preview_fetcher = PixivFetcher(
        object(),
        FetchConfig(original=False, multi_page=True),
        stop_event=threading.Event(),
    )

    assert original_fetcher._get_image_urls(illust) == [("https://img/42_p0.jpg", 0)]
    assert original_fetcher._get_original_download_metadata(illust) == (
        ("https://img/42_p0.jpg", "https://img/42_p1.jpg"),
        2,
    )
    assert preview_fetcher._get_image_urls(illust) == [("https://img/42-large.jpg", 0)]


def test_original_fetcher_labels_large_url_as_probe_not_download_fallback() -> None:
    logs: list[str] = []
    illust = SimpleNamespace(
        id=42,
        meta_pages=[],
        meta_single_page={},
        image_urls=SimpleNamespace(original=None, large="https://i.pximg.net/img-master/42.jpg"),
    )
    fetcher = PixivFetcher(
        object(),
        FetchConfig(original=True),
        log_callback=logs.append,
        stop_event=threading.Event(),
    )

    assert fetcher._get_image_urls(illust) == [("https://i.pximg.net/img-master/42.jpg", 0)]
    assert any("仅提供预览地址" in message and "绝不直接保存" in message for message in logs)


def test_queue_pipeline_skips_legacy_downloaded_id_without_api_detail() -> None:
    illust = SimpleNamespace(id=42, title="demo", total_bookmarks=10, illust_ai_type=0)

    class Result:
        illusts = [illust]
        next_url = None

    class Api:
        def illust_follow(self, **kwargs):
            return Result()

        def illust_detail(self, illust_id):
            raise AssertionError("detail must not be requested for a downloaded id")

    fetcher = PixivFetcher(
        Api(),
        FetchConfig(mode="follow", original=False, api_delay=0),
        stop_event=threading.Event(),
    )
    task_queue, stats, lock, update = _context()

    assert fetcher.fetch_by_mode("follow", task_queue, stats, lock, update, {"42"}) == 0
    assert stats["skipped_downloaded"] == 1
    assert task_queue.empty()


def test_original_multi_page_legacy_id_is_requeued_when_any_page_is_missing(monkeypatch, tmp_path) -> None:
    """A work-level legacy ID must not hide an incomplete multi-page artwork."""

    illust = SimpleNamespace(
        id=42,
        title="demo",
        page_count=3,
        meta_pages=[SimpleNamespace(image_urls=SimpleNamespace(original="https://img/42_p0.jpg"))],
        meta_single_page={},
        image_urls=SimpleNamespace(large="https://img/42-large.jpg"),
        total_bookmarks=10,
        illust_ai_type=0,
        create_date="2026-01-01T00:00:00+00:00",
        user=SimpleNamespace(id=7, name="artist"),
    )

    class Result:
        illusts = [illust]
        next_url = None

    class Api:
        def illust_follow(self, **_kwargs):
            return Result()

        def illust_detail(self, _illust_id):
            return SimpleNamespace(illust=illust)

    monkeypatch.setattr(
        download_service.record_manager,
        "check_record_skip",
        lambda _root, _illust_id, page: page in {0, 1},
    )
    logs: list[str] = []
    fetcher = PixivFetcher(
        Api(),
        FetchConfig(
            save_dir=str(tmp_path),
            mode="follow",
            original=True,
            multi_page=True,
            api_delay=0,
        ),
        log_callback=logs.append,
        stop_event=threading.Event(),
    )
    task_queue, stats, lock, update = _context()

    assert fetcher.fetch_by_mode("follow", task_queue, stats, lock, update, {"42"}) == 1
    assert task_queue.get_nowait().illust_id == 42
    assert any("缺页" in message and "p2" in message for message in logs)


def test_original_multi_page_legacy_id_skips_only_after_every_page_is_verified(monkeypatch, tmp_path) -> None:
    illust = SimpleNamespace(id=42, title="demo", page_count=3, total_bookmarks=10, illust_ai_type=0)

    class Result:
        illusts = [illust]
        next_url = None

    class Api:
        def illust_follow(self, **_kwargs):
            return Result()

        def illust_detail(self, _illust_id):
            raise AssertionError("complete records must skip before detail lookup")

    monkeypatch.setattr(download_service.record_manager, "check_record_skip", lambda *_args: True)
    fetcher = PixivFetcher(
        Api(),
        FetchConfig(
            save_dir=str(tmp_path),
            mode="follow",
            original=True,
            multi_page=True,
            api_delay=0,
        ),
        stop_event=threading.Event(),
    )
    task_queue, stats, lock, update = _context()

    assert fetcher.fetch_by_mode("follow", task_queue, stats, lock, update, {"42"}) == 0
    assert stats["skipped_downloaded"] == 1
    assert task_queue.empty()


def test_original_multi_page_expansion_resolves_each_page_url(monkeypatch, tmp_path) -> None:
    """Recursive page downloads must retain the original URL substitution."""

    requested_urls: list[str] = []

    monkeypatch.setattr(
        download_service,
        "fetch_original_meta",
        lambda *args, **kwargs: {
            "original": "https://i.pximg.net/img-original/123_p0.jpg",
            "page_count": 3,
        },
    )
    monkeypatch.setattr(download_service, "get_remote_content_length", lambda *args, **kwargs: None)
    monkeypatch.setattr(download_service.record_manager, "record_download_event", lambda *args, **kwargs: None)

    def fake_download_file(url, save_path, *args, **kwargs):
        requested_urls.append(url)
        path = Path(save_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image")
        return True, 5

    monkeypatch.setattr(download_service, "download_file", fake_download_file)

    assert download_service.download_image(
        "https://i.pximg.net/img-master/123_p0.jpg",
        str(tmp_path / "123.jpg"),
        illust_id="123",
        page_index=0,
        original_enabled=True,
        multi_page=True,
        auth_cookie="session=test",
        download_root=str(tmp_path),
    )

    assert requested_urls == [
        "https://i.pximg.net/img-original/123_p0.jpg",
        "https://i.pximg.net/img-original/123_p1.jpg",
        "https://i.pximg.net/img-original/123_p2.jpg",
    ]


def test_original_multi_page_resume_audits_each_page_and_repairs_only_the_gap(monkeypatch, tmp_path) -> None:
    requested_pages: list[int] = []
    page_results: list[dict[str, object]] = []
    monkeypatch.setattr(
        download_service,
        "fetch_original_meta",
        lambda *args, **kwargs: {
            "original": "https://i.pximg.net/img-original/42_p0.jpg",
            "page_count": 3,
        },
    )
    monkeypatch.setattr(
        download_service.record_manager,
        "check_record_skip",
        lambda _root, _illust_id, page: page in {0, 1},
    )
    monkeypatch.setattr(download_service.record_manager, "record_download_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(download_service, "get_remote_content_length", lambda *args, **kwargs: None)

    def fake_download_file(url, save_path, _headers, **kwargs):
        page = int(url.rsplit("_p", 1)[1].split(".", 1)[0])
        requested_pages.append(page)
        target = Path(save_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"repaired")
        return True, 8

    monkeypatch.setattr(download_service, "download_file", fake_download_file)

    assert download_service.download_image(
        "https://i.pximg.net/img-original/42_p0.jpg",
        str(tmp_path / "42_p0_date.jpg"),
        illust_id="42",
        page_index=0,
        original_enabled=True,
        multi_page=True,
        resume_enabled=True,
        auth_cookie="session=test",
        download_root=str(tmp_path),
        page_result_callback=page_results.append,
    )

    assert requested_pages == [2]
    assert [(item["page_index"], item["status"]) for item in page_results] == [
        (0, "skipped_record"),
        (1, "skipped_record"),
        (2, "downloaded"),
    ]


def test_download_file_logs_recoverable_ssl_error_as_retry_warning(monkeypatch, tmp_path) -> None:
    import requests

    import core.download_engine as engine

    class Response:
        status_code = 200
        headers = {
            "Content-Length": str(len(PNG_BYTES)),
            "Content-Type": "image/png",
        }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, _chunk_size):
            yield PNG_BYTES

    calls = 0

    def flaky_request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise requests.exceptions.SSLError("temporary EOF")
        return Response()

    logs: list[str] = []
    retries: list[tuple[int, int]] = []
    monkeypatch.setattr(engine, "request_get", flaky_request)
    monkeypatch.setattr(engine.time, "sleep", lambda _seconds: None)

    result = download_file(
        "https://i.pximg.net/img-original/42_p0.jpg",
        str(tmp_path / "42.jpg"),
        {},
        retries=3,
        log_callback=logs.append,
        retry_callback=lambda attempt, total, _exc: retries.append((attempt, total)),
    )

    assert result == (True, len(PNG_BYTES))
    assert calls == 2
    assert retries == [(1, 3)]
    assert any("传输中断，准备重试 (1/3)" in message for message in logs)
    assert not any("下载失败 (1/3)" in message for message in logs)


def test_page_path_replaces_template_page_token_in_any_position() -> None:
    source = r"E:\images\123_title_7_p0_20260720.jpg"

    assert build_save_path_for_page(source, 4) == r"E:\images\123_title_7_p4_20260720.jpg"
    assert build_save_path_for_page(r"E:\images\123_title.jpg", 2) == r"E:\images\123_title_p2.jpg"


def test_original_mode_never_silently_falls_back_to_master(monkeypatch, tmp_path) -> None:
    """A failed Ajax lookup must not turn an original request into a preview."""

    monkeypatch.setattr(
        download_service,
        "fetch_original_meta",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ajax unavailable")),
    )
    monkeypatch.setattr(
        download_service.record_manager,
        "record_download_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        download_service,
        "download_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("preview must not download")),
    )

    assert is_original_image_url("https://i.pximg.net/img-original/img/1_p0.jpg")
    assert not is_original_image_url("https://i.pximg.net/c/600x1200_90/img-master/img/1_p0_master1200.jpg")
    assert not download_service.download_image(
        "https://i.pximg.net/c/600x1200_90/img-master/img/1_p0_master1200.jpg",
        str(tmp_path / "1.jpg"),
        illust_id="1",
        original_enabled=True,
        auth_cookie="session=test",
        download_root=str(tmp_path),
    )


def test_original_meta_rejects_non_original_ajax_url(monkeypatch, tmp_path) -> None:
    """A malformed/preview URL from Ajax must fail closed in original mode."""

    monkeypatch.setattr(
        download_service,
        "fetch_original_meta",
        lambda *args, **kwargs: {
            "original": "https://i.pximg.net/img-master/img/1_p0_master1200.jpg",
            "page_count": 1,
        },
    )
    monkeypatch.setattr(
        download_service.record_manager,
        "record_download_event",
        lambda *args, **kwargs: None,
    )

    assert not download_service.download_image(
        "https://i.pximg.net/img-master/img/1_p0_master1200.jpg",
        str(tmp_path / "1.jpg"),
        illust_id="1",
        original_enabled=True,
        auth_cookie="session=test",
        download_root=str(tmp_path),
    )


def test_probe_page_limit_counts_api_pages_not_artwork_pages(monkeypatch) -> None:
    """A multi-page artwork must not prematurely exhaust ``max_pages``."""

    class Result:
        def __init__(self, marker, next_url):
            self.illusts = [SimpleNamespace(id=marker)]
            self.next_url = next_url

    class Api:
        def parse_qs(self, url):
            return {"page": url}

    fetcher = PixivFetcher(
        Api(),
        FetchConfig(max_count=10, api_delay=0),
        stop_event=threading.Event(),
    )
    results = iter([Result(1, "next-1"), Result(2, None)])
    processed: list[int] = []

    monkeypatch.setattr(fetcher, "_load_initial_result", lambda callback: next(results))
    monkeypatch.setattr(fetcher, "_should_stop", lambda: False)
    monkeypatch.setattr(fetcher, "_api_sleep", lambda: None)
    monkeypatch.setattr(fetcher, "api_counter", SimpleNamespace(increment=lambda: None))

    def fake_process(illust, *args):
        processed.append(illust.id)
        # Simulate the last image page index of a five-page artwork.  This
        # value must not be used as the API pagination counter.
        return _IllustOutcome(fetched=1, queued=1, page_index=4)

    monkeypatch.setattr(fetcher, "_process_illust", fake_process)
    next_calls = []

    def next_fetch(result, query):
        next_calls.append(query)
        return next(results)

    task_queue = queue.Queue()
    result = fetcher._fetch_and_queue(
        lambda: None,
        next_fetch,
        task_queue,
        {},
        threading.Lock(),
        lambda: None,
        set(),
        max_pages=2,
    )

    assert result == 2
    assert processed == [1, 2]
    assert len(next_calls) == 1


def test_api_pause_honours_cancellation_event(monkeypatch) -> None:
    stop_event = threading.Event()
    stop_event.set()
    counter = APICallCounter(stop_event=stop_event)
    counter.threshold = 1
    monkeypatch.setattr(
        "core.download_engine.time.sleep",
        lambda seconds: (_ for _ in ()).throw(AssertionError("blocking sleep used")),
    )

    assert counter.increment() is True
    assert counter.count == 0


def test_multi_filter_user_source_paginates_each_user() -> None:
    first = SimpleNamespace(
        illusts=[SimpleNamespace(id=1)],
        next_url="page-2",
    )
    second = SimpleNamespace(
        illusts=[SimpleNamespace(id=2)],
        next_url=None,
    )

    class Api:
        def user_illusts(self, *args, **kwargs):
            return first if args else second

        def parse_qs(self, url):
            assert url == "page-2"
            return {"offset": 30}

    dialog = SimpleNamespace(
        api=Api(),
        stop_event=threading.Event(),
        log_queue=queue.Queue(),
        api_counter=SimpleNamespace(increment=lambda: None),
    )

    result = fetch_source(dialog, "user", 2, snapshot={"user_ids": "7"})
    assert [item.id for item in result] == [1, 2]


def test_multi_filter_download_reuses_original_and_multi_page_settings(monkeypatch, tmp_path) -> None:
    captured = {}
    illust = SimpleNamespace(
        id=42,
        title="demo",
        meta_pages=[],
        meta_single_page={"original_image_url": "https://i.pximg.net/img-original/42_p0.jpg"},
        image_urls=SimpleNamespace(large="https://i.pximg.net/img-master/42.jpg"),
        illust_ai_type=0,
        total_bookmarks=10,
        create_date="2026-07-20T00:00:00+09:00",
        user=SimpleNamespace(id=7, name="artist"),
    )

    class Api:
        def illust_detail(self, _illust_id):
            return SimpleNamespace(illust=illust)

    class Root:
        def after(self, _delay, callback):
            callback()

    class Dialog(multi_runtime_mixin.MultiFilterRuntimeMixin):
        def __init__(self):
            self.api = Api()
            self.root = Root()
            self.stop_event = threading.Event()
            self.log_queue = queue.Queue()
            self.status_label = SimpleNamespace(config=lambda **_kwargs: None)
            self.config = {
                "threads": "2",
                "delay": "0",
                "api_delay": "0",
                "original": True,
                "multi_page": True,
                "folder_by_user": True,
                "skip_downloaded": True,
                "name_template": "{id}_{title}_p{page}",
                "auth_cookie": "session=test",
            }

    def fake_download_image(*args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(multi_runtime_mixin, "download_image", fake_download_image)
    Dialog()._download_all([illust], str(tmp_path))

    assert captured["original_enabled"] is True
    assert captured["multi_page"] is True
    assert captured["folder_by_user"] is True
    assert captured["resume_enabled"] is True
    assert captured["auth_cookie"] == "session=test"
    assert captured["original_urls"] == (
        "https://i.pximg.net/img-original/42_p0.jpg",
    )
    assert captured["known_page_count"] == 1


def test_gui_worker_failure_unblocks_bounded_producer(monkeypatch, tmp_path) -> None:
    """A failed consumer must not leave the producer blocked on a full queue."""

    class Value:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

    class Root:
        def after(self, *_args, **_kwargs):
            return None

    class FakeFetcher:
        def __init__(self, api, config, log_callback, stop_event):
            self.stop_event = stop_event

        def fetch_by_mode(self, mode, download_queue, stats, stats_lock, update_status, downloaded_ids):
            del mode, stats, stats_lock, update_status, downloaded_ids
            download_queue.put(
                DownloadTask(
                    "https://img/1.jpg",
                    str(tmp_path / "1.jpg"),
                    1,
                    str(tmp_path),
                    False,
                    0,
                    None,
                    0,
                    "title",
                    "7",
                    "artist",
                )
            )

    class Gui(runtime_mixin.PixivDownloaderRuntimeMixin):
        def __init__(self):
            self.stop_event = threading.Event()
            self.log_queue = queue.Queue()
            self.root = Root()
            self.status_label = SimpleNamespace(config=lambda **kwargs: None)
            self.skip_downloaded_var = Value(False)
            self.api = object()
            self.mode_var = Value("follow")
            self.auth_cookie = ""
            self.user_agent = ""
            self.finished = False

        def _downloaded_ids_path(self):
            return tmp_path / "downloaded_ids.txt"

        def _finish(self):
            self.finished = True

    monkeypatch.setattr(runtime_mixin, "PixivFetcher", FakeFetcher)
    monkeypatch.setattr(runtime_mixin, "download_image", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(runtime_mixin, "close_current_http_session", lambda: None)

    gui = Gui()
    config = SimpleNamespace(save_dir=str(tmp_path), folder_by_user=False, original=False, multi_page=False)
    gui._download_task(config, max_workers=1, delay=0)

    assert gui.finished is True
    assert gui.stop_event.is_set()


def test_gui_completion_summary_separates_artworks_pages_and_recovered_retries(monkeypatch, tmp_path) -> None:
    class Value:
        def get(self):
            return False

    class Root:
        def after(self, _delay, callback):
            callback()

    class FakeFetcher:
        def __init__(self, api, config, log_callback, stop_event):
            pass

        def fetch_by_mode(self, _mode, download_queue, _stats, _lock, _update, _downloaded_ids):
            download_queue.put(
                DownloadTask(
                    "https://img/1.jpg",
                    str(tmp_path / "1.jpg"),
                    1,
                    str(tmp_path),
                    False,
                    0,
                    None,
                    0,
                    "title",
                    "7",
                    "artist",
                )
            )

    class Gui(runtime_mixin.PixivDownloaderRuntimeMixin):
        def __init__(self):
            self.stop_event = threading.Event()
            self.log_queue = queue.Queue()
            self.root = Root()
            self.status_label = SimpleNamespace(config=lambda **_kwargs: None)
            self.skip_downloaded_var = Value()
            self.api = object()
            self.finished = False

        def _downloaded_ids_path(self):
            return tmp_path / "downloaded_ids.txt"

        def _finish(self):
            self.finished = True

    def fake_download_image(*_args, **kwargs):
        callback = kwargs["page_result_callback"]
        callback({"status": "downloaded", "retried": True})
        callback({"status": "skipped_record", "retried": False})
        return True

    monkeypatch.setattr(runtime_mixin, "PixivFetcher", FakeFetcher)
    monkeypatch.setattr(runtime_mixin, "download_image", fake_download_image)
    monkeypatch.setattr(runtime_mixin, "close_current_http_session", lambda: None)
    monkeypatch.setattr(runtime_mixin, "flush_pending_markdown", lambda: None)
    monkeypatch.setattr(runtime_mixin, "clear_record_cache", lambda: None)

    gui = Gui()
    config = SimpleNamespace(
        save_dir=str(tmp_path),
        folder_by_user=False,
        original=True,
        multi_page=True,
        auth_cookie="session=test",
        user_agent="test",
    )
    gui._download_task(config, max_workers=1, delay=0, skip_downloaded=False, mode="follow")
    messages = []
    while not gui.log_queue.empty():
        messages.append(gui.log_queue.get_nowait())
    summary = next(message for message in messages if "完成!" in message)

    assert "完成作品: 1" in summary
    assert "新增图片: 1" in summary
    assert "记录/文件跳过: 1" in summary
    assert "最终失败图片: 0" in summary
    assert "重试恢复: 1" in summary
