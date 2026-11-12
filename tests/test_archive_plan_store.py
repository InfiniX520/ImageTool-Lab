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
    InventoryIssue,
    InventoryIssueCode,
    InventoryPolicy,
    InventoryStatus,
    build_archive_inventory,
    plan_archive_capacity,
)
from core.archive_plan_store import save_archive_plan as _save_archive_plan
from core.archive_plan_store import (
    ARCHIVE_PLAN_MAX_BYTES,
    ArchivePlanConflictError,
    ArchivePlanEntryLimitError,
    ArchivePlanStoreError,
    ArchivePlanTooLargeError,
    load_archive_plan,
    prepare_archive_plan,
    save_archive_plan,
)
from core.archive_scope import (
    SCOPE_RECEIPT_SCHEMA_VERSION, ScopeReceipt, ScopeSelectionKind, ScopeState,
    _digest_text, _entry_fact, _facts_digest, _path_key,
)
from core.local_observation import FileIdentity
from core.archive_schema import (
    ArchiveSchemaError, _content_key, ensure_archive_asset, migrate_archive_copy,
    validate_archive_facts,
)
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


def make_receipt(inventory):
    entries = tuple(sorted(
        (ArchiveEntry(asset.content, asset.path, asset.page_index, asset.physical_identity)
         for asset in inventory.assets),
        key=lambda entry: (_path_key(entry.path.relative_path), entry.path.relative_path),
    ))
    facts = tuple(_entry_fact(entry, root_revision=1) for entry in entries)
    count = len(entries)
    first = inventory.assets[0].path if inventory.assets else None
    return ScopeReceipt(
        schema_version=SCOPE_RECEIPT_SCHEMA_VERSION,
        kind=first.kind if first is not None else "external",
        root_id=first.root_id if first is not None else "synthetic",
        root_revision=1,
        rules_digest="0" * 64, selection_digest="0" * 64,
        selection_kind=ScopeSelectionKind.SNAPSHOT,
        sub_batch_limit=None, sub_batch_description=None,
        snapshot_id="snapshot-1", snapshot_digest="0" * 64,
        root_identity=FileIdentity(1, 1),
        entries_in_snapshot=count, exclusions_in_snapshot=0, issues_in_snapshot=0,
        enumeration_state=(ScopeState.COMPLETE if inventory.scope_complete
                           else ScopeState.COMPLETE_WITH_ISSUES),
        page_count=1,
        first_page_index=0, last_page_index=0, expected_page_count=1,
        last_page_has_next=False, consumed_entries=count,
        consumed_exclusions=0, consumed_issues=0, remaining_entries=0,
        aborted=False, entries_digest=_facts_digest(facts), entry_facts=facts,
        exclusions_digest=_digest_text([]), snapshot_verified=True,
    )


def save_plan(conn, inventory, capacity, **kwargs):
    kwargs.setdefault("receipt", make_receipt(inventory))
    return _save_archive_plan(conn, inventory, capacity, **kwargs)


def assert_history(conn, old_rows) -> None:
    assert conn.execute("SELECT * FROM jobs ORDER BY job_id").fetchall() == old_rows[0]
    assert conn.execute("SELECT * FROM artifacts ORDER BY artifact_id").fetchall() == old_rows[1]


def test_v1_migration_save_reload_and_exact_replay_preserve_history(migrated_copy):
    conn, old_rows = migrated_copy
    inventory, capacity = make_plan()
    scope = {"kind": "explicit_inventory", "selected_roots": ["synthetic:scope"]}
    before = (conn.execute("SELECT * FROM jobs").fetchall(),
              conn.execute("SELECT * FROM artifacts").fetchall())

    saved = save_plan(conn, inventory, capacity, scope=scope)
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
    replay = save_plan(conn, inventory, capacity, scope=scope)
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
    save_plan(conn, first, first_capacity)
    other_content = ContentVersion("other-asset", 1, 20, "b" * 64)
    other = build_archive_inventory((ArtworkGroup("other-work", (
        ArchiveEntry(other_content, first.assets[0].path, 0),
    )),), batch_id="batch-two")
    with pytest.raises(ArchivePlanStoreError):
        save_plan(conn, other, plan_archive_capacity(other, available_additional_bytes=1_000_000))
    assert conn.execute("SELECT content_key FROM archive_assets").fetchone()[0] == _content_key(first.assets[0].content)
    assert conn.execute("SELECT COUNT(*) FROM archive_batches").fetchone()[0] == 1
    assert not conn.in_transaction


def test_different_scope_same_batch_is_rejected_atomically(migrated_copy):
    conn, old_rows = migrated_copy
    inventory, capacity = make_plan()
    save_plan(conn, inventory, capacity, scope={"selected": ["one"]})
    before = {table: conn.execute("SELECT * FROM " + table).fetchall()
              for table in ("archive_batches", "archive_contents", "archive_assets", "archive_packages")}
    with pytest.raises(ArchivePlanConflictError):
        save_plan(conn, inventory, capacity, scope={"selected": ["two"]})
    assert_history(conn, old_rows)
    assert {table: conn.execute("SELECT * FROM " + table).fetchall() for table in before} == before
    assert not conn.in_transaction
    different_capacity = plan_archive_capacity(inventory, available_additional_bytes=2_000_000)
    with pytest.raises(ArchivePlanConflictError):
        save_plan(conn, inventory, different_capacity, scope={"selected": ["one"]})
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
        "historical-batch", 2, "{}", "{}", "completed", None, "2026-01-01T00:00:00+00:00",
    ))
    conn.execute("INSERT INTO archive_packages VALUES (?,?,?,?,?,?,?,?,?)", historical)
    conn.commit()
    with pytest.raises(ArchivePlanConflictError):
        save_plan(conn, inventory, capacity)
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
        save_plan(conn, inventory, capacity)
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
            save_plan(bad, inventory, capacity)
        with pytest.raises(ArchivePlanStoreError, match="explicit sqlite3"):
            load_archive_plan(bad, "batch-synthetic")
    assert not (tmp_path / "never-open.sqlite3").exists()
    conn.execute("UPDATE schema_meta SET value='4' WHERE key='schema_version'")
    conn.commit()
    with pytest.raises(ArchivePlanStoreError, match="schema 3|schema version"):
        save_plan(conn, inventory, capacity)
    with pytest.raises(ArchivePlanStoreError, match="schema 3|schema version"):
        load_archive_plan(conn, "batch-synthetic")
    assert not conn.in_transaction
    conn.execute("UPDATE schema_meta SET value='3' WHERE key='schema_version'")
    conn.commit()

    save_plan(conn, inventory, capacity)
    with conn:
        conn.execute(
            "UPDATE archive_plan_segments SET payload='{}' "
            "WHERE batch_id=? AND segment_index=0",
            (inventory.batch_id,),
        )
    with pytest.raises(ArchivePlanStoreError, match="segment|digest|plan"):
        save_plan(conn, inventory, capacity)
    with pytest.raises(ArchivePlanStoreError, match="segment|digest|plan"):
        load_archive_plan(conn, inventory.batch_id)
    assert not conn.in_transaction


def test_missing_batch_malformed_json_and_inconsistent_relations_fail_closed(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan()
    save_plan(conn, inventory, capacity)
    with pytest.raises(ArchivePlanStoreError, match="not found"):
        load_archive_plan(conn, "missing-batch")
    assert not conn.in_transaction

    for statement, match in (
        ("UPDATE archive_batches SET capacity_json='{'", "capacity_json|strict"),
        ("UPDATE archive_batches SET capacity_json='[]'", "capacity_json|strict"),
        ("UPDATE archive_batches SET scope_json='{\"a\":1,\"a\":2}'", "scope_json"),
        ("UPDATE archive_packages SET tool_version='fabricated-tool'", "Package index"),
        ("UPDATE archive_assets SET size_bytes=999", "Content/path|asset identity"),
        ("UPDATE archive_batches SET capacity_json='{\"batch_id\":1}'", "capacity|strict"),
    ):
        with conn:
            conn.execute(statement)
        with pytest.raises(ArchivePlanStoreError, match=match):
            load_archive_plan(conn, inventory.batch_id)
        assert not conn.in_transaction
        with conn:
            conn.execute("DELETE FROM archive_plan_segments")
            conn.execute("DELETE FROM archive_path_bindings")
            conn.execute("DELETE FROM archive_packages")
            conn.execute("DELETE FROM archive_assets")
            conn.execute("DELETE FROM archive_contents")
            conn.execute("DELETE FROM archive_batches")
        save_plan(conn, inventory, capacity)


def test_readonly_reload_does_not_leave_a_deferred_transaction(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan()
    save_plan(conn, inventory, capacity)
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
    save_plan(conn, inventory, capacity)
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
        save_plan(conn, inventory, capacity)
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
        save_plan(conn, inventory, bad)
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
    saved = save_plan(conn, inventory, capacity, scope={"kind": "explicit_incomplete"})
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
    saved = save_plan(conn, inventory, capacity)
    assert saved.plan.inventory == inventory
    assert len(saved.plan.packages) == len(inventory.packages)
    assert conn.execute("SELECT COUNT(*) FROM archive_assets").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM archive_packages").fetchone()[0] > 1
    assert load_archive_plan(conn, "multi-package") == saved.plan
    assert_history(conn, old_rows)


def test_path_fact_tamper_is_rejected_without_disabling_foreign_keys(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan()
    save_plan(conn, inventory, capacity)
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
    with pytest.raises(ArchivePlanStoreError, match="Content/path|asset identity"):
        load_archive_plan(conn, inventory.batch_id)
    assert not conn.in_transaction


def test_regular_save_ignores_unrelated_corruption_but_full_audit_rejects_it(migrated_copy):
    conn, old_rows = migrated_copy
    inventory, capacity = make_plan()
    # An unrelated malformed historical batch must not make every small plan pay
    # for a full-library audit; the explicit audit entry still fails closed.
    conn.execute("INSERT INTO archive_batches VALUES (?,?,?,?,?,?,?)", (
        "old-batch", 2, "{}", "{", "paused", "synthetic pause", "2026-01-01T00:00:00+00:00",
    ))
    conn.commit()
    with pytest.raises(ArchiveSchemaError, match="capacity_json|Invalid"):
        validate_archive_facts(conn)
    saved = save_plan(conn, inventory, capacity)
    assert saved.plan.inventory == inventory
    assert conn.execute("SELECT COUNT(*) FROM archive_batches WHERE batch_id=?", (
        inventory.batch_id,
    )).fetchone()[0] == 1
    assert_history(conn, old_rows)
    assert not conn.in_transaction


def test_cross_batch_inventory_cannot_be_relabelled(migrated_copy):
    conn, _ = migrated_copy
    first, first_capacity = make_plan("batch-original")
    save_plan(conn, first, first_capacity)
    different = replace(first, batch_id="batch-different")
    with pytest.raises(ArchivePlanStoreError):
        save_plan(conn, different, first_capacity)
    assert load_archive_plan(conn, "batch-original").inventory == first
    with pytest.raises(ArchivePlanStoreError, match="not found"):
        load_archive_plan(conn, "batch-different")


def make_large_plan(count: int):
    entries = []
    for index in range(count):
        digest = hashlib.sha256(("asset-" + str(index)).encode("ascii")).hexdigest()
        content = ContentVersion("asset-" + str(index), 1, 1, digest)
        path = PathRef(
            kind="external", root_id="synthetic",
            relative_path="scope/" + str(index).zfill(5) + ".bin",
        )
        entries.append(ArchiveEntry(content, path, None))
    inventory = build_archive_inventory(
        (ArtworkGroup("large", tuple(entries)),),
        batch_id="large-" + str(count),
        policy=InventoryPolicy(target_package_bytes=10**9, batch_budget_bytes=10**12),
    )
    capacity = plan_archive_capacity(inventory, available_additional_bytes=10**12)
    return inventory, capacity


def test_two_thousand_assets_round_trip_through_bounded_segments(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_large_plan(2000)
    saved = save_plan(conn, inventory, capacity)
    assert saved.plan.inventory == inventory
    header = json.loads(conn.execute(
        "SELECT scope_json FROM archive_batches WHERE batch_id=?", (inventory.batch_id,)
    ).fetchone()[0])
    assert "inventory" not in header
    assert header["segment_count"] > 1
    assert len(json.dumps(header, separators=(",", ":")).encode("utf-8")) < 1024 * 1024
    assert load_archive_plan(conn, inventory.batch_id).inventory == inventory


def test_ten_thousand_assets_is_bounded_or_rejected_before_any_write(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_large_plan(10000)
    try:
        saved = save_plan(conn, inventory, capacity)
    except ArchivePlanTooLargeError as exc:
        assert exc.batch_id == inventory.batch_id
        assert exc.payload_bytes > ARCHIVE_PLAN_MAX_BYTES
        assert conn.execute("SELECT COUNT(*) FROM archive_batches").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM archive_assets").fetchone()[0] == 0
    except ArchivePlanEntryLimitError as exc:
        assert exc.batch_id == inventory.batch_id
        assert exc.entry_count > 9_000
        assert conn.execute("SELECT COUNT(*) FROM archive_batches").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM archive_assets").fetchone()[0] == 0
    else:
        assert saved.plan.inventory == inventory
        assert load_archive_plan(conn, inventory.batch_id).inventory == inventory


def test_cross_batch_reuses_observed_asset_without_relabelling(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan("cross-batch")
    asset = inventory.assets[0]
    ensure_archive_asset(
        conn, asset.content, asset.path, source_kind="observed_existing",
    )
    conn.commit()
    saved = save_plan(conn, inventory, capacity)
    assert saved.plan.inventory == inventory
    assert conn.execute(
        "SELECT source_kind FROM archive_assets WHERE path_key=?", (asset.path_key,)
    ).fetchone()[0] == "observed_existing"
    assert conn.execute("SELECT COUNT(*) FROM archive_contents").fetchone()[0] == 1
    assert load_archive_plan(conn, "cross-batch").inventory == inventory


def test_plan_revision_advances_binding_but_old_plan_still_reloads(migrated_copy):
    conn, _ = migrated_copy
    old, old_capacity = make_plan("generation-one")
    save_plan(conn, old, old_capacity)
    old_asset = old.assets[0]
    newer_content = ContentVersion(
        old_asset.content.asset_id, old_asset.content.revision + 1,
        old_asset.content.size_bytes + 1, "b" * 64,
    )
    newer = build_archive_inventory(
        (ArtworkGroup("synthetic-work", (
            ArchiveEntry(newer_content, old_asset.path, old_asset.page_index),
        )),),
        batch_id="generation-two",
        policy=InventoryPolicy(target_package_bytes=1024),
    )
    newer_capacity = plan_archive_capacity(newer, available_additional_bytes=1_000_000)
    save_plan(conn, newer, newer_capacity)
    assert load_archive_plan(conn, "generation-one").inventory == old
    assert load_archive_plan(conn, "generation-two").inventory == newer
    assert conn.execute(
        "SELECT asset_id,revision FROM archive_path_bindings WHERE path_key=?",
        (old_asset.path_key,),
    ).fetchone() == (newer_content.asset_id, newer_content.revision)
    assert conn.execute("SELECT COUNT(*) FROM archive_assets").fetchone()[0] == 2


def test_ten_thousand_one_entries_rejected_by_explicit_count_budget():
    inventory, capacity = make_large_plan(10001)
    with pytest.raises(ArchivePlanEntryLimitError, match="entry budget"):
        prepare_archive_plan(inventory, capacity)


def test_receipt_root_must_match_every_plan_path():
    inventory, capacity = make_plan("root-mismatch")
    original = make_receipt(inventory)
    wrong_facts = tuple(replace(fact, kind="project", root_id="project")
                        for fact in original.entry_facts)
    wrong = replace(
        original, kind="project", root_id="project", digest=None,
        entry_facts=wrong_facts, entries_digest=_facts_digest(wrong_facts),
    )
    with pytest.raises(ArchivePlanStoreError, match="root"):
        prepare_archive_plan(inventory, capacity, receipt=wrong)


def test_entry_cap_counts_rejected_input_entries():
    inventory, capacity = make_plan("input-cap")
    inflated = replace(inventory, input_entry_count=9_001, assets=(), issues=())
    with pytest.raises(ArchivePlanEntryLimitError, match="entry budget"):
        prepare_archive_plan(inflated, capacity)


def test_hardlink_physical_identity_round_trips_and_capacity_counts_source_once(
    migrated_copy,
):
    conn, _ = migrated_copy
    content = ContentVersion("hardlink-plan", 1, 12, "a" * 64)
    identity = FileIdentity(9, 21)
    inventory = build_archive_inventory((ArtworkGroup("hardlink", (
        ArchiveEntry(content, PathRef(kind="external", root_id="synthetic",
                                     relative_path="scope/a.bin"), 0, identity),
        ArchiveEntry(content, PathRef(kind="external", root_id="synthetic",
                                     relative_path="scope/b.bin"), 0, identity),
    )),), batch_id="hardlink-plan", policy=InventoryPolicy(target_package_bytes=1024))
    capacity = plan_archive_capacity(inventory, available_additional_bytes=1_000_000)
    assert inventory.member_payload_bytes == 24
    assert inventory.physical_member_payload_bytes == 12
    assert capacity.source_member_bytes_excluded == 12
    saved = save_plan(conn, inventory, capacity)
    loaded = load_archive_plan(conn, "hardlink-plan")
    assert loaded.inventory.physical_member_payload_bytes == 12
    assert loaded.capacity.source_member_bytes_excluded == 12
    assert loaded == saved.plan


def test_low_level_save_requires_receipt(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan("requires-receipt")
    with pytest.raises(ArchivePlanStoreError, match="ScopeReceipt"):
        _save_archive_plan(conn, inventory, capacity)
    assert conn.execute("SELECT COUNT(*) FROM archive_batches").fetchone()[0] == 0


def test_prepared_plan_cannot_drop_its_receipt():
    inventory, capacity = make_plan("prepared-receipt")
    prepared = prepare_archive_plan(inventory, capacity, receipt=make_receipt(inventory))
    with pytest.raises(ArchivePlanStoreError, match="receipt"):
        replace(prepared, scope_receipt_json=None)


def test_plan_receipt_is_persisted_and_tampering_rejected(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan("receipt-batch")
    receipt = make_receipt(inventory)
    saved = save_plan(conn, inventory, capacity, receipt=receipt)
    assert saved.plan.scope_receipt == json.loads(receipt.to_json())
    assert load_archive_plan(conn, "receipt-batch") == saved.plan
    header = json.loads(conn.execute(
        "SELECT scope_json FROM archive_batches WHERE batch_id='receipt-batch'"
    ).fetchone()[0])
    assert header["scope_receipt"]["digest"] == receipt.digest

    partial = replace(
        receipt, consumed_entries=0, remaining_entries=len(inventory.assets),
        last_page_has_next=True, digest=None, entry_facts=(),
        entries_digest=_facts_digest(()),
    )
    with pytest.raises(ArchivePlanStoreError, match="complete"):
        prepare_archive_plan(inventory, capacity, receipt=partial)

    tampered = dict(header)
    tampered["scope_receipt"] = dict(header["scope_receipt"])
    tampered["scope_receipt"]["entries_digest"] = "f" * 64
    with conn:
        conn.execute(
            "UPDATE archive_batches SET scope_json=? WHERE batch_id='receipt-batch'",
            (json.dumps(tampered),),
        )
    with pytest.raises(ArchivePlanStoreError, match="receipt|digest"):
        load_archive_plan(conn, "receipt-batch")


def test_persisted_null_receipt_is_rejected_by_load_and_full_audit(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan("null-receipt")
    save_plan(conn, inventory, capacity)
    header = json.loads(conn.execute(
        "SELECT scope_json FROM archive_batches WHERE batch_id='null-receipt'"
    ).fetchone()[0])
    header["scope_receipt"] = None
    with conn:
        conn.execute(
            "UPDATE archive_batches SET scope_json=? WHERE batch_id='null-receipt'",
            (json.dumps(header),),
        )
    with pytest.raises(ArchivePlanStoreError, match="receipt"):
        load_archive_plan(conn, "null-receipt")
    with pytest.raises(ArchiveSchemaError, match="receipt"):
        validate_archive_facts(conn)


def test_referenced_content_digest_is_checked_on_load_and_replay(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan("content-corruption")
    save_plan(conn, inventory, capacity)
    with conn:
        conn.execute(
            "UPDATE archive_contents SET sha256=NULL WHERE asset_id='synthetic-asset'"
        )
    with pytest.raises(ArchivePlanStoreError, match="content/path|Referenced"):
        load_archive_plan(conn, "content-corruption")
    with pytest.raises(ArchivePlanStoreError, match="content/path|Referenced"):
        save_plan(conn, inventory, capacity)


def test_reload_rejects_duplicate_inventory_json_key(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan("duplicate-key")
    save_plan(conn, inventory, capacity)
    payload = conn.execute(
        "SELECT payload FROM archive_plan_segments "
        "WHERE batch_id='duplicate-key' AND segment_index=0"
    ).fetchone()[0]
    assert payload.endswith("}")
    changed = payload[:-1] + ',"batch_id":"duplicate"}'
    header = json.loads(conn.execute(
        "SELECT scope_json FROM archive_batches WHERE batch_id='duplicate-key'"
    ).fetchone()[0])
    header["inventory_bytes"] = len(changed.encode("utf-8"))
    header["inventory_sha256"] = hashlib.sha256(changed.encode("utf-8")).hexdigest()
    with conn:
        conn.execute(
            "UPDATE archive_plan_segments SET payload=?,byte_length=? "
            "WHERE batch_id='duplicate-key' AND segment_index=0",
            (changed, len(changed.encode("utf-8"))),
        )
        conn.execute(
            "UPDATE archive_batches SET scope_json=? WHERE batch_id='duplicate-key'",
            (json.dumps(header),),
        )
    with pytest.raises(ArchivePlanStoreError, match="inventory"):
        load_archive_plan(conn, "duplicate-key")


def test_reload_rejects_header_document_budget_before_segments(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan("over-budget-header")
    save_plan(conn, inventory, capacity)
    header = json.loads(conn.execute(
        "SELECT scope_json FROM archive_batches WHERE batch_id='over-budget-header'"
    ).fetchone()[0])
    header["inventory_bytes"] = ARCHIVE_PLAN_MAX_BYTES + 1
    with conn:
        conn.execute(
            "UPDATE archive_batches SET scope_json=? WHERE batch_id='over-budget-header'",
            (json.dumps(header),),
        )
    with pytest.raises(ArchivePlanStoreError, match="bound|budget"):
        load_archive_plan(conn, "over-budget-header")


def test_full_audit_recomputes_each_plan_segment_and_header(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan("audit-segments")
    save_plan(conn, inventory, capacity)
    validate_archive_facts(conn)
    payload, byte_length = conn.execute(
        "SELECT payload,byte_length FROM archive_plan_segments "
        "WHERE batch_id='audit-segments' AND segment_index=0"
    ).fetchone()
    header = conn.execute(
        "SELECT scope_json FROM archive_batches WHERE batch_id='audit-segments'"
    ).fetchone()[0]
    with conn:
        conn.execute(
            "DELETE FROM archive_plan_segments WHERE batch_id='audit-segments'"
        )
    with pytest.raises(ArchiveSchemaError, match="segment"):
        validate_archive_facts(conn)
    with conn:
        conn.execute(
            "INSERT INTO archive_plan_segments VALUES ('audit-segments',0,?,?)",
            (payload, byte_length),
        )
    validate_archive_facts(conn)
    parsed = json.loads(header)
    parsed["segment_count"] = 2
    with conn:
        conn.execute(
            "UPDATE archive_batches SET scope_json=? WHERE batch_id='audit-segments'",
            (json.dumps(parsed),),
        )
    with pytest.raises(ArchiveSchemaError, match="segment"):
        validate_archive_facts(conn)
    with conn:
        conn.execute(
            "UPDATE archive_batches SET scope_json=? WHERE batch_id='audit-segments'",
            (header,),
        )
    changed = payload + " "
    with conn:
        conn.execute(
            "UPDATE archive_plan_segments SET payload=?,byte_length=? "
            "WHERE batch_id='audit-segments' AND segment_index=0",
            (changed, len(changed.encode("utf-8"))),
        )
        conn.execute(
            "UPDATE archive_batches SET scope_json=? WHERE batch_id='audit-segments'",
            (json.dumps({**parsed, "segment_count": 1,
                         "inventory_bytes": len(changed.encode("utf-8"))}),),
        )
    with pytest.raises(ArchiveSchemaError, match="digest"):
        validate_archive_facts(conn)


def test_oversized_segment_is_rejected_before_its_payload_is_selected(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan("oversized-segment")
    save_plan(conn, inventory, capacity)
    oversized = "x" * (2 * 1024 * 1024)
    with conn:
        conn.execute(
            "UPDATE archive_plan_segments SET payload=?,byte_length=? "
            "WHERE batch_id='oversized-segment' AND segment_index=0",
            (oversized, len(oversized)),
        )
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        with pytest.raises(ArchivePlanStoreError, match="segment byte length"):
            load_archive_plan(conn, "oversized-segment")
    finally:
        conn.set_trace_callback(None)
    assert any("length(CAST(payload AS BLOB))" in statement for statement in statements)
    assert not any(statement.lstrip().upper().startswith("SELECT PAYLOAD")
                   for statement in statements)


def test_large_scope_header_is_accepted_by_save_load_and_audit(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan("large-scope")
    scope = {"kind": "explicit_selection", "blob": "x" * 1_500_000}
    saved = save_plan(conn, inventory, capacity, scope=scope)
    assert saved.plan.scope == scope
    assert len(saved.plan.scope_receipt["entry_facts"]) == 1
    assert load_archive_plan(conn, "large-scope") == saved.plan
    validate_archive_facts(conn)


def test_audit_reuses_the_full_plan_protocol_for_receipts(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan("audit-receipt")
    receipt = make_receipt(inventory)
    save_plan(conn, inventory, capacity, receipt=receipt)
    validate_archive_facts(conn)
    header = json.loads(conn.execute(
        "SELECT scope_json FROM archive_batches WHERE batch_id='audit-receipt'"
    ).fetchone()[0])
    partial = replace(
        receipt, consumed_entries=0, remaining_entries=len(inventory.assets),
        last_page_has_next=True, digest=None, entry_facts=(),
        entries_digest=_facts_digest(()),
    )
    header["scope_receipt"] = json.loads(partial.to_json())
    with conn:
        conn.execute(
            "UPDATE archive_batches SET scope_json=? WHERE batch_id='audit-receipt'",
            (json.dumps(header),),
        )
    with pytest.raises(ArchiveSchemaError, match="protocol|receipt|complete"):
        validate_archive_facts(conn)


def test_complete_inventory_rejects_sub_batch_and_complete_receipts_reject_partial_inventory(
    migrated_copy,
):
    conn, _ = migrated_copy
    inventory, capacity = make_plan("completeness")
    receipt = make_receipt(inventory)
    sub = replace(
        receipt, selection_kind=ScopeSelectionKind.SUB_BATCH,
        sub_batch_limit=1, sub_batch_description="one page", digest=None,
    )
    with pytest.raises(ArchivePlanStoreError, match="complete whole-snapshot"):
        prepare_archive_plan(inventory, capacity, receipt=sub)

    partial_inventory = replace(
        inventory, status=InventoryStatus.NEEDS_ADJUSTMENT,
        scope_complete=False, asset_count=0, path_count=0,
        packages=(), member_payload_bytes=0, physical_member_payload_bytes=0,
        unique_content_count=0, unique_content_bytes=0,
        adjustment_codes=("inventory_rejected_items",),
        issues=(InventoryIssue(
            InventoryIssueCode.INVALID_PATH, "synthetic rejected item"),),
    )
    complete_receipt = replace(
        make_receipt(partial_inventory), enumeration_state=ScopeState.COMPLETE,
        digest=None,
    )
    partial_capacity = plan_archive_capacity(
        partial_inventory, available_additional_bytes=1_000_000,
    )
    with pytest.raises(ArchivePlanStoreError, match="complete scope|incomplete inventory"):
        prepare_archive_plan(partial_inventory, partial_capacity, receipt=complete_receipt)


def test_asset_source_tamper_is_rejected_by_load_and_audit(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan("source-tamper")
    save_plan(conn, inventory, capacity)
    validate_archive_facts(conn)
    with conn:
        conn.execute("UPDATE archive_assets SET source_kind='observed_existing'")
    with pytest.raises(ArchivePlanStoreError, match="source"):
        load_archive_plan(conn, "source-tamper")


def test_transactional_reload_wraps_timestamp_value_errors(migrated_copy):
    conn, _ = migrated_copy
    inventory, capacity = make_plan("bad-timestamp")
    save_plan(conn, inventory, capacity)
    with conn:
        conn.execute(
            "UPDATE archive_batches SET created_at='not-a-timestamp' WHERE batch_id='bad-timestamp'"
        )
    with pytest.raises(ArchivePlanStoreError):
        load_archive_plan(conn, "bad-timestamp")
    conn.execute("BEGIN")
    try:
        with pytest.raises(ArchivePlanStoreError):
            from core.archive_plan_store import load_archive_plan_in_transaction
            load_archive_plan_in_transaction(conn, "bad-timestamp")
    finally:
        conn.rollback()


def test_prepared_scope_is_a_frozen_copy():
    inventory, capacity = make_plan("frozen-batch")
    scope = {"kind": "explicit", "selected": ["one"]}
    prepared = prepare_archive_plan(inventory, capacity, scope=scope, receipt=make_receipt(inventory))
    scope["selected"].append("two")
    assert prepared.scope == {"kind": "explicit", "selected": ["one"]}
    assert prepared.batch_id == "frozen-batch"