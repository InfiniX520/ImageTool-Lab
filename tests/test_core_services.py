#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility contracts retained from the v2.0 release."""

from __future__ import annotations

from pathlib import Path

import core.pixiv_utils as pixiv_utils
import core.record_manager as record_manager
from core.deduplication import DeduplicationService
from core.pixiv_utils import GLOBAL_ID_RECORD_FILE, GLOBAL_MD_RECORD_FILE
from core.record_manager import RecordManager, configure_record_storage


def test_deduplication_service_save_and_load_ids(tmp_path: Path) -> None:
    id_file = tmp_path / "downloaded_ids.txt"
    service = DeduplicationService(id_file)

    assert service.load_ids() == set()
    assert service.save_id("123")
    assert service.save_id(456)
    assert service.load_ids() == {"123", "456"}


def test_deduplication_service_clear_records_in_save_dir(tmp_path: Path) -> None:
    save_dir = tmp_path / "downloads"
    save_dir.mkdir(parents=True, exist_ok=True)
    (save_dir / GLOBAL_ID_RECORD_FILE).write_text("{}", encoding="utf-8")
    (save_dir / GLOBAL_MD_RECORD_FILE).write_text("# log", encoding="utf-8")

    service = DeduplicationService(tmp_path / "downloaded_ids.txt")
    service.clear(str(save_dir), clear_central=False)

    assert not (save_dir / GLOBAL_ID_RECORD_FILE).exists()
    assert not (save_dir / GLOBAL_MD_RECORD_FILE).exists()


def test_record_manager_paths_are_under_project_root(tmp_path: Path) -> None:
    manager = RecordManager(tmp_path)

    assert manager.central_log_dir == tmp_path / "download_logs"
    assert manager.central_record_json == tmp_path / "download_logs" / "master_downloaded_record.json"
    manager.ensure_dirs()
    assert manager.central_log_dir.exists()
    assert manager.central_monthly_dir.exists()


def test_debug_mode_toggle_via_function_api() -> None:
    original = pixiv_utils.get_debug_mode()
    try:
        assert pixiv_utils.set_debug_mode(True) is True
        assert pixiv_utils.get_debug_mode() is True
        assert pixiv_utils.set_debug_mode(False) is False
        assert pixiv_utils.get_debug_mode() is False
    finally:
        pixiv_utils.set_debug_mode(original)


def test_record_storage_can_follow_an_injected_application_root(tmp_path: Path) -> None:
    original_root = record_manager.ROOT
    try:
        assert configure_record_storage(tmp_path) == tmp_path / "download_logs"
        assert Path(record_manager.get_central_log_root()) == tmp_path / "download_logs"
    finally:
        configure_record_storage(original_root)
