#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared fetch → filter → queue pipeline for Pixiv modes."""

from __future__ import annotations

import os
import queue
import random
import time
from dataclasses import dataclass

import core.download_service as pixiv_utils
from core.models import DownloadTask
from core.pixiv_api_policy import FollowCrawlControl


@dataclass(frozen=True, slots=True)
class _IllustOutcome:
    fetched: int = 0
    queued: int = 0
    skipped: int = 0
    page_index: int | None = None


class PixivFetcherQueueMixin:
    """Cancellation-aware, bounded producer pipeline.

    负责 ``fetch → filter → enqueue`` 主循环：分页拉取、收藏/AI 过滤、
    已下载去重、单作品入队，并提供空数据重试与可中断 sleep。

    Host contract（由 ``PixivFetcher`` 宿主及其他 mixin 提供）:
        - ``self.config`` — ``FetchConfig``，读取 ``max_count``/``api_delay``/
          ``original``/``multi_page``/``save_dir``/``folder_by_user``/``skip_ai``/
          ``min_bookmarks``/``mode`` 等字段
        - ``self.api`` — ``AppPixivAPI``，调用 ``parse_qs``/``illust_detail``
        - ``self.log(msg)`` — 日志回调
        - ``self.stop_event`` — ``threading.Event``，``_api_sleep`` 与重试等待读取
        - ``self.api_counter`` — ``APICallCounter``，分页/详情请求后 ``increment()``
        - ``self._should_stop()`` — 取消信号（来自 ``SearchMixin``）
        - ``self._wait_interruptible(seconds)`` — 可中断 sleep（来自宿主）
        - ``self._get_image_urls(illust)`` / ``self._get_original_download_metadata(illust)``
          / ``self._get_save_path(illust, url)`` — 路径计算（来自 ``PathMixin``）
    """

    def _fetch_and_queue(
        self,
        first_fetch,
        next_fetch,
        download_queue,
        stats,
        stats_lock,
        update_status,
        downloaded_ids,
        max_pages=None,
    ):
        queued_count = 0
        skipped_count = 0

        if pixiv_utils.DEBUG_MODE:
            self.log(f"[D] 开始获取，最大数量: {self.config.max_count}")

        result = self._load_initial_result(first_fetch)
        if result is None:
            return 0

        if pixiv_utils.DEBUG_MODE:
            self.log(f"[D] 首次请求返回 {len(result.illusts)} 个作品")

        fetched = 0
        # This counter represents API pagination, not the image page index
        # embedded in an individual artwork URL.  Keeping the two concepts
        # separate is important for bounded probe modes: a multi-page artwork
        # must not consume the API-page budget by itself.
        api_page_index = 0
        while self._has_illusts(result) and fetched < self.config.max_count:
            if self._should_stop():
                break
            api_page_index += 1
            page_processed = 0

            for illust in result.illusts:
                if fetched >= self.config.max_count or self._should_stop():
                    break
                page_processed += 1
                outcome = self._process_illust(
                    illust,
                    fetched,
                    download_queue,
                    stats,
                    stats_lock,
                    update_status,
                    downloaded_ids,
                )
                fetched += outcome.fetched
                queued_count += outcome.queued
                skipped_count += outcome.skipped

            if pixiv_utils.DEBUG_MODE and page_processed > 0 and queued_count == 0:
                self.log(f"[D] 本页 {page_processed} 个作品全部被跳过")

            if not result.next_url or fetched >= self.config.max_count:
                if not result.next_url and (pixiv_utils.DEBUG_MODE or skipped_count > 0):
                    self.log(f"[!] API 分页结束，已获取 {fetched} 条，已跳过 {skipped_count} 条")
                break
            if max_pages is not None and api_page_index >= max_pages:
                if pixiv_utils.DEBUG_MODE:
                    self.log(f"[D] 已达到页探测上限: {max_pages}")
                break

            next_qs = self.api.parse_qs(result.next_url)
            if pixiv_utils.DEBUG_MODE:
                self.log(f"[D] 获取下一页，当前已获取 {fetched} 个，已跳过 {skipped_count} 个")
            try:
                result = next_fetch(result, next_qs)
                self.api_counter.increment()
            except Exception as exc:
                self.log(f"[-] 获取下一页失败: {exc}")
                self._log_debug_exception()
                break
            if not self._has_illusts(result):
                if pixiv_utils.DEBUG_MODE:
                    self.log(f"[D] API 返回空数据，已获取 {fetched} 条，已跳过 {skipped_count} 条")
                break
            if pixiv_utils.DEBUG_MODE:
                self.log(f"[D] 下一页返回 {len(result.illusts)} 个作品")
            self._api_sleep()

        if pixiv_utils.DEBUG_MODE:
            self.log(f"[D] 获取完成，共添加 {queued_count} 个到队列，跳过 {skipped_count} 个")
        return queued_count

    @staticmethod
    def _has_illusts(result) -> bool:
        return bool(result and hasattr(result, "illusts") and result.illusts)

    def _load_initial_result(self, first_fetch):
        try:
            result = first_fetch()
        except Exception as exc:
            self.log(f"[-] 获取失败: {exc}")
            self._log_debug_exception()
            return None
        if result is None:
            self.log("[-] API 返回空值")
            return None
        if self._has_illusts(result):
            return result

        self.log("[!] API 返回空数据，开始重试...")
        retry_delays = [15, 30, 60]
        for retry, delay in enumerate(retry_delays):
            self.log(f"[!] 等待 {delay} 秒后重试 ({retry + 1}/{len(retry_delays)})...")
            if not self._wait_interruptible(delay):
                return None
            try:
                result = first_fetch()
                if self._has_illusts(result):
                    self.log(f"[+] 重试成功，获取到 {len(result.illusts)} 个作品")
                    return result
                self.log(f"[!] 重试 {retry + 1} 仍返回空数据")
            except Exception as exc:
                self.log(f"[-] 重试失败: {exc}")
        self.log("[!] 该用户作品列表为空或被限流，跳过")
        return None

    def _process_illust(
        self,
        illust,
        fetched,
        download_queue,
        stats,
        stats_lock,
        update_status,
        downloaded_ids,
    ) -> _IllustOutcome:
        """Process one illustration through the filter → detail → enqueue pipeline.

        结构（R8-2 重构）：原 E(37) 函数按流水线阶段拆为3个 helper ——
        ``_handle_legacy_skip_check``（已下载审计/缺页重排）+
        ``_fetch_illust_detail_with_timing``（详情请求+耗时告警+异常分类）+
        ``_build_and_enqueue_pages``（URL 提取+逐页构造 DownloadTask 入队）。
        主函数降为编排器，两次 ``_filter_and_record_skip`` 守卫保持内联。
        """
        skip_outcome = self._handle_legacy_skip_check(
            illust, downloaded_ids, stats, stats_lock, update_status
        )
        if skip_outcome is not None:
            return skip_outcome

        pre_ai_type = getattr(illust, "illust_ai_type", None)
        pre_bookmarks = getattr(illust, "total_bookmarks", None)
        if self._filter_and_record_skip(
            illust,
            fetched,
            stats,
            stats_lock,
            update_status,
            ai_type=pre_ai_type,
            bookmarks=pre_bookmarks,
            check_bookmarks=pre_bookmarks is not None,
        ):
            return _IllustOutcome(fetched=1)

        illust = self._fetch_illust_detail_with_timing(illust)

        if self._filter_and_record_skip(
            illust,
            fetched,
            stats,
            stats_lock,
            update_status,
            ai_type=getattr(illust, "illust_ai_type", 0) or 0,
            bookmarks=getattr(illust, "total_bookmarks", 0) or 0,
            check_bookmarks=True,
        ):
            return _IllustOutcome(fetched=1)

        queued, last_page_index = self._build_and_enqueue_pages(illust, download_queue)

        with stats_lock:
            stats["fetched"] = fetched + 1
        update_status()
        if getattr(self, "_active_api_request_executor", None) is None:
            self._api_sleep()
        return _IllustOutcome(fetched=1, queued=queued, page_index=last_page_index)

    def _handle_legacy_skip_check(
        self, illust, downloaded_ids, stats, stats_lock, update_status
    ) -> _IllustOutcome | None:
        """审计历史已下载 ID；可安全跳过时返回 outcome，否则返回 None 继续。

        当旧 ID 记录对应的作品存在缺页时，重新入队校验完整性而非直接跳过。
        """
        if not (downloaded_ids and str(illust.id) in downloaded_ids):
            return None
        can_skip, missing_pages = self._audit_legacy_downloaded_id(illust)
        if can_skip:
            with stats_lock:
                stats["skipped_downloaded"] = stats.get("skipped_downloaded", 0) + 1
            update_status()
            if pixiv_utils.DEBUG_MODE:
                self.log(f"[D] 跳过已完整下载: {illust.id}")
            return _IllustOutcome(skipped=1)

        with stats_lock:
            stats["requeued_incomplete"] = stats.get("requeued_incomplete", 0) + 1
        if missing_pages:
            preview = ", ".join(f"p{page}" for page in missing_pages[:12])
            suffix = "…" if len(missing_pages) > 12 else ""
            self.log(
                f"[!] 检测到历史多页作品缺页，重新校验: {illust.id} " f"缺少 {preview}{suffix}"
            )
        else:
            self.log(f"[!] 无法从旧 ID 记录确认多页完整性，重新校验: {illust.id}")
        return None

    def _fetch_illust_detail_with_timing(self, illust):
        """请求作品详情，记录耗时告警，异常分类后返回（可能富化后的）illust。

        ``FollowCrawlControl`` 重新抛出以传播取消信号；其余异常仅记录日志，
        返回原 illust 以便后续过滤/入队流程继续。
        """
        detail_t0 = time.monotonic()
        try:
            request_executor = getattr(self, "_active_api_request_executor", None)
            if request_executor is not None:
                detail = request_executor.call(
                    f"获取作品 {illust.id} 详情",
                    lambda: self.api.illust_detail(illust.id),
                )
            else:
                detail = self.api.illust_detail(illust.id)
            detail_cost = time.monotonic() - detail_t0
            if detail_cost > 5:
                self.log(
                    f"[!] 作品 {illust.id} 详情请求耗时 {detail_cost:.1f}s（网络缓慢或被限流）"
                )
            if detail and hasattr(detail, "illust") and detail.illust:
                illust = detail.illust
            if request_executor is None:
                self.api_counter.increment()
            if pixiv_utils.DEBUG_MODE:
                bookmarks = getattr(illust, "total_bookmarks", 0) or 0
                ai_type = getattr(illust, "illust_ai_type", 0) or 0
                self.log(f"[D] 作品详情: {illust.id} 收藏={bookmarks} AI类型={ai_type}")
        except FollowCrawlControl:
            raise
        except Exception as exc:
            detail_cost = time.monotonic() - detail_t0
            self.log(f"[!] 获取 {illust.id} 详情失败（{detail_cost:.1f}s）: {exc}")
        return illust

    def _build_and_enqueue_pages(self, illust, download_queue) -> tuple[int, int | None]:
        """提取图片 URL/元数据，逐页构造 DownloadTask 并入队。

        返回 ``(queued_count, last_page_index)``。入队失败时提前终止循环。
        """
        urls = self._get_image_urls(illust)
        original_urls, known_page_count = (
            self._get_original_download_metadata(illust) if self.config.original else ((), 0)
        )
        if pixiv_utils.DEBUG_MODE:
            self.log(f"[D] 作品 {illust.id} 获取到 {len(urls)} 个图片URL")

        is_ai = getattr(illust, "illust_ai_type", 0) == 2
        bookmarks = getattr(illust, "total_bookmarks", 0) or 0
        create_date = getattr(illust, "create_date", None)
        title = getattr(illust, "title", "") or ""
        artist_id = ""
        artist_name = ""
        if hasattr(illust, "user") and illust.user:
            artist_id = str(getattr(illust.user, "id", "") or "")
            artist_name = getattr(illust.user, "name", "") or ""

        queued = 0
        last_page_index = None
        for url, page_index in urls:
            save_path = self._get_save_path(illust, url)
            if self.config.mode == "ranking":
                log_dir = os.path.dirname(save_path)
            else:
                log_dir = (
                    os.path.dirname(save_path)
                    if self.config.folder_by_user
                    else self.config.save_dir
                )
            task = DownloadTask(
                url=url,
                save_path=save_path,
                illust_id=illust.id,
                log_dir=log_dir,
                is_ai=is_ai,
                bookmarks=bookmarks,
                create_date=create_date,
                page_index=page_index,
                title=title,
                artist_id=artist_id,
                artist_name=artist_name,
                original_urls=original_urls,
                known_page_count=known_page_count,
                download_root=str(self.config.save_dir or ""),
                folder_by_user=bool(self.config.folder_by_user),
                original_enabled=bool(self.config.original),
                multi_page=bool(self.config.multi_page),
            )
            if not self._enqueue_download_task(download_queue, task):
                break
            queued += 1
            last_page_index = page_index
        return queued, last_page_index

    def _audit_legacy_downloaded_id(self, illust) -> tuple[bool, list[int]]:
        """Return whether a work-level legacy ID is safe to skip.

        Single-page/preview modes preserve the inexpensive legacy shortcut.
        Original multi-page mode verifies every expected page against the
        page-level record repository and the actual file before skipping.
        """
        if not (self.config.original and self.config.multi_page):
            return True, []

        try:
            page_count = int(getattr(illust, "page_count", 0) or 0)
        except (TypeError, ValueError):
            page_count = 0
        if page_count <= 0:
            meta_pages = getattr(illust, "meta_pages", None)
            page_count = len(meta_pages) if meta_pages else 0
        if page_count <= 0:
            return False, []

        download_root = str(getattr(self.config, "save_dir", "") or "")
        missing_pages = [
            page
            for page in range(page_count)
            if not pixiv_utils.record_manager.check_record_skip(
                download_root,
                getattr(illust, "id", ""),
                page,
            )
        ]
        return not missing_pages, missing_pages

    def _filter_and_record_skip(
        self,
        illust,
        fetched,
        stats,
        stats_lock,
        update_status,
        *,
        ai_type,
        bookmarks,
        check_bookmarks: bool,
    ) -> bool:
        if self.config.skip_ai and ai_type == 2:
            title = getattr(illust, "title", "") or ""
            self.log(f"[*] 跳过AI作品: {illust.id} {title}")
            with stats_lock:
                stats["fetched"] = fetched + 1
                stats["skipped"] = stats.get("skipped", 0) + 1
            update_status()
            self._api_sleep()
            return True

        if (
            self.config.min_bookmarks > 0
            and check_bookmarks
            and (bookmarks or 0) < self.config.min_bookmarks
        ):
            title = getattr(illust, "title", "") or ""
            level_prefix = "[D]" if pixiv_utils.DEBUG_MODE else "[*]"
            self.log(
                f"{level_prefix} 收藏不足({bookmarks or 0}<{self.config.min_bookmarks}): "
                f"{illust.id} {title}"
            )
            with stats_lock:
                stats["fetched"] = fetched + 1
                stats["low_bookmarks"] = stats.get("low_bookmarks", 0) + 1
            update_status()
            self._api_sleep()
            return True
        return False

    def _api_sleep(self):
        """API 请求延迟。"""
        actual_delay = self.config.api_delay + random.uniform(0, 0.5)
        if self.stop_event is not None:
            self.stop_event.wait(actual_delay)
        else:
            time.sleep(actual_delay)

    def _enqueue_download_task(self, download_queue, task: DownloadTask) -> bool:
        """Put a task with cancellation-aware back-pressure."""
        while not self._should_stop():
            try:
                accepted = download_queue.put(task, timeout=0.25)
                return accepted is not False
            except queue.Full:
                continue
        return False

    def _log_debug_exception(self):
        if pixiv_utils.DEBUG_MODE:
            import traceback

            self.log(f"[D] {traceback.format_exc()}")


__all__ = ["PixivFetcherQueueMixin"]
