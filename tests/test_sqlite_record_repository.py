"""SQLite record migration, durability and source-preservation tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from core.sqlite_record_repository import SqliteRecordError, SqliteRecordRepository


def _record(illust_id: str, page: int, status: str = "下载完成") -> dict[str, object]:
    return {
        "file_path": f"E:/images/{illust_id}_p{page}.jpg",
        "status": status,
        "file_size": 123,
        "updated_at": "2026-07-20 12:00:00",
        "illust_id": illust_id,
        "page": page,
        "title": "title",
        "artist_id": "7",
        "artist_name": "artist",
        "bookmarks": 10,
        "is_ai": False,
        "publish_time": "2026-07-20",
        "original_url": f"https://i.pximg.net/img-original/{illust_id}_p{page}.jpg",
    }


def test_legacy_json_is_imported_verified_and_never_modified(tmp_path: Path) -> None:
    source = tmp_path / "master_downloaded_record.json"
    source.write_text(
        json.dumps({"1_p0": _record("1", 0), "2_p0": _record("2", 0)}, ensure_ascii=False),
        encoding="utf-8",
    )
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    repository = SqliteRecordRepository(tmp_path)
    assert repository.count() == 2
    assert repository.integrity_check() == "ok"
    assert repository.get_summary("1", 0)[0] == "下载完成"

    repository.record("3_p1", _record("3", 1, "下载失败"))
    assert repository.count() == 3
    repository.close()

    reopened = SqliteRecordRepository(tmp_path)
    assert reopened.count() == 3
    assert reopened.get_summary("3", 1)[0] == "下载失败"
    reopened.close()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_corrupt_legacy_json_is_rejected_without_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "master_downloaded_record.json"
    original = b"{not-valid-json"
    source.write_bytes(original)

    repository = SqliteRecordRepository(tmp_path)
    with pytest.raises(SqliteRecordError):
        repository.count()
    repository.close()
    assert source.read_bytes() == original


def test_skip_event_does_not_downgrade_completed_latest_state(tmp_path: Path) -> None:
    repository = SqliteRecordRepository(tmp_path)
    repository.record("9_p0", _record("9", 0, "下载完成"))
    repository.record("9_p0", _record("9", 0, "跳过"))

    assert repository.get_summary("9", 0)[0] == "下载完成"
    repository.close()
    connection = sqlite3.connect(tmp_path / "download_records.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM download_events").fetchone()[0] == 2
    finally:
        connection.close()


def test_failure_event_does_not_erase_completed_latest_state(tmp_path: Path) -> None:
    repository = SqliteRecordRepository(tmp_path)
    completed = _record("10", 0, "下载完成")
    repository.record("10_p0", completed)
    failed = _record("10", 0, "下载失败")
    failed["file_path"] = ""
    failed["file_size"] = 0
    failed["original_url"] = ""
    repository.record("10_p0", failed)

    assert repository.get_summary("10", 0) == (
        "下载完成",
        completed["file_path"],
        completed["file_size"],
    )
    repository.close()
    connection = sqlite3.connect(tmp_path / "download_records.sqlite3")
    try:
        statuses = connection.execute(
            "SELECT status FROM download_events ORDER BY event_id"
        ).fetchall()
        assert statuses == [("下载完成",), ("下载失败",)]
    finally:
        connection.close()


def test_crash_lagged_legacy_projection_is_streamed_from_sqlite(tmp_path: Path) -> None:
    repository = SqliteRecordRepository(tmp_path)
    repository.record("11_p0", _record("11", 0, "下载完成"))
    repository.record("12_p1", _record("12", 1, "下载失败"))
    assert repository.projection_is_current() is False

    assert repository.reconcile_legacy_projection() is True
    assert repository.projection_is_current() is True
    exported = json.loads((tmp_path / "master_downloaded_record.json").read_text(encoding="utf-8"))
    assert set(exported) == {"11_p0", "12_p1"}
    assert exported["11_p0"]["status"] == "下载完成"
    assert exported["12_p1"]["status"] == "下载失败"
    assert repository.reconcile_legacy_projection() is False
    repository.close()
