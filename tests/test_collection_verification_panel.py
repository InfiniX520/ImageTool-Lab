#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""U1-A/W5: the pending-verification panel and its controller wiring.

Every test is offline: no SQLite, no file system, no network.  The widget tests
use a real Tk root (one process, one file) and the controller tests use fake
ports, so the panel behaviour is proven without a library or an account.

The controller contract under test: one bounded read per visit, one serial
chain of single-page recoveries with a fresh command id each, a stopped pass on
the first failure, and nothing dispatched after close.
"""

from __future__ import annotations

import re
import tkinter as tk
from concurrent.futures import Future

import pytest

from gui.collection_controller import CollectionController
from gui.collection_theme import apply_collection_styles
from gui.collection_view import CollectionView
from gui.verification_panel import (
    BATCH_LIMIT,
    EMPTY_TEXT,
    ERROR_TEXT,
    READY_TEXT,
    ROW_LIMIT,
    VerificationPanel,
    describe_result,
    outcome_text,
    output_summary,
    reason_text,
    row_key,
    state_text,
    summary_text,
)

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_BATCH = "batch-1"


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _row(illust="100", page=0, delivery="d-1", *, batch=_BATCH,
         state="needs_verification", error="artifact_changed",
         recoverable=True, output="abcdef0123456789",
         updated="2026-11-14T02:00:00+00:00", batch_phase="running",
         blocked_reason=None, attempts=1, retry_at=None):
    return {
        "batch_id": batch,
        "illust_id": illust,
        "page_index": page,
        "delivery_id": delivery,
        "state": state,
        "error_code": error,
        "recoverable": recoverable,
        "batch_phase": batch_phase,
        "blocked_reason": blocked_reason,
        "attempts": attempts,
        "retry_at": retry_at,
        "output_key": output,
        "updated_at": updated,
    }


def _key(illust="100", page=0, delivery="d-1", batch=_BATCH):
    return row_key(batch, illust, page, delivery)


def _resolved(value):
    future = Future()
    future.set_result(value)
    return future


def _summary(counts, total=908):
    return {"batch_id": _BATCH, "phase": "paused", "counts": counts,
            "recoverable": counts.get("needs_verification", 0),
            "total_pages": total}


class _FakeRoot:
    def __init__(self):
        self.delays = []

    def after(self, delay, callback):
        self.delays.append(delay)
        return "after-%d" % len(self.delays)

    def after_cancel(self, token):
        return None


class _FakeView:
    """Records every call the controller makes; no widgets involved."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return record

    def named(self, name):
        return [call for call in self.calls if call[0] == name]


class _FakeSession:
    # Mirrors the real port: the controller reads identity when it refreshes the
    # overview, and a fake that lacks it turns an unrelated assertion into an
    # AttributeError.
    identity = None

    def __init__(self, results=None, *, error=None, future_error=None):
        self.commands = []
        self.results = results or {}
        self.error = error
        self.future_error = future_error

    def recover_verification_page(self, batch_id, illust_id, page_index, *,
                                  expected_delivery_id=None, command_id=None):
        self.commands.append({"batch_id": batch_id, "illust_id": illust_id,
                              "page_index": page_index, "command_id": command_id,
                              "expected_delivery_id": expected_delivery_id})
        if self.error is not None:
            raise self.error
        result = Future()
        if self.future_error is not None:
            result.set_exception(self.future_error)
            return result
        result.set_result(self.results.get(illust_id, {
            "outcome": "reused", "reason": "hash_match",
            "illust_id": illust_id, "page_index": page_index}))
        return result


class _FakeRuntime:
    def __init__(self, rows=(), summary=None, *, error=None):
        self.rows = tuple(rows)
        self.summary = summary if summary is not None else _summary({})
        self.error = error
        self.requests = []

    def verification_pages(self, *, batch_id=None, states=None, after=None, limit=50):
        self.requests.append(("pages", batch_id, limit))
        future = Future()
        if self.error is not None:
            future.set_exception(self.error)
        else:
            future.set_result({"rows": self.rows, "next_cursor": None,
                               "truncated": False, "stats": {}})
        return future

    def verification_summary(self, *, batch_id=None):
        self.requests.append(("summary", batch_id))
        future = Future()
        if self.error is not None:
            future.set_exception(self.error)
        else:
            future.set_result(self.summary)
        return future

    def overview(self, *, after=None, limit=100):
        self.requests.append(("overview", after, limit))
        future = Future()
        future.set_result({"rows": (), "next_cursor": None, "revision": 1})
        return future


class _FakeDialogs:
    def __init__(self):
        self.errors = []
        self.infos = []

    def error(self, text):
        self.errors.append(text)

    def info(self, title, text):
        self.infos.append((title, text))


def _controller(rows, summary, session):
    view, root, dialogs = _FakeView(), _FakeRoot(), _FakeDialogs()
    controller = CollectionController(root, view, session, dialogs=dialogs)
    controller.runtime = _FakeRuntime(rows, summary)
    return controller, view, dialogs


def _open(controller, view, *, polls=2):
    controller.handle("navigate", {"page": "verify"})
    for _ in range(polls):
        controller._poll()
    return view.named("set_verification_rows")


@pytest.fixture(scope="module")
def tk_root():
    """One interpreter for the whole file.

    Creating a second Tk root in the same process after the first one was
    destroyed fails in this environment ('Can't find a usable tk.tcl'), so the
    root is created once and every test builds only widgets under it.
    """
    root = tk.Tk()
    root.withdraw()
    apply_collection_styles(root)
    try:
        yield root
    finally:
        root.destroy()


@pytest.fixture()
def panel(tk_root):
    calls = []
    widget = VerificationPanel(tk_root,
        lambda action, payload: calls.append((action, payload)))
    widget.pack(fill="both", expand=True)
    try:
        yield widget, calls, tk_root
    finally:
        widget.dispose()
        widget.destroy()


@pytest.fixture()
def mounted(tk_root):
    calls = []
    view = CollectionView(tk_root, lambda action, payload: calls.append((action, payload)))
    try:
        yield view, calls, tk_root
    finally:
        view.dispose()
        view.destroy()


# ----------------------------------------------------------------------
# wording and result lines (no Tk)
# ----------------------------------------------------------------------


def test_every_documented_reason_code_has_plain_chinese_wording():
    documented = (
        "hash_match", "artifact_missing", "repair_already_pending",
        "artifact_changed", "artifact_unreadable", "artifact_root_unavailable",
        "artifact_changed_during_check", "observation_unknown",
        "observation_unavailable", "no_recorded_artifact",
        "cross_task_reuse_disabled", "already_completed", "batch_not_running",
        "page_not_in_batch", "page_owner_mismatch", "work_budget_exhausted",
        "output_blocked_by_other_task", "state_changed", "stale_preflight",
        "unsupported_previous_state",
    )
    for code in documented:
        text = reason_text(code)
        assert text and text != code, code
        assert "未知" not in text, (code, text)


def test_an_unknown_reason_keeps_its_code_visible():
    assert reason_text("brand_new_verdict") == "未知原因（brand_new_verdict）"
    assert reason_text(None) == "—"


def test_state_and_outcome_texts_cover_every_known_code():
    for code in ("needs_verification", "recovery_needed", "pending", "suspended",
                 "retry_wait", "transferring", "validating", "prepared", "completed"):
        assert state_text(code) != code
    assert state_text("mystery_state") == "未知状态（mystery_state）"
    for code in ("reused", "repair_queued", "held", "completed", "refused"):
        assert outcome_text(code) != code
    assert outcome_text("surprise") == "未知结果（surprise）"


def test_reused_result_says_no_download_is_needed():
    text, tone = describe_result({"outcome": "reused", "reason": "hash_match",
                                  "illust_id": "77", "page_index": 0})
    assert tone == "success"
    assert "不需要重新下载" in text and "77" in text and "第 1 页" in text


def test_held_result_promises_the_file_is_untouched():
    text, tone = describe_result({"outcome": "held", "reason": "artifact_changed",
                                  "illust_id": "77", "page_index": 2})
    assert tone == "warning"
    assert "没有删除、没有覆盖" in text and "第 3 页" in text


def test_refused_result_says_nothing_changed():
    text, tone = describe_result({"outcome": "refused",
                                  "reason": "batch_not_running",
                                  "illust_id": "77", "page_index": 0})
    assert tone == "warning" and "本次没有改动任何记录" in text


def test_repair_result_points_at_the_next_pass():
    text, _tone = describe_result({"outcome": "repair_queued",
                                   "reason": "artifact_missing",
                                   "illust_id": "77", "page_index": 0})
    assert "只补这一页" in text


def test_unknown_outcome_or_malformed_result_is_reported_not_swallowed():
    text, tone = describe_result({"outcome": "mystery", "reason": "mystery_reason"})
    assert tone == "warning" and "未知结果" in text and "未知原因" in text
    assert describe_result(None) == ("核验结果无法读取（返回格式不正确）。", "danger")


def test_summary_separates_a_paused_batch_from_a_verification_hold():
    text = summary_text({"total_pages": 908,
                         "counts": {"needs_verification": 2, "suspended": 891,
                                    "completed": 11}})
    assert "本批 908 页" in text
    assert "待核验 2 页" in text
    assert "停住 891 页（批次暂停中，恢复批次后自动继续）" in text
    assert "已完成 11 页" in text


def test_summary_says_a_paused_batch_must_be_resumed_before_recovery():
    text = summary_text({"phase": "paused", "total_pages": 908, "linked_pages": 908,
                         "counts": {"needs_verification": 2}})
    assert "批次已暂停，恢复后才能核验或补下载" in text
    running = summary_text({"phase": "running", "total_pages": 3,
                            "counts": {"needs_verification": 1}})
    assert "恢复后才能核验" not in running


def test_summary_counts_the_pages_that_have_no_delivery_of_their_own():
    """A page can carry a recorded artifact instead of a delivery of its own.

    Reporting it as "0 pages registered" (the old line) read as the opposite of
    the truth, so the header now names the existing pages and the remainder.
    """
    text = summary_text({"phase": "running", "total_pages": 908,
                         "linked_pages": 900, "counts": {"completed": 900}})
    assert "本批 908 页" in text and "尚未开始 8 页" in text
    assert "已登记 0 页" not in text and "已登记 900 页" not in text


def test_summary_skips_zero_counts_and_survives_a_missing_payload():
    text = summary_text({"total_pages": 3, "counts": {"needs_verification": 0,
                                                       "suspended": 0}})
    assert "停住" not in text and "待核验" not in text
    assert summary_text(None) == ""


def test_output_summary_shows_a_digest_prefix_and_refuses_a_path():
    assert output_summary("abcdef0123456789abcdef") == "abcdef012345…"
    assert output_summary("abcdef") == "abcdef"
    assert output_summary(r"C:\Pictures\a.png") == "—"
    assert output_summary("/home/user/a.png") == "—"
    assert output_summary(None) == "—"


def test_row_key_separates_pages_and_deliveries_of_one_artwork():
    first = _key(page=0, delivery="d-1")
    assert first != _key(page=1, delivery="d-1")
    assert first != _key(page=0, delivery="d-2")
    assert row_key(_BATCH, "100", "0", "d-1") is None
    assert row_key(None, "100", 0, "d-1") is None


# ----------------------------------------------------------------------
# panel widget
# ----------------------------------------------------------------------


def test_panel_empty_state_explains_itself_and_disables_both_actions(panel):
    widget, calls, _root = panel
    widget.set_rows([])
    assert widget._hint.cget("text") == EMPTY_TEXT
    assert EMPTY_TEXT == "当前批次没有待核验页面；停住的页会在这里出现。"
    assert "disabled" in widget._recover.state()
    assert "disabled" in widget._batch.state()
    assert widget._wrap.winfo_manager() == ""
    assert calls == []


def test_panel_renders_rows_with_localised_state_reason_and_digest(panel):
    widget, _calls, _root = panel
    widget.set_rows([_row(illust="100", page=0, delivery="d-1"),
                     _row(illust="200", page=3, delivery="d-2",
                          state="recovery_needed", error="artifact_missing")],
                    page_text="本批 908 页 · 待核验 2 页")
    tree = widget._tree
    children = tree.get_children()
    assert len(children) == 2
    assert tree.set(_key(illust="100", delivery="d-1"), "state") == "待核验"
    assert (tree.set(_key(illust="100", delivery="d-1"), "reason")
            == "文件内容已变，保留待你决定")
    assert tree.set(_key(illust="100", delivery="d-1"), "output") == "abcdef012345…"
    assert tree.set(_key(illust="200", page=3, delivery="d-2"), "page") == "第 4 页"
    assert tree.set(_key(illust="200", page=3, delivery="d-2"), "reason") == (
        "文件不在原位，已排入补下载")
    assert widget._summary.cget("text") == "本批 908 页 · 待核验 2 页"


def test_panel_selection_enables_the_single_page_action(panel):
    widget, _calls, root = panel
    widget.set_rows([_row()])
    assert "disabled" in widget._recover.state()
    widget._tree.selection_set(_key())
    root.update()
    assert "disabled" not in widget._recover.state()
    assert widget.selected_key() == _key()


def test_panel_sends_the_frozen_identity_of_the_selected_row(panel):
    widget, calls, root = panel
    widget.set_rows([_row(illust="100", page=2, delivery="d-1")])
    widget._tree.selection_set(_key(illust="100", page=2, delivery="d-1"))
    root.update()
    widget._recover.invoke()
    assert calls == [("verification_recover",
                      {"key": _key(illust="100", page=2, delivery="d-1")})]


def test_panel_refuses_a_row_that_is_not_recoverable(panel):
    widget, calls, root = panel
    widget.set_rows([_row(state="suspended", error=None, recoverable=False)])
    widget._tree.selection_set(_key())
    root.update()
    assert "disabled" in widget._recover.state()
    widget._emit_recover()
    assert calls == []
    assert "当前不可核验" in widget._result.cget("text")


def test_panel_disables_actions_and_says_the_batch_is_paused(panel):
    widget, calls, root = panel
    widget.set_rows([_row(state="needs_verification", error="artifact_changed",
                          recoverable=False, batch_phase="paused",
                          blocked_reason="batch_not_running")],
        page_text="本批 908 页 · 停住 891 页")
    tree = widget._tree
    cell = tree.set(_key(), "reason")
    assert "批次已暂停，恢复后才能核验" in cell
    assert "disabled" in widget._recover.state()
    assert "disabled" in widget._batch.state()
    widget._tree.selection_set(_key())
    root.update()
    assert "disabled" in widget._recover.state()
    widget._emit_recover()
    widget._emit_batch()
    assert calls == []
    assert "批次已暂停，恢复后才能核验" in widget._result.cget("text")


def test_panel_enables_recovery_for_a_running_batch_hold(panel):
    widget, calls, root = panel
    widget.set_rows([_row(state="needs_verification",
                          recoverable=True, batch_phase="running")])
    widget._tree.selection_set(_key())
    root.update()
    assert "disabled" not in widget._recover.state()
    assert "disabled" not in widget._batch.state()
    assert "批次已暂停" not in widget._tree.set(_key(), "reason")
    widget._recover.invoke()
    assert calls == [("verification_recover", {"key": _key()})]


def test_panel_batch_is_capped_and_carries_only_recoverable_keys(panel):
    widget, calls, _root = panel
    rows = [_row(illust=str(100 + index), delivery="d-%d" % index)
            for index in range(BATCH_LIMIT + 5)]
    rows.append(_row(illust="900", delivery="d-900", state="suspended",
                     recoverable=False))
    widget.set_rows(rows)
    widget._batch.invoke()
    assert len(calls) == 1 and calls[0][0] == "verification_batch"
    keys = calls[0][1]["keys"]
    assert len(keys) == BATCH_LIMIT
    assert _key(illust="900", delivery="d-900") not in keys


def test_panel_batch_action_is_disabled_when_nothing_is_recoverable(panel):
    widget, calls, _root = panel
    widget.set_rows([_row(state="suspended", recoverable=False)])
    assert "disabled" in widget._batch.state()
    widget._emit_batch()
    assert calls == []
    assert "没有可以核验的页面" in widget._result.cget("text")


def test_panel_truncates_rows_above_the_display_limit(panel):
    widget, _calls, _root = panel
    rows = [_row(illust=str(index), delivery="d-%d" % index)
            for index in range(ROW_LIMIT + 30)]
    widget.set_rows(rows)
    assert len(widget.rows()) == ROW_LIMIT
    assert len(widget._tree.get_children()) == ROW_LIMIT


def test_panel_drops_a_selection_that_disappeared_on_refresh(panel):
    widget, calls, root = panel
    widget.set_rows([_row(illust="100", delivery="d-1")])
    widget._tree.selection_set(_key(illust="100", delivery="d-1"))
    root.update()
    assert widget.selected_key() is not None
    widget.set_rows([_row(illust="200", delivery="d-2")])
    assert widget.selected_key() is None
    assert "disabled" in widget._recover.state()
    widget._emit_recover()
    assert calls == []


def test_panel_result_uses_the_danger_style_for_a_failure(panel):
    widget, _calls, _root = panel
    widget.set_result("核验没有完成。", "danger")
    assert widget._result.cget("style") == "CollectionStatusDanger.TLabel"
    widget.set_result("已完成 2/2 页。", "success")
    assert widget._result.cget("style") == "CollectionStatusSuccess.TLabel"


def test_panel_stops_emitting_and_updating_after_dispose(panel):
    widget, calls, _root = panel
    widget.set_rows([_row()])
    widget.dispose()
    widget.set_rows([_row(illust="200", delivery="d-2")])
    widget.set_result("ignored")
    widget.set_busy(True, text="ignored")
    widget._emit_batch()
    widget._emit_recover()
    assert calls == []
    assert widget.rows() == ()


def test_panel_requires_a_callable_action_handler(tk_root):
    with pytest.raises(ValueError):
        VerificationPanel(tk_root, None)


def test_panel_busy_state_blocks_both_actions_and_shows_progress(panel):
    widget, calls, root = panel
    widget.set_rows([_row()])
    widget._tree.selection_set(_key())
    root.update()
    widget.set_busy(True, text="已完成 0/1")
    assert "disabled" in widget._recover.state()
    assert "disabled" in widget._batch.state()
    assert widget._progress.cget("text") == "已完成 0/1"
    widget._emit_recover()
    widget._emit_batch()
    assert calls == []
    widget.set_busy(False)
    assert "disabled" not in widget._recover.state()


# ----------------------------------------------------------------------
# collection view integration
# ----------------------------------------------------------------------


def test_view_mounts_the_panel_on_its_own_navigation_page(mounted):
    view, calls, root = mounted
    slot = view.verification_slot
    assert isinstance(slot, VerificationPanel)
    assert view._nav_buttons["verify"].cget("text") == "核验"
    view._nav_buttons["verify"].invoke()
    root.update()
    assert calls[-1] == ("navigate", {"page": "verify"})
    assert view._pages["verify"].winfo_manager() == "grid"
    assert view._pages["records"].winfo_manager() == ""


def test_view_forwards_rows_results_and_busy_state_to_the_panel(mounted):
    view, _calls, root = mounted
    view.set_verification_rows([_row()], page_text="本批 1 页 · 待核验 1 页")
    panel = view.verification_slot
    assert len(panel.rows()) == 1
    assert panel._summary.cget("text") == "本批 1 页 · 待核验 1 页"
    view.set_verification_result("作品 100 第 1 页：已复用 · 字节一致，已复用",
                                 "success")
    assert "已复用" in panel._result.cget("text")
    view.set_verification_busy(True, text="已完成 0/2")
    assert "disabled" in panel._batch.state()
    view.set_verification_busy(False)
    assert "disabled" not in panel._batch.state()
    panel._tree.selection_set(_key())
    root.update()
    assert panel.selected_key() == _key()
    view.dispose()
    view.set_verification_rows([_row(illust="200", delivery="d-2")])
    view.set_verification_result("ignored")
    view.set_verification_busy(True, text="ignored")


# ----------------------------------------------------------------------
# controller wiring
# ----------------------------------------------------------------------


def test_controller_reads_one_bounded_page_and_one_summary_on_navigation():
    session = _FakeSession()
    controller, view, _dialogs = _controller([_row()], _summary(
        {"needs_verification": 1, "suspended": 891}), session)
    rendered = _open(controller, view)
    assert rendered, view.calls
    requests = controller.runtime.requests
    assert ("pages", None, 50) in requests
    assert ("summary", None) in requests
    # Repeated polls must not flood the shared query owner with repeat reads.
    for _ in range(3):
        controller._poll()
    assert requests.count(("pages", None, 50)) == 1
    assert requests.count(("summary", None)) == 1


def test_controller_passes_the_summary_into_the_panel_header():
    session = _FakeSession()
    controller, view, _dialogs = _controller([_row()], _summary(
        {"needs_verification": 1, "suspended": 891}), session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    calls = view.named("set_verification_rows")
    assert calls, view.calls
    _name, args, kwargs = calls[-1]
    assert len(args[0]) == 1
    text = kwargs.get("page_text", "")
    assert "停住 891 页（批次暂停中，恢复批次后自动继续）" in text
    assert "列出 1 行" in text


def test_controller_shows_the_empty_wording_when_nothing_is_held():
    session = _FakeSession()
    controller, view, _dialogs = _controller((), _summary({"suspended": 891}),
                                             session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    calls = view.named("set_verification_rows")
    assert calls[-1][1][0] == []


def test_controller_forwards_the_blocked_reason_and_phase_into_the_panel():
    session = _FakeSession()
    row = _row(recoverable=False, batch_phase="paused",
               blocked_reason="batch_not_running", attempts=2,
               retry_at="2026-11-14T03:00:00+00:00")
    controller, view, _dialogs = _controller([row], _summary({
        "needs_verification": 1}), session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    calls = view.named("set_verification_rows")
    display = calls[-1][1][0]
    assert display[0]["blocked_reason"] == "batch_not_running"
    assert display[0]["batch_phase"] == "paused"
    assert display[0]["recoverable"] is False
    assert display[0]["attempts"] == 2
    assert "批次已暂停，恢复后才能核验或补下载" in calls[-1][2]["page_text"]


def test_controller_reports_the_truncated_list_without_pretending_a_cursor():
    session = _FakeSession()
    controller, view, _dialogs = _controller([_row()], _summary({
        "needs_verification": 1}), session)
    controller.runtime.verification_pages = lambda **kwargs: _resolved({
        "rows": (_row(),), "next_cursor": ("100", "d-1"), "truncated": True,
        "stats": {}})
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    text = view.named("set_verification_rows")[-1][2]["page_text"]
    assert "还有更多未列出" in text


def test_controller_uses_a_fresh_command_id_for_every_page():
    session = _FakeSession()
    rows = [_row(illust="100", delivery="d-1"), _row(illust="200", delivery="d-2")]
    controller, view, _dialogs = _controller(rows, _summary({"needs_verification": 2}),
                                             session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_recover", {"key": _key(illust="100", delivery="d-1")})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_recover", {"key": _key(illust="200", delivery="d-2")})
    for _ in range(3):
        controller._poll()
    assert len(session.commands) == 2
    first, second = session.commands
    assert _HEX32.match(first["command_id"]) and _HEX32.match(second["command_id"])
    assert first["command_id"] != second["command_id"]
    assert (first["illust_id"], first["page_index"]) == ("100", 0)
    assert (second["illust_id"], second["page_index"]) == ("200", 0)


def test_controller_maps_a_reused_result_into_the_panel():
    session = _FakeSession({"100": {"outcome": "reused", "reason": "hash_match",
                                    "illust_id": "100", "page_index": 0}})
    controller, view, _dialogs = _controller([_row()], _summary({
        "needs_verification": 1}), session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_recover", {"key": _key()})
    for _ in range(3):
        controller._poll()
    texts = [call[1][0] for call in view.named("set_verification_result")]
    assert any("不需要重新下载" in text for text in texts), texts
    tones = [call[1][1] for call in view.named("set_verification_result")]
    assert "success" in tones


def test_controller_chains_a_batch_serially_and_caps_it_at_twenty():
    session = _FakeSession()
    rows = [_row(illust=str(100 + index), delivery="d-%d" % index)
            for index in range(25)]
    keys = [_key(illust=str(100 + index), delivery="d-%d" % index)
            for index in range(25)]
    controller, view, _dialogs = _controller(rows, _summary({
        "needs_verification": 25}), session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_batch", {"keys": keys})
    for _ in range(60):
        controller._poll()
    assert len(session.commands) == 20
    assert len({item["command_id"] for item in session.commands}) == 20
    assert [item["illust_id"] for item in session.commands] == [
        str(100 + index) for index in range(20)]
    texts = [call[1][0] for call in view.named("set_verification_result")]
    assert any("本轮核验完成：20/20 页" in text for text in texts), texts[-3:]
    assert any("已跳过" in text for text in texts)
    progress = [call[2].get("text") for call in view.named("set_verification_busy")]
    assert "已完成 0/20" in progress and "已完成 20/20" in progress


def test_controller_reports_a_row_that_vanished_instead_of_dispatching():
    session = _FakeSession()
    controller, view, _dialogs = _controller([_row()], _summary({
        "needs_verification": 1}), session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_recover", {"key": "stale/row/key"})
    for _ in range(3):
        controller._poll()
    assert session.commands == []
    texts = [call[1][0] for call in view.named("set_verification_result")]
    assert any("已不在待核验列表里" in text for text in texts), texts


def test_controller_stops_the_pass_when_dispatch_raises():
    session = _FakeSession(error=RuntimeError("queue full"))
    rows = [_row(illust="100", delivery="d-1"), _row(illust="200", delivery="d-2")]
    controller, view, dialogs = _controller(rows, _summary({
        "needs_verification": 2}), session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_batch",
                      {"keys": [_key(illust="100", delivery="d-1"),
                                _key(illust="200", delivery="d-2")]})
    for _ in range(5):
        controller._poll()
    assert len(session.commands) == 1
    assert controller._verify_busy is False
    texts = [call[1][0] for call in view.named("set_verification_result")]
    assert any("核验没有完成" in text for text in texts), texts
    assert dialogs.errors == []


def test_controller_reports_a_failed_page_without_a_modal_dialog():
    session = _FakeSession(future_error=RuntimeError("writer said no"))
    controller, view, dialogs = _controller([_row()], _summary({
        "needs_verification": 1}), session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_recover", {"key": _key()})
    for _ in range(3):
        controller._poll()
    texts = [call[1][0] for call in view.named("set_verification_result")]
    assert any("核验没有完成" in text for text in texts), texts
    assert dialogs.errors == []
    assert controller._verify_busy is False


def test_controller_dispatches_one_page_at_a_time_in_a_batch():
    """The IO owner queue holds four slots; a batch must not fill it."""
    pending = Future()
    session = _FakeSession()
    dispatched = []

    def slow(batch_id, illust_id, page_index, *, expected_delivery_id=None,
             command_id=None):
        dispatched.append((illust_id, command_id))
        return pending

    session.recover_verification_page = slow
    rows = [_row(illust=str(100 + index), delivery="d-%d" % index)
            for index in range(3)]
    keys = [_key(illust=str(100 + index), delivery="d-%d" % index)
            for index in range(3)]
    controller, _view, _dialogs = _controller(rows, _summary({
        "needs_verification": 3}), session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_batch", {"keys": keys})
    assert len(dispatched) == 1
    assert controller._verify_busy is True
    pending.set_result({"outcome": "reused", "reason": "hash_match",
                        "illust_id": "100", "page_index": 0})
    controller._poll()
    assert len(dispatched) == 2
    assert dispatched[1][1] != dispatched[0][1]
    assert dispatched[1][0] == "101"


def test_controller_dispatches_nothing_after_dispose():
    session = _FakeSession()
    controller, view, _dialogs = _controller([_row()], _summary({
        "needs_verification": 1}), session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.dispose()
    before = len(view.calls)
    controller.handle("verification_recover", {"key": _key()})
    controller.handle("verification_batch", {"keys": [_key()]})
    controller.handle("verification_refresh")
    controller._poll()
    assert session.commands == []
    assert len(view.calls) == before


def test_controller_ignores_a_second_pass_while_one_is_running():
    pending = Future()
    session = _FakeSession()
    calls = {"count": 0}

    def slow(batch_id, illust_id, page_index, *, expected_delivery_id=None,
             command_id=None):
        session.commands.append({"batch_id": batch_id, "illust_id": illust_id,
                                 "page_index": page_index, "command_id": command_id})
        calls["count"] += 1
        return pending if calls["count"] == 1 else Future()

    session.recover_verification_page = slow
    controller, view, _dialogs = _controller([_row()], _summary({
        "needs_verification": 1}), session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_recover", {"key": _key()})
    controller.handle("verification_recover", {"key": _key()})
    controller.handle("verification_batch", {"keys": [_key()]})
    assert len(session.commands) == 1
    texts = [call[1][0] for call in view.named("set_verification_result")]
    assert texts.count("正在核验，请等这一轮结束。") == 2, texts


# ----------------------------------------------------------------------
# contract v5: page scope, result aggregation and the existing-page count
# ----------------------------------------------------------------------


class _QueuedRuntime(_FakeRuntime):
    """A runtime whose reads stay pending until the test resolves them."""

    def __init__(self, rows=(), summary=None):
        super().__init__(rows, summary)
        self.pending = {"pages": [], "summary": []}

    def verification_pages(self, *, batch_id=None, states=None, after=None, limit=50):
        self.requests.append(("pages", batch_id, limit))
        future = Future()
        self.pending["pages"].append(future)
        return future

    def verification_summary(self, *, batch_id=None):
        self.requests.append(("summary", batch_id))
        future = Future()
        self.pending["summary"].append(future)
        return future


def _pin_task_rows(controller, rows):
    """Seed the task table and stop the periodic overview from replacing it.

    The real overview re-supplies the same jobs, so an empty fake overview would
    only erase this fixture's own rows; pinning the next refresh keeps the test
    about the verification scope instead of about the task list.
    """
    controller._task_rows = dict(rows)
    controller._next_overview = float("inf")


def _queued_controller(rows=(), summary=None):
    view, root, dialogs = _FakeView(), _FakeRoot(), _FakeDialogs()
    controller = CollectionController(root, view, _FakeSession(), dialogs=dialogs)
    controller.runtime = _QueuedRuntime(rows, summary if summary is not None else _summary({}))
    _pin_task_rows(controller, {
        "job-a": {"batch_id": "batch-a"}, "job-b": {"batch_id": "batch-b"}})
    # The two task lookups the selection path issues must not resolve here.
    controller.runtime.request = lambda job: Future()
    controller.runtime.snapshot = lambda job: Future()
    return controller, view


def _resolve(future, value):
    future.set_result(value)


def _rows_page(rows, *, batch=None, truncated=False):
    return {"rows": list(rows), "next_cursor": None, "truncated": truncated,
            "stats": {"batch_id": batch}}


def test_summary_reports_artifact_only_pages_as_existing():
    text = summary_text({"phase": "running", "total_pages": 1,
                         "linked_pages": 0, "existing_pages": 1,
                         "unstarted_pages": 0, "counts": {}})
    assert "本批 1 页" in text and "已有 1 页" in text
    assert "已登记 0 页" not in text


def test_summary_prefers_a_reported_unstarted_count_over_arithmetic():
    text = summary_text({"phase": "running", "total_pages": 10,
                         "linked_pages": 10, "existing_pages": 0,
                         "unstarted_pages": 3, "counts": {}})
    assert "尚未开始 3 页" in text


def test_summary_keeps_a_quiet_library_to_one_segment():
    assert summary_text({"phase": "running", "total_pages": 3,
                         "linked_pages": 3, "counts": {}}) == "本批 3 页"


def test_controller_drops_a_summary_that_belongs_to_an_older_selection():
    controller, view = _queued_controller()
    controller.handle("select_task", {"id": "job-a"})
    controller.handle("navigate", {"page": "verify"})
    for _ in range(2):
        controller._poll()
    stale_summary = controller.runtime.pending["summary"][0]
    controller.handle("select_task", {"id": "job-b"})
    controller.handle("navigate", {"page": "verify"})
    for _ in range(2):
        controller._poll()
    _resolve(stale_summary, {"total_pages": 908, "counts": {"needs_verification": 7}})
    controller.runtime.pending["pages"][-1].set_result(_rows_page([]))
    for _ in range(3):
        controller._poll()
    rendered = [call[1][0] for call in view.named("set_verification_rows")]
    assert rendered, view.calls
    assert all("待核验 7 页" not in text for text in rendered), rendered
    assert controller._verify_summary != {"total_pages": 908,
                                          "counts": {"needs_verification": 7}}
    # The new selection's own summary still lands.
    controller.runtime.pending["summary"][-1].set_result(
        {"total_pages": 2, "counts": {"needs_verification": 2}})
    for _ in range(2):
        controller._poll()
    assert "待核验 2 页" in view.named("set_verification_rows")[-1][2]["page_text"]


def test_controller_drops_rows_that_name_another_batch():
    controller, view = _queued_controller()
    controller.handle("select_task", {"id": "job-a"})
    controller.handle("navigate", {"page": "verify"})
    for _ in range(2):
        controller._poll()
    controller.handle("select_task", {"id": "job-b"})
    controller.handle("navigate", {"page": "verify"})
    for _ in range(2):
        controller._poll()
    current = controller.runtime.pending["pages"][-1]
    _resolve(current, _rows_page([_row(batch="batch-a")], batch="batch-a"))
    for _ in range(3):
        controller._poll()
    assert controller._verify_display == []
    assert controller._verify_rows == {}
    assert view.named("set_verification_rows")[-1][1][0] == []
    assert "另有 1 行属于其他批次" in view.named("set_verification_rows")[-1][2]["page_text"]


def test_controller_refuses_a_key_from_a_previous_epoch_after_a_new_read():
    controller, _view = _queued_controller()
    controller.handle("select_task", {"id": "job-a"})
    controller.handle("navigate", {"page": "verify"})
    for _ in range(2):
        controller._poll()
    _resolve(controller.runtime.pending["pages"][0],
             _rows_page([_row(batch="batch-a")], batch="batch-a"))
    for _ in range(2):
        controller._poll()
    assert _key(batch="batch-a") in controller._verify_rows
    controller.handle("select_task", {"id": "job-b"})
    controller.handle("navigate", {"page": "verify"})
    for _ in range(2):
        controller._poll()
    assert controller._verify_rows == {}
    controller.handle("verification_recover", {"key": _key(batch="batch-a")})
    for _ in range(2):
        controller._poll()
    assert controller.session.commands == []


def test_controller_stops_the_rest_of_a_pass_when_the_selection_changes():
    session = _FakeSession()
    rows = [_row(illust=str(200 + index), delivery="d-%d" % index) for index in range(3)]
    keys = [_key(illust=str(200 + index), delivery="d-%d" % index) for index in range(3)]
    controller, view, _dialogs = _controller(rows, _summary({"needs_verification": 3}), session)
    _pin_task_rows(controller, {"job-a": {"batch_id": _BATCH},
                                "job-b": {"batch_id": "batch-b"}})
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    pending = Future()

    def slow(batch_id, illust_id, page_index, *, expected_delivery_id=None,
             command_id=None):
        session.commands.append({"batch_id": batch_id, "illust_id": illust_id,
                                 "page_index": page_index, "command_id": command_id})
        return pending

    session.recover_verification_page = slow
    controller.handle("verification_batch", {"keys": keys})
    assert len(session.commands) == 1
    controller.handle("select_task", {"id": "job-b"})
    pending.set_result({"outcome": "reused", "reason": "hash_match",
                        "illust_id": "200", "page_index": 0})
    for _ in range(4):
        controller._poll()
    assert len(session.commands) == 1
    assert controller._verify_busy is False
    last = view.named("set_verification_result")[-1]
    assert last[1][1] == "warning"
    assert "上一批（批次 %s）已停止" % _BATCH in last[1][0]
    assert "还剩 2 页未处理" in last[1][0]


def test_controller_keeps_an_in_flight_command_on_its_original_batch():
    session = _FakeSession()
    controller, view, _dialogs = _controller([_row(batch=_BATCH)], _summary(
        {"needs_verification": 1}), session)
    _pin_task_rows(controller, {"job-a": {"batch_id": _BATCH},
                                "job-b": {"batch_id": "batch-b"}})
    pending = Future()
    calls = []

    def slow(batch_id, illust_id, page_index, *, expected_delivery_id=None,
             command_id=None):
        calls.append((batch_id, illust_id, page_index, command_id))
        return pending

    session.recover_verification_page = slow
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_recover", {"key": _key()})
    controller.handle("select_task", {"id": "job-b"})
    pending.set_result({"outcome": "held", "reason": "artifact_changed",
                        "illust_id": "100", "page_index": 0})
    for _ in range(4):
        controller._poll()
    assert [item[0] for item in calls] == [_BATCH]
    last = view.named("set_verification_result")[-1]
    assert last[1][1] != "success"
    assert "上一批" in last[1][0]


def test_controller_touches_no_widget_when_a_read_lands_after_dispose():
    controller, view = _queued_controller()
    controller.handle("select_task", {"id": "job-a"})
    controller.handle("navigate", {"page": "verify"})
    for _ in range(2):
        controller._poll()
    pages = controller.runtime.pending["pages"][0]
    controller.dispose()
    before = len(view.calls)
    _resolve(pages, _rows_page([_row(batch="batch-a")], batch="batch-a"))
    controller._poll()
    assert len(view.calls) == before


def test_controller_ends_a_mixed_batch_on_the_worst_outcome():
    session = _FakeSession({
        "100": {"outcome": "reused", "reason": "hash_match",
                "illust_id": "100", "page_index": 0},
        "200": {"outcome": "held", "reason": "artifact_changed",
                "illust_id": "200", "page_index": 0},
    })
    rows = [_row(illust="100", delivery="d-1"), _row(illust="200", delivery="d-2")]
    keys = [_key(illust="100", delivery="d-1"), _key(illust="200", delivery="d-2")]
    controller, view, _dialogs = _controller(rows, _summary({"needs_verification": 2}), session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_batch", {"keys": keys})
    for _ in range(10):
        controller._poll()
    last = view.named("set_verification_result")[-1]
    assert last[1][1] == "warning"
    assert "本轮处理 2/2 页" in last[1][0]
    assert "已复用 1" in last[1][0] and "保留待定 1" in last[1][0]
    assert "artifact_changed" not in last[1][0]
    assert "文件内容已变" in last[1][0]


def test_controller_does_not_call_a_queued_repair_a_verified_recovery():
    session = _FakeSession({"100": {"outcome": "repair_queued",
                                    "reason": "artifact_missing",
                                    "illust_id": "100", "page_index": 0}})
    controller, view, _dialogs = _controller([_row(error="artifact_missing")],
                                             _summary({"needs_verification": 1}), session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_recover", {"key": _key()})
    for _ in range(5):
        controller._poll()
    last = view.named("set_verification_result")[-1]
    assert last[1][1] != "success"
    assert "已排补下 1" in last[1][0]
    assert "本轮核验完成" not in last[1][0]


def test_controller_keeps_the_unknown_reason_in_the_final_line():
    session = _FakeSession({"100": {"outcome": "mystery", "reason": "brand_new_reason",
                                    "illust_id": "100", "page_index": 0}})
    controller, view, _dialogs = _controller([_row()], _summary({"needs_verification": 1}),
                                             session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_recover", {"key": _key()})
    for _ in range(5):
        controller._poll()
    last = view.named("set_verification_result")[-1]
    assert last[1][1] == "warning"
    assert "无法识别 1" in last[1][0]
    assert "未知原因（brand_new_reason）" in last[1][0]


def test_controller_reports_how_far_a_failing_pass_got():
    class _HalfBroken(_FakeSession):
        def recover_verification_page(self, batch_id, illust_id, page_index, *,
                                      expected_delivery_id=None, command_id=None):
            self.commands.append({"batch_id": batch_id, "illust_id": illust_id,
                                  "page_index": page_index, "command_id": command_id,
                                  "expected_delivery_id": expected_delivery_id})
            result = Future()
            if len(self.commands) == 2:
                result.set_exception(RuntimeError("writer said no"))
            else:
                result.set_result({"outcome": "reused", "reason": "hash_match",
                                   "illust_id": illust_id, "page_index": page_index})
            return result

    session = _HalfBroken()
    rows = [_row(illust="100", delivery="d-1"), _row(illust="200", delivery="d-2")]
    keys = [_key(illust="100", delivery="d-1"), _key(illust="200", delivery="d-2")]
    controller, view, _dialogs = _controller(rows, _summary({"needs_verification": 2}), session)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_batch", {"keys": keys})
    for _ in range(10):
        controller._poll()
    last = view.named("set_verification_result")[-1]
    assert last[1][1] == "danger"
    assert "核验没有完成" in last[1][0]
    assert "本轮已完成 1/2 页" in last[1][0]
    assert controller._verify_busy is False

def test_controller_drops_a_read_from_an_earlier_visit_to_the_same_batch():
    """A->B->A keeps the batch id but not the read.

    The row's batch still matches, so only the epoch can tell that this answer
    belongs to the first visit and must not repopulate the list.
    """
    controller, view = _queued_controller()
    controller.handle("select_task", {"id": "job-a"})
    controller.handle("navigate", {"page": "verify"})
    for _ in range(2):
        controller._poll()
    first = controller.runtime.pending["pages"][0]
    controller.handle("select_task", {"id": "job-b"})
    controller.handle("select_task", {"id": "job-a"})
    controller.handle("navigate", {"page": "verify"})
    for _ in range(2):
        controller._poll()
    _resolve(first, _rows_page([_row(batch="batch-a")], batch="batch-a"))
    for _ in range(3):
        controller._poll()
    assert controller._verify_display == []
    assert controller._verify_rows == {}


def test_controller_refuses_a_cached_row_whose_epoch_is_not_current():
    """Second gate: even a row left in the action table cannot leave this scope."""
    session = _FakeSession()
    controller, view, _dialogs = _controller([], _summary({}), session)
    # This case exercises the action gate, not the periodic overview read.
    controller._next_overview = float("inf")
    key = _key()
    controller._verify_rows[key] = {
        "batch_id": _BATCH, "illust_id": "100", "page_index": 0, "delivery_id": "d-1",
        "_batch": _BATCH, "_epoch": controller._verification_epoch - 1}
    controller.handle("verification_recover", {"key": key})
    for _ in range(2):
        controller._poll()
    assert session.commands == []
    assert any("已不在待核验列表里" in call[1][0]
               for call in view.named("set_verification_result"))

# ----------------------------------------------------------------------
# contract v6 sec.4: paging, the read error state and the expected identity
# ----------------------------------------------------------------------


class _RecordingPanel:
    """Stands in for the mounted panel and records the state it is told."""

    def __init__(self):
        self.states = []

    def set_page_state(self, **kwargs):
        self.states.append(dict(kwargs))

    @property
    def last(self):
        return self.states[-1] if self.states else {}


class _SlotView(_FakeView):
    """The real view exposes its mounted panel; this fake does the same."""

    def __init__(self):
        super().__init__()
        self.panel = _RecordingPanel()

    @property
    def verification_slot(self):
        return self.panel


class _PagedRuntime(_FakeRuntime):
    """One page per cursor, and every cursor the controller asked for."""

    def __init__(self, pages, summary=None):
        super().__init__((), summary)
        self.pages = dict(pages)
        self.cursors = []

    def verification_pages(self, *, batch_id=None, states=None, after=None, limit=50):
        self.requests.append(("pages", batch_id, limit))
        self.cursors.append(after)
        future = Future()
        value = self.pages.get(after)
        if isinstance(value, Exception):
            future.set_exception(value)
        else:
            future.set_result(value)
        return future


def _page(rows, *, cursor=None, total=None, truncated=None, revision=1):
    return {"rows": list(rows), "next_cursor": cursor,
            "truncated": bool(cursor) if truncated is None else bool(truncated),
            "read_revision": revision,
            "stats": {"total": len(rows) if total is None else total}}


def _paged_controller(pages, summary=None):
    view, root, dialogs = _SlotView(), _FakeRoot(), _FakeDialogs()
    controller = CollectionController(root, view, _FakeSession(), dialogs=dialogs)
    controller.runtime = _PagedRuntime(pages, summary)
    # The periodic overview would replace the pinned task table; this section
    # is about one list, so the next refresh is parked instead of fired.
    controller._next_overview = float("inf")
    return controller, view, controller.runtime


def _fifty_one_rows():
    return [_row(illust=str(1000 + index), delivery="d-%d" % index)
            for index in range(51)]


def _fifty_one_pages():
    rows = _fifty_one_rows()
    first = rows[:50]
    cursor = (_BATCH, "1049", 0, "d-49")
    return {None: _page(first, cursor=cursor, total=51, revision=7),
            cursor: _page(rows[50:], total=51, revision=11)}, cursor


# -- panel widget ------------------------------------------------------


def test_panel_pager_follows_the_cursor_it_was_given(panel):
    widget, calls, _root = panel
    widget.set_rows([_row()])
    widget.set_page_state(state="ready", has_previous=False, has_next=True)
    assert "disabled" in widget._previous.state()
    assert "disabled" not in widget._next.state()
    widget.set_page_state(state="ready", has_previous=True, has_next=True)
    assert "disabled" not in widget._previous.state()
    widget._next.invoke()
    widget._previous.invoke()
    assert calls == [("verification_page_next", {}),
                     ("verification_page_previous", {})]
    widget.set_page_state(state="ready", has_previous=True, has_next=False)
    assert "disabled" in widget._next.state()


def test_panel_pager_is_disabled_while_a_pass_runs(panel):
    widget, _calls, _root = panel
    widget.set_rows([_row()])
    widget.set_page_state(state="ready", has_previous=True, has_next=True)
    widget.set_busy(True, text="已完成 0/1")
    assert "disabled" in widget._previous.state()
    assert "disabled" in widget._next.state()
    widget.set_busy(False, text="")
    assert "disabled" not in widget._next.state()


def test_panel_error_state_removes_the_rows_and_every_action(panel):
    widget, calls, _root = panel
    widget.set_rows([_row()])
    widget._tree.selection_set(_key())
    widget.set_page_state(state="error", text="读取待核验页面没有完成。")
    assert widget.rows() == () and widget._tree.get_children() == ()
    assert widget.selected_key() is None
    assert widget._hint.cget("text") == "读取待核验页面没有完成。"
    assert widget._hint.cget("text") != EMPTY_TEXT
    assert ERROR_TEXT and ERROR_TEXT != EMPTY_TEXT
    assert "disabled" in widget._recover.state()
    assert "disabled" in widget._batch.state()
    assert "disabled" in widget._previous.state()
    assert "disabled" in widget._next.state()
    assert "disabled" not in widget._refresh.state()
    assert calls == []


def test_panel_loading_state_says_it_is_reading_and_shows_no_rows(panel):
    widget, _calls, _root = panel
    widget.set_rows([_row()])
    widget.set_page_state(state="loading")
    assert widget.rows() == ()
    assert widget._hint.cget("text") == READY_TEXT
    assert "disabled" in widget._recover.state()


def test_panel_refuses_an_unknown_load_state(panel):
    widget, _calls, _root = panel
    with pytest.raises(ValueError):
        widget.set_page_state(state="half-ready")


# -- controller reads --------------------------------------------------


def test_controller_reports_the_page_and_the_scope_total():
    pages, _cursor = _fifty_one_pages()
    controller, view, runtime = _paged_controller(pages, _summary(
        {"needs_verification": 51}))
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    text = view.named("set_verification_rows")[-1][2]["page_text"]
    assert "列出 50 行" in text
    assert "本页 50 / 范围内 51" in text
    assert view.panel.last["state"] == "ready"
    assert view.panel.last["has_next"] is True
    assert view.panel.last["has_previous"] is False
    assert runtime.cursors == [None]


def test_controller_reaches_the_fifty_first_row_and_acts_on_it():
    pages, cursor = _fifty_one_pages()
    controller, view, runtime = _paged_controller(pages, _summary(
        {"needs_verification": 51}))
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_page_next", {})
    for _ in range(3):
        controller._poll()
    assert runtime.cursors == [None, cursor]
    display = view.named("set_verification_rows")[-1][1][0]
    assert [row["illust_id"] for row in display] == ["1050"]
    assert view.panel.last["has_previous"] is True
    assert view.panel.last["has_next"] is False
    key = _key(illust="1050", delivery="d-50")
    assert key in controller._verify_rows
    controller.handle("verification_recover", {"key": key})
    for _ in range(3):
        controller._poll()
    assert len(controller.session.commands) == 1
    assert controller.session.commands[0]["expected_delivery_id"] == "d-50"
    assert controller.session.commands[0]["illust_id"] == "1050"


def test_controller_previous_page_returns_to_the_stored_cursor():
    pages, cursor = _fifty_one_pages()
    controller, view, runtime = _paged_controller(pages, _summary({}))
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_page_next", {})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_page_previous", {})
    for _ in range(3):
        controller._poll()
    assert runtime.cursors == [None, cursor, None]
    display = view.named("set_verification_rows")[-1][1][0]
    assert len(display) == 50
    assert view.panel.last["has_previous"] is False


def test_controller_never_turns_a_page_without_a_cursor():
    pages, _cursor = _fifty_one_pages()
    controller, _view, runtime = _paged_controller(pages, _summary({}))
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_page_previous", {})
    assert runtime.cursors == [None]
    # The first page is the last page: no continuation was returned for it.
    controller.handle("verification_page_next", {})
    for _ in range(3):
        controller._poll()
    assert runtime.cursors == [None, _cursor_from(pages)]


def _cursor_from(pages):
    return pages[None]["next_cursor"]


def test_controller_refuses_a_malformed_cursor_instead_of_reading_it():
    pages = {None: {"rows": [_row()], "next_cursor": ("100", "d-1"),
                    "truncated": True, "stats": {"total": 1}}}
    controller, view, runtime = _paged_controller(pages, _summary({}))
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    assert controller._verify_next is None
    assert view.panel.last["has_next"] is False
    # The truncation is still reported: the list really is not the whole scope.
    assert "还有更多未列出" in view.named("set_verification_rows")[-1][2]["page_text"]
    controller.handle("verification_page_next", {})
    for _ in range(2):
        controller._poll()
    assert runtime.cursors == [None]


def test_controller_turns_a_failed_page_read_into_an_error_state():
    pages = {None: RuntimeError("ledger gone")}
    controller, view, _runtime = _paged_controller(pages, _summary({}))
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    assert controller._verify_state == "error"
    assert controller._verify_display == [] and controller._verify_rows == {}
    assert view.panel.last["state"] == "error"
    assert view.panel.last["has_next"] is False
    assert view.panel.last["text"]
    texts = [call[1][0] for call in view.named("set_verification_result")]
    assert texts and all("没有完成" in text or "读取" in text for text in texts), texts
    # A read failure is a panel state, not a modal dialog.
    assert view.named("set_verification_result")[-1][1][1] == "danger"


def test_controller_reports_a_failed_summary_apart_from_the_rows():
    pages = {None: _page([_row()], total=1)}
    controller, view, _runtime = _paged_controller(pages, _summary({}))
    controller.runtime.verification_summary = lambda **kwargs: _failed(
        RuntimeError("counts gone"))
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    assert len(controller._verify_display) == 1
    text = view.named("set_verification_rows")[-1][2]["page_text"]
    assert "本批统计没有读出来" in text
    assert controller._verify_summary_error


def _failed(error):
    future = Future()
    future.set_exception(error)
    return future


def test_controller_carries_the_read_revision_and_never_mixes_batches():
    pages, cursor = _fifty_one_pages()
    controller, _view, _runtime = _paged_controller(pages, _summary({}))
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    assert controller._verify_read_revision == 7
    controller.handle("verification_page_next", {})
    for _ in range(3):
        controller._poll()
    assert controller._verify_read_revision == 11
    # Another task is another read: the revision and the cursor belong to the
    # selection, so they are dropped with it.
    _pin_task_rows(controller, {"job-a": {"batch_id": "batch-a"},
                                "job-b": {"batch_id": "batch-b"}})
    controller.handle("select_task", {"id": "job-b"})
    assert controller._verify_read_revision is None
    assert controller._verify_cursors == [None] and controller._verify_next is None
    assert controller._verify_state == "idle"


def test_controller_invalidates_the_visible_page_when_the_selection_changes():
    pages, cursor = _fifty_one_pages()
    controller, view, runtime = _paged_controller(pages, _summary({}))
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    controller.handle("verification_page_next", {})
    for _ in range(3):
        controller._poll()
    _pin_task_rows(controller, {"job-a": {"batch_id": "batch-a"},
                                "job-b": {"batch_id": "batch-b"}})
    controller.handle("select_task", {"id": "job-b"})
    assert controller._verify_display == [] and controller._verify_rows == {}
    # The next read starts at the head of the new batch, never at the cursor a
    # previous batch left behind.
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    assert runtime.cursors[-1] is None
    assert cursor not in runtime.cursors[3:]


def test_controller_invalidates_the_page_when_the_user_leaves_it():
    pages, _cursor = _fifty_one_pages()
    controller, _view, _runtime = _paged_controller(pages, _summary({}))
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    assert controller._verify_display
    key = _key(illust="1000", delivery="d-0")
    assert key in controller._verify_rows
    controller.handle("navigate", {"page": "follow"})
    assert controller._verify_display == [] and controller._verify_rows == {}
    assert controller._verify_state == "idle"
    controller._verify_rows[key] = {
        "batch_id": _BATCH, "illust_id": "1000", "page_index": 0,
        "delivery_id": "d-0", "_batch": _BATCH,
        "_epoch": controller._verification_epoch - 1}
    controller.handle("verification_recover", {"key": key})
    for _ in range(2):
        controller._poll()
    assert controller.session.commands == []


def test_the_real_view_exposes_the_panel_state_entry_point(mounted):
    """The controller reaches the mounted panel; the real view must expose it."""
    view, _calls, _root = mounted
    panel = view.verification_slot
    assert panel is not None and hasattr(panel, "set_page_state")
    panel.set_page_state(state="loading")
    assert panel._hint.cget("text") == READY_TEXT
    panel.set_rows([_row()])
    panel.set_page_state(state="ready", has_previous=True, has_next=True)
    assert panel.rows() and "disabled" not in panel._next.state()
    panel.set_page_state(state="error", text="读取待核验页面没有完成。")
    assert panel._hint.cget("text") == "读取待核验页面没有完成。"
    assert panel.rows() == ()
    assert "disabled" in panel._next.state() and "disabled" in panel._previous.state()


def test_controller_labels_a_header_read_from_another_transaction():
    pages = {None: _page([_row()], total=1, revision=7)}
    summary = dict(_summary({"needs_verification": 1}), read_revision=3)
    controller, view, _runtime = _paged_controller(pages, summary)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    assert "统计与列表不是同一次读取" in view.named(
        "set_verification_rows")[-1][2]["page_text"]
    # Same revision: the two reads agree, so nothing is claimed either way.
    pages = {None: _page([_row()], total=1, revision=7)}
    summary = dict(_summary({"needs_verification": 1}), read_revision=7)
    controller, view, _runtime = _paged_controller(pages, summary)
    controller.handle("navigate", {"page": "verify"})
    for _ in range(3):
        controller._poll()
    assert "统计与列表不是同一次读取" not in view.named(
        "set_verification_rows")[-1][2]["page_text"]

