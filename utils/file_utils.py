#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件与配置工具函数（纯函数风格）。"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path

import yaml


def ensure_directory(path: str | Path) -> Path:
    """确保目录存在并返回 Path。"""
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def read_yaml(path: str | Path, default: dict[str, object] | None = None) -> dict[str, object]:
    """读取 YAML，失败时返回默认值副本。"""
    file_path = Path(path)
    fallback = copy.deepcopy(default) if default is not None else {}

    if not file_path.exists():
        return fallback

    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, dict):
            return data
        return fallback
    except Exception:
        return fallback


def write_yaml(path: str | Path, data: dict[str, object]) -> None:
    """原子写入 YAML（自动创建父目录）。"""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{file_path.name}.", suffix=".tmp", dir=file_path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, file_path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    finally:
        temp_path.unlink(missing_ok=True)


def write_json(path: str | Path, data: object) -> None:
    """Atomically persist a JSON document using the app's stable formatting."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{file_path.name}.", suffix=".tmp", dir=file_path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
            json.dump(data, file_obj, ensure_ascii=False, indent=2)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_path, file_path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    finally:
        temp_path.unlink(missing_ok=True)


def deep_merge_dict(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    """深度合并字典，override 优先。"""
    result: dict[str, object] = copy.deepcopy(base)

    for key, value in override.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(value, dict):
            result[key] = deep_merge_dict(base_value, value)
        else:
            result[key] = value

    return result
