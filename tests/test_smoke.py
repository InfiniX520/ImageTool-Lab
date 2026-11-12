#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重构骨架冒烟测试。"""

from __future__ import annotations

from pathlib import Path

from core.config_manager import ConfigManager


def test_config_manager_loads() -> None:
    root = Path(__file__).resolve().parents[1]
    manager = ConfigManager(root)
    config = manager.load().data
    assert "app" in config
    assert isinstance(config["app"], dict)
