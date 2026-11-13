"""R4: schema 5 gates the two covering indexes the hot queries need.

Offline and synthetic.  The audited gap behind this module is
docs/audits/2026-11-13-a1-acceptance/FINDINGS.md AR4-07: the digest lookup scanned
``archive_contents`` with a temporary sort tree and the path-owner lookup scanned
``archive_assets``, so both grew with the whole history.  Schema 5 adds the two
covering indexes and refuses the schema 4 draft instead of relabelling it.

The version gate is the point: one schema number describes exactly one object set,
so a v4 copy cannot be opened as if it were v5, and a v5 copy that lost an index is
refused by the object check rather than silently degrading to a scan.
"""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from core.archive_inventory import (
    ArchiveEntry, ArtworkGroup, CapacityPolicy, InventoryPolicy,
    build_archive_inventory, plan_archive_capacity,
)
from core.archive_plan_store import (
    ArchivePlanStoreError, load_archive_plan, save_archive_plan,
)
from core.archive_repository import ArchiveLibraryRepository
from core.archive_schema import (
    ARCHIVE_DDL, ARCHIVE_SCHEMA_VERSION, ArchiveMigrationError, _check_existing_objects,
    find_known_content, migrate_archive_copy, validate_archive_facts,
)
from core.archive_scope import (
    SCOPE_RECEIPT_SCHEMA_VERSION, ScopeReceipt, ScopeSelectionKind, ScopeState,
    _digest_text, _entry_fact, _facts_digest, _path_key,
)
from core.archive_schema import ArchiveSchemaError
from core.library_repository import LibraryRepository, SchemaVersionError
from core.local_observation import FileIdentity
from core.preservation_policy import ContentVersion
from core.task_contracts import CollectionRequest, PathRef

DIGEST_INDEX = "idx_archive_contents_digest"
PATH_INDEX = "idx_archive_assets_path"
DIGEST_SQL = (
    "CREATE INDEX idx_archive_contents_digest ON archive_contents("
    "sha256, size_bytes, asset_id, revision)"
)
PATH_SQL = "CREATE INDEX idx_archive_assets_path ON archive_assets(path_key, asset_id)"


def make_v1(path: Path) -> None:
    with LibraryRepository(path) as repository:
        repository.submit(
            CollectionRequest("request-1", "synthetic-account"), "submit-1")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def migrated(tmp_path: Path, name: str = "library"):
    source = tmp_path / (name + ".sqlite3")
    make_v1(source)
    report = migrate_archive_copy(
        source, tmp_path / (name + "-before.sqlite3"), temporary_copy_confirmed=True)
    return source, report


def schema_version(path_or_conn) -> str:
    if isinstance(path_or_conn, sqlite3.Connection):
        return path_or_conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
    with closing(sqlite3.connect(path_or_conn)) as conn:
        return conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]


def set_version(conn, version: str) -> None:
    conn.execute("UPDATE schema_meta SET value=? WHERE key='schema_version'", (version,))
    conn.commit()


def index_sql(conn) -> dict[str, str]:
    return {
        row[0]: " ".join(row[1].split())
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
    }


def objects(conn) -> list[tuple[str, str, str]]:
    return conn.execute(
        "SELECT type, name, COALESCE(sql,'') FROM sqlite_master ORDER BY type, name"
    ).fetchall()


def make_receipt(inventory):
    entries = tuple(sorted(
        (ArchiveEntry(asset.content, asset.path, asset.page_index, asset.physical_identity)
         for asset in inventory.assets),
        key=lambda entry: (_path_key(entry.path.relative_path), entry.path.relative_path),
    ))
    facts = tuple(_entry_fact(entry, root_revision=1) for entry in entries)
    count = len(entries)
    first = inventory.assets[0].path
    return ScopeReceipt(
        schema_version=SCOPE_RECEIPT_SCHEMA_VERSION,
        kind=first.kind, root_id=first.root_id, root_revision=1,
        rules_digest="0" * 64, selection_digest="0" * 64,
        selection_kind=ScopeSelectionKind.SNAPSHOT,
        sub_batch_limit=None, sub_batch_description=None,
        snapshot_id="snapshot-1", snapshot_digest="0" * 64,
        root_identity=FileIdentity(1, 1),
        entries_in_snapshot=count, exclusions_in_snapshot=0, issues_in_snapshot=0,
        enumeration_state=ScopeState.COMPLETE,
        page_count=1,
        first_page_index=0, last_page_index=0, expected_page_count=1,
        last_page_has_next=False, consumed_entries=count,
        consumed_exclusions=0, consumed_issues=0, remaining_entries=0,
        aborted=False, entries_digest=_facts_digest(facts), entry_facts=facts,
        exclusions_digest=_digest_text([]), snapshot_verified=True,
    )


def make_plan(conn, batch_id: str):
    content = ContentVersion("asset-r4", 1, 4, hashlib.sha256(b"data").hexdigest())
    path = PathRef("project", "project", "gallery/r4.bin")
    inventory = build_archive_inventory(
        (ArtworkGroup("work-1", (
            ArchiveEntry(content, path, 0, None),
        )),),
        batch_id=batch_id,
        policy=InventoryPolicy(target_package_bytes=1024, batch_budget_bytes=10 ** 9),
    )
    capacity = plan_archive_capacity(
        inventory, available_additional_bytes=10 ** 9,
        policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
    )
    return inventory, capacity


def test_schema_five_migrates_from_v1_with_both_covering_indexes(tmp_path):
    source, report = migrated(tmp_path)
    assert report.status == "migrated"
    assert report.source_version == "1"
    assert report.target_version == ARCHIVE_SCHEMA_VERSION == "5"
    assert schema_version(report.backup_path) == "1"

    with closing(sqlite3.connect(source)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert schema_version(conn) == "5"
        sql = index_sql(conn)
        assert " ".join(sql[DIGEST_INDEX].split()) == DIGEST_SQL
        assert " ".join(sql[PATH_INDEX].split()) == PATH_SQL
        assert DIGEST_SQL in ARCHIVE_DDL and PATH_SQL in ARCHIVE_DDL
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        validate_archive_facts(conn)

    # The unchanged production repository still refuses this schema, while the
    # explicit archive repository accepts it.
    with pytest.raises(SchemaVersionError):
        LibraryRepository(source)
    with ArchiveLibraryRepository(source, temporary_copy_confirmed=True):
        pass


def test_a_second_migration_is_already_current_and_writes_nothing(tmp_path):
    source, _ = migrated(tmp_path)
    with closing(sqlite3.connect(source)) as conn:
        before_master = objects(conn)
        before_hash = file_hash(source)
    second = migrate_archive_copy(
        source, tmp_path / "second.backup", temporary_copy_confirmed=True)
    assert second.status == "already_current"
    assert second.source_version == "5"
    assert second.target_version == "5"
    assert second.backup_path.is_file()
    with closing(sqlite3.connect(source)) as conn:
        assert objects(conn) == before_master
    assert file_hash(source) == before_hash


@pytest.mark.parametrize("dropped", ((), (DIGEST_INDEX,), (DIGEST_INDEX, PATH_INDEX)),
                         ids=("none", "digest", "both"))
def test_a_schema_four_draft_copy_is_refused_by_every_entry(tmp_path, dropped):
    source, _ = migrated(tmp_path)
    with closing(sqlite3.connect(source)) as conn:
        for name in dropped:
            conn.execute("DROP INDEX " + name)
        set_version(conn, "4")
        conn.commit()
    before = file_hash(source)
    backup = tmp_path / "refused.backup"

    with pytest.raises(ArchiveMigrationError) as caught:
        migrate_archive_copy(source, backup, temporary_copy_confirmed=True)
    message = str(caught.value)
    assert "schema 4 acceptance-event draft" in message
    assert "schema 1" in message and "schema 5" in message
    assert file_hash(source) == before
    assert not backup.exists()

    with pytest.raises(SchemaVersionError):
        ArchiveLibraryRepository(source, temporary_copy_confirmed=True)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        inventory, capacity = make_plan(conn, "batch-draft-four")
        with pytest.raises(ArchivePlanStoreError, match="schema version"):
            save_archive_plan(conn, inventory, capacity, receipt=make_receipt(inventory))
        with pytest.raises(ArchivePlanStoreError, match="schema version"):
            load_archive_plan(conn, "batch-draft-four")
    assert file_hash(source) == before


def test_a_schema_five_copy_that_lost_an_index_is_refused(tmp_path):
    source, _ = migrated(tmp_path)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("DROP INDEX " + PATH_INDEX)
        conn.commit()
    before = file_hash(source)
    # A wrong object set is refused by the object check, not relabelled as a version.
    with pytest.raises(ArchiveSchemaError, match=PATH_INDEX):
        ArchiveLibraryRepository(source, temporary_copy_confirmed=True)
    with closing(sqlite3.connect(source)) as conn:
        with pytest.raises(Exception, match=PATH_INDEX):
            _check_existing_objects(conn)
    assert file_hash(source) == before


def test_the_index_prefix_is_pinned_and_a_same_name_index_is_not_accepted(tmp_path):
    source, _ = migrated(tmp_path)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("DROP INDEX " + DIGEST_INDEX)
        conn.execute(
            "CREATE INDEX idx_archive_contents_digest ON archive_contents(sha256, size_bytes)")
        conn.commit()
    with pytest.raises(ArchiveSchemaError, match=DIGEST_INDEX):
        ArchiveLibraryRepository(source, temporary_copy_confirmed=True)


@pytest.mark.parametrize("version", ("6", "3", "2", "unknown"))
def test_every_other_version_is_refused_without_writing(tmp_path, version):
    source, _ = migrated(tmp_path)
    with closing(sqlite3.connect(source)) as conn:
        set_version(conn, version)
    before = file_hash(source)
    backup = tmp_path / ("other-" + version + ".backup")
    with pytest.raises(ArchiveMigrationError):
        migrate_archive_copy(source, backup, temporary_copy_confirmed=True)
    assert file_hash(source) == before
    assert not backup.exists()
    with pytest.raises(SchemaVersionError):
        ArchiveLibraryRepository(source, temporary_copy_confirmed=True)


def test_saved_plans_survive_the_upgrade_and_analyze(tmp_path):
    source, _ = migrated(tmp_path)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        inventory, capacity = make_plan(conn, "batch-schema-five-plan")
        save_archive_plan(conn, inventory, capacity, receipt=make_receipt(inventory))
        stored = load_archive_plan(conn, "batch-schema-five-plan")
        assert stored.inventory == inventory
        conn.execute("ANALYZE")
        conn.commit()
        again = load_archive_plan(conn, "batch-schema-five-plan")
        assert again.inventory == stored.inventory
        assert again.capacity == stored.capacity
        assert again.scope == stored.scope
        validate_archive_facts(conn)


def test_hot_queries_use_the_covering_indexes_without_scans(tmp_path):
    source, _ = migrated(tmp_path)
    digest = hashlib.sha256(b"data").hexdigest()
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        inventory, capacity = make_plan(conn, "batch-explain")
        save_archive_plan(conn, inventory, capacity, receipt=make_receipt(inventory))
        detail = " | ".join(
            str(row[3]) for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT asset_id,revision,size_bytes,sha256 "
                "FROM archive_contents WHERE sha256=? AND size_bytes=? "
                "ORDER BY asset_id,revision LIMIT 1", (digest, 4)))
        assert "SCAN" not in detail, detail
        assert "TEMP B-TREE" not in detail, detail
        assert DIGEST_INDEX in detail, detail
        assert "COVERING INDEX" in detail, detail

        owner = " | ".join(
            str(row[3]) for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT DISTINCT asset_id FROM archive_assets "
                "WHERE path_key=?", (inventory.assets[0].path_key,)))
        assert "SCAN" not in owner, owner
        assert "TEMP B-TREE" not in owner, owner
        assert PATH_INDEX in owner, owner

        # The real helper returns the stored generation without scanning.
        assert find_known_content(conn, digest, 4) == inventory.assets[0].content
