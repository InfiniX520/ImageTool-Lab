#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""U1-A/W5 desktop panel: the pending-verification list and its two actions.

The panel is presentation only.  It never opens SQLite, never touches the file
system, never hashes bytes, never starts a thread and never talks to the
network.  Every user action leaves through one callback, on_action(name,
payload), so the controller stays the only owner of I/O and of the command id
that makes one recovery exactly-once.

Row identity is frozen before the action is emitted: batch id, illust id, page
index and delivery id.  The panel never sends a tree row number, so a refresh
that moves or drops a row can never redirect an action to another page (see
docs/optimization/2026-11-13/U1A_W5_RECOVERY_ENTRY.md).

Every method must be called on the Tk owner thread.  The controller refreshes
rows only from its root.after poll, so no query or writer thread ever touches a
widget.
"""

from __future__ import annotations

import gc
import tkinter as tk
from tkinter import ttk

from gui.collection_theme import scale_px

# One list request can never render more than this many rows; the controller
# pages the rest.  A redraw is bounded work, not a library scan.
ROW_LIMIT = 200

# A batch recovery is a chain of single-page commands.  Twenty keeps one pass
# short enough to stay responsive and small enough to read in the result line.
BATCH_LIMIT = 20

EMPTY_TEXT = "当前批次没有待核验页面；停住的页会在这里出现。"
READY_TEXT = "正在读取待核验页面…"
# A failed read is its own state: the old rows are gone and nothing on screen
# may be clicked as if it were a live page.
ERROR_TEXT = "读取待核验页面没有完成；请检查后点“刷新”重试。"
LOAD_STATES = ("idle", "loading", "ready", "error")

_STATE_TEXT = {
    "needs_verification": "待核验",
    "recovery_needed": "需要恢复",
    "pending": "待下载",
    "suspended": "已停住",
    "retry_wait": "等待重试",
    "transferring": "传输中",
    "validating": "校验中",
    "prepared": "已就绪",
    "completed": "已完成",
}

# W5 recovery reason codes (docs/optimization/2026-11-13/U1A_W5_RECOVERY_ENTRY.md
# section 2.1) plus the internal codes the repository adds around them.  The
# wording never claims a loss: changed and unreadable keep the file.
_REASON_TEXT = {
    "hash_match": "字节一致，已复用",
    "artifact_missing": "文件不在原位，已排入补下载",
    "repair_already_pending": "已经在补下载队列里",
    "artifact_changed": "文件内容已变，保留待你决定",
    "artifact_unreadable": "文件读不出来，保留待你决定",
    "artifact_root_unavailable": "所在盘或目录不可用，未做任何改动",
    "artifact_changed_during_check": "核验过程中文件被改动，保留待你决定",
    "observation_unknown": "无法完成字节核验，保留待你决定",
    "observation_unavailable": "暂时没有可用的核验位置",
    "observation_failed": "核验没有得出结果",
    "no_recorded_artifact": "没有记录过这一页的字节",
    "no_resolver": "保存位置暂时不可用",
    "cross_task_reuse_disabled": "本任务不允许复用其他任务的成果",
    "already_completed": "这一页已经完成",
    "batch_not_running": "批次已结束，不能再核验",
    "page_not_in_batch": "这一页不在本批次里",
    "page_owner_mismatch": "这一页属于别的任务",
    "work_budget_exhausted": "本批作品名额已用完",
    "output_blocked_by_other_task": "同一路径正被其他任务占用",
    "state_changed": "页面状态刚被改动，请刷新后重试",
    "stale_preflight": "核验证据已过期，请重试",
    "unsupported_previous_state": "当前状态不支持核验",
    "superseded_unresolved": "已被后续任务接替，尚未收尾",
    "supersede_chain": "已由后续任务接替",
}

# Why a visible held page cannot be handled right now.  The repository refuses
# a recovery on a batch that is not running, so the panel has to say that
# instead of offering a button that would do nothing.
_BLOCKED_TEXT = {
    "batch_not_running": "批次已暂停，恢复后才能核验",
    "page_owner_mismatch": "这一页属于别的任务",
    "output_blocked_by_other_task": "同一路径正被其他任务占用",
    "work_budget_exhausted": "本批作品名额已用完",
}

_OUTCOME_TEXT = {
    "reused": "已复用",
    "repair_queued": "已排入补下载",
    "held": "已保留待决定",
    "completed": "已完成",
    "refused": "未处理",
}

_RESULT_STYLES = {
    "muted": "CollectionStatusMuted.TLabel",
    "success": "CollectionStatusSuccess.TLabel",
    "warning": "CollectionStatusWarning.TLabel",
    "danger": "CollectionStatusDanger.TLabel",
    "error": "CollectionStatusDanger.TLabel",
    "accent": "CollectionStatusAccent.TLabel",
}

_COLUMNS = (
    ("illust", "作品", 190, True),
    ("page", "页", 76, False),
    ("state", "状态", 100, False),
    ("reason", "当前情况", 280, True),
    ("output", "路径摘要", 150, False),
    ("time", "更新时间", 132, False),
)

# Outcome -> (closing sentence, result tone).  The sentence states exactly what
# was and was not done; the file is never described as lost or deleted.
_OUTCOME_CLOSING = {
    "reused": ("，已直接复用记录，不需要重新下载。", "success"),
    "completed": ("，这一页早前已经完成，本次没有改动。", "success"),
    "repair_queued": ("，已排入补下载；继续本批时只补这一页。", "accent"),
    "held": ("，文件保持原样：没有删除、没有覆盖。", "warning"),
    "refused": ("，本次没有改动任何记录。", "warning"),
}

# Never render a path.  Only the recorded output summary may appear, and a
# value that looks like a path is replaced by a dash instead of being shown.
_KEY_LIMIT = 12
_PATH_MARKERS = ("\\", "/", ":", "~")


def _text(value: object) -> str:
    if value is None:
        return "—"
    text = str(value)
    return text if text else "—"


def output_summary(value: object) -> str:
    """First characters of the recorded output key, or a dash.

    The key is a path digest, never a path.  Anything that looks like a real
    location is refused so a future caller cannot leak one through this cell.
    """
    if not isinstance(value, str) or not value:
        return "—"
    if any(marker in value for marker in _PATH_MARKERS):
        return "—"
    return value[:_KEY_LIMIT] + "…" if len(value) > _KEY_LIMIT else value


def state_text(code: object) -> str:
    """Localise a delivery state; an unknown state keeps its raw code."""
    if code is None:
        return "—"
    key = str(code)
    return _STATE_TEXT.get(key, "未知状态（%s）" % key[:32])


def reason_text(code: object) -> str:
    """Localise a recovery reason; an unknown code is shown, never swallowed."""
    if code is None or code == "":
        return "—"
    key = str(code)
    return _REASON_TEXT.get(key, "未知原因（%s）" % key[:48])


def blocked_text(code: object) -> str:
    """Localise why a held page cannot be recovered now; unknown codes stay."""
    if code is None or code == "":
        return ""
    key = str(code)
    return _BLOCKED_TEXT.get(key, "暂时不能核验（%s）" % key[:32])


def outcome_text(code: object) -> str:
    if code is None:
        return "未知结果"
    key = str(code)
    return _OUTCOME_TEXT.get(key, "未知结果（%s）" % key[:32])


def row_key(batch_id: object, illust_id: object, page_index: object,
            delivery_id: object) -> str | None:
    """The frozen identity of one page row, or None when it is incomplete."""
    if not all(isinstance(value, str) and value
               for value in (batch_id, illust_id, delivery_id)):
        return None
    if type(page_index) is not int:
        return None
    return "%s/%s/p%d/%s" % (batch_id, illust_id, page_index, delivery_id)


def outcome_pair(code: object) -> tuple[str, str]:
    """Return the closing sentence and the tone for one outcome code."""
    key = str(code) if code is not None else ""
    return _OUTCOME_CLOSING.get(key, ("，结果未知，请刷新后重试。", "warning"))


def _reason_cell(item: dict) -> str:
    """The observed reason, plus why it cannot be handled when it cannot."""
    base = reason_text(item.get("reason"))
    blocked = item.get("blocked") or ""
    if blocked and item.get("recoverable") is not True:
        return "%s（%s）" % (base, blocked)
    return base


def describe_result(result: object) -> tuple[str, str]:
    """One readable line for a recovery result dict.

    Unknown outcomes and unknown reason codes stay visible with their raw code
    so a new repository verdict cannot silently look like a familiar one.
    """
    if not isinstance(result, dict):
        return ("核验结果无法读取（返回格式不正确）。", "danger")
    outcome = result.get("outcome")
    where = "作品 %s" % _text(result.get("illust_id"))
    page = result.get("page_index")
    if type(page) is int and 0 <= page <= 9999:
        where += " 第 %d 页" % (page + 1)
    closing, tone = outcome_pair(outcome)
    line = "%s：%s · %s%s" % (where, outcome_text(outcome),
                              reason_text(result.get("reason")), closing)
    if outcome not in _OUTCOME_TEXT:
        tone = "warning"
    return (line, tone)


def summary_text(summary: object) -> str:
    """Render the repository summary dict as one bounded line.

    A suspended page is a paused batch, not a lost file: it is worded apart
    from a verification hold so the two can never be read as one problem.
    """
    if not isinstance(summary, dict):
        return ""
    counts = summary.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    parts: list[str] = []
    total = summary.get("total_pages")
    if type(total) is int:
        parts.append("本批 %d 页" % total)
    existing = summary.get("existing_pages")
    existing = existing if type(existing) is int else 0
    if existing:
        # A page can carry a recorded artifact without a delivery of its own
        # (a strict reuse from another batch).  Saying nothing here used to
        # read as "0 pages registered", which is the opposite of the truth.
        parts.append("已有 %d 页" % existing)
    unverified = counts.get("needs_verification")
    recovery = counts.get("recovery_needed")
    held = (unverified if type(unverified) is int else 0) + (
        recovery if type(recovery) is int else 0)
    if held:
        parts.append("待核验 %d 页" % held)
    suspended = counts.get("suspended")
    if type(suspended) is int and suspended:
        parts.append("停住 %d 页（批次暂停中，恢复批次后自动继续）" % suspended)
    pending = counts.get("pending")
    if type(pending) is int and pending:
        parts.append("待下载 %d 页" % pending)
    completed = counts.get("completed")
    if type(completed) is int and completed:
        parts.append("已完成 %d 页" % completed)
    # "unstarted" is reported, never inferred from total - linked: the
    # difference belongs to the existing_pages bucket first.
    unstarted = summary.get("unstarted_pages")
    if type(unstarted) is not int:
        linked = summary.get("linked_pages")
        unstarted = (
            max(0, total - linked - existing)
            if type(linked) is int and type(total) is int
            else 0
        )
    if unstarted:
        parts.append("尚未开始 %d 页" % unstarted)
    phase = summary.get("phase")
    if isinstance(phase, str) and phase and phase != "running":
        parts.append("批次已暂停，恢复后才能核验或补下载")
    return " · ".join(parts)


class VerificationPanel(ttk.Frame):
    """A self-contained list of held pages with a bounded batch action."""

    ROW_LIMIT = ROW_LIMIT
    BATCH_LIMIT = BATCH_LIMIT

    def __init__(self, master: tk.Misc, on_action) -> None:
        if not callable(on_action):
            raise ValueError("on_action must be callable")
        super().__init__(master, style="CollectionPage.TFrame")
        self._on_action = on_action
        self._rows: tuple[dict, ...] = ()
        self._by_key: dict[str, dict] = {}
        self._selected: str | None = None
        self._page_text = ""
        self._busy = False
        self._disposed = False
        self._suspend = False
        # One read is one page: the pager flags come from the controller's
        # cursor, never from the row count, so "next" can only exist when the
        # repository really returned a continuation.
        self._load_state = "idle"
        self._state_text = ""
        self._has_previous = False
        self._has_next = False
        self._build()

    # -- construction ----------------------------------------------------

    def _build(self) -> None:
        s = lambda value: scale_px(self, value)  # noqa: E731
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        bar = ttk.Frame(self, style="CollectionPage.TFrame")
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        self._summary = ttk.Label(bar, text=READY_TEXT, style="CollectionMuted.TLabel")
        self._summary.grid(row=0, column=0, sticky="w")
        self._previous = ttk.Button(bar, text="上一页", style="CollectionQuiet.TButton",
            name="verification_previous_button", takefocus=True,
            command=lambda: self._emit("verification_page_previous", {}))
        self._previous.grid(row=0, column=1, sticky="e", padx=(s(20), 0))
        self._next = ttk.Button(bar, text="下一页", style="CollectionQuiet.TButton",
            name="verification_next_button", takefocus=True,
            command=lambda: self._emit("verification_page_next", {}))
        self._next.grid(row=0, column=2, sticky="e", padx=(s(8), 0))
        self._refresh = ttk.Button(bar, text="刷新", style="CollectionQuiet.TButton",
            name="verification_refresh_button", takefocus=True,
            command=lambda: self._emit("verification_refresh", {}))
        self._refresh.grid(row=0, column=3, sticky="e", padx=(s(8), 0))
        self._hint = ttk.Label(self, text="", style="CollectionMuted.TLabel",
            justify="left", wraplength=s(640))
        self._hint.grid(row=1, column=0, sticky="w", pady=(s(10), s(6)))
        wrap = ttk.Frame(self, style="CollectionPage.TFrame")
        wrap.grid(row=2, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self._wrap = wrap
        tree = ttk.Treeview(wrap, columns=tuple(item[0] for item in _COLUMNS),
            show="headings", height=8, selectmode="browse",
            style="Collection.Treeview", name="verification_tree", takefocus=True)
        for column, heading, width, stretch in _COLUMNS:
            tree.heading(column, text=heading, anchor="w")
            tree.column(column, width=scale_px(self, width),
                minwidth=scale_px(self, 48), stretch=stretch, anchor="w")
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview,
            style="Collection.Vertical.TScrollbar")
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=lambda first, last: self._scrollbar_set(
            scrollbar, first, last))
        tree.bind("<<TreeviewSelect>>", self._on_select, add="+")
        tree.tag_configure("alternate", background="#F9FBFC")
        self._tree = tree
        actions = ttk.Frame(self, style="CollectionPage.TFrame")
        actions.grid(row=3, column=0, sticky="ew", pady=(s(14), 0))
        actions.columnconfigure(0, weight=1)
        self._progress = ttk.Label(actions, text="", style="CollectionMuted.TLabel")
        self._progress.grid(row=0, column=0, sticky="w")
        self._recover = ttk.Button(actions, text="核验并处理选中页",
            style="CollectionSecondary.TButton",
            name="verification_recover_button", takefocus=True,
            command=self._emit_recover)
        self._recover.grid(row=0, column=1, padx=(s(16), 0))
        self._batch = ttk.Button(actions,
            text="批量核验（最多 %d 页）" % BATCH_LIMIT,
            style="CollectionQuiet.TButton",
            name="verification_batch_button", takefocus=True,
            command=self._emit_batch)
        self._batch.grid(row=0, column=2, padx=(s(12), 0))
        self._result = ttk.Label(self, text="", style="CollectionStatusMuted.TLabel",
            justify="left", wraplength=s(720))
        self._result.grid(row=4, column=0, sticky="w", pady=(s(10), 0))
        self._wrap.grid_remove()
        self._sync_actions()

    # -- public API ------------------------------------------------------

    def set_rows(self, rows: object, *, page_text: str = "") -> None:
        """Replace the visible rows; the controller owns their origin."""
        if self._disposed:
            return
        prepared: list[dict] = []
        seen: set[str] = set()
        for row in rows if isinstance(rows, (list, tuple)) else ():
            if len(prepared) >= ROW_LIMIT:
                break
            item = self._normalize(row)
            if item is None or item["key"] in seen:
                continue
            seen.add(item["key"])
            prepared.append(item)
        self._rows = tuple(prepared)
        self._by_key = {item["key"]: item for item in prepared}
        self._page_text = str(page_text or "")
        self._render()

    def set_page_state(self, *, state: str = "ready",
                       has_previous: bool = False, has_next: bool = False,
                       text: object = None) -> None:
        """Say which read version the visible rows came from.

        The rows themselves still arrive through set_rows; this call only
        describes the read that produced them.  'loading' and 'error' clear the
        list on purpose: an old row must never stay clickable while the current
        read has no answer, and a failed read is not an empty library.  An
        unknown state is a programming error, not something to guess at.
        """
        if self._disposed:
            return
        key = str(state)
        if key not in LOAD_STATES:
            raise ValueError("unknown verification load state: %r" % (state,))
        self._load_state = key
        self._has_previous = bool(has_previous)
        self._has_next = bool(has_next) and key == "ready"
        if key in {"loading", "error"}:
            self._rows = ()
            self._by_key = {}
            self._selected = None
        if text is not None:
            self._state_text = str(text)
        elif key != "error":
            self._state_text = ""
        self._render()

    def set_result(self, text: object, tone: str = "muted") -> None:
        if self._disposed:
            return
        style = _RESULT_STYLES.get(str(tone), _RESULT_STYLES["muted"])
        try:
            self._result.configure(text="" if text is None else _text(text), style=style)
        except tk.TclError:  # pragma: no cover - widget being destroyed
            return

    def set_progress(self, text: object) -> None:
        if self._disposed:
            return
        try:
            self._progress.configure(text="" if text is None else str(text))
        except tk.TclError:  # pragma: no cover - widget being destroyed
            return

    def set_busy(self, busy: bool, *, text: object = None) -> None:
        """Disable both actions while the controller runs them."""
        if self._disposed:
            return
        self._busy = bool(busy)
        if text is not None:
            self.set_progress(text)
        self._sync_actions()

    def selected_key(self) -> str | None:
        return None if self._disposed else self._selected

    def rows(self) -> tuple[dict, ...]:
        return self._rows

    def dispose(self) -> None:
        """Detach the callback; a later refresh or click must do nothing."""
        self._disposed = True
        self._on_action = None
        self._rows = ()
        self._by_key = {}
        self._selected = None
        self._load_state = "idle"
        self._state_text = ""
        self._has_previous = False
        self._has_next = False
        # Tk objects must be finalized while this panel's interpreter is still
        # alive: one left over and collected later, on a worker thread that
        # happens to start a GC cycle, calls into a destroyed Tcl interpreter
        # (seen here as a native breakpoint).  Collecting at dispose is cheap
        # and keeps teardown local to the thread that owns the widgets.
        gc.collect()

    # -- internals -------------------------------------------------------

    @staticmethod
    def _normalize(row: object) -> dict | None:
        if not isinstance(row, dict):
            return None
        key = row_key(row.get("batch_id"), row.get("illust_id"),
                      row.get("page_index"), row.get("delivery_id"))
        if key is None:
            return None
        return {
            "key": key,
            "batch_id": row.get("batch_id"),
            "illust_id": row.get("illust_id"),
            "page_index": row.get("page_index"),
            "delivery_id": row.get("delivery_id"),
            "state": row.get("state"),
            "reason": row.get("error_code"),
            "blocked": blocked_text(row.get("blocked_reason")),
            "attempts": row.get("attempts"),
            "output": output_summary(row.get("output_key")),
            "time": _text(row.get("time") or row.get("updated_at")
                          or row.get("retry_at")),
            "recoverable": row.get("recoverable") is True,
        }

    def _render(self) -> None:
        if self._disposed:
            return
        tree = self._tree
        self._suspend = True
        try:
            children = tree.get_children()
            if children:
                tree.delete(*children)
            for index, item in enumerate(self._rows):
                tree.insert("", "end", iid=item["key"], values=(
                    item["illust_id"],
                    "第 %d 页" % (item["page_index"] + 1),
                    state_text(item["state"]),
                    _reason_cell(item),
                    item["output"],
                    item["time"],
                ), tags=("alternate",) if index % 2 else ())
            if self._selected and tree.exists(self._selected):
                tree.selection_set(self._selected)
            else:
                self._selected = None
        except tk.TclError:  # pragma: no cover - widget being destroyed
            return
        finally:
            self._suspend = False
        try:
            self._summary.configure(
                text=self._page_text or "共 %d 页待核验" % len(self._rows))
            self._hint.configure(text=self._hint_text())
        except tk.TclError:  # pragma: no cover - widget being destroyed
            return
        if self._rows:
            self._wrap.grid()
        else:
            self._wrap.grid_remove()
        self._sync_actions()

    def _hint_text(self) -> str:
        """What the empty area means: loaded and empty, reading, or failed."""
        if self._rows:
            return ""
        if self._load_state == "error":
            return self._state_text or ERROR_TEXT
        if self._load_state == "loading":
            return READY_TEXT
        return EMPTY_TEXT

    def _on_select(self, _event: object = None) -> None:
        if self._disposed or self._suspend:
            return
        try:
            selection = self._tree.selection()
        except tk.TclError:  # pragma: no cover - widget being destroyed
            return
        self._selected = str(selection[0]) if selection else None
        self._sync_actions()

    def _sync_actions(self) -> None:
        if self._disposed:
            return
        ready = self._load_state != "error" and self._load_state != "loading"
        selected = self._by_key.get(self._selected) if self._selected else None
        can_recover = (not self._busy and ready and selected is not None
                       and selected.get("recoverable") is True)
        batchable = (not self._busy and ready
                     and any(item.get("recoverable") is True for item in self._rows))
        # A failed read leaves the refresh button as the only way out, and the
        # cursor is unknown then, so no page can be offered in either direction.
        previous = not self._busy and self._has_previous and ready
        following = not self._busy and self._has_next and ready
        try:
            self._recover.state(["!disabled"] if can_recover else ["disabled"])
            self._batch.state(["!disabled"] if batchable else ["disabled"])
            self._previous.state(["!disabled"] if previous else ["disabled"])
            self._next.state(["!disabled"] if following else ["disabled"])
            self._refresh.state(["disabled"] if self._busy else ["!disabled"])
        except tk.TclError:  # pragma: no cover - widget being destroyed
            return

    def _emit_recover(self) -> None:
        if self._disposed or self._busy:
            return
        item = self._by_key.get(self._selected) if self._selected else None
        if item is None:
            self.set_result("选中的页已不在列表里；请刷新后再看。", "warning")
            return
        if item.get("recoverable") is not True:
            blocked = item.get("blocked")
            self.set_result(
                ("这一页现在不能核验：%s。" % blocked) if blocked
                else "选中的页当前不可核验；请刷新后再看。", "warning")
            return
        self._emit("verification_recover", {"key": item["key"]})

    def _emit_batch(self) -> None:
        if self._disposed or self._busy:
            return
        keys = [item["key"] for item in self._rows
                if item.get("recoverable") is True][:BATCH_LIMIT]
        if not keys:
            blocked = next((item["blocked"] for item in self._rows
                            if item.get("blocked")), "")
            self.set_result(
                ("现在没有可以核验的页面：%s。" % blocked) if blocked
                else "现在没有可以核验的页面。", "warning")
            return
        self._emit("verification_batch", {"keys": keys})

    def _emit(self, action: str, payload: dict) -> None:
        if self._disposed:
            return
        callback = self._on_action
        if callback is None:
            return
        callback(action, dict(payload))

    def _scrollbar_set(self, bar, first, last) -> None:
        bar.set(first, last)
        visible = float(first) > 0.0001 or float(last) < 0.9999
        if visible and not bar.winfo_manager():
            bar.grid()
        elif not visible and bar.winfo_manager():
            bar.grid_remove()


__all__ = [
    "BATCH_LIMIT",
    "EMPTY_TEXT",
    "ERROR_TEXT",
    "LOAD_STATES",
    "READY_TEXT",
    "ROW_LIMIT",
    "VerificationPanel",
    "blocked_text",
    "describe_result",
    "outcome_text",
    "output_summary",
    "reason_text",
    "row_key",
    "state_text",
    "summary_text",
]
