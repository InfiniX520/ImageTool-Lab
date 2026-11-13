"""Durable facts for cleaning a cancelled, never-published delivery.

A cancelled delivery can still own a staged '.part'.  The database row is the
only authority on which file is ours: this module turns that row into a
verifiable cleanup fact, so a retry deletes exactly one file and never scans a
directory or guesses from a name.  No I/O happens here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from core.task_contracts import ContractError

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class CleanupError(ContractError):
    """The stored prepared evidence cannot authorize a cleanup."""


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise CleanupError(f"Invalid {name}")
    return value


@dataclass(frozen=True, slots=True)
class CleanupFact:
    """One cancelled delivery whose staged temp may still exist."""

    delivery_id: str
    batch_id: str
    job_id: str
    state: str
    prepared: Mapping[str, Any]
    generation: int

    def __post_init__(self):
        _token(self.delivery_id, "delivery id")
        _token(self.batch_id, "batch id")
        _token(self.job_id, "job id")
        if self.state != "cancelled":
            raise CleanupError("Only a cancelled delivery can be cleaned")
        if type(self.generation) is not int or self.generation < 1:
            raise CleanupError("Invalid delivery generation")
        evidence = self.prepared
        if not isinstance(evidence, Mapping) or evidence.get("schema_version") != 1:
            raise CleanupError("Invalid prepared evidence")
        for name in ("temp_ref", "final_ref", "file_id"):
            if not isinstance(evidence.get(name), Mapping):
                raise CleanupError(f"Invalid prepared {name}")
        if not isinstance(evidence.get("source_identity"), str):
            raise CleanupError("Invalid prepared source identity")
        if type(evidence.get("size")) is not int or evidence["size"] < 0:
            raise CleanupError("Invalid prepared size")
        if not isinstance(evidence.get("sha256"), str) or (
            _DIGEST_RE.fullmatch(evidence["sha256"]) is None
        ):
            raise CleanupError("Invalid prepared digest")

    @property
    def temp_ref(self) -> dict[str, Any]:
        return dict(self.prepared["temp_ref"])

    @property
    def final_ref(self) -> dict[str, Any]:
        return dict(self.prepared["final_ref"])

    @property
    def file_id(self) -> dict[str, Any]:
        return dict(self.prepared["file_id"])

    @property
    def size(self) -> int:
        return int(self.prepared["size"])

    @property
    def sha256(self) -> str:
        return str(self.prepared["sha256"])


def cleanup_fact(row: Any) -> CleanupFact | None:
    """Build a cleanup fact from a delivery row, or None when it does not own one.

    The prepared evidence survives cancellation on purpose: it is the only
    proof that a '.part' belongs to this delivery.  A row without it is never
    guessed from the file system.
    """
    try:
        state = str(row["state"])
        if state != "cancelled":
            return None
        prepared = row["prepared"]
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(prepared, Mapping):
        return None
    try:
        return CleanupFact(
            delivery_id=str(row["delivery_id"]),
            batch_id=str(row["current_batch_id"]),
            job_id=str(row["job_id"]),
            state=state,
            prepared=dict(prepared),
            generation=int(row["generation"]),
        )
    except (CleanupError, KeyError, TypeError, ValueError):
        return None


def cleanup_event_payload(fact: CleanupFact, *, outcome: str, error_code: str = "") -> dict[str, Any]:
    """A stable audit payload; the outcome never claims delivery success.

    The payload is the only durable cleanup fact, so it carries the prepared
    identity it refers to (digest and size) and the terminal boolean the
    candidate query reads.  The producer here and the reader in
    delivery_repository share this one shape (RFA-03).
    """
    if outcome not in {"cleaned", "failed"}:
        raise CleanupError("Invalid cleanup outcome")
    return {
        "schema_version": 1,
        "delivery_id": fact.delivery_id,
        "batch_id": fact.batch_id,
        "outcome": outcome,
        "error_code": error_code,
        "temp_sha256": fact.sha256,
        "temp_size": fact.size,
        "cleanup_completed": outcome == "cleaned",
    }


def cleanup_is_terminal(payload: Mapping[str, Any]) -> bool:
    """Whether one cleanup fact ends the retry queue for its delivery.

    Two shapes are accepted on purpose.  The current producer always writes
    ``cleanup_completed``; a record written before RFA-03 has only
    ``outcome == "cleaned"`` and must still leave the queue.  ``failed`` is
    never terminal: a retry has to stay possible.  The SQL predicate in
    ``DeliveryRepositoryMixin.list_cleanup_candidates`` is the transliteration
    of this rule, and a test asserts the two agree.
    """
    if not isinstance(payload, Mapping):
        return False
    if "cleanup_completed" in payload:
        return payload.get("cleanup_completed") is True
    return payload.get("outcome") == "cleaned"
