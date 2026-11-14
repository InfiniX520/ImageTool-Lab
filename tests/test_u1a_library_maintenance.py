"""W6 preflight and backup contract: read-only verdicts, faithful snapshots.

Every scenario is synthetic: temporary databases built from the shared DDL
declaration and temporary directories under the pytest basetemp.  No network,
no account, no real gallery and no production database is opened by this file.

The contract under test is docs/optimization/2026-11-14/U1A_W5_W6_SCOPE.md
section 5.1 plus the round's two corrections: backups prefer the SQLite online
backup API (a consistent snapshot while the application keeps writing), the
file-group copy stays available for damaged libraries, and the source group is
asserted unchanged around both.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core import library_maintenance as m
from core.library_repository import _DDL

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Synthetic library helpers
# ---------------------------------------------------------------------------


def insert_job(connection, job_id):
    connection.execute(
        "INSERT INTO jobs(job_id,parent_job_id,request_id,request_version,request_json,"
        "scope_fingerprint,account_ref,state,created_at,updated_at) VALUES "
        "(?,NULL,'request',1,'{}','scope','pixiv:1','queued',"
        "'2026-11-14T00:00:00+00:00','2026-11-14T00:00:00+00:00')",
        (job_id,),
    )


def open_library(path, *, jobs=0, wal=True):
    """Create the library the way the repository does and keep it open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
    for statement in _DDL:
        connection.execute(statement)
    connection.execute(
        'INSERT INTO schema_meta("key","value") VALUES (?,?)', ("format", "jpgpng-library")
    )
    connection.execute(
        'INSERT INTO schema_meta("key","value") VALUES (?,?)', ("schema_version", "1")
    )
    for index in range(jobs):
        insert_job(connection, "job-%04d" % index)
    return connection


def make_library(path, *, jobs=0, wal=False, statements=()):
    connection = open_library(path, jobs=jobs, wal=wal)
    try:
        for statement in statements:
            connection.execute(statement)
    finally:
        connection.close()
    return path


def add_foreign_key_violation(path):
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO batches(batch_id,job_id,command_id,budget_json,phase,stop_reason,"
            "stop_target,coverage_complete,active_slot,created_at,started_at,ended_at) "
            "VALUES ('batch-1','missing-job','command-1','{}','completed',NULL,NULL,NULL,"
            "NULL,'2026-11-14T00:00:00+00:00',NULL,NULL)"
        )
    finally:
        connection.close()


def library_with_pending_wal(root):
    """A library whose committed frames still live in a real -wal file.

    Copying the three files of a live WAL database and then closing the writer
    reproduces the shape the application leaves behind; it needs no live
    writer, so the WAL stays readable.
    """
    source = root / "source" / "library.sqlite3"
    writer = open_library(source, jobs=2, wal=True)
    try:
        target = root / "lib"
        target.mkdir()
        for suffix in ("", "-wal", "-shm"):
            item = Path(str(source) + suffix)
            if item.exists():
                shutil.copy2(item, str(target / ("library.sqlite3" + suffix)))
    finally:
        writer.close()
    return target / "library.sqlite3"


def damage_page(library, page=4096):
    data = bytearray(library.read_bytes())
    assert len(data) > page * 2, "fixture needs a multi-page database"
    data[page:page + 32] = b"\xff" * 32
    library.write_bytes(bytes(data))


def facts(path):
    return {
        suffix: m._file_fact(Path(str(path) + suffix)) for suffix in ("", "-wal", "-shm")
    }


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def test_a_fresh_library_reports_compatible(tmp_path):
    library = make_library(tmp_path / "library.sqlite3", jobs=2)
    report = m.inspect_library(library)
    assert report["status"] == "compatible"
    assert report["healthy"] is True
    assert report["objects_declared"] == len(_DDL) == 49
    assert report["objects_actual"] == 49
    assert report["missing_objects"] == []
    assert report["mismatched_objects"] == []
    assert report["missing_required_tables"] == []
    assert report["quick_check"] == ["ok"]
    assert report["integrity_check"] == ["ok"]
    assert report["foreign_key_violations"] == 0
    assert report["counts"]["jobs"] == 2
    assert report["journal_mode"] == "delete"
    assert report["reasons"] == []


def test_a_missing_path_reports_missing(tmp_path):
    report = m.inspect_library(tmp_path / "absent.sqlite3")
    assert report["status"] == "missing"
    assert report["healthy"] is False
    assert "library_missing" in report["warnings"]
    assert report["size_bytes"] == 0


def test_a_zero_byte_file_reports_empty(tmp_path):
    library = tmp_path / "library.sqlite3"
    library.write_bytes(b"")
    report = m.inspect_library(library)
    assert report["status"] == "empty"
    assert "empty_file" in report["warnings"]


def test_a_sqlite_file_without_tables_reports_empty(tmp_path):
    library = tmp_path / "library.sqlite3"
    connection = sqlite3.connect(library, isolation_level=None)
    connection.execute("PRAGMA user_version=0")
    connection.execute("CREATE TABLE scratch (value TEXT)")
    connection.execute("DROP TABLE scratch")
    connection.close()
    report = m.inspect_library(library)
    assert report["status"] == "empty"
    assert "sqlite_file_without_tables" in report["warnings"]


def test_a_non_sqlite_file_reports_unreadable(tmp_path):
    library = tmp_path / "library.sqlite3"
    library.write_bytes(b"this is not a database at all" * 64)
    report = m.inspect_library(library)
    assert report["status"] == "unreadable"
    assert "not_a_sqlite_file" in report["warnings"]


def test_a_library_without_a_schema_marker_is_incompatible(tmp_path):
    library = tmp_path / "library.sqlite3"
    connection = sqlite3.connect(library, isolation_level=None)
    connection.execute("CREATE TABLE unrelated (value TEXT)")
    connection.close()
    report = m.inspect_library(library)
    assert report["status"] == "incompatible"
    assert "no_library_schema_marker" in report["warnings"]
    assert "format_mismatch" in report["reasons"]
    assert "missing_objects" in report["reasons"]
    assert report["healthy"] is True


def test_a_foreign_product_marker_is_incompatible(tmp_path):
    library = tmp_path / "library.sqlite3"
    connection = open_library(library)
    connection.execute('UPDATE schema_meta SET value=? WHERE "key"=?', ("other-product", "format"))
    connection.close()
    report = m.inspect_library(library)
    assert report["status"] == "incompatible"
    assert "format_mismatch" in report["reasons"]
    assert report["healthy"] is True


def test_a_different_schema_version_is_incompatible(tmp_path):
    library = tmp_path / "library.sqlite3"
    connection = open_library(library)
    connection.execute('UPDATE schema_meta SET value=? WHERE "key"=?', ("2", "schema_version"))
    connection.close()
    report = m.inspect_library(library)
    assert report["status"] == "incompatible"
    assert report["schema_version"] == "2"
    assert "schema_version_mismatch" in report["reasons"]


def test_a_dropped_table_is_listed_as_missing(tmp_path):
    library = make_library(
        tmp_path / "library.sqlite3", statements=("DROP TABLE legacy_imports",)
    )
    report = m.inspect_library(library)
    assert report["status"] == "incompatible"
    assert "legacy_imports" in report["missing_objects"]
    assert "missing_required_tables" in report["reasons"]
    assert "missing_objects" in report["reasons"]


def test_a_drifted_object_text_is_listed_as_mismatched(tmp_path):
    library = make_library(
        tmp_path / "library.sqlite3",
        statements=("DROP TABLE events", "CREATE TABLE events (event_id TEXT PRIMARY KEY)"),
    )
    report = m.inspect_library(library)
    assert report["status"] == "incompatible"
    assert "events" in report["mismatched_objects"]
    assert "mismatched_objects" in report["reasons"]


def test_an_extra_object_is_reported_but_tolerated(tmp_path):
    library = make_library(
        tmp_path / "library.sqlite3", statements=("CREATE TABLE tool_state (value TEXT)",)
    )
    report = m.inspect_library(library)
    assert report["status"] == "compatible"
    assert report["extra_objects"] == ["tool_state"]
    assert report["reasons"] == []
    assert "extra_objects" in report["warnings"]
    assert "tolerated_by_open_path" in report["warnings"]


def test_a_foreign_key_violation_is_incompatible_and_unhealthy(tmp_path):
    library = make_library(tmp_path / "library.sqlite3")
    add_foreign_key_violation(library)
    report = m.inspect_library(library)
    assert report["foreign_key_violations"] == 1
    assert report["healthy"] is False
    assert "foreign_key_violations" in report["reasons"]
    assert report["status"] == "incompatible"


def test_a_structurally_damaged_page_is_never_reported_compatible(tmp_path):
    library = make_library(tmp_path / "library.sqlite3", jobs=300)
    damage_page(library)
    report = m.inspect_library(library)
    assert report["status"] != "compatible"
    assert report["healthy"] is False


def test_a_failed_quick_check_is_never_compatible(tmp_path, monkeypatch):
    library = make_library(tmp_path / "library.sqlite3")

    def damaged_facts(connection):
        return {
            "tables": set(m._DECLARED_TABLES),
            "warnings": [],
            "format": "jpgpng-library",
            "schema_version": "1",
            "journal_mode": "delete",
            "objects_actual": 49,
            "missing_objects": [],
            "extra_objects": [],
            "mismatched_objects": [],
            "missing_required_tables": [],
            "quick_check": ["*** in database main *** Page 3: btree init page error"],
            "integrity_check": ["ok"],
            "foreign_key_violations": 0,
            "counts": {name: 0 for name in m._DECLARED_TABLES},
        }

    monkeypatch.setattr(m, "_facts", damaged_facts)
    report = m.inspect_library(library)
    assert report["status"] == "incompatible"
    assert report["healthy"] is False
    assert "quick_check_failed" in report["reasons"]


def test_an_exclusively_locked_library_is_unreadable_and_does_not_hang(tmp_path):
    library = make_library(tmp_path / "library.sqlite3", jobs=1)
    blocker = sqlite3.connect(library, isolation_level=None)
    blocker.execute("PRAGMA locking_mode=EXCLUSIVE")
    blocker.execute("BEGIN EXCLUSIVE")
    blocker.execute("SELECT COUNT(*) FROM jobs").fetchone()
    try:
        started = time.monotonic()
        report = m.inspect_library(library)
        elapsed = time.monotonic() - started
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert elapsed < 30.0
    assert report["status"] == "unreadable"
    assert "library_locked" in report["warnings"]


def test_a_live_wal_library_can_be_inspected(tmp_path):
    library = tmp_path / "library.sqlite3"
    connection = open_library(library, jobs=1, wal=True)
    try:
        report = m.inspect_library(library)
        assert report["status"] == "compatible"
        assert report["journal_mode"] == "wal"
        assert report["wal_bytes"] > 0
        assert report["counts"]["jobs"] == 1
    finally:
        connection.close()


def test_the_counts_cover_every_declared_table(tmp_path):
    report = m.inspect_library(make_library(tmp_path / "library.sqlite3"))
    assert set(report["counts"]) == set(m._DECLARED_TABLES)
    assert report["counts"]["jobs"] == 0


def test_the_declaration_is_imported_from_the_repository():
    assert len(m._DECLARED) == len(_DDL)
    assert m._DECLARED["jobs"][0] == "TABLE"
    assert m._DECLARED["ux_deliveries_active_output"][0] == "INDEX"
    assert "jobs" in m._DECLARED_TABLES
    assert "ux_deliveries_active_output" not in m._DECLARED_TABLES


def test_inspection_never_executes_a_write_statement(tmp_path, monkeypatch):
    library = make_library(tmp_path / "library.sqlite3", jobs=2)
    seen = []
    original = m._connect_readonly

    def spy(path, *, snapshot=True):
        connection = original(path, snapshot=snapshot)
        connection.set_trace_callback(seen.append)
        return connection

    monkeypatch.setattr(m, "_connect_readonly", spy)
    m.inspect_library(library)
    lowered = [str(sql).lower() for sql in seen]
    for forbidden in (
        "wal_checkpoint",
        "journal_mode=",
        "journal_mode =",
        "writable_schema",
        "vacuum",
        "begin immediate",
        "begin exclusive",
        "insert into",
        "update ",
        "delete from",
        "create table",
        "drop table",
        "alter table",
        "create index",
    ):
        assert not any(forbidden in sql for sql in lowered), forbidden
    assert any(sql.startswith("pragma quick_check") for sql in lowered)
    assert any(sql.startswith("pragma journal_mode") for sql in lowered)


def test_inspection_does_not_change_the_database_or_its_wal(tmp_path):
    library = make_library(tmp_path / "library.sqlite3", jobs=2)
    before = facts(library)
    m.inspect_library(library)
    m.switch_plan(library)
    after = facts(library)
    assert before == after


def test_a_unc_path_is_refused_before_any_read():
    with pytest.raises(m.UnsupportedPathError) as info:
        m.inspect_library(r"\\server\share\library.sqlite3")
    assert info.value.code == "unsupported_path"


def test_a_linked_library_path_is_refused(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3")
    junction = tmp_path / "junction"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(library.parent)],
        capture_output=True,
        text=True,
    )
    if made.returncode != 0:
        pytest.skip("this host cannot create a junction: " + made.stderr.strip())
    with pytest.raises(m.UnsupportedPathError):
        m.inspect_library(junction / "library.sqlite3")


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


def test_the_default_backup_is_one_verified_snapshot(tmp_path):
    # The real library's header is WAL, so the snapshot has to be normalised
    # into one self-contained file; a rollback-journal fixture would not prove
    # that the normalisation happens.
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=3, wal=True)
    manifest = m.backup_library(library, tmp_path / "backups", label="before-w5")
    assert manifest["mode"] == "online_backup"
    assert [item["name"] for item in manifest["files"]] == ["library.sqlite3"]
    assert manifest["verified"] is True
    assert manifest["objects_match_source"] is True
    assert manifest["counts_match_source"] is True
    assert manifest["counts"]["jobs"] == 3
    assert manifest["quick_check"] == ["ok"]
    assert manifest["integrity_check"] == ["ok"]
    assert manifest["journal_mode_source"] == "wal"
    assert manifest["journal_mode_copy"] == "delete"
    assert manifest["source_checks"]["main_unchanged"] is True
    assert manifest["source_checks"]["shm_excluded"] is True
    assert manifest["restore_steps"]
    directory = Path(manifest["manifest_path"]).parent
    assert directory.name == "before-w5"
    written = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert written["status"] == "ok"
    assert written["mode"] == "online_backup"
    assert m.inspect_library(directory / "library.sqlite3")["counts"]["jobs"] == 3


def test_a_backup_is_taken_while_a_writer_holds_an_uncommitted_transaction(tmp_path):
    library = tmp_path / "lib" / "library.sqlite3"
    writer = open_library(library, jobs=2, wal=True)
    try:
        writer.execute("BEGIN")
        insert_job(writer, "uncommitted-1")
        manifest = m.backup_library(library, tmp_path / "backups", label="live")
        assert manifest["mode"] == "online_backup"
        assert manifest["verified"] is True
        assert manifest["counts"]["jobs"] == 2
        assert manifest["integrity_check"] == ["ok"]
        assert manifest["source_checks"]["main_unchanged"] is True
        copy = Path(manifest["manifest_path"]).parent / "library.sqlite3"
        report = m.inspect_library(copy)
        assert report["status"] == "compatible"
        assert report["healthy"] is True
        assert report["counts"]["jobs"] == 2
        writer.execute("ROLLBACK")
        assert writer.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    finally:
        writer.close()


def test_the_backup_never_touches_the_source(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2)
    before = facts(library)
    m.backup_library(library, tmp_path / "backups", label="quiet")
    after = facts(library)
    for suffix in ("", "-wal", "-shm"):
        assert before[suffix]["bytes"] == after[suffix]["bytes"]
        assert before[suffix]["mtime_ns"] == after[suffix]["mtime_ns"]
        assert before[suffix]["sha256"] == after[suffix]["sha256"]


def test_an_empty_or_absent_wal_counts_as_unchanged():
    present = {
        "": {"present": True, "bytes": 10, "mtime_ns": 1},
        "-wal": {"present": True, "bytes": 0, "mtime_ns": 5},
        "-shm": {"present": True, "bytes": 32768, "mtime_ns": 7},
    }
    absent = {
        "": {"present": True, "bytes": 10, "mtime_ns": 1},
        "-wal": {"present": False, "bytes": 0, "mtime_ns": 0},
        "-shm": {"present": False, "bytes": 0, "mtime_ns": 0},
    }
    assert m._comparable(present) == m._comparable(absent)
    with_wal = dict(present)
    with_wal["-wal"] = {"present": True, "bytes": 4096, "mtime_ns": 9}
    assert m._comparable(with_wal) != m._comparable(absent)
    moved = dict(present)
    moved[""] = {"present": True, "bytes": 10, "mtime_ns": 2}
    assert m._comparable(present) != m._comparable(moved)


def test_inspecting_a_wal_library_leaves_the_database_bytes_alone(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=1, wal=True)
    before = m._file_fact(library)
    m.inspect_library(library)
    after = m._file_fact(library)
    assert (before["bytes"], before["mtime_ns"], before["sha256"]) == (
        after["bytes"],
        after["mtime_ns"],
        after["sha256"],
    )
    journal = Path(str(library) + "-wal")
    if journal.exists():
        assert journal.stat().st_size == 0


def test_a_file_group_backup_keeps_the_source_bytes(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2, wal=True)
    manifest = m.backup_library(
        library, tmp_path / "backups", label="bytes", mode="file_group"
    )
    assert manifest["mode"] == "file_group"
    assert manifest["source_checks"]["group_unchanged"] is True
    copy = Path(manifest["manifest_path"]).parent / library.name
    assert copy.read_bytes() == library.read_bytes()
    assert m.inspect_library(copy)["status"] == "compatible"


def test_a_wal_with_frames_is_never_silently_dropped(tmp_path, monkeypatch):
    library = library_with_pending_wal(tmp_path)
    assert Path(str(library) + "-wal").stat().st_size > 0
    original = m._copy_file

    def vanishing(source, destination):
        if source.name.endswith("-wal"):
            source.unlink()
        return original(source, destination)

    monkeypatch.setattr(m, "_copy_file", vanishing)
    with pytest.raises(m.BackupError) as info:
        m.backup_library(
            library, tmp_path / "backups", label="vanish", mode="file_group"
        )
    assert info.value.code == "backup_failed", info.value.detail
    assert "FileNotFoundError" in info.value.detail


def test_a_file_group_backup_refuses_a_source_that_moves_during_the_copy(tmp_path, monkeypatch):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2, wal=True)
    original = m._copy_file

    def moving(source, destination):
        # Another process commits while the main bytes are being copied: the
        # file group can no longer be one consistent state.
        if source.name == library.name:
            connection = sqlite3.connect(library, isolation_level=None)
            insert_job(connection, "late-job")
            connection.close()
        return original(source, destination)

    monkeypatch.setattr(m, "_copy_file", moving)
    with pytest.raises(m.BackupError) as info:
        m.backup_library(
            library, tmp_path / "backups", label="moving", mode="file_group"
        )
    assert info.value.code == "backup_verification_failed", info.value.detail
    assert "library_changed_during_copy" in info.value.detail
    assert not (tmp_path / "backups" / "moving").exists()


def test_a_backup_of_an_unhealthy_but_readable_library_is_faithful(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3")
    add_foreign_key_violation(library)
    manifest = m.backup_library(library, tmp_path / "backups", label="fk")
    assert manifest["mode"] == "file_group"
    assert manifest["healthy"] is False
    assert manifest["foreign_key_violations"] == 1
    assert manifest["verified"] is True


def test_an_unreadable_source_is_refused_instead_of_silently_copied(tmp_path):
    library = tmp_path / "lib" / "library.sqlite3"
    library.parent.mkdir()
    library.write_bytes(b"not a database" * 200)
    with pytest.raises(m.BackupError) as info:
        m.backup_library(library, tmp_path / "backups")
    assert info.value.code == "source_unreadable"
    assert not (tmp_path / "backups").exists()


def test_a_missing_source_is_refused(tmp_path):
    (tmp_path / "lib").mkdir()
    with pytest.raises(m.BackupError) as info:
        m.backup_library(tmp_path / "lib" / "absent.sqlite3", tmp_path / "backups")
    assert info.value.code == "library_missing"


def test_an_empty_source_is_refused(tmp_path):
    library = tmp_path / "lib" / "library.sqlite3"
    library.parent.mkdir()
    library.write_bytes(b"")
    with pytest.raises(m.BackupError) as info:
        m.backup_library(library, tmp_path / "backups")
    assert info.value.code == "library_empty"


def test_a_failed_copy_removes_its_own_directory(tmp_path, monkeypatch):
    library = make_library(tmp_path / "lib" / "library.sqlite3", wal=True)

    def explode(source, destination):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(m, "_copy_file", explode)
    with pytest.raises(m.BackupError) as info:
        m.backup_library(
            library, tmp_path / "backups", label="enospc", mode="file_group"
        )
    assert info.value.code == "backup_failed"
    assert not (tmp_path / "backups" / "enospc").exists()


def test_an_interrupted_copy_removes_its_own_directory(tmp_path, monkeypatch):
    library = make_library(tmp_path / "lib" / "library.sqlite3", wal=True)

    def interrupt(source, destination):
        raise KeyboardInterrupt

    monkeypatch.setattr(m, "_copy_file", interrupt)
    with pytest.raises(KeyboardInterrupt):
        m.backup_library(
            library, tmp_path / "backups", label="interrupted", mode="file_group"
        )
    assert not (tmp_path / "backups" / "interrupted").exists()


def test_a_verification_failure_leaves_no_backup_behind(tmp_path, monkeypatch):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2)

    def disagree(path):
        raise AssertionError("unused")

    monkeypatch.setattr(m, "inspect_library", disagree)
    with pytest.raises(AssertionError):
        m.backup_library(library, tmp_path / "backups", label="broken")
    assert not (tmp_path / "backups" / "broken").exists()


def test_an_existing_label_target_is_refused(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3")
    (tmp_path / "backups" / "keep").mkdir(parents=True)
    with pytest.raises(m.BackupError) as info:
        m.backup_library(library, tmp_path / "backups", label="keep")
    assert info.value.code == "backup_target_exists"


def test_two_unlabelled_backups_never_collide(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3")
    first = m.backup_library(library, tmp_path / "backups")
    second = m.backup_library(library, tmp_path / "backups")
    assert first["manifest_path"] != second["manifest_path"]
    assert Path(first["manifest_path"]).parent != Path(second["manifest_path"]).parent


def test_an_invalid_label_is_refused(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3")
    with pytest.raises(m.BackupError) as info:
        m.backup_library(library, tmp_path / "backups", label="with space")
    assert info.value.code == "invalid_label"


def test_a_backup_inside_the_library_directory_is_refused(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3")
    with pytest.raises(m.BackupError) as info:
        m.backup_library(library, tmp_path / "lib" / "backup")
    assert info.value.code == "destination_inside_library"


def test_a_backup_destination_below_a_file_is_refused(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3")
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(m.BackupError) as info:
        m.backup_library(library, blocker / "backups")
    assert info.value.code == "destination_unavailable"


def test_the_restore_steps_depend_on_the_backup_mode(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=1, wal=True)
    online = m.backup_library(library, tmp_path / "b1", label="online")
    group = m.backup_library(library, tmp_path / "b2", label="group", mode="file_group")
    # WR-01: the old text told the operator to delete the -wal beside the
    # target.  Neither mode may do that any more; both must keep the current
    # group in a retention directory before anything is put back.
    assert not any("一并删除" in step for step in online["restore_steps"])
    assert not any("一并删除" in step for step in group["restore_steps"])
    assert any("整套文件" in step for step in group["restore_steps"])
    assert any("-wal" in step for step in group["restore_steps"])
    assert any("自包含" in step for step in online["restore_steps"])
    for steps in (online["restore_steps"], group["restore_steps"]):
        assert any("保留目录" in step for step in steps)
        assert any("verify" in step for step in steps)
        assert any("compare" in step for step in steps)
        assert steps[-1].startswith("7."), "the last step must be the way back"


def test_the_manifest_hashes_match_the_copied_bytes(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=4)
    manifest = m.backup_library(library, tmp_path / "backups", label="hash")
    directory = Path(manifest["manifest_path"]).parent
    for item in manifest["files"]:
        path = directory / item["name"]
        assert path.stat().st_size == item["bytes"]
        assert m._sha256_file(path) == item["sha256"]


# ---------------------------------------------------------------------------
# Copy verification
# ---------------------------------------------------------------------------


def test_verify_accepts_a_snapshot_copy(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2)
    manifest = m.backup_library(library, tmp_path / "backups", label="ok")
    copy = Path(manifest["manifest_path"]).parent / "library.sqlite3"
    result = m.verify_library_copy(library, copy)
    assert result["ok"] is True
    assert result["objects_match_source"] is True
    assert result["counts_match"] is True
    assert result["checks_match"] is True
    assert result["declared_ok"] is True
    assert result["reasons"] == []


def test_verify_rejects_a_copy_with_a_dropped_table(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2)
    manifest = m.backup_library(library, tmp_path / "backups", label="dropped")
    copy = Path(manifest["manifest_path"]).parent / "library.sqlite3"
    connection = sqlite3.connect(copy, isolation_level=None)
    connection.execute("DROP TABLE events")
    connection.close()
    result = m.verify_library_copy(library, copy)
    assert result["ok"] is False
    assert "copy_objects_mismatch" in result["reasons"]
    assert result["declared_ok"] is False


def test_verify_rejects_a_copy_with_fewer_rows(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=3)
    manifest = m.backup_library(library, tmp_path / "backups", label="short")
    copy = Path(manifest["manifest_path"]).parent / "library.sqlite3"
    connection = sqlite3.connect(copy, isolation_level=None)
    connection.execute("DELETE FROM jobs WHERE rowid=1")
    connection.close()
    result = m.verify_library_copy(library, copy)
    assert result["ok"] is False
    assert result["counts_match"] is False
    assert "counts_mismatch" in result["reasons"]


def test_verify_reports_a_missing_copy_as_unreadable(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3")
    result = m.verify_library_copy(library, tmp_path / "absent.sqlite3")
    assert result["ok"] is False
    assert result["copy_status"] == "missing"


# ---------------------------------------------------------------------------
# Switch plan
# ---------------------------------------------------------------------------


def test_the_switch_plan_orders_backup_before_the_switch(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3")
    plan = m.switch_plan(library)
    assert plan["compatible"] is True
    assert plan["blockers"] == []
    text = "\n".join(plan["steps"])
    assert text.index("maintain.py backup") < text.index("切换入口")
    assert "verify" in text
    assert "-wal" in text and "删除" in text


def test_the_switch_plan_blocks_an_incompatible_library(tmp_path):
    library = make_library(
        tmp_path / "library.sqlite3", statements=("DROP TABLE legacy_imports",)
    )
    plan = m.switch_plan(library)
    assert plan["compatible"] is False
    assert "missing_objects" in plan["blockers"]
    assert any("file_group" in step for step in plan["steps"])


def test_the_switch_plan_of_a_missing_library_is_not_blocked(tmp_path):
    plan = m.switch_plan(tmp_path / "absent.sqlite3")
    assert plan["status"] == "missing"
    assert plan["compatible"] is True
    assert plan["blockers"] == []


def test_the_switch_plan_and_inspection_are_read_only(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=1)
    before = facts(library)
    m.switch_plan(library)
    m.inspect_library(library)
    after = facts(library)
    assert before == after


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_the_cli_prints_parseable_json_for_a_compatible_library(tmp_path, capsys):
    library = make_library(tmp_path / "library.sqlite3", jobs=1)
    assert m.main(["--json", "inspect", str(library)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "compatible"
    assert payload["objects_declared"] == 49


def test_the_cli_accepts_the_json_flag_after_the_command(tmp_path, capsys):
    library = make_library(tmp_path / "library.sqlite3")
    assert m.main(["inspect", "--json", str(library)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "compatible"


def test_the_cli_exit_codes_follow_the_verdict(tmp_path, capsys):
    compatible = make_library(tmp_path / "compatible.sqlite3")
    drifted = make_library(
        tmp_path / "drifted.sqlite3", statements=("DROP TABLE legacy_imports",)
    )
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"not a database" * 100)
    assert m.main(["inspect", str(compatible)]) == 0
    assert m.main(["inspect", str(drifted)]) == 2
    assert m.main(["inspect", str(broken)]) == 3
    assert m.main(["inspect", str(tmp_path / "absent.sqlite3")]) == 0
    capsys.readouterr()


def test_the_cli_error_payload_is_json(tmp_path, capsys):
    library = tmp_path / "lib" / "library.sqlite3"
    library.parent.mkdir()
    library.write_bytes(b"not a database" * 100)
    assert m.main(["--json", "backup", str(library), str(tmp_path / "backups")]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["code"] == "source_unreadable"


def test_the_cli_backup_then_verify_round_trip(tmp_path, capsys):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2)
    assert m.main(["--json", "backup", str(library), str(tmp_path / "backups"),
                   "--label", "cli"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    copy = Path(manifest["manifest_path"]).parent / "library.sqlite3"
    assert m.main(["--json", "verify", str(library), str(copy)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert m.main(["--json", "plan", str(library)]) == 0
    assert json.loads(capsys.readouterr().out)["compatible"] is True


def test_the_cli_verify_reports_a_mismatch_with_exit_two(tmp_path, capsys):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2)
    manifest = m.backup_library(library, tmp_path / "backups", label="mismatch")
    copy = Path(manifest["manifest_path"]).parent / "library.sqlite3"
    connection = sqlite3.connect(copy, isolation_level=None)
    connection.execute("DELETE FROM jobs")
    connection.close()
    assert m.main(["--json", "verify", str(library), str(copy)]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_the_script_entry_point_runs_from_the_repository_root(tmp_path):
    library = make_library(tmp_path / "library.sqlite3", jobs=1)
    result = subprocess.run(
        [sys.executable, "-s", "-B", "-X", "utf8", "maintain.py", "--json",
         "inspect", str(library)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "compatible"


def test_the_script_entry_point_is_silent_on_import():
    result = subprocess.run(
        [sys.executable, "-s", "-B", "-X", "utf8", "-c", "import maintain"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# Logical content fingerprint (contract v5 section 1.3)
# ---------------------------------------------------------------------------


def test_the_fingerprint_is_stable_for_the_same_database(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=3)
    first = m.logical_fingerprint(library)
    second = m.logical_fingerprint(library)
    assert first["version"] == m.FINGERPRINT_VERSION
    assert first["digest"] == second["digest"]
    assert first["rows"] == second["rows"]
    assert first["rows"]["jobs"] == 3
    assert len(first["objects_digest"]) == 64


def test_one_changed_value_changes_the_fingerprint_not_the_counts(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2)
    before = m.logical_fingerprint(library)
    connection = sqlite3.connect(library, isolation_level=None)
    try:
        connection.execute("UPDATE jobs SET state='cancelled' WHERE job_id='job-0000'")
    finally:
        connection.close()
    after = m.logical_fingerprint(library)
    assert after["rows"] == before["rows"]
    assert after["digest"] != before["digest"]
    assert after["tables"]["jobs"]["digest"] != before["tables"]["jobs"]["digest"]


def test_swapping_two_values_changes_the_fingerprint(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2)
    connection = sqlite3.connect(library, isolation_level=None)
    try:
        connection.execute("UPDATE jobs SET request_id='a' WHERE job_id='job-0000'")
        connection.execute("UPDATE jobs SET request_id='b' WHERE job_id='job-0001'")
    finally:
        connection.close()
    before = m.logical_fingerprint(library)["digest"]
    connection = sqlite3.connect(library, isolation_level=None)
    try:
        connection.execute("UPDATE jobs SET request_id='b' WHERE job_id='job-0000'")
        connection.execute("UPDATE jobs SET request_id='a' WHERE job_id='job-0001'")
    finally:
        connection.close()
    after = m.logical_fingerprint(library)["digest"]
    assert after != before


def test_null_empty_text_and_empty_blob_are_distinct_values(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3")
    connection = sqlite3.connect(library, isolation_level=None)
    try:
        connection.execute("CREATE TABLE scratch(value)")
        connection.execute("INSERT INTO scratch(value) VALUES (NULL)")
    finally:
        connection.close()
    null_digest = m.logical_fingerprint(library)["digest"]
    connection = sqlite3.connect(library, isolation_level=None)
    try:
        connection.execute("UPDATE scratch SET value=''")
    finally:
        connection.close()
    empty_text = m.logical_fingerprint(library)["digest"]
    connection = sqlite3.connect(library, isolation_level=None)
    try:
        connection.execute("UPDATE scratch SET value=x''")
    finally:
        connection.close()
    empty_blob = m.logical_fingerprint(library)["digest"]
    assert len({null_digest, empty_text, empty_blob}) == 3


def test_an_integer_and_its_text_form_do_not_collide(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3")
    connection = sqlite3.connect(library, isolation_level=None)
    try:
        connection.execute("CREATE TABLE scratch(value)")
        connection.execute("INSERT INTO scratch(value) VALUES (1)")
    finally:
        connection.close()
    integer_digest = m.logical_fingerprint(library)["digest"]
    connection = sqlite3.connect(library, isolation_level=None)
    try:
        connection.execute("UPDATE scratch SET value='1'")
    finally:
        connection.close()
    text_digest = m.logical_fingerprint(library)["digest"]
    assert integer_digest != text_digest


def test_the_fingerprint_covers_extra_objects(tmp_path):
    plain = make_library(tmp_path / "a" / "library.sqlite3", jobs=1)
    indexed = make_library(
        tmp_path / "b" / "library.sqlite3",
        jobs=1,
        statements=("CREATE INDEX extra_same_name ON jobs(state)",),
    )
    left = m.logical_fingerprint(plain)
    right = m.logical_fingerprint(indexed)
    assert left["rows"] == right["rows"]
    assert left["objects_digest"] != right["objects_digest"]
    assert left["digest"] != right["digest"]


def test_the_fingerprint_of_a_without_rowid_table_is_deterministic(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3")
    connection = sqlite3.connect(library, isolation_level=None)
    try:
        connection.execute(
            "CREATE TABLE scratch(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID"
        )
        for index in range(5):
            connection.execute(
                "INSERT INTO scratch(key,value) VALUES (?,?)",
                ("k%d" % index, "v%d" % index),
            )
    finally:
        connection.close()
    first = m.logical_fingerprint(library)
    second = m.logical_fingerprint(library)
    assert first["digest"] == second["digest"]
    assert first["tables"]["scratch"]["order"] == "primary_key"
    assert first["rows"]["scratch"] == 5


def test_the_fingerprint_does_not_depend_on_the_read_batch_size(tmp_path, monkeypatch):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=40)
    default = m.logical_fingerprint(library)["digest"]
    monkeypatch.setattr(m, "FINGERPRINT_CHUNK_ROWS", 7)
    chunked = m.logical_fingerprint(library)["digest"]
    assert chunked == default


# ---------------------------------------------------------------------------
# Sealed backup verification (contract v5 section 1.4)
# ---------------------------------------------------------------------------


def test_backup_writes_a_sealed_v2_manifest(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2)
    manifest = m.backup_library(library, tmp_path / "backups", label="sealed")
    directory = Path(manifest["manifest_path"]).parent
    payload = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert payload["manifest_version"] == 2
    assert payload["finalizer"]["sealed_by"] == "backup_library"
    assert payload["sealed_members"] == ["library.sqlite3"]
    assert [item["name"] for item in payload["members"]] == ["library.sqlite3"]
    fingerprint = payload["logical_fingerprint"]
    assert fingerprint["version"] == m.FINGERPRINT_VERSION
    assert len(fingerprint["digest"]) == 64
    assert fingerprint["rows"]["jobs"] == 2
    assert set(payload) >= {"members", "sealed_members", "finalizer", "files"}


def test_verify_backup_accepts_a_fresh_backup_without_touching_it(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2)
    manifest = m.backup_library(library, tmp_path / "backups", label="fresh")
    directory = Path(manifest["manifest_path"]).parent
    before = facts(directory / "library.sqlite3")
    result = m.verify_backup(directory)
    after = facts(directory / "library.sqlite3")
    assert result["ok"] is True
    assert result["sealed"]["state"] == "ok"
    assert result["logical"]["state"] == "ok"
    assert result["health"]["state"] == "ok"
    assert result["compatible"]["state"] == "ok"
    assert result["source_equivalent"]["state"] == "not_checked"
    assert result["reasons"] == []
    assert before == after


def test_verify_backup_rejects_a_same_count_row_change(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2)
    manifest = m.backup_library(library, tmp_path / "backups", label="changed")
    directory = Path(manifest["manifest_path"]).parent
    copy = directory / "library.sqlite3"
    connection = sqlite3.connect(copy, isolation_level=None)
    try:
        connection.execute("UPDATE jobs SET state='cancelled'")
    finally:
        connection.close()
    result = m.verify_backup(directory)
    assert result["ok"] is False
    assert result["logical"]["state"] == "mismatch"
    assert "logical_mismatch" in result["reasons"]
    assert result["sealed"]["state"] == "mismatch"


def test_verify_backup_rejects_a_forged_logical_digest(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=1)
    manifest = m.backup_library(library, tmp_path / "backups", label="forged")
    directory = Path(manifest["manifest_path"]).parent
    path = directory / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["logical_fingerprint"]["digest"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = m.verify_backup(directory)
    assert result["sealed"]["state"] == "ok"
    assert result["logical"]["state"] == "mismatch"
    assert result["ok"] is False


def test_verify_backup_reports_a_missing_member(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=1)
    manifest = m.backup_library(library, tmp_path / "backups", label="gone")
    directory = Path(manifest["manifest_path"]).parent
    (directory / "library.sqlite3").unlink()
    result = m.verify_backup(directory)
    assert result["sealed"]["state"] == "missing"
    assert result["ok"] is False
    assert "sealed_missing" in result["reasons"]


def test_verify_backup_refuses_a_missing_manifest(tmp_path):
    directory = tmp_path / "empty"
    directory.mkdir()
    with pytest.raises(m.BackupError) as caught:
        m.verify_backup(directory)
    assert caught.value.code == "manifest_missing"


def test_verify_backup_refuses_an_unknown_manifest_version(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3")
    manifest = m.backup_library(library, tmp_path / "backups", label="v99")
    directory = Path(manifest["manifest_path"]).parent
    path = directory / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["manifest_version"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(m.BackupError) as caught:
        m.verify_backup(directory)
    assert caught.value.code == "manifest_version_unsupported"


def test_verify_backup_refuses_a_v1_manifest(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=1)
    manifest = m.backup_library(library, tmp_path / "backups", label="v1")
    directory = Path(manifest["manifest_path"]).parent
    path = directory / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["manifest_version"] = 1
    for key in ("logical_fingerprint", "members", "sealed_members", "finalizer"):
        payload.pop(key, None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = m.verify_backup(directory)
    assert result["ok"] is False
    assert result["logical"]["state"] == "not_checked"
    assert "manifest_too_old" in result["reasons"]
    assert result["sealed"]["state"] == "not_checked"


def test_verify_backup_refuses_a_member_outside_the_directory(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3")
    manifest = m.backup_library(library, tmp_path / "backups", label="traversal")
    directory = Path(manifest["manifest_path"]).parent
    path = directory / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["members"][0]["name"] = "../outside.sqlite3"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(m.BackupError) as caught:
        m.verify_backup(directory)
    assert caught.value.code == "manifest_member_invalid"


def test_verify_backup_refuses_case_colliding_members(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3")
    manifest = m.backup_library(library, tmp_path / "backups", label="collide")
    directory = Path(manifest["manifest_path"]).parent
    path = directory / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["members"].append(dict(payload["members"][0], name="LIBRARY.SQLITE3"))
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(m.BackupError) as caught:
        m.verify_backup(directory)
    assert caught.value.code == "manifest_member_duplicate"


def test_verify_backup_separates_a_changed_source_from_a_sound_backup(tmp_path):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2)
    manifest = m.backup_library(library, tmp_path / "backups", label="drift")
    directory = Path(manifest["manifest_path"]).parent
    connection = sqlite3.connect(library, isolation_level=None)
    try:
        connection.execute(
            "INSERT INTO jobs(job_id,parent_job_id,request_id,request_version,"
            "request_json,scope_fingerprint,account_ref,state,created_at,updated_at) "
            "VALUES ('job-new',NULL,'request',1,'{}','scope','pixiv:1','queued',"
            "'2026-11-14T00:00:00+00:00','2026-11-14T00:00:00+00:00')"
        )
    finally:
        connection.close()
    result = m.verify_backup(directory, source=library)
    assert result["sealed"]["state"] == "ok"
    assert result["logical"]["state"] == "ok"
    assert result["source_equivalent"]["state"] == "mismatch"
    assert "source_changed_since_backup" in result["warnings"]
    assert result["ok"] is True


def test_compare_library_accepts_two_identical_databases(tmp_path):
    first = make_library(tmp_path / "a" / "library.sqlite3", jobs=2)
    second = make_library(tmp_path / "b" / "library.sqlite3", jobs=2)
    result = m.compare_library(first, second)
    assert result["ok"] is True
    assert result["logical_state"] == "ok"
    assert result["objects_match_source"] is True
    assert result["reasons"] == []


def test_compare_library_reports_equal_counts_with_different_values(tmp_path):
    first = make_library(tmp_path / "a" / "library.sqlite3", jobs=2)
    second = make_library(tmp_path / "b" / "library.sqlite3", jobs=2)
    connection = sqlite3.connect(second, isolation_level=None)
    try:
        connection.execute("UPDATE jobs SET state='cancelled'")
    finally:
        connection.close()
    result = m.compare_library(first, second)
    assert result["ok"] is False
    assert result["counts_match"] is True
    assert result["logical"]["state"] == "mismatch"
    assert "copy_content_mismatch" in result["reasons"]


def test_verify_backup_accepts_a_file_group_backup_twice(tmp_path):
    """The byte-for-byte mode must stay verifiable; -shm is bookkeeping."""
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2, wal=True)
    manifest = m.backup_library(library, tmp_path / "backups", label="fg", mode="file_group")
    directory = Path(manifest["manifest_path"]).parent
    assert manifest["sealed_members"] == ["library.sqlite3", "library.sqlite3-wal"]
    for _ in range(2):
        result = m.verify_backup(directory)
        assert result["ok"] is True
        assert result["sealed"]["state"] == "ok"
        assert result["logical"]["state"] == "ok"
    assert sorted(item.name for item in directory.iterdir()) == [
        "library.sqlite3",
        "library.sqlite3-shm",
        "library.sqlite3-wal",
        "manifest.json",
    ]
