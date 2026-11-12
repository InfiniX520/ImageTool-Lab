"""Mode-specific Pixiv fetch strategies.

The legacy fetcher mixed seven workflows into one conditional method.  These
small strategy functions keep the exact API calls/log messages while making
each workflow independently testable and keeping the facade's dispatch path
stable for existing callers.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable


@dataclass(slots=True)
class FetchContext:
    download_queue: object
    stats: dict
    stats_lock: object
    update_status: Callable[[], None]
    downloaded_ids: set[str]


def _is_popular_high_quality_mode(config, sort_mode: str) -> bool:
    """主搜索/兜底是否走「热门 + 高收藏」快速探测路径。

    `popular_desc` 排序且最低收藏 ≥ 1000 时，搜索链路启用首屏阈值预判与
    关键词回退兜底；其余情况走标准 App API 翻页。提取为函数以消除魔法
    表达式在 ``_search`` 三段中的重复。
    """
    return sort_mode == "popular_desc" and int(config.min_bookmarks or 0) >= 1000


def _search_web_popular_fallback(fetcher, ctx: FetchContext, config, sort_mode: str):
    """网页热门排序兜底段。

    Returns:
        - ``int``: 命中后返回正计数，调用方应直接返回。
        - ``None``: 未触发或未命中，调用方继续执行 App API 主搜索。
    """
    if not (
        sort_mode == "popular_desc" and config.auth_cookie and not fetcher._disable_web_popular
    ):
        return None

    web_probe_pages = 1 if _is_popular_high_quality_mode(config, sort_mode) else 2
    fetcher.log("[*] 搜索: 先尝试网页热门排序兜底")
    web_count = fetcher._fetch_search_web_popular(
        config.keyword,
        ctx.download_queue,
        ctx.stats,
        ctx.stats_lock,
        ctx.update_status,
        ctx.downloaded_ids,
        max_probe_pages=web_probe_pages,
    )
    if web_count > 0:
        return web_count
    fetcher.log("[!] 网页热门排序未取到可用结果，回退 App API 搜索")
    return None


def _search_main_query(fetcher, ctx: FetchContext, config, sort_mode: str) -> int:
    """App API 主搜索段，返回本段命中计数。

    高收藏热门模式下先取首屏结果做阈值预判，低于阈值直接返回 0 触发后续兜底；
    其他模式直接进入 ``_fetch_and_queue`` 翻页循环。
    """
    fetcher.log(f"[*] 搜索: {config.keyword} | 排序: {sort_mode}")
    probe_pages = 1 if _is_popular_high_quality_mode(config, sort_mode) else None

    if not _is_popular_high_quality_mode(config, sort_mode):
        return fetcher._fetch_and_queue(
            lambda: fetcher._search_illust_with_sort(config.keyword, sort_mode),
            lambda r, qs: fetcher._search_next_page_with_runtime(qs),
            ctx.download_queue,
            ctx.stats,
            ctx.stats_lock,
            ctx.update_status,
            ctx.downloaded_ids,
            max_pages=probe_pages,
        )

    first_result = fetcher._search_illust_with_sort(config.keyword, sort_mode)
    if fetcher._is_popular_preview_below_threshold(first_result):
        fetcher.log("[!] App热门首屏最高收藏低于阈值，跳过当前页并进入推荐/排行榜兜底")
        return 0
    return fetcher._fetch_and_queue(
        lambda r=first_result: r,
        lambda r, qs: fetcher._search_next_page_with_runtime(qs),
        ctx.download_queue,
        ctx.stats,
        ctx.stats_lock,
        ctx.update_status,
        ctx.downloaded_ids,
        max_pages=probe_pages,
    )


def _search_keyword_fallback_chain(
    fetcher, ctx: FetchContext, config, sort_mode: str, search_count: int
):
    """关键词回退链段：推荐流 → 排行榜。

    仅在主搜索零命中且未停止、且为高收藏热门模式时触发。

    Returns:
        - ``int``: 兜底命中后返回正计数，调用方应直接返回。
        - ``None``: 未触发或兜底未命中，调用方沿用 ``search_count``。
    """
    if not (
        search_count == 0
        and not fetcher._should_stop()
        and _is_popular_high_quality_mode(config, sort_mode)
    ):
        return None

    fetcher.log("[!] 关键词热门流无命中，自动切换到推荐流兜底")
    rec_count = fetcher._fetch_recommended_by_keyword(
        config.keyword,
        ctx.download_queue,
        ctx.stats,
        ctx.stats_lock,
        ctx.update_status,
        ctx.downloaded_ids,
    )
    if rec_count > 0:
        fetcher.log(f"[*] 推荐流兜底命中: {rec_count}")
        return rec_count

    fetcher.log("[!] 推荐流仍无命中，自动切换到排行榜关键词兜底")
    rank_count = fetcher._fetch_keyword_from_ranking(
        config.keyword,
        ctx.download_queue,
        ctx.stats,
        ctx.stats_lock,
        ctx.update_status,
        ctx.downloaded_ids,
    )
    if rank_count > 0:
        fetcher.log(f"[*] 排行榜关键词兜底命中: {rank_count}")
        return rank_count

    return None


def _search(fetcher, ctx: FetchContext) -> int:
    """搜索模式策略编排器。

    结构（R6-3 重构）：原 D(23) 单体函数拆为三段模块级 helper ——
    ``_search_web_popular_fallback``（网页热门兜底）、``_search_main_query``
    （App API 主搜索）、``_search_keyword_fallback_chain``（推荐/排行榜回退）。
    共用判定 ``_is_popular_high_quality_mode`` 消除魔法表达式重复。
    主函数降为编排器，依次尝试三段并在命中时短路返回。
    """
    config = fetcher.config
    if not config.keyword:
        fetcher.log("[!] 请输入搜索关键词")
        return 0

    sort_mode = config.search_sort or "popular_desc"
    fetcher._search_runtime = None

    web_count = _search_web_popular_fallback(fetcher, ctx, config, sort_mode)
    if web_count is not None:
        return web_count

    search_count = _search_main_query(fetcher, ctx, config, sort_mode)

    fallback_count = _search_keyword_fallback_chain(fetcher, ctx, config, sort_mode, search_count)
    if fallback_count is not None:
        return fallback_count

    return search_count


def _related(fetcher, ctx: FetchContext) -> int:
    seed_id = str(fetcher.config.related_illust_id or "").strip()
    if not seed_id.isdigit():
        fetcher.log("[!] 请输入有效的作品ID")
        return 0
    fetcher.log(f"[*] 基于作品相似推荐: illust_id={seed_id}")
    return fetcher._fetch_and_queue(
        lambda: fetcher.api.illust_related(seed_id),
        lambda r, qs: fetcher.api.illust_related(**qs),
        ctx.download_queue,
        ctx.stats,
        ctx.stats_lock,
        ctx.update_status,
        ctx.downloaded_ids,
    )


def _recommended(fetcher, ctx: FetchContext) -> int:
    fetcher.log("[*] 获取个性化推荐作品")
    return fetcher._fetch_and_queue(
        lambda: fetcher.api.illust_recommended(),
        lambda r, qs: fetcher.api.illust_recommended(**qs),
        ctx.download_queue,
        ctx.stats,
        ctx.stats_lock,
        ctx.update_status,
        ctx.downloaded_ids,
    )


def _user(fetcher, ctx: FetchContext) -> int:
    config = fetcher.config
    if not config.user_ids:
        fetcher.log("[!] 请输入用户ID")
        return 0
    fetcher.log(f"[*] 共 {len(config.user_ids)} 个用户，开始下载...")
    total = 0
    max_total = config.max_count
    skipped_users = []

    for i, user_id in enumerate(config.user_ids):
        if fetcher._should_stop():
            break
        if total >= max_total:
            fetcher.log(f"[*] 已达到下载数量限制 {max_total}，停止获取")
            break
        fetcher.log(f"[*] [{i + 1}/{len(config.user_ids)}] 获取用户 {user_id} 的作品")
        config.max_count = max_total - total
        count = fetcher._fetch_and_queue(
            lambda uid=user_id: fetcher.api.user_illusts(uid),
            lambda r, qs: fetcher.api.user_illusts(**qs),
            ctx.download_queue,
            ctx.stats,
            ctx.stats_lock,
            ctx.update_status,
            ctx.downloaded_ids,
        )
        if count == 0:
            skipped_users.append(user_id)
        total += count

        if i < len(config.user_ids) - 1:
            delay = random.uniform(10, 20)
            fetcher.log(f"[*] 等待 {delay:.1f} 秒后获取下一个用户...")
            fetcher._wait_interruptible(delay)

    config.max_count = max_total
    if skipped_users:
        fetcher._save_skipped_users(skipped_users)
    return total


def _ranking(fetcher, ctx: FetchContext) -> int:
    config = fetcher.config
    date_start = config.rank_date.strip() if config.rank_date else None
    date_end = config.rank_date_end.strip() if config.rank_date_end else None
    if date_start and date_end:
        return fetcher._fetch_ranking_range(
            date_start,
            date_end,
            ctx.download_queue,
            ctx.stats,
            ctx.stats_lock,
            ctx.update_status,
            ctx.downloaded_ids,
        )

    if date_start:
        fetcher.log(f"[*] 获取排行榜: {config.rank_mode} (日期: {date_start})")
    else:
        fetcher.log(f"[*] 获取排行榜: {config.rank_mode} (今天)")
    return fetcher._fetch_and_queue(
        lambda: (
            fetcher.api.illust_ranking(mode=config.rank_mode, date=date_start)
            if date_start
            else fetcher.api.illust_ranking(mode=config.rank_mode)
        ),
        lambda r, qs: fetcher.api.illust_ranking(**qs),
        ctx.download_queue,
        ctx.stats,
        ctx.stats_lock,
        ctx.update_status,
        ctx.downloaded_ids,
    )


def _follow(fetcher, ctx: FetchContext) -> int:
    fetcher.log("[*] 获取关注用户新作")
    return fetcher._fetch_and_queue(
        lambda: fetcher.api.illust_follow(),
        lambda r, qs: fetcher.api.illust_follow(**qs),
        ctx.download_queue,
        ctx.stats,
        ctx.stats_lock,
        ctx.update_status,
        ctx.downloaded_ids,
    )


def _follow_all(fetcher, ctx: FetchContext) -> int:
    return fetcher._fetch_follow_all(
        ctx.download_queue,
        ctx.stats,
        ctx.stats_lock,
        ctx.update_status,
        ctx.downloaded_ids,
    )


MODE_STRATEGIES: dict[str, Callable[[object, FetchContext], int]] = {
    "search": _search,
    "related": _related,
    "recommended": _recommended,
    "user": _user,
    "ranking": _ranking,
    "follow": _follow,
    "follow_all": _follow_all,
}


__all__ = ["FetchContext", "MODE_STRATEGIES"]
