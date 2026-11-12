#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only list/history queries and Markdown export for the new ledger.

This module owns no schema and never writes the library.  Every public call
opens one of the repository's short read-only transactions (``BEGIN`` plus
``PRAGMA query_only``), reads only frozen ledger facts and closes it again.  It
performs no HTTP, never touches image files and never rebuilds the ledger.

Two views are exposed through :class:`LibraryQuery`:

``batch``
    Work rows of one batch (``batch_id`` required).  Page counters are
    aggregated from the frozen ``batch_item_pages`` association, so success is
    only counted when a valid structure-checked artifact exists.  ``new`` counts
    pages whose artifact was completed by this batch
    (``deliveries.completed_batch_id``); pages completed by an earlier batch are
    ``existing``.  A partly completed work is therefore never reported as
    complete: ``state`` is derived from the frozen page links (mirroring the
    writer's result truth table) and ``result_state`` exposes the persisted
    batch-item value for audit.
``pages``
    Page detail of one work inside one batch (``batch_id`` and ``illust_id``
    required), ordered by ``page_index``.
``history``
    Cumulative artifact pages only.  Candidates that were filtered, unknown or
    never downloaded are not mixed into history.  Display metadata such as the
    title/artist name is joined from the original batch item snapshot; missing
    facts stay ``None`` and no network lookup is performed.

Pagination is keyset based (no ``OFFSET``) and bounded to 50/100/200 rows per
page.  Batch rows are ordered by ``created_at`` then ``illust_id`` (descending),
history rows by ``completed_at`` then ``artifact_id`` (descending) and page rows
by ``page_index`` (ascending).  The opaque cursor is bound to the full query
fingerprint (kind, batch/work/artist, text, states, time bounds) and carries the
last row key, so changing any condition invalidates it.  Each page's metadata
and rows come from the same read transaction; a cursor does not promise a
historical snapshot across fetches (concurrent writes may become visible on the
next fetch).

``export_markdown`` opens one read-only snapshot itself, reads the event
high-water mark first, then streams bounded row batches into a unique temporary
file in the target directory and publishes it atomically.  Concurrent writers
continue to work, but the export strictly reflects the snapshot taken at the
first read.  Cancellation, a deadline or any read/write/publish error leaves the
previous product untouched and removes only the temporary file created by this
call.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from core.library_repository import (
    CorruptDataError,
    LibraryError,
    _parse_json_object,
)
from core.task_contracts import ContractError, PathRef, canonical_json

_KINDS = ("batch", "history", "pages")
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
_PAGE_STATUSES = (
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
_ACTIVE_PHASES = (
    "queued",
    "running",
    "cooling",
    "pausing",
    "cancelling",
    "settling",
)
_PAGE_SIZES = (50, 100, 200)
_DEFAULT_PAGE_SIZE = 100
_EXPORT_CHUNK = 200
_DEFAULT_EXPORT_SECONDS = 300.0
_MAX_EXPORT_SECONDS = 86_400.0
_MAX_TEXT_CHARS = 1024
_MAX_CURSOR_CHARS = 4096
_CURSOR_VERSION = 1
_STABLE_ID_RE = re.compile(r"^[0-9]{1,32}$")
_ARTWORK_ID_RE = re.compile(r"^[0-9]{1,32}$")

_VALID_ARTIFACT_SQL = (
    "a.artifact_id IS NOT NULL AND a.verification = 'structure_checked' "
    "AND a.size IS NOT NULL AND a.size > 0 "
    "AND a.sha256 IS NOT NULL AND length(a.sha256) = 64 "
    "AND a.sha256 NOT GLOB '*[^0-9a-f]*' "
    "AND a.media_type IN ('image/jpeg','image/png','image/gif','image/webp')"
)

# The same truth table the writer maintains for batch items: only a valid
# artifact counts as success; a delivery whose artifact is missing or unusable
# is never presented as completed.
_PAGE_STATUS_SQL = (
    "CASE "
    f"WHEN {_VALID_ARTIFACT_SQL} THEN 'completed' "
    "WHEN d.delivery_id IS NULL THEN 'pending' "
    "WHEN d.state = 'completed' THEN 'needs_verification' "
    "ELSE d.state END"
)

_BATCH_SELECT = (
    "SELECT bi.batch_id, bi.illust_id, bi.artist_id, bi.artist_name, bi.title, "
    "bi.page_count, bi.decision, bi.reason_codes_json, bi.required_pages_json, "
    "bi.result_state, bi.bookmarks, bi.ai_flag, bi.created_at, bi.updated_at "
    "FROM batch_items AS bi"
)
_BATCH_ORDER = "ORDER BY bi.created_at DESC, bi.illust_id DESC"
_BATCH_AGGREGATE_SQL = (
    "SELECT bip.illust_id, COUNT(*) AS link_total, "
    f"COALESCE(SUM(CASE WHEN {_VALID_ARTIFACT_SQL} THEN 1 ELSE 0 END), 0) AS completed, "
    "COALESCE(SUM(CASE WHEN "
    f"{_VALID_ARTIFACT_SQL} AND ad.completed_batch_id = ? THEN 1 ELSE 0 END), 0) "
    "AS new_completed, "
    "COALESCE(SUM(CASE WHEN "
    f"{_VALID_ARTIFACT_SQL} AND (ad.completed_batch_id IS NULL "
    "OR ad.completed_batch_id <> ?) THEN 1 ELSE 0 END), 0) AS existing, "
    "COALESCE(SUM(CASE WHEN NOT ("
    f"{_VALID_ARTIFACT_SQL}"
    ") AND d.state = 'failed' THEN 1 ELSE 0 END), 0) AS failed, "
    "COALESCE(SUM(CASE WHEN NOT ("
    f"{_VALID_ARTIFACT_SQL}"
    ") AND d.state IN ('needs_verification','recovery_needed') "
    "THEN 1 ELSE 0 END), 0) AS needs_verification, "
    "COALESCE(SUM(CASE WHEN NOT ("
    f"{_VALID_ARTIFACT_SQL}"
    ") AND d.state = 'cancelled' THEN 1 ELSE 0 END), 0) AS cancelled, "
    "COALESCE(SUM(CASE WHEN NOT ("
    f"{_VALID_ARTIFACT_SQL}"
    ") AND (d.delivery_id IS NULL OR d.state NOT IN "
    "('failed','needs_verification','recovery_needed','cancelled')) "
    "THEN 1 ELSE 0 END), 0) AS pending "
    "FROM batch_item_pages AS bip "
    "LEFT JOIN artifacts AS abd ON abd.delivery_id = bip.delivery_id "
    "LEFT JOIN artifacts AS a "
    "ON a.artifact_id = COALESCE(bip.artifact_id, abd.artifact_id) "
    "LEFT JOIN deliveries AS ad ON ad.delivery_id = a.delivery_id "
    "LEFT JOIN deliveries AS d "
    "ON d.delivery_id = COALESCE(bip.delivery_id, a.delivery_id)"
)
_HISTORY_SELECT = (
    "SELECT a.artifact_id, a.delivery_id, a.job_id, a.illust_id, a.page_index, "
    "COALESCE(a.artist_id, bi.artist_id) AS artist_id, "
    "bi.artist_name, bi.title, "
    "a.output_key, a.output_ref_json, a.quality_json, a.media_type, a.size, "
    "a.sha256, a.verification, a.completed_at, a.legacy_origin, "
    "d.completed_batch_id, d.origin_batch_id "
    "FROM artifacts AS a "
    "JOIN deliveries AS d ON d.delivery_id = a.delivery_id "
    "LEFT JOIN batch_items AS bi "
    "ON bi.batch_id = COALESCE(d.completed_batch_id, d.origin_batch_id) "
    "AND bi.illust_id = a.illust_id"
)
_HISTORY_ORDER = "ORDER BY a.completed_at DESC, a.artifact_id DESC"
_PAGES_SELECT = (
    "SELECT bip.batch_id, bip.illust_id, bip.page_index, bip.output_key, "
    "bip.quality_json, "
    "bip.delivery_id AS link_delivery_id, bip.artifact_id AS link_artifact_id, "
    "a.artifact_id AS artifact_id, a.verification, a.size, a.sha256, "
    "a.media_type, a.completed_at, a.legacy_origin, "
    "d.delivery_id AS delivery_id, d.state AS delivery_state, d.error_code, "
    "d.completed_batch_id, "
    "COALESCE(a.output_ref_json, d.output_ref_json) AS output_ref_json, "
    f"{_PAGE_STATUS_SQL} AS page_status "
    "FROM batch_item_pages AS bip "
    "LEFT JOIN artifacts AS abd ON abd.delivery_id = bip.delivery_id "
    "LEFT JOIN artifacts AS a "
    "ON a.artifact_id = COALESCE(bip.artifact_id, abd.artifact_id) "
    "LEFT JOIN deliveries AS d "
    "ON d.delivery_id = COALESCE(bip.delivery_id, a.delivery_id)"
)
_PAGES_ORDER = "ORDER BY bip.page_index ASC"

_EXPORT_TABLES = {
    "batch": (
        "| 作品 | 标题 | 画师 | 画师 ID | 所需 | 完成 | 新增 | 已有 | 失败 "
        "| 待核验 | 未决 | 取消 | 结果 |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: "
        "| ---: | ---: | --- |",
    ),
    "history": (
        "| 作品 | 页 | 标题 | 画师 | 画师 ID | 完成时间 | 验证 | 摘要 | 大小 "
        "| 格式 | 路径 |",
        "| --- | ---: | --- | --- | --- | --- | --- | --- | ---: | --- | --- |",
    ),
    "pages": (
        "| 页 | 状态 | 原因 | 验证 | 摘要 | 大小 | 格式 | 完成时间 | 路径 |",
        "| ---: | --- | --- | --- | --- | ---: | --- | --- | --- |",
    ),
}
_QUERY_LABELS = {"batch": "本批作品", "pages": "页明细", "history": "累计历史"}


# ---------------------------------------------------------------------------
# Small validation helpers
# ---------------------------------------------------------------------------


def _utc_timestamp(value: object, name: str) -> str:
    """Normalize an explicit-UTC ISO timestamp for fingerprint stability."""
    if not isinstance(value, str):
        raise LibraryError(f"{name} must be an ISO-8601 string")
    text = value.strip()
    if not text:
        raise LibraryError(f"{name} must not be empty")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LibraryError(f"Invalid {name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise LibraryError(f"{name} must carry an explicit UTC timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _identifier(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(char) < 32 for char in value)
    ):
        raise LibraryError(f"Invalid {name}")
    return value


def _stable_id(value: object, name: str) -> str:
    if type(value) is int and 0 <= value <= 10**32 - 1:
        value = str(value)
    if not isinstance(value, str) or _STABLE_ID_RE.fullmatch(value) is None:
        raise LibraryError(f"{name} must be a stable numeric id")
    return value


def _like_pattern(text: str) -> str:
    """Escape LIKE metacharacters so user text is matched literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _states_for_kind(kind: str) -> tuple[str, ...]:
    if kind == "batch":
        return _RESULT_STATES
    if kind == "pages":
        return _PAGE_STATUSES
    return ("completed",)


def _normalize_states(value: object, kind: str) -> tuple[str, ...]:
    if isinstance(value, str) or value is None:
        raise LibraryError("states must be an explicit tuple of state names")
    if not isinstance(value, (tuple, list)):
        raise LibraryError("states must be an explicit tuple of state names")
    allowed = _states_for_kind(kind)
    selected: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in allowed:
            raise LibraryError(f"Unsupported state filter for {kind}: {item!r}")
        if item not in selected:
            selected.append(item)
    return tuple(sorted(selected))


# ---------------------------------------------------------------------------
# Public query description
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LibraryQuery:
    """One immutable, fully validated manifest query.

    ``kind='batch'`` lists the work rows of ``batch_id`` (optional
    ``illust_id``/``artist_id``/``text``/``states``/time filters).
    ``kind='pages'`` lists the selected pages of one work and rejects
    ``artist_id``/``text`` because those fields do not exist on page rows.
    ``kind='history'`` reads completed artifact pages, rejects ``batch_id`` and
    only accepts the truthful ``states=('completed',)``.

    Time bounds are inclusive ``after`` (>=) and exclusive ``before`` (<).
    Batch rows compare against ``batch_items.created_at`` (when the work entered
    the batch), history/pages rows against ``artifacts.completed_at``; rows
    without a provable time are not guessed into a filtered range.
    """

    kind: str = "batch"
    batch_id: str | None = None
    illust_id: str | None = None
    artist_id: str | None = None
    text: str = ""
    states: tuple[str, ...] = ()
    after: str | None = None
    before: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in _KINDS:
            raise LibraryError("kind must be one of batch/history/pages")
        object.__setattr__(self, "states", _normalize_states(self.states, self.kind))

        if self.kind in {"batch", "pages"}:
            object.__setattr__(
                self, "batch_id", _identifier(self.batch_id, "batch id")
            )
        elif self.batch_id is not None:
            raise LibraryError("history does not accept a batch id")
        else:
            object.__setattr__(self, "batch_id", None)

        if self.illust_id is not None:
            object.__setattr__(
                self, "illust_id", _stable_id(self.illust_id, "illust id")
            )
        elif self.kind == "pages":
            raise LibraryError("pages requires an illust id")

        if self.kind == "pages":
            if self.artist_id is not None:
                raise LibraryError("pages does not accept an artist filter")
            if self.text != "":
                raise LibraryError("pages does not accept a text filter")
        elif self.artist_id is not None:
            object.__setattr__(
                self, "artist_id", _stable_id(self.artist_id, "artist id")
            )

        if not isinstance(self.text, str):
            raise LibraryError("text must be a string")
        if len(self.text) > _MAX_TEXT_CHARS:
            raise LibraryError("text is too long")
        if any(ord(char) < 32 for char in self.text):
            raise LibraryError("text must not contain control characters")

        if self.after is not None:
            object.__setattr__(self, "after", _utc_timestamp(self.after, "after"))
        if self.before is not None:
            object.__setattr__(self, "before", _utc_timestamp(self.before, "before"))
        if self.after is not None and self.before is not None:
            if self.after > self.before:
                raise LibraryError("after must not be later than before")


@dataclass(frozen=True, slots=True)
class QueryPage:
    """One bounded keyset page plus the ledger event watermark it was read at."""

    rows: tuple[dict[str, Any], ...]
    next_cursor: str | None
    watermark: int


@dataclass(frozen=True, slots=True)
class ExportResult:
    """A published Markdown manifest and the snapshot facts it represents."""

    output_ref: PathRef
    row_count: int
    watermark: int
    generated_at: str
    bytes_written: int


# ---------------------------------------------------------------------------
# Markdown safety helpers
# ---------------------------------------------------------------------------

_MD_ESCAPE_RE = re.compile(r"([\\`*_\[\]<>|~])")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_CELL_CHARS = 400


def _markdown_text(value: object, *, limit: int = _MAX_CELL_CHARS) -> str:
    """Escape one value so it cannot break a table cell or inject markup."""
    if value is None:
        return "—"
    text = _CONTROL_RE.sub(" ", str(value))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if limit > 0 and len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    lines = [_MD_ESCAPE_RE.sub(r"\\\1", line) for line in text.split("\n")]
    return "<br>".join(lines)


def _markdown_int(value: object) -> str:
    if type(value) is not int:
        return "—"
    return str(value)


def _markdown_path(ref: dict[str, Any] | None) -> str:
    if not isinstance(ref, dict):
        return "—"
    text = (
        f"{ref.get('kind', '')}:{ref.get('root_id', '')}/"
        f"{ref.get('relative_path', '')}"
    )
    return _markdown_text(text)


def _artwork_cell(illust_id: object) -> str:
    if isinstance(illust_id, str) and _ARTWORK_ID_RE.fullmatch(illust_id):
        return f"[{illust_id}](https://www.pixiv.net/artworks/{illust_id})"
    return _markdown_text(illust_id)


# ---------------------------------------------------------------------------
# Public reader
# ---------------------------------------------------------------------------


class LibraryQueries:
    """Read-only manifest queries and snapshot Markdown export.

    ``repository`` must be an open :class:`core.library_repository.LibraryRepository`;
    only its short read-only transaction factory is used.  ``clock`` and
    ``export_time_limit`` bound one export call (default 300 seconds) and exist
    so tests can prove cancellation without a real long wait.
    """

    def __init__(
        self,
        repository,
        *,
        clock: Callable[[], float] = time.monotonic,
        export_time_limit: float = _DEFAULT_EXPORT_SECONDS,
    ) -> None:
        if not callable(getattr(repository, "_readonly_connection", None)):
            raise LibraryError("LibraryQueries requires a LibraryRepository")
        if not callable(clock):
            raise LibraryError("clock must be callable")
        if type(export_time_limit) not in (int, float) or not (
            0 < export_time_limit <= _MAX_EXPORT_SECONDS
        ):
            raise LibraryError("export_time_limit must be a positive number of seconds")
        self._repository = repository
        self._clock = clock
        self._export_time_limit = float(export_time_limit)

    # -- public API -----------------------------------------------------

    def fetch(
        self,
        query: LibraryQuery,
        *,
        cursor: str | None = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> QueryPage:
        self._require_open()
        if not isinstance(query, LibraryQuery):
            raise LibraryError("fetch requires a LibraryQuery")
        if type(page_size) is not int or page_size not in _PAGE_SIZES:
            raise LibraryError("page_size must be one of 50, 100 or 200")
        key = self._decode_cursor(query, cursor)
        conn = self._repository._readonly_connection()
        try:
            watermark = self._watermark(conn)
            raw, has_more = self._select(conn, query, page_size + 1, key)
            raw = raw[:page_size]
            rows = self._materialize(conn, query, raw)
            next_cursor = None
            if has_more and raw:
                next_cursor = self._encode_cursor(query, raw[-1])
            return QueryPage(tuple(rows), next_cursor, watermark)
        finally:
            conn.close()

    def export_markdown(
        self,
        query: LibraryQuery,
        output_ref: PathRef,
        resolver,
        *,
        stop_event: object | None = None,
    ) -> ExportResult:
        self._require_open()
        if not isinstance(query, LibraryQuery):
            raise LibraryError("export_markdown requires a LibraryQuery")
        if not isinstance(output_ref, PathRef):
            raise LibraryError("export_markdown requires a PathRef output")
        self._validate_target_name(output_ref)
        if not callable(getattr(resolver, "resolve", None)):
            raise LibraryError("export_markdown requires a PathResolver")
        if stop_event is not None and not callable(
            getattr(stop_event, "is_set", None)
        ):
            raise LibraryError("stop_event must expose is_set()")

        final = resolver.resolve(output_ref)
        if final.exists() and final.is_dir():
            raise LibraryError("Export target is a directory")
        final.parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve after creating parents so a replaced/linked directory is
        # still refused before the temporary file is created.
        final = resolver.resolve(output_ref)
        if final.exists() and final.is_dir():
            raise LibraryError("Export target is a directory")

        started = self._clock()
        deadline = started + self._export_time_limit
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = None
        stream = None
        temp_path: Path | None = None
        temp_identity: tuple[int, int] | None = None
        row_count = 0
        try:
            conn = self._repository._readonly_connection()
            # First read: the snapshot and its event watermark start here.
            watermark = self._watermark(conn)
            running = self._is_running(conn, query)
            self._check_cancel(stop_event, deadline)

            stream, temp_path, temp_identity = self._open_temp(final)
            self._write_header(
                stream, query, watermark, running, generated_at
            )
            key: list[Any] | None = None
            while True:
                raw, _ = self._select(conn, query, _EXPORT_CHUNK, key)
                if not raw:
                    break
                for row in self._materialize(conn, query, raw):
                    self._check_cancel(stop_event, deadline)
                    stream.write(self._render_row(query, row))
                    stream.write("\n")
                    row_count += 1
                key = self._row_key(query, raw[-1])
                if len(raw) < _EXPORT_CHUNK:
                    break
            stream.write("\n---\n\n")
            stream.write(
                f"共 {row_count} 行；事件水位 {watermark}；"
                f"名单状态 {'运行中' if running else '已结束'}。\n"
            )
            self._check_cancel(stop_event, deadline)
            stream.flush()
            os.fsync(stream.fileno())
            bytes_written = int(os.fstat(stream.fileno()).st_size)
            stream.close()
            stream = None
            if temp_path is None or temp_identity is None:
                raise LibraryError("Export temporary file was lost")
            _verify_identity(temp_path, temp_identity)
            if resolver.resolve(output_ref) != final:
                raise LibraryError("Export target root changed before publication")
            _replace_file(temp_path, final)
            temp_path = None
            return ExportResult(
                output_ref=output_ref,
                row_count=row_count,
                watermark=watermark,
                generated_at=generated_at,
                bytes_written=bytes_written,
            )
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            if conn is not None:
                conn.close()
            if temp_path is not None:
                _discard_file(temp_path, temp_identity)

    # -- validation -----------------------------------------------------

    def _require_open(self) -> None:
        ensure = getattr(self._repository, "_ensure_open", None)
        if callable(ensure):
            ensure()

    @staticmethod
    def _validate_target_name(output_ref: PathRef) -> None:
        suffix = PurePosixPath(output_ref.relative_path).suffix.lower()
        if suffix != ".md":
            raise LibraryError("Export target must use the .md suffix")

    # -- read helpers ---------------------------------------------------

    @staticmethod
    def _watermark(conn) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(event_seq), 0) AS watermark FROM events"
        ).fetchone()
        return int(row["watermark"])

    @staticmethod
    def _is_running(conn, query: LibraryQuery) -> bool:
        if query.kind in {"batch", "pages"}:
            row = conn.execute(
                "SELECT phase FROM batches WHERE batch_id = ?",
                (query.batch_id,),
            ).fetchone()
            return row is not None and str(row["phase"]) in _ACTIVE_PHASES
        row = conn.execute(
            "SELECT 1 FROM batches WHERE phase IN ("
            + ",".join("?" for _ in _ACTIVE_PHASES)
            + ") LIMIT 1",
            _ACTIVE_PHASES,
        ).fetchone()
        return row is not None

    def _check_cancel(self, stop_event: object | None, deadline: float) -> None:
        if stop_event is not None and stop_event.is_set():
            raise LibraryError("Export cancelled")
        if self._clock() >= deadline:
            raise LibraryError("Export time limit exceeded")

    # -- SQL selection --------------------------------------------------

    @staticmethod
    def _select_sql(query: LibraryQuery) -> tuple[str, str]:
        if query.kind == "batch":
            return _BATCH_SELECT, _BATCH_ORDER
        if query.kind == "history":
            return _HISTORY_SELECT, _HISTORY_ORDER
        return _PAGES_SELECT, _PAGES_ORDER

    def _select(
        self,
        conn,
        query: LibraryQuery,
        limit: int,
        key: list[Any] | None,
    ) -> tuple[list[Any], bool]:
        select, order = self._select_sql(query)
        parts, params = self._predicates(query, key)
        sql = select
        if parts:
            sql += " WHERE " + " AND ".join(parts)
        sql += " " + order + " LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return rows, len(rows) >= limit

    def _predicates(
        self, query: LibraryQuery, key: list[Any] | None
    ) -> tuple[list[str], list[Any]]:
        parts: list[str] = []
        params: list[Any] = []
        if query.kind == "batch":
            parts.append("bi.batch_id = ?")
            params.append(query.batch_id)
            if query.illust_id is not None:
                parts.append("bi.illust_id = ?")
                params.append(query.illust_id)
            if query.artist_id is not None:
                parts.append("bi.artist_id = ?")
                params.append(query.artist_id)
            if query.text:
                pattern = _like_pattern(query.text)
                parts.append(
                    "(bi.title LIKE ? ESCAPE '\\' OR bi.artist_name LIKE ? ESCAPE '\\')"
                )
                params.extend((pattern, pattern))
            if query.states:
                parts.append(
                    "bi.result_state IN ("
                    + ",".join("?" for _ in query.states)
                    + ")"
                )
                params.extend(query.states)
            if query.after is not None:
                parts.append("julianday(bi.created_at) >= julianday(?)")
                params.append(query.after)
            if query.before is not None:
                parts.append("julianday(bi.created_at) < julianday(?)")
                params.append(query.before)
            if key is not None:
                parts.append(
                    "(bi.created_at < ? OR (bi.created_at = ? AND bi.illust_id < ?))"
                )
                params.extend((key[0], key[0], key[1]))
            return parts, params

        if query.kind == "history":
            if query.illust_id is not None:
                parts.append("a.illust_id = ?")
                params.append(query.illust_id)
            if query.artist_id is not None:
                parts.append(
                    "(a.artist_id = ? OR (a.artist_id IS NULL AND bi.artist_id = ?))"
                )
                params.extend((query.artist_id, query.artist_id))
            if query.text:
                pattern = _like_pattern(query.text)
                parts.append(
                    "(bi.title LIKE ? ESCAPE '\\' OR bi.artist_name LIKE ? ESCAPE '\\')"
                )
                params.extend((pattern, pattern))
            if query.after is not None:
                parts.append("julianday(a.completed_at) >= julianday(?)")
                params.append(query.after)
            if query.before is not None:
                parts.append("julianday(a.completed_at) < julianday(?)")
                params.append(query.before)
            if key is not None:
                parts.append(
                    "(a.completed_at < ? OR (a.completed_at = ? AND a.artifact_id < ?))"
                )
                params.extend((key[0], key[0], key[1]))
            return parts, params

        parts.append("bip.batch_id = ?")
        params.append(query.batch_id)
        parts.append("bip.illust_id = ?")
        params.append(query.illust_id)
        if query.states:
            parts.append(
                f"({_PAGE_STATUS_SQL}) IN ("
                + ",".join("?" for _ in query.states)
                + ")"
            )
            params.extend(query.states)
        if query.after is not None:
            parts.append("julianday(a.completed_at) >= julianday(?)")
            params.append(query.after)
        if query.before is not None:
            parts.append("julianday(a.completed_at) < julianday(?)")
            params.append(query.before)
        if key is not None:
            parts.append("bip.page_index > ?")
            params.append(key[0])
        return parts, params

    # -- row materialization -------------------------------------------

    def _materialize(
        self, conn, query: LibraryQuery, raw: Iterable[Any]
    ) -> list[dict[str, Any]]:
        if query.kind == "batch":
            return self._batch_rows(conn, raw)
        if query.kind == "history":
            return [self._history_row(row) for row in raw]
        return [self._page_row(row) for row in raw]

    def _batch_rows(self, conn, raw: Iterable[Any]) -> list[dict[str, Any]]:
        rows = list(raw)
        if not rows:
            return []
        batch_id = str(rows[0]["batch_id"])
        illust_ids = [str(row["illust_id"]) for row in rows]
        aggregates = self._batch_aggregates(conn, batch_id, illust_ids)
        result: list[dict[str, Any]] = []
        for row in rows:
            illust_id = str(row["illust_id"])
            required_pages = _required_pages(row["required_pages_json"])
            counts = aggregates.get(illust_id, _empty_page_counts())
            missing = max(0, len(required_pages) - int(counts["link_total"]))
            pending = int(counts["pending"]) + missing
            decision = str(row["decision"])
            completed = int(counts["completed"])
            failed = int(counts["failed"])
            unverified = int(counts["needs_verification"])
            cancelled = int(counts["cancelled"])
            total = len(required_pages)
            result.append(
                {
                    "kind": "batch",
                    "batch_id": batch_id,
                    "illust_id": illust_id,
                    "artist_id": row["artist_id"],
                    "artist_name": row["artist_name"],
                    "title": row["title"],
                    "page_count": _optional_int(row["page_count"]),
                    "decision": decision,
                    # The visible result is derived from the frozen page links:
                    # two of three verified pages is never "completed".  The
                    # persisted batch-item value is exposed alongside for audit.
                    "state": _derived_work_state(
                        decision,
                        total,
                        completed,
                        failed,
                        unverified,
                        cancelled,
                        pending,
                    ),
                    "result_state": str(row["result_state"]),
                    "required": total,
                    "completed": completed,
                    "new_completed": int(counts["new_completed"]),
                    "existing": int(counts["existing"]),
                    "failed": failed,
                    "needs_verification": unverified,
                    "cancelled": cancelled,
                    "pending": pending,
                    "reason_codes": _reason_codes(row["reason_codes_json"]),
                    "bookmarks": _optional_int(row["bookmarks"]),
                    "ai_flag": _optional_bool(row["ai_flag"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return result

    @staticmethod
    def _batch_aggregates(
        conn, batch_id: str, illust_ids: list[str]
    ) -> dict[str, Any]:
        if not illust_ids:
            return {}
        placeholders = ",".join("?" for _ in illust_ids)
        sql = (
            _BATCH_AGGREGATE_SQL
            + f" WHERE bip.batch_id = ? AND bip.illust_id IN ({placeholders}) "
            "GROUP BY bip.illust_id"
        )
        params = [batch_id, batch_id, batch_id, *illust_ids]
        return {
            str(row["illust_id"]): row
            for row in conn.execute(sql, params).fetchall()
        }

    @staticmethod
    def _history_row(row: Any) -> dict[str, Any]:
        completed_batch_id = row["completed_batch_id"]
        legacy_origin = row["legacy_origin"]
        return {
            "kind": "history",
            "artifact_id": str(row["artifact_id"]),
            "delivery_id": str(row["delivery_id"]),
            "job_id": str(row["job_id"]),
            "illust_id": str(row["illust_id"]),
            "page_index": int(row["page_index"]),
            "title": row["title"],
            "artist_id": row["artist_id"],
            "artist_name": row["artist_name"],
            "output_key": str(row["output_key"]),
            "output_ref": _output_ref(row["output_ref_json"]),
            "quality": _quality(row["quality_json"]),
            "media_type": row["media_type"],
            "size": _optional_int(row["size"]),
            "sha256": row["sha256"],
            "verification": row["verification"],
            "completed_at": row["completed_at"],
            "legacy_origin": legacy_origin,
            "legacy": legacy_origin is not None,
            "historical": completed_batch_id is None,
            "completed_batch_id": completed_batch_id,
            "origin_batch_id": row["origin_batch_id"],
        }

    @staticmethod
    def _page_row(row: Any) -> dict[str, Any]:
        return {
            "kind": "pages",
            "batch_id": str(row["batch_id"]),
            "illust_id": str(row["illust_id"]),
            "page_index": int(row["page_index"]),
            "delivery_id": row["delivery_id"],
            "artifact_id": row["artifact_id"],
            "status": str(row["page_status"]),
            "delivery_state": row["delivery_state"],
            "error_code": row["error_code"],
            "output_key": str(row["output_key"]),
            "output_ref": _output_ref(row["output_ref_json"]),
            "quality": _quality(row["quality_json"]),
            "verification": row["verification"],
            "size": _optional_int(row["size"]),
            "sha256": row["sha256"],
            "media_type": row["media_type"],
            "completed_at": row["completed_at"],
            "legacy_origin": row["legacy_origin"],
            "completed_batch_id": row["completed_batch_id"],
        }

    # -- cursor ---------------------------------------------------------

    @staticmethod
    def _fingerprint(query: LibraryQuery) -> str:
        payload = {
            "kind": query.kind,
            "batch_id": query.batch_id,
            "illust_id": query.illust_id,
            "artist_id": query.artist_id,
            "text": query.text,
            "states": list(query.states),
            "after": query.after,
            "before": query.before,
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def _row_key(query: LibraryQuery, row: Any) -> list[Any]:
        if query.kind == "batch":
            return [str(row["created_at"]), str(row["illust_id"])]
        if query.kind == "history":
            return [str(row["completed_at"]), str(row["artifact_id"])]
        return [int(row["page_index"])]

    def _encode_cursor(self, query: LibraryQuery, row: Any) -> str:
        payload = {
            "b": query.batch_id,
            "f": self._fingerprint(query),
            "k": query.kind,
            "key": self._row_key(query, row),
            "v": _CURSOR_VERSION,
        }
        encoded = canonical_json(payload).encode("utf-8")
        return base64.urlsafe_b64encode(encoded).decode("ascii")

    def _decode_cursor(
        self, query: LibraryQuery, cursor: object
    ) -> list[Any] | None:
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not cursor or len(cursor) > _MAX_CURSOR_CHARS:
            raise LibraryError("Invalid query cursor")
        try:
            decoded = base64.b64decode(
                cursor.encode("ascii"), altchars=b"-_", validate=True
            )
            payload = json.loads(decoded.decode("utf-8"))
        except (ValueError, UnicodeError, binascii.Error, RecursionError) as exc:
            raise LibraryError("Invalid query cursor") from exc
        if not isinstance(payload, dict) or set(payload) != {"b", "f", "k", "key", "v"}:
            raise LibraryError("Invalid query cursor")
        if payload["v"] != _CURSOR_VERSION:
            raise LibraryError("Unsupported query cursor version")
        if payload["k"] != query.kind or payload["b"] != query.batch_id:
            raise LibraryError("Query cursor belongs to a different query")
        if payload["f"] != self._fingerprint(query):
            raise LibraryError("Query cursor belongs to a different query")
        key = payload["key"]
        if not isinstance(key, list):
            raise LibraryError("Invalid query cursor")
        if query.kind == "batch":
            if len(key) != 2 or not all(isinstance(item, str) for item in key):
                raise LibraryError("Invalid query cursor")
            return [key[0], key[1]]
        if query.kind == "history":
            if len(key) != 2 or not all(isinstance(item, str) for item in key):
                raise LibraryError("Invalid query cursor")
            return [key[0], key[1]]
        if (
            len(key) != 1
            or type(key[0]) is not int
            or not 0 <= key[0] <= 9999
        ):
            raise LibraryError("Invalid query cursor")
        return [key[0]]

    # -- export writing -------------------------------------------------

    @staticmethod
    def _open_temp(final: Path) -> tuple[Any, Path, tuple[int, int]]:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{final.name}.", suffix=".tmp", dir=str(final.parent)
        )
        temp_path = Path(name)
        try:
            info = os.fstat(descriptor)
            identity = (int(info.st_dev), int(info.st_ino))
            stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        except BaseException:
            os.close(descriptor)
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise
        return stream, temp_path, identity

    def _write_header(
        self,
        stream,
        query: LibraryQuery,
        watermark: int,
        running: bool,
        generated_at: str,
    ) -> None:
        conditions = [
            ("查询种类", _QUERY_LABELS[query.kind]),
            ("批次", query.batch_id if query.batch_id is not None else "（不适用）"),
            ("作品 ID", query.illust_id if query.illust_id is not None else "（不适用）"),
            ("画师 ID", query.artist_id if query.artist_id is not None else "（不适用）"),
            ("文字", query.text if query.text else "（无）"),
            ("结果状态", "、".join(query.states) if query.states else "（全部）"),
            (
                "时间范围",
                f"[{query.after or '不限'}, {query.before or '不限'})",
            ),
            ("事件水位", watermark),
            ("名单状态", "运行中（名单可能仍在变化）" if running else "已结束（快照）"),
            ("生成时间", generated_at),
        ]
        stream.write("# 下载名单\n\n")
        for label, value in conditions:
            stream.write(f"- {label}: {_markdown_text(value)}\n")
        stream.write(
            "\n> 本文件由只读快照导出，不重新下载、不读取原图；"
            "作品链接仅由合法数字 ID 生成。\n\n"
        )
        header, separator = _EXPORT_TABLES[query.kind]
        stream.write(header)
        stream.write("\n")
        stream.write(separator)
        stream.write("\n")

    def _render_row(self, query: LibraryQuery, row: dict[str, Any]) -> str:
        if query.kind == "batch":
            return "| " + " | ".join(
                [
                    _artwork_cell(row.get("illust_id")),
                    _markdown_text(row.get("title")),
                    _markdown_text(row.get("artist_name")),
                    _markdown_text(row.get("artist_id")),
                    _markdown_int(row.get("required")),
                    _markdown_int(row.get("completed")),
                    _markdown_int(row.get("new_completed")),
                    _markdown_int(row.get("existing")),
                    _markdown_int(row.get("failed")),
                    _markdown_int(row.get("needs_verification")),
                    _markdown_int(row.get("pending")),
                    _markdown_int(row.get("cancelled")),
                    _markdown_text(row.get("state")),
                ]
            ) + " |"
        if query.kind == "history":
            return "| " + " | ".join(
                [
                    _artwork_cell(row.get("illust_id")),
                    _markdown_int(row.get("page_index")),
                    _markdown_text(row.get("title")),
                    _markdown_text(row.get("artist_name")),
                    _markdown_text(row.get("artist_id")),
                    _markdown_text(row.get("completed_at")),
                    _markdown_text(row.get("verification")),
                    _markdown_text(row.get("sha256")),
                    _markdown_int(row.get("size")),
                    _markdown_text(row.get("media_type")),
                    _markdown_path(row.get("output_ref")),
                ]
            ) + " |"
        return "| " + " | ".join(
            [
                _markdown_int(row.get("page_index")),
                _markdown_text(row.get("status")),
                _markdown_text(row.get("error_code")),
                _markdown_text(row.get("verification")),
                _markdown_text(row.get("sha256")),
                _markdown_int(row.get("size")),
                _markdown_text(row.get("media_type")),
                _markdown_text(row.get("completed_at")),
                _markdown_path(row.get("output_ref")),
            ]
        ) + " |"


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _replace_file(source: Path, target: Path) -> None:
    """Publish the finished temporary file with one atomic replacement."""
    os.replace(source, target)


def _discard_file(path: Path, identity: tuple[int, int] | None) -> None:
    """Remove only the temporary file created by this export call."""
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


def _verify_identity(path: Path, identity: tuple[int, int]) -> None:
    try:
        info = path.stat()
    except OSError as exc:
        raise LibraryError("Export temporary file disappeared") from exc
    if (int(info.st_dev), int(info.st_ino)) != identity:
        raise LibraryError("Export temporary file identity changed")


def _empty_page_counts() -> dict[str, int]:
    return {
        "link_total": 0,
        "completed": 0,
        "new_completed": 0,
        "existing": 0,
        "failed": 0,
        "needs_verification": 0,
        "cancelled": 0,
        "pending": 0,
    }


def _derived_work_state(
    decision: str,
    total: int,
    completed: int,
    failed: int,
    unverified: int,
    cancelled: int,
    pending: int,
) -> str:
    """Mirror the writer's result truth table from frozen page links."""
    if decision != "download":
        return decision
    if total > 0 and completed == total:
        return "completed"
    if completed > 0:
        return "partial"
    if unverified > 0 and pending == 0:
        return "needs_verification"
    if failed > 0 and cancelled + failed == total:
        return "failed"
    if total > 0 and cancelled == total:
        return "cancelled"
    return "pending"


def _required_pages(text: object) -> set[int]:
    try:
        value = json.loads(text)
    except (TypeError, ValueError, RecursionError) as exc:
        raise CorruptDataError("Corrupt required page list") from exc
    if not isinstance(value, list):
        raise CorruptDataError("Corrupt required page list")
    return {
        int(item)
        for item in value
        if type(item) is int and 0 <= item <= 9999
    }


def _reason_codes(text: object) -> list[str]:
    try:
        value = json.loads(text)
    except (TypeError, ValueError, RecursionError) as exc:
        raise CorruptDataError("Corrupt reason code list") from exc
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise CorruptDataError("Corrupt reason code list")
    return list(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise CorruptDataError("Corrupt integer column")
    return int(value)


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if type(value) is not int or value not in (0, 1):
        raise CorruptDataError("Corrupt boolean column")
    return bool(value)


def _output_ref(text: object) -> dict[str, str] | None:
    if text is None:
        return None
    value = _parse_json_object(text, "output reference")
    if set(value) != {"kind", "root_id", "relative_path"}:
        raise CorruptDataError("Corrupt output reference")
    try:
        ref = PathRef(**value)
    except (TypeError, ValueError, ContractError) as exc:
        raise CorruptDataError("Corrupt output reference") from exc
    return {
        "kind": ref.kind,
        "root_id": ref.root_id,
        "relative_path": ref.relative_path,
    }


_QUALITY_KEYS = ("original", "all_pages", "validation_policy_version")


def _quality(text: object) -> dict[str, Any]:
    value = _parse_json_object(text, "quality")
    # Only the safe quality facts are exposed; arbitrary payload keys never are.
    return {key: value.get(key) for key in _QUALITY_KEYS}


__all__ = [
    "ExportResult",
    "LibraryQueries",
    "LibraryQuery",
    "QueryPage",
]
