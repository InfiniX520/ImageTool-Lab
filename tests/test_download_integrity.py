#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 download-integrity regressions.

These tests pin the conservative contract introduced for the download layer:

* HTTP 200 error pages (HTML/JSON/XML), unsupported formats and explicit error
  MIME types must never be saved as images.
* A supported image signature and a consistent transfer length are required.
* Existing files are not truncated before the response is confirmed to be an
  allowed image.
* Cross-call Range resume is disabled in P1: no Range header may be sent and an
  unsolicited 206 must be rejected without appending to the old file.
* Cancellation is immediate before the request, during transfer and during the
  retry backoff.
* ``download_service`` no longer skips a same-sized local file on its own.
"""

from __future__ import annotations

import struct
import threading
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import core.download_engine as engine
import core.download_service as download_service
from core.download_engine import DownloadStopped, download_file

try:  # pragma: no cover - pytest prepend mode puts ``tests`` on sys.path
    from image_fixtures import (
        GIF_BYTES,
        JPEG_BYTES,
        PNG_BYTES,
        WEBP_SIGNATURE_BYTES,
    )
except ImportError:  # pragma: no cover - importlib/package execution fallback
    from tests.image_fixtures import (
        GIF_BYTES,
        JPEG_BYTES,
        PNG_BYTES,
        WEBP_SIGNATURE_BYTES,
    )


def _response(
    payload: bytes,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    chunk_size: int | None = None,
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = dict(headers or {})
    if chunk_size is None:
        chunks = [payload]
    else:
        chunks = [
            payload[index : index + chunk_size]
            for index in range(0, len(payload), chunk_size)
        ]
    response.iter_content = lambda _size: iter(chunks)
    response.__enter__ = lambda self: self
    response.__exit__ = lambda self, *args: None
    response.raise_for_status = MagicMock()
    return response


def _image_headers(
    payload: bytes, content_type: str | None = "image/png", **extra: str
) -> dict[str, str]:
    headers = {"Content-Length": str(len(payload))}
    if content_type:
        headers["Content-Type"] = content_type
    headers.update(extra)
    return headers


def _download(url: str, save_path: Path, **kwargs) -> tuple[bool, int]:
    return download_file(url, str(save_path), {}, **kwargs)


def test_png_fixture_has_valid_chunk_crcs_iend_and_scanline() -> None:
    """Independently validate the PNG sample used by the download tests."""
    data = PNG_BYTES
    assert data.startswith(b"\x89PNG\r\n\x1a\n")

    chunks: list[tuple[bytes, bytes]] = []
    offset = 8
    while offset < len(data):
        assert offset + 12 <= len(data)
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + length
        crc_end = payload_end + 4
        assert crc_end <= len(data)
        payload = data[payload_start:payload_end]
        stored_crc = struct.unpack(">I", data[payload_end:crc_end])[0]
        assert (zlib.crc32(chunk_type + payload) & 0xFFFFFFFF) == stored_crc
        chunks.append((chunk_type, payload))
        offset = crc_end

    assert offset == len(data)
    chunk_types = [chunk_type for chunk_type, _payload in chunks]
    assert chunk_types[0] == b"IHDR"
    assert chunk_types[-1] == b"IEND"
    assert b"IDAT" in chunk_types
    assert chunks[-1][1] == b""

    width, height, bit_depth, color_type, compression, filtering, interlace = (
        struct.unpack(">IIBBBBB", chunks[0][1])
    )
    assert (width, height) == (1, 1)
    assert bit_depth == 8
    assert color_type == 6  # RGBA
    assert (compression, filtering, interlace) == (0, 0, 0)

    idat = b"".join(payload for chunk_type, payload in chunks if chunk_type == b"IDAT")
    scanline = zlib.decompress(idat)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    row_bytes = 1 + width * channels
    assert len(scanline) == height * row_bytes == 5


@pytest.mark.parametrize("content_type", [None, "text/html; charset=utf-8"])
def test_http_200_html_is_rejected_without_touching_existing_file(
    tmp_path: Path, content_type: str | None
) -> None:
    payload = b"<!DOCTYPE html><html><body>login required</body></html>"
    target = tmp_path / "existing.png"
    old_bytes = b"old-but-not-an-image"
    target.write_bytes(old_bytes)
    response = _response(payload, headers=_image_headers(payload, content_type))

    with patch("core.download_engine.request_get", return_value=response):
        result = _download("https://img.example.com/x.png", target, retries=1)

    assert result == (False, 0)
    assert target.read_bytes() == old_bytes


@pytest.mark.parametrize(
    ("payload", "content_type"),
    [
        (b'\xef\xbb\xbf  {"error": "login required"}', None),
        (b'\xef\xbb\xbf<?xml version="1.0"?><error>login required</error>', None),
        (b'\n\t<?xml version="1.0"?><error>login required</error>', "application/xml"),
        (b'{"error": "login required"}', "application/json"),
        (b"\n  <html>login required</html>", None),
    ],
)
def test_json_and_xml_error_bodies_are_rejected(
    tmp_path: Path, payload: bytes, content_type: str | None
) -> None:
    target = tmp_path / "existing.jpg"
    target.write_bytes(b"old")
    response = _response(payload, headers=_image_headers(payload, content_type))

    with patch("core.download_engine.request_get", return_value=response):
        result = _download("https://img.example.com/x.jpg", target, retries=1)

    assert result == (False, 0)
    assert target.read_bytes() == b"old"


@pytest.mark.parametrize(
    ("payload", "content_type"),
    [
        (PNG_BYTES, None),
        (PNG_BYTES, "image/png"),
        (JPEG_BYTES, "image/jpeg"),
        (GIF_BYTES, "image/gif"),
        (WEBP_SIGNATURE_BYTES, "image/webp"),
        (PNG_BYTES, "application/octet-stream"),
    ],
)
def test_supported_signature_payloads_are_saved_verbatim_across_tiny_chunks(
    tmp_path: Path, payload: bytes, content_type: str | None
) -> None:
    target = tmp_path / "out.bin"
    response = _response(payload, headers=_image_headers(payload, content_type), chunk_size=1)

    with patch("core.download_engine.request_get", return_value=response):
        result = _download("https://img.example.com/x", target, retries=1)

    assert result == (True, len(payload))
    assert target.read_bytes() == payload


def test_same_size_existing_file_is_revalidated_and_replaced(tmp_path: Path) -> None:
    target = tmp_path / "same-size.png"
    target.write_bytes(b"\x00" * len(PNG_BYTES))
    response = _response(PNG_BYTES, headers=_image_headers(PNG_BYTES, "image/png"))
    captured: dict[str, object] = {}

    def fake_request_get(url, **kwargs):
        captured.update(kwargs)
        return response

    with patch("core.download_engine.request_get", side_effect=fake_request_get):
        result = _download(
            "https://img.example.com/x.png",
            target,
            retries=1,
            expected_size=len(PNG_BYTES),
            resume_enabled=True,
        )

    assert result == (True, len(PNG_BYTES))
    assert captured["headers"]["Accept-Encoding"] == "identity"
    assert "Range" not in captured["headers"]
    assert target.read_bytes() == PNG_BYTES


def test_same_size_local_file_does_not_hide_an_error_body(tmp_path: Path) -> None:
    payload = b"<html><body>login required</body></html>"
    target = tmp_path / "same-size.jpg"
    old_bytes = b"x" * len(payload)
    target.write_bytes(old_bytes)
    response = _response(payload, headers=_image_headers(payload, "text/html"))

    with patch("core.download_engine.request_get", return_value=response):
        result = _download(
            "https://img.example.com/x.jpg",
            target,
            retries=1,
            expected_size=len(payload),
            resume_enabled=True,
        )

    assert result == (False, 0)
    assert target.read_bytes() == old_bytes


def test_partial_existing_file_never_triggers_a_range_request(tmp_path: Path) -> None:
    target = tmp_path / "partial.png"
    target.write_bytes(PNG_BYTES[:10])
    response = _response(PNG_BYTES, headers=_image_headers(PNG_BYTES, "image/png"))
    captured: dict[str, object] = {}

    def fake_request_get(url, **kwargs):
        captured.update(kwargs)
        return response

    with patch("core.download_engine.request_get", side_effect=fake_request_get):
        result = _download(
            "https://img.example.com/x.png",
            target,
            retries=1,
            expected_size=len(PNG_BYTES),
            resume_enabled=True,
        )

    assert result == (True, len(PNG_BYTES))
    assert "Range" not in captured["headers"]
    assert "If-Range" not in captured["headers"]
    assert target.read_bytes() == PNG_BYTES


def test_unsolicited_206_is_rejected_without_appending_to_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "existing.png"
    old_bytes = b"old-complete-file"
    target.write_bytes(old_bytes)
    response = _response(
        PNG_BYTES,
        status_code=206,
        headers=_image_headers(
            PNG_BYTES,
            "image/png",
            **{"Content-Range": f"bytes 0-{len(PNG_BYTES) - 1}/{len(PNG_BYTES)}"},
        ),
    )

    with patch("core.download_engine.request_get", return_value=response):
        result = _download("https://img.example.com/x.png", target, retries=1)

    assert result == (False, 0)
    assert target.read_bytes() == old_bytes


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Length": str(len(PNG_BYTES) + 10), "Content-Type": "image/png"},
        {"Content-Length": str(len(PNG_BYTES) - 10), "Content-Type": "image/png"},
        {"Content-Length": "not-a-number", "Content-Type": "image/png"},
        {"Content-Length": "0", "Content-Type": "image/png"},
        {"Content-Length": f"+{len(PNG_BYTES)}", "Content-Type": "image/png"},
        {"Content-Length": "", "Content-Type": "image/png"},
        {"Content-Length": "   ", "Content-Type": "image/png"},
        {"Content-Length": f"{len(PNG_BYTES)}, {len(PNG_BYTES)}", "Content-Type": "image/png"},
        {
            "Content-Length": f"{len(PNG_BYTES)}, {len(PNG_BYTES) + 1}",
            "Content-Type": "image/png",
        },
        {"Content-Length": "-1", "Content-Type": "image/png"},
        _image_headers(PNG_BYTES, "image/png", **{"Content-Encoding": "gzip"}),
        _image_headers(PNG_BYTES, "image/png", **{"Content-Encoding": "br"}),
    ],
)
def test_length_and_encoding_conflicts_cannot_succeed(
    tmp_path: Path, headers: dict[str, str]
) -> None:
    response = _response(PNG_BYTES, headers=headers)

    with patch("core.download_engine.request_get", return_value=response):
        result = _download("https://img.example.com/x.png", tmp_path / "out.png", retries=1)

    assert result == (False, 0)


def test_expected_size_conflict_with_response_header_fails(tmp_path: Path) -> None:
    response = _response(PNG_BYTES, headers=_image_headers(PNG_BYTES, "image/png"))

    with patch("core.download_engine.request_get", return_value=response):
        result = _download(
            "https://img.example.com/x.png",
            tmp_path / "out.png",
            retries=1,
            expected_size=len(PNG_BYTES) + 1,
        )

    assert result == (False, 0)


def test_missing_content_length_is_checked_against_expected_size(tmp_path: Path) -> None:
    headers = {"Content-Type": "image/png"}
    response = _response(PNG_BYTES, headers=headers)

    with patch("core.download_engine.request_get", return_value=response):
        result = _download(
            "https://img.example.com/x.png",
            tmp_path / "out.png",
            retries=1,
            expected_size=len(PNG_BYTES),
        )

    assert result == (True, len(PNG_BYTES))


def test_chunked_image_without_content_length_succeeds(tmp_path: Path) -> None:
    target = tmp_path / "chunked.png"
    response = _response(PNG_BYTES, headers={"Content-Type": "image/png"}, chunk_size=3)

    with patch("core.download_engine.request_get", return_value=response):
        result = _download("https://img.example.com/x.png", target, retries=1)

    assert result == (True, len(PNG_BYTES))
    assert target.read_bytes() == PNG_BYTES


def test_missing_content_length_with_wrong_expected_size_fails(tmp_path: Path) -> None:
    headers = {"Content-Type": "image/png"}
    response = _response(PNG_BYTES, headers=headers)

    with patch("core.download_engine.request_get", return_value=response):
        result = _download(
            "https://img.example.com/x.png",
            tmp_path / "out.png",
            retries=1,
            expected_size=len(PNG_BYTES) + 1,
        )

    assert result == (False, 0)


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "5"}])
def test_empty_200_body_is_rejected(tmp_path: Path, headers: dict[str, str]) -> None:
    response = _response(b"", headers=headers)

    with patch("core.download_engine.request_get", return_value=response):
        result = _download("https://img.example.com/x.png", tmp_path / "out.png", retries=1)

    assert result == (False, 0)


@pytest.mark.parametrize("status_code", [204, 304])
def test_no_content_and_not_modified_statuses_are_rejected(
    tmp_path: Path, status_code: int
) -> None:
    response = _response(PNG_BYTES, status_code=status_code, headers=_image_headers(PNG_BYTES))

    with patch("core.download_engine.request_get", return_value=response):
        result = _download("https://img.example.com/x.png", tmp_path / "out.png", retries=1)

    assert result == (False, 0)


@pytest.mark.parametrize(
    ("content_type", "payload"),
    [
        ("text/html", PNG_BYTES),
        ("application/json", PNG_BYTES),
        ("image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'),
        ("image/avif", b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00"),
    ],
)
def test_explicit_error_or_unsupported_mime_is_rejected_even_with_disguised_body(
    tmp_path: Path, content_type: str, payload: bytes
) -> None:
    target = tmp_path / "existing.png"
    target.write_bytes(b"old")
    response = _response(payload, headers=_image_headers(payload, content_type))

    with patch("core.download_engine.request_get", return_value=response):
        result = _download("https://img.example.com/x.png", target, retries=1)

    assert result == (False, 0)
    assert target.read_bytes() == b"old"


def test_image_mime_and_signature_mismatch_is_rejected(tmp_path: Path) -> None:
    response = _response(JPEG_BYTES, headers=_image_headers(JPEG_BYTES, "image/png"))

    with patch("core.download_engine.request_get", return_value=response):
        result = _download("https://img.example.com/x.png", tmp_path / "out.png", retries=1)

    assert result == (False, 0)


def test_stop_event_before_download_skips_request(tmp_path: Path) -> None:
    stop_event = threading.Event()
    stop_event.set()

    with patch(
        "core.download_engine.request_get", side_effect=AssertionError("must not request")
    ):
        with pytest.raises(DownloadStopped):
            _download(
                "https://img.example.com/x.png",
                tmp_path / "out.png",
                retries=3,
                stop_event=stop_event,
            )


def test_stop_event_during_transfer_raises_download_stopped(tmp_path: Path) -> None:
    stop_event = threading.Event()

    def chunks(_size):
        yield PNG_BYTES[:20]
        stop_event.set()
        yield PNG_BYTES[20:]

    response = _response(PNG_BYTES, headers=_image_headers(PNG_BYTES, "image/png"))
    response.iter_content = chunks

    with patch("core.download_engine.request_get", return_value=response):
        with pytest.raises(DownloadStopped):
            _download(
                "https://img.example.com/x.png",
                tmp_path / "out.png",
                retries=3,
                stop_event=stop_event,
            )


def test_stop_event_during_retry_backoff_raises_download_stopped(
    monkeypatch, tmp_path: Path
) -> None:
    stop_event = threading.Event()
    calls = {"value": 0}

    def failing_request(*_args, **_kwargs):
        calls["value"] += 1
        raise ConnectionError("temporary network failure")

    def note_retry(_attempt, _total, _exception) -> None:
        stop_event.set()

    def uncancellable_sleep(_seconds) -> None:
        raise AssertionError("backoff must use stop_event.wait when available")

    monkeypatch.setattr(engine, "request_get", failing_request)
    monkeypatch.setattr(engine.time, "sleep", uncancellable_sleep)

    with pytest.raises(DownloadStopped):
        _download(
            "https://img.example.com/x.png",
            tmp_path / "out.png",
            retries=3,
            retry_callback=note_retry,
            stop_event=stop_event,
        )

    assert calls["value"] == 1


def test_download_service_probe_forces_identity_encoding(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "probe.png"
    captured: dict[str, dict[str, str]] = {}

    def fake_length(_url, headers):
        captured["probe_headers"] = dict(headers)
        return len(PNG_BYTES)

    def fake_download(_url, save_path, headers, **_kwargs):
        captured["download_headers"] = dict(headers)
        Path(save_path).write_bytes(PNG_BYTES)
        return True, len(PNG_BYTES)

    monkeypatch.setattr(download_service, "get_remote_content_length", fake_length)
    monkeypatch.setattr(
        download_service.record_manager, "check_record_skip", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        download_service.record_manager, "record_download_event", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(download_service, "download_file", fake_download)

    assert download_service.download_image(
        "https://i.pximg.net/img-original/11_p0.png",
        str(target),
        illust_id="11",
        page_index=0,
        retries=1,
        auth_cookie="session=test",
        download_root=str(tmp_path),
    )
    assert captured["probe_headers"]["Accept-Encoding"] == "identity"
    assert captured["download_headers"]["Accept-Encoding"] == "identity"


def test_download_service_same_size_local_file_cannot_bypass_validation(
    monkeypatch, tmp_path: Path
) -> None:
    payload = b"<html><body>not an image</body></html>"
    target = tmp_path / "42_p0.jpg"
    old_bytes = b"x" * len(payload)
    target.write_bytes(old_bytes)
    requests = {"value": 0}

    monkeypatch.setattr(
        download_service, "get_remote_content_length", lambda *_args, **_kwargs: len(payload)
    )
    monkeypatch.setattr(
        download_service.record_manager, "check_record_skip", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        download_service.record_manager, "record_download_event", lambda *_args, **_kwargs: None
    )

    response = _response(payload, headers=_image_headers(payload, "text/html"))

    def fake_request_get(*_args, **_kwargs):
        requests["value"] += 1
        return response

    monkeypatch.setattr(engine, "request_get", fake_request_get)

    assert (
        download_service.download_image(
            "https://i.pximg.net/img-original/42_p0.jpg",
            str(target),
            illust_id="42",
            page_index=0,
            retries=1,
            resume_enabled=True,
            auth_cookie="session=test",
            download_root=str(tmp_path),
        )
        is False
    )
    assert requests["value"] == 1
    assert target.read_bytes() == old_bytes


def test_download_service_trusted_record_skip_still_short_circuits(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        download_service.record_manager, "check_record_skip", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        download_service.record_manager, "record_download_event", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        download_service,
        "get_remote_content_length",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("trusted record skip must not probe the network")
        ),
    )
    monkeypatch.setattr(
        download_service,
        "download_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("trusted record skip must not download")
        ),
    )

    assert download_service.download_image(
        "https://i.pximg.net/img-original/7_p0.jpg",
        str(tmp_path / "7_p0.jpg"),
        illust_id="7",
        page_index=0,
        resume_enabled=True,
        auth_cookie="session=test",
        download_root=str(tmp_path),
    )
