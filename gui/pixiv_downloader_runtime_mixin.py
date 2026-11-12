#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pixiv 主界面登录与下载任务执行逻辑。"""

from __future__ import annotations

import os
import queue
import random
import re
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from string import Formatter
from tkinter import messagebox

from core.app_paths import get_app_paths
from core.download_service import (
    DownloadStopped,
    download_image,
    load_downloaded_ids,
    save_downloaded_id,
)
from core.download_task_store import DownloadTaskStore, DurableDownloadQueue
from core.http_client import close_current_http_session, close_http_sessions
from core.models import DownloadTask
from core.pixiv_fetcher import FetchConfig, PixivFetcher
from core.record_manager import clear_record_cache, flush_pending_markdown
from gui.ui_fx import set_pill

APP_PATHS = get_app_paths()
ROOT = APP_PATHS.project_root
DOWNLOADED_IDS_FILE = APP_PATHS.downloaded_ids_file

try:
    from pixivpy3 import AppPixivAPI
except ImportError:
    AppPixivAPI = None


class PixivDownloaderRuntimeMixin:
    """登录、下载任务编排与停止回收。

    负责 Refresh Token 登录、参数校验、构建 ``FetchConfig``、启动下载线程、
    join 回收、停止 force-close 与完成统计。定义 ``JOIN_TIMEOUT_STOP``/
    ``JOIN_TIMEOUT_NORMAL`` 常量。

    Host contract（由 ``PixivDownloaderGUI`` 宿主提供）:
        - ``self.root`` / ``self.log_queue`` / ``self._ui_throttler`` / ``self._colors``
        - ``self.api`` / ``self.running`` / ``self.stop_event`` / ``self._is_closing``
        - ``self._download_thread`` — 由本 mixin 创建并 join
        - 控件：``login_btn``/``start_btn``/``stop_btn``/``login_status``/
          ``status_label`` + 全部 Entry/Combobox/Tk 变量（同 ConfigMixin 列表）
        - 属性：``auth_cookie``/``user_agent``/``app_paths``
        - 方法：``_clear_log()``/``_save_config()``/``_load_cookie_file()``/
          ``_parse_user_ids()``/``_search_sort_value()``/``_follow_start_mode_value()``/
          ``_app_paths()``/``_downloaded_ids_path()``
    """

    # join 超时：停止时短超时快速回收，正常完成时给足时间等 I/O 收尾。
    JOIN_TIMEOUT_STOP = 2.0
    JOIN_TIMEOUT_NORMAL = 30.0

    def _post_to_ui(self, callback) -> None:
        """Schedule a UI callback unless the Tk root is closing/destroyed."""
        if getattr(self, "_is_closing", False):
            return
        try:
            self.root.after(0, callback)
        except (tk.TclError, RuntimeError):
            pass

    def _post_status(self, text: str) -> None:
        """高频状态栏更新走节流合并，避免下载洪峰淹没 UI 线程。"""
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

    def _set_status_pill(self, label, text: str, tone: str) -> None:
        """以平滑色彩过渡更新状态徽章；可在任意线程调用。"""
        colors = getattr(self, "_colors", None) or {}
        if threading.current_thread() is threading.main_thread():
            set_pill(label, text, tone, colors)
        else:
            self._post_to_ui(lambda: set_pill(label, text, tone, colors))

    """PixivDownloaderRuntimeMixin。"""

    def _login(self):
        """登录 Pixiv"""
        if AppPixivAPI is None:
            messagebox.showerror("错误", "请先安装 pixivpy3")
            return

        token = self.token_entry.get().strip()
        if not token:
            messagebox.showerror("错误", "请输入 Refresh Token")
            return

        self.login_btn.config(state="disabled", text="登录中…")
        self._set_status_pill(self.login_status, "登录中", "warning")
        self.log_queue.put("[*] 正在登录...")

        def do_login():
            try:
                self.api = AppPixivAPI()
                self.api.auth(refresh_token=token)
                self._refresh_auth_context_from_api()
                self.log_queue.put(f"[+] 登录成功! 用户: {self.api.user_id}")

                def show_success():
                    self._set_status_pill(self.login_status, "已登录", "success")
                    self.login_btn.config(state="normal", text="重新登录")

                self._post_to_ui(show_success)
            except Exception as e:
                self.log_queue.put(f"[-] 登录失败: {e}")
                self.api = None

                def show_failure():
                    self._set_status_pill(self.login_status, "登录失败", "danger")
                    self.login_btn.config(state="normal", text="登录")

                self._post_to_ui(show_failure)

        threading.Thread(target=do_login, daemon=True).start()

    def _refresh_auth_context_from_api(self):
        """尽量从已登录会话提取 Cookie/UA，提升网页端原图请求成功率。"""
        try:
            # A browser-exported Netscape file is the canonical credential
            # source.  Re-read its metadata after login so a refreshed file
            # is picked up, and do not let an incomplete pixivpy session
            # overwrite a valid export.
            if self._load_cookie_file():
                return

            # 优先使用配置中的 UA，若没有再从会话中取。
            if not self.user_agent:
                self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

            # pixivpy3 内部字段版本差异较大，使用宽松探测。
            session = getattr(self.api, "requests", None) or getattr(self.api, "_requests", None)
            if session and hasattr(session, "cookies") and session.cookies:
                cookie_pairs = []
                for c in session.cookies:
                    if getattr(c, "name", None) and getattr(c, "value", None):
                        cookie_pairs.append(f"{c.name}={c.value}")
                if cookie_pairs:
                    self.auth_cookie = "; ".join(cookie_pairs)
                    self.log_queue.put("[*] 已从会话提取 Cookie")
        except Exception as e:
            self.log_queue.put(f"[!] 会话信息提取失败: {e}")

    def _start_download(self):
        """开始下载"""
        if self.running or self.api is None:
            if self.api is None:
                messagebox.showerror("错误", "请先登录")
            return

        save_dir = self.dir_entry.get().strip()
        if not save_dir:
            messagebox.showerror("错误", "请选择保存目录")
            return

        self.log_queue.put("[*] 收到开始下载指令，正在读取原图 Cookie...")
        # Cookies can be refreshed by exporting a new browser file while the
        # application remains open.  Resolve it at the start of every batch;
        # the provider's metadata cache makes this a cheap operation.
        self._load_cookie_file()
        self.log_queue.put(
            f"[*] Cookie 就绪（{'有' if self.auth_cookie else '无'}），正在校验参数..."
        )

        try:
            max_count = int(self.count_entry.get())
            max_workers = max(1, int(self.thread_entry.get()))
            delay = max(0, float(self.delay_entry.get()))
            api_delay = max(0, float(self.api_delay_entry.get()))
            min_bookmarks = max(0, int(self.min_bookmarks_entry.get() or 0))
            follow_batch_artists = int(
                getattr(self, "follow_batch_artists_entry", self.count_entry).get() or 30
            )
            follow_api_budget = int(
                getattr(self, "follow_api_budget_entry", self.count_entry).get() or 500
            )
        except (TypeError, ValueError):
            self.log_queue.put("[-] 参数解析失败，请检查数量、线程和间隔设置")
            messagebox.showerror("错误", "参数无效")
            return

        validation_error = self._validate_download_request_inputs(
            mode=self.mode_var.get(),
            max_count=max_count,
            max_workers=max_workers,
            delay=delay,
            api_delay=api_delay,
            name_template=self.name_template.get(),
            follow_start_mode=(
                self._follow_start_mode_value()
                if hasattr(self, "_follow_start_mode_value")
                else "resume"
            ),
            follow_start_value=(
                self.follow_start_value_entry.get().strip()
                if hasattr(self, "follow_start_value_entry")
                else ""
            ),
            follow_batch_artists=follow_batch_artists,
            follow_api_budget=follow_api_budget,
        )
        if validation_error:
            self.log_queue.put(f"[-] 参数校验未通过: {validation_error}")
            messagebox.showerror("无法开始下载", validation_error)
            return

        self.log_queue.put("[*] 参数校验通过，正在准备保存目录...")
        try:
            os.makedirs(save_dir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("保存目录不可用", str(exc))
            return

        # Snapshot all Tk variables on the main thread.  The worker receives
        # immutable values and never calls ``Variable.get()`` after launch.
        skip_downloaded = bool(self.skip_downloaded_var.get())
        skip_ai = bool(self.skip_ai_var.get())
        original_enabled = bool(self.original_var.get())
        multi_page = bool(self.multi_var.get())
        folder_by_user = bool(self.folder_var.get())
        mode = self.mode_var.get()
        auth_cookie = self.auth_cookie
        user_agent = self.user_agent

        self.running = True
        self.stop_event.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._clear_log()
        self._save_config()
        self.log_queue.put("[*] 配置已保存，正在构建下载任务...")

        # 更新状态栏
        self.status_label.config(text="正在准备下载...")

        # 构建配置
        config = FetchConfig(
            save_dir=save_dir,
            max_count=max_count,
            api_delay=api_delay,
            min_bookmarks=min_bookmarks,
            skip_ai=skip_ai,
            original=original_enabled,
            multi_page=multi_page,
            folder_by_user=folder_by_user,
            name_template=self.name_template.get(),
            mode=mode,
            keyword=self.keyword_entry.get().strip(),
            search_sort=(
                self._search_sort_value()
                if hasattr(self, "_search_sort_value")
                else self.search_sort_combo.get()
            ),
            user_ids=self._parse_user_ids(),
            related_illust_id=self.related_entry.get().strip(),
            rank_mode=self.rank_combo.get(),
            rank_date=self.rank_date_entry.get().strip(),  # 排行榜开始日期
            rank_date_end=self.rank_date_end_entry.get().strip(),  # 排行榜结束日期
            follow_start_mode=(
                self._follow_start_mode_value()
                if hasattr(self, "_follow_start_mode_value")
                else "resume"
            ),
            follow_start_value=(
                self.follow_start_value_entry.get().strip()
                if hasattr(self, "follow_start_value_entry")
                else ""
            ),
            follow_batch_artists=follow_batch_artists,
            follow_api_budget=follow_api_budget,
            crawl_state_path=str(
                self._app_paths().download_logs_dir / "follow_crawl_state.sqlite3"
            ),
            resume_enabled=skip_downloaded,
            auth_cookie=auth_cookie,
            user_agent=user_agent,
        )

        self.log_queue.put(
            f"[*] 下载间隔: {delay}~{delay+1}秒 | API间隔: {api_delay}~{api_delay+0.5}秒"
        )

        self._download_thread = threading.Thread(
            target=self._download_task,
            daemon=True,
            args=(config, max_workers, delay, skip_downloaded, mode),
        )
        self._download_thread.start()

    def _validate_download_request_inputs(
        self,
        *,
        mode: str,
        max_count: int,
        max_workers: int,
        delay: float,
        api_delay: float,
        name_template: str,
        follow_start_mode: str = "resume",
        follow_start_value: str = "",
        follow_batch_artists: int = 30,
        follow_api_budget: int = 80,
    ) -> str:
        """Return a user-facing validation message or an empty string.

        结构（R8-1 重构）：原 E(34) 函数按校验职责拆为3个 helper ——
        ``_validate_numeric_bounds``（数值范围）+ ``_validate_mode_inputs``
        （模式相关）+ ``_validate_name_template``（命名模板）。主函数降为
        顺序编排器，返回首个非空错误。
        """
        message = self._validate_numeric_bounds(max_count, max_workers, delay, api_delay)
        if message:
            return message
        message = self._validate_mode_inputs(
            mode,
            follow_start_mode=follow_start_mode,
            follow_start_value=follow_start_value,
            follow_batch_artists=follow_batch_artists,
            follow_api_budget=follow_api_budget,
        )
        if message:
            return message
        return self._validate_name_template(name_template)

    def _validate_numeric_bounds(
        self, max_count: int, max_workers: int, delay: float, api_delay: float
    ) -> str:
        """校验数值型参数范围（max_count/max_workers/delay/api_delay）。"""
        if max_count <= 0:
            return "下载作品数量必须大于 0。"
        if not 1 <= max_workers <= 32:
            return "线程数必须在 1 到 32 之间。"
        if delay < 0 or api_delay < 0:
            return "下载间隔和 API 间隔不能为负数。"
        if delay > 300:
            return "下载间隔过大（上限 300 秒），请调小后重试。"
        if api_delay > 60:
            return "API 间隔过大（上限 60 秒），请调小后重试。"
        return ""

    def _validate_mode_inputs(
        self,
        mode: str,
        *,
        follow_start_mode: str,
        follow_start_value: str,
        follow_batch_artists: int,
        follow_api_budget: int,
    ) -> str:
        """校验与下载模式相关的输入（search/user/related/ranking/follow_all）。"""
        if mode == "search" and not self.keyword_entry.get().strip():
            return "关键词搜索模式需要填写搜索关键词。"
        if mode == "user" and not self._parse_user_ids():
            return "用户作品模式需要至少一个有效的数字用户 ID。"
        if mode == "related" and not self.related_entry.get().strip().isdigit():
            return "作品相似推荐需要填写有效的数字作品 ID。"
        if mode == "ranking":
            message = self._validate_ranking_dates()
            if message:
                return message
        if mode == "follow_all":
            return self._validate_follow_all_params(
                follow_start_mode, follow_start_value, follow_batch_artists, follow_api_budget
            )
        return ""

    def _validate_ranking_dates(self) -> str:
        """校验排行榜开始/结束日期（YYYY-MM-DD，结束需要开始）。"""
        start_text = self.rank_date_entry.get().strip()
        end_text = self.rank_date_end_entry.get().strip()
        if end_text and not start_text:
            return "填写排行榜结束日期时，也需要填写开始日期。"
        for value in (start_text, end_text):
            if not value:
                continue
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return "排行榜日期格式必须为 YYYY-MM-DD。"
        return ""

    def _validate_follow_all_params(
        self,
        follow_start_mode: str,
        follow_start_value: str,
        follow_batch_artists: int,
        follow_api_budget: int,
    ) -> str:
        """校验关注全量模式的批量/API 预算与起始点参数。"""
        if not 1 <= int(follow_batch_artists) <= 1000:
            return "每批画师数量必须在 1 到 1000 之间。"
        if not 1 <= int(follow_api_budget) <= 10000:
            return "API 请求预算必须在 1 到 10000 之间。"
        if follow_start_mode in ("index", "artist"):
            if not str(follow_start_value).strip().isdigit() or int(follow_start_value) <= 0:
                label = "画师序号" if follow_start_mode == "index" else "画师 ID"
                return f"{label}必须是正整数。"
        return ""

    def _validate_name_template(self, name_template: str) -> str:
        """校验命名模板（空值/分隔符/字段解析/未知变量/{id} 要求）。"""
        if not name_template.strip():
            return "命名模板不能为空。"
        if any(char in name_template for char in ("/", "\\")):
            return "命名模板不能包含路径分隔符。"
        allowed_fields = {"id", "title", "user", "user_id", "page", "date"}
        try:
            fields = {
                field_name for _, field_name, _, _ in Formatter().parse(name_template) if field_name
            }
        except ValueError:
            return "命名模板中的花括号不完整。"
        unknown = sorted(fields - allowed_fields)
        if unknown:
            return f"命名模板包含未知变量：{', '.join(unknown)}。"
        if "id" not in fields:
            return "命名模板必须包含 {id}，以避免不同作品互相覆盖。"
        return ""

    def _parse_user_ids(self):
        """解析用户ID"""
        text = self.user_entry.get().strip()
        if not text:
            return []
        return [uid.strip() for uid in re.split(r"[,\s\n]+", text) if uid.strip().isdigit()]

    def _stop_download(self):
        """停止下载"""
        if messagebox.askyesno("确认", "确定要停止下载吗？"):
            self.stop_event.set()
            self.log_queue.put("[!] 正在停止...")
            self.stop_btn.config(state="disabled")

    def _finish(self):
        """下载完成：单次回调内完成全部控件复位，减少主线程重绘次数。"""
        self.running = False

        def reset_ui():
            try:
                self.start_btn.config(state="normal")
                self.stop_btn.config(state="disabled")
            except tk.TclError:
                pass

        self._post_to_ui(reset_ui)

    def _setup_download_context(self, config, max_workers, skip_downloaded):
        """创建任务存储、下载队列、统计字典，并加载已下载 ID 集合。"""
        crawl_state_path = str(getattr(config, "crawl_state_path", "") or "")
        task_store_root = (
            Path(crawl_state_path).parent if crawl_state_path else Path(config.save_dir) / ".pidl"
        )
        task_store = DownloadTaskStore(task_store_root)
        recovered_tasks = task_store.list_retryable()
        download_queue = DurableDownloadQueue(
            maxsize=max(4, max_workers * 4),
            store=task_store,
        )
        stats = {
            "fetched": 0,
            "downloaded": 0,
            "failed": 0,
            "skipped": 0,
            "low_bookmarks": 0,
            "skipped_downloaded": 0,
            "requeued_incomplete": 0,
            "pages_downloaded": 0,
            "pages_failed": 0,
            "pages_skipped": 0,
            "pages_retried": 0,
            "recovered_tasks": len(recovered_tasks),
        }
        stats_lock = threading.Lock()
        downloaded_ids = set()
        if skip_downloaded:
            downloaded_ids = load_downloaded_ids(self._downloaded_ids_path())
            if downloaded_ids:
                self.log_queue.put(f"[*] 已加载 {len(downloaded_ids)} 条下载记录")
        return task_store, download_queue, stats, stats_lock, downloaded_ids, recovered_tasks

    def _join_download_threads(self, threads):
        """有限超时 join 下载线程；超时则 force-close 全部 HTTP 会话以解锁。"""
        if self.stop_event.is_set():
            close_http_sessions()
            join_timeout = self.JOIN_TIMEOUT_STOP
            self.log_queue.put(
                f"[*] 已请求停止，等待 {len(threads)} 个下载线程退出（超时 {join_timeout}s/线程）..."
            )
        else:
            join_timeout = self.JOIN_TIMEOUT_NORMAL
            self.log_queue.put(
                f"[*] 等待 {len(threads)} 个下载线程完成（超时 {join_timeout}s/线程）..."
            )
        for t in threads:
            t.join(timeout=join_timeout)
        alive_threads = [t for t in threads if t.is_alive()]
        if alive_threads:
            self.log_queue.put(
                f"[!] {len(alive_threads)} 个下载线程仍在运行（可能卡在网络 I/O），"
                f"强制关闭全部 HTTP 会话以解锁"
            )
            close_http_sessions()
            for t in threads:
                t.join(timeout=2.0)
            still_alive = [t for t in threads if t.is_alive()]
            if still_alive:
                self.log_queue.put(
                    f"[!] {len(still_alive)} 个下载线程在强制关闭后仍未退出，"
                    f"将以 daemon 模式在后台回收"
                )

    def _report_download_completion(self, stats, stats_lock, mode):
        """格式化并发布最终下载统计。"""
        if self.stop_event.is_set():
            self._post_to_ui(lambda: self.status_label.config(text="已停止"))
            return
        with stats_lock:
            final_stats = dict(stats)
        follow_status = final_stats.get("follow_run_status", "")
        follow_paused = mode == "follow_all" and follow_status not in ("", "completed")
        prefix = "[!] 本批已安全暂停" if follow_paused else "[+] 完成"
        msg = (
            f"{prefix}! 完成作品: {final_stats['downloaded']}, "
            f"最终失败作品: {final_stats['failed']} | "
            f"新增图片: {final_stats['pages_downloaded']}, "
            f"已完整作品跳过: {final_stats['skipped_downloaded']}, "
            f"记录/文件跳过: {final_stats['pages_skipped']}, "
            f"最终失败图片: {final_stats['pages_failed']}, "
            f"重试恢复: {final_stats['pages_retried']}"
        )
        if final_stats["requeued_incomplete"] > 0:
            msg += f", 历史缺页重检作品: {final_stats['requeued_incomplete']}"
        if final_stats["recovered_tasks"] > 0:
            msg += f", 本次恢复任务: {final_stats['recovered_tasks']}"
        if mode == "follow_all":
            msg += (
                f", API请求: {final_stats.get('follow_api_requests', 0)}, "
                f"任务状态: {follow_status or 'completed'}"
            )
            if final_stats.get("follow_stop_reason"):
                msg += f", 原因: {final_stats['follow_stop_reason']}"
        self.log_queue.put(msg)
        self._post_to_ui(
            lambda: self.status_label.config(
                text=(
                    f"{'已保存断点' if follow_paused else '完成'} | 作品: {final_stats['downloaded']} | "
                    f"图片: {final_stats['pages_downloaded']} | "
                    f"失败图片: {final_stats['pages_failed']}"
                )
            )
        )

    def _download_task(self, config, max_workers, delay, skip_downloaded=None, mode=None):
        """下载任务（生产者-消费者模式）"""
        # Sentinel: emitted on the worker thread BEFORE anything else so the
        # session log always proves the thread started (and how far it got).
        self.log_queue.put(f"[*] 下载线程已启动，模式: {mode or getattr(config, 'mode', '')}")
        if skip_downloaded is None:
            # Compatibility for integrations that call this private method
            # directly; normal GUI launches pass the main-thread snapshot.
            skip_downloaded = bool(self.skip_downloaded_var.get())
        if mode is None:
            mode = getattr(config, "mode", "")
        task_store = None
        try:
            task_store, download_queue, stats, stats_lock, downloaded_ids, recovered_tasks = (
                self._setup_download_context(config, max_workers, skip_downloaded)
            )
            fetch_done = threading.Event()

            def update_status():
                with stats_lock:
                    snapshot = dict(stats)
                text = (
                    f"获取作品: {snapshot['fetched']} | 完成作品: {snapshot['downloaded']} "
                    f"| 最终失败作品: {snapshot['failed']} | 图片: {snapshot['pages_downloaded']}"
                )
                if snapshot["pages_failed"] > 0:
                    text += f" | 失败图片: {snapshot['pages_failed']}"
                if snapshot["pages_retried"] > 0:
                    text += f" | 重试恢复: {snapshot['pages_retried']}"
                if snapshot["pages_skipped"] > 0:
                    text += f" | 已完整跳过: {snapshot['pages_skipped']}"
                if snapshot["skipped_downloaded"] > 0:
                    text += f" | 已下载作品跳过: {snapshot['skipped_downloaded']}"
                if snapshot["skipped"] > 0:
                    text += f" | 跳过AI: {snapshot['skipped']}"
                if snapshot["low_bookmarks"] > 0:
                    text += f" | 收藏不足: {snapshot['low_bookmarks']}"
                if snapshot.get("follow_total_artists", 0):
                    text += (
                        f" | 画师: {snapshot.get('follow_artist_ordinal', 0)}/"
                        f"{snapshot['follow_total_artists']}"
                    )
                if snapshot.get("follow_api_requests", 0):
                    text += f" | API: {snapshot['follow_api_requests']}"
                self._post_status(text)

            def record_page_result(result):
                """Aggregate one leaf-page result without changing download control flow."""
                page_status = str((result or {}).get("status", ""))
                with stats_lock:
                    if page_status == "downloaded":
                        stats["pages_downloaded"] += 1
                        if bool((result or {}).get("retried")):
                            stats["pages_retried"] += 1
                    elif page_status == "failed":
                        stats["pages_failed"] += 1
                    elif page_status.startswith("skipped_"):
                        stats["pages_skipped"] += 1

            # 消费者：下载线程
            def downloader():
                while True:
                    if self.stop_event.is_set():
                        break
                    if fetch_done.is_set() and download_queue.empty():
                        break
                    task = None
                    try:
                        task = download_queue.get(timeout=0.5)
                        (
                            url,
                            save_path,
                            illust_id,
                            log_dir,
                            is_ai,
                            bookmarks,
                            create_date,
                            page_index,
                            title,
                            artist_id,
                            artist_name,
                        ) = task
                        original_urls = tuple(getattr(task, "original_urls", ()) or ())
                        known_page_count = int(getattr(task, "known_page_count", 0) or 0)
                        task_download_root = str(
                            getattr(task, "download_root", "") or config.save_dir
                        )
                        task_folder_by_user = getattr(task, "folder_by_user", None)
                        task_original_enabled = getattr(task, "original_enabled", None)
                        task_multi_page = getattr(task, "multi_page", None)
                        if isinstance(task, DownloadTask):
                            task_store.mark_running(task)
                        success = download_image(
                            url,
                            save_path,
                            self.log_queue,
                            success_log_dir=log_dir,
                            illust_id=illust_id,
                            is_ai=is_ai,
                            bookmarks=bookmarks,
                            create_date=create_date,
                            page_index=page_index,
                            title=title,
                            artist_id=artist_id,
                            artist_name=artist_name,
                            download_root=task_download_root,
                            folder_by_user=(
                                config.folder_by_user
                                if task_folder_by_user is None
                                else bool(task_folder_by_user)
                            ),
                            resume_enabled=skip_downloaded,
                            original_enabled=(
                                config.original
                                if task_original_enabled is None
                                else bool(task_original_enabled)
                            ),
                            multi_page=(
                                config.multi_page
                                if task_multi_page is None
                                else bool(task_multi_page)
                            ),
                            auth_cookie=config.auth_cookie,
                            user_agent=config.user_agent,
                            stop_event=self.stop_event,
                            page_result_callback=record_page_result,
                            original_urls=original_urls,
                            known_page_count=known_page_count,
                        )
                        with stats_lock:
                            if success:
                                stats["downloaded"] += 1
                                if isinstance(task, DownloadTask):
                                    task_store.complete(task)
                                if skip_downloaded:
                                    save_downloaded_id(self._downloaded_ids_path(), illust_id)
                            else:
                                stats["failed"] += 1
                                if isinstance(task, DownloadTask):
                                    task_store.mark_failed(task, "download returned false")
                        update_status()
                        self.stop_event.wait(delay + random.uniform(0, 1))
                    except queue.Empty:
                        continue
                    except Exception as exc:
                        is_user_stop = isinstance(exc, DownloadStopped)
                        if is_user_stop:
                            # 用户主动停止：不计为失败，不标记任务失败。
                            self.log_queue.put("[*] 下载已被用户中止，跳过当前文件")
                        else:
                            # A persistence/network error must not silently kill a
                            # consumer while the bounded producer is blocked on a
                            # full queue. Stop the batch and account for the task.
                            self.stop_event.set()
                            with stats_lock:
                                stats["failed"] += 1
                            if isinstance(task, DownloadTask):
                                try:
                                    task_store.mark_failed(task, f"{type(exc).__name__}: {exc}")
                                except Exception:
                                    self.log_queue.put(
                                        "[!] 标记任务失败状态时出错，任务记录可能不一致"
                                    )
                            self.log_queue.put(f"[-] 下载任务失败: {exc}")
                        try:
                            update_status()
                        except Exception:
                            pass
                    finally:
                        if task is not None:
                            download_queue.task_done()

            # 启动下载线程
            threads = []
            try:
                threads = [
                    threading.Thread(target=downloader, daemon=True) for _ in range(max_workers)
                ]
                for t in threads:
                    t.start()

                if recovered_tasks:
                    self.log_queue.put(
                        f"[!] 检测到 {len(recovered_tasks)} 个未完成持久化任务，优先补下载"
                    )
                    for recovered_task in recovered_tasks:
                        if self.stop_event.is_set():
                            break
                        download_queue.put(recovered_task)

                # 生产者：获取作品
                self.log_queue.put(
                    f"[*] 下载线程池已就绪（{max_workers} 线程），开始获取作品列表..."
                )
                fetcher = PixivFetcher(
                    api=self.api,
                    config=config,
                    log_callback=lambda msg: self.log_queue.put(msg),
                    stop_event=self.stop_event,
                )
                try:
                    fetcher.fetch_by_mode(
                        mode, download_queue, stats, stats_lock, update_status, downloaded_ids
                    )
                    self.log_queue.put("[*] 作品列表获取结束，等待剩余图片下载完成...")
                except Exception:
                    self.stop_event.set()
                    raise
            finally:
                # Consumers must always observe completion, including API
                # exceptions and user cancellation.
                fetch_done.set()
                self._join_download_threads(threads)

            # 完成
            self._report_download_completion(stats, stats_lock, mode)

        except Exception as e:
            # Last-resort reporting: a failure in the logging path itself must
            # not swallow the real traceback.  Write to both the queue and the
            # console so a hung UI can never hide why the worker died.
            try:
                self.log_queue.put(f"[-] 错误: {e}")
            except Exception:
                pass
            import traceback

            traceback.print_exc()
            import core.download_service as pixiv_utils

            if pixiv_utils.DEBUG_MODE:
                try:
                    self.log_queue.put(f"[D] {traceback.format_exc()}")
                except Exception:
                    pass
        finally:
            if task_store is not None:
                task_store.close()
            close_current_http_session()
            flush_pending_markdown()
            clear_record_cache()
            self._finish()
