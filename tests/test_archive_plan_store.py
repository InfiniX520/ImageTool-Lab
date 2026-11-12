"""Offline plan-adapter tests; every DB is an explicit synthetic temporary copy."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from core.archive_inventory import (
    ArchiveEntry,
    ArtworkGroup,
    CapacityPlan,
    CapacityPolicy,
    InventoryPolicy,
    build_archive_inventory,
    plan_archive_capacity,
)
from core.archive_plan_store import (
    ArchivePlanConflictError,
    ArchivePlanStoreError,
    load_archive_plan,
    save_archive_plan,
)
from core.archive_schema import _content_key, migrate_archive_copy, validate_archive_facts
from core.library_repository import LibraryRepository
from core.preservation_policy import ContentVersion
from core.task_contracts import CollectionRequest, PathRef


def make_v1(path: Path) -> tuple:
    with LibraryRepository(path) as repo:
        repo.submit(CollectionRequest("request-1", "synthetic-account"), "submit-1")
    with sqlite3.connect(path) as conn:
        jobs = conn.execute("SELECT * FROM jobs ORDER BY job_id").fetchall()
        artifacts = conn.execute("SELECT * FROM artifacts ORDER BY artifact_id").fetchall()
    return jobs, artifacts


@pytest.fixture
def migrated_copy(tmp_path):
    """Close v1 before migration and provide a caller-owned explicit connection."""
    original = tmp_path / "original-v1.sqlite3"
    copy = tmp_path / "copy-to-migrate.sqlite3"
    old_rows = make_v1(original)
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    shutil.copy2(original, copy)
    report = migrate_archive_copy(copy, tmp_path / "migration-backup.sqlite3", temporary_copy_confirmed=True)
    assert report.status == "migrated"
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
    conn = sqlite3.connect(copy)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn, old_rows
    finally:
        conn.close()


def make_plan(batch_id: str = "batch-synthetic", *, digest: str | None = "a"):
    content = ContentVersion(
        "synthetic-asset", 3, 12, None if digest is None else digest * 64,
    )
    groups = (ArtworkGroup("synthetic-work", (
        ArchiveEntry(content, PathRef(
            kind="external", root_id="synthetic", relative_path="scope/page.png"
        ), 0),
    )),)
    inventory = build_archive_inventory(
        groups, batch_id=batch_id, policy=InventoryPolicy(target_package_bytes=1024),
    )
    capacity = plan_archive_capacity(inventory, available_additional_bytes=1_000_000)
    assert inventory.scope_complete and inventory.packages
    return inventory, capacity


def assert_history(conn, old_rows) -> None:
    assert conn.execute("SELECT * FROM jobs ORDER BY job_id").fetchall() == old_rows[0]
    assert conn.execute("SELECT * FROM artifacts ORDER BY artifact_id").fetchall() == old_rows[1]


def test_v1_migration_save_reload_and_exact_replay_preserve_history(migrated_copy):
    conn, old_rows = migrated_copy
    inventory, capacity = make_plan()
    scope = {"kind": "explicit_inventory", "selected_roots": ["synthetic:scope"]}
    before = (conn.execute("SELECT * FROM jobs").fetchall(),
              conn.execute("SELECT * FROM artifacts").fetchall())

    saved = save_archive_plan(conn, inventory, capacity, scope=scope)
    assert not saved.replayed
    assert saved.plan.inventory == inventory
    assert saved.plan.capacity == capacity
    assert saved.plan.scope == scope
    assert all(package.state == "planned" for package in saved.plan.packages)
    assert all(package.size_bytes is None and package.sha256 is None
               and package.tool_version is None and package.key_ref is None
               for package in saved.plan.packages)
    assert conn.execute("SELECT COUNT(*) FROM archive_members").fetchone()[0] == 0
    for table in ("archive_objects", "archive_locations", "archive_checks", "archive_releases"):
        assert conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0

    loaded = load_archive_plan(conn, "batch-synthetic")
    assert loaded == saved.plan
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not conn.in_transaction

    first_row = conn.execute("SELECT * FROM archive_batches").fetchone()
    replay = save_archive_plan(conn, inventory, capacity, scope=scope)
    assert replay.replayed
    assert replay.plan == saved.plan
    assert conn.execute("SELECT * FROM archive_batches").fetchone() == first_row
    for table in ("archive_contents", "archive_assets", "archive_packages", "archive_members"):
        assert conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == (1 if table != "archive_members" else 0)
    assert_history(conn, old_rows)
    assert_history(conn, before)
    validate_archive_facts(conn)


def test_separate_batch_does_not_relabel_existing_path(migrated_copy):
    conn, _ = migrated_copy
    first, first_capacity = make_plan("batch-one")
    save_archive_plan(conn, first, first_capacity)
    other_content = ContentVersion("other-asset", 1, 20, "b" * 64)
    other = build_archive_inventory((ArtworkGroup("other-work", (
        ArchiveEntry(other_content, first.assets[0].path, 0),
    )),), batch_id="batch-two")
    with pytest.raises(ArchivePlanStoreError):
        save_archive_plan(conn, other, plan_archive_capacity(other, available_additional_bytes=1_000_000))
    assert conn.execute("SELECT content_key FROM archive_assets").fetchone()[0] == _content_key(first.assets[0].content)
    assert conn.execute("SELECT COUNT(*) FROM archive_batches").fetchone()[0] == 1
    assert not conn.in_transaction


def test_different_scope_same_batch_is_rejected_atomically(migrated_copy):
    conn, old_rows = migrated_copy
    inventory, capacity = make_plan()
    save_archive_plan(conn, inventory, capacity, scope={"selected": ["one"]})
    before = {table: conn.execute("SELECT * FROM " + table).fetchall()
              for table in ("archive_batches", "archive_contents", "archive_assets", "archive_packages")}
    with pytest.raises(ArchivePlanConflictError):
        save_archive_plan(conn, inventory, capacity, scope={"selected": ["two"]})
    assert_history(conn, old_rows)
    assert {table: conn.execute("SELECT * FROM " + table).fetchall() for table in before} == before
    assert not conn.in_transaction
    different_capacity = plan_archive_capacity(inventory, available_additional_bytes=2_000_000)
    with pytest.raises(ArchivePlanConflictError):
        save_archive_plan(conn, inventory, different_capacity, scope={"selected": ["one"]})
    assert {table: conn.execute("SELECT * FROM " + table).fetchall() for table in before} == before
    assert not conn.in_transaction


def test_package_collision_and_sealed_history_are_not_overwritten(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan()
    package_id = inventory.packages[0].package_id
    historical = (
        package_id, "historical-batch", 1, "7z", "synthetic-tool", "synthetic-key",
        32, "c" * 64, "sealed",
    )
    conn.execute("INSERT INTO archive_batches VALUES (?,?,?,?,?,?,?)", (
        "historical-batch", 1, "{}", "{}", "completed", None, "2026-01-01T00:00:00+00:00",
    ))
    conn.execute("INSERT INTO archive_packages VALUES (?,?,?,?,?,?,?,?,?)", historical)
    conn.commit()
    with pytest.raises(ArchivePlanConflictError):
        save_archive_plan(conn, inventory, capacity)
    assert conn.execute("SELECT * FROM archive_packages WHERE package_id=?", (package_id,)).fetchone() == historical
    assert conn.execute("SELECT COUNT(*) FROM archive_batches").fetchone()[0] == 1
    assert not conn.in_transaction


def test_late_content_conflict_rolls_back_entire_new_batch(migrated_copy):
    conn, old_rows = migrated_copy
    inventory, capacity = make_plan()
    path = inventory.assets[0].path
    # A valid existing immutable row collides only after the new batch was inserted.
    original_content = ContentVersion("old-asset", 1, 8, "d" * 64)
    from core.archive_schema import ensure_archive_asset
    ensure_archive_asset(conn, original_content, path, source_kind="observed_existing")
    conn.commit()
    before_assets = conn.execute("SELECT * FROM archive_assets").fetchall()
    with pytest.raises(ArchivePlanStoreError):
        save_archive_plan(conn, inventory, capacity)
    assert conn.execute("SELECT COUNT(*) FROM archive_batches").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM archive_contents").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM archive_packages").fetchone()[0] == 0
    assert conn.execute("SELECT * FROM archive_assets").fetchall() == before_assets
    assert_history(conn, old_rows)
    assert not conn.in_transaction


def test_schema_and_version_gates_reject_wrong_connections(tmp_path, migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan()
    for bad in (None, str(tmp_path / "never-open.sqlite3"), Path("never-open.sqlite3")):
        with pytest.raises(ArchivePlanStoreError, match="explicit sqlite3"):
            save_archive_plan(bad, inventory, capacity)
        with pytest.raises(ArchivePlanStoreError, match="explicit sqlite3"):
            load_archive_plan(bad, "batch-synthetic")
    assert not (tmp_path / "never-open.sqlite3").exists()
    conn.execute("UPDATE schema_meta SET value='3' WHERE key='schema_version'")
    conn.commit()
    with pytest.raises(ArchivePlanStoreError, match="schema 2|schema version"):
        save_archive_plan(conn, inventory, capacity)
    with pytest.raises(ArchivePlanStoreError, match="schema 2|schema version"):
        load_archive_plan(conn, "batch-synthetic")
    assert not conn.in_transaction
    conn.execute("UPDATE schema_meta SET value='2' WHERE key='schema_version'")
    conn.commit()

    save_archive_plan(conn, inventory, capacity)
    stored = json.loads(conn.execute("SELECT scope_json FROM archive_batches").fetchone()[0])
    stored["inventory"]["schema_version"] = 2
    conn.execute("UPDATE archive_batches SET scope_json=? WHERE batch_id=?", (
        json.dumps(stored), inventory.batch_id,
    ))
    conn.commit()
    with pytest.raises(ArchivePlanStoreError, match="inventory schema"):
        save_archive_plan(conn, inventory, capacity)
    with pytest.raises(ArchivePlanStoreError, match="inventory schema"):
        load_archive_plan(conn, inventory.batch_id)
    assert not conn.in_transaction


def test_missing_batch_malformed_json_and_inconsistent_relations_fail_closed(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan()
    save_archive_plan(conn, inventory, capacity)
    with pytest.raises(ArchivePlanStoreError, match="not found"):
        load_archive_plan(conn, "missing-batch")
    assert not conn.in_transaction

    for statement, match in (
        ("UPDATE archive_batches SET capacity_json='{'", "capacity_json|strict"),
        ("UPDATE archive_batches SET capacity_json='[]'", "capacity_json|strict"),
        ("UPDATE archive_batches SET scope_json='{\"a\":1,\"a\":2}'", "scope_json"),
        ("UPDATE archive_packages SET tool_version='fabricated-tool'", "Package index"),
        ("UPDATE archive_assets SET source_kind='observed_existing'", "Content/path"),
        ("UPDATE archive_batches SET capacity_json='{\"batch_id\":1}'", "capacity|strict"),
    ):
        with conn:
            conn.execute(statement)
        with pytest.raises(ArchivePlanStoreError, match=match):
            load_archive_plan(conn, inventory.batch_id)
        assert not conn.in_transaction
        with conn:
            conn.execute("DELETE FROM archive_packages")
            conn.execute("DELETE FROM archive_assets")
            conn.execute("DELETE FROM archive_contents")
            conn.execute("DELETE FROM archive_batches")
        save_archive_plan(conn, inventory, capacity)


def test_readonly_reload_does_not_leave_a_deferred_transaction(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan()
    save_archive_plan(conn, inventory, capacity)
    conn.isolation_level = "IMMEDIATE"
    assert load_archive_plan(conn, inventory.batch_id) == load_archive_plan(conn, inventory.batch_id)
    assert conn.isolation_level == "IMMEDIATE"
    assert not conn.in_transaction
    conn.row_factory = sqlite3.Row
    assert load_archive_plan(conn, inventory.batch_id).inventory == inventory
    assert not conn.in_transaction


def test_readonly_reload_and_foreign_key_restoration(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan()
    save_archive_plan(conn, inventory, capacity)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA query_only=ON")
    try:
        assert load_archive_plan(conn, inventory.batch_id).inventory == inventory
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert not conn.in_transaction
    finally:
        conn.execute("PRAGMA query_only=OFF")
        conn.execute("PRAGMA foreign_keys=ON")


def test_caller_transaction_is_not_committed_or_rolled_back(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan()
    conn.execute("BEGIN")
    with pytest.raises(ArchivePlanStoreError, match="transaction"):
        save_archive_plan(conn, inventory, capacity)
    assert conn.in_transaction
    with pytest.raises(ArchivePlanStoreError, match="transaction"):
        load_archive_plan(conn, inventory.batch_id)
    assert conn.in_transaction
    conn.rollback()


def test_unknown_capacity_schema_fails_before_any_write(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan()
    bad = replace(capacity, schema_version=999)
    with pytest.raises(ArchivePlanStoreError):
        save_archive_plan(conn, inventory, bad)
    assert conn.execute("SELECT COUNT(*) FROM archive_batches").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM archive_assets").fetchone()[0] == 0


def test_incomplete_scope_cannot_have_partial_package_or_capacity(migrated_copy):
    conn, _ = migrated_copy
    groups = (
        ArtworkGroup("good", (ArchiveEntry(
            ContentVersion("good", 1, 4, "a" * 64), "good.png", 0
        ),)),
        ArtworkGroup("bad", (ArchiveEntry(None, "../bad.png", 0),)),
    )
    inventory = build_archive_inventory(groups, batch_id="incomplete")
    assert not inventory.scope_complete and not inventory.packages and inventory.assets
    capacity = plan_archive_capacity(inventory, available_additional_bytes=1024)
    saved = save_archive_plan(conn, inventory, capacity, scope={"kind": "explicit_incomplete"})
    assert not saved.plan.packages
    assert saved.plan.capacity.peak_additional_bytes is None
    assert load_archive_plan(conn, "incomplete").inventory == inventory


def test_multiple_packages_and_duplicates_round_trip(migrated_copy):
    conn, old_rows = migrated_copy
    content = ContentVersion("synthetic-multi", 1, 8, "e" * 64)
    entries = tuple(
        ArchiveEntry(content, PathRef(
            kind="external", root_id="synthetic", relative_path="scope/" + str(index) + ".png",
        ), index)
        for index in range(4)
    ) + (ArchiveEntry(content, PathRef(
        kind="external", root_id="synthetic", relative_path="scope/0.png",
    ), 0),)
    inventory = build_archive_inventory(
        (ArtworkGroup("multi", entries),), batch_id="multi-package",
        policy=InventoryPolicy(target_package_bytes=16),
    )
    capacity = plan_archive_capacity(
        inventory, available_additional_bytes=1_000_000,
        policy=CapacityPolicy(staging_mode="single_package", control_bytes=64, safety_margin_bytes=128),
    )
    assert len(inventory.packages) > 1 and inventory.duplicate_entry_count == 1
    saved = save_archive_plan(conn, inventory, capacity)
    assert saved.plan.inventory == inventory
    assert len(saved.plan.packages) == len(inventory.packages)
    assert conn.execute("SELECT COUNT(*) FROM archive_assets").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM archive_packages").fetchone()[0] > 1
    assert load_archive_plan(conn, "multi-package") == saved.plan
    assert_history(conn, old_rows)


def test_path_fact_tamper_is_rejected_without_disabling_foreign_keys(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan()
    save_archive_plan(conn, inventory, capacity)
    path = inventory.assets[0].path
    # Preserve path semantics and FK validity, but change the persisted path fact's
    # representation; the adapter must not silently equate it with the frozen plan.
    reordered_path = json.dumps({
        "relative_path": path.relative_path, "root_id": path.root_id, "kind": path.kind,
    }, separators=(", ", ": "))
    with conn:
        conn.execute("UPDATE archive_assets SET path_json=? WHERE path_key=?", (
            reordered_path, inventory.assets[0].path_key,
        ))
    with pytest.raises(ArchivePlanStoreError, match="Content/path"):
        load_archive_plan(conn, inventory.batch_id)
    assert not conn.in_transaction


def test_save_validation_failure_rolls_back_inserted_batch(migrated_copy):
    conn, old_rows = migrated_copy
    inventory, capacity = make_plan()
    # A concurrent/old incompatible batch is allowed by the base archive schema,
    # but this adapter cannot silently accept malformed plan JSON for a new save.
    conn.execute("INSERT INTO archive_batches VALUES (?,?,?,?,?,?,?)", (
        "old-batch", 1, "{}", "{", "paused", "synthetic pause", "2026-01-01T00:00:00+00:00",
    ))
    conn.commit()
    with pytest.raises(ArchivePlanStoreError, match="schema validation"):
        save_archive_plan(conn, inventory, capacity)
    assert conn.execute("SELECT COUNT(*) FROM archive_batches WHERE batch_id=?", (
        inventory.batch_id,
    )).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM archive_assets").fetchone()[0] == 0
    assert_history(conn, old_rows)
    assert not conn.in_transaction


def test_cross_batch_inventory_cannot_be_relabelled(migrated_copy):
    conn, _ = migrated_copy
    first, first_capacity = make_plan("batch-original")
    save_archive_plan(conn, first, first_capacity)
    different = replace(first, batch_id="batch-different")
    with pytest.raises(ArchivePlanStoreError):
        save_archive_plan(conn, different, first_capacity)
    assert load_archive_plan(conn, "batch-original").inventory == first
    with pytest.raises(ArchivePlanStoreError, match="not found"):
        load_archive_plan(conn, "batch-different")
