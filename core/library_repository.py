#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 single-library persistence boundary for the new collection core.

This module owns one SQLite database (``library.sqlite3`` by convention) and
never opens the legacy three databases.  It implements immutable jobs, queued
and active batches, page deliveries, source checkpoints, command idempotency
and local recovery facts.  It deliberately does not implement file publication
or artifact completion; those belong to the P3 delivery protocol.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from core.collection_scope import scope_fingerprint
from core.delivery_repository import (
    DELIVERY_DDL,
    DELIVERY_REQUIRED_TABLES,
    DeliveryRepositoryMixin,
    _artifact_is_valid,
    path_ref_from_object,
)
from core.follow_repository import FOLLOW_DDL, FOLLOW_TABLES, FOLLOW_COMMANDS, FollowRepositoryMixin
from core.query_repository import QUERY_DDL, QUERY_TABLES, QUERY_COMMANDS, QueryRepositoryMixin
from core.library_paths import InstanceLock
from core.task_contracts import (
    BatchBudget,
    CollectionRequest,
    ContractError,
    PageSpec,
    PathRef,
    SourcePage,
    canonical_json,
    normalize_json,
)

_LIBRARY_FORMAT = "jpgpng-library"
_SCHEMA_VERSION = "1"
_MAX_COMMAND_BYTES = 8 * 1024 * 1024
_MAX_PENDING_BYTES = 8 * 1024 * 1024
_BUSY_TIMEOUT_MS = 30_000
_MAX_DISPATCH_PAGE = 200
_MAX_EXCLUDED_IDS = 200
_MAX_EXCLUDE_ID_LEN = 512

# A failed constructor whose writer refused to terminate must keep its OS lock
# alive instead of pretending the resource is closed.
_RETAINED_FAILED_LOCKS: set[InstanceLock] = set()

_DISPATCHABLE_STATES = ("pending", "suspended", "retry_wait")
_INFLIGHT_STATES = (
    "pending",
    "transferring",
    "validating",
    "prepared",
    "needs_verification",
    "suspended",
    "retry_wait",
    "recovery_needed",
)
_DECISIONS = (
    "download",
    "filtered",
    "metadata_unknown",
    "unavailable",
    "unsupported",
    "skipped_existing",
)
_RESULT_STATES = (
    "pending",
    "filtered",
    "metadata_unknown",
    "unavailable",
    "unsupported",
    "skipped_existing",
    "completed",
    "partial",
    "needs_verification",
    "failed",
    "cancelled",
)
_DELIVERY_STATES = (
    "pending",
    "transferring",
    "validating",
    "prepared",
    "needs_verification",
    "suspended",
    "retry_wait",
    "failed",
    "completed",
    "cancelled",
    "recovery_needed",
)
_JOB_STATES = (
    "queued",
    "running",
    "paused",
    "blocked",
    "partial",
    "pausing",
    "cancelling",
    "settling",
    "cancelled",
    "completed",
)
_BATCH_PHASES = (
    "queued",
    "running",
    "cooling",
    "pausing",
    "cancelling",
    "settling",
    "partial",
    "paused",
    "cancelled",
    "blocked",
    "completed",
)

_REQUIRED_TABLES = frozenset(
    {
        "schema_meta",
        "jobs",
        "batches",
        "batch_items",
        "batch_item_pages",
        "deliveries",
        "artifacts",
        "checkpoints",
        "source_items",
        "source_pages",
        "events",
        "legacy_imports",
    }
) | DELIVERY_REQUIRED_TABLES | FOLLOW_TABLES | QUERY_TABLES

_COMMAND_ID_RE = re.compile(r"^[^\x00-\x1f]{1,128}$")
_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{0,64}$")
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")


class LibraryError(Exception):
    """Base error for the new library boundary."""


class SchemaVersionError(LibraryError):
    """The existing file is not the expected product/schema version."""


class CommandConflict(LibraryError):
    """A command id was reused with a different type, target or payload."""


class CursorConflict(LibraryError):
    """A source page cursor or page identity conflicts with persisted facts."""


class OutputConflict(LibraryError):
    """An output path is outside its root or already owned by an active writer."""


class BudgetExceeded(LibraryError):
    """The batch work budget would be exceeded by a page."""


class SourceCooling(LibraryError):
    """The account is within a persisted server-requested cooldown."""
    def __init__(self,retry_at):
        self.retry_at=retry_at
        super().__init__('source_cooling')


class PageCoverageError(LibraryError):
    """Required pages are incomplete or violate the selected page policy."""


class NotFoundError(LibraryError):
    """A requested persistent object does not exist."""


class CorruptDataError(LibraryError):
    """A stored JSON/record is corrupt; never return an empty surrogate."""


class InvalidStateError(LibraryError):
    """The requested transition is not legal for the current state."""


class LibraryClosedError(LibraryError):
    """The repository has been closed (or is closing)."""


@dataclass(slots=True)
class _Command:
    kind: str
    payload: dict[str, Any]
    future: Future
    size_bytes: int


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _retry_due_now() -> str:
    """A retry_wait marker that is unambiguously due for sqlite julianday().

    ``julianday('now')`` only resolves to the millisecond, so a marker stamped
    at the current microsecond can look a fraction of a millisecond in the
    future and hide an immediately retryable page.  A marker that means "due
    now" is therefore stamped just inside the past.
    """
    return (
        datetime.now(timezone.utc) - timedelta(milliseconds=2)
    ).isoformat(timespec="microseconds")


def _new_id() -> str:
    return uuid.uuid4().hex


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_command_id(command_id: object) -> str:
    if not isinstance(command_id, str) or _COMMAND_ID_RE.fullmatch(command_id) is None:
        raise LibraryError("Invalid command id")
    return command_id


def _validate_error_code(error_code: object) -> str:
    if not isinstance(error_code, str) or _ERROR_CODE_RE.fullmatch(error_code) is None:
        raise LibraryError("Invalid error code")
    return error_code


def _validate_stop_reason(reason_code: object, cancel: bool) -> str:
    if reason_code is None:
        return "user_cancel" if cancel else "user_pause"
    if not isinstance(reason_code, str) or _REASON_CODE_RE.fullmatch(reason_code) is None:
        raise LibraryError("Invalid stop reason code")
    return reason_code


def _validate_database_path(database_path: str | Path) -> Path:
    """Reject UNC/extended and linked locations before any resolution/write."""
    raw = Path(database_path).expanduser()
    text = str(raw)
    if text.startswith("\\\\") or text.startswith("//"):
        raise LibraryError("UNC and extended database paths are not supported")
    absolute = Path(os.path.abspath(text))
    absolute_text = str(absolute)
    if absolute_text.startswith("\\\\") or absolute_text.startswith("//"):
        raise LibraryError("UNC and extended database paths are not supported")
    for item in (absolute, *absolute.parents):
        if item.is_symlink() or (
            hasattr(item, "is_junction") and item.is_junction()
        ):
            raise LibraryError("Linked database paths are not supported")
    return absolute.resolve()


def _validate_identifier(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(char) < 32 for char in value)
    ):
        raise LibraryError(f"Invalid {name}")
    return value


def _retry_due(retry_at: object) -> bool:
    if not retry_at:
        return True
    if not isinstance(retry_at, str):
        return True
    try:
        due = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
    except ValueError:
        # A malformed local retry marker must not strand a persistent todo.
        return True
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due <= datetime.now(timezone.utc)


def _parse_json_object(text: object, what: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise CorruptDataError(f"Corrupt {what}: not text")
    try:
        value = json.loads(text)
    except (TypeError, ValueError, RecursionError) as exc:
        raise CorruptDataError(f"Corrupt {what}") from exc
    if not isinstance(value, dict):
        raise CorruptDataError(f"Corrupt {what}: expected object")
    return value


def _validate_json_text(text: object, what: str, *, object_only: bool = False) -> None:
    if not isinstance(text, str):
        raise CorruptDataError(f"Corrupt {what}: not text")
    try:
        normalize_json(text, object_only=object_only)
    except ContractError as exc:
        raise CorruptDataError(f"Corrupt {what}") from exc


_DDL = (
    """
    CREATE TABLE schema_meta (
        "key" TEXT PRIMARY KEY,
        "value" TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY,
        parent_job_id TEXT,
        request_id TEXT NOT NULL,
        request_version INTEGER NOT NULL,
        request_json TEXT NOT NULL,
        scope_fingerprint TEXT NOT NULL,
        account_ref TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN (
            'queued','running','paused','blocked','partial','pausing',
            'cancelling','settling','cancelled','completed'
        )),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE batches (
        batch_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        command_id TEXT NOT NULL UNIQUE,
        budget_json TEXT NOT NULL,
        phase TEXT NOT NULL CHECK (phase IN (
            'queued','running','cooling','pausing','cancelling','settling',
            'partial','paused','cancelled','blocked','completed'
        )),
        stop_reason TEXT,
        stop_target TEXT CHECK (
            stop_target IS NULL OR stop_target IN ('paused','cancelled','blocked')
        ),
        coverage_complete INTEGER CHECK (
            coverage_complete IS NULL OR coverage_complete IN (0, 1)
        ),
        active_slot INTEGER,
        created_at TEXT NOT NULL,
        started_at TEXT,
        ended_at TEXT,
        CHECK (active_slot IS NULL OR active_slot = 1),
        CHECK (
            (active_slot IS NULL AND phase NOT IN (
                'running','cooling','pausing','cancelling','settling'
            ))
            OR (active_slot = 1 AND phase IN (
                'running','cooling','pausing','cancelling','settling'
            ))
        )
    )
    """,
    """
    CREATE TABLE batch_items (
        batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
        illust_id TEXT NOT NULL,
        artist_id TEXT,
        artist_name TEXT,
        title TEXT,
        page_count INTEGER CHECK (
            page_count IS NULL OR (page_count >= 1 AND page_count <= 10000)
        ),
        decision TEXT NOT NULL CHECK (decision IN (
            'download','filtered','metadata_unknown','unavailable',
            'unsupported','skipped_existing'
        )),
        counts_work INTEGER NOT NULL DEFAULT 0 CHECK (counts_work IN (0,1)),
        reason_codes_json TEXT NOT NULL,
        required_pages_json TEXT NOT NULL,
        result_state TEXT NOT NULL CHECK (result_state IN (
            'pending','filtered','metadata_unknown','unavailable','unsupported',
            'skipped_existing','completed','partial','needs_verification',
            'failed','cancelled'
        )),
        bookmarks INTEGER CHECK (bookmarks IS NULL OR bookmarks >= 0),
        ai_flag INTEGER CHECK (ai_flag IS NULL OR ai_flag IN (0, 1)),
        provenance_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (batch_id, illust_id)
    )
    """,
    """
    CREATE TABLE deliveries (
        delivery_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        origin_batch_id TEXT NOT NULL REFERENCES batches(batch_id),
        current_batch_id TEXT REFERENCES batches(batch_id),
        completed_batch_id TEXT REFERENCES batches(batch_id),
        illust_id TEXT NOT NULL,
        page_index INTEGER NOT NULL CHECK (page_index >= 0 AND page_index <= 9999),
        output_key TEXT NOT NULL,
        output_ref_json TEXT NOT NULL,
        quality_json TEXT NOT NULL,
        quality_key TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        source_identity TEXT,
        state TEXT NOT NULL CHECK (state IN (
            'pending','transferring','validating','prepared','needs_verification',
            'suspended','retry_wait','failed','completed','cancelled','recovery_needed'
        )),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        http_attempts INTEGER NOT NULL DEFAULT 0 CHECK (http_attempts >= 0),
        retry_at TEXT,
        error_code TEXT CHECK (error_code IS NULL OR length(error_code) <= 64),
        generation INTEGER NOT NULL CHECK (generation >= 1),
        checkpoint_json TEXT,
        prepared_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (job_id, illust_id, page_index, quality_key, generation)
    )
    """,
    """
    CREATE TABLE artifacts (
        artifact_id TEXT PRIMARY KEY,
        delivery_id TEXT NOT NULL UNIQUE REFERENCES deliveries(delivery_id),
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        illust_id TEXT NOT NULL,
        page_index INTEGER NOT NULL CHECK (page_index >= 0 AND page_index <= 9999),
        artist_id TEXT,
        output_key TEXT NOT NULL,
        output_ref_json TEXT NOT NULL,
        quality_json TEXT NOT NULL,
        media_type TEXT,
        size INTEGER CHECK (size IS NULL OR size > 0),
        sha256 TEXT CHECK (
            sha256 IS NULL OR (
                length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
        verification TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        legacy_origin TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE checkpoints (
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        source_id TEXT NOT NULL,
        pass_id TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        artist_id TEXT,
        ordinal INTEGER CHECK (ordinal IS NULL OR ordinal >= 0),
        cursor_json TEXT NOT NULL,
        item_anchor TEXT,
        exhausted INTEGER NOT NULL CHECK (exhausted IN (0, 1)),
        coverage_json TEXT NOT NULL,
        actual_sort TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (job_id, source_id, pass_id, entity_id)
    )
    """,
    """
    CREATE TABLE source_items (
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        source_id TEXT NOT NULL,
        pass_id TEXT NOT NULL,
        occurrence_key TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (job_id, source_id, pass_id, occurrence_key)
    )
    """,
    """
    CREATE TABLE source_pages (
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        page_identity TEXT NOT NULL,
        source_id TEXT NOT NULL,
        pass_id TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        batch_id TEXT NOT NULL REFERENCES batches(batch_id),
        content_digest TEXT NOT NULL,
        delivery_ids_json TEXT NOT NULL,
        accepted_at TEXT NOT NULL,
        PRIMARY KEY (job_id, source_id, pass_id, entity_id, page_identity)
    )
    """,
    """
    CREATE TABLE events (
        event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_uuid TEXT NOT NULL UNIQUE,
        accepted_command_id TEXT UNIQUE,
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        batch_id TEXT REFERENCES batches(batch_id),
        kind TEXT NOT NULL,
        safe_payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE legacy_imports (
        import_id TEXT PRIMARY KEY,
        source_digest TEXT NOT NULL,
        source_format TEXT NOT NULL,
        scope TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN (
            'pending','running','completed','failed','cancelled'
        )),
        cursor TEXT,
        counts_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    "CREATE INDEX idx_jobs_state ON jobs(state, created_at)",
    "CREATE INDEX idx_batches_job_started ON batches(job_id, started_at)",
    "CREATE INDEX idx_batches_phase_created ON batches(phase, created_at, batch_id)",
    "CREATE UNIQUE INDEX ux_batches_single_active "
    "ON batches(active_slot) WHERE active_slot IS NOT NULL",
    "CREATE INDEX idx_batch_items_batch_decision "
    "ON batch_items(batch_id, decision, illust_id)",
    "CREATE INDEX idx_batch_items_artist ON batch_items(artist_id, batch_id)",
    "CREATE INDEX idx_deliveries_state_retry ON deliveries(state, retry_at)",
    "CREATE INDEX idx_deliveries_job_work_page "
    "ON deliveries(job_id, illust_id, page_index)",
    "CREATE INDEX idx_deliveries_current_batch "
    "ON deliveries(current_batch_id, state)",
    "CREATE UNIQUE INDEX ux_deliveries_active_output ON deliveries(output_key) "
    "WHERE state IN ('pending','transferring','validating','prepared',"
    "'needs_verification','suspended','retry_wait','recovery_needed')",
    "CREATE INDEX idx_artifacts_artist_completed "
    "ON artifacts(artist_id, completed_at, artifact_id)",
    "CREATE INDEX idx_artifacts_completed "
    "ON artifacts(completed_at DESC, artifact_id DESC)",
    "CREATE INDEX idx_batch_items_created "
    "ON batch_items(batch_id, created_at DESC, illust_id DESC)",
    "CREATE INDEX idx_source_cooldown_job "
    "ON events(job_id, event_seq) WHERE kind = 'source_cooldown'",
    "CREATE INDEX idx_artifacts_work_page ON artifacts(illust_id, page_index)",
    "CREATE INDEX idx_artifacts_output ON artifacts(output_key)",
    "CREATE INDEX idx_checkpoints_job_source_pass "
    "ON checkpoints(job_id, source_id, pass_id)",
    "CREATE INDEX idx_source_items_entity "
    "ON source_items(job_id, source_id, pass_id, entity_id)",
    "CREATE INDEX idx_source_pages_batch ON source_pages(batch_id)",
    "CREATE INDEX idx_events_job_seq ON events(job_id, event_seq)",
    "CREATE INDEX idx_events_batch_seq ON events(batch_id, event_seq)",
    "CREATE INDEX idx_legacy_imports_source "
    "ON legacy_imports(source_digest, import_id)",
    *DELIVERY_DDL,
    *FOLLOW_DDL,
    *QUERY_DDL,
)


class LibraryRepository(DeliveryRepositoryMixin, FollowRepositoryMixin, QueryRepositoryMixin):
    """Own one library database through a dedicated non-daemon writer thread."""

    def __init__(self, database_path: str | Path, *, queue_capacity: int = 128):
        if type(queue_capacity) is not int or queue_capacity < 1:
            raise LibraryError("queue_capacity must be a positive integer")
        self._path = _validate_database_path(database_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._queue_capacity = queue_capacity
        self._commands: queue.Queue = queue.Queue(maxsize=queue_capacity)
        self._admission = threading.Condition()
        self._admitted_bytes = 0
        self._accepting = threading.Event()
        self._accepting.set()
        self._closed = False
        self._close_lock = threading.RLock()
        self._ready = threading.Event()
        self._writer_done = threading.Event()
        self._init_error: BaseException | None = None
        self._needs_initialize = False
        self._writer_thread: threading.Thread | None = None
        self._lock: InstanceLock | None = None
        self._lock_acquired = False

        self._lock = InstanceLock(self._path.parent / ".writer.lock")
        try:
            self._lock.acquire()
            self._lock_acquired = True
            self._needs_initialize = self._inspect_existing()
            self._writer_thread = threading.Thread(
                target=self._writer_main,
                name=f"jpgpng-library-writer-{self._path.name}",
                daemon=False,
            )
            self._writer_thread.start()
            if not self._ready.wait(30):
                raise LibraryError("writer initialization timed out")
            if self._init_error is not None:
                raise self._init_error
        except BaseException as exc:
            with self._admission:
                self._accepting.clear()
                self._admission.notify_all()
            thread = self._writer_thread
            if thread is not None and thread.is_alive():
                thread.join(30)
            if thread is not None and thread.is_alive():
                # Do not release an OS lock while the actual writer is alive.
                if self._lock is not None:
                    _RETAINED_FAILED_LOCKS.add(self._lock)
                self._closed = False
                raise LibraryError(
                    "Initialization failed before the writer stopped; writer lock retained"
                ) from exc
            self._release_lock()
            self._closed = True
            raise

    @property
    def database_path(self) -> Path:
        return self._path

    def __enter__(self) -> "LibraryRepository":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Initialization and schema validation
    # ------------------------------------------------------------------

    def _readonly_connection(self) -> sqlite3.Connection:
        # A live library may have newer committed facts only in its WAL.
        # Fail closed on read errors; immutable=1 would silently ignore that
        # WAL. One explicit short read transaction also keeps phase, counters
        # and event revision in the same snapshot.
        conn = sqlite3.connect(
            self._path.as_uri() + "?mode=ro", uri=True,
            timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None,
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            return conn
        except BaseException:
            conn.close()
            raise

    def _inspect_existing(self) -> bool:
        """Return True for a new/empty database; reject unknown products/schemas.

        This runs through a read-only connection and never executes a write
        PRAGMA or DDL against an existing file.
        """
        if not self._path.exists() or self._path.stat().st_size == 0:
            return True
        try:
            conn = self._readonly_connection()
        except sqlite3.Error as exc:
            raise SchemaVersionError("Existing database is not a readable SQLite library") from exc
        try:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "schema_meta" not in tables:
                if not (tables - {"sqlite_sequence"}):
                    return True
                raise SchemaVersionError("Existing database has no library schema marker")
            meta: dict[str, str] = {}
            for row in conn.execute('SELECT "key", "value" FROM schema_meta').fetchall():
                key = str(row[0])
                if key in meta:
                    raise SchemaVersionError("Duplicate library schema marker")
                meta[key] = str(row[1])
            if meta.get("format") != _LIBRARY_FORMAT:
                raise SchemaVersionError("Different product/schema format")
            if meta.get("schema_version") != _SCHEMA_VERSION:
                raise SchemaVersionError("Unsupported library schema version")
            missing = _REQUIRED_TABLES - tables
            if missing:
                raise SchemaVersionError(
                    "Library schema is incomplete: " + ",".join(sorted(missing))
                )
            # During pre-production v1 development, old isolated trials can
            # share the version marker but have incompatible keys/checks.
            # Validate the known SQL objects before executing write PRAGMAs.
            actual = {
                str(row["name"]): " ".join(str(row["sql"]).split())
                for row in conn.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'"
                )
            }
            for statement in _DDL:
                match = re.match(r"\s*CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+(\w+)", statement, re.IGNORECASE)
                if match is None:
                    raise SchemaVersionError("Unrecognized internal schema declaration")
                name = match.group(1)
                if actual.get(name) != " ".join(statement.split()):
                    raise SchemaVersionError(f"Incompatible library schema object: {name}")
            return False
        except SchemaVersionError:
            raise
        except sqlite3.Error as exc:
            raise SchemaVersionError("Existing database schema cannot be read") from exc
        finally:
            conn.close()

    def _writer_main(self) -> None:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                str(self._path),
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            if self._needs_initialize:
                with self._transaction(conn):
                    for statement in _DDL:
                        conn.execute(statement)
                    conn.execute(
                        'INSERT INTO schema_meta("key", "value") VALUES (?, ?)',
                        ("format", _LIBRARY_FORMAT),
                    )
                    conn.execute(
                        'INSERT INTO schema_meta("key", "value") VALUES (?, ?)',
                        ("schema_version", _SCHEMA_VERSION),
                    )
            self._ready.set()
        except BaseException as exc:
            self._init_error = exc
            self._ready.set()
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self._writer_done.set()
            return

        fatal_error: BaseException | None = None
        try:
            while True:
                try:
                    command = self._commands.get(timeout=0.1)
                except queue.Empty:
                    with self._admission:
                        if (
                            not self._accepting.is_set()
                            and self._commands.empty()
                        ):
                            break
                    continue
                with self._admission:
                    # A queue slot is free even though the command's bytes are
                    # still counted until the writer finishes processing it.
                    self._admission.notify_all()
                try:
                    try:
                        result = self._dispatch(conn, command)
                    except Exception as exc:
                        if not command.future.done():
                            command.future.set_exception(exc)
                    else:
                        if not command.future.done():
                            command.future.set_result(result)
                except BaseException as exc:
                    fatal_error = exc
                    if not command.future.done():
                        command.future.set_exception(exc)
                    break
                finally:
                    with self._admission:
                        self._admitted_bytes -= command.size_bytes
                        self._admission.notify_all()
        finally:
            with self._admission:
                self._accepting.clear()
                self._drain_admitted_commands(fatal_error)
                self._admission.notify_all()
            if conn is not None:
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            self._writer_done.set()

    def _drain_admitted_commands(self, error: BaseException | None = None) -> None:
        """Fail every queued Future; caller holds the admission condition."""
        failure = error if error is not None else LibraryClosedError(
            "Repository writer stopped"
        )
        while True:
            try:
                item = self._commands.get_nowait()
            except queue.Empty:
                return
            if not item.future.done():
                item.future.set_exception(failure)
            self._admitted_bytes -= item.size_bytes

    @contextmanager
    def _transaction(self, conn: sqlite3.Connection) -> Iterator[None]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        else:
            try:
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    @staticmethod
    def _get_job_row(conn: sqlite3.Connection, job_id: str):
        return conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()

    @staticmethod
    def _get_batch_row(conn: sqlite3.Connection, batch_id: str):
        return conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()

    @staticmethod
    def _get_delivery_row(conn: sqlite3.Connection, delivery_id: str):
        return conn.execute(
            "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: str,
        batch_id: str | None,
        kind: str,
        payload: dict[str, Any],
        accepted_command_id: str | None = None,
    ) -> int:
        safe_payload = canonical_json(payload)
        cursor = conn.execute(
            "INSERT INTO events("
            "event_uuid, accepted_command_id, job_id, batch_id, kind, "
            "safe_payload, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _new_id(),
                accepted_command_id,
                job_id,
                batch_id,
                kind,
                safe_payload,
                _utcnow(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def _command_event(
        self, conn: sqlite3.Connection, command_id: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT safe_payload FROM events WHERE accepted_command_id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        info = _parse_json_object(row["safe_payload"], "command event payload")
        return info

    @staticmethod
    def _command_result(info: dict[str, Any]) -> dict[str, Any]:
        result = info.get("result")
        if not isinstance(result, dict):
            raise CorruptDataError("Corrupt command result")
        return dict(result)

    def _release_lock(self) -> None:
        if self._lock is not None and self._lock_acquired:
            self._lock.release()
            self._lock_acquired = False

    # ------------------------------------------------------------------
    # Command transport and dispatch
    # ------------------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise LibraryClosedError("Repository is closed")

    def _enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        size_payload: object | None = None,
    ) -> Any:
        try:
            measured = canonical_json(
                {"kind": kind, "payload": payload if size_payload is None else size_payload}
            )
        except (ContractError, TypeError, ValueError) as exc:
            raise LibraryError("Command payload violates the JSON contract") from exc
        size_bytes = len(measured.encode("utf-8"))
        # Archive plan commands declare the complete frozen document size; count it
        # against the same admission budget instead of only the compact summary.
        declared_bytes = (
            size_payload.get("payload_bytes") if isinstance(size_payload, dict) else None
        )
        if type(declared_bytes) is int and declared_bytes >= 0:
            size_bytes = max(size_bytes, declared_bytes)
        if size_bytes > _MAX_COMMAND_BYTES:
            raise LibraryError("Command exceeds 8 MiB")

        future: Future = Future()
        command = _Command(
            kind=kind,
            payload=payload,
            future=future,
            size_bytes=size_bytes,
        )
        with self._admission:
            while True:
                if not self._accepting.is_set():
                    raise LibraryClosedError("Repository is closing or closed")
                writer = self._writer_thread
                if writer is None or not writer.is_alive():
                    raise LibraryClosedError("Repository writer is not running")
                if (
                    self._commands.qsize() < self._queue_capacity
                    and self._admitted_bytes + size_bytes <= _MAX_PENDING_BYTES
                ):
                    try:
                        self._commands.put_nowait(command)
                    except queue.Full:
                        self._admission.wait(timeout=0.25)
                        continue
                    self._admitted_bytes += size_bytes
                    break
                self._admission.wait(timeout=0.25)
        return future.result()

    def _dispatch(self, conn: sqlite3.Connection, command: _Command) -> Any:
        if command.kind == "submit":
            return self._submit(conn, command.payload)
        if command.kind == "resume":
            return self._resume(conn, command.payload)
        if command.kind == "activate_next":
            return self._activate_next(conn)
        if command.kind == "accept_source_page":
            return self._accept_source_page(conn, command.payload)
        if command.kind == "claim_delivery":
            return self._claim_delivery(conn, command.payload)
        if command.kind == "release_delivery":
            return self._release_delivery(conn, command.payload)
        if command.kind == "request_stop":
            return self._request_stop(conn, command.payload)
        if command.kind == "settle_stop":
            return self._settle_stop(conn, command.payload["batch_id"])
        if command.kind == "recover_interrupted":
            return self._recover_interrupted(conn)
        if command.kind in self._DELIVERY_COMMAND_KINDS:
            return self._dispatch_delivery(conn, command)
        if command.kind in FOLLOW_COMMANDS:
            return self._dispatch_follow(conn, command)
        if command.kind in QUERY_COMMANDS:
            return self._dispatch_query(conn, command)
        raise LibraryError(f"Unknown library command: {command.kind}")

    # ------------------------------------------------------------------
    # Public commands
    # ------------------------------------------------------------------

    def submit(self, request: CollectionRequest, command_id: str, *, parent_job_id: str | None = None) -> dict[str, str]:
        if not isinstance(request, CollectionRequest):
            raise LibraryError("submit requires CollectionRequest")
        command_id = _validate_command_id(command_id)
        if parent_job_id is not None:
            parent_job_id = _validate_identifier(parent_job_id, "parent job id")
        try:
            request_json = request.to_json()
        except (ContractError, TypeError, ValueError) as exc:
            raise LibraryError("Invalid collection request") from exc
        digest = _sha256_text(request_json)
        payload = {
            "command_id": command_id,
            "request": request,
            "request_json": request_json,
            "request_digest": digest,
            "scope_fingerprint": scope_fingerprint(request),
            "parent_job_id": parent_job_id,
        }
        return self._enqueue(
            "submit",
            payload,
            size_payload={
                "kind": "submit",
                "command_id": command_id,
                "request_json": request_json,
                "parent_job_id": parent_job_id,
            },
        )

    def resume(
        self,
        job_id: str,
        command_id: str,
        budget: BatchBudget | None = None,
        *,
        retry_failed: bool = False,
    ) -> dict[str, str]:
        job_id = _validate_identifier(job_id, "job id")
        command_id = _validate_command_id(command_id)
        if budget is not None and not isinstance(budget, BatchBudget):
            raise LibraryError("resume budget must be BatchBudget or None")
        if type(retry_failed) is not bool:
            raise LibraryError("retry_failed must be boolean")
        budget_json = canonical_json(asdict(budget)) if budget is not None else None
        payload = {
            "command_id": command_id,
            "job_id": job_id,
            "budget": budget,
            "budget_json": budget_json,
            "retry_failed": retry_failed,
        }
        return self._enqueue(
            "resume",
            payload,
            size_payload={
                "kind": "resume",
                "command_id": command_id,
                "job_id": job_id,
                "budget_json": budget_json,
                "retry_failed": retry_failed,
            },
        )

    def activate_next(self) -> dict[str, Any] | None:
        return self._enqueue("activate_next", {})

    def reserve_source_request(self, batch_id: str, *, kind: str) -> Any:
        """Route follow batches to the legacy command and query batches to query budgets."""
        batch = self.get_batch(batch_id)
        request = self.get_request(str(batch["job_id"]))
        if request.query.source == "follow":
            return super().reserve_source_request(batch_id, kind=kind)
        query_kind = "detail" if kind == "detail" else "query_page"
        return self.reserve_query_request(batch_id, kind=query_kind)

    def record_source_cooldown(self, batch_id: str, seconds: int, *, reason_code: str) -> Any:
        batch = self.get_batch(batch_id)
        request = self.get_request(str(batch["job_id"]))
        if request.query.source == "follow":
            return super().record_source_cooldown(batch_id, seconds, reason_code=reason_code)
        return self.record_query_cooldown(batch_id, seconds, reason_code=reason_code)

    def carry_unresolved_items(self, batch_id: str, *, after: str = "", limit: int = 100) -> Any:
        batch = self.get_batch(batch_id)
        request = self.get_request(str(batch["job_id"]))
        if request.query.source == "follow":
            return super().carry_unresolved_items(batch_id, after=after, limit=limit)
        return self.carry_query_unresolved_items(batch_id, after=after, limit=limit)

    def admit_source_artist(self, batch_id: str, artist_id: str) -> Any:
        batch = self.get_batch(batch_id)
        request = self.get_request(str(batch["job_id"]))
        if request.query.source == "follow":
            return super().admit_source_artist(batch_id, artist_id)
        return self.admit_query_artist(batch_id, artist_id)

    def accept_source_page(
        self, batch_id: str, page: SourcePage
    ) -> tuple[str, ...]:
        batch_id = _validate_identifier(batch_id, "batch id")
        if not isinstance(page, SourcePage):
            raise LibraryError("accept_source_page requires SourcePage")
        page_json = canonical_json(asdict(page))
        digest = _sha256_text(page_json)
        payload = {
            "batch_id": batch_id,
            "page": page,
            "page_json": page_json,
            "page_digest": digest,
        }
        return self._enqueue(
            "accept_source_page",
            payload,
            size_payload={
                "kind": "accept_source_page",
                "batch_id": batch_id,
                "page": asdict(page),
            },
        )

    def get_request(self, job_id: str) -> CollectionRequest:
        job_id = _validate_identifier(job_id, "job id")
        self._ensure_open()
        with contextlib_closing(self._readonly_connection()) as conn:
            row = conn.execute(
                "SELECT request_json FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Unknown job: {job_id}")
        try:
            return CollectionRequest.from_json(row["request_json"])
        except (ContractError, TypeError, ValueError, KeyError) as exc:
            raise CorruptDataError(f"Corrupt request JSON for job {job_id}") from exc

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        batch_id = _validate_identifier(batch_id, "batch id")
        self._ensure_open()
        with contextlib_closing(self._readonly_connection()) as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Unknown batch: {batch_id}")
        return self._batch_dict(row)

    def get_delivery(self, delivery_id: str) -> dict[str, Any]:
        delivery_id = _validate_identifier(delivery_id, "delivery id")
        self._ensure_open()
        with contextlib_closing(self._readonly_connection()) as conn:
            row = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Unknown delivery: {delivery_id}")
        return self._delivery_dict(row)

    def get_checkpoint(
        self, job_id: str, source_id: str, pass_id: str, entity_id: str
    ) -> dict[str, Any] | None:
        job_id = _validate_identifier(job_id, "job id")
        source_id = _validate_identifier(source_id, "source id")
        pass_id = _validate_identifier(pass_id, "pass id")
        entity_id = _validate_identifier(entity_id, "entity id")
        self._ensure_open()
        with contextlib_closing(self._readonly_connection()) as conn:
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE job_id = ? AND source_id = ? "
                "AND pass_id = ? AND entity_id = ?",
                (job_id, source_id, pass_id, entity_id),
            ).fetchone()
        if row is None:
            return None
        _validate_json_text(row["cursor_json"], "checkpoint cursor")
        _validate_json_text(row["coverage_json"], "checkpoint coverage", object_only=True)
        return {
            "job_id": row["job_id"],
            "source_id": row["source_id"],
            "pass_id": row["pass_id"],
            "entity_id": row["entity_id"],
            "artist_id": row["artist_id"],
            "ordinal": row["ordinal"],
            "cursor_json": row["cursor_json"],
            "exhausted": bool(row["exhausted"]),
            "coverage_json": row["coverage_json"],
            "actual_sort": row["actual_sort"],
            "updated_at": row["updated_at"],
        }

    def list_dispatchable(
        self,
        job_id: str,
        *,
        limit: int = 200,
        exclude_ids: Iterable[str] = (),
    ) -> tuple[str, ...]:
        job_id = _validate_identifier(job_id, "job id")
        self._ensure_open()
        if type(limit) is bool or not isinstance(limit, int):
            raise LibraryError("limit must be an integer")
        limit = max(0, min(limit, _MAX_DISPATCH_PAGE))
        if limit == 0:
            return ()

        excluded: list[str] = []
        seen_excluded: set[str] = set()
        raw_excluded = 0
        for item in exclude_ids:
            raw_excluded += 1
            if raw_excluded > _MAX_EXCLUDED_IDS:
                raise LibraryError("Too many excluded delivery ids")
            delivery_id = _validate_identifier(item, "excluded delivery id")
            if len(delivery_id) > _MAX_EXCLUDE_ID_LEN:
                raise LibraryError("Excluded delivery id is too long")
            if delivery_id not in seen_excluded:
                seen_excluded.add(delivery_id)
                excluded.append(delivery_id)

        where = [
            "d.job_id = ?",
            "d.state IN ('pending','suspended','retry_wait')",
            "("
            "d.state != 'retry_wait' OR d.retry_at IS NULL "
            "OR julianday(d.retry_at) IS NULL "
            "OR julianday(d.retry_at) <= julianday('now')"
            ")",
        ]
        params: list[Any] = [job_id]
        if excluded:
            where.append(
                "d.delivery_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
            )
            params.extend(excluded)

        with contextlib_closing(self._readonly_connection()) as conn:
            running = conn.execute(
                "SELECT * FROM batches WHERE job_id = ? "
                "AND phase IN ('running','settling') "
                "ORDER BY rowid DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if running is not None:
                budget = _parse_json_object(running["budget_json"], "batch budget")
                max_works = budget.get("max_works")
                if type(max_works) is not int or max_works < 1:
                    raise CorruptDataError("Corrupt batch work budget")
                used_works = int(
                    conn.execute(
                        "SELECT COUNT(DISTINCT illust_id) AS count FROM batch_items "
                        "WHERE batch_id = ? AND counts_work = 1",
                        (running["batch_id"],),
                    ).fetchone()["count"]
                )
                if used_works >= max_works:
                    where.append(
                        "EXISTS (SELECT 1 FROM batch_items AS bi "
                        "WHERE bi.batch_id = ? AND bi.illust_id = d.illust_id "
                        "AND bi.decision = 'download')"
                    )
                    params.append(running["batch_id"])
            params.append(limit)
            rows = conn.execute(
                "SELECT d.delivery_id FROM deliveries AS d WHERE "
                + " AND ".join(where)
                + " ORDER BY d.rowid LIMIT ?",
                params,
            ).fetchall()
        return tuple(str(row["delivery_id"]) for row in rows)

    def budget_status(self, batch_id: str) -> dict[str, int]:
        batch_id = _validate_identifier(batch_id, "batch id")
        self._ensure_open()
        with contextlib_closing(self._readonly_connection()) as conn:
            batch = conn.execute(
                "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise NotFoundError(f"Unknown batch: {batch_id}")
            budget = _parse_json_object(batch["budget_json"], "batch budget")
            max_works = budget.get("max_works")
            if type(max_works) is not int or max_works < 1:
                raise CorruptDataError("Corrupt batch work budget")
            used_works = int(
                conn.execute(
                    "SELECT COUNT(DISTINCT illust_id) AS count FROM batch_items "
                    "WHERE batch_id = ? AND counts_work = 1",
                    (batch_id,),
                ).fetchone()["count"]
            )
            deferred_works = int(
                conn.execute(
                    "SELECT COUNT(DISTINCT d.illust_id) AS count "
                    "FROM deliveries AS d "
                    "WHERE d.job_id = ? "
                    "AND d.state IN ('pending','suspended','retry_wait') "
                    "AND ("
                    "d.state != 'retry_wait' OR d.retry_at IS NULL "
                    "OR julianday(d.retry_at) IS NULL "
                    "OR julianday(d.retry_at) <= julianday('now')"
                    ") "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM batch_items AS bi "
                    "WHERE bi.batch_id = ? AND bi.illust_id = d.illust_id "
                    "AND bi.decision = 'download'"
                    ")",
                    (str(batch["job_id"]), batch_id),
                ).fetchone()["count"]
            )
        return {
            "used_works": used_works,
            "max_works": int(max_works),
            "deferred_works": deferred_works,
        }

    def claim_delivery(self, delivery_id: str, batch_id: str) -> dict[str, Any] | None:
        delivery_id = _validate_identifier(delivery_id, "delivery id")
        batch_id = _validate_identifier(batch_id, "batch id")
        return self._enqueue(
            "claim_delivery", {"delivery_id": delivery_id, "batch_id": batch_id}
        )

    def release_delivery(
        self,
        delivery_id: str,
        batch_id: str,
        *,
        state: str = "suspended",
        error_code: str = "",
    ) -> None:
        delivery_id = _validate_identifier(delivery_id, "delivery id")
        batch_id = _validate_identifier(batch_id, "batch id")
        if state not in {"suspended", "failed", "retry_wait", "cancelled"}:
            raise LibraryError("release_delivery supports only suspended/failed/retry_wait/cancelled")
        error_code = _validate_error_code(error_code)
        self._enqueue(
            "release_delivery",
            {
                "delivery_id": delivery_id,
                "batch_id": batch_id,
                "state": state,
                "error_code": error_code,
            },
        )

    def request_stop(
        self,
        job_id: str,
        command_id: str,
        *,
        cancel: bool = False,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        job_id = _validate_identifier(job_id, "job id")
        command_id = _validate_command_id(command_id)
        if type(cancel) is not bool:
            raise LibraryError("cancel must be boolean")
        reason = _validate_stop_reason(reason_code, cancel)
        payload = {
            "job_id": job_id,
            "command_id": command_id,
            "cancel": cancel,
            "reason": reason,
            "reason_code": reason_code,
        }
        return self._enqueue("request_stop", payload)

    def settle_stop(self, batch_id: str) -> bool:
        batch_id = _validate_identifier(batch_id, "batch id")
        return bool(self._enqueue("settle_stop", {"batch_id": batch_id}))

    def recover_interrupted(self) -> tuple[str, ...]:
        return self._enqueue("recover_interrupted", {})

    def snapshot(self, job_id: str) -> dict[str, Any]:
        job_id = _validate_identifier(job_id, "job id")
        self._ensure_open()
        with contextlib_closing(self._readonly_connection()) as conn:
            job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if job is None:
                raise NotFoundError(f"Unknown job: {job_id}")
            batch = conn.execute(
                "SELECT * FROM batches WHERE job_id = ? AND phase IN ("
                "'running','cooling','pausing','cancelling','settling'"
                ") ORDER BY rowid DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if batch is None:
                batch = conn.execute(
                    "SELECT * FROM batches WHERE job_id = ? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
            revision = int(
                conn.execute(
                    "SELECT COALESCE(MAX(event_seq), 0) AS revision "
                    "FROM events WHERE job_id = ?",
                    (job_id,),
                ).fetchone()["revision"]
            )
            if batch is None:
                works = {"total": 0, **{name: 0 for name in _DECISIONS}}
                pages = {"total": 0, **{name: 0 for name in _DELIVERY_STATES}}
                batch_id = None
                phase = job["state"]
            else:
                batch_id = str(batch["batch_id"])
                phase = str(batch["phase"])
                works = {"total": 0, **{name: 0 for name in _DECISIONS}}
                for row in conn.execute(
                    "SELECT decision, COUNT(*) AS count FROM batch_items "
                    "WHERE batch_id = ? GROUP BY decision",
                    (batch_id,),
                ).fetchall():
                    count = int(row["count"])
                    works["total"] += count
                    works[str(row["decision"])] = count
                pages = {"total": 0, **{name: 0 for name in _DELIVERY_STATES}}
                for row in conn.execute(
                    "SELECT state, COUNT(*) AS count FROM deliveries "
                    "WHERE origin_batch_id = ? OR current_batch_id = ? "
                    "GROUP BY state",
                    (batch_id, batch_id),
                ).fetchall():
                    count = int(row["count"])
                    pages["total"] += count
                    pages[str(row["state"])] = count
            results = None if batch is None else {
                "batch_id": batch_id,
                "job_id": job_id,
                "phase": phase,
                "coverage_complete": (
                    None if batch["coverage_complete"] is None
                    else bool(batch["coverage_complete"])
                ),
                "stop_target": batch["stop_target"],
                **self._results_counts(conn, batch_id),
            }
        return {
            "job_id": job_id,
            "batch_id": batch_id,
            "phase": phase,
            "stop_reason": None if batch is None else batch["stop_reason"],
            "job_state": str(job["state"]),
            "revision": revision,
            "counts": {"works": works, "pages": pages},
            "results": results,
        }

    def close(self, timeout: float | None = None) -> None:
        with self._close_lock:
            if self._closed:
                return
            if threading.current_thread() is self._writer_thread:
                raise LibraryError("close() cannot run on the writer thread")
            thread = self._writer_thread
            started = time.monotonic()
            with self._admission:
                self._accepting.clear()
                self._admission.notify_all()
            if thread is not None and thread.is_alive():
                remaining = None
                if timeout is not None:
                    remaining = max(0.0, timeout - (time.monotonic() - started))
                thread.join(remaining)
                if thread.is_alive():
                    raise LibraryError("writer thread did not stop; lock remains held")
            if thread is not None:
                thread.join(0)
            self._release_lock()
            self._closed = True

    # ------------------------------------------------------------------
    # Write commands
    # ------------------------------------------------------------------

    def _submit(self, conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, str]:
        command_id = str(payload["command_id"])
        request = payload["request"]
        request_json = str(payload["request_json"])
        digest = str(payload["request_digest"])
        with self._transaction(conn):
            info = self._command_event(conn, command_id)
            if info is not None:
                if (info.get("command_type") != "submit" or info.get("request_digest") != digest
                        or info.get("parent_job_id") != payload.get("parent_job_id")):
                    raise CommandConflict("command id reused for a different submit")
                return self._command_result(info)
            existing_batch = conn.execute(
                "SELECT batch_id FROM batches WHERE command_id = ?", (command_id,)
            ).fetchone()
            if existing_batch is not None:
                raise CommandConflict("command id already owns a batch")

            parent_id = payload.get("parent_job_id")
            if parent_id is not None:
                parent = self._get_job_row(conn, parent_id)
                if parent is None:
                    raise NotFoundError("Unknown parent job")
                if parent["account_ref"] != request.account_ref:
                    raise InvalidStateError("Parent job belongs to another account")
            job_id = _new_id()
            batch_id = _new_id()
            now = _utcnow()
            budget_json = canonical_json(asdict(request.budget))
            conn.execute(
                "INSERT INTO jobs("
                "job_id, parent_job_id, request_id, request_version, request_json, "
                "scope_fingerprint, account_ref, state, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                (
                    job_id,
                    parent_id,
                    request.request_id,
                    int(request.schema_version),
                    request_json,
                    payload["scope_fingerprint"],
                    request.account_ref,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO batches("
                "batch_id, job_id, command_id, budget_json, phase, stop_reason, "
                "active_slot, created_at, started_at, ended_at"
                ") VALUES (?, ?, ?, ?, 'queued', NULL, NULL, ?, NULL, NULL)",
                (batch_id, job_id, command_id, budget_json, now),
            )
            result = {"job_id": job_id, "batch_id": batch_id}
            self._insert_event(
                conn,
                job_id=job_id,
                batch_id=batch_id,
                kind="command_submit",
                payload={
                    "command_type": "submit",
                    "request_digest": digest,
                    "parent_job_id": parent_id,
                    "job_id": job_id,
                    "batch_id": batch_id,
                    "result": result,
                },
                accepted_command_id=command_id,
            )
            return result

    def _resume(self, conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, str]:
        command_id = str(payload["command_id"])
        job_id = str(payload["job_id"])
        supplied_budget = payload.get("budget")
        retry_failed = bool(payload.get("retry_failed", False))
        with self._transaction(conn):
            job = self._get_job_row(conn, job_id)
            if job is None:
                raise NotFoundError(f"Unknown job: {job_id}")
            request = self._request_from_row(job)

            effective_budget = supplied_budget if supplied_budget is not None else request.budget
            budget_json = canonical_json(asdict(effective_budget))
            now = _utcnow()

            info = self._command_event(conn, command_id)
            if info is not None:
                if (
                    info.get("command_type") != "resume"
                    or info.get("job_id") != job_id
                    or info.get("budget_json") != budget_json
                    or bool(info.get("retry_failed", False)) != retry_failed
                ):
                    raise CommandConflict("command id reused for a different resume")
                return self._command_result(info)

            existing = conn.execute(
                "SELECT * FROM batches WHERE job_id = ? AND phase IN "
                "('queued','running') ORDER BY rowid LIMIT 1",
                (job_id,),
            ).fetchone()
            if existing is not None:
                if retry_failed:
                    self._reset_failed_deliveries_for_resume(conn, job_id, now)
                result = {"job_id": job_id, "batch_id": str(existing["batch_id"])}
                self._insert_event(
                    conn,
                    job_id=job_id,
                    batch_id=result["batch_id"],
                    kind="command_resume",
                    payload={
                        "command_type": "resume",
                        "job_id": job_id,
                        "budget_json": budget_json,
                        "retry_failed": retry_failed,
                        "result": result,
                    },
                    accepted_command_id=command_id,
                )
                return result

            active_other = conn.execute(
                "SELECT 1 FROM batches WHERE job_id = ? AND phase IN ("
                "'cooling','pausing','cancelling','settling'"
                ") LIMIT 1",
                (job_id,),
            ).fetchone()
            if active_other is not None:
                raise InvalidStateError("job has an active batch that has not settled")
            if job["state"] not in {"paused", "blocked", "partial"}:
                raise InvalidStateError("resume requires paused, blocked or partial job state")

            batch_id = _new_id()
            conn.execute(
                "INSERT INTO batches("
                "batch_id, job_id, command_id, budget_json, phase, stop_reason, "
                "active_slot, created_at, started_at, ended_at"
                ") VALUES (?, ?, ?, ?, 'queued', NULL, NULL, ?, NULL, NULL)",
                (batch_id, job_id, command_id, budget_json, now),
            )
            conn.execute(
                "UPDATE jobs SET state = 'queued', updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )
            if retry_failed:
                self._reset_failed_deliveries_for_resume(conn, job_id, now)
            result = {"job_id": job_id, "batch_id": batch_id}
            self._insert_event(
                conn,
                job_id=job_id,
                batch_id=batch_id,
                kind="command_resume",
                payload={
                    "command_type": "resume",
                    "job_id": job_id,
                    "budget_json": budget_json,
                    "retry_failed": retry_failed,
                    "result": result,
                },
                accepted_command_id=command_id,
            )
            return result

    def _activate_next(self, conn: sqlite3.Connection) -> dict[str, Any] | None:
        with self._transaction(conn):
            active = conn.execute(
                "SELECT 1 FROM batches WHERE phase IN ("
                "'running','cooling','pausing','cancelling','settling'"
                ") LIMIT 1"
            ).fetchone()
            if active is not None:
                return None
            queued = conn.execute(
                "SELECT * FROM batches WHERE phase = 'queued' "
                "ORDER BY rowid LIMIT 1"
            ).fetchone()
            if queued is None:
                return None
            now = _utcnow()
            batch_id = str(queued["batch_id"])
            job_id = str(queued["job_id"])
            conn.execute(
                "UPDATE batches SET phase = 'running', active_slot = 1, "
                "started_at = ? WHERE batch_id = ?",
                (now, batch_id),
            )
            conn.execute(
                "UPDATE jobs SET state = 'running', updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )
            self._insert_event(
                conn,
                job_id=job_id,
                batch_id=batch_id,
                kind="batch_activated",
                payload={"batch_id": batch_id, "started_at": now},
            )
            row = self._get_batch_row(conn, batch_id)
            return self._batch_dict(row)

    def _accept_source_page(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> tuple[str, ...]:
        batch_id = str(payload["batch_id"])
        page: SourcePage = payload["page"]
        page_digest = str(payload["page_digest"])

        with self._transaction(conn):
            batch = self._get_batch_row(conn, batch_id)
            if batch is None:
                raise NotFoundError(f"Unknown batch: {batch_id}")
            if batch["phase"] != "running":
                raise InvalidStateError("source page acceptance requires a running batch")
            job = self._get_job_row(conn, str(batch["job_id"]))
            if job is None:
                raise CorruptDataError("Batch references a missing job")
            request = self._request_from_row(job)
            job_id = str(job["job_id"])

            local_receipt = conn.execute(
                "SELECT * FROM source_batch_pages WHERE batch_id=? AND source_id=? "
                "AND pass_id=? AND entity_id=? AND page_identity=?",
                (batch_id,page.source_id,page.pass_id,page.entity_id,page.page_identity),
            ).fetchone()
            receipt = conn.execute(
                "SELECT * FROM source_pages WHERE job_id = ? AND source_id = ? "
                "AND pass_id = ? AND entity_id = ? AND page_identity = ?",
                (
                    job_id,
                    page.source_id,
                    page.pass_id,
                    page.entity_id,
                    page.page_identity,
                ),
            ).fetchone()
            if local_receipt is not None or receipt is not None:
                prior_receipt = local_receipt if local_receipt is not None else receipt
                if prior_receipt["content_digest"] != page_digest:
                    raise CursorConflict("page identity was already accepted with different content")
                try:
                    stored_ids = json.loads(prior_receipt["delivery_ids_json"])
                except (TypeError, ValueError) as exc:
                    raise CorruptDataError("Corrupt source page receipt") from exc
                if not isinstance(stored_ids, list) or not all(
                    isinstance(item, str) for item in stored_ids
                ):
                    raise CorruptDataError("Corrupt source page delivery ids")
                if local_receipt is not None:
                    return tuple(stored_ids)

            checkpoint = conn.execute(
                "SELECT * FROM checkpoints WHERE job_id = ? AND source_id = ? "
                "AND pass_id = ? AND entity_id = ?",
                (job_id, page.source_id, page.pass_id, page.entity_id),
            ).fetchone()
            current_cursor = checkpoint["cursor_json"] if checkpoint is not None else "null"
            cursor_matches = self._same_json(page.cursor_before_json, current_cursor)
            if not cursor_matches and receipt is None:
                raise CursorConflict("cursor_before does not match the persisted checkpoint")

            budget = _parse_json_object(batch["budget_json"], "batch budget")
            max_works = budget.get("max_works")
            if type(max_works) is not int or max_works < 1:
                raise CorruptDataError("Corrupt batch work budget")

            existing_download_works = {
                str(row["illust_id"])
                for row in conn.execute(
                    "SELECT illust_id FROM batch_items "
                    "WHERE batch_id = ? AND counts_work = 1",
                    (batch_id,),
                ).fetchall()
            }
            planned: set[str] = set()
            selected_pages: dict[str, list[PageSpec]] = {}
            output_keys: set[str] = set()
            for candidate in page.candidates:
                pages = list(candidate.pages)
                if candidate.decision == "download":
                    self._validate_page_policy(candidate.page_count, pages, request.quality.all_pages)
                    if self._candidate_requires_write(conn, candidate, request):
                        planned.add(candidate.illust_id)
                    for page_spec in pages:
                        self._validate_output_ref(request.output.root_ref, page_spec.output_ref)
                        key = page_spec.output_ref.target_key
                        if key in output_keys:
                            raise OutputConflict("two deliveries in this page share one output path")
                        output_keys.add(key)
                    selected_pages[candidate.illust_id] = pages
            new_download_works = (existing_download_works | planned) - existing_download_works
            if len(existing_download_works) + len(new_download_works) > max_works:
                raise BudgetExceeded("batch max_works would be exceeded")

            now = _utcnow()
            delivery_ids: list[str] = []
            quality_json = canonical_json(asdict(request.quality))
            quality_key = _sha256_text(quality_json)

            for candidate in page.candidates:
                is_download = candidate.decision == "download"
                item = conn.execute(
                    "SELECT * FROM batch_items WHERE batch_id = ? AND illust_id = ?",
                    (batch_id, candidate.illust_id),
                ).fetchone()
                pages = selected_pages.get(candidate.illust_id, [])
                if is_download:
                    required_indices = {page_spec.page_index for page_spec in pages}
                    if item is not None and item["decision"] == "download":
                        try:
                            existing_required = json.loads(item["required_pages_json"])
                        except (TypeError, ValueError) as exc:
                            raise CorruptDataError("Corrupt required page list") from exc
                        if isinstance(existing_required, list):
                            required_indices.update(
                                int(index) for index in existing_required if isinstance(index, int)
                            )
                    required_pages_json = canonical_json(sorted(required_indices))
                    reason_codes_json = canonical_json(list(candidate.reason_codes))
                    if item is None:
                        conn.execute(
                            "INSERT INTO batch_items("
                            "batch_id, illust_id, artist_id, artist_name, title, "
                            "page_count, decision, reason_codes_json, required_pages_json, "
                            "result_state, bookmarks, ai_flag, provenance_json, "
                            "created_at, updated_at"
                            ") VALUES (?, ?, ?, ?, ?, ?, 'download', ?, ?, 'pending', ?, ?, ?, ?, ?)",
                            (
                                batch_id,
                                candidate.illust_id,
                                candidate.artist_id or None,
                                candidate.artist_name,
                                candidate.title,
                                candidate.page_count,
                                reason_codes_json,
                                required_pages_json,
                                candidate.bookmarks,
                                None if candidate.ai_flag is None else int(candidate.ai_flag),
                                candidate.provenance_json,
                                now,
                                now,
                            ),
                        )
                    elif item["decision"] != "download":
                        conn.execute(
                            "UPDATE batch_items SET artist_id = ?, artist_name = ?, title = ?, "
                            "page_count = ?, decision = 'download', reason_codes_json = ?, "
                            "required_pages_json = ?, result_state = 'pending', bookmarks = ?, "
                            "ai_flag = ?, provenance_json = ?, updated_at = ? "
                            "WHERE batch_id = ? AND illust_id = ?",
                            (
                                candidate.artist_id or None,
                                candidate.artist_name,
                                candidate.title,
                                candidate.page_count,
                                reason_codes_json,
                                required_pages_json,
                                candidate.bookmarks,
                                None if candidate.ai_flag is None else int(candidate.ai_flag),
                                candidate.provenance_json,
                                now,
                                batch_id,
                                candidate.illust_id,
                            ),
                        )
                    else:
                        conn.execute(
                            "UPDATE batch_items SET artist_id = ?, artist_name = ?, title = ?, "
                            "page_count = ?, reason_codes_json = ?, required_pages_json = ?, "
                            "bookmarks = ?, ai_flag = ?, provenance_json = ?, updated_at = ? "
                            "WHERE batch_id = ? AND illust_id = ?",
                            (
                                candidate.artist_id or None,
                                candidate.artist_name,
                                candidate.title,
                                candidate.page_count,
                                reason_codes_json,
                                required_pages_json,
                                candidate.bookmarks,
                                None if candidate.ai_flag is None else int(candidate.ai_flag),
                                candidate.provenance_json,
                                now,
                                batch_id,
                                candidate.illust_id,
                            ),
                        )
                elif item is None:
                    conn.execute(
                        "INSERT INTO batch_items("
                        "batch_id, illust_id, artist_id, artist_name, title, "
                        "page_count, decision, reason_codes_json, required_pages_json, "
                        "result_state, bookmarks, ai_flag, provenance_json, "
                        "created_at, updated_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?)",
                        (
                            batch_id,
                            candidate.illust_id,
                            candidate.artist_id or None,
                            candidate.artist_name,
                            candidate.title,
                            candidate.page_count,
                            candidate.decision,
                            canonical_json(list(candidate.reason_codes)),
                            candidate.decision,
                            candidate.bookmarks,
                            None if candidate.ai_flag is None else int(candidate.ai_flag),
                            candidate.provenance_json,
                            now,
                            now,
                        ),
                    )
                elif item["decision"] != "download":
                    conn.execute(
                        "UPDATE batch_items SET artist_id = ?, artist_name = ?, title = ?, "
                        "page_count = ?, decision = ?, reason_codes_json = ?, "
                        "required_pages_json = '[]', result_state = ?, bookmarks = ?, "
                        "ai_flag = ?, provenance_json = ?, updated_at = ? "
                        "WHERE batch_id = ? AND illust_id = ?",
                        (
                            candidate.artist_id or None,
                            candidate.artist_name,
                            candidate.title,
                            candidate.page_count,
                            candidate.decision,
                            canonical_json(list(candidate.reason_codes)),
                            candidate.decision,
                            candidate.bookmarks,
                            None if candidate.ai_flag is None else int(candidate.ai_flag),
                            candidate.provenance_json,
                            now,
                            batch_id,
                            candidate.illust_id,
                        ),
                    )

                occurrence_key = canonical_json(
                    [page.entity_id, page.page_identity, candidate.illust_id]
                )
                existing_occurrence = conn.execute(
                    "SELECT payload FROM source_items WHERE job_id = ? AND source_id = ? "
                    "AND pass_id = ? AND occurrence_key = ?",
                    (job_id, page.source_id, page.pass_id, occurrence_key),
                ).fetchone()
                candidate_payload = canonical_json(asdict(candidate))
                if existing_occurrence is None:
                    conn.execute(
                        "INSERT INTO source_items("
                        "job_id, source_id, pass_id, occurrence_key, entity_type, "
                        "entity_id, payload, created_at"
                        ") VALUES (?, ?, ?, ?, 'illust', ?, ?, ?)",
                        (
                            job_id,
                            page.source_id,
                            page.pass_id,
                            occurrence_key,
                            candidate.illust_id,
                            candidate_payload,
                            now,
                        ),
                    )
                elif existing_occurrence["payload"] != candidate_payload:
                    raise CursorConflict("source occurrence payload changed")

                if not is_download:
                    continue

                for page_spec in pages:
                    output_key = page_spec.output_ref.target_key
                    existing = conn.execute(
                        "SELECT * FROM deliveries WHERE job_id = ? AND illust_id = ? "
                        "AND page_index = ? AND quality_key = ? AND output_key = ? "
                        "ORDER BY generation DESC LIMIT 1",
                        (
                            job_id,
                            candidate.illust_id,
                            page_spec.page_index,
                            quality_key,
                            output_key,
                        ),
                    ).fetchone()
                    if existing is not None and (request.dedup_policy != "redownload" or receipt is not None):
                        if existing["state"] == "completed":
                            artifact = conn.execute(
                                "SELECT * FROM artifacts WHERE delivery_id = ?",
                                (existing["delivery_id"],),
                            ).fetchone()
                            if artifact is None:
                                raise CorruptDataError(
                                    "Completed delivery has no artifact"
                                )
                            if not _artifact_is_valid(artifact):
                                raise CorruptDataError(
                                    "Completed delivery has invalid artifact evidence"
                                )
                            link_delivery_id = str(existing["delivery_id"])
                            link_artifact_id = str(artifact["artifact_id"])
                        else:
                            link_delivery_id = str(existing["delivery_id"])
                            link_artifact_id = None
                        self._link_batch_item_page(
                            conn,
                            batch_id,
                            candidate.illust_id,
                            page_spec.page_index,
                            output_key,
                            quality_json,
                            delivery_id=link_delivery_id,
                            artifact_id=link_artifact_id,
                        )
                        delivery_ids.append(str(existing["delivery_id"]))
                        continue
                    if request.dedup_policy == "trusted":
                        reusable = self._find_reusable_artifact(
                            conn,
                            job_id=job_id,
                            illust_id=candidate.illust_id,
                            page_index=page_spec.page_index,
                            output_key=output_key,
                            original=request.quality.original,
                            validation_policy_version=request.quality.validation_policy_version,
                        )
                        if reusable is not None:
                            # all_pages is a work-selection option, not a page
                            # byte-quality property and is intentionally ignored.
                            self._link_batch_item_page(
                                conn,
                                batch_id,
                                candidate.illust_id,
                                page_spec.page_index,
                                output_key,
                                quality_json,
                                delivery_id=None,
                                artifact_id=str(reusable["artifact_id"]),
                            )
                            continue
                    max_generation_row = conn.execute(
                        "SELECT COALESCE(MAX(generation), 0) AS generation FROM deliveries "
                        "WHERE job_id = ? AND illust_id = ? AND page_index = ? "
                        "AND quality_key = ?",
                        (
                            job_id,
                            candidate.illust_id,
                            page_spec.page_index,
                            quality_key,
                        ),
                    ).fetchone()
                    generation = int(max_generation_row["generation"]) + 1
                    delivery_id = _new_id()
                    delivery_payload = {
                        "illust_id": candidate.illust_id,
                        "artist_id": candidate.artist_id,
                        "artist_name": candidate.artist_name,
                        "title": candidate.title,
                        "page": asdict(page_spec),
                        "quality": asdict(request.quality),
                    }
                    try:
                        conn.execute(
                            "INSERT INTO deliveries("
                            "delivery_id, job_id, origin_batch_id, current_batch_id, "
                            "completed_batch_id, illust_id, page_index, output_key, "
                            "output_ref_json, quality_json, quality_key, payload_json, "
                            "source_identity, state, attempts, retry_at, error_code, "
                            "generation, checkpoint_json, prepared_json, created_at, updated_at"
                            ") VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, "
                            "'pending', 0, NULL, NULL, ?, ?, NULL, ?, ?)",
                            (
                                delivery_id,
                                job_id,
                                batch_id,
                                batch_id,
                                candidate.illust_id,
                                page_spec.page_index,
                                output_key,
                                canonical_json(asdict(page_spec.output_ref)),
                                quality_json,
                                quality_key,
                                canonical_json(delivery_payload),
                                f"{page.source_id}/{page.pass_id}/{page.entity_id}",
                                generation,
                                page.cursor_after_json,
                                now,
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        message = str(exc)
                        if "deliveries.output_key" in message or "ux_deliveries_active_output" in message:
                            raise OutputConflict("output path already has an active writer") from exc
                        raise
                    self._link_batch_item_page(
                        conn,
                        batch_id,
                        candidate.illust_id,
                        page_spec.page_index,
                        output_key,
                        quality_json,
                        delivery_id=delivery_id,
                        artifact_id=None,
                    )
                    delivery_ids.append(delivery_id)

            for candidate in page.candidates:
                if candidate.illust_id in planned:
                    conn.execute("UPDATE batch_items SET counts_work=1 WHERE batch_id=? AND illust_id=?", (batch_id, candidate.illust_id))
                if candidate.decision == "download":
                    self._refresh_batch_item_result(
                        conn, batch_id, candidate.illust_id, now
                    )

            if checkpoint is None:
                conn.execute(
                    "INSERT INTO checkpoints("
                    "job_id, source_id, pass_id, entity_id, artist_id, ordinal, "
                    "cursor_json, exhausted, coverage_json, actual_sort, updated_at"
                    ") VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        page.source_id,
                        page.pass_id,
                        page.entity_id,
                        page.cursor_after_json,
                        int(page.exhausted),
                        page.coverage_json,
                        page.actual_sort,
                        now,
                    ),
                )
            elif cursor_matches:
                conn.execute(
                    "UPDATE checkpoints SET cursor_json = ?, exhausted = ?, "
                    "coverage_json = ?, actual_sort = ?, updated_at = ? "
                    "WHERE job_id = ? AND source_id = ? AND pass_id = ? AND entity_id = ?",
                    (
                        page.cursor_after_json,
                        int(page.exhausted),
                        page.coverage_json,
                        page.actual_sort,
                        now,
                        job_id,
                        page.source_id,
                        page.pass_id,
                        page.entity_id,
                    ),
                )

            if receipt is None:
                conn.execute(
                    "INSERT INTO source_pages("
                    "job_id, page_identity, source_id, pass_id, entity_id, batch_id, "
                    "content_digest, delivery_ids_json, accepted_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        page.page_identity,
                        page.source_id,
                        page.pass_id,
                        page.entity_id,
                        batch_id,
                        page_digest,
                        canonical_json(delivery_ids),
                        now,
                    ),
                )

            conn.execute("INSERT INTO source_batch_pages VALUES (?,?,?,?,?,?,?)",
                (batch_id,page.source_id,page.pass_id,page.entity_id,page.page_identity,page_digest,canonical_json(delivery_ids)))

            self._insert_event(
                conn,
                job_id=job_id,
                batch_id=batch_id,
                kind="source_page_accepted",
                payload={
                    "page_identity": page.page_identity,
                    "delivery_ids": delivery_ids,
                    "candidate_count": len(page.candidates),
                },
            )
            return tuple(delivery_ids)

    def _admit_claimed_work(
        self,
        conn: sqlite3.Connection,
        batch: sqlite3.Row,
        delivery: sqlite3.Row,
        now: str,
    ) -> None:
        """Persist a recovered work in this batch before its first claim.

        The budget check happens before delivery state/attempts/event changes.
        The old batch and its batch item are read-only inputs.
        """
        batch_id = str(batch["batch_id"])
        job_id = str(batch["job_id"])
        illust_id = str(delivery["illust_id"])
        item = conn.execute(
            "SELECT * FROM batch_items WHERE batch_id = ? AND illust_id = ?",
            (batch_id, illust_id),
        ).fetchone()
        if item is not None and item["decision"] == "download" and item["counts_work"]:
            source = conn.execute(
                "SELECT * FROM batch_items WHERE batch_id = ? AND illust_id = ?",
                (delivery["origin_batch_id"], illust_id),
            ).fetchone()
            if source is None:
                source = conn.execute(
                    "SELECT bi.* FROM batch_items AS bi "
                    "JOIN batches AS b ON b.batch_id = bi.batch_id "
                    "WHERE b.job_id = ? AND bi.illust_id = ? "
                    "ORDER BY b.rowid DESC LIMIT 1",
                    (job_id, illust_id),
                ).fetchone()
            if source is not None:
                self._copy_batch_item_page_links(
                    conn, str(source["batch_id"]), batch_id, illust_id
                )
            self._ensure_claimed_page_link(conn, batch_id, delivery)
            self._refresh_batch_item_result(conn, batch_id, illust_id, now)
            return

        budget = _parse_json_object(batch["budget_json"], "batch budget")
        max_works = budget.get("max_works")
        if type(max_works) is not int or max_works < 1:
            raise CorruptDataError("Corrupt batch work budget")
        used_works = int(
            conn.execute(
                "SELECT COUNT(DISTINCT illust_id) AS count FROM batch_items "
                "WHERE batch_id = ? AND counts_work = 1",
                (batch_id,),
            ).fetchone()["count"]
        )
        if used_works + 1 > max_works:
            raise BudgetExceeded("batch max_works would be exceeded by this recovered work")

        source = conn.execute(
            "SELECT * FROM batch_items WHERE batch_id = ? AND illust_id = ?",
            (delivery["origin_batch_id"], illust_id),
        ).fetchone()
        if source is None:
            source = conn.execute(
                "SELECT bi.* FROM batch_items AS bi "
                "JOIN batches AS b ON b.batch_id = bi.batch_id "
                "WHERE b.job_id = ? AND bi.illust_id = ? "
                "ORDER BY b.rowid DESC LIMIT 1",
                (job_id, illust_id),
            ).fetchone()

        page_index = int(delivery["page_index"])
        if source is not None:
            try:
                source_required = json.loads(source["required_pages_json"])
            except (TypeError, ValueError) as exc:
                raise CorruptDataError("Corrupt recovered required page list") from exc
            if not isinstance(source_required, list):
                raise CorruptDataError("Corrupt recovered required page list")
            required_indices = {
                int(value)
                for value in source_required
                if isinstance(value, int) and 0 <= value <= 9999
            }
            required_indices.add(page_index)
            artist_id = source["artist_id"]
            artist_name = source["artist_name"]
            title = source["title"]
            page_count = source["page_count"]
            bookmarks = source["bookmarks"]
            ai_flag = source["ai_flag"]
            provenance_json = source["provenance_json"]
            reason_codes_json = source["reason_codes_json"]
        else:
            payload = _parse_json_object(delivery["payload_json"], "delivery payload")
            page = payload.get("page")
            if not isinstance(page, dict):
                raise CorruptDataError("Corrupt delivery page payload")
            payload_page_index = page.get("page_index")
            if type(payload_page_index) is not int or payload_page_index != page_index:
                raise CorruptDataError("Corrupt delivery page index")
            artist_id = payload.get("artist_id")
            if not isinstance(artist_id, str):
                artist_id = None
            artist_name = payload.get("artist_name")
            if not isinstance(artist_name, str):
                artist_name = ""
            title = payload.get("title")
            if not isinstance(title, str):
                title = ""
            required_indices = {page_index}
            page_count = page_index + 1
            bookmarks = None
            ai_flag = None
            provenance_json = "{}"
            reason_codes_json = "[]"
        required_pages_json = canonical_json(sorted(required_indices))

        if item is None:
            conn.execute(
                "INSERT INTO batch_items("
                "batch_id, illust_id, artist_id, artist_name, title, page_count, "
                "decision, reason_codes_json, required_pages_json, result_state, "
                "bookmarks, ai_flag, provenance_json, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 'download', ?, ?, 'pending', ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    illust_id,
                    artist_id,
                    artist_name,
                    title,
                    page_count,
                    reason_codes_json,
                    required_pages_json,
                    bookmarks,
                    ai_flag,
                    provenance_json,
                    now,
                    now,
                ),
            )
        else:
            conn.execute(
                "UPDATE batch_items SET artist_id = ?, artist_name = ?, title = ?, "
                "page_count = ?, decision = 'download', reason_codes_json = ?, "
                "required_pages_json = ?, result_state = 'pending', bookmarks = ?, "
                "ai_flag = ?, provenance_json = ?, updated_at = ? "
                "WHERE batch_id = ? AND illust_id = ?",
                (
                    artist_id,
                    artist_name,
                    title,
                    page_count,
                    reason_codes_json,
                    required_pages_json,
                    bookmarks,
                    ai_flag,
                    provenance_json,
                    now,
                    batch_id,
                    illust_id,
                ),
            )
        conn.execute("UPDATE batch_items SET counts_work=1 WHERE batch_id=? AND illust_id=?", (batch_id, illust_id))
        if source is not None:
            self._copy_batch_item_page_links(
                conn, str(source["batch_id"]), batch_id, illust_id
            )
        self._ensure_claimed_page_link(conn, batch_id, delivery)
        self._refresh_batch_item_result(conn, batch_id, illust_id, now)

    def _claim_delivery(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        delivery_id = str(payload["delivery_id"])
        batch_id = str(payload["batch_id"])
        with self._transaction(conn):
            batch = self._get_batch_row(conn, batch_id)
            if batch is None or batch["phase"] not in {"running", "settling"}:
                return None
            delivery = self._get_delivery_row(conn, delivery_id)
            if delivery is None or delivery["job_id"] != batch["job_id"]:
                return None
            if delivery["state"] not in _DISPATCHABLE_STATES:
                return None
            if delivery["state"] == "retry_wait" and not _retry_due(delivery["retry_at"]):
                return None
            now = _utcnow()
            self._admit_claimed_work(conn, batch, delivery, now)
            cursor = conn.execute(
                "UPDATE deliveries SET state = 'transferring', attempts = attempts + 1, "
                "current_batch_id = ?, retry_at = NULL, updated_at = ? "
                "WHERE delivery_id = ? AND state IN ('pending','suspended','retry_wait')",
                (batch_id, now, delivery_id),
            )
            if cursor.rowcount != 1:
                return None
            self._insert_event(
                conn,
                job_id=str(batch["job_id"]),
                batch_id=batch_id,
                kind="delivery_claimed",
                payload={
                    "delivery_id": delivery_id,
                    "batch_id": batch_id,
                    "state": "transferring",
                },
            )
            row = self._get_delivery_row(conn, delivery_id)
            return self._delivery_dict(row)

    def _release_delivery(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> None:
        delivery_id = str(payload["delivery_id"])
        batch_id = str(payload["batch_id"])
        state = str(payload["state"])
        error_code = str(payload["error_code"])
        with self._transaction(conn):
            batch = self._get_batch_row(conn, batch_id)
            if batch is None:
                raise NotFoundError(f"Unknown batch: {batch_id}")
            delivery = self._get_delivery_row(conn, delivery_id)
            if delivery is None:
                raise NotFoundError(f"Unknown delivery: {delivery_id}")
            if delivery["job_id"] != batch["job_id"]:
                raise LibraryError("Batch and delivery belong to different jobs")
            if delivery["current_batch_id"] != batch_id:
                # An old batch must never release a delivery now owned by a new batch.
                return None
            if delivery["state"] == state:
                return None
            if delivery["state"] not in {"transferring", "validating"}:
                raise InvalidStateError("Only the owning transferring/validating delivery can be released")
            now = _utcnow()
            retry_at = _retry_due_now() if state == "retry_wait" else None
            conn.execute(
                "UPDATE deliveries SET state = ?, retry_at = ?, error_code = ?, "
                "updated_at = ? WHERE delivery_id = ?",
                (state, retry_at, error_code, now, delivery_id),
            )
            self._insert_event(
                conn,
                job_id=str(batch["job_id"]),
                batch_id=batch_id,
                kind="delivery_released",
                payload={
                    "delivery_id": delivery_id,
                    "batch_id": batch_id,
                    "state": state,
                    "error_code": error_code,
                },
            )
            self._refresh_items_for_delivery(conn, delivery_id, now)
            return None

    def _request_stop(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> dict[str, Any]:
        job_id = str(payload["job_id"])
        command_id = str(payload["command_id"])
        cancel = bool(payload["cancel"])
        explicit_reason = payload.get("reason_code") is not None
        reason = str(
            payload.get("reason")
            or ("user_cancel" if cancel else "user_pause")
        )
        with self._transaction(conn):
            job = self._get_job_row(conn, job_id)
            if job is None:
                raise NotFoundError(f"Unknown job: {job_id}")

            info = self._command_event(conn, command_id)
            if info is not None:
                stored_reason = info.get("reason_code", reason)
                if (
                    info.get("command_type") != "stop"
                    or info.get("job_id") != job_id
                    or bool(info.get("cancel")) != cancel
                    or stored_reason != reason
                ):
                    raise CommandConflict("command id reused for a different stop")
                return self._command_result(info)

            now = _utcnow()
            active = conn.execute(
                "SELECT * FROM batches WHERE job_id = ? AND phase IN ("
                "'running','cooling','pausing','cancelling','settling'"
                ") ORDER BY rowid DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if active is not None:
                current = str(active["phase"])
                if cancel:
                    if current in {"running", "cooling", "pausing", "settling"}:
                        new_phase = "cancelling"
                    else:
                        new_phase = current
                else:
                    if current in {"running", "cooling", "settling"}:
                        new_phase = "pausing"
                    else:
                        new_phase = current
                if new_phase != current:
                    new_target = (
                        "paused" if new_phase == "pausing" else
                        "cancelled" if new_phase == "cancelling" else
                        active["stop_target"]
                    )
                    conn.execute(
                        "UPDATE batches SET phase = ?, active_slot = 1, "
                        "stop_target = ?, stop_reason = ? WHERE batch_id = ?",
                        (
                            new_phase,
                            new_target,
                            reason,
                            active["batch_id"],
                        ),
                    )
                    self._insert_event(
                        conn,
                        job_id=job_id,
                        batch_id=str(active["batch_id"]),
                        kind="batch_stop_requested",
                        payload={
                            "phase": new_phase,
                            "cancel": cancel,
                            "reason_code": reason,
                        },
                    )
                elif explicit_reason:
                    conn.execute(
                        "UPDATE batches SET stop_reason = ? WHERE batch_id = ?",
                        (reason, active["batch_id"]),
                    )
                    self._insert_event(
                        conn,
                        job_id=job_id,
                        batch_id=str(active["batch_id"]),
                        kind="batch_stop_reason_updated",
                        payload={"phase": current, "reason_code": reason},
                    )
                conn.execute(
                    "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                    (new_phase, now, job_id),
                )
                result = {
                    "job_id": job_id,
                    "batch_id": str(active["batch_id"]),
                    "phase": new_phase,
                }
            else:
                queued = conn.execute(
                    "SELECT * FROM batches WHERE job_id = ? AND phase = 'queued' "
                    "ORDER BY rowid",
                    (job_id,),
                ).fetchall()
                if queued:
                    new_phase = "cancelled" if cancel else "paused"
                    for row in queued:
                        conn.execute(
                            "UPDATE batches SET phase = ?, active_slot = NULL, "
                            "stop_target = ?, stop_reason = COALESCE(stop_reason, ?), "
                            "ended_at = ? WHERE batch_id = ?",
                            (
                                new_phase,
                                "cancelled" if cancel else "paused",
                                reason,
                                now,
                                row["batch_id"],
                            ),
                        )
                    conn.execute(
                        "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                        (new_phase, now, job_id),
                    )
                    self._insert_event(
                        conn,
                        job_id=job_id,
                        batch_id=str(queued[0]["batch_id"]),
                        kind="batch_stop_requested",
                        payload={
                            "phase": new_phase,
                            "cancel": cancel,
                            "reason_code": reason,
                        },
                    )
                    result = {
                        "job_id": job_id,
                        "batch_id": str(queued[0]["batch_id"]),
                        "phase": new_phase,
                    }
                else:
                    latest = conn.execute(
                        "SELECT batch_id FROM batches WHERE job_id = ? "
                        "ORDER BY rowid DESC LIMIT 1",
                        (job_id,),
                    ).fetchone()
                    result = {
                        "job_id": job_id,
                        "batch_id": str(latest["batch_id"]) if latest is not None else None,
                        "phase": str(job["state"]),
                    }

            self._insert_event(
                conn,
                job_id=job_id,
                batch_id=result["batch_id"],
                kind="command_stop",
                payload={
                    "command_type": "stop",
                    "job_id": job_id,
                    "cancel": cancel,
                    "reason_code": reason,
                    "result": result,
                },
                accepted_command_id=command_id,
            )
            return result

    def _settle_stop(self, conn: sqlite3.Connection, batch_id: str) -> bool:
        with self._transaction(conn):
            batch = self._get_batch_row(conn, batch_id)
            if batch is None:
                return False
            phase = str(batch["phase"])
            if phase not in {"pausing", "cancelling"}:
                return False
            busy = conn.execute(
                "SELECT 1 FROM deliveries WHERE job_id = ? "
                "AND state IN ('transferring','validating') LIMIT 1",
                (str(batch["job_id"]),),
            ).fetchone()
            if busy is not None:
                return False
            held = conn.execute(
                "SELECT 1 FROM deliveries WHERE job_id = ? "
                "AND state IN ('prepared','recovery_needed') LIMIT 1",
                (str(batch["job_id"]),),
            ).fetchone()
            now = _utcnow()
            job_id = str(batch["job_id"])
            stop_target = batch["stop_target"]
            if held is not None or str(stop_target or "") == "blocked":
                block_reason = (
                    "prepared_pending" if held is not None else "blocked"
                )
                conn.execute(
                    "UPDATE batches SET phase = 'blocked', active_slot = NULL, "
                    "stop_target = 'blocked', "
                    "stop_reason = COALESCE(stop_reason, ?), "
                    "ended_at = ? WHERE batch_id = ?",
                    (block_reason, now, batch_id),
                )
                conn.execute(
                    "UPDATE jobs SET state = 'blocked', updated_at = ? WHERE job_id = ?",
                    (now, job_id),
                )
                self._refresh_items_for_job(conn, job_id, now)
                self._insert_event(
                    conn,
                    job_id=job_id,
                    batch_id=batch_id,
                    kind="batch_blocked",
                    payload={"batch_id": batch_id, "reason": block_reason},
                )
                return True

            if stop_target == "cancelled":
                final_phase = "cancelled"
            elif stop_target == "paused":
                final_phase = "paused"
            else:
                final_phase = "cancelled" if phase == "cancelling" else "paused"
            final_state = "cancelled" if final_phase == "cancelled" else "suspended"
            conn.execute(
                "UPDATE deliveries SET state = ?, retry_at = NULL, updated_at = ? "
                "WHERE job_id = ? AND state IN "
                "('pending','retry_wait','suspended')",
                (final_state, now, job_id),
            )
            conn.execute(
                "UPDATE batches SET phase = ?, active_slot = NULL, "
                "stop_target = ?, stop_reason = COALESCE(stop_reason, ?), "
                "ended_at = ? WHERE batch_id = ?",
                (
                    final_phase,
                    "cancelled" if final_phase == "cancelled" else "paused",
                    final_phase,
                    now,
                    batch_id,
                ),
            )
            conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (final_phase, now, job_id),
            )
            self._refresh_items_for_job(conn, job_id, now)
            self._insert_event(
                conn,
                job_id=job_id,
                batch_id=batch_id,
                kind="batch_settled",
                payload={"batch_id": batch_id, "phase": final_phase},
            )
            return True

    def _recover_interrupted(self, conn: sqlite3.Connection) -> tuple[str, ...]:
        if not self._lock_acquired:
            raise LibraryError("Interrupted recovery requires the writer lock")
        with self._transaction(conn):
            active = conn.execute(
                "SELECT * FROM batches WHERE phase IN ("
                "'running','cooling','pausing','cancelling','settling'"
                ") ORDER BY rowid"
            ).fetchall()
            if not active:
                return ()
            now = _utcnow()
            recovered: list[str] = []
            for batch in active:
                batch_id = str(batch["batch_id"])
                job_id = str(batch["job_id"])
                conn.execute(
                    "UPDATE batches SET phase = 'blocked', active_slot = NULL, "
                    "stop_target = 'blocked', stop_reason = 'interrupted', "
                    "ended_at = ? WHERE batch_id = ?",
                    (now, batch_id),
                )
                conn.execute(
                    "UPDATE jobs SET state = 'blocked', updated_at = ? WHERE job_id = ?",
                    (now, job_id),
                )
                conn.execute(
                    "UPDATE deliveries SET state = 'suspended', retry_at = NULL, "
                    "error_code = COALESCE(error_code, 'interrupted'), updated_at = ? "
                    "WHERE current_batch_id = ? AND state IN ('transferring','validating')",
                    (now, batch_id),
                )
                self._refresh_items_for_job(conn, job_id, now)
                self._insert_event(
                    conn,
                    job_id=job_id,
                    batch_id=batch_id,
                    kind="batch_interrupted",
                    payload={"batch_id": batch_id, "reason": "interrupted"},
                )
                recovered.append(batch_id)
            return tuple(recovered)

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def _request_from_row(self, row: sqlite3.Row) -> CollectionRequest:
        try:
            return CollectionRequest.from_json(row["request_json"])
        except (ContractError, TypeError, ValueError, KeyError) as exc:
            raise CorruptDataError("Corrupt collection request JSON") from exc

    def _batch_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        budget = _parse_json_object(row["budget_json"], "batch budget")
        if type(budget.get("max_works")) is not int:
            raise CorruptDataError("Corrupt batch work budget")
        return {
            "batch_id": row["batch_id"],
            "job_id": row["job_id"],
            "command_id": row["command_id"],
            "budget_json": row["budget_json"],
            "budget": budget,
            "phase": row["phase"],
            "stop_reason": row["stop_reason"],
            "stop_target": row["stop_target"],
            "coverage_complete": (
                None
                if row["coverage_complete"] is None
                else bool(row["coverage_complete"])
            ),
            "active": bool(row["active_slot"]),
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
        }

    def _delivery_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = _parse_json_object(row["payload_json"], "delivery payload")
        quality = _parse_json_object(row["quality_json"], "delivery quality")
        output_ref = _parse_json_object(row["output_ref_json"], "delivery output ref")
        page = payload.get("page")
        if not isinstance(page, dict) or not all(
            key in page for key in ("page_index", "output_ref", "url")
        ):
            raise CorruptDataError("Corrupt delivery page payload")
        payload_quality = payload.get("quality")
        if not isinstance(payload_quality, dict) or not all(
            key in payload_quality
            for key in ("original", "all_pages", "validation_policy_version")
        ):
            raise CorruptDataError("Corrupt delivery payload quality")
        if not isinstance(page.get("output_ref"), dict):
            raise CorruptDataError("Corrupt delivery page output reference")
        if not all(
            key in quality
            for key in ("original", "all_pages", "validation_policy_version")
        ):
            raise CorruptDataError("Corrupt delivery quality payload")
        if not all(key in output_ref for key in ("kind", "root_id", "relative_path")):
            raise CorruptDataError("Corrupt delivery output reference")
        try:
            final_ref = path_ref_from_object(output_ref)
        except LibraryError as exc:
            raise CorruptDataError("Corrupt delivery output reference") from exc
        delivery_id = str(row["delivery_id"])
        checkpoint = self._parse_transfer_checkpoint_json(
            row["checkpoint_json"], delivery_id, final_ref
        )
        prepared = self._parse_prepared_json(
            row["prepared_json"], delivery_id, final_ref
        )
        return {
            "delivery_id": row["delivery_id"],
            "job_id": row["job_id"],
            "origin_batch_id": row["origin_batch_id"],
            "current_batch_id": row["current_batch_id"],
            "completed_batch_id": row["completed_batch_id"],
            "state": row["state"],
            "attempts": int(row["attempts"]),
            "http_attempts": int(row["http_attempts"]),
            "payload_json": row["payload_json"],
            "output_key": row["output_key"],
            "page_index": int(row["page_index"]),
            "illust_id": row["illust_id"],
            "quality_json": row["quality_json"],
            "generation": int(row["generation"]),
            "retry_at": row["retry_at"],
            "error_code": row["error_code"],
            "source_identity": row["source_identity"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "checkpoint_json": row["checkpoint_json"],
            "prepared_json": row["prepared_json"],
            "checkpoint": checkpoint,
            "prepared": prepared,
            "payload": payload,
            "quality": quality,
            "output_ref": output_ref,
        }

    @staticmethod
    def _same_json(left: str, right: str) -> bool:
        try:
            return normalize_json(left) == normalize_json(right)
        except ContractError as exc:
            raise CorruptDataError("Corrupt cursor JSON") from exc

    def _validate_page_policy(
        self, page_count: int | None, pages: list[PageSpec], all_pages: bool
    ) -> None:
        if page_count is None:
            raise PageCoverageError("download candidate has no known page count")
        indices = {page_spec.page_index for page_spec in pages}
        if all_pages:
            expected = set(range(page_count))
            if indices != expected:
                raise PageCoverageError("all_pages candidate does not cover required pages")
        else:
            if indices != {0}:
                raise PageCoverageError("all_pages=False accepts only page 0")

    @staticmethod
    def _validate_output_ref(root_ref: PathRef, output_ref: PathRef) -> None:
        if output_ref.kind != root_ref.kind:
            raise OutputConflict("output path kind differs from request root")
        if output_ref.root_id.casefold() != root_ref.root_id.casefold():
            raise OutputConflict("output path root_id differs from request root")
        if output_ref.relative_path == ".":
            raise OutputConflict("output path must name a file")
        root_parts = ([] if root_ref.kind == "external" and root_ref.relative_path == "." else
                      [part.casefold() for part in root_ref.relative_path.split("/")])
        output_parts = [part.casefold() for part in output_ref.relative_path.split("/")]
        if (
            len(output_parts) <= len(root_parts)
            or output_parts[: len(root_parts)] != root_parts
        ):
            raise OutputConflict("output path is outside the request root")


# Small local closing helper keeps read connections short and explicit.
@contextmanager
def contextlib_closing(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
    finally:
        conn.close()


__all__ = [
    "LibraryRepository",
    "LibraryError",
    "SchemaVersionError",
    "CommandConflict",
    "CursorConflict",
    "OutputConflict",
    "BudgetExceeded",
    "PageCoverageError",
    "NotFoundError",
    "CorruptDataError",
    "InvalidStateError",
    "LibraryClosedError",
]
