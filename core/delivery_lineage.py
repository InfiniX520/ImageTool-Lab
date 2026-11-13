"""Read-only successor resolution for superseded deliveries.

Recovery may retire another task's hold with a deliberate supersede.  The old
job keeps its historical rows; its next active batch must nevertheless be able
to find the newer delivery that now owns the same page.  This module resolves
that link strictly by page identity and quality, never by "any cancelled row".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_SUPERSEDE_HOPS = 4

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


class LineageError(Exception):
    """A successor link would be ambiguous or cyclic."""


@dataclass(frozen=True, slots=True)
class Successor:
    delivery_id: str
    job_id: str
    batch_id: str
    state: str
    generation: int
    artifact_id: str | None

    @property
    def is_completed(self) -> bool:
        return self.state == "completed" and self.artifact_id is not None


def is_supersede_code(value: object) -> bool:
    return isinstance(value, str) and value in SUPERSEDE_CODES


def _row_artifact(conn: Any, delivery_id: str) -> str | None:
    row = conn.execute(
        "SELECT artifact_id FROM artifacts WHERE delivery_id = ?", (delivery_id,)
    ).fetchone()
    return None if row is None else str(row["artifact_id"])


def _candidate(conn: Any, row: Any) -> Successor:
    state = str(row["state"])
    artifact_id = None
    if state == "completed":
        artifact_id = _row_artifact(conn, str(row["delivery_id"]))
    return Successor(
        delivery_id=str(row["delivery_id"]),
        job_id=str(row["job_id"]),
        batch_id=str(row["current_batch_id"]),
        state=state,
        generation=int(row["generation"]),
        artifact_id=artifact_id,
    )


def _recorded_successor(conn: Any, delivery: Any) -> Successor | None:
    """Follow an explicit handover event when the artifact row is older.

    A reuse handover links the current batch to an artifact that may predate
    the retired hold, so "newer rowid" alone cannot find it.  The
    recovery_owner_transfer event records exactly which artifact took over.
    """
    delivery_id = str(delivery["delivery_id"])
    rows = conn.execute(
        "SELECT safe_payload FROM events WHERE kind = 'recovery_owner_transfer' "
        "AND safe_payload LIKE ? ORDER BY event_seq DESC LIMIT 8",
        (f'%"delivery_id":"{delivery_id}"%',),
    ).fetchall()
    for row in rows:
        try:
            info = json.loads(row["safe_payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(info, dict) or str(info.get("delivery_id")) != delivery_id:
            continue
        artifact_id = info.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            continue
        artifact = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if artifact is None:
            continue
        if (
            str(artifact["illust_id"]) != str(delivery["illust_id"])
            or int(artifact["page_index"]) != int(delivery["page_index"])
            or str(artifact["output_key"]) != str(delivery["output_key"])
        ):
            continue
        candidate = conn.execute(
            "SELECT * FROM deliveries WHERE delivery_id = ?",
            (str(artifact["delivery_id"]),),
        ).fetchone()
        if candidate is not None and str(candidate["state"]) == "completed":
            return _candidate(conn, candidate)
    return None


def resolve_successor(conn: Any, delivery: Any, *, hops: int = 0) -> Successor | None:
    """Find the newest delivery that owns the same page, or None.

    Candidates are newer rows with the same illust/page/output_key and quality
    key.  A successor may belong to another job (a handover is exactly that),
    so page identity and quality decide, and rowid decides "newer": the per-job
    generation counter is not comparable across tasks.  A completed candidate
    wins; otherwise the newest active follow-up is returned.  A superseded
    candidate is followed, bounded by MAX_SUPERSEDE_HOPS.
    """
    if hops > MAX_SUPERSEDE_HOPS:
        # A chain this deep is treated as "no resolvable successor"; recovery
        # then refuses without touching anything instead of crashing.
        return None
    rows = conn.execute(
        "SELECT * FROM deliveries WHERE illust_id = ? AND page_index = ? "
        "AND output_key = ? AND quality_key = ? AND rowid > ("
        "SELECT rowid FROM deliveries WHERE delivery_id = ?"
        ") ORDER BY rowid DESC LIMIT 32",
        (
            str(delivery["illust_id"]),
            int(delivery["page_index"]),
            str(delivery["output_key"]),
            str(delivery["quality_key"]),
            str(delivery["delivery_id"]),
        ),
    ).fetchall()
    completed: Successor | None = None
    followup: Successor | None = None
    for row in rows:
        state = str(row["state"])
        code = row["error_code"]
        if state == "cancelled" and is_supersede_code(code):
            nested = resolve_successor(conn, row, hops=hops + 1)
            if nested is not None and completed is None:
                if nested.is_completed:
                    completed = nested
                elif followup is None:
                    followup = nested
            continue
        if state in TERMINAL_SUCCESS:
            candidate = _candidate(conn, row)
            if candidate.is_completed and completed is None:
                completed = candidate
        elif state in ACTIVE_FOLLOWUP and followup is None:
            followup = _candidate(conn, row)
    if completed is not None:
        return completed
    if followup is not None:
        return followup
    return _recorded_successor(conn, delivery)
