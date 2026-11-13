"""Read-only successor resolution for superseded deliveries.

Recovery may retire another task's hold with a deliberate supersede.  The old
job keeps its historical rows; its next active batch must nevertheless be able
to find the newer delivery that now owns the same page.  This module resolves
that link strictly by page identity and quality, never by "any cancelled row".
"""
from __future__ import annotations

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


def resolve_successor(conn: Any, delivery: Any, *, hops: int = 0) -> Successor | None:
    """Find the newest delivery that owns the same page, or None.

    Candidates are newer generations with the same illust/page/output_key and
    quality key.  A completed candidate wins; otherwise the newest active
    follow-up is returned so the caller can decide whether to hold or repair.
    A superseded candidate is followed, bounded by MAX_SUPERSEDE_HOPS.
    """
    if hops > MAX_SUPERSEDE_HOPS:
        raise LineageError("successor chain is too deep")
    rows = conn.execute(
        "SELECT * FROM deliveries WHERE job_id = ? AND illust_id = ? "
        "AND page_index = ? AND output_key = ? AND quality_key = ? "
        "AND generation > ? ORDER BY generation DESC LIMIT 32",
        (
            str(delivery["job_id"]),
            str(delivery["illust_id"]),
            int(delivery["page_index"]),
            str(delivery["output_key"]),
            str(delivery["quality_key"]),
            int(delivery["generation"]),
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
    return completed if completed is not None else followup
