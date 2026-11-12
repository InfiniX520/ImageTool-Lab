#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pixiv 获取器文件名与保存路径逻辑。"""

from __future__ import annotations

import os
import re

from core.download_service import sanitize_filename


class PixivFetcherPathMixin:
    """原图 URL 提取、文件名模板渲染与保存路径计算。

    Host contract（由 ``PixivFetcher`` 宿主提供）:
        - ``self.config`` — ``FetchConfig`` 实例，读取 ``original``/``name_template``/
          ``mode``/``folder_by_user``/``save_dir`` 等字段
        - ``self.log(msg)`` — 日志输出回调（仅 ``_get_image_urls`` 在预览兜底时调用）
    """

    def _get_original_download_metadata(self, illust) -> tuple[tuple[str, ...], int]:
        """Return exact App-API original URLs plus the declared page count.

        Pixiv's detail response already contains every ``meta_pages`` URL for
        normal multi-page works. Carrying that metadata into the download task
        avoids a second web Ajax request for each artwork.
        """
        try:
            page_count = max(0, int(getattr(illust, "page_count", 0) or 0))
        except (TypeError, ValueError):
            page_count = 0

        meta_pages = list(getattr(illust, "meta_pages", None) or [])
        if meta_pages:
            page_urls: list[str] = []
            for page in meta_pages:
                image_urls = getattr(page, "image_urls", None)
                original = getattr(image_urls, "original", None) if image_urls else None
                if not original:
                    page_urls = []
                    break
                page_urls.append(str(original))
            if page_urls:
                return tuple(page_urls), page_count or len(page_urls)

        single_page = getattr(illust, "meta_single_page", None) or {}
        if isinstance(single_page, dict):
            original = single_page.get("original_image_url")
        else:
            original = getattr(single_page, "original_image_url", None)
        if original:
            return (str(original),), page_count or 1

        image_urls = getattr(illust, "image_urls", None)
        original = getattr(image_urls, "original", None) if image_urls else None
        if original:
            return (str(original),), page_count or 1
        return (), page_count

    def _get_image_urls(self, illust):
        """获取作品的图片URL列表"""
        urls = []

        if self.config.original:
            # Queue exactly one artwork task.  The download service expands
            # Ajax ``pageCount`` recursively, preserving the legacy stats,
            # retry and per-artwork queue semantics.
            original_urls, _page_count = self._get_original_download_metadata(illust)
            if original_urls:
                urls.append((original_urls[0], 0))

            # 兜底
            if not urls and hasattr(illust, "image_urls"):
                if hasattr(illust.image_urls, "original") and illust.image_urls.original:
                    urls.append((illust.image_urls.original, 0))
                elif hasattr(illust.image_urls, "large") and illust.image_urls.large:
                    urls.append((illust.image_urls.large, 0))
                    self.log(
                        f"[*] 作品 {illust.id} 的列表接口仅提供预览地址；"
                        "下载层将通过 Ajax 强制解析 img-original，绝不直接保存该预览。"
                    )
        else:
            if (
                hasattr(illust, "image_urls")
                and hasattr(illust.image_urls, "large")
                and illust.image_urls.large
            ):
                urls.append((illust.image_urls.large, 0))

        return urls

    def _make_filename(self, illust, url):
        """根据模板生成文件名"""
        ext = url.split(".")[-1].split("?")[0]

        # 提取页码
        match = re.search(r"_p(\d+)", url)
        page = match.group(1) if match else "0"

        # 提取日期
        date_str = ""
        create_date = getattr(illust, "create_date", None)
        if create_date:
            date_str = create_date[:10].replace("-", "")
        else:
            date_match = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
            if date_match:
                date_str = date_match.group(1) + date_match.group(2) + date_match.group(3)

        # 获取画师信息
        user_name = ""
        user_id = ""
        if hasattr(illust, "user") and illust.user:
            user_name = sanitize_filename(getattr(illust.user, "name", "") or "")[:30]
            user_id = str(getattr(illust.user, "id", "") or "")

        title = sanitize_filename(getattr(illust, "title", "") or "")[:50]

        # 应用模板
        template = self.config.name_template or "{id}_{title}_p{page}"
        filename = template.format(
            id=illust.id, title=title, user=user_name, user_id=user_id, page=page, date=date_str
        )

        filename = sanitize_filename(filename)
        if not filename:
            filename = str(illust.id)

        return f"{filename}.{ext}"

    def _get_save_path(self, illust, url):
        """获取保存路径"""
        filename = self._make_filename(illust, url)

        # 排行榜模式：按年/月分文件夹
        if self.config.mode == "ranking":
            # 获取作品发布日期
            create_date = getattr(illust, "create_date", None)
            if create_date:
                # 格式: 2024-12-30T12:00:00+09:00
                year = create_date[:4]
                month = create_date[5:7]
            else:
                # 从URL提取日期作为备选
                import re

                date_match = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
                if date_match:
                    year = date_match.group(1)
                    month = date_match.group(2)
                else:
                    # 使用当前日期
                    import datetime

                    now = datetime.datetime.now()
                    year = str(now.year)
                    month = f"{now.month:02d}"

            # 构建路径
            if self.config.folder_by_user and hasattr(illust, "user") and illust.user:
                # 年/月/画师/
                user_id = getattr(illust.user, "id", "") or ""
                user_name = getattr(illust.user, "name", "") or ""
                user_folder = sanitize_filename(f"{user_id}_{user_name}")[:50]
                if not user_folder:
                    user_folder = str(user_id) if user_id else "unknown"
                folder_path = os.path.join(self.config.save_dir, year, month, user_folder)
            else:
                # 年/月/
                folder_path = os.path.join(self.config.save_dir, year, month)

            return os.path.join(folder_path, filename)

        # 其他模式：原有逻辑
        if self.config.folder_by_user and hasattr(illust, "user") and illust.user:
            user_id = getattr(illust.user, "id", "") or ""
            user_name = getattr(illust.user, "name", "") or ""
            user_folder = sanitize_filename(f"{user_id}_{user_name}")[:50]
            if not user_folder:
                user_folder = str(user_id) if user_id else "unknown"
            folder_path = os.path.join(self.config.save_dir, user_folder)
            return os.path.join(folder_path, filename)
        else:
            return os.path.join(self.config.save_dir, filename)
