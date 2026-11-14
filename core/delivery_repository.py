#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3 SQL half of the safe file-delivery protocol.

This module is a mixin for :class:`core.library_repository.LibraryRepository`.
It never opens a write connection, never starts a writer thread and never
performs file or network IO.  Public methods enqueue commands onto the existing
P2 writer; the repository dispatches the delivery command kinds here.

The filesystem adapter is responsible for creating/validating/publishing local
files.  This module stores and enforces the durable protocol facts around that
work: ownership, bounded HTTP attempts, checkpoints, validation evidence,
artifacts, selected-page links, batch result accounting and recovery facts.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from core.delivery_cleanup import cleanup_event_payload, cleanup_fact, cleanup_is_terminal
from core.delivery_lineage import (
    ACTIVE_FOLLOWUP,
    Successor,
    is_supersede_code,
    resolve_successor,
)
from core.task_contracts import ContractError, PathRef, canonical_json, normalize_json

# New table/index DDL is imported by core.library_repository and appended to
# its _DDL tuple.  That keeps the exact pre-production schema guard intact.
DELIVERY_DDL = (
    """
    CREATE TABLE batch_item_pages (
        batch_id TEXT NOT NULL,
        illust_id TEXT NOT NULL,
        page_index INTEGER NOT NULL CHECK (page_index >= 0 AND page_index <= 9999),
        delivery_id TEXT REFERENCES deliveries(delivery_id),
        artifact_id TEXT REFERENCES artifacts(artifact_id),
        output_key TEXT NOT NULL,
        quality_json TEXT NOT NULL,
        PRIMARY KEY (batch_id, illust_id, page_index),
        FOREIGN KEY (batch_id, illust_id)
            REFERENCES batch_items(batch_id, illust_id) ON DELETE CASCADE,
        CHECK (delivery_id IS NOT NULL OR artifact_id IS NOT NULL)
    )
    """,
    "CREATE INDEX idx_batch_item_pages_delivery "
    "ON batch_item_pages(delivery_id, batch_id)",
    "CREATE INDEX idx_batch_item_pages_artifact "
    "ON batch_item_pages(artifact_id, batch_id)",
)

DELIVERY_REQUIRED_TABLES = frozenset({"batch_item_pages"})

_DELIVERY_COMMAND_KINDS = frozenset(
    {
        "start_transfer_attempt",
        "save_transfer_checkpoint",
        "begin_validation",
        "prepare_delivery",
        "confirm_delivery",
        "set_delivery_issue",
        "request_block",
        "finish_source",
        "settle_batch",
        "mark_cleanup_completed",
    }
)

_SUPPORTED_MEDIA = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)
_REASON_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DELIVERY_STATE_RE = re.compile(r"^[a-z][a-z_]{2,31}$")

# The SQL transliteration of delivery_cleanup.cleanup_is_terminal: a terminal
# fact is either the current boolean shape or the legacy "cleaned" outcome that
# predates it.  A failed attempt stays retryable on purpose (RFA-03).
_TERMINAL_CLEANUP_SQL = """
    SELECT 1 FROM events AS e
    WHERE e.kind = 'delivery_cleanup'
      AND e.safe_payload LIKE '%"delivery_id":"' || d.delivery_id || '"%'
      AND (
        e.safe_payload LIKE '%"cleanup_completed":true%'
        OR (
          e.safe_payload NOT LIKE '%"cleanup_completed":%'
          AND e.safe_payload LIKE '%"outcome":"cleaned"%'
        )
      )
"""

# Failed attempts stay retryable, but they are paced behind rows nobody has
# tried yet: this is a repair queue, not a loop on one locked file.  Terminal
# facts are filtered out before this ordering, so a delivery that already
# succeeded never reaches it (contract Sec.2.4, fair retry).
_RETRY_PACING_SQL = """
    EXISTS (
      SELECT 1 FROM events AS r
      WHERE r.kind = 'delivery_cleanup'
        AND r.safe_payload LIKE '%"delivery_id":"' || d.delivery_id || '"%'
        AND r.safe_payload LIKE '%"outcome":"failed"%'
    )
"""

# One window shape for both tiers: a cancelled row with prepared evidence and
# no terminal cleanup fact.  Only the pacing predicate differs.
_CLEANUP_WINDOW_SQL = (
    "SELECT d.*, d.rowid AS _rowid FROM deliveries AS d "
    "WHERE d.state = 'cancelled' AND d.prepared_json IS NOT NULL "
    "AND d.rowid > ? AND NOT EXISTS ("
    + _TERMINAL_CLEANUP_SQL
    + ") AND "
)
# Tier 0 = nobody tried yet, tier 1 = a previous attempt failed.
_CLEANUP_TIER0_SQL = (
    _CLEANUP_WINDOW_SQL + "NOT " + _RETRY_PACING_SQL + " ORDER BY d.rowid LIMIT ?"
)
_CLEANUP_TIER1_SQL = (
    _CLEANUP_WINDOW_SQL + _RETRY_PACING_SQL + " ORDER BY d.rowid LIMIT ?"
)


@dataclass(frozen=True, slots=True)
class TierScan:
    """One bounded read of one cleanup tier.

    eof is only true when the query itself proved there is nothing behind
    the cursor.  A scan stopped by the examination cap reports
    budget_exhausted instead, with next_cursor already past every row it
    examined, so the caller must continue from there and must not wrap
    (contract v4 Sec.4, RFB-03).
    """

    facts: tuple[dict, ...]
    next_cursor: int
    eof: bool
    budget_exhausted: bool
    examined: int
    offered: int


_CHECKPOINT_KEYS = {
    "schema_version",
    "delivery_id",
    "temp_ref",
    "file_id",
    "source_identity",
    "strong_etag",
    "expected_total",
    "verified_bytes",
    "prefix_sha256",
    "media_type",
    "validation_policy_version",
}
_PREPARED_KEYS = {
    "schema_version",
    "delivery_id",
    "temp_ref",
    "final_ref",
    "file_id",
    "source_identity",
    "size",
    "sha256",
    "media_type",
    "verification",
    "validation_policy_version",
    "width",
    "height",
    "frames",
}
_PAGE_KEYS = (
    "total",
    "completed",
    "new_completed",
    "existing",
    "failed",
    "needs_verification",
    "pending",
    "cancelled",
)
_RESULT_KEYS = (
    "completed",
    "partial",
    "failed",
    "needs_verification",
    "pending",
    "cancelled",
    "filtered",
    "metadata_unknown",
    "unavailable",
    "unsupported",
    "skipped_existing",
)
_VALID_ARTIFACT_SQL = (
    "a.artifact_id IS NOT NULL AND a.verification = 'structure_checked' "
    "AND a.size IS NOT NULL AND a.size > 0 "
    "AND a.sha256 IS NOT NULL AND length(a.sha256) = 64 "
    "AND a.sha256 NOT GLOB '*[^0-9a-f]*' "
    "AND a.media_type IN ('image/jpeg','image/png','image/gif','image/webp')"
)


# The W5 verification list may only be asked for these delivery states.  The
# default pair is "stopped and waiting for a decision"; suspended/retry_wait
# belong to a paused or backing-off batch, and pending is a page whose bytes
# were never published yet.  Anything else is a programming error rather than
# an empty page, so an unknown state is rejected instead of silently ignored.
_VERIFICATION_LIST_STATES = (
    "needs_verification",
    "recovery_needed",
    "suspended",
    "pending",
    "retry_wait",
)
_VERIFICATION_DEFAULT_STATES = ("needs_verification", "recovery_needed")
# SQL transliteration of the two rows of the recovery decision table in
# core/delivery_recovery.py that own a real decide-and-link step: a held page
# and a plain pending page.  That module cannot be imported at load time
# (delivery mixin -> recovery -> repository), so the pair is restated here and
# tests pin it against decide_page_recovery.
_RECOVERABLE_PAGE_STATES = ("needs_verification", "pending")
_VERIFICATION_MAX_LIMIT = 200


def _lib():
    # Runtime import avoids the delivery mixin -> repository import cycle.
    from core import library_repository as module

    return module


def _is_int(value: object) -> bool:
    return type(value) is int


def _verification_states(states: object) -> tuple[str, ...]:
    """Validate the requested delivery states, preserving caller order.

    A bare string is rejected on purpose: iterating it would silently turn
    "suspended" into seven single-letter states and match nothing.
    """
    lib = _lib()
    if states is None:
        return _VERIFICATION_DEFAULT_STATES
    if isinstance(states, str) or not isinstance(states, (tuple, list)):
        raise lib.LibraryError("Verification states must be a sequence")
    if not states:
        raise lib.LibraryError("Verification states must not be empty")
    ordered: list[str] = []
    for state in states:
        if not isinstance(state, str) or state not in _VERIFICATION_LIST_STATES:
            raise lib.LibraryError("Unsupported verification page state")
        if state not in ordered:
            ordered.append(state)
    return tuple(ordered)


def _verification_cursor(after: object) -> tuple[str, str, int, str] | None:
    """Validate the (batch, illust, page, delivery) cursor of one page row."""
    lib = _lib()
    if after is None:
        return None
    if not isinstance(after, tuple) or len(after) != 4:
        raise lib.LibraryError("Invalid verification page cursor")
    batch_id, illust_id, page_index, delivery_id = after
    for value in (batch_id, illust_id, delivery_id):
        if not isinstance(value, str) or not value or len(value) > 512:
            raise lib.LibraryError("Invalid verification page cursor")
    if not _is_int(page_index) or not 0 <= page_index <= 9999:
        raise lib.LibraryError("Invalid verification page cursor")
    return (batch_id, illust_id, page_index, delivery_id)


def _require_library_error(message: str) -> None:
    lib = _lib()
    raise lib.LibraryError(message)


def _parse_json_object(text: object, what: str) -> dict[str, Any]:
    lib = _lib()
    if not isinstance(text, str):
        raise lib.CorruptDataError(f"Corrupt {what}: not text")
    try:
        value = json.loads(text)
    except (TypeError, ValueError, RecursionError) as exc:
        raise lib.CorruptDataError(f"Corrupt {what}") from exc
    if not isinstance(value, dict):
        raise lib.CorruptDataError(f"Corrupt {what}: expected object")
    return value


def path_ref_from_object(value: object) -> PathRef:
    """Validate an exact PathRef JSON object for P3 evidence/checkpoint."""
    if (
        not isinstance(value, dict)
        or set(value) != {"kind", "root_id", "relative_path"}
    ):
        _require_library_error("Invalid path reference object")
    try:
        return PathRef(**value)
    except (TypeError, ContractError) as exc:
        raise _lib().LibraryError("Invalid path reference") from exc


def _validate_file_id(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"device", "inode"}:
        _require_library_error("Invalid file identity")
    device = value["device"]
    inode = value["inode"]
    if not _is_int(device) or not _is_int(inode) or inode <= 0:
        _require_library_error("Invalid file identity")
    return {"device": device, "inode": inode}


def _validate_hex64(value: object, what: str) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        _require_library_error(f"Invalid {what}")
    return value


def _validate_strong_etag(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        _require_library_error("Invalid strong ETag")
    if (
        len(value) < 2
        or len(value) > 512
        or not value.startswith('"')
        or not value.endswith('"')
        or value.startswith("W/")
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        _require_library_error("Invalid strong ETag")
    return value


def _relative_parts(ref: PathRef) -> list[str]:
    return [part.casefold() for part in ref.relative_path.split("/")]


def _same_directory(temp_ref: PathRef, final_ref: PathRef) -> bool:
    if temp_ref.kind != final_ref.kind:
        return False
    if temp_ref.root_id.casefold() != final_ref.root_id.casefold():
        return False
    temp_parts = _relative_parts(temp_ref)
    final_parts = _relative_parts(final_ref)
    return (
        len(temp_parts) >= 2
        and len(final_parts) >= 2
        and temp_parts[:-1] == final_parts[:-1]
    )


def _validate_temp_ref(
    temp_ref: PathRef,
    final_ref: PathRef,
    delivery_id: str,
) -> None:
    if not _same_directory(temp_ref, final_ref):
        _require_library_error("Temporary file is outside the final directory")
    if temp_ref.target_key == final_ref.target_key:
        _require_library_error("Temporary file equals the final file")
    filename = temp_ref.relative_path.rsplit("/", 1)[-1]
    if delivery_id not in filename or ".part" not in filename:
        _require_library_error("Temporary filename must contain delivery id and .part")


def _validate_checkpoint_payload(
    payload: object,
    delivery_id: str,
    final_ref: PathRef,
) -> str:
    if not isinstance(payload, dict) or set(payload) != _CHECKPOINT_KEYS:
        _require_library_error("Checkpoint schema/keys do not match")
    if not _is_int(payload["schema_version"]) or payload["schema_version"] != 1:
        _require_library_error("Unsupported checkpoint schema version")
    if payload["delivery_id"] != delivery_id:
        _require_library_error("Checkpoint delivery_id does not match")
    temp_ref = path_ref_from_object(payload["temp_ref"])
    _validate_temp_ref(temp_ref, final_ref, delivery_id)
    _validate_file_id(payload["file_id"])
    _validate_hex64(payload["source_identity"], "source identity")
    _validate_strong_etag(payload["strong_etag"])
    expected_total = payload["expected_total"]
    if expected_total is not None and (
        not _is_int(expected_total) or expected_total <= 0
    ):
        _require_library_error("Invalid expected total")
    verified_bytes = payload["verified_bytes"]
    if not _is_int(verified_bytes) or verified_bytes < 0:
        _require_library_error("Invalid verified byte count")
    if expected_total is not None and verified_bytes > expected_total:
        _require_library_error("Verified bytes exceed expected total")
    _validate_hex64(payload["prefix_sha256"], "prefix sha256")
    media_type = payload["media_type"]
    if media_type is not None and media_type not in _SUPPORTED_MEDIA:
        _require_library_error("Unsupported checkpoint media type")
    if (
        not _is_int(payload["validation_policy_version"])
        or payload["validation_policy_version"] != 1
    ):
        _require_library_error("Unsupported validation policy version")
    try:
        return canonical_json(payload)
    except (ContractError, TypeError, ValueError) as exc:
        raise _lib().LibraryError("Checkpoint violates JSON contract") from exc


def _validate_prepared_payload(
    payload: object,
    delivery_id: str,
    final_ref: PathRef,
) -> str:
    if not isinstance(payload, dict) or set(payload) != _PREPARED_KEYS:
        _require_library_error("Prepared evidence schema/keys do not match")
    if not _is_int(payload["schema_version"]) or payload["schema_version"] != 1:
        _require_library_error("Unsupported prepared schema version")
    if payload["delivery_id"] != delivery_id:
        _require_library_error("Prepared delivery_id does not match")
    temp_ref = path_ref_from_object(payload["temp_ref"])
    prepared_final = path_ref_from_object(payload["final_ref"])
    if prepared_final.target_key != final_ref.target_key:
        _require_library_error("Prepared final_ref does not match delivery output")
    _validate_temp_ref(temp_ref, final_ref, delivery_id)
    _validate_file_id(payload["file_id"])
    _validate_hex64(payload["source_identity"], "source identity")
    size = payload["size"]
    if not _is_int(size) or size <= 0:
        _require_library_error("Invalid prepared size")
    _validate_hex64(payload["sha256"], "prepared sha256")
    if payload["media_type"] not in _SUPPORTED_MEDIA:
        _require_library_error("Unsupported prepared media type")
    if payload["verification"] != "structure_checked":
        _require_library_error("Unsupported verification mode")
    if (
        not _is_int(payload["validation_policy_version"])
        or payload["validation_policy_version"] != 1
    ):
        _require_library_error("Unsupported validation policy version")
    for name in ("width", "height", "frames"):
        if not _is_int(payload[name]) or payload[name] <= 0:
            _require_library_error(f"Invalid prepared {name}")
    try:
        return canonical_json(payload)
    except (ContractError, TypeError, ValueError) as exc:
        raise _lib().LibraryError("Prepared evidence violates JSON contract") from exc


def _parse_checkpoint_json(
    raw: object,
    delivery_id: str,
    final_ref: PathRef,
):
    lib = _lib()
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise lib.CorruptDataError("Checkpoint JSON is not text")
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, RecursionError) as exc:
        raise lib.CorruptDataError("Corrupt checkpoint JSON") from exc
    if not isinstance(decoded, dict):
        # P2 stored source-page cursors here. A JSON scalar/vector is legacy
        # cursor data, not a transfer checkpoint.
        return None
    transfer_markers = {"temp_ref", "file_id", "prefix_sha256", "verified_bytes"}
    if not (transfer_markers & set(decoded)):
        return None
    try:
        _validate_checkpoint_payload(decoded, delivery_id, final_ref)
    except lib.LibraryError as exc:
        raise lib.CorruptDataError("Corrupt transfer checkpoint") from exc
    return decoded


def _parse_prepared_json(
    raw: object,
    delivery_id: str,
    final_ref: PathRef,
):
    lib = _lib()
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise lib.CorruptDataError("Prepared JSON is not text")
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, RecursionError) as exc:
        raise lib.CorruptDataError("Corrupt prepared JSON") from exc
    if not isinstance(decoded, dict):
        raise lib.CorruptDataError("Corrupt prepared JSON")
    try:
        _validate_prepared_payload(decoded, delivery_id, final_ref)
    except lib.LibraryError as exc:
        raise lib.CorruptDataError("Corrupt prepared evidence") from exc
    return decoded


def _artifact_is_valid(value) -> bool:
    """P3 artifacts are usable only with complete structure-checked evidence."""
    try:
        verification = value["verification"]
        size = value["size"]
        sha256 = value["sha256"]
        media_type = value["media_type"]
    except (KeyError, TypeError):
        return False
    return (
        verification == "structure_checked"
        and _is_int(size)
        and size > 0
        and isinstance(sha256, str)
        and _HEX64_RE.fullmatch(sha256) is not None
        and media_type in _SUPPORTED_MEDIA
    )


class DeliveryRepositoryMixin:
    """SQL-only delivery protocol; uses the repository's existing writer."""

    _DELIVERY_COMMAND_KINDS = _DELIVERY_COMMAND_KINDS

    # ------------------------------------------------------------------
    # Public API (all writes enqueue to the one existing writer)
    # ------------------------------------------------------------------

    def start_transfer_attempt(
        self, delivery_id: str, batch_id: str, *, limit: int = 3
    ) -> int:
        lib = _lib()
        delivery_id = lib._validate_identifier(delivery_id, "delivery id")
        batch_id = lib._validate_identifier(batch_id, "batch id")
        if not _is_int(limit) or limit < 1 or limit > 1000:
            raise lib.LibraryError("limit must be a positive integer")
        return int(
            self._enqueue(
                "start_transfer_attempt",
                {"delivery_id": delivery_id, "batch_id": batch_id, "limit": limit},
            )
        )

    def save_transfer_checkpoint(
        self, delivery_id: str, batch_id: str, checkpoint: dict
    ) -> None:
        lib = _lib()
        delivery_id = lib._validate_identifier(delivery_id, "delivery id")
        batch_id = lib._validate_identifier(batch_id, "batch id")
        if not isinstance(checkpoint, dict):
            raise lib.LibraryError("checkpoint must be a dictionary")
        try:
            canonical_json(checkpoint)
        except (ContractError, TypeError, ValueError) as exc:
            raise lib.LibraryError("Checkpoint violates JSON contract") from exc
        self._enqueue(
            "save_transfer_checkpoint",
            {
                "delivery_id": delivery_id,
                "batch_id": batch_id,
                "checkpoint": checkpoint,
            },
        )

    def begin_validation(self, delivery_id: str, batch_id: str) -> None:
        lib = _lib()
        delivery_id = lib._validate_identifier(delivery_id, "delivery id")
        batch_id = lib._validate_identifier(batch_id, "batch id")
        self._enqueue(
            "begin_validation",
            {"delivery_id": delivery_id, "batch_id": batch_id},
        )

    def prepare_delivery(
        self, delivery_id: str, batch_id: str, evidence: dict
    ) -> None:
        lib = _lib()
        delivery_id = lib._validate_identifier(delivery_id, "delivery id")
        batch_id = lib._validate_identifier(batch_id, "batch id")
        if not isinstance(evidence, dict):
            raise lib.LibraryError("evidence must be a dictionary")
        try:
            canonical_json(evidence)
        except (ContractError, TypeError, ValueError) as exc:
            raise lib.LibraryError("Prepared evidence violates JSON contract") from exc
        self._enqueue(
            "prepare_delivery",
            {"delivery_id": delivery_id, "batch_id": batch_id, "evidence": evidence},
        )

    def confirm_delivery(self, delivery_id: str) -> dict:
        lib = _lib()
        delivery_id = lib._validate_identifier(delivery_id, "delivery id")
        return self._enqueue("confirm_delivery", {"delivery_id": delivery_id})

    def set_delivery_issue(
        self,
        delivery_id: str,
        batch_id: str,
        *,
        state: str,
        error_code: str,
        expected_state: str | None = None,
    ) -> None:
        """Park one delivery on a durable issue fact.

        expected_state is an optional compare-and-set guard: the writer
        refuses the write when the row already moved, so a recovery
        compensation can notice a competing winner instead of overwriting it
        (RFA-07).
        """
        lib = _lib()
        delivery_id = lib._validate_identifier(delivery_id, "delivery id")
        batch_id = lib._validate_identifier(batch_id, "batch id")
        if state not in {"needs_verification", "recovery_needed"}:
            raise lib.LibraryError("Unsupported delivery issue state")
        error_code = lib._validate_error_code(error_code)
        if expected_state is not None and (
            not isinstance(expected_state, str)
            or _DELIVERY_STATE_RE.fullmatch(expected_state) is None
        ):
            raise lib.LibraryError("Unsupported expected delivery state")
        self._enqueue(
            "set_delivery_issue",
            {
                "delivery_id": delivery_id,
                "batch_id": batch_id,
                "state": state,
                "error_code": error_code,
                "expected_state": expected_state,
            },
        )
    def list_recoverable(self, *, limit: int = 200) -> tuple[dict, ...]:
        lib = _lib()
        self._ensure_open()
        if not _is_int(limit):
            raise lib.LibraryError("limit must be an integer")
        limit = max(0, min(limit, 200))
        if limit == 0:
            return ()
        with closing(self._readonly_connection()) as conn:
            rows = conn.execute(
                "SELECT * FROM deliveries "
                "WHERE state IN ('prepared','recovery_needed') "
                "ORDER BY rowid LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._delivery_dict(row) for row in rows)

    # ------------------------------------------------------------------
    # W5: bounded read-only view of the pages a user has to decide about
    # ------------------------------------------------------------------

    def list_verification_pages(
        self,
        *,
        batch_id: str | None = None,
        states: object = None,
        after: object = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return one stable page of delivery rows the user may need to resolve.

        This is the read half of the W5 entry: it never opens a file, never
        hashes, never touches the network and never writes.  A row is one
        selected page (batch_item_pages) whose delivery is in one of the
        requested states, so a page carried into a later batch is listed under
        that batch instead of disappearing.

        recoverable and actions say whether recover_verification_page can act
        on the row at all.  Only a held page or a plain pending page owns a
        decide-and-link step, and the batch has to be running: the command
        refuses a paused batch with batch_not_running, so offering the button
        there would be a dead end.  Every other row is reported for the
        operator and left untouched.

        The cursor is the row primary key plus its delivery id, ordered the
        same way, so an accept that inserts an earlier row cannot make a later
        page appear twice or be skipped.  limit=0 returns no rows and no
        cursor on purpose: the caller asked for counts only.

        read_revision is the ledger's event revision read inside the very same
        short transaction as stats.total and the rows, so one response always
        describes one read version.  It is monotone and never decreases; a
        caller can compare two responses and say "these are two different
        reads" without pretending they were one snapshot.  observed_at is the
        wall clock of that read, for display only.
        """
        lib = _lib()
        self._ensure_open()
        if batch_id is not None:
            batch_id = lib._validate_identifier(batch_id, "batch id")
        selected = _verification_states(states)
        cursor = _verification_cursor(after)
        if not _is_int(limit):
            raise lib.LibraryError("limit must be an integer")
        if not 0 <= limit <= _VERIFICATION_MAX_LIMIT:
            raise lib.LibraryError(
                f"limit must be between 0 and {_VERIFICATION_MAX_LIMIT}"
            )
        base = (
            " FROM batch_item_pages AS bip"
            " JOIN deliveries AS d ON d.delivery_id = bip.delivery_id"
            " LEFT JOIN batches AS b ON b.batch_id = bip.batch_id"
        )
        condition = ["d.state IN (" + ",".join("?" * len(selected)) + ")"]
        params: list[Any] = list(selected)
        if batch_id is not None:
            condition.append("bip.batch_id = ?")
            params.append(batch_id)
        # The scope (states plus batch) and the continuation are separate
        # conditions: the total counts the whole scope, while the page starts
        # after the cursor.  Mixing the two would make every later page
        # report a smaller total than the one before it.
        scope_where = " WHERE " + " AND ".join(condition)
        row_condition = list(condition)
        row_params = list(params)
        if cursor is not None:
            row_condition.append(
                "(bip.batch_id, bip.illust_id, bip.page_index, d.delivery_id)"
                " > (?, ?, ?, ?)"
            )
            row_params.extend(cursor)
        row_where = " WHERE " + " AND ".join(row_condition)
        select = (
            "SELECT bip.batch_id, d.job_id, bip.illust_id, bip.page_index,"
            " d.delivery_id, d.state, d.error_code, d.generation, d.updated_at,"
            " d.output_key, d.attempts, d.retry_at, b.phase AS batch_phase"
        )
        with closing(self._readonly_connection()) as conn:
            # One short read transaction: the revision, the total and the page
            # come from the same snapshot, so "truncated" describes exactly the
            # rows it ships and read_revision names that read.
            # The revision rides in the same statement as the total: one more
            # column, not one more round trip, so a page read is still two
            # statements and the revision describes this very snapshot.
            total, revision = conn.execute(
                "SELECT COUNT(*), (SELECT COALESCE(MAX(event_seq), 0) FROM events)"
                + base
                + scope_where,
                params,
            ).fetchone()
            total, revision = int(total), int(revision)
            if limit == 0:
                fetched: list[sqlite3.Row] = []
            else:
                fetched = conn.execute(
                    select
                    + base
                    + row_where
                    + " ORDER BY bip.batch_id, bip.illust_id, bip.page_index,"
                    " d.delivery_id LIMIT ?",
                    (*row_params, limit + 1),
                ).fetchall()
        truncated = len(fetched) > limit
        rows = [self._verification_row(row) for row in fetched[:limit]]
        next_cursor = (
            (
                rows[-1]["batch_id"],
                rows[-1]["illust_id"],
                rows[-1]["page_index"],
                rows[-1]["delivery_id"],
            )
            if truncated and rows
            else None
        )
        return {
            "rows": rows,
            "next_cursor": next_cursor,
            "truncated": truncated,
            "read_revision": revision,
            "observed_at": lib._utcnow(),
            "stats": {
                "batch_id": batch_id,
                "states": list(selected),
                "limit": limit,
                "total": total,
                "returned": len(rows),
                "truncated": truncated,
                "read_revision": revision,
            },
        }

    def _verification_row(self, row: sqlite3.Row) -> dict[str, Any]:
        """Project one joined row; payloads and the filesystem stay untouched."""
        state = str(row["state"])
        phase = row["batch_phase"]
        # A held page in a paused batch is a real fact the operator must see,
        # but recovery cannot act on it: report the reason instead of a button.
        running = phase is not None and str(phase) == "running"
        recoverable = state in _RECOVERABLE_PAGE_STATES and running
        return {
            "batch_id": str(row["batch_id"]),
            "job_id": str(row["job_id"]),
            "illust_id": str(row["illust_id"]),
            "page_index": int(row["page_index"]),
            "delivery_id": str(row["delivery_id"]),
            "state": state,
            "error_code": row["error_code"],
            "generation": int(row["generation"]),
            "updated_at": row["updated_at"],
            "output_key": str(row["output_key"]),
            # attempts and retry_at are stored facts, not guesses: a waiting
            # page can say how often it failed and when it is due again.
            "attempts": int(row["attempts"]),
            "retry_at": row["retry_at"],
            "batch_phase": None if phase is None else str(phase),
            "recoverable": recoverable,
            "actions": ["verify"] if recoverable else [],
            "blocked_reason": (
                "batch_not_running"
                if state in _RECOVERABLE_PAGE_STATES and not running
                else None
            ),
        }

    def verification_summary(self, *, batch_id: str | None = None) -> dict[str, Any]:
        """Count the page rows by delivery state for one batch or the library.

        Read-only and bounded: three statements per call and no per-row loop in
        Python.  counts always carries the five listable states (zero when
        absent) plus any other state actually present, so a caller can render a
        stable set of chips.

        total_pages counts every selected page link.  linked_pages counts the
        pages that own a delivery row and therefore a state; existing_pages
        counts the pages that only carry a recorded artifact (a strict reuse
        from another batch, where the page was never downloaded again); the
        remainder is unstarted_pages.  The page table's CHECK constraint
        (delivery_id IS NOT NULL OR artifact_id IS NOT NULL) makes that
        remainder zero today, and the field is reported instead of inferred so
        a caller never has to guess which of the two kinds a page is.

        A batch id that does not exist is not an error: the counts are empty,
        known is false and a caller can say so without catching anything.

        truncated is always false: a GROUP BY aggregate cannot be cut.  The key
        exists so a future capped implementation reports the same shape.

        read_revision and observed_at come from the same short read as the
        counts.  They are the summary's own version, not a claim that a page
        list read in another call saw the same snapshot.
        """
        lib = _lib()
        self._ensure_open()
        if batch_id is not None:
            batch_id = lib._validate_identifier(batch_id, "batch id")
        with closing(self._readonly_connection()) as conn:
            if batch_id is None:
                batch = conn.execute(
                    "SELECT batch_id, phase FROM batches"
                    " ORDER BY created_at DESC, rowid DESC LIMIT 1"
                ).fetchone()
            else:
                batch = conn.execute(
                    "SELECT batch_id, phase FROM batches WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()
            scope = "" if batch_id is None else " WHERE bip.batch_id = ?"
            params: tuple[Any, ...] = () if batch_id is None else (batch_id,)
            counts = {state: 0 for state in _VERIFICATION_LIST_STATES}
            for state, number in conn.execute(
                "SELECT d.state, COUNT(*) FROM batch_item_pages AS bip"
                " JOIN deliveries AS d ON d.delivery_id = bip.delivery_id"
                + scope
                + " GROUP BY d.state",
                params,
            ):
                counts[str(state)] = int(number)
            total, recoverable, existing, revision = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN d.state IN"
                " ('needs_verification','pending') AND b.phase = 'running'"
                " THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN"
                " bip.delivery_id IS NULL AND bip.artifact_id IS NOT NULL"
                " THEN 1 ELSE 0 END), 0),"
                " (SELECT COALESCE(MAX(event_seq), 0) FROM events)"
                " FROM batch_item_pages AS bip"
                " LEFT JOIN deliveries AS d ON d.delivery_id = bip.delivery_id"
                " LEFT JOIN batches AS b ON b.batch_id = bip.batch_id"
                + scope,
                params,
            ).fetchone()
            revision = int(revision)
        linked = sum(counts.values())
        existing_pages = int(existing)
        total_pages = int(total)
        # Never let an inconsistent ledger produce a negative remainder: the
        # three counts must add up, and a page can only be linked or existing.
        unstarted = max(0, total_pages - linked - existing_pages)
        return {
            "batch_id": batch_id,
            "latest_batch_id": None if batch is None else str(batch["batch_id"]),
            "phase": None if batch is None else str(batch["phase"]),
            "known": batch is not None,
            "counts": counts,
            "recoverable": int(recoverable),
            "total_pages": total_pages,
            "linked_pages": linked,
            "existing_pages": existing_pages,
            "unstarted_pages": unstarted,
            "truncated": False,
            "read_revision": revision,
            "observed_at": lib._utcnow(),
        }

    def list_cleanup_candidates(
        self,
        *,
        limit: int = 200,
        after_rowid: int = 0,
        after_retry_rowid: int = 0,
        stats: dict[str, object] | None = None,
    ) -> tuple[dict, ...]:
        """Cancelled deliveries whose staged temp may still need deleting.

        The queue has two tiers.  Tier 0 rows were never attempted; tier 1 rows
        already carry a failed fact.  Tier 0 is served first so one locked file
        cannot dominate the queue, but a reservation keeps tier 1 reachable
        while fresh work keeps arriving: with a window of at least eight rows,
        one eighth is offered to tier 1 whenever tier 0 can fill the window.
        The tier 1 scan wraps inside this call only when it proved EOF, so a
        failed row is retried on every pass instead of only after the fresh
        backlog drains.

        Each tier reports its own TierScan: an empty fact list is not an
        end-of-queue proof.  A scan stopped by the examination cap reports
        budget_exhausted and a cursor past every examined row, so the next
        pass continues instead of wrapping onto the same unusable prefix
        (RFB-03, contract v4 Sec.4).  A terminal fact still leaves the queue
        before either tier is consulted (RFA-03).

        The optional stats sink reports examined / skipped / returned /
        next_rowid / next_retry_rowid / reserve plus the per-tier eof and
        budget flags for diagnostics.
        """
        lib = _lib()
        self._ensure_open()
        if not _is_int(limit):
            raise lib.LibraryError("limit must be an integer")
        if not _is_int(after_rowid) or after_rowid < 0:
            raise lib.LibraryError("after_rowid must be a non-negative integer")
        if not _is_int(after_retry_rowid) or after_retry_rowid < 0:
            raise lib.LibraryError("after_retry_rowid must be a non-negative integer")
        limit = max(0, min(limit, 200))
        if limit == 0:
            if stats is not None:
                stats.update(
                    {
                        "examined": 0,
                        "skipped": 0,
                        "returned": 0,
                        "next_rowid": int(after_rowid),
                        "next_retry_rowid": int(after_retry_rowid),
                        "reserve": 0,
                        "tier0_eof": False,
                        "tier1_eof": False,
                        "tier0_budget_exhausted": False,
                        "tier1_budget_exhausted": False,
                    }
                )
            return ()
        retry_reserve = 0 if limit < 8 else max(1, limit // 8)
        examine_cap = min(max(limit * 5, limit), 2000)
        with closing(self._readonly_connection()) as conn:
            tier0 = self._scan_cleanup_tier(
                conn, _CLEANUP_TIER0_SQL, int(after_rowid), limit, examine_cap
            )
            fresh = list(tier0.facts)
            tier1 = TierScan((), int(after_retry_rowid), False, False, 0, 0)
            if retry_reserve and len(fresh) >= limit:
                tier1 = self._retry_window(
                    conn, int(after_retry_rowid), retry_reserve, examine_cap
                )
                if tier1.facts:
                    fresh = fresh[: limit - len(tier1.facts)]
            elif len(fresh) < limit:
                tier1 = self._retry_window(
                    conn, int(after_retry_rowid), limit - len(fresh), examine_cap
                )
        facts = fresh + list(tier1.facts)
        # The fresh continuation follows the last offered row, so rows dropped
        # by the reservation are examined again instead of disappearing.  When
        # a capped scan offered nothing, it still moved past the rows it
        # examined; continuing there is what keeps an unusable prefix finite.
        next_rowid = int(fresh[-1]["rowid"]) if fresh else int(tier0.next_cursor)
        next_retry_rowid = (
            int(tier1.facts[-1]["rowid"]) if tier1.facts else int(tier1.next_cursor)
        )
        skipped = (tier0.examined - tier0.offered) + (
            tier1.examined - tier1.offered
        )
        if stats is not None:
            stats.update(
                {
                    "examined": tier0.examined + tier1.examined,
                    "skipped": skipped,
                    "returned": len(facts),
                    "next_rowid": next_rowid,
                    "next_retry_rowid": next_retry_rowid,
                    "reserve": retry_reserve,
                    "tier0_eof": tier0.eof,
                    "tier1_eof": tier1.eof,
                    "tier0_budget_exhausted": tier0.budget_exhausted,
                    "tier1_budget_exhausted": tier1.budget_exhausted,
                }
            )
        return tuple(facts)

    def _scan_cleanup_tier(
        self,
        conn: sqlite3.Connection,
        sql: str,
        cursor: int,
        want: int,
        examine_cap: int,
    ) -> TierScan:
        """Read one tier of the cleanup window, skipping unusable evidence.

        The result separates "nothing behind the cursor" from "the
        examination cap was reached".  Only a short page from the query
        proves EOF; a capped scan keeps the cursor after the last examined
        row so unusable rows cannot hold the window forever (RFB-03).
        """
        if want <= 0 or examine_cap <= 0:
            return TierScan((), int(cursor), False, want > 0, 0, 0)
        facts: list[dict] = []
        examined = 0
        eof = False
        while len(facts) < want and examined < examine_cap:
            take = min(want - len(facts), examine_cap - examined)
            rows = conn.execute(sql, (cursor, take)).fetchall()
            if not rows:
                eof = True
                break
            examined += len(rows)
            cursor = int(rows[-1]["_rowid"])
            for row in rows:
                fact = self._cleanup_candidate_fact(row)
                if fact is None:
                    continue
                facts.append(fact)
            if len(rows) < take:
                eof = True
                break
        budget_exhausted = bool(
            not eof and examined >= examine_cap and len(facts) < want
        )
        return TierScan(
            tuple(facts), int(cursor), eof, budget_exhausted, examined, len(facts)
        )

    def _retry_window(
        self,
        conn: sqlite3.Connection,
        cursor: int,
        want: int,
        examine_cap: int,
    ) -> TierScan:
        """Read the failed-retry tier, wrapping inside the call only at EOF."""
        scan = self._scan_cleanup_tier(
            conn, _CLEANUP_TIER1_SQL, int(cursor), want, examine_cap
        )
        if scan.facts or not scan.eof or cursor <= 0:
            return scan
        wrapped = self._scan_cleanup_tier(
            conn, _CLEANUP_TIER1_SQL, 0, want, max(0, examine_cap - scan.examined)
        )
        return TierScan(
            scan.facts + wrapped.facts,
            wrapped.next_cursor,
            wrapped.eof,
            wrapped.budget_exhausted,
            scan.examined + wrapped.examined,
            scan.offered + wrapped.offered,
        )

    def _cleanup_candidate_fact(self, row) -> dict | None:
        """Turn one cancelled row into a cleanup fact, or skip it honestly."""
        lib = _lib()
        try:
            final_ref = self._delivery_final_ref(row)
            prepared = _parse_prepared_json(
                row["prepared_json"], str(row["delivery_id"]), final_ref
            )
        except (lib.LibraryError, ContractError):
            return None
        fact = cleanup_fact(
            {
                "delivery_id": row["delivery_id"],
                "current_batch_id": row["current_batch_id"],
                "job_id": row["job_id"],
                "state": row["state"],
                "prepared": prepared,
                "generation": row["generation"],
            }
        )
        if fact is None:
            return None
        return {
            "delivery_id": fact.delivery_id,
            "batch_id": fact.batch_id,
            "job_id": fact.job_id,
            "prepared": dict(fact.prepared),
            "generation": fact.generation,
            "rowid": int(row["_rowid"]),
        }

    def mark_cleanup_completed(
        self,
        delivery_id: str,
        *,
        outcome: str,
        error_code: str = "",
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> bool:
        """Record the result of one cleanup attempt; never a delivery success.

        The expected digest and size are the prepared identity the executor
        observed.  The writer refuses a row whose evidence moved, so a
        replaced staged file can no longer be reported as cleaned (RFA-03).
        """
        lib = _lib()
        delivery_id = lib._validate_identifier(delivery_id, "delivery id")
        if outcome not in {"cleaned", "failed"}:
            raise lib.LibraryError("Unsupported cleanup outcome")
        if error_code:
            error_code = lib._validate_error_code(error_code)
        if expected_sha256 is not None:
            if (
                not isinstance(expected_sha256, str)
                or _HEX64_RE.fullmatch(expected_sha256) is None
            ):
                raise lib.LibraryError(
                    "expected_sha256 must be a lowercase hex digest"
                )
        if expected_size is not None:
            if not _is_int(expected_size) or expected_size < 0:
                raise lib.LibraryError(
                    "expected_size must be a non-negative integer"
                )
        return self._enqueue(
            "mark_cleanup_completed",
            {
                "delivery_id": delivery_id,
                "outcome": outcome,
                "error_code": error_code,
                "expected_sha256": expected_sha256,
                "expected_size": expected_size,
            },
        )
    def get_artifact(self, delivery_id: str) -> dict | None:
        lib = _lib()
        delivery_id = lib._validate_identifier(delivery_id, "delivery id")
        self._ensure_open()
        with closing(self._readonly_connection()) as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        return self._artifact_dict(row) if row is not None else None

    def batch_results(self, batch_id: str) -> dict:
        lib = _lib()
        batch_id = lib._validate_identifier(batch_id, "batch id")
        self._ensure_open()
        with closing(self._readonly_connection()) as conn:
            batch = conn.execute(
                "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise lib.NotFoundError(f"Unknown batch: {batch_id}")
            counts = self._results_counts(conn, batch_id)
        return {
            "batch_id": batch_id,
            "job_id": str(batch["job_id"]),
            "phase": str(batch["phase"]),
            "coverage_complete": (
                None
                if batch["coverage_complete"] is None
                else bool(batch["coverage_complete"])
            ),
            "stop_target": batch["stop_target"],
            "pages": counts["pages"],
            "works": counts["works"],
        }

    def request_block(self, batch_id: str, reason_code: str) -> None:
        lib = _lib()
        batch_id = lib._validate_identifier(batch_id, "batch id")
        if not isinstance(reason_code, str) or _REASON_RE.fullmatch(reason_code) is None:
            raise lib.LibraryError("Invalid block reason code")
        self._enqueue(
            "request_block",
            {"batch_id": batch_id, "reason_code": reason_code},
        )

    def finish_source(
        self,
        batch_id: str,
        *,
        coverage_complete: bool,
        reason_code: str = "source_exhausted",
    ) -> None:
        lib = _lib()
        batch_id = lib._validate_identifier(batch_id, "batch id")
        if type(coverage_complete) is not bool:
            raise lib.LibraryError("coverage_complete must be boolean")
        if not isinstance(reason_code, str) or _REASON_RE.fullmatch(reason_code) is None:
            raise lib.LibraryError("Invalid source finish reason code")
        self._enqueue(
            "finish_source",
            {
                "batch_id": batch_id,
                "coverage_complete": coverage_complete,
                "reason_code": reason_code,
            },
        )

    def settle_batch(self, batch_id: str) -> bool:
        lib = _lib()
        batch_id = lib._validate_identifier(batch_id, "batch id")
        return bool(self._enqueue("settle_batch", {"batch_id": batch_id}))

    # ------------------------------------------------------------------
    # Dispatch and private SQL handlers
    # ------------------------------------------------------------------

    def _dispatch_delivery(self, conn: sqlite3.Connection, command) -> Any:
        kind = command.kind
        payload = command.payload
        if kind == "start_transfer_attempt":
            return self._start_transfer_attempt(conn, payload)
        if kind == "save_transfer_checkpoint":
            return self._save_transfer_checkpoint(conn, payload)
        if kind == "begin_validation":
            return self._begin_validation(conn, payload)
        if kind == "prepare_delivery":
            return self._prepare_delivery(conn, payload)
        if kind == "confirm_delivery":
            return self._confirm_delivery(conn, payload)
        if kind == "set_delivery_issue":
            return self._set_delivery_issue(conn, payload)
        if kind == "request_block":
            return self._request_block(conn, payload)
        if kind == "finish_source":
            return self._finish_source(conn, payload)
        if kind == "settle_batch":
            return self._settle_batch(conn, payload)
        if kind == "mark_cleanup_completed":
            return self._mark_cleanup_completed(conn, payload)
        raise _lib().LibraryError(f"Unknown delivery command: {kind}")

    @staticmethod
    def _execution_phase_allowed(batch: sqlite3.Row) -> bool:
        return str(batch["phase"]) in {
            "running",
            "settling",
            "pausing",
            "cancelling",
        }

    def _owned_delivery_batch(
        self, conn: sqlite3.Connection, delivery_id: str, batch_id: str
    ):
        lib = _lib()
        delivery = self._get_delivery_row(conn, delivery_id)
        if delivery is None:
            raise lib.NotFoundError(f"Unknown delivery: {delivery_id}")
        batch = self._get_batch_row(conn, batch_id)
        if batch is None:
            raise lib.NotFoundError(f"Unknown batch: {batch_id}")
        if delivery["job_id"] != batch["job_id"]:
            raise lib.InvalidStateError("Delivery and batch belong to different jobs")
        if delivery["current_batch_id"] != batch_id:
            raise lib.InvalidStateError("Delivery is not owned by this batch")
        return delivery, batch

    def _delivery_final_ref(self, delivery: sqlite3.Row) -> PathRef:
        output_ref = _parse_json_object(
            delivery["output_ref_json"], "delivery output reference"
        )
        return path_ref_from_object(output_ref)

    def _start_transfer_attempt(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> int:
        lib = _lib()
        delivery_id = str(payload["delivery_id"])
        batch_id = str(payload["batch_id"])
        limit = int(payload["limit"])
        with self._transaction(conn):
            delivery, batch = self._owned_delivery_batch(
                conn, delivery_id, batch_id
            )
            if not self._execution_phase_allowed(batch):
                raise lib.InvalidStateError("Batch is not executing delivery work")
            if delivery["state"] not in {"transferring", "validating"}:
                raise lib.InvalidStateError("Delivery is not in an executing state")
            attempts = int(delivery["http_attempts"])
            if attempts >= limit:
                raise lib.InvalidStateError("HTTP attempt budget is exhausted")
            new_attempts = attempts + 1
            now = lib._utcnow()
            conn.execute(
                "UPDATE deliveries SET state = 'transferring', http_attempts = ?, "
                "error_code = NULL, updated_at = ? WHERE delivery_id = ?",
                (new_attempts, now, delivery_id),
            )
            self._insert_event(
                conn,
                job_id=str(delivery["job_id"]),
                batch_id=batch_id,
                kind="delivery_http_attempt",
                payload={
                    "delivery_id": delivery_id,
                    "batch_id": batch_id,
                    "http_attempts": new_attempts,
                    "limit": limit,
                },
            )
            return new_attempts

    def _save_transfer_checkpoint(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> None:
        lib = _lib()
        delivery_id = str(payload["delivery_id"])
        batch_id = str(payload["batch_id"])
        checkpoint = payload["checkpoint"]
        with self._transaction(conn):
            delivery, batch = self._owned_delivery_batch(
                conn, delivery_id, batch_id
            )
            if delivery["state"] != "transferring":
                raise lib.InvalidStateError(
                    "Checkpoint can only be saved for an owned transferring delivery"
                )
            final_ref = self._delivery_final_ref(delivery)
            canonical = _validate_checkpoint_payload(
                checkpoint, delivery_id, final_ref
            )
            old = _parse_checkpoint_json(
                delivery["checkpoint_json"], delivery_id, final_ref
            )
            if old is not None:
                old_temp = path_ref_from_object(old["temp_ref"])
                new_temp = path_ref_from_object(checkpoint["temp_ref"])
                if old_temp.target_key == new_temp.target_key:
                    if old["source_identity"] != checkpoint["source_identity"]:
                        raise lib.InvalidStateError(
                            "Source identity changed for the same temporary file"
                        )
                    if (
                        dict(old["file_id"]) == dict(checkpoint["file_id"])
                        and int(checkpoint["verified_bytes"])
                        < int(old["verified_bytes"])
                    ):
                        raise lib.InvalidStateError(
                            "Checkpoint verified offset moved backwards"
                        )
            now = lib._utcnow()
            conn.execute(
                "UPDATE deliveries SET checkpoint_json = ?, source_identity = ?, "
                "updated_at = ? WHERE delivery_id = ?",
                (canonical, checkpoint["source_identity"], now, delivery_id),
            )
            self._insert_event(
                conn,
                job_id=str(delivery["job_id"]),
                batch_id=batch_id,
                kind="delivery_checkpoint_saved",
                payload={
                    "delivery_id": delivery_id,
                    "batch_id": batch_id,
                    "verified_bytes": int(checkpoint["verified_bytes"]),
                    "source_identity": checkpoint["source_identity"],
                },
            )
            return None

    def _begin_validation(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> None:
        lib = _lib()
        delivery_id = str(payload["delivery_id"])
        batch_id = str(payload["batch_id"])
        with self._transaction(conn):
            delivery, batch = self._owned_delivery_batch(
                conn, delivery_id, batch_id
            )
            if delivery["state"] not in {"transferring", "validating"}:
                raise lib.InvalidStateError("Delivery is not transferring/validating")
            if delivery["state"] == "validating":
                return None
            now = lib._utcnow()
            conn.execute(
                "UPDATE deliveries SET state = 'validating', updated_at = ? "
                "WHERE delivery_id = ?",
                (now, delivery_id),
            )
            self._insert_event(
                conn,
                job_id=str(delivery["job_id"]),
                batch_id=batch_id,
                kind="delivery_validation_started",
                payload={"delivery_id": delivery_id, "batch_id": batch_id},
            )
            return None

    def _prepare_delivery(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> None:
        lib = _lib()
        delivery_id = str(payload["delivery_id"])
        batch_id = str(payload["batch_id"])
        evidence = payload["evidence"]
        with self._transaction(conn):
            delivery, batch = self._owned_delivery_batch(
                conn, delivery_id, batch_id
            )
            final_ref = self._delivery_final_ref(delivery)
            canonical = _validate_prepared_payload(
                evidence, delivery_id, final_ref
            )
            if delivery["state"] == "prepared":
                existing = _parse_prepared_json(
                    delivery["prepared_json"], delivery_id, final_ref
                )
                if existing is not None and canonical_json(existing) == canonical:
                    return None
                raise lib.InvalidStateError(
                    "Conflicting prepared evidence for an already prepared delivery"
                )
            if delivery["state"] != "validating":
                raise lib.InvalidStateError(
                    "prepare_delivery requires an owned validating delivery"
                )
            if not self._execution_phase_allowed(batch):
                raise lib.InvalidStateError("Batch is not executing delivery work")
            checkpoint = _parse_checkpoint_json(
                delivery["checkpoint_json"], delivery_id, final_ref
            )
            if checkpoint is None:
                raise lib.InvalidStateError("No valid transfer checkpoint")
            checkpoint_temp = path_ref_from_object(checkpoint["temp_ref"])
            evidence_temp = path_ref_from_object(evidence["temp_ref"])
            if checkpoint_temp.target_key != evidence_temp.target_key:
                raise lib.InvalidStateError("Prepared temp_ref differs from checkpoint")
            if dict(checkpoint["file_id"]) != dict(evidence["file_id"]):
                raise lib.InvalidStateError("Prepared file identity differs from checkpoint")
            if checkpoint["source_identity"] != evidence["source_identity"]:
                raise lib.InvalidStateError("Prepared source identity differs from checkpoint")
            if int(evidence["size"]) != int(checkpoint["verified_bytes"]):
                raise lib.InvalidStateError("Prepared size differs from verified bytes")
            if evidence["sha256"] != checkpoint["prefix_sha256"]:
                raise lib.InvalidStateError("Prepared hash differs from checkpoint prefix hash")
            now = lib._utcnow()
            conn.execute(
                "UPDATE deliveries SET prepared_json = ?, state = 'prepared', "
                "error_code = NULL, retry_at = NULL, updated_at = ? "
                "WHERE delivery_id = ?",
                (canonical, now, delivery_id),
            )
            self._insert_event(
                conn,
                job_id=str(delivery["job_id"]),
                batch_id=batch_id,
                kind="delivery_prepared",
                payload={
                    "delivery_id": delivery_id,
                    "batch_id": batch_id,
                    "size": int(evidence["size"]),
                    "sha256": evidence["sha256"],
                    "media_type": evidence["media_type"],
                    "verification": evidence["verification"],
                },
            )
            return None

    def _confirm_delivery(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> dict[str, Any]:
        lib = _lib()
        delivery_id = str(payload["delivery_id"])
        with self._transaction(conn):
            delivery = self._get_delivery_row(conn, delivery_id)
            if delivery is None:
                raise lib.NotFoundError(f"Unknown delivery: {delivery_id}")
            final_ref = self._delivery_final_ref(delivery)
            if delivery["state"] == "completed":
                artifact = conn.execute(
                    "SELECT * FROM artifacts WHERE delivery_id = ?", (delivery_id,)
                ).fetchone()
                if artifact is None:
                    raise lib.CorruptDataError(
                        "Completed delivery has no artifact"
                    )
                return self._artifact_dict(artifact)
            if delivery["state"] not in {"prepared", "recovery_needed"}:
                raise lib.InvalidStateError(
                    "confirm_delivery requires prepared/recovery_needed"
                )
            prepared = _parse_prepared_json(
                delivery["prepared_json"], delivery_id, final_ref
            )
            if prepared is None:
                raise lib.InvalidStateError("Delivery has no valid prepared evidence")
            current_batch_id = delivery["current_batch_id"]
            if current_batch_id is None:
                raise lib.InvalidStateError("Prepared delivery has no current batch")
            now = lib._utcnow()
            payload_json = _parse_json_object(
                delivery["payload_json"], "delivery payload"
            )
            artist_id = payload_json.get("artist_id")
            if not isinstance(artist_id, str) or not artist_id:
                artist_id = None
            artifact_id = lib._new_id()
            conn.execute(
                "INSERT INTO artifacts("
                "artifact_id, delivery_id, job_id, illust_id, page_index, artist_id, "
                "output_key, output_ref_json, quality_json, media_type, size, sha256, "
                "verification, completed_at, legacy_origin, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    artifact_id,
                    delivery_id,
                    str(delivery["job_id"]),
                    str(delivery["illust_id"]),
                    int(delivery["page_index"]),
                    artist_id,
                    str(delivery["output_key"]),
                    canonical_json(prepared["final_ref"]),
                    str(delivery["quality_json"]),
                    prepared["media_type"],
                    int(prepared["size"]),
                    prepared["sha256"],
                    prepared["verification"],
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE deliveries SET state = 'completed', completed_batch_id = ?, "
                "error_code = NULL, retry_at = NULL, updated_at = ? "
                "WHERE delivery_id = ?",
                (current_batch_id, now, delivery_id),
            )
            affected = conn.execute(
                "SELECT DISTINCT batch_id, illust_id FROM batch_item_pages "
                "WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchall()
            conn.execute(
                "UPDATE batch_item_pages SET artifact_id = ? "
                "WHERE delivery_id = ? AND batch_id = ?",
                (artifact_id, delivery_id, current_batch_id),
            )
            for row in affected:
                self._refresh_batch_item_result(
                    conn, str(row["batch_id"]), str(row["illust_id"]), now
                )
            self._insert_event(
                conn,
                job_id=str(delivery["job_id"]),
                batch_id=str(current_batch_id),
                kind="delivery_completed",
                payload={
                    "delivery_id": delivery_id,
                    "artifact_id": artifact_id,
                    "batch_id": str(current_batch_id),
                    "size": int(prepared["size"]),
                    "sha256": prepared["sha256"],
                    "media_type": prepared["media_type"],
                    "verification": prepared["verification"],
                },
            )
            artifact = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            return self._artifact_dict(artifact)

    def _set_delivery_issue(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> None:
        lib = _lib()
        delivery_id = str(payload["delivery_id"])
        batch_id = str(payload["batch_id"])
        state = str(payload["state"])
        error_code = str(payload["error_code"])
        expected_state = payload.get("expected_state")
        if state not in {"needs_verification", "recovery_needed"}:
            raise lib.LibraryError("Unsupported delivery issue state")
        with self._transaction(conn):
            delivery, batch = self._owned_delivery_batch(
                conn, delivery_id, batch_id
            )
            if expected_state is not None and str(delivery["state"]) != str(
                expected_state
            ):
                # A competing writer already moved the row.  The caller has to
                # report the winner instead of overwriting it (RFA-07).
                raise lib.InvalidStateError(
                    "Delivery state changed before the issue was recorded"
                )
            if delivery["state"] == "completed":
                raise lib.InvalidStateError("Completed delivery is immutable")
            allowed = {
                "transferring",
                "validating",
                "prepared",
                "recovery_needed",
            }
            if delivery["state"] == state:
                return None
            if delivery["state"] not in allowed:
                raise lib.InvalidStateError(
                    "Delivery issue is not legal in this state"
                )
            now = lib._utcnow()
            conn.execute(
                "UPDATE deliveries SET state = ?, error_code = ?, retry_at = NULL, "
                "updated_at = ? WHERE delivery_id = ?",
                (state, error_code, now, delivery_id),
            )
            self._insert_event(
                conn,
                job_id=str(delivery["job_id"]),
                batch_id=batch_id,
                kind="delivery_issue",
                payload={
                    "delivery_id": delivery_id,
                    "batch_id": batch_id,
                    "state": state,
                    "error_code": error_code,
                },
            )
            for row in conn.execute(
                "SELECT DISTINCT batch_id, illust_id FROM batch_item_pages "
                "WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchall():
                self._refresh_batch_item_result(
                    conn, str(row["batch_id"]), str(row["illust_id"]), now
                )
            return None

    def _cleanup_fact(self, delivery: sqlite3.Row):
        """Rebuild the durable cleanup fact from the row itself.

        The row is the only authority on which staged file this delivery owns,
        so the writer never trusts a caller-supplied fact.
        """
        lib = _lib()
        delivery_id = str(delivery["delivery_id"])
        try:
            final_ref = self._delivery_final_ref(delivery)
            prepared = _parse_prepared_json(
                delivery["prepared_json"], delivery_id, final_ref
            )
        except (lib.LibraryError, ContractError) as exc:
            raise lib.CorruptDataError(
                "Cancelled delivery has unreadable prepared evidence"
            ) from exc
        fact = cleanup_fact(
            {
                "delivery_id": delivery_id,
                "current_batch_id": str(delivery["current_batch_id"]),
                "job_id": str(delivery["job_id"]),
                "state": str(delivery["state"]),
                "prepared": prepared,
                "generation": int(delivery["generation"]),
            }
        )
        if fact is None:
            raise lib.CorruptDataError(
                "Cancelled delivery cannot produce a cleanup fact"
            )
        return fact

    def _cleanup_fact_recorded(
        self, conn: sqlite3.Connection, delivery_id: str, body: dict[str, Any]
    ) -> bool:
        """Whether this exact cleanup fact is already recorded.

        Scoped to one delivery instead of the last 200 events, so a busy ledger
        cannot hide an older terminal fact (RFA-03).
        """
        rows = conn.execute(
            "SELECT safe_payload FROM events WHERE kind = 'delivery_cleanup' "
            "AND safe_payload LIKE ? ORDER BY event_seq DESC LIMIT 8",
            (f'%"delivery_id":"{delivery_id}"%',),
        ).fetchall()
        for row in rows:
            try:
                info = json.loads(row["safe_payload"])
            except (TypeError, ValueError):
                continue
            if (
                not isinstance(info, dict)
                or str(info.get("delivery_id")) != delivery_id
            ):
                continue
            if cleanup_is_terminal(info) and not cleanup_is_terminal(body):
                # A terminal fact already ended this queue entry; a later
                # failure cannot make it retryable again.
                return True
            if (
                info.get("outcome") == body["outcome"]
                and info.get("error_code") == body["error_code"]
                and info.get("temp_sha256") == body["temp_sha256"]
                and info.get("temp_size") == body["temp_size"]
            ):
                return True
        return False

    def _mark_cleanup_completed(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> bool:
        lib = _lib()
        delivery_id = str(payload["delivery_id"])
        outcome = str(payload["outcome"])
        error_code = str(payload.get("error_code", ""))
        expected_sha = payload.get("expected_sha256")
        expected_size = payload.get("expected_size")
        if outcome not in {"cleaned", "failed"}:
            raise lib.LibraryError("Unsupported cleanup outcome")
        with self._transaction(conn):
            delivery = self._get_delivery_row(conn, delivery_id)
            if delivery is None:
                raise lib.NotFoundError(f"Unknown delivery: {delivery_id}")
            if str(delivery["state"]) != "cancelled":
                return False
            fact = self._cleanup_fact(delivery)
            if expected_sha is not None and fact.sha256 != str(expected_sha):
                # The caller verified one exact staged object.  A row whose
                # prepared evidence moved now describes a different file, so a
                # terminal fact would be a lie about bytes nobody checked.
                raise lib.InvalidStateError(
                    "Prepared digest changed before cleanup was recorded"
                )
            if expected_size is not None and fact.size != int(expected_size):
                raise lib.InvalidStateError(
                    "Prepared size changed before cleanup was recorded"
                )
            body = cleanup_event_payload(
                fact, outcome=outcome, error_code=error_code
            )
            if self._cleanup_fact_recorded(conn, delivery_id, body):
                return outcome == "cleaned"
            self._insert_event(
                conn,
                job_id=str(delivery["job_id"]),
                batch_id=str(delivery["current_batch_id"]),
                kind="delivery_cleanup",
                payload=body,
            )
            return outcome == "cleaned"

    # ------------------------------------------------------------------
    # Batch lifecycle extensions
    # ------------------------------------------------------------------

    def _request_block(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> None:
        lib = _lib()
        batch_id = str(payload["batch_id"])
        reason = str(payload["reason_code"])
        with self._transaction(conn):
            batch = self._get_batch_row(conn, batch_id)
            if batch is None:
                raise lib.NotFoundError(f"Unknown batch: {batch_id}")
            phase = str(batch["phase"])
            job_id = str(batch["job_id"])
            now = lib._utcnow()
            if phase == "cancelling":
                # Explicit cancellation already in progress takes priority.
                conn.execute(
                    "UPDATE batches SET stop_target = COALESCE(stop_target, 'cancelled'), "
                    "stop_reason = COALESCE(stop_reason, ?) WHERE batch_id = ?",
                    (reason, batch_id),
                )
                return None
            if phase not in {"running", "cooling", "settling", "pausing"}:
                return None
            conn.execute(
                "UPDATE batches SET phase = 'pausing', active_slot = 1, "
                "stop_target = 'blocked', stop_reason = ? WHERE batch_id = ?",
                (reason, batch_id),
            )
            conn.execute(
                "UPDATE jobs SET state = 'pausing', updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )
            self._insert_event(
                conn,
                job_id=job_id,
                batch_id=batch_id,
                kind="batch_block_requested",
                payload={
                    "batch_id": batch_id,
                    "phase": "pausing",
                    "stop_target": "blocked",
                    "reason_code": reason,
                },
            )
            return None

    def _finish_source(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> None:
        lib = _lib()
        batch_id = str(payload["batch_id"])
        coverage = bool(payload["coverage_complete"])
        reason = str(payload["reason_code"])
        with self._transaction(conn):
            batch = self._get_batch_row(conn, batch_id)
            if batch is None:
                raise lib.NotFoundError(f"Unknown batch: {batch_id}")
            phase = str(batch["phase"])
            if phase == "settling":
                if (
                    batch["coverage_complete"] is not None
                    and bool(batch["coverage_complete"]) == coverage
                    and str(batch["stop_reason"] or "") == reason
                ):
                    return None
                raise lib.InvalidStateError(
                    "Batch is already settling with a different finish payload"
                )
            if phase != "running":
                raise lib.InvalidStateError("finish_source requires a running batch")
            now = lib._utcnow()
            conn.execute(
                "UPDATE batches SET phase = 'settling', coverage_complete = ?, "
                "stop_target = NULL, stop_reason = ? WHERE batch_id = ?",
                (int(coverage), reason, batch_id),
            )
            conn.execute(
                "UPDATE jobs SET state = 'settling', updated_at = ? WHERE job_id = ?",
                (now, str(batch["job_id"])),
            )
            self._insert_event(
                conn,
                job_id=str(batch["job_id"]),
                batch_id=batch_id,
                kind="source_finished",
                payload={
                    "batch_id": batch_id,
                    "coverage_complete": coverage,
                    "reason_code": reason,
                },
            )
            return None

    def _settle_batch(
        self, conn: sqlite3.Connection, payload: dict[str, Any]
    ) -> bool:
        lib = _lib()
        batch_id = str(payload["batch_id"])
        with self._transaction(conn):
            batch = self._get_batch_row(conn, batch_id)
            if batch is None or str(batch["phase"]) != "settling":
                return False
            now = lib._utcnow()
            self._refresh_batch_items_for_batch(conn, batch_id, now)
            busy = conn.execute(
                "SELECT 1 FROM batch_item_pages AS bip "
                "JOIN deliveries AS d ON d.delivery_id = bip.delivery_id "
                "WHERE bip.batch_id = ? AND d.state IN ("
                "'pending','transferring','validating','suspended','retry_wait'"
                ") LIMIT 1",
                (batch_id,),
            ).fetchone()
            if busy is not None:
                return False
            held = conn.execute(
                "SELECT 1 FROM deliveries "
                "WHERE job_id = ? AND state IN ('prepared','recovery_needed') LIMIT 1",
                (str(batch["job_id"]),),
            ).fetchone()
            counts = self._results_counts(conn, batch_id)
            pages = counts["pages"]
            works = counts["works"]
            budget = self._budget_counts_conn(conn, batch)
            deferred_works = int(budget["deferred_works"])
            download_row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(CASE WHEN result_state = 'completed' THEN 1 ELSE 0 END), 0) "
                "AS completed FROM batch_items "
                "WHERE batch_id = ? AND decision = 'download'",
                (batch_id,),
            ).fetchone()
            download_total = int(download_row["total"])
            download_completed = int(download_row["completed"])
            coverage = bool(batch["coverage_complete"])
            failure_states = {
                "failed",
                "partial",
                "needs_verification",
                "metadata_unknown",
                "unavailable",
                "unsupported",
                "cancelled",
            }
            has_failure = (
                int(pages["failed"]) > 0
                or int(pages["needs_verification"]) > 0
                or any(int(works.get(name, 0)) > 0 for name in failure_states)
            )
            all_pages_verified = int(pages["completed"]) == int(pages["total"])
            all_download_works_completed = download_completed == download_total
            all_selected_verified = all_pages_verified and all_download_works_completed
            if held is not None:
                decision = "blocked"
            elif has_failure:
                decision = "partial"
            elif coverage and all_selected_verified and deferred_works == 0:
                decision = "completed"
            else:
                # Leftover budget-deferred work is unfinished, not a failure.
                decision = "paused"
            next_phase = decision
            job_state = decision
            if decision == "blocked":
                stop_target = "blocked"
                stop_reason = str(batch["stop_reason"] or "prepared_pending")
            elif decision == "partial":
                stop_target = None
                stop_reason = str(batch["stop_reason"] or "partial_delivery")
            elif decision == "completed":
                stop_target = None
                stop_reason = str(batch["stop_reason"] or "source_exhausted")
            else:
                stop_target = "paused"
                stop_reason = str(
                    batch["stop_reason"]
                    or (
                        "budget_deferred"
                        if deferred_works > 0
                        else "incomplete_coverage"
                    )
                )
            conn.execute(
                "UPDATE batches SET phase = ?, active_slot = NULL, stop_target = ?, "
                "stop_reason = ?, ended_at = ? WHERE batch_id = ?",
                (
                    next_phase,
                    stop_target,
                    stop_reason,
                    now,
                    batch_id,
                ),
            )
            conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (job_state, now, str(batch["job_id"])),
            )
            self._insert_event(
                conn,
                job_id=str(batch["job_id"]),
                batch_id=batch_id,
                kind="batch_settled",
                payload={
                    "batch_id": batch_id,
                    "decision": decision,
                    "coverage_complete": coverage,
                    "pages": pages,
                    "works": works,
                    "deferred_works": deferred_works,
                },
            )
            return True

    # ------------------------------------------------------------------
    # Association and result accounting helpers
    # ------------------------------------------------------------------

    def _link_batch_item_page(
        self,
        conn: sqlite3.Connection,
        batch_id: str,
        illust_id: str,
        page_index: int,
        output_key: str,
        quality_json: str,
        *,
        delivery_id: str | None,
        artifact_id: str | None,
        overwrite: bool = True,
    ) -> None:
        if delivery_id is None and artifact_id is None:
            raise _lib().LibraryError("Selected page needs a delivery or artifact")
        conflict = (
            "DO UPDATE SET "
            "delivery_id = excluded.delivery_id, "
            "artifact_id = excluded.artifact_id, "
            "output_key = excluded.output_key, "
            "quality_json = excluded.quality_json"
            if overwrite
            else "DO NOTHING"
        )
        conn.execute(
            "INSERT INTO batch_item_pages("
            "batch_id, illust_id, page_index, delivery_id, artifact_id, "
            "output_key, quality_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(batch_id, illust_id, page_index) " + conflict,
            (
                batch_id,
                illust_id,
                int(page_index),
                delivery_id,
                artifact_id,
                output_key,
                quality_json,
            ),
        )

    def _find_reusable_artifact_candidates(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: str,
        illust_id: str,
        page_index: int,
        output_key: str,
        original: bool,
        validation_policy_version: int,
        limit: int = 16,
    ) -> list:
        """Return quality-matching historical artifacts, newest first.

        This is only candidate selection for the file-I/O preflight.  Reuse
        itself must go through _find_reusable_artifact with current health.
        """
        if type(limit) is not int or not 1 <= limit <= 64:
            raise _lib().LibraryError("Invalid artifact candidate limit")
        rows = conn.execute(
            "SELECT a.* FROM artifacts AS a "
            "JOIN deliveries AS ad ON ad.delivery_id = a.delivery_id "
            "WHERE ad.job_id != ? AND a.illust_id = ? AND a.page_index = ? "
            "AND a.output_key = ? AND a.verification = 'structure_checked' "
            "AND a.size IS NOT NULL AND a.sha256 IS NOT NULL "
            "ORDER BY a.completed_at DESC, a.artifact_id DESC LIMIT ?",
            (job_id, illust_id, int(page_index), output_key, int(limit)),
        ).fetchall()
        candidates = []
        for artifact in rows:
            if not _artifact_is_valid(artifact):
                continue
            try:
                quality = _parse_json_object(
                    artifact["quality_json"], "artifact quality"
                )
            except Exception:
                continue
            if (
                quality.get("original") is original
                and quality.get("validation_policy_version")
                == validation_policy_version
            ):
                candidates.append(artifact)
        return candidates

    def _find_reusable_artifact(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: str,
        illust_id: str,
        page_index: int,
        output_key: str,
        original: bool,
        validation_policy_version: int,
        health_by_artifact=None,
        candidates=None,
    ):
        """Select a current, hash-matched artifact; never a bare receipt.

        health_by_artifact is the frozen preflight evidence.  Without it this
        returns None (fail closed): a database row alone is history, not proof
        that the file still exists with the recorded bytes.
        """
        if health_by_artifact is None:
            return None
        if candidates is None:
            candidates = self._find_reusable_artifact_candidates(
                conn,
                job_id=job_id,
                illust_id=illust_id,
                page_index=page_index,
                output_key=output_key,
                original=original,
                validation_policy_version=validation_policy_version,
            )
        for artifact in candidates:
            health = health_by_artifact.get(str(artifact["artifact_id"]))
            if health is None or not health.reusable:
                continue
            if not health.expectation.matches_row(artifact):
                continue
            return artifact
        return None

    def _ensure_claimed_page_link(
        self,
        conn: sqlite3.Connection,
        batch_id: str,
        delivery: sqlite3.Row,
    ) -> None:
        """Freeze this claim's selected page when the copied source lacked it."""
        delivery_id = str(delivery["delivery_id"])
        illust_id = str(delivery["illust_id"])
        page_index = int(delivery["page_index"])
        existing = conn.execute(
            "SELECT 1 FROM batch_item_pages WHERE batch_id = ? AND illust_id = ? "
            "AND page_index = ?",
            (batch_id, illust_id, page_index),
        ).fetchone()
        if existing is not None:
            return
        artifact = conn.execute(
            "SELECT artifact_id FROM artifacts WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        self._link_batch_item_page(
            conn,
            batch_id,
            illust_id,
            page_index,
            str(delivery["output_key"]),
            str(delivery["quality_json"]),
            delivery_id=delivery_id,
            artifact_id=None if artifact is None else str(artifact["artifact_id"]),
        )

    def _copy_batch_item_page_links(
        self,
        conn: sqlite3.Connection,
        source_batch_id: str,
        target_batch_id: str,
        illust_id: str,
        *,
        health_by_artifact=None,
        enforce_completed_health: bool = False,
    ) -> None:
        """Copy links for a recovered work without reusing unverified bytes.

        Existing target links are never overwritten: the accept path already
        made a current health decision for this batch.  With enforcement on, a
        completed source link is copied only when the frozen preflight says its
        bytes still match; otherwise a fresh needs_verification delivery is
        created instead of claiming completion.
        """
        rows = conn.execute(
            "SELECT * FROM batch_item_pages WHERE batch_id = ? AND illust_id = ?",
            (source_batch_id, illust_id),
        ).fetchall()
        target_batch = self._get_batch_row(conn, target_batch_id)
        if target_batch is None:
            raise _lib().NotFoundError(f"Unknown batch: {target_batch_id}")
        target_job_id = str(target_batch["job_id"])
        for row in rows:
            target_exists = conn.execute(
                "SELECT 1 FROM batch_item_pages WHERE batch_id = ? AND illust_id = ? "
                "AND page_index = ?",
                (target_batch_id, illust_id, int(row["page_index"])),
            ).fetchone()
            if target_exists is not None:
                continue
            artifact_id = row["artifact_id"]
            delivery_id = row["delivery_id"]
            source_delivery = None
            if delivery_id is not None:
                source_delivery = conn.execute(
                    "SELECT * FROM deliveries WHERE delivery_id = ?",
                    (str(delivery_id),),
                ).fetchone()
            if (
                source_delivery is not None
                and str(source_delivery["state"]) == "cancelled"
                and is_supersede_code(source_delivery["error_code"])
            ):
                # The historical link points at a row that a later owner
                # deliberately superseded.  Resolve the successor instead of
                # copying a cancelled reference into the active batch.
                successor = self._resolve_carried_successor(conn, source_delivery)
                if successor is not None and successor.is_completed:
                    artifact = conn.execute(
                        "SELECT * FROM artifacts WHERE artifact_id = ?",
                        (successor.artifact_id,),
                    ).fetchone()
                    if artifact is not None:
                        gate = getattr(self, "_health_allows_reuse", None)
                        health = (
                            None
                            if health_by_artifact is None
                            else health_by_artifact.get(str(artifact["artifact_id"]))
                        )
                        if callable(gate) and gate(health, artifact):
                            same_job = str(artifact["job_id"]) == target_job_id
                            self._link_batch_item_page(
                                conn,
                                target_batch_id,
                                illust_id,
                                int(row["page_index"]),
                                str(row["output_key"]),
                                str(row["quality_json"]),
                                delivery_id=(
                                    str(artifact["delivery_id"]) if same_job else None
                                ),
                                artifact_id=str(artifact["artifact_id"]),
                                overwrite=False,
                            )
                            continue
                elif successor is not None and successor.state == "needs_verification":
                    self._link_batch_item_page(
                        conn,
                        target_batch_id,
                        illust_id,
                        int(row["page_index"]),
                        str(row["output_key"]),
                        str(row["quality_json"]),
                        delivery_id=successor.delivery_id,
                        artifact_id=None,
                        overwrite=False,
                    )
                    continue
                self._create_carried_unverified_delivery(
                    conn,
                    target_batch_id,
                    illust_id,
                    int(row["page_index"]),
                    str(row["output_key"]),
                    str(row["quality_json"]),
                    str(source_delivery["delivery_id"]),
                    error_code=(
                        "carried_superseded_unverified"
                        if successor is not None and successor.is_completed
                        else "carried_superseded_unresolved"
                    ),
                )
                continue
            artifact = None
            if artifact_id is not None:
                artifact = conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_id = ?",
                    (str(artifact_id),),
                ).fetchone()
            elif delivery_id is not None:
                artifact = conn.execute(
                    "SELECT * FROM artifacts WHERE delivery_id = ?",
                    (str(delivery_id),),
                ).fetchone()
                if artifact is not None:
                    artifact_id = str(artifact["artifact_id"])
            if artifact is not None and enforce_completed_health:
                gate = getattr(self, "_health_allows_reuse", None)
                health = None if health_by_artifact is None else health_by_artifact.get(
                    str(artifact["artifact_id"])
                )
                if not callable(gate) or not gate(health, artifact):
                    source_delivery = str(artifact["delivery_id"])
                    self._create_carried_unverified_delivery(
                        conn,
                        target_batch_id,
                        illust_id,
                        int(row["page_index"]),
                        str(row["output_key"]),
                        str(row["quality_json"]),
                        source_delivery,
                    )
                    continue
            self._link_batch_item_page(
                conn,
                target_batch_id,
                illust_id,
                int(row["page_index"]),
                str(row["output_key"]),
                str(row["quality_json"]),
                delivery_id=None if delivery_id is None else str(delivery_id),
                artifact_id=None if artifact_id is None else str(artifact_id),
                overwrite=False,
            )

    def _resolve_carried_successor(self, conn, delivery):
        """Resolve a superseded link for carry.

        The superseding batch records the replacement on its own page link;
        following that link is exact and works across jobs.  Everything else
        goes through the one lineage entry point, so carry and recovery never
        disagree about which delivery owns the page: a recorded handover event,
        then a newer completed row, then the bounded superseded chain (contract
        Sec.3.1).  An older completed row that no event names is history, not a
        successor — exactly what the previous local "newest completed" scan
        used to get wrong.
        """
        link = conn.execute(
            "SELECT delivery_id, artifact_id FROM batch_item_pages "
            "WHERE batch_id = ? AND illust_id = ? AND page_index = ?",
            (
                str(delivery["current_batch_id"]),
                str(delivery["illust_id"]),
                int(delivery["page_index"]),
            ),
        ).fetchone()
        if link is not None:
            if link["artifact_id"] is not None:
                artifact = conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_id = ?",
                    (str(link["artifact_id"]),),
                ).fetchone()
                if artifact is not None:
                    row = conn.execute(
                        "SELECT * FROM deliveries WHERE delivery_id = ?",
                        (str(artifact["delivery_id"]),),
                    ).fetchone()
                    if row is not None:
                        return Successor(
                            delivery_id=str(row["delivery_id"]),
                            job_id=str(row["job_id"]),
                            batch_id=str(row["current_batch_id"]),
                            state="completed",
                            generation=int(row["generation"]),
                            artifact_id=str(artifact["artifact_id"]),
                        )
            elif link["delivery_id"] is not None and str(
                link["delivery_id"]
            ) != str(delivery["delivery_id"]):
                row = conn.execute(
                    "SELECT * FROM deliveries WHERE delivery_id = ?",
                    (str(link["delivery_id"]),),
                ).fetchone()
                if row is not None and str(row["state"]) in ACTIVE_FOLLOWUP:
                    return Successor(
                        delivery_id=str(row["delivery_id"]),
                        job_id=str(row["job_id"]),
                        batch_id=str(row["current_batch_id"]),
                        state=str(row["state"]),
                        generation=int(row["generation"]),
                        artifact_id=None,
                    )
        return resolve_successor(conn, delivery)

    def _create_carried_unverified_delivery(
        self,
        conn: sqlite3.Connection,
        target_batch_id: str,
        illust_id: str,
        page_index: int,
        output_key: str,
        quality_json: str,
        source_delivery_id: str,
        *,
        error_code: str = "artifact_unverified_carry",
    ) -> None:
        lib = _lib()
        batch = self._get_batch_row(conn, target_batch_id)
        if batch is None:
            raise lib.NotFoundError(f"Unknown batch: {target_batch_id}")
        source = self._get_delivery_row(conn, source_delivery_id)
        if source is None:
            raise lib.CorruptDataError("Carried completed page has no source delivery")
        job_id = str(batch["job_id"])
        quality_key = str(source["quality_key"])
        blocker = conn.execute(
            "SELECT delivery_id, state FROM deliveries WHERE output_key = ? "
            "AND state IN ("
            "'pending','transferring','validating','prepared',"
            "'needs_verification','suspended','retry_wait','recovery_needed'"
            ") ORDER BY rowid LIMIT 1",
            (output_key,),
        ).fetchone()
        if blocker is not None:
            if str(blocker["state"]) == "needs_verification":
                # Link the existing hold instead of creating a second active
                # row for the same output; the batch honestly reports NV.
                self._link_batch_item_page(
                    conn,
                    target_batch_id,
                    illust_id,
                    int(page_index),
                    output_key,
                    quality_json,
                    delivery_id=str(blocker["delivery_id"]),
                    artifact_id=None,
                )
                return
            raise lib.OutputConflict(
                "carried page output is held by another active delivery"
            )
        max_generation = conn.execute(
            "SELECT COALESCE(MAX(generation), 0) AS generation FROM deliveries "
            "WHERE job_id = ? AND illust_id = ? AND page_index = ? AND quality_key = ?",
            (job_id, illust_id, int(page_index), quality_key),
        ).fetchone()["generation"]
        generation = int(max_generation) + 1
        delivery_id = lib._new_id()
        now = lib._utcnow()
        try:
            conn.execute(
                "INSERT INTO deliveries("
                "delivery_id, job_id, origin_batch_id, current_batch_id, "
                "completed_batch_id, illust_id, page_index, output_key, "
                "output_ref_json, quality_json, quality_key, payload_json, "
                "source_identity, state, attempts, retry_at, error_code, "
                "generation, checkpoint_json, prepared_json, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'needs_verification', 0, NULL, ?, ?, ?, NULL, ?, ?)",
                (
                    delivery_id,
                    job_id,
                    target_batch_id,
                    target_batch_id,
                    illust_id,
                    int(page_index),
                    output_key,
                    str(source["output_ref_json"]),
                    quality_json,
                    quality_key,
                    str(source["payload_json"]),
                    None if source["source_identity"] is None else str(source["source_identity"]),
                    error_code,
                    generation,
                    # A transfer checkpoint embeds its owning delivery id; it
                    # is not portable to a new generation.  Keep the carried
                    # hold readable (checkpoint_json NULL) instead of copying
                    # corrupt evidence.
                    None,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise lib.OutputConflict(
                "carried page output is held by another active delivery"
            ) from exc
        self._link_batch_item_page(
            conn,
            target_batch_id,
            illust_id,
            int(page_index),
            output_key,
            quality_json,
            delivery_id=delivery_id,
            artifact_id=None,
        )

    def _refresh_batch_item_result(
        self,
        conn: sqlite3.Connection,
        batch_id: str,
        illust_id: str,
        now: str,
    ) -> None:
        lib = _lib()
        item = conn.execute(
            "SELECT * FROM batch_items WHERE batch_id = ? AND illust_id = ?",
            (batch_id, illust_id),
        ).fetchone()
        if item is None or item["decision"] != "download":
            return
        try:
            required = json.loads(item["required_pages_json"])
        except (TypeError, ValueError) as exc:
            raise lib.CorruptDataError("Corrupt required page list") from exc
        if not isinstance(required, list):
            raise lib.CorruptDataError("Corrupt required page list")
        required_pages = sorted(
            {int(value) for value in required if _is_int(value) and 0 <= value <= 9999}
        )
        if not required_pages:
            return
        success = 0
        failed = 0
        cancelled = 0
        unverified = 0
        pending = 0
        for page_index in required_pages:
            row = conn.execute(
                "SELECT * FROM batch_item_pages WHERE batch_id = ? "
                "AND illust_id = ? AND page_index = ?",
                (batch_id, illust_id, page_index),
            ).fetchone()
            if row is None:
                pending += 1
                continue
            artifact_id = row["artifact_id"]
            delivery_id = row["delivery_id"]
            delivery_state = None
            if delivery_id is not None:
                delivery = conn.execute(
                    "SELECT state FROM deliveries WHERE delivery_id = ?",
                    (delivery_id,),
                ).fetchone()
                if delivery is not None:
                    delivery_state = str(delivery["state"])
                    if artifact_id is None:
                        artifact = conn.execute(
                            "SELECT artifact_id, verification, size, sha256, media_type "
                            "FROM artifacts WHERE delivery_id = ?",
                            (delivery_id,),
                        ).fetchone()
                        if artifact is not None:
                            artifact_id = str(artifact["artifact_id"])
            if artifact_id is not None:
                artifact = conn.execute(
                    "SELECT artifact_id, verification, size, sha256, media_type "
                    "FROM artifacts WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if artifact is not None and _artifact_is_valid(artifact):
                    success += 1
                    continue
            if delivery_state == "cancelled":
                cancelled += 1
            elif delivery_state == "failed":
                failed += 1
            elif delivery_state in {"needs_verification", "recovery_needed"}:
                unverified += 1
            else:
                pending += 1
        total = len(required_pages)
        if success == total:
            result = "completed"
        elif success > 0:
            result = "partial"
        elif unverified > 0 and pending == 0:
            result = "needs_verification"
        elif failed > 0 and cancelled + failed == total:
            result = "failed"
        elif cancelled == total:
            result = "cancelled"
        else:
            result = "pending"
        conn.execute(
            "UPDATE batch_items SET result_state = ?, updated_at = ? "
            "WHERE batch_id = ? AND illust_id = ?",
            (result, now, batch_id, illust_id),
        )

    def _refresh_batch_items_for_batch(
        self, conn: sqlite3.Connection, batch_id: str, now: str
    ) -> None:
        rows = conn.execute(
            "SELECT illust_id FROM batch_items "
            "WHERE batch_id = ? AND decision = 'download'",
            (batch_id,),
        ).fetchall()
        for row in rows:
            self._refresh_batch_item_result(conn, batch_id, str(row["illust_id"]), now)

    def _refresh_items_for_job(
        self, conn: sqlite3.Connection, job_id: str, now: str
    ) -> None:
        rows = conn.execute(
            "SELECT bi.batch_id, bi.illust_id FROM batch_items AS bi "
            "JOIN batches AS b ON b.batch_id = bi.batch_id "
            "WHERE b.job_id = ? AND bi.decision = 'download'",
            (job_id,),
        ).fetchall()
        for row in rows:
            self._refresh_batch_item_result(
                conn, str(row["batch_id"]), str(row["illust_id"]), now
            )

    def _refresh_items_for_delivery(
        self, conn: sqlite3.Connection, delivery_id: str, now: str
    ) -> None:
        rows = conn.execute(
            "SELECT DISTINCT batch_id, illust_id FROM batch_item_pages "
            "WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchall()
        for row in rows:
            self._refresh_batch_item_result(
                conn, str(row["batch_id"]), str(row["illust_id"]), now
            )

    def _reset_failed_deliveries_for_resume(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        now: str,
    ) -> int:
        """Explicit resume retry: only failed deliveries, never successful ones."""
        lib = _lib()
        rows = conn.execute(
            "SELECT delivery_id, illust_id, page_index, http_attempts, output_key "
            "FROM deliveries WHERE job_id = ? AND state = 'failed' "
            "ORDER BY rowid",
            (job_id,),
        ).fetchall()
        reset: list[dict[str, Any]] = []
        for row in rows:
            delivery_id = str(row["delivery_id"])
            blocker = conn.execute(
                "SELECT delivery_id FROM deliveries WHERE output_key = ? "
                "AND delivery_id != ? AND state IN ("
                "'pending','transferring','validating','prepared',"
                "'needs_verification','suspended','retry_wait','recovery_needed'"
                ") LIMIT 1",
                (str(row["output_key"]), delivery_id),
            ).fetchone()
            if blocker is not None:
                raise lib.OutputConflict(
                    "failed delivery path is held by another active delivery"
                )
            conn.execute(
                "UPDATE deliveries SET state = 'suspended', http_attempts = 0, "
                "error_code = NULL, retry_at = NULL, updated_at = ? "
                "WHERE delivery_id = ? AND state = 'failed'",
                (now, delivery_id),
            )
            reset.append(
                {
                    "delivery_id": delivery_id,
                    "illust_id": str(row["illust_id"]),
                    "page_index": int(row["page_index"]),
                    "previous_http_attempts": int(row["http_attempts"]),
                }
            )
        if reset:
            self._refresh_items_for_job(conn, job_id, now)
            self._insert_event(
                conn,
                job_id=job_id,
                batch_id=None,
                kind="deliveries_retry_reset",
                payload={
                    "job_id": job_id,
                    "count": len(reset),
                    "deliveries": reset,
                },
            )
        return len(reset)

    def _results_counts(
        self, conn: sqlite3.Connection, batch_id: str
    ) -> dict[str, dict[str, int]]:
        page_row = conn.execute(
            f"SELECT "
            "COUNT(*) AS total, "
            f"COALESCE(SUM(CASE WHEN {_VALID_ARTIFACT_SQL} THEN 1 ELSE 0 END),0) "
            "AS completed, "
            f"COALESCE(SUM(CASE WHEN {_VALID_ARTIFACT_SQL} AND "
            "COALESCE(ad.completed_batch_id, d.completed_batch_id) = ? "
            "THEN 1 ELSE 0 END),0) AS new_completed, "
            f"COALESCE(SUM(CASE WHEN {_VALID_ARTIFACT_SQL} AND "
            "(COALESCE(ad.completed_batch_id, d.completed_batch_id) IS NULL OR "
            "COALESCE(ad.completed_batch_id, d.completed_batch_id) != ?) "
            "THEN 1 ELSE 0 END),0) AS existing, "
            "COALESCE(SUM(CASE WHEN NOT ("
            f"{_VALID_ARTIFACT_SQL}"
            ") AND d.state = 'failed' THEN 1 ELSE 0 END),0) AS failed, "
            "COALESCE(SUM(CASE WHEN NOT ("
            f"{_VALID_ARTIFACT_SQL}"
            ") AND d.state IN ('needs_verification','recovery_needed') "
            "THEN 1 ELSE 0 END),0) AS needs_verification, "
            "COALESCE(SUM(CASE WHEN NOT ("
            f"{_VALID_ARTIFACT_SQL}"
            ") AND d.state = 'cancelled' THEN 1 ELSE 0 END),0) AS cancelled, "
            "COALESCE(SUM(CASE WHEN NOT ("
            f"{_VALID_ARTIFACT_SQL}"
            ") AND (d.delivery_id IS NULL OR d.state NOT IN "
            "('failed','needs_verification','recovery_needed','cancelled')) "
            "THEN 1 ELSE 0 END),0) AS pending "
            "FROM ("
            "SELECT bip.*, "
            "COALESCE(bip.artifact_id, abd.artifact_id) AS eff_artifact_id, "
            "COALESCE(bip.delivery_id, aref.delivery_id, abd.delivery_id) "
            "AS eff_delivery_id "
            "FROM batch_item_pages AS bip "
            "LEFT JOIN artifacts AS abd ON abd.delivery_id = bip.delivery_id "
            "LEFT JOIN artifacts AS aref ON aref.artifact_id = bip.artifact_id "
            "WHERE bip.batch_id = ?"
            ") AS p "
            "LEFT JOIN artifacts AS a ON a.artifact_id = p.eff_artifact_id "
            "LEFT JOIN deliveries AS d ON d.delivery_id = p.eff_delivery_id "
            "LEFT JOIN deliveries AS ad ON ad.delivery_id = a.delivery_id",
            (batch_id, batch_id, batch_id),
        ).fetchone()
        pages = {name: int(page_row[name] or 0) for name in _PAGE_KEYS}
        workspace = {
            "total": 0,
            "download": 0,
            **{name: 0 for name in _RESULT_KEYS},
        }
        for row in conn.execute(
            "SELECT result_state, COUNT(*) AS count FROM batch_items "
            "WHERE batch_id = ? GROUP BY result_state",
            (batch_id,),
        ).fetchall():
            count = int(row["count"])
            workspace["total"] += count
            workspace[str(row["result_state"])] = count
        download = conn.execute(
            "SELECT COUNT(*) AS count FROM batch_items "
            "WHERE batch_id = ? AND decision = 'download'",
            (batch_id,),
        ).fetchone()
        workspace["download"] = int(download["count"])
        return {"pages": pages, "works": workspace}

    def _budget_counts_conn(
        self, conn: sqlite3.Connection, batch: sqlite3.Row
    ) -> dict[str, int]:
        lib = _lib()
        budget = _parse_json_object(batch["budget_json"], "batch budget")
        max_works = budget.get("max_works")
        if not _is_int(max_works) or max_works < 1:
            raise lib.CorruptDataError("Corrupt batch work budget")
        batch_id = str(batch["batch_id"])
        used = int(
            conn.execute(
                "SELECT COUNT(DISTINCT illust_id) AS count FROM batch_items "
                "WHERE batch_id = ? AND counts_work = 1",
                (batch_id,),
            ).fetchone()["count"]
        )
        deferred = int(
            conn.execute(
                "SELECT COUNT(DISTINCT d.illust_id) AS count FROM deliveries AS d "
                "WHERE d.job_id = ? "
                "AND d.state IN ('pending','suspended','retry_wait') "
                "AND (d.state != 'retry_wait' OR d.retry_at IS NULL "
                "OR julianday(d.retry_at) IS NULL "
                "OR julianday(d.retry_at) <= julianday('now')) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM batch_items AS bi WHERE bi.batch_id = ? "
                "AND bi.illust_id = d.illust_id AND bi.decision = 'download'"
                ")",
                (str(batch["job_id"]), batch_id),
            ).fetchone()["count"]
        )
        return {
            "used_works": used,
            "max_works": int(max_works),
            "deferred_works": deferred,
        }

    def _artifact_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            output_ref = _parse_json_object(
                row["output_ref_json"], "artifact output reference"
            )
            quality = _parse_json_object(row["quality_json"], "artifact quality")
        except Exception as exc:
            raise _lib().CorruptDataError("Corrupt artifact row") from exc
        return {
            "artifact_id": row["artifact_id"],
            "delivery_id": row["delivery_id"],
            "job_id": row["job_id"],
            "illust_id": row["illust_id"],
            "page_index": int(row["page_index"]),
            "artist_id": row["artist_id"],
            "output_key": row["output_key"],
            "output_ref_json": row["output_ref_json"],
            "quality_json": row["quality_json"],
            "media_type": row["media_type"],
            "size": None if row["size"] is None else int(row["size"]),
            "sha256": row["sha256"],
            "verification": row["verification"],
            "completed_at": row["completed_at"],
            "legacy_origin": row["legacy_origin"],
            "created_at": row["created_at"],
            "output_ref": output_ref,
            "quality": quality,
        }

    # Parsed field helpers used by LibraryRepository._delivery_dict.
    def _parse_transfer_checkpoint_json(
        self, raw: object, delivery_id: str, final_ref: PathRef
    ):
        return _parse_checkpoint_json(raw, delivery_id, final_ref)

    def _parse_prepared_json(
        self, raw: object, delivery_id: str, final_ref: PathRef
    ):
        return _parse_prepared_json(raw, delivery_id, final_ref)
