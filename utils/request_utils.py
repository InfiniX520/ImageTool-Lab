#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""请求相关工具函数。"""

from __future__ import annotations

import random
import time


def build_pixiv_headers(
    referer: str,
    user_agent: str,
    cookie: str = "",
    accept: str = "application/json",
) -> dict[str, str]:
    """构建 Pixiv 请求头。"""
    headers: dict[str, str] = {
        "Referer": referer,
        "User-Agent": user_agent,
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def jitter_sleep(base_seconds: float, jitter_seconds: float) -> None:
    """带随机抖动的休眠。"""
    delay = max(0.0, base_seconds) + random.uniform(0.0, max(0.0, jitter_seconds))
    time.sleep(delay)
