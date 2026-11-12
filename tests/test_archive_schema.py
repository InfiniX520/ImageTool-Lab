"""Offline tests for the A1 schema, using only synthetic temporary libraries."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

import pytest

from core.archive_schema import (
    ARCHIVE_SCHEMA_VERSION, ARCHIVE_TABLES, ArchiveMigrationError, ArchiveSchemaError,
    _content_key, _archive_from_json, _local_from_json, bind_archive_path,
    ensure_archive_asset, migrate_archive_copy, record_archive_observation,
    record_local_observation, record_release_event, record_transfer_intent,
    validate_archive_facts,
)
from core.archive_inventory import (
    ArchiveEntry, ArtworkGroup, CapacityPolicy, CapacityStatus, InventoryPolicy,
    build_archive_inventory, plan_archive_capacity,
)
from core.library_paths import InstanceLock
from core.local_observation import (
    LocalObserver, ObservationBudget, ObservationRequest, ObservationStatus,
)
from core.library_repository import LibraryRepository, SchemaVersionError
from core.preservation_policy import (
    ArchiveEvidence, ArchiveState, ContentVersion, LocalEvidence, LocalState, RemovalIntent,
)
from core.task_contracts import CollectionRequest, ContractError, PathRef

CONTENT = ContentVersion("asset-1", 1, 4, "a" * 64)
PATH = PathRef(relative_path="library/artist/asset-1.png")
TIME = "2026-11-11T08:00:00+00:00"


def make_v1(path: Path) -> str:
    with LibraryRepository(path) as repo:
        repo.submit(CollectionRequest("request-1", "synthetic-account"), "submit-1")
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT job_id FROM jobs").fetchone()[0]


def local(state=LocalState.HASH_MATCH, **overrides):
    values = dict(
        content=CONTENT, path=PATH, root_revision=1, run_id="run-1",
        check_id="local-1", checked_at=TIME, state=state,
    )
    if state in {LocalState.HASH_MATCH, LocalState.QUICK_MATCH}:
        values["observed_size"] = 4
    if state is LocalState.HASH_MATCH:
        values["observed_sha256"] = "a" * 64
    values.update(overrides)
    return LocalEvidence(**values)


def database_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def connect(path: Path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_explicit_copy_acknowledgement_and_backup_separation(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    before = database_hash(source)
    with pytest.raises(ArchiveMigrationError, match="confirmation"):
        migrate_archive_copy(source, tmp_path / "backup.sqlite3")
    with pytest.raises(ArchiveMigrationError, match="separate"):
        migrate_archive_copy(source, source, temporary_copy_confirmed=True)
    assert database_hash(source) == before
    assert not (tmp_path / "backup.sqlite3").exists()


def test_migration_preserves_old_rows_and_verified_backup(tmp_path):
    source = tmp_path / "source.sqlite3"
    job_id = make_v1(source)
    with sqlite3.connect(source) as conn:
        old_schema = conn.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    result = migrate_archive_copy(
        source, tmp_path / "recovery" / "before.sqlite3", temporary_copy_confirmed=True,
    )
    assert result.status == "migrated"
    assert result.backup_path.is_file()
    assert len(result.backup_sha256) == 64
    for path in (source, result.backup_path):
        with connect(path) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT job_id FROM jobs").fetchone()[0] == job_id
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with connect(source) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert ARCHIVE_TABLES <= tables
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == ARCHIVE_SCHEMA_VERSION
        validate_archive_facts(conn)
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall() == old_schema
        assert backup.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "1"


def test_idempotent_second_run_uses_new_backup_without_changes(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    first = migrate_archive_copy(source, tmp_path / "one.backup", temporary_copy_confirmed=True)
    second = migrate_archive_copy(source, tmp_path / "two.backup", temporary_copy_confirmed=True)
    assert second.status == "already_current"
    assert first.backup_path.is_file() and second.backup_path.is_file()
    with pytest.raises(ArchiveMigrationError, match="already exists"):
        migrate_archive_copy(source, tmp_path / "two.backup", temporary_copy_confirmed=True)


def test_unknown_future_and_incomplete_schemas_are_refused_without_changes(tmp_path):
    for index, version in enumerate(("4", "2", "unknown")):
        source = tmp_path / f"bad-{index}.sqlite3"
        make_v1(source)
        with sqlite3.connect(source) as conn:
            conn.execute("UPDATE schema_meta SET value=? WHERE key='schema_version'", (version,))
        before = database_hash(source)
        with pytest.raises(ArchiveMigrationError):
            migrate_archive_copy(source, tmp_path / f"bad-{index}.backup", temporary_copy_confirmed=True)
        assert database_hash(source) == before
        assert not (tmp_path / f"bad-{index}.backup").exists()


def test_existing_archive_or_base_object_conflict_rolls_back(tmp_path):
    for index, statement in enumerate((
        "CREATE TABLE archive_batches (batch_id TEXT)",
        "DROP INDEX ux_batches_single_active",
        "DROP TABLE jobs",
    )):
        source = tmp_path / f"conflict-{index}.sqlite3"
        make_v1(source)
        with sqlite3.connect(source) as conn:
            conn.execute(statement)
        before = database_hash(source)
        with pytest.raises(ArchiveMigrationError):
            migrate_archive_copy(source, tmp_path / f"conflict-{index}.backup", temporary_copy_confirmed=True)
        assert database_hash(source) == before


def test_active_writer_lock_prevents_any_migration_write(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    before = database_hash(source)
    with InstanceLock(tmp_path / ".writer.lock"):
        with pytest.raises(ArchiveMigrationError, match="already in use"):
            migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    assert database_hash(source) == before
    assert not (tmp_path / "backup.sqlite3").exists()


def test_archive_asset_and_intent_are_distinct_from_download_history(tmp_path):
    source = tmp_path / "source.sqlite3"
    job_id = make_v1(source)
    migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    with connect(source) as conn:
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="observed_existing")
        record_transfer_intent(
            conn, CONTENT, PATH, intent_id="intent-1", root_revision=1,
            intent=RemovalIntent.OFFLOADED, source="user_reported", observed_at=TIME,
            details={"reason": "synthetic history"},
        )
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM archive_checks").fetchone()[0] == 0
        validate_archive_facts(conn)
        conn.execute(
            "UPDATE archive_transfer_intents SET content_key=? WHERE intent_id='intent-1'",
            ("f" * 64,),
        )
        with pytest.raises(ArchiveSchemaError, match="Transfer intent"):
            validate_archive_facts(conn)


def test_local_evidence_round_trip_and_state_binding(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    with connect(source) as conn:
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="observed_existing")
        for observation in (
            local(), local(LocalState.MISSING, check_id="local-2"),
            local(LocalState.CHANGED_DURING_CHECK, check_id="local-3"),
        ):
            record_local_observation(conn, observation)
        validate_archive_facts(conn)
        stored = conn.execute(
            "SELECT evidence_json FROM archive_local_checks WHERE check_id='local-1'"
        ).fetchone()[0]
        assert _local_from_json(json.loads(stored)) == local()
        assert len(conn.execute("SELECT * FROM archive_local_checks").fetchall()) == 3


def test_stale_or_conflicting_asset_and_evidence_are_rejected(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    with connect(source) as conn:
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="user_selected")
        with pytest.raises(ContractError):
            ensure_archive_asset(conn, CONTENT, PATH, source_kind="unknown")
        with pytest.raises(ContractError):
            ensure_archive_asset(
                conn, replace(CONTENT, asset_id="asset-2"), PATH, source_kind="user_selected",
            )
        with pytest.raises(ContractError):
            record_local_observation(conn, local(content=replace(CONTENT, asset_id="asset-2")))
        with pytest.raises(ContractError):
            ensure_archive_asset(conn, CONTENT, PATH, source_kind="user_selected", artifact_id="absent")
        with pytest.raises(ContractError):
            record_transfer_intent(
                conn, CONTENT, PATH, intent_id="bad", root_revision=1,
                intent="offloaded", source="user_reported", observed_at=TIME,
            )


def _sealed_package(conn, *, check_id="archive-1"):
    conn.execute(
        "INSERT INTO archive_batches VALUES ('batch-1',2,'{}','{}','planned',NULL,?)", (TIME,),
    )
    conn.execute(
        "INSERT INTO archive_packages VALUES ('package-1','batch-1',1,'7z','synthetic-tool',"
        "'external-key-ref',8,?, 'sealed')", ("b" * 64,),
    )
    conn.execute(
        """INSERT INTO archive_members (
            package_id,asset_id,revision,path_key,content_key,size_bytes,sha256,member_path
        ) VALUES ('package-1','asset-1',1,?,?,4,?,'asset-1.png')""",
        (PATH.identity_key, _content_key(CONTENT), "a" * 64),
    )
    conn.execute(
        "INSERT INTO archive_objects VALUES ('package-1',1,'package',8,?,'{}')", ("b" * 64,),
    )
    conn.execute(
        "INSERT INTO archive_locations VALUES ('location-1','package-1',1,1,'synthetic-local-alias','{}',?)",
        (TIME,),
    )


def test_archive_upload_report_does_not_become_verified(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    with connect(source) as conn:
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="observed_existing")
        _sealed_package(conn)
        evidence = ArchiveEvidence(
            CONTENT, ArchiveState.UPLOAD_REPORTED, "archive-1", TIME, "location-1",
        )
        record_archive_observation(conn, evidence)
        row = conn.execute(
            "SELECT state,observed_size,observed_sha256 FROM archive_checks"
        ).fetchone()
        assert row == ("user_reported_uploaded", None, None)
        validate_archive_facts(conn)


def test_verified_archive_must_have_matching_readback_and_containing_package(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    with connect(source) as conn:
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="observed_existing")
        evidence = ArchiveEvidence(
            CONTENT, ArchiveState.USER_RETURNED_VERIFIED, "archive-1", TIME, "location-1",
        )
        with pytest.raises(ContractError):
            record_archive_observation(conn, evidence, observed_size=4)
        with pytest.raises(ContractError):
            record_archive_observation(conn, evidence, observed_size=4, observed_sha256="b" * 64)
        _sealed_package(conn)
        record_archive_observation(conn, evidence, observed_size=4, observed_sha256="a" * 64)
        validate_archive_facts(conn)


def test_release_record_requires_verified_matching_facts_not_only_a_flag(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    with connect(source) as conn:
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="observed_existing")
        _sealed_package(conn)
        record_local_observation(conn, local(LocalState.MISSING))
        record_archive_observation(
            conn, ArchiveEvidence(CONTENT, ArchiveState.UPLOAD_REPORTED, "archive-1", TIME, "location-1"),
        )
        with pytest.raises(ContractError, match="Release"):
            record_release_event(
                conn, CONTENT, operation_id="release-op", event_id="release-1",
                sequence=1, event="prepared", local_check_id="local-1",
                archive_check_id="archive-1", observed_at=TIME,
            )


def test_tampered_indexed_evidence_cannot_survive_validation(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    with connect(source) as conn:
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="observed_existing")
        record_local_observation(conn, local())
        conn.execute("UPDATE archive_local_checks SET run_id='another-run' WHERE check_id='local-1'")
        with pytest.raises(ArchiveSchemaError):
            validate_archive_facts(conn)


def test_synthetic_file_observation_inventory_and_persistence_work_together(tmp_path):
    payload = b"ordinary synthetic bytes"
    root = tmp_path / "root"
    root.mkdir()
    path = PathRef(relative_path="artist/work-1_p0.png")
    (root / "artist").mkdir()
    (root / "artist/work-1_p0.png").write_bytes(payload)
    content = ContentVersion("work-1-p0", 1, len(payload), hashlib.sha256(payload).hexdigest())
    observation = LocalObserver(
        root, root_id="project", root_revision=3,
    ).observe(ObservationRequest(content, path, "integration-run", "local-integration"))
    assert observation.status is ObservationStatus.HASH_MATCH
    inventory = build_archive_inventory(
        [ArtworkGroup("work-1", (ArchiveEntry(content, path, 0),))],
        batch_id="synthetic-batch",
        policy=InventoryPolicy(target_package_bytes=128, batch_budget_bytes=128),
    )
    assert inventory.scope_complete and inventory.packages[0].payload_bytes == len(payload)
    capacity = plan_archive_capacity(
        inventory, available_additional_bytes=1024,
        policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
    )
    assert capacity.status is CapacityStatus.READY
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    with connect(source) as conn:
        ensure_archive_asset(conn, content, path, source_kind="observed_existing")
        record_local_observation(conn, observation.evidence, result=observation)
        validate_archive_facts(conn)
        stored = _local_from_json(json.loads(conn.execute(
            "SELECT evidence_json FROM archive_local_checks WHERE check_id='local-integration'"
        ).fetchone()[0]))
        from core.preservation_policy import PreservationOutcome, assess_preservation
        assert assess_preservation(
            content, path, root_revision=3, run_id="integration-run", local=stored,
        ).outcome is PreservationOutcome.REUSE_LOCAL


def test_legacy_baseline_is_a_new_fact_not_a_historical_hash_match(tmp_path):
    payload = b"existing file"
    root = tmp_path / "root"
    root.mkdir()
    (root / "library/artist").mkdir(parents=True)
    (root / PATH.relative_path).write_bytes(payload)
    content = ContentVersion("asset-1", 1, len(payload), None)
    observation = LocalObserver(root, root_id="project", root_revision=1).observe(
        ObservationRequest(content, PATH, "baseline-run", "baseline-check")
    )
    assert observation.status is ObservationStatus.BASELINE_OBSERVED
    assert observation.digest_complete and observation.evidence.state is LocalState.UNKNOWN
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    with connect(source) as conn:
        ensure_archive_asset(conn, content, PATH, source_kind="observed_existing")
        record_local_observation(conn, observation.evidence, result=observation)
        validate_archive_facts(conn)
        assert conn.execute(
            "SELECT observed_sha256 FROM archive_local_checks"
        ).fetchone()[0] == hashlib.sha256(payload).hexdigest()


def test_cancelled_or_exhausted_observation_can_be_saved_without_reuse(tmp_path):
    content = ContentVersion("asset-1", 1, 4, "a" * 64)
    root = tmp_path / "root"
    root.mkdir()
    (root / "library/artist").mkdir(parents=True)
    (root / PATH.relative_path).write_bytes(b"data")
    for index, request in enumerate((
        ObservationRequest(content, PATH, "run-cancel", "check-cancel"),
        ObservationRequest(content, PATH, "run-budget", "check-budget", ObservationBudget(max_bytes=0)),
    )):
        source = tmp_path / f"source-{index}.sqlite3"
        make_v1(source)
        migrate_archive_copy(source, tmp_path / f"backup-{index}.sqlite3", temporary_copy_confirmed=True)
        with connect(source) as conn:
            ensure_archive_asset(conn, content, PATH, source_kind="observed_existing")
            class Stop:
                def is_set(self):
                    return True
            if index == 0:
                request = replace(request, cancellation=Stop())
            result = LocalObserver(
                root, root_id="project", root_revision=1,
                _clock=lambda: datetime.now(timezone.utc).isoformat(),
            ).observe(request)
            assert result.evidence.state is LocalState.UNKNOWN
            assert not result.digest_complete and result.evidence.observed_sha256 is None
            record_local_observation(conn, result.evidence, result=result)
            validate_archive_facts(conn)


def test_migrated_copy_is_intentionally_not_accepted_by_unchanged_production(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    with pytest.raises(SchemaVersionError):
        LibraryRepository(source)


def test_content_revision_requires_expected_old_path_binding(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    newer = ContentVersion("asset-1", 2, 5, "c" * 64)
    with connect(source) as conn:
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="observed_existing")
        bind_archive_path(conn, CONTENT, PATH, bound_at=TIME)
        ensure_archive_asset(conn, newer, PATH, source_kind="observed_existing")
        with pytest.raises(ContractError):
            bind_archive_path(conn, newer, PATH, bound_at=TIME)
        bind_archive_path(
            conn, newer, PATH, bound_at=TIME, expected_content_key=_content_key(CONTENT),
        )
        assert conn.execute(
            "SELECT asset_id,revision FROM archive_path_bindings WHERE path_key=?",
            (PATH.identity_key,),
        ).fetchone() == ("asset-1", 2)
        assert conn.execute("SELECT COUNT(*) FROM archive_contents").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM archive_assets").fetchone()[0] == 2
        validate_archive_facts(conn)


def test_release_events_append_after_verified_checks_and_stop_at_terminal(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    with connect(source) as conn:
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="observed_existing")
        _sealed_package(conn)
        record_local_observation(conn, local())
        record_archive_observation(
            conn,
            ArchiveEvidence(CONTENT, ArchiveState.USER_RETURNED_VERIFIED, "archive-1", TIME, "location-1"),
            observed_size=4, observed_sha256="a" * 64,
        )
        record_release_event(
            conn, CONTENT, operation_id="release-op", event_id="release-1",
            sequence=1, event="prepared", local_check_id="local-1",
            archive_check_id="archive-1", observed_at=TIME,
        )
        record_release_event(
            conn, CONTENT, operation_id="release-op", event_id="release-2",
            sequence=2, event="released", local_check_id="local-1",
            archive_check_id="archive-1", observed_at=TIME,
        )
        record_release_event(
            conn, CONTENT, operation_id="release-op", event_id="release-1",
            sequence=1, event="prepared", local_check_id="local-1",
            archive_check_id="archive-1", observed_at=TIME,
        )
        validate_archive_facts(conn)
        with pytest.raises(ContractError):
            record_release_event(
                conn, CONTENT, operation_id="release-op", event_id="release-3",
                sequence=3, event="released", local_check_id="local-1",
                archive_check_id="archive-1", observed_at=TIME,
            )
        with pytest.raises(ContractError):
            record_release_event(
                conn, CONTENT, operation_id="other-op", event_id="release-4",
                sequence=1, event="released", local_check_id="local-1",
                archive_check_id="archive-1", observed_at=TIME,
            )


def test_null_content_digest_cannot_be_upgraded_to_verified(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    with connect(source) as conn:
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="observed_existing")
        _sealed_package(conn)
        record_archive_observation(
            conn,
            ArchiveEvidence(CONTENT, ArchiveState.USER_RETURNED_VERIFIED, "archive-1", TIME, "location-1"),
            observed_size=4, observed_sha256="a" * 64,
        )
        conn.execute("UPDATE archive_contents SET sha256=NULL")
        conn.execute("UPDATE archive_assets SET sha256=NULL")
        with pytest.raises(ContractError):
            record_archive_observation(
                conn,
                ArchiveEvidence(CONTENT, ArchiveState.USER_RETURNED_VERIFIED, "archive-2", TIME, "location-1"),
                observed_size=4, observed_sha256="a" * 64,
            )
        with pytest.raises(ArchiveSchemaError):
            validate_archive_facts(conn)


def test_row_factory_asset_reuse_is_exact(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_v1(source)
    migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    with sqlite3.connect(source) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        other = PathRef(relative_path="library/artist/alias.png")
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="observed_existing")
        ensure_archive_asset(conn, CONTENT, other, source_kind="observed_existing")
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="observed_existing")
        assert conn.execute("SELECT COUNT(*) FROM archive_contents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM archive_assets").fetchone()[0] == 2
        validate_archive_facts(conn)


def test_download_receipt_must_match_artifact_path_size_and_digest(tmp_path):
    source = tmp_path / "source.sqlite3"
    job_id = make_v1(source)
    migrate_archive_copy(source, tmp_path / "backup.sqlite3", temporary_copy_confirmed=True)
    output_ref = json.dumps({
        "kind": "project", "root_id": "project", "relative_path": PATH.relative_path,
    })
    with connect(source) as conn:
        conn.execute(
            "INSERT INTO batches (batch_id,job_id,command_id,budget_json,phase,created_at) "
            "VALUES ('batch-1',?,'command-1','{}','queued',?)",
            (job_id, TIME),
        )
        conn.execute(
            """INSERT INTO deliveries (
                delivery_id,job_id,origin_batch_id,illust_id,page_index,output_key,
                output_ref_json,quality_json,quality_key,payload_json,state,generation,
                created_at,updated_at
            ) VALUES ('delivery-1',?,'batch-1','illust-1',0,'key-1',?,'{}','q1','{}',
                      'prepared',1,?,?)""",
            (job_id, output_ref, TIME, TIME),
        )
        conn.execute(
            """INSERT INTO artifacts (
                artifact_id,delivery_id,job_id,illust_id,page_index,output_key,
                output_ref_json,quality_json,size,sha256,verification,completed_at,created_at
            ) VALUES ('artifact-1','delivery-1',?,'illust-1',0,'key-1',?,'{}',4,?,
                      'structure_checked',?,?)""",
            (job_id, output_ref, "a" * 64, TIME, TIME),
        )
        ensure_archive_asset(
            conn, CONTENT, PATH, source_kind="download_receipt", artifact_id="artifact-1",
        )
        validate_archive_facts(conn)
        with pytest.raises(ContractError):
            ensure_archive_asset(
                conn, replace(CONTENT, size_bytes=5), PATH,
                source_kind="download_receipt", artifact_id="artifact-1",
            )
