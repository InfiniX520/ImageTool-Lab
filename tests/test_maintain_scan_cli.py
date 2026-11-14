"""The maintain.py scan subcommand: read-only and honest about findings."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core import library_maintenance as maintenance
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
    # Contract v6 section 3.2 replaced the page-level "truncated" with the
    # walk vocabulary: coverage is the answer and resume says whether the
    # prefix is complete.
    assert report["coverage"] == "complete"
    assert report["scope"] == "full_walk"
    assert report["resume"]["complete_prefix"] is True


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
    # Contract v5 wording: coverage is stated, not implied.
    assert "核验: 覆盖 1 行（匹配 1，排除 0，无法核验 0）" in printed
    assert "本次核验覆盖全部 1 行，全部与记录一致" in printed


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
    # Contract v5: a row nobody could read is not "all verified", and the exit
    # code says so (5 = not covered / unverifiable).  The engine's own reason is
    # reported verbatim instead of being relabelled as a legacy row.
    assert code == 5
    assert "无法核验: file_exceeds_budget: 1" in printed
    assert "本次只核验了 0/1 行" in printed
    assert "每一行都与记录一致" not in printed
    assert "旧行或缺摘要" not in printed


def test_a_continuation_past_the_end_is_not_an_empty_library(tmp_path, capsys):
    """A cursor at the end says "nothing left", never "no delivered work" here."""
    database = _empty_library(tmp_path)
    data = b"\xff\xd8only row\xff\xd9"
    _write_file(tmp_path, "downl_Pixiv/artist/7_p0.jpg", data)
    _insert_artifact(database, "downl_Pixiv/artist/7_p0.jpg", data)
    connection = sqlite3.connect(str(database))
    try:
        completed_at, artifact_id = connection.execute(
            "SELECT completed_at, artifact_id FROM artifacts").fetchone()
    finally:
        connection.close()
    cursor = "%s,%s" % (completed_at, artifact_id)

    assert main(["scan", str(database)]) == 0
    capsys.readouterr()

    code = main(["scan", str(database), "--after", cursor])

    printed = capsys.readouterr().out
    # Contract v6 section 3.2 / amendment A5: a bare cursor is a continuation,
    # so even a cursor that sits at the very end cannot produce 0.
    assert code == 5
    assert "游标已在末尾" in printed
    assert "范围内" not in printed
    assert "库里没有已登记的成品" not in printed


def test_an_unreadable_ledger_exits_three(tmp_path, capsys):
    database = _empty_library(tmp_path)
    database.write_bytes(b"not a database at all")

    code = main(["scan", str(database), "--json"])

    assert code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"


def _backup_of(tmp_path, database):
    return maintenance.backup_library(database, tmp_path / "backups", label="cli")


def _insert_legacy_artifact(database, relative_path, data):
    """A pre-digest row: size recorded, sha256 NULL, so nobody can verify it."""
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
                "legacy-1", "delivery-1", "job-1", "999", 0, "7", "key-1",
                reference, "{}", "image/jpeg", len(data), None,
                "structure_checked", stamp, None, stamp,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_scan_exits_zero_for_an_empty_library(tmp_path, capsys):
    database = _empty_library(tmp_path)

    code = main(["scan", str(database)])

    printed = capsys.readouterr().out
    assert code == 0
    assert "库里没有已登记的成品可核验" in printed


def test_scan_allow_unknown_only_skips_legacy_rows(tmp_path, capsys):
    database = _empty_library(tmp_path)
    data = b"\xff\xd8legacy row\xff\xd9"
    _write_file(tmp_path, "downl_Pixiv/artist/8_p0.jpg", data)
    _insert_legacy_artifact(database, "downl_Pixiv/artist/8_p0.jpg", data)

    assert main(["scan", str(database)]) == 5
    printed = capsys.readouterr().out
    assert "无法核验: no_recorded_digest: 1" in printed
    assert "每一行都与记录一致" not in printed

    code = main(["scan", str(database), "--allow-unknown"])
    printed = capsys.readouterr().out
    assert code == 0
    assert "已按 --allow-unknown 跳过 1 行无摘要旧行" in printed


def test_verify_cli_accepts_a_fresh_backup(tmp_path, capsys):
    database = _empty_library(tmp_path)
    manifest = _backup_of(tmp_path, database)
    directory = Path(manifest["manifest_path"]).parent

    code = main(["verify", str(directory), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["sealed"]["state"] == "ok"
    assert payload["logical"]["state"] == "ok"
    assert payload["manifest_version"] == 2


def test_verify_cli_exits_two_for_a_tampered_backup(tmp_path, capsys):
    database = _empty_library(tmp_path)
    manifest = _backup_of(tmp_path, database)
    directory = Path(manifest["manifest_path"]).parent
    copy = directory / "library.sqlite3"
    data = bytearray(copy.read_bytes())
    data[-1] = data[-1] ^ 0xFF
    copy.write_bytes(bytes(data))

    code = main(["verify", str(directory)])

    printed = capsys.readouterr().out
    assert code == 2
    assert "结论: ok=False" in printed
    assert "封存校验 mismatch" in printed


def test_verify_cli_exits_three_for_a_missing_backup_directory(tmp_path, capsys):
    code = main(["verify", str(tmp_path / "absent"), "--json"])

    assert code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "backup_directory_missing"


def test_verify_cli_two_paths_is_a_deprecated_compare(tmp_path, capsys):
    database = _empty_library(tmp_path)
    second = _empty_library(tmp_path / "other")

    code = main(["verify", str(database), str(second)])

    printed = capsys.readouterr().out
    assert code == 0
    assert "已弃用" in printed
    assert "结论: ok=True" in printed


def test_compare_cli_exits_two_for_a_changed_copy(tmp_path, capsys):
    database = _empty_library(tmp_path)
    data = b"\xff\xd8compare row\xff\xd9"
    _write_file(tmp_path, "downl_Pixiv/artist/9_p0.jpg", data)
    _insert_artifact(database, "downl_Pixiv/artist/9_p0.jpg", data)
    other = tmp_path / "other" / "library.sqlite3"
    other.parent.mkdir()
    shutil.copy2(database, other)
    connection = sqlite3.connect(other)
    try:
        connection.execute("UPDATE artifacts SET size = size + 1")
        connection.commit()
    finally:
        connection.close()

    code = main(["compare", str(database), str(other)])

    printed = capsys.readouterr().out
    assert code == 2
    assert "内容一致=False" in printed
    assert "copy_content_mismatch" in printed


def test_scan_lines_say_no_work_only_when_the_ledger_is_empty():
    """Coverage "empty" can be a cursor at the end, not an empty library."""
    empty_ledger = {
        "stats": {"library_rows": 0, "examined": 0, "matched": 0, "unknown": 0},
        "rows": [],
        "coverage": "complete",
    }
    assert "库里没有已登记的成品可核验" in maintenance._scan_lines(empty_ledger)[0]

    finished_cursor = {
        "stats": {"library_rows": 5, "examined": 0, "matched": 0, "unknown": 0},
        "rows": [],
        "coverage": "empty",
    }
    printed = "\n".join(maintenance._scan_lines(finished_cursor))
    assert "库里没有已登记的成品可核验" not in printed
    assert "每一行都与记录一致" not in printed


def test_scan_lines_report_the_ledger_error_instead_of_a_verdict():
    report = {
        "stats": {"examined": 0, "matched": 0, "unknown": 0},
        "rows": [],
        "coverage": "error",
        "ledger": {"state": "error", "code": "ledger_unreadable",
                   "last_safe_cursor": ["2026-11-14T00:00:00+00:00", "artifact-1"]},
    }
    printed = "\n".join(maintenance._scan_lines(report))
    assert "账本读不出来" in printed
    assert "最后安全游标: 2026-11-14T00:00:00+00:00,artifact-1" in printed
    assert "本次没有给出任何核验结论" in printed
    assert "一致" not in printed


def test_scan_outcome_treats_coverage_error_as_a_ledger_failure():
    report = {"coverage": "error", "stats": {}, "rows": []}
    code, findings = maintenance._scan_outcome(report, allow_unknown=False)
    assert code == 3
    assert findings == []


def test_scan_outcome_requires_complete_coverage_before_allow_unknown():
    report = {
        "coverage": "page",
        "rows": [{"verdict": "unknown", "reason": "no_recorded_digest"}],
        "stats": {"examined": 1, "matched": 0, "unknown": 1,
                  "unknown_reasons": {"no_recorded_digest": 1}},
        "truncated": True,
    }
    assert maintenance._scan_outcome(report, allow_unknown=False)[0] == 5
    assert maintenance._scan_outcome(report, allow_unknown=True)[0] == 5


def test_scan_outcome_lets_allow_unknown_skip_only_a_legacy_row():
    report = {
        "coverage": "complete",
        "rows": [{"verdict": "unknown", "reason": "no_recorded_digest"}],
        "stats": {"examined": 1, "matched": 0, "unknown": 1,
                  "unknown_reasons": {"no_recorded_digest": 1}},
        "truncated": False,
    }
    assert maintenance._scan_outcome(report, allow_unknown=False)[0] == 5
    assert maintenance._scan_outcome(report, allow_unknown=True)[0] == 0


def test_scan_outcome_never_waves_a_malformed_row_through():
    report = {
        "coverage": "complete",
        "rows": [{"verdict": "unknown", "reason": "artifact_record_invalid"}],
        "stats": {"examined": 1, "matched": 0, "unknown": 1,
                  "unknown_reasons": {"artifact_record_invalid": 1}},
        "truncated": False,
    }
    assert maintenance._scan_outcome(report, allow_unknown=True)[0] == 5
