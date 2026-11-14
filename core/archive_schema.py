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

# Schema 4 added the append-only acceptance table that separates "what a path is
# currently bound to" from "which diagnostic observations were written".  Schema 5
# keeps those objects and adds the two covering indexes the digest and path-owner
# lookups need; the schema 4 acceptance-event draft is refused rather than
# silently reinterpreted, and no production library of either draft exists.
ARCHIVE_SCHEMA_VERSION = "5"
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


class _UnsetType:
    """The "this caller supplied no token" value.

    ``None`` already means "the current binding has no acceptance" - an index-only
    binding created by a plan, whose physical object is explicitly unknown - so it
    cannot also mean "skip the comparison".  Only this sentinel says "not given".
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"


UNSET = _UnsetType()


@dataclass(frozen=True, slots=True)
class _AcceptanceFacts:
    """One stored acceptance row after its own column facts are validated."""

    acceptance_id: str
    path_key: str
    content_key: str
    asset_id: str
    revision: int
    expected_content_key: str | None
    root_revision: int
    check_id: str | None
    file_device: str | None
    file_index: str | None
    accepted_at: str
    reason: str


ACCEPTANCE_COLUMNS = (
    "acceptance_id,path_key,content_key,asset_id,revision,expected_content_key,"
    "root_revision,check_id,file_device,file_index,accepted_at,reason"
)


def _acceptance_facts(row: tuple) -> _AcceptanceFacts:
    """Rebuild one acceptance row, refusing malformed columns.

    Every reader (write entry points, binding load and the maintenance audit) goes
    through this function, so none of them can reinterpret the same row differently.
    """
    (acceptance_id, path_key, content_key, asset_id, revision, expected_content_key,
     root_revision, check_id, device, index, accepted_at, reason) = row
    for value, field in (
        (acceptance_id, "acceptance identity"), (path_key, "acceptance path"),
        (content_key, "acceptance content"), (asset_id, "acceptance asset"),
        (reason, "acceptance reason"),
    ):
        if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
            raise ArchiveSchemaError("Invalid stored " + field)
    if type(revision) is not int or revision < 0:
        raise ArchiveSchemaError("Path acceptance revision is invalid")
    if expected_content_key is not None and (
            not isinstance(expected_content_key, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_content_key) is None):
        raise ArchiveSchemaError("Path acceptance expected content is invalid")
    if type(root_revision) is not int or root_revision < 0:
        raise ArchiveSchemaError("Path acceptance root revision is invalid")
    if check_id is not None and (
            not isinstance(check_id, str) or _TOKEN.fullmatch(check_id) is None):
        raise ArchiveSchemaError("Path acceptance check identity is invalid")
    if (device is None) != (index is None):
        raise ArchiveSchemaError("Path acceptance physical identity is incomplete")
    for value, field in ((device, "device"), (index, "index")):
        if value is not None and (
                not isinstance(value, str) or re.fullmatch(r"[0-9]{1,32}", value) is None):
            raise ArchiveSchemaError("Path acceptance physical " + field + " is invalid")
    if not isinstance(accepted_at, str) or not accepted_at or len(accepted_at) > 64:
        raise ArchiveSchemaError("Path acceptance time is invalid")
    return _AcceptanceFacts(
        acceptance_id, path_key, content_key, asset_id, revision, expected_content_key,
        root_revision, check_id, device, index, accepted_at, reason,
    )


def _acceptance_values(facts: _AcceptanceFacts) -> tuple[object, ...]:
    """The immutable facts of one acceptance.

    ``accepted_at`` is deliberately excluded: it records when the evidence was
    first committed, so a replay with a later call time is still the same event.
    """
    return (
        facts.path_key, facts.content_key, facts.asset_id, facts.revision,
        facts.expected_content_key, facts.root_revision, facts.check_id,
        facts.file_device, facts.file_index, facts.reason,
    )


def _expected_predecessor_reason(
    *, asset_id: str, revision: int, expected_row: tuple | None,
) -> str | None:
    """Why this expected-old content cannot have been replaced here, or None.

    An acceptance may name the generation it replaced only when that generation is
    an older revision of the same logical asset.  Naming itself, a later revision,
    another asset or an unknown row is refused before anything is written, exactly
    as the maintenance audit refuses it afterwards.
    """
    if expected_row is None:
        return "Path acceptance expects an unknown content row"
    if expected_row[0] != asset_id or expected_row[1] >= revision:
        return "Path acceptance expects a generation that cannot precede it"
    return None


def _acceptance_check_reason(
    facts: _AcceptanceFacts, check_row: tuple | None, *,
    size_bytes: int, sha256: str | None,
) -> str | None:
    """Why this acceptance's evidence cannot justify these exact bytes, or None."""
    if facts.check_id is None:
        if _device_claimed(facts):
            return "Path acceptance claims an unobserved physical object"
        return None
    if check_row is None:
        return "Path acceptance references a missing local check"
    if (
        check_row[0] != facts.path_key
        or check_row[1] != facts.root_revision
        or check_row[2] != size_bytes
        or check_row[3] != sha256
        or check_row[4] != facts.file_device
        or check_row[5] != facts.file_index
    ):
        return "Path acceptance differs from its local check"
    return None


def _device_claimed(facts: _AcceptanceFacts) -> bool:
    return facts.file_device is not None or facts.file_index is not None


def _local_check_row(conn: sqlite3.Connection, check_id: str | None) -> tuple | None:
    """Read the observation an acceptance claims, without any other table scan."""
    if check_id is None:
        return None
    return conn.execute(
        """SELECT path_key,root_revision,observed_size,observed_sha256,file_device,file_index
           FROM archive_local_checks WHERE check_id=?""",
        (check_id,),
    ).fetchone()


def _acceptance_link_reason(
    facts: _AcceptanceFacts, *, content: ContentVersion, path: PathRef,
    check_row: tuple | None, root_revision: int | None = None,
) -> str | None:
    """Why this acceptance is not the evidence of exactly this path and content."""
    content_key = _content_key(content)
    if (
        facts.path_key != path.identity_key
        or facts.content_key != content_key
        or facts.asset_id != content.asset_id
        or facts.revision != content.revision
    ):
        return "Path acceptance does not describe this path and content"
    if root_revision is not None and facts.root_revision != root_revision:
        return "Path acceptance belongs to another root generation"
    return _acceptance_check_reason(
        facts, check_row, size_bytes=content.size_bytes, sha256=content.sha256)


def _load_path_acceptance(
    conn: sqlite3.Connection, acceptance_id: str,
) -> _AcceptanceFacts | None:
    row = conn.execute(
        "SELECT " + ACCEPTANCE_COLUMNS + " FROM archive_path_acceptances "
        "WHERE acceptance_id=?",
        (acceptance_id,),
    ).fetchone()
    return None if row is None else _acceptance_facts(tuple(row))


def _require_acceptance_link(
    conn: sqlite3.Connection, acceptance_id: str, content: ContentVersion,
    path: PathRef, *, root_revision: int | None = None,
) -> _AcceptanceFacts:
    """Load one acceptance and refuse it unless it proves exactly this binding.

    Merely existing is not enough: a path must not inherit another path's, another
    revision's or another asset's accepted physical object.  This is the write-time
    twin of the audit's "path binding does not match its acceptance" check.
    """
    facts = _load_path_acceptance(conn, acceptance_id)
    if facts is None:
        raise ContractError("Path binding references an unknown acceptance")
    reason = _acceptance_link_reason(
        facts, content=content, path=path,
        check_row=_local_check_row(conn, facts.check_id), root_revision=root_revision,
    )
    if reason is not None:
        raise ArchiveSchemaError(reason)
    return facts


def _acceptance_object(
    facts: _AcceptanceFacts, content: ContentVersion,
) -> PathAcceptance:
    identity = None
    if facts.file_device is not None and facts.file_index is not None:
        try:
            identity = FileIdentity(int(facts.file_device), int(facts.file_index))
        except (TypeError, ValueError) as exc:
            raise ArchiveSchemaError("Stored accepted physical identity is invalid") from exc
    return PathAcceptance(
        facts.acceptance_id, facts.path_key, content, facts.content_key,
        facts.expected_content_key, facts.root_revision, facts.check_id, identity,
        facts.accepted_at, facts.reason,
    )


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
    # AR4-07: the digest lookup and the path-owner lookup both scanned with a
    # temporary sort tree.  These prefixes cover the whole hot query, so neither
    # needs a table scan or a temporary B-tree any more.
    "CREATE INDEX idx_archive_contents_digest ON archive_contents(sha256, size_bytes, asset_id, revision)",
    "CREATE INDEX idx_archive_assets_path ON archive_assets(path_key, asset_id)",
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
# This enumeration only cross-checks names that are already declared below;
            # it never compares "extra" objects, so the imprecise LIKE cannot hide a
            # user table (that defect was fixed in core.library_maintenance).
                        "AND name NOT LIKE 'sqlite_%'"  # LIKE reads _ as a wildcard: see the note above
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

    content_keys = {key: value for key, value in content_facts.values()}
    acceptances: dict[str, tuple[str, str, int]] = {}
    for row in conn.execute(
        "SELECT " + ACCEPTANCE_COLUMNS + " FROM archive_path_acceptances"
    ):
        facts = _acceptance_facts(tuple(row))
        asset = assets.get((facts.asset_id, facts.revision, facts.path_key))
        if asset is None:
            raise ArchiveSchemaError("Path acceptance has no exact asset row")
        content = asset[0]
        if facts.content_key != _content_key(content):
            raise ArchiveSchemaError("Path acceptance content differs from its asset")
        if facts.expected_content_key is not None:
            expected_content = content_keys.get(facts.expected_content_key)
            expected_row = (
                None if expected_content is None
                else (expected_content.asset_id, expected_content.revision)
            )
            # An acceptance replaces an older generation of the same logical asset.
            # It cannot expect itself, a later generation, or another asset's row;
            # record_path_acceptance applies this same rule before its insert.
            expected_reason = _expected_predecessor_reason(
                asset_id=facts.asset_id, revision=facts.revision,
                expected_row=expected_row,
            )
            if expected_reason is not None:
                raise ArchiveSchemaError(expected_reason)
        check_row = _local_check_row(conn, facts.check_id)
        # The accepted physical object must be the one the referenced check
        # observed, and a check-less acceptance must not claim one at all.
        reason = _acceptance_check_reason(
            facts, None if check_row is None else tuple(check_row),
            size_bytes=content.size_bytes, sha256=content.sha256,
        )
        if reason is not None:
            raise ArchiveSchemaError(reason)
        acceptances[facts.acceptance_id] = (
            facts.path_key, facts.asset_id, facts.revision)

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
    from the caller, so a caller cannot claim an object it did not observe.  That
    check must name this path, this root generation and exactly these bytes.  Its
    own content link is deliberately not compared: a baseline observation is
    stored against the unknown historical generation it replaced while the
    accepted revision is the newly created one.

    ``expected_content_key`` names the generation this acceptance replaces, which
    is only meaningful as an older revision of the same logical asset.  Naming
    itself, a later generation, another asset or an unknown row is refused before
    the insert, exactly as the maintenance audit would refuse it afterwards.

    Re-recording the same evidence is a replay: every immutable fact must agree, a
    later ``accepted_at`` does not make it another event, and the first stored time
    is what the caller receives back.
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
    check = _local_check_row(conn, check_id)
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
    if expected_content_key is not None:
        expected_row = conn.execute(
            "SELECT asset_id,revision FROM archive_contents WHERE content_key=?",
            (expected_content_key,),
        ).fetchone()
        expected_reason = _expected_predecessor_reason(
            asset_id=content.asset_id, revision=content.revision,
            expected_row=None if expected_row is None else tuple(expected_row),
        )
        if expected_reason is not None:
            raise ArchiveSchemaError(expected_reason)
    acceptance_id = _acceptance_key(path.identity_key, content_key, check_id)
    proposed = _AcceptanceFacts(
        acceptance_id, path.identity_key, content_key, content.asset_id,
        content.revision, expected_content_key, root_revision, check_id,
        check[4], check[5], accepted_at, reason,
    )
    existing = _load_path_acceptance(conn, acceptance_id)
    if existing is not None:
        if _acceptance_values(existing) != _acceptance_values(proposed):
            raise ArchiveSchemaError("Acceptance identity already has different facts")
        return _acceptance_object(existing, content)
    conn.execute(
        """INSERT INTO archive_path_acceptances (
            acceptance_id,path_key,content_key,asset_id,revision,expected_content_key,
            root_revision,check_id,file_device,file_index,accepted_at,reason
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            acceptance_id, proposed.path_key, proposed.content_key, proposed.asset_id,
            proposed.revision, proposed.expected_content_key, proposed.root_revision,
            proposed.check_id, proposed.file_device, proposed.file_index,
            proposed.accepted_at, proposed.reason,
        ),
    )
    return _acceptance_object(proposed, content)


def _require_expected_acceptance(
    current_acceptance: str | None, expected_acceptance_id: str | None | _UnsetType,
) -> None:
    """Require the caller's token to name the acceptance still in place.

    ``UNSET`` means the caller supplied none, which is only acceptable when the
    binding has no accepted evidence at all: rewriting an accepted binding without
    stating which acceptance it expected is exactly the stale-caller hole AR4-01
    describes.  ``None`` states "there is no acceptance", and a string states which
    one.
    """
    if current_acceptance is None:
        if isinstance(expected_acceptance_id, str):
            raise ContractError(
                "Path binding has no acceptance the expected token could name")
    elif expected_acceptance_id is UNSET:
        raise ContractError(
            "Updating an accepted path binding requires its expected acceptance ID")
    elif expected_acceptance_id != current_acceptance:
        raise ContractError("Path acceptance changed without the expected acceptance ID")


def bind_archive_path(
    conn: sqlite3.Connection, content: ContentVersion, path: PathRef, *,
    bound_at: str, expected_content_key: str | None = None,
    acceptance_id: str | None = None,
    expected_acceptance_id: str | None | _UnsetType = UNSET,
) -> None:
    """Point a path at a content revision only with explicit expectations.

    ``expected_content_key=None`` still means "this path must still be unbound", so
    a value re-read by the caller is never substituted for the resolution input.
    ``acceptance_id`` records which accepted evidence the binding now points at;
    that row must prove exactly this path, content, asset and revision instead of
    merely existing.

    ``expected_acceptance_id`` is the binding-generation half of the expectation:
    ``UNSET`` means the caller supplied none, ``None`` means the current binding must
    be an index-only binding without accepted evidence, and a string means it must
    still point at exactly that acceptance.  An accepted binding therefore can
    never be silently rewritten into an index-only one, and a stale caller cannot
    move the pointer back to older evidence.
    """
    _require_write_transaction(conn, "Binding an archive path")
    if not isinstance(bound_at, str) or not bound_at or len(bound_at) > 64:
        raise ContractError("Invalid path binding time")
    if acceptance_id is not None:
        _token(acceptance_id, "path acceptance ID")
    if not (expected_acceptance_id is UNSET or expected_acceptance_id is None):
        _token(expected_acceptance_id, "expected path acceptance ID")
    _require_asset_link(conn, content, path)
    if acceptance_id is not None:
        _require_acceptance_link(conn, acceptance_id, content, path)
    new_key = _content_key(content)
    current = conn.execute(
        "SELECT asset_id,revision,acceptance_id FROM archive_path_bindings WHERE path_key=?",
        (path.identity_key,),
    ).fetchone()
    if current is None:
        if expected_content_key is not None:
            raise ContractError("First path binding cannot expect a previous content")
        if isinstance(expected_acceptance_id, str):
            raise ContractError("First path binding cannot expect an acceptance")
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
        # Same content: re-pointing at the very acceptance the binding already has
        # (or naming no acceptance at all) changes nothing, so it is a no-op and
        # needs no token.  Only actually moving the accepted evidence does.
        if acceptance_id is None or acceptance_id == current[2]:
            return
        _require_expected_acceptance(current[2], expected_acceptance_id)
        conn.execute(
            "UPDATE archive_path_bindings SET bound_at=?, acceptance_id=? WHERE path_key=?",
            (bound_at, acceptance_id, path.identity_key),
        )
        return
    _require_expected_acceptance(current[2], expected_acceptance_id)
    if expected_content_key is None or expected_content_key != current_key[0]:
        raise ContractError("Path binding changed without the expected old content")
    if current[2] is not None and acceptance_id is None:
        raise ContractError(
            "An accepted path binding cannot become an index-only binding")
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
    at, and that row must describe exactly this path, content, asset and revision.
    A crossed or damaged acceptance raises instead of handing back another file's
    physical identity.  Diagnostic observations are never consulted here:
    recording a changed, missing or unreadable check cannot move what this path
    has accepted.
    """
    if not isinstance(path, PathRef):
        raise ContractError("Expected a path reference")
    row = conn.execute(
        """SELECT c.content_key,c.asset_id,c.revision,c.size_bytes,c.sha256,b.acceptance_id
           FROM archive_path_bindings AS b
           JOIN archive_assets AS a
             ON a.asset_id=b.asset_id AND a.revision=b.revision
            AND a.path_key=b.path_key
           JOIN archive_contents AS c
             ON c.asset_id=a.asset_id AND c.revision=a.revision
           WHERE b.path_key=?""",
        (path.identity_key,),
    ).fetchone()
    if row is None:
        return None
    content = ContentVersion(row[1], row[2], row[3], row[4])
    if row[0] != _content_key(content):
        raise ArchiveSchemaError("Path binding content identity differs from its facts")
    if row[5] is None:
        return ContentBinding(content)
    facts = _load_path_acceptance(conn, row[5])
    if facts is None:
        raise ArchiveSchemaError("Path binding references a missing acceptance")
    reason = _acceptance_link_reason(
        facts, content=content, path=path,
        check_row=_local_check_row(conn, facts.check_id),
    )
    if reason is not None:
        raise ArchiveSchemaError(reason)
    acceptance = _acceptance_object(facts, content)
    return ContentBinding(
        content, acceptance.physical_identity, facts.acceptance_id, facts.root_revision,
    )


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


def _asset_max_revision(
    conn: sqlite3.Connection, asset_id: str,
) -> int | None:
    """Highest generation already stored for one logical asset, or None.

    The scope asset id follows the physical object, so a rename keeps it.  This is
    the floor a new generation must clear; identical bytes owned by another asset
    are never consulted here.
    """
    row = conn.execute(
        "SELECT MAX(revision) FROM archive_contents WHERE asset_id=?", (asset_id,),
    ).fetchone()
    value = None if row is None else row[0]
    return None if value is None else int(value)


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
        asset_max_revision=(
            None if content is None else _asset_max_revision(conn, content.asset_id)
        ),
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


@dataclass(frozen=True, slots=True)
class _StoredBinding:
    """The current binding row plus the exact content facts it points at."""

    asset_id: str
    revision: int
    content_key: str
    size_bytes: int
    sha256: str | None
    acceptance_id: str | None
    root_revision: int | None


def _stored_binding(
    conn: sqlite3.Connection, path: PathRef,
) -> _StoredBinding | None:
    """Read the current binding and the acceptance generation it points at.

    The root generation comes from the acceptance row, so a tampered acceptance
    also changes what the expectation token is compared against.
    """
    row = conn.execute(
        "SELECT asset_id,revision,acceptance_id FROM archive_path_bindings WHERE path_key=?",
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
    root_revision = None
    if row[2] is not None:
        facts = _load_path_acceptance(conn, row[2])
        if facts is None:
            raise ArchiveSchemaError("Path binding references a missing acceptance")
        root_revision = facts.root_revision
    return _StoredBinding(row[0], row[1], key[0], key[1], key[2], row[2], root_revision)


def apply_content_resolution(
    conn: sqlite3.Connection, resolution: ContentResolution, *,
    observed: ObservedFile, bound_at: str,
    source_kind: str = "observed_existing",
) -> PathAcceptance:
    """Write one resolved generation inside the caller's transaction.

    The expectation is the immutable binding the resolution was computed from:
    the content key, the acceptance that binding pointed at and that acceptance's
    root generation, plus "this path must not exist yet" as its own state.  All of
    it is compared with the stored binding before any row is written, so a
    resolution produced before another acceptance, a root remap or a same-content
    physical replacement is refused instead of silently applied.

    Two cases are not conflicts:

    * a genuine replay - the same path, content and check already accepted, with
      every immutable fact equal and the binding still pointing at that absolute
      acceptance - returns the first stored acceptance and writes nothing;
    * a stored check ID that already carries exactly these observation facts is
      reused, so an observation recorded as a diagnostic before the confirmation is
      not inserted twice.

    Everything else fails before the first INSERT/UPDATE, and the caller's
    transaction rolls back whatever it had already done.
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

    rebound = rebind_observation(observed, resolution)
    if rebound is None:
        raise ContractError("The observation cannot justify an accepted binding")
    # A reused check ID must describe exactly these observation facts.  Comparing
    # them here - before the replay shortcut - means a wrong run, time, digest,
    # size, identity or root is a structured refusal, not a free replay.
    _require_local_check_consistent(conn, rebound.evidence, rebound)

    # Nothing was superseded when the binding already names this generation, so
    # the acceptance records no older generation instead of naming itself.
    acceptance_expected = None if expected_key == new_key else expected_key
    replay = _replayed_acceptance(
        conn, path, content, check_id=check_id, root_revision=root_revision,
        expected_content_key=acceptance_expected, reason=resolution.reason,
    )
    if replay is not None:
        return replay

    current = _stored_binding(conn, path)
    if current is None:
        if not absent:
            raise ArchiveBindingConflictError(
                path.identity_key, expected_key, None, "expected_binding_missing")
    else:
        if absent:
            raise ArchiveBindingConflictError(
                path.identity_key, None, current.content_key, "expected_binding_present")
        if current.content_key != expected_key:
            raise ArchiveBindingConflictError(
                path.identity_key, expected_key, current.content_key,
                "expected_binding_changed")
        if expected is None or (current.size_bytes, current.sha256) != (
                expected.size_bytes, expected.sha256):
            raise ArchiveBindingConflictError(
                path.identity_key, expected_key, current.content_key,
                "content_facts_differ")
        if current.acceptance_id != resolution.expected_acceptance_id:
            raise ArchiveBindingConflictError(
                path.identity_key, expected_key, current.content_key,
                "expected_acceptance_changed")
        if current.root_revision != resolution.expected_root_revision:
            raise ArchiveBindingConflictError(
                path.identity_key, expected_key, current.content_key,
                "expected_root_changed")
        if current.root_revision is not None and root_revision < current.root_revision:
            raise ArchiveBindingConflictError(
                path.identity_key, expected_key, current.content_key,
                "observation_root_precedes_binding")
        if expected is not None and current.revision != expected.revision:
            raise ArchiveBindingConflictError(
                path.identity_key, expected_key, current.content_key,
                "expected_revision_changed")

    # The asset's generations are allocated by the resolver from its own history.
    # Another path of the same asset may have committed since; a revision that is
    # no longer free must be refused here with a re-observe decision instead of
    # failing deep inside ensure_archive_asset on an immutable-key collision.
    stored = conn.execute(
        "SELECT asset_id,revision,size_bytes,sha256 FROM archive_contents WHERE content_key=?",
        (new_key,),
    ).fetchone()
    if stored is None or tuple(stored) != _content_values(content):
        # The key is derived from (asset, revision) alone, so an existing row with
        # other bytes means another path of this asset already took that
        # generation.  Refuse with a re-observe decision instead of letting
        # ensure_archive_asset report an immutable-key collision.
        allocated = _asset_max_revision(conn, content.asset_id)
        if allocated is not None and content.revision <= allocated:
            raise ArchiveBindingConflictError(
                path.identity_key, expected_key, new_key,
                "asset_revision_allocated_concurrently")

    # The stored diagnostic references the generation it was rebound to, so both
    # that generation and the resolved one must be indexed before the observation
    # row.  The raw probe is a read contract (it may carry the new size), never a
    # stored fact.
    for referenced in (expected, rebound.evidence.content):
        if referenced is None or _content_key(referenced) == new_key:
            continue
        # The row must exist for *this* path, not only somewhere in the library:
        # a renamed object carries its unknown-history generation with it, so the
        # content row may already be indexed under the old path.
        if _asset_row_is_exact(conn, referenced, path):
            continue
        ensure_archive_asset(conn, referenced, path, source_kind=source_kind)
    if not _asset_row_is_exact(conn, content, path):
        ensure_archive_asset(conn, content, path, source_kind=source_kind)
    record_local_observation(conn, rebound.evidence, result=rebound)
    acceptance = record_path_acceptance(
        conn, content, path, check_id=check_id, root_revision=root_revision,
        expected_content_key=acceptance_expected,
        accepted_at=bound_at, reason=resolution.reason,
    )
    bind_archive_path(
        conn, content, path, bound_at=bound_at, expected_content_key=expected_key,
        acceptance_id=acceptance.acceptance_id,
        expected_acceptance_id=resolution.expected_acceptance_id,
    )
    return acceptance


def _replayed_acceptance(
    conn: sqlite3.Connection, path: PathRef, content: ContentVersion, *,
    check_id: str, root_revision: int, expected_content_key: str | None, reason: str,
) -> PathAcceptance | None:
    """Return the first stored result of exactly this operation when it is current.

    A replay needs all three versions to agree: the same operation identity, every
    immutable acceptance fact, and a current binding that is still the one this
    operation wrote.  A historical success whose pointer has since advanced is not
    reported as a new write; the caller's token comparison refuses it instead of
    rolling the current pointer back to the older event.
    """
    acceptance_id = _acceptance_key(path.identity_key, _content_key(content), check_id)
    facts = _load_path_acceptance(conn, acceptance_id)
    if facts is None:
        return None
    mismatch = _acceptance_link_reason(
        facts, content=content, path=path,
        check_row=_local_check_row(conn, check_id), root_revision=root_revision,
    )
    if (
        mismatch is not None
        or facts.expected_content_key != expected_content_key
        or facts.reason != reason
    ):
        raise ArchiveSchemaError("Acceptance identity already has different facts")
    current = _stored_binding(conn, path)
    if (
        current is None
        or current.content_key != facts.content_key
        or current.acceptance_id != acceptance_id
        or current.root_revision != root_revision
    ):
        return None
    return _acceptance_object(facts, content)


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


LOCAL_CHECK_COLUMNS = (
    "content_key,asset_id,revision,path_key,root_revision,run_id,checked_at,state,"
    "observed_size,observed_sha256,evidence_json,observation_status,observation_reason,"
    "digest_complete,bytes_read,physical_size,physical_mtime_ns,file_device,file_index"
)


def _local_check_values(
    evidence: LocalEvidence, result: LocalObservation | None,
) -> tuple[object, ...]:
    """The exact stored columns of one observation, including its check ID."""
    physical = (None, None, None, None, None, None, None, None)
    if result is not None:
        identity = result.identity
        physical = (
            result.status.value, result.reason, int(result.digest_complete),
            result.bytes_read, result.size_bytes, result.mtime_ns,
            str(identity.device) if identity is not None else None,
            str(identity.file_index) if identity is not None else None,
        )
    content, path = evidence.content, evidence.path
    return (
        evidence.check_id, _content_key(content), content.asset_id, content.revision,
        path.identity_key, evidence.root_revision, evidence.run_id, evidence.checked_at,
        evidence.state.value, evidence.observed_size, evidence.observed_sha256,
        _json(asdict(evidence)), *physical,
    )


def _require_local_check_consistent(
    conn: sqlite3.Connection, evidence: LocalEvidence,
    result: LocalObservation | None = None,
) -> bool:
    """Refuse a check ID that already stores other observation facts.

    Returns True when exactly these diagnostic facts are already stored, so a
    caller can skip both the comparison and the insert.  Applying a resolution
    compares these facts before its replay shortcut: reusing one operation ID with
    another run, time, digest, size, identity or root generation is a structured
    refusal, never a free pass.
    """
    row = conn.execute(
        "SELECT " + LOCAL_CHECK_COLUMNS + " FROM archive_local_checks WHERE check_id=?",
        (evidence.check_id,),
    ).fetchone()
    if row is None:
        return False
    if tuple(row) != _local_check_values(evidence, result)[1:]:
        raise ArchiveSchemaError("Local check ID already has different facts")
    return True


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
    _require_content(conn, evidence.content)
    _require_asset_link(conn, evidence.content, evidence.path)
    if _require_local_check_consistent(conn, evidence, result):
        return
    conn.execute(
        """INSERT INTO archive_local_checks (
            check_id,content_key,asset_id,revision,path_key,root_revision,run_id,
            checked_at,state,observed_size,observed_sha256,evidence_json,
            observation_status,observation_reason,digest_complete,bytes_read,
            physical_size,physical_mtime_ns,file_device,file_index
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        _local_check_values(evidence, result),
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


_SUPERSEDED_SCHEMA_VERSIONS = {
    "4": "the schema 4 acceptance-event draft, superseded by schema 5; "
         "no production library exists",
    "3": "the schema 3 pre-acceptance draft",
    "2": "the schema 2 draft",
}


def _refuse_source_version(version: str) -> None:
    """Refuse a draft version instead of relabelling or reinterpreting it.

    The list of superseded versions and its wording live in one place so a new
    version number cannot silently fall back to an older message.
    """
    detail = _SUPERSEDED_SCHEMA_VERSIONS.get(version, "unknown version marker")
    raise ArchiveSchemaError(
        "Unsupported archive schema version %r (%s); only a schema 1 base library "
        "migrates, and a current schema %s copy is used as is. Re-create a temporary "
        "copy from a schema 1 library instead of relabelling a draft."
        % (version, detail, ARCHIVE_SCHEMA_VERSION)
    )


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
        _refuse_source_version(version)
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
        _refuse_source_version(version)
    return version
