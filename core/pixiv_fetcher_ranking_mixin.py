#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pixiv 获取器排行榜时间范围逻辑。"""

from __future__ import annotations

import random


class PixivFetcherRankingMixin:
    """排行榜按日/周/月步长批量获取与首屏收藏预览。

    Host contract（由 ``PixivFetcher`` 宿主及其他 mixin 提供）:
        - ``self.config`` — ``FetchConfig``，读取 ``rank_mode``/``min_bookmarks``
        - ``self.api`` — ``AppPixivAPI``，调用 ``illust_ranking``
        - ``self.log(msg)`` — 日志回调
        - ``self._search_runtime`` — dict，首屏预览读取排序策略
        - ``self._should_stop()`` — 取消信号（来自 ``SearchMixin``）
        - ``self._fetch_and_queue(...)`` — 生产者入队（来自 ``QueueMixin``）
        - ``self._wait_interruptible(seconds)`` — 可中断 sleep（来自宿主）
    """

    def _log_search_preview(self, result):
        """记录搜索首屏收藏数预览，便于判断排序是否生效。"""
        try:
            if not result or not hasattr(result, "illusts") or not result.illusts:
                self.log("[!] 搜索首屏为空")
                return
            sample = []
            values = []
            for illust in result.illusts[:5]:
                value = getattr(illust, "total_bookmarks", 0) or 0
                values.append(value)
                sample.append(str(value))
            self.log(f"[*] 首屏收藏预览(top5): {', '.join(sample)}")

            runtime = self._search_runtime or {}
            min_bookmarks = int(self.config.min_bookmarks or 0)
            if runtime.get("sort") == "popular_desc" and min_bookmarks > 0:
                top_max = max(values) if values else 0
                if top_max < min_bookmarks:
                    self.log(
                        "[!] 热度首屏仍低于你的收藏阈值，可能是账号热门排序权限受限或关键词高热作品本就很少"
                    )
                    self.log("[*] 建议：改用“基于关注推荐”或“基于作品相似推荐”模式获取高质量样本")
        except Exception:
            pass

    def _get_ranking_step(self):
        """根据排行榜类型返回时间步长"""
        mode = self.config.rank_mode
        if mode in ("month", "month_manga"):
            return "month"
        elif mode.startswith("week"):
            return "week"
        else:
            return "day"

    def _fetch_ranking_range(
        self, date_start, date_end, download_queue, stats, stats_lock, update_status, downloaded_ids
    ):
        """获取日期范围内的排行榜"""
        from calendar import monthrange
        from datetime import datetime, timedelta

        try:
            start = datetime.strptime(date_start, "%Y-%m-%d")
            end = datetime.strptime(date_end, "%Y-%m-%d")
        except ValueError:
            self.log("[-] 日期格式错误，请使用 YYYY-MM-DD 格式")
            return 0

        if start > end:
            start, end = end, start  # 自动交换

        step = self._get_ranking_step()

        # 计算总数
        if step == "day":
            total_count = (end - start).days + 1
            step_name = "天"
        elif step == "week":
            total_count = ((end - start).days // 7) + 1
            step_name = "周"
        else:  # month
            total_count = (end.year - start.year) * 12 + (end.month - start.month) + 1
            step_name = "月"

        self.log(f"[*] 批量获取排行榜: {self.config.rank_mode}")
        self.log(f"[*] 日期范围: {date_start} ~ {date_end} (共 {total_count} {step_name})")

        total_queued = 0
        current = start
        count_idx = 0

        while current <= end:
            if self._should_stop():
                break

            count_idx += 1
            date_str = current.strftime("%Y-%m-%d")
            self.log(f"[*] [{count_idx}/{total_count}] 获取 {date_str} 的排行榜")

            count = self._fetch_and_queue(
                lambda d=date_str: self.api.illust_ranking(mode=self.config.rank_mode, date=d),
                lambda r, qs: self.api.illust_ranking(**qs),
                download_queue,
                stats,
                stats_lock,
                update_status,
                downloaded_ids,
            )

            total_queued += count
            self.log(f"[*] {date_str} 添加了 {count} 个到队列，累计 {total_queued}")

            # 根据步长计算下一个日期
            if step == "day":
                current += timedelta(days=1)
            elif step == "week":
                current += timedelta(weeks=1)
            else:  # month
                # 跳到下个月同一天（或月末）
                if current.month == 12:
                    next_year = current.year + 1
                    next_month = 1
                else:
                    next_year = current.year
                    next_month = current.month + 1
                # 处理月末情况（如1月31日 -> 2月28日）
                max_day = monthrange(next_year, next_month)[1]
                next_day = min(current.day, max_day)
                current = current.replace(year=next_year, month=next_month, day=next_day)

            # 每次之间增加延迟
            if current <= end and not self._should_stop():
                delay = random.uniform(3, 6)
                self.log(f"[*] 等待 {delay:.1f} 秒后获取下一个...")
                self._wait_interruptible(delay)

        return total_queued
