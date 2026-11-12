#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志工具：统一前缀格式，支持调试开关与多种回调输出。"""

from __future__ import annotations

import logging
import os
import queue
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event, RLock, Thread, current_thread

LOG_SUCCESS = "[+]"  # 成功/完成
LOG_ERROR = "[-]"  # 错误/失败
LOG_WARNING = "[!]"  # 警告
LOG_INFO = "[*]"  # 信息
LOG_DEBUG = "[D]"  # 调试

DEBUG_MODE = False


class _BufferedRotatingFileHandler(RotatingFileHandler):
    """Rotating handler that batches OS-level flushes.

    ``StreamHandler.emit`` flushes after every record; with per-page
    download logs that is one syscall per line on every worker thread.
    Flushing every ``flush_every`` records keeps rotation accounting
    intact (``tell`` includes buffered data) while removing the hot-path
    syscall storm.  Durability is preserved via periodic flushes,
    explicit ``SessionLogQueue.flush`` and close.
    """

    def __init__(self, *args, flush_every: int = 50, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._flush_every = max(1, int(flush_every))
        self._pending_flush = 0

    def emit(self, record) -> None:
        try:
            if self.shouldRollover(record):
                self.doRollover()
            if self.stream is None:
                self.stream = self._open()
            msg = self.format(record)
            self.stream.write(msg + self.terminator)
            self._pending_flush += 1
            if self._pending_flush >= self._flush_every:
                self.flush()
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        self._pending_flush = 0
        super().flush()


class SessionLogQueue(queue.Queue):
    """Thread-safe UI queue that also persists a bounded session log.

    The queue contract remains compatible with the former ``queue.Queue``
    usage.  Disk logs are rotated by bytes, while the Tk view is free to keep
    only a bounded recent window without losing the complete session trail.
    """

    def __init__(
        self,
        log_dir: str | Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        flush_interval: float = 1.0,
    ) -> None:
        super().__init__()
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        session_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_id = f"{session_stamp}_{os.getpid()}"
        self.session_path = self.log_dir / f"pixiv_session_{self.session_id}.log"
        self._backup_count = max(1, int(backup_count))
        self._io_lock = RLock()
        self._logger = logging.getLogger(f"pidl.session.{self.session_id}.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        handler = _BufferedRotatingFileHandler(
            self.session_path,
            maxBytes=max(1024, int(max_bytes)),
            backupCount=self._backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        self._handler = handler
        self._logger.addHandler(handler)
        self._flush_interval = max(0.05, float(flush_interval))
        self._flush_stop = Event()
        self._flush_thread = Thread(
            target=self._flush_loop,
            name=f"pidl-log-flush-{self.session_id}",
            daemon=True,
        )
        self._flush_thread.start()

    def _flush_loop(self) -> None:
        """Bound crash-loss exposure without flushing every high-volume line."""
        while not self._flush_stop.wait(self._flush_interval):
            self.flush()

    def put(self, item, block: bool = True, timeout: float | None = None) -> None:
        message = str(item)
        with self._io_lock:
            self._logger.info(message)
            if message.startswith((LOG_ERROR, LOG_WARNING)) and self._handler is not None:
                self._handler.flush()
        super().put(message, block=block, timeout=timeout)

    def flush(self) -> None:
        with self._io_lock:
            handler = self._handler
            if handler is not None:
                handler.flush()

    def close_log(self) -> None:
        self._flush_stop.set()
        flush_thread = self._flush_thread
        if flush_thread is not current_thread() and flush_thread.is_alive():
            flush_thread.join(timeout=max(0.2, self._flush_interval * 2))
        with self._io_lock:
            if self._handler is None:
                return
            handler = self._handler
            self._handler = None
            handler.flush()
            self._logger.removeHandler(handler)
            handler.close()

    def read_session_text(self) -> str:
        """Return all online rotation segments from oldest to newest."""
        self.flush()
        paths = []
        for index in range(self._backup_count, 0, -1):
            candidate = Path(f"{self.session_path}.{index}")
            if candidate.exists():
                paths.append(candidate)
        if self.session_path.exists():
            paths.append(self.session_path)
        chunks = []
        for path in paths:
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        return "".join(chunks)


def set_debug_mode(enabled: bool) -> bool:
    """设置调试模式状态并返回当前值。"""
    global DEBUG_MODE
    DEBUG_MODE = bool(enabled)
    return DEBUG_MODE


def get_debug_mode() -> bool:
    """获取当前调试模式状态。"""
    return DEBUG_MODE


def _emit_message(formatted_message: str, callback=None) -> None:
    """统一输出：支持队列、函数回调或控制台打印。"""
    if callback is None:
        print(formatted_message)
        return

    try:
        if hasattr(callback, "put"):
            callback.put(formatted_message)
            return
        if callable(callback):
            callback(formatted_message)
            return
    except Exception:
        pass

    print(formatted_message)


def log_message(msg: str, level: str = "info", callback=None) -> str:
    """输出带语义前缀的日志，返回格式化后的文本。"""
    if level == "debug" and not DEBUG_MODE:
        return ""

    prefix = LOG_INFO
    if level == "success":
        prefix = LOG_SUCCESS
    elif level == "error":
        prefix = LOG_ERROR
    elif level == "warning":
        prefix = LOG_WARNING
    elif level == "debug":
        prefix = LOG_DEBUG

    output = f"{prefix} {msg}"
    _emit_message(output, callback)
    return output


def log_exception(exception_obj: Exception, context: str = "", callback=None) -> None:
    """记录异常信息；调试模式下附加堆栈详情。"""
    error_message = (
        f"{context}: {type(exception_obj).__name__}: {exception_obj}"
        if context
        else f"{type(exception_obj).__name__}: {exception_obj}"
    )
    log_message(error_message, "error", callback)

    if DEBUG_MODE:
        stack_text = "".join(
            traceback.format_exception(
                type(exception_obj), exception_obj, exception_obj.__traceback__
            )
        )
        for line in stack_text.strip().split("\n"):
            log_message(line, "debug", callback)
