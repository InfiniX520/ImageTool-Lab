"""WR-01: a restore must always keep a way back (synthetic drill).

The generated instructions are executed here as plain code against synthetic
directories: verify the backup, prepare the matching group in a staging
directory, move the current group into a same-volume retention directory, put
the staged group in place, reopen and compare, and put the old group back when
the replacement turns out to be wrong.  Nothing here touches a real library,
gallery, account or network.

The behaviour under test is contract v6 section 2.3 plus amendment A4: the old
"overwrite the database and delete the -wal/-shm beside it" card is forbidden.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_u1a_library_maintenance as helper  # noqa: E402
from core import library_maintenance as m  # noqa: E402


def siblings(library):
    """The existing members of one file group, keyed by suffix."""
    found = {}
    for suffix in ("", "-wal", "-shm"):
        item = Path(str(library) + suffix)
        if item.exists():
            found[suffix] = m._sha256_file(item)
    return found


def test_the_online_protocol_keeps_the_old_group_and_restores_the_snapshot(tmp_path):
    library = helper.library_with_pending_wal(tmp_path)
    manifest = m.backup_library(library, tmp_path / "backups", label="online")
    directory = Path(manifest["manifest_path"]).parent
    assert m.verify_backup(directory)["ok"] is True
    sealed = directory / library.name

    # Step 1: no writer holds the database.
    assert m.inspect_library(library)["status"] == "compatible"

    # Step 3: prepare the matching group in a staging directory and prove it.
    staging = tmp_path / "staging"
    staging.mkdir()
    shutil.copy2(sealed, staging / library.name)
    assert m.restore_check(staging / library.name)["state"] == "ok"
    assert m.compare_library(staging / library.name, sealed)["ok"] is True

    # Step 4: move the current group into a retention directory, never delete.
    retention = tmp_path / "retention"
    retention.mkdir()
    retained = siblings(library)
    for suffix in retained:
        os.replace(Path(str(library) + suffix), retention / (library.name + suffix))
    for suffix in retained:
        assert not Path(str(library) + suffix).exists()
        assert m._sha256_file(retention / (library.name + suffix)) == retained[suffix]

    # Step 5: put the verified group in place (the snapshot is one file).
    os.replace(staging / library.name, library)

    # Step 6: reopen and compare with the sealed copy.
    assert m.inspect_library(library)["status"] == "compatible"
    restored = m.compare_library(library, sealed)
    assert restored["ok"] is True
    assert restored["logical"]["state"] == "ok"
    assert m.restore_check(library)["state"] == "ok"

    # Step 7: nothing was deleted, and the old group is still complete.
    kept = sorted(item.name for item in retention.iterdir())
    assert kept == sorted(library.name + suffix for suffix in retained)


def test_a_leftover_wal_beside_a_restored_snapshot_is_refused(tmp_path):
    library = helper.library_with_pending_wal(tmp_path)
    manifest = m.backup_library(library, tmp_path / "backups", label="online")
    sealed = Path(manifest["manifest_path"]).parent / library.name
    restored = tmp_path / "restored" / library.name
    restored.parent.mkdir()
    shutil.copy2(sealed, restored)
    alone = m.restore_check(restored)
    assert alone["state"] == "ok"
    assert alone["wal_mode"] is False
    assert alone["header_journal"] == [1, 1]
    # The mixed group the protocol forbids: a new self-contained database with
    # the old transaction log beside it.
    shutil.copy2(Path(str(library) + "-wal"), Path(str(restored) + "-wal"))
    result = m.restore_check(restored)
    assert result["state"] == "orphan_wal"
    assert result["reasons"] == ["orphan_wal"]
    assert result["wal_mode"] is False
    assert int(result["wal_bytes"]) > 0


def test_a_file_group_restore_needs_the_whole_sealed_group(tmp_path):
    library = helper.library_with_pending_wal(tmp_path)
    manifest = m.backup_library(
        library, tmp_path / "backups", label="fg", mode="file_group"
    )
    directory = Path(manifest["manifest_path"]).parent
    assert m.verify_backup(directory)["ok"] is True
    sealed = directory / library.name
    staged = tmp_path / "staged"
    staged.mkdir()
    for name in ("library.sqlite3", "library.sqlite3-wal"):
        shutil.copy2(directory / name, staged / name)

    # Only the main file: the frames that live in the WAL are gone, and the
    # comparison against the sealed content says so.
    partial = tmp_path / "partial" / library.name
    partial.parent.mkdir()
    shutil.copy2(staged / library.name, partial)
    assert m.compare_library(partial, sealed)["ok"] is False

    # The whole group is one consistent state and does compare equal.
    full = tmp_path / "full" / library.name
    full.parent.mkdir()
    shutil.copy2(staged / library.name, full)
    shutil.copy2(staged / "library.sqlite3-wal", Path(str(full) + "-wal"))
    assert m.restore_check(full)["state"] == "ok"
    assert m.compare_library(full, sealed)["ok"] is True


def test_an_open_exclusive_writer_is_detected_before_anything_moves(tmp_path):
    library = helper.make_library(tmp_path / "lib" / "library.sqlite3", jobs=1)
    writer = sqlite3.connect(library, isolation_level=None)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        report = m.inspect_library(library)
        assert report["status"] == "unreadable"
        assert "library_locked" in report["warnings"]
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert m.inspect_library(library)["status"] == "compatible"


def test_the_generated_steps_verify_before_they_move_anything(tmp_path):
    library = helper.make_library(tmp_path / "lib" / "library.sqlite3", jobs=1)
    manifest = m.backup_library(library, tmp_path / "backups", label="steps")
    steps = manifest["restore_steps"]
    text = "\n".join(steps)
    assert text.index("verify") < text.index("保留目录")
    assert "不删除" in text
    assert ("新主库配旧 WAL" in text) or ("不得再留 -wal" in text)
    assert "一并删除" not in text
    plan_text = "\n".join(m.switch_plan(library)["steps"])
    assert "保留目录" in plan_text
    assert "一并删除" not in plan_text


def test_a_failed_restore_can_put_the_retained_group_back(tmp_path):
    library = helper.library_with_pending_wal(tmp_path)
    before = siblings(library)
    retention = tmp_path / "retention"
    retention.mkdir()
    for suffix in before:
        os.replace(Path(str(library) + suffix), retention / (library.name + suffix))
    # The staged replacement turns out to be wrong: the protocol puts back the
    # group it moved aside, byte for byte.
    library.write_bytes(b"not a database any more")
    for suffix in before:
        os.replace(retention / (library.name + suffix), Path(str(library) + suffix))
    assert siblings(library) == before
    assert m.inspect_library(library)["status"] == "compatible"
    assert not any(retention.iterdir())
