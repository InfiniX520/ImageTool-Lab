"""Archive schema repository on the existing single-writer transport.

This class is deliberately opt-in for already migrated temporary copies.  It
keeps the production LibraryRepository v1 guard and _enqueue transport unchanged,
and does not migrate, discover, or open a production database automatically.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from core.archive_inventory import ArchiveInventory, CapacityPlan
from core.archive_scope import ContentResolution, ObservedFile, ScopeReceipt
from core.archive_plan_store import (
    ArchivePlanConflictError, ArchivePlanSaveResult, ArchivePlanStoreError,
    PreparedArchivePlan, StoredArchivePlan, load_archive_plan_in_transaction,
    prepare_archive_plan, save_prepared_plan_in_transaction,
)
from core.archive_schema import (
    ARCHIVE_SCHEMA_VERSION, ARCHIVE_TABLES, ArchiveMigrationError, PathAcceptance,
    _check_existing_objects, apply_content_resolution, load_content_binding,
)
from core.preservation_policy import ContentVersion
from core.library_repository import (
    LibraryBusyError, LibraryRepository, SchemaVersionError, _LIBRARY_FORMAT,
)
from core.task_contracts import ContractError


class ArchiveLibraryError(ArchivePlanStoreError):
    """The archive repository cannot safely accept a command or connection."""


class ArchiveLibraryBusyError(ArchiveLibraryError):
    """Another writer or reader holds the SQLite lock past the configured timeout."""


class ArchiveLibraryRepository(LibraryRepository):
    """Use one inherited writer/lock for immutable plans on an explicit copy.

    The caller must migrate and close a temporary copy first.  This subclass
    never performs migration or accepts schema 1/future versions.  No production
    entrypoint constructs it, so the current desktop and v1 schema stay unchanged.
    """

    def __init__(self, database_path: str | Path, *, queue_capacity: int = 128,
                 temporary_copy_confirmed: bool = False):
        if temporary_copy_confirmed is not True:
            raise ArchiveMigrationError(
                "Archive repository requires an explicit temporary-copy acknowledgement"
            )
        super().__init__(database_path, queue_capacity=queue_capacity)

    def _inspect_existing(self) -> bool:
        if not self._path.exists() or self._path.stat().st_size == 0:
            raise SchemaVersionError(
                "Archive repository requires an existing migrated archive copy "
                "(current schema %s)" % ARCHIVE_SCHEMA_VERSION
            )
        try:
            conn = self._readonly_connection()
        except sqlite3.Error as exc:
            raise SchemaVersionError("Archive copy is not a readable SQLite library") from exc
        try:
            tables = {
                str(row[0]) for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "schema_meta" not in tables:
                raise SchemaVersionError("Archive copy has no library schema marker")
            metadata = {
                str(row[0]): str(row[1]) for row in conn.execute(
                    'SELECT "key", "value" FROM schema_meta'
                )
            }
            if metadata.get("format") != _LIBRARY_FORMAT:
                raise SchemaVersionError("Archive copy belongs to a different product")
            if metadata.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
                raise SchemaVersionError(
                    "Archive repository requires exactly schema " + ARCHIVE_SCHEMA_VERSION
                )
            if not ARCHIVE_TABLES <= tables:
                raise SchemaVersionError("Archive copy has incomplete archive tables")
            _check_existing_objects(conn)
            return False
        except SchemaVersionError:
            raise
        except sqlite3.Error as exc:
            raise SchemaVersionError("Archive copy schema cannot be read") from exc
        finally:
            conn.close()

    def _dispatch(self, conn: sqlite3.Connection, command):
        if command.kind == "save_archive_plan":
            prepared = command.payload["prepared"]
            if not isinstance(prepared, PreparedArchivePlan):
                raise ArchivePlanStoreError("Queued archive plan is not a prepared plan")
            try:
                with self._transaction(conn):
                    return save_prepared_plan_in_transaction(conn, prepared)
            except ContractError as exc:
                raise ArchiveLibraryError("Archive plan facts were rejected") from exc
        if command.kind == "apply_observed_plan":
            prepared = command.payload["prepared"]
            items = command.payload["items"]
            bound_at = command.payload["bound_at"]
            if not isinstance(prepared, PreparedArchivePlan):
                raise ArchivePlanStoreError("Queued archive plan is not a prepared plan")
            try:
                with self._transaction(conn):
                    stored = conn.execute(
                        "SELECT 1 FROM archive_batches WHERE batch_id=?",
                        (prepared.batch_id,),
                    ).fetchone()
                    if stored is not None:
                        # A replay only re-proves the stored plan.  It must not accept
                        # the queued observations again or move any current binding.
                        return save_prepared_plan_in_transaction(conn, prepared), ()
                    acceptances = tuple(sorted(
                        (
                            apply_content_resolution(
                                conn, resolution, observed=observed, bound_at=bound_at,
                            )
                            for resolution, observed in items
                        ),
                        key=lambda acceptance: acceptance.path_key,
                    ))
                    for asset in prepared.inventory.assets:
                        binding = load_content_binding(conn, asset.path)
                        if (
                            binding is None
                            or binding.content != asset.content
                            or binding.acceptance_id is None
                        ):
                            raise ArchivePlanConflictError(
                                "Accepted binding does not match the planned content")
                    return save_prepared_plan_in_transaction(conn, prepared), acceptances
            except ContractError as exc:
                raise ArchiveLibraryError(
                    "Archive observation and plan facts were rejected") from exc
        if command.kind == "apply_archive_resolutions":
            items = command.payload["items"]
            bound_at = command.payload["bound_at"]
            try:
                with self._transaction(conn):
                    return tuple(sorted(
                        (
                            apply_content_resolution(
                                conn, resolution, observed=observed, bound_at=bound_at,
                            )
                            for resolution, observed in items
                        ),
                        key=lambda acceptance: acceptance.path_key,
                    ))
            except ContractError as exc:
                raise ArchiveLibraryError("Archive resolution facts were rejected") from exc
        if command.kind == "load_archive_plan":
            try:
                with self._transaction(conn):
                    return load_archive_plan_in_transaction(conn, command.payload["batch_id"])
            except ContractError as exc:
                raise ArchiveLibraryError("Archive plan facts were rejected") from exc
        return super()._dispatch(conn, command)

    def save_archive_plan(
        self, inventory: ArchiveInventory, capacity: CapacityPlan, *,
        scope: dict[str, object] | None = None,
        receipt: ScopeReceipt | None = None,
        root_revision: int | None = None,
    ) -> ArchivePlanSaveResult:
        """Enqueue one immutable plan; file hashing never enters this transaction."""
        if not isinstance(inventory, ArchiveInventory) or not isinstance(capacity, CapacityPlan):
            raise ArchiveLibraryError("Archive inventory and capacity are required")
        if scope is not None and not isinstance(scope, dict):
            raise ArchiveLibraryError("Archive scope must be a JSON object")
        if inventory.batch_id != capacity.batch_id:
            raise ArchiveLibraryError("Inventory and capacity batch IDs differ")
        try:
            prepared = prepare_archive_plan(
                inventory, capacity, scope=scope, receipt=receipt,
                root_revision=root_revision,
            )
        except ContractError as exc:
            raise ArchiveLibraryError("Archive plan input was rejected") from exc
        try:
            return self._enqueue(
                "save_archive_plan",
                {"prepared": prepared},
                size_payload={
                    "batch_id": prepared.batch_id,
                    "payload_bytes": prepared.payload_bytes,
                    "segment_count": len(prepared.segments),
                    "scope_bytes": len(prepared.scope_json.encode("utf-8")),
                    "capacity_bytes": len(prepared.capacity_json.encode("utf-8")),
                    "receipt_bytes": (
                        0 if prepared.scope_receipt_json is None
                        else len(prepared.scope_receipt_json.encode("utf-8"))
                    ),
                },
            )
        except LibraryBusyError as exc:
            raise ArchiveLibraryBusyError("Archive library is busy") from exc
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise ArchiveLibraryBusyError("Archive library is busy") from exc
            raise

    def apply_archive_resolutions(
        self, items,
    ) -> tuple[PathAcceptance, ...]:
        """Accept one batch of resolved observations in a single transaction.

        This is the only write path for an accepted generation: every expected old
        binding is rechecked inside this writer's transaction, the observations,
        acceptances and path bindings are committed together, and any refusal
        rolls the whole batch back.  Callers resolve outside this transaction.
        """
        self._ensure_open()
        supplied = tuple(items)
        if not supplied:
            raise ArchiveLibraryError("At least one resolved observation is required")
        for item in supplied:
            if (not isinstance(item, (tuple, list)) or len(item) != 2
                    or not isinstance(item[0], ContentResolution)
                    or not isinstance(item[1], ObservedFile)):
                raise ArchiveLibraryError(
                    "Each item must pair one content resolution with its observation"
                )
            if item[0].requires_attention or item[0].content is None:
                raise ArchiveLibraryError(
                    "An unresolved or unconfirmed decision cannot be applied"
                )
            if item[0].candidate != item[1].candidate:
                raise ArchiveLibraryError(
                    "A resolution and its observation must name the same candidate"
                )
        bound_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            return self._enqueue(
                "apply_archive_resolutions",
                {"items": supplied, "bound_at": bound_at},
                size_payload={
                    "kind": "apply_archive_resolutions",
                    "bound_at": bound_at,
                    "path_keys": sorted(
                        item[0].candidate.path.identity_key for item in supplied
                    ),
                },
            )
        except LibraryBusyError as exc:
            raise ArchiveLibraryBusyError("Archive library is busy") from exc
        except ContractError as exc:
            raise ArchiveLibraryError("Archive resolution input was rejected") from exc

    def apply_observed_plan(
        self, prepared: PreparedArchivePlan, items,
    ) -> tuple[ArchivePlanSaveResult, tuple[PathAcceptance, ...]]:
        """Accept one batch of observations and save their plan in one transaction.

        This is the only command that may move a current path binding and persist the
        plan that describes it.  Every file was observed outside this transaction;
        inside it the expected binding tokens are rechecked, the accepted evidence is
        recorded, each plan asset must then be bound to exactly the accepted content
        with a live acceptance, and only then is the immutable plan written.  A replay
        of an already saved batch accepts nothing again: it re-runs the plan's own
        strict comparison and changes no binding.
        """
        self._ensure_open()
        if not isinstance(prepared, PreparedArchivePlan):
            raise ArchiveLibraryError("An observed plan command needs one prepared plan")
        supplied = tuple(items)
        if not supplied:
            raise ArchiveLibraryError("At least one resolved observation is required")
        try:
            receipt = ScopeReceipt.from_json(prepared.scope_receipt_json)
        except (ContractError, ValueError, TypeError) as exc:
            raise ArchiveLibraryError("Prepared plan receipt cannot be read") from exc
        observed_paths: dict[str, ContentVersion] = {}
        for item in supplied:
            if (not isinstance(item, (tuple, list)) or len(item) != 2
                    or not isinstance(item[0], ContentResolution)
                    or not isinstance(item[1], ObservedFile)):
                raise ArchiveLibraryError(
                    "Each item must pair one content resolution with its observation")
            resolution, observed = item
            if resolution.requires_attention or resolution.content is None:
                raise ArchiveLibraryError(
                    "An unresolved or unconfirmed decision cannot be applied")
            if resolution.candidate != observed.candidate:
                raise ArchiveLibraryError(
                    "A resolution and its observation must name the same candidate")
            if (
                resolution.candidate.root_revision != receipt.root_revision
                or resolution.candidate.snapshot_id != receipt.snapshot_id
            ):
                # A decision taken from another enumeration belongs to another scope
                # snapshot (or another root generation) and cannot be written into
                # this plan, even when its content happens to match.
                raise ArchiveLibraryError(
                    "An observed item does not belong to the plan's scope snapshot")
            path_key = resolution.candidate.path.identity_key
            if path_key in observed_paths:
                raise ArchiveLibraryError("One path may enter the batch once")
            observed_paths[path_key] = resolution.content
        planned: dict[str, ContentVersion] = {}
        for asset in prepared.inventory.assets:
            if asset.path_key in planned:
                raise ArchiveLibraryError("The prepared plan repeats one path")
            planned[asset.path_key] = asset.content
        if set(observed_paths) != set(planned):
            raise ArchiveLibraryError(
                "Observed items must cover exactly the planned paths")
        for path_key, content in planned.items():
            if observed_paths[path_key] != content:
                raise ArchiveLibraryError(
                    "An observed item does not carry the planned content")
        bound_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            return self._enqueue(
                "apply_observed_plan",
                {"prepared": prepared, "items": supplied, "bound_at": bound_at},
                size_payload={
                    "kind": "apply_observed_plan",
                    "batch_id": prepared.batch_id,
                    "payload_bytes": prepared.payload_bytes,
                    "item_count": len(supplied),
                    "path_keys": sorted(observed_paths),
                    "bound_at": bound_at,
                    "scope_bytes": len(prepared.scope_json.encode("utf-8")),
                    "receipt_bytes": (
                        0 if prepared.scope_receipt_json is None
                        else len(prepared.scope_receipt_json.encode("utf-8"))
                    ),
                    "capacity_bytes": len(prepared.capacity_json.encode("utf-8")),
                },
            )
        except LibraryBusyError as exc:
            raise ArchiveLibraryBusyError("Archive library is busy") from exc
        except ContractError as exc:
            raise ArchiveLibraryError("Archive observation input was rejected") from exc

    def load_archive_plan(self, batch_id: str) -> StoredArchivePlan:
        """Run strict validation in a short inherited read transaction."""
        self._ensure_open()
        try:
            with closing(self._readonly_connection()) as conn:
                return load_archive_plan_in_transaction(conn, batch_id)
        except ArchivePlanStoreError:
            raise
        except ContractError as exc:
            raise ArchiveLibraryError("Archive plan facts were rejected") from exc
        except LibraryBusyError as exc:
            raise ArchiveLibraryBusyError("Archive library is busy") from exc
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise ArchiveLibraryBusyError("Archive library is busy") from exc
            raise ArchiveLibraryError("Archive plan reload failed") from exc
        except sqlite3.Error as exc:
            raise ArchiveLibraryError("Archive plan reload failed") from exc
