#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Config manager tests."""

from __future__ import annotations

from pathlib import Path

import yaml

from core.config_manager import ConfigManager


def _write_yaml(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def test_config_manager_merges_default_and_user(tmp_path: Path) -> None:
    default_path = tmp_path / "config" / "default_config.yaml"
    user_path = tmp_path / "config" / "config.yaml"

    _write_yaml(
        default_path,
        {
            "app": {"title": "Default", "geometry": "850x750"},
            "logging": {"level": "INFO", "keep_days": 30},
        },
    )
    _write_yaml(
        user_path,
        {
            "app": {"title": "User"},
            "logging": {"level": "DEBUG"},
        },
    )

    config = ConfigManager(tmp_path).load().data

    assert config["app"]["title"] == "User"
    assert config["app"]["geometry"] == "850x750"
    assert config["logging"]["level"] == "DEBUG"
    assert config["logging"]["keep_days"] == 30


def test_config_manager_bootstraps_user_config_when_missing(tmp_path: Path) -> None:
    default_path = tmp_path / "config" / "default_config.yaml"
    user_path = tmp_path / "config" / "config.yaml"

    _write_yaml(default_path, {"app": {"title": "Default"}, "runtime": {"locale": "zh-CN"}})

    config = ConfigManager(tmp_path).load().data

    assert user_path.exists()
    with user_path.open("r", encoding="utf-8") as f:
        saved_user = yaml.safe_load(f) or {}

    assert saved_user == config