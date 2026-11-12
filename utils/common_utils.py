#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用辅助函数模块：集成哈希计算、请求头构建以及休眠机制等基础功能。"""

from __future__ import annotations

import hashlib
import random
import time
from pathlib import Path


def calculate_file_sha256(file_path: str | Path, chunk_size: int = 1048576) -> str:
    """
    计算指定文件的 SHA-256 哈希值。

    Args:
        file_path (str | Path): 要计算的文件路径。
        chunk_size (int): 每次读取的块大小（默认 1MB）。

    Returns:
        str: 文件的 SHA-256 十六进制哈希字符串。
    """
    hasher = hashlib.sha256()
    path = Path(file_path)

    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)

    return hasher.hexdigest()


def build_pixiv_headers(
    referer: str,
    user_agent: str,
    cookie: str = "",
    accept: str = "application/json",
) -> dict[str, str]:
    """
    构建用于 Pixiv 请求的标准 HTTP 请求头字典。

    Args:
        referer (str): 来源页面的 URL。
        user_agent (str): 浏览器 User-Agent 标识。
        cookie (str): 会话相关的 Cookie 字符串（可选）。
        accept (str): Accept 头部内容（默认为纯 JSON 请求）。

    Returns:
        dict[str, str]: 包含必备字段的字典。
    """
    headers: dict[str, str] = {
        "Referer": referer,
        "User-Agent": user_agent,
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def sleep_with_jitter(base_seconds: float, jitter_seconds: float) -> None:
    """
    执行带有随机抖动的休眠操作，以模拟自然请求频率。

    Args:
        base_seconds (float): 基础休眠时间（秒）。
        jitter_seconds (float): 附加的随机超时上限（秒）。
    """
    delay = max(0.0, base_seconds) + random.uniform(0.0, max(0.0, jitter_seconds))
    time.sleep(delay)
