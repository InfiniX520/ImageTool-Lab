#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pixiv API 客户端封装（过渡版）。"""

from __future__ import annotations

from dataclasses import dataclass

try:
    from pixivpy3 import AppPixivAPI
except Exception:
    AppPixivAPI = None


@dataclass
class LoginResult:
    """登录结果。"""

    ok: bool
    message: str


class PixivApiClient:
    """对 AppPixivAPI 的轻量封装。"""

    def __init__(self) -> None:
        self._api = None

    @property
    def api(self):
        """返回底层 API 对象。"""
        return self._api

    def login(self, refresh_token: str) -> LoginResult:
        """使用 refresh token 登录。"""
        if AppPixivAPI is None:
            return LoginResult(False, "未安装 pixivpy3")
        if not refresh_token:
            return LoginResult(False, "refresh token 为空")

        try:
            api = AppPixivAPI()
            api.auth(refresh_token=refresh_token)
            self._api = api
            return LoginResult(True, f"登录成功: user_id={getattr(api, 'user_id', '')}")
        except Exception as e:
            return LoginResult(False, f"登录失败: {e}")

    def extract_cookie_header(self) -> str:
        """从登录会话中提取 Cookie 头字符串。"""
        if self._api is None:
            return ""

        try:
            session = getattr(self._api, "requests", None) or getattr(self._api, "_requests", None)
            if not session or not hasattr(session, "cookies"):
                return ""

            pairs: list[str] = []
            for c in session.cookies:
                name = getattr(c, "name", "")
                value = getattr(c, "value", "")
                if name and value:
                    pairs.append(f"{name}={value}")
            return "; ".join(pairs)
        except Exception:
            return ""
