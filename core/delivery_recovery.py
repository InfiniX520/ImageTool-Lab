#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""U1-A/W5: an explicit, offline recovery entry for a page parked in verification.

A page can be left in 'needs_verification' (or in 'pending') because the bytes
recorded for it are no longer provably present on disk.  Without an explicit
entry point that state is a dead end: the hold occupies the unique active
output slot (ux_deliveries_active_output), the batch can only settle as
'partial', and a brand new task cannot repair the page either.

This module adds exactly one repository command, 'recover_verification_page':

* the caller thread observes the candidate files of one page through
  core.local_observation - outside any database transaction;
* the existing single writer revalidates that frozen evidence and performs one
  short compare-and-set transaction that only touches rows of this page;
* history stays append-only: no artifact row, completed delivery, finished
  batch count or superseded delivery row is rewritten or deleted.

Nothing here opens a socket, deletes a file, moves a file or rewrites bytes.
Failed checks keep the page in 'needs_verification' and report a safe reason
code; a later UI round decides what to offer the user.

State machine ('previous_state' is the delivery the page is linked to):

| previous_state      | observed file        | action  | result state         |
| ------------------- | -------------------- | ------- | -------------------- |
| completed / linked  | not observed         | none    | completed            |
| needs_verification  | hash_match           | reuse   | completed (reused)   |
| needs_verification  | missing              | repair  | pending (new gen)    |
| needs_verification  | changed              | hold    | needs_verification   |
| needs_verification  | unreadable           | hold    | needs_verification   |
| needs_verification  | root_unavailable     | hold    | needs_verification   |
| needs_verification  | changed_during_check | hold    | needs_verification   |
| needs_verification  | unknown / none       | hold    | needs_verification   |
| pending             | hash_match           | reuse   | completed (reused)   |
| pending             | missing              | none    | pending (idempotent) |
| pending             | changed              | park    | needs_verification   |
| pending             | other                | none    | pending              |
| anything else       | -                    | none    | refused              |

'reuse' retires the hold as 'cancelled' (never deleted) and links the page to
the already recorded artifact, so the page becomes reusable without a download.
'repair' retires the hold the same way and inserts one new 'pending' delivery
generation that follows the existing transfer, prepared and publish protocol.
'park' converts a queued repair into a verification hold because a foreign file
occupies the output path; it never overwrites bytes.

The mixin expects its host repository to provide the documented single-writer
infrastructure (_transaction, _enqueue, _insert_event, _readonly_connection,
_link_batch_item_page, _refresh_batch_item_result) and the artifact health
preflight methods.  It is mixed into core.library_repository.LibraryRepository.
"""

from __future__ import annotations

import re
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from core.artifact_health import ArtifactHealth
from core.preservation_policy import LocalState
from core.task_contracts import ContractError

RECOVERY_COMMANDS = frozenset({"recover_verification_page"})

# Exactly the delivery states covered by ux_deliveries_active_output in
# core/library_repository.py.  Recovery never creates a second row in this set
# for one output path; the check mirrors the index instead of guessing at it.
ACTIVE_OUTPUT_STATES = (
    "pending",
    "transferring",
    "validating",
    "prepared",
    "needs_verification",
    "suspended",
    "retry_wait",
    "recovery_needed",
)

# A page always has at most one page link, so the candidate list stays bounded
# by the same order of magnitude as the accept-time preflight.
_MAX_RECOVERY_CANDIDATES = 8

# Path-level verdicts are identical for every candidate artifact of one page
# (they share one PathRef), so the observation loop stops on them.  A
# content-level CHANGED keeps the loop going: another recorded version of the
# same page may still match the current file.
_DECISIVE_FILE_STATES = frozenset(
    {
        LocalState.MISSING,
        LocalState.ROOT_UNAVAILABLE,
        LocalState.UNREADABLE,
        LocalState.CHANGED_DURING_CHECK,
    }
)

_REASON_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")

# The action and the reported outcome are one fact; a command that claims one
# and reports the other is rejected before any row is touched.
_ACTION_OUTCOME = {
    "reuse": "reused",
    "repair": "repair_queued",
    "park": "held",
}


def _lib():
    # Runtime import avoids the mixing -> repository import cycle.
    from core import library_repository as module

    return module


class RecoveryOutcome(StrEnum):
    """What the page looks like after this call, not what was attempted."""

    REUSED = "reused"
    REPAIR_QUEUED = "repair_queued"
    HELD = "held"
    COMPLETED = "completed"
    REFUSED = "refused"


class RecoveryAction(StrEnum):
    """The only write a recovery command may perform."""

    REUSE = "reuse"
    REPAIR = "repair"
    PARK = "park"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """One pure decision; it never performs I/O."""

    outcome: RecoveryOutcome
    action: RecoveryAction
    reason: str

    def __post_init__(self):
        if not isinstance(self.outcome, RecoveryOutcome):
            raise ContractError("Invalid recovery outcome")
        if not isinstance(self.action, RecoveryAction):
            raise ContractError("Invalid recovery action")
        if not isinstance(self.reason, str) or _REASON_RE.fullmatch(self.reason) is None:
            raise ContractError("Invalid recovery reason")


def decide_page_recovery(
    previous_state: object,
    health_state: object,
    *,
    reusable: bool,
    unavailable_reason: str = "observation_unknown",
) -> RecoveryDecision:
    """Map (current delivery state, observed file state) to one action.

    'reusable' is the frozen preflight verdict for the artifact that was
    chosen; a health record that is not reusable can never authorize 'reuse'.
    'unavailable_reason' distinguishes "no observation at all" from
    "observation rejected" for the caller without weakening fail-closed rules.
    """
    if type(reusable) is not bool:
        raise ContractError("Invalid recovery reuse flag")
    if health_state is not None and not isinstance(health_state, LocalState):
        raise ContractError("Invalid recovery health state")
    if _REASON_RE.fullmatch(str(unavailable_reason)) is None:
        raise ContractError("Invalid recovery unavailable reason")

    if previous_state == "completed":
        return RecoveryDecision(
            RecoveryOutcome.COMPLETED, RecoveryAction.NONE, "already_completed"
        )
    if previous_state not in {"needs_verification", "pending"}:
        return RecoveryDecision(
            RecoveryOutcome.REFUSED, RecoveryAction.NONE, "unsupported_previous_state"
        )
    if health_state is LocalState.HASH_MATCH:
        if reusable:
            return RecoveryDecision(
                RecoveryOutcome.REUSED, RecoveryAction.REUSE, "hash_match"
            )
        # A full digest that no longer binds the recorded row is never reuse.
        return RecoveryDecision(
            RecoveryOutcome.HELD, RecoveryAction.NONE, "observation_unknown"
        )
    if health_state is LocalState.MISSING:
        if previous_state == "pending":
            return RecoveryDecision(
                RecoveryOutcome.REPAIR_QUEUED, RecoveryAction.NONE, "repair_already_pending"
            )
        return RecoveryDecision(
            RecoveryOutcome.REPAIR_QUEUED, RecoveryAction.REPAIR, "artifact_missing"
        )
    if health_state is LocalState.CHANGED:
        if previous_state == "pending":
            return RecoveryDecision(
                RecoveryOutcome.HELD, RecoveryAction.PARK, "artifact_changed"
            )
        return RecoveryDecision(
            RecoveryOutcome.HELD, RecoveryAction.NONE, "artifact_changed"
        )
    if health_state is LocalState.UNREADABLE:
        return RecoveryDecision(
            RecoveryOutcome.HELD, RecoveryAction.NONE, "artifact_unreadable"
        )
    if health_state is LocalState.ROOT_UNAVAILABLE:
        # Never translate an unavailable root into "all files are gone".
        return RecoveryDecision(
            RecoveryOutcome.HELD, RecoveryAction.NONE, "artifact_root_unavailable"
        )
    if health_state is LocalState.CHANGED_DURING_CHECK:
        return RecoveryDecision(
            RecoveryOutcome.HELD, RecoveryAction.NONE, "artifact_changed_during_check"
        )
    return RecoveryDecision(RecoveryOutcome.HELD, RecoveryAction.NONE, unavailable_reason)


@dataclass(frozen=True, slots=True)
class _RecoveryPage:
    """One read-only snapshot; the writer re-reads and re-compares every field."""

    batch_id: str
    job_id: str
    phase: str
    illust_id: str
    page_index: int
    output_key: str
    delivery_id: str
    delivery_state: str
    error_code: str | None
    quality_key: str
    artifacts: tuple[dict[str, Any], ...]
    cross_task_receipt: bool


def _unavailable_reason(page: _RecoveryPage) -> str:
    """Why no usable observation exists; never invents a loss."""
    if page.artifacts:
        return "observation_unknown"
    if page.cross_task_receipt:
        return "cross_task_reuse_disabled"
    return "no_recorded_artifact"


def _recovery_result(
    page: _RecoveryPage,
    decision: RecoveryDecision,
    primary: ArtifactHealth | None,
    chosen: tuple[dict[str, Any], ArtifactHealth] | None,
) -> dict[str, Any]:
    """The frozen result draft; the writer finalizes the durable state."""
    artifact = None if chosen is None else chosen[0]
    return {
        "outcome": decision.outcome.value,
        "reason": decision.reason,
        "batch_id": page.batch_id,
        "job_id": page.job_id,
        "illust_id": page.illust_id,
        "page_index": page.page_index,
        "output_key": page.output_key,
        "previous_delivery_id": page.delivery_id,
        "previous_state": page.delivery_state,
        "previous_error_code": page.error_code,
        "delivery_id": page.delivery_id,
        "state": page.delivery_state,
        "artifact_id": None if artifact is None else str(artifact["artifact_id"]),
        "repair_delivery_id": None,
        "health": None if primary is None else primary.to_payload(),
        "observed_size": None if primary is None else primary.observed_size,
        "observed_sha256": None if primary is None else primary.observed_sha256,
        "checked_at": None if primary is None else primary.checked_at,
    }


class DeliveryRecoveryMixin:
    """Repository-layer recovery of one page; no UI and no network."""

    _RECOVERY_COMMAND_KINDS = RECOVERY_COMMANDS

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def recover_verification_page(
        self,
        batch_id: str,
        illust_id: str,
        page_index: int,
        *,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Re-check one page of a running batch and apply the decide-and-link step.

        Returns a JSON-safe result dict with 'outcome' (reused / repair_queued /
        held / completed / refused), a stable 'reason' code and the page's
        delivery and artifact identifiers.  File observation happens here, on
        the caller thread, before the command is queued; the writer only
        compare-and-sets rows.

        A repeated call is idempotent: a reused page reports 'completed' and a
        queued repair reports 'repair_queued' without a second generation.  With
        'command_id' the first accepted result is replayed verbatim and no file
        is read again.
        """
        lib = _lib()
        batch_id = lib._validate_identifier(batch_id, "batch id")
        illust_id = lib._validate_identifier(illust_id, "illust id")
        if type(page_index) is not int or not 0 <= page_index <= 9999:
            raise lib.LibraryError("Invalid page index")
        if command_id is not None:
            command_id = lib._validate_command_id(command_id)
        self._ensure_open()
        if command_id is not None:
            stored = self._stored_recovery_result(
                command_id, batch_id, illust_id, page_index
            )
            if stored is not None:
                return stored

        page = self._read_recovery_page(batch_id, illust_id, page_index)
        resolver = self._path_resolver_snapshot()
        if page.delivery_state == "completed":
            decision = RecoveryDecision(
                RecoveryOutcome.COMPLETED, RecoveryAction.NONE, "already_completed"
            )
            primary = None
            chosen = None
        elif page.phase != "running":
            decision = RecoveryDecision(
                RecoveryOutcome.REFUSED, RecoveryAction.NONE, "batch_not_running"
            )
            primary = None
            chosen = None
        elif resolver is None:
            decision = RecoveryDecision(
                RecoveryOutcome.HELD, RecoveryAction.NONE, "observation_unavailable"
            )
            primary = None
            chosen = None
        else:
            primary, chosen = self._observe_recovery_page(
                page, resolver, f"recover-{batch_id}"
            )
            decision = decide_page_recovery(
                page.delivery_state,
                None if primary is None else primary.state,
                reusable=chosen is not None,
                unavailable_reason=_unavailable_reason(page),
            )

        draft = _recovery_result(page, decision, primary, chosen)
        return self._enqueue(
            "recover_verification_page",
            {
                "batch_id": batch_id,
                "illust_id": illust_id,
                "page_index": int(page_index),
                "command_id": command_id,
                "previous_delivery_id": page.delivery_id,
                "previous_state": page.delivery_state,
                "action": decision.action.value,
                "artifact_id": None if chosen is None else str(chosen[0]["artifact_id"]),
                "health": [] if chosen is None else [chosen[1].to_payload()],
                "draft": draft,
            },
        )

    # ------------------------------------------------------------------
    # Read-only snapshot and file preflight (caller thread)
    # ------------------------------------------------------------------

    def _stored_recovery_result(
        self, command_id: str, batch_id: str, illust_id: str, page_index: int
    ) -> dict[str, Any] | None:
        """Replay an accepted command without repeating any file observation."""
        lib = _lib()
        with closing(self._readonly_connection()) as conn:
            info = self._command_event(conn, command_id)
        if info is None:
            return None
        result = self._command_result(info)
        if (
            result.get("batch_id") != batch_id
            or result.get("illust_id") != illust_id
            or result.get("page_index") != page_index
        ):
            raise lib.CommandConflict(
                "command id was already used for another page recovery"
            )
        return result

    def _read_recovery_page(
        self, batch_id: str, illust_id: str, page_index: int
    ) -> _RecoveryPage:
        """Read one consistent snapshot; this never opens a file or a write txn."""
        lib = _lib()
        from core.delivery_repository import _artifact_is_valid

        with closing(self._readonly_connection()) as conn:
            batch = self._get_batch_row(conn, batch_id)
            if batch is None:
                raise lib.NotFoundError(f"Unknown batch: {batch_id}")
            job = self._get_job_row(conn, str(batch["job_id"]))
            if job is None:
                raise lib.CorruptDataError("Batch references a missing job")
            link = conn.execute(
                "SELECT * FROM batch_item_pages WHERE batch_id = ? AND illust_id = ? "
                "AND page_index = ?",
                (batch_id, illust_id, int(page_index)),
            ).fetchone()
            if link is None:
                raise lib.NotFoundError(
                    f"Page {illust_id}/{page_index} is not part of batch {batch_id}"
                )
            output_key = str(link["output_key"])

            delivery = None
            if link["delivery_id"] is not None:
                delivery = self._get_delivery_row(conn, str(link["delivery_id"]))
                if delivery is None:
                    raise lib.CorruptDataError(
                        "Selected page references a missing delivery"
                    )
                if str(delivery["output_key"]) != output_key:
                    raise lib.CorruptDataError(
                        "Selected page and its delivery disagree about the output path"
                    )
            artifact = None
            if link["artifact_id"] is not None:
                artifact = conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_id = ?",
                    (str(link["artifact_id"]),),
                ).fetchone()
                if artifact is None or not _artifact_is_valid(dict(artifact)):
                    raise lib.CorruptDataError("Selected page links an invalid artifact")
            if artifact is None and delivery is not None:
                if str(delivery["state"]) == "completed":
                    artifact = conn.execute(
                        "SELECT * FROM artifacts WHERE delivery_id = ?",
                        (str(delivery["delivery_id"]),),
                    ).fetchone()
                    if artifact is None or not _artifact_is_valid(dict(artifact)):
                        raise lib.CorruptDataError(
                            "Completed delivery has no valid artifact"
                        )
            if artifact is not None:
                completed_delivery_id = str(artifact["delivery_id"])
            elif delivery is not None:
                completed_delivery_id = str(delivery["delivery_id"])
            else:
                raise lib.CorruptDataError(
                    "Selected page has neither a delivery nor an artifact"
                )

            state = "completed" if artifact is not None else str(delivery["state"])
            error_code = None if delivery is None else delivery["error_code"]
            quality_key = (
                str(delivery["quality_key"])
                if delivery is not None
                else lib._sha256_text(str(link["quality_json"]))
            )
            artifacts: tuple[dict[str, Any], ...] = ()
            cross_task_receipt = False
            if artifact is None:
                request = self._request_from_row(job)
                artifacts = self._read_recovery_candidates(
                    conn,
                    job_id=str(job["job_id"]),
                    illust_id=illust_id,
                    page_index=int(page_index),
                    output_key=output_key,
                    quality_key=quality_key,
                    request=request,
                )
                if not artifacts and request.dedup_policy != "trusted":
                    # A read-only diagnostic: another task does hold a receipt,
                    # this batch merely opted out of cross-task reuse.
                    cross_task_receipt = self._has_cross_task_receipt(
                        conn,
                        job_id=str(job["job_id"]),
                        illust_id=illust_id,
                        page_index=int(page_index),
                        output_key=output_key,
                        request=request,
                    )
        return _RecoveryPage(
            batch_id=batch_id,
            job_id=str(job["job_id"]),
            phase=str(batch["phase"]),
            illust_id=illust_id,
            page_index=int(page_index),
            output_key=output_key,
            delivery_id=completed_delivery_id,
            delivery_state=state,
            error_code=None if error_code is None else str(error_code),
            quality_key=quality_key,
            artifacts=artifacts,
            cross_task_receipt=cross_task_receipt,
        )

    def _read_recovery_candidates(
        self,
        conn,
        *,
        job_id: str,
        illust_id: str,
        page_index: int,
        output_key: str,
        quality_key: str,
        request,
    ) -> tuple[dict[str, Any], ...]:
        """Same-task receipt first, then the trusted cross-task candidates.

        The order matches the accept path: the page's own previous completion is
        the most specific receipt; cross-task reuse is only considered when the
        batch request explicitly opted into trusted deduplication.
        """
        from core.delivery_repository import _artifact_is_valid

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        same = conn.execute(
            "SELECT * FROM deliveries WHERE job_id = ? AND illust_id = ? "
            "AND page_index = ? AND quality_key = ? AND output_key = ? "
            "AND state = 'completed' ORDER BY generation DESC LIMIT 1",
            (job_id, illust_id, int(page_index), quality_key, output_key),
        ).fetchone()
        if same is not None:
            artifact = conn.execute(
                "SELECT * FROM artifacts WHERE delivery_id = ?",
                (str(same["delivery_id"]),),
            ).fetchone()
            if artifact is not None:
                value = dict(artifact)
                if _artifact_is_valid(value):
                    seen.add(str(value["artifact_id"]))
                    rows.append(value)
        if request.dedup_policy == "trusted":
            for artifact in self._find_reusable_artifact_candidates(
                conn,
                job_id=job_id,
                illust_id=illust_id,
                page_index=int(page_index),
                output_key=output_key,
                original=request.quality.original,
                validation_policy_version=request.quality.validation_policy_version,
                limit=_MAX_RECOVERY_CANDIDATES,
            ):
                value = dict(artifact)
                artifact_id = str(value["artifact_id"])
                if artifact_id in seen:
                    continue
                seen.add(artifact_id)
                rows.append(value)
        return tuple(rows)

    def _has_cross_task_receipt(
        self,
        conn,
        *,
        job_id: str,
        illust_id: str,
        page_index: int,
        output_key: str,
        request,
    ) -> bool:
        """Whether another task recorded this page; never opens a file."""
        return bool(
            self._find_reusable_artifact_candidates(
                conn,
                job_id=job_id,
                illust_id=illust_id,
                page_index=int(page_index),
                output_key=output_key,
                original=request.quality.original,
                validation_policy_version=request.quality.validation_policy_version,
                limit=1,
            )
        )

    def _observe_recovery_page(
        self, page: _RecoveryPage, resolver, run_id: str
    ) -> tuple[ArtifactHealth | None, tuple[dict[str, Any], ArtifactHealth] | None]:
        """Hash candidate files outside the write transaction; fail closed."""
        primary: ArtifactHealth | None = None
        for artifact in page.artifacts:
            record = self._observe_artifact(
                artifact,
                resolver=resolver,
                run_id=run_id,
                quality_key=page.quality_key,
            )
            if record is None:
                continue
            if primary is None:
                primary = record
            if record.reusable:
                return primary, (artifact, record)
            if record.state in _DECISIVE_FILE_STATES:
                break
        return primary, None

    # ------------------------------------------------------------------
    # Dispatch and the short compare-and-set transaction
    # ------------------------------------------------------------------

    def _dispatch_recovery(self, conn, command) -> Any:
        if command.kind == "recover_verification_page":
            return self._recover_verification_page(conn, command.payload)
        raise _lib().LibraryError(f"Unknown recovery command: {command.kind}")

    def _recover_verification_page(self, conn, payload: dict[str, Any]) -> dict[str, Any]:
        lib = _lib()
        batch_id = str(payload["batch_id"])
        illust_id = str(payload["illust_id"])
        page_index = int(payload["page_index"])
        command_id = payload.get("command_id")
        previous_delivery_id = payload.get("previous_delivery_id")
        previous_state = payload.get("previous_state")
        action = str(payload["action"])
        draft = payload.get("draft")
        if action not in {item.value for item in RecoveryAction}:
            raise lib.LibraryError("Unsupported recovery action")
        if not isinstance(draft, dict):
            raise lib.LibraryError("A recovery draft result is required")
        if _ACTION_OUTCOME.get(action, draft.get("outcome")) != draft.get("outcome"):
            raise lib.LibraryError("Recovery draft does not match its action")
        with self._transaction(conn):
            batch = self._get_batch_row(conn, batch_id)
            if batch is None:
                raise lib.NotFoundError(f"Unknown batch: {batch_id}")
            if command_id is not None:
                info = self._command_event(conn, command_id)
                if info is not None:
                    stored = self._command_result(info)
                    if (
                        stored.get("batch_id") != batch_id
                        or stored.get("illust_id") != illust_id
                        or stored.get("page_index") != page_index
                    ):
                        raise lib.CommandConflict(
                            "command id was already used for another page recovery"
                        )
                    return stored
            result = dict(draft)
            if str(batch["phase"]) != "running":
                return self._refuse_recovery(
                    conn, batch, result, "batch_not_running", command_id
                )
            link = conn.execute(
                "SELECT * FROM batch_item_pages WHERE batch_id = ? AND illust_id = ? "
                "AND page_index = ?",
                (batch_id, illust_id, page_index),
            ).fetchone()
            if link is None:
                return self._refuse_recovery(
                    conn, batch, result, "page_not_in_batch", command_id
                )
            delivery_id = None if link["delivery_id"] is None else str(link["delivery_id"])
            if link["artifact_id"] is not None:
                result.update(outcome="completed", reason="already_completed")
                result["state"] = "completed"
                result["artifact_id"] = str(link["artifact_id"])
                if delivery_id is not None:
                    result["delivery_id"] = delivery_id
                result["repair_delivery_id"] = None
                return self._finish_recovery(conn, batch, result, command_id, action="none")
            if delivery_id is None:
                return self._refuse_recovery(
                    conn, batch, result, "page_not_in_batch", command_id
                )
            delivery = self._get_delivery_row(conn, delivery_id)
            if delivery is None:
                raise lib.CorruptDataError("Selected page references a missing delivery")
            if str(delivery["job_id"]) != str(batch["job_id"]):
                return self._refuse_recovery(
                    conn, batch, result, "page_owner_mismatch", command_id
                )
            current_state = str(delivery["state"])
            result["delivery_id"] = delivery_id
            result["state"] = current_state
            if delivery_id != previous_delivery_id or current_state != previous_state:
                # The page moved between the preflight and this transaction.
                return self._refuse_recovery(
                    conn, batch, result, "state_changed", command_id
                )
            if action == RecoveryAction.NONE.value:
                if result.get("outcome") == RecoveryOutcome.REPAIR_QUEUED.value:
                    result["repair_delivery_id"] = delivery_id
                return self._finish_recovery(conn, batch, result, command_id, action=action)
            if action == RecoveryAction.REUSE.value:
                reason = self._apply_recovery_reuse(
                    conn, batch, link, delivery, payload, result
                )
            elif action == RecoveryAction.REPAIR.value:
                reason = self._apply_recovery_repair(conn, batch, link, delivery, result)
            else:
                reason = self._apply_recovery_park(conn, batch, delivery, result)
            if reason is not None:
                result["outcome"] = RecoveryOutcome.REFUSED.value
                result["reason"] = reason
                result["state"] = current_state
                result["delivery_id"] = delivery_id
                result["repair_delivery_id"] = None
            return self._finish_recovery(conn, batch, result, command_id, action=action)

    def _apply_recovery_reuse(
        self, conn, batch, link, delivery, payload: dict[str, Any], result: dict[str, Any]
    ) -> str | None:
        """Link the recorded artifact and retire the hold; never touch history."""
        lib = _lib()
        artifact_id = payload.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            return "stale_preflight"
        health = self._health_map(payload.get("health")).get(artifact_id)
        artifact = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if artifact is None or health is None:
            return "stale_preflight"
        if not self._health_allows_reuse(health, artifact):
            return "stale_preflight"
        if (
            str(artifact["illust_id"]) != str(link["illust_id"])
            or int(artifact["page_index"]) != int(link["page_index"])
            or str(artifact["output_key"]) != str(link["output_key"])
        ):
            return "stale_preflight"
        now = lib._utcnow()
        retired = conn.execute(
            "UPDATE deliveries SET state = 'cancelled', "
            "error_code = 'recovered_hash_match', retry_at = NULL, updated_at = ? "
            "WHERE delivery_id = ? AND state = ?",
            (now, str(delivery["delivery_id"]), str(delivery["state"])),
        ).rowcount
        if retired != 1:
            return "state_changed"
        same_job = str(artifact["job_id"]) == str(batch["job_id"])
        self._link_batch_item_page(
            conn,
            str(batch["batch_id"]),
            str(link["illust_id"]),
            int(link["page_index"]),
            str(link["output_key"]),
            str(link["quality_json"]),
            delivery_id=str(artifact["delivery_id"]) if same_job else None,
            artifact_id=artifact_id,
        )
        self._refresh_batch_item_result(
            conn, str(batch["batch_id"]), str(link["illust_id"]), now
        )
        result.update(
            outcome=RecoveryOutcome.REUSED.value,
            state="completed",
            delivery_id=str(artifact["delivery_id"]),
            artifact_id=artifact_id,
            repair_delivery_id=None,
        )
        return None

    def _apply_recovery_repair(
        self, conn, batch, link, delivery, result: dict[str, Any]
    ) -> str | None:
        """Retire the hold and queue one new pending delivery generation."""
        lib = _lib()
        batch_id = str(batch["batch_id"])
        job_id = str(batch["job_id"])
        illust_id = str(link["illust_id"])
        page_index = int(link["page_index"])
        item = conn.execute(
            "SELECT * FROM batch_items WHERE batch_id = ? AND illust_id = ?",
            (batch_id, illust_id),
        ).fetchone()
        if item is None or str(item["decision"]) != "download":
            return "page_not_in_batch"
        counts_work = int(item["counts_work"] or 0)
        if not counts_work:
            budget = lib._parse_json_object(batch["budget_json"], "batch budget")
            max_works = budget.get("max_works")
            if type(max_works) is not int or max_works < 1:
                raise lib.CorruptDataError("Corrupt batch work budget")
            used_works = int(
                conn.execute(
                    "SELECT COUNT(DISTINCT illust_id) AS count FROM batch_items "
                    "WHERE batch_id = ? AND counts_work = 1",
                    (batch_id,),
                ).fetchone()["count"]
            )
            if used_works + 1 > max_works:
                # The design keeps the page parked instead of over-spending.
                return "work_budget_exhausted"
        blocking = conn.execute(
            "SELECT delivery_id FROM deliveries WHERE output_key = ? "
            "AND delivery_id != ? AND state IN ("
            + ",".join("?" for _ in ACTIVE_OUTPUT_STATES)
            + ") LIMIT 1",
            (str(link["output_key"]), str(delivery["delivery_id"]), *ACTIVE_OUTPUT_STATES),
        ).fetchone()
        if blocking is not None:
            # Another task owns the single active output slot; never steal it.
            return "output_blocked_by_other_task"
        now = lib._utcnow()
        retired = conn.execute(
            "UPDATE deliveries SET state = 'cancelled', "
            "error_code = 'superseded_by_recovery_repair', retry_at = NULL, updated_at = ? "
            "WHERE delivery_id = ? AND state = 'needs_verification'",
            (now, str(delivery["delivery_id"])),
        ).rowcount
        if retired != 1:
            return "state_changed"
        generation = (
            int(
                conn.execute(
                    "SELECT COALESCE(MAX(generation), 0) AS generation FROM deliveries "
                    "WHERE job_id = ? AND illust_id = ? AND page_index = ? "
                    "AND quality_key = ?",
                    (job_id, illust_id, page_index, str(delivery["quality_key"])),
                ).fetchone()["generation"]
            )
            + 1
        )
        repair_delivery_id = lib._new_id()
        conn.execute(
            "INSERT INTO deliveries("
            "delivery_id, job_id, origin_batch_id, current_batch_id, "
            "completed_batch_id, illust_id, page_index, output_key, "
            "output_ref_json, quality_json, quality_key, payload_json, "
            "source_identity, state, attempts, http_attempts, retry_at, error_code, "
            "generation, checkpoint_json, prepared_json, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', "
            "0, 0, NULL, NULL, ?, NULL, NULL, ?, ?)",
            (
                repair_delivery_id,
                job_id,
                batch_id,
                batch_id,
                illust_id,
                page_index,
                str(delivery["output_key"]),
                str(delivery["output_ref_json"]),
                str(delivery["quality_json"]),
                str(delivery["quality_key"]),
                str(delivery["payload_json"]),
                None
                if delivery["source_identity"] is None
                else str(delivery["source_identity"]),
                generation,
                now,
                now,
            ),
        )
        self._link_batch_item_page(
            conn,
            batch_id,
            illust_id,
            page_index,
            str(link["output_key"]),
            str(link["quality_json"]),
            delivery_id=repair_delivery_id,
            artifact_id=None,
        )
        if not counts_work:
            conn.execute(
                "UPDATE batch_items SET counts_work = 1 WHERE batch_id = ? AND illust_id = ?",
                (batch_id, illust_id),
            )
        self._refresh_batch_item_result(conn, batch_id, illust_id, now)
        result.update(
            outcome=RecoveryOutcome.REPAIR_QUEUED.value,
            state="pending",
            delivery_id=repair_delivery_id,
            artifact_id=None,
            repair_delivery_id=repair_delivery_id,
        )
        return None

    def _apply_recovery_park(self, conn, batch, delivery, result: dict[str, Any]) -> str | None:
        """Hold a queued repair when a foreign file owns the output path."""
        lib = _lib()
        now = lib._utcnow()
        updated = conn.execute(
            "UPDATE deliveries SET state = 'needs_verification', "
            "error_code = 'artifact_changed', retry_at = NULL, updated_at = ? "
            "WHERE delivery_id = ? AND state = 'pending'",
            (now, str(delivery["delivery_id"])),
        ).rowcount
        if updated != 1:
            return "state_changed"
        self._refresh_batch_item_result(
            conn, str(batch["batch_id"]), str(delivery["illust_id"]), now
        )
        result.update(state="needs_verification", repair_delivery_id=None)
        return None

    def _refuse_recovery(
        self, conn, batch, result: dict[str, Any], reason: str, command_id
    ) -> dict[str, Any]:
        result["outcome"] = RecoveryOutcome.REFUSED.value
        result["reason"] = reason
        result["repair_delivery_id"] = None
        return self._finish_recovery(conn, batch, result, command_id, action="none")

    def _finish_recovery(
        self, conn, batch, result: dict[str, Any], command_id, *, action: str
    ) -> dict[str, Any]:
        """Append the audit event (exactly once per accepted command id)."""
        self._insert_event(
            conn,
            job_id=str(batch["job_id"]),
            batch_id=str(batch["batch_id"]),
            kind="verification_recovery",
            payload={**result, "action": action, "result": result},
            accepted_command_id=command_id,
        )
        return result
