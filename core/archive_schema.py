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
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from core.library_paths import AlreadyRunningError, InstanceLock
from core.library_repository import _DDL, _REQUIRED_TABLES, _validate_database_path
from core.preservation_policy import (
    ArchiveEvidence, ArchiveState, ContentVersion, LocalEvidence, LocalState, RemovalIntent,
)
from core.task_contracts import ContractError, PathRef, canonical_json, normalize_json

ARCHIVE_SCHEMA_VERSION = "3"
ARCHIVE_PROTOCOL_VERSION = 2
ARCHIVE_JSON_LIMIT = 8 * 1024 * 1024
ARCHIVE_TABLES = frozenset({
    "archive_batches", "archive_contents", "archive_assets", "archive_transfer_intents",
    "archive_local_checks", "archive_packages", "archive_members", "archive_objects",
    "archive_path_bindings", "archive_groups", "archive_group_objects", "archive_locations",
    "archive_checks", "archive_releases",
})
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class ArchiveMigrationError(Exception):
    """The explicit temporary-copy migration cannot safely proceed."""


class ArchiveSchemaError(ArchiveMigrationError):
    """The source, destination or recorded evidence violates this schema."""


def _token(value: object, field: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise ContractError(f"Invalid {field}")
    return value


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
        raise ContractError("Archive evidence detail exceeds 1 MiB")
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
        CHECK ((source_kind != 'download_receipt') OR (artifact_id IS NOT NULL))
    )""",
    """CREATE TABLE archive_path_bindings (
        path_key TEXT PRIMARY KEY,
        content_key TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 0),
        bound_at TEXT NOT NULL,
        FOREIGN KEY (asset_id, revision, path_key) REFERENCES archive_assets(
            asset_id, revision, path_key
        )
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
        FOREIGN KEY (asset_id, revision, path_key) REFERENCES archive_assets(
            asset_id, revision, path_key
        ),
        UNIQUE (check_id, content_key)
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
        UNIQUE (operation_id, event)
    )""",
    "CREATE INDEX idx_archive_assets_content ON archive_assets(content_key, path_key)",
    "CREATE INDEX idx_archive_intents_asset ON archive_transfer_intents(asset_id, revision, observed_at)",
    "CREATE INDEX idx_archive_local_run ON archive_local_checks(run_id, path_key, checked_at)",
    "CREATE INDEX idx_archive_packages_batch ON archive_packages(batch_id, package_id)",
    "CREATE INDEX idx_archive_members_asset ON archive_members(asset_id, revision, path_key)",
    "CREATE INDEX idx_archive_locations_object ON archive_locations(object_id, locator_revision)",
    "CREATE INDEX idx_archive_checks_content ON archive_checks(content_key, checked_at, check_id)",
    "CREATE INDEX idx_archive_releases_checks ON archive_releases(local_check_id, archive_check_id, observed_at)",
)

_INDEXED_DDL = tuple(
    (re.match(r"\s*CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+(\w+)", sql, re.I).group(1), sql)
    for sql in ARCHIVE_DDL
)


def _normalized_sql_objects(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): " ".join(str(row[1]).split())
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table','index') "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }


def _check_existing_objects(conn: sqlite3.Connection) -> None:
    actual = _normalized_sql_objects(conn)
    for name, statement in _INDEXED_DDL:
        if actual.get(name) != " ".join(statement.split()):
            raise ArchiveSchemaError(f"Missing or incompatible archive object: {name}")


def validate_archive_facts(conn: sqlite3.Connection) -> None:
    """Fail closed on malformed stored facts, not only on syntactically valid DDL."""
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ArchiveSchemaError("Archive validation requires foreign keys enabled")
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
    content_facts = {}
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
    asset_rows = conn.execute(
        "SELECT asset_id,revision,path_key,path_json,content_key,size_bytes,sha256 FROM archive_assets"
    )
    for asset_id, revision, path_key, path_json, content_key, size, digest in asset_rows:
        try:
            path = PathRef(**json.loads(path_json))
            content = ContentVersion(asset_id, revision, size, digest)
        except (ContractError, TypeError, ValueError) as exc:
            raise ArchiveSchemaError("Invalid archive asset fact") from exc
        if path.target_key != path_key or content_facts.get((asset_id, revision)) != (content_key, content):
            raise ArchiveSchemaError("Archive asset identity does not match its facts")
    local_rows = conn.execute(
        """SELECT l.check_id,l.content_key,l.asset_id,l.revision,l.path_key,l.root_revision,l.run_id,
        l.checked_at,l.state,l.observed_size,l.observed_sha256,l.evidence_json,
        a.path_json,a.size_bytes,a.sha256 FROM archive_local_checks AS l
        JOIN archive_assets AS a ON a.asset_id=l.asset_id AND a.revision=l.revision
            AND a.path_key=l.path_key"""
    )
    for row in local_rows:
        try:
            evidence = _local_from_json(json.loads(row[11]))
            content = ContentVersion(row[2], row[3], row[13], row[14])
            path = PathRef(**json.loads(row[12]))
        except (ContractError, TypeError, ValueError) as exc:
            raise ArchiveSchemaError("Invalid stored local evidence") from exc
        expected = (evidence.check_id, _content_key(content), content.asset_id, content.revision,
                    path.target_key, evidence.root_revision, evidence.run_id, evidence.checked_at,
                    evidence.state.value, evidence.observed_size, evidence.observed_sha256)
        if evidence.content != content or evidence.path != path or row[:11] != expected:
            raise ArchiveSchemaError("Local evidence differs from its indexed facts")
    archive_rows = conn.execute(
        """SELECT check_id,content_key,asset_id,revision,location_id,checked_at,state,
        observed_size,observed_sha256,evidence_json FROM archive_checks"""
    )
    for row in archive_rows:
        try:
            evidence = _archive_from_json(json.loads(row[-1]))
        except (ContractError, TypeError, ValueError) as exc:
            raise ArchiveSchemaError("Invalid stored archive evidence") from exc
        expected = (evidence.check_id, _content_key(evidence.content), evidence.content.asset_id,
                    evidence.content.revision, evidence.location_ref, evidence.checked_at,
                    evidence.state.value, row[7], row[8])
        if row[:-1] != expected:
            raise ArchiveSchemaError("Archive evidence differs from its indexed facts")
    mismatched_members = conn.execute(
        """SELECT COUNT(*) FROM archive_members AS m
        JOIN archive_assets AS a ON a.asset_id=m.asset_id AND a.revision=m.revision
            AND a.path_key=m.path_key
        JOIN archive_packages AS p ON p.package_id=m.package_id
        WHERE m.content_key != a.content_key OR m.size_bytes != a.size_bytes
           OR m.sha256 != a.sha256 OR p.state != 'sealed'"""
    ).fetchone()[0]
    if mismatched_members:
        raise ArchiveSchemaError("Package members must match sealed content facts")
    if conn.execute(
        """SELECT COUNT(*) FROM archive_checks AS c
        JOIN archive_contents AS a ON a.content_key=c.content_key
            AND a.asset_id=c.asset_id AND a.revision=c.revision
        WHERE c.observed_size IS NOT NULL AND (
            c.observed_size != a.size_bytes OR c.observed_sha256 != a.sha256
        )"""
    ).fetchone()[0]:
        raise ArchiveSchemaError("Archive check content differs from its asset")
    if conn.execute(
        """SELECT COUNT(*) FROM archive_checks AS c
        JOIN archive_locations AS l ON l.location_id=c.location_id
        WHERE NOT EXISTS (
            SELECT 1 FROM archive_objects AS o
            JOIN archive_members AS m ON m.package_id=o.object_id
            WHERE o.object_id=l.object_id AND o.object_revision=l.object_revision
              AND o.object_kind='package' AND m.content_key=c.content_key
        )"""
    ).fetchone()[0]:
        raise ArchiveSchemaError("Archive check location does not contain this content")
    if conn.execute(
        """SELECT COUNT(*) FROM archive_objects AS o
        LEFT JOIN archive_packages AS p ON p.package_id=o.object_id
            AND p.package_revision=o.object_revision
        WHERE (o.object_kind='package' AND (
            p.package_id IS NULL OR p.state!='sealed' OR p.size_bytes!=o.size_bytes
            OR p.sha256!=o.sha256
        )) OR (o.object_kind!='package' AND p.package_id IS NOT NULL)"""
    ).fetchone()[0]:
        raise ArchiveSchemaError("Physical object and package facts differ")
    bad_release = conn.execute(
        """SELECT COUNT(*) FROM archive_releases AS r
        JOIN archive_local_checks AS l ON l.check_id=r.local_check_id
        JOIN archive_checks AS a ON a.check_id=r.archive_check_id
        WHERE l.state != 'hash_match'
           OR a.state NOT IN ('user_returned_verified','adapter_readback_verified')"""
    ).fetchone()[0]
    if bad_release:
        raise ArchiveSchemaError("Release facts require matching verified local/archive checks")


def ensure_archive_asset(
    conn: sqlite3.Connection, content: ContentVersion, path: PathRef, *,
    source_kind: str, artifact_id: str | None = None,
) -> str:
    """Insert one immutable content/path mapping; never relabel a historical asset."""
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
            if PathRef(**json.loads(output_ref)).target_key != path.target_key:
                raise ContractError("Download receipt path does not match this asset")
        except (TypeError, ValueError) as exc:
            raise ContractError("Download receipt path is invalid") from exc
        if verification != "structure_checked" or size != content.size_bytes or digest != content.sha256:
            raise ContractError("Download receipt does not prove this exact content")
    elif artifact_id is not None:
        raise ContractError("Only download receipt assets may reference artifacts")
    content_values = (
        content.asset_id, content.revision, content.size_bytes, content.sha256,
    )
    existing = conn.execute(
        "SELECT asset_id,revision,size_bytes,sha256 FROM archive_contents WHERE content_key=?",
        (content_key,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != content_values:
            raise ContractError("Content identity already has different facts")
    else:
        conn.execute(
            "INSERT INTO archive_contents VALUES (?,?,?,?,?)",
            (content_key, *content_values),
        )
    values = (
        content_key, *content_values[:2], path.target_key, _path_json(path),
        artifact_id, source_kind, content.size_bytes, content.sha256,
    )
    existing_asset = conn.execute(
        """SELECT content_key,asset_id,revision,path_key,path_json,artifact_id,
                  source_kind,size_bytes,sha256 FROM archive_assets
           WHERE asset_id=? AND revision=? AND path_key=?""",
        (content.asset_id, content.revision, path.target_key),
    ).fetchone()
    if existing_asset is not None:
        if tuple(existing_asset) != values:
            raise ContractError("Existing asset has different immutable facts")
        return content_key
    conn.execute(
        """INSERT INTO archive_assets (
            content_key,asset_id,revision,path_key,path_json,artifact_id,source_kind,size_bytes,sha256
        ) VALUES (?,?,?,?,?,?,?,?,?)""", values,
    )
    return content_key


def record_transfer_intent(
    conn: sqlite3.Connection, content: ContentVersion, path: PathRef, *,
    intent_id: str, root_revision: int, intent: RemovalIntent,
    source: str, observed_at: str, details: object = None,
) -> None:
    if not isinstance(intent, RemovalIntent) or source not in {
        "user_reported", "program_observed", "excluded_scope"
    }:
        raise ContractError("Invalid transfer intent or provenance")
    if type(root_revision) is not int or root_revision < 0:
        raise ContractError("Invalid root revision")
    _token(intent_id, "intent ID")
    conn.execute(
        """INSERT INTO archive_transfer_intents (
            intent_id,content_key,asset_id,revision,path_key,root_revision,
            intent,source,observed_at,details_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (intent_id, _content_key(content), content.asset_id, content.revision,
         path.target_key, root_revision, intent.value, source, observed_at, _json(details)),
    )


def record_local_observation(conn: sqlite3.Connection, evidence: LocalEvidence) -> None:
    """Persist the exact immutable evidence; I/O completion is not inferred here."""
    if not isinstance(evidence, LocalEvidence):
        raise ContractError("Expected LocalEvidence")
    content, path = evidence.content, evidence.path
    conn.execute(
        """INSERT INTO archive_local_checks (
            check_id,content_key,asset_id,revision,path_key,root_revision,run_id,
            checked_at,state,observed_size,observed_sha256,evidence_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (evidence.check_id, _content_key(content), content.asset_id, content.revision,
         path.target_key, evidence.root_revision, evidence.run_id, evidence.checked_at,
         evidence.state.value, evidence.observed_size, evidence.observed_sha256,
         _json(asdict(evidence))),
    )


def record_archive_observation(
    conn: sqlite3.Connection, evidence: ArchiveEvidence, *,
    observed_size: int | None = None, observed_sha256: str | None = None,
) -> None:
    """Store observations without turning a reported upload into a verified one."""
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
    if evidence.state.value in {"user_returned_verified", "adapter_readback_verified"} and (
        observed_size != content.size_bytes or observed_sha256 != content.sha256
    ):
        raise ContractError("Verified archive observations require exact size and digest")
    linked = conn.execute(
        """SELECT COUNT(*) FROM archive_members AS m
        JOIN archive_objects AS o ON o.object_id=m.package_id
        JOIN archive_locations AS l ON l.object_id=o.object_id
            AND l.object_revision=o.object_revision
        WHERE m.content_key=? AND l.location_id=? AND o.object_kind='package'""",
        (_content_key(content), evidence.location_ref),
    ).fetchone()[0]
    if not linked:
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
    if version == "2":
        _check_existing_objects(conn)
    elif version != "1":
        raise ArchiveSchemaError("Unsupported/future source schema")
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
            with sqlite3.connect(source, isolation_level=None) as conn:
                conn.execute("PRAGMA busy_timeout=1000")
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA foreign_keys=ON")
                source_version = _inspect_source_for_migration(conn)
                backup.parent.mkdir(parents=True, exist_ok=True)
                try:
                    # Reserve a new name atomically; never overwrite an existing backup.
                    with backup.open("xb"):
                        pass
                    with sqlite3.connect(backup) as destination:
                        conn.backup(destination)
                        destination.execute("PRAGMA foreign_keys=ON")
                        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise ArchiveMigrationError("Backup integrity check failed")
                        _inspect_source_for_migration(destination)
                except BaseException:
                    # Keep a failed destination for diagnosis; never overwrite it.
                    raise
                conn.execute("PRAGMA query_only=OFF")
                if source_version == "2":
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
                digest = hashlib.sha256(backup.read_bytes()).hexdigest()
                return ArchiveMigrationReport(
                    "already_current" if source_version == "2" else "migrated",
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
    if version == "2":
        _check_existing_objects(conn)
    elif version != "1":
        raise ArchiveSchemaError("Unsupported/future schema")
    return version
