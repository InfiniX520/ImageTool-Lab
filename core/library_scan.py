"""Whole-library scan aggregation (contract v6 section 3.2).

The engine in core.library_audit answers one page at a time and its API is
frozen: it reports how much of the library *that page* covered, never what the
library as a whole holds.  This module is the layer above it.  It walks pages
from the head (or from one explicit cursor), accumulates every verdict and
derives the single answer a maintenance command may print.

Three rules make that answer honest:

* A page is a page.  Reaching the end of the ledger is a fact about the walk,
  not about the last response: only a walk that started at the head and never
  stopped for a budget, a cancellation or a ledger failure may say "complete".
* A continuation says nothing about the whole library.  It has no row count,
  its prefix is unproven, and the most it may report is "these rows were
  examined"; a bare cursor can therefore never produce exit code 0, even when
  it happens to land on the last page.
* Severity is monotone.  One missing or changed file on page 1 of 3 is exit 2
  even when pages 2 and 3 are entirely green; a ledger that dies on page 2 is
  exit 3 even when page 1 was green.  A later page never overwrites an earlier
  finding.

Nothing here writes.  The engine opens one short read transaction per page, so
a long walk does not pin the WAL or hold a connection while hashing, and the
report states explicitly that a multi-page walk is not one point-in-time
snapshot of the library.

The public entry point is::

    scan_library_report(source, *, project_root=None, roots=None, after=None,
                        limit=200, budget=None, cancel=None,
                        kind="full_walk", allow_unknown=False) -> dict
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from core.app_paths import AppPaths
from core.library_audit import (
    MAX_SCAN_LIMIT,
    LibraryAuditError,
    ScanBudget,
    scan_library_artifacts,
)
from core.library_paths import PathResolver
from core.output_locations import LocationError, load_output_roots
from core.task_contracts import ContractError

#: What one *walk* covered.  This is deliberately a different vocabulary from
#: the engine's per-page coverage: "page" is not an answer about the library,
#: and "empty" here means the ledger holds no delivered artifact at all.
SCAN_COVERAGES = frozenset({"complete", "partial", "cancelled", "empty", "error"})

#: Verdicts that are a fact about a file the user asked us to keep.  "unknown"
#: is not here: nobody compared the bytes, so it is not a finding about them --
#: it is a reason the walk cannot say "all green" (contract v6 section 3.3).
FINDING_VERDICTS = (
    "missing",
    "changed",
    "unreadable",
    "root_unavailable",
    "changed_during_check",
)

#: The only unknown reasons --allow-unknown may release: a legacy row with no
#: recorded size or digest has nothing to compare against.  Every other reason
#: is a live gap and stays fatal to exit code 0.
LEGACY_UNKNOWN_REASONS = frozenset({"no_recorded_size", "no_recorded_digest"})

_KINDS = ("full_walk", "continuation", "page")
_MAX_LISTED_FINDINGS = 5000
_MAX_LISTED_ROWS = 5000
_MAX_PAGES_WITHOUT_COUNT = 10000
_BUDGET_REASONS = frozenset({"max_files", "max_bytes", "max_seconds", "cancel_requested"})
_COUNTERS = (
    "examined",
    "considered",
    "matched",
    "missing",
    "changed",
    "unreadable",
    "root_unavailable",
    "changed_during_check",
    "unknown",
    "excluded",
)

_EXIT_OK = 0
_EXIT_FINDINGS = 2
_EXIT_UNREADABLE = 3
_EXIT_INCOMPLETE = 5


class ScanMappingError(RuntimeError):
    """The saved output-root mapping cannot be used for this walk.

    The command line maps this to exit code 3: without the mapping an external
    row is not "matching" and not "missing", it is unanswerable, and scanning
    without it would silently report the wrong thing about every page stored
    outside the project.
    """

    code = "mapping_unreadable"

    def __init__(self, detail: str):
        self.detail = str(detail)
        super().__init__(self.detail)


def scan_library_report(
    source,
    *,
    project_root=None,
    roots=None,
    after=None,
    limit: int = 200,
    budget=None,
    cancel=None,
    kind: str = "full_walk",
    allow_unknown: bool = False,
):
    """Walk the library and report one honest answer about the whole walk.

    source is exactly what core.library_audit.scan_library_artifacts accepts:
    an open LibraryRepository or a (database_path, resolver) pair.  kind
    decides the walk:

    full_walk      start at the head and keep going until the end of the
                   ledger, a budget stop, a cancellation or a ledger error.
    continuation   exactly one page after the caller's cursor.  It never
                   claims the library and it never exits 0.
    page           exactly one page from the head, for inspection.

    roots binds an already-loaded {root_id: Path} mapping; when it is omitted
    and project_root is given, the saved mapping is read from that project.
    Binding never mutates the caller's resolver.

    Returns a mapping with coverage, scope, pages_examined, library_rows,
    stats, findings, unknown_reasons, next_cursor, resume and exit_code
    (contract v6 section 3.2).
    """
    walk = _kind(kind)
    page_limit = _limit(limit)
    scan_budget = _budget(budget)
    cursor = _cursor(after)
    if walk == "continuation" and cursor is None:
        raise ContractError("a continuation walk needs a cursor")
    if walk == "full_walk" and cursor is not None:
        raise ContractError("a full walk starts at the head, not at a cursor")
    gates = _cancel(cancel)
    bound_roots, mapping_state = _roots_for(project_root, roots)
    if bound_roots:
        source = _bind_source(source, bound_roots)
    root_summary = _root_summary(bound_roots)

    started_at = _now()
    started = time.monotonic()
    counters = {name: 0 for name in _COUNTERS}
    unknown_reasons: dict[str, int] = {}
    findings: list[dict] = []
    rows: list[dict] = []
    findings_omitted = 0
    rows_omitted = 0
    bytes_hashed = 0
    page_seconds = 0.0
    library_rows: int | None = None
    pages = 0
    coverage = "complete"
    stopped_at: str | None = None
    budget_reason: str | None = None
    ledger_error: str | None = None
    next_cursor: tuple[str, str] | None = cursor
    page_guard = _MAX_PAGES_WITHOUT_COUNT

    while True:
        remaining_files = (
            None
            if scan_budget.max_files is None
            else scan_budget.max_files - counters["examined"]
        )
        if remaining_files is not None and remaining_files <= 0:
            coverage, stopped_at, budget_reason = "partial", "max_files", "max_files"
            break
        remaining_seconds = scan_budget.max_seconds - (time.monotonic() - started)
        if remaining_seconds <= 0:
            coverage, stopped_at, budget_reason = "partial", "max_seconds", "max_seconds"
            break
        remaining_bytes = scan_budget.max_bytes - bytes_hashed
        if remaining_bytes <= 0:
            coverage, stopped_at, budget_reason = "partial", "max_bytes", "max_bytes"
            break
        page_budget = ScanBudget(
            max_files=(
                None
                if remaining_files is None
                else max(1, min(page_limit, remaining_files))
            ),
            max_bytes=remaining_bytes,
            max_seconds=max(1e-6, remaining_seconds),
            per_file_seconds=scan_budget.per_file_seconds,
        )
        try:
            page = scan_library_artifacts(
                source,
                after=cursor,
                limit=page_limit,
                budget=page_budget,
                cancel=cancel,
            )
        except LibraryAuditError as error:
            # The ledger could not be opened at all: that is not a finding
            # about a row, so it is a typed stop instead of a traceback.
            ledger_error = getattr(error, "code", None) or "ledger_unreadable"
            coverage = "error"
            stopped_at = "ledger_error"
            break
        pages += 1
        page_stats = page.get("stats") or {}
        page_rows = list(page.get("rows") or [])
        _collect(page_rows, findings=findings, rows=rows)
        _sum_page_stats(counters, page_stats, unknown_reasons)
        findings_omitted, rows_omitted = _trim(
            findings, rows, findings_omitted, rows_omitted
        )
        bytes_hashed += int(page_stats.get("bytes_hashed") or 0)
        page_seconds += float(page_stats.get("seconds") or 0)
        if library_rows is None:
            recorded = page_stats.get("library_rows")
            if type(recorded) is int:
                library_rows = recorded
                page_guard = recorded // page_limit + 2
        ledger = page.get("ledger") or {}
        if ledger.get("state") == "error" or page.get("coverage") == "error":
            ledger_error = str(ledger.get("code") or "ledger_error")
            next_cursor = _cursor_of(page.get("next_cursor")) or cursor
            coverage, stopped_at = "error", "ledger_error"
            break
        next_cursor = _cursor_of(page.get("next_cursor")) or cursor
        reason = page_stats.get("budget_exhausted_reason")
        if reason == "cancel_requested" or gates():
            coverage, stopped_at, budget_reason = "cancelled", "cancel", "cancel_requested"
            break
        if reason in _BUDGET_REASONS:
            coverage = "partial"
            stopped_at = str(page_stats.get("stopped_at") or reason)
            budget_reason = str(reason)
            break
        if page.get("coverage") == "empty":
            # The engine says "empty" for two different facts: the ledger holds
            # no delivered artifact at all, or this cursor already sits past its
            # end.  Only the first one is an answer about the library *and* the
            # walk: a full walk that has already examined pages reached the end
            # of the ledger, and a continuation that lands here learned nothing
            # about the library.
            if walk == "full_walk" and (pages > 1 or cursor is not None):
                coverage, stopped_at = "complete", None
            else:
                coverage, stopped_at = "empty", None
            break
        if walk in ("continuation", "page"):
            coverage = "partial" if page_rows else "empty"
            stopped_at = "single_page"
            break
        if not page.get("truncated"):
            coverage = "complete"
            stopped_at = None
            break
        if next_cursor is None or next_cursor == cursor:
            # No cursor movement means the next pass would examine the same
            # rows again; stopping here is the only honest answer.
            coverage, stopped_at = "partial", "no_progress"
            break
        cursor = next_cursor
        if pages >= page_guard:
            coverage, stopped_at = "partial", "page_guard"
            break

    findings_count = sum(counters[name] for name in FINDING_VERDICTS)
    unknown = counters["unknown"]
    exit_code = _exit_code(
        walk, coverage, findings_count, unknown, unknown_reasons, allow_unknown
    )
    stats = {name: counters[name] for name in _COUNTERS}
    stats.update(
        {
            "library_rows": library_rows,
            "unknown_reasons": {
                key: unknown_reasons[key] for key in sorted(unknown_reasons)
            },
            "bytes_hashed": bytes_hashed,
            "seconds": round(page_seconds, 6),
            "elapsed": round(time.monotonic() - started, 6),
            "budget_exhausted_reason": budget_reason,
            "stopped_at": stopped_at,
            "pages_examined": pages,
            "findings": findings_count,
            "findings_omitted": findings_omitted,
        }
    )
    ledger = {
        "state": "ok" if ledger_error is None else "error",
        "code": ledger_error,
        "last_safe_cursor": list(next_cursor) if next_cursor is not None else None,
    }
    return {
        "question": "library_scan",
        "scope": walk,
        "coverage": coverage,
        "pages_examined": pages,
        "library_rows": library_rows,
        "examined": counters["examined"],
        "stats": stats,
        "findings": findings,
        "findings_count": findings_count,
        "findings_omitted": findings_omitted,
        "rows": rows,
        "rows_omitted": rows_omitted,
        "unknown_reasons": stats["unknown_reasons"],
        "next_cursor": list(next_cursor) if next_cursor is not None else None,
        "resume": _resume(walk, coverage, exit_code, next_cursor),
        "exit_code": exit_code,
        "ledger": ledger,
        "mapping": mapping_state,
        "roots": root_summary,
        "started_at": started_at,
        "finished_at": _now(),
        "snapshot": "one_short_read_transaction_per_page",
        "note": "跨页遍历是多次短读，不是全库同一时刻的快照。",
    }


# ---------------------------------------------------------------------------
# Roots: the saved mapping, bound without mutating the caller
# ---------------------------------------------------------------------------


def bind_output_roots(resolver, roots):
    """A resolver that also knows roots; the original stays unchanged."""
    if not isinstance(resolver, PathResolver):
        raise ContractError("The scan needs a PathResolver")
    if not roots:
        return resolver
    if not isinstance(roots, dict):
        raise ContractError("roots must be a {root_id: path} mapping")
    merged = dict(resolver.external_roots)
    merged.update({str(key): path for key, path in roots.items()})
    try:
        return PathResolver(AppPaths(resolver.project_root), merged)
    except ContractError:
        raise ScanMappingError("location_mapping_invalid") from None


def _roots_for(project_root, roots):
    """(mapping to bind, how it was obtained) for this walk."""
    if roots is not None:
        if not isinstance(roots, dict):
            raise ContractError("roots must be a {root_id: path} mapping")
        return dict(roots), "explicit"
    if project_root is None:
        return {}, "none"
    try:
        loaded = load_output_roots(project_root)
    except LocationError as error:
        raise ScanMappingError(
            getattr(error, "code", "location_mapping_invalid")
        ) from None
    return loaded, ("file" if loaded else "absent")


def _bind_source(source, roots):
    if isinstance(source, (tuple, list)):
        if len(source) != 2:
            raise ContractError("Audit source must be (db_path, resolver)")
        database_path, resolver = source
        return (database_path, bind_output_roots(resolver, roots))
    raise ContractError("External roots need a (db_path, resolver) scan source")


def _root_summary(roots) -> dict:
    """What the mapping says, without creating or probing anything."""
    summary: dict[str, dict] = {}
    for key in sorted(roots):
        path = Path(roots[key])
        try:
            state = "ok" if path.is_dir() else "missing"
        except OSError:
            state = "missing"
        summary[str(key)] = {"path": str(path), "state": state}
    return summary


# ---------------------------------------------------------------------------
# Accumulation and severity
# ---------------------------------------------------------------------------


def _collect(page_rows, *, findings, rows):
    """Keep the listed rows; the engine already counted them for us."""
    for row in page_rows:
        rows.append(row)
        if str(row.get("verdict") or "") in FINDING_VERDICTS:
            findings.append(row)


def _sum_page_stats(counters, page_stats, unknown_reasons):
    """One page's own counters are the authority; never recount its rows.

    The engine counts what it examined, and its counters include the row that
    stopped the pass.  Recounting here would either double count or invent a
    vocabulary of its own -- which is exactly how a "match" once became an
    "unknown" in a draft of this layer.
    """
    for name in _COUNTERS:
        value = page_stats.get(name)
        if type(value) is int:
            counters[name] += value
    reasons = page_stats.get("unknown_reasons")
    if isinstance(reasons, dict):
        for key, value in reasons.items():
            if type(value) is int:
                unknown_reasons[str(key)] = unknown_reasons.get(str(key), 0) + value


def _trim(findings, rows, findings_omitted, rows_omitted):
    """Keep the listed rows bounded; the counters stay exact."""
    if len(findings) > _MAX_LISTED_FINDINGS:
        findings_omitted += len(findings) - _MAX_LISTED_FINDINGS
        del findings[_MAX_LISTED_FINDINGS:]
    if len(rows) > _MAX_LISTED_ROWS:
        rows_omitted += len(rows) - _MAX_LISTED_ROWS
        del rows[_MAX_LISTED_ROWS:]
    return findings_omitted, rows_omitted


def _exit_code(walk, coverage, findings_count, unknown, unknown_reasons, allow_unknown):
    """Contract v6 section 3.3, applied once to the whole walk."""
    if coverage == "error":
        return _EXIT_UNREADABLE
    if findings_count:
        return _EXIT_FINDINGS
    if coverage == "empty":
        # An empty ledger from the head is a complete answer; an empty page
        # after a cursor only says the cursor is at the end.
        return _EXIT_OK if walk == "full_walk" else _EXIT_INCOMPLETE
    if coverage != "complete" or walk != "full_walk":
        return _EXIT_INCOMPLETE
    if unknown:
        if (
            allow_unknown
            and unknown_reasons
            and set(unknown_reasons) <= LEGACY_UNKNOWN_REASONS
        ):
            return _EXIT_OK
        return _EXIT_INCOMPLETE
    return _EXIT_OK


def _resume(walk, coverage, exit_code, next_cursor):
    prefix = walk == "full_walk" and coverage != "error"
    if exit_code == _EXIT_OK:
        state = "not_needed"
    elif coverage == "error":
        state, prefix = "blocked", False
    elif next_cursor is not None:
        state = "at_cursor"
    else:
        state, prefix = "stopped", False
    return {"state": state, "complete_prefix": prefix, "exit_code": exit_code}


# ---------------------------------------------------------------------------
# Argument validation: the same vocabulary the page API rejects
# ---------------------------------------------------------------------------


def _kind(value):
    if value not in _KINDS:
        raise ContractError("kind must be full_walk, continuation or page")
    return value


def _limit(value):
    if type(value) is not int or not 1 <= value <= MAX_SCAN_LIMIT:
        raise ContractError("limit must be between 1 and %d" % MAX_SCAN_LIMIT)
    return value


def _budget(value):
    if value is None:
        return ScanBudget()
    if not isinstance(value, ScanBudget):
        raise ContractError("budget must be a ScanBudget")
    return value


def _cursor(value):
    if value is None:
        return None
    if (
        isinstance(value, (tuple, list))
        and len(value) == 2
        and all(isinstance(item, str) for item in value)
    ):
        return (value[0], value[1])
    raise ContractError("after must be a (completed_at, artifact_id) pair")


def _cursor_of(value):
    if value is None:
        return None
    try:
        return _cursor(list(value))
    except (ContractError, TypeError):
        return None


def _cancel(value):
    if value is None:
        return lambda: False
    is_set = getattr(value, "is_set", None)
    if not callable(is_set):
        raise ContractError("cancel must provide is_set()")

    def gates():
        flag = is_set()
        if type(flag) is not bool:
            raise ContractError("cancel must return boolean")
        return flag

    return gates


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


__all__ = [
    "FINDING_VERDICTS",
    "LEGACY_UNKNOWN_REASONS",
    "SCAN_COVERAGES",
    "ScanMappingError",
    "bind_output_roots",
    "scan_library_report",
]
