#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制器层：连接配置、视图和现有业务实现。"""

from __future__ import annotations

from pathlib import Path

from core.config_manager import ConfigManager
from gui.main_view import MainView

try:
    from gui.pixiv_downloader_gui import PixivDownloaderGUI
except Exception:
    PixivDownloaderGUI = None


class MainController:
    """主控制器。"""

    def __init__(self, project_root: str | Path | None = None) -> None:
        self.project_root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
        self.config_manager = ConfigManager(self.project_root)
        self.config = self.config_manager.load().data

        app_cfg = self._dict_value(self.config.get("app"))
        title = str(app_cfg.get("title", "Pixiv 图片下载器"))
        geometry = str(app_cfg.get("geometry", "850x750"))

        self.view = MainView(title=title, geometry=geometry)

    def run(self) -> None:
        """启动应用。"""
        self._mount_main_gui()
        self.view.run()

    def _mount_main_gui(self) -> None:
        """挂载重构后的主 GUI。"""
        if PixivDownloaderGUI is None:
            raise RuntimeError("无法导入主 GUI，请检查 gui/pixiv_downloader_gui.py")
        self.view.mount_legacy_gui(PixivDownloaderGUI)

    @staticmethod
    def _dict_value(value: object) -> dict[str, object]:
        """将对象安全转换为字典。"""
        if isinstance(value, dict):
            return value
        return {}
