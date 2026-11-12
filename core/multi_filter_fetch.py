"""Headless source collectors used by the multi-filter dialog."""

from __future__ import annotations

import random
import time
from collections.abc import Mapping

from utils.logger import log_message


def _sleep(dialog, seconds: float) -> None:
    if getattr(dialog, "stop_event", None) is not None:
        dialog.stop_event.wait(seconds)
    else:
        time.sleep(seconds)


def _increment(dialog) -> None:
    if hasattr(dialog, "api_counter"):
        dialog.api_counter.increment()


def _collect_pages(dialog, result, max_count: int, next_call, *, catch_next: bool = False) -> list:
    illusts = []
    while result and hasattr(result, "illusts") and result.illusts and len(illusts) < max_count:
        if dialog.stop_event.is_set():
            break
        for illust in result.illusts:
            if len(illusts) >= max_count:
                break
            illusts.append(illust)
        if not result.next_url:
            break
        next_qs = dialog.api.parse_qs(result.next_url)
        try:
            result = next_call(next_qs)
            _increment(dialog)
        except Exception:
            if catch_next:
                break
            raise
        _sleep(dialog, 0.3 + random.uniform(0, 0.5))
    return illusts


def _fetch_follow(dialog, max_count: int, snapshot: Mapping[str, object] | None = None) -> list:
    del snapshot
    try:
        result = dialog.api.illust_follow()
        _increment(dialog)
    except Exception as exc:
        log_message(f"获取关注新作失败: {exc}", "error", dialog.log_queue)
        return []
    # 与其他来源一致：后续翻页失败时保留已获取部分，避免整个交集流程中止。
    return _collect_pages(
        dialog, result, max_count, lambda qs: dialog.api.illust_follow(**qs), catch_next=True
    )


def _fetch_ranking(dialog, max_count: int, snapshot: Mapping[str, object] | None = None) -> list:
    mode = (
        str(snapshot.get("rank_mode") or "day") if snapshot is not None else dialog.rank_combo.get()
    )
    try:
        result = dialog.api.illust_ranking(mode=mode)
        _increment(dialog)
    except Exception as exc:
        log_message(f"获取排行榜失败: {exc}", "error", dialog.log_queue)
        return []
    return _collect_pages(
        dialog, result, max_count, lambda qs: dialog.api.illust_ranking(**qs), catch_next=True
    )


def _fetch_search(dialog, max_count: int, snapshot: Mapping[str, object] | None = None) -> list:
    keyword = (
        str(snapshot.get("keyword") or "").strip()
        if snapshot is not None
        else dialog.search_entry.get().strip()
    )
    try:
        result = dialog.api.search_illust(keyword, search_target="partial_match_for_tags")
        _increment(dialog)
    except Exception as exc:
        log_message(f"搜索失败: {exc}", "error", dialog.log_queue)
        return []
    return _collect_pages(
        dialog, result, max_count, lambda qs: dialog.api.search_illust(**qs), catch_next=True
    )


def _fetch_user(dialog, max_count: int, snapshot: Mapping[str, object] | None = None) -> list:
    illusts = []
    user_text = (
        str(snapshot.get("user_ids") or "") if snapshot is not None else dialog.user_entry.get()
    )
    user_ids = [user_id.strip() for user_id in user_text.split(",") if user_id.strip()]
    for user_id in user_ids:
        if dialog.stop_event.is_set() or len(illusts) >= max_count:
            break
        try:
            result = dialog.api.user_illusts(user_id)
            _increment(dialog)
        except Exception as exc:
            log_message(f"获取用户 {user_id} 作品失败: {exc}", "error", dialog.log_queue)
            continue
        remaining = max_count - len(illusts)
        illusts.extend(
            _collect_pages(
                dialog,
                result,
                remaining,
                lambda qs: dialog.api.user_illusts(**qs),
                catch_next=True,
            )
        )
        _sleep(dialog, 0.3 + random.uniform(0, 0.5))
    return illusts


SOURCE_FETCHERS = {
    "follow": _fetch_follow,
    "ranking": _fetch_ranking,
    "search": _fetch_search,
    "user": _fetch_user,
}


def fetch_source(
    dialog,
    source: str,
    max_count: int,
    *,
    snapshot: Mapping[str, object] | None = None,
) -> list:
    """Collect a bounded source using the dialog's existing API adapter."""
    handler = SOURCE_FETCHERS.get(source)
    if handler is None:
        return []
    if snapshot is None:
        return handler(dialog, max_count)
    return handler(dialog, max_count, snapshot)


__all__ = ["SOURCE_FETCHERS", "fetch_source"]
