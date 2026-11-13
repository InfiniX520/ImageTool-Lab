"""The maintain.py scan subcommand: read-only and honest about findings."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from core.library_maintenance import main
from core.library_repository import LibraryRepository


def _empty_library(root):
    """Create a real schema at <root>/download_logs/library.sqlite3 and close it."""
    database = root / "download_logs" / "library.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    repository = LibraryRepository(database)
    try:
        assert repository._ready.wait(30)
        assert repository._init_error is None
    finally:
        repository.close(timeout=30)
    return database


def _insert_artifact(database, relative_path, data, *, recorded_sha=None, recorded_size=None):
    """Insert one artifact row with foreign keys left off (synthetic ledger)."""
    digest = hashlib.sha256(data).hexdigest() if recorded_sha is None else recorded_sha
    size = len(data) if recorded_size is None else recorded_size
    stamp = datetime.now(timezone.utc).isoformat()
    reference = json.dumps(
        {"kind": "project", "relative_path": relative_path, "root_id": "project"},
        sort_keys=True,
        separators=(",", ":"),
    )
    connection = sqlite3.connect(str(database))
    try:
        connection.execute(
            "INSERT INTO artifacts (artifact_id, delivery_id, job_id, illust_id, page_index,"
            " artist_id, output_key, output_ref_json, quality_json, media_type, size, sha256,"
            " verification, completed_at, legacy_origin, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "artifact-1",
                "delivery-1",
                "job-1",
                "999",
                0,
                "7",
                "key-1",
                reference,
                "{}",
                "image/jpeg",
                size,
                digest,
                "structure_checked",
                stamp,
                None,
                stamp,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _write_file(root, relative_path, data):
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def test_a_matching_file_is_reported_and_exits_zero(tmp_path, capsys):
    database = _empty_library(tmp_path)
    data = b"\xff\xd8matching bytes\xff\xd9"
    _write_file(tmp_path, "downl_Pixiv/artist/1_p0.jpg", data)
    _insert_artifact(database, "downl_Pixiv/artist/1_p0.jpg", data)

    code = main(["scan", str(database), "--json"])

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert [row["verdict"] for row in report["rows"]] == ["match"]
    assert report["rows"][0]["reason"] == "full_sha256_match"
    assert report["stats"]["matched"] == 1
    assert report["truncated"] is False


def test_a_missing_file_is_a_finding_and_exits_two(tmp_path, capsys):
    database = _empty_library(tmp_path)
    _insert_artifact(database, "downl_Pixiv/artist/2_p0.jpg", b"\xff\xd8gone\xff\xd9")

    code = main(["scan", str(database), "--json"])

    assert code == 2
    report = json.loads(capsys.readouterr().out)
    assert report["rows"][0]["verdict"] == "missing"
    assert report["stats"]["missing"] == 1


def test_a_same_size_rewrite_is_a_finding_and_exits_two(tmp_path, capsys):
    database = _empty_library(tmp_path)
    original = b"\xff\xd8original\xff\xd9"
    _write_file(tmp_path, "downl_Pixiv/artist/3_p0.jpg", b"\xff\xd8tampered\xff\xd9")
    _insert_artifact(database, "downl_Pixiv/artist/3_p0.jpg", original)

    code = main(["scan", str(database), "--json"])

    assert code == 2
    report = json.loads(capsys.readouterr().out)
    row = report["rows"][0]
    assert row["verdict"] == "changed"
    assert row["size_observed"] == row["size_recorded"]
    # The observed digest is real evidence for a rewrite, and it must differ
    # from the recorded one: same size is not the same bytes.
    assert row["observed_sha256"] is not None
    assert row["observed_sha256"] != row["sha256_recorded"]


def test_the_root_defaults_to_the_project_directory(tmp_path, capsys):
    database = _empty_library(tmp_path)
    data = b"\xff\xd8default root\xff\xd9"
    _write_file(tmp_path, "downl_Pixiv/artist/4_p0.jpg", data)
    _insert_artifact(database, "downl_Pixiv/artist/4_p0.jpg", data)

    code = main(["scan", str(database), "--json"])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["rows"][0]["verdict"] == "match"


def test_the_text_output_names_the_counts_and_the_verdict(tmp_path, capsys):
    database = _empty_library(tmp_path)
    data = b"\xff\xd8text mode\xff\xd9"
    _write_file(tmp_path, "downl_Pixiv/artist/5_p0.jpg", data)
    _insert_artifact(database, "downl_Pixiv/artist/5_p0.jpg", data)

    code = main(["scan", str(database)])

    printed = capsys.readouterr().out
    assert code == 0
    assert "核验: 1 行" in printed
    assert "每一行都与记录一致" in printed


def test_a_malformed_cursor_is_refused_instead_of_ignored(tmp_path, capsys):
    database = _empty_library(tmp_path)

    code = main(["scan", str(database), "--after", "not-a-cursor", "--json"])

    assert code == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "scan_cursor_invalid"


def test_an_out_of_range_limit_is_refused_not_a_crash(tmp_path, capsys):
    database = _empty_library(tmp_path)

    assert main(["scan", str(database), "--limit", "0", "--json"]) == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "invalid_argument"

    assert main(["scan", str(database), "--limit", "500", "--json"]) == 4
    assert json.loads(capsys.readouterr().out)["code"] == "invalid_argument"


def test_a_negative_budget_is_refused_not_a_crash(tmp_path, capsys):
    database = _empty_library(tmp_path)

    code = main(["scan", str(database), "--max-bytes", "-1", "--json"])

    assert code == 4
    assert json.loads(capsys.readouterr().out)["code"] == "invalid_argument"


def test_unverifiable_rows_are_counted_out_loud(tmp_path, capsys):
    database = _empty_library(tmp_path)
    data = b"\xff\xd8too big for the budget\xff\xd9"
    _write_file(tmp_path, "downl_Pixiv/artist/6_p0.jpg", data)
    _insert_artifact(database, "downl_Pixiv/artist/6_p0.jpg", data)

    code = main(["scan", str(database), "--max-bytes", "1"])

    printed = capsys.readouterr().out
    assert code == 0
    assert "无法核验 1" in printed
    assert "另有 1 行没有可核验的记录" in printed


def test_an_unreadable_ledger_exits_three(tmp_path, capsys):
    database = _empty_library(tmp_path)
    database.write_bytes(b"not a database at all")

    code = main(["scan", str(database), "--json"])

    assert code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
