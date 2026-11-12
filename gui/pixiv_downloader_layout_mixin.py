#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pixiv 主界面布局、视觉系统与轻量交互逻辑。"""

from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from core.download_service import clear_download_records
from gui import theme
from gui.ui_fx import (
    DualScrollText,
    append_log_lines,
    bind_hover_highlight,
    poll_delay_for,
    style_text_scrollbar,
)

try:
    from gui.pixiv_multi_filter import open_multi_filter
except ImportError:
    open_multi_filter = None


class PixivDownloaderLayoutMixin:
    """Build the desktop presentation while preserving legacy widget names.

    负责主窗口全部控件创建与布局，以及日志轮询/渲染/清空、模式切换、
    目录浏览/导入、调试切换、记录备份等 UI 事件处理。本 mixin 创建的
    控件实例（``token_entry``/``dir_entry``/``status_label`` 等）成为其他
    GUI mixin 的宿主属性。

    Host contract（由 ``PixivDownloaderGUI`` 宿主提供）:
        - ``self.root`` — Tk 根窗口
        - ``self._colors`` — 主题色字典
        - ``self.log_queue`` — ``SessionLogQueue``，日志源
        - ``self._ui_throttler`` — ``UiThrottler``，高频 UI 更新节流
        - ``self._log_lines`` — ``deque``，日志行缓冲（``MAX_UI_LOG_LINES=512``）
        - 方法由 RuntimeMixin/ConfigMixin 提供：``_login``/``_start_download``/
          ``_stop_download``/``_save_config`` 等（绑定到按钮命令）
    """

    MODE_LABELS = {
        "search": "关键词搜索",
        "user": "用户作品",
        "follow": "关注用户新作",
        "follow_all": "关注用户全部作品",
        "ranking": "排行榜",
        "related": "作品相似推荐",
        "recommended": "个性化推荐",
    }
    SEARCH_SORT_LABELS = {
        "popular_desc": "热门优先",
        "date_desc": "最新优先",
    }
    FOLLOW_START_LABELS = {
        "resume": "智能续跑（推荐）",
        "restart": "从头开始",
        "index": "从指定序号开始",
        "artist": "从指定画师 ID 开始",
        "retry_failed": "仅重试未完整画师",
    }
    LOG_FILTERS = ("全部", "警告与错误", "仅错误")
    MAX_UI_LOG_LINES = 512
    # 日志泵：自适应轮询与按块裁剪，保证高频日志下界面依旧流畅。
    LOG_DRAIN_BATCH = 400
    LOG_POLL_BUSY_MS = 60
    LOG_POLL_ACTIVE_MS = 120
    LOG_POLL_IDLE_MS = 240
    LOG_TRIM_MARGIN = 32

    def _build_ui(self):
        """构建编辑式纸张工作台，同时保留全部历史控件契约。"""
        self._configure_theme()
        self.root.configure(background=self._colors["page"])
        self.root.grid_columnconfigure(0, minsize=76)
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(4, weight=1)

        self._build_rail_ui()
        self._build_header_ui()
        self._build_login_ui()

        workspace = ttk.Frame(self.root, style="Page.TFrame", padding=(28, 12, 28, 5))
        workspace.grid(row=2, column=1, sticky="nsew")
        workspace.grid_columnconfigure(0, weight=4, uniform="workspace")
        workspace.grid_columnconfigure(1, weight=6, uniform="workspace")
        workspace.grid_rowconfigure(0, weight=1)

        source_shell, source_body = self._create_section_shell(
            workspace,
            index="01",
            kicker="SOURCE",
            title="选择下载来源",
            description="七种入口 · 参数随模式切换",
        )
        source_shell.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        task_shell, task_body = self._create_section_shell(
            workspace,
            index="02",
            kicker="TASK",
            title="设置保存与品质",
            description="原图优先 · 可恢复 · 可追溯",
        )
        task_shell.grid(row=0, column=1, sticky="nsew", padx=(7, 0))

        self._build_mode_ui(source_body)
        self._build_params_ui(task_body)
        self._build_options_ui(task_body)
        self._build_buttons_ui()
        self._build_log_ui()

    def _configure_theme(self):
        """应用主窗口主题样式并构建色板。"""
        self._colors = theme.build_colors()
        theme.apply_main_styles(self.root, self._colors)

    def _build_rail_ui(self):
        """用窄索引轨替代传统侧边栏，提供稳定但克制的视觉锚点。"""
        colors = self._colors
        rail = tk.Frame(self.root, background=colors["paper_soft"], width=76)
        rail.grid(row=0, column=0, rowspan=5, sticky="nsw")
        rail.grid_propagate(False)
        rail.grid_rowconfigure(2, weight=1)

        mark = tk.Canvas(
            rail, width=36, height=36, background=colors["paper_soft"], highlightthickness=0
        )
        mark.grid(row=0, column=0, padx=16, pady=(22, 14))
        mark.create_oval(3, 3, 33, 33, outline=colors["accent"], width=1)
        mark.create_oval(15, 15, 21, 21, fill=colors["accent"], outline="")
        mark.create_line(18, 0, 18, 10, fill=colors["accent"], width=1)

        tk.Label(
            rail,
            text="PX\n—\nARCHIVE",
            background=colors["paper_soft"],
            foreground=colors["ink"],
            font=(theme.FONT_SERIF, 8),
            justify="center",
        ).grid(row=1, column=0, padx=5)
        tk.Frame(rail, width=1, background=colors["border"]).place(
            relx=1, rely=0, relheight=1, anchor="ne"
        )
        tk.Label(
            rail,
            text="00\n\n01\n\n02\n\n03\n\n04",
            background=colors["paper_soft"],
            foreground=colors["quiet"],
            font=(theme.FONT_MONO, 8),
            justify="center",
        ).grid(row=2, column=0, sticky="n", pady=(42, 0))
        tk.Label(
            rail,
            text="ORIGINAL\n/ FULL RES /",
            background=colors["paper_soft"],
            foreground=colors["accent"],
            font=(theme.FONT_MONO, 7, "bold"),
            justify="center",
        ).grid(row=3, column=0, padx=4, pady=(0, 20))

    def _create_section_shell(self, parent, *, index, kicker, title, description):
        """创建无圆角、带编号和细规则线的编辑式章节表面。"""
        colors = self._colors
        shell = tk.Frame(
            parent,
            background=colors["paper"],
            highlightbackground=colors["border"],
            highlightthickness=1,
            bd=0,
        )
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(2, weight=1)
        bind_hover_highlight(shell, normal=colors["border"], hover=colors["rule"])

        heading = tk.Frame(shell, background=colors["paper"])
        heading.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 9))
        heading.grid_columnconfigure(1, weight=1)
        tk.Label(
            heading,
            text=index,
            background=colors["paper"],
            foreground=colors["accent"],
            font=(theme.FONT_SERIF, 22),
        ).grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 12))
        tk.Label(
            heading,
            text=kicker,
            background=colors["paper"],
            foreground=colors["accent"],
            font=(theme.FONT_MONO, 8, "bold"),
        ).grid(row=0, column=1, sticky="sw")
        tk.Label(
            heading,
            text=title,
            background=colors["paper"],
            foreground=colors["ink"],
            font=(theme.FONT_UI, 11, "bold"),
        ).grid(row=1, column=1, sticky="nw")
        tk.Label(
            heading,
            text=description,
            background=colors["paper"],
            foreground=colors["muted"],
            font=(theme.FONT_UI, 8),
            anchor="e",
        ).grid(row=0, column=2, rowspan=2, sticky="e", padx=(12, 0))
        tk.Frame(shell, height=1, background=colors["rule"]).grid(row=1, column=0, sticky="ew")
        body = ttk.Frame(shell, style="Paper.TFrame", padding=(16, 11, 16, 13))
        body.grid(row=2, column=0, sticky="nsew")
        return shell, body

    def _build_header_ui(self):
        """构建顶部标题栏（PIXIV 标识 + 副标题）。"""
        colors = self._colors
        header = tk.Frame(self.root, background=colors["page"], height=96)
        header.grid(row=0, column=1, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(0, weight=1)

        title_block = tk.Frame(header, background=colors["page"])
        title_block.grid(row=0, column=0, sticky="w", padx=(28, 0), pady=(16, 10))
        title_line = tk.Frame(title_block, background=colors["page"])
        title_line.pack(anchor="w")
        tk.Label(
            title_line,
            text="PIXIV",
            background=colors["page"],
            foreground=colors["ink"],
            font=(theme.FONT_SERIF, 27),
        ).pack(side="left")
        tk.Label(
            title_line,
            text=" / ARCHIVE",
            background=colors["page"],
            foreground=colors["accent"],
            font=(theme.FONT_SERIF, 27),
        ).pack(side="left")
        tk.Label(
            title_block,
            text="原图采集工作台  ·  ORIGINAL IMAGE WORKSTATION",
            background=colors["page"],
            foreground=colors["muted"],
            font=(theme.FONT_UI, 8),
        ).pack(anchor="w", pady=(2, 0))

        edition = tk.Frame(header, background=colors["page"])
        edition.grid(row=0, column=1, sticky="e", padx=(0, 28), pady=(18, 10))
        tk.Label(
            edition,
            text="EDITION 03  /  DESKTOP",
            background=colors["page"],
            foreground=colors["quiet"],
            font=(theme.FONT_MONO, 8),
        ).pack(anchor="e")
        tk.Label(
            edition,
            text="QUALITY GUARANTEE   /img-original/",
            background=colors["page"],
            foreground=colors["accent"],
            font=(theme.FONT_MONO, 9, "bold"),
        ).pack(anchor="e", pady=(7, 0))
        tk.Frame(header, height=1, background=colors["rule"]).place(
            x=28, rely=1, relwidth=1, width=-56, y=-1
        )
        tk.Frame(
            header,
            width=54,
            height=3,
            background=colors["accent"],
        ).place(x=28, rely=1, y=-3)

    def _build_login_ui(self):
        """构建 Refresh Token 登录区与登录状态徽章。"""
        colors = self._colors
        outer = ttk.Frame(self.root, style="Page.TFrame", padding=(28, 9, 28, 0))
        outer.grid(row=1, column=1, sticky="ew")
        frame = tk.Frame(
            outer,
            background=colors["paper"],
            highlightbackground=colors["border"],
            highlightthickness=1,
            bd=0,
        )
        frame.pack(fill="x")
        frame.grid_columnconfigure(1, weight=1)
        bind_hover_highlight(frame, normal=colors["border"], hover=colors["rule"])

        access = tk.Frame(frame, background=colors["paper"])
        access.grid(row=0, column=0, rowspan=2, sticky="nsw", padx=(14, 18), pady=10)
        tk.Label(
            access,
            text="00 / ACCESS",
            background=colors["paper"],
            foreground=colors["accent"],
            font=(theme.FONT_MONO, 8, "bold"),
        ).pack(anchor="w")
        tk.Label(
            access,
            text="认证连接",
            background=colors["paper"],
            foreground=colors["ink"],
            font=(theme.FONT_UI, 10, "bold"),
        ).pack(anchor="w", pady=(2, 0))

        ttk.Label(frame, text="API / REFRESH TOKEN", style="Eyebrow.TLabel").grid(
            row=0, column=1, sticky="sw", padx=(0, 8), pady=(8, 2)
        )
        self.token_entry = ttk.Entry(frame, show="•", style="Editorial.TEntry")
        self.token_entry.grid(row=1, column=1, sticky="ew", padx=(0, 7), pady=(0, 9))
        self.login_btn = ttk.Button(
            frame, text="登录", command=self._login, style="Secondary.TButton"
        )
        self.login_btn.grid(row=1, column=2, padx=(0, 8), pady=(0, 9))
        self.login_status = tk.Label(
            frame,
            text="● 未登录",
            background=colors["danger_bg"],
            foreground=colors["danger"],
            font=(theme.FONT_UI, 8, "bold"),
            padx=8,
            pady=2,
        )
        self.login_status.grid(row=1, column=3, sticky="w", padx=(0, 13), pady=(0, 9))

        tk.Frame(frame, width=1, background=colors["border"]).grid(
            row=0, column=4, rowspan=2, sticky="ns", pady=9
        )
        cookie_meta = tk.Frame(frame, background=colors["paper"])
        cookie_meta.grid(row=0, column=5, rowspan=2, sticky="w", padx=(15, 8), pady=8)
        tk.Label(
            cookie_meta,
            text="COOKIE / ORIGINAL",
            background=colors["paper"],
            foreground=colors["accent"],
            font=(theme.FONT_MONO, 8, "bold"),
        ).pack(anchor="w")
        tk.Label(
            cookie_meta,
            text="自动读取 www.pixiv.net_cookies.txt",
            background=colors["paper"],
            foreground=colors["muted"],
            font=(theme.FONT_UI, 8),
        ).pack(anchor="w", pady=(3, 0))
        self.cookie_status_label = tk.Label(
            frame,
            text="● 正在检测",
            background=colors["warning_bg"],
            foreground=colors["warning"],
            font=(theme.FONT_UI, 8, "bold"),
            padx=8,
            pady=2,
        )
        self.cookie_status_label.grid(row=1, column=6, sticky="e", padx=(0, 6), pady=(0, 9))
        ttk.Button(
            frame, text="重新检测", command=self._refresh_cookie_status, style="Tool.TButton"
        ).grid(row=1, column=7, sticky="e", padx=(0, 12), pady=(0, 9))

    def _build_mode_ui(self, parent):
        """构建下载模式选择区（搜索/用户/相关/排行/关注）及模式相关输入控件。"""
        colors = self._colors
        parent.grid_columnconfigure(0, weight=1)
        selector = ttk.Frame(parent, style="Paper.TFrame")
        selector.grid(row=0, column=0, sticky="ew")
        selector.grid_columnconfigure(0, weight=1)
        ttk.Label(selector, text="MODE / 下载模式", style="Eyebrow.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.mode_var = tk.StringVar(value="search")
        self.mode_display_var = tk.StringVar(value=self.MODE_LABELS["search"])
        self.mode_combo = ttk.Combobox(
            selector,
            textvariable=self.mode_display_var,
            values=list(self.MODE_LABELS.values()),
            state="readonly",
            height=8,
            style="Editorial.TCombobox",
        )
        self.mode_combo.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.mode_combo.bind("<<ComboboxSelected>>", self._toggle_mode)

        ttk.Separator(parent, style="Editorial.TSeparator").grid(
            row=1, column=0, sticky="ew", pady=10
        )
        self.mode_detail_frame = ttk.Frame(parent, style="Paper.TFrame")
        self.mode_detail_frame.grid(row=2, column=0, sticky="nsew")
        self.mode_detail_frame.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(3, weight=1)
        tk.Label(
            parent,
            text="SOURCE / 01",
            background=colors["paper"],
            foreground=colors["ghost"],
            font=(theme.FONT_SERIF, 19),
        ).grid(row=3, column=0, sticky="se", pady=(12, 0))

        self.search_frame = ttk.Frame(self.mode_detail_frame, style="Paper.TFrame")
        self.search_frame.grid_columnconfigure(0, weight=3)
        self.search_frame.grid_columnconfigure(1, weight=2)
        ttk.Label(self.search_frame, text="搜索关键词", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.keyword_entry = ttk.Entry(self.search_frame, style="Editorial.TEntry")
        self.keyword_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0), padx=(0, 12))
        ttk.Label(self.search_frame, text="结果排序", style="Muted.TLabel").grid(
            row=0, column=1, sticky="w"
        )
        self.search_sort_combo = ttk.Combobox(
            self.search_frame,
            values=list(self.SEARCH_SORT_LABELS.values()),
            state="readonly",
            style="Editorial.TCombobox",
        )
        self.search_sort_combo.set(self.SEARCH_SORT_LABELS["popular_desc"])
        self.search_sort_combo.grid(row=1, column=1, sticky="ew", pady=(4, 0))

        self.user_frame = ttk.Frame(self.mode_detail_frame, style="Paper.TFrame")
        self.user_frame.grid_columnconfigure(0, weight=1)
        ttk.Label(
            self.user_frame, text="画师用户 ID（支持逗号、空格或换行）", style="Muted.TLabel"
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self.user_entry = ttk.Entry(self.user_frame, style="Editorial.TEntry")
        self.user_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0), padx=(0, 8))
        ttk.Button(
            self.user_frame, text="导入 TXT", command=self._import_user_ids, style="Tool.TButton"
        ).grid(row=1, column=1, sticky="e", pady=(4, 0))

        self.related_frame = ttk.Frame(self.mode_detail_frame, style="Paper.TFrame")
        self.related_frame.grid_columnconfigure(0, weight=1)
        ttk.Label(self.related_frame, text="种子作品 ID", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.related_entry = ttk.Entry(self.related_frame, style="Editorial.TEntry")
        self.related_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        self.rank_frame = ttk.Frame(self.mode_detail_frame, style="Paper.TFrame")
        self.rank_frame.grid_columnconfigure(0, weight=2)
        self.rank_frame.grid_columnconfigure(1, weight=3)
        ttk.Label(self.rank_frame, text="排行榜类型", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.rank_combo = ttk.Combobox(
            self.rank_frame,
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
                "week_r18g",
                "day_manga",
                "week_manga",
                "month_manga",
                "week_rookie_manga",
            ],
            state="readonly",
            style="Editorial.TCombobox",
        )
        self.rank_combo.set("day")
        self.rank_combo.grid(row=1, column=0, sticky="ew", pady=(4, 0), padx=(0, 12))
        dates = ttk.Frame(self.rank_frame, style="Paper.TFrame")
        dates.grid(row=1, column=1, sticky="ew")
        dates.grid_columnconfigure(0, weight=1)
        dates.grid_columnconfigure(2, weight=1)
        self.rank_date_entry = ttk.Entry(dates, style="Editorial.TEntry")
        self.rank_date_entry.grid(row=0, column=0, sticky="ew")
        ttk.Label(dates, text="至", style="Muted.TLabel").grid(row=0, column=1, padx=6)
        self.rank_date_end_entry = ttk.Entry(dates, style="Editorial.TEntry")
        self.rank_date_end_entry.grid(row=0, column=2, sticky="ew")
        ttk.Label(
            self.rank_frame, text="日期范围（YYYY-MM-DD，均留空表示今天）", style="Muted.TLabel"
        ).grid(row=0, column=1, sticky="w")

        self.empty_mode_frame = ttk.Frame(self.mode_detail_frame, style="Paper.TFrame")
        self.mode_hint_label = ttk.Label(
            self.empty_mode_frame,
            text="此模式无需额外参数。",
            style="Muted.TLabel",
            wraplength=390,
            justify="left",
        )
        self.mode_hint_label.pack(anchor="w", pady=(4, 0))

        self.follow_all_frame = ttk.Frame(self.mode_detail_frame, style="Paper.TFrame")
        self.follow_all_frame.grid_columnconfigure(0, weight=3)
        self.follow_all_frame.grid_columnconfigure(1, weight=2)
        ttk.Label(self.follow_all_frame, text="任务起点", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 10)
        )
        ttk.Label(self.follow_all_frame, text="序号 / 画师 ID", style="Muted.TLabel").grid(
            row=0, column=1, sticky="w"
        )
        self.follow_start_mode_var = tk.StringVar(value=self.FOLLOW_START_LABELS["resume"])
        self.follow_start_mode_combo = ttk.Combobox(
            self.follow_all_frame,
            textvariable=self.follow_start_mode_var,
            values=list(self.FOLLOW_START_LABELS.values()),
            state="readonly",
            style="Editorial.TCombobox",
        )
        self.follow_start_mode_combo.grid(row=1, column=0, sticky="ew", padx=(0, 10), pady=(4, 0))
        self.follow_start_mode_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._toggle_follow_start_input(),
        )
        self.follow_start_value_entry = ttk.Entry(self.follow_all_frame, style="Editorial.TEntry")
        self.follow_start_value_entry.grid(row=1, column=1, sticky="ew", pady=(4, 0))

        limits = ttk.Frame(self.follow_all_frame, style="Paper.TFrame")
        limits.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        limits.grid_columnconfigure(1, weight=1)
        limits.grid_columnconfigure(3, weight=1)
        ttk.Label(limits, text="每批画师", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.follow_batch_artists_entry = ttk.Entry(limits, width=7, style="Editorial.TEntry")
        self.follow_batch_artists_entry.grid(row=0, column=1, sticky="ew", padx=(7, 16))
        self.follow_batch_artists_entry.insert(0, "30")
        ttk.Label(limits, text="API 请求预算", style="Muted.TLabel").grid(
            row=0, column=2, sticky="w"
        )
        self.follow_api_budget_entry = ttk.Entry(limits, width=7, style="Editorial.TEntry")
        self.follow_api_budget_entry.grid(row=0, column=3, sticky="ew", padx=(7, 0))
        self.follow_api_budget_entry.insert(0, "500")
        ttk.Label(
            self.follow_all_frame,
            text="断流、429 或关闭程序后均会保存到当前画师和分页；下载记录不受影响。",
            style="Muted.TLabel",
            wraplength=390,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self._toggle_follow_start_input()
        self._toggle_mode()

    def _build_params_ui(self, parent):
        """构建参数输入区（数量/线程/下载间隔/API间隔/收藏阈值）。"""
        parent.grid_columnconfigure(0, weight=1)
        summary = ttk.Frame(parent, style="Paper.TFrame")
        summary.grid(row=0, column=0, sticky="ew")
        summary.grid_columnconfigure(0, weight=1)
        summary.grid_columnconfigure(1, weight=1)

        count_box = ttk.Frame(summary, style="Paper.TFrame")
        count_box.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Label(count_box, text="下载作品数量", style="Muted.TLabel").pack(anchor="w")
        self.count_entry = ttk.Entry(count_box, style="Editorial.TEntry")
        self.count_entry.pack(fill="x", pady=(4, 0))
        self.count_entry.insert(0, "100")

        bookmark_box = ttk.Frame(summary, style="Paper.TFrame")
        bookmark_box.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Label(bookmark_box, text="最低收藏数", style="Muted.TLabel").pack(anchor="w")
        self.min_bookmarks_entry = ttk.Entry(bookmark_box, style="Editorial.TEntry")
        self.min_bookmarks_entry.pack(fill="x", pady=(4, 0))
        self.min_bookmarks_entry.insert(0, "0")

        ttk.Label(parent, text="保存目录", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(12, 0)
        )
        dir_row = ttk.Frame(parent, style="Paper.TFrame")
        dir_row.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        dir_row.grid_columnconfigure(0, weight=1)
        self.dir_entry = ttk.Entry(dir_row, style="Editorial.TEntry")
        self.dir_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(dir_row, text="浏览", command=self._browse_dir, style="Tool.TButton").grid(
            row=0, column=1, padx=(0, 4)
        )
        ttk.Button(dir_row, text="打开", command=self._open_dir, style="Tool.TButton").grid(
            row=0, column=2
        )

    def _build_options_ui(self, parent):
        """构建选项开关区（原图/多页/按画师分目录/跳过AI/跳过已下载）与命名模板。"""
        ttk.Separator(parent, style="Editorial.TSeparator").grid(
            row=3, column=0, sticky="ew", pady=(11, 9)
        )
        ttk.Label(parent, text="QUALITY / 下载策略", style="Eyebrow.TLabel").grid(
            row=4, column=0, sticky="w"
        )
        option_grid = ttk.Frame(parent, style="Paper.TFrame")
        option_grid.grid(row=5, column=0, sticky="ew", pady=(4, 0))
        for column in range(3):
            option_grid.grid_columnconfigure(column, weight=1)

        self.original_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            option_grid,
            text="下载原图（禁止降级）",
            variable=self.original_var,
            style="Card.TCheckbutton",
        ).grid(row=0, column=0, sticky="w")
        self.multi_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            option_grid, text="多图全部页", variable=self.multi_var, style="Card.TCheckbutton"
        ).grid(row=0, column=1, sticky="w")
        self.skip_downloaded_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            option_grid,
            text="断点续传",
            variable=self.skip_downloaded_var,
            style="Card.TCheckbutton",
        ).grid(row=0, column=2, sticky="w")
        self.skip_ai_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            option_grid, text="跳过 AI 作品", variable=self.skip_ai_var, style="Card.TCheckbutton"
        ).grid(row=1, column=0, sticky="w", pady=(3, 0))
        self.folder_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            option_grid, text="按画师分文件夹", variable=self.folder_var, style="Card.TCheckbutton"
        ).grid(row=1, column=1, sticky="w", pady=(3, 0))

        self.advanced_var = tk.BooleanVar(value=False)
        advanced_toggle = ttk.Checkbutton(
            option_grid,
            text="显示高级设置",
            variable=self.advanced_var,
            command=self._toggle_advanced,
            style="Card.TCheckbutton",
        )
        advanced_toggle.grid(row=1, column=2, sticky="w", pady=(3, 0))

        self.advanced_frame = ttk.Frame(parent, style="Paper.TFrame")
        for column in range(3):
            self.advanced_frame.grid_columnconfigure(column, weight=1)
        thread_box = ttk.Frame(self.advanced_frame, style="Paper.TFrame")
        thread_box.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Label(thread_box, text="线程", style="Muted.TLabel").pack(side="left", padx=(0, 5))
        self.thread_entry = ttk.Entry(thread_box, width=6, style="Editorial.TEntry")
        self.thread_entry.pack(side="left", fill="x", expand=True)
        self.thread_entry.insert(0, "3")
        delay_box = ttk.Frame(self.advanced_frame, style="Paper.TFrame")
        delay_box.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(delay_box, text="下载间隔", style="Muted.TLabel").pack(side="left", padx=(0, 5))
        self.delay_entry = ttk.Entry(delay_box, width=6, style="Editorial.TEntry")
        self.delay_entry.pack(side="left", fill="x", expand=True)
        self.delay_entry.insert(0, "0.5")
        api_box = ttk.Frame(self.advanced_frame, style="Paper.TFrame")
        api_box.grid(row=0, column=2, sticky="ew", padx=(6, 0))
        ttk.Label(api_box, text="API 间隔", style="Muted.TLabel").pack(side="left", padx=(0, 5))
        self.api_delay_entry = ttk.Entry(api_box, width=6, style="Editorial.TEntry")
        self.api_delay_entry.pack(side="left", fill="x", expand=True)
        self.api_delay_entry.insert(0, "0.3")

        ttk.Label(self.advanced_frame, text="命名模板", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(7, 0)
        )
        self.name_template = ttk.Combobox(
            self.advanced_frame,
            values=[
                "{id}_{title}_p{page}",
                "{user}_{id}_{title}_p{page}",
                "{id}_p{page}",
                "{date}_{id}_{title}_p{page}",
            ],
            style="Editorial.TCombobox",
        )
        self.name_template.grid(row=1, column=1, sticky="ew", padx=(8, 6), pady=(7, 0))
        self.name_template.set("{id}_{title}_p{page}")
        ttk.Label(
            self.advanced_frame,
            text="变量：{id} {title} {user} {user_id} {page} {date}",
            style="Muted.TLabel",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(3, 0))
        ttk.Button(
            self.advanced_frame,
            text="清空下载记录…",
            command=self._clear_downloaded_ids,
            style="Danger.TButton",
        ).grid(row=1, column=2, sticky="e", pady=(7, 0))

    def _build_buttons_ui(self):
        """构建操作按钮区（开始/停止/打开目录/工具菜单）。"""
        colors = self._colors
        outer = ttk.Frame(self.root, style="Page.TFrame", padding=(28, 6, 28, 5))
        outer.grid(row=3, column=1, sticky="ew")
        frame = tk.Frame(
            outer,
            background=colors["paper"],
            highlightbackground=colors["border"],
            highlightthickness=1,
            bd=0,
        )
        frame.pack(fill="x")
        frame.grid_columnconfigure(6, weight=1)
        bind_hover_highlight(frame, normal=colors["border"], hover=colors["rule"])

        meta = tk.Frame(frame, background=colors["paper"])
        meta.grid(row=0, column=0, rowspan=2, sticky="w", padx=(14, 18), pady=9)
        tk.Label(
            meta,
            text="03 / EXECUTE",
            background=colors["paper"],
            foreground=colors["accent"],
            font=(theme.FONT_MONO, 8, "bold"),
        ).pack(anchor="w")
        tk.Label(
            meta,
            text="执行队列",
            background=colors["paper"],
            foreground=colors["ink"],
            font=(theme.FONT_UI, 10, "bold"),
        ).pack(anchor="w", pady=(2, 0))

        self.start_btn = ttk.Button(
            frame, text="开始下载  →", command=self._start_download, style="Primary.TButton"
        )
        self.start_btn.grid(row=0, column=1, rowspan=2, padx=(0, 7), pady=9)
        self.stop_btn = ttk.Button(
            frame,
            text="停止",
            command=self._stop_download,
            state="disabled",
            style="Secondary.TButton",
        )
        self.stop_btn.grid(row=0, column=2, rowspan=2, padx=(0, 7), pady=9)
        ttk.Button(
            frame, text="多条件筛选", command=self._open_multi_filter, style="Secondary.TButton"
        ).grid(row=0, column=3, rowspan=2, padx=(0, 12), pady=9)

        import core.download_service as pixiv_utils

        self.debug_var = tk.BooleanVar(value=pixiv_utils.DEBUG_MODE)
        ttk.Checkbutton(
            frame,
            text="调试日志",
            variable=self.debug_var,
            command=self._toggle_debug,
            style="Card.TCheckbutton",
        ).grid(row=0, column=4, rowspan=2, padx=(0, 12), pady=9)

        tk.Frame(frame, width=1, background=colors["border"]).grid(
            row=0, column=5, rowspan=2, sticky="ns", pady=10, padx=(0, 14)
        )
        progress = ttk.Frame(frame, style="Paper.TFrame")
        progress.grid(row=0, column=6, rowspan=2, sticky="ew", padx=(0, 14), pady=10)
        progress.grid_columnconfigure(0, weight=1)
        self.status_label = ttk.Label(
            progress, text="就绪", style="Card.TLabel", font=(theme.FONT_UI, 9, "bold")
        )
        self.status_label.grid(row=0, column=0, sticky="w")

    def _build_log_ui(self):
        """构建日志显示区与工具栏（清空/导出/调试切换/记录备份）。"""
        colors = self._colors
        outer = ttk.Frame(self.root, style="Page.TFrame", padding=(28, 5, 28, 18))
        outer.grid(row=4, column=1, sticky="nsew")
        outer.grid_rowconfigure(0, weight=1)
        outer.grid_columnconfigure(0, weight=1)
        stage = tk.Frame(outer, background=colors["offset"])
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
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        card.grid_rowconfigure(2, weight=1)
        card.grid_columnconfigure(0, weight=1)
        bind_hover_highlight(card, normal=colors["rule"], hover=colors["accent"])

        heading = tk.Frame(card, background=colors["paper"])
        heading.grid(row=0, column=0, sticky="ew", padx=13, pady=(9, 5))
        heading.grid_columnconfigure(1, weight=1)
        tk.Label(
            heading,
            text="04 / SESSION LOG",
            background=colors["paper"],
            foreground=colors["accent"],
            font=(theme.FONT_MONO, 8, "bold"),
        ).grid(row=0, column=0, sticky="w")

        toolbar = ttk.Frame(heading, style="Paper.TFrame")
        toolbar.grid(row=0, column=1, sticky="e")
        ttk.Label(toolbar, text="VIEW", style="Eyebrow.TLabel").pack(side="left", padx=(0, 5))
        self.log_filter_var = tk.StringVar(value=self.LOG_FILTERS[0])
        filter_combo = ttk.Combobox(
            toolbar,
            textvariable=self.log_filter_var,
            values=self.LOG_FILTERS,
            state="readonly",
            width=10,
            style="Editorial.TCombobox",
        )
        filter_combo.pack(side="left", padx=(0, 10))
        filter_combo.bind("<<ComboboxSelected>>", lambda _event: self._render_log_buffer())
        self.auto_scroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            toolbar,
            text="自动滚动",
            variable=self.auto_scroll_var,
            style="Card.TCheckbutton",
        ).pack(side="left")
        ttk.Button(toolbar, text="清空视图", command=self._clear_log, style="Tool.TButton").pack(
            side="left", padx=(10, 5)
        )
        ttk.Button(
            toolbar, text="导出完整会话", command=self._export_log, style="Tool.TButton"
        ).pack(side="left")
        ttk.Label(
            card,
            text=f"界面窗口仅保留最近 {self.MAX_UI_LOG_LINES} 行  /  完整会话分卷落盘，不丢失历史",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", padx=13, pady=(0, 5))

        # wrap="none"（DualScrollText 默认值）：避免长 URL/路径触发整控件
        # 换行重排，这是长会话日志卡顿的主要根因。
        self.log_text = DualScrollText(
            card,
            state="disabled",
            height=10,
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
            padx=10,
            pady=8,
        )
        self.log_text.grid(row=2, column=0, sticky="nsew", padx=13, pady=(0, 13))
        style_text_scrollbar(self.log_text, colors)
        self._log_lines = deque(maxlen=self.MAX_UI_LOG_LINES)
        self._display_line_count = 0

    def _toggle_mode(self, _event=None):
        """切换下载模式并只显示当前模式的参数。"""
        if _event is not None:
            display_value = self.mode_display_var.get()
            reverse_labels = {label: value for value, label in self.MODE_LABELS.items()}
            if display_value in reverse_labels:
                self.mode_var.set(reverse_labels[display_value])
        mode = self.mode_var.get()
        self.mode_display_var.set(self.MODE_LABELS.get(mode, self.MODE_LABELS["search"]))

        frames = [
            self.search_frame,
            self.user_frame,
            self.rank_frame,
            self.related_frame,
            self.empty_mode_frame,
        ]
        follow_all_frame = getattr(self, "follow_all_frame", None)
        if follow_all_frame is not None:
            frames.append(follow_all_frame)
        for frame in frames:
            frame.grid_forget()
        active_frame = {
            "search": self.search_frame,
            "user": self.user_frame,
            "ranking": self.rank_frame,
            "related": self.related_frame,
            "follow_all": follow_all_frame or self.empty_mode_frame,
        }.get(mode, self.empty_mode_frame)
        if active_frame is self.empty_mode_frame:
            hints = {
                "follow": "下载当前账号关注用户的最新作品，无需额外参数。",
                "follow_all": "遍历所有关注画师；任务较长，会自动限速并支持停止。",
                "recommended": "获取 Pixiv 个性化推荐作品（不是简单的关注列表）。",
            }
            self.mode_hint_label.config(text=hints.get(mode, "此模式无需额外参数。"))
        active_frame.grid(row=0, column=0, sticky="nsew")

    def _follow_start_mode_value(self) -> str:
        labels = getattr(self, "FOLLOW_START_LABELS", {})
        reverse_labels = {label: value for value, label in labels.items()}
        selected = self.follow_start_mode_var.get()
        return reverse_labels.get(selected, selected or "resume")

    def _toggle_follow_start_input(self):
        entry = getattr(self, "follow_start_value_entry", None)
        if entry is None:
            return
        mode = self._follow_start_mode_value()
        entry.configure(state="normal" if mode in ("index", "artist") else "disabled")

    def _toggle_advanced(self):
        if self.advanced_var.get():
            self.advanced_frame.grid(row=7, column=0, sticky="ew", pady=(8, 0))
        else:
            self.advanced_frame.grid_forget()

    def _search_sort_value(self) -> str:
        selected = self.search_sort_combo.get()
        reverse_labels = {label: value for value, label in self.SEARCH_SORT_LABELS.items()}
        return reverse_labels.get(selected, selected or "popular_desc")

    def _refresh_cookie_status(self):
        provider = getattr(self, "cookie_provider", None)
        if provider is not None:
            provider.invalidate()
        if self._load_cookie_file():
            self.log_queue.put("[+] 原图 Cookie 检测成功")
        else:
            self.log_queue.put("[!] 未检测到有效原图 Cookie，请更新 www.pixiv.net_cookies.txt")

    def _browse_dir(self):
        directory = filedialog.askdirectory()
        if directory:
            self._set_entry(self.dir_entry, directory)

    def _open_dir(self):
        save_dir = self.dir_entry.get().strip()
        if save_dir and os.path.exists(save_dir):
            if sys.platform == "win32":
                os.startfile(save_dir)
            elif sys.platform == "darwin":
                subprocess.run(["open", save_dir], check=False)
            else:
                subprocess.run(["xdg-open", save_dir], check=False)

    def _import_user_ids(self):
        file_path = filedialog.askopenfilename(filetypes=[("文本文件", "*.txt")])
        if not file_path:
            return
        try:
            ids = []
            with open(file_path, "r", encoding="utf-8", errors="ignore") as file_obj:
                for line in file_obj:
                    ids.extend(re.findall(r"\d+", line))
            if ids:
                self._set_entry(self.user_entry, ", ".join(ids))
                messagebox.showinfo("导入完成", f"已导入 {len(ids)} 个用户 ID")
        except Exception as exc:
            messagebox.showerror("读取失败", str(exc))

    def _clear_log(self):
        self._log_lines.clear()
        self._display_line_count = 0
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state="disabled")

    def _export_log(self):
        content = ""
        reader = getattr(self.log_queue, "read_session_text", None)
        if callable(reader):
            content = reader().strip()
        if not content:
            content = self.log_text.get("1.0", tk.END).strip()
        if not content:
            messagebox.showinfo("暂无日志", "当前会话还没有可导出的日志。")
            return
        file_path = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt")],
            initialfile=f"pixiv_session_{time.strftime('%Y%m%d_%H%M%S')}.log",
        )
        if file_path:
            Path(file_path).write_text(content, encoding="utf-8")
            messagebox.showinfo("导出完成", "完整会话日志已导出。")

    def _toggle_debug(self):
        import core.download_service as pixiv_utils

        pixiv_utils.set_debug_mode(self.debug_var.get())
        self.log_queue.put(f"[*] 调试模式: {'开启' if self.debug_var.get() else '关闭'}")

    @staticmethod
    def _is_download_record_file(file_name: str) -> bool:
        return (
            file_name
            in {
                "downloaded_id_record.json",
                "downloaded_record.json",
                "global_download_history.md",
                "全局下载总记录.md",
                "success_log.md",
            }
            or file_name.endswith("_download_history.md")
            or file_name.endswith("_作品下载记录.md")
            or file_name.endswith("_download_history.md.json")
            or file_name.endswith("_作品下载记录.md.json")
        )

    def _backup_download_records(self, save_dir: str) -> Path:
        backup_root = (
            self._app_paths().project_root / "record_backups" / time.strftime("%Y%m%d_%H%M%S")
        )
        backup_root.mkdir(parents=True, exist_ok=False)
        downloaded_ids = self._downloaded_ids_path()
        if downloaded_ids.exists():
            shutil.copy2(downloaded_ids, backup_root / downloaded_ids.name)
        central_root = Path(self._app_paths().download_logs_dir)
        if central_root.exists():
            shutil.copytree(central_root, backup_root / "download_logs")
        source_root = Path(save_dir) if save_dir else None
        if source_root and source_root.exists():
            for source in source_root.rglob("*"):
                if not source.is_file() or not self._is_download_record_file(source.name):
                    continue
                relative = source.relative_to(source_root)
                destination = backup_root / "download_records" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        return backup_root

    def _clear_downloaded_ids(self):
        if getattr(self, "running", False):
            messagebox.showwarning("任务运行中", "请先停止下载并等待任务完全结束，再管理下载记录。")
            return
        warning = (
            "此操作会清空断点续传 ID、本地记录和中央记录。\n\n"
            "程序会先创建完整记录备份；图片文件不会删除。是否继续？"
        )
        if not messagebox.askyesno("高风险操作", warning, icon="warning"):
            return
        try:
            save_dir = self.dir_entry.get().strip()
            backup_root = self._backup_download_records(save_dir)
            if not messagebox.askyesno(
                "最后确认",
                f"记录已备份到：\n{backup_root}\n\n确认清空原记录？",
                icon="warning",
            ):
                return
            downloaded_ids_path = self._downloaded_ids_path()
            if downloaded_ids_path.exists():
                downloaded_ids_path.unlink()
            clear_download_records(save_dir, clear_central=True)
            messagebox.showinfo("已完成", f"记录已清空，备份保存在：\n{backup_root}")
        except Exception as exc:
            messagebox.showerror("操作失败", f"未能安全清空记录：{exc}")

    def _open_multi_filter(self):
        if open_multi_filter is None:
            messagebox.showerror("模块缺失", "多条件筛选模块未找到")
            return
        if self.api is None:
            messagebox.showwarning("需要登录", "请先完成 Pixiv API 登录")
            return
        config = {
            "project_root": str(self._app_paths().project_root),
            "save_dir": self.dir_entry.get().strip(),
            "threads": self.thread_entry.get().strip(),
            "delay": self.delay_entry.get().strip(),
            "api_delay": self.api_delay_entry.get().strip(),
            "original": bool(self.original_var.get()),
            "multi_page": bool(self.multi_var.get()),
            "folder_by_user": bool(self.folder_var.get()),
            "skip_downloaded": bool(self.skip_downloaded_var.get()),
            "name_template": self.name_template.get(),
            "auth_cookie": self.auth_cookie,
            "user_agent": self.user_agent,
        }
        open_multi_filter(self.root, self.api, config)

    def _log_line_visible(self, line: str) -> bool:
        selected = self.log_filter_var.get()
        if selected == "仅错误":
            return line.lstrip().startswith("[-]")
        if selected == "警告与错误":
            return line.lstrip().startswith(("[-]", "[!]"))
        return True

    def _render_log_buffer(self):
        visible = [line for line in self._log_lines if self._log_line_visible(line)]
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", tk.END)
        if visible:
            self.log_text.insert(tk.END, "\n".join(visible) + "\n")
        self.log_text.config(state="disabled")
        self._display_line_count = len(visible)
        if self.auto_scroll_var.get():
            self.log_text.see(tk.END)

    def _process_log_queue(self):
        """批量刷新日志：自适应轮询 + 按块裁剪 + 贴底跟随，避免高频日志卡顿。"""
        if getattr(self, "_is_closing", False):
            return
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return

        drained = 0
        try:
            lines = []
            for _ in range(self.LOG_DRAIN_BATCH):
                try:
                    message = str(self.log_queue.get_nowait())
                except queue.Empty:
                    break
                drained += 1
                split_lines = message.splitlines() or [""]
                for line in split_lines:
                    self._log_lines.append(line)
                    if self._log_line_visible(line):
                        lines.append(line)

            if lines:
                self._display_line_count, _ok = append_log_lines(
                    self.log_text,
                    lines,
                    max_lines=self.MAX_UI_LOG_LINES,
                    display_count=self._display_line_count,
                    trim_margin=self.LOG_TRIM_MARGIN,
                    autoscroll=bool(self.auto_scroll_var.get()),
                )
                # _ok=False 仅在 Text 控件触发 TclError（例如窗口正在销毁）
                # 时出现。不能因此终止轮询：_is_closing 守卫与下一次
                # winfo_exists 检查会干净地处理关闭流程，避免一次瞬时异常
                # 让日志永久停止显示。
        except tk.TclError:
            pass
        except Exception:
            # 任何意外异常都不得杀死日志轮询循环，否则日志会从 UI 中
            # 静默消失且永远不会恢复。限流打印到 stderr 供诊断，避免刷屏。
            count = getattr(self, "_log_poll_error_count", 0)
            if count < 3:
                self._log_poll_error_count = count + 1
                import traceback as _tb

                print(
                    f"[pidl] _process_log_queue 第 {count + 1} 次异常（已忽略，轮询继续）:\n"
                    f"{_tb.format_exc()}",
                    flush=True,
                )

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
            self._log_after_id = self.root.after(delay, self._process_log_queue)
        except tk.TclError:
            self._log_after_id = None
