"""Read-only successor resolution for superseded deliveries.

Recovery may retire another task's hold with a deliberate supersede.  The old
job keeps its historical rows; its next active batch must nevertheless be able
to find the newer delivery that now owns the same page.  This module resolves
that link strictly by page identity and quality, never by "any cancelled row".

The frozen contract (U1A_STATE_CONTRACT_V4 section 7) fixes one authority
order, and this module is its only implementation.  Every node of the bounded
node set is decided by the same order:

1. the node's own recovery_owner_transfer event, newest usable first: the
   artifact it names may be older than the retired row, so rowid alone can
   never express that handover;
2. the newest completed row newer than this one on the same page and quality,
   read through one bounded window;
3. the newest active follow-up in that same window;
4. the bounded superseded chain inside that window, where each superseded node
   contributes its own recorded handover.

The node set is the anchor, the rows of one window read and the delivery rows
that own the named handover artifacts.  The previous implementation only
looked for a handover on the anchor, so an intermediate superseded row that
pointed at an earlier completed delivery was silently dropped: the chain read
as "superseded only" and the public recovery refused a legal history.  The
handover events of the whole node set are now read in one statement and the
walk visits every node once.

The walk reads its candidates once.  An earlier recursive implementation
re-issued the "later rows on this page" query for every superseded row it met,
so a page with 32 retired generations cost 82,901 statements for one
resolution; the cost here is at most MAX_LINEAGE_QUERIES statements and one
visit per node, and a budget that runs out is reported as budget_exhausted
instead of being mistaken for "no successor".

Nothing in this module touches the file system.  It is safe to call inside a
writer transaction, and both the read-only preflight and the write-time check
must call it so they can never disagree about the authority order.

Both paths use one quality scope: the window statement selects rows whose
quality_key equals the resolved delivery's quality_key, and a handover target
is usable only when its owning delivery shares that same quality_key.  A
handover into another quality scope is therefore refused instead of silently
accepted where the window could never reach it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

MAX_SUPERSEDE_HOPS = 4
"""Legacy depth bound, kept for callers.

The walk is iterative, so a chain is bounded by MAX_SUPERSEDE_NODES and
MAX_LINEAGE_QUERIES instead of by depth.  The constant stays exported because
it documents the historical limit and because callers may still pass hops to
resolve_successor.
"""

MAX_SUPERSEDE_NODES = 64
"""Most nodes (events plus window rows) one resolution may examine."""

MAX_LINEAGE_QUERIES = 4
"""Most statements one resolution may issue."""

WINDOW_LIMIT = 64
"""Rows read by the single "newer rows on this page" statement."""

EVENT_LIMIT = 8
"""Handover events considered per delivery, newest first."""

MAX_EVENTS = 64
"""Handover events read for the whole node set by the batched statement."""

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

# One statement covers the whole node set: every delivery id is matched against
# the serialized payload, and only the newest MAX_EVENTS rows are parsed.
_EVENT_SQL = (
    "SELECT safe_payload FROM events WHERE kind = ? AND ({matches}) "
    "ORDER BY event_seq DESC LIMIT ?"
)
_EVENT_TARGET_SQL = (
    "SELECT a.artifact_id AS artifact_id, a.illust_id AS illust_id, "
    "a.page_index AS page_index, a.output_key AS output_key, "
    "a.verification AS verification, a.size AS size, a.sha256 AS sha256, "
    "a.media_type AS media_type, a.quality_json AS artifact_quality_json, "
    "d.delivery_id AS owner_delivery_id, d.job_id AS owner_job_id, "
    "d.current_batch_id AS owner_batch_id, d.generation AS owner_generation, "
    "d.quality_key AS owner_quality_key, d.state AS owner_state "
    "FROM artifacts AS a JOIN deliveries AS d ON d.delivery_id = a.delivery_id "
    "WHERE a.artifact_id IN ({placeholders})"
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


class _EventCapExceeded(Exception):
    """More handover events exist than one batched read may examine.

    This is a budget result, not an absent record: the oldest nodes of the set
    may still hold the decisive handover, so the caller must refuse with
    budget_exhausted instead of reporting "no successor".
    """


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
    implementation follows at most one recorded handover per node and one
    window, keeps a visited set, and refuses a handover that names its own
    delivery as event:self_reference, so it can never loop.  Callers that only
    need the successor keep using resolve_successor; callers that must explain
    a refusal use reason/diagnostics instead of guessing.

    nodes counts the handover events and window rows this resolution examined;
    events counts the event rows and targets the distinct artifact rows that
    were validated.
    """

    successor: Successor | None
    reason: str
    diagnostics: tuple[str, ...] = ()
    queries: int = 0
    nodes: int = 0
    events: int = 0
    targets: int = 0


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
    node once and never caches anything across calls: a caller that resolves in
    a preflight and again inside its write transaction always sees the current
    rows, and a candidate that appeared in between produces a different
    successor, which the caller reports as a retry.
    """
    if type(hops) is not int or hops < 0:
        raise LineageError("hops must be a non-negative integer")
    page = _page_identity(delivery)
    probe = _Probe(conn)
    try:
        return _resolve(probe, page)
    except _EventCapExceeded:
        probe.diagnostics.append("event_cap")
        return probe.result(None, "budget_exhausted")
    except _BudgetExceeded:
        probe.diagnostics.append("statement_budget")
        return probe.result(None, "budget_exhausted")


def _resolve(probe: "_Probe", page: _PageIdentity) -> LineageResult:
    # 1. The anchor's own handover decides before any rowid reasoning: the
    #    artifact it names may be older than the anchor itself.
    anchor_events, corrupt = probe.events_for((page.delivery_id,))
    if corrupt:
        return probe.result(None, "event_target_unusable", "corrupt_event")
    payloads = anchor_events.get(page.delivery_id, ())
    if payloads:
        named = _named_targets(payloads)
        targets = probe.event_targets(named) if named else {}
        decision = _decide_by_events(probe, page, payloads, targets, reason="event")
        if decision is not None:
            return decision

    # 2. One bounded window read supplies the rest of the node set.
    rows = probe.window_rows(page)
    if not rows:
        return probe.result(None, "none")
    completed, behind, active, superseded = _classify_window(probe, rows)
    if completed is not None:
        reason = "supersede_chain" if behind else "rowid_completed"
        return probe.result(completed, reason)
    if active is not None:
        return probe.result(active, "rowid_active")
    # 3. Only superseded rows can carry a decisive handover, so their events
    #    (and the artifacts they name) are read in one batch each.
    if superseded:
        node_events, corrupt = probe.events_for(superseded)
        if corrupt:
            return probe.result(None, "event_target_unusable", "corrupt_event")
        named = _named_targets(
            payload for payloads in node_events.values() for payload in payloads
        )
        targets = probe.event_targets(named) if named else {}
        for key in superseded:
            decision = _decide_by_events(
                probe,
                page,
                node_events.get(key, ()),
                targets,
                reason="supersede_chain",
                via=key,
            )
            if decision is None:
                continue
            if decision.successor is not None:
                return decision
            if decision.reason == "event_target_unusable":
                # A named but unusable handover is a refusal, never a silent
                # fallback to another completed row.
                return decision
    if len(rows) >= WINDOW_LIMIT:
        # The window is full and nothing terminal was found: older rows could
        # still decide the page, so refusing is the only honest answer.
        return probe.result(None, "budget_exhausted", "window_full")
    if superseded:
        probe.diagnostics.append(f"superseded_only:{len(superseded)}")
    return probe.result(None, "none")


class _Probe:
    """One resolution: its connection, budget and diagnostics."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.queries = 0
        self.nodes = 0
        self.events = 0
        self.targets = 0
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
            events=self.events,
            targets=self.targets,
        )

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[Any]:
        if self.queries >= MAX_LINEAGE_QUERIES:
            raise _BudgetExceeded()
        self.queries += 1
        return self._conn.execute(sql, parameters).fetchall()

    def _count_node(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_SUPERSEDE_NODES:
            # The node set is bounded even when a page has a long history.
            raise _BudgetExceeded()

    # -- handover events of the whole node set --------------------------
    def events_for(
        self, delivery_ids: Iterable[str]
    ) -> tuple[dict[str, tuple[dict[str, Any], ...]], bool]:
        """Read the handover events of several deliveries in one statement.

        Returns the parsed payloads per delivery id (newest first, at most
        EVENT_LIMIT each) and whether any matching payload could not be
        parsed.  A payload that mentions one of our ids but is corrupt is a
        data fault, not an absent record.

        A batch that has more rows than MAX_EVENTS sets event_cap_hit: the
        caller must report a budget result instead of deciding on the newest
        page alone, because an older node's decisive handover may have been
        cut off (the same "legal chain refused" harm as a missed edge).
        """
        ids = [
            value
            for value in dict.fromkeys(delivery_ids)
            if isinstance(value, str) and value
        ]
        if not ids:
            return {}, False
        matches = " OR ".join("safe_payload LIKE ?" for _ in ids)
        # One row beyond the cap proves that older handovers exist, so a batch
        # of exactly MAX_EVENTS rows is not reported as exhausted.
        parameters = (
            _EVENT_KIND,
            *(f'%"delivery_id":"{value}"%' for value in ids),
            MAX_EVENTS + 1,
        )
        rows = self.execute(_EVENT_SQL.format(matches=matches), parameters)
        if len(rows) > MAX_EVENTS:
            # The batch is longer than the budget: older nodes may still hold
            # the decisive handover, so this is a budget result, not "none".
            raise _EventCapExceeded()
        allowed = set(ids)
        grouped: dict[str, list[dict[str, Any]]] = {}
        corrupt = False
        for row in rows:
            payload = _event_payload(row)
            if payload is None:
                corrupt = True
                continue
            key = payload.get("delivery_id")
            if not isinstance(key, str) or key not in allowed:
                # The LIKE also matches payloads that merely contain the id.
                continue
            bucket = grouped.setdefault(key, [])
            if len(bucket) >= EVENT_LIMIT:
                continue
            bucket.append(payload)
            self.events += 1
            self._count_node()
        return {key: tuple(value) for key, value in grouped.items()}, corrupt

    def event_targets(self, artifact_ids: Iterable[str]) -> dict[str, Any]:
        """Fetch every named handover target in one statement."""
        ids = [
            value
            for value in dict.fromkeys(artifact_ids)
            if isinstance(value, str) and value
        ]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.execute(
            _EVENT_TARGET_SQL.format(placeholders=placeholders), tuple(ids)
        )
        found: dict[str, Any] = {}
        for row in rows:
            artifact_id = str(row["artifact_id"])
            found.setdefault(artifact_id, row)
            self.targets += 1
        return found

    # -- one bounded window ---------------------------------------------
    def window_rows(self, page: _PageIdentity) -> list[Any]:
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
        for _ in rows:
            self._count_node()
        return rows


def _decide_by_events(
    probe: _Probe,
    page: _PageIdentity,
    payloads: Iterable[dict[str, Any]],
    targets: dict[str, Any],
    *,
    reason: str = "event",
    via: str | None = None,
) -> LineageResult | None:
    """Decide one node by its own handover events, newest first.

    An event without artifact_id records that ownership moved to a live
    successor (a hold or a queued repair); it names no artifact, so it cannot
    decide this page on its own.  An event that does name an artifact decides
    the node: the first usable target wins, and if every named target is
    unusable the resolution refuses instead of silently falling back to
    "newest row", because that would discard an explicit decision.
    """
    named = 0
    for payload in payloads:
        target = payload.get("artifact_id")
        if target is None:
            probe.diagnostics.append("event_without_artifact")
            continue
        if not isinstance(target, str) or not target:
            return probe.result(None, "event_target_unusable", "corrupt_event_target")
        named += 1
        successor, problem = _validate_target(page, targets.get(target), target)
        if successor is not None:
            extra = () if via is None else (f"via:{via}",)
            return probe.result(successor, reason, *extra)
        probe.diagnostics.append(f"event:{problem}")
    if named:
        return probe.result(None, "event_target_unusable")
    return None


def _classify_window(
    probe: _Probe, rows: list[Any]
) -> tuple[Successor | None, int, Successor | None, list[str]]:
    """Classify one window read by the frozen precedence.

    Returns the newest completed row (and how many superseded rows preceded
    it), the newest active follow-up, and the superseded delivery ids in rowid
    order.  Only superseded rows can carry a decisive handover, so the caller
    reads their events after this pass instead of paying for them on every
    page.
    """
    completed: Successor | None = None
    completed_behind_supersede = 0
    active: Successor | None = None
    superseded: list[str] = []
    for row in rows:
        key = str(row["delivery_id"])
        if key in probe.visited:
            # Rowid order reaches a row once; a duplicate delivery id here
            # would mean a corrupt table.
            probe.diagnostics.append(f"duplicate:{key}")
            continue
        probe.visited.add(key)
        state = str(row["state"])
        artifact_id = row["artifact_id"]
        if state in TERMINAL_SUCCESS:
            if artifact_id is not None and completed is None:
                completed = _successor(row)
                completed_behind_supersede = len(superseded)
            continue
        if state in ACTIVE_FOLLOWUP:
            if active is None:
                active = _successor(row)
            continue
        if state == "cancelled" and is_supersede_code(row["error_code"]):
            superseded.append(key)
    return completed, completed_behind_supersede, active, superseded


def _validate_target(
    page: _PageIdentity, row: Any, artifact_id: str
) -> tuple[Successor | None, str | None]:
    """Validate one handover target against the page it must own.

    The target must live in the same quality scope as the delivery being
    resolved.  The window path can only ever offer rows with this exact
    quality_key, so accepting a handover into another quality scope would let
    the event path answer a question the window path never could: the two
    paths would not be the same authority order any more.
    """
    if row is None:
        return None, "artifact_missing"
    if (
        str(row["illust_id"]) != page.illust_id
        or int(row["page_index"]) != page.page_index
        or str(row["output_key"]) != page.output_key
    ):
        return None, "page_mismatch"
    if str(row["owner_quality_key"]) != page.quality_key:
        # Only the delivery row carries quality_key; an artifact row never
        # does, so this compares the target delivery, never the bytes.
        return None, "quality_key_mismatch"
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


def _named_targets(payloads: Iterable[dict[str, Any]]) -> list[str]:
    named: list[str] = []
    for payload in payloads:
        target = payload.get("artifact_id")
        if isinstance(target, str) and target:
            named.append(target)
    return named


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
