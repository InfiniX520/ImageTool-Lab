#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集中式设计令牌：调色板、字体、DPI 感知与共享 ttk 样式。

主窗口与多条件筛选窗口共用同一份令牌，保证视觉一致性；
样式名仍保持各自的历史契约（主窗口无前缀，筛选窗口使用 Filter 前缀）。
"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"
FONT_SERIF = "Georgia"


def build_colors() -> dict:
    """返回一份全新的应用调色板（键集合是旧版的超集）。"""
    return {
        "page": "#F5F4EF",
        "paper": "#FFFFFF",
        "paper_soft": "#FCFBF8",
        "card": "#FFFFFF",
        "ink": "#201B15",
        "muted": "#66707A",
        "quiet": "#9AA3A9",
        "border": "#D8DEE2",
        "rule": "#9CC4DA",
        "accent": "#1F6FD0",
        "accent_hover": "#1A5EB4",
        "accent_active": "#144E96",
        "accent_wash": "#EAF2FC",
        "offset": "#DCE9F4",
        "success": "#177245",
        "success_bg": "#E6F4EC",
        "warning": "#9A5B12",
        "warning_bg": "#FBF1DF",
        "danger": "#B3261E",
        "danger_bg": "#FAE9E8",
        "danger_border": "#E7C5C2",
        "log": "#FAFBF7",
        "log_ink": "#26363C",
        "ghost": "#E2EDF1",
    }


_TONE_KEYS = {
    "success": ("success", "success_bg"),
    "warning": ("warning", "warning_bg"),
    "danger": ("danger", "danger_bg"),
    "accent": ("accent", "accent_wash"),
    "muted": ("muted", "paper"),
}


def tone_colors(colors: dict, tone: str) -> tuple[str, str]:
    """把语义色调映射为 (前景, 背景)，用于状态徽章。"""
    fg_key, bg_key = _TONE_KEYS.get(tone, _TONE_KEYS["muted"])
    return colors.get(fg_key, "#66707A"), colors.get(bg_key, "#FFFFFF")


def enable_dpi_awareness() -> None:
    """在 Windows 上启用 Per-Monitor DPI 感知，保证高分屏渲染清晰。

    必须在创建第一个 Tk 窗口之前调用；其他平台为空操作。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            # PROCESS_PER_MONITOR_DPI_AWARE = 2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def hex_blend(color_a: str, color_b: str, t: float) -> str:
    """在两个 #rrggbb 颜色之间线性插值。"""
    try:
        a = tuple(int(color_a[i : i + 2], 16) for i in (1, 3, 5))
        b = tuple(int(color_b[i : i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return color_b
    t = min(1.0, max(0.0, t))
    mixed = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return "#{:02X}{:02X}{:02X}".format(*mixed)


def _use_clam(style: ttk.Style) -> None:
    if "clam" in style.theme_names():
        style.theme_use("clam")


def apply_main_styles(root: tk.Misc, colors: dict) -> None:
    """配置主窗口全部 ttk 样式（含全局基础样式）。"""
    style = ttk.Style(root)
    _use_clam(style)

    style.configure(".", font=(FONT_UI, 9))
    style.configure("TFrame", background=colors["paper"])
    style.configure("TLabel", background=colors["paper"], foreground=colors["ink"])
    style.configure("Page.TFrame", background=colors["page"])
    style.configure("Paper.TFrame", background=colors["paper"])
    style.configure("Card.TFrame", background=colors["paper"])
    style.configure(
        "Card.TLabelframe",
        background=colors["paper"],
        bordercolor=colors["border"],
        relief="flat",
        borderwidth=1,
    )
    style.configure(
        "Card.TLabelframe.Label",
        background=colors["paper"],
        foreground=colors["ink"],
        font=(FONT_UI, 10, "bold"),
    )
    style.configure("Card.TLabel", background=colors["paper"], foreground=colors["ink"])
    style.configure("Body.TLabel", background=colors["paper"], foreground=colors["ink"])
    style.configure(
        "Muted.TLabel",
        background=colors["paper"],
        foreground=colors["muted"],
        font=(FONT_UI, 8),
    )
    style.configure(
        "Eyebrow.TLabel",
        background=colors["paper"],
        foreground=colors["accent"],
        font=(FONT_MONO, 8, "bold"),
    )
    _configure_checkbutton(style, colors, "Card.TCheckbutton")
    _configure_entry(style, colors, "Editorial.TEntry", padding=(8, 6))
    _configure_combobox(style, colors, "Editorial.TCombobox", padding=(8, 5))
    _configure_primary_button(style, colors, "Primary.TButton", padding=(20, 9))
    _configure_secondary_button(style, colors, "Secondary.TButton", padding=(12, 7))
    style.configure(
        "Tool.TButton",
        background=colors["paper"],
        foreground=colors["muted"],
        bordercolor=colors["border"],
        lightcolor=colors["paper"],
        darkcolor=colors["paper"],
        padding=(9, 6),
        font=(FONT_UI, 8),
    )
    style.map(
        "Tool.TButton",
        foreground=[("active", colors["accent"]), ("disabled", colors["quiet"])],
        bordercolor=[("active", colors["accent"])],
        background=[("active", colors["accent_wash"]), ("disabled", colors["page"])],
    )
    style.configure(
        "Danger.TButton",
        background=colors["paper"],
        foreground=colors["danger"],
        bordercolor=colors["danger_border"],
        lightcolor=colors["paper"],
        darkcolor=colors["paper"],
        padding=(12, 7),
    )
    style.map(
        "Danger.TButton",
        background=[("active", colors["danger_bg"]), ("disabled", colors["page"])],
        bordercolor=[("active", colors["danger"])],
        foreground=[("disabled", colors["quiet"])],
    )
    style.configure(
        "App.Horizontal.TProgressbar",
        background=colors["accent"],
        troughcolor=colors["accent_wash"],
        bordercolor=colors["accent_wash"],
        lightcolor=colors["accent"],
        darkcolor=colors["accent"],
        thickness=6,
    )
    style.configure("Editorial.TSeparator", background=colors["border"])
    root.option_add("*TCombobox*Listbox.font", (FONT_UI, 9))
    root.option_add("*TCombobox*Listbox.selectBackground", colors["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")


def apply_filter_styles(root: tk.Misc, colors: dict) -> None:
    """配置多条件筛选窗口的 ttk 样式（全部使用 Filter 前缀，避免污染全局）。"""
    style = ttk.Style(root)
    _use_clam(style)

    style.configure("FilterPage.TFrame", background=colors["page"])
    style.configure("FilterPaper.TFrame", background=colors["paper"])
    style.configure(
        "FilterMuted.TLabel",
        background=colors["paper"],
        foreground=colors["muted"],
        font=(FONT_UI, 8),
    )
    style.configure(
        "FilterEyebrow.TLabel",
        background=colors["paper"],
        foreground=colors["accent"],
        font=(FONT_MONO, 8, "bold"),
    )
    _configure_checkbutton(style, colors, "Filter.TCheckbutton")
    _configure_entry(style, colors, "Filter.TEntry", padding=(7, 5))
    _configure_combobox(style, colors, "Filter.TCombobox", padding=(7, 4))
    _configure_primary_button(style, colors, "FilterPrimary.TButton", padding=(18, 8))
    _configure_secondary_button(style, colors, "FilterSecondary.TButton", padding=(11, 6))


def _configure_checkbutton(style: ttk.Style, colors: dict, name: str) -> None:
    style.configure(
        name,
        background=colors["paper"],
        foreground=colors["ink"],
        indicatorbackground=colors["paper"],
        indicatorforeground=colors["accent"],
        padding=(0, 2),
    )
    style.map(
        name,
        background=[("active", colors["paper"])],
        indicatorbackground=[
            ("selected", colors["accent"]),
            ("active", colors["accent_wash"]),
        ],
        indicatorforeground=[("selected", "#FFFFFF")],
    )


def _configure_entry(style: ttk.Style, colors: dict, name: str, *, padding: tuple) -> None:
    style.configure(
        name,
        fieldbackground=colors["paper_soft"],
        foreground=colors["ink"],
        bordercolor=colors["border"],
        lightcolor=colors["border"],
        darkcolor=colors["border"],
        insertcolor=colors["ink"],
        padding=padding,
    )
    style.map(
        name,
        bordercolor=[("focus", colors["accent"])],
        lightcolor=[("focus", colors["accent"])],
        darkcolor=[("focus", colors["accent"])],
        fieldbackground=[("disabled", colors["page"])],
        foreground=[("disabled", colors["quiet"])],
    )


def _configure_combobox(style: ttk.Style, colors: dict, name: str, *, padding: tuple) -> None:
    style.configure(
        name,
        fieldbackground=colors["paper_soft"],
        background=colors["paper"],
        foreground=colors["ink"],
        arrowcolor=colors["accent"],
        bordercolor=colors["border"],
        lightcolor=colors["border"],
        darkcolor=colors["border"],
        padding=padding,
    )
    style.map(
        name,
        fieldbackground=[("readonly", colors["paper_soft"])],
        bordercolor=[("focus", colors["accent"])],
        arrowcolor=[("active", colors["accent_hover"])],
    )


def _configure_primary_button(style: ttk.Style, colors: dict, name: str, *, padding: tuple) -> None:
    style.configure(
        name,
        background=colors["accent"],
        foreground="#FFFFFF",
        bordercolor=colors["accent"],
        lightcolor=colors["accent"],
        darkcolor=colors["accent"],
        padding=padding,
        font=(FONT_UI, 9, "bold"),
    )
    style.map(
        name,
        background=[
            ("pressed", colors["accent_active"]),
            ("active", colors["accent_hover"]),
            ("disabled", "#A9C6D6"),
        ],
        bordercolor=[
            ("pressed", colors["accent_active"]),
            ("active", colors["accent_hover"]),
        ],
        foreground=[("disabled", "#EFF5F7")],
    )


def _configure_secondary_button(
    style: ttk.Style, colors: dict, name: str, *, padding: tuple
) -> None:
    style.configure(
        name,
        background=colors["paper"],
        foreground=colors["accent"],
        bordercolor=colors["rule"],
        lightcolor=colors["paper"],
        darkcolor=colors["paper"],
        padding=padding,
    )
    style.map(
        name,
        background=[
            ("pressed", colors["offset"]),
            ("active", colors["accent_wash"]),
            ("disabled", colors["page"]),
        ],
        foreground=[("disabled", colors["quiet"])],
        bordercolor=[("active", colors["accent"])],
    )
