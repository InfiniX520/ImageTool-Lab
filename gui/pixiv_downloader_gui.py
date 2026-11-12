#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pixiv 图片下载器 GUI 入口。"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path

from core.app_paths import get_app_paths
from core.cookie_provider import CookieProvider
from core.record_manager import configure_record_storage, reconcile_record_projection
from gui.pixiv_downloader_config_mixin import PixivDownloaderConfigMixin
from gui.pixiv_downloader_layout_mixin import PixivDownloaderLayoutMixin
from gui.pixiv_downloader_runtime_mixin import PixivDownloaderRuntimeMixin
from gui.theme import enable_dpi_awareness
from gui.ui_fx import UiThrottler
from utils.logger import SessionLogQueue

try:
    from pixivpy3 import AppPixivAPI
except ImportError:
    AppPixivAPI = None

APP_PATHS = get_app_paths()
ROOT = APP_PATHS.project_root
CONFIG_FILE = APP_PATHS.legacy_config_file
DOWNLOADED_IDS_FILE = APP_PATHS.downloaded_ids_file


class PixivDownloaderGUI(
    PixivDownloaderConfigMixin,
    PixivDownloaderLayoutMixin,
    PixivDownloaderRuntimeMixin,
):
    """Pixiv 下载器图形界面。"""

    def __init__(
        self,
        root,
        *,
        title: str = "Pixiv 图片下载器",
        geometry: str = "1100x860",
        project_root: str | Path | None = None,
    ):
        self.root = root
        self.app_paths = get_app_paths(project_root)
        configure_record_storage(self.app_paths.project_root)
        reconcile_record_projection()
        # Defaults preserve the standalone entry point; the controller can
        # inject the same values from the typed YAML configuration.
        self.root.title(title)
        self.root.geometry(geometry)
        self.root.minsize(1000, 860)

        self.api = None
        self.auth_cookie = ""
        self.cookie_source = "none"
        self.cookie_source_path = None
        self.cookie_provider = CookieProvider(paths=self.app_paths)
        self.user_agent = ""
        self.running = False
        self.stop_event = threading.Event()
        self._download_thread = None
        self.log_queue = SessionLogQueue(self.app_paths.project_root / "runtime_logs")
        self._is_closing = False
        self._log_after_id = None
        # 高频 UI 更新（状态栏等）按键合并，防止回调洪峰拖垮主线程。
        self._ui_throttler = UiThrottler(self.root, interval_ms=110)

        self._build_ui()
        self._load_config()
        self.log_queue.put("[*] 应用已启动")
        self._log_after_id = self.root.after(100, self._process_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)


def main():
    if AppPixivAPI is None:
        print("请先安装 pixivpy3: pip install pixivpy3")
        return
    enable_dpi_awareness()
    root = tk.Tk()
    PixivDownloaderGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
