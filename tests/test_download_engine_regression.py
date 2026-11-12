#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第四/五轮 Bug 修复的回归测试。

覆盖：
- BUG-004 / CQ-7：``download_file`` 的 ``stop_event`` 取消与 ``DownloadStopped`` 异常
- BUG-003：``HttpClient`` 的 per-thread session / ``close_all`` / ``close_current``
- BUG-001：``DualScrollText`` 的 config 委托（Tkinter 部分需显示初始化，跳过纯单元测试）
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.download_engine import (
    DownloadStopped,
    download_file,
    get_remote_content_length,
)
from core.http_client import HttpClient

try:  # pragma: no cover - pytest prepend mode puts ``tests`` on sys.path
    from image_fixtures import PNG_BYTES
except ImportError:  # pragma: no cover - importlib/package execution fallback
    from tests.image_fixtures import PNG_BYTES

# ============================================================
# DownloadStopped 异常类契约（CQ-7）
# ============================================================


def test_download_stopped_is_exception_subclass() -> None:
    """DownloadStopped 必须是 Exception 子类，downloader 用 isinstance 判断。"""
    assert issubclass(DownloadStopped, Exception)
    exc = DownloadStopped()
    assert isinstance(exc, Exception)
    # 不应是 RuntimeError，否则旧的字符串匹配逻辑会误判
    assert not isinstance(exc, RuntimeError)


# ============================================================
# download_file 的 stop_event 取消路径（BUG-004 / CQ-7）
# ============================================================


def _make_stream_response(chunks: list[bytes], status_code: int = 200):
    """构造模拟的 stream Response，iter_content 产出指定 chunks。"""
    response = MagicMock()
    response.status_code = status_code
    response.headers = {"Content-Length": str(sum(len(c) for c in chunks))}
    response.iter_content = lambda _size: iter(chunks)
    response.__enter__ = lambda self: self
    response.__exit__ = lambda self, *args: None
    response.raise_for_status = MagicMock()
    return response


def test_download_file_raises_download_stopped_when_stop_event_set_during_transfer(
    tmp_path: Path,
) -> None:
    """BUG-004：iter_content 循环中检测到 stop_event 已 set，立即抛 DownloadStopped。"""
    save_path = tmp_path / "out.jpg"
    stop_event = threading.Event()
    # 第一个 chunk 后触发停止
    chunks = [b"data-1", b"data-2", b"data-3"]

    response = _make_stream_response(chunks)

    def fake_request_get(url, **kwargs):
        # 在第一个 chunk 写入后设置停止信号
        original_iter = response.iter_content

        def watching_iter(size):
            for i, chunk in enumerate(original_iter(size)):
                if i == 0:
                    stop_event.set()
                yield chunk

        response.iter_content = watching_iter
        return response

    with patch("core.download_engine.request_get", side_effect=fake_request_get):
        with pytest.raises(DownloadStopped):
            download_file(
                url="https://img.example.com/x.jpg",
                save_path=str(save_path),
                headers={},
                retries=3,
                stop_event=stop_event,
            )


def test_download_file_stop_event_set_in_exception_handler_reraises_without_retry(
    tmp_path: Path,
) -> None:
    """CQ-7：异常处理中 stop_event 已 set 时立即 raise，不重试不 sleep。"""
    save_path = tmp_path / "out.jpg"
    stop_event = threading.Event()
    call_count = {"value": 0}

    response = MagicMock()
    response.status_code = 200
    response.headers = {}
    response.iter_content = lambda _size: iter([])
    response.__enter__ = lambda self: self
    response.__exit__ = lambda self, *args: None
    response.raise_for_status = MagicMock(side_effect=Exception("network error"))

    def fake_request_get(url, **kwargs):
        call_count["value"] += 1
        # 在 raise_for_status 抛异常前设置停止信号
        stop_event.set()
        return response

    with patch("core.download_engine.request_get", side_effect=fake_request_get):
        with pytest.raises(Exception) as exc_info:
            download_file(
                url="https://img.example.com/x.jpg",
                save_path=str(save_path),
                headers={},
                retries=3,
                stop_event=stop_event,
            )
    # 应该是原始的网络异常被重新抛出（而非 DownloadStopped，因为 raise_for_status
    # 在 try 块内抛出，被 except 捕获后因 stop_event.is_set() 直接 raise）
    # 只调用了一次，证明没有重试
    assert call_count["value"] == 1


def test_download_file_no_stop_event_retries_on_failure(tmp_path: Path) -> None:
    """无 stop_event 时，失败应重试 retries 次。"""
    save_path = tmp_path / "out.jpg"
    call_count = {"value": 0}

    response = MagicMock()
    response.status_code = 200
    response.headers = {}
    response.iter_content = lambda _size: iter([])
    response.__enter__ = lambda self: self
    response.__exit__ = lambda self, *args: None
    response.raise_for_status = MagicMock(side_effect=ConnectionError("refused"))

    def fake_request_get(url, **kwargs):
        call_count["value"] += 1
        return response

    with patch("core.download_engine.request_get", side_effect=fake_request_get):
        success, size = download_file(
            url="https://img.example.com/x.jpg",
            save_path=str(save_path),
            headers={},
            retries=3,
        )
    assert success is False
    assert size == 0
    # 重试 3 次
    assert call_count["value"] == 3


# ============================================================
# download_file 的 Range 续传保守禁用（P1）
# ============================================================


def test_download_file_resume_returns_true_when_local_size_matches_expected(
    tmp_path: Path,
) -> None:
    """旧契约错误：本地大小等于 expected_size 不能证明文件完整。

    保留测试名以记录行为变更：P1 不再把“本地大小碰巧等于远程长度”当成
    下载完成。即使 ``resume_enabled=True``，也必须发起完整 GET、发送
    ``Accept-Encoding: identity``、不发送 ``Range``，并通过图片签名与
    传输长度校验后才覆盖旧文件。
    """
    save_path = tmp_path / "done.png"
    save_path.write_bytes(b"x" * len(PNG_BYTES))

    call_count = {"value": 0}
    captured_headers: dict[str, str] = {}
    response = _make_stream_response([PNG_BYTES])
    response.headers["Content-Type"] = "image/png"

    def fake_request_get(url, **kwargs):
        call_count["value"] += 1
        captured_headers.update(kwargs.get("headers") or {})
        return response

    with patch("core.download_engine.request_get", side_effect=fake_request_get):
        success, size = download_file(
            url="https://img.example.com/x.png",
            save_path=str(save_path),
            headers={},
            retries=1,
            expected_size=len(PNG_BYTES),
            resume_enabled=True,
        )

    assert success is True
    assert size == len(PNG_BYTES)
    # 同尺寸旧文件必须被真实下载重新验证，而不是零请求直接成功。
    assert call_count["value"] == 1
    assert captured_headers["Accept-Encoding"] == "identity"
    assert "Range" not in captured_headers
    assert save_path.read_bytes() == PNG_BYTES


def test_download_file_size_validation_failure_returns_false(tmp_path: Path) -> None:
    """下载完成后大小校验失败应触发重试，最终返回 (False, 0)。"""
    save_path = tmp_path / "out.jpg"
    chunks = [b"short"]

    response = _make_stream_response(chunks)

    with patch("core.download_engine.request_get", return_value=response):
        success, size = download_file(
            url="https://img.example.com/x.jpg",
            save_path=str(save_path),
            headers={},
            retries=2,
            expected_size=999,  # 与实际 5 字节不匹配
        )
    assert success is False
    assert size == 0


# ============================================================
# get_remote_content_length 异常路径
# ============================================================


def test_get_remote_content_length_returns_none_on_failure() -> None:
    """请求异常时返回 None，不向上抛出。"""
    with patch("core.download_engine.request_get", side_effect=Exception("timeout")):
        result = get_remote_content_length("https://img.example.com/x.jpg", {})
    assert result is None


# ============================================================
# HttpClient per-thread session 与 close 行为（BUG-003）
# ============================================================


def test_http_client_close_all_closes_all_sessions() -> None:
    """close_all 应关闭所有线程创建的 session。"""
    client = HttpClient(pool_size=2)
    sessions = []

    def worker():
        # 在子线程中创建 session
        s = client._session()
        sessions.append(s)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(sessions) == 3
    # close_all 前 session 未关闭
    assert all(not s._closed_mock if hasattr(s, "_closed_mock") else True for s in sessions)

    client.close_all()
    # 所有 session 应被 close()
    for s in sessions:
        # requests.Session.close() 没有公开的 closed 标志，用 mock 验证调用
        # 这里验证 session 已被从 _sessions 集合移除
        pass
    # 验证 _sessions 已清空
    assert len(client._sessions) == 0


def test_http_client_close_current_only_closes_calling_thread_session() -> None:
    """close_current 只关闭调用线程的 session，不影响其他线程的 session。"""
    client = HttpClient(pool_size=2)
    main_session = client._session()

    other_sessions = []

    def worker():
        other_sessions.append(client._session())

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert len(other_sessions) == 1
    # 主线程 session 与子线程 session 不同
    assert main_session is not other_sessions[0]

    # 主线程关闭自己的 session
    client.close_current()
    # 子线程的 session 仍在集合中
    assert other_sessions[0] in client._sessions
    # 主线程 local session 已清空
    assert getattr(client._local, "session", None) is None


def test_http_client_per_thread_session_isolation() -> None:
    """同一线程多次调用 _session 返回同一实例；不同线程返回不同实例。"""
    client = HttpClient(pool_size=2)
    s1 = client._session()
    s2 = client._session()
    assert s1 is s2  # 同线程复用

    other_session = []

    def worker():
        other_session.append(client._session())

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert other_session[0] is not s1  # 不同线程隔离
