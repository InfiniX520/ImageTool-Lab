"""One stable backup generation, one object definition (PA-01, PA-02, WR-04).

Every scenario is synthetic: a temporary library built from the shared DDL
declaration, temporary backup directories and temporary restore targets under
the pytest basetemp.  No real library, gallery, account, network or production
database is opened by this file.

The behaviour under test is the post-tidy audit's PA-01 (a backup check may mix
evidence from two versions), PA-02 (the internal-object filter drops a legal
user table) and WR-04 (the manifest is not validated strictly), as amended by
contract v6 A1-A3:

    verify_backup copies the member group once, hashes exactly the bytes it
    read, and answers every later question from that one copy.  A member that
    moves while it is copied fails the check as changed_during_check instead of
    being hashed across two versions.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_u1a_library_maintenance as helper  # noqa: E402
from core import library_maintenance as m  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sealed_backup(tmp_path, *, jobs=2, wal=False, mode=None, label="sealed"):
    source = helper.make_library(
        tmp_path / "lib" / "library.sqlite3", jobs=jobs, wal=wal
    )
    if mode is None:
        manifest = m.backup_library(source, tmp_path / "backups", label=label)
    else:
        manifest = m.backup_library(
            source, tmp_path / "backups", label=label, mode=mode
        )
    return Path(manifest["manifest_path"]).parent


def tree(directory):
    """Size, digest and mtime of everything under one directory."""
    facts = {}
    for path in sorted(Path(directory).rglob("*")):
        if path.is_file():
            info = path.stat()
            facts[str(path.relative_to(directory))] = (
                info.st_size,
                m._sha256_file(path),
                info.st_mtime_ns,
            )
    return facts


def read_manifest(directory):
    return (Path(directory) / "manifest.json").read_text(encoding="utf-8")


def write_manifest(directory, payload):
    (Path(directory) / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def append_wal(directory, name="library.sqlite3-wal", extra=b"\x00\x00\x00\x00"):
    with (Path(directory) / name).open("ab") as stream:
        stream.write(extra)


# ---------------------------------------------------------------------------
# A1: every fact describes one isolated copy
# ---------------------------------------------------------------------------


def test_a_sealed_backup_is_verified_from_one_isolated_copy(tmp_path):
    directory = sealed_backup(tmp_path)
    before = tree(directory)
    result = m.verify_backup(directory)
    evidence = result["evidence"]
    assert result["ok"] is True
    assert result["sealed"]["state"] == "ok"
    assert result["logical"]["state"] == "ok"
    assert result["health"]["state"] == "ok"
    assert result["compatible"]["state"] == "ok"
    assert result["reasons"] == []
    assert evidence["scope"] == "isolated_copy"
    assert evidence["workspace"] in {"sibling", "system_temp"}
    assert evidence["stability"] == "stable"
    assert evidence["cleanup"] == "ok"
    assert int(evidence["copy_bytes"]) > 0
    assert float(evidence["copy_seconds"]) >= 0.0
    assert float(evidence["hash_seconds"]) >= 0.0
    assert len(str(evidence["snapshot_id"])) == 32
    assert evidence["source_state_before"] == evidence["source_state_after"]
    # The verified directory is never written, and the copy never survives.
    assert tree(directory) == before
    assert not Path(str(evidence["path"])).exists()


def test_a_write_during_the_copy_is_refused_as_changed(tmp_path, monkeypatch):
    directory = sealed_backup(tmp_path)
    copy = directory / "library.sqlite3"
    original = m._copy_and_hash
    state = {"wrote": False}

    def copy_then_write(source, destination):
        fact = original(source, destination)
        if not state["wrote"]:
            state["wrote"] = True
            with sqlite3.connect(copy, isolation_level=None) as connection:
                connection.execute("UPDATE jobs SET state='cancelled'")
        return fact

    monkeypatch.setattr(m, "_copy_and_hash", copy_then_write)
    result = m.verify_backup(directory)
    monkeypatch.setattr(m, "_copy_and_hash", original)

    assert state["wrote"] is True
    assert result["ok"] is False
    assert "changed_during_check" in result["reasons"]
    assert result["sealed"]["state"] == "changed"
    assert result["evidence"]["stability"] == "changed"
    assert not Path(str(result["evidence"]["path"])).exists()
    # The next check sees the new content as an ordinary mismatch.
    second = m.verify_backup(directory)
    assert second["ok"] is False
    assert second["sealed"]["state"] == "mismatch"
    assert second["logical"]["state"] == "mismatch"
    assert second["logical"]["tables_differing"] == ["jobs"]


def test_a_write_made_inside_the_check_is_reported_as_changed(tmp_path, monkeypatch):
    """The audit's PA-01 shape, injected at the stage that now exists.

    The wrapper answers for the isolated copy, so the write it performs lands
    on the verified directory while the check is still running.  The last
    stability look sees it and no four facts are pooled from two versions.
    """
    directory = sealed_backup(tmp_path)
    original = m.inspect_library
    state = {"wrote": False}

    def inspect(path, **kwargs):
        if kwargs.get("connection") is not None and not state["wrote"]:
            state["wrote"] = True
            with sqlite3.connect(
                directory / "library.sqlite3", isolation_level=None
            ) as connection:
                connection.execute("UPDATE jobs SET state='cancelled'")
        return original(path, **kwargs)

    monkeypatch.setattr(m, "inspect_library", inspect)
    result = m.verify_backup(directory)
    monkeypatch.setattr(m, "inspect_library", original)
    assert state["wrote"] is True
    assert result["ok"] is False
    assert "changed_during_check" in result["reasons"]
    assert result["sealed"]["state"] == "changed"
    assert result["evidence"]["stability"] == "changed"


def test_a_write_between_two_checks_is_caught_by_the_second_one(tmp_path):
    directory = sealed_backup(tmp_path)
    assert m.verify_backup(directory)["ok"] is True
    with sqlite3.connect(directory / "library.sqlite3", isolation_level=None) as conn:
        conn.execute("UPDATE jobs SET state='cancelled'")
    second = m.verify_backup(directory)
    assert second["ok"] is False
    assert second["logical"]["state"] == "mismatch"
    assert second["logical"]["tables_differing"] == ["jobs"]


def test_an_equal_length_rewrite_is_a_mismatch(tmp_path):
    directory = sealed_backup(tmp_path)
    with sqlite3.connect(directory / "library.sqlite3", isolation_level=None) as conn:
        conn.execute("UPDATE jobs SET request_json='[]'")
    result = m.verify_backup(directory)
    assert result["ok"] is False
    assert result["sealed"]["state"] == "mismatch"
    assert result["logical"]["state"] == "mismatch"
    assert result["logical"]["rows_recorded"] == result["logical"]["rows_actual"]


def test_replacing_the_database_at_the_same_path_is_a_mismatch(tmp_path):
    directory = sealed_backup(tmp_path, jobs=1)
    other = sealed_backup(tmp_path / "other", jobs=4, label="other")
    assert m.verify_backup(directory)["ok"] is True
    os.replace(other / "library.sqlite3", directory / "library.sqlite3")
    result = m.verify_backup(directory)
    assert result["ok"] is False
    assert result["sealed"]["state"] == "mismatch"
    assert result["logical"]["state"] == "mismatch"
    assert result["logical"]["tables_differing"] == ["jobs"]


def test_no_single_generation_can_satisfy_an_inconsistent_manifest(tmp_path, monkeypatch):
    directory = sealed_backup(tmp_path, jobs=1)
    other = helper.make_library(tmp_path / "other.sqlite3", jobs=1)
    with sqlite3.connect(other, isolation_level=None) as connection:
        connection.execute("UPDATE jobs SET state='cancelled'")
    payload = json.loads(read_manifest(directory))
    payload["logical_fingerprint"] = m.logical_fingerprint(other)
    write_manifest(directory, payload)
    # Sealed bytes come from one version, the recorded content from another.
    assert m.verify_backup(directory)["ok"] is False
    original = m._copy_and_hash

    def copy_then_adopt(source, destination):
        fact = original(source, destination)
        with sqlite3.connect(
            directory / "library.sqlite3", isolation_level=None
        ) as connection:
            connection.execute("UPDATE jobs SET state='cancelled'")
        return fact

    monkeypatch.setattr(m, "_copy_and_hash", copy_then_adopt)
    result = m.verify_backup(directory)
    monkeypatch.setattr(m, "_copy_and_hash", original)
    assert result["ok"] is False
    assert result["sealed"]["state"] in {"changed", "mismatch"}
    assert result["logical"]["state"] in {"mismatch", "not_checked"}


def test_the_copy_falls_back_to_the_system_temp_directory(tmp_path, monkeypatch):
    directory = sealed_backup(tmp_path)
    real_mkdir = Path.mkdir

    def refusing(self, *args, **kwargs):
        if ".verify-" in self.name:
            raise PermissionError("the volume is read-only")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refusing)
    result = m.verify_backup(directory)
    monkeypatch.setattr(Path, "mkdir", real_mkdir)
    assert result["ok"] is True
    assert result["evidence"]["workspace"] == "system_temp"
    assert result["evidence"]["cleanup"] == "ok"
    assert not Path(str(result["evidence"]["path"])).exists()


def test_a_workspace_that_cannot_be_created_is_not_a_pass(tmp_path, monkeypatch):
    directory = sealed_backup(tmp_path)

    def unavailable(root):
        raise OSError("no writable place")

    monkeypatch.setattr(m, "_verification_workspace", unavailable)
    result = m.verify_backup(directory)
    assert result["ok"] is False
    assert "verify_workspace_unavailable" in result["reasons"]
    assert result["evidence"]["workspace"] == "unavailable"
    assert result["sealed"]["state"] == "not_checked"
    assert result["logical"]["state"] == "not_checked"
    assert result["health"]["state"] == "not_checked"
    assert result["compatible"]["state"] == "not_checked"


def test_an_unreadable_member_is_one_typed_refusal(tmp_path, monkeypatch):
    directory = sealed_backup(tmp_path)

    def refuse(source, destination):
        raise PermissionError("the file is open in another program")

    monkeypatch.setattr(m, "_copy_and_hash", refuse)
    with pytest.raises(m.LibraryMaintenanceError) as caught:
        m.verify_backup(directory)
    assert caught.value.code == "library_unavailable"
    assert "library.sqlite3" in caught.value.detail
    assert not [item for item in directory.parent.iterdir() if ".verify-" in item.name]


def test_an_interrupted_copy_removes_the_workspace(tmp_path, monkeypatch):
    directory = sealed_backup(tmp_path)
    seen = {}

    def interrupt(source, destination):
        seen["workspace"] = destination.parent
        raise KeyboardInterrupt()

    monkeypatch.setattr(m, "_copy_and_hash", interrupt)
    with pytest.raises(KeyboardInterrupt):
        m.verify_backup(directory)
    assert "workspace" in seen
    assert not seen["workspace"].exists()


def test_the_text_report_names_the_copy_and_its_boundary(tmp_path):
    directory = sealed_backup(tmp_path)
    result = m.verify_backup(directory)
    text = "\n".join(m._verify_lines(result))
    assert "隔离副本" in text
    assert "边界:" in text
    assert "结论: ok=True" in text


# ---------------------------------------------------------------------------
# A1: the sealed group is taken as a group
# ---------------------------------------------------------------------------


def test_a_commit_that_lives_only_in_the_wal_is_part_of_the_sealed_group(tmp_path):
    library = helper.library_with_pending_wal(tmp_path)
    manifest = m.backup_library(
        library, tmp_path / "backups", label="wal", mode="file_group"
    )
    directory = Path(manifest["manifest_path"]).parent
    assert manifest["sealed_members"] == ["library.sqlite3", "library.sqlite3-wal"]
    result = m.verify_backup(directory)
    assert result["ok"] is True
    assert result["logical"]["rows_actual"]["jobs"] == 2
    assert result["logical"]["rows_recorded"]["jobs"] == 2
    # The WAL carries content, so it is sealed and touching it fails the check.
    append_wal(directory)
    second = m.verify_backup(directory)
    assert second["ok"] is False
    assert second["sealed"]["state"] == "mismatch"


def test_losing_a_sealed_member_is_missing_not_a_crash(tmp_path):
    library = helper.library_with_pending_wal(tmp_path)
    manifest = m.backup_library(
        library, tmp_path / "backups", label="fg", mode="file_group"
    )
    directory = Path(manifest["manifest_path"]).parent
    (directory / "library.sqlite3-wal").unlink()
    result = m.verify_backup(directory)
    assert result["ok"] is False
    assert result["sealed"]["state"] == "missing"
    assert "sealed_missing" in result["reasons"]


def test_a_stranger_in_the_backup_is_reported_and_never_deleted(tmp_path):
    directory = sealed_backup(tmp_path)
    stranger = directory / "notes.txt"
    stranger.write_text("not part of the backup", encoding="utf-8")
    result = m.verify_backup(directory)
    assert result["ok"] is True
    assert "unexpected_members" in result["warnings"]
    assert result["evidence"]["unexpected_members"] == ["notes.txt"]
    assert stranger.exists()


# ---------------------------------------------------------------------------
# A2: the internal-object test is literal, not a wildcard
# ---------------------------------------------------------------------------


def test_the_internal_object_test_is_literal_and_case_insensitive():
    assert m._is_internal_object("sqlite_sequence") is True
    assert m._is_internal_object("SQLITE_stat1") is True
    assert m._is_internal_object("sqliteXuser") is False
    assert m._is_internal_object("sqlite9user") is False
    assert m._is_internal_object("my_sqlite_table") is False
    # SQLite reserves the prefix in any case, so this name is internal too; the
    # point of the test is that the underscore is a literal, not a wildcard.
    assert m._is_internal_object("Sqlite_users") is True


def test_a_legal_sqlite_similar_user_table_is_fingerprinted(tmp_path):
    one = helper.make_library(tmp_path / "one.sqlite3", jobs=1)
    two = helper.make_library(tmp_path / "two.sqlite3", jobs=1)
    for path, value in ((one, "before"), (two, "after")):
        with sqlite3.connect(path, isolation_level=None) as connection:
            connection.execute("CREATE TABLE sqliteXuser (value TEXT)")
            connection.execute("INSERT INTO sqliteXuser VALUES (?)", (value,))
    first = m.logical_fingerprint(one)
    second = m.logical_fingerprint(two)
    assert "sqliteXuser" in first["tables"]
    assert first["rows"]["sqliteXuser"] == 1
    assert first["digest"] != second["digest"]
    result = m.compare_library(one, two)
    assert result["ok"] is False
    assert result["logical"]["tables_differing"] == ["sqliteXuser"]


def test_a_backup_of_a_library_with_such_a_table_is_still_verifiable(tmp_path):
    source = helper.make_library(tmp_path / "lib" / "library.sqlite3", jobs=1)
    with sqlite3.connect(source, isolation_level=None) as connection:
        connection.execute("CREATE TABLE sqlite9user (value TEXT)")
        connection.execute("INSERT INTO sqlite9user VALUES ('kept')")
    manifest = m.backup_library(source, tmp_path / "backups", label="extra")
    directory = Path(manifest["manifest_path"]).parent
    assert "sqlite9user" in manifest["logical_fingerprint"]["tables"]
    result = m.verify_backup(directory)
    assert result["ok"] is True
    with sqlite3.connect(directory / "library.sqlite3", isolation_level=None) as conn:
        conn.execute("UPDATE sqlite9user SET value='changed'")
    assert m.verify_backup(directory)["ok"] is False


# ---------------------------------------------------------------------------
# A3: the manifest is validated before anything is opened
# ---------------------------------------------------------------------------


def test_a_seal_that_skips_the_database_is_refused(tmp_path):
    directory = sealed_backup(tmp_path)
    data = b"bookkeeping"
    (directory / "marker.bin").write_bytes(data)
    payload = json.loads(read_manifest(directory))
    payload["members"].append(
        {"name": "marker.bin", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    )
    payload["sealed_members"] = ["marker.bin"]
    write_manifest(directory, payload)
    with pytest.raises(m.LibraryMaintenanceError) as caught:
        m.verify_backup(directory)
    assert caught.value.code == "manifest_member_missing"
    assert "not sealed" in caught.value.detail


def test_a_member_size_that_is_not_a_byte_count_is_a_typed_refusal(tmp_path):
    directory = sealed_backup(tmp_path)
    original = read_manifest(directory)
    payload = json.loads(original)
    payload["members"][0]["bytes"] = "not-an-integer"
    write_manifest(directory, payload)
    with pytest.raises(m.BackupError) as caught:
        m.verify_backup(directory)
    assert caught.value.code == "manifest_unreadable"
    assert "member[0].bytes" in caught.value.detail
    (directory / "manifest.json").write_text(original, encoding="utf-8")


def test_a_malformed_digest_bool_size_and_mode_are_refused(tmp_path):
    directory = sealed_backup(tmp_path)
    original = read_manifest(directory)
    for change, expected in (
        ({"sha256": "zz"}, "member[0].sha256"),
        ({"bytes": True}, "member[0].bytes"),
    ):
        payload = json.loads(original)
        payload["members"][0].update(change)
        write_manifest(directory, payload)
        with pytest.raises(m.BackupError) as caught:
            m.verify_backup(directory)
        assert caught.value.code == "manifest_unreadable"
        assert expected in caught.value.detail
    payload = json.loads(original)
    payload["mode"] = "copied"
    write_manifest(directory, payload)
    with pytest.raises(m.BackupError) as caught:
        m.verify_backup(directory)
    assert caught.value.code == "manifest_unreadable"
    assert "mode" in caught.value.detail


def test_a_manifest_without_a_sealed_member_cannot_pass(tmp_path):
    directory = sealed_backup(tmp_path)
    payload = json.loads(read_manifest(directory))
    payload["sealed_members"] = []
    write_manifest(directory, payload)
    result = m.verify_backup(directory)
    assert result["ok"] is False
    assert result["sealed"]["state"] == "not_checked"
    assert "sealed_not_checked" in result["reasons"]
    assert "manifest_without_sealed_members" in result["warnings"]


def test_the_shared_memory_file_can_never_be_sealed(tmp_path):
    library = helper.library_with_pending_wal(tmp_path)
    manifest = m.backup_library(
        library, tmp_path / "backups", label="shm", mode="file_group"
    )
    directory = Path(manifest["manifest_path"]).parent
    payload = json.loads(read_manifest(directory))
    payload["sealed_members"] = ["library.sqlite3", "library.sqlite3-shm"]
    write_manifest(directory, payload)
    with pytest.raises(m.LibraryMaintenanceError) as caught:
        m.verify_backup(directory)
    assert caught.value.code == "manifest_unreadable"
    assert "shared-memory" in caught.value.detail


def test_an_online_manifest_cannot_seal_a_wal_with_frames(tmp_path):
    directory = sealed_backup(tmp_path)
    payload = json.loads(read_manifest(directory))
    payload["members"].append(
        {"name": "library.sqlite3-wal", "bytes": 4, "sha256": "0" * 64}
    )
    payload["sealed_members"].append("library.sqlite3-wal")
    write_manifest(directory, payload)
    with pytest.raises(m.BackupError) as caught:
        m.verify_backup(directory)
    assert caught.value.code == "manifest_unreadable"
    assert "self-contained" in caught.value.detail


def test_a_forged_fingerprint_shape_is_refused(tmp_path):
    directory = sealed_backup(tmp_path)
    original = read_manifest(directory)
    for change, expected in (
        ({"version": 7}, "version"),
        ({"digest": "not-a-digest"}, "digest"),
        ({"tables": []}, "tables"),
    ):
        payload = json.loads(original)
        payload["logical_fingerprint"].update(change)
        write_manifest(directory, payload)
        with pytest.raises(m.BackupError) as caught:
            m.verify_backup(directory)
        assert caught.value.code == "manifest_unreadable"
        assert expected in caught.value.detail
