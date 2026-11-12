#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pixiv 多数据源交集下载器 GUI 入口。"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path

from core.app_paths import get_app_paths
from gui.pixiv_multi_filter_runtime_mixin import MultiFilterRuntimeMixin
from gui.pixiv_multi_filter_ui_mixin import MultiFilterUIMixin
from gui.theme import enable_dpi_awareness
from gui.ui_fx import UiThrottler
from utils.logger import SessionLogQueue


class MultiFilterDialog(MultiFilterUIMixin, MultiFilterRuntimeMixin):
    """多数据源交集下载对话框。"""

    def __init__(self, parent=None, api=None, config=None):
        self.api = api
        self.config = config or {}

        # 创建窗口（进程级 DPI 感知幂等，独立运行时同样生效）
        enable_dpi_awareness()
        if parent:
            self.root = tk.Toplevel(parent)
        else:
            self.root = tk.Tk()

        self.root.title("Pixiv 多数据源交集下载")
        self.root.geometry("800x720")
        self.root.minsize(720, 640)

        # 状态变量
        self.running = False
        self.stop_event = threading.Event()
        project_root = Path(self.config.get("project_root") or get_app_paths().project_root)
        self.log_queue = SessionLogQueue(project_root / "runtime_logs")
        self._is_closing = False
        self._log_after_id = None
        self._ui_throttler = UiThrottler(self.root, interval_ms=110)

        self._build_ui()
        self._log_after_id = self.root.after(100, self.process_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)


def open_multi_filter(parent=None, api=None, config=None):
    """
    从主程序调用此函数打开多条件筛选窗口

    Args:
        parent: 父窗口
        api: 已登录的 Pixiv API 对象
        config: 配置字典

    Returns:
        MultiFilterDialog 实例
    """
    dialog = MultiFilterDialog(parent, api, config)
    return dialog
