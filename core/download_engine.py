#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载引擎封装（过渡版，复用旧下载实现）。"""

from __future__ import annotations

from dataclasses import dataclass

from core.pixiv_utils import download_image


@dataclass
class DownloadRequest:
    """单个下载请求。"""

    url: str
    save_path: str
    illust_id: int | str
    page_index: int
    title: str = ""
    artist_id: str = ""
    artist_name: str = ""
    is_ai: bool | None = None
    bookmarks: int | None = None
    create_date: str | None = None


class DownloadEngine:
    """下载引擎（同步包装）。"""

    def download(self, request: DownloadRequest, **kwargs: object) -> bool:
        """执行下载请求。"""
        return download_image(
            url=request.url,
            save_path=request.save_path,
            illust_id=request.illust_id,
            page_index=request.page_index,
            title=request.title,
            artist_id=request.artist_id,
            artist_name=request.artist_name,
            is_ai=request.is_ai,
            bookmarks=request.bookmarks,
            create_date=request.create_date,
            **kwargs,
        )
