"""The opt-in schema-3 archive repository uses the inherited writer safely."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.archive_inventory import (
    ArchiveEntry, ArtworkGroup, InventoryPolicy, build_archive_inventory,
    plan_archive_capacity,
)
from core.archive_plan_store import ArchivePlanConflictError, ArchivePlanStoreError
from core.archive_scope import (
    SCOPE_RECEIPT_SCHEMA_VERSION, ScopeReceipt, ScopeSelectionKind, ScopeState,
    _digest_text, _entry_fact, _facts_digest, _path_key,
)
from core.local_observation import FileIdentity
from core.archive_repository import ArchiveLibraryRepository
from core.archive_schema import ArchiveMigrationError, migrate_archive_copy
from core.library_repository import (
    LibraryClosedError, LibraryRepository, SchemaVersionError,
)
from core.preservation_policy import ContentVersion
from core.task_contracts import CollectionRequest, PathRef


def migrated(path: Path):
    with LibraryRepository(path) as repository:
        job = repository.submit(CollectionRequest("request", "synthetic"), "submit")
    migrate_archive_copy(path, path.parent / "before-migration.sqlite3", temporary_copy_confirmed=True)
    return job


def make_plan(batch_id="archive-batch"):
    content = ContentVersion("asset-1", 1, 8, "a" * 64)
    path = PathRef(kind="external", root_id="synthetic", relative_path="library/page.png")
    inventory = build_archive_inventory(
        [ArtworkGroup("work-1", (ArchiveEntry(content, path, 0),))],
        batch_id=batch_id, policy=InventoryPolicy(target_package_bytes=128),
    )
    return inventory, plan_archive_capacity(inventory, available_additional_bytes=4096)


def make_receipt(inventory):
    entries = tuple(sorted(
        (ArchiveEntry(asset.content, asset.path, asset.page_index, asset.physical_identity)
         for asset in inventory.assets),
        key=lambda entry: (_path_key(entry.path.relative_path), entry.path.relative_path),
    ))
    facts = tuple(_entry_fact(entry, root_revision=1) for entry in entries)
    count = len(entries)
    return ScopeReceipt(
        schema_version=SCOPE_RECEIPT_SCHEMA_VERSION,
        kind="external", root_id="synthetic", root_revision=1,
        rules_digest="0" * 64, selection_digest="0" * 64,
        selection_kind=ScopeSelectionKind.SNAPSHOT,
        sub_batch_limit=None, sub_batch_description=None,
        snapshot_id="snapshot-1", snapshot_digest="0" * 64,
        root_identity=FileIdentity(1, 1),
        entries_in_snapshot=count, exclusions_in_snapshot=0, issues_in_snapshot=0,
        enumeration_state=ScopeState.COMPLETE, page_count=1,
        first_page_index=0, last_page_index=0, expected_page_count=1,
        last_page_has_next=False, consumed_entries=count,
        consumed_exclusions=0, consumed_issues=0, remaining_entries=0,
        aborted=False, entries_digest=_facts_digest(facts), entry_facts=facts,
        exclusions_digest=_digest_text([]), snapshot_verified=True,
    )


def open_repository(path: Path):
    return ArchiveLibraryRepository(path, temporary_copy_confirmed=True)


def test_explicit_temporary_copy_gate_and_version_rejection(tmp_path):
    with pytest.raises(ArchiveMigrationError):
        ArchiveLibraryRepository(tmp_path / "missing.sqlite3")
    with pytest.raises(SchemaVersionError):
        open_repository(tmp_path / "missing.sqlite3")
    path = tmp_path / "v1.sqlite3"
    with LibraryRepository(path):
            pass
    with pytest.raises(SchemaVersionError):
        open_repository(path)
    path.rename(tmp_path / "renamed.sqlite3")
    path = tmp_path / "renamed.sqlite3"
    migrated(path)
    with pytest.raises(SchemaVersionError):
        LibraryRepository(path)
    with open_repository(path) as repository:
        assert repository.database_path == path
    inventory, capacity = make_plan()
    with pytest.raises(LibraryClosedError):
        repository.save_archive_plan(
            inventory, capacity, receipt=make_receipt(inventory),
        )


def test_inherited_single_writer_saves_reloads_and_preserves_history(tmp_path):
    path = tmp_path / "library.sqlite3"
    job = migrated(path)
    with open_repository(path) as repository:
        inventory, capacity = make_plan()
        with pytest.raises(ArchivePlanStoreError, match="ScopeReceipt|receipt"):
            repository.save_archive_plan(inventory, capacity)
        scope = {"kind": "synthetic_explicit"}
        saved = repository.save_archive_plan(
            inventory, capacity, scope=scope, receipt=make_receipt(inventory),
        )
        assert not saved.replayed and saved.plan.inventory == inventory
        assert all(package.state == "planned" for package in saved.plan.packages)
        replay = repository.save_archive_plan(
            inventory, capacity, scope=scope, receipt=make_receipt(inventory),
        )
        assert replay.replayed and replay.plan == saved.plan
        assert repository.load_archive_plan(inventory.batch_id) == saved.plan
        assert repository.get_request(job["job_id"]).request_id == "request"
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM archive_members").fetchone()[0] == 0
            for table in ("archive_objects", "archive_locations", "archive_checks", "archive_releases"):
                assert conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
    with open_repository(path) as repository:
        assert repository.load_archive_plan(inventory.batch_id).inventory == inventory
        with pytest.raises(ArchivePlanConflictError):
            repository.save_archive_plan(
                inventory, capacity, scope={"kind": "different"},
                receipt=make_receipt(inventory),
            )


def test_repository_root_revision_gate_is_wired(tmp_path):
    path = tmp_path / "library.sqlite3"
    migrated(path)
    with open_repository(path) as repository:
        inventory, capacity = make_plan()
        receipt = make_receipt(inventory)
        with pytest.raises(ArchivePlanStoreError, match="root mapping revision|root revision"):
            repository.save_archive_plan(
                inventory, capacity, receipt=receipt, root_revision=2,
            )
        saved = repository.save_archive_plan(
            inventory, capacity, receipt=receipt, root_revision=1,
        )
        assert repository.load_archive_plan(inventory.batch_id) == saved.plan


def test_concurrent_first_save_is_serialized_and_different_plan_cannot_replay(tmp_path):
    path = tmp_path / "library.sqlite3"
    migrated(path)
    inventory, capacity = make_plan()
    with open_repository(path) as repository:
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(
                lambda _: repository.save_archive_plan(inventory, capacity, receipt=make_receipt(inventory)), range(3),
            ))
        assert [item.replayed for item in results].count(False) == 1
        assert results[0].plan == results[1].plan == results[2].plan
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM archive_batches").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM archive_packages").fetchone()[0] == 1
        with pytest.raises(ArchivePlanConflictError):
            repository.save_archive_plan(
                inventory, capacity, scope={"changed": True},
                receipt=make_receipt(inventory),
            )
        other, other_capacity = make_plan("another-batch")
        assert repository.save_archive_plan(
            other, other_capacity, receipt=make_receipt(other),
        ).plan.inventory == other


def test_plan_precheck_conflict_does_not_overwrite_existing_batch(tmp_path):
    path = tmp_path / "library.sqlite3"
    migrated(path)
    inventory, capacity = make_plan()
    with open_repository(path) as repository:
        repository.save_archive_plan(inventory, capacity, receipt=make_receipt(inventory))
        with sqlite3.connect(path) as conn:
            before = conn.execute("SELECT * FROM archive_batches").fetchall()
        with pytest.raises(ArchivePlanConflictError):
            repository.save_archive_plan(
                inventory, capacity, scope={"changed": True},
                receipt=make_receipt(inventory),
            )
        assert repository.load_archive_plan(inventory.batch_id).scope == {"kind": "explicit_inventory"}
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT * FROM archive_batches").fetchall() == before


def test_corrupt_archive_facts_are_rejected_without_leaving_read_transaction(tmp_path):
    path = tmp_path / "library.sqlite3"
    migrated(path)
    inventory, capacity = make_plan()
    with open_repository(path) as repository:
        repository.save_archive_plan(inventory, capacity, receipt=make_receipt(inventory))
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE archive_batches SET capacity_json='{'")
        with pytest.raises(ArchivePlanStoreError):
            repository.load_archive_plan(inventory.batch_id)
        with pytest.raises(ArchivePlanStoreError):
            repository.save_archive_plan(inventory, capacity, receipt=make_receipt(inventory))
        with sqlite3.connect(path) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_inherited_v1_job_commands_remain_available_on_archive_copy(tmp_path):
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repository:
        repository.submit(CollectionRequest("request", "synthetic"), "submit")
    migrate_archive_copy(path, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    with open_repository(path) as repository:
        second = repository.submit(CollectionRequest("request-2", "synthetic"), "submit-2")
        assert second["job_id"]
        repository.activate_next()
        repository.request_stop(second["job_id"], "stop-2")
        # The second job is still queued, so request_stop pauses it immediately;
        # settle_stop is reserved for a job that actually entered running/settling.
        assert repository.get_batch(second["batch_id"])["phase"] == "paused"
