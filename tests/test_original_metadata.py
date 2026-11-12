"""Original-image metadata reuse, concurrency and partial-file contracts."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import core.download_engine as download_engine
import core.download_service as download_service


def test_app_api_original_urls_bypass_ajax_and_keep_exact_page_urls(monkeypatch, tmp_path: Path) -> None:
    exact_urls = (
        "https://i.pximg.net/img-original/a/123_p0.jpg?source=app0",
        "https://i.pximg.net/img-original/b/123_p1.jpg?source=app1",
    )
    requested: list[str] = []
    monkeypatch.setattr(
        download_service,
        "fetch_original_meta",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Ajax must not run")),
    )
    monkeypatch.setattr(download_service, "get_remote_content_length", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(download_service.record_manager, "record_download_event", lambda *_args, **_kwargs: None)

    def fake_download(url, save_path, *_args, **_kwargs):
        requested.append(url)
        target = Path(save_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"image")
        return True, 5

    monkeypatch.setattr(download_service, "download_file", fake_download)

    assert download_service.download_image(
        "https://i.pximg.net/img-master/123_p0.jpg",
        str(tmp_path / "123_p0.jpg"),
        illust_id="123",
        page_index=0,
        original_enabled=True,
        multi_page=True,
        original_urls=exact_urls,
        known_page_count=2,
        download_root=str(tmp_path),
    )
    assert requested == list(exact_urls)


def test_same_artwork_concurrent_ajax_resolution_is_single_flight(monkeypatch) -> None:
    class ImmediateLimiter:
        def wait(self, _stop_event=None):
            return None

        def report_success(self):
            return None

        def report_429(self, _retry_after=None):
            return 0

    class Response:
        status_code = 200
        headers = {}

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "body": {
                    "pageCount": 1,
                    "urls": {"original": "https://i.pximg.net/img-original/123_p0.jpg"},
                }
            }

    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def fake_get(*_args, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(2)
        return Response()

    monkeypatch.setattr(download_engine, "_AJAX_RATE_LIMITER", ImmediateLimiter())
    monkeypatch.setattr(download_engine, "request_get", fake_get)
    with download_engine._META_CACHE_LOCK:
        download_engine._META_CACHE.clear()
        download_engine._META_REQUEST_FLIGHTS.clear()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(download_engine.fetch_original_meta, "123", cookie="session=test")
            for _ in range(2)
        ]
        assert entered.wait(1)
        release.set()
        results = [future.result(timeout=2) for future in futures]

    assert calls == 1
    assert results[0] == results[1]


def test_nonzero_partial_file_is_repaired_instead_of_skipped(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "partial.jpg"
    target.write_bytes(b"bad")
    calls: list[str] = []
    events: list[str] = []
    monkeypatch.setattr(download_service, "get_remote_content_length", lambda *_args, **_kwargs: 10)
    monkeypatch.setattr(
        download_service.record_manager,
        "record_download_event",
        lambda *args, **_kwargs: events.append(str(args[-1])),
    )

    def fake_download(url, save_path, *_args, **_kwargs):
        calls.append(url)
        Path(save_path).write_bytes(b"x" * 10)
        return True, 10

    monkeypatch.setattr(download_service, "download_file", fake_download)

    assert download_service.download_image(
        "https://i.pximg.net/img-original/321_p0.jpg",
        str(target),
        illust_id="321",
        page_index=0,
        original_enabled=False,
        resume_enabled=True,
        download_root=str(tmp_path),
    )
    assert calls == ["https://i.pximg.net/img-original/321_p0.jpg"]
    assert target.stat().st_size == 10
    assert events[-1] == "下载完成"
