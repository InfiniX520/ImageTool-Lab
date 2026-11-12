"""Crash-safe outbox for artwork download tasks.

Traversal checkpoints record what the Pixiv API has already enumerated.  This
outbox independently records what still needs to reach the filesystem, so an
API traversal can complete without losing a later CDN/Ajax failure.
"""

from __future__ import annotations

import hashlib
import json
import queue
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from core.models import DownloadTask

DATABASE_FILE_NAME = "pending_download_tasks.sqlite3"


class DownloadTaskStoreError(RuntimeError):
    """Raised when the durable task outbox cannot be updated safely."""


def _text(value) -> str:
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value or "")


def task_identity(task: DownloadTask) -> str:
    """Stable non-secret identity for one artwork-level output task."""
    try:
        save_path = str(Path(task.save_path).expanduser().resolve())
    except (OSError, RuntimeError, TypeError, ValueError):
        save_path = str(task.save_path)
    raw = f"{save_path}|{task.illust_id}|{int(task.page_index or 0)}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _serialize(task: DownloadTask) -> str:
    payload = {
        "url": str(task.url),
        "save_path": str(task.save_path),
        "illust_id": str(task.illust_id),
        "log_dir": str(task.log_dir),
        "is_ai": task.is_ai,
        "bookmarks": (
            task.bookmarks
            if isinstance(task.bookmarks, (int, float, str, bool, type(None)))
            else _text(task.bookmarks)
        ),
        "create_date": _text(task.create_date),
        "page_index": int(task.page_index or 0),
        "title": str(task.title or ""),
        "artist_id": str(task.artist_id or ""),
        "artist_name": str(task.artist_name or ""),
        "original_urls": [str(url) for url in task.original_urls],
        "known_page_count": int(task.known_page_count or 0),
        "download_root": str(task.download_root or ""),
        "folder_by_user": task.folder_by_user,
        "original_enabled": task.original_enabled,
        "multi_page": task.multi_page,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _deserialize(payload_json: str) -> DownloadTask:
    payload = json.loads(payload_json)
    if not isinstance(payload, dict):
        raise ValueError("下载任务载荷不是对象")
    return DownloadTask(
        url=str(payload.get("url") or ""),
        save_path=str(payload.get("save_path") or ""),
        illust_id=str(payload.get("illust_id") or ""),
        log_dir=str(payload.get("log_dir") or ""),
        is_ai=payload.get("is_ai"),
        bookmarks=payload.get("bookmarks"),
        create_date=str(payload.get("create_date") or "") or None,
        page_index=int(payload.get("page_index") or 0),
        title=str(payload.get("title") or ""),
        artist_id=str(payload.get("artist_id") or ""),
        artist_name=str(payload.get("artist_name") or ""),
        original_urls=tuple(str(url) for url in (payload.get("original_urls") or ())),
        known_page_count=int(payload.get("known_page_count") or 0),
        download_root=str(payload.get("download_root") or ""),
        folder_by_user=payload.get("folder_by_user"),
        original_enabled=payload.get("original_enabled"),
        multi_page=payload.get("multi_page"),
    )


class DownloadTaskStore:
    """Thread-safe SQLite outbox; completed tasks are removed atomically."""

    def __init__(self, log_root: str | Path) -> None:
        self.log_root = Path(log_root).expanduser().resolve()
        self.database_path = self.log_root / DATABASE_FILE_NAME
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._connection is not None:
                return self._connection
            self.log_root.mkdir(parents=True, exist_ok=True)
            try:
                connection = sqlite3.connect(self.database_path, timeout=5, check_same_thread=False)
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS download_tasks (
                        task_id TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        state TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_download_tasks_state_updated
                        ON download_tasks(state, updated_at);
                    """)
                connection.commit()
            except (OSError, sqlite3.Error) as exc:
                raise DownloadTaskStoreError("无法初始化持久化下载任务箱") from exc
            self._connection = connection
            return connection

    def enqueue(self, task: DownloadTask) -> str:
        task_id = task_identity(task)
        payload = _serialize(task)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    """
                    INSERT INTO download_tasks(
                        task_id, payload_json, state, attempts, last_error,
                        created_at, updated_at
                    ) VALUES (?, ?, 'queued', 0, '', ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        payload_json=excluded.payload_json,
                        state='queued',
                        last_error='',
                        updated_at=excluded.updated_at
                    """,
                    (task_id, payload, now, now),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise DownloadTaskStoreError("无法保存下载任务") from exc
        return task_id

    def mark_running(self, task: DownloadTask) -> None:
        with self._lock:
            self._connect().execute(
                """
                UPDATE download_tasks
                SET state='running', attempts=attempts+1, updated_at=?
                WHERE task_id=?
                """,
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_identity(task)),
            )
            self._connection.commit()

    def mark_failed(self, task: DownloadTask, error: object = "") -> None:
        message = str(error or "")[:500]
        with self._lock:
            self._connect().execute(
                """
                UPDATE download_tasks
                SET state='failed', last_error=?, updated_at=?
                WHERE task_id=?
                """,
                (
                    message,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    task_identity(task),
                ),
            )
            self._connection.commit()

    def complete(self, task: DownloadTask) -> None:
        with self._lock:
            self._connect().execute(
                "DELETE FROM download_tasks WHERE task_id=?",
                (task_identity(task),),
            )
            self._connection.commit()

    def list_retryable(self, *, limit: int = 10000) -> list[DownloadTask]:
        with self._lock:
            rows = (
                self._connect()
                .execute(
                    """
                SELECT payload_json
                FROM download_tasks
                WHERE state IN ('queued', 'running', 'failed')
                ORDER BY created_at, task_id
                LIMIT ?
                """,
                    (max(1, int(limit)),),
                )
                .fetchall()
            )
        tasks: list[DownloadTask] = []
        for (payload_json,) in rows:
            try:
                tasks.append(_deserialize(payload_json))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise DownloadTaskStoreError("持久化下载任务载荷损坏") from exc
        return tasks

    def count(self) -> int:
        with self._lock:
            return int(self._connect().execute("SELECT COUNT(*) FROM download_tasks").fetchone()[0])

    def integrity_check(self) -> str:
        with self._lock:
            return str(self._connect().execute("PRAGMA integrity_check").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is None:
                return
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()


class DurableDownloadQueue(queue.Queue):
    """A bounded in-memory queue whose accepted tasks are already durable."""

    def __init__(self, maxsize: int, store: DownloadTaskStore) -> None:
        super().__init__(maxsize=maxsize)
        self.store = store
        self._seen_lock = threading.Lock()
        self._seen: set[str] = set()

    def put(self, item, block: bool = True, timeout: float | None = None):
        if not isinstance(item, DownloadTask):
            super().put(item, block=block, timeout=timeout)
            return True
        task_id = task_identity(item)
        with self._seen_lock:
            if task_id in self._seen:
                return False
            self._seen.add(task_id)
        try:
            self.store.enqueue(item)
            super().put(item, block=block, timeout=timeout)
            return True
        except Exception:
            with self._seen_lock:
                self._seen.discard(task_id)
            raise


__all__ = [
    "DATABASE_FILE_NAME",
    "DownloadTaskStore",
    "DownloadTaskStoreError",
    "DurableDownloadQueue",
    "task_identity",
]
