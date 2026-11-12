#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P6 原生收藏界面的真实 Tk 行为测试。

根窗口统一 withdraw，不抢用户当前窗口；不联网、不读认证、不写业务数据。
测试通过真实控件动作（按钮 invoke、选择虚拟事件、Configure 事件）验证行为，
而不是重复实现等价的 getter。
"""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import tkinter as tk
from tkinter import ttk

from core.log_view import ViewDelta
from gui.collection_theme import PALETTE, scale_px
from gui.collection_view import CollectionView, PAGE_LABELS


_GC_CHILD_SCRIPT = r'''
import gc
import sys
import threading

import tkinter as tk

from core.log_view import ViewDelta
from gui.collection_view import CollectionView

mode = sys.argv[1]
unraisable = []
sys.unraisablehook = lambda args: unraisable.append(repr(args.exc_value))
gc.disable()
root = tk.Tk()
root.withdraw()
view = CollectionView(root, lambda action, payload: None)
root.update_idletasks()
view.set_follow_form({
    "mode": "new",
    "min_bookmarks_enabled": True,
    "min_bookmarks": "88",
    "output_relative": "out",
})
view.set_artists([
    {"id": "a1", "name": "画师", "state": "完成", "counts": "1"},
])
view.set_tasks([
    {"id": "t1", "title": "作品", "source": "关注", "state": "运行", "counts": "1", "reason": ""},
])
view.set_task_info("第 1 / 2 页")
view.set_records([
    {"id": "r1", "title": "记录", "artist": "画师", "pages": "1", "result": "完成", "time": "12:00"},
], page_text="第 1 / 1 页")
view.apply_log_delta(
    ViewDelta(append_lines=("one", "two"), remove_lines=0, follow=True,
              skipped_events=0, unseen_lines=0)
)
view.set_log_open(True)
view.set_settings_form({"workers": "3"})
view.show_page("records")

if mode == "dispose_destroy":
    view.dispose()
    view.destroy()
    root.destroy()
elif mode == "dispose_root":
    view.dispose()
    root.destroy()
elif mode == "destroy_only":
    view.destroy()
    root.destroy()
elif mode == "root_only":
    root.destroy()
else:
    raise SystemExit(20)

del view, root
remaining_variables = [
    type(item).__name__ for item in gc.get_objects() if isinstance(item, tk.Variable)
]
errors = []


def worker():
    try:
        for _ in range(5):
            gc.collect()
    except BaseException as exc:
        errors.append(repr(exc))


thread = threading.Thread(target=worker, name="p6-gc-worker")
thread.start()
thread.join(30)
leftovers = [item.name for item in threading.enumerate() if item is not threading.main_thread()]
print("variables", remaining_variables)
print("threads", leftovers)
print("errors", errors)
print("unraisable", unraisable)
if thread.is_alive() or errors or unraisable or leftovers or remaining_variables:
    print("P6_GC_BAD")
    raise SystemExit(10)
print("P6_GC_OK")
raise SystemExit(0)
'''


@pytest.fixture(scope="module")
def tk_root():
    try:
        root = tk.Tk()
    except tk.TclError:  # pragma: no cover - headless Tk
        pytest.skip("Tk is not available")
    root.withdraw()
    try:
        yield root
    finally:
        try:
            root.destroy()
        except tk.TclError:  # pragma: no cover - already destroyed
            pass


@pytest.fixture
def app(tk_root):
    root = tk_root
    calls: list[tuple[str, dict]] = []

    def on_action(action, payload):
        calls.append((action, dict(payload)))

    view = CollectionView(root, on_action)
    view.pack(fill="both", expand=True)
    root.update_idletasks()
    try:
        yield root, view, calls
    finally:
        try:
            view.dispose()
        except Exception:  # pragma: no cover - defensive cleanup
            pass
        try:
            if view.winfo_exists():
                view.destroy()
            root.update_idletasks()
        except tk.TclError:  # pragma: no cover - already destroyed
            pass


def _configure(view, root, width, height=720):
    view.event_generate("<Configure>", width=width, height=height)
    root.update()


def _label_texts(widget):
    found = []
    try:
        children = widget.winfo_children()
    except tk.TclError:  # pragma: no cover - destroyed
        return found
    for child in children:
        try:
            if isinstance(child, ttk.Label):
                found.append(str(child.cget("text")))
        except tk.TclError:  # pragma: no cover - destroyed
            continue
        found.extend(_label_texts(child))
    return found


def _entry_values(widget):
    found = []
    try:
        children = widget.winfo_children()
    except tk.TclError:  # pragma: no cover - destroyed
        return found
    for child in children:
        try:
            if isinstance(child, ttk.Entry):
                found.append(str(child.get()))
        except tk.TclError:  # pragma: no cover - destroyed
            continue
        found.extend(_entry_values(child))
    return found


def test_default_page_navigation_marker_and_navigate_action(app):
    root, view, calls = app
    assert view._active_page == "follow"
    assert view._nav_lines["follow"].cget("background") == PALETTE["accent"]
    assert view._nav_lines["tasks"].cget("background") == PALETTE["page"]
    for button in view._nav_buttons.values():
        assert button.cget("takefocus") in (1, "1", True)

    # Programmatic page switching never reports a user action.
    for key in ("follow", "tasks", "sources", "records", "settings"):
        view.show_page(key)
        root.update_idletasks()
        assert view._active_page == key
        assert view._nav_lines[key].cget("background") == PALETTE["accent"]
        for other, button in view._nav_buttons.items():
            if other != key:
                assert view._nav_lines[other].cget("background") == PALETTE["page"]
    assert calls == []

    view._nav_buttons["records"].invoke()
    root.update()
    assert view._active_page == "records"
    assert calls[-1] == ("navigate", {"page": "records"})

    with pytest.raises(ValueError):
        view.show_page("unknown")


def test_follow_defaults_mode_switch_and_independent_filters(app):
    root, view, calls = app
    form = view.get_follow_form()
    assert form["mode"] == "history"
    assert form["min_bookmarks_enabled"] is False
    assert form["min_bookmarks"] == ""
    assert form["exclude_ai"] is False
    assert form["max_artists"] == "30"
    assert form["max_requests"] == "80"
    assert form["max_works"] == "100"
    assert form["output_relative"] == "downl_Pixiv"
    assert form["folder_by_artist"] is True
    assert set(form) == {
        "mode",
        "min_bookmarks_enabled",
        "min_bookmarks",
        "exclude_ai",
        "max_artists",
        "max_requests",
        "max_works",
        "artist_ids",
        "start_artist_id",
        "start_ordinal",
        "output_relative",
        "output_kind",
        "output_root_id",
        "folder_by_artist",
    }

    view.set_follow_form(
        {
            "min_bookmarks_enabled": True,
            "min_bookmarks": "120",
            "exclude_ai": True,
            "artist_ids": "11, 22",
            "start_artist_id": "11",
            "start_ordinal": "4",
            "max_artists": "9",
        }
    )
    assert "disabled" not in view._min_entry.state()
    view._follow_mode_new.invoke()
    root.update()
    assert view.get_follow_form()["mode"] == "new"
    assert view.get_follow_form()["min_bookmarks_enabled"] is True
    assert view.get_follow_form()["min_bookmarks"] == "120"
    assert view.get_follow_form()["exclude_ai"] is True
    assert view.get_follow_form()["artist_ids"] == "11, 22"
    assert view.get_follow_form()["start_artist_id"] == "11"
    assert view.get_follow_form()["start_ordinal"] == "4"
    assert view.get_follow_form()["max_artists"] == "9"

    # Switching mode back must not clear the independent condition either.
    view._follow_mode_history.invoke()
    root.update()
    assert view.get_follow_form()["mode"] == "history"
    assert view.get_follow_form()["min_bookmarks"] == "120"
    assert view.get_follow_form()["exclude_ai"] is True
    assert calls == []

    # Turning the bookmark filter off preserves the typed value, disables the
    # entry, and only changes provided keys.
    view.set_follow_form({"min_bookmarks_enabled": False})
    assert view.get_follow_form()["min_bookmarks_enabled"] is False
    assert view.get_follow_form()["min_bookmarks"] == "120"
    assert "disabled" in view._min_entry.state()
    view.set_follow_form({"max_works": "77"})
    assert view.get_follow_form()["max_works"] == "77"
    assert view.get_follow_form()["max_artists"] == "9"
    assert calls == []


def test_advanced_follow_settings_collapse_and_keep_values(app):
    root, view, calls = app
    assert view._follow_advanced.winfo_manager() == ""
    view._more_checkbox.invoke()
    root.update()
    assert view._follow_advanced.winfo_manager() == "grid"
    view.set_follow_form(
        {"output_relative": "out/收藏", "folder_by_artist": False, "start_ordinal": "8"}
    )
    view._more_checkbox.invoke()
    root.update()
    assert view._follow_advanced.winfo_manager() == ""
    view._more_checkbox.invoke()
    root.update()
    assert view.get_follow_form()["output_relative"] == "out/收藏"
    assert view.get_follow_form()["folder_by_artist"] is False
    assert view.get_follow_form()["start_ordinal"] == "8"
    assert calls == []


def test_action_buttons_disable_and_report_only_real_user_actions(app):
    root, view, calls = app
    view.set_tasks([{"id":"active","title":"正在下载","state":"收集中"}])
    view.select_task("active")
    pause = view._action_buttons["pause"][0]
    view.configure_action("pause", enabled=False)
    assert "disabled" in pause.state()
    pause.invoke()
    root.update()
    assert calls == []

    view.configure_action("pause", enabled=True, label="暂停中")
    assert pause.cget("text") == "暂停中"
    pause.invoke()
    root.update()
    assert calls[-1] == ("pause", {})

    view.set_summary({"button_label": "继续收集"})
    start = view._action_buttons["start"][0]
    assert start.cget("text") == "继续收集"
    start.invoke()
    root.update()
    assert calls[-1] == ("start", {})
    view.configure_action("start", enabled=False)
    before = len(calls)
    start.invoke()
    root.update()
    assert len(calls) == before

    toggle = view._action_buttons["log_toggle"][0]
    toggle.invoke()
    root.update()
    assert calls[-1] == ("log_toggle", {"open": True})
    view.set_log_open(True)
    assert "收起日志" in toggle.cget("text")
    toggle.invoke()
    root.update()
    assert calls[-1] == ("log_toggle", {"open": False})


def test_task_table_caps_rows_preserves_selection_and_actions(app):
    root, view, calls = app
    rows = [
        {
            "id": f"id{index}",
            "title": f"作品 {index}",
            "source": "关注",
            "state": "排队",
            "counts": f"{index}/10",
            "reason": "",
        }
        for index in range(250)
    ]
    view.set_tasks(rows)
    tree = view._trees["tasks"]
    assert len(tree.get_children()) == 200
    assert view.selected_id("tasks") is None
    assert calls == []

    tree.selection_set("id150")
    tree.event_generate("<<TreeviewSelect>>")
    root.update()
    assert calls[-1] == ("select_task", {"id": "id150"})
    assert view.selected_id("tasks") == "id150"

    before = len(calls)
    view.set_tasks(list(reversed(rows)))
    assert len(tree.get_children()) == 200
    assert view.selected_id("tasks") == "id150"
    assert len(calls) == before  # programmatic refresh does not fake a user select

    view.set_tasks([])
    assert tree.get_children() == ()
    assert view.selected_id("tasks") is None
    assert view._tree_hints["tasks"].winfo_manager() == "grid"
    assert len(calls) == before

    for button in view._task_controls:
        assert "disabled" in button.state()
    view.set_tasks(rows[:1])
    view.select_task("id0")
    for action in ("pause", "resume", "cancel", "retry_failed", "results"):
        view._action_buttons[action][0].invoke()
    root.update()
    assert [item[0] for item in calls[-5:]] == [
        "pause",
        "resume",
        "cancel",
        "retry_failed",
        "results",
    ]


def test_task_pagination_info_and_actions(app):
    root, view, calls = app
    view.show_page("tasks")
    view.set_task_info("第 2 / 5 页 · 共 900 条")
    assert view._task_page_label.cget("text") == "第 2 / 5 页 · 共 900 条"
    view._action_buttons["task_next"][0].invoke()
    view._action_buttons["task_previous"][0].invoke()
    root.update()
    assert [item[0] for item in calls[-2:]] == ["task_next", "task_previous"]
    before = len(calls)
    view.configure_action("task_next", enabled=False)
    view._action_buttons["task_next"][0].invoke()
    root.update()
    assert len(calls) == before
    view.configure_action("task_next", enabled=True)
    view._action_buttons["task_next"][0].invoke()
    root.update()
    assert calls[-1] == ("task_next", {})


def test_task_rows_consume_at_most_201_from_iterables(app):
    root, view, calls = app
    consumed = []

    def rows():
        for index in range(5000):
            consumed.append(index)
            yield {
                "id": f"id{index}",
                "title": f"作品 {index}",
                "source": "关注",
                "state": "排队",
                "counts": "0/1",
                "reason": "",
            }

    view.set_tasks(rows())
    assert len(consumed) == 201
    assert len(view._trees["tasks"].get_children()) == 200


def test_identical_task_refresh_skips_rebuild_and_keeps_selection(app, monkeypatch):
    root, view, calls = app
    rows = [
        {
            "id": f"id{index}",
            "title": f"作品 {index}",
            "source": "关注",
            "state": "排队",
            "counts": "0/1",
            "reason": "",
        }
        for index in range(40)
    ]
    view.set_tasks(rows)
    tree = view._trees["tasks"]
    tree.selection_set("id5")
    tree.event_generate("<<TreeviewSelect>>")
    root.update()
    assert calls[-1] == ("select_task", {"id": "id5"})
    before_calls = len(calls)

    delete_calls = []
    original_delete = tree.delete

    def counting_delete(*args):
        delete_calls.append(args)
        return original_delete(*args)

    monkeypatch.setattr(tree, "delete", counting_delete)
    view.set_tasks([dict(row) for row in rows])
    assert delete_calls == []
    assert view.selected_id("tasks") == "id5"
    assert len(calls) == before_calls

    changed = [dict(row) for row in rows]
    changed[0]["state"] = "完成"
    view.set_tasks(changed)
    assert delete_calls
    assert view.selected_id("tasks") == "id5"
    assert len(calls) == before_calls


def test_three_tables_have_horizontal_scrollbars(app):
    root, view, calls = app
    for kind in ("artists", "tasks", "records"):
        tree = view._trees[kind]
        assert str(tree.cget("xscrollcommand"))
        bars = [
            child
            for child in tree.master.winfo_children()
            if isinstance(child, ttk.Scrollbar)
        ]
        orientations = [str(bar.cget("orient")) for bar in bars]
        assert "horizontal" in orientations
        assert "vertical" in orientations


def test_artist_and_record_tables_cap_rows_and_selected_id(app):
    root, view, calls = app
    artists = [
        {"id": str(index), "name": f"画师 {index}", "state": "完成", "counts": {"作品": 1}}
        for index in range(240)
    ]
    view.set_artists(artists)
    assert len(view._trees["artists"].get_children()) == 200
    view.set_artists([])
    assert view._trees["artists"].get_children() == ()
    assert view._tree_hints["artists"].winfo_manager() == "grid"

    records = [
        {
            "id": f"r{index}",
            "title": f"记录 {index}",
            "artist": "画师",
            "pages": "3",
            "result": "完成",
            "time": "12:00",
        }
        for index in range(3)
    ]
    view.set_records(records, page_text="第 1 / 1 页")
    record_tree = view._trees["records"]
    assert len(record_tree.get_children()) == 3
    record_tree.selection_set("r1")
    record_tree.event_generate("<<TreeviewSelect>>")
    root.update()
    assert calls[-1] == ("select_record", {"id": "r1"})
    assert view.selected_id("records") == "r1"
    with pytest.raises(ValueError):
        view.selected_id("unknown")


def test_records_form_query_actions_detail_and_pagination(app):
    root, view, calls = app
    assert view.get_record_form() == {"scope": "batch", "search": "", "result": "all"}
    view._record_search_entry.delete(0, "end")
    view._record_search_entry.insert(0, "海")
    view._record_result_combo.set("失败")
    root.update()
    assert calls == []  # editing filters never submits a query by itself
    assert view.get_record_form() == {"scope": "batch", "search": "海", "result": "failed"}

    view._record_query_button.invoke()
    root.update()
    assert calls[-1] == ("record_query", {})
    # Enter is wired to the same user submission path; key delivery itself is
    # not simulated on a withdrawn, unmapped entry.
    assert view._record_search_entry.bind("<Return>")

    records = [
        {"id": "r1", "title": "作品一", "artist": "画师甲", "pages": "3", "result": "完成", "time": "12:00"},
        {"id": "r2", "title": "作品二", "artist": "画师乙", "pages": "1", "result": "失败", "time": "12:05"},
    ]
    view.set_records(records, page_text="第 1 / 2 页")
    assert view._records_page_label.cget("text") == "第 1 / 2 页"
    view.set_records_info("第 2 / 2 页")
    assert view._records_page_label.cget("text") == "第 2 / 2 页"
    assert view._detail_text.get("1.0", "end-1c") == "选择一条记录查看详情。"

    view._trees["records"].selection_set("r1")
    view._trees["records"].event_generate("<<TreeviewSelect>>")
    root.update()
    assert calls[-1] == ("select_record", {"id": "r1"})
    view.set_detail("作品一\n原图 3 页")
    assert "作品一" in view._detail_text.get("1.0", "end-1c")

    for action in ("record_next", "record_previous", "export_records"):
        view._action_buttons[action][0].invoke()
    root.update()
    assert [item[0] for item in calls[-3:]] == [
        "record_next",
        "record_previous",
        "export_records",
    ]
    view._action_buttons["open_artwork"][0].invoke()
    root.update()
    assert calls[-1] == ("open_artwork", {"id": "r1"})

    view.set_records([{"id": "r2", "title": "作品二", "artist": "画师乙", "pages": "1"}])
    assert view.selected_id("records") is None
    assert view._detail_text.get("1.0", "end-1c") == "选择一条记录查看详情。"
    view._action_buttons["open_artwork"][0].invoke()
    root.update()
    assert calls[-1] == ("open_artwork", {})


def test_log_default_collapsed_follow_false_keeps_reading_position(app):
    root, view, calls = app
    assert view._log_open is False
    assert view._log_body.winfo_manager() == ""
    assert "展开日志" in view._action_buttons["log_toggle"][0].cget("text")

    view.set_log_open(True)
    root.update_idletasks()
    assert view._log_body.winfo_manager() == "grid"
    text = view._log_text
    initial = ViewDelta(
        append_lines=tuple(f"第 {index} 行" for index in range(80)),
        remove_lines=0,
        follow=True,
        skipped_events=0,
        unseen_lines=0,
    )
    view.apply_log_delta(initial)
    root.update_idletasks()
    assert view._log_line_count == 80
    assert view.log_at_bottom() is True

    text.yview_moveto(0.5)
    root.update_idletasks()
    top_line = int(text.index("@0,0").split(".")[0])
    top_text = text.get(f"{top_line}.0", f"{top_line}.end")
    before_calls = len(calls)
    view.apply_log_delta(
        ViewDelta(
            append_lines=("新增甲", "新增乙"),
            remove_lines=5,
            follow=False,
            skipped_events=3,
            unseen_lines=2,
        )
    )
    root.update_idletasks()
    assert view._log_line_count == 77
    assert view.log_at_bottom() is False
    new_top = int(text.index("@0,0").split(".")[0])
    assert text.get(f"{new_top}.0", f"{new_top}.end") == top_text
    assert len(calls) == before_calls  # applying deltas is not a user action

    view.apply_log_delta(
        ViewDelta(
            append_lines=("最新",),
            remove_lines=0,
            follow=True,
            skipped_events=0,
            unseen_lines=0,
        )
    )
    root.update_idletasks()
    assert view.log_at_bottom() is True

    view.set_log_notice("日志受限：只保留最近 3000 行")
    assert "日志受限" in view._log_notice.cget("text")
    for action in ("log_clear", "log_pause", "log_follow", "export_log"):
        view._action_buttons[action][0].invoke()
    root.update()
    assert [item[0] for item in calls[-4:]] == [
        "log_clear",
        "log_pause",
        "log_follow",
        "export_log",
    ]

    view.clear_log_text()
    assert view._log_line_count == 0
    assert text.get("1.0", "end-1c") == ""
    assert view.log_at_bottom() is True
    view.set_log_open(False)
    assert view._log_body.winfo_manager() == ""
    with pytest.raises(ValueError):
        view.set_log_open(1)
    with pytest.raises(ValueError):
        view.apply_log_delta(object())


def test_settings_form_roundtrip_and_actions(app):
    root, view, calls = app
    form = view.get_settings_form()
    assert form == {
        "output_relative": "downl_Pixiv",
        "output_kind": "project",
        "output_root_id": "project",
        "folder_by_artist": True,
        "workers": "3",
        "name_template": "{illust_id}_p{page_index}",
        "api_interval": "0.5",
        "image_interval": "0.5",
        "user_agent": "",
    }
    view.set_settings_form(
        {
            "output_relative": "out/收藏",
            "folder_by_artist": False,
            "workers": "5",
            "name_template": "{id}_{title}_p{page}",
            "api_interval": "1.25",
            "image_interval": "0.75",
            "user_agent": "ExampleAgent/1.0",
        }
    )
    assert view.get_settings_form() == {
        "output_relative": "out/收藏",
        "output_kind": "project",
        "output_root_id": "project",
        "folder_by_artist": False,
        "workers": "5",
        "name_template": "{id}_{title}_p{page}",
        "api_interval": "1.25",
        "image_interval": "0.75",
        "user_agent": "ExampleAgent/1.0",
    }
    view.set_settings_form({"workers": "2"})
    assert view.get_settings_form()["output_relative"] == "out/收藏"
    assert view.get_settings_form()["workers"] == "2"
    assert calls == []

    view.show_page("settings")
    for action in ("save_settings", "verify_account", "open_output"):
        view._action_buttons[action][0].invoke()
    root.update()
    assert [item[0] for item in calls[-3:]] == [
        "save_settings",
        "verify_account",
        "open_output",
    ]
    texts = "\n".join(_label_texts(view._pages["settings"]))
    assert "已有授权" in texts and "检查账号状态" in texts
    assert set(view.get_settings_form()) == {
        "output_relative",
        "output_kind",
        "output_root_id",
        "folder_by_artist",
        "workers",
        "name_template",
        "api_interval",
        "image_interval",
        "user_agent",
    }
    assert all(
        "token" not in value.lower() and "bearer" not in value.lower()
        for value in _entry_values(view._pages["settings"])
    )


def test_header_summary_account_and_status_tones(app):
    root, view, calls = app
    view.set_account("示例账号")
    assert "示例账号" in view._account_label.cget("text")
    view.set_status("正在收集", "success")
    assert view._status_label.cget("text") == "正在收集"
    assert "Success" in view._status_label.cget("style")
    view.set_status("需要处理", "danger")
    assert "Danger" in view._status_label.cget("style")
    view.set_summary(
        {
            "title": "继续收集关注画师",
            "description": "历史补全说明",
            "counts": "30 位画师 · 100 个作品",
            "button_label": "继续收集",
        }
    )
    assert view._summary_title.cget("text") == "继续收集关注画师"
    assert "30 位" in view._summary_counts.cget("text")
    assert view._action_buttons["start"][0].cget("text") == "继续收集"
    view.set_summary({"title": "只更新标题"})
    assert view._summary_counts.cget("text") == "30 位画师 · 100 个作品"
    assert calls == []


def test_sources_page_keeps_five_disabled_entries_with_explicit_notice(app):
    root, view, calls = app
    view.show_page("sources")
    root.update_idletasks()
    assert len(view._source_buttons) == 5
    for button in view._source_buttons:
        assert "disabled" not in button.state()
        button.invoke()
    root.update()
    assert calls == []
    texts = "\n".join(_label_texts(view._pages["sources"]))
    assert "同时出现在以下来源中" in texts
    assert view.get_source_form()['source']=='intersection'
    view._action_buttons['start_source'][0].invoke()
    assert calls==[('start_source',{})]


def test_narrow_width_moves_filters_below_and_keeps_controls(app):
    root, view, calls = app
    view.show_page("follow")
    wide_width = view._breakpoint + scale_px(root, 80)
    narrow_width = max(scale_px(root, 620), view._breakpoint - scale_px(root, 80))
    _configure(view, root, wide_width)
    assert view._follow_wide is True
    assert int(view._follow_filters.grid_info()["row"]) == 0
    assert int(view._follow_filters.grid_info()["column"]) == 1

    _configure(view, root, narrow_width)
    assert view._follow_wide is False
    info = view._follow_filters.grid_info()
    assert int(info["row"]) == 1
    assert int(info["column"]) == 0
    assert int(view._follow_left.grid_info()["row"]) == 0
    for widget in (
        view._start_button,
        view._min_entry,
        view._more_checkbox,
        view._nav_buttons["follow"],
        view._action_buttons["sync_following"][0],
    ):
        assert widget.winfo_exists()
        assert widget.winfo_manager() != ""
    assert view._follow_filters.winfo_manager() == "grid"

    _configure(view, root, wide_width)
    assert view._follow_wide is True
    assert int(view._follow_filters.grid_info()["row"]) == 0
    assert int(view._follow_filters.grid_info()["column"]) == 1
    # one root-view Configure bind only; scroll viewports also bind <Configure>
    # but they are separate widgets and must not multiply the page re-layout.
    configure_binds = [
        item
        for item in view._bindings
        if item[1] == "<Configure>" and item[0] is view
    ]
    assert len(configure_binds) == 1


def test_white_main_surface_compact_header_and_toolbar_heights(app):
    root, view, calls = app
    assert PALETTE["page"] == "#FFFFFF"
    assert PALETTE["paper"] == "#FFFFFF"
    assert PALETTE["paper_soft"] == "#F4F7F9"
    assert view._title_label.cget("text") == "JpgPng"
    assert "收藏" not in view._title_label.cget("text")
    for key, label in PAGE_LABELS.items():
        assert label in view._nav_buttons[key].cget("text")

    wide_width = view._breakpoint + scale_px(root, 80)
    narrow_width = max(scale_px(root, 620), view._breakpoint - scale_px(root, 80))
    _configure(view, root, wide_width)
    assert view._header_wide is True
    assert int(view._nav_frame.grid_info()["row"]) == 0
    assert int(view._nav_frame.grid_info()["column"]) == 1
    assert int(view._title_label.grid_info()["row"]) == 0
    assert view._header.winfo_reqheight() < scale_px(root, 85)
    assert scale_px(root, 36) <= view._start_button.winfo_reqheight() <= scale_px(root, 46)
    assert (
        scale_px(root, 24)
        <= view._action_buttons["pause"][0].winfo_reqheight()
        <= scale_px(root, 46)
    )

    _configure(view, root, narrow_width)
    assert view._header_wide is False
    assert int(view._nav_frame.grid_info()["row"]) == 1
    assert view._header.winfo_reqheight() < scale_px(root, 140)
    for widget in (view._start_button, view._nav_buttons["follow"], view._min_entry):
        assert widget.winfo_exists()
        assert widget.winfo_manager() != ""
    assert calls == []


def test_collection_styles_are_prefixed_and_leave_old_styles_alone(app):
    root, view, calls = app
    style = ttk.Style(root)
    style.configure("Card.TLabel", foreground="#123456")
    extra = CollectionView(root, lambda _action, _payload: None)
    extra.pack(fill="both", expand=True)
    root.update_idletasks()
    try:
        assert style.lookup("Card.TLabel", "foreground") == "#123456"
        assert style.lookup("Collection.TLabel", "foreground") == PALETTE["ink"]
        assert style.lookup("CollectionPage.TFrame", "background") == PALETTE["page"]
    finally:
        extra.dispose()
        extra.destroy()


def test_no_background_threads_timers_images_or_second_root(app):
    root, view, calls = app
    images_before = tuple(root.tk.call("image", "names"))
    after_before = tuple(root.tk.call("after", "info"))
    threads_before = threading.active_count()

    view.set_artists([{"id": "1", "name": "画师", "state": "完成", "counts": "1"}])
    view.set_tasks([{"id": "t1", "title": "作品", "source": "关注", "state": "运行", "counts": "1", "reason": ""}])
    view.set_records([{"id": "r1", "title": "记录", "artist": "画师", "pages": "1", "result": "完成", "time": "12:00"}])
    view.apply_log_delta(
        ViewDelta(append_lines=("一行日志",), remove_lines=0, follow=True, skipped_events=0, unseen_lines=0)
    )
    for page in ("tasks", "sources", "records", "settings", "follow"):
        view.show_page(page)

    assert tuple(root.tk.call("image", "names")) == images_before
    assert tuple(root.tk.call("after", "info")) == after_before
    assert threading.active_count() == threads_before
    assert not any(isinstance(child, tk.Toplevel) for child in root.winfo_children())
    assert calls == []


def test_dispose_and_destroy_never_trigger_user_callbacks(app):
    root, view, calls = app
    view.show_page("tasks")
    view.set_tasks(
        [{"id": "t1", "title": "作品", "source": "关注", "state": "运行", "counts": "1", "reason": ""}]
    )
    tree = view._trees["tasks"]
    tree.selection_set("t1")
    tree.event_generate("<<TreeviewSelect>>")
    root.update()
    assert calls[-1] == ("select_task", {"id": "t1"})

    view.dispose()
    before = len(calls)
    view._action_buttons["pause"][0].invoke()
    for item in tree.selection():
        tree.selection_remove(item)
    tree.event_generate("<<TreeviewSelect>>")
    root.update()
    assert len(calls) == before

    view.destroy()
    root.update_idletasks()
    assert len(calls) == before
    assert root.winfo_exists()


def test_destroy_is_idempotent_and_releases_owned_variables_and_widgets(app):
    root, view, calls = app
    view.set_follow_form({"min_bookmarks_enabled": True, "min_bookmarks": "5"})
    variables = list(view._variables)
    assert variables
    for variable in variables:
        assert variable._root is root
        assert variable._tk is not None
    assert view._owned_widgets

    view.dispose()
    assert view._variables == []
    for variable in variables:
        assert variable._tk is None
        assert variable._root is None
    view.dispose()  # idempotent
    view.destroy()
    view.destroy()  # idempotent, never touches Tcl twice
    assert view._destroyed is True
    assert view._disposed is True
    assert view._bindings == []
    assert view._owned_widgets == []
    assert view._action_buttons == {}
    assert view._trees == {}
    assert root.winfo_exists()
    gc.collect()  # owner-thread collection after teardown is safe


@pytest.mark.parametrize(
    "mode", ["dispose_destroy", "dispose_root", "destroy_only", "root_only"]
)
def test_background_gc_after_teardown_is_safe_in_subprocess(mode):
    repo_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONNOUSERSITE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-s",
            "-B",
            "-X",
            "utf8",
            "-c",
            _GC_CHILD_SCRIPT,
            mode,
        ],
        cwd=str(repo_root),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    assert completed.returncode == 0, output[-4000:]
    assert "P6_GC_OK" in output, output[-4000:]
    assert "variables []" in output, output[-4000:]
    assert "threads []" in output, output[-4000:]
    lowered = output.lower()
    for marker in (
        "tcl_asyncdelete",
        "fatal python error",
        "main thread is not in main loop",
        "p6_gc_bad",
        "0x80000003",
    ):
        assert marker not in lowered, output[-4000:]
