#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视图层：仅负责窗口初始化与事件循环。"""

from __future__ import annotations

import tkinter as tk


class MainView:
    """主窗口视图。"""

    def __init__(self, title: str, geometry: str) -> None:
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry(geometry)

    def mount_legacy_gui(self, legacy_gui_class: type) -> None:
        """挂载旧版 GUI（过渡阶段）。"""
        legacy_gui_class(self.root)

    def run(self) -> None:
        """进入事件循环。"""
        self.root.mainloop()
