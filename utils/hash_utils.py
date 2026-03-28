#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""哈希工具函数。"""

from __future__ import annotations

import hashlib
from pathlib import Path


def file_sha256(file_path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """计算文件 SHA-256。"""
    hasher = hashlib.sha256()
    path = Path(file_path)

    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)

    return hasher.hexdigest()
