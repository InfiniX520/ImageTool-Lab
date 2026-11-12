#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UI 工具：状态徽章、悬停高亮、双滚动条、UI 线程合并器、日志追加。"""

from __future__ import annotations

import tkinter as tk

from gui.theme import tone_colors


def set_pill(label: tk.Label, text: str, tone: str, colors: dict) -> None:
    """更新状态徽章文本与语义色调（即时切换，无过渡动画）。必须在 UI 线程调用。"""
    foreground, background = tone_colors(colors, tone)
    try:
        label.config(text=text, foreground=foreground, background=background)
    except tk.TclError:
        pass


def bind_hover_highlight(widget: tk.Widget, *, normal: str, hover: str) -> None:
    """给卡片型 Frame 的边框添加悬停高亮。

    指针在卡片与其子控件之间移动时不会闪烁：离开时延迟复核，
    若指针仍在卡片内部（某个子控件上）则保持高亮。
    """
    state = {"leave_id": None}

    def _pointer_inside() -> bool:
        try:
            containing = widget.winfo_containing(*widget.winfo_pointerxy())
            while containing is not None:
                if containing == widget:
                    return True
                containing = containing.master
        except tk.TclError:
            pass
        return False

    def on_enter(_event) -> None:
        leave_id = state["leave_id"]
        if leave_id is not None:
            try:
                widget.after_cancel(leave_id)
            except Exception:
                pass
            state["leave_id"] = None
        try:
            widget.config(highlightbackground=hover)
        except tk.TclError:
            pass

    def on_leave(_event) -> None:
        if state["leave_id"] is not None:
            try:
                widget.after_cancel(state["leave_id"])
            except Exception:
                pass

        def _check() -> None:
            state["leave_id"] = None
            if not _pointer_inside():
                try:
                    widget.config(highlightbackground=normal)
                except tk.TclError:
                    pass

        try:
            state["leave_id"] = widget.after(50, _check)
        except tk.TclError:
            state["leave_id"] = None

    widget.bind("<Enter>", on_enter)
    widget.bind("<Leave>", on_leave)


class DualScrollText(tk.Frame):
    """Text widget with vertical + horizontal scrollbars.

    The public API mirrors ``scrolledtext.ScrolledText`` (unknown
    attributes delegate to the inner ``tk.Text``), but word wrap defaults
    to ``"none"``: with wrap disabled Tk does not recompute wrapped
    layouts on every insert, which keeps long session logs (URLs, file
    paths) responsive where ``wrap="word"`` degrades badly.
    """

    def __init__(self, master: tk.Misc, **text_options) -> None:
        super().__init__(master)
        text_options.setdefault("wrap", "none")
        self.text = tk.Text(self, **text_options)
        self.vbar = tk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.hbar = tk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=self.vbar.set, xscrollcommand=self.hbar.set)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.hbar.grid(row=1, column=0, sticky="ew")

    # ``__getattr__`` only fires for attributes NOT found on the class MRO.
    # ``config``/``configure``/``cget``/``yview``/``see`` are all defined on
    # ``tk.Misc`` (the Frame base), so without these explicit overrides they
    # would silently operate on the Frame instead of the inner Text — e.g.
    # ``config(state="normal")`` raises TclError on a Frame, which killed
    # log display AND crashed _start_download via _clear_log.
    def configure(self, *args, **kwargs):
        return self.text.configure(*args, **kwargs)

    config = configure

    def cget(self, *args):
        return self.text.cget(*args)

    def yview(self, *args):
        return self.text.yview(*args)

    def see(self, *args):
        return self.text.see(*args)

    def __getattr__(self, name: str):
        # Delegate the rest of the Text API (insert/delete/get/index/...).
        # ``__getattr__`` only fires for missing attributes; guarding the
        # ``text`` name keeps construction-order lookups non-recursive.
        if name == "text":
            raise AttributeError(name)
        return getattr(self.__dict__["text"], name)


def style_text_scrollbar(scrolled: tk.Widget, colors: dict) -> None:
    """美化滚动条（垂直 + 可选水平，逐选项容错）。"""
    options = {
        "width": 10,
        "bd": 0,
        "relief": "flat",
        "background": colors["border"],
        "troughcolor": colors["paper"],
        "activebackground": colors["rule"],
        "highlightthickness": 0,
        "elementborderwidth": 0,
    }
    for bar in (getattr(scrolled, "vbar", None), getattr(scrolled, "hbar", None)):
        if bar is None:
            continue
        for option, value in options.items():
            try:
                bar.config(**{option: value})
            except tk.TclError:
                pass


class UiThrottler:
    """按 key 合并高频 UI 更新：同一时间窗内只保留最新回调。"""

    def __init__(self, root: tk.Misc, interval_ms: int = 110) -> None:
        self._root = root
        self._interval = max(16, int(interval_ms))
        self._pending: dict[str, object] = {}
        self._timer = None
        self._closed = False

    def call(self, key: str, fn) -> None:
        """登记一次更新；窗口期结束时以最新回调统一执行。"""
        if self._closed:
            return
        self._pending[key] = fn
        if self._timer is None:
            try:
                self._timer = self._root.after(self._interval, self._flush)
            except (tk.TclError, RuntimeError):
                self._timer = None

    def _flush(self) -> None:
        self._timer = None
        pending, self._pending = self._pending, {}
        for fn in pending.values():
            try:
                fn()
            except tk.TclError:
                pass
            except Exception:
                pass

    def close(self) -> None:
        self._closed = True
        if self._timer is not None:
            try:
                self._root.after_cancel(self._timer)
            except Exception:
                pass
            self._timer = None
        self._pending.clear()


def append_log_lines(
    text_widget: tk.Text,
    lines: list[str],
    *,
    max_lines: int,
    display_count: int,
    trim_margin: int = 32,
    autoscroll: bool = True,
) -> tuple[int, bool]:
    """高效追加日志行：单次插入、按块裁剪、仅贴底时跟随滚动。

    返回 (新的显示行数, 是否成功)。裁剪时额外多裁 ``trim_margin`` 行，
    避免持续输出时每个刷新周期都触发一次首行删除。
    """
    if not lines:
        return display_count, True
    try:
        at_bottom = text_widget.yview()[1] >= 0.995
        text_widget.config(state="normal")
        text_widget.insert(tk.END, "\n".join(lines) + "\n")
        display_count += len(lines)
        if display_count > max_lines:
            remove = (display_count - max_lines) + max(0, trim_margin)
            text_widget.delete("1.0", f"{remove + 1}.0")
            display_count -= remove
        text_widget.config(state="disabled")
        if autoscroll and at_bottom:
            text_widget.see(tk.END)
    except tk.TclError:
        return display_count, False
    return display_count, True


def poll_delay_for(
    drained: int, *, batch: int, busy_ms: int = 60, active_ms: int = 120, idle_ms: int = 240
) -> int:
    """根据本轮 drained 的消息量计算下一次轮询间隔（自适应退避）。"""
    if drained >= batch:
        return busy_ms
    return active_ms if drained else idle_ms
