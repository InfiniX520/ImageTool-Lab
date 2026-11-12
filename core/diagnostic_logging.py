#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bounded diagnostic logging for the new runtime.

This module owns the *diagnostic* path only: business facts stay in the ledger
and task events.  It provides a non-blocking, bounded producer API, one
non-daemon writer thread, JSONL volume files with rotation/retention, a bounded
UI event ring, credential redaction, and a streaming text export from a
consistent snapshot.

The producer never touches the disk.  Every queue is bounded by records and by
UTF-8 bytes; on overload warning/error records win over debug/info records, and
the exact number and (bounded) sequence ranges of skipped records stay visible.
Closing uses an independent stop flag, so a full queue can never block shutdown.

Credential handling is a deliberately bounded heuristic: only the selected
field set is accepted, and a message matching one of the fixed credential-like
patterns is replaced as a whole with one digest.  It is not a detector for
arbitrary unknown secret formats and does not claim to be one.
"""

from __future__ import annotations

import copy
import json
import os
import re
import stat as stat_module
import tempfile
import threading
import time
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from core.task_contracts import PathRef

LEVELS = ("debug", "info", "success", "warning", "error")
LEVEL_PREFIXES = {
    "debug": "[D]",
    "info": "[*]",
    "success": "[+]",
    "warning": "[!]",
    "error": "[-]",
}
_LEGACY_PREFIXES = {
    "[+]": "success",
    "[-]": "error",
    "[!]": "warning",
    "[*]": "info",
    "[D]": "debug",
}
_LEVEL_PRIORITY = {"debug": 0, "info": 1, "success": 1, "warning": 2, "error": 3}

_CODE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SESSION_RE = re.compile(r"^[0-9]{8}T[0-9]{6}_[0-9]{6}Z-[0-9a-f]{32}$")
_VOLUME_RE = re.compile(r"^diagnostic_(.+)_v([0-9]{4})\.jsonl$")

_MANIFEST_NAME = "diagnostic_manifest_v2.json"
_MANIFEST_FORMAT = "jpgpng-diagnostic-v2"
_MANIFEST_VERSION = 2
_MANIFEST_SESSION_CAP = 64
_MANIFEST_VOLUME_CAP = 16
_MANIFEST_BYTE_CAP = 2 * 1024 * 1024
_MANIFEST_DELETION_CAP = 1024
_MANIFEST_GUARD = threading.RLock()
_MAINTENANCE_DELETE_CAP = 64

_EXPORT_SUFFIXES = (".txt", ".log")
_EXPORT_CHUNK = 64 * 1024
_EXPORT_LINE_CAP = 256 * 1024
_EXPORT_RECORD_CAP = 200_000

_WRITE_BATCH_RECORDS = 64
_WRITE_BATCH_BYTES = 256 * 1024
_MAX_DROPPED_RANGES = 8
_MAX_HEALTH_CODES = 16
_REDACTION_TEXT = "[redacted: credential-like content]"

# Only these deliberately selected patterns are treated as credential-like.
# The whole text is replaced with one fixed digest; no attempt is made to keep
# "safe" fragments of a line that may carry a secret.
_CREDENTIAL_RE = re.compile(
    r"(?:"
    r"cookie|set-cookie|authorization|bearer|"
    r"access[_-]?token|refresh[_-]?token|token|"
    r"phpssid|phpsessid|session[_-]?id|sessionid|"
    r"api[_-]?key|apikey|x[_-]?api[_-]?key|"
    r"password|passwd|secret|signature|credential|"
    r"(?:^|[?&;])(?:sid|sig|key|token|auth|expires|password|credential)="
    r")",
    re.IGNORECASE,
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class DiagnosticLogError(RuntimeError):
    """A diagnostic log operation could not be completed."""


@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    """One immutable diagnostic record after field selection and redaction."""

    session_id: str
    sequence: int
    timestamp: str
    level: str
    code: str
    message: str
    job_id: str | None = None
    batch_id: str | None = None
    work_id: str | None = None
    page_index: int | None = None


@dataclass(slots=True)
class _Pending:
    event: DiagnosticEvent
    repeat: int = 1


class _SequenceGaps:
    """Bounded, exact record of skipped sequence numbers."""

    __slots__ = ("ranges", "count", "truncated", "_limit")

    def __init__(self, limit: int = _MAX_DROPPED_RANGES) -> None:
        self.ranges: list[list[int]] = []
        self.count = 0
        self.truncated = False
        self._limit = limit

    def add(self, sequence: int) -> None:
        self.count += 1
        if self.ranges and sequence == self.ranges[-1][1] + 1:
            self.ranges[-1][1] = sequence
            return
        if len(self.ranges) < self._limit:
            self.ranges.append([sequence, sequence])
        else:
            self.truncated = True

    def add_range(self, first: int | None, last: int | None) -> None:
        if not first or not last or last < first:
            return
        if self.ranges and first == self.ranges[-1][1] + 1:
            self.ranges[-1][1] = last
        elif len(self.ranges) < self._limit:
            self.ranges.append([first, last])
        else:
            self.truncated = True

    def as_tuple(self) -> tuple[tuple[int, int], ...]:
        return tuple((first, last) for first, last in self.ranges)


def _validate_limit(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _validate_seconds(value: object, name: str, maximum: float) -> float:
    if type(value) not in (int, float) or not 0 < float(value) <= maximum:
        raise ValueError(f"{name} must be a positive number")
    return float(value)


def _redact_message(text: str) -> str:
    probe = unicodedata.normalize("NFKC", text)
    if _CREDENTIAL_RE.search(probe):
        return _REDACTION_TEXT
    return text


def _sanitize_message(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_RE.sub(" ", text)


def _truncate_utf8(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    marker = "…[truncated]"
    budget = limit - len(marker.encode("utf-8"))
    if budget < 1:
        return marker.encode("utf-8")[:limit].decode("utf-8", "ignore"), True
    cut = encoded[:budget]
    while cut:
        try:
            kept = cut.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            cut = cut[: exc.start]
    else:
        kept = ""
    return kept + marker, True


def _event_bytes(event: DiagnosticEvent) -> int:
    return len(event.message.encode("utf-8"))


def _record_bytes(pending: _Pending) -> int:
    return len(pending.event.message.encode("utf-8"))


def _single_line(text: str) -> str:
    return text.replace("\r", " ").replace("\n", "\\n")


def _replace_file(source: Path, target: Path) -> None:
    """Atomic publish hook (kept module level so failures are injectable)."""
    os.replace(source, target)


def _open_volume(path: Path):
    """Open one append-only volume (module level so disk faults are injectable)."""
    return open(path, "ab", buffering=64 * 1024)


def _iter_volume_lines(
    path: Path,
    capped_bytes: int,
    stop_event: object | None = None,
    *,
    chunk_size: int = _EXPORT_CHUNK,
):
    """Yield raw JSONL lines from at most ``capped_bytes`` of one volume."""
    remaining = max(0, int(capped_bytes))
    if remaining == 0:
        return
    with open(path, "rb") as stream:
        buffer = bytearray()
        oversize = False
        while remaining > 0:
            if stop_event is not None and stop_event.is_set():
                raise DiagnosticLogError("export cancelled")
            chunk = stream.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            buffer.extend(chunk)
            while True:
                index = buffer.find(b"\n")
                if index < 0:
                    break
                line = bytes(buffer[:index])
                del buffer[: index + 1]
                if oversize:
                    oversize = False
                    continue
                yield line
            if len(buffer) > _EXPORT_LINE_CAP:
                # A single line above the cap is dropped rather than buffered
                # without bound; the export notes the skipped line.
                oversize = True
                buffer.clear()
        if buffer and not oversize:
            yield bytes(buffer)


class DiagnosticLog:
    """One bounded diagnostic session: producer queue, UI ring and writer."""

    def __init__(
        self,
        log_dir: str | Path,
        *,
        queue_records: int = 1024,
        queue_bytes: int = 4 * 1024 * 1024,
        ui_records: int = 3000,
        ui_bytes: int = 2 * 1024 * 1024,
        max_message_bytes: int = 16 * 1024,
        volume_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        flush_interval: float = 1.0,
        directory_budget_bytes: int = 256 * 1024 * 1024,
        max_age_days: float = 30,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._queue_limit_records = _validate_limit(
            queue_records, "queue_records", 1, 1_000_000
        )
        self._queue_limit_bytes = _validate_limit(
            queue_bytes, "queue_bytes", 1024, 1 << 31
        )
        self._ui_limit_records = _validate_limit(ui_records, "ui_records", 1, 1_000_000)
        self._ui_limit_bytes = _validate_limit(ui_bytes, "ui_bytes", 1024, 1 << 31)
        self._max_message_bytes = _validate_limit(
            max_message_bytes, "max_message_bytes", 64, 16 * 1024
        )
        self._volume_bytes = _validate_limit(
            volume_bytes, "volume_bytes", 1024, 1 << 31
        )
        self._backup_count = _validate_limit(backup_count, "backup_count", 0, 100)
        self._flush_interval = _validate_seconds(flush_interval, "flush_interval", 3600.0)
        self._budget_bytes = _validate_limit(
            directory_budget_bytes, "directory_budget_bytes", 1, 1 << 40
        )
        self._max_age = _validate_seconds(max_age_days, "max_age_days", 3650.0)
        if not callable(clock):
            raise ValueError("clock must be callable")
        if wall_clock is not None and not callable(wall_clock):
            raise ValueError("wall_clock must be callable or None")
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))

        self._dir = Path(log_dir)
        self._dir_ok = True
        self._manifest_error = False
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._dir_ok = False

        stamp = self._wall_now()
        self._session_id = (
            stamp.strftime("%Y%m%dT%H%M%S")
            + f"_{stamp.microsecond:06d}Z-"
            + uuid.uuid4().hex
        )
        self._manifest: dict[str, Any] = {
            "format": _MANIFEST_FORMAT,
            "version": _MANIFEST_VERSION,
            "sessions": {},
        }
        self._spool: list[dict[str, Any]] = []
        self._deleted_volumes: set[str] = set()
        self._deleted_sessions: set[str] = set()
        self._volume_number = 0
        self._stream = None
        self._current_bytes = 0
        self._flushed_bytes = 0
        self._written_sequence = 0
        self._flushed_sequence = 0
        self._disk_available = self._dir_ok
        self._next_reopen = 0.0
        self._write_errors = 0
        self._rotation_blocked = 0
        self._truncated_messages = 0
        self._post_close_dropped = 0
        self._volumes_removed = 0
        self._rotated_ranges = _SequenceGaps()
        self._file_gaps = _SequenceGaps()
        self._ui_gaps = _SequenceGaps()
        self._merged = 0
        self._maintenance_removed = 0
        self._maintenance_limited = False
        self._export_protected: dict[str, int] = {}
        self._export_count = 0

        self._health_codes: list[str] = []
        self._cond = threading.Condition()
        # Only background export/retention owners take this lock. File I/O
        # must never retain the producer/UI condition while waiting on disk.
        self._storage_guard = threading.RLock()
        self._sequence = 0
        self._queue: deque[_Pending] = deque()
        self._queue_bytes = 0
        self._ui: deque[DiagnosticEvent] = deque()
        self._ui_bytes = 0
        self._writing = False
        self._closing = False
        self._closed = False
        self._finalized = False
        self._stop = threading.Event()
        self._writer_done = threading.Event()
        self._writer: threading.Thread | None = None

        if self._dir_ok:
            try:
                self._manifest = self._load_manifest()
            except Exception:  # pragma: no cover - defensive
                self._manifest_error = True
                self._mark_health("manifest_invalid")
            self._manifest["sessions"][self._session_id] = {
                "closed": False,
                "closed_at": None,
                "updated": self._wall_now().isoformat(timespec="seconds"),
                "volumes": [],
            }
            try:
                self._open_next_volume()
            except OSError:
                self._disk_available = False
                self._mark_health("open_failed")
            self._maintain_storage()
            self._write_manifest()
        else:
            self._mark_health("open_failed")

        self._writer = threading.Thread(
            target=self._writer_loop,
            name=f"jpgpng-diagnostic-{self._session_id[-12:]}",
            daemon=False,
        )
        self._writer.start()

    # ------------------------------------------------------------------
    # Public producer API
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def sequence(self) -> int:
        with self._cond:
            return self._sequence

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    def emit(
        self,
        message: str,
        *,
        level: str = "info",
        code: str = "legacy",
        job_id: str | None = None,
        batch_id: str | None = None,
        work_id: str | None = None,
        page_index: int | None = None,
    ) -> int:
        """Record one diagnostic event and return its session sequence."""
        if not isinstance(message, str):
            raise ValueError("message must be a string")
        if not isinstance(level, str) or level not in LEVELS:
            raise ValueError("level must be one of " + "/".join(LEVELS))
        if not isinstance(code, str) or _CODE_RE.fullmatch(code) is None:
            raise ValueError("code must be a short lowercase safe identifier")
        for name, value in (
            ("job_id", job_id),
            ("batch_id", batch_id),
            ("work_id", work_id),
        ):
            if value is None:
                continue
            if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
                raise ValueError(f"{name} must be a safe identifier")
        if page_index is not None and (
            type(page_index) is not int or not 0 <= page_index <= 9999
        ):
            raise ValueError("page_index must be an integer in [0, 9999]")

        text = _sanitize_message(message)
        text = _redact_message(text)
        text, truncated = _truncate_utf8(text, self._max_message_bytes)
        timestamp = self._wall_now().astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        )
        with self._cond:
            if self._closed or self._closing:
                self._post_close_dropped += 1
                return self._sequence
            self._sequence += 1
            sequence = self._sequence
            if truncated:
                self._truncated_messages += 1
            event = DiagnosticEvent(
                session_id=self._session_id,
                sequence=sequence,
                timestamp=timestamp,
                level=level,
                code=code,
                message=text,
                job_id=job_id,
                batch_id=batch_id,
                work_id=work_id,
                page_index=page_index,
            )
            self._append_ui_locked(event)
            self._enqueue_disk_locked(event)
            self._cond.notify_all()
            return sequence

    def put(self, message: object, block: bool = True, timeout: float | None = None) -> None:
        """Queue-callback compatibility for the legacy ``log_message`` helper.

        ``block``/``timeout`` are accepted for signature compatibility but never
        used: the producer path is always non-blocking and bounded.
        """
        text = message if isinstance(message, str) else str(message)
        level = "info"
        stripped = text
        for prefix, mapped in _LEGACY_PREFIXES.items():
            if text.startswith(prefix):
                level = mapped
                stripped = text[len(prefix):]
                if stripped.startswith(" "):
                    stripped = stripped[1:]
                break
        self.emit(stripped, level=level, code="legacy")

    def events_after(
        self, sequence: int = 0, *, limit: int = 200
    ) -> tuple[DiagnosticEvent, ...]:
        """Return at most ``limit`` retained UI events with a larger sequence."""
        if type(sequence) is not int or sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 2000:
            raise ValueError("limit must be an integer in [1, 2000]")
        with self._cond:
            if not self._ui:
                return ()
            found: list[DiagnosticEvent] = []
            for event in self._ui:
                if event.sequence > sequence:
                    found.append(event)
                    if len(found) >= limit:
                        break
            return tuple(found)

    def flush(self, timeout: float = 5) -> bool:
        """Wait until the writer has written and flushed every queued record."""
        if type(timeout) not in (int, float) or timeout < 0:
            raise ValueError("timeout must be a non-negative number")
        if threading.current_thread() is self._writer:
            raise RuntimeError("flush() cannot run on the diagnostic writer thread")
        deadline = time.monotonic() + float(timeout)
        with self._cond:
            if self._closed:
                return True
            if not self._writer.is_alive() and self._writer_done.is_set():
                return True
            self._cond.notify_all()
            while self._queue or self._writing:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.1))
            return True

    def close(self, timeout: float = 20) -> bool:
        """Ask the single owner thread to drain, finalize and stop.

        All finalization I/O (volume close, manifest, retention) happens on the
        existing non-daemon writer thread, never on the caller thread.  The
        caller only waits up to ``timeout``.  ``False`` means the owner has not
        finished yet: the log then honestly stays ``closed=False`` and a later
        call may complete the same close; ``closed`` is set only after the
        owner thread has really finished its finalization attempt.
        """
        if type(timeout) not in (int, float) or timeout < 0:
            raise ValueError("timeout must be a non-negative number")
        if threading.current_thread() is self._writer:
            raise RuntimeError("close() cannot run on the diagnostic writer thread")
        with self._cond:
            if self._closed:
                return True
            self._closing = True
            self._stop.set()
            self._cond.notify_all()
        deadline = time.monotonic() + float(timeout)
        writer = self._writer
        if writer is not None:
            remaining = max(0.0, deadline - time.monotonic())
            if writer.is_alive():
                writer.join(remaining)
            if writer.is_alive():
                self._mark_health("close_timeout")
                return False
        with self._cond:
            if self._closed:
                return True
            self._closed = True
        return True

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return a bounded, side-effect free status dictionary."""
        with self._cond:
            volumes = tuple(
                {
                    "name": entry["name"],
                    "bytes": int(entry["bytes"]),
                    "first_sequence": entry.get("first_sequence"),
                    "last_sequence": entry.get("last_sequence"),
                    "closed": bool(entry.get("closed")),
                }
                for entry in self._spool[-_MANIFEST_VOLUME_CAP:]
            )
            directory_bytes = 0
            sessions = self._manifest.get("sessions", {})
            for entry in sessions.values():
                for volume in entry.get("volumes", []):
                    directory_bytes += int(volume.get("bytes") or 0)
            return {
                "session_id": self._session_id,
                "sequence": self._sequence,
                "queue_records": len(self._queue),
                "queue_bytes": self._queue_bytes,
                "queue_record_limit": self._queue_limit_records,
                "queue_byte_limit": self._queue_limit_bytes,
                "ui_records": len(self._ui),
                "ui_bytes": self._ui_bytes,
                "ui_record_limit": self._ui_limit_records,
                "ui_byte_limit": self._ui_limit_bytes,
                "file_dropped": self._file_gaps.count,
                "file_dropped_ranges": self._file_gaps.as_tuple(),
                "ui_dropped": self._ui_gaps.count,
                "ui_dropped_ranges": self._ui_gaps.as_tuple(),
                "dropped_ranges_truncated": bool(
                    self._file_gaps.truncated or self._ui_gaps.truncated
                ),
                "merged": self._merged,
                "truncated_messages": self._truncated_messages,
                "write_errors": self._write_errors,
                "post_close_dropped": self._post_close_dropped,
                "rotated_out_volumes": self._volumes_removed,
                "rotated_out_ranges": self._rotated_ranges.as_tuple(),
                "rotation_blocked": self._rotation_blocked,
                "maintenance_removed": self._maintenance_removed,
                "maintenance_limited": self._maintenance_limited,
                "retained_volumes": volumes,
                "retained_volume_count": len(self._spool),
                "directory_bytes": directory_bytes,
                "directory_budget_bytes": self._budget_bytes,
                "directory_sessions": len(sessions),
                "closed": self._closed,
                "closing": self._closing,
                "finalized": self._finalized,
                "writer_alive": bool(self._writer and self._writer.is_alive()),
                "pending_writes": len(self._queue) + (1 if self._writing else 0),
                "health_code": self._health_code_locked(),
                "health_codes": tuple(self._health_codes),
                "export_protected": len(self._export_protected),
            }

    # ------------------------------------------------------------------
    # Internal producer helpers
    # ------------------------------------------------------------------

    def _append_ui_locked(self, event: DiagnosticEvent) -> None:
        cost = _event_bytes(event)
        if cost > self._ui_limit_bytes:
            self._ui_gaps.add(event.sequence)
            self._mark_health("degraded_ui")
            return
        while self._ui and (
            len(self._ui) >= self._ui_limit_records
            or self._ui_bytes + cost > self._ui_limit_bytes
        ):
            dropped = self._ui.popleft()
            self._ui_bytes -= _event_bytes(dropped)
            self._ui_gaps.add(dropped.sequence)
            self._mark_health("degraded_ui")
        self._ui.append(event)
        self._ui_bytes += cost

    def _enqueue_disk_locked(self, event: DiagnosticEvent) -> None:
        cost = _event_bytes(event)
        if (
            len(self._queue) < self._queue_limit_records
            and self._queue_bytes + cost <= self._queue_limit_bytes
        ):
            self._queue.append(_Pending(event))
            self._queue_bytes += cost
            return
        priority = _LEVEL_PRIORITY[event.level]
        if priority <= 1:
            # Low priority: merge with the newest similar pending record when
            # possible, otherwise drop.  Never evict a warning/error for it.
            for pending in reversed(self._queue):
                if (
                    pending.event.level == event.level
                    and pending.event.code == event.code
                ):
                    pending.repeat += 1
                    self._merged += 1
                    return
            self._file_gaps.add(event.sequence)
            self._mark_health("queue_overflow")
            return
        # Warning/error: make room by evicting the lowest-priority, oldest item.
        victim_index = None
        victim_priority = priority
        for index, pending in enumerate(self._queue):
            pending_priority = _LEVEL_PRIORITY[pending.event.level]
            if pending_priority < victim_priority:
                victim_index = index
                victim_priority = pending_priority
        if victim_index is None:
            if event.level == "error":
                # Keep the newest errors under a bounded queue.
                victim_index = 0
            else:
                # A warning must never evict an error; it may replace an older
                # warning, otherwise it is dropped and counted.
                for index, pending in enumerate(self._queue):
                    if pending.event.level == "warning":
                        victim_index = index
                        break
        if victim_index is None:
            self._file_gaps.add(event.sequence)
            self._mark_health("queue_overflow")
            return
        victim = self._queue[victim_index]
        del self._queue[victim_index]
        self._queue_bytes -= _record_bytes(victim)
        self._file_gaps.add(victim.event.sequence)
        self._mark_health("queue_overflow")
        self._queue.append(_Pending(event))
        self._queue_bytes += cost

    # ------------------------------------------------------------------
    # Writer thread
    # ------------------------------------------------------------------

    def _writer_loop(self) -> None:
        try:
            while True:
                batch = self._take_batch()
                if not batch:
                    if self._stop.is_set():
                        break
                    continue
                try:
                    self._write_batch(batch)
                except OSError as exc:
                    self._handle_disk_failure(batch, exc)
                finally:
                    with self._cond:
                        self._writing = False
                        self._cond.notify_all()
        finally:
            self._stop.set()
            try:
                self._finalize_owner()
            except BaseException:  # pragma: no cover - defensive
                self._mark_health("finalize_error")
            finally:
                with self._cond:
                    self._finalized = True
                    # Finalization really happened on the owner thread; the
                    # close caller may have timed out earlier, in which case a
                    # retry simply observes the completed state.
                    self._closed = True
                    self._writer_done.set()
                    self._cond.notify_all()

    def _finalize_owner(self) -> None:
        """Finalization I/O executed by the one non-daemon owner thread."""
        self._close_stream()
        if not self._dir_ok:
            return
        self._finalize_session()
        self._maintain_storage()
        self._write_manifest()

    def _take_batch(self) -> list[_Pending]:
        with self._cond:
            if not self._queue and not self._stop.is_set():
                self._cond.wait(self._flush_interval)
            if not self._queue:
                return []
            batch: list[_Pending] = []
            batch_bytes = 0
            while self._queue and len(batch) < _WRITE_BATCH_RECORDS:
                pending = self._queue[0]
                cost = _record_bytes(pending)
                if batch and batch_bytes + cost > _WRITE_BATCH_BYTES:
                    break
                self._queue.popleft()
                self._queue_bytes -= cost
                batch.append(pending)
                batch_bytes += cost
            self._writing = True
            return batch

    def _write_batch(self, batch: list[_Pending]) -> None:
        if not self._disk_available:
            now = self._clock()
            if now < self._next_reopen:
                self._drop_batch(batch)
                return
            try:
                self._open_next_volume()
            except OSError as exc:
                self._next_reopen = now + max(0.5, self._flush_interval)
                raise exc
        for pending in batch:
            line = self._record_line(pending)
            if self._stream is None:
                raise OSError("diagnostic volume is not open")
            if self._current_bytes and self._current_bytes + len(line) > self._volume_bytes:
                self._rotate()
            assert self._stream is not None
            self._stream.write(line)
            self._current_bytes += len(line)
            with self._cond:
                self._written_sequence = max(
                    self._written_sequence, pending.event.sequence
                )
                entry = self._spool[-1] if self._spool else None
                if entry is None:
                    raise OSError("diagnostic volume manifest is missing")
                entry["bytes"] = self._current_bytes
                if entry["first_sequence"] is None:
                    entry["first_sequence"] = pending.event.sequence
                entry["last_sequence"] = pending.event.sequence
        self._flush_stream()

    def _flush_stream(self) -> None:
        if self._stream is None:
            return
        self._stream.flush()
        with self._cond:
            self._flushed_bytes = self._current_bytes
            self._flushed_sequence = self._written_sequence

    def _handle_disk_failure(self, batch: list[_Pending], exc: OSError) -> None:
        self._close_stream()
        with self._cond:
            self._disk_available = False
            self._next_reopen = self._clock() + max(0.5, self._flush_interval)
            self._write_errors += 1
            self._mark_health("write_error")
            if self._spool:
                self._spool[-1]["closed"] = True
                self._spool[-1]["bytes"] = self._current_bytes
                self._sync_session_manifest()
            dropped = list(batch) + list(self._queue)
            self._queue.clear()
            self._queue_bytes = 0
            for pending in dropped:
                self._file_gaps.add(pending.event.sequence)
            self._cond.notify_all()

    def _drop_batch(self, batch: list[_Pending]) -> None:
        with self._cond:
            for pending in batch:
                self._file_gaps.add(pending.event.sequence)

    def _record_line(self, pending: _Pending) -> bytes:
        event = pending.event
        payload: dict[str, Any] = {
            "v": _MANIFEST_VERSION,
            "session_id": event.session_id,
            "sequence": event.sequence,
            "timestamp": event.timestamp,
            "level": event.level,
            "code": event.code,
            "message": event.message,
        }
        if event.job_id is not None:
            payload["job_id"] = event.job_id
        if event.batch_id is not None:
            payload["batch_id"] = event.batch_id
        if event.work_id is not None:
            payload["work_id"] = event.work_id
        if event.page_index is not None:
            payload["page_index"] = event.page_index
        if pending.repeat > 1:
            payload["repeat"] = pending.repeat
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return encoded.encode("utf-8") + b"\n"

    # ------------------------------------------------------------------
    # Volumes, manifest and retention
    # ------------------------------------------------------------------

    def _volume_name(self, session_id: str, number: int) -> str:
        return f"diagnostic_{session_id}_v{number:04d}.jsonl"

    def _open_next_volume(self) -> None:
        if not self._dir_ok:
            raise OSError("diagnostic directory is unavailable")
        if len(self._spool) >= _MANIFEST_VOLUME_CAP:
            self._trim_own_volumes()
            if len(self._spool) >= _MANIFEST_VOLUME_CAP:
                with self._cond:
                    self._rotation_blocked += 1
                self._mark_health("rotation_limited")
                raise OSError("diagnostic retained-volume limit reached")
        self._volume_number += 1
        name = self._volume_name(self._session_id, self._volume_number)
        path = self._dir / name
        if path.is_symlink():
            raise OSError("diagnostic volume path is a link")
        stream = _open_volume(path)
        self._stream = stream
        self._current_bytes = 0
        self._flushed_bytes = 0
        with self._cond:
            self._spool.append(
                {
                    "name": name,
                    "number": self._volume_number,
                    "bytes": 0,
                    "first_sequence": None,
                    "last_sequence": None,
                    "closed": False,
                }
            )
        self._disk_available = True
        self._sync_session_manifest()

    def _rotate(self) -> None:
        if self._stream is not None:
            try:
                self._stream.flush()
                self._stream.close()
            except OSError:
                self._mark_health("write_error")
            self._stream = None
        with self._cond:
            if self._spool:
                self._spool[-1]["closed"] = True
                self._spool[-1]["bytes"] = self._current_bytes
        self._trim_own_volumes()
        self._sync_session_manifest()
        self._open_next_volume()
        self._write_manifest()

    def _trim_own_volumes(self) -> None:
        with self._storage_guard:
            with self._cond:
                closed = [entry for entry in self._spool if entry.get("closed")]
            while len(closed) > self._backup_count:
                victim = closed.pop(0)
                with self._cond:
                    if victim["name"] in self._export_protected:
                        self._rotation_blocked += 1
                        continue
                if self._delete_volume(victim, self._session_id):
                    with self._cond:
                        self._volumes_removed += 1
                        self._rotated_ranges.add_range(
                            victim.get("first_sequence"), victim.get("last_sequence")
                        )
                        self._spool.remove(victim)

    def _sync_session_manifest(self) -> None:
        with self._cond:
            sessions = self._manifest.setdefault("sessions", {})
            entry = sessions.get(self._session_id)
            if entry is None:
                entry = {
                    "closed": False,
                    "closed_at": None,
                    "updated": self._wall_now().isoformat(timespec="seconds"),
                    "volumes": [],
                }
                sessions[self._session_id] = entry
            entry["volumes"] = [
                {
                    "name": volume["name"],
                    "number": volume["number"],
                    "bytes": int(volume["bytes"]),
                    "first_sequence": volume.get("first_sequence"),
                    "last_sequence": volume.get("last_sequence"),
                    "closed": bool(volume.get("closed")),
                }
                for volume in self._spool
            ]
            entry["updated"] = self._wall_now().isoformat(timespec="seconds")

    def _manifest_path(self) -> Path:
        return self._dir / _MANIFEST_NAME

    def _load_manifest(self) -> dict[str, Any]:
        path = self._manifest_path()
        try:
            with path.open("rb") as stream:
                raw = stream.read(_MANIFEST_BYTE_CAP + 1)
        except FileNotFoundError:
            return {"format": _MANIFEST_FORMAT, "version": _MANIFEST_VERSION, "sessions": {}}
        except OSError:
            self._mark_health("manifest_invalid")
            return {"format": _MANIFEST_FORMAT, "version": _MANIFEST_VERSION, "sessions": {}}
        try:
            if len(raw) > _MANIFEST_BYTE_CAP:
                self._maintenance_limited = True
                raise ValueError("diagnostic manifest size limit exceeded")
            data = json.loads(raw)
        except (ValueError, TypeError):
            self._manifest_error = True
            self._mark_health("manifest_invalid")
            return {"format": _MANIFEST_FORMAT, "version": _MANIFEST_VERSION, "sessions": {}}
        if (
            not isinstance(data, dict)
            or data.get("format") != _MANIFEST_FORMAT
            or data.get("version") != _MANIFEST_VERSION
        ):
            self._manifest_error = True
            self._mark_health("manifest_invalid")
            return {"format": _MANIFEST_FORMAT, "version": _MANIFEST_VERSION, "sessions": {}}
        sessions: dict[str, Any] = {}
        raw_sessions = data.get("sessions")
        if isinstance(raw_sessions, dict):
            for session_id, entry in raw_sessions.items():
                if len(sessions) >= _MANIFEST_SESSION_CAP:
                    self._maintenance_limited = True
                    break
                if not isinstance(session_id, str) or _SESSION_RE.fullmatch(session_id) is None:
                    continue
                if not isinstance(entry, dict):
                    continue
                volumes = []
                for item in list(entry.get("volumes") or [])[:_MANIFEST_VOLUME_CAP]:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name")
                    number = item.get("number")
                    if (
                        not isinstance(name, str)
                        or type(number) is not int
                        or name != self._volume_name(session_id, number)
                    ):
                        continue
                    volumes.append(
                        {
                            "name": name,
                            "number": number,
                            "bytes": max(0, int(item.get("bytes") or 0)),
                            "first_sequence": item.get("first_sequence"),
                            "last_sequence": item.get("last_sequence"),
                            "closed": bool(item.get("closed")),
                        }
                    )
                sessions[session_id] = {
                    "closed": bool(entry.get("closed")),
                    "closed_at": entry.get("closed_at")
                    if isinstance(entry.get("closed_at"), str)
                    else None,
                    "updated": entry.get("updated")
                    if isinstance(entry.get("updated"), str)
                    else None,
                    "volumes": volumes,
                }
        return {"format": _MANIFEST_FORMAT, "version": _MANIFEST_VERSION, "sessions": sessions}

    def _write_manifest(self) -> None:
        if not self._dir_ok:
            return
        with self._cond:
            our_entry = copy.deepcopy(
                self._manifest.get("sessions", {}).get(self._session_id)
            )
            deleted_volumes = set(self._deleted_volumes)
            deleted_sessions = set(self._deleted_sessions)
            now = self._wall_now().isoformat(timespec="seconds")
        with _MANIFEST_GUARD:
            sessions = self._read_manifest_sessions()
            for session_id in list(sessions):
                if session_id in deleted_sessions:
                    del sessions[session_id]
                    continue
                entry = sessions[session_id]
                entry["volumes"] = [
                    volume
                    for volume in entry.get("volumes", [])
                    if volume.get("name") not in deleted_volumes
                ]
                if not entry["volumes"] and not entry.get("closed"):
                    del sessions[session_id]
            if our_entry is not None:
                sessions[self._session_id] = our_entry
            ordered = list(sessions.items())
            ordered.sort(
                key=lambda item: (
                    item[0] != self._session_id,
                    item[1].get("updated") or "",
                ),
                reverse=True,
            )
            bounded: dict[str, Any] = {}
            for session_id, entry in ordered[:_MANIFEST_SESSION_CAP]:
                bounded[session_id] = entry
            if len(ordered) > _MANIFEST_SESSION_CAP:
                self._maintenance_limited = True
            payload = {
                "format": _MANIFEST_FORMAT,
                "version": _MANIFEST_VERSION,
                "updated_at": now,
                "sessions": bounded,
            }
            text = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            temp_path = None
            try:
                descriptor, name = tempfile.mkstemp(
                    prefix=".diagnostic-manifest-", suffix=".tmp", dir=str(self._dir)
                )
                temp_path = Path(name)
                with os.fdopen(
                    descriptor, "w", encoding="utf-8", newline="\n"
                ) as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                _replace_file(temp_path, self._manifest_path())
                temp_path = None
            except OSError:
                self._manifest_error = True
                self._mark_health("manifest_error")
                return
            finally:
                if temp_path is not None:
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
            with self._cond:
                self._manifest["sessions"] = sessions
                self._deleted_volumes.clear()
                self._deleted_sessions.clear()

    def _read_manifest_sessions(self) -> dict[str, Any]:
        try:
            data = self._load_manifest()
        except Exception:  # pragma: no cover - defensive
            return {}
        return {
            session_id: copy.deepcopy(entry)
            for session_id, entry in data.get("sessions", {}).items()
        }

    def _delete_volume(self, entry: dict[str, Any], session_id: str) -> bool:
        """Delete one manifest-owned volume after re-verifying name and path."""
        name = entry.get("name")
        number = entry.get("number")
        if not isinstance(name, str) or type(number) is not int:
            return False
        if name != self._volume_name(session_id, number):
            return False
        with self._storage_guard:
            with self._cond:
                if name in self._export_protected:
                    return False
            path = self._dir / name
            try:
                if path.is_symlink():
                    return False
                resolved_parent = path.parent.resolve()
                base = self._dir.resolve()
                if resolved_parent != base:
                    return False
                info = path.stat()
                if not stat_module.S_ISREG(info.st_mode):
                    return False
                path.unlink()
            except OSError:
                self._mark_health("delete_error")
                return False
            with self._cond:
                sessions = self._manifest.get("sessions", {})
                session_entry = sessions.get(session_id)
                if session_entry is not None:
                    session_entry["volumes"] = [
                        item
                        for item in session_entry.get("volumes", [])
                        if item.get("name") != name
                    ]
                if len(self._deleted_volumes) >= _MANIFEST_DELETION_CAP:
                    self._deleted_volumes.clear()
                    self._maintenance_limited = True
                self._deleted_volumes.add(name)
            return True

    def _maintain_storage(self) -> None:
        """Age/budget maintenance over explicit v2-owned closed sessions only."""
        if not self._dir_ok:
            return
        with self._storage_guard:
            with self._cond:
                sessions = copy.deepcopy(self._manifest.get("sessions", {}))
            changed = self._maintain_storage_locked(sessions)
            with self._cond:
                self._manifest["sessions"] = sessions
            if changed:
                self._write_manifest()

    def _maintain_storage_locked(self, sessions: dict[str, Any]) -> bool:
        """Sweep a bounded metadata copy under storage_guard, never _cond."""
        now = self._wall_now()
        removed_any = False
        deletions = 0
        # 1. Drop entries whose files are all gone (no deletion).
        for session_id in list(sessions):
            if session_id == self._session_id:
                continue
            entry = sessions[session_id]
            volumes = entry.get("volumes", [])
            existing = [
                volume
                for volume in volumes
                if (self._dir / str(volume.get("name"))).is_file()
            ]
            if not existing and volumes:
                del sessions[session_id]
                self._deleted_sessions.add(session_id)
                removed_any = True
            elif existing and len(existing) != len(volumes):
                entry["volumes"] = existing
                removed_any = True
        # 2. Age and empty closed sessions.
        cutoff = now - timedelta(days=self._max_age)
        for session_id in list(sessions):
            if session_id == self._session_id:
                continue
            entry = sessions[session_id]
            if not entry.get("closed"):
                continue
            closed_at = _parse_timestamp(entry.get("closed_at"))
            if closed_at is not None and closed_at < cutoff:
                for volume in list(entry.get("volumes", [])):
                    if deletions >= _MAINTENANCE_DELETE_CAP:
                        self._maintenance_limited = True
                        break
                    if self._delete_volume(volume, session_id):
                        entry["volumes"] = [item for item in entry.get("volumes", [])
                                            if item.get("name") != volume.get("name")]
                        deletions += 1
                        removed_any = True
                if not entry.get("volumes"):
                    del sessions[session_id]
                    self._deleted_sessions.add(session_id)
                    removed_any = True
        # 3. Directory budget: oldest closed volumes first, bounded per pass.
        def managed_bytes() -> int:
            total = 0
            for entry in sessions.values():
                for volume in entry.get("volumes", []):
                    total += int(volume.get("bytes") or 0)
            return total

        while managed_bytes() > self._budget_bytes:
            if deletions >= _MAINTENANCE_DELETE_CAP:
                self._maintenance_limited = True
                break
            candidates = []
            for session_id, entry in sessions.items():
                if session_id == self._session_id or not entry.get("closed"):
                    continue
                for volume in entry.get("volumes", []):
                    if volume.get("name") in self._export_protected:
                        continue
                    candidates.append((entry.get("closed_at") or "", session_id, volume))
            if not candidates:
                break
            candidates.sort(key=lambda item: item[0])
            _, session_id, volume = candidates[0]
            if not self._delete_volume(volume, session_id):
                # Unverifiable entry: stop rather than guess.
                break
            sessions[session_id]["volumes"] = [item for item in sessions[session_id].get("volumes", [])
                                               if item.get("name") != volume.get("name")]
            deletions += 1
            removed_any = True
            entry = sessions.get(session_id)
            if entry is not None and not entry.get("volumes"):
                del sessions[session_id]
                self._deleted_sessions.add(session_id)
        if managed_bytes() > self._budget_bytes:
            self._mark_health("directory_over_budget")
        if removed_any:
            self._maintenance_removed += deletions
        return removed_any

    def _finalize_session(self) -> None:
        self._close_stream()
        with self._cond:
            if self._spool:
                self._spool[-1]["closed"] = True
                self._spool[-1]["bytes"] = self._current_bytes
            self._sync_session_manifest()
            entry = self._manifest.get("sessions", {}).get(self._session_id)
            if entry is not None:
                entry["closed"] = True
                entry["closed_at"] = self._wall_now().isoformat(timespec="seconds")
                entry["updated"] = entry["closed_at"]

    def _close_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.flush()
            stream.close()
        except OSError:
            self._mark_health("write_error")

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_text(
        self,
        output_ref: PathRef,
        resolver,
        *,
        stop_event: object | None = None,
    ) -> dict[str, Any]:
        """Stream a bounded text export of this session's persisted records.

        Cancellation is checked at the start, during streaming and immediately
        before publishing (also for an empty session), so a cancelled export
        never replaces the previous output file.  The target is re-resolved
        through the caller's ``resolver`` after the temporary-file identity
        check and before publishing, so a parent replaced by a junction/link
        cannot receive the file.  On every failure the old product is kept and
        only the identity-checked temporary file may be removed.
        """
        if not isinstance(output_ref, PathRef):
            raise ValueError("output_ref must be a PathRef")
        suffix = Path(output_ref.relative_path).suffix.lower()
        if suffix not in _EXPORT_SUFFIXES:
            raise ValueError("export target must end with .txt or .log")
        if not callable(getattr(resolver, "resolve", None)):
            raise ValueError("resolver must provide resolve()")
        if stop_event is not None and not callable(getattr(stop_event, "is_set", None)):
            raise ValueError("stop_event must expose is_set()")
        self._check_export_cancel(stop_event)
        final = resolver.resolve(output_ref)
        if final.exists() and final.is_dir():
            raise DiagnosticLogError("export target is a directory")
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DiagnosticLogError("export directory is unavailable") from exc
        final = resolver.resolve(output_ref)
        if final.exists() and final.is_dir():
            raise DiagnosticLogError("export target is a directory")

        names: tuple[str, ...] = ()
        with self._storage_guard, self._cond:
            snapshot = self._export_snapshot_locked()
            names = snapshot.pop("names")
            for name in names:
                self._export_protected[name] = (
                    self._export_protected.get(name, 0) + 1
                )
            self._export_count += 1
        try:
            self._check_export_cancel(stop_event)
            temp_path: Path | None = None
            stream = None
            identity: tuple[int, int] | None = None
            record_count = 0
            skipped_lines = 0
            record_cap_reached = False
            descriptor, name = tempfile.mkstemp(
                prefix=f".{final.name}.", suffix=".tmp", dir=str(final.parent)
            )
            temp_path = Path(name)
            info = os.fstat(descriptor)
            identity = (int(info.st_dev), int(info.st_ino))
            stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            stream.write(self._export_header(snapshot))
            expected_session = self._session_id
            sequence_cap = snapshot["sequence_max"]
            for path, volume_cap in snapshot["sources"]:
                self._check_export_cancel(stop_event)
                if volume_cap <= 0:
                    continue
                try:
                    for raw in _iter_volume_lines(path, volume_cap, stop_event):
                        self._check_export_cancel(stop_event)
                        record = _parse_record(raw, expected_session)
                        if record is None:
                            skipped_lines += 1
                            continue
                        if record["sequence"] > sequence_cap:
                            continue
                        text = _export_record_text(record)
                        if text is None:
                            skipped_lines += 1
                            continue
                        stream.write(text)
                        stream.write("\n")
                        record_count += 1
                        if record_count >= _EXPORT_RECORD_CAP:
                            record_cap_reached = True
                            break
                except FileNotFoundError:
                    skipped_lines += 1
                if record_cap_reached:
                    break
            stream.write(
                self._export_footer(
                    record_count, snapshot, skipped_lines, record_cap_reached
                )
            )
            stream.flush()
            os.fsync(stream.fileno())
            written = int(os.fstat(stream.fileno()).st_size)
            stream.close()
            stream = None
            if temp_path is None or identity is None:
                raise DiagnosticLogError("export temporary file was lost")
            if resolver.resolve(output_ref) != final:
                raise DiagnosticLogError("export target path changed")
            _verify_identity(temp_path, identity)
            # Re-resolve after the identity check: a parent replaced by a
            # junction/symlink after the earlier resolve must not receive the
            # publish.  The resolver rejects linked paths instead of following
            # them, so nothing is deleted through the changed path.
            if resolver.resolve(output_ref) != final:
                raise DiagnosticLogError("export target path changed")
            self._check_export_cancel(stop_event)
            _replace_file(temp_path, final)
            temp_path = None
            snapshot["record_count"] = record_count
            snapshot["skipped_lines"] = skipped_lines
            snapshot["record_cap_reached"] = record_cap_reached
            snapshot["bytes_written"] = written
            snapshot["generated_at"] = self._wall_now().isoformat(timespec="seconds")
            snapshot["output_ref"] = output_ref
            snapshot.pop("sources", None)
            return snapshot
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            with self._storage_guard, self._cond:
                for name in names:
                    remaining = self._export_protected.get(name, 0) - 1
                    if remaining <= 0:
                        self._export_protected.pop(name, None)
                    else:
                        self._export_protected[name] = remaining
                self._export_count = max(0, self._export_count - 1)
            if temp_path is not None:
                _discard_identity_checked(temp_path, identity)

    @staticmethod
    def _check_export_cancel(stop_event: object | None) -> None:
        if stop_event is not None and stop_event.is_set():
            raise DiagnosticLogError("export cancelled")

    def _export_snapshot_locked(self) -> dict[str, Any]:
        self._sync_session_manifest()
        entry = self._manifest.get("sessions", {}).get(self._session_id, {})
        volumes = entry.get("volumes", [])
        sources: list[tuple[Path, int]] = []
        names: list[str] = []
        captured_bytes = 0
        sequence_min = None
        sequence_max = 0
        for volume in volumes:
            name = str(volume.get("name"))
            capped = int(volume.get("bytes") or 0)
            if not volume.get("closed") and name == (
                self._spool[-1]["name"] if self._spool else None
            ):
                capped = self._flushed_bytes
            if capped > 0:
                sources.append((self._dir / name, capped))
                names.append(name)
                captured_bytes += capped
            first = volume.get("first_sequence")
            last = volume.get("last_sequence")
            if first is not None and sequence_min is None:
                sequence_min = int(first)
            if last is not None:
                sequence_max = max(sequence_max, int(last))
        sequence_max = max(sequence_max, self._flushed_sequence)
        return {
            "session_id": self._session_id,
            "sequence_min": int(sequence_min or 0),
            "sequence_max": int(sequence_max),
            "captured_bytes": int(captured_bytes),
            "volumes": tuple(volume.get("name") for volume in volumes),
            "names": tuple(names),
            "sources": sources,
            "file_dropped": self._file_gaps.count,
            "file_dropped_ranges": self._file_gaps.as_tuple(),
            "ui_dropped": self._ui_gaps.count,
            "ui_dropped_ranges": self._ui_gaps.as_tuple(),
            "merged": self._merged,
            "truncated_messages": self._truncated_messages,
            "rotated_out_volumes": self._volumes_removed,
            "rotated_out_ranges": self._rotated_ranges.as_tuple(),
            "rotation_blocked": self._rotation_blocked,
            "health_code": self._health_code_locked(),
        }

    @staticmethod
    def _export_header(snapshot: dict[str, Any]) -> str:
        ranges = ",".join(
            f"{first}-{last}" for first, last in snapshot["file_dropped_ranges"]
        ) or "-"
        rotated = ",".join(
            f"{first}-{last}" for first, last in snapshot["rotated_out_ranges"]
        ) or "-"
        lines = [
            "# jpgpng diagnostic log export",
            f"# session_id: {snapshot['session_id']}",
            f"# sequence_min: {snapshot['sequence_min']}",
            f"# sequence_max: {snapshot['sequence_max']}",
            f"# captured_bytes: {snapshot['captured_bytes']}",
            f"# volumes: {', '.join(snapshot['volumes']) or '-'}",
            f"# file_dropped: {snapshot['file_dropped']}",
            f"# file_dropped_ranges: {ranges}",
            f"# ui_dropped: {snapshot['ui_dropped']}",
            f"# merged_low_priority: {snapshot['merged']}",
            f"# truncated_messages: {snapshot['truncated_messages']}",
            f"# rotated_out_volumes: {snapshot['rotated_out_volumes']}",
            f"# rotated_out_ranges: {rotated}",
            f"# rotation_blocked: {snapshot['rotation_blocked']}",
            f"# health_code: {snapshot['health_code']}",
            "# note: bounded snapshot; rotated-out, dropped, merged or "
            "truncated records may be absent and this export is not claimed "
            "to be complete.",
            "",
        ]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _export_footer(
        record_count: int,
        snapshot: dict[str, Any],
        skipped_lines: int,
        record_cap_reached: bool = False,
    ) -> str:
        truncated = bool(
            snapshot["file_dropped"]
            or snapshot["rotated_out_volumes"]
            or skipped_lines
            or snapshot["truncated_messages"]
            or record_cap_reached
        )
        return (
            f"\n# records: {record_count}\n"
            f"# skipped_lines: {skipped_lines}\n"
            f"# record_cap_reached: {'yes' if record_cap_reached else 'no'}\n"
            f"# truncated: {'yes' if truncated else 'no'}\n"
        )

    # ------------------------------------------------------------------
    # Health helpers
    # ------------------------------------------------------------------

    def _mark_health(self, code: str) -> None:
        with self._cond:
            if (
                code not in self._health_codes
                and len(self._health_codes) < _MAX_HEALTH_CODES
            ):
                self._health_codes.append(code)

    def _health_code_locked(self) -> str:
        for code in (
            "open_failed",
            "write_error",
            "finalize_error",
            "manifest_error",
            "manifest_invalid",
            "close_timeout",
            "directory_over_budget",
            "delete_error",
            "queue_overflow",
            "degraded_ui",
            "ok",
        ):
            if code == "ok":
                return "ok"
            if code in self._health_codes:
                return code
        return "ok"

    def _wall_now(self) -> datetime:
        value = self._wall_clock()
        if not isinstance(value, datetime):
            raise TypeError("wall_clock must return a datetime")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_record(raw: bytes, expected_session: str) -> dict[str, Any] | None:
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("session_id") != expected_session:
        return None
    sequence = payload.get("sequence")
    if type(sequence) is not int or sequence < 0:
        return None
    if not isinstance(payload.get("timestamp"), str):
        return None
    if payload.get("level") not in LEVELS:
        return None
    if not isinstance(payload.get("message"), str):
        return None
    return payload


def _export_record_text(record: dict[str, Any]) -> str | None:
    message = _single_line(str(record.get("message", "")))
    context = []
    for key in ("job_id", "batch_id", "work_id"):
        value = record.get(key)
        if isinstance(value, str) and value:
            context.append(f"{key}={value}")
    if record.get("page_index") is not None:
        context.append(f"page_index={record['page_index']}")
    if record.get("repeat"):
        context.append(f"repeat={record['repeat']}")
    suffix = (" " + " ".join(context)) if context else ""
    return (
        f"{record['timestamp']} {record['level']:<7} {record['code']} "
        f"seq={record['sequence']} {message}{suffix}"
    )


def _verify_identity(path: Path, identity: tuple[int, int]) -> None:
    try:
        info = path.stat()
    except OSError as exc:
        raise DiagnosticLogError("export temporary file disappeared") from exc
    if (int(info.st_dev), int(info.st_ino)) != identity:
        raise DiagnosticLogError("export temporary file identity changed")


def _discard_identity_checked(
    path: Path, identity: tuple[int, int] | None
) -> None:
    try:
        info = path.stat()
    except OSError:
        return
    if identity is not None and (int(info.st_dev), int(info.st_ino)) != identity:
        return
    try:
        path.unlink()
    except OSError:
        pass


__all__ = [
    "DiagnosticEvent",
    "DiagnosticLog",
    "DiagnosticLogError",
    "LEVELS",
    "LEVEL_PREFIXES",
]
