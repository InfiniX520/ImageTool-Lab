#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多数据源交集下载的抓取与下载执行逻辑。"""

from __future__ import annotations

import random
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import messagebox

from core.download_service import (
    APICallCounter,
    download_image,
    log_exception,
    log_message,
)
from core.http_client import close_current_http_session
from core.multi_filter_fetch import fetch_source
from core.pixiv_fetcher import FetchConfig, PixivFetcher
from core.record_manager import clear_record_cache, flush_pending_markdown


class MultiFilterRuntimeMixin:
    """多来源交集筛选的执行逻辑：拉取 → 交集 → 附加筛选 → 下载。

    从勾选来源拉取作品 ID，计算交集，按收藏阈值/AI 过滤，最后多线程下载。
    通过 ``_post_to_ui``/``_post_status`` 跨线程更新界面。

    Host contract（由 ``MultiFilterGUI`` 宿主提供）:
        - ``self.root`` / ``self.log_queue`` / ``self.stop_event``
        - ``self.api`` — ``AppPixivAPI``，调用 ``illust_detail``
        - ``self.config`` — dict，读取 ``threads``/``delay``/``api_delay``/
          ``original``/``multi_page``/``folder_by_user``/``name_template``/
          ``auth_cookie``/``user_agent``
        - 控件（由 ``MultiFilterUIMixin`` 创建）：``count_entry``/``bookmark_entry``/
          ``skip_ai_var``/``rank_combo``/``search_entry``/``user_entry``/``follow_var``/
          ``ranking_var``/``search_var``/``user_var``/``dir_entry``
        - 属性：``self.api_counter`` — 由本 mixin 创建的 ``APICallCounter``
        - 方法：``finish()`` — 任务结束回调（宿主提供，恢复按钮状态等）
    """

    def _post_to_ui(self, callback) -> None:
        """Schedule a callback only while the dialog is alive."""
        if getattr(self, "_is_closing", False):
            return
        try:
            self.root.after(0, callback)
        except (tk.TclError, RuntimeError):
            pass

    def _post_status(self, text: str) -> None:
        """高频状态栏更新走节流合并，避免逐项回调洪峰拖垮 UI 线程。"""
        label = getattr(self, "status_label", None)
        if label is None:
            return

        def apply(label=label, text=text):
            try:
                label.config(text=text)
            except tk.TclError:
                pass

        throttler = getattr(self, "_ui_throttler", None)
        if throttler is not None:
            throttler.call("status", apply)
        else:
            self._post_to_ui(apply)

    def _run(self, sources, save_dir, filter_snapshot=None):
        """执行交集下载"""
        try:
            if filter_snapshot is None:
                # Compatibility for direct integrations; normal UI launches
                # pass a main-thread snapshot.
                filter_snapshot = {
                    "max_count": self.count_entry.get(),
                    "min_bookmarks": self.bookmark_entry.get(),
                    "skip_ai": bool(self.skip_ai_var.get()),
                    "rank_mode": self.rank_combo.get(),
                    "keyword": self.search_entry.get().strip(),
                    "user_ids": self.user_entry.get(),
                }
            max_count = int(filter_snapshot.get("max_count") or 200)
            min_bookmarks = int(filter_snapshot.get("min_bookmarks") or 0)

            log_message(f"数据来源: {', '.join(sources)}", "info", self.log_queue)
            log_message(f"每个来源获取 {max_count} 个作品", "info", self.log_queue)

            # API 请求计数器（防封）
            self.api_counter = APICallCounter(
                log_callback=lambda msg: self.log_queue.put(msg),
                stop_event=self.stop_event,
            )

            # 从各个来源获取作品ID集合
            source_sets = {}
            source_illusts = {}

            for source in sources:
                if self.stop_event.is_set():
                    break

                self._post_status(f"获取: {source}")
                log_message(f"--- 获取 {source} ---", "info", self.log_queue)

                illusts = self._fetch_source(source, max_count, filter_snapshot)
                ids = set(str(i.id) for i in illusts)
                source_sets[source] = ids

                for i in illusts:
                    source_illusts[str(i.id)] = i

                log_message(f"{source}: 获取到 {len(ids)} 个作品", "success", self.log_queue)

            if self.stop_event.is_set():
                self.finish()
                return

            # 计算交集
            log_message("--- 计算交集 ---", "info", self.log_queue)
            intersection = None
            for source, ids in source_sets.items():
                if intersection is None:
                    intersection = ids
                else:
                    intersection = intersection & ids
                log_message(f"与 {source} 交集后: {len(intersection)} 个", "info", self.log_queue)

            if not intersection:
                log_message("交集为空，没有同时出现在所有来源中的作品", "warning", self.log_queue)
                self._post_to_ui(
                    lambda: messagebox.showinfo(
                        "结果", "交集为空\n没有同时出现在所有选中来源中的作品"
                    )
                )
                self.finish()
                return

            log_message(f"最终交集: {len(intersection)} 个作品", "success", self.log_queue)

            # 应用附加筛选
            filtered_ids = []
            for illust_id in intersection:
                illust = source_illusts.get(illust_id)
                if not illust:
                    continue

                if min_bookmarks > 0:
                    bookmarks = getattr(illust, "total_bookmarks", 0) or 0
                    if bookmarks < min_bookmarks:
                        continue

                if filter_snapshot.get("skip_ai"):
                    if getattr(illust, "illust_ai_type", 0) == 2:
                        continue

                filtered_ids.append(illust_id)

            if len(filtered_ids) < len(intersection):
                log_message(f"附加筛选后: {len(filtered_ids)} 个作品", "info", self.log_queue)

            if not filtered_ids:
                log_message("筛选后没有符合条件的作品", "warning", self.log_queue)
                self.finish()
                return

            # 下载
            log_message(f"--- 开始下载 {len(filtered_ids)} 个作品 ---", "info", self.log_queue)
            self._download_all([source_illusts[i] for i in filtered_ids], save_dir)

            log_message("完成!", "success", self.log_queue)

        except Exception as e:
            log_exception(e, "执行出错", self.log_queue)
        finally:
            close_current_http_session()
            flush_pending_markdown()
            clear_record_cache()
            self.finish()

    def _fetch_source(self, source, max_count, filter_snapshot=None):
        """Compatibility facade over the isolated source strategies."""
        return fetch_source(self, source, max_count, snapshot=filter_snapshot)

    def _download_all(self, illusts, save_dir):
        """复用主下载语义，先串行补详情，再并发下载作品。"""
        total = len(illusts)
        downloaded = 0
        failed = 0

        try:
            max_workers = min(32, max(1, int(self.config.get("threads") or 3)))
            delay = max(0.0, float(self.config.get("delay") or 0.5))
            api_delay = max(0.0, float(self.config.get("api_delay") or 0.3))
        except (TypeError, ValueError):
            max_workers, delay, api_delay = 3, 0.5, 0.3

        fetch_config = FetchConfig(
            save_dir=save_dir,
            max_count=total,
            api_delay=api_delay,
            original=bool(self.config.get("original", True)),
            multi_page=bool(self.config.get("multi_page", True)),
            folder_by_user=bool(self.config.get("folder_by_user", False)),
            name_template=str(self.config.get("name_template") or "{id}_{title}_p{page}"),
            mode="multi_filter",
            auth_cookie=str(self.config.get("auth_cookie") or ""),
            user_agent=str(self.config.get("user_agent") or ""),
        )
        path_builder = PixivFetcher(
            self.api,
            fetch_config,
            log_callback=lambda message: self.log_queue.put(message),
            stop_event=self.stop_event,
        )

        prepared = []
        for index, illust in enumerate(illusts, 1):
            if self.stop_event.is_set():
                break

            self._post_status(f"准备作品 {index}/{total}")
            try:
                detail = self.api.illust_detail(illust.id)
                if detail and detail.illust:
                    illust = detail.illust
                if hasattr(self, "api_counter"):
                    self.api_counter.increment()
            except Exception as exc:
                log_message(
                    f"获取作品 {illust.id} 详情失败，将使用来源数据: {exc}",
                    "warning",
                    self.log_queue,
                )

            urls = path_builder._get_image_urls(illust)
            if not urls:
                failed += 1
                log_message(f"作品 {illust.id} 没有可用图片地址", "warning", self.log_queue)
                continue
            url, page_index = urls[0]
            original_urls, known_page_count = path_builder._get_original_download_metadata(illust)
            save_path = path_builder._get_save_path(illust, url)
            is_ai = getattr(illust, "illust_ai_type", 0) == 2
            bookmarks = getattr(illust, "total_bookmarks", 0) or 0
            create_date = getattr(illust, "create_date", None)
            artist = getattr(illust, "user", None)
            prepared.append(
                (
                    url,
                    save_path,
                    illust,
                    page_index,
                    is_ai,
                    bookmarks,
                    create_date,
                    artist,
                    original_urls,
                    known_page_count,
                )
            )
            self.stop_event.wait(api_delay + random.uniform(0, 0.5))

        counts_lock = threading.Lock()

        def download_one(task):
            (
                url,
                save_path,
                illust,
                page_index,
                is_ai,
                bookmarks,
                create_date,
                artist,
                original_urls,
                known_page_count,
            ) = task
            success = download_image(
                url,
                save_path,
                self.log_queue,
                success_log_dir=str(Path(save_path).parent),
                illust_id=illust.id,
                is_ai=is_ai,
                bookmarks=bookmarks,
                create_date=create_date,
                page_index=page_index,
                title=getattr(illust, "title", "") or "",
                artist_id=str(getattr(artist, "id", "") or ""),
                artist_name=getattr(artist, "name", "") or "",
                download_root=save_dir,
                folder_by_user=fetch_config.folder_by_user,
                resume_enabled=bool(self.config.get("skip_downloaded", True)),
                original_enabled=fetch_config.original,
                multi_page=fetch_config.multi_page,
                auth_cookie=fetch_config.auth_cookie,
                user_agent=fetch_config.user_agent,
                stop_event=self.stop_event,
                original_urls=original_urls,
                known_page_count=known_page_count,
            )
            self.stop_event.wait(delay + random.uniform(0, 1))
            return success

        with ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="pidl-filter"
        ) as executor:
            futures = [executor.submit(download_one, task) for task in prepared]
            for completed, future in enumerate(as_completed(futures), 1):
                if self.stop_event.is_set():
                    for pending in futures:
                        pending.cancel()
                    break
                try:
                    success = bool(future.result())
                except Exception as exc:
                    log_exception(exc, "交集下载任务失败", self.log_queue)
                    success = False
                with counts_lock:
                    if success:
                        downloaded += 1
                    else:
                        failed += 1
                    ok, bad = downloaded, failed
                self._post_status(f"下载 {completed}/{len(prepared)} · 成功 {ok} · 失败 {bad}")

        log_message(
            f"下载完成: {downloaded}/{total}" + (f", 失败: {failed}" if failed > 0 else ""),
            "success",
            self.log_queue,
        )
