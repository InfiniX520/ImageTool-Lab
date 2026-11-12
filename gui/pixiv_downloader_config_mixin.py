#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pixiv 主界面配置读写与窗口生命周期逻辑。"""

from __future__ import annotations

import json
import threading
import tkinter as tk

from core.app_paths import get_app_paths
from core.cookie_provider import CookieProvider
from core.download_service import log_message
from core.http_client import close_http_sessions
from core.record_manager import clear_record_cache, flush_pending_markdown
from gui.ui_fx import set_pill
from utils.file_utils import write_json

APP_PATHS = get_app_paths()
ROOT = APP_PATHS.project_root
CONFIG_FILE = APP_PATHS.legacy_config_file
DOWNLOADED_IDS_FILE = APP_PATHS.downloaded_ids_file
CONFIG_VERSION = "2.0.0"


class PixivDownloaderConfigMixin:
    """配置读写、Cookie 加载与窗口关闭收口。

    负责将 ``config.json`` 映射到 Tk 变量、加载 Netscape Cookie 文件、
    在关闭时持久化配置并清理明文 Cookie 残留。

    Host contract（由 ``PixivDownloaderGUI`` 宿主提供）:
        - Tk 变量：``mode_var``/``folder_var``/``original_var``/``multi_var``/
          ``skip_ai_var``/``skip_downloaded_var``/``name_template``/
          ``follow_start_mode_var``
        - Entry 控件：``token_entry``/``dir_entry``/``count_entry``/``thread_entry``/
          ``delay_entry``/``api_delay_entry``/``min_bookmarks_entry``/``keyword_entry``/
          ``user_entry``/``related_entry``/``rank_date_entry``/``rank_date_end_entry``/
          ``follow_start_value_entry``/``follow_batch_artists_entry``/
          ``follow_api_budget_entry``
        - Combobox：``search_sort_combo``/``rank_combo``
        - 属性：``auth_cookie``/``user_agent``/``cookie_provider``/``cookie_source``/
          ``cookie_source_path``/``log_queue``/``app_paths``
        - 方法：``_toggle_mode()``/``_set_cookie_status(...)``/``_set_entry(...)``/
          ``_search_sort_value()``/``_follow_start_mode_value()``
    """

    def _app_paths(self):
        """Return the instance-scoped path provider (with legacy fallback)."""
        return getattr(self, "app_paths", APP_PATHS)

    def _config_path(self):
        """Return the JSON configuration path for this GUI instance."""
        return self._app_paths().legacy_config_file

    def _downloaded_ids_path(self):
        """Return the downloaded-ID path for this GUI instance."""
        return self._app_paths().downloaded_ids_file

    """PixivDownloaderConfigMixin。"""

    def _load_config(self):
        """加载配置"""
        try:
            cfg = {}
            config_path = self._config_path()
            if config_path.exists():
                with config_path.open("r", encoding="utf-8") as f:
                    cfg = json.load(f)
                if not isinstance(cfg, dict):
                    cfg = {}

                self._set_entry(self.token_entry, cfg.get("token", ""))
                self._set_entry(self.dir_entry, cfg.get("save_dir", ""))
                self._set_entry(self.count_entry, cfg.get("count", "100"))
                self._set_entry(self.thread_entry, cfg.get("threads", "3"))
                self._set_entry(self.delay_entry, cfg.get("delay", "0.5"))
                self._set_entry(self.api_delay_entry, cfg.get("api_delay", "0.3"))
                self._set_entry(self.min_bookmarks_entry, cfg.get("min_bookmarks", "0"))
                # Legacy inline cookies remain a compatibility fallback.  A
                # Netscape export next to the application is resolved below
                # and takes precedence without adding a new UI field.
                self.auth_cookie = cfg.get("cookie", "")
                self.user_agent = cfg.get("user_agent", "")

                if cfg.get("name_template"):
                    self.name_template.set(cfg["name_template"])
                if cfg.get("folder_by_user") is not None:
                    self.folder_var.set(cfg["folder_by_user"])
                if cfg.get("original") is not None:
                    self.original_var.set(bool(cfg["original"]))
                if cfg.get("multi_page") is not None:
                    self.multi_var.set(bool(cfg["multi_page"]))
                if cfg.get("skip_ai") is not None:
                    self.skip_ai_var.set(cfg["skip_ai"])
                if cfg.get("skip_downloaded") is not None:
                    self.skip_downloaded_var.set(cfg["skip_downloaded"])

                saved_mode = cfg.get("mode", "search")
                if saved_mode in (
                    "search",
                    "user",
                    "follow",
                    "follow_all",
                    "ranking",
                    "related",
                    "recommended",
                ):
                    self.mode_var.set(saved_mode)

                if cfg.get("keyword") is not None:
                    self._set_entry(self.keyword_entry, cfg.get("keyword", ""))
                if cfg.get("search_sort"):
                    labels = getattr(self, "SEARCH_SORT_LABELS", {})
                    self.search_sort_combo.set(labels.get(cfg["search_sort"], cfg["search_sort"]))
                if cfg.get("user_ids") is not None:
                    self._set_entry(self.user_entry, cfg.get("user_ids", ""))
                if cfg.get("related_illust_id") is not None:
                    self._set_entry(self.related_entry, cfg.get("related_illust_id", ""))
                follow_labels = getattr(self, "FOLLOW_START_LABELS", {})
                follow_mode = cfg.get("follow_start_mode", "resume")
                if hasattr(self, "follow_start_mode_var"):
                    self.follow_start_mode_var.set(
                        follow_labels.get(follow_mode, follow_labels.get("resume", follow_mode))
                    )
                if hasattr(self, "follow_start_value_entry"):
                    self._set_entry(
                        self.follow_start_value_entry, cfg.get("follow_start_value", "")
                    )
                if hasattr(self, "follow_batch_artists_entry"):
                    self._set_entry(
                        self.follow_batch_artists_entry, cfg.get("follow_batch_artists", "30")
                    )
                if hasattr(self, "follow_api_budget_entry"):
                    self._set_entry(
                        self.follow_api_budget_entry, cfg.get("follow_api_budget", "500")
                    )
                toggle_follow_input = getattr(self, "_toggle_follow_start_input", None)
                if callable(toggle_follow_input):
                    toggle_follow_input()
                if cfg.get("rank_mode"):
                    self.rank_combo.set(cfg.get("rank_mode", "day"))
                if cfg.get("rank_date") is not None:
                    self._set_entry(self.rank_date_entry, cfg.get("rank_date", ""))
                if cfg.get("rank_date_end") is not None:
                    self._set_entry(self.rank_date_end_entry, cfg.get("rank_date_end", ""))

                self._toggle_mode()
            self._load_cookie_file(cfg)
        except Exception as e:
            # Keep automatic Cookie discovery independent from optional GUI
            # config parsing; a damaged legacy JSON must not disable auth.
            self._load_cookie_file(cfg)
            log_message(f"加载配置失败: {e}", "warning")

    def _load_cookie_file(self, config: dict | None = None) -> bool:
        """Load the browser-exported cookie file, silently and safely.

        The method deliberately does not enqueue the Cookie header or any
        exception text.  It only updates the in-memory request context; the
        existing inline JSON value remains available when the export is
        absent or invalid.
        """
        config = config or {}
        provider = getattr(self, "cookie_provider", None)
        paths = getattr(self, "app_paths", APP_PATHS)
        configured_path = config.get("cookie_file")
        try:
            if configured_path:
                provider = CookieProvider(
                    paths=paths,
                    cookie_path=paths.resolve(configured_path),
                )
                self.cookie_provider = provider
            elif provider is None:
                provider = CookieProvider(paths=paths)
                self.cookie_provider = provider
        except (OSError, TypeError, ValueError):
            self._set_cookie_status(False)
            return False

        try:
            records = provider.get_records()
            header = provider.get_cookie_header()
        except Exception:
            self._set_cookie_status(False)
            return False

        if not header:
            self._set_cookie_status(False)
            return False

        self.auth_cookie = header
        self.cookie_source = provider.source
        self.cookie_source_path = provider.source_path
        self._set_cookie_status(True, len(records), provider.source_path)

        # Netscape 导出成功后，主动清理 config.json 中残留的旧版明文 Cookie，
        # 避免凭据在文件中长期留存（_save_config 仅在关闭时清理，进程被杀则残留）。
        if provider.source == "netscape" and config.get("cookie"):
            config.pop("cookie", None)
            try:
                write_json(self._config_path(), config)
            except Exception:
                pass

        return True

    def _set_cookie_status(self, available: bool, count: int = 0, source_path=None) -> None:
        """Update only redacted Cookie metadata in the presentation."""
        label = getattr(self, "cookie_status_label", None)
        if label is None:
            return

        if available:
            source_name = getattr(source_path, "name", "") or "Cookie 文件"
            text = f"● 已就绪 · {count} 项"
            tone = "success"
            tooltip_text = f"来源：{source_name}"
        else:
            text = "○ 未检测到"
            tone = "warning"
            tooltip_text = "请更新 www.pixiv.net_cookies.txt"

        def apply_status():
            try:
                set_pill(label, text, tone, getattr(self, "_colors", {}) or {})
                label.configure(cursor="arrow")
                label._status_detail = tooltip_text
            except tk.TclError:
                pass

        if threading.current_thread() is threading.main_thread():
            apply_status()
        else:
            post_to_ui = getattr(self, "_post_to_ui", None)
            if callable(post_to_ui):
                post_to_ui(apply_status)

    def _save_config(self):
        """保存配置"""
        try:
            # 保留未知字段，避免覆盖用户扩展配置。Cookie 文件是首选，
            # 因此一旦成功读取 Netscape 导出，就迁移掉旧的明文 cookie
            # 字段；没有导出时仍保留旧字段以兼容已有安装。
            base_cfg = {}
            config_path = self._config_path()
            if config_path.exists():
                with config_path.open("r", encoding="utf-8") as f:
                    base_cfg = json.load(f)
                if not isinstance(base_cfg, dict):
                    base_cfg = {}

            config = {
                "version": CONFIG_VERSION,
                "token": self.token_entry.get().strip(),
                "save_dir": self.dir_entry.get().strip(),
                "count": self.count_entry.get().strip(),
                "threads": self.thread_entry.get().strip(),
                "delay": self.delay_entry.get().strip(),
                "api_delay": self.api_delay_entry.get().strip(),
                "name_template": self.name_template.get(),
                "folder_by_user": self.folder_var.get(),
                "original": self.original_var.get(),
                "multi_page": self.multi_var.get(),
                "min_bookmarks": self.min_bookmarks_entry.get().strip(),
                "skip_ai": self.skip_ai_var.get(),
                "skip_downloaded": self.skip_downloaded_var.get(),
                "mode": self.mode_var.get(),
                "keyword": self.keyword_entry.get().strip(),
                "search_sort": (
                    self._search_sort_value()
                    if hasattr(self, "_search_sort_value")
                    else self.search_sort_combo.get()
                ),
                "user_ids": self.user_entry.get().strip(),
                "related_illust_id": self.related_entry.get().strip(),
                "follow_start_mode": (
                    self._follow_start_mode_value()
                    if hasattr(self, "_follow_start_mode_value")
                    else "resume"
                ),
                "follow_start_value": (
                    self.follow_start_value_entry.get().strip()
                    if hasattr(self, "follow_start_value_entry")
                    else ""
                ),
                "follow_batch_artists": (
                    self.follow_batch_artists_entry.get().strip()
                    if hasattr(self, "follow_batch_artists_entry")
                    else "30"
                ),
                "follow_api_budget": (
                    self.follow_api_budget_entry.get().strip()
                    if hasattr(self, "follow_api_budget_entry")
                    else "500"
                ),
                "rank_mode": self.rank_combo.get(),
                "rank_date": self.rank_date_entry.get().strip(),
                "rank_date_end": self.rank_date_end_entry.get().strip(),
                "user_agent": base_cfg.get("user_agent", self.user_agent),
            }
            cookie_provider = getattr(self, "cookie_provider", None)
            if cookie_provider is not None and cookie_provider.source == "netscape":
                base_cfg.pop("cookie", None)
                config["cookie_file"] = str(
                    cookie_provider.source_path or self._app_paths().cookie_file
                )
            elif "cookie" in base_cfg:
                # Keep a pre-existing legacy value, but never create a new
                # inline secret merely because a session supplied one.
                config["cookie"] = base_cfg["cookie"]
            base_cfg.update(config)
            write_json(config_path, base_cfg)
        except Exception as e:
            log_message(f"保存配置失败: {e}", "error")

    def _set_entry(self, entry, value):
        """设置输入框值"""
        entry.delete(0, tk.END)
        entry.insert(0, str(value))

    def _on_close(self):
        """关闭窗口"""
        if getattr(self, "_is_closing", False):
            return

        self._is_closing = True
        self.running = False

        if hasattr(self, "stop_event"):
            self.stop_event.set()

        worker = getattr(self, "_download_thread", None)
        if worker is not None and worker.is_alive():
            close_http_sessions()
            worker.join(timeout=2.0)
        flush_pending_markdown()
        clear_record_cache()

        log_after_id = getattr(self, "_log_after_id", None)
        if log_after_id:
            try:
                self.root.after_cancel(log_after_id)
            except Exception:
                pass
            self._log_after_id = None

        throttler = getattr(self, "_ui_throttler", None)
        if throttler is not None:
            throttler.close()

        self._save_config()

        close_log = getattr(getattr(self, "log_queue", None), "close_log", None)
        if callable(close_log):
            close_log()

        try:
            self.root.quit()
        except Exception:
            pass

        try:
            self.root.destroy()
        except Exception:
            pass
