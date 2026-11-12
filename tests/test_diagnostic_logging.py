#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline P5 tests for the bounded diagnostic log core.

Only temporary directories/files and synthetic messages are used; no HTTP, no
GUI, no production log or configuration is touched.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core.diagnostic_logging as diagnostic_module
from core.app_paths import AppPaths
from core.diagnostic_logging import (
    DiagnosticEvent,
    DiagnosticLog,
    DiagnosticLogError,
    LEVELS,
)
from core.library_paths import PathResolver
from core.task_contracts import ContractError, PathRef


@pytest.fixture
def logs():
    created = []

    def factory(log_dir, **kwargs):
        kwargs.setdefault("flush_interval", 0.01)
        log = DiagnosticLog(log_dir, **kwargs)
        created.append(log)
        return log

    yield factory
    for log in created:
        try:
            log.close(timeout=10)
        except Exception:
            pass


def _manifest(log_dir) -> dict:
    return json.loads(
        (Path(log_dir) / "diagnostic_manifest_v2.json").read_text(encoding="utf-8")
    )


def _volume_files(log_dir, session_id) -> list[Path]:
    return sorted(Path(log_dir).glob(f"diagnostic_{session_id}_v*.jsonl"))


def _records(log_dir, session_id) -> list[dict]:
    records = []
    for path in _volume_files(log_dir, session_id):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def _all_text(log_dir, session_id) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in _volume_files(log_dir, session_id)
    )


def _resolver(tmp_path):
    return PathResolver(AppPaths(tmp_path))


def _make_directory_link(link: Path, target: Path) -> None:
    """Create a real directory link; junctions need no symlink privilege."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def _remove_directory_link(link: Path) -> None:
    if not (link.exists() or link.is_symlink()):
        return
    try:
        os.unlink(link)
        return
    except OSError:
        pass
    try:
        os.rmdir(link)
    except OSError:
        pass


def _block_writes(log, *, timeout=10.0):
    """Delay (not replace) the writer's I/O so overflow can be forced."""
    release = threading.Event()
    original = log._write_batch

    def blocked(batch):
        release.wait(timeout)
        original(batch)

    log._write_batch = blocked
    return release


# ---------------------------------------------------------------------------
# Records, identity and producer compatibility
# ---------------------------------------------------------------------------


def test_emit_persists_jsonl_with_sequence_fields_and_uuid_session(logs, tmp_path):
    directory = tmp_path / "logs"
    log = logs(directory)
    first = log.emit("hello", level="info", code="test")
    second = log.emit(
        "with context",
        level="warning",
        code="page",
        job_id="job-1",
        batch_id="batch-2",
        work_id="12345",
        page_index=3,
    )
    third = log.emit("done", level="success", code="result")
    assert (first, second, third) == (1, 2, 3)
    assert log.sequence == 3
    assert log.session_id.count("-") >= 1
    assert any(char.isdigit() for char in log.session_id)

    assert log.flush(5) is True
    files = _volume_files(directory, log.session_id)
    assert len(files) == 1
    assert log.session_id in files[0].name
    assert files[0].name.endswith("_v0001.jsonl")
    records = _records(directory, log.session_id)
    assert [record["sequence"] for record in records] == [1, 2, 3]
    assert all(record["session_id"] == log.session_id for record in records)
    assert all(record["v"] == 2 for record in records)
    assert records[0]["message"] == "hello"
    assert records[1]["job_id"] == "job-1"
    assert records[1]["batch_id"] == "batch-2"
    assert records[1]["work_id"] == "12345"
    assert records[1]["page_index"] == 3
    assert records[2]["level"] == "success"
    assert datetime.fromisoformat(records[0]["timestamp"]).tzinfo is not None

    manifest = _manifest(directory)
    assert manifest["format"] == "jpgpng-diagnostic-v2"
    assert manifest["version"] == 2
    entry = manifest["sessions"][log.session_id]
    assert entry["closed"] is False
    assert [volume["name"] for volume in entry["volumes"]] == [files[0].name]


def test_two_sessions_in_the_same_second_never_share_files(logs, tmp_path):
    directory = tmp_path / "logs"
    first = logs(directory)
    second = logs(directory)
    assert first.session_id != second.session_id
    first.emit("first-session", code="first")
    second.emit("second-session", code="second")
    assert first.flush(5) and second.flush(5)
    first_files = {path.name for path in _volume_files(directory, first.session_id)}
    second_files = {path.name for path in _volume_files(directory, second.session_id)}
    assert first_files and second_files
    assert first_files.isdisjoint(second_files)
    assert "first-session" in _all_text(directory, first.session_id)
    assert "first-session" not in _all_text(directory, second.session_id)
    assert first.close(5) is True
    assert second.close(5) is True
    manifest = _manifest(directory)
    assert manifest["sessions"][first.session_id]["closed"] is True
    assert manifest["sessions"][second.session_id]["closed"] is True


def test_put_legacy_prefixes_map_levels_without_duplicate_prefix(logs, tmp_path):
    directory = tmp_path / "logs"
    log = logs(directory)
    log.put("[+] success text")
    log.put("[-] error text")
    log.put("[!] warning text")
    log.put("[*] info text")
    log.put("[D] debug text")
    log.put("plain text")
    events = log.events_after(0, limit=10)
    assert [event.level for event in events] == [
        "success",
        "error",
        "warning",
        "info",
        "debug",
        "info",
    ]
    assert [event.message for event in events] == [
        "success text",
        "error text",
        "warning text",
        "info text",
        "debug text",
        "plain text",
    ]
    assert all(event.code == "legacy" for event in events)
    assert log.flush(5) is True
    text = _all_text(directory, log.session_id)
    for legacy in ("[+]", "[-]", "[!]", "[*]", "[D]"):
        assert legacy not in text
    assert "success text" in text


def test_message_truncation_is_marked_and_never_breaks_utf8(logs, tmp_path):
    directory = tmp_path / "logs"
    log = logs(directory)
    log.emit("密" * 10_000, code="big")
    log.emit("a" * 16_000, code="limit")
    assert log.flush(5) is True
    records = _records(directory, log.session_id)
    big = records[0]["message"]
    encoded = big.encode("utf-8")
    assert len(encoded) <= 16 * 1024
    assert big.endswith("…[truncated]")
    assert "\ufffd" not in big
    assert len(big.encode("utf-8")) >= 16 * 1024 - 32
    assert records[1]["message"] == "a" * 16_000
    snapshot = log.snapshot()
    assert snapshot["truncated_messages"] == 1


def test_redaction_replaces_whole_credential_like_messages(logs, tmp_path):
    directory = tmp_path / "logs"
    log = logs(directory)
    secrets = [
        "secret-value-abc",
        "eyJhbGciOiJIUzI1NiJ9.secret",
        "hunter2",
        "abc123",
        "cookie-value",
        "tok_live_987",
        "p@ssw0rd-秘密",
        "sid-value-xyz",
        "sig-value-123",
    ]
    messages = [
        "Cookie: SESSDATA=" + secrets[4],
        "Authorization: Bearer " + secrets[1],
        '{"access_token": "' + secrets[0] + '", "user": "u1"}',
        "password=" + secrets[2] + "\nsecond line with api_key=" + secrets[5],
        "https://example.test/download?token=" + secrets[3] + "&user=1",
        "PHPSESSID=" + secrets[4],
        "ＡＵＴＨＯＲＩＺＡＴＩＯＮ: Ｂｅａｒｅｒ " + secrets[6],
        "https://example.test/x?sid=" + secrets[7] + "&sig=" + secrets[8],
        "token refreshed for account summary",  # keyword alone still redacted
    ]
    for message in messages:
        log.emit(message, level="error", code="redact")
    log.emit("ordinary progress 42", level="info", code="progress")
    assert log.flush(5) is True
    stored = _all_text(directory, log.session_id)
    for secret in secrets:
        assert secret not in stored
    records = _records(directory, log.session_id)
    redacted = [record for record in records if record["code"] == "redact"]
    assert len(redacted) == len(messages)
    assert all(
        record["message"] == "[redacted: credential-like content]"
        for record in redacted
    )
    assert records[-1]["message"] == "ordinary progress 42"
    assert all(
        event.message != "Cookie: SESSDATA=cookie-value"
        for event in log.events_after(0, limit=50)
    )

    target = tmp_path / "exports" / "redacted.txt"
    result = log.export_text(
        PathRef(relative_path="exports/redacted.txt"), _resolver(tmp_path)
    )
    assert result["record_count"] == len(messages) + 1
    exported = target.read_text(encoding="utf-8")
    for secret in secrets:
        assert secret not in exported
    assert "[redacted: credential-like content]" in exported


@pytest.mark.parametrize(
    "kwargs",
    [
        {"message": 5},
        {"message": "x", "level": "INFO"},
        {"message": "x", "level": "fatal"},
        {"message": "x", "code": "Bad Code"},
        {"message": "x", "code": "UPPER"},
        {"message": "x", "job_id": "bad\nid"},
        {"message": "x", "batch_id": "b" * 200},
        {"message": "x", "work_id": "a b"},
        {"message": "x", "page_index": -1},
        {"message": "x", "page_index": 10_000},
        {"message": "x", "page_index": True},
    ],
)
def test_emit_rejects_invalid_fields(logs, tmp_path, kwargs):
    log = logs(tmp_path / "logs")
    with pytest.raises(ValueError):
        log.emit(**kwargs)
    assert log.snapshot()["sequence"] == 0


def test_emit_accepts_boundary_page_indices(logs, tmp_path):
    log = logs(tmp_path / "logs")
    assert log.emit("a", page_index=0) == 1
    assert log.emit("b", page_index=9999) == 2
    assert log.emit("c", code="legacy") == 3


def test_diagnostic_event_is_frozen_and_session_identity_is_uuid_based(logs, tmp_path):
    import dataclasses
    import re

    log = logs(tmp_path / "logs")
    log.emit("event", job_id="job-1")
    event = log.events_after(0)[0]
    assert isinstance(event, DiagnosticEvent)
    assert dataclasses.is_dataclass(event)
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.message = "changed"
    assert re.fullmatch(
        r"[0-9]{8}T[0-9]{6}_[0-9]{6}Z-[0-9a-f]{32}", event.session_id
    )
    assert event.sequence == log.sequence == 1
    assert event.timestamp.endswith("+00:00")
    with pytest.raises(ValueError):
        DiagnosticLog(tmp_path / "other", max_message_bytes=32 * 1024)


# ---------------------------------------------------------------------------
# Bounded queue, priority and UI ring
# ---------------------------------------------------------------------------


def test_10k_burst_is_bounded_and_reports_gaps(logs, tmp_path):
    directory = tmp_path / "logs"
    log = logs(
        directory,
        queue_records=64,
        queue_bytes=8 * 1024,
        ui_records=100,
        ui_bytes=64 * 1024,
        volume_bytes=64 * 1024,
    )
    release = _block_writes(log)
    try:
        for index in range(10_000):
            log.emit(
                "burst-%d" % index,
                level="debug",
                code="c%d" % index,
            )
        snapshot = log.snapshot()
        assert snapshot["sequence"] == 10_000
        assert snapshot["queue_records"] <= 64
        assert snapshot["queue_bytes"] <= 8 * 1024
        assert snapshot["ui_records"] <= 100
        assert snapshot["ui_bytes"] <= 64 * 1024
        assert snapshot["file_dropped"] > 0
        assert snapshot["merged"] == 0
        assert snapshot["ui_dropped"] > 0
        assert len(snapshot["file_dropped_ranges"]) <= 8
        assert len(snapshot["ui_dropped_ranges"]) <= 8
        if not snapshot["dropped_ranges_truncated"]:
            assert (
                sum(last - first + 1 for first, last in snapshot["file_dropped_ranges"])
                == snapshot["file_dropped"]
            )
        assert snapshot["health_code"] in {
            "queue_overflow",
            "degraded_ui",
        }
    finally:
        release.set()
    assert log.flush(10) is True
    snapshot = log.snapshot()
    assert snapshot["queue_records"] == 0
    assert snapshot["writer_alive"] is True


def test_repeated_low_priority_events_are_coalesced(logs, tmp_path):
    log = logs(tmp_path / "logs", queue_records=8, flush_interval=0.01)
    release = _block_writes(log)
    try:
        for _index in range(1000):
            log.emit("same progress", level="info", code="same")
        snapshot = log.snapshot()
        assert snapshot["merged"] > 0
        assert snapshot["queue_records"] <= 8
        assert snapshot["sequence"] == 1000
    finally:
        release.set()
    assert log.flush(10) is True


def test_warning_and_error_survive_a_low_priority_flood(logs, tmp_path):
    directory = tmp_path / "logs"
    log = logs(
        directory,
        queue_records=16,
        queue_bytes=16 * 1024,
        flush_interval=0.01,
    )
    release = _block_writes(log)
    try:
        for index in range(300):
            log.emit("noise-%d" % index, level="info", code="noise")
        for index in range(3):
            log.emit("important-error-%d" % index, level="error", code="boom")
        log.emit("important-warning", level="warning", code="warn")
        snapshot = log.snapshot()
        assert snapshot["file_dropped"] > 0
        assert snapshot["queue_records"] <= 16
    finally:
        release.set()
    assert log.flush(10) is True
    messages = [record["message"] for record in _records(directory, log.session_id)]
    for index in range(3):
        assert "important-error-%d" % index in messages
    assert "important-warning" in messages


def test_warning_never_evicts_errors_and_errors_stay_bounded(logs, tmp_path):
    directory = tmp_path / "logs"
    log = logs(directory, queue_records=4, queue_bytes=8 * 1024, flush_interval=0.01)
    release = _block_writes(log)
    try:
        for index in range(8):
            log.emit("error-%d" % index, level="error", code="boom")
        log.emit("late-warning", level="warning", code="warn")
        log.emit("late-error", level="error", code="boom")
        snapshot = log.snapshot()
        assert snapshot["queue_records"] <= 4
        assert snapshot["file_dropped"] >= 1
    finally:
        release.set()
    assert log.flush(10) is True
    messages = [record["message"] for record in _records(directory, log.session_id)]
    assert "late-warning" not in messages
    assert "late-error" in messages
    assert any(message.startswith("error-") for message in messages)


def test_ui_ring_is_bounded_and_events_after_filters(logs, tmp_path):
    log = logs(tmp_path / "logs", ui_records=5, ui_bytes=64 * 1024)
    for index in range(20):
        log.emit("line-%d" % index, code="ui")
    snapshot = log.snapshot()
    assert snapshot["ui_records"] == 5
    assert snapshot["ui_dropped"] == 15
    events = log.events_after(0)
    assert [event.sequence for event in events] == [16, 17, 18, 19, 20]
    assert [event.sequence for event in log.events_after(17)] == [18, 19, 20]
    assert log.events_after(20) == ()
    assert len(log.events_after(0, limit=3)) == 3
    with pytest.raises(ValueError):
        log.events_after(0, limit=0)
    with pytest.raises(ValueError):
        log.events_after(-1)


def test_snapshot_is_bounded_and_lists_retained_volumes(logs, tmp_path):
    directory = tmp_path / "logs"
    log = logs(directory, volume_bytes=1024, backup_count=2)
    for index in range(200):
        log.emit("rotation-%d-%s" % (index, "y" * 40), code="rotation")
    assert log.flush(5) is True
    snapshot = log.snapshot()
    assert snapshot["closed"] is False
    assert snapshot["writer_alive"] is True
    assert snapshot["retained_volume_count"] <= 3
    assert len(snapshot["retained_volumes"]) <= 3
    assert len(snapshot["rotated_out_ranges"]) <= 8
    assert snapshot["rotated_out_volumes"] > 0
    assert set(snapshot) >= {
        "session_id",
        "sequence",
        "queue_records",
        "queue_bytes",
        "ui_records",
        "ui_bytes",
        "file_dropped",
        "ui_dropped",
        "closed",
        "health_code",
        "retained_volumes",
    }
    for volume in snapshot["retained_volumes"]:
        assert (directory / volume["name"]).is_file()
        assert volume["bytes"] == (directory / volume["name"]).stat().st_size


def test_rotation_keeps_only_bounded_backups_on_disk(logs, tmp_path):
    directory = tmp_path / "logs"
    log = logs(directory, volume_bytes=1024, backup_count=2)
    for index in range(300):
        log.emit("rotate-%d-%s" % (index, "z" * 60), code="rotate")
    assert log.flush(5) is True
    snapshot = log.snapshot()
    retained = [volume["name"] for volume in snapshot["retained_volumes"]]
    assert 1 <= len(retained) <= 3
    assert {path.name for path in _volume_files(directory, log.session_id)} == set(retained)
    assert snapshot["rotated_out_volumes"] > 0
    assert snapshot["rotated_out_ranges"]
    manifest = _manifest(directory)
    entry = manifest["sessions"][log.session_id]
    assert {volume["name"] for volume in entry["volumes"]} == set(retained)


def test_sustained_short_pressure_stays_bounded(logs, tmp_path):
    log = logs(
        tmp_path / "logs",
        queue_records=32,
        queue_bytes=8192,
        ui_records=200,
        flush_interval=0.01,
    )
    started = time.monotonic()
    count = 0
    while time.monotonic() - started < 0.2 and count < 300:
        log.emit("sustained-%d" % count, level="info", code="sustain")
        count += 1
        time.sleep(0.001)
    assert log.flush(5) is True
    snapshot = log.snapshot()
    assert snapshot["sequence"] == count
    assert snapshot["queue_records"] == 0
    assert snapshot["ui_records"] <= 200
    assert snapshot["writer_alive"] is True


# ---------------------------------------------------------------------------
# Failure, close and flush boundaries
# ---------------------------------------------------------------------------


def test_unavailable_directory_degrades_without_raising(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    log = DiagnosticLog(
        blocker, queue_records=8, queue_bytes=1024, flush_interval=0.01
    )
    try:
        for index in range(50):
            log.emit("degraded-error-%d" % index, level="error", code="disk")
        assert log.flush(2) is True
        snapshot = log.snapshot()
        assert snapshot["health_code"] == "open_failed"
        assert snapshot["write_errors"] >= 1
        assert snapshot["file_dropped"] >= 1
        assert snapshot["queue_records"] <= 8
        assert snapshot["writer_alive"] is True
        assert log.close(5) is True
        assert log.snapshot()["writer_alive"] is False
    finally:
        log.close(5)


def test_write_failure_on_rotation_keeps_writer_alive(tmp_path, monkeypatch, logs):
    directory = tmp_path / "logs"
    log = logs(directory, volume_bytes=1024, backup_count=1, queue_records=16)

    def boom(path):
        raise OSError("simulated disk full")

    monkeypatch.setattr(diagnostic_module, "_open_volume", boom)
    for index in range(120):
        log.emit("write-failure-%d-%s" % (index, "x" * 80), code="disk")
    assert log.flush(5) is True
    snapshot = log.snapshot()
    assert snapshot["write_errors"] >= 1
    assert snapshot["health_code"] in {"write_error", "open_failed"}
    assert snapshot["queue_records"] <= 16
    assert snapshot["writer_alive"] is True
    for index in range(20):
        log.emit("after-failure-%d" % index, level="error", code="disk")
    assert log.flush(5) is True
    assert log.snapshot()["writer_alive"] is True
    assert log.close(5) is True
    assert log.snapshot()["closed"] is True


def test_close_with_blocked_writer_times_out_honestly_then_finishes(logs, tmp_path):
    log = logs(tmp_path / "logs", queue_records=32)
    release = _block_writes(log)
    try:
        for index in range(10):
            log.emit("queued-%d" % index, code="close")
        assert log.close(timeout=0.2) is False
        snapshot = log.snapshot()
        assert snapshot["closed"] is False
        assert snapshot["closing"] is True
        assert snapshot["health_code"] == "close_timeout"
        assert snapshot["pending_writes"] > 0
    finally:
        release.set()
    assert log.close(timeout=10) is True
    assert log.snapshot()["closed"] is True
    assert log.snapshot()["writer_alive"] is False
    assert not any(
        thread.name.startswith("jpgpng-diagnostic-") for thread in threading.enumerate()
    )
    assert log.close(timeout=5) is True
    sequence = log.sequence
    assert log.emit("after-close", code="late") == sequence
    assert log.snapshot()["post_close_dropped"] == 1


def test_close_timeout_includes_finalization_on_the_owner_thread(logs, tmp_path):
    directory = tmp_path / "logs"
    log = logs(directory, queue_records=32)
    log.emit("persist-before-close", code="close")
    assert log.flush(5) is True
    original = log._finalize_session
    release = threading.Event()
    entered = threading.Event()
    state = {}

    def blocked_finalize():
        state["thread"] = threading.current_thread().name
        entered.set()
        release.wait(10)
        original()

    log._finalize_session = blocked_finalize
    try:
        started = time.monotonic()
        assert log.close(timeout=0.05) is False
        elapsed = time.monotonic() - started
        assert elapsed < 0.5, "close() blocked on finalization I/O"
        assert entered.wait(2) is True
        assert state["thread"].startswith("jpgpng-diagnostic-")
        snapshot = log.snapshot()
        assert snapshot["closed"] is False
        assert snapshot["finalized"] is False
        assert snapshot["closing"] is True
        assert snapshot["writer_alive"] is True
        assert snapshot["health_code"] == "close_timeout"
        manifest = json.loads(
            (directory / "diagnostic_manifest_v2.json").read_text(encoding="utf-8")
        )
        assert manifest["sessions"][log.session_id]["closed"] is False
    finally:
        release.set()
    assert log.close(timeout=10) is True
    snapshot = log.snapshot()
    assert snapshot["closed"] is True
    assert snapshot["finalized"] is True
    assert snapshot["writer_alive"] is False
    assert not any(
        thread.name.startswith("jpgpng-diagnostic-") for thread in threading.enumerate()
    )
    manifest = json.loads(
        (directory / "diagnostic_manifest_v2.json").read_text(encoding="utf-8")
    )
    assert manifest["sessions"][log.session_id]["closed"] is True


def test_flush_timeout_is_reported_not_faked(logs, tmp_path):
    log = logs(tmp_path / "logs", queue_records=32)
    release = _block_writes(log)
    try:
        for index in range(5):
            log.emit("flush-%d" % index, code="flush")
        assert log.flush(0.1) is False
    finally:
        release.set()
    assert log.flush(10) is True


def test_close_drains_every_queued_record(logs, tmp_path):
    directory = tmp_path / "logs"
    log = logs(directory, queue_records=1024, flush_interval=0.05)
    for index in range(200):
        log.emit("drain-%d" % index, code="drain")
    assert log.close(timeout=10) is True
    records = _records(directory, log.session_id)
    assert len(records) == 200
    assert records[-1]["message"] == "drain-199"
    assert log.snapshot()["closed"] is True
    assert log.snapshot()["writer_alive"] is False


# ---------------------------------------------------------------------------
# Retention: only explicit v2-owned closed sessions
# ---------------------------------------------------------------------------


def test_budget_prunes_only_own_closed_sessions_and_keeps_unknown_files(tmp_path):
    base = tmp_path / "logs"
    first = DiagnosticLog(base, volume_bytes=2048, backup_count=1, flush_interval=0.01)
    try:
        for index in range(30):
            first.emit("old-session-%d-%s" % (index, "a" * 40), code="old")
        assert first.flush(5) is True
    finally:
        assert first.close(10) is True
    old_files = {path.name for path in _volume_files(base, first.session_id)}
    assert old_files

    unknown_legacy = base / "pixiv_session_20200101_1234.log"
    unknown_legacy.write_text("[*] legacy\n", encoding="utf-8")
    notes = base / "notes.txt"
    notes.write_text("keep me", encoding="utf-8")
    foreign = base / "diagnostic_foreign_v0001.jsonl"
    foreign.write_text("{}\n", encoding="utf-8")

    second = DiagnosticLog(
        base,
        volume_bytes=2048,
        backup_count=1,
        directory_budget_bytes=1,
        flush_interval=0.01,
    )
    try:
        second.emit("new-session", code="new")
        assert second.flush(5) is True
        assert not any((base / name).exists() for name in old_files)
        assert unknown_legacy.read_text(encoding="utf-8") == "[*] legacy\n"
        assert notes.read_text(encoding="utf-8") == "keep me"
        assert foreign.exists()
        assert (base / "diagnostic_manifest_v2.json").is_file()
    finally:
        assert second.close(10) is True
    snapshot = second.snapshot()
    assert snapshot["directory_sessions"] >= 1
    assert snapshot["health_code"] in {"directory_over_budget", "ok"}


def test_max_age_pruning_uses_injected_wall_clock(tmp_path):
    base = tmp_path / "logs"
    old_now = datetime.now(timezone.utc) - timedelta(days=40)
    first = DiagnosticLog(
        base,
        wall_clock=lambda: old_now,
        volume_bytes=2048,
        flush_interval=0.01,
    )
    try:
        for index in range(10):
            first.emit("expiring-%d" % index, code="age")
        assert first.flush(5) is True
    finally:
        assert first.close(10) is True
    old_files = _volume_files(base, first.session_id)
    assert old_files

    second = DiagnosticLog(
        base,
        max_age_days=30,
        directory_budget_bytes=1 << 20,
        flush_interval=0.01,
    )
    try:
        second.emit("fresh", code="age")
        assert second.flush(5) is True
        assert not any(path.exists() for path in old_files)
    finally:
        assert second.close(10) is True
    assert _manifest(base)["sessions"][second.session_id]["closed"] is True


# ---------------------------------------------------------------------------
# Streaming export
# ---------------------------------------------------------------------------


def test_export_streams_snapshot_with_rotation_and_drop_notes(logs, tmp_path):
    directory = tmp_path / "logs"
    log = logs(directory, volume_bytes=1024, backup_count=1)
    for index in range(160):
        log.emit("exported-%d-%s" % (index, "e" * 40), code="export")
    assert log.flush(5) is True
    log.emit("exported-after-snapshot", code="export")
    assert log.flush(5) is True

    target = tmp_path / "exports" / "session.txt"
    result = log.export_text(
        PathRef(relative_path="exports/session.txt"), _resolver(tmp_path)
    )
    assert isinstance(result, dict)
    assert result["output_ref"] == PathRef(relative_path="exports/session.txt")
    assert result["session_id"] == log.session_id
    assert result["sequence_max"] == log.sequence
    assert result["sequence_min"] >= 1
    assert result["record_count"] >= 1
    assert result["bytes_written"] == target.stat().st_size
    assert result["captured_bytes"] > 0
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# jpgpng diagnostic log export")
    assert f"# sequence_max: {result['sequence_max']}" in text
    assert "# note: bounded snapshot" in text
    assert "rotated_out_volumes" in text
    assert "exported-" in text
    lines = [
        line for line in text.splitlines() if line and not line.startswith("#")
    ]
    assert len(lines) == result["record_count"]
    assert all(" seq=" in line for line in lines)
    assert str(tmp_path) not in text


def test_export_cancel_and_publish_failure_keep_old_product(logs, tmp_path, monkeypatch):
    directory = tmp_path / "logs"
    log = logs(directory, volume_bytes=2048)
    for index in range(20):
        log.emit("protect-%d" % index, code="export")
    assert log.flush(5) is True
    target = tmp_path / "exports" / "keep.log"
    target.parent.mkdir(parents=True)
    target.write_text("OLD PRODUCT", encoding="utf-8")
    ref = PathRef(relative_path="exports/keep.log")
    resolver = _resolver(tmp_path)

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(DiagnosticLogError, match="cancel"):
        log.export_text(ref, resolver, stop_event=cancelled)
    assert target.read_text(encoding="utf-8") == "OLD PRODUCT"
    assert sorted(os.listdir(target.parent)) == ["keep.log"]

    def boom(source, destination):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(diagnostic_module, "_replace_file", boom)
    with pytest.raises(OSError, match="publish"):
        log.export_text(ref, resolver)
    assert target.read_text(encoding="utf-8") == "OLD PRODUCT"
    assert sorted(os.listdir(target.parent)) == ["keep.log"]


def test_export_target_is_limited_and_resolver_checked(logs, tmp_path):
    log = logs(tmp_path / "logs")
    log.emit("x", code="export")
    assert log.flush(5) is True
    resolver = _resolver(tmp_path)
    for relative_path in ("exports/a.jsonl", "exports/a.sqlite3", "exports/a"):
        with pytest.raises(ValueError, match=".txt or .log"):
            log.export_text(PathRef(relative_path=relative_path), resolver)
    with pytest.raises(ValueError):
        log.export_text("exports/a.txt", resolver)
    with pytest.raises(ValueError):
        log.export_text(PathRef(relative_path="exports/a.txt"), object())
    with pytest.raises(ValueError):
        log.export_text(
            PathRef(relative_path="exports/a.txt"), resolver, stop_event=object()
        )
    external = PathRef(kind="external", root_id="archive", relative_path="a.txt")
    with pytest.raises(ContractError, match="not been mapped"):
        log.export_text(external, resolver)


def test_export_protects_volumes_from_rotation_while_reading(logs, tmp_path, monkeypatch):
    directory = tmp_path / "logs"
    log = logs(directory, volume_bytes=1024, backup_count=2)
    for index in range(30):
        log.emit("before-export-%d-%s" % (index, "p" * 30), code="export")
    assert log.flush(5) is True
    initial_files = _volume_files(directory, log.session_id)
    assert len(initial_files) >= 2
    protected = initial_files[0].name
    expected_records = len(_records(directory, log.session_id))
    assert expected_records >= 5
    protected_first = min(
        json.loads(line)["sequence"]
        for line in initial_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    )

    started = threading.Event()
    release = threading.Event()
    original = diagnostic_module._iter_volume_lines

    def blocking_iter(path, capped_bytes, stop_event=None):
        if path.name == protected:
            started.set()
            release.wait(10)
        yield from original(path, capped_bytes, stop_event)

    monkeypatch.setattr(diagnostic_module, "_iter_volume_lines", blocking_iter)
    results = {}

    def run_export():
        try:
            results["value"] = log.export_text(
                PathRef(relative_path="exports/protected.txt"),
                _resolver(tmp_path),
            )
        except BaseException as exc:  # pragma: no cover - failure path
            results["error"] = exc

    worker = threading.Thread(target=run_export)
    worker.start()
    try:
        assert started.wait(10), "export never started reading the volume"
        for index in range(80):
            log.emit("during-export-%d-%s" % (index, "q" * 40), code="export")
        assert log.flush(10) is True
        assert (directory / protected).is_file()
        assert log.snapshot()["rotation_blocked"] >= 1
    finally:
        release.set()
        worker.join(20)
    assert "error" not in results
    value = results["value"]
    assert value["record_count"] == expected_records
    text = (tmp_path / "exports" / "protected.txt").read_text(encoding="utf-8")
    assert "before-export-" in text
    assert value["sequence_min"] == protected_first


def test_export_empty_session_cancelled_never_overwrites_product(logs, tmp_path):
    log = logs(tmp_path / "logs")
    reports = tmp_path / "reports"
    reports.mkdir()
    target = reports / "output.txt"
    target.write_text("previous", encoding="utf-8")
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(DiagnosticLogError, match="cancel"):
        log.export_text(
            PathRef(relative_path="reports/output.txt"),
            _resolver(tmp_path),
            stop_event=cancelled,
        )
    assert target.read_text(encoding="utf-8") == "previous"
    assert sorted(os.listdir(reports)) == ["output.txt"]


def test_export_cancelled_before_publish_keeps_old_product(logs, tmp_path, monkeypatch):
    log = logs(tmp_path / "logs")
    log.emit("cancel-before-publish", code="cancel")
    assert log.flush(5) is True
    reports = tmp_path / "reports"
    reports.mkdir()
    target = reports / "output.txt"
    target.write_text("previous", encoding="utf-8")
    cancelled = threading.Event()
    original = diagnostic_module._verify_identity

    def verify_then_cancel(path, identity):
        original(path, identity)
        cancelled.set()

    monkeypatch.setattr(diagnostic_module, "_verify_identity", verify_then_cancel)
    with pytest.raises(DiagnosticLogError, match="cancel"):
        log.export_text(
            PathRef(relative_path="reports/output.txt"),
            _resolver(tmp_path),
            stop_event=cancelled,
        )
    assert target.read_text(encoding="utf-8") == "previous"
    assert sorted(os.listdir(reports)) == ["output.txt"]


def test_export_rechecks_target_after_identity_verification(logs, tmp_path, monkeypatch):
    project = tmp_path / "project"
    reports = project / "reports"
    reports.mkdir(parents=True)
    target = reports / "output.txt"
    target.write_text("previous", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    log = logs(tmp_path / "logs")
    for index in range(3):
        log.emit("recheck-%d" % index, code="recheck")
    assert log.flush(5) is True
    resolver = PathResolver(AppPaths(project))
    original_verify = diagnostic_module._verify_identity
    state = {"swapped": False}

    def verify_then_swap(path, identity):
        original_verify(path, identity)
        if state["swapped"]:
            return
        state["swapped"] = True
        real = project / "reports-real"
        os.rename(reports, real)
        _make_directory_link(reports, outside)

    monkeypatch.setattr(diagnostic_module, "_verify_identity", verify_then_swap)
    try:
        with pytest.raises((ContractError, DiagnosticLogError)):
            log.export_text(
                PathRef(relative_path="reports/output.txt"), resolver
            )
        assert state["swapped"] is True
        assert (project / "reports-real" / "output.txt").read_text(
            encoding="utf-8"
        ) == "previous"
        assert not (outside / "output.txt").exists()
    finally:
        _remove_directory_link(reports)
        real = project / "reports-real"
        if real.exists():
            os.rename(real, reports)
    assert (reports / "output.txt").read_text(encoding="utf-8") == "previous"
