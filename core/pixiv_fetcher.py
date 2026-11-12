#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pixiv artwork fetching facade with explicit mode strategies."""

from __future__ import annotations

import random  # noqa: F401  legacy monkeypatch/import seam
import time
from dataclasses import dataclass, field

import requests  # noqa: F401  legacy monkeypatch/import seam

from core.auth_context import resolve_cookie_header
from core.download_service import APICallCounter
from core.fetch_strategies import MODE_STRATEGIES, FetchContext
from core.pixiv_fetcher_follow_mixin import PixivFetcherFollowMixin
from core.pixiv_fetcher_path_mixin import PixivFetcherPathMixin
from core.pixiv_fetcher_queue_mixin import PixivFetcherQueueMixin
from core.pixiv_fetcher_ranking_mixin import PixivFetcherRankingMixin
from core.pixiv_fetcher_search_mixin import PixivFetcherSearchMixin


@dataclass(init=False)
class FetchConfig:
    """Fetch options with the legacy ``FetchConfig(**kwargs)`` contract."""

    save_dir: str = ""
    max_count: int = 100
    api_delay: float = 0.3
    min_bookmarks: int = 0
    skip_ai: bool = False
    original: bool = True
    multi_page: bool = True
    folder_by_user: bool = False
    name_template: str = "{id}_{title}_p{page}"
    mode: str = ""
    keyword: str = ""
    search_sort: str = "popular_desc"
    user_ids: list[str] = field(default_factory=list)
    rank_mode: str = "day"
    rank_date: str = ""
    rank_date_end: str = ""
    related_illust_id: str = ""
    follow_start_mode: str = "resume"
    follow_start_value: str = ""
    follow_batch_artists: int = 30
    follow_api_budget: int = 500
    crawl_state_path: str = ""
    resume_enabled: bool = True
    auth_cookie: str = field(default="", repr=False)
    user_agent: str = ""

    def __init__(self, **kwargs):
        self.save_dir = kwargs.get("save_dir", "")
        self.max_count = kwargs.get("max_count", 100)
        self.api_delay = kwargs.get("api_delay", 0.3)
        self.min_bookmarks = kwargs.get("min_bookmarks", 0)
        self.skip_ai = kwargs.get("skip_ai", False)
        self.original = kwargs.get("original", True)
        self.multi_page = kwargs.get("multi_page", True)
        self.folder_by_user = kwargs.get("folder_by_user", False)
        self.name_template = kwargs.get("name_template", "{id}_{title}_p{page}")
        self.mode = kwargs.get("mode", "")
        self.keyword = kwargs.get("keyword", "")
        self.search_sort = kwargs.get("search_sort", "popular_desc")
        raw_user_ids = kwargs.get("user_ids", [])
        self.user_ids = (
            list(raw_user_ids) if isinstance(raw_user_ids, (list, tuple, set)) else raw_user_ids
        )
        self.rank_mode = kwargs.get("rank_mode", "day")
        self.rank_date = kwargs.get("rank_date", "")
        self.rank_date_end = kwargs.get("rank_date_end", "")
        self.related_illust_id = kwargs.get("related_illust_id", "")
        self.follow_start_mode = kwargs.get("follow_start_mode", "resume")
        self.follow_start_value = kwargs.get("follow_start_value", "")
        self.follow_batch_artists = kwargs.get("follow_batch_artists", 30)
        self.follow_api_budget = kwargs.get("follow_api_budget", 500)
        self.crawl_state_path = kwargs.get("crawl_state_path", "")
        self.resume_enabled = kwargs.get("resume_enabled", True)
        self.auth_cookie = resolve_cookie_header(kwargs.get("auth_cookie", ""))
        self.user_agent = kwargs.get("user_agent", "")


class PixivFetcher(
    PixivFetcherSearchMixin,
    PixivFetcherRankingMixin,
    PixivFetcherFollowMixin,
    PixivFetcherQueueMixin,
    PixivFetcherPathMixin,
):
    """Fetch facade retaining the historical public method signature."""

    def __init__(self, api, config, log_callback=None, stop_event=None):
        self.api = api
        self.config = config
        self.log = log_callback or (lambda msg: print(msg))
        self.stop_event = stop_event
        self._search_runtime = None
        self._disable_web_popular = False
        self.api_counter = APICallCounter(log_callback=self.log, stop_event=self.stop_event)

    def _wait_interruptible(self, seconds: float) -> bool:
        """Sleep without delaying cancellation when a stop event is set."""
        delay = max(0.0, float(seconds))
        if self.stop_event is not None:
            return not self.stop_event.wait(delay)
        time.sleep(delay)
        return True

    def fetch_by_mode(
        self, mode, download_queue, stats, stats_lock, update_status, downloaded_ids=None
    ):
        """Dispatch one of the supported fetch strategies.

        The context object replaces the former seven-argument implicit tuple;
        the facade itself remains source-compatible for existing callers.
        """
        context = FetchContext(
            download_queue=download_queue,
            stats=stats,
            stats_lock=stats_lock,
            update_status=update_status,
            downloaded_ids=set() if downloaded_ids is None else downloaded_ids,
        )
        strategy = MODE_STRATEGIES.get(mode)
        if strategy is None:
            return 0
        return strategy(self, context)


__all__ = ["FetchConfig", "PixivFetcher"]
