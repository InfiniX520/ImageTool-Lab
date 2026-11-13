"""Read-only successor resolution for superseded deliveries.

Recovery may retire another task's hold with a deliberate supersede.  The old
job keeps its historical rows; its next active batch must nevertheless be able
to find the newer delivery that now owns the same page.  This module resolves
that link strictly by page identity and quality, never by "any cancelled row".

The frozen contract (U1A_STATE_CONTRACT_V3 section 3) fixes one authority
order, and this module is its only implementation:

1. the delivery's own recovery_owner_transfer event, newest first: the
   artifact it names may be older than the retired row, so rowid alone can
   never express that handover;
2. the newest completed row newer than this one on the same page and quality,
   read through one bounded window;
3. the newest active follow-up in that same window;
4. the bounded superseded chain inside that window.

The walk reads its candidates once.  The previous implementation re-issued the
"later rows on this page" query for every superseded row it met, so a page
with 32 retired generations cost 82,901 statements for one resolution; the
cost here is at most MAX_LINEAGE_QUERIES statements and one visit per node.

Nothing in this module touches the file system.  It is safe to call inside a
writer transaction, and both the read-only preflight and the write-time check
must call it so they can never disagree about the authority order.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_SUPERSEDE_HOPS = 4
"""Legacy depth bound, kept for callers.

The walk no longer recurses, so a chain is bounded by MAX_SUPERSEDE_NODES and
MAX_LINEAGE_QUERIES instead of by depth.  The constant stays exported because
it documents the historical limit and because callers may still pass hops to
resolve_successor.
"""

MAX_SUPERSEDE_NODES = 64
"""Most candidate rows one resolution may examine before it refuses."""

MAX_LINEAGE_QUERIES = 4
"""Most statements one resolution may issue."""

WINDOW_LIMIT = 64
"""Rows read by the single "newer rows on this page" statement."""

EVENT_LIMIT = 8
"""Handover events read for one delivery, newest first."""

TERMINAL_SUCCESS = ("completed",)
ACTIVE_FOLLOWUP = (
    "pending",
    "suspended",
    "retry_wait",
    "needs_verification",
    "transferring",
    "validating",
    "prepared",
    "recovery_needed",
)
SUPERSEDE_CODES = (
    "superseded_by_recovery_reuse",
    "superseded_by_recovery_repair",
    "superseded_by_new_task",
)

_EVENT_KIND = "recovery_owner_transfer"
_SUPPORTED_MEDIA = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_HEX = frozenset("0123456789abcdef")

_EVENT_SQL = (
    "SELECT safe_payload FROM events WHERE kind = ? AND safe_payload LIKE ? "
    "ORDER BY event_seq DESC LIMIT ?"
)
_EVENT_TARGET_SQL = (
    "SELECT a.artifact_id AS artifact_id, a.illust_id AS illust_id, "
    "a.page_index AS page_index, a.output_key AS output_key, "
    "a.verification AS verification, a.size AS size, a.sha256 AS sha256, "
    "a.media_type AS media_type, a.quality_json AS artifact_quality_json, "
    "d.delivery_id AS owner_delivery_id, d.job_id AS owner_job_id, "
    "d.current_batch_id AS owner_batch_id, d.generation AS owner_generation, "
    "d.state AS owner_state "
    "FROM artifacts AS a JOIN deliveries AS d ON d.delivery_id = a.delivery_id "
    "WHERE a.artifact_id = ?"
)
# The delivery table is intentionally not aliased: the single window read keeps
# the historical statement shape ("AND rowid > ("), so a trace-based audit can
# count window scans and prove the same row is never expanded twice.
_WINDOW_SQL = (
    "SELECT *, ("
    "SELECT a.artifact_id FROM artifacts AS a "
    "WHERE a.delivery_id = deliveries.delivery_id"
    ") AS artifact_id "
    "FROM deliveries "
    "WHERE illust_id = ? AND page_index = ? AND output_key = ? AND quality_key = ? "
    "AND rowid > (SELECT rowid FROM deliveries WHERE delivery_id = ?) "
    "ORDER BY rowid DESC LIMIT ?"
)


class LineageError(Exception):
    """The caller handed over a row this module cannot interpret."""


class _BudgetExceeded(Exception):
    """One resolution ran out of its statement budget."""


@dataclass(frozen=True, slots=True)
class Successor:
    """The delivery that now owns the page, and the artifact it owns."""

    delivery_id: str
    job_id: str
    batch_id: str
    state: str
    generation: int
    artifact_id: str | None

    @property
    def is_completed(self) -> bool:
        return self.state == "completed" and self.artifact_id is not None


@dataclass(frozen=True, slots=True)
class LineageResult:
    """One resolution, with the authority path and the cost it took.

    reason is the branch that decided the result: "event", "rowid_completed",
    "rowid_active", "supersede_chain", "none", "event_target_unusable" or
    "budget_exhausted".  "cycle" is reserved for a future multi-hop walk: this
    implementation follows at most one handover hop and one window, so it can
    never loop, and a handover that names its own delivery is refused as
    event:self_reference.  Callers that only need the successor keep using
    resolve_successor; callers that must explain a refusal use
    reason/diagnostics instead of guessing.
    """

    successor: Successor | None
    reason: str
    diagnostics: tuple[str, ...] = ()
    queries: int = 0
    nodes: int = 0


@dataclass(frozen=True, slots=True)
class _PageIdentity:
    delivery_id: str
    illust_id: str
    page_index: int
    output_key: str
    quality_key: str
    quality: tuple[bool, int] | None


def is_supersede_code(value: object) -> bool:
    return isinstance(value, str) and value in SUPERSEDE_CODES


def resolve_successor(conn: Any, delivery: Any, *, hops: int = 0) -> Successor | None:
    """Return the delivery that now owns this page, or None.

    hops is accepted for compatibility with the previous recursive signature.
    The walk is iterative now, so the value no longer changes the depth of
    anything; it is validated and otherwise unused.
    """
    return resolve_successor_ex(conn, delivery, hops=hops).successor


def resolve_successor_ex(conn: Any, delivery: Any, *, hops: int = 0) -> LineageResult:
    """Resolve one successor and report how it was decided.

    The function performs at most MAX_LINEAGE_QUERIES statements, visits every
    candidate once and never caches anything across calls: a caller that
    resolves in a preflight and again inside its write transaction always sees
    the current rows, and a candidate that appeared in between produces a
    different successor, which the caller reports as a retry.
    """
    if type(hops) is not int or hops < 0:
        raise LineageError("hops must be a non-negative integer")
    page = _page_identity(delivery)
    probe = _Probe(conn)
    try:
        recorded = probe.recorded_successor(page)
        if recorded is not None:
            return recorded
        return probe.window_successor(page)
    except _BudgetExceeded:
        probe.diagnostics.append("statement_budget")
        return probe.result(None, "budget_exhausted")


class _Probe:
    """One resolution: its connection, budget and diagnostics."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.queries = 0
        self.nodes = 0
        self.diagnostics: list[str] = []
        self.visited: set[str] = set()

    def result(
        self, successor: Successor | None, reason: str, *extra: str
    ) -> LineageResult:
        diagnostics = tuple(self.diagnostics) + tuple(extra)
        return LineageResult(
            successor=successor,
            reason=reason,
            diagnostics=diagnostics,
            queries=self.queries,
            nodes=self.nodes,
        )

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[Any]:
        if self.queries >= MAX_LINEAGE_QUERIES:
            raise _BudgetExceeded()
        self.queries += 1
        return self._conn.execute(sql, parameters).fetchall()

    # -- 1. exact handover ---------------------------------------------
    def recorded_successor(self, page: _PageIdentity) -> LineageResult | None:
        """Follow the newest usable handover event, or fall through.

        An event without artifact_id records that ownership moved to a live
        successor (a hold or a queued repair); it names no artifact, so it
        cannot decide this page and the window still has to be read.  An event
        that does name an artifact decides the page: if none of the named
        targets is usable the resolution refuses instead of silently falling
        back to "newest row", because that would discard an explicit decision.
        """
        rows = self.execute(
            _EVENT_SQL,
            (_EVENT_KIND, f'%"delivery_id":"{page.delivery_id}"%', EVENT_LIMIT),
        )
        named = 0
        for row in rows:
            payload = _event_payload(row)
            if payload is None:
                # A payload that mentions this delivery but cannot be parsed
                # is a data fault, not an absent record.
                return self.result(None, "event_target_unusable", "corrupt_event")
            if str(payload.get("delivery_id")) != page.delivery_id:
                # The LIKE also matches payloads that merely contain the id.
                continue
            self.nodes += 1
            target = payload.get("artifact_id")
            if target is None:
                self.diagnostics.append("event_without_artifact")
                continue
            if not isinstance(target, str) or not target:
                return self.result(None, "event_target_unusable", "corrupt_event_target")
            named += 1
            successor, problem = self.event_target(target, page)
            if successor is not None:
                return self.result(successor, "event")
            self.diagnostics.append(f"event:{problem}")
        if named:
            # Handover events exist and name artifacts, but none is usable.
            return self.result(None, "event_target_unusable")
        return None

    def event_target(
        self, artifact_id: str, page: _PageIdentity
    ) -> tuple[Successor | None, str | None]:
        """Validate one handover target against the page it must own."""
        rows = self.execute(_EVENT_TARGET_SQL, (artifact_id,))
        if not rows:
            return None, "artifact_missing"
        row = rows[0]
        if (
            str(row["illust_id"]) != page.illust_id
            or int(row["page_index"]) != page.page_index
            or str(row["output_key"]) != page.output_key
        ):
            return None, "page_mismatch"
        if not _artifact_is_usable(row):
            return None, "artifact_invalid"
        if page.quality is None:
            return None, "quality_unreadable"
        if _quality_tokens(row["artifact_quality_json"]) != page.quality:
            return None, "quality_mismatch"
        owner = str(row["owner_delivery_id"])
        if owner == page.delivery_id:
            return None, "self_reference"
        if str(row["owner_state"]) != "completed":
            return None, "target_not_completed"
        return (
            Successor(
                delivery_id=owner,
                job_id=str(row["owner_job_id"]),
                batch_id=str(row["owner_batch_id"]),
                state="completed",
                generation=int(row["owner_generation"]),
                artifact_id=artifact_id,
            ),
            None,
        )

    # -- 2..4. window ---------------------------------------------------
    def window_successor(self, page: _PageIdentity) -> LineageResult:
        """Read the newer rows once and decide by the frozen precedence."""
        rows = self.execute(
            _WINDOW_SQL,
            (
                page.illust_id,
                page.page_index,
                page.output_key,
                page.quality_key,
                page.delivery_id,
                WINDOW_LIMIT,
            ),
        )
        self.nodes += len(rows)
        completed: Successor | None = None
        completed_behind_supersede = 0
        active: Successor | None = None
        superseded = 0
        for row in rows:
            key = str(row["delivery_id"])
            if key in self.visited:
                # Rowid order reaches a row once; a duplicate delivery id here
                # would mean a corrupt table.
                self.diagnostics.append(f"duplicate:{key}")
                continue
            self.visited.add(key)
            state = str(row["state"])
            artifact_id = row["artifact_id"]
            if state in TERMINAL_SUCCESS:
                if artifact_id is not None and completed is None:
                    completed = _successor(row)
                    completed_behind_supersede = superseded
                continue
            if state in ACTIVE_FOLLOWUP:
                if active is None:
                    active = _successor(row)
                continue
            if state == "cancelled" and is_supersede_code(row["error_code"]):
                superseded += 1
        if completed is not None:
            reason = (
                "supersede_chain" if completed_behind_supersede else "rowid_completed"
            )
            return self.result(completed, reason)
        if active is not None:
            return self.result(active, "rowid_active")
        if len(rows) >= WINDOW_LIMIT:
            # The window is full and nothing terminal was found: older rows
            # could still decide the page, so refusing is the only honest
            # answer.  Never guess a successor past the budget.
            return self.result(None, "budget_exhausted", "window_full")
        if superseded:
            self.diagnostics.append(f"superseded_only:{superseded}")
        return self.result(None, "none")


def _field(row: Any, name: str, default: Any = None) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError):
        return default


def _event_payload(row: Any) -> dict[str, Any] | None:
    text = _field(row, "safe_payload")
    if not isinstance(text, str):
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _quality_tokens(text: object) -> tuple[bool, int] | None:
    """The byte-quality fields a handover target must share, or None."""
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    original = value.get("original")
    version = value.get("validation_policy_version")
    if type(original) is not bool or type(version) is not int:
        return None
    return (original, version)


def _artifact_is_usable(row: Any) -> bool:
    sha = _field(row, "sha256")
    size = _field(row, "size")
    return (
        str(_field(row, "verification")) == "structure_checked"
        and type(size) is int
        and size > 0
        and isinstance(sha, str)
        and len(sha) == 64
        and all(character in _HEX for character in sha)
        and _field(row, "media_type") in _SUPPORTED_MEDIA
    )


def _successor(row: Any) -> Successor:
    artifact_id = row["artifact_id"]
    return Successor(
        delivery_id=str(row["delivery_id"]),
        job_id=str(row["job_id"]),
        batch_id=str(row["current_batch_id"]),
        state=str(row["state"]),
        generation=int(row["generation"]),
        artifact_id=None if artifact_id is None else str(artifact_id),
    )


def _page_identity(row: Any) -> _PageIdentity:
    try:
        delivery_id = str(row["delivery_id"])
        illust_id = str(row["illust_id"])
        page_index = int(row["page_index"])
        output_key = str(row["output_key"])
        quality_key = str(row["quality_key"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LineageError("Unusable delivery row") from exc
    return _PageIdentity(
        delivery_id=delivery_id,
        illust_id=illust_id,
        page_index=page_index,
        output_key=output_key,
        quality_key=quality_key,
        quality=_quality_tokens(_field(row, "quality_json")),
    )
