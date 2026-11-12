"""A1 preservation schema and explicit migrations for isolated library copies.

Nothing here is imported by the production repository.  This module deliberately
requires an explicit test-copy acknowledgement and a separate backup destination;
it is not authorization to migrate a real library or release local originals.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from core.archive_scope import (
    ContentBinding, ContentResolution, ObservedFile, rebind_observation,
    resolve_content_version,
)
from core.library_paths import AlreadyRunningError, InstanceLock
from core.library_repository import _DDL, _REQUIRED_TABLES, _validate_database_path
from core.local_observation import FileIdentity, LocalObservation, ObservationStatus
from core.preservation_policy import (
    ArchiveEvidence, ArchiveState, ContentVersion, LocalEvidence, LocalState, RemovalIntent,
)
from core.task_contracts import ContractError, PathRef, canonical_json, normalize_json

# Schema 4 adds the append-only acceptance table that separates "what a path is
# currently bound to" from "which diagnostic observations were written"; schema 3
# copies are refused rather than silently reinterpreted.
ARCHIVE_SCHEMA_VERSION = "4"
ARCHIVE_PROTOCOL_VERSION = 2
# One document budget for plan headers, capacity and inventory JSON at both the
# writer and the audit.  The previous 1 MiB audit limit rejected a legal plan
# whose scope happened to be larger than 1 MiB.
ARCHIVE_JSON_LIMIT = 8 * 1024 * 1024
ARCHIVE_PLAN_DOCUMENT_LIMIT = 8 * 1024 * 1024
ARCHIVE_PLAN_SEGMENT_LIMIT = 1024 * 1024
ARCHIVE_PLAN_SEGMENT_COUNT = 128
ARCHIVE_TABLES = frozenset({
    "archive_batches", "archive_contents", "archive_assets", "archive_transfer_intents",
    "archive_local_checks", "archive_packages", "archive_members", "archive_objects",
    "archive_path_bindings", "archive_path_acceptances", "archive_groups",
    "archive_group_objects", "archive_locations",
    "archive_checks", "archive_releases", "archive_plan_segments",
})
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class ArchiveMigrationError(Exception):
    """The explicit temporary-copy migration cannot safely proceed."""


class ArchiveSchemaError(ArchiveMigrationError):
    """The source, destination or recorded evidence violates this schema."""


class ArchiveTransactionRequiredError(ArchiveSchemaError):
    """A low-level write was attempted without a caller-owned transaction.

    This module never opens, commits or rolls back a transaction.  Writing inside
    an implicit autocommit statement is exactly how a failed multi-step accept used
    to leave content/asset rows behind, so every public writer refuses it before
    reading or writing anything.
    """


class ArchiveBindingConflictError(ArchiveSchemaError):
    """The current path binding is not the one the resolution was computed from.

    The resolution carries the expected old binding as immutable input; when the
    database no longer matches it, the caller must re-observe and re-resolve.  No
    fact is written and no newer revision is adopted automatically.
    """

    def __init__(
        self, path_key: str, expected_content_key: str | None,
        actual_content_key: str | None, reason: str,
    ):
        self.path_key = path_key
        self.expected_content_key = expected_content_key
        self.actual_content_key = actual_content_key
        self.reason = reason
        super().__init__(
            "Path binding for %s changed since it was resolved (%s)" % (path_key, reason)
        )


class ArchiveIdentityConfirmationRequired(ArchiveSchemaError):
    """A path now names a different physical object; a human must decide.

    The resolution is attached so a caller can show the exact path, previous
    revision, observed facts and next action.  This is deliberately not a
    generic database error and it never claims the scope was collected fully.
    """

    def __init__(self, resolution: ContentResolution):
        if not isinstance(resolution, ContentResolution):
            raise TypeError("Expected a ContentResolution")
        self.resolution = resolution
        self.scope_complete = False
        super().__init__(
            "Path %s requires confirmation (%s); the scope is not complete"
            % (resolution.candidate.relative_path, resolution.reason)
        )


def _token(value: object, field: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise ContractError(f"Invalid {field}")
    return value


def _require_write_transaction(conn: sqlite3.Connection, action: str) -> None:
    """Require a unit of work the caller owns, before the first read.

    Nothing here commits or rolls back: the caller's with-block (or its explicit
    BEGIN/COMMIT) stays the only owner of durability.  A connection in true
    autocommit mode (isolation_level is None) has no such unit, so it is refused
    immediately - that is exactly how a failed multi-step accept used to leave
    content and asset rows behind.  When the connection is in a managed isolation
    mode, one BEGIN IMMEDIATE is issued so the expected-binding comparison and every
    write happen under the same write lock.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise ContractError("An explicit sqlite3 connection is required")
    if conn.in_transaction:
        return
    if conn.isolation_level is None:
        raise ArchiveTransactionRequiredError(
            action + " requires a caller-owned transaction"
        )
    conn.execute("BEGIN IMMEDIATE")


def _plain_json(value: object) -> object:
    if isinstance(value, dict):
        return {key: _plain_json(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(child) for child in value]
    if isinstance(value, Enum) and isinstance(value.value, str):
        return value.value
    return value


def _json(value: object) -> str:
    if value is None:
        return "{}"
    result = canonical_json(_plain_json(value))
    if len(result.encode("utf-8")) > ARCHIVE_JSON_LIMIT:
        raise ContractError("Archive evidence detail exceeds its bound")
    return result


def _stored_json(value: object, field: str) -> None:
    if not isinstance(value, str):
        raise ArchiveSchemaError(f"Invalid stored {field}")
    try:
        if len(value.encode("utf-8")) > ARCHIVE_JSON_LIMIT:
            raise ArchiveSchemaError(f"Stored {field} exceeds its bound")
        normalize_json(value, object_only=True)
    except (ContractError, UnicodeError) as exc:
        raise ArchiveSchemaError(f"Invalid stored {field}") from exc


def _local_from_json(value: object) -> LocalEvidence:
    if not isinstance(value, dict):
        raise ContractError("Invalid local evidence JSON")
    try:
        return LocalEvidence(
            content=ContentVersion(**value["content"]), path=PathRef(**value["path"]),
            root_revision=value["root_revision"], run_id=value["run_id"],
            check_id=value["check_id"], checked_at=value["checked_at"],
            state=LocalState(value["state"]), observed_size=value["observed_size"],
            observed_sha256=value["observed_sha256"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("Invalid local evidence JSON") from exc


def _archive_from_json(value: object) -> ArchiveEvidence:
    if not isinstance(value, dict):
        raise ContractError("Invalid archive evidence JSON")
    try:
        return ArchiveEvidence(
            content=ContentVersion(**value["content"]), state=ArchiveState(value["state"]),
            check_id=value["check_id"], checked_at=value["checked_at"],
            location_ref=value["location_ref"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("Invalid archive evidence JSON") from exc


def _path_json(path: PathRef) -> str:
    if not isinstance(path, PathRef):
        raise ContractError("Expected PathRef")
    return canonical_json({
        "kind": path.kind, "root_id": path.root_id,
        "relative_path": path.relative_path,
    })


def _content_key(content: ContentVersion) -> str:
    if not isinstance(content, ContentVersion):
        raise ContractError("Expected ContentVersion")
    return hashlib.sha256(
        canonical_json([content.asset_id, content.revision]).encode("utf-8")
    ).hexdigest()


def _content_values(content: ContentVersion) -> tuple[object, ...]:
    return (content.asset_id, content.revision, content.size_bytes, content.sha256)


def _require_content(
    conn: sqlite3.Connection, content: ContentVersion, *,
    error: type[Exception] = ContractError, message: str = "Content facts are not indexed",
) -> str:
    """Require the exact content row, including a NULL digest, not just its key."""
    if not isinstance(content, ContentVersion):
        raise error("Expected ContentVersion")
    content_key = _content_key(content)
    row = conn.execute(
        "SELECT asset_id,revision,size_bytes,sha256 FROM archive_contents WHERE content_key=?",
        (content_key,),
    ).fetchone()
    if row is None or tuple(row) != _content_values(content):
        raise error(message)
    return content_key


def _require_asset_link(
    conn: sqlite3.Connection, content: ContentVersion, path: PathRef, *,
    error: type[Exception] = ContractError,
    message: str = "Asset path is not indexed for this exact content",
) -> None:
    """Require the asset row for this path key and these exact content facts.

    The comparison is by identity key, not by display spelling: one Windows
    directory entry reached through another ASCII case spelling is the same
    asset, and the row keeps the spelling it was first indexed with.
    """
    if not isinstance(content, ContentVersion) or not isinstance(path, PathRef):
        raise error("Expected content and path facts")
    row = conn.execute(
        """SELECT content_key,asset_id,revision,path_key,size_bytes,sha256
           FROM archive_assets WHERE asset_id=? AND revision=? AND path_key=?""",
        (content.asset_id, content.revision, path.identity_key),
    ).fetchone()
    expected = (
        _content_key(content), content.asset_id, content.revision, path.identity_key,
        content.size_bytes, content.sha256,
    )
    if row is None or tuple(row) != expected:
        raise error(message)


ARCHIVE_DDL = (
    """CREATE TABLE archive_batches (
        batch_id TEXT PRIMARY KEY,
        protocol_version INTEGER NOT NULL CHECK (protocol_version = 2),
        scope_json TEXT NOT NULL,
        capacity_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('planned','paused','blocked','completed')),
        pause_reason TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE archive_contents (
        content_key TEXT PRIMARY KEY,
        asset_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 0),
        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
        sha256 TEXT CHECK (sha256 IS NULL OR (
            length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
        )),
        UNIQUE (asset_id, revision)
    )""",
    """CREATE TABLE archive_assets (
        content_key TEXT NOT NULL REFERENCES archive_contents(content_key),
        asset_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 0),
        path_key TEXT NOT NULL,
        path_json TEXT NOT NULL,
        artifact_id TEXT REFERENCES artifacts(artifact_id),
        source_kind TEXT NOT NULL CHECK (source_kind IN (
            'download_receipt','observed_existing','user_selected'
        )),
        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
        sha256 TEXT CHECK (sha256 IS NULL OR (
            length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
        )),
        PRIMARY KEY (asset_id, revision, path_key),
        FOREIGN KEY (asset_id, revision) REFERENCES archive_contents(asset_id, revision),
        UNIQUE (content_key, path_key),
        CHECK ((
            source_kind = 'download_receipt' AND artifact_id IS NOT NULL
        ) OR (
            source_kind != 'download_receipt' AND artifact_id IS NULL
        ))
    )""",
    """CREATE TABLE archive_path_acceptances (
        acceptance_id TEXT PRIMARY KEY,
        path_key TEXT NOT NULL,
        content_key TEXT NOT NULL REFERENCES archive_contents(content_key),
        asset_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 0),
        expected_content_key TEXT REFERENCES archive_contents(content_key),
        root_revision INTEGER NOT NULL CHECK (root_revision >= 0),
        check_id TEXT REFERENCES archive_local_checks(check_id),
        file_device TEXT CHECK (file_device IS NULL OR (
            length(file_device) BETWEEN 1 AND 32 AND file_device NOT GLOB '*[^0-9]*'
        )),
        file_index TEXT CHECK (file_index IS NULL OR (
            length(file_index) BETWEEN 1 AND 32 AND file_index NOT GLOB '*[^0-9]*'
        )),
        accepted_at TEXT NOT NULL,
        reason TEXT NOT NULL,
        FOREIGN KEY (asset_id, revision, path_key) REFERENCES archive_assets(
            asset_id, revision, path_key
        ),
        CHECK ((file_device IS NULL AND file_index IS NULL) OR
               (file_device IS NOT NULL AND file_index IS NOT NULL)),
        CHECK (check_id IS NOT NULL OR file_device IS NULL)
    )""",
    """CREATE TABLE archive_path_bindings (
        path_key TEXT PRIMARY KEY,
        asset_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 0),
        bound_at TEXT NOT NULL,
        acceptance_id TEXT REFERENCES archive_path_acceptances(acceptance_id),
        FOREIGN KEY (asset_id, revision, path_key) REFERENCES archive_assets(
            asset_id, revision, path_key
        )
    )""",
    """CREATE TABLE archive_plan_segments (
        batch_id TEXT NOT NULL REFERENCES archive_batches(batch_id) ON DELETE CASCADE,
        segment_index INTEGER NOT NULL CHECK (segment_index >= 0),
        payload TEXT NOT NULL,
        byte_length INTEGER NOT NULL CHECK (byte_length >= 0),
        PRIMARY KEY (batch_id, segment_index)
    )""",
    """CREATE TABLE archive_transfer_intents (
        intent_id TEXT PRIMARY KEY,
        content_key TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        path_key TEXT NOT NULL,
        root_revision INTEGER NOT NULL CHECK (root_revision >= 0),
        intent TEXT NOT NULL CHECK (intent IN ('none','offloaded','excluded')),
        source TEXT NOT NULL CHECK (source IN (
            'user_reported','program_observed','excluded_scope'
        )),
        observed_at TEXT NOT NULL,
        details_json TEXT NOT NULL,
        FOREIGN KEY (asset_id, revision, path_key) REFERENCES archive_assets(
            asset_id, revision, path_key
        )
    )""",
    """CREATE TABLE archive_local_checks (
        check_id TEXT PRIMARY KEY,
        content_key TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        path_key TEXT NOT NULL,
        root_revision INTEGER NOT NULL CHECK (root_revision >= 0),
        run_id TEXT NOT NULL,
        checked_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN (
            'unknown','hash_match','quick_match','missing','changed','unreadable',
            'root_unavailable','changed_during_check'
        )),
        observed_size INTEGER CHECK (observed_size IS NULL OR observed_size >= 0),
        observed_sha256 TEXT CHECK (observed_sha256 IS NULL OR (
            length(observed_sha256) = 64 AND observed_sha256 NOT GLOB '*[^0-9a-f]*'
        )),
        evidence_json TEXT NOT NULL,
        observation_status TEXT,
        observation_reason TEXT,
        digest_complete INTEGER CHECK (digest_complete IS NULL OR digest_complete IN (0, 1)),
        bytes_read INTEGER CHECK (bytes_read IS NULL OR bytes_read >= 0),
        physical_size INTEGER CHECK (physical_size IS NULL OR physical_size >= 0),
        physical_mtime_ns INTEGER,
        file_device TEXT CHECK (file_device IS NULL OR (
            length(file_device) BETWEEN 1 AND 32 AND file_device NOT GLOB '*[^0-9]*'
        )),
        file_index TEXT CHECK (file_index IS NULL OR (
            length(file_index) BETWEEN 1 AND 32 AND file_index NOT GLOB '*[^0-9]*'
        )),
        FOREIGN KEY (asset_id, revision, path_key) REFERENCES archive_assets(
            asset_id, revision, path_key
        ),
        UNIQUE (check_id, content_key),
        CHECK ((
            observation_status IS NULL AND observation_reason IS NULL AND
            digest_complete IS NULL AND bytes_read IS NULL
        ) OR (
            observation_status IS NOT NULL AND observation_reason IS NOT NULL AND
            digest_complete IS NOT NULL AND bytes_read IS NOT NULL
        )),
        CHECK ((file_device IS NULL AND file_index IS NULL) OR
               (file_device IS NOT NULL AND file_index IS NOT NULL)),
        CHECK (digest_complete IS NULL OR digest_complete = 0 OR (
            file_device IS NOT NULL AND file_index IS NOT NULL AND
            physical_size IS NOT NULL AND physical_mtime_ns IS NOT NULL
        ))
    )""",
    """CREATE TABLE archive_packages (
        package_id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL REFERENCES archive_batches(batch_id),
        package_revision INTEGER NOT NULL CHECK (package_revision >= 1),
        format TEXT NOT NULL,
        tool_version TEXT,
        key_ref TEXT,
        size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
        sha256 TEXT CHECK (sha256 IS NULL OR (
            length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
        )),
        state TEXT NOT NULL CHECK (state IN ('planned','sealed')),
        CHECK ((state = 'planned') OR (
            size_bytes IS NOT NULL AND sha256 IS NOT NULL AND
            format = '7z' AND tool_version IS NOT NULL AND key_ref IS NOT NULL
        )),
        UNIQUE (package_id, package_revision)
    )""",
    """CREATE TABLE archive_members (
        package_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        path_key TEXT NOT NULL,
        content_key TEXT NOT NULL,
        member_path TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
        sha256 TEXT NOT NULL CHECK (
            length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        PRIMARY KEY (package_id, member_path),
        UNIQUE (package_id, asset_id, revision, path_key),
        FOREIGN KEY (package_id) REFERENCES archive_packages(package_id),
        FOREIGN KEY (asset_id, revision, path_key) REFERENCES archive_assets(
            asset_id, revision, path_key
        )
    )""",
    """CREATE TABLE archive_groups (
        group_id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL REFERENCES archive_batches(batch_id),
        protocol_version INTEGER NOT NULL CHECK (protocol_version = 2),
        parameters_json TEXT NOT NULL,
        target_capability TEXT NOT NULL CHECK (target_capability IN (
            'partial_damage','whole_largest_pack'
        )),
        measured_capability TEXT CHECK (measured_capability IS NULL OR
            measured_capability IN ('partial_damage','whole_largest_pack')),
        UNIQUE (group_id, batch_id)
    )""",
    """CREATE TABLE archive_objects (
        object_id TEXT NOT NULL,
        object_revision INTEGER NOT NULL CHECK (object_revision >= 1),
        object_kind TEXT NOT NULL CHECK (object_kind IN ('package','parity','control')),
        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
        sha256 TEXT NOT NULL CHECK (
            length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        parameters_json TEXT NOT NULL,
        PRIMARY KEY (object_id, object_revision)
    )""",
    """CREATE TABLE archive_group_objects (
        group_id TEXT NOT NULL,
        object_id TEXT NOT NULL,
        object_revision INTEGER NOT NULL CHECK (object_revision >= 1),
        required INTEGER NOT NULL CHECK (required IN (0,1)),
        PRIMARY KEY (group_id, object_id),
        FOREIGN KEY (group_id) REFERENCES archive_groups(group_id),
        FOREIGN KEY (object_id, object_revision) REFERENCES archive_objects(
            object_id, object_revision
        )
    )""",
    """CREATE TABLE archive_locations (
        location_id TEXT PRIMARY KEY,
        object_id TEXT NOT NULL,
        object_revision INTEGER NOT NULL CHECK (object_revision >= 1),
        locator_revision INTEGER NOT NULL CHECK (locator_revision >= 1),
        provider_alias TEXT NOT NULL,
        locator_json TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        FOREIGN KEY (object_id, object_revision) REFERENCES archive_objects(
            object_id, object_revision
        ),
        UNIQUE (object_id, object_revision, locator_revision, location_id)
    )""",
    """CREATE TABLE archive_checks (
        check_id TEXT PRIMARY KEY,
        content_key TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        location_id TEXT NOT NULL REFERENCES archive_locations(location_id),
        checked_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN (
            'user_reported_uploaded','user_returned_verified',
            'adapter_readback_verified','access_failed'
        )),
        observed_size INTEGER CHECK (observed_size IS NULL OR observed_size >= 0),
        observed_sha256 TEXT CHECK (observed_sha256 IS NULL OR (
            length(observed_sha256) = 64 AND observed_sha256 NOT GLOB '*[^0-9a-f]*'
        )),
        evidence_json TEXT NOT NULL,
        FOREIGN KEY (asset_id, revision) REFERENCES archive_contents(
            asset_id, revision
        ),
        UNIQUE (check_id, content_key),
        CHECK (state NOT IN ('user_returned_verified','adapter_readback_verified') OR (
            observed_size IS NOT NULL AND observed_sha256 IS NOT NULL
        ))
    )""",
    """CREATE TABLE archive_releases (
        event_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        content_key TEXT NOT NULL,
        local_check_id TEXT NOT NULL,
        archive_check_id TEXT NOT NULL,
        event TEXT NOT NULL CHECK (event IN ('prepared','released','failed','cancelled')),
        observed_at TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        FOREIGN KEY (local_check_id, content_key) REFERENCES archive_local_checks(
            check_id, content_key
        ),
        FOREIGN KEY (archive_check_id, content_key) REFERENCES archive_checks(
            check_id, content_key
        ),
        UNIQUE (operation_id, sequence),
        UNIQUE (operation_id, event)
    )""",
    "CREATE INDEX idx_archive_assets_content ON archive_assets(content_key, path_key)",
    "CREATE INDEX idx_archive_bindings_asset ON archive_path_bindings(asset_id, revision)",
    "CREATE INDEX idx_archive_acceptances_path ON archive_path_acceptances(path_key, accepted_at, acceptance_id)",
    "CREATE INDEX idx_archive_plan_segments_batch ON archive_plan_segments(batch_id, segment_index)",
    "CREATE INDEX idx_archive_intents_asset ON archive_transfer_intents(asset_id, revision, observed_at)",
    "CREATE INDEX idx_archive_local_run ON archive_local_checks(run_id, path_key, checked_at)",
    "CREATE INDEX idx_archive_packages_batch ON archive_packages(batch_id, package_id)",
    "CREATE INDEX idx_archive_members_asset ON archive_members(asset_id, revision, path_key)",
    "CREATE INDEX idx_archive_locations_object ON archive_locations(object_id, locator_revision)",
    "CREATE INDEX idx_archive_checks_content ON archive_checks(content_key, checked_at, check_id)",
    "CREATE INDEX idx_archive_releases_checks ON archive_releases(local_check_id, archive_check_id, observed_at)",
    "CREATE INDEX idx_archive_releases_operation ON archive_releases(operation_id, sequence)",
    """CREATE TRIGGER archive_asset_path_owner
    BEFORE INSERT ON archive_assets
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1 FROM archive_assets
        WHERE path_key = NEW.path_key AND asset_id != NEW.asset_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'archive path belongs to another asset');
    END""",
)

_INDEXED_DDL = tuple(
    (re.match(r"\s*CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER)\s+(\w+)", sql, re.I).group(1), sql)
    for sql in ARCHIVE_DDL
)


def _normalized_sql_objects(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): " ".join(str(row[1]).split())
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table','index','trigger') "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }


def _check_existing_objects(conn: sqlite3.Connection) -> None:
    actual = _normalized_sql_objects(conn)
    for name, statement in _INDEXED_DDL:
        if actual.get(name) != " ".join(statement.split()):
            raise ArchiveSchemaError(f"Missing or incompatible archive object: {name}")


def read_plan_segments(
    conn: sqlite3.Connection, batch_id: str, segment_count: object,
    inventory_bytes: object, inventory_sha256: object,
) -> str:
    """Read and verify one plan document without materializing an oversized row.

    The byte length is computed by SQLite first; the payload row is fetched only
    after the per-segment and cumulative budgets pass.  A 9 MiB or larger row is
    therefore rejected before Python receives it.
    """
    if (type(segment_count) is not int or not 1 <= segment_count <= ARCHIVE_PLAN_SEGMENT_COUNT
            or type(inventory_bytes) is not int
            or not 0 <= inventory_bytes <= ARCHIVE_PLAN_DOCUMENT_LIMIT
            or not isinstance(inventory_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", inventory_sha256) is None):
        raise ArchiveSchemaError("Stored archive plan header bound is invalid")
    lengths = []
    total_bytes = 0
    rows = conn.execute(
        """SELECT segment_index,byte_length,length(CAST(payload AS BLOB))
           FROM archive_plan_segments WHERE batch_id=? ORDER BY segment_index""",
        (batch_id,),
    )
    for index, row in enumerate(rows):
        if index >= segment_count:
            raise ArchiveSchemaError("Stored plan has extra segments")
        if tuple(row[:1]) != (index,):
            raise ArchiveSchemaError("Stored plan segments are not contiguous")
        declared, actual = row[1], row[2]
        if type(declared) is not int or type(actual) is not int:
            raise ArchiveSchemaError("Stored plan segment length is invalid")
        if declared != actual or actual > ARCHIVE_PLAN_SEGMENT_LIMIT:
            raise ArchiveSchemaError("Stored plan segment byte length differs")
        total_bytes += actual
        if total_bytes > ARCHIVE_PLAN_DOCUMENT_LIMIT:
            raise ArchiveSchemaError("Stored plan exceeds the document byte budget")
        lengths.append(actual)
    if len(lengths) != segment_count:
        raise ArchiveSchemaError("Stored plan segment count differs from its header")
    if total_bytes != inventory_bytes:
        raise ArchiveSchemaError("Stored plan segment bytes differ from its header")
    pieces = []
    for index in range(segment_count):
        row = conn.execute(
            """SELECT payload FROM archive_plan_segments
               WHERE batch_id=? AND segment_index=?""",
            (batch_id, index),
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            raise ArchiveSchemaError("Stored plan segment payload is invalid")
        payload = row[0]
        if len(payload.encode("utf-8")) != lengths[index]:
            raise ArchiveSchemaError("Stored plan segment byte length changed while reading")
        pieces.append(payload)
    joined = "".join(pieces)
    if len(joined.encode("utf-8")) != inventory_bytes:
        raise ArchiveSchemaError("Stored plan segment bytes differ from its header")
    if hashlib.sha256(joined.encode("utf-8")).hexdigest() != inventory_sha256:
        raise ArchiveSchemaError("Stored plan document digest differs from its header")
    return joined


def _check_stored_plan_segments(
    conn: sqlite3.Connection, batch_id: str, scope: dict[str, object],
) -> None:
    """Recompute one stored plan document from its segments and header.

    The maintenance audit must not accept a plan whose segment_count, byte
    count or inventory digest no longer describes the stored segments.  The
    plan loader already checks this; keeping the audit independent would let
    the two entry points disagree about the same database.
    """
    version = scope.get("plan_schema_version")
    if type(version) is not int or version != 3:
        raise ArchiveSchemaError("Unsupported stored archive plan version")
    if scope.get("batch_id") != batch_id:
        raise ArchiveSchemaError("Stored archive plan batch identity differs")
    read_plan_segments(
        conn, batch_id, scope.get("segment_count"),
        scope.get("inventory_bytes"), scope.get("inventory_sha256"),
    )


def validate_archive_facts(conn: sqlite3.Connection) -> None:
    """Fail closed on malformed stored facts, not only on syntactically valid DDL."""
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    query_only = conn.execute("PRAGMA query_only").fetchone()[0]
    if foreign_keys != 1 and query_only != 1:
        raise ArchiveSchemaError("Archive validation requires foreign keys enabled for writes")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise ArchiveSchemaError("Archive foreign key violation")
    for table, field in (
        ("archive_batches", "scope_json"), ("archive_batches", "capacity_json"),
        ("archive_assets", "path_json"), ("archive_transfer_intents", "details_json"),
        ("archive_local_checks", "evidence_json"), ("archive_groups", "parameters_json"),
        ("archive_objects", "parameters_json"), ("archive_locations", "locator_json"),
        ("archive_checks", "evidence_json"), ("archive_releases", "evidence_json"),
    ):
        for (value,) in conn.execute(f"SELECT {field} FROM {table}"):
            _stored_json(value, field)
    for (batch_id, scope_json, state) in conn.execute(
        "SELECT batch_id,scope_json,state FROM archive_batches"
    ):
        try:
            scope = json.loads(normalize_json(scope_json, object_only=True))
        except (ContractError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise ArchiveSchemaError("Invalid archive plan scope header") from exc
        if (
            state == "planned"
            and isinstance(scope, dict)
            and scope.get("format") == "jpgpng-archive-plan"
            and not isinstance(scope.get("scope_receipt"), dict)
        ):
            raise ArchiveSchemaError("Stored archive plan requires a scope receipt")
        if isinstance(scope, dict) and scope.get("format") == "jpgpng-archive-plan":
            _check_stored_plan_segments(conn, batch_id, scope)
            # The maintenance audit and the plan loader must agree on the
            # full protocol, not just on segment bytes.  Import lazily: the
            # plan store imports this module at import time.
            from core.archive_plan_store import (
                _capacity, _check_assets, _decode_scope, _load_in_transaction,
                _strict_object,
            )
            try:
                if state == "planned":
                    _load_in_transaction(conn, batch_id)
                else:
                    inventory, _scope, _receipt, sources = _decode_scope(
                        conn, scope_json, batch_id)
                    _capacity(
                        _strict_object(
                            conn.execute(
                                "SELECT capacity_json FROM archive_batches WHERE batch_id=?",
                                (batch_id,),
                            ).fetchone()[0],
                            "capacity_json",
                        ),
                        inventory,
                    )
                    _check_assets(conn, inventory, sources)
            except Exception as exc:
                raise ArchiveSchemaError(
                    "Stored archive plan failed the shared protocol check"
                ) from exc
    for batch_id, total_bytes, segment_count in conn.execute(
        "SELECT batch_id,COALESCE(SUM(byte_length),0),COUNT(*) "
        "FROM archive_plan_segments GROUP BY batch_id"
    ):
        if total_bytes > 8 * 1024 * 1024 or segment_count > 128:
            raise ArchiveSchemaError("Archive plan segment budget exceeded")
    content_facts: dict[tuple[str, int], tuple[str, ContentVersion]] = {}
    for row in conn.execute(
        "SELECT content_key,asset_id,revision,size_bytes,sha256 FROM archive_contents"
    ):
        try:
            content_key, asset_id, revision, size, digest = row
            content = ContentVersion(asset_id, revision, size, digest)
        except (ContractError, TypeError, ValueError) as exc:
            raise ArchiveSchemaError("Invalid archive content fact") from exc
        if _content_key(content) != content_key:
            raise ArchiveSchemaError("Archive content identity does not match its facts")
        content_facts[(asset_id, revision)] = (content_key, content)

    assets: dict[tuple[str, int, str], tuple[ContentVersion, PathRef, str, str | None]] = {}
    for row in conn.execute(
        """SELECT content_key,asset_id,revision,path_key,path_json,artifact_id,
                  source_kind,size_bytes,sha256 FROM archive_assets"""
    ):
        content_key, asset_id, revision, path_key, path_json, artifact_id, source_kind, size, digest = row
        try:
            path = PathRef(**json.loads(path_json))
            content = ContentVersion(asset_id, revision, size, digest)
        except (ContractError, TypeError, ValueError) as exc:
            raise ArchiveSchemaError("Invalid archive asset fact") from exc
        if (
            _content_key(content) != content_key
            or content_facts.get((asset_id, revision)) != (content_key, content)
            or _path_json(path) != path_json
            or path.identity_key != path_key
        ):
            raise ArchiveSchemaError("Archive asset identity does not match its facts")
        if artifact_id is None:
            if source_kind == "download_receipt":
                raise ArchiveSchemaError("Download receipt asset has no artifact")
        else:
            if source_kind != "download_receipt":
                raise ArchiveSchemaError("Non-receipt asset references an artifact")
            receipt = conn.execute(
                "SELECT output_ref_json,size,sha256,verification FROM artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if receipt is None:
                raise ArchiveSchemaError("Download receipt artifact is missing")
            try:
                receipt_path = PathRef(**json.loads(
                    normalize_json(receipt[0], object_only=True)))
            except (KeyError, TypeError, ValueError) as exc:
                raise ArchiveSchemaError("Download receipt path is invalid") from exc
            if (
                receipt_path.identity_key != path_key
                or receipt[1] != content.size_bytes
                or receipt[2] != content.sha256
                or receipt[3] != "structure_checked"
            ):
                raise ArchiveSchemaError("Download receipt does not prove this exact content")
        assets[(asset_id, revision, path_key)] = (content, path, source_kind, artifact_id)

    content_keys = {key for key, _ in content_facts.values()}
    acceptances: dict[str, tuple[str, str, int]] = {}
    for row in conn.execute(
        """SELECT acceptance_id,path_key,content_key,asset_id,revision,
                  expected_content_key,root_revision,check_id,file_device,file_index,
                  accepted_at,reason
           FROM archive_path_acceptances"""
    ):
        (acceptance_id, path_key, content_key, asset_id, revision,
         expected_content_key, root_revision, check_id, device, index,
         accepted_at, reason) = row
        asset = assets.get((asset_id, revision, path_key))
        if asset is None:
            raise ArchiveSchemaError("Path acceptance has no exact asset row")
        content = asset[0]
        if content_key != _content_key(content):
            raise ArchiveSchemaError("Path acceptance content differs from its asset")
        if expected_content_key is not None and expected_content_key not in content_keys:
            raise ArchiveSchemaError("Path acceptance expects an unknown content row")
        if type(root_revision) is not int or root_revision < 0:
            raise ArchiveSchemaError("Path acceptance root revision is invalid")
        if _TOKEN.fullmatch(reason) is None:
            raise ArchiveSchemaError("Path acceptance reason is invalid")
        if not isinstance(accepted_at, str) or not accepted_at or len(accepted_at) > 64:
            raise ArchiveSchemaError("Path acceptance time is invalid")
        if (device is None) != (index is None):
            raise ArchiveSchemaError("Path acceptance physical identity is incomplete")
        if check_id is None:
            if device is not None:
                raise ArchiveSchemaError(
                    "Path acceptance claims an unobserved physical object")
        else:
            check = conn.execute(
                """SELECT path_key,root_revision,observed_size,observed_sha256,
                          file_device,file_index
                   FROM archive_local_checks WHERE check_id=?""",
                (check_id,),
            ).fetchone()
            if check is None:
                raise ArchiveSchemaError("Path acceptance references a missing local check")
            if (
                check[0] != path_key
                or check[1] != root_revision
                or check[2] != content.size_bytes
                or check[3] != content.sha256
                or check[4] != device
                or check[5] != index
            ):
                raise ArchiveSchemaError("Path acceptance differs from its local check")
        acceptances[acceptance_id] = (path_key, asset_id, revision)

    for path_key, asset_id, revision, bound_at, acceptance_id in conn.execute(
        "SELECT path_key,asset_id,revision,bound_at,acceptance_id FROM archive_path_bindings"
    ):
        if (asset_id, revision, path_key) not in assets:
            raise ArchiveSchemaError("Path binding has no exact asset row")
        if not isinstance(bound_at, str) or not bound_at or len(bound_at) > 64:
            raise ArchiveSchemaError("Path binding time is invalid")
        if acceptance_id is not None:
            accepted = acceptances.get(acceptance_id)
            if accepted is None:
                raise ArchiveSchemaError("Path binding references a missing acceptance")
            if accepted != (path_key, asset_id, revision):
                raise ArchiveSchemaError("Path binding does not match its acceptance")

    if conn.execute(
        """SELECT COUNT(*) FROM (
               SELECT path_key FROM archive_assets
               GROUP BY path_key HAVING COUNT(DISTINCT asset_id) > 1
           )"""
    ).fetchone()[0]:
        raise ArchiveSchemaError("One path is owned by multiple assets")

    for row in conn.execute(
        """SELECT intent_id,content_key,asset_id,revision,path_key,root_revision,
                  intent,source,observed_at,details_json
           FROM archive_transfer_intents"""
    ):
        intent_id, content_key, asset_id, revision, path_key, root_revision, intent, source, observed_at, _ = row
        asset = assets.get((asset_id, revision, path_key))
        if asset is None or content_key != _content_key(asset[0]):
            raise ArchiveSchemaError("Transfer intent content/path differs from its asset")
        if (
            _TOKEN.fullmatch(intent_id) is None
            or type(root_revision) is not int or root_revision < 0
            or intent not in {"none", "offloaded", "excluded"}
            or source not in {"user_reported", "program_observed", "excluded_scope"}
            or not isinstance(observed_at, str) or not observed_at or len(observed_at) > 64
        ):
            raise ArchiveSchemaError("Transfer intent has invalid immutable facts")

    for row in conn.execute(
        """SELECT check_id,content_key,asset_id,revision,path_key,root_revision,run_id,
                  checked_at,state,observed_size,observed_sha256,evidence_json,
                  observation_status,observation_reason,digest_complete,bytes_read,
                  physical_size,physical_mtime_ns,file_device,file_index
           FROM archive_local_checks"""
    ):
        try:
            evidence = _local_from_json(json.loads(row[11]))
        except (ContractError, TypeError, ValueError) as exc:
            raise ArchiveSchemaError("Invalid stored local evidence") from exc
        asset = assets.get((row[2], row[3], row[4]))
        if asset is None:
            raise ArchiveSchemaError("Local check has no exact asset row")
        content, path, _, _ = asset
        if (
            _content_key(content) != row[1]
            or evidence.content != content
            or evidence.path.identity_key != path.identity_key
        ):
            raise ArchiveSchemaError("Local evidence differs from its indexed facts")
        expected = (
            evidence.check_id, row[1], content.asset_id, content.revision, path.identity_key,
            evidence.root_revision, evidence.run_id, evidence.checked_at,
            evidence.state.value, evidence.observed_size, evidence.observed_sha256,
        )
        if tuple(row[:11]) != expected:
            raise ArchiveSchemaError("Local evidence differs from its indexed facts")
        physical_core = row[12:16]
        if any(value is not None for value in physical_core):
            if any(value is None for value in physical_core):
                raise ArchiveSchemaError("Local physical observation is incomplete")
            try:
                identity = (
                    FileIdentity(int(row[18]), int(row[19]))
                    if row[18] is not None and row[19] is not None else None
                )
            except (TypeError, ValueError) as exc:
                raise ArchiveSchemaError("Invalid stored file identity") from exc
            try:
                observation = LocalObservation(
                    status=ObservationStatus(row[12]), reason=row[13], evidence=evidence,
                    digest_complete=bool(row[14]), bytes_read=row[15], size_bytes=row[16],
                    mtime_ns=row[17], identity=identity,
                )
            except (ContractError, TypeError, ValueError) as exc:
                raise ArchiveSchemaError("Invalid stored local physical observation") from exc
            if observation.evidence != evidence:
                raise ArchiveSchemaError("Local physical observation has different evidence")
            if (
                observation.size_bytes is not None and evidence.observed_size is not None
                and observation.size_bytes != evidence.observed_size
            ):
                raise ArchiveSchemaError("Local physical size differs from its evidence")
            if observation.digest_complete and observation.size_bytes != evidence.observed_size:
                raise ArchiveSchemaError("Complete local digest requires the exact observed size")
        elif any(value is not None for value in row[16:20]):
            raise ArchiveSchemaError("Local physical observation has orphan fields")

    for row in conn.execute(
        """SELECT check_id,content_key,asset_id,revision,location_id,checked_at,state,
                  observed_size,observed_sha256,evidence_json FROM archive_checks"""
    ):
        try:
            evidence = _archive_from_json(json.loads(row[9]))
        except (ContractError, TypeError, ValueError) as exc:
            raise ArchiveSchemaError("Invalid stored archive evidence") from exc
        content_fact = content_facts.get((row[2], row[3]))
        if content_fact is None:
            raise ArchiveSchemaError("Archive check has no exact content row")
        content_key, content = content_fact
        if (
            content_key != row[1]
            or evidence.content != content
            or evidence.check_id != row[0]
            or evidence.location_ref != row[4]
            or evidence.checked_at != row[5]
            or evidence.state.value != row[6]
        ):
            raise ArchiveSchemaError("Archive evidence differs from its indexed facts")
        if row[6] in {"user_returned_verified", "adapter_readback_verified"}:
            if (
                content.sha256 is None
                or row[7] != content.size_bytes
                or row[8] != content.sha256
            ):
                raise ArchiveSchemaError("Verified archive check has unknown or different content")
        elif (row[7] is not None or row[8] is not None) and (
            row[7] != content.size_bytes or row[8] != content.sha256
        ):
            raise ArchiveSchemaError("Archive check content differs from its asset")
        members = conn.execute(
            """SELECT m.size_bytes,m.sha256 FROM archive_members AS m
               JOIN archive_objects AS o ON o.object_id=m.package_id
               JOIN archive_locations AS l ON l.object_id=o.object_id
                   AND l.object_revision=o.object_revision
               WHERE m.content_key=? AND l.location_id=? AND o.object_kind='package'""",
            (content_key, row[4]),
        ).fetchall()
        if not any(
            tuple(member) == (content.size_bytes, content.sha256) for member in members
        ):
            raise ArchiveSchemaError("Archive check location does not contain this content")

    for row in conn.execute(
        """SELECT package_id,asset_id,revision,path_key,content_key,member_path,
                  size_bytes,sha256 FROM archive_members"""
    ):
        asset = assets.get((row[1], row[2], row[3]))
        package = conn.execute(
            "SELECT state FROM archive_packages WHERE package_id=?", (row[0],)
        ).fetchone()
        if asset is None or package is None or package[0] != "sealed":
            raise ArchiveSchemaError("Package member has no sealed exact asset")
        content = asset[0]
        if (
            row[4] != _content_key(content)
            or row[5] is None or not isinstance(row[5], str) or not row[5]
            or row[6] != content.size_bytes
            or row[7] != content.sha256
        ):
            raise ArchiveSchemaError("Package members must match sealed content facts")

    for row in conn.execute(
        """SELECT package_id,batch_id,package_revision,format,tool_version,key_ref,
                  size_bytes,sha256,state FROM archive_packages"""
    ):
        if row[8] == "sealed":
            if (
                row[3] != "7z" or row[4] is None or row[5] is None
                or row[6] is None or row[7] is None
            ):
                raise ArchiveSchemaError("Sealed package lacks required facts")
        elif row[8] != "planned":
            raise ArchiveSchemaError("Invalid package state")

    for row in conn.execute(
        """SELECT object_id,object_revision,object_kind,size_bytes,sha256,parameters_json
           FROM archive_objects"""
    ):
        package = conn.execute(
            """SELECT state,size_bytes,sha256 FROM archive_packages
               WHERE package_id=? AND package_revision=?""",
            (row[0], row[1]),
        ).fetchone()
        if row[2] == "package":
            if (
                package is None or package[0] != "sealed"
                or package[1] != row[3] or package[2] != row[4]
            ):
                raise ArchiveSchemaError("Physical object and package facts differ")
        elif package is not None:
            raise ArchiveSchemaError("Non-package object has package facts")

    for row in conn.execute(
        "SELECT batch_id,protocol_version,state FROM archive_batches"
    ):
        if row[1] != ARCHIVE_PROTOCOL_VERSION or row[2] not in {
            "planned", "paused", "blocked", "completed"
        }:
            raise ArchiveSchemaError("Invalid archive batch fact")

    release_rows = conn.execute(
        """SELECT event_id,operation_id,sequence,content_key,local_check_id,
                  archive_check_id,event,observed_at,evidence_json
           FROM archive_releases ORDER BY operation_id,sequence"""
    ).fetchall()
    operations: dict[str, list[tuple]] = {}
    for row in release_rows:
        local = conn.execute(
            "SELECT content_key,state FROM archive_local_checks WHERE check_id=?",
            (row[4],),
        ).fetchone()
        archive = conn.execute(
            "SELECT content_key,state FROM archive_checks WHERE check_id=?",
            (row[5],),
        ).fetchone()
        if (
            local is None or tuple(local) != (row[3], "hash_match")
            or archive is None or archive[0] != row[3]
            or archive[1] not in {"user_returned_verified", "adapter_readback_verified"}
        ):
            raise ArchiveSchemaError("Release facts require matching verified local/archive checks")
        operations.setdefault(row[1], []).append(tuple(row))
    for rows in operations.values():
        if rows[0][2] != 1 or rows[0][6] != "prepared":
            raise ArchiveSchemaError("Release operation must start with prepared")
        if any(tuple(row[3:6]) != tuple(rows[0][3:6]) for row in rows[1:]):
            raise ArchiveSchemaError("Release operation mixes content or evidence checks")
        for index, row in enumerate(rows):
            if row[2] != index + 1:
                raise ArchiveSchemaError("Release event sequence is not contiguous")
            if index and rows[index - 1][6] in {"released", "failed", "cancelled"}:
                raise ArchiveSchemaError("Release event follows a terminal event")
            if row[6] == "prepared" and index != 0:
                raise ArchiveSchemaError("Release operation prepares more than once")
            if row[6] in {"released", "failed", "cancelled"} and index != len(rows) - 1:
                raise ArchiveSchemaError("Release terminal event is not last")


def ensure_archive_asset(
    conn: sqlite3.Connection, content: ContentVersion, path: PathRef, *,
    source_kind: str, artifact_id: str | None = None,
) -> str:
    """Insert one immutable content/path mapping; never relabel a historical asset."""
    _require_write_transaction(conn, "Indexing an archive asset")
    if not isinstance(content, ContentVersion) or not isinstance(path, PathRef):
        raise ContractError("Expected content and path facts")
    content_key = _content_key(content)
    if source_kind not in {"download_receipt", "observed_existing", "user_selected"}:
        raise ContractError("Invalid asset source")
    if source_kind == "download_receipt":
        if artifact_id is None:
            raise ContractError("Download receipt source requires an artifact reference")
        receipt = conn.execute(
            "SELECT output_ref_json,size,sha256,verification FROM artifacts WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        if receipt is None:
            raise ContractError("Referenced download receipt does not exist")
        output_ref, size, digest, verification = receipt
        try:
            if PathRef(**json.loads(
                    normalize_json(output_ref, object_only=True))).identity_key != path.identity_key:
                raise ContractError("Download receipt path does not match this asset")
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("Download receipt path is invalid") from exc
        if verification != "structure_checked" or size != content.size_bytes or digest != content.sha256:
            raise ContractError("Download receipt does not prove this exact content")
    elif artifact_id is not None:
        raise ContractError("Only download receipt assets may reference artifacts")

    owners = conn.execute(
        "SELECT DISTINCT asset_id FROM archive_assets WHERE path_key=?",
        (path.identity_key,),
    ).fetchall()
    if any(row[0] != content.asset_id for row in owners):
        raise ContractError("Path already belongs to a different asset")

    content_values = _content_values(content)
    existing = conn.execute(
        "SELECT asset_id,revision,size_bytes,sha256 FROM archive_contents WHERE content_key=?",
        (content_key,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != content_values:
            raise ContractError("Content identity already has different facts")
    else:
        conn.execute(
            "INSERT INTO archive_contents (content_key,asset_id,revision,size_bytes,sha256) "
            "VALUES (?,?,?,?,?)",
            (content_key, *content_values),
        )

    values = (
        content_key, content.asset_id, content.revision, path.identity_key, _path_json(path),
        artifact_id, source_kind, content.size_bytes, content.sha256,
    )
    existing_asset = conn.execute(
        """SELECT content_key,asset_id,revision,path_key,path_json,artifact_id,
                  source_kind,size_bytes,sha256 FROM archive_assets
           WHERE asset_id=? AND revision=? AND path_key=?""",
        (content.asset_id, content.revision, path.identity_key),
    ).fetchone()
    if existing_asset is not None:
        stored = tuple(existing_asset)
        if stored != values:
            # Index 4 is path_json: the display spelling.  Two spellings of one
            # Windows directory entry share the identity key, so they are the same
            # asset row; the row keeps the spelling it was first indexed with and
            # the immutable facts must still agree exactly.
            if stored[:4] != values[:4] or stored[5:] != values[5:]:
                raise ContractError("Existing asset has different immutable facts")
        return content_key
    conn.execute(
        """INSERT INTO archive_assets (
            content_key,asset_id,revision,path_key,path_json,artifact_id,source_kind,size_bytes,sha256
        ) VALUES (?,?,?,?,?,?,?,?,?)""", values,
    )
    return content_key


@dataclass(frozen=True, slots=True)
class PathAcceptance:
    """One accepted path/content binding event and the evidence behind it."""

    acceptance_id: str
    path_key: str
    content: ContentVersion
    content_key: str
    expected_content_key: str | None
    root_revision: int
    check_id: str | None
    physical_identity: FileIdentity | None
    accepted_at: str
    reason: str


def _acceptance_key(path_key: str, content_key: str, check_id: str) -> str:
    """Deterministic identity of one accepted evidence set.

    The same path, content generation and local check therefore map to the same
    acceptance row: applying the same observation twice appends nothing, while any
    different evidence produces a different row.
    """
    return hashlib.sha256(
        canonical_json([path_key, content_key, check_id]).encode("utf-8")
    ).hexdigest()


def record_path_acceptance(
    conn: sqlite3.Connection, content: ContentVersion, path: PathRef, *,
    check_id: str, root_revision: int, expected_content_key: str | None,
    accepted_at: str, reason: str,
) -> PathAcceptance:
    """Append one acceptance event for this path and return it.

    The accepted physical object is read from the referenced local check, never
    from the caller, so a caller cannot claim an object it did not observe.  The
    check must name this path, this root generation and exactly these bytes.
    """
    _require_write_transaction(conn, "Recording a path acceptance")
    if not isinstance(content, ContentVersion) or not isinstance(path, PathRef):
        raise ContractError("Expected content and path facts")
    _token(check_id, "acceptance check ID")
    _token(reason, "acceptance reason")
    if type(root_revision) is not int or root_revision < 0:
        raise ContractError("Invalid acceptance root revision")
    if not isinstance(accepted_at, str) or not accepted_at or len(accepted_at) > 64:
        raise ContractError("Invalid acceptance time")
    if expected_content_key is not None and (
        not isinstance(expected_content_key, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_content_key) is None
    ):
        raise ContractError("Invalid expected content key for a path acceptance")
    _require_asset_link(conn, content, path)
    content_key = _content_key(content)
    check = conn.execute(
        """SELECT path_key,root_revision,observed_size,observed_sha256,
                  file_device,file_index
           FROM archive_local_checks WHERE check_id=?""",
        (check_id,),
    ).fetchone()
    if check is None:
        raise ContractError("An acceptance must reference a stored local check")
    if check[0] != path.identity_key or check[1] != root_revision:
        raise ArchiveSchemaError("Accepted check belongs to another path or root generation")
    if (
        content.sha256 is None
        or check[2] != content.size_bytes
        or check[3] != content.sha256
    ):
        raise ArchiveSchemaError("Accepted check does not prove this exact content")
    identity = None
    if check[4] is not None and check[5] is not None:
        try:
            identity = FileIdentity(int(check[4]), int(check[5]))
        except (TypeError, ValueError) as exc:
            raise ArchiveSchemaError("Stored accepted physical identity is invalid") from exc
    acceptance = PathAcceptance(
        _acceptance_key(path.identity_key, content_key, check_id),
        path.identity_key, content, content_key, expected_content_key, root_revision,
        check_id, identity, accepted_at, reason,
    )
    values = (
        acceptance.path_key, acceptance.content_key, content.asset_id, content.revision,
        acceptance.expected_content_key, acceptance.root_revision, acceptance.check_id,
        None if identity is None else str(identity.device),
        None if identity is None else str(identity.file_index),
        acceptance.accepted_at, acceptance.reason,
    )
    existing = conn.execute(
        """SELECT path_key,content_key,asset_id,revision,expected_content_key,
                  root_revision,check_id,file_device,file_index,accepted_at,reason
           FROM archive_path_acceptances WHERE acceptance_id=?""",
        (acceptance.acceptance_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise ArchiveSchemaError("Acceptance identity already has different facts")
        return acceptance
    conn.execute(
        """INSERT INTO archive_path_acceptances (
            acceptance_id,path_key,content_key,asset_id,revision,expected_content_key,
            root_revision,check_id,file_device,file_index,accepted_at,reason
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (acceptance.acceptance_id, *values),
    )
    return acceptance


def bind_archive_path(
    conn: sqlite3.Connection, content: ContentVersion, path: PathRef, *,
    bound_at: str, expected_content_key: str | None = None,
    acceptance_id: str | None = None,
) -> None:
    """Point a path at a content revision only with an explicit expected old key.

    expected_content_key=None means "this path must still be unbound".  The
    comparison always uses the stored binding; a value re-read by the caller is
    never substituted for the resolution input.  acceptance_id records which
    accepted evidence the binding now points at.
    """
    _require_write_transaction(conn, "Binding an archive path")
    if not isinstance(bound_at, str) or not bound_at or len(bound_at) > 64:
        raise ContractError("Invalid path binding time")
    if acceptance_id is not None:
        _token(acceptance_id, "path acceptance ID")
        if conn.execute(
            "SELECT 1 FROM archive_path_acceptances WHERE acceptance_id=?",
            (acceptance_id,),
        ).fetchone() is None:
            raise ContractError("Path binding references an unknown acceptance")
    _require_asset_link(conn, content, path)
    new_key = _content_key(content)
    current = conn.execute(
        "SELECT asset_id,revision,acceptance_id FROM archive_path_bindings WHERE path_key=?",
        (path.identity_key,),
    ).fetchone()
    if current is None:
        if expected_content_key is not None:
            raise ContractError("First path binding cannot expect a previous content")
        conn.execute(
            "INSERT INTO archive_path_bindings (path_key,asset_id,revision,bound_at,acceptance_id) "
            "VALUES (?,?,?,?,?)",
            (path.identity_key, content.asset_id, content.revision, bound_at, acceptance_id),
        )
        return
    current_key = conn.execute(
        "SELECT content_key FROM archive_assets WHERE asset_id=? AND revision=? AND path_key=?",
        (current[0], current[1], path.identity_key),
    ).fetchone()
    if current_key is None:
        raise ContractError("Current path binding has no exact asset row")
    if current_key[0] == new_key:
        if acceptance_id is not None and acceptance_id != current[2]:
            conn.execute(
                "UPDATE archive_path_bindings SET bound_at=?, acceptance_id=? WHERE path_key=?",
                (bound_at, acceptance_id, path.identity_key),
            )
        return
    if expected_content_key is None or expected_content_key != current_key[0]:
        raise ContractError("Path binding changed without the expected old content")
    conn.execute(
        """UPDATE archive_path_bindings
           SET asset_id=?, revision=?, bound_at=?, acceptance_id=? WHERE path_key=?""",
        (content.asset_id, content.revision, bound_at, acceptance_id, path.identity_key),
    )


def load_content_binding(
    conn: sqlite3.Connection, path: PathRef,
) -> ContentBinding | None:
    """Read the current path binding and the evidence it explicitly accepts.

    The accepted physical object comes from the acceptance row the binding points
    at.  Diagnostic observations are never consulted here: recording a changed,
    missing or unreadable check cannot move what this path has accepted.
    """
    if not isinstance(path, PathRef):
        raise ContractError("Expected a path reference")
    row = conn.execute(
        """SELECT c.content_key,c.asset_id,c.revision,c.size_bytes,c.sha256,
                  b.acceptance_id,acc.root_revision,acc.check_id,
                  acc.file_device,acc.file_index
           FROM archive_path_bindings AS b
           JOIN archive_assets AS a
             ON a.asset_id=b.asset_id AND a.revision=b.revision
            AND a.path_key=b.path_key
           JOIN archive_contents AS c
             ON c.asset_id=a.asset_id AND c.revision=a.revision
           LEFT JOIN archive_path_acceptances AS acc
             ON acc.acceptance_id=b.acceptance_id
           WHERE b.path_key=?""",
        (path.identity_key,),
    ).fetchone()
    if row is None:
        return None
    content = ContentVersion(row[1], row[2], row[3], row[4])
    if row[0] != _content_key(content):
        raise ArchiveSchemaError("Path binding content identity differs from its facts")
    acceptance_id, root_revision = row[5], row[6]
    if acceptance_id is not None and root_revision is None:
        raise ArchiveSchemaError("Path binding references a missing acceptance")
    physical = None
    if row[8] is not None and row[9] is not None:
        try:
            physical = FileIdentity(int(row[8]), int(row[9]))
        except (TypeError, ValueError) as exc:
            raise ArchiveSchemaError("Stored accepted physical identity is invalid") from exc
    return ContentBinding(content, physical, acceptance_id, root_revision)



def find_known_content(
    conn: sqlite3.Connection, sha256: str, size_bytes: int,
) -> ContentVersion | None:
    """Find one already indexed content generation for these exact bytes.

    The result is byte evidence, not ownership: it says "these bytes are already
    indexed", not "this path may adopt that asset's generation".  Callers must
    compare asset_id before reusing a revision.
    """
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ContractError("Invalid content digest")
    if type(size_bytes) is not int or size_bytes < 0:
        raise ContractError("Invalid content size")
    row = conn.execute(
        """SELECT asset_id,revision,size_bytes,sha256 FROM archive_contents
           WHERE sha256=? AND size_bytes=? ORDER BY asset_id,revision LIMIT 1""",
        (sha256, size_bytes),
    ).fetchone()
    return None if row is None else ContentVersion(row[0], row[1], row[2], row[3])


def content_digest_is_known(
    conn: sqlite3.Connection, sha256: str, size_bytes: int,
) -> bool:
    """True when this exact size and digest already has a content row."""
    return find_known_content(conn, sha256, size_bytes) is not None


def resolve_observed_content(
    conn: sqlite3.Connection, observed: ObservedFile, *,
    replacement_confirmed: bool = False,
) -> ContentResolution:
    """Resolve one real observation against the current indexed binding.

    Reads only the current binding and whether the observed digest is already
    known.  It never writes; the single writer transaction calls
    ``apply_content_resolution`` and rechecks the expected binding there.
    """
    if not isinstance(observed, ObservedFile):
        raise ContractError("Expected an observed file")
    path = observed.candidate.path
    existing = load_content_binding(conn, path)
    known_content = None
    content = observed.content
    if observed.digest_complete and content is not None and content.sha256 is not None:
        known_content = find_known_content(conn, content.sha256, content.size_bytes)
    return resolve_content_version(
        observed.candidate, observed, existing=existing,
        digest_known=known_content is not None, known_content=known_content,
        replacement_confirmed=replacement_confirmed,
    )


def _asset_row_is_exact(
    conn: sqlite3.Connection, content: ContentVersion, path: PathRef,
) -> bool:
    """True when the indexed asset row already carries these exact facts.

    The stored display spelling is not compared: it belongs to the row, and an
    ASCII case alias of the same directory entry must not rewrite it.
    """
    row = conn.execute(
        """SELECT content_key,asset_id,revision,path_key,size_bytes,sha256
           FROM archive_assets WHERE asset_id=? AND revision=? AND path_key=?""",
        (content.asset_id, content.revision, path.identity_key),
    ).fetchone()
    expected = (
        _content_key(content), content.asset_id, content.revision, path.identity_key,
        content.size_bytes, content.sha256,
    )
    return row is not None and tuple(row) == expected


def _bound_content_key(
    conn: sqlite3.Connection, path: PathRef,
) -> tuple[str, int, str, int, str | None] | None:
    """Return (asset_id, revision, content_key, size, sha256) of this path, or None."""
    row = conn.execute(
        "SELECT asset_id,revision FROM archive_path_bindings WHERE path_key=?",
        (path.identity_key,),
    ).fetchone()
    if row is None:
        return None
    key = conn.execute(
        """SELECT a.content_key,c.size_bytes,c.sha256
           FROM archive_assets AS a
           JOIN archive_contents AS c ON c.content_key=a.content_key
           WHERE a.asset_id=? AND a.revision=? AND a.path_key=?""",
        (row[0], row[1], path.identity_key),
    ).fetchone()
    if key is None:
        raise ArchiveSchemaError("Current path binding has no exact asset row")
    return row[0], row[1], key[0], key[1], key[2]


def apply_content_resolution(
    conn: sqlite3.Connection, resolution: ContentResolution, *,
    observed: ObservedFile, bound_at: str,
    source_kind: str = "observed_existing",
) -> PathAcceptance:
    """Write one resolved generation inside the caller's transaction.

    The expected old binding is the immutable one the resolution was computed
    from - including "this path must not exist yet".  It is compared with the
    stored binding before any row is written, so a resolution produced before the
    rest of the world moved is refused instead of silently applied.  The only
    tolerated mismatch is the exact one this call would write: the proposed
    content is already the accepted binding, in which case nothing is overwritten
    and the same acceptance is returned.

    Every write happens in the caller's transaction, and the path binding always
    points at the acceptance of the observation that justified it.
    """
    _require_write_transaction(conn, "Applying a content resolution")
    if not isinstance(resolution, ContentResolution):
        raise ContractError("Expected a content resolution")
    if not isinstance(bound_at, str) or not bound_at or len(bound_at) > 64:
        raise ContractError("Invalid path binding time")
    if not isinstance(observed, ObservedFile):
        raise ContractError("Expected an observed file")
    if observed.candidate != resolution.candidate:
        raise ContractError("Observation candidate differs from the resolution")
    if resolution.requires_attention or resolution.content is None:
        raise ArchiveIdentityConfirmationRequired(resolution)
    path = resolution.candidate.path
    content = resolution.content
    expected = resolution.previous_content
    # The resolution states which expectation it carries.  "No binding exists" is a
    # fact of its own: previous_content is then only the unknown placeholder the
    # observation was probed with, never an expected old binding.
    absent = resolution.expected_binding_absent
    previous_key = None if expected is None else _content_key(expected)
    expected_key = None if absent or expected is None else previous_key
    new_key = _content_key(content)
    if expected is not None and content.asset_id != expected.asset_id:
        # A different logical asset cannot take over a path through a resolver
        # output alone; that is a replacement decision, not a revision advance.
        raise ArchiveIdentityConfirmationRequired(resolution)
    if (not absent and expected is not None and expected_key != new_key
            and content.revision <= expected.revision):
        raise ArchiveSchemaError("Resolved content does not advance its revision")

    if observed.observation is None or not observed.digest_complete:
        raise ContractError("An accepted binding needs one complete observation")
    check_id = observed.observation.evidence.check_id
    root_revision = observed.observation.evidence.root_revision

    # Compare the resolution's expected binding with the stored one first.
    current = _bound_content_key(conn, path)
    if current is None:
        if expected_key is not None:
            raise ArchiveBindingConflictError(
                path.identity_key, expected_key, None, "expected_binding_missing")
    elif current[2] != expected_key:
        if current[2] != new_key:
            raise ArchiveBindingConflictError(
                path.identity_key, expected_key, current[2], "expected_binding_changed")
        if (current[3], current[4]) != (content.size_bytes, content.sha256):
            raise ArchiveBindingConflictError(
                path.identity_key, expected_key, current[2], "content_facts_differ")
        # Already exactly what this call would write: only the accepted evidence
        # may be appended, never a second revision or an overwrite.
        acceptance = _accepted_binding_acceptance(conn, path, current[2], check_id)
        if acceptance is not None:
            return acceptance
    elif not absent and expected is not None and current[1] != expected.revision:
        raise ArchiveBindingConflictError(
            path.identity_key, expected_key, current[2], "expected_revision_changed")

    rebound = rebind_observation(observed, resolution)
    if rebound is None:
        raise ContractError("The observation cannot justify an accepted binding")
    # The stored diagnostic references the generation it was rebound to, so both
    # that generation and the resolved one must be indexed before the observation
    # row.  The raw probe is a read contract (it may carry the new size), never a
    # stored fact.
    for referenced in (expected, rebound.evidence.content):
        if referenced is None or _content_key(referenced) == new_key:
            continue
        referenced_key = _content_key(referenced)
        if conn.execute(
            "SELECT 1 FROM archive_contents WHERE content_key=?", (referenced_key,),
        ).fetchone() is None:
            ensure_archive_asset(conn, referenced, path, source_kind=source_kind)
        else:
            _require_content(conn, referenced)
    if not _asset_row_is_exact(conn, content, path):
        ensure_archive_asset(conn, content, path, source_kind=source_kind)
    record_local_observation(conn, rebound.evidence, result=rebound)
    acceptance = record_path_acceptance(
        conn, content, path, check_id=check_id, root_revision=root_revision,
        expected_content_key=expected_key, accepted_at=bound_at,
        reason=resolution.reason,
    )
    bind_archive_path(
        conn, content, path, bound_at=bound_at, expected_content_key=expected_key,
        acceptance_id=acceptance.acceptance_id,
    )
    return acceptance


def _accepted_binding_acceptance(
    conn: sqlite3.Connection, path: PathRef, content_key: str, check_id: str,
) -> PathAcceptance | None:
    """Return the acceptance for this exact evidence when it is already current."""
    if conn.execute(
        "SELECT 1 FROM archive_local_checks WHERE check_id=?", (check_id,),
    ).fetchone() is None:
        return None
    row = conn.execute(
        """SELECT archive_path_acceptances.acceptance_id
           FROM archive_path_bindings
           JOIN archive_path_acceptances
             ON archive_path_acceptances.acceptance_id=archive_path_bindings.acceptance_id
           WHERE archive_path_bindings.path_key=?
             AND archive_path_acceptances.content_key=?
             AND archive_path_acceptances.check_id=?""",
        (path.identity_key, content_key, check_id),
    ).fetchone()
    return None if row is None else _acceptance_by_id(conn, row[0])


def _acceptance_by_id(
    conn: sqlite3.Connection, acceptance_id: str,
) -> PathAcceptance | None:
    row = conn.execute(
        """SELECT archive_path_acceptances.acceptance_id,
                  archive_path_acceptances.path_key,
                  archive_path_acceptances.content_key,
                  archive_path_acceptances.asset_id,
                  archive_path_acceptances.revision,
                  archive_path_acceptances.expected_content_key,
                  archive_path_acceptances.root_revision,
                  archive_path_acceptances.check_id,
                  archive_path_acceptances.file_device,
                  archive_path_acceptances.file_index,
                  archive_path_acceptances.accepted_at,
                  archive_path_acceptances.reason,
                  archive_contents.size_bytes,
                  archive_contents.sha256
           FROM archive_path_acceptances
           JOIN archive_contents
             ON archive_contents.content_key=archive_path_acceptances.content_key
           WHERE archive_path_acceptances.acceptance_id=?""",
        (acceptance_id,),
    ).fetchone()
    if row is None:
        return None
    identity = None
    if row[8] is not None and row[9] is not None:
        identity = FileIdentity(int(row[8]), int(row[9]))
    return PathAcceptance(
        row[0], row[1], ContentVersion(row[3], row[4], row[12], row[13]), row[2],
        row[5], row[6], row[7], identity, row[10], row[11],
    )


def record_transfer_intent(
    conn: sqlite3.Connection, content: ContentVersion, path: PathRef, *,
    intent_id: str, root_revision: int, intent: RemovalIntent,
    source: str, observed_at: str, details: object = None,
) -> None:
    _require_write_transaction(conn, "Recording a transfer intent")
    if not isinstance(intent, RemovalIntent) or source not in {
        "user_reported", "program_observed", "excluded_scope"
    }:
        raise ContractError("Invalid transfer intent or provenance")
    if not isinstance(content, ContentVersion) or not isinstance(path, PathRef):
        raise ContractError("Expected content and path facts")
    if type(root_revision) is not int or root_revision < 0:
        raise ContractError("Invalid root revision")
    if not isinstance(observed_at, str) or not observed_at or len(observed_at) > 64:
        raise ContractError("Invalid transfer observation time")
    _token(intent_id, "intent ID")
    _require_asset_link(conn, content, path)
    conn.execute(
        """INSERT INTO archive_transfer_intents (
            intent_id,content_key,asset_id,revision,path_key,root_revision,
            intent,source,observed_at,details_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (intent_id, _content_key(content), content.asset_id, content.revision,
         path.identity_key, root_revision, intent.value, source, observed_at, _json(details)),
    )


def record_local_observation(
    conn: sqlite3.Connection, evidence: LocalEvidence, *,
    result: LocalObservation | None = None,
) -> None:
    """Persist exact evidence and, when supplied, its physical observation facts.

    This is the append-only diagnostic stream; it never moves which evidence a
    path has accepted.  Re-recording the same check ID with the same facts is
    idempotent, because a diagnostic written before a confirmation is applied is
    otherwise re-inserted by the acceptance step.
    """
    _require_write_transaction(conn, "Recording a local observation")
    if not isinstance(evidence, LocalEvidence):
        raise ContractError("Expected LocalEvidence")
    if result is not None and (
        not isinstance(result, LocalObservation) or result.evidence != evidence
    ):
        raise ContractError("Local observation does not match its evidence")
    content, path = evidence.content, evidence.path
    _require_content(conn, content)
    _require_asset_link(conn, content, path)

    physical = (None, None, None, None, None, None, None, None)
    if result is not None:
        identity = result.identity
        physical = (
            result.status.value, result.reason, int(result.digest_complete),
            result.bytes_read, result.size_bytes, result.mtime_ns,
            str(identity.device) if identity is not None else None,
            str(identity.file_index) if identity is not None else None,
        )
    values = (
        evidence.check_id, _content_key(content), content.asset_id, content.revision,
        path.identity_key, evidence.root_revision, evidence.run_id, evidence.checked_at,
        evidence.state.value, evidence.observed_size, evidence.observed_sha256,
        _json(asdict(evidence)), *physical,
    )
    existing = conn.execute(
        """SELECT content_key,asset_id,revision,path_key,root_revision,run_id,
                  checked_at,state,observed_size,observed_sha256,evidence_json,
                  observation_status,observation_reason,digest_complete,bytes_read,
                  physical_size,physical_mtime_ns,file_device,file_index
           FROM archive_local_checks WHERE check_id=?""",
        (evidence.check_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != values[1:]:
            raise ArchiveSchemaError("Local check ID already has different facts")
        return
    conn.execute(
        """INSERT INTO archive_local_checks (
            check_id,content_key,asset_id,revision,path_key,root_revision,run_id,
            checked_at,state,observed_size,observed_sha256,evidence_json,
            observation_status,observation_reason,digest_complete,bytes_read,
            physical_size,physical_mtime_ns,file_device,file_index
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        values,
    )


def record_archive_observation(
    conn: sqlite3.Connection, evidence: ArchiveEvidence, *,
    observed_size: int | None = None, observed_sha256: str | None = None,
) -> None:
    """Store observations without turning a reported upload into a verified one."""
    _require_write_transaction(conn, "Recording an archive observation")
    if not isinstance(evidence, ArchiveEvidence):
        raise ContractError("Expected ArchiveEvidence")
    content = evidence.content
    _token(evidence.location_ref, "location reference")
    if observed_size is not None and (
        type(observed_size) is not int or observed_size < 0
    ):
        raise ContractError("Invalid observed archive size")
    if observed_sha256 is not None and (
        not isinstance(observed_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", observed_sha256)
    ):
        raise ContractError("Invalid observed archive digest")
    verified = evidence.state.value in {"user_returned_verified", "adapter_readback_verified"}
    if verified and content.sha256 is None:
        raise ContractError("Verified archive observations require indexed content digest")
    if observed_size is not None and observed_size != content.size_bytes:
        raise ContractError("Observed archive size differs from indexed content")
    if observed_sha256 is not None and observed_sha256 != content.sha256:
        raise ContractError("Observed archive digest differs from indexed content")
    if verified and (
        observed_size != content.size_bytes or observed_sha256 != content.sha256
    ):
        raise ContractError("Verified archive observations require exact size and digest")
    _require_content(conn, content)
    rows = conn.execute(
        """SELECT m.size_bytes,m.sha256 FROM archive_members AS m
        JOIN archive_objects AS o ON o.object_id=m.package_id
        JOIN archive_locations AS l ON l.object_id=o.object_id
            AND l.object_revision=o.object_revision
        WHERE m.content_key=? AND l.location_id=? AND o.object_kind='package'""",
        (_content_key(content), evidence.location_ref),
    ).fetchall()
    if not any(tuple(row) == (content.size_bytes, content.sha256) for row in rows):
        raise ContractError("Archive location does not contain this exact content")
    conn.execute(
        """INSERT INTO archive_checks (
            check_id,content_key,asset_id,revision,location_id,checked_at,state,
            observed_size,observed_sha256,evidence_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (evidence.check_id, _content_key(content), content.asset_id, content.revision,
         evidence.location_ref, evidence.checked_at, evidence.state.value,
         observed_size, observed_sha256, _json(asdict(evidence))),
    )


def record_release_event(
    conn: sqlite3.Connection, content: ContentVersion, *,
    operation_id: str, event_id: str, sequence: int, event: str,
    local_check_id: str, archive_check_id: str, observed_at: str,
    evidence: object = None,
) -> None:
    """Append one release event after proving both evidence checks are verified."""
    _require_write_transaction(conn, "Recording a release event")
    _token(operation_id, "release operation ID")
    _token(event_id, "release event ID")
    _token(local_check_id, "local check ID")
    _token(archive_check_id, "archive check ID")
    if type(sequence) is not int or sequence < 1:
        raise ContractError("Invalid release sequence")
    if event not in {"prepared", "released", "failed", "cancelled"}:
        raise ContractError("Invalid release event")
    if not isinstance(observed_at, str) or not observed_at or len(observed_at) > 64:
        raise ContractError("Invalid release observation time")
    content_key = _require_content(conn, content)
    local = conn.execute(
        "SELECT content_key,state FROM archive_local_checks WHERE check_id=?",
        (local_check_id,),
    ).fetchone()
    archive = conn.execute(
        "SELECT content_key,state FROM archive_checks WHERE check_id=?",
        (archive_check_id,),
    ).fetchone()
    if local is None or tuple(local) != (content_key, "hash_match"):
        raise ContractError("Release requires a matching full local hash check")
    if archive is None or archive[0] != content_key or archive[1] not in {
        "user_returned_verified", "adapter_readback_verified"
    }:
        raise ContractError("Release requires a matching verified archive check")

    existing = conn.execute(
        """SELECT operation_id,sequence,content_key,local_check_id,archive_check_id,
                  event,observed_at,evidence_json FROM archive_releases WHERE event_id=?""",
        (event_id,),
    ).fetchone()
    evidence_json = _json(evidence)
    expected = (
        operation_id, sequence, content_key, local_check_id, archive_check_id,
        event, observed_at, evidence_json,
    )
    if existing is not None:
        if tuple(existing) != expected:
            raise ContractError("Release event ID already has different facts")
        return
    prior = conn.execute(
        """SELECT sequence,event,content_key,local_check_id,archive_check_id
           FROM archive_releases WHERE operation_id=? ORDER BY sequence""",
        (operation_id,),
    ).fetchall()
    if prior:
        last = prior[-1]
        if last[0] != sequence - 1:
            raise ContractError("Release events must be appended contiguously")
        if last[1] in {"released", "failed", "cancelled"}:
            raise ContractError("Release operation is already terminal")
        if event == "prepared":
            raise ContractError("A release operation may prepare only once")
        if any(row[1] == event for row in prior):
            raise ContractError("Release event type already exists for this operation")
        if any(tuple(row[2:]) != (content_key, local_check_id, archive_check_id) for row in prior):
            raise ContractError("Release events must bind one content and evidence pair")
    elif event != "prepared" or sequence != 1:
        raise ContractError("A release operation must start with prepared at sequence 1")
    conn.execute(
        """INSERT INTO archive_releases (
            event_id,operation_id,sequence,content_key,local_check_id,
            archive_check_id,event,observed_at,evidence_json
        ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (event_id, operation_id, sequence, content_key, local_check_id,
         archive_check_id, event, observed_at, evidence_json),
    )


@dataclass(frozen=True, slots=True)
class ArchiveMigrationReport:
    status: str
    source_version: str
    target_version: str
    backup_path: Path
    backup_sha256: str
    migrated_tables: tuple[str, ...]


def _inspect_source(conn: sqlite3.Connection) -> str:
    if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise ArchiveSchemaError("Source inspection must be read-only")
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ArchiveSchemaError("Source library failed integrity check")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "schema_meta" not in tables or _REQUIRED_TABLES - tables:
        raise ArchiveSchemaError("Source is not a complete JpgPng library")
    metadata = dict(conn.execute('SELECT "key","value" FROM schema_meta'))
    if metadata.get("format") != "jpgpng-library":
        raise ArchiveSchemaError("Source belongs to a different product")
    version = metadata.get("schema_version")
    if version == ARCHIVE_SCHEMA_VERSION:
        _check_existing_objects(conn)
    elif version != "1":
        raise ArchiveSchemaError(
            "Unsupported schema version; only the schema 1 base library migrates "
            "(schema 2 was a draft and schema 3 is superseded by schema 4)"
        )
    return version


def _check_destination(backup: Path, source: Path) -> None:
    if backup == source or source in backup.parents:
        raise ArchiveMigrationError("Backup must be a separate file outside the source")
    if backup.exists() or Path(str(backup) + "-wal").exists() or Path(str(backup) + "-shm").exists():
        raise ArchiveMigrationError("Backup destination already exists")
    _validate_database_path(backup)


def migrate_archive_copy(
    database_path: str | Path, backup_path: str | Path, *,
    temporary_copy_confirmed: bool = False,
) -> ArchiveMigrationReport:
    """Migrate only a closed, explicitly acknowledged temporary library copy.

    The original library is never discovered or opened by this function.  No
    automatic rollback deletes/overwrites a database after a failed commit; the
    verified pre-migration backup is the recovery artifact.
    """
    if temporary_copy_confirmed is not True:
        raise ArchiveMigrationError("Explicit temporary-copy confirmation is required")
    source = _validate_database_path(database_path)
    if not source.is_file():
        raise ArchiveMigrationError("Temporary source database does not exist")
    backup = _validate_database_path(backup_path)
    _check_destination(backup, source)
    lock_path = source.parent / ".writer.lock"
    try:
        with InstanceLock(lock_path):
            with closing(sqlite3.connect(source, isolation_level=None)) as conn:
                conn.execute("PRAGMA busy_timeout=1000")
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA foreign_keys=ON")
                source_version = _inspect_source_for_migration(conn)
                backup.parent.mkdir(parents=True, exist_ok=True)
                try:
                    # Reserve a new name atomically; never overwrite an existing backup.
                    with backup.open("xb"):
                        pass
                    with closing(sqlite3.connect(backup)) as destination:
                        conn.backup(destination)
                        destination.execute("PRAGMA foreign_keys=ON")
                        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise ArchiveMigrationError("Backup integrity check failed")
                        _inspect_source_for_migration(destination)
                except BaseException:
                    # Keep a failed destination for diagnosis; never overwrite it.
                    raise
                conn.execute("PRAGMA query_only=OFF")
                if source_version == ARCHIVE_SCHEMA_VERSION:
                    validate_archive_facts(conn)
                else:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        _inspect_source_for_migration(conn)
                        for statement in ARCHIVE_DDL:
                            conn.execute(statement)
                        conn.execute(
                            "UPDATE schema_meta SET value=? WHERE \"key\"='schema_version'",
                            (ARCHIVE_SCHEMA_VERSION,),
                        )
                        _check_existing_objects(conn)
                        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise ArchiveMigrationError("Migrated copy failed integrity check")
                        if conn.execute("PRAGMA foreign_key_check").fetchall():
                            raise ArchiveMigrationError("Migrated copy has foreign key violations")
                        conn.execute("COMMIT")
                    except BaseException:
                        if conn.in_transaction:
                            conn.execute("ROLLBACK")
                        raise
                checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is not None and checkpoint[0] != 0:
                    raise ArchiveMigrationError("Archive migration checkpoint could not drain WAL")
                digest = hashlib.sha256()
                with backup.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                digest = digest.hexdigest()
                return ArchiveMigrationReport(
                    "already_current" if source_version == ARCHIVE_SCHEMA_VERSION else "migrated",
                    source_version, ARCHIVE_SCHEMA_VERSION, backup, digest,
                    tuple(sorted(ARCHIVE_TABLES)),
                )
    except AlreadyRunningError as exc:
        raise ArchiveMigrationError("Temporary database is already in use") from exc
    except ArchiveMigrationError:
        raise
    except (sqlite3.Error, OSError, ContractError) as exc:
        raise ArchiveMigrationError("Temporary archive migration failed") from exc


def _inspect_source_for_migration(conn: sqlite3.Connection) -> str:
    """Validate a caller-provided database; migration itself supplies the write boundary."""
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ArchiveSchemaError("Library failed integrity check")
    actual = _normalized_sql_objects(conn)
    for statement in _DDL:
        match = re.match(r"\s*CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+(\w+)", statement, re.I)
        if match is None or actual.get(match.group(1)) != " ".join(statement.split()):
            raise ArchiveSchemaError("Missing or incompatible base library object")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "schema_meta" not in tables or _REQUIRED_TABLES - tables:
        raise ArchiveSchemaError("Not a complete JpgPng library")
    metadata = dict(conn.execute('SELECT "key","value" FROM schema_meta'))
    if metadata.get("format") != "jpgpng-library":
        raise ArchiveSchemaError("Not a JpgPng library")
    version = metadata.get("schema_version")
    if version == ARCHIVE_SCHEMA_VERSION:
        _check_existing_objects(conn)
    elif version != "1":
        raise ArchiveSchemaError(
            "Unsupported schema version; only the schema 1 base library migrates "
            "(schema 2 was a draft and schema 3 is superseded by schema 4)"
        )
    return version
