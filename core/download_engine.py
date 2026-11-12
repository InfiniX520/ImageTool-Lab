#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载引擎模块：负责请求头构建、原图元数据解析与底层文件下载。"""

from __future__ import annotations

import hashlib
import random
import re
import string
import threading
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

# Kept as a module attribute for legacy monkeypatches; ``request_get`` uses
# the same requests module when deciding whether to use the pooled transport.
import requests

from core.http_client import request_get
from utils.logger import log_message


class DownloadStopped(Exception):
    """用户主动停止下载时由 download_file 抛出，downloader 据此区分中止与失败。"""


# ============================================================
# 常量配置
# ============================================================

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DEFAULT_REFERER = "https://www.pixiv.net/"

AJAX_ACCEPT = "application/json"
IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"

MAX_FILENAME_LENGTH = 200
DOWNLOAD_CHUNK_SIZE = 64 * 1024

API_CALL_THRESHOLD_MIN = 50
API_CALL_THRESHOLD_MAX = 100
API_PAUSE_MIN = 5
API_PAUSE_MAX = 15
META_CACHE_MAX_ITEMS = 1024

_META_CACHE_LOCK = threading.Lock()
_META_CACHE: "OrderedDict[str, dict[str, object]]" = OrderedDict()
_META_REQUEST_FLIGHTS: dict[str, "_MetaRequestFlight"] = {}


class _MetaRequestFlight:
    """One in-flight Ajax request shared by workers resolving the same work."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.response = None
        self.error: BaseException | None = None


class _AjaxRateLimiter:
    """Thread-safe rate limiter for Pixiv Ajax endpoint calls.

    Enforces a minimum interval between requests and dynamically adjusts
    based on 429 responses to avoid triggering further rate limits.
    """

    def __init__(self, min_interval: float = 1.5, max_interval: float = 60.0):
        self._lock = threading.Lock()
        self._min_interval = min_interval
        self._max_interval = max_interval
        self._current_interval = min_interval
        self._last_call_time: float | None = None
        self._consecutive_429 = 0
        self._blocked_until = 0.0

    def wait(self, stop_event: threading.Event | None = None) -> None:
        """Wait without holding the limiter lock and remain cancellation-aware."""
        while True:
            with self._lock:
                now = time.monotonic()
                interval_ready = (
                    self._last_call_time + self._current_interval
                    if self._last_call_time is not None
                    else now
                )
                ready_at = max(interval_ready, self._blocked_until)
                delay = ready_at - now
                if delay <= 0:
                    self._last_call_time = now
                    return
            if stop_event is not None:
                if stop_event.wait(delay):
                    raise DownloadStopped()
            else:
                time.sleep(delay)

    def report_success(self) -> None:
        with self._lock:
            self._consecutive_429 = 0
            self._current_interval = max(self._min_interval, self._current_interval * 0.9)

    def report_429(self, retry_after: float | None = None) -> float:
        """Report a 429 response and return recommended wait time."""
        with self._lock:
            self._consecutive_429 += 1
            if retry_after is not None:
                wait_time = max(1.0, retry_after)
            else:
                wait_time = min(self._max_interval, 5.0 * (2 ** (self._consecutive_429 - 1)))
            self._current_interval = min(self._max_interval, self._current_interval * 1.5)
            wait_time += random.uniform(0, 1.0)
            self._blocked_until = max(self._blocked_until, time.monotonic() + wait_time)
            return wait_time


_AJAX_RATE_LIMITER = _AjaxRateLimiter()


def _request_meta_response(
    cache_key: str,
    endpoint: str,
    headers: dict[str, str],
    *,
    stop_event: threading.Event | None,
):
    """Coalesce concurrent Ajax calls for the same artwork into one request."""
    with _META_CACHE_LOCK:
        flight = _META_REQUEST_FLIGHTS.get(cache_key)
        leader = flight is None
        if leader:
            flight = _MetaRequestFlight()
            _META_REQUEST_FLIGHTS[cache_key] = flight

    if not leader:
        while not flight.done.wait(0.25):
            if stop_event is not None and stop_event.is_set():
                raise DownloadStopped()
        if flight.error is not None:
            raise RuntimeError("共享 Ajax 请求失败") from flight.error
        return flight.response

    try:
        flight.response = request_get(endpoint, headers=headers, timeout=30)
        return flight.response
    except BaseException as exc:
        flight.error = exc
        raise
    finally:
        flight.done.set()
        with _META_CACHE_LOCK:
            if _META_REQUEST_FLIGHTS.get(cache_key) is flight:
                _META_REQUEST_FLIGHTS.pop(cache_key, None)


def _parse_retry_after(response) -> float | None:
    """Parse Retry-After header from a 429 response."""
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(str(raw))
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


@dataclass
class DownloadRequest:
    """Compatibility DTO retained for callers of the v2.0 engine wrapper."""

    url: str
    save_path: str
    illust_id: int | str
    page_index: int
    title: str = ""
    artist_id: str = ""
    artist_name: str = ""
    is_ai: bool | None = None
    bookmarks: int | None = None
    create_date: str | None = None


class DownloadEngine:
    """Synchronous compatibility adapter over ``download_service``."""

    def download(self, request: DownloadRequest, **kwargs: object) -> bool:
        return download_image(
            url=request.url,
            save_path=request.save_path,
            illust_id=request.illust_id,
            page_index=request.page_index,
            title=request.title,
            artist_id=request.artist_id,
            artist_name=request.artist_name,
            is_ai=request.is_ai,
            bookmarks=request.bookmarks,
            create_date=request.create_date,
            **kwargs,
        )


def download_image(*args: object, **kwargs: object) -> bool:
    """Lazy compatibility wrapper preserving the old engine monkeypatch seam."""

    from core.download_service import download_image as _download_image

    return _download_image(*args, **kwargs)


# ============================================================
# 请求计数器（防封）
# ============================================================


class APICallCounter:
    """API 请求计数器：达到随机阈值后自动暂停，降低风控风险。"""

    def __init__(self, log_callback=None, stop_event: threading.Event | None = None):
        self.count = 0
        self.threshold = random.randint(API_CALL_THRESHOLD_MIN, API_CALL_THRESHOLD_MAX)
        self.log_callback = log_callback
        self.stop_event = stop_event

    def increment(self) -> bool:
        """增加计数，达到阈值时触发随机暂停。"""
        self.count += 1

        if self.count >= self.threshold:
            pause_seconds = random.randint(API_PAUSE_MIN, API_PAUSE_MAX)
            log_message(
                f"已请求 {self.count} 次，暂停 {pause_seconds} 秒防封...",
                "warning",
                self.log_callback,
            )
            cancelled = False
            if self.stop_event is not None:
                cancelled = self.stop_event.wait(pause_seconds)
            else:
                time.sleep(pause_seconds)
            self.reset()
            if cancelled:
                return True
            log_message(f"继续下载，下次暂停阈值: {self.threshold}", "info", self.log_callback)
            return True

        return False

    def reset(self) -> None:
        """重置计数器和随机阈值。"""
        self.count = 0
        self.threshold = random.randint(API_CALL_THRESHOLD_MIN, API_CALL_THRESHOLD_MAX)


# ============================================================
# 文件名与请求头
# ============================================================


def sanitize_filename(file_name: str) -> str:
    """清理非法字符并保留中日文字符，返回可安全落盘的文件名。"""
    if not file_name:
        return "untitled"

    decoded_name = urllib.parse.unquote(file_name)
    valid_chars = f"-_.() {string.ascii_letters}{string.digits}"
    safe_name = "".join(
        char
        for char in decoded_name
        if char in valid_chars or "\u4e00" <= char <= "\u9fff" or "\u3040" <= char <= "\u30ff"
    )

    if len(safe_name) > MAX_FILENAME_LENGTH:
        safe_name = safe_name[:MAX_FILENAME_LENGTH]

    return safe_name or "untitled"


def _safe_int(value, default=None):
    """将输入安全转换为整数，失败时返回默认值。"""
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def build_common_headers(
    illust_id=None, user_agent: str | None = None, cookie: str | None = None
) -> dict[str, str]:
    """构建常用请求头，默认对齐 Pixiv 网页请求习惯。"""
    final_user_agent = user_agent or DEFAULT_USER_AGENT
    referer = f"https://www.pixiv.net/artworks/{illust_id}" if illust_id else DEFAULT_REFERER

    headers = {
        "User-Agent": final_user_agent,
        "Referer": referer,
        "Accept-Language": ACCEPT_LANGUAGE,
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def is_original_image_url(url: str | None) -> bool:
    """Return whether a Pixiv URL points at the unscaled ``img-original`` path.

    Pixiv API responses commonly expose an ``img-master`` preview URL first.
    The downloader must not treat that preview as an original when the user
    explicitly enabled original mode.
    """

    path = urllib.parse.urlparse(str(url or "")).path.lower()
    return "/img-original/" in path


# ============================================================
# 原图元数据解析
# ============================================================


def _interruptible_sleep(seconds: float, stop_event: threading.Event | None) -> None:
    """可被 ``stop_event`` 中断的 sleep；中断时抛 ``DownloadStopped``。

    提取自 ``fetch_original_meta`` 中重复 3 次的 ``if stop_event … else sleep`` 模式。
    """
    if stop_event is not None:
        if stop_event.wait(seconds):
            raise DownloadStopped()
    else:
        time.sleep(seconds)


def _parse_ajax_response_body(data: dict, illust_id) -> tuple[str, int]:
    """从 Ajax JSON 响应提取原图 URL 与页数，校验失败时抛 ``ValueError``。"""
    body = data.get("body") or {}
    urls = body.get("urls") or {}
    original_url = urls.get("original")
    page_count = _safe_int(body.get("pageCount"), 1) or 1

    if not original_url:
        raise ValueError(f"作品 {illust_id} 的 body.urls.original 为空")
    if not is_original_image_url(original_url):
        raise ValueError("Pixiv 原图接口返回的 URL 不是 img-original 地址")
    return original_url, page_count


def _handle_429_if_needed(
    response,
    attempt: int,
    max_retries: int,
    illust_id,
    log_callback,
    stop_event: threading.Event | None,
) -> bool:
    """若响应是 429 限流，执行退避并返回 ``True`` 表示应重试。

    非限流响应返回 ``False``，由调用方继续处理。达到重试上限时抛 ``ValueError``。
    """
    if getattr(response, "status_code", None) != 429:
        return False

    retry_after = _parse_retry_after(response)
    wait_time = _AJAX_RATE_LIMITER.report_429(retry_after)
    if attempt < max_retries:
        log_message(
            f"Ajax 接口限流，{wait_time:.0f} 秒后重试 ({attempt}/{max_retries}): {illust_id}",
            "warning",
            log_callback,
        )
        _interruptible_sleep(wait_time, stop_event)
        return True
    raise ValueError(f"作品 {illust_id} Ajax 接口持续限流 (429)")


def fetch_original_meta(
    illust_id,
    log_callback=None,
    user_agent: str | None = None,
    cookie: str | None = None,
    max_retries: int = 3,
    stop_event: threading.Event | None = None,
) -> dict[str, object]:
    """通过 Ajax 接口获取作品原图 URL 与页数信息。

    内置限流控制与 429 退避重试，避免高频调用触发 Pixiv 风控。

    结构（R6-2 重构）：429 处理 / JSON 解析 / 可中断 sleep 提取为模块级辅助函数，
    主函数聚焦于重试循环编排与缓存管理。
    """
    # Never retain the raw credential in a long-lived cache key.  The digest
    # still separates sessions while keeping the cache metadata non-sensitive.
    cookie_fingerprint = hashlib.sha256((cookie or "").encode("utf-8")).hexdigest()[:24]
    cache_key = f"{illust_id}|{user_agent or ''}|{cookie_fingerprint}"
    with _META_CACHE_LOCK:
        cached = _META_CACHE.get(cache_key)
        if cached:
            _META_CACHE.move_to_end(cache_key)
            return cached

    headers = build_common_headers(illust_id=illust_id, user_agent=user_agent, cookie=cookie)
    headers["Accept"] = AJAX_ACCEPT
    endpoint = f"https://www.pixiv.net/ajax/illust/{illust_id}"

    last_error: Exception | None = None
    _req_t0 = time.monotonic()
    for attempt in range(1, max_retries + 1):
        if stop_event is not None and stop_event.is_set():
            raise DownloadStopped()

        _AJAX_RATE_LIMITER.wait(stop_event)

        # Another worker may have populated the cache while this worker was
        # waiting for the global Ajax slot.
        with _META_CACHE_LOCK:
            cached = _META_CACHE.get(cache_key)
            if cached:
                _META_CACHE.move_to_end(cache_key)
                return cached

        try:
            response = _request_meta_response(
                cache_key,
                endpoint,
                headers,
                stop_event=stop_event,
            )

            if _handle_429_if_needed(
                response, attempt, max_retries, illust_id, log_callback, stop_event
            ):
                continue

            response.raise_for_status()
            _AJAX_RATE_LIMITER.report_success()

            original_url, page_count = _parse_ajax_response_body(response.json(), illust_id)

            result = {"original": original_url, "page_count": page_count}
            with _META_CACHE_LOCK:
                _META_CACHE[cache_key] = result
                _META_CACHE.move_to_end(cache_key)
                while len(_META_CACHE) > META_CACHE_MAX_ITEMS:
                    _META_CACHE.popitem(last=False)

            return result

        except (ValueError, RuntimeError):
            raise
        except Exception as exc:
            last_error = exc
            _cost = time.monotonic() - _req_t0
            if attempt < max_retries:
                delay = min(30.0, 3.0 * attempt + random.uniform(0, 2))
                log_message(
                    f"获取原图信息失败（{_cost:.1f}s），{delay:.0f} 秒后重试 ({attempt}/{max_retries}): {exc}",
                    "warning",
                    log_callback,
                )
                _interruptible_sleep(delay, stop_event)
            else:
                raise

    if last_error is not None:
        raise last_error
    raise ValueError(f"作品 {illust_id} 原图信息获取失败")


def build_original_url_for_page(original_url: str, page_index: int) -> str:
    """根据页码替换原图 URL 中的 _pN 段。"""
    return re.sub(r"_p\d+", f"_p{page_index}", original_url)


def build_save_path_for_page(save_path: str, page_index: int) -> str:
    """根据页码生成保存路径，优先替换文件名中的 _pN。"""
    target_path = Path(save_path)
    stem = target_path.stem
    suffix = target_path.suffix

    # ``{page}`` can sit before another template field (for example a date).
    # Replace the last complete page token instead of appending a second one.
    matches = list(re.finditer(r"_p\d+(?=_|$)", stem))
    if matches:
        match = matches[-1]
        new_stem = f"{stem[:match.start()]}_p{page_index}{stem[match.end():]}"
        return str(target_path.with_name(f"{new_stem}{suffix}"))

    if page_index == 0:
        return str(target_path)

    return str(target_path.with_name(f"{stem}_p{page_index}{suffix}"))


# ============================================================
# 底层下载
# ============================================================


def get_remote_content_length(url: str, headers: dict[str, str]) -> int | None:
    """获取远程文件长度，失败时返回 None。"""
    try:
        with request_get(url, headers=headers, stream=True, timeout=30) as response:
            response.raise_for_status()
            return _safe_int(response.headers.get("Content-Length"))
    except Exception:
        return None


_IMAGE_PREFIX_SCAN_LIMIT = 4096
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF_MAGICS = (b"GIF87a", b"GIF89a")
_WEBP_RIFF = b"RIFF"
_WEBP_TAG = b"WEBP"

_IMAGE_MIME_FORMATS = {
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/pjpeg": "jpeg",
    "image/png": "png",
    "image/x-png": "png",
    "image/apng": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}
_GENERIC_BINARY_MIME_TYPES = frozenset(
    {"application/octet-stream", "binary/octet-stream", "application/binary"}
)
_ERROR_DOCUMENT_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/xhtml+xml",
        "text/html",
        "text/xml",
        "text/json",
        "text/plain",
        "application/problem+json",
        "application/problem+xml",
    }
)


def _response_header(response, name: str) -> str | None:
    """Read one response header from requests' dict or plain test fixtures."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except Exception:
        value = None
    if value is not None:
        return str(value).strip()

    lowered = name.lower()
    try:
        items = headers.items()
    except Exception:
        return None
    for key, value in items:
        try:
            if str(key).lower() == lowered:
                return str(value).strip()
        except Exception:
            continue
    return None


def _content_type_format(content_type: str | None) -> str | None:
    """Return the required signature family for an allowed MIME type.

    An explicit error MIME (HTML/JSON/XML/text) is rejected even when the body
    happens to start with a valid image signature.  A missing MIME or a generic
    binary MIME is accepted only if the body signature validates.
    """
    if not content_type:
        return None
    base = content_type.split(";", 1)[0].strip().lower()
    if not base:
        return None
    if base in _IMAGE_MIME_FORMATS:
        return _IMAGE_MIME_FORMATS[base]
    if base in _GENERIC_BINARY_MIME_TYPES:
        return None
    if (
        base in _ERROR_DOCUMENT_MIME_TYPES
        or base.startswith("text/")
        or base.endswith("+json")
        or base.endswith("+xml")
    ):
        raise ValueError(f"响应 MIME 表明这是错误正文，拒绝保存: {base}")
    if base.startswith("image/"):
        raise ValueError(f"不支持的图片 MIME: {base}")
    raise ValueError(f"不支持的响应 MIME: {base}")


def _detect_image_format(data: bytes) -> str | None:
    """Recognize only the four raster formats this project may save as-is."""
    if data.startswith(_JPEG_MAGIC):
        return "jpeg"
    if data.startswith(_PNG_MAGIC):
        return "png"
    if data.startswith(_GIF_MAGICS[0]) or data.startswith(_GIF_MAGICS[1]):
        return "gif"
    if len(data) >= 12 and data[:4] == _WEBP_RIFF and data[8:12] == _WEBP_TAG:
        return "webp"
    return None


def _possible_image_prefix(data: bytes) -> bool:
    """Return whether ``data`` is still a prefix of a supported signature."""
    for magic in (_JPEG_MAGIC, _PNG_MAGIC, *_GIF_MAGICS):
        size = min(len(data), len(magic))
        if data[:size] == magic[:size]:
            return True

    if len(data) <= len(_WEBP_RIFF):
        return _WEBP_RIFF.startswith(data)
    if data[:4] != _WEBP_RIFF:
        return False
    if len(data) < 12:
        return True
    return data[8:12] == _WEBP_TAG


def _is_text_lead_only(data: bytes) -> bool:
    """Recognize a still-undecided prefix made only of BOM/whitespace bytes."""
    sample = data.lstrip(b" \t\r\n\f\v")
    if not sample:
        return True
    if sample.startswith(b"\xef\xbb\xbf"):
        return not sample[3:].strip(b" \t\r\n\f\v")
    if b"\xef\xbb\xbf".startswith(sample):
        return True
    if b"\xff\xfe".startswith(sample) or b"\xfe\xff".startswith(sample):
        return True
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    return False


def _error_document_marker(data: bytes) -> str | None:
    """Detect HTML/XML/JSON error bodies, including leading whitespace/BOM."""
    sample = data.lstrip(b" \t\r\n\f\v")
    try:
        if sample.startswith(b"\xef\xbb\xbf"):
            text = sample[3:].decode("utf-8", errors="ignore")
        elif sample.startswith(b"\xff\xfe"):
            text = sample[2:].decode("utf-16-le", errors="ignore")
        elif sample.startswith(b"\xfe\xff"):
            text = sample[2:].decode("utf-16-be", errors="ignore")
        else:
            text = sample.decode("latin-1")
    except Exception:
        return None

    text = text.lstrip(" \t\r\n\f\v\u00a0\ufeff")
    if not text:
        return None
    if text[0] == "<":
        return "HTML/XML"
    if text[0] in "{[":
        return "JSON"
    return None


def _classify_image_prefix(data: bytes) -> tuple[str, str | None]:
    """Classify the bytes read so far as image/pending/error/unsupported."""
    if not data:
        return "pending", None
    marker = _error_document_marker(data)
    if marker is not None:
        return marker, None
    detected = _detect_image_format(data)
    if detected is not None:
        return "image", detected
    if _is_text_lead_only(data) or _possible_image_prefix(data):
        return "pending", None
    return "unsupported", None


def _image_prefix_error_message(state: str) -> str:
    if state == "HTML/XML":
        return "HTTP 响应正文是 HTML/XML 错误页，已拒绝保存"
    if state == "JSON":
        return "HTTP 响应正文是 JSON 错误正文，已拒绝保存"
    return "HTTP 响应正文不是受支持的图片格式，已拒绝保存"


def _parse_declared_length(response) -> int | None:
    raw = _response_header(response, "Content-Length")
    if raw is None:
        return None
    # HTTP Content-Length is exactly 1*DIGIT.  Do not pass the raw value to
    # int(): it accepts "+68", "1_0", and other non-HTTP spellings.
    if re.fullmatch(r"[0-9]+", raw) is None:
        raise ValueError("响应 Content-Length 非法")
    value = int(raw)
    if value == 0:
        raise ValueError("响应 Content-Length 非法: 0")
    return value


def _normalize_expected_size(expected_size) -> int | None:
    if expected_size is None:
        return None
    try:
        value = int(expected_size)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("expected_size 非法") from None
    if value <= 0:
        raise ValueError(f"expected_size 非法: {value}")
    return value


def _validate_content_encoding(response) -> None:
    encoding = _response_header(response, "Content-Encoding")
    if encoding and encoding.lower() != "identity":
        raise ValueError(f"响应使用了未请求的内容编码: {encoding}")


def _build_download_request_headers(headers: dict[str, str]) -> dict[str, str]:
    """Force identity transfer and never send a Range request in P1."""
    skip = {"accept-encoding", "range", "if-range", "content-range"}
    request_headers = {
        str(key): value for key, value in dict(headers).items() if str(key).lower() not in skip
    }
    request_headers["Accept-Encoding"] = "identity"
    return request_headers


def _validate_transfer_lengths(
    written: int, declared_length: int | None, expected_size: int | None
) -> None:
    if written <= 0:
        raise ValueError("响应正文为空，未收到任何图片数据")
    if declared_length is not None and written != declared_length:
        raise ValueError(
            f"传输长度与 Content-Length 不一致: received={written}, declared={declared_length}"
        )
    if expected_size is not None and written != expected_size:
        raise ValueError(f"文件大小与元数据不一致: received={written}, expected={expected_size}")


def download_file(
    url: str,
    save_path: str,
    headers: dict[str, str],
    retries: int = 3,
    expected_size: int | None = None,
    resume_enabled: bool = False,
    log_callback=None,
    retry_callback=None,
    stop_event: threading.Event | None = None,
) -> tuple[bool, int]:
    """执行带重试的底层下载，并在保存前验证响应确实是受支持的图片。

    返回值保持 ``(success, size)`` 兼容契约。可选 ``retry_callback``
    只在仍可恢复的失败后触发，用于上层统计"重试后恢复"的图片页。
    ``stop_event`` 用于在数据传输过程中响应用户取消，避免工作线程
    卡在 ``iter_content`` 里导致整个任务无法停止。

    P1 安全边界：
    - 跨调用 Range 续传暂时禁用，每次都执行完整 GET；``resume_enabled``
      仅为调用兼容而保留，绝不据此拼接旧文件。持久前缀/强 ETag 恢复
      凭据属于后续交付协议。
    - 仅接受 HTTP 200、identity 编码、受支持的图片签名，并核对本次
      ``Content-Length``、实际写入字节数和 ``expected_size``。
    - 在状态、MIME 与首块签名确认之前不截断已有文件。这里只做文件头
      与传输校验，不是深度解码完整性保证。
    """
    path = Path(save_path)
    # ``resume_enabled`` deliberately does not authorize appending: without a
    # durable prefix proof or strong ETag it cannot prove the local fragment
    # belongs to the same version.  The parameter stays for API compatibility.
    _ = resume_enabled

    if stop_event is not None and stop_event.is_set():
        raise DownloadStopped()

    for attempt in range(1, retries + 1):
        file_obj = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if stop_event is not None and stop_event.is_set():
                raise DownloadStopped()

            request_headers = _build_download_request_headers(headers)
            expected_size_value = _normalize_expected_size(expected_size)

            with request_get(url, headers=request_headers, timeout=60, stream=True) as response:
                response.raise_for_status()

                status = getattr(response, "status_code", None)
                if status == 206:
                    raise ValueError("服务端返回未请求的 HTTP 206 分段响应，已拒绝保存")
                if status in (204, 304):
                    raise ValueError(f"HTTP {status} 响应没有可保存的正文")
                if status != 200:
                    raise ValueError(f"HTTP {status} 不是受支持的下载响应")
                if _response_header(response, "Content-Range"):
                    raise ValueError("服务端返回了未请求的分段响应头 Content-Range")

                _validate_content_encoding(response)
                content_type = _response_header(response, "Content-Type")
                expected_format = _content_type_format(content_type)
                declared_length = _parse_declared_length(response)
                if (
                    declared_length is not None
                    and expected_size_value is not None
                    and declared_length != expected_size_value
                ):
                    raise ValueError(
                        "远程 Content-Length 与 expected_size 冲突: "
                        f"header={declared_length}, expected={expected_size_value}"
                    )

                prefix = bytearray()
                written = 0
                for chunk in response.iter_content(DOWNLOAD_CHUNK_SIZE):
                    if stop_event is not None and stop_event.is_set():
                        log_message("检测到停止信号，中止当前文件传输", "info", log_callback)
                        raise DownloadStopped()
                    if not chunk:
                        continue

                    if file_obj is None:
                        prefix.extend(chunk)
                        state, detected_format = _classify_image_prefix(bytes(prefix))
                        if state == "pending":
                            if len(prefix) >= _IMAGE_PREFIX_SCAN_LIMIT:
                                raise ValueError(
                                    "无法在限定长度内识别受支持的图片格式，已拒绝保存"
                                )
                            continue
                        if state != "image" or detected_format is None:
                            raise ValueError(_image_prefix_error_message(state))
                        if expected_format is not None and detected_format != expected_format:
                            raise ValueError(
                                "图片 MIME 与内容签名不一致: "
                                f"mime={content_type or ''}, signature={detected_format}"
                            )
                        file_obj = path.open("wb")
                        file_obj.write(prefix)
                        written = len(prefix)
                        prefix = bytearray()
                        continue

                    file_obj.write(chunk)
                    written += len(chunk)

                if stop_event is not None and stop_event.is_set():
                    log_message("检测到停止信号，中止当前文件传输", "info", log_callback)
                    raise DownloadStopped()
                if file_obj is None:
                    raise ValueError("响应正文为空或不包含受支持的图片数据")
                _validate_transfer_lengths(written, declared_length, expected_size_value)
                return True, written
        except DownloadStopped:
            # 主动取消必须立即向上抛出，不进入任何重试等待。
            raise
        except Exception as exception_obj:
            if stop_event is not None and stop_event.is_set():
                # 用户取消：立即向上抛出已发生的异常而非重试，让工作线程
                # 尽快退出，避免 sleep 重试间隔期间空转。
                raise
            if attempt < retries:
                log_message(
                    f"传输中断，准备重试 ({attempt}/{retries}): {exception_obj}",
                    "warning",
                    log_callback,
                )
                if callable(retry_callback):
                    try:
                        retry_callback(attempt, retries, exception_obj)
                    except Exception:
                        # 观测回调不得改变下载结果。
                        pass
                if stop_event is not None:
                    if stop_event.wait(2):
                        raise DownloadStopped()
                else:
                    time.sleep(2)
            else:
                log_message(
                    f"下载最终失败 ({attempt}/{retries}): {exception_obj}",
                    "error",
                    log_callback,
                )
        finally:
            if file_obj is not None:
                file_obj.close()

    return False, 0
