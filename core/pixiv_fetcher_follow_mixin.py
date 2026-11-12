#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pixiv 获取器关注用户批量逻辑。"""

from __future__ import annotations

import os
from pathlib import Path

import core.download_service as pixiv_utils
from core.app_paths import get_app_paths
from core.follow_crawl_service import (
    FollowCrawlOptions,
    FollowCrawlService,
    build_follow_config_key,
)
from core.follow_crawl_state import FollowCrawlStateRepository
from core.pixiv_api_policy import PixivApiRequestExecutor


class PixivFetcherFollowMixin:
    """关注画师批量遍历，基于 ``FollowCrawlService`` 的可续跑任务。

    Host contract（由 ``PixivFetcher`` 宿主及其他 mixin 提供）:
        - ``self.config`` — ``FetchConfig``，读取 ``crawl_state_path``/``follow_api_budget``/
          ``api_delay``/``follow_start_mode``/``follow_start_value``/
          ``follow_batch_artists``/``max_count``/``resume_enabled``
        - ``self.api`` — ``AppPixivAPI``，传入 ``FollowCrawlService`` 与 ``user_following``
        - ``self.log(msg)`` — 日志回调
        - ``self._should_stop()`` — 取消信号（来自 ``SearchMixin``）
        - ``self._wait_interruptible(seconds)`` — 可中断 sleep（来自宿主）
        - ``self._process_illust(...)`` — 单作品入队（来自 ``QueueMixin``）
        - ``self._active_api_request_executor`` — 宿主持有的可中断执行器句柄，
          运行期赋值、结束时置 ``None``
    """

    def _fetch_follow_all(self, download_queue, stats, stats_lock, update_status, downloaded_ids):
        """Traverse followed artists through a durable, resumable job."""

        configured_state_path = str(getattr(self.config, "crawl_state_path", "") or "").strip()
        state_path = (
            Path(configured_state_path).expanduser().resolve()
            if configured_state_path
            else get_app_paths().download_logs_dir / "follow_crawl_state.sqlite3"
        )
        repository = FollowCrawlStateRepository(state_path)
        request_executor = PixivApiRequestExecutor(
            request_budget=max(1, int(getattr(self.config, "follow_api_budget", 500) or 500)),
            min_interval=max(0.0, float(getattr(self.config, "api_delay", 0.3) or 0.0)),
            wait=self._wait_interruptible,
            should_stop=self._should_stop,
            log=self.log,
        )

        def process_illust(illust, fetched):
            return self._process_illust(
                illust,
                fetched,
                download_queue,
                stats,
                stats_lock,
                update_status,
                downloaded_ids,
            )

        def publish_progress(payload):
            with stats_lock:
                stats["follow_run_id"] = payload.get("run_id", "")
                stats["follow_total_artists"] = payload.get("total_artists", 0)
                stats["follow_artist_ordinal"] = payload.get("artist_ordinal", 0)
                stats["follow_artist_id"] = payload.get("artist_id", "")
                stats["follow_artist_name"] = payload.get("artist_name", "")
                stats["follow_completed_artists"] = payload.get("completed_artists", 0)
                stats["follow_api_requests"] = payload.get("api_requests", 0)
            update_status()

        options = FollowCrawlOptions(
            start_mode=str(getattr(self.config, "follow_start_mode", "resume") or "resume"),
            start_value=str(getattr(self.config, "follow_start_value", "") or ""),
            max_artists_per_batch=max(
                1,
                int(getattr(self.config, "follow_batch_artists", 30) or 30),
            ),
            max_queued_items=max(1, int(getattr(self.config, "max_count", 100) or 100)),
        )
        service = FollowCrawlService(
            api=self.api,
            repository=repository,
            request_executor=request_executor,
            options=options,
            process_illust=process_illust,
            should_stop=self._should_stop,
            log=self.log,
            progress_callback=publish_progress,
        )
        config_key = build_follow_config_key(
            self.config,
            resume_enabled=bool(getattr(self.config, "resume_enabled", True)),
        )

        self._active_api_request_executor = request_executor
        try:
            summary = service.run(
                account_id=str(getattr(self.api, "user_id", "") or ""),
                config_key=config_key,
            )
            with stats_lock:
                stats["follow_run_status"] = summary.status
                stats["follow_stop_reason"] = summary.stop_reason
                stats["follow_api_requests"] = summary.api_requests
                stats["follow_completed_artists"] = summary.completed_artists
                stats["follow_total_artists"] = summary.total_artists
            update_status()
            return summary.queued_items
        finally:
            self._active_api_request_executor = None
            repository.close()

    def _get_following_users(self):
        """获取关注用户列表"""
        user_ids = []
        try:
            if pixiv_utils.DEBUG_MODE:
                self.log(f"[D] 开始获取关注用户列表，用户ID: {self.api.user_id}")
            result = self.api.user_following(self.api.user_id)
            page = 1
            while result:
                if self._should_stop():
                    break
                if not hasattr(result, "user_previews") or not result.user_previews:
                    if pixiv_utils.DEBUG_MODE:
                        self.log(f"[D] 第 {page} 页无用户数据")
                    break
                for user_preview in result.user_previews:
                    if hasattr(user_preview, "user") and user_preview.user:
                        user_ids.append(str(user_preview.user.id))
                if pixiv_utils.DEBUG_MODE:
                    self.log(
                        f"[D] 第 {page} 页获取 {len(result.user_previews)} 个用户，累计 {len(user_ids)}"
                    )
                if result.next_url:
                    next_qs = self.api.parse_qs(result.next_url)
                    result = self.api.user_following(**next_qs)
                    page += 1
                    self._wait_interruptible(0.5)
                else:
                    break
        except Exception as e:
            self.log(f"[-] 获取关注列表失败: {e}")
            if pixiv_utils.DEBUG_MODE:
                import traceback

                self.log(f"[D] {traceback.format_exc()}")
        return user_ids

    def _save_skipped_users(self, skipped_users):
        """保存被跳过的用户到文件"""
        if not skipped_users:
            return

        skipped_file = os.path.join(self.config.save_dir, "skipped_users.txt")
        try:
            # 读取已有的跳过用户（避免重复）
            existing = set()
            if os.path.exists(skipped_file):
                with open(skipped_file, "r", encoding="utf-8") as f:
                    existing = set(line.strip() for line in f if line.strip())

            # 添加新的跳过用户
            new_skipped = [uid for uid in skipped_users if uid not in existing]
            if new_skipped:
                with open(skipped_file, "a", encoding="utf-8") as f:
                    for uid in new_skipped:
                        f.write(f"{uid}\n")
                self.log(f"[!] {len(new_skipped)} 个用户被跳过，已保存到 skipped_users.txt")
                self.log("[*] 提示: 稍后可以用「用户作品」模式输入这些ID重新下载")
        except Exception as e:
            self.log(f"[-] 保存跳过用户失败: {e}")
