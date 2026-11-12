#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置管理器：读取 default + user 配置并合并。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from utils.file_utils import deep_merge_dict, read_yaml, write_yaml


@dataclass
class AppConfig:
    """应用配置包装对象。"""

    data: dict[str, object]

    def get(self, key: str, default: object = None) -> object:
        """读取顶层键。"""
        return self.data.get(key, default)


class ConfigManager:
    """配置文件管理。"""

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root)
        self.default_config_path = self.project_root / "config" / "default_config.yaml"
        self.user_config_path = self.project_root / "config" / "config.yaml"

    def load(self) -> AppConfig:
        """加载并合并配置。"""
        default_data = read_yaml(self.default_config_path, default={})
        user_data = read_yaml(self.user_config_path, default={})
        merged = deep_merge_dict(default_data, user_data)

        # 若用户配置不存在，则写入一份默认用户配置，保持可见可改。
        if not self.user_config_path.exists():
            write_yaml(self.user_config_path, merged)

        return AppConfig(data=merged)

    def save(self, data: dict[str, object]) -> None:
        """保存用户配置。"""
        write_yaml(self.user_config_path, data)
