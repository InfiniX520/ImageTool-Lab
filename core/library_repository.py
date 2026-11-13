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

from core.artifact_health import (
    ArtifactExpectation,
    ArtifactHealth,
    ArtifactHealthPolicy,
    artifact_health_allows_reuse,
    assess_observation,
    unobserved_health,
)
from core.collection_scope import scope_fingerprint
from core.delivery_recovery import RECOVERY_COMMANDS, DeliveryRecoveryMixin
from core.delivery_repository import (
    DELIVERY_DDL,
    DELIVERY_REQUIRED_TABLES,
    DeliveryRepositoryMixin,
    _artifact_is_valid,
    path_ref_from_object,
)
from core.follow_repository import FOLLOW_DDL, FOLLOW_TABLES, FOLLOW_COMMANDS, FollowRepositoryMixin
from core.local_observation import LocalObserver, ObservationBudget, ObservationRequest
from core.observation_snapshot import (
    MAX_PROGRESS_EVENTS,
    MAX_PROGRESS_EVENT_RECORDS,
    MAX_PROGRESS_RECORDS,
    PROGRESS_EVENT_KIND,
    ObservationKey,
    PageBudget,
    PageKey,
    PageObservationProgress,
    PageSnapshot,
    PreflightCancelled,
    PreflightSnapshot,
    PreflightUnavailable,
    ProgressRecord,
    RetryPreflight,
    SnapshotError,
    progress_event_payload,
    progress_from_event,
)
from core.preservation_policy import LocalState, RemovalIntent
from core.removal_scope import (
    CANCELLABLE_STATES,
    CorruptIntentError,
    IntentKey,
    IntentProjection,
    read_projection,
)
from core.query_repository import QUERY_DDL, QUERY_TABLES, QUERY_COMMANDS, QueryRepositoryMixin
from core.library_paths import InstanceLock, PathResolver
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
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_HEALTH_POLICY = ArtifactHealthPolicy()
_ARTIFACT_OBSERVATION_BUDGET = ObservationBudget(
    max_bytes=4 * 1024 * 1024 * 1024,
    max_seconds=30.0,
)
_MAX_HEALTH_CANDIDATES = 8
# The writer may see evidence the preflight never covered (a concurrent
# completion).  It then asks for a new preflight instead of inventing a hold;
# the retry is bounded in count and wall time so a pathological page cannot
# spin the caller forever.
_ACCEPT_PREFLIGHT_ATTEMPTS = 3
_ACCEPT_PREFLIGHT_SECONDS = 2.0

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


class LibraryBusyError(LibraryError):
    """SQLite could not take the lock it needed before the busy timeout.

    Only lock contention (SQLITE_BUSY/SQLITE_LOCKED) is reported through this
    type; disk, corruption and constraint failures keep their own sqlite3 type.
    The original sqlite3.OperationalError is always kept as __cause__.  This is
    the v1 writer-transport busy error; the archive repository's
    ArchiveLibraryBusyError lives in a separate hierarchy
    (ArchiveLibraryError/ArchivePlanStoreError) and is deliberately not merged.
    """


# SQLite primary result codes for lock contention.  Extended result codes keep
# the primary code in the low byte; the values are spelled out because the
# module-level sqlite3 constants do not exist on every supported Python.
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6


def _is_lock_contention(exc: sqlite3.OperationalError) -> bool:
    """True only when SQLite itself reported lock contention.

    An expired busy timeout is retryable; an I/O error, a corrupt database or a
    constraint violation is not.  Classification uses the extended result code
    when Python provides it and falls back to the message only for exceptions
    built without one.
    """
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in (_SQLITE_BUSY, _SQLITE_LOCKED)
    name = getattr(exc, "sqlite_errorname", None)
    if isinstance(name, str) and name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
        return True
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _translate_busy_failure(exc: BaseException) -> BaseException:
    """Return the exception the caller should see for one failed command."""
    if isinstance(exc, sqlite3.OperationalError) and _is_lock_contention(exc):
        busy = LibraryBusyError("Library database is locked by another connection")
        busy.__cause__ = exc
        return busy
    return exc


@dataclass(slots=True)
class _Command:
    kind: str
    payload: dict[str, Any]
    future: Future
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _PagePlan:
    """One read-only snapshot of every candidate this page would observe.

    Built once per acceptance attempt, outside any write transaction.  Slices
    and retries reuse it so the database is not re-read file by file.
    """

    batch_id: str
    job_id: str
    run_id: str
    quality_key: str
    resolver: PathResolver
    selected: dict[tuple[str, int, str, str], tuple[PageKey, int, str | None, list[Any]]]


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


class LibraryRepository(
    DeliveryRecoveryMixin,
    DeliveryRepositoryMixin,
    FollowRepositoryMixin,
    QueryRepositoryMixin,
):
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
        self._path_resolver: PathResolver | None = None
        self._path_resolver_lock = threading.RLock()
        # Cancellation and the whole-page budget belong to the acceptance that
        # is currently observing files on this thread.  Thread-local keeps one
        # observer's context from leaking into another repository call.
        self._accept_context = threading.local()
        # Diagnostics only: a progress fact that the writer rejected.  It never
        # fails an acceptance, so it is recorded instead of raised.
        self._progress_write_error: str | None = None

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

    def bind_path_resolver(self, resolver: PathResolver) -> None:
        """Bind the root owner that can observe already-delivered files.

        The repository deliberately does not guess a root from its database
        location.  The production executor and the delivery tests construct the
        same resolver they use to publish files, so reuse planning and file I/O
        share one mapping.  Rebinding may add external roots but may not replace
        the project root or remove an existing external root.
        """
        if not isinstance(resolver, PathResolver):
            raise LibraryError("A PathResolver is required for artifact health")
        with self._path_resolver_lock:
            current = self._path_resolver
            if current is None:
                self._path_resolver = resolver
                return
            if current.project_root != resolver.project_root:
                raise LibraryError("A library cannot change its project root")
            previous = current.external_roots
            replacement = resolver.external_roots
            for key, root in previous.items():
                if replacement.get(key) != root:
                    raise LibraryError("A library cannot remove or move an external root")
            self._path_resolver = resolver

    def _accept_cancel(self):
        return getattr(self._accept_context, "cancel", None)

    def _accept_budget(self) -> PageBudget | None:
        return getattr(self._accept_context, "budget", None)

    def _accept_page_digest(self) -> str | None:
        return getattr(self._accept_context, "page_digest", None)

    def _path_resolver_snapshot(self) -> PathResolver | None:
        with self._path_resolver_lock:
            return self._path_resolver

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
                            # Lock contention is retryable and must not escape as a
                            # bare sqlite3.OperationalError.
                            command.future.set_exception(_translate_busy_failure(exc))
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
        if command.kind == "record_page_observation":
            return self._record_page_observation(conn, command.payload)
        if command.kind == "record_removal_intent":
            return self._record_removal_intent(conn, command.payload)
        if command.kind == "claim_delivery":
            return self._claim_delivery(conn, command.payload)
        if command.kind == "release_delivery":
            return self._release_delivery(conn, command.payload)
        if command.kind == "cancel_unpublished_delivery":
            return self._cancel_unpublished_delivery(conn, command.payload)
        if command.kind == "request_stop":
            return self._request_stop(conn, command.payload)
        if command.kind == "settle_stop":
            return self._settle_stop(conn, command.payload["batch_id"])
        if command.kind == "recover_interrupted":
            return self._recover_interrupted(conn)
        if command.kind in RECOVERY_COMMANDS:
            return self._dispatch_recovery(conn, command)
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

    def carry_unresolved_items(self, batch_id: str, *, after: str = "", limit: int = 100,
                                enforce_health: bool = True) -> Any:
        if enforce_health is not True:
            raise LibraryError("Carry health verification cannot be disabled")
        if not isinstance(after, str) or (after and not after.isdecimal()):
            raise LibraryError("Invalid unresolved item page")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise LibraryError("Invalid unresolved item page")
        health = None
        if enforce_health:
            records = self._preflight_carry_health(batch_id, after, limit)
            health = [record.to_payload() for record in records]
        batch = self.get_batch(batch_id)
        request = self.get_request(str(batch["job_id"]))
        if request.query.source == "follow":
            return super().carry_unresolved_items(
                batch_id, after=after, limit=limit, enforce_health=enforce_health,
                health=health,
            )
        return self.carry_query_unresolved_items(
            batch_id, after=after, limit=limit, enforce_health=enforce_health,
            health=health,
        )

    def admit_source_artist(self, batch_id: str, artist_id: str) -> Any:
        batch = self.get_batch(batch_id)
        request = self.get_request(str(batch["job_id"]))
        if request.query.source == "follow":
            return super().admit_source_artist(batch_id, artist_id)
        return self.admit_query_artist(batch_id, artist_id)

    def accept_source_page(
        self,
        batch_id: str,
        page: SourcePage,
        *,
        admitted_running: bool = False,
        cancel=None,
        page_budget: PageBudget | None = None,
    ) -> tuple[str, ...]:
        batch_id = _validate_identifier(batch_id, "batch id")
        if not isinstance(page, SourcePage):
            raise LibraryError("accept_source_page requires SourcePage")
        if type(admitted_running) is not bool:
            raise LibraryError("admitted_running must be a boolean")
        if cancel is not None and not callable(getattr(cancel, "is_set", None)):
            raise LibraryError("cancel must expose is_set()")
        if page_budget is not None and not isinstance(page_budget, PageBudget):
            raise LibraryError("Invalid page observation budget")
        page_json = canonical_json(asdict(page))
        digest = _sha256_text(page_json)
        deadline = time.monotonic() + _ACCEPT_PREFLIGHT_SECONDS
        attempts = 0
        last_reason = "preflight_incomplete"
        self._accept_context.cancel = cancel
        self._accept_context.budget = page_budget
        # The digest names the exact page content; it is the identity a later
        # slice or a resumed batch may reuse observations for.
        self._accept_context.page_digest = digest
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise PreflightCancelled("cancelled")
                attempts += 1
                replay = self._accepted_page_receipt(batch_id, page, digest)
                snapshot_payload = None
                if replay is None:
                    snapshot = self._preflight_page_health(batch_id, page)
                    snapshot_payload = snapshot.to_payload()
                payload = {
                    "batch_id": batch_id,
                    "page": page,
                    "page_json": page_json,
                    "page_digest": digest,
                    "snapshot": snapshot_payload,
                    "replay": replay is not None,
                    "admitted_running": admitted_running,
                }
                try:
                    return self._enqueue(
                        "accept_source_page",
                        payload,
                        size_payload={
                            "kind": "accept_source_page",
                            "batch_id": batch_id,
                            "page": asdict(page),
                            "snapshot": snapshot_payload,
                            "replay": replay is not None,
                        },
                    )
                except RetryPreflight as exc:
                    # The writer saw a completion the snapshot never covered;
                    # the transaction rolled back and wrote nothing.  Preflight
                    # again outside it and let the writer decide on evidence.
                    last_reason = exc.reason
                except SnapshotError as exc:
                    raise LibraryError("Corrupt preflight snapshot") from exc
                if (
                    attempts >= _ACCEPT_PREFLIGHT_ATTEMPTS
                    or time.monotonic() >= deadline
                ):
                    raise PreflightUnavailable(last_reason)
        finally:
            self._accept_context.cancel = None
            self._accept_context.budget = None
            self._accept_context.page_digest = None

    def record_page_observation(
        self,
        job_id: str,
        key: ObservationKey,
        records: Iterable[ProgressRecord],
        *,
        batch_id: str | None = None,
        observed_files: int = 0,
        observed_bytes: int = 0,
        slice_seq: int = 0,
    ) -> int:
        """Append one slice of page observations as an immutable fact.

        Progress never advances a cursor, writes a receipt or claims a delivery:
        it records which artifact bytes were observed for which page content,
        so a later slice or a resumed batch can skip hashing them again.  The
        page identity deliberately excludes the batch, because a resume creates
        a new batch for the same page.
        """
        job_id = _validate_identifier(job_id, "job id")
        if not isinstance(key, ObservationKey):
            raise LibraryError("record_page_observation requires ObservationKey")
        if key.job_id != job_id:
            raise LibraryError("Observation progress key does not belong to the job")
        if batch_id is not None:
            batch_id = _validate_identifier(batch_id, "batch id")
        for value, name in (
            (observed_files, "observed files"),
            (observed_bytes, "observed bytes"),
            (slice_seq, "slice sequence"),
        ):
            if type(value) is not int or value < 0:
                raise LibraryError(f"Invalid {name}")
        items = tuple(records)
        if not all(isinstance(item, ProgressRecord) for item in items):
            raise LibraryError("Observation progress records must be ProgressRecord")
        if not items:
            return 0
        self._ensure_open()
        stored = 0
        for start in range(0, len(items), MAX_PROGRESS_EVENT_RECORDS):
            chunk = items[start : start + MAX_PROGRESS_EVENT_RECORDS]
            roots = {
                record.health.expectation.path.root_id: record.root_revision
                for record in chunk
            }
            event = progress_event_payload(
                key,
                chunk,
                observed_files=observed_files,
                observed_bytes=observed_bytes,
                slice_seq=slice_seq,
                root_revisions=roots,
                policy_version=_ARTIFACT_HEALTH_POLICY.version,
            )
            stored += int(
                self._enqueue(
                    "record_page_observation",
                    {"job_id": job_id, "batch_id": batch_id, "event": event},
                    size_payload={
                        "kind": "record_page_observation",
                        "job_id": job_id,
                        "event": event,
                    },
                )
                or 0
            )
        return stored

    def record_removal_intent(
        self,
        batch_id: str,
        illust_id: str,
        page_index: int,
        output_ref: PathRef,
        *,
        intent: str,
        command_id: str,
    ) -> dict[str, Any]:
        """Record or revoke a user's intentional removal/transfer decision.

        This is an append-only event, not a deletion.  EXCLUDED suppresses an
        automatic repair of a missing page; OFFLOADED records the user's
        transfer intent without claiming that a remote copy is verified.
        """
        batch_id = _validate_identifier(batch_id, "batch id")
        illust_id = _validate_identifier(illust_id, "illust id")
        command_id = _validate_identifier(command_id, "command id")
        if type(page_index) is not int or not 0 <= page_index <= 9999:
            raise LibraryError("Invalid page index")
        if not isinstance(output_ref, PathRef):
            raise LibraryError("A validated output reference is required")
        try:
            normalized_intent = RemovalIntent(intent)
        except ValueError:
            raise LibraryError("Unsupported removal intent") from None
        if normalized_intent not in {RemovalIntent.EXCLUDED, RemovalIntent.NONE}:
            raise LibraryError("Offloaded removal intent is not wired yet")
        return self._enqueue(
            "record_removal_intent",
            {
                "batch_id": batch_id,
                "command_id": command_id,
                "illust_id": illust_id,
                "page_index": page_index,
                "output_ref": asdict(output_ref),
                "intent": normalized_intent.value,
            },
        )

    def publish_authorized(self, delivery_id: str) -> dict[str, Any]:
        """Read-only publication gate for the delivery executor.

        Returns `{"allowed", "reason"}`.  The executor calls this immediately
        before it creates the final name; that call is the publication
        linearization point.  Confirmation does not re-check intent: it only
        completes bytes that are already on disk.  An unreadable projection
        denies publication instead of guessing that the user never excluded
        the page.
        """
        delivery_id = _validate_identifier(delivery_id, "delivery id")
        self._ensure_open()
        with contextlib_closing(self._readonly_connection()) as conn:
            delivery = self._get_delivery_row(conn, delivery_id)
            if delivery is None:
                raise NotFoundError(f"Unknown delivery: {delivery_id}")
            try:
                output_ref = path_ref_from_object(
                    _parse_json_object(
                        delivery["output_ref_json"], "delivery output reference"
                    )
                )
            except (ContractError, LibraryError) as exc:
                raise CorruptDataError(
                    "Delivery has an invalid output reference"
                ) from exc
            projection = read_projection(conn)
        if projection.corrupt_events:
            return {
                "delivery_id": delivery_id,
                "allowed": False,
                "reason": "intent_unreadable",
            }
        key = IntentKey(
            illust_id=str(delivery["illust_id"]),
            page_index=int(delivery["page_index"]),
            output_key=str(delivery["output_key"]),
            root_revision=self._root_revision_for(output_ref),
        )
        if projection.is_excluded(key):
            return {
                "delivery_id": delivery_id,
                "allowed": False,
                "reason": "intent_excluded",
            }
        return {"delivery_id": delivery_id, "allowed": True, "reason": "ok"}

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
        health = self._preflight_claim_health(delivery_id, batch_id)
        payload: dict[str, Any] = {"delivery_id": delivery_id, "batch_id": batch_id}
        if health is not None:
            payload["health"] = [record.to_payload() for record in health]
        result = self._enqueue("claim_delivery", payload)
        if isinstance(result, dict) and "corrupt_intent" in result:
            # The writer already blocked the batch (durably) and appended a
            # diagnostic event; surface the corruption to the caller.
            raise CorruptDataError(
                f"removal intent event {result['corrupt_intent']} is unreadable; "
                "the batch is blocked"
            )
        return result

    def _preflight_claim_health(
        self, delivery_id: str, batch_id: str
    ) -> tuple[ArtifactHealth, ...] | None:
        """Observe completed pages of the work a recovered claim will copy.

        The target batch may have no page links yet (resume followed by a
        direct claim).  Without this preflight, copying an old artifact link
        would bypass the health decision made by the accept path.
        """
        resolver = self._path_resolver_snapshot()
        records: dict[str, ArtifactHealth] = {}
        try:
            with contextlib_closing(self._readonly_connection()) as conn:
                delivery = conn.execute(
                    "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
                ).fetchone()
                if delivery is None:
                    return ()
                source_batch_id = str(delivery["origin_batch_id"])
                illust_id = str(delivery["illust_id"])
                target_pages = {
                    int(row["page_index"])
                    for row in conn.execute(
                        "SELECT page_index FROM batch_item_pages "
                        "WHERE batch_id = ? AND illust_id = ?",
                        (batch_id, illust_id),
                    ).fetchall()
                }
                rows = conn.execute(
                    "SELECT * FROM batch_item_pages WHERE batch_id = ? AND illust_id = ?",
                    (source_batch_id, illust_id),
                ).fetchall()
                missing_rows = [
                    row for row in rows if int(row["page_index"]) not in target_pages
                ]
                if not missing_rows:
                    # The target batch already made all page decisions; never
                    # overwrite them.
                    return ()
                if resolver is None:
                    return ()
                for row in missing_rows:
                    artifact = None
                    if row["artifact_id"] is not None:
                        artifact = conn.execute(
                            "SELECT a.*, d.quality_key AS delivery_quality_key "
                            "FROM artifacts AS a JOIN deliveries AS d "
                            "ON d.delivery_id = a.delivery_id WHERE a.artifact_id = ?",
                            (str(row["artifact_id"]),),
                        ).fetchone()
                    elif row["delivery_id"] is not None:
                        artifact = conn.execute(
                            "SELECT a.*, d.quality_key AS delivery_quality_key "
                            "FROM artifacts AS a JOIN deliveries AS d "
                            "ON d.delivery_id = a.delivery_id WHERE a.delivery_id = ?",
                            (str(row["delivery_id"]),),
                        ).fetchone()
                    if artifact is None:
                        continue
                    artifact_id = str(artifact["artifact_id"])
                    if artifact_id in records:
                        continue
                    record = self._observe_artifact(
                        dict(artifact),
                        resolver=resolver,
                        run_id=f"claim-{delivery_id}",
                        quality_key=str(artifact["delivery_quality_key"] or ""),
                    )
                    if record is not None:
                        records[artifact_id] = record
        except (sqlite3.Error, ContractError, OSError, ValueError):
            return ()
        return tuple(records.values())

    def _preflight_work_health(
        self, source_batch_id: str, illust_id: str, run_id: str
    ) -> tuple[ArtifactHealth, ...]:
        """Observe completed artifacts of one source work for carry/claim."""
        resolver = self._path_resolver_snapshot()
        if resolver is None:
            return ()
        records: dict[str, ArtifactHealth] = {}
        try:
            with contextlib_closing(self._readonly_connection()) as conn:
                rows = conn.execute(
                    "SELECT * FROM batch_item_pages WHERE batch_id = ? AND illust_id = ?",
                    (source_batch_id, illust_id),
                ).fetchall()
                for row in rows:
                    artifact = None
                    if row["artifact_id"] is not None:
                        artifact = conn.execute(
                            "SELECT a.*, d.quality_key AS delivery_quality_key "
                            "FROM artifacts AS a JOIN deliveries AS d "
                            "ON d.delivery_id = a.delivery_id WHERE a.artifact_id = ?",
                            (str(row["artifact_id"]),),
                        ).fetchone()
                    elif row["delivery_id"] is not None:
                        artifact = conn.execute(
                            "SELECT a.*, d.quality_key AS delivery_quality_key "
                            "FROM artifacts AS a JOIN deliveries AS d "
                            "ON d.delivery_id = a.delivery_id WHERE a.delivery_id = ?",
                            (str(row["delivery_id"]),),
                        ).fetchone()
                    if artifact is None:
                        continue
                    artifact_id = str(artifact["artifact_id"])
                    if artifact_id in records:
                        continue
                    record = self._observe_artifact(
                        dict(artifact),
                        resolver=resolver,
                        run_id=run_id,
                        quality_key=str(artifact["delivery_quality_key"] or ""),
                    )
                    if record is not None:
                        records[artifact_id] = record
        except (sqlite3.Error, ContractError, OSError, ValueError):
            return ()
        return tuple(records.values())

    def _preflight_carry_health(
        self, batch_id: str, after: str, limit: int
    ) -> tuple[ArtifactHealth, ...]:
        resolver = self._path_resolver_snapshot()
        if resolver is None:
            return ()
        try:
            with contextlib_closing(self._readonly_connection()) as conn:
                batch = self._get_batch_row(conn, batch_id)
                if batch is None:
                    return ()
                job = self._get_job_row(conn, str(batch["job_id"]))
                if job is None:
                    return ()
                rows = conn.execute(
                    "SELECT bi.batch_id, bi.illust_id FROM batch_items AS bi "
                    "JOIN batches AS b ON b.batch_id = bi.batch_id "
                    "WHERE b.job_id = ? AND bi.batch_id != ? AND bi.illust_id > ? "
                    "AND NOT EXISTS (SELECT 1 FROM batch_items AS newer "
                    "JOIN batches AS nb ON nb.batch_id = newer.batch_id "
                    "WHERE nb.job_id = b.job_id AND newer.illust_id = bi.illust_id "
                    "AND nb.rowid > b.rowid) "
                    "AND (bi.decision IN ('metadata_unknown','unavailable','unsupported') "
                    "OR (bi.decision = 'download' AND bi.result_state IN "
                    "('failed','needs_verification','partial') AND NOT EXISTS ("
                    "SELECT 1 FROM batch_item_pages AS bip JOIN deliveries AS d "
                    "ON d.delivery_id = bip.delivery_id WHERE bip.batch_id = bi.batch_id "
                    "AND bip.illust_id = bi.illust_id AND d.state IN "
                    "('pending','transferring','validating','suspended','retry_wait')))) "
                    "ORDER BY bi.illust_id LIMIT ?",
                    (str(job["job_id"]), batch_id, after, int(limit)),
                ).fetchall()
        except (sqlite3.Error, ContractError, ValueError):
            return ()
        records: dict[str, ArtifactHealth] = {}
        for row in rows:
            for record in self._preflight_work_health(
                str(row["batch_id"]), str(row["illust_id"]),
                f"carry-{batch_id}-{row['illust_id']}",
            ):
                records[record.expectation.artifact_id] = record
        return tuple(records.values())

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

    def cancel_unpublished_delivery(
        self,
        delivery_id: str,
        batch_id: str,
        *,
        error_code: str,
        expected_state: str = "prepared",
        expected_prepared_sha256: str | None = None,
        expected_generation: int | None = None,
    ) -> None:
        """Cancel a prepared delivery whose final name was never created.

        The executor calls this only after it verified the final path does not
        exist and that the staged temp file belongs to this delivery (UA-11).
        A published file is never rewritten by this command.

        ``expected_prepared_sha256`` and ``expected_generation`` bind the
        cancellation to the exact evidence the caller observed.  The writer
        refuses a row whose prepared digest or generation moved in the
        meantime, so a staged file that was replaced while the command waited
        is never deleted by guesswork (RFA-06).  Both stay optional: a caller
        that observed nothing keeps the previous behaviour.
        """
        delivery_id = _validate_identifier(delivery_id, "delivery id")
        batch_id = _validate_identifier(batch_id, "batch id")
        error_code = _validate_error_code(error_code)
        if expected_state not in {"prepared", "recovery_needed"}:
            raise LibraryError(
                "cancel_unpublished_delivery supports prepared/recovery_needed"
            )
        if expected_prepared_sha256 is not None:
            if (
                not isinstance(expected_prepared_sha256, str)
                or _SHA256_RE.fullmatch(expected_prepared_sha256) is None
            ):
                raise LibraryError(
                    "expected_prepared_sha256 must be a lowercase hex digest"
                )
        if expected_generation is not None:
            if type(expected_generation) is not int or expected_generation < 1:
                raise LibraryError(
                    "expected_generation must be a positive integer"
                )
        self._enqueue(
            "cancel_unpublished_delivery",
            {
                "delivery_id": delivery_id,
                "batch_id": batch_id,
                "error_code": error_code,
                "expected_state": expected_state,
                "expected_prepared_sha256": expected_prepared_sha256,
                "expected_generation": expected_generation,
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

    def _accepted_page_receipt(
        self, batch_id: str, page: SourcePage, digest: str
    ) -> tuple[str, ...] | None:
        """Return the stored delivery ids when this exact page was accepted.

        A pure read: no artifact file is opened, so an idempotent replay cannot
        rehash a multi-gigabyte image (UA-09).  A different digest is not a
        replay; the writer raises CursorConflict for it as before.
        """
        try:
            with contextlib_closing(self._readonly_connection()) as conn:
                row = conn.execute(
                    "SELECT p.content_digest, p.delivery_ids_json "
                    "FROM source_batch_pages AS p "
                    "JOIN batches AS b ON b.batch_id = p.batch_id "
                    "WHERE p.batch_id = ? AND p.source_id = ? AND p.pass_id = ? "
                    "AND p.entity_id = ? AND p.page_identity = ? "
                    "AND b.phase = 'running'",
                    (
                        batch_id,
                        page.source_id,
                        page.pass_id,
                        page.entity_id,
                        page.page_identity,
                    ),
                ).fetchone()
        except (sqlite3.Error, LibraryError):
            return None
        if row is None or str(row["content_digest"]) != digest:
            return None
        try:
            stored = json.loads(row["delivery_ids_json"])
        except (TypeError, ValueError):
            return None
        if not isinstance(stored, list) or not all(
            isinstance(item, str) for item in stored
        ):
            return None
        return tuple(stored)

    def _preflight_page_health(
        self, batch_id: str, page: SourcePage
    ) -> PreflightSnapshot:
        """Observe this page's candidates before the writer transaction.

        Returns a frozen snapshot, never a bare health list.  The writer must be
        able to tell "the preflight looked at this exact completed generation
        and found the file changed" from "the preflight never saw this page"
        (UA-01); the old empty list collapsed both into `artifact_unknown`.

        Every database read happens in one read-only snapshot before any file
        is opened; the writer revalidates each record against its own row.

        One budget is one *slice*, not the page's permanent limit: observations
        an earlier slice already persisted are reused, a page that needs more
        files than one slice continues in the next slice, and only a candidate
        that cannot fit a slice at all stops the page.  Nothing here writes a
        delivery, receipt or cursor: the caller's atomic acceptance remains the
        only point where a page becomes committed.
        """
        run_id = f"batch-{batch_id}"
        resolver = self._path_resolver_snapshot()
        if resolver is None:
            return PreflightSnapshot(
                run_id=run_id, pages={}, degraded=True, reason="no_resolver"
            )
        plan = self._plan_page_health(batch_id, page, resolver=resolver, run_id=run_id)
        if isinstance(plan, PreflightSnapshot):
            return plan
        return self._observe_page_plan(plan, page)

    def _plan_page_health(
        self,
        batch_id: str,
        page: SourcePage,
        *,
        resolver: PathResolver,
        run_id: str,
    ) -> _PagePlan | PreflightSnapshot:
        """Read every candidate row this page would observe, in one snapshot."""
        selected: dict[
            tuple[str, int, str, str], tuple[PageKey, int, str | None, list[Any]]
        ] = {}
        quality_key = ""
        try:
            with contextlib_closing(self._readonly_connection()) as conn:
                batch = self._get_batch_row(conn, batch_id)
                if batch is None or str(batch["phase"]) != "running":
                    return PreflightSnapshot(
                        run_id=run_id, pages={}, degraded=True, reason="batch_not_running"
                    )
                job = self._get_job_row(conn, str(batch["job_id"]))
                if job is None:
                    return PreflightSnapshot(
                        run_id=run_id, pages={}, degraded=True, reason="job_missing"
                    )
                request = self._request_from_row(job)
                quality_json = canonical_json(asdict(request.quality))
                quality_key = _sha256_text(quality_json)
                for candidate in page.candidates:
                    if candidate.decision != "download":
                        continue
                    for page_spec in candidate.pages:
                        key = PageKey(
                            illust_id=candidate.illust_id,
                            page_index=int(page_spec.page_index),
                            output_key=page_spec.output_ref.target_key,
                            quality_key=quality_key,
                        )
                        generation = 0
                        delivery_id: str | None = None
                        artifacts: list[Any] = []
                        same = conn.execute(
                            "SELECT delivery_id, generation FROM deliveries "
                            "WHERE job_id = ? AND illust_id = ? AND page_index = ? "
                            "AND quality_key = ? AND output_key = ? AND state = 'completed' "
                            "ORDER BY generation DESC LIMIT 1",
                            (
                                str(job["job_id"]),
                                candidate.illust_id,
                                int(page_spec.page_index),
                                quality_key,
                                page_spec.output_ref.target_key,
                            ),
                        ).fetchone()
                        if same is not None:
                            generation = int(same["generation"])
                            delivery_id = str(same["delivery_id"])
                            artifact = conn.execute(
                                "SELECT * FROM artifacts WHERE delivery_id = ?",
                                (delivery_id,),
                            ).fetchone()
                            if artifact is not None:
                                artifacts.append(artifact)
                        if request.dedup_policy == "trusted":
                            artifacts.extend(
                                self._find_reusable_artifact_candidates(
                                    conn,
                                    job_id=str(job["job_id"]),
                                    illust_id=candidate.illust_id,
                                    page_index=int(page_spec.page_index),
                                    output_key=page_spec.output_ref.target_key,
                                    original=request.quality.original,
                                    validation_policy_version=request.quality.validation_policy_version,
                                    limit=_MAX_HEALTH_CANDIDATES,
                                )
                            )
                        selected[key.storage_key] = (key, generation, delivery_id, artifacts)
        except (sqlite3.Error, ContractError, OSError, ValueError):
            # No page was observed; the writer decides on an honest failure,
            # never on a fabricated "unknown" hold for a completed delivery.
            return PreflightSnapshot(
                run_id=run_id, pages={}, degraded=True, reason="preflight_failed"
            )
        # The read snapshot is closed before any file is opened.
        return _PagePlan(
            batch_id=batch_id,
            job_id=str(job["job_id"]),
            run_id=run_id,
            quality_key=quality_key,
            resolver=resolver,
            selected=selected,
        )

    def _observe_page_plan(
        self, plan: _PagePlan, page: SourcePage
    ) -> PreflightSnapshot:
        """Observe one planned page in slices, reusing usable observations."""
        cancel = self._accept_cancel()
        budget = self._accept_budget()
        digest = self._accept_page_digest()
        key: ObservationKey | None = None
        if digest is not None:
            try:
                key = ObservationKey(
                    job_id=plan.job_id,
                    source_id=page.source_id,
                    pass_id=page.pass_id,
                    entity_id=page.entity_id,
                    page_identity=page.page_identity,
                    page_digest=digest,
                )
            except SnapshotError:
                key = None
        progress = None if key is None else self._load_observation_progress(key)
        slices_enabled = progress is not None and budget is not None
        records: dict[str, ArtifactHealth | None] = {}
        ordered: list[tuple[Any, str]] = []
        seen: set[str] = set()
        for _, _, _, artifacts in plan.selected.values():
            for artifact in artifacts:
                artifact_id = str(artifact["artifact_id"])
                if artifact_id in seen:
                    continue
                seen.add(artifact_id)
                ordered.append((artifact, artifact_id))
        new_records: list[ProgressRecord] = []
        fresh_files = 0
        fresh_bytes = 0
        slices = 0
        slice_started = time.monotonic()

        def flush() -> None:
            """Hand this slice's new observations to the progress log."""
            nonlocal new_records, fresh_files, fresh_bytes
            pending = new_records
            new_records = []
            if not pending or key is None:
                fresh_files, fresh_bytes = 0, 0
                return
            self._persist_progress(
                key,
                tuple(pending),
                batch_id=plan.batch_id,
                observed_files=fresh_files,
                observed_bytes=fresh_bytes,
                slice_seq=slices + 1,
            )
            fresh_files, fresh_bytes = 0, 0

        def close_slice() -> None:
            """Persist the finished slice, then open the next one or stop.

            A call may take at most `max_slices` slices, so a page that fills the
            last allowed slice returns a budget result even when nothing is left
            to observe; the following call then commits without opening a file.
            """
            nonlocal slices, slice_started
            flush()
            slices += 1
            slice_started = time.monotonic()
            if budget is not None and slices >= budget.max_slices:
                raise PreflightCancelled("page_slice_limit")

        index = 0
        while index < len(ordered):
            artifact, artifact_id = ordered[index]
            if cancel is not None and cancel.is_set():
                # A pause/close stops the remaining candidates instead of
                # hashing the whole page; this is a control result, not a
                # file-health verdict.
                flush()
                raise PreflightCancelled("cancelled")
            reused = None
            if progress is not None:
                reused = self._progress_health(
                    progress.record(artifact_id),
                    artifact,
                    quality_key=plan.quality_key,
                    resolver=plan.resolver,
                )
            if reused is not None:
                # An earlier slice already observed these exact bytes within
                # the freshness window; do not open the file again.
                records[artifact_id] = reused
                index += 1
                continue
            if budget is not None:
                exhausted = self._slice_exhausted(
                    budget, fresh_files, fresh_bytes, slice_started
                )
                if exhausted is not None:
                    if not slices_enabled:
                        raise PreflightCancelled(exhausted)
                    # The slice is full (or over its byte/time budget): the
                    # next file starts a fresh slice, never an N+1 in this one.
                    close_slice()
                    continue
            try:
                artifact_path = path_ref_from_object(
                    _parse_json_object(
                        artifact["output_ref_json"], "artifact output reference"
                    )
                )
                plan.resolver.root_path(artifact_path)
            except (ContractError, LibraryError, KeyError, TypeError, ValueError):
                records[artifact_id] = None
                index += 1
                continue
            try:
                record = self._observe_artifact(
                    artifact,
                    resolver=plan.resolver,
                    run_id=plan.run_id,
                    quality_key=plan.quality_key,
                )
            except (ContractError, OSError, ValueError):
                flush()
                return PreflightSnapshot(
                    run_id=plan.run_id,
                    pages={},
                    degraded=True,
                    reason="observation_failed",
                )
            records[artifact_id] = record
            fresh_files += 1
            if record is not None:
                fresh_bytes += int(record.bytes_read or 0)
                if key is not None:
                    new_records.append(
                        ProgressRecord(
                            artifact_id=artifact_id,
                            root_revision=record.expectation.root_revision,
                            health=record,
                        )
                    )
            index += 1
            if (
                slices_enabled
                and budget is not None
                and fresh_files >= budget.max_files
            ):
                # A full slice ends as soon as it is full: one call may take at
                # most max_slices slices, and the next call continues from the
                # durable progress instead of re-reading these files.
                close_slice()
        # Every slice, including the last one, leaves its observations behind:
        # a crash between the last observation and the atomic commit must not
        # force the same files to be hashed again.
        flush()
        try:
            pages: dict[tuple[str, int, str, str], PageSnapshot] = {}
            for page_key, generation, delivery_id, artifacts in plan.selected.values():
                observed: list[ArtifactHealth] = []
                unobserved: list[str] = []
                for artifact in artifacts:
                    artifact_id = str(artifact["artifact_id"])
                    health = records.get(artifact_id)
                    if health is None:
                        unobserved.append(artifact_id)
                    else:
                        observed.append(health)
                pages[page_key.storage_key] = PageSnapshot(
                    key=page_key,
                    generation=generation,
                    delivery_id=delivery_id,
                    observed=tuple(observed),
                    unobserved=tuple(unobserved),
                    degraded=False,
                    reason="ok",
                )
            return PreflightSnapshot(run_id=plan.run_id, pages=pages)
        except SnapshotError:
            return PreflightSnapshot(
                run_id=plan.run_id,
                pages={},
                degraded=True,
                reason="snapshot_invalid",
            )

    @staticmethod
    def _slice_exhausted(
        budget: PageBudget, fresh_files: int, fresh_bytes: int, started: float
    ) -> str | None:
        """Name the budget a slice has used up, or None while it still fits."""
        if fresh_files >= budget.max_files:
            return "page_file_budget"
        if fresh_bytes > budget.max_bytes:
            return "page_byte_budget"
        if time.monotonic() - started > float(budget.max_seconds):
            return "page_time_budget"
        return None

    def _load_observation_progress(
        self, key: ObservationKey
    ) -> PageObservationProgress | None:
        """Read the newest progress facts for this page.

        None means "start over": losing progress only costs hashes, never
        correctness, so an unreadable event log is not a page failure.  A
        malformed fact is skipped instead of being trusted.
        """
        try:
            with contextlib_closing(self._readonly_connection()) as conn:
                rows = conn.execute(
                    "SELECT safe_payload FROM events WHERE kind = ? AND job_id = ? "
                    "ORDER BY event_seq DESC LIMIT ?",
                    (PROGRESS_EVENT_KIND, key.job_id, MAX_PROGRESS_EVENTS),
                ).fetchall()
        except (sqlite3.Error, LibraryError):
            return None
        records: dict[str, ProgressRecord] = {}
        truncated = False
        for row in rows:
            try:
                stored_key, stored = progress_from_event(
                    json.loads(str(row["safe_payload"]))
                )
            except (TypeError, ValueError, SnapshotError):
                continue
            if stored_key != key:
                continue
            for record in stored:
                if len(records) >= MAX_PROGRESS_RECORDS:
                    truncated = True
                    break
                # Newest event first: the first record per artifact wins.
                records.setdefault(record.artifact_id, record)
        return PageObservationProgress(key=key, records=records, truncated=truncated)

    def _persist_progress(
        self,
        key: ObservationKey,
        records: tuple[ProgressRecord, ...],
        *,
        batch_id: str,
        observed_files: int,
        observed_bytes: int,
        slice_seq: int,
    ) -> None:
        """Persist one slice of observations; never fail the acceptance.

        A progress fact is an accelerator, not a precondition.  If the writer
        rejects it, this page is still observed and accepted; the only cost is
        that a later slice hashes the same files again.
        """
        try:
            self.record_page_observation(
                key.job_id,
                key,
                records,
                batch_id=batch_id,
                observed_files=observed_files,
                observed_bytes=observed_bytes,
                slice_seq=slice_seq,
            )
        except (LibraryError, ContractError, OSError, sqlite3.Error) as exc:
            self._progress_write_error = type(exc).__name__

    def _progress_health(
        self,
        record: ProgressRecord | None,
        artifact: Any,
        *,
        quality_key: str,
        resolver: PathResolver,
    ) -> ArtifactHealth | None:
        """Return a recorded observation that still counts as current evidence.

        Every condition of the frozen reuse rule is re-checked against the row
        read for *this* attempt: artifact identity and bytes, page quality
        decision, mapped root revision, policy version and observation age.
        Anything else is dropped and observed again: an absent or unreadable
        artifact is never promoted to healthy, and a stale record is never
        treated as current proof.
        """
        if record is None or not _artifact_is_valid(artifact):
            return None
        health = record.health
        if health.policy != _ARTIFACT_HEALTH_POLICY:
            return None
        if health.state in {
            LocalState.UNKNOWN,
            LocalState.UNREADABLE,
            LocalState.ROOT_UNAVAILABLE,
        }:
            return None
        expectation = health.expectation
        if not expectation.matches_row(artifact):
            return None
        if expectation.quality_key != quality_key or record.quality_key != quality_key:
            return None
        if expectation.policy_version != _ARTIFACT_HEALTH_POLICY.version:
            return None
        try:
            revision = resolver.root_revision(expectation.path)
        except ContractError:
            return None
        if revision != expectation.root_revision or revision != record.root_revision:
            return None
        if not health.is_fresh(_utcnow()):
            return None
        if not self._progress_object_is_unchanged(health, resolver):
            return None
        return health

    @staticmethod
    def _progress_object_is_unchanged(
        health: ArtifactHealth, resolver: PathResolver
    ) -> bool:
        """Compare the recorded object identity with the file on disk now.

        A stat is not a read: it costs one metadata lookup, never a hash.  It
        catches the file that was replaced, moved or resized between slices
        while the record was still inside its freshness window.  A same-size
        in-place rewrite keeps the identity and is deliberately left to the
        publication/confirmation gates instead of re-hashing every page.
        """
        identity = health.identity
        if identity is None or health.observed_size is None:
            return False
        try:
            info = os.stat(resolver.resolve(health.expectation.path))
        except (OSError, ContractError, ValueError):
            return False
        return (
            int(identity.device) == int(info.st_dev)
            and int(identity.file_index) == int(info.st_ino)
            and int(health.observed_size) == int(info.st_size)
        )

    def _observe_artifact(
        self,
        artifact: Any,
        *,
        resolver: PathResolver,
        run_id: str,
        quality_key: str,
    ) -> ArtifactHealth | None:
        if not _artifact_is_valid(artifact):
            return None
        artifact_id = str(artifact["artifact_id"])
        try:
            output_ref = _parse_json_object(
                artifact["output_ref_json"], "artifact output reference"
            )
            path = path_ref_from_object(output_ref)
            root = resolver.root_path(path)
            root_revision = resolver.root_revision(path)
            expectation = ArtifactExpectation.from_artifact_row(
                artifact,
                path=path,
                root_revision=root_revision,
                quality_key=quality_key,
                policy_version=_ARTIFACT_HEALTH_POLICY.version,
            )
        except (ContractError, KeyError, TypeError, ValueError):
            return None
        try:
            observer = LocalObserver(
                root,
                root_id=path.root_id,
                kind=path.kind,
                root_revision=root_revision,
            )
            observation = observer.observe(
                ObservationRequest(
                    content=expectation.content,
                    path=path,
                    run_id=run_id,
                    check_id=f"artifact-{artifact_id}",
                    budget=_ARTIFACT_OBSERVATION_BUDGET,
                )
            )
            return assess_observation(
                expectation, observation, _ARTIFACT_HEALTH_POLICY
            )
        except ContractError:
            return unobserved_health(
                expectation,
                state=LocalState.UNKNOWN,
                reason="observation_rejected",
                checked_at=_utcnow(),
                policy=_ARTIFACT_HEALTH_POLICY,
            )
        except OSError:
            return unobserved_health(
                expectation,
                state=LocalState.UNREADABLE,
                reason="observation_failed",
                checked_at=_utcnow(),
                policy=_ARTIFACT_HEALTH_POLICY,
            )

    def _health_map(self, value: object) -> dict[str, ArtifactHealth]:
        """Parse only fresh records bound to the currently mapped roots.

        A queued command can outlive its preflight.  Dropping stale or
        re-mapped records here makes every downstream path fail closed instead
        of treating an old observation as current.
        """
        if not isinstance(value, list):
            return {}
        resolver = self._path_resolver_snapshot()
        if resolver is None:
            return {}
        now = _utcnow()
        records: dict[str, ArtifactHealth] = {}
        for item in value:
            try:
                record = ArtifactHealth.from_payload(item)
            except (ContractError, TypeError, ValueError):
                continue
            if record.policy != _ARTIFACT_HEALTH_POLICY:
                continue
            if not record.is_fresh(now):
                continue
            try:
                revision = resolver.root_revision(record.expectation.path)
            except ContractError:
                continue
            if revision != record.expectation.root_revision:
                continue
            records[record.expectation.artifact_id] = record
        return records

    def _health_is_current(self, health) -> bool:
        """Same currency gate `_health_map` applies to a queued payload.

        Freshness, policy version and the currently mapped root revision must
        all still hold.  The snapshot is produced milliseconds earlier, but the
        writer re-checks instead of trusting its own message.
        """
        if health is None or health.policy != _ARTIFACT_HEALTH_POLICY:
            return False
        if not health.is_fresh(_utcnow()):
            return False
        resolver = self._path_resolver_snapshot()
        if resolver is None:
            return False
        try:
            revision = resolver.root_revision(health.expectation.path)
        except ContractError:
            return False
        return revision == health.expectation.root_revision

    def _health_allows_reuse(self, health, artifact) -> bool:
        resolver = self._path_resolver_snapshot()
        if health is None or resolver is None:
            return False
        try:
            root_revision = resolver.root_revision(health.expectation.path)
        except ContractError:
            return False
        return artifact_health_allows_reuse(
            health, artifact, now=_utcnow(), root_revision=root_revision
        )

    def _root_revision_for(self, output_ref: PathRef) -> int:
        resolver = self._path_resolver_snapshot()
        if resolver is None:
            return 1
        try:
            return resolver.root_revision(output_ref)
        except ContractError:
            return 1

    def _removal_intents_for_page(self, conn, page: SourcePage) -> IntentProjection:
        """Read the one global intent projection for this source page.

        The scan is ordered and latest-wins, exactly as before, but an
        unreadable event now blocks the command instead of silently becoming
        "no intent" (UA-10).  The caller's write transaction then rolls back
        with no page accepted.
        """
        del page  # the projection is global; the parameter keeps call sites explicit
        try:
            projection = read_projection(conn)
            projection.require_clean()
        except CorruptIntentError as exc:
            raise CorruptDataError(
                f"Removal intent event {exc.event_seq} is unreadable"
            ) from exc
        return projection

    def _record_removal_intent(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> dict[str, Any]:
        batch_id = str(payload["batch_id"])
        command_id = str(payload["command_id"])
        illust_id = str(payload["illust_id"])
        page_index = int(payload["page_index"])
        output_ref = path_ref_from_object(payload["output_ref"])
        intent = str(payload["intent"])
        with self._transaction(conn):
            batch = self._get_batch_row(conn, batch_id)
            if batch is None:
                raise NotFoundError(f"Unknown batch: {batch_id}")
            job = self._get_job_row(conn, str(batch["job_id"]))
            if job is None:
                raise CorruptDataError("Batch references a missing job")
            request = self._request_from_row(job)
            self._validate_output_ref(request.output.root_ref, output_ref)
            existing = self._command_event(conn, command_id)
            if existing is not None:
                result = self._command_result(existing)
                if (
                    result.get("illust_id") != illust_id
                    or result.get("page_index") != page_index
                    or result.get("output_key") != output_ref.target_key
                    or result.get("intent") != intent
                ):
                    raise CommandConflict("command id was already used for another intent")
                return result
            revision = self._root_revision_for(output_ref)
            now = _utcnow()
            result = {
                "illust_id": illust_id,
                "page_index": page_index,
                "output_key": output_ref.target_key,
                "root_revision": revision,
                "intent": intent,
            }
            self._insert_event(
                conn,
                job_id=str(job["job_id"]),
                batch_id=batch_id,
                kind="removal_intent",
                payload={
                    **result,
                    "path": asdict(output_ref),
                    "result": result,
                },
                accepted_command_id=command_id,
            )
            if intent == RemovalIntent.EXCLUDED.value:
                # The exclusion is content-addressed: every queued delivery for
                # this page, in this job or any other, must stop.  Published
                # bytes are never rewritten (UA-02).
                placeholders = ",".join("?" for _ in CANCELLABLE_STATES)
                rows = conn.execute(
                    "SELECT delivery_id FROM deliveries "
                    "WHERE illust_id = ? AND page_index = ? AND output_key = ? "
                    f"AND state IN ({placeholders})",
                    (illust_id, page_index, output_ref.target_key, *CANCELLABLE_STATES),
                ).fetchall()
                for row in rows:
                    conn.execute(
                        "UPDATE deliveries SET state = 'cancelled', "
                        "error_code = 'intent_excluded', retry_at = NULL, updated_at = ? "
                        f"WHERE delivery_id = ? AND state IN ({placeholders})",
                        (now, str(row["delivery_id"]), *CANCELLABLE_STATES),
                    )
                    self._refresh_items_for_delivery(conn, str(row["delivery_id"]), now)
            return result

    def _record_page_observation(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> int:
        """Store one progress fact; a deleted job simply has nothing to record."""
        job_id = str(payload["job_id"])
        event = payload.get("event")
        batch_id = payload.get("batch_id")
        if not isinstance(event, dict):
            raise CorruptDataError("Corrupt observation progress event")
        # Parse before writing: a malformed progress fact is never stored, so
        # the reader can trust every stored fact.
        progress_from_event(event)
        with self._transaction(conn):
            if self._get_job_row(conn, job_id) is None:
                return 0
            stored_batch: str | None = None
            if (
                batch_id is not None
                and self._get_batch_row(conn, str(batch_id)) is not None
            ):
                stored_batch = str(batch_id)
            self._insert_event(
                conn,
                job_id=job_id,
                batch_id=stored_batch,
                kind=PROGRESS_EVENT_KIND,
                payload=event,
            )
            return len(event["records"])

    def _accept_source_page(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> tuple[str, ...]:
        batch_id = str(payload["batch_id"])
        page: SourcePage = payload["page"]
        page_digest = str(payload["page_digest"])
        replay = bool(payload.get("replay"))
        snapshot_payload = payload.get("snapshot")
        snapshot = None if replay else PreflightSnapshot.from_payload(snapshot_payload)
        health_by_artifact: dict[str, ArtifactHealth] = {}
        if snapshot is not None:
            for page_snapshot in snapshot.pages.values():
                for record in page_snapshot.observed:
                    if self._health_is_current(record):
                        health_by_artifact[record.expectation.artifact_id] = record

        with self._transaction(conn):
            batch = self._get_batch_row(conn, batch_id)
            if batch is None:
                raise NotFoundError(f"Unknown batch: {batch_id}")
            phase_now = str(batch["phase"])
            if phase_now != "running" and not (
                phase_now == "pausing" and bool(payload.get("admitted_running"))
            ):
                # An acceptance admitted while the batch was running may still
                # commit into 'pausing' (the hash was already in flight); a
                # page admitted after the pause is rejected.
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

            if replay:
                # The receipt vanished between the caller's fast path and this
                # transaction.  Re-preflight instead of planning on no evidence.
                raise RetryPreflight("receipt_missing")

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
            excluded_pages: set[tuple[str, int, str]] = set()
            intents = self._removal_intents_for_page(conn, page)
            for candidate in page.candidates:
                if candidate.decision != "download":
                    continue
                for page_spec in candidate.pages:
                    key = (
                        candidate.illust_id,
                        page_spec.page_index,
                        page_spec.output_ref.target_key,
                    )
                    revision = self._root_revision_for(page_spec.output_ref)
                    if intents.is_excluded(IntentKey(*key, revision)):
                        excluded_pages.add(key)

            planned: set[str] = set()
            selected_pages: dict[str, list[PageSpec]] = {}
            output_keys: set[str] = set()
            for candidate in page.candidates:
                pages = list(candidate.pages)
                if candidate.decision == "download":
                    self._validate_page_policy(candidate.page_count, pages, request.quality.all_pages)
                    if self._candidate_requires_write(
                        conn, candidate, request, health_by_artifact, excluded_pages
                    ):
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
                    excluded = (
                        candidate.illust_id,
                        page_spec.page_index,
                        output_key,
                    ) in excluded_pages
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
                    page_key = PageKey(
                        illust_id=candidate.illust_id,
                        page_index=int(page_spec.page_index),
                        output_key=output_key,
                        quality_key=quality_key,
                    )
                    page_snapshot = (
                        None
                        if snapshot is None or snapshot.degraded
                        else snapshot.page(page_key)
                    )
                    blocked_state: LocalState | None = None
                    degraded_hold = False
                    candidates: list = []
                    if (
                        existing is not None
                        and existing["state"] != "cancelled"
                        and (request.dedup_policy != "redownload" or receipt is not None)
                        and (not excluded or existing["state"] == "completed")
                    ):
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
                            artifact_id = str(artifact["artifact_id"])
                            if snapshot is None or snapshot.degraded:
                                # A transient observation failure is retried
                                # outside this transaction; a permanent one
                                # (no resolver, invalid snapshot) holds
                                # honestly instead of inventing a verdict.
                                degraded_reason = (
                                    "no_resolver" if snapshot is None else snapshot.reason
                                )
                                if degraded_reason in {
                                    "preflight_failed",
                                    "observation_failed",
                                    "batch_not_running",
                                }:
                                    raise RetryPreflight(degraded_reason)
                                degraded_hold = True
                            else:
                                if page_snapshot is None or page_snapshot.stale_for(
                                    int(existing["generation"]),
                                    str(existing["delivery_id"]),
                                ):
                                    # A completion the snapshot never covered:
                                    # re-preflight outside this transaction.
                                    raise RetryPreflight("generation_advanced")
                                health = page_snapshot.health_for(artifact_id)
                                if health is None:
                                    raise RetryPreflight("artifact_not_preflighted")
                                if self._health_is_current(
                                    health
                                ) and self._health_allows_reuse(health, artifact):
                                    self._link_batch_item_page(
                                        conn,
                                        batch_id,
                                        candidate.illust_id,
                                        page_spec.page_index,
                                        output_key,
                                        quality_json,
                                        delivery_id=str(existing["delivery_id"]),
                                        artifact_id=artifact_id,
                                    )
                                    delivery_ids.append(str(existing["delivery_id"]))
                                    continue
                                blocked_state = health.state
                        else:
                            self._link_batch_item_page(
                                conn,
                                batch_id,
                                candidate.illust_id,
                                page_spec.page_index,
                                output_key,
                                quality_json,
                                delivery_id=str(existing["delivery_id"]),
                                artifact_id=None,
                            )
                            delivery_ids.append(str(existing["delivery_id"]))
                            continue
                    if request.dedup_policy == "trusted":
                        candidates = self._find_reusable_artifact_candidates(
                            conn,
                            job_id=job_id,
                            illust_id=candidate.illust_id,
                            page_index=page_spec.page_index,
                            output_key=output_key,
                            original=request.quality.original,
                            validation_policy_version=request.quality.validation_policy_version,
                            limit=_MAX_HEALTH_CANDIDATES,
                        )
                        reusable = self._find_reusable_artifact(
                            conn,
                            job_id=job_id,
                            illust_id=candidate.illust_id,
                            page_index=page_spec.page_index,
                            output_key=output_key,
                            original=request.quality.original,
                            validation_policy_version=request.quality.validation_policy_version,
                            health_by_artifact=health_by_artifact,
                            candidates=candidates,
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
                        if candidates and blocked_state is None:
                            if page_snapshot is None:
                                degraded_reason = (
                                    "no_resolver" if snapshot is None else snapshot.reason
                                )
                                if (
                                    snapshot is not None
                                    and snapshot.degraded
                                    and degraded_reason
                                    not in {
                                        "preflight_failed",
                                        "observation_failed",
                                        "batch_not_running",
                                    }
                                ):
                                    # Nothing could be observed; hold honestly.
                                    degraded_hold = True
                                else:
                                    raise RetryPreflight("page_not_preflighted")
                            else:
                                covered = [
                                    str(item["artifact_id"])
                                    for item in candidates
                                    if page_snapshot.health_for(
                                        str(item["artifact_id"])
                                    )
                                    is not None
                                ]
                                if not covered:
                                    # A candidate appeared after the candidate
                                    # read, or it could not be observed at all.
                                    # Neither is evidence of a bad file.
                                    raise RetryPreflight("trusted_candidate_unobserved")
                                observed = None
                                for record in page_snapshot.observed:
                                    if record.state in {
                                        LocalState.HASH_MATCH,
                                        LocalState.QUICK_MATCH,
                                    }:
                                        if not self._health_is_current(record):
                                            # Matching bytes whose record is
                                            # stale are not a healthy verdict;
                                            # keep the precise diagnostic.
                                            observed = record.state
                                            break
                                        continue
                                    observed = record.state
                                    break
                                if observed is None:
                                    # Only usable matches were observed, yet no
                                    # reuse verdict was reached: hold honestly
                                    # instead of reporting "unknown".
                                    degraded_hold = True
                                else:
                                    blocked_state = observed
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
                    new_state = "pending"
                    new_error_code = None
                    if excluded:
                        new_state = "cancelled"
                        new_error_code = "intent_excluded"
                    elif degraded_hold:
                        new_state = "needs_verification"
                        new_error_code = "artifact_health_unavailable"
                    elif blocked_state is not None and blocked_state is not LocalState.MISSING:
                        new_state = "needs_verification"
                        new_error_code = (
                            "artifact_stale_health"
                            if blocked_state in {LocalState.HASH_MATCH, LocalState.QUICK_MATCH}
                            else f"artifact_{blocked_state.value}"
                        )
                    if new_state != "cancelled":
                        blocking = conn.execute(
                            "SELECT delivery_id, job_id, state FROM deliveries "
                            "WHERE output_key = ? AND state IN ("
                            "'pending','transferring','validating','prepared',"
                            "'needs_verification','suspended','retry_wait','recovery_needed'"
                            ") ORDER BY rowid LIMIT 1",
                            (output_key,),
                        ).fetchone()
                        if (
                            blocking is not None
                            and str(blocking["job_id"]) != job_id
                            and str(blocking["state"]) == "needs_verification"
                        ):
                            blocked_state_name = "needs_verification"
                            next_state = "failed"
                            conn.execute(
                                "UPDATE deliveries SET state = ?, "
                                "error_code = 'superseded_by_new_task', retry_at = NULL, "
                                "updated_at = ? WHERE delivery_id = ? AND state = ?",
                                (next_state, now, str(blocking["delivery_id"]),
                                 blocked_state_name),
                            )
                            self._refresh_items_for_delivery(
                                conn, str(blocking["delivery_id"]), now
                            )
                    try:
                        conn.execute(
                            "INSERT INTO deliveries("
                            "delivery_id, job_id, origin_batch_id, current_batch_id, "
                            "completed_batch_id, illust_id, page_index, output_key, "
                            "output_ref_json, quality_json, quality_key, payload_json, "
                            "source_identity, state, attempts, retry_at, error_code, "
                            "generation, checkpoint_json, prepared_json, created_at, updated_at"
                            ") VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, NULL, ?, ?)",
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
                                new_state,
                                new_error_code,
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
        *,
        health_by_artifact=None,
        enforce_completed_health: bool = False,
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
                    conn, str(source["batch_id"]), batch_id, illust_id,
                    health_by_artifact=health_by_artifact,
                    enforce_completed_health=enforce_completed_health,
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
                conn, str(source["batch_id"]), batch_id, illust_id,
                health_by_artifact=health_by_artifact,
                enforce_completed_health=enforce_completed_health,
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
            if delivery["state"] == "completed":
                # Idempotent re-claim: a retried acceptance that raced a real
                # completion must be able to report the same finished page
                # instead of inventing "unavailable".  Coordinators never
                # dispatch a completed row (list_dispatchable excludes it).
                return self._delivery_dict(delivery)
            if delivery["state"] not in _DISPATCHABLE_STATES:
                return None
            if delivery["state"] == "retry_wait" and not _retry_due(delivery["retry_at"]):
                return None
            projection = read_projection(conn)
            if projection.corrupt_events:
                # UA-10: an unreadable intent is not permission to download and
                # not a silent "nothing to do" either.  Block the batch so the
                # coordinator stops dispatching, and surface the corruption.
                now = _utcnow()
                conn.execute(
                    "UPDATE batches SET phase = 'pausing', active_slot = 1, "
                    "stop_target = 'blocked', stop_reason = 'intent_unreadable' "
                    "WHERE batch_id = ? AND phase IN "
                    "('running','cooling','settling','pausing')",
                    (batch_id,),
                )
                conn.execute(
                    "UPDATE jobs SET state = 'pausing', updated_at = ? WHERE job_id = ?",
                    (now, str(batch["job_id"])),
                )
                self._insert_event(
                    conn,
                    job_id=str(batch["job_id"]),
                    batch_id=batch_id,
                    kind="batch_block_requested",
                    payload={
                        "batch_id": batch_id,
                        "phase": "pausing",
                        "stop_target": "blocked",
                        "reason_code": "intent_unreadable",
                        "corrupt_event_seq": projection.corrupt_events[0],
                    },
                )
                # The block must commit, so report it as a result and let the
                # public wrapper raise outside the transaction.
                return {"corrupt_intent": projection.corrupt_events[0]}
            try:
                delivery_ref = path_ref_from_object(
                    _parse_json_object(
                        delivery["output_ref_json"], "delivery output reference"
                    )
                )
            except (ContractError, LibraryError) as exc:
                raise CorruptDataError(
                    "Delivery has an invalid output reference"
                ) from exc
            intent_key = IntentKey(
                illust_id=str(delivery["illust_id"]),
                page_index=int(delivery["page_index"]),
                output_key=str(delivery["output_key"]),
                root_revision=self._root_revision_for(delivery_ref),
            )
            if projection.is_excluded(intent_key):
                now = _utcnow()
                placeholders = ",".join("?" for _ in CANCELLABLE_STATES)
                conn.execute(
                    "UPDATE deliveries SET state = 'cancelled', "
                    "error_code = 'intent_excluded', retry_at = NULL, updated_at = ? "
                    f"WHERE delivery_id = ? AND state IN ({placeholders})",
                    (now, delivery_id, *CANCELLABLE_STATES),
                )
                self._refresh_items_for_delivery(conn, delivery_id, now)
                return None
            health_raw = payload.get("health")
            health_by_artifact = (
                self._health_map(health_raw) if health_raw is not None else None
            )
            now = _utcnow()
            self._admit_claimed_work(
                conn, batch, delivery, now,
                health_by_artifact=health_by_artifact,
                enforce_completed_health=health_raw is not None,
            )
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

    def _cancel_unpublished_delivery(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> None:
        """CAS a prepared, never-published delivery into cancelled.

        This is the only place that may move 'prepared' to 'cancelled'.  The
        executor has already verified that the final path does not exist; a
        crash between the final link and confirmation must go through
        confirm/recovery instead, so it is never treated as unpublished here.
        """
        delivery_id = str(payload["delivery_id"])
        batch_id = str(payload["batch_id"])
        error_code = str(payload["error_code"])
        expected_state = str(payload.get("expected_state", "prepared"))
        if expected_state not in {"prepared", "recovery_needed"}:
            raise InvalidStateError("Unsupported unpublished state")
        with self._transaction(conn):
            batch = self._get_batch_row(conn, batch_id)
            if batch is None:
                raise NotFoundError(f"Unknown batch: {batch_id}")
            delivery = self._get_delivery_row(conn, delivery_id)
            if delivery is None:
                raise NotFoundError(f"Unknown delivery: {delivery_id}")
            if delivery["job_id"] != batch["job_id"]:
                raise InvalidStateError("Batch and delivery belong to different jobs")
            if delivery["current_batch_id"] != batch_id:
                raise InvalidStateError("Only the owning batch can cancel the delivery")
            if delivery["state"] == "cancelled":
                return None
            if delivery["state"] != expected_state:
                raise InvalidStateError(
                    "Only the expected unpublished state can be cancelled"
                )
            expected_sha = payload.get("expected_prepared_sha256")
            expected_generation = payload.get("expected_generation")
            if expected_generation is not None:
                if int(delivery["generation"]) != int(expected_generation):
                    raise InvalidStateError(
                        "Delivery generation changed before cancellation"
                    )
            if expected_sha is not None:
                # The caller verified one exact staged file.  A row whose
                # prepared evidence moved now describes a different file, so
                # cancelling it would authorize deleting the replacement.
                try:
                    prepared_now = _parse_json_object(
                        delivery["prepared_json"], "prepared evidence"
                    )
                except (CorruptDataError, KeyError, TypeError) as exc:
                    raise InvalidStateError(
                        "Prepared evidence is unreadable"
                    ) from exc
                if str(prepared_now.get("sha256")) != str(expected_sha):
                    raise InvalidStateError(
                        "Prepared evidence changed before cancellation"
                    )
            now = _utcnow()
            updated = conn.execute(
                "UPDATE deliveries SET state = 'cancelled', error_code = ?, "
                "retry_at = NULL, updated_at = ? "
                "WHERE delivery_id = ? AND state = ?",
                (error_code, now, delivery_id, expected_state),
            ).rowcount
            if updated != 1:
                raise InvalidStateError("Delivery state changed before cancellation")
            self._insert_event(
                conn,
                job_id=str(batch["job_id"]),
                batch_id=batch_id,
                kind="delivery_cancelled",
                payload={
                    "delivery_id": delivery_id,
                    "batch_id": batch_id,
                    "error_code": error_code,
                    "reason": "unpublished_intent",
                    "expected_state": expected_state,
                    **(
                        {"expected_prepared_sha256": str(expected_sha)}
                        if expected_sha is not None
                        else {}
                    ),
                    **(
                        {"expected_generation": int(expected_generation)}
                        if expected_generation is not None
                        else {}
                    ),
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
                    "stop_target = 'blocked', "
                    "stop_reason = COALESCE(stop_reason, 'interrupted'), "
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
