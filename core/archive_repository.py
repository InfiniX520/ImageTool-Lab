"""Schema-3 archive repository on the existing single-writer transport.

This class is deliberately opt-in for already migrated temporary copies.  It
keeps the production LibraryRepository v1 guard and _enqueue transport unchanged,
and does not migrate, discover, or open a production database automatically.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from core.archive_inventory import ArchiveInventory, CapacityPlan
from core.archive_scope import ScopeReceipt
from core.archive_plan_store import (
    ArchivePlanSaveResult, ArchivePlanStoreError, PreparedArchivePlan,
    StoredArchivePlan, load_archive_plan_in_transaction, prepare_archive_plan,
    save_prepared_plan_in_transaction,
)
from core.archive_schema import (
    ARCHIVE_SCHEMA_VERSION, ARCHIVE_TABLES, ArchiveMigrationError, _check_existing_objects,
)
from core.library_repository import (
    LibraryRepository, SchemaVersionError, _LIBRARY_FORMAT,
)
from core.task_contracts import ContractError


class ArchiveLibraryError(ArchivePlanStoreError):
    """The archive repository cannot safely accept a command or connection."""


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
                "Archive repository requires an existing migrated schema-3 copy"
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
                raise SchemaVersionError("Archive repository requires exactly schema 3")
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
            )
        except ContractError as exc:
            raise ArchiveLibraryError("Archive plan input was rejected") from exc
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
        except sqlite3.Error as exc:
            raise ArchiveLibraryError("Archive plan reload failed") from exc
