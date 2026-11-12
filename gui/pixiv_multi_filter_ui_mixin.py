#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多数据源交集下载界面的布局与交互逻辑。"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core.download_service import log_message
from gui import theme
from gui.ui_fx import (
    DualScrollText,
    append_log_lines,
    bind_hover_highlight,
    poll_delay_for,
    style_text_scrollbar,
)


class MultiFilterUIMixin:
    """多来源交集筛选窗口的 UI 构建与主题应用。

    创建来源勾选区（关注/排行榜/关键词/用户 ID）、筛选条件区（收藏阈值/
    跳过 AI）、设置区（数量/目录）与按钮区。本 mixin 创建的控件成为
    ``MultiFilterRuntimeMixin`` 的宿主属性。

    Host contract（由 ``MultiFilterGUI`` 宿主提供）:
        - ``self.root`` — Tk 根窗口
        - ``self._colors`` — 主题色字典（由 ``_configure_filter_theme`` 构建）
        - ``self.config`` — dict，读取 ``threads``/``delay``/``original``/``multi_page``
          用于设置区只读展示
        - 方法由 RuntimeMixin 提供：``browse_dir()``/``start()``/``stop()``/
          ``finish()``/``_on_close()``/``process_log_queue()``/``run()``（绑定到按钮命令）
    """

    MAX_UI_LOG_LINES = 512
    LOG_DRAIN_BATCH = 400
    LOG_POLL_BUSY_MS = 60
    LOG_POLL_ACTIVE_MS = 120
    LOG_POLL_IDLE_MS = 240
    LOG_TRIM_MARGIN = 32

    def _build_ui(self):
        """构建与主工作台一致的编辑式交集筛选界面。"""
        self._configure_filter_theme()
        self.root.configure(background=self._colors["page"])
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(3, weight=1)

        colors = self._colors
        header = tk.Frame(self.root, background=colors["page"], height=86)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(0, weight=1)
        title = tk.Frame(header, background=colors["page"])
        title.grid(row=0, column=0, sticky="w", padx=20, pady=(14, 9))
        tk.Label(
            title,
            text="INTERSECTION / FILTER",
            background=colors["page"],
            foreground=colors["ink"],
            font=(theme.FONT_SERIF, 22),
        ).pack(anchor="w")
        tk.Label(
            title,
            text="多数据源交集  ·  仅下载同时出现在全部已选来源中的作品",
            background=colors["page"],
            foreground=colors["muted"],
            font=(theme.FONT_UI, 8),
        ).pack(anchor="w", pady=(2, 0))
        tk.Label(
            header,
            text="SET THEORY  /  PIXIV ARCHIVE",
            background=colors["page"],
            foreground=colors["accent"],
            font=(theme.FONT_MONO, 8, "bold"),
        ).grid(row=0, column=1, sticky="e", padx=20)
        tk.Frame(header, height=1, background=colors["rule"]).place(
            x=20, rely=1, relwidth=1, width=-40, y=-1
        )

        workspace = ttk.Frame(self.root, style="FilterPage.TFrame", padding=(20, 10, 20, 4))
        workspace.grid(row=1, column=0, sticky="nsew")
        workspace.grid_columnconfigure(0, weight=1, uniform="filter")
        workspace.grid_columnconfigure(1, weight=1, uniform="filter")
        self._source_host = self._create_filter_section(
            workspace, index="01", kicker="SOURCES", title="选择交集来源"
        )
        self._source_host.master.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self._task_host = self._create_filter_section(
            workspace, index="02", kicker="CONDITIONS", title="筛选与保存"
        )
        self._task_host.master.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        self._build_source_ui()
        self._build_filter_ui()
        self._build_settings_ui()
        self._build_buttons_ui()

    def _configure_filter_theme(self):
        self._colors = theme.build_colors()
        theme.apply_filter_styles(self.root, self._colors)

    def _create_filter_section(self, parent, *, index, kicker, title):
        colors = self._colors
        shell = tk.Frame(
            parent,
            background=colors["paper"],
            highlightbackground=colors["border"],
            highlightthickness=1,
            bd=0,
        )
        shell.grid_columnconfigure(0, weight=1)
        bind_hover_highlight(shell, normal=colors["border"], hover=colors["rule"])
        heading = tk.Frame(shell, background=colors["paper"])
        heading.grid(row=0, column=0, sticky="ew", padx=13, pady=(10, 8))
        tk.Label(
            heading,
            text=index,
            background=colors["paper"],
            foreground=colors["accent"],
            font=(theme.FONT_SERIF, 19),
        ).pack(side="left", padx=(0, 10))
        labels = tk.Frame(heading, background=colors["paper"])
        labels.pack(side="left")
        tk.Label(
            labels,
            text=kicker,
            background=colors["paper"],
            foreground=colors["accent"],
            font=(theme.FONT_MONO, 8, "bold"),
        ).pack(anchor="w")
        tk.Label(
            labels,
            text=title,
            background=colors["paper"],
            foreground=colors["ink"],
            font=(theme.FONT_UI, 10, "bold"),
        ).pack(anchor="w")
        tk.Frame(shell, height=1, background=colors["rule"]).grid(row=1, column=0, sticky="ew")
        body = ttk.Frame(shell, style="FilterPaper.TFrame", padding=(13, 10, 13, 12))
        body.grid(row=2, column=0, sticky="nsew")
        return body

    def _build_source_ui(self):
        """构建数据来源选择区域。"""
        colors = self._colors
        source_frame = self._source_host
        source_frame.grid_columnconfigure(0, weight=1)
        ttk.Label(
            source_frame, text="至少勾选两个来源，结果取共同作品", style="FilterMuted.TLabel"
        ).grid(row=0, column=0, sticky="w", pady=(0, 5))
        self.follow_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            source_frame, text="关注用户新作", variable=self.follow_var, style="Filter.TCheckbutton"
        ).grid(row=1, column=0, sticky="w", pady=2)

        row_rank = ttk.Frame(source_frame, style="FilterPaper.TFrame")
        row_rank.grid(row=2, column=0, sticky="ew", pady=2)
        row_rank.grid_columnconfigure(1, weight=1)
        self.ranking_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row_rank, text="排行榜", variable=self.ranking_var, style="Filter.TCheckbutton"
        ).grid(row=0, column=0, sticky="w", padx=(0, 7))
        self.rank_combo = ttk.Combobox(
            row_rank,
            values=[
                "day",
                "week",
                "month",
                "day_male",
                "day_female",
                "week_original",
                "week_rookie",
                "day_r18",
                "day_male_r18",
                "day_female_r18",
                "week_r18",
                "day_manga",
                "week_manga",
                "month_manga",
            ],
            state="readonly",
            style="Filter.TCombobox",
        )
        self.rank_combo.set("day")
        self.rank_combo.grid(row=0, column=1, sticky="ew")

        row_search = ttk.Frame(source_frame, style="FilterPaper.TFrame")
        row_search.grid(row=3, column=0, sticky="ew", pady=2)
        row_search.grid_columnconfigure(1, weight=1)
        self.search_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row_search, text="关键词", variable=self.search_var, style="Filter.TCheckbutton"
        ).grid(row=0, column=0, sticky="w", padx=(0, 7))
        self.search_entry = ttk.Entry(row_search, style="Filter.TEntry")
        self.search_entry.grid(row=0, column=1, sticky="ew")

        row_user = ttk.Frame(source_frame, style="FilterPaper.TFrame")
        row_user.grid(row=4, column=0, sticky="ew", pady=2)
        row_user.grid_columnconfigure(1, weight=1)
        self.user_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row_user, text="用户 ID", variable=self.user_var, style="Filter.TCheckbutton"
        ).grid(row=0, column=0, sticky="w", padx=(0, 7))
        self.user_entry = ttk.Entry(row_user, style="Filter.TEntry")
        self.user_entry.grid(row=0, column=1, sticky="ew")
        ttk.Label(source_frame, text="多个用户 ID 使用逗号分隔", style="FilterMuted.TLabel").grid(
            row=5, column=0, sticky="e", pady=(2, 0)
        )
        source_frame.grid_rowconfigure(6, weight=1)
        tk.Label(
            source_frame,
            text="SOURCES  ∩  01",
            background=colors["paper"],
            foreground=colors["ghost"],
            font=(theme.FONT_SERIF, 17),
        ).grid(row=6, column=0, sticky="se", pady=(10, 0))

    def _build_filter_ui(self):
        """构建筛选条件区域。"""
        filter_frame = self._task_host
        filter_frame.grid_columnconfigure(0, weight=1)
        ttk.Label(filter_frame, text="FILTER / 附加筛选", style="FilterEyebrow.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        row_filter = ttk.Frame(filter_frame, style="FilterPaper.TFrame")
        row_filter.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        row_filter.grid_columnconfigure(0, weight=1)
        bookmark_box = ttk.Frame(row_filter, style="FilterPaper.TFrame")
        bookmark_box.grid(row=0, column=0, sticky="ew", padx=(0, 9))
        ttk.Label(bookmark_box, text="最少收藏", style="FilterMuted.TLabel").pack(anchor="w")
        self.bookmark_entry = ttk.Entry(bookmark_box, style="Filter.TEntry")
        self.bookmark_entry.pack(fill="x", pady=(3, 0))
        self.bookmark_entry.insert(0, "0")
        self.skip_ai_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row_filter, text="跳过 AI 作品", variable=self.skip_ai_var, style="Filter.TCheckbutton"
        ).grid(row=0, column=1, sticky="sw", pady=(0, 4))
        ttk.Label(
            filter_frame, text="已存在文件仍按文件名自动查重", style="FilterMuted.TLabel"
        ).grid(row=2, column=0, sticky="w", pady=(4, 0))

    def _build_settings_ui(self):
        """构建下载设置区域。"""
        parent = self._task_host
        ttk.Separator(parent).grid(row=3, column=0, sticky="ew", pady=8)
        ttk.Label(parent, text="OUTPUT / 下载设置", style="FilterEyebrow.TLabel").grid(
            row=4, column=0, sticky="w"
        )
        setting_frame = ttk.Frame(parent, style="FilterPaper.TFrame")
        setting_frame.grid(row=5, column=0, sticky="ew", pady=(4, 0))
        setting_frame.grid_columnconfigure(0, weight=1)
        ttk.Label(setting_frame, text="每个来源获取", style="FilterMuted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.count_entry = ttk.Entry(setting_frame, style="Filter.TEntry")
        self.count_entry.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self.count_entry.insert(0, "200")
        ttk.Label(
            setting_frame, text="建议 200 以上；求交集后数量会减少", style="FilterMuted.TLabel"
        ).grid(row=2, column=0, sticky="w", pady=(3, 0))
        inherited = (
            f"并发 {self.config.get('threads', '3')} · 间隔 {self.config.get('delay', '0.5')} 秒 · "
            f"{'原图' if self.config.get('original', True) else '预览图'} · "
            f"{'全部页' if self.config.get('multi_page', True) else '仅首图'}"
        )
        ttk.Label(parent, text=inherited, style="FilterMuted.TLabel").grid(
            row=6, column=0, sticky="w", pady=(5, 0)
        )

        dir_frame = ttk.Frame(parent, style="FilterPaper.TFrame")
        dir_frame.grid(row=7, column=0, sticky="ew", pady=(5, 0))
        dir_frame.grid_columnconfigure(0, weight=1)
        ttk.Label(dir_frame, text="保存目录", style="FilterMuted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.dir_entry = ttk.Entry(dir_frame, style="Filter.TEntry")
        self.dir_entry.grid(row=1, column=0, sticky="ew", pady=(3, 0), padx=(0, 5))
        if self.config.get("save_dir"):
            self.dir_entry.insert(0, self.config["save_dir"])
        ttk.Button(
            dir_frame, text="浏览", command=self.browse_dir, style="FilterSecondary.TButton"
        ).grid(row=1, column=1, pady=(3, 0))

    def _build_buttons_ui(self):
        """构建操作区与交集会话日志。"""
        colors = self._colors
        outer = ttk.Frame(self.root, style="FilterPage.TFrame", padding=(20, 7, 20, 5))
        outer.grid(row=2, column=0, sticky="ew")
        btn_frame = tk.Frame(
            outer,
            background=colors["paper"],
            highlightbackground=colors["border"],
            highlightthickness=1,
            bd=0,
        )
        btn_frame.pack(fill="x")
        btn_frame.grid_columnconfigure(4, weight=1)
        bind_hover_highlight(btn_frame, normal=colors["border"], hover=colors["rule"])
        tk.Label(
            btn_frame,
            text="03 / EXECUTE",
            background=colors["paper"],
            foreground=colors["accent"],
            font=(theme.FONT_MONO, 8, "bold"),
        ).grid(row=0, column=0, padx=(12, 15), pady=11)
        self.start_btn = ttk.Button(
            btn_frame, text="开始交集下载  →", command=self.start, style="FilterPrimary.TButton"
        )
        self.start_btn.grid(row=0, column=1, padx=(0, 6), pady=8)
        self.stop_btn = ttk.Button(
            btn_frame,
            text="停止",
            command=self.stop,
            state="disabled",
            style="FilterSecondary.TButton",
        )
        self.stop_btn.grid(row=0, column=2, padx=(0, 10), pady=8)

        import core.download_service as pixiv_utils

        self.debug_var = tk.BooleanVar(value=pixiv_utils.DEBUG_MODE)
        ttk.Checkbutton(
            btn_frame,
            text="调试日志",
            variable=self.debug_var,
            command=self._toggle_debug,
            style="Filter.TCheckbutton",
        ).grid(row=0, column=3, padx=(0, 10), pady=8)
        self.status_label = tk.Label(
            btn_frame,
            text="● 就绪",
            background=colors["paper"],
            foreground=colors["muted"],
            font=(theme.FONT_UI, 8, "bold"),
            padx=8,
            pady=2,
        )
        self.status_label.grid(row=0, column=4, sticky="e", padx=(0, 12), pady=8)

        log_outer = ttk.Frame(self.root, style="FilterPage.TFrame", padding=(20, 5, 20, 16))
        log_outer.grid(row=3, column=0, sticky="nsew")
        log_outer.grid_rowconfigure(0, weight=1)
        log_outer.grid_columnconfigure(0, weight=1)
        stage = tk.Frame(log_outer, background=colors["offset"])
        stage.grid(row=0, column=0, sticky="nsew")
        stage.grid_rowconfigure(0, weight=1)
        stage.grid_columnconfigure(0, weight=1)
        card = tk.Frame(
            stage,
            background=colors["paper"],
            highlightbackground=colors["rule"],
            highlightthickness=1,
            bd=0,
        )
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=(0, 5))
        card.grid_rowconfigure(1, weight=1)
        card.grid_columnconfigure(0, weight=1)
        bind_hover_highlight(card, normal=colors["rule"], hover=colors["accent"])
        tk.Label(
            card,
            text="04 / INTERSECTION LOG",
            background=colors["paper"],
            foreground=colors["accent"],
            font=(theme.FONT_MONO, 8, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=11, pady=(8, 5))
        # wrap="none"（DualScrollText 默认值），避免长日志换行重排卡顿。
        self.log_text = DualScrollText(
            card,
            height=8,
            state="disabled",
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=colors["border"],
            highlightcolor=colors["accent"],
            background=colors["log"],
            foreground=colors["log_ink"],
            insertbackground=colors["accent"],
            selectbackground=colors["accent_wash"],
            selectforeground=colors["ink"],
            font=(theme.FONT_MONO, 9),
            spacing3=1,
            padx=8,
            pady=7,
        )
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=11, pady=(0, 11))
        style_text_scrollbar(self.log_text, colors)
        self._display_line_count = 0

    def _toggle_debug(self):
        """切换调试模式"""
        import core.download_service as pixiv_utils

        pixiv_utils.set_debug_mode(self.debug_var.get())
        if self.debug_var.get():
            log_message("调试模式已开启，将显示详细错误信息", "info", self.log_queue)
        else:
            log_message("调试模式已关闭", "info", self.log_queue)

    def browse_dir(self):
        """浏览选择保存目录"""
        d = filedialog.askdirectory()
        if d:
            self.dir_entry.delete(0, tk.END)
            self.dir_entry.insert(0, d)

    def start(self):
        """开始下载"""
        if self.running:
            return
        if not self.api:
            messagebox.showerror("错误", "未登录，请先在主界面登录")
            return

        # 检查至少选了2个数据来源
        sources = []
        if self.follow_var.get():
            sources.append("follow")
        if self.ranking_var.get():
            sources.append("ranking")
        if self.search_var.get() and self.search_entry.get().strip():
            sources.append("search")
        if self.user_var.get() and self.user_entry.get().strip():
            sources.append("user")

        if len(sources) < 2:
            messagebox.showwarning(
                "提示", "请至少选择2个数据来源才能取交集\n如果只需要单个来源，请使用主界面"
            )
            return

        save_dir = self.dir_entry.get().strip()
        if not save_dir:
            messagebox.showerror("错误", "请选择保存目录")
            return

        try:
            max_count = int(self.count_entry.get())
            min_bookmarks = int(self.bookmark_entry.get() or 0)
        except ValueError:
            messagebox.showerror("参数错误", "每个来源获取数量和最低收藏数必须是整数")
            return
        if max_count <= 0 or min_bookmarks < 0:
            messagebox.showerror("参数错误", "获取数量必须大于 0，最低收藏数不能为负数")
            return

        if not os.path.exists(save_dir):
            os.makedirs(save_dir)

        self.running = True
        self.stop_event.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        self.log_text.config(state="normal")
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state="disabled")
        self._display_line_count = 0

        # Snapshot Tk state before handing work to the background thread.
        filter_snapshot = {
            "max_count": self.count_entry.get(),
            "min_bookmarks": self.bookmark_entry.get(),
            "skip_ai": bool(self.skip_ai_var.get()),
            "rank_mode": self.rank_combo.get(),
            "keyword": self.search_entry.get().strip(),
            "user_ids": self.user_entry.get(),
        }
        threading.Thread(
            target=self._run,
            daemon=True,
            args=(sources, save_dir, filter_snapshot),
        ).start()

    def stop(self):
        """停止下载（带确认弹窗）"""
        if messagebox.askyesno("确认", "确定要停止下载吗？"):
            self.stop_event.set()
            log_message("正在停止...", "warning", self.log_queue)
            self.stop_btn.config(state="disabled")

    def finish(self):
        """完成后的清理"""
        self.running = False
        if getattr(self, "_is_closing", False):
            return

        self._post_to_ui(lambda: self.start_btn.config(state="normal"))
        self._post_to_ui(lambda: self.stop_btn.config(state="disabled"))
        self._post_to_ui(lambda: self.status_label.config(text="就绪"))

    def _on_close(self):
        """关闭窗口并停止后台任务。"""
        if getattr(self, "_is_closing", False):
            return

        self._is_closing = True
        self.running = False
        self.stop_event.set()

        log_after_id = getattr(self, "_log_after_id", None)
        if log_after_id:
            try:
                self.root.after_cancel(log_after_id)
            except Exception:
                pass
            self._log_after_id = None

        throttler = getattr(self, "_ui_throttler", None)
        if throttler is not None:
            throttler.close()

        try:
            self.root.destroy()
        except Exception:
            pass
        close_log = getattr(self.log_queue, "close_log", None)
        if callable(close_log):
            close_log()

    def process_log_queue(self):
        """处理日志队列：自适应轮询 + 按块裁剪 + 贴底跟随。"""
        if getattr(self, "_is_closing", False):
            return

        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return

        lines = []
        drained = 0
        for _ in range(self.LOG_DRAIN_BATCH):
            try:
                message = str(self.log_queue.get_nowait())
            except queue.Empty:
                break
            drained += 1
            lines.extend(message.splitlines() or [""])

        if lines:
            self._display_line_count, ok = append_log_lines(
                self.log_text,
                lines,
                max_lines=self.MAX_UI_LOG_LINES,
                display_count=self._display_line_count,
                trim_margin=self.LOG_TRIM_MARGIN,
                autoscroll=True,
            )
            if not ok:
                return

        if getattr(self, "_is_closing", False):
            return

        delay = poll_delay_for(
            drained,
            batch=self.LOG_DRAIN_BATCH,
            busy_ms=self.LOG_POLL_BUSY_MS,
            active_ms=self.LOG_POLL_ACTIVE_MS,
            idle_ms=self.LOG_POLL_IDLE_MS,
        )
        try:
            self._log_after_id = self.root.after(delay, self.process_log_queue)
        except tk.TclError:
            self._log_after_id = None

    def run(self):
        """独立运行时的主循环"""
        self.root.mainloop()
