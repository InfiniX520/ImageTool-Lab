"""Regression tests for bounded record persistence and lookup caching."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.record_manager as record_manager
from core.legacy_record_utils import clear_download_records


def _record(status: str = "下载失败", *, file_path: str = "", file_size: int = 0) -> dict[str, object]:
    return {
        "file_path": file_path,
        "status": status,
        "file_size": file_size,
        "updated_at": "2026-07-20 12:00:00",
        "illust_id": "123",
        "page": 0,
    }


def test_save_json_is_atomic_and_keeps_previous_file_on_serialization_error(tmp_path: Path) -> None:
    target = tmp_path / "records.json"
    record_manager.save_json(target, {"old": 1})
    record_manager.save_json(target, {"new": 2})

    assert json.loads(target.read_text(encoding="utf-8")) == {"new": 2}

    with pytest.raises(TypeError):
        record_manager.save_json(target, {"not_serializable": object()})

    # The failed serialization must not truncate the last good snapshot, and
    # the sibling temporary file must always be cleaned up.
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": 2}
    assert not list(tmp_path.glob(".records.json.*.tmp"))


def test_corrupt_existing_record_is_not_replaced(tmp_path: Path) -> None:
    target = tmp_path / "records.json"
    original = b"{this is not valid json"
    target.write_bytes(original)

    with pytest.raises(record_manager.RecordPersistenceError):
        record_manager._update_id_record(target, "123_p0", _record())

    assert target.read_bytes() == original
    assert not list(tmp_path.glob(".records.json.*.tmp"))


def test_check_record_skip_reuses_compact_index(monkeypatch, tmp_path: Path) -> None:
    record_manager.clear_record_cache()
    download_root = tmp_path / "downloads"
    local_path = download_root / record_manager.GLOBAL_ID_RECORD_FILE
    central_root = tmp_path / "central"
    central_path = central_root / record_manager.CENTRAL_ID_RECORD_FILE
    record_manager.save_json(local_path, {"123_p0": _record()})
    record_manager.save_json(central_path, {})
    monkeypatch.setattr(record_manager, "CENTRAL_LOG_DIR", central_root)

    calls = 0
    original_json_load = record_manager.json.load

    def counted_json_load(file_obj, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_json_load(file_obj, *args, **kwargs)

    monkeypatch.setattr(record_manager.json, "load", counted_json_load)

    assert not record_manager.check_record_skip(str(download_root), "123", 0)
    assert not record_manager.check_record_skip(str(download_root), "123", 0)
    # The first lookup parses each JSON file once; subsequent lookups use the
    # bounded summary index and do not rehydrate the complete documents.
    assert calls == 2


def test_record_index_invalidates_after_external_write(monkeypatch, tmp_path: Path) -> None:
    record_manager.clear_record_cache()
    download_root = tmp_path / "downloads"
    local_path = download_root / record_manager.GLOBAL_ID_RECORD_FILE
    central_root = tmp_path / "central"
    central_path = central_root / record_manager.CENTRAL_ID_RECORD_FILE
    monkeypatch.setattr(record_manager, "CENTRAL_LOG_DIR", central_root)

    record_manager.save_json(local_path, {"123_p0": _record()})
    record_manager.save_json(central_path, {})
    assert not record_manager.check_record_skip(str(download_root), "123", 0)

    # A completed record with a valid file is recognized after the atomic
    # replacement changes the path signature.
    completed_file = download_root / "image.jpg"
    completed_file.parent.mkdir(parents=True, exist_ok=True)
    completed_file.write_bytes(b"image")
    record_manager.save_json(
        local_path,
        {"123_p0": _record("下载完成", file_path=str(completed_file), file_size=5)},
    )
    assert record_manager.check_record_skip(str(download_root), "123", 0)


def test_trust_completed_record_without_file_semantics_are_preserved(monkeypatch, tmp_path: Path) -> None:
    record_manager.clear_record_cache()
    download_root = tmp_path / "downloads"
    local_path = download_root / record_manager.GLOBAL_ID_RECORD_FILE
    central_root = tmp_path / "central"
    record_manager.save_json(local_path, {"123_p0": _record("下载完成")})
    monkeypatch.setattr(record_manager, "CENTRAL_LOG_DIR", central_root)
    record_manager.save_json(central_root / record_manager.CENTRAL_ID_RECORD_FILE, {})

    assert record_manager.check_record_skip(str(download_root), "123", 0)

    monkeypatch.setattr(record_manager, "TRUST_COMPLETED_RECORD_WITHOUT_FILE", False)
    record_manager.clear_record_cache()
    assert not record_manager.check_record_skip(str(download_root), "123", 0)


def test_clear_records_invalidates_skip_index(monkeypatch, tmp_path: Path) -> None:
    record_manager.clear_record_cache()
    download_root = tmp_path / "downloads"
    local_path = download_root / record_manager.GLOBAL_ID_RECORD_FILE
    central_root = tmp_path / "central"
    monkeypatch.setattr(record_manager, "CENTRAL_LOG_DIR", central_root)

    record_manager.save_json(local_path, {"123_p0": _record("下载完成")})
    record_manager.save_json(central_root / record_manager.CENTRAL_ID_RECORD_FILE, {})
    assert record_manager.check_record_skip(str(download_root), "123", 0)

    clear_download_records(str(download_root), clear_central=False)
    assert not record_manager.check_record_skip(str(download_root), "123", 0)


def test_markdown_refresh_reuses_updated_json_snapshot(monkeypatch, tmp_path: Path) -> None:
    """A refresh should not parse each just-written JSON file a second time."""

    record_manager.clear_record_cache()
    central_root = tmp_path / "central"
    monkeypatch.setattr(record_manager, "CENTRAL_LOG_DIR", central_root)

    calls = 0
    original_load_json = record_manager.load_json

    def counted_load_json(path, default):
        nonlocal calls
        calls += 1
        return original_load_json(path, default)

    monkeypatch.setattr(record_manager, "load_json", counted_load_json)
    record_manager._record_to_central_logs(
        "123_p0",
        _record("下载完成", file_path=str(tmp_path / "image.jpg"), file_size=5),
        refresh_markdown=True,
    )

    # The writer decodes each JSON once and renders Markdown from the same
    # in-memory snapshot; it no longer performs a second ``load_json`` pass.
    assert calls == 0
    assert (central_root / record_manager.CENTRAL_RECENT_MD_FILE).exists()


def test_deferred_markdown_flush_catches_up_before_batch_close(monkeypatch, tmp_path: Path) -> None:
    record_manager.clear_record_cache()
    record_manager.clear_pending_markdown()
    central_root = tmp_path / "central"
    download_root = tmp_path / "downloads"
    monkeypatch.setattr(record_manager, "CENTRAL_LOG_DIR", central_root)

    for illust_id in ("101", "102"):
        record_manager.record_download_event(
            str(download_root),
            False,
            str(download_root / f"{illust_id}.jpg"),
            illust_id,
            0,
            "title",
            "7",
            "artist",
            0,
            False,
            "2026-07-20T00:00:00+00:00",
            "https://img.example/item.jpg",
            "跳过",
        )

    recent_path = central_root / record_manager.CENTRAL_RECENT_MD_FILE
    assert not recent_path.exists()
    record_manager.flush_pending_markdown()
    text = recent_path.read_text(encoding="utf-8")
    assert "101" in text and "102" in text


def test_sqlite_commits_before_batched_legacy_projection(tmp_path: Path) -> None:
    original_root = record_manager.ROOT
    download_root = tmp_path / "downloads"
    try:
        record_manager.configure_record_storage(tmp_path)
        record_manager.record_download_event(
            str(download_root),
            False,
            str(download_root / "501.jpg"),
            "501",
            0,
            "title",
            "7",
            "artist",
            10,
            False,
            "2026-07-20T00:00:00+00:00",
            "https://i.pximg.net/img-original/501_p0.jpg",
            "下载完成",
        )

        database_path = tmp_path / "download_logs" / "download_records.sqlite3"
        assert database_path.exists()
        assert not (tmp_path / "download_logs" / record_manager.CENTRAL_ID_RECORD_FILE).exists()
        assert record_manager.check_record_skip(str(download_root), "501", 0)

        record_manager.flush_pending_markdown()
        central_json = tmp_path / "download_logs" / record_manager.CENTRAL_ID_RECORD_FILE
        assert json.loads(central_json.read_text(encoding="utf-8"))["501_p0"]["status"] == "下载完成"
    finally:
        record_manager.clear_pending_markdown()
        record_manager.clear_record_cache()
        record_manager.configure_record_storage(original_root)
