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


def resolve_successor(
    conn: Any, delivery: Any, *, hops: int = 0, _seen: frozenset[str] | None = None
) -> Successor | None:
    """Find the delivery that now owns the same page, or None.

    v2.1 order (the RF review showed a cross-job, older completed winner):

    1. the newest completed delivery for the same page and quality, in any
       job, whose artifact row still exists;
    2. a follow-up row created after the current one (rowid greater) that is
       still active;
    3. a newer cancelled row that carries a supersede code, followed with a
       bounded depth.

    History is never rewritten: the caller links only the current active batch.
    """
    if hops > MAX_SUPERSEDE_HOPS:
        raise LineageError("successor chain is too deep")
    current_id = str(delivery["delivery_id"])
    seen = set() if _seen is None else set(_seen)
    if current_id in seen:
        raise LineageError("successor chain is cyclic")
    seen.add(current_id)
    key = (
        str(delivery["illust_id"]),
        int(delivery["page_index"]),
        str(delivery["output_key"]),
        str(delivery["quality_key"]),
    )

    completed_rows = conn.execute(
        "SELECT * FROM deliveries WHERE illust_id = ? AND page_index = ? "
        "AND output_key = ? AND quality_key = ? AND delivery_id != ? "
        "AND state = 'completed' ORDER BY completed_at DESC, rowid DESC LIMIT 8",
        (*key, current_id),
    ).fetchall()
    for row in completed_rows:
        artifact_id = _row_artifact(conn, str(row["delivery_id"]))
        if artifact_id is not None:
            return _candidate(conn, row)

    current_rowid = conn.execute(
        "SELECT rowid FROM deliveries WHERE delivery_id = ?", (current_id,)
    ).fetchone()
    if current_rowid is not None:
        placeholders = ",".join("?" for _ in ACTIVE_FOLLOWUP)
        rows = conn.execute(
            "SELECT * FROM deliveries WHERE illust_id = ? AND page_index = ? "
            "AND output_key = ? AND quality_key = ? AND rowid > ? "
            f"AND state IN ({placeholders}) ORDER BY rowid DESC LIMIT 8",
            (*key, int(current_rowid[0]), *ACTIVE_FOLLOWUP),
        ).fetchall()
        for row in rows:
            if str(row["delivery_id"]) != current_id:
                return _candidate(conn, row)

    placeholders = ",".join("?" for _ in SUPERSEDE_CODES)
    rows = conn.execute(
        "SELECT * FROM deliveries WHERE illust_id = ? AND page_index = ? "
        "AND output_key = ? AND quality_key = ? AND delivery_id != ? "
        f"AND state = 'cancelled' AND error_code IN ({placeholders}) "
        "ORDER BY rowid DESC LIMIT 8",
        (*key, current_id, *SUPERSEDE_CODES),
    ).fetchall()
    for row in rows:
        nested = resolve_successor(conn, row, hops=hops + 1, _seen=frozenset(seen))
        if nested is not None:
            return nested
    return None
