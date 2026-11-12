#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline P5 tests for the Tk-free bounded log view model."""

from __future__ import annotations

import dataclasses

import pytest

from core.diagnostic_logging import DiagnosticLog
from core.log_view import LogView, ViewDelta


@pytest.fixture
def make_log(tmp_path):
    created = []

    def factory(**kwargs):
        kwargs.setdefault("flush_interval", 0.01)
        kwargs.setdefault("ui_records", 5000)
        kwargs.setdefault("ui_bytes", 4 * 1024 * 1024)
        log = DiagnosticLog(tmp_path / "logs", **kwargs)
        created.append(log)
        return log

    yield factory
    for log in created:
        try:
            log.close(timeout=10)
        except Exception:
            pass


class _StepClock:
    def __init__(self, step: float = 0.0004):
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_drain_formats_levels_and_follows(make_log):
    log = make_log()
    log.emit("info one", level="info")
    log.emit("success one", level="success")
    log.emit("warn one", level="warning")
    log.emit("error one", level="error")
    log.emit("debug one", level="debug")
    view = LogView(log)
    delta = view.drain(max_lines=100, max_bytes=32 * 1024, max_ms=8)
    assert isinstance(delta, ViewDelta)
    assert delta.append_lines == (
        "[*] info one",
        "[+] success one",
        "[!] warn one",
        "[-] error one",
        "[D] debug one",
    )
    assert delta.remove_lines == 0
    assert delta.follow is True
    assert delta.skipped_events == 0
    assert delta.unseen_lines == 0
    assert view.line_count == 5
    assert view.lines() == delta.append_lines
    assert view.retained_bytes == sum(
        len(line.encode("utf-8")) for line in delta.append_lines
    )
    assert view.follow is True and view.paused is False
    assert view.drain(max_lines=100).append_lines == ()


def test_multi_line_event_is_split_into_physical_lines_across_drains(make_log):
    log = make_log()
    log.emit("alpha\nbeta\ngamma\ndelta", level="info")
    view = LogView(log)
    first = view.drain(max_lines=2)
    assert first.append_lines == ("[*] alpha", "beta")
    assert view.pending_lines == 2
    second = view.drain(max_lines=10)
    assert second.append_lines == ("gamma", "delta")
    assert view.pending_lines == 0
    assert view.line_count == 4


def test_drain_stops_at_line_byte_and_time_boundaries(make_log):
    log = make_log()
    for index in range(5):
        log.emit("event-%d" % index, level="info")
    view = LogView(log)
    limited = view.drain(max_lines=3, max_bytes=32 * 1024, max_ms=8)
    assert len(limited.append_lines) == 3
    rest = view.drain(max_lines=10, max_bytes=32 * 1024, max_ms=8)
    assert len(rest.append_lines) == 2
    assert view.line_count == 5

    byte_log = make_log()
    for index in range(4):
        byte_log.emit("0123456789", level="info")
    byte_view = LogView(byte_log)
    byte_delta = byte_view.drain(max_lines=100, max_bytes=20, max_ms=8)
    # A single physical line is never split to satisfy the byte budget.
    assert len(byte_delta.append_lines) == 1
    assert byte_view.pending_lines == 1
    collected_bytes = []
    for _ in range(10):
        delta = byte_view.drain(max_lines=100, max_bytes=1024, max_ms=8)
        if not delta.append_lines and byte_view.pending_lines == 0:
            break
        collected_bytes.extend(delta.append_lines)
    assert len(collected_bytes) == 3

    timed_log = make_log()
    for index in range(6):
        timed_log.emit("timed-%d" % index, level="info")
    timed_view = LogView(timed_log, clock=_StepClock())
    timed = timed_view.drain(max_lines=100, max_bytes=32 * 1024, max_ms=1)
    assert 0 < len(timed.append_lines) < 6
    collected = list(timed.append_lines)
    for _ in range(20):
        delta = timed_view.drain(max_lines=100, max_bytes=32 * 1024, max_ms=1)
        if not delta.append_lines:
            break
        collected.extend(delta.append_lines)
    assert len(collected) == 6
    assert timed_view.pending_lines == 0


def test_pending_multiline_is_never_reordered_by_newer_events(make_log):
    log = make_log()
    log.emit("first\nsecondAA\nthirdBBB", level="info")
    log.emit("X", level="info")
    view = LogView(log)
    collected = []
    for _ in range(10):
        delta = view.drain(max_bytes=15, max_ms=100)
        assert sum(len(line.encode("utf-8")) for line in delta.append_lines) <= 15
        collected.extend(delta.append_lines)
        if not delta.append_lines and view.pending_lines == 0:
            break
    assert collected == ["[*] first", "secondAA", "thirdBBB", "[*] X"]
    assert view.pending_lines == 0


def test_drain_hard_byte_limit_clips_one_oversized_line(make_log):
    log = make_log()
    log.emit("中文" * 1000, level="info")
    view = LogView(log)
    delta = view.drain(max_bytes=128, max_ms=100)
    assert len(delta.append_lines) == 1
    line = delta.append_lines[0]
    total = len(line.encode("utf-8"))
    assert total <= 128
    assert line.endswith("…[line truncated]")
    assert "\ufffd" not in line
    # The persisted/UI event keeps the complete message; only display is clipped.
    event = log.events_after(0)[0]
    assert len(event.message) == 2000
    assert len(event.message.encode("utf-8")) == 6000
    assert view.pending_lines == 0
    for _ in range(5):
        again = view.drain(max_bytes=128, max_ms=100)
        assert sum(len(item.encode("utf-8")) for item in again.append_lines) <= 128
        assert again.append_lines == ()
    assert view.pending_lines == 0
    assert view.line_count == 1
    assert view.retained_bytes == total


def test_line_that_fits_full_budget_waits_instead_of_gap_truncation(make_log):
    log = make_log()
    log.emit("1234567890\nabcdefghij", level="info")
    view = LogView(log)
    first = view.drain(max_bytes=20, max_ms=100)
    assert first.append_lines == ("[*] 1234567890",)
    assert view.pending_lines == 1
    second = view.drain(max_bytes=20, max_ms=100)
    assert second.append_lines == ("abcdefghij",)
    assert not second.append_lines[0].endswith("…[line truncated]")
    assert view.pending_lines == 0


def test_tiny_byte_budget_is_explicit_and_bounded(make_log):
    log = make_log()
    log.emit("abcdefghij", level="info")
    view = LogView(log)
    assert view.drain(max_bytes=0, max_ms=100).append_lines == ()
    tiny = view.drain(max_bytes=1, max_ms=100)
    assert len(tiny.append_lines) == 1
    assert tiny.append_lines[0]
    assert len(tiny.append_lines[0].encode("utf-8")) <= 1
    assert view.pending_lines == 0
    assert view.drain(max_bytes=1, max_ms=100).append_lines == ()

    cjk_log = make_log()
    cjk_log.emit("header\n中文", level="info")
    cjk_view = LogView(cjk_log)
    first = cjk_view.drain(max_bytes=1, max_ms=100)
    assert first.append_lines == ("[",)
    second = cjk_view.drain(max_bytes=1, max_ms=100)
    assert second.append_lines == ("?",)
    assert cjk_view.pending_lines == 0
    assert cjk_view.drain(max_bytes=1, max_ms=100).append_lines == ()


def test_clear_suppresses_backlog_and_pending_multiline(make_log):
    log = make_log()
    log.emit("old-1", level="info")
    log.emit("old-2", level="info")
    log.emit("old-3", level="info")
    view = LogView(log)
    assert view.clear() == 0
    assert view.drain(max_lines=100).append_lines == ()
    assert view.line_count == 0

    log.emit("multi-a\nmulti-b\nmulti-c", level="info")
    partial = view.drain(max_lines=1)
    assert partial.append_lines == ("[*] multi-a",)
    assert view.pending_lines == 2
    removed = view.clear()
    assert removed == 1
    assert view.pending_lines == 0
    assert view.line_count == 0
    log.emit("after-clear", level="info")
    after = view.drain(max_lines=100)
    assert after.append_lines == ("[*] after-clear",)


def test_clear_while_paused_never_resurrects_old_events(make_log):
    log = make_log()
    log.emit("one", level="info")
    log.emit("two", level="info")
    view = LogView(log)
    view.pause(True)
    assert view.drain(max_lines=100).append_lines == ()
    assert view.line_count == 0
    view.clear()
    view.pause(False)
    assert view.drain(max_lines=100).append_lines == ()
    log.emit("new", level="info")
    assert view.drain(max_lines=100).append_lines == ("[*] new",)


def test_pause_resume_explains_skipped_events_without_flood(make_log):
    log = make_log(ui_records=5, ui_bytes=64 * 1024)
    log.emit("first", level="info")
    view = LogView(log)
    view.drain(max_lines=100)
    view.pause(True)
    for index in range(60):
        log.emit("paused-%d" % index, level="info")
    while_paused = view.drain(max_lines=100)
    assert while_paused.append_lines == ()
    assert while_paused.skipped_events == 0
    view.pause(False)
    resumed = view.drain(max_lines=100, max_ms=8)
    assert 0 < len(resumed.append_lines) <= 5
    assert resumed.skipped_events > 0
    assert view.line_count <= 6


def test_follow_false_accumulates_unseen_until_explicit_restore(make_log):
    log = make_log()
    for index in range(3):
        log.emit("base-%d" % index, level="info")
    view = LogView(log)
    view.drain(max_lines=100)
    view.set_follow(False)
    for index in range(4):
        log.emit("unseen-%d" % index, level="info")
    delta = view.drain(max_lines=100)
    assert len(delta.append_lines) == 4
    assert delta.follow is False
    assert delta.unseen_lines == 4
    assert view.unseen_lines == 4
    view.set_follow(True)
    assert view.unseen_lines == 0
    restored = view.drain(max_lines=100)
    assert restored.unseen_lines == 0
    assert restored.follow is True


def test_window_dual_limits_hold_and_removals_are_reported(make_log):
    log = make_log()
    view = LogView(log, max_lines=3, max_bytes=1024)
    for index in range(10):
        log.emit("window-%d" % index, level="info")
    delta = view.drain(max_lines=100)
    assert len(delta.append_lines) == 10
    assert view.line_count == 3
    assert delta.remove_lines == 7
    assert view.lines() == delta.append_lines[-3:]

    byte_log = make_log()
    byte_view = LogView(byte_log, max_lines=1000, max_bytes=256)
    for index in range(30):
        byte_log.emit("x" * 20, level="info")
    byte_delta = byte_view.drain(max_lines=1000)
    assert len(byte_delta.append_lines) == 30
    assert byte_view.line_count < 30
    assert byte_view.retained_bytes <= 256
    assert byte_delta.remove_lines == 30 - byte_view.line_count


def test_single_long_line_is_truncated_to_the_window_budget(make_log):
    log = make_log()
    log.emit("a" * 2000, level="info")
    view = LogView(log, max_lines=10, max_bytes=256)
    delta = view.drain(max_lines=100)
    assert len(delta.append_lines) == 1
    assert view.line_count == 1
    assert view.retained_bytes <= 256
    line = view.lines()[0]
    assert line.endswith("…[line truncated]")
    assert "\ufffd" not in line
    assert len(line.encode("utf-8")) <= 256


def test_skipped_events_counts_ring_gaps(make_log):
    log = make_log(ui_records=3, ui_bytes=64 * 1024)
    for index in range(10):
        log.emit("gap-%d" % index, level="info")
    view = LogView(log)
    delta = view.drain(max_lines=100)
    assert [line for line in delta.append_lines][0] == "[*] gap-7"
    assert len(delta.append_lines) == 3
    assert delta.skipped_events == 7


def test_400_multiline_events_stay_bounded_and_complete(make_log):
    log = make_log(ui_records=600, ui_bytes=4 * 1024 * 1024)
    message = "\n".join("line-%03d" % index for index in range(100))
    for index in range(400):
        log.emit(message, level="info", code="bulk")
    view = LogView(log)
    total = 0
    for _ in range(600):
        delta = view.drain(max_lines=100, max_bytes=32 * 1024, max_ms=100)
        total += len(delta.append_lines)
        assert view.line_count <= view.max_lines
        assert view.retained_bytes <= view.max_bytes
        if not delta.append_lines and view.pending_lines == 0:
            break
    assert total == 400 * 100
    assert view.line_count <= view.max_lines
    assert view.retained_bytes <= view.max_bytes


def test_invalid_view_and_drain_arguments_are_rejected(make_log):
    log = make_log()
    with pytest.raises(ValueError):
        LogView(object())
    with pytest.raises(ValueError):
        LogView(log, max_lines=0)
    with pytest.raises(ValueError):
        LogView(log, max_bytes=100)
    with pytest.raises(ValueError):
        LogView(log, clock=5)
    view = LogView(log)
    with pytest.raises(ValueError):
        view.drain(max_ms=-1)
    with pytest.raises(ValueError):
        view.drain(max_lines=-1)
    with pytest.raises(ValueError):
        view.drain(max_bytes=-1)
    with pytest.raises(ValueError):
        view.pause(1)
    with pytest.raises(ValueError):
        view.set_follow("yes")


def test_view_delta_is_frozen_and_has_required_fields():
    delta = ViewDelta(append_lines=("a",), remove_lines=1, follow=False,
                      skipped_events=2, unseen_lines=3)
    assert dataclasses.is_dataclass(delta)
    assert (delta.append_lines, delta.remove_lines, delta.follow,
            delta.skipped_events, delta.unseen_lines) == (("a",), 1, False, 2, 3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        delta.follow = True
