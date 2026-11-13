"""Read-only audit of delivered artifact rows against the bytes on disk.

An artifact row is a historical delivery fact.  This module never promotes that
row to a health claim: every verdict comes from one observation of the bytes
that exist *now*.  The bound output root is classified first, so an unplugged
volume, a moved library and a deleted file never collapse into one answer; then
the file is opened once and read to a full SHA-256 with the same
device/file-index/size/mtime before and after the read.

Nothing here writes.  There is no UPDATE/INSERT/DELETE, no download, no rename,
no deletion and no directory creation; the ledger is opened read-only and no
statement writes.  One page of artifact rows plus the removal-intent
projection is read inside a short transaction that is closed *before* the first
file is opened, so a long audit cannot pin the WAL or block a concurrent
writer's checkpoint.

"missing" only means "the entry was absent during this observation": it is not
an accident report and it never authorizes a repair.  A recorded EXCLUDED
removal intent is the user's own decision, so an absent file is reported as
"excluded" instead of "missing"; an unreadable intent record fails closed as
"unknown" instead of guessing that the user never excluded it.

A legacy artifact without a recorded size or digest is reported as "unknown"
with a stable reason code.  Those rows deliberately do not travel through
``core.artifact_health.ArtifactExpectation``, which requires a positive size and
a legal digest, so they never produce an ``ArtifactHealth`` payload: there is no
comparison to make and inventing one would be a guess.

Signatures are frozen by ``docs/optimization/2026-11-14/U1A_W5_W6_SCOPE.md``::

    scan_library_artifacts(source, *, after=None, limit=200, budget=None,
                           cancel=None, stats=None) -> dict

``source`` is either an open ``LibraryRepository`` (its bound ``PathResolver``
is used) or a ``(db_path, resolver)`` pair.  Verdicts are a closed vocabulary:
``match`` / ``missing`` / ``changed`` / ``unreadable`` / ``root_unavailable`` /
``changed_during_check`` / ``unknown`` / ``excluded``.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from core.artifact_health import (
    ArtifactExpectation,
    ArtifactHealthPolicy,
    assess_observation,
)
from core.file_presence import Presence, probe_ref
from core.library_paths import PathResolver
from core.local_observation import (
    LocalObserver,
    ObservationBudget,
    ObservationRequest,
    ObservationStatus,
)
from core.preservation_policy import LocalState
from core.removal_scope import IntentKey, read_projection
from core.task_contracts import ContractError, PathRef

MAX_SCAN_LIMIT = 200
DEFAULT_MAX_BYTES = 8 * 1024 ** 3
DEFAULT_MAX_SECONDS = 300.0
DEFAULT_PER_FILE_SECONDS = 60.0
_CHUNK_SIZE = 1024 * 1024

VERDICTS = frozenset({
    "match",
    "missing",
    "changed",
    "unreadable",
    "root_unavailable",
    "changed_during_check",
    "unknown",
    "excluded",
})

# health state -> verdict.  Every state the frozen contract can produce is here;
# an unmapped state falls back to "unknown" instead of inventing a verdict.
_STATE_VERDICTS = {
    LocalState.HASH_MATCH: "match",
    LocalState.CHANGED: "changed",
    LocalState.MISSING: "missing",
    LocalState.UNREADABLE: "unreadable",
    LocalState.ROOT_UNAVAILABLE: "root_unavailable",
    LocalState.CHANGED_DURING_CHECK: "changed_during_check",
}

_SELECT = (
    "SELECT a.artifact_id, a.delivery_id, a.job_id, a.illust_id, a.page_index, "
    "a.output_key, a.output_ref_json, a.size, a.sha256, a.media_type, "
    "a.completed_at, a.verification FROM artifacts AS a"
)
_ORDER = " ORDER BY a.completed_at ASC, a.artifact_id ASC"
_AFTER = " WHERE (a.completed_at, a.artifact_id) > (?, ?)"

_BUDGET_REASONS = frozenset({"max_files", "max_bytes", "max_seconds", "cancel_requested"})


class LibraryAuditError(RuntimeError):
    """The ledger needed by one audit pass cannot be read."""


class _Cancellation(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ScanBudget:
    """Limits for one audit pass; ``max_files`` defaults to the caller's limit.

    ``max_bytes``/``max_seconds`` bound the whole pass, ``per_file_seconds``
    bounds one observation so a single large file cannot consume the pass.
    A file recorded larger than ``max_bytes`` is reported as uncheckable
    (``file_exceeds_budget``) instead of being opened or silently skipped.
    """

    max_files: int | None = None
    max_bytes: int = DEFAULT_MAX_BYTES
    max_seconds: float = DEFAULT_MAX_SECONDS
    per_file_seconds: float = DEFAULT_PER_FILE_SECONDS

    def __post_init__(self):
        if self.max_files is not None and (
            type(self.max_files) is not int or not 1 <= self.max_files <= MAX_SCAN_LIMIT
        ):
            raise ContractError("Invalid audit file budget")
        if type(self.max_bytes) is not int or not 0 <= self.max_bytes <= 2 ** 63 - 1:
            raise ContractError("Invalid audit byte budget")
        for name, label in (
            ("max_seconds", "audit time budget"),
            ("per_file_seconds", "audit per-file time budget"),
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not 0 < value <= 86400:
                raise ContractError("Invalid " + label)
            object.__setattr__(self, name, float(value))


def scan_library_artifacts(
    source: Any,
    *,
    after: tuple[str, str] | list[str] | None = None,
    limit: int = 200,
    budget: ScanBudget | None = None,
    cancel: _Cancellation | None = None,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Observe delivered artifacts and report what the disk holds right now.

    The returned mapping is ``{"rows", "next_cursor", "truncated", "stats"}``.
    Each row is ``{"artifact_id", "delivery_id", "illust_id", "page_index",
    "verdict", "reason", "size_recorded", "size_observed", "sha256_recorded",
    "observed_sha256", "checked_at", "output_key"}``; ``output_key`` is the
    recorded path digest, never a plaintext path.

    ``truncated`` means this response is not the whole library: either more
    rows exist beyond ``next_cursor`` or a budget/cancellation stopped the
    pass.  In the second case ``stats["budget_exhausted_reason"]`` names the
    cause and the cursor stays on the last row that really reached a verdict,
    so the next pass examines the stopped row again instead of skipping it.
    A pass that stopped before its first row returns the incoming cursor
    unchanged (``None`` on a first call): nothing was consumed.
    """
    audit_source = _source(source)
    page_limit = _limit(limit)
    scan_budget = _budget(budget)
    gates = _cancel(cancel)
    cursor = _cursor(after)
    if stats is not None and not isinstance(stats, dict):
        raise ContractError("Audit stats sink must be a dict")
    file_budget = page_limit if scan_budget.max_files is None else scan_budget.max_files

    run_id = "scan-" + uuid.uuid4().hex
    started = time.monotonic()
    rows: list[dict[str, Any]] = []
    counters = {verdict: 0 for verdict in VERDICTS}
    bytes_hashed = 0
    reason: str | None = None
    last_examined = None

    page, projection = audit_source.read(cursor, page_limit + 1)
    has_more = len(page) > page_limit
    facts = page[:page_limit]
    observers: dict[tuple[str, str, int], LocalObserver] = {}

    for index, row in enumerate(facts):
        if gates():
            reason = "cancel_requested"
            break
        if len(rows) >= file_budget:
            reason = "max_files"
            break
        remaining_seconds = scan_budget.max_seconds - (time.monotonic() - started)
        if remaining_seconds <= 0:
            reason = "max_seconds"
            break
        remaining_bytes = scan_budget.max_bytes - bytes_hashed
        if remaining_bytes <= 0:
            reason = "max_bytes"
            break
        fact, stop, bytes_read = _audit_row(
            row,
            resolver=audit_source.resolver,
            projection=projection,
            observers=observers,
            run_id=run_id,
            index=index,
            remaining_bytes=remaining_bytes,
            remaining_seconds=remaining_seconds,
            budget=scan_budget,
        )
        rows.append(fact)
        bytes_hashed += bytes_read
        counters[fact["verdict"]] += 1
        if stop is not None:
            reason = stop
            break
        last_examined = row

    if reason is not None:
        # The pass stopped before (or inside) a row that was not examined to a
        # verdict, so the cursor stays on the last row that really was.
        next_cursor: tuple[str, str] | None = (
            cursor if last_examined is None else _row_cursor(last_examined)
        )
        truncated = True
    elif facts:
        next_cursor = _row_cursor(facts[len(rows) - 1])
        truncated = has_more
    else:
        next_cursor = cursor
        truncated = False

    summary = {
        "examined": len(rows),
        "matched": counters["match"],
        "missing": counters["missing"],
        "changed": counters["changed"],
        "unreadable": counters["unreadable"],
        "root_unavailable": counters["root_unavailable"],
        "changed_during_check": counters["changed_during_check"],
        "unknown": counters["unknown"],
        "excluded": counters["excluded"],
        "bytes_hashed": bytes_hashed,
        "seconds": round(time.monotonic() - started, 6),
        "budget_exhausted_reason": reason if reason in _BUDGET_REASONS else None,
    }
    if stats is not None:
        stats.update(summary)
    return {
        "rows": rows,
        "next_cursor": next_cursor,
        "truncated": truncated,
        "stats": summary,
    }


def _audit_row(
    row: Any,
    *,
    resolver: PathResolver,
    projection: Any,
    observers: dict[tuple[str, str, int], LocalObserver],
    run_id: str,
    index: int,
    remaining_bytes: int,
    remaining_seconds: float,
    budget: ScanBudget,
) -> tuple[dict[str, Any], str | None, int]:
    """Return one frozen row, an optional stop reason and the bytes read."""
    fmt = _row_fields(row)
    try:
        ref = _path_ref(row["output_ref_json"])
    except (ContractError, ValueError, TypeError):
        return _row(fmt, "unknown", "artifact_path_invalid"), None, 0

    try:
        presence = probe_ref(resolver, ref)
    except (ContractError, OSError):
        return _row(fmt, "unknown", "path_rejected"), None, 0
    if presence.state is Presence.ROOT_UNAVAILABLE:
        return _row(fmt, "root_unavailable", presence.detail), None, 0
    if presence.state is Presence.UNREADABLE:
        return _row(fmt, "unreadable", presence.detail), None, 0
    if presence.state is Presence.ABSENT:
        return _absent(fmt, row, resolver, projection, ref), None, 0

    try:
        revision = resolver.root_revision(ref)
    except (ContractError, OSError):
        return _row(fmt, "unknown", "path_rejected"), None, 0
    recorded = _recorded_expectation(fmt, row, ref, revision)
    if isinstance(recorded, str):
        # A legacy or malformed row has nothing to compare against.  It never
        # becomes an ArtifactHealth and it never hashes a file.
        return _row(fmt, "unknown", recorded), None, 0
    recorded_size = recorded.size_bytes
    if recorded_size > budget.max_bytes:
        # Not progressable under any pass of this budget: say so instead of
        # opening the file or inventing a verdict the pass could not reach.
        return _row(fmt, "unknown", "file_exceeds_budget"), None, 0
    if recorded_size > remaining_bytes:
        return _row(fmt, "unknown", "byte_budget_exhausted"), "max_bytes", 0

    observer = _observer(observers, resolver, ref, revision)
    if observer is None:
        return _row(fmt, "unknown", "path_rejected"), None, 0
    per_file_seconds = min(budget.per_file_seconds, remaining_seconds)
    observation = observer.observe(
        ObservationRequest(
            content=recorded.content,
            path=ref,
            run_id=run_id,
            check_id="%s-%03d" % (run_id, index),
            budget=ObservationBudget(
                max_bytes=remaining_bytes,
                max_seconds=per_file_seconds,
                chunk_size=_CHUNK_SIZE,
            ),
        )
    )
    payload = _payload(recorded, observation)
    stop = None
    if observation.status is ObservationStatus.TIME_BUDGET_EXHAUSTED and (
        per_file_seconds == remaining_seconds
    ):
        stop = "max_seconds"
    elif observation.status is ObservationStatus.BYTE_BUDGET_EXHAUSTED:
        stop = "max_bytes"
    return payload, stop, observation.bytes_read


def _payload(
    expectation: ArtifactExpectation,
    observation: Any,
) -> dict[str, Any]:
    """Map one observation through the frozen health contract."""
    health = assess_observation(expectation, observation, ArtifactHealthPolicy())
    if health.reason == "observation_scope_mismatch":
        verdict = "unknown"
    else:
        verdict = _STATE_VERDICTS.get(health.state, "unknown")
    fmt = {
        "artifact_id": expectation.artifact_id,
        "delivery_id": expectation.delivery_id,
        "illust_id": expectation.illust_id,
        "page_index": expectation.page_index,
        "output_key": expectation.output_key,
        "size_recorded": expectation.size_bytes,
        "sha256_recorded": expectation.sha256,
    }
    return _row(
        fmt,
        verdict,
        health.reason,
        size_observed=health.observed_size,
        observed_sha256=health.observed_sha256,
        checked_at=health.checked_at,
    )


def _absent(
    fmt: dict[str, Any],
    row: Any,
    resolver: PathResolver,
    projection: Any,
    ref: PathRef,
) -> dict[str, Any]:
    """An absent entry is the user's decision only when one is recorded."""
    if projection is not None and projection.corrupt_events:
        return _row(fmt, "unknown", "corrupt_removal_intent")
    try:
        key = IntentKey(
            illust_id=str(row["illust_id"]),
            page_index=int(row["page_index"]),
            output_key=str(row["output_key"]),
            root_revision=resolver.root_revision(ref),
        )
    except (ContractError, TypeError, ValueError, OSError):
        return _row(fmt, "unknown", "intent_key_invalid")
    if projection is not None and projection.is_excluded(key):
        return _row(fmt, "excluded", "intent_excluded")
    return _row(fmt, "missing", "path_not_found")


def _recorded_expectation(
    fmt: dict[str, Any],
    row: Any,
    ref: PathRef,
    revision: int,
) -> ArtifactExpectation | str:
    """One validated expectation, or the stable reason it cannot exist."""
    size = row["size"]
    digest = row["sha256"]
    if type(size) is not int or size <= 0:
        return "no_recorded_size"
    if not isinstance(digest, str) or len(digest) != 64:
        return "no_recorded_digest"
    try:
        return ArtifactExpectation.from_artifact_row(
            row, path=ref, root_revision=revision
        )
    except (ContractError, IndexError, KeyError, TypeError, ValueError):
        return "artifact_record_invalid"


def _row_fields(row: Any) -> dict[str, Any]:
    return {
        "artifact_id": str(row["artifact_id"]),
        "delivery_id": str(row["delivery_id"]),
        "illust_id": str(row["illust_id"]),
        "page_index": int(row["page_index"]),
        "output_key": str(row["output_key"]),
        "size_recorded": None if row["size"] is None else int(row["size"]),
        "sha256_recorded": None if row["sha256"] is None else str(row["sha256"]),
    }


def _row(
    fmt: dict[str, Any],
    verdict: str,
    reason: str,
    *,
    size_observed: int | None = None,
    observed_sha256: str | None = None,
    checked_at: str | None = None,
) -> dict[str, Any]:
    if verdict not in VERDICTS:
        raise ContractError("Unsupported audit verdict")
    return {
        "artifact_id": fmt["artifact_id"],
        "delivery_id": fmt["delivery_id"],
        "illust_id": fmt["illust_id"],
        "page_index": fmt["page_index"],
        "verdict": verdict,
        "reason": reason,
        "size_recorded": fmt["size_recorded"],
        "size_observed": size_observed,
        "sha256_recorded": fmt["sha256_recorded"],
        "observed_sha256": observed_sha256,
        "checked_at": checked_at or _now(),
        "output_key": fmt["output_key"],
    }


def _observer(
    observers: dict[tuple[str, str, int], LocalObserver],
    resolver: PathResolver,
    ref: PathRef,
    revision: int,
) -> LocalObserver | None:
    key = (ref.kind, ref.root_id.casefold(), revision)
    observer = observers.get(key)
    if observer is not None:
        return observer
    try:
        root = resolver.root_path(ref)
    except (ContractError, OSError):
        return None
    observer = LocalObserver(
        root, root_id=ref.root_id, kind=ref.kind, root_revision=revision
    )
    observers[key] = observer
    return observer


def _path_ref(raw: object) -> PathRef:
    if not isinstance(raw, str):
        raise ContractError("Artifact output reference is not text")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ContractError("Artifact output reference is not JSON") from exc
    if not isinstance(value, dict):
        raise ContractError("Artifact output reference is not an object")
    return PathRef(**value)


def _row_cursor(row: Any) -> tuple[str, str]:
    return (str(row["completed_at"]), str(row["artifact_id"]))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class _Pages:
    """One short read transaction per page: rows and the intent projection."""

    resolver: PathResolver

    def read(self, after: tuple[str, str] | None, fetch: int):
        connection = self._connect()
        try:
            connection.row_factory = sqlite3.Row
            try:
                if after is None:
                    rows = connection.execute(
                        _SELECT + _ORDER + " LIMIT ?", (fetch,)
                    ).fetchall()
                else:
                    rows = connection.execute(
                        _SELECT + _AFTER + _ORDER + " LIMIT ?",
                        (after[0], after[1], fetch),
                    ).fetchall()
                projection = read_projection(connection)
            except sqlite3.Error as exc:
                raise LibraryAuditError("The library cannot be read") from exc
            return rows, projection
        finally:
            _close(connection)


class _RepositoryPages(_Pages):
    def __init__(self, repository: Any, resolver: PathResolver):
        self._repository = repository
        self.resolver = resolver

    def _connect(self):
        return self._repository._readonly_connection()


class _DatabasePages(_Pages):
    def __init__(self, database_path: Any, resolver: PathResolver):
        self._path = database_path
        self.resolver = resolver

    def _connect(self):
        return _open_readonly(self._path)


def _open_readonly(database_path: Any) -> sqlite3.Connection:
    """One read-only connection; WAL frames are honoured, nothing is written."""
    path = Path(database_path)
    if not path.exists():
        raise LibraryAuditError("The library database does not exist")
    try:
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro", uri=True, timeout=15, isolation_level=None
        )
    except sqlite3.Error as exc:
        raise LibraryAuditError("The library cannot be opened") from exc
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        return connection
    except BaseException:
        connection.close()
        raise


def _close(connection: sqlite3.Connection) -> None:
    try:
        connection.close()
    except sqlite3.Error:
        pass


def _source(source: Any) -> _Pages:
    if isinstance(source, (tuple, list)):
        if len(source) != 2:
            raise ContractError("Audit source must be (db_path, resolver)")
        database_path, resolver = source
        if not isinstance(resolver, PathResolver):
            raise ContractError("The audit needs a PathResolver")
        return _DatabasePages(database_path, resolver)
    read = getattr(source, "_readonly_connection", None)
    snapshot = getattr(source, "_path_resolver_snapshot", None)
    if callable(read) and callable(snapshot):
        resolver = snapshot()
        if not isinstance(resolver, PathResolver):
            raise ContractError("The library has no bound PathResolver")
        return _RepositoryPages(source, resolver)
    raise ContractError("Unsupported library audit source")


def _limit(value: Any) -> int:
    if type(value) is not int:
        raise ContractError("limit must be an integer")
    if not 1 <= value <= MAX_SCAN_LIMIT:
        raise ContractError("limit must be between 1 and %d" % MAX_SCAN_LIMIT)
    return value


def _budget(value: Any) -> ScanBudget:
    if value is None:
        return ScanBudget()
    if not isinstance(value, ScanBudget):
        raise ContractError("budget must be a ScanBudget")
    return value


def _cancel(value: Any):
    if value is None:
        return lambda: False
    is_set = getattr(value, "is_set", None)
    if not callable(is_set):
        raise ContractError("cancel must provide is_set()")

    def gates() -> bool:
        flag = is_set()
        if type(flag) is not bool:
            raise ContractError("cancel must return boolean")
        return flag

    return gates


def _cursor(value: Any) -> tuple[str, str] | None:
    if value is None:
        return None
    if isinstance(value, (tuple, list)) and len(value) == 2:
        if all(isinstance(item, str) for item in value):
            return (value[0], value[1])
    raise ContractError("after must be a (completed_at, artifact_id) pair")


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_SECONDS",
    "DEFAULT_PER_FILE_SECONDS",
    "LibraryAuditError",
    "MAX_SCAN_LIMIT",
    "ScanBudget",
    "VERDICTS",
    "scan_library_artifacts",
]
