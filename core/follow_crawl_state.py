"""Durable state for resumable followed-artist crawl jobs.

This database is intentionally separate from ``download_records.sqlite3``.
Download records describe durable image files; this repository describes
temporary traversal progress.  Keeping the two consistency boundaries apart
means a crawl-state migration can never rewrite the user's download history.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA_VERSION = 2
RUN_ACTIVE_STATES = ("running", "paused", "partial", "failed")
ARTIST_RETRY_STATES = ("pending", "running", "deferred", "failed")
ARTIST_UNFOLLOWED_STATE = "unfollowed"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _encode_cursor(cursor: dict | None) -> str:
    return json.dumps(cursor or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_cursor(payload: str | None) -> dict:
    if not payload:
        return {}
    try:
        value = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True, slots=True)
class FollowArtistSnapshot:
    artist_id: str
    artist_name: str = ""


@dataclass(frozen=True, slots=True)
class FollowCrawlRun:
    run_id: str
    account_id: str
    config_key: str
    start_mode: str
    start_value: str
    start_ordinal: int
    status: str
    total_artists: int
    stop_reason: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ArtistProgress:
    run_id: str
    artist_id: str
    artist_name: str
    ordinal: int
    state: str
    cursor: dict
    item_index: int
    last_illust_id: str
    attempts: int
    works_seen: int
    queued_items: int
    last_error_kind: str
    last_error_message: str
    retry_at: str
    updated_at: str
    newest_illust_id: str = ""
    swept_at: str = ""


class FollowCrawlStateRepository:
    """Small transactional repository for crawl runs and artist checkpoints."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).expanduser().resolve()
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._connection is not None:
                return self._connection
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.database_path,
                timeout=5.0,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            self._create_schema(connection)
            self._connection = connection
            return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crawl_runs (
                run_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                config_key TEXT NOT NULL,
                start_mode TEXT NOT NULL,
                start_value TEXT NOT NULL DEFAULT '',
                start_ordinal INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                total_artists INTEGER NOT NULL DEFAULT 0,
                stop_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artist_progress (
                run_id TEXT NOT NULL,
                artist_id TEXT NOT NULL,
                artist_name TEXT NOT NULL DEFAULT '',
                ordinal INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                cursor_json TEXT NOT NULL DEFAULT '{}',
                item_index INTEGER NOT NULL DEFAULT 0,
                last_illust_id TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                works_seen INTEGER NOT NULL DEFAULT 0,
                queued_items INTEGER NOT NULL DEFAULT 0,
                last_error_kind TEXT NOT NULL DEFAULT '',
                last_error_message TEXT NOT NULL DEFAULT '',
                retry_at TEXT NOT NULL DEFAULT '',
                newest_illust_id TEXT NOT NULL DEFAULT '',
                swept_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, artist_id),
                UNIQUE (run_id, ordinal),
                FOREIGN KEY (run_id) REFERENCES crawl_runs(run_id) ON DELETE CASCADE
            ) WITHOUT ROWID;

            CREATE INDEX IF NOT EXISTS idx_crawl_runs_resume
                ON crawl_runs(account_id, config_key, status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_artist_progress_state
                ON artist_progress(run_id, state, ordinal);
            """)
        # v1 → v2: incremental-sweep columns.  Column-presence checks keep the
        # migration idempotent even if a previous attempt was interrupted.
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(artist_progress)").fetchall()
        }
        if "newest_illust_id" not in columns:
            connection.execute(
                "ALTER TABLE artist_progress ADD COLUMN newest_illust_id TEXT NOT NULL DEFAULT ''"
            )
        if "swept_at" not in columns:
            connection.execute(
                "ALTER TABLE artist_progress ADD COLUMN swept_at TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        connection.commit()

    def create_run(
        self,
        *,
        account_id: str,
        config_key: str,
        artists: Sequence[FollowArtistSnapshot],
        start_mode: str,
        start_value: str = "",
        start_ordinal: int = 1,
    ) -> FollowCrawlRun:
        now = _utc_now()
        run_id = uuid.uuid4().hex
        normalized_start = max(1, int(start_ordinal))
        rows = [
            (
                run_id,
                str(artist.artist_id),
                str(artist.artist_name or ""),
                ordinal,
                now,
            )
            for ordinal, artist in enumerate(artists, start=1)
        ]
        with self._lock:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO crawl_runs(
                        run_id, account_id, config_key, start_mode, start_value,
                        start_ordinal, status, total_artists, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(account_id),
                        str(config_key),
                        str(start_mode),
                        str(start_value or ""),
                        normalized_start,
                        len(rows),
                        now,
                        now,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO artist_progress(
                        run_id, artist_id, artist_name, ordinal, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_run(run_id)

    def find_resumable_run(self, account_id: str, config_key: str) -> FollowCrawlRun | None:
        placeholders = ",".join("?" for _ in RUN_ACTIVE_STATES)
        query = f"""
            SELECT * FROM crawl_runs
            WHERE account_id=? AND config_key=? AND status IN ({placeholders})
            ORDER BY updated_at DESC LIMIT 1
        """
        with self._lock:
            row = (
                self._connect()
                .execute(
                    query,
                    (str(account_id), str(config_key), *RUN_ACTIVE_STATES),
                )
                .fetchone()
            )
        return self._run_from_row(row) if row is not None else None

    def find_latest_run(self, account_id: str, config_key: str) -> FollowCrawlRun | None:
        """Return the most recent run in any status (including completed)."""

        with self._lock:
            row = (
                self._connect()
                .execute(
                    """
                SELECT * FROM crawl_runs
                WHERE account_id=? AND config_key=?
                ORDER BY updated_at DESC LIMIT 1
                """,
                    (str(account_id), str(config_key)),
                )
                .fetchone()
            )
        return self._run_from_row(row) if row is not None else None

    def reopen_run(self, run_id: str) -> None:
        """Move a finished run back to ``running`` so it can be continued."""

        now = _utc_now()
        with self._lock:
            connection = self._connect()
            connection.execute(
                "UPDATE crawl_runs SET status='running', "
                "stop_reason='', updated_at=? WHERE run_id=?",
                (now, str(run_id)),
            )
            connection.commit()

    def refresh_run_totals(self, run_id: str) -> FollowCrawlRun:
        """Recompute ``total_artists`` from the snapshot table and return the run."""

        now = _utc_now()
        with self._lock:
            connection = self._connect()
            connection.execute(
                """
                UPDATE crawl_runs
                SET total_artists=(
                    SELECT COUNT(*) FROM artist_progress WHERE run_id=?
                ), updated_at=?
                WHERE run_id=?
                """,
                (str(run_id), now, str(run_id)),
            )
            connection.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> FollowCrawlRun:
        with self._lock:
            row = (
                self._connect()
                .execute(
                    "SELECT * FROM crawl_runs WHERE run_id=?",
                    (str(run_id),),
                )
                .fetchone()
            )
        if row is None:
            raise KeyError(f"unknown crawl run: {run_id}")
        return self._run_from_row(row)

    def list_artists(
        self,
        run_id: str,
        *,
        states: Iterable[str] | None = None,
        start_ordinal: int | None = None,
    ) -> list[ArtistProgress]:
        clauses = ["run_id=?"]
        parameters: list[object] = [str(run_id)]
        normalized_states = tuple(states or ())
        if normalized_states:
            clauses.append(f"state IN ({','.join('?' for _ in normalized_states)})")
            parameters.extend(normalized_states)
        if start_ordinal is not None:
            clauses.append("ordinal>=?")
            parameters.append(max(1, int(start_ordinal)))
        query = f"SELECT * FROM artist_progress WHERE {' AND '.join(clauses)} ORDER BY ordinal"
        with self._lock:
            rows = self._connect().execute(query, parameters).fetchall()
        return [self._artist_from_row(row) for row in rows]

    def mark_run(self, run_id: str, status: str, stop_reason: str = "") -> None:
        now = _utc_now()
        with self._lock:
            connection = self._connect()
            connection.execute(
                "UPDATE crawl_runs SET status=?, stop_reason=?, updated_at=? WHERE run_id=?",
                (str(status), str(stop_reason or "")[:500], now, str(run_id)),
            )
            connection.commit()

    def mark_artist_running(self, run_id: str, artist_id: str) -> None:
        now = _utc_now()
        with self._lock:
            connection = self._connect()
            connection.execute(
                """
                UPDATE artist_progress
                SET state='running', attempts=attempts+1,
                    last_error_kind='', last_error_message='', retry_at='', updated_at=?
                WHERE run_id=? AND artist_id=?
                """,
                (now, str(run_id), str(artist_id)),
            )
            connection.execute(
                "UPDATE crawl_runs SET status='running', "
                "stop_reason='', updated_at=? WHERE run_id=?",
                (now, str(run_id)),
            )
            connection.commit()

    def append_artists(self, run_id: str, artists: Sequence[FollowArtistSnapshot]) -> int:
        """Append newly followed artists to an existing snapshot."""

        if not artists:
            return 0
        now = _utc_now()
        with self._lock:
            connection = self._connect()
            row = connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) FROM artist_progress WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
            next_ordinal = int(row[0]) + 1
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    """
                    INSERT INTO artist_progress(
                        run_id, artist_id, artist_name, ordinal, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            str(run_id),
                            str(artist.artist_id),
                            str(artist.artist_name or ""),
                            ordinal,
                            now,
                        )
                        for ordinal, artist in enumerate(artists, start=next_ordinal)
                    ],
                )
                connection.execute(
                    "UPDATE crawl_runs SET updated_at=? WHERE run_id=?",
                    (now, str(run_id)),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return len(artists)

    def _set_artists_state(self, run_id: str, artist_ids: Iterable[str], state: str) -> int:
        ids = [str(artist_id) for artist_id in artist_ids]
        if not ids:
            return 0
        now = _utc_now()
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            connection = self._connect()
            cursor = connection.execute(
                f"""
                UPDATE artist_progress
                SET state=?, last_error_kind='', last_error_message='', retry_at='', updated_at=?
                WHERE run_id=? AND artist_id IN ({placeholders})
                """,
                (str(state), now, str(run_id), *ids),
            )
            connection.execute(
                "UPDATE crawl_runs SET updated_at=? WHERE run_id=?",
                (now, str(run_id)),
            )
            connection.commit()
        return int(cursor.rowcount or 0)

    def mark_artists_unfollowed(self, run_id: str, artist_ids: Iterable[str]) -> int:
        """Terminal state for artists no longer followed; never crawled again."""

        return self._set_artists_state(run_id, artist_ids, ARTIST_UNFOLLOWED_STATE)

    def revive_artists(self, run_id: str, artist_ids: Iterable[str]) -> int:
        """Re-queue previously unfollowed artists that are followed again."""

        return self._set_artists_state(run_id, artist_ids, "pending")

    def update_newest_illust(self, run_id: str, artist_id: str, newest_illust_id: str) -> None:
        """Record the newest work seen for an artist (incremental sweep cutoff)."""

        newest = str(newest_illust_id or "")
        if not newest:
            return
        now = _utc_now()
        with self._lock:
            connection = self._connect()
            connection.execute(
                """
                UPDATE artist_progress
                SET newest_illust_id=?, updated_at=?
                WHERE run_id=? AND artist_id=?
                """,
                (newest, now, str(run_id), str(artist_id)),
            )
            connection.commit()

    def list_artists_for_sweep(
        self, run_id: str, *, before: str | None = None
    ) -> list[ArtistProgress]:
        """Completed artists swept at or before ``before`` (default: now).

        The comparison is inclusive on purpose: timestamps are
        second-precision, so two batches within the same second must still
        see each other's completed artists.  Artists crawled during the
        current batch are excluded by the caller in memory instead.
        """

        cutoff = str(before or _utc_now())
        with self._lock:
            rows = (
                self._connect()
                .execute(
                    """
                SELECT * FROM artist_progress
                WHERE run_id=? AND state='completed' AND (swept_at='' OR swept_at<=?)
                ORDER BY ordinal
                """,
                    (str(run_id), cutoff),
                )
                .fetchall()
            )
        return [self._artist_from_row(row) for row in rows]

    def mark_artist_swept(
        self, run_id: str, artist_id: str, *, queued_delta: int, newest_illust_id: str
    ) -> None:
        """Stamp a finished incremental sweep without disturbing crawl state."""

        now = _utc_now()
        with self._lock:
            connection = self._connect()
            connection.execute(
                """
                UPDATE artist_progress
                SET swept_at=?,
                    queued_items=queued_items+?,
                    newest_illust_id=COALESCE(NULLIF(?, ''), newest_illust_id),
                    updated_at=?
                WHERE run_id=? AND artist_id=?
                """,
                (
                    now,
                    max(0, int(queued_delta)),
                    str(newest_illust_id or ""),
                    now,
                    str(run_id),
                    str(artist_id),
                ),
            )
            connection.execute(
                "UPDATE crawl_runs SET updated_at=? WHERE run_id=?",
                (now, str(run_id)),
            )
            connection.commit()

    def checkpoint_artist(
        self,
        run_id: str,
        artist_id: str,
        *,
        cursor: dict | None,
        item_index: int,
        last_illust_id: str,
        works_seen: int,
        queued_items: int,
    ) -> None:
        now = _utc_now()
        with self._lock:
            connection = self._connect()
            connection.execute(
                """
                UPDATE artist_progress
                SET cursor_json=?, item_index=?, last_illust_id=?, works_seen=?,
                    queued_items=?, updated_at=?
                WHERE run_id=? AND artist_id=?
                """,
                (
                    _encode_cursor(cursor),
                    max(0, int(item_index)),
                    str(last_illust_id or ""),
                    max(0, int(works_seen)),
                    max(0, int(queued_items)),
                    now,
                    str(run_id),
                    str(artist_id),
                ),
            )
            connection.execute(
                "UPDATE crawl_runs SET updated_at=? WHERE run_id=?",
                (now, str(run_id)),
            )
            connection.commit()

    def mark_artist_completed(
        self,
        run_id: str,
        artist_id: str,
        *,
        works_seen: int,
        queued_items: int,
        state: str = "completed",
    ) -> None:
        now = _utc_now()
        with self._lock:
            connection = self._connect()
            connection.execute(
                """
                UPDATE artist_progress
                SET state=?, cursor_json='{}', item_index=0, last_illust_id='',
                    works_seen=?, queued_items=?, last_error_kind='',
                    last_error_message='', retry_at='', swept_at=?, updated_at=?
                WHERE run_id=? AND artist_id=?
                """,
                (
                    str(state),
                    max(0, int(works_seen)),
                    max(0, int(queued_items)),
                    now,
                    now,
                    str(run_id),
                    str(artist_id),
                ),
            )
            connection.execute(
                "UPDATE crawl_runs SET updated_at=? WHERE run_id=?",
                (now, str(run_id)),
            )
            connection.commit()

    def mark_artist_deferred(
        self,
        run_id: str,
        artist_id: str,
        *,
        error_kind: str,
        error_message: str,
        retry_at: str = "",
        state: str = "deferred",
    ) -> None:
        now = _utc_now()
        with self._lock:
            connection = self._connect()
            connection.execute(
                """
                UPDATE artist_progress
                SET state=?, last_error_kind=?, last_error_message=?, retry_at=?, updated_at=?
                WHERE run_id=? AND artist_id=?
                """,
                (
                    str(state),
                    str(error_kind or "unknown")[:80],
                    str(error_message or "")[:500],
                    str(retry_at or ""),
                    now,
                    str(run_id),
                    str(artist_id),
                ),
            )
            connection.commit()

    def integrity_check(self) -> str:
        with self._lock:
            return str(self._connect().execute("PRAGMA integrity_check").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> FollowCrawlRun:
        return FollowCrawlRun(
            run_id=str(row["run_id"]),
            account_id=str(row["account_id"]),
            config_key=str(row["config_key"]),
            start_mode=str(row["start_mode"]),
            start_value=str(row["start_value"]),
            start_ordinal=int(row["start_ordinal"]),
            status=str(row["status"]),
            total_artists=int(row["total_artists"]),
            stop_reason=str(row["stop_reason"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _artist_from_row(row: sqlite3.Row) -> ArtistProgress:
        return ArtistProgress(
            run_id=str(row["run_id"]),
            artist_id=str(row["artist_id"]),
            artist_name=str(row["artist_name"]),
            ordinal=int(row["ordinal"]),
            state=str(row["state"]),
            cursor=_decode_cursor(row["cursor_json"]),
            item_index=int(row["item_index"]),
            last_illust_id=str(row["last_illust_id"]),
            attempts=int(row["attempts"]),
            works_seen=int(row["works_seen"]),
            queued_items=int(row["queued_items"]),
            last_error_kind=str(row["last_error_kind"]),
            last_error_message=str(row["last_error_message"]),
            retry_at=str(row["retry_at"]),
            updated_at=str(row["updated_at"]),
            newest_illust_id=str(row["newest_illust_id"]),
            swept_at=str(row["swept_at"]),
        )


__all__ = [
    "ARTIST_RETRY_STATES",
    "ARTIST_UNFOLLOWED_STATE",
    "ArtistProgress",
    "FollowArtistSnapshot",
    "FollowCrawlRun",
    "FollowCrawlStateRepository",
    "RUN_ACTIVE_STATES",
]
