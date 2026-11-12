"""Durable SQLite storage for page-level download records.

The legacy JSON files remain reversible compatibility projections.  SQLite is
the transactional source of truth used by normal skip checks and writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Mapping

SCHEMA_VERSION = 1
DATABASE_FILE_NAME = "download_records.sqlite3"
LEGACY_RECORD_FILE_NAME = "master_downloaded_record.json"


class SqliteRecordError(RuntimeError):
    """Raised when the durable record store cannot be verified safely."""


class SqliteRecordRepository:
    """Single-process, thread-safe SQLite repository with WAL journaling."""

    def __init__(self, log_root: str | Path) -> None:
        self.log_root = Path(log_root).expanduser().resolve()
        self.database_path = self.log_root / DATABASE_FILE_NAME
        self.legacy_json_path = self.log_root / LEGACY_RECORD_FILE_NAME
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._connection is not None:
                return self._connection
            self.log_root.mkdir(parents=True, exist_ok=True)
            connection = None
            try:
                connection = sqlite3.connect(
                    self.database_path,
                    timeout=5.0,
                    check_same_thread=False,
                )
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")
                self._create_schema(connection)
                self._import_legacy_once(connection)
            except (OSError, sqlite3.Error, ValueError, TypeError, SqliteRecordError) as exc:
                try:
                    if connection is not None:
                        connection.close()
                except Exception:
                    pass
                raise SqliteRecordError(f"无法初始化下载记录数据库: {self.database_path}") from exc
            self._connection = connection
            return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS download_records (
                illust_id TEXT NOT NULL,
                page_index INTEGER NOT NULL,
                file_path TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                artist_id TEXT NOT NULL DEFAULT '',
                artist_name TEXT NOT NULL DEFAULT '',
                bookmarks INTEGER,
                is_ai INTEGER NOT NULL DEFAULT 0,
                publish_time TEXT NOT NULL DEFAULT '',
                original_url TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (illust_id, page_index)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS download_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                illust_id TEXT NOT NULL,
                page_index INTEGER NOT NULL,
                status TEXT NOT NULL,
                file_path TEXT NOT NULL DEFAULT '',
                file_size INTEGER NOT NULL DEFAULT 0,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_download_records_status_updated
                ON download_records(status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_download_records_artist_updated
                ON download_records(artist_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_download_events_record
                ON download_events(illust_id, page_index, event_id DESC);
            """)
        connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        connection.commit()

    def _import_legacy_once(self, connection: sqlite3.Connection) -> None:
        imported = connection.execute(
            "SELECT value FROM schema_meta WHERE key='legacy_import_completed'"
        ).fetchone()
        if imported is not None:
            return

        if not self.legacy_json_path.exists():
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) "
                "VALUES('legacy_import_completed', 'empty')"
            )
            connection.commit()
            return

        try:
            raw_bytes = self.legacy_json_path.read_bytes()
            decoded = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SqliteRecordError(
                f"旧下载记录无法解析，已拒绝迁移: {self.legacy_json_path}"
            ) from exc
        if not isinstance(decoded, dict):
            raise SqliteRecordError(f"旧下载记录格式不是对象: {self.legacy_json_path}")

        rows = []
        for record_key, item in decoded.items():
            if not isinstance(item, dict):
                continue
            illust_id = str(item.get("illust_id") or str(record_key).split("_p", 1)[0])
            page_index = _safe_int(item.get("page"), None)
            if page_index is None:
                page_index = _safe_int(str(record_key).rsplit("_p", 1)[-1], 0)
            rows.append(_row_from_item(illust_id, page_index or 0, item))

        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(_UPSERT_SQL, rows)
            actual_count = connection.execute("SELECT COUNT(*) FROM download_records").fetchone()[0]
            if actual_count != len(rows):
                raise SqliteRecordError(
                    f"旧记录迁移对账失败: source={len(rows)}, sqlite={actual_count}"
                )
            metadata = {
                "legacy_import_completed": "true",
                "legacy_source_path": str(self.legacy_json_path),
                "legacy_source_sha256": hashlib.sha256(raw_bytes).hexdigest().upper(),
                "legacy_source_count": str(len(rows)),
            }
            connection.executemany(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
                metadata.items(),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            del decoded

    def record(self, record_key: str, item: Mapping[str, object]) -> None:
        illust_id = str(item.get("illust_id") or str(record_key).split("_p", 1)[0])
        page_index = _safe_int(item.get("page"), None)
        if page_index is None:
            page_index = _safe_int(str(record_key).rsplit("_p", 1)[-1], 0)
        row = _row_from_item(illust_id, page_index or 0, item)
        payload = json.dumps(dict(item), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(_UPSERT_SQL, row)
                connection.execute(
                    """
                    INSERT INTO download_events(
                        illust_id, page_index, status, file_path, file_size,
                        occurred_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row[0],
                        row[1],
                        row[3],
                        row[2],
                        row[4],
                        row[5],
                        payload,
                    ),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise SqliteRecordError("写入下载记录数据库失败") from exc

    def get_summary(self, illust_id, page_index: int) -> tuple[object, object, object] | None:
        with self._lock:
            connection = self._connect()
            row = connection.execute(
                """
                SELECT status, file_path, file_size
                FROM download_records
                WHERE illust_id=? AND page_index=?
                """,
                (str(illust_id), int(page_index)),
            ).fetchone()
        return tuple(row) if row is not None else None

    def count(self) -> int:
        with self._lock:
            return int(
                self._connect().execute("SELECT COUNT(*) FROM download_records").fetchone()[0]
            )

    def integrity_check(self) -> str:
        with self._lock:
            return str(self._connect().execute("PRAGMA integrity_check").fetchone()[0])

    def _latest_event_id(self) -> int:
        row = (
            self._connect()
            .execute("SELECT COALESCE(MAX(event_id), 0) FROM download_events")
            .fetchone()
        )
        return int(row[0] or 0)

    def projection_is_current(self) -> bool:
        """Return whether the central compatibility JSON includes every event."""
        with self._lock:
            row = (
                self._connect()
                .execute("SELECT value FROM schema_meta WHERE key='legacy_projection_event_id'")
                .fetchone()
            )
            checkpoint = _safe_int(row[0], -1) if row else -1
            return self.legacy_json_path.exists() and checkpoint == self._latest_event_id()

    def mark_legacy_projection_current(self) -> None:
        """Checkpoint a successfully flushed set of compatibility projections."""
        with self._lock:
            connection = self._connect()
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
                ("legacy_projection_event_id", str(self._latest_event_id())),
            )
            connection.commit()

    def reconcile_legacy_projection(self, *, force: bool = False) -> bool:
        """Stream SQLite latest-state rows into the central legacy JSON.

        This is the crash-recovery path for events committed after the last
        in-memory projection batch. Rows are streamed directly to a temporary
        file, keeping memory bounded even for very large download histories.
        """
        with self._lock:
            connection = self._connect()
            if not force and self.projection_is_current():
                return False
            snapshot_event_id = self._latest_event_id()
            record_count = int(
                connection.execute("SELECT COUNT(*) FROM download_records").fetchone()[0]
            )
            if record_count == 0 and not self.legacy_json_path.exists():
                connection.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
                    ("legacy_projection_event_id", str(snapshot_event_id)),
                )
                connection.commit()
                return False
            cursor = connection.execute("""
                SELECT illust_id, page_index, file_path, status, file_size,
                       updated_at, title, artist_id, artist_name, bookmarks,
                       is_ai, publish_time, original_url
                FROM download_records
                ORDER BY illust_id, page_index
                """)
            self.log_root.mkdir(parents=True, exist_ok=True)
            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    delete=False,
                    dir=self.log_root,
                    prefix=f".{self.legacy_json_path.name}.",
                    suffix=".tmp",
                ) as file_obj:
                    temp_path = Path(file_obj.name)
                    file_obj.write("{\n")
                    first = True
                    while True:
                        rows = cursor.fetchmany(1000)
                        if not rows:
                            break
                        for row in rows:
                            if not first:
                                file_obj.write(",\n")
                            first = False
                            key = f"{row[0]}_p{row[1]}"
                            item = {
                                "file_path": row[2],
                                "status": row[3],
                                "file_size": row[4],
                                "updated_at": row[5],
                                "illust_id": row[0],
                                "page": row[1],
                                "title": row[6],
                                "artist_id": row[7],
                                "artist_name": row[8],
                                "bookmarks": row[9] if row[9] is not None else "",
                                "is_ai": bool(row[10]),
                                "publish_time": row[11],
                                "original_url": row[12],
                            }
                            file_obj.write("  ")
                            file_obj.write(json.dumps(key, ensure_ascii=False))
                            file_obj.write(": ")
                            file_obj.write(
                                json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                            )
                    file_obj.write("\n}\n")
                    file_obj.flush()
                    os.fsync(file_obj.fileno())
                os.replace(temp_path, self.legacy_json_path)
                temp_path = None
                connection.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
                    ("legacy_projection_event_id", str(snapshot_event_id)),
                )
                connection.commit()
                return True
            except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
                connection.rollback()
                raise SqliteRecordError("无法从 SQLite 恢复兼容下载记录") from exc
            finally:
                if temp_path is not None:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass

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


def _safe_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _nullable_int(value):
    if value in (None, ""):
        return None
    return _safe_int(value, None)


def _row_from_item(illust_id: str, page_index: int, item: Mapping[str, object]) -> tuple:
    return (
        str(illust_id),
        int(page_index),
        str(item.get("file_path") or ""),
        str(item.get("status") or ""),
        _safe_int(item.get("file_size"), 0) or 0,
        str(item.get("updated_at") or ""),
        str(item.get("title") or ""),
        str(item.get("artist_id") or ""),
        str(item.get("artist_name") or ""),
        _nullable_int(item.get("bookmarks")),
        1 if item.get("is_ai") else 0,
        str(item.get("publish_time") or ""),
        str(item.get("original_url") or ""),
    )


_UPSERT_SQL = """
INSERT INTO download_records(
    illust_id, page_index, file_path, status, file_size, updated_at,
    title, artist_id, artist_name, bookmarks, is_ai, publish_time,
    original_url
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(illust_id, page_index) DO UPDATE SET
    file_path=excluded.file_path,
    status=excluded.status,
    file_size=excluded.file_size,
    updated_at=excluded.updated_at,
    title=excluded.title,
    artist_id=excluded.artist_id,
    artist_name=excluded.artist_name,
    bookmarks=excluded.bookmarks,
    is_ai=excluded.is_ai,
    publish_time=excluded.publish_time,
    original_url=excluded.original_url
WHERE NOT (
    download_records.status='下载完成'
    AND excluded.status<>'下载完成'
)
"""


__all__ = [
    "DATABASE_FILE_NAME",
    "SCHEMA_VERSION",
    "SqliteRecordError",
    "SqliteRecordRepository",
]
