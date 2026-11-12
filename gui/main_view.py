#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视图层：仅负责窗口初始化与事件循环。"""

from __future__ import annotations

import tkinter as tk

from gui.theme import enable_dpi_awareness


class MainView:
    """主窗口视图。"""

    def __init__(self, title: str, geometry: str) -> None:
        # 必须在创建第一个 Tk 窗口之前启用 DPI 感知，保证高分屏清晰。
        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry(geometry)

    def mount_main_gui(self, gui_class: type, **kwargs: object):
        """Mount the application view and return its controller object."""
        return gui_class(self.root, **kwargs)

    # Public compatibility alias retained for integrations from the scaffold
    # release.
    def mount_legacy_gui(self, legacy_gui_class: type, **kwargs: object):
        return self.mount_main_gui(legacy_gui_class, **kwargs)

    def run(self) -> None:
        """进入事件循环。"""
        self.root.mainloop()
