#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pixiv 获取器搜索策略与关键词兜底逻辑。"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import quote

from core.http_client import request_get

CHARACTER_ALIASES = {
    "初音未来": ["初音未来", "初音ミク", "miku", "hatsune miku", "hatsunemiku"],
    "洛天依": ["洛天依", "luo tianyi", "luotianyi", "vsinger 洛天依"],
}


class PixivFetcherSearchMixin:
    """关键词搜索策略与多层数据源兜底（网页热门 → 日榜 → 推荐流）。

    定义取消信号 ``_should_stop`` 与搜索运行时状态 ``_search_runtime``，
    被其他 fetcher mixin 共享。提供关键词别名匹配用于兜底召回。

    Host contract（由 ``PixivFetcher`` 宿主及其他 mixin 提供）:
        - ``self.config`` — ``FetchConfig``，读取 ``min_bookmarks``/``max_count``/
          ``api_delay``/``user_agent``/``auth_cookie``
        - ``self.api`` — ``AppPixivAPI``，调用 ``search_illust``/``illust_ranking``/
          ``illust_recommended``/``parse_qs``
        - ``self.log(msg)`` — 日志回调
        - ``self.stop_event`` — ``threading.Event``，``_should_stop`` 读取
        - ``self.api_counter`` — ``APICallCounter``，每次 API 调用后 ``increment()``
        - ``self._fetch_and_queue(...)`` — 生产者入队（来自 ``QueueMixin``）
        - ``self._api_sleep()`` — API 间隔（来自 ``QueueMixin``）
        - 实例状态：``self._search_runtime``（dict）/``self._disable_web_popular``（bool）
          由本 mixin 写入，供 ``RankingMixin`` 与网页热门兜底读取
    """

    def _should_stop(self):
        """检查是否应该停止"""
        return self.stop_event and self.stop_event.is_set()

    def _search_illust_with_sort(self, keyword, sort_mode):
        """关键词搜索：优先热门 + 精确标签，失败自动降级，并记录本次搜索策略用于翻页。"""
        plan = [
            (sort_mode, "exact_match_for_tags"),
            (sort_mode, "partial_match_for_tags"),
        ]
        if sort_mode == "popular_desc":
            plan.extend(
                [
                    ("date_desc", "exact_match_for_tags"),
                    ("date_desc", "partial_match_for_tags"),
                ]
            )

        last_error = None
        for sort_value, target in plan:
            try:
                result = self.api.search_illust(
                    keyword,
                    search_target=target,
                    sort=sort_value,
                )
                self._search_runtime = {
                    "keyword": keyword,
                    "sort": sort_value,
                    "search_target": target,
                }
                if sort_value != sort_mode:
                    self.log(f"[!] 热度排序不可用，已切换 {sort_value}")
                self.log(f"[*] 搜索策略: {target} + {sort_value}")
                self._log_search_preview(result)
                return result
            except Exception as e:
                last_error = e
                self.log(f"[!] 搜索策略失败({target}+{sort_value}): {e}")

        if last_error:
            raise last_error
        return None

    def _search_next_page_with_runtime(self, next_qs):
        """搜索翻页时强制沿用首屏选定的排序与匹配参数，避免接口回退到默认排序。"""
        params = dict(next_qs or {})
        runtime = self._search_runtime or {}

        # 保底 keyword：不同版本接口可能用 word/keyword。
        keyword = runtime.get("keyword")
        if keyword:
            params["word"] = keyword

        if runtime.get("sort"):
            params["sort"] = runtime["sort"]
        if runtime.get("search_target"):
            params["search_target"] = runtime["search_target"]

        return self.api.search_illust(**params)

    def _is_popular_preview_below_threshold(self, result):
        """判断热门首屏收藏数是否整体低于当前阈值。"""
        threshold = int(self.config.min_bookmarks or 0)
        if threshold <= 0:
            return False
        try:
            if not result or not hasattr(result, "illusts") or not result.illusts:
                return True
            values = []
            for illust in result.illusts[:5]:
                values.append(getattr(illust, "total_bookmarks", 0) or 0)
            return bool(values) and max(values) < threshold
        except Exception:
            return False

    def _fetch_search_web_popular(
        self,
        keyword,
        download_queue,
        stats,
        stats_lock,
        update_status,
        downloaded_ids,
        max_probe_pages=None,
    ):
        """使用网页 Ajax 热门排序获取作品，再复用现有下载队列逻辑。"""
        page = 1
        queued_total = 0
        fetched = 0

        while fetched < self.config.max_count and not self._should_stop():
            try:
                result = self._web_popular_page_to_result(keyword, page)
            except Exception as e:
                self.log(f"[!] 网页热门搜索失败(p={page}): {e}")
                # Windows/OpenSSL 环境下常见 EOF，直接停用网页分支，避免反复报错。
                if "SSL" in str(e) or "EOF" in str(e):
                    self._disable_web_popular = True
                    self.log(
                        "[!] 检测到 SSL EOF，已停用网页热门兜底，后续直接走 App/推荐/排行榜兜底"
                    )
                break

            if not result or not getattr(result, "illusts", None):
                if page == 1:
                    self.log("[!] 网页热门搜索返回空数据")
                break

            # 首屏若最高收藏都小于阈值，判定热门权限不可用，立即切换下个兜底。
            if page == 1 and int(self.config.min_bookmarks or 0) > 0:
                preview = getattr(result, "preview_bookmarks", None) or []
                if preview and max(preview) < int(self.config.min_bookmarks or 0):
                    self.log("[!] 网页热门首屏最高收藏低于阈值，立即切换 App/推荐/排行榜兜底")
                    return 0

            # 复用当前筛选与入队流程：单页处理，避免重写下载逻辑。
            remain = max(1, self.config.max_count - fetched)
            old_limit = self.config.max_count
            self.config.max_count = remain
            queued_now = self._fetch_and_queue(
                lambda r=result: r,
                lambda r, qs: None,
                download_queue,
                stats,
                stats_lock,
                update_status,
                downloaded_ids,
            )
            self.config.max_count = old_limit

            queued_total += queued_now
            fetched += len(result.illusts)

            # 高频场景只做短探测，防止长时间卡在网页分支。
            if max_probe_pages is not None and page >= max_probe_pages and queued_total == 0:
                self.log(f"[!] 网页热门探测 {max_probe_pages} 页无命中，立即切换下个兜底")
                return 0

            if not getattr(result, "next_url", None):
                break

            page += 1
            self._api_sleep()

        if queued_total > 0:
            self.log(f"[*] 网页热门排序入队完成: {queued_total}")
        return queued_total

    def _fetch_keyword_from_ranking(
        self, keyword, download_queue, stats, stats_lock, update_status, downloaded_ids
    ):
        """当搜索热门不可用时，用近期日榜数据按关键词匹配兜底。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return 0
        aliases = self._build_keyword_aliases(keyword)
        self.log(f"[*] 排行榜兜底关键词组: {', '.join(aliases[:6])}")

        total_queued = 0
        max_target = self.config.max_count
        # 向前回溯 30 天，通常足够覆盖高热作品池。
        for i in range(30):
            if self._should_stop():
                break
            day = (datetime.now() - timedelta(days=i + 1)).strftime("%Y-%m-%d")
            try:
                result = self.api.illust_ranking(mode="day", date=day)
                self.api_counter.increment()
            except Exception as e:
                self.log(f"[!] 读取日榜失败({day}): {e}")
                continue

            if not result or not hasattr(result, "illusts") or not result.illusts:
                continue

            matched = []
            for illust in result.illusts:
                if self._match_keyword_aliases(illust, aliases):
                    matched.append(illust)

            if not matched:
                continue

            self.log(f"[*] 日榜兜底命中({day}): {len(matched)}")

            remain = max(1, self.config.max_count - total_queued)
            old_limit = self.config.max_count
            self.config.max_count = remain
            queued = self._fetch_and_queue(
                lambda m=matched: SimpleNamespace(illusts=m, next_url=None),
                lambda r, qs: None,
                download_queue,
                stats,
                stats_lock,
                update_status,
                downloaded_ids,
            )
            self.config.max_count = old_limit
            total_queued += queued

            if total_queued >= max_target:
                break

            self._api_sleep()

        return total_queued

    def _fetch_recommended_by_keyword(
        self, keyword, download_queue, stats, stats_lock, update_status, downloaded_ids
    ):
        """推荐流兜底：先按关键词同义词过滤，再进入统一下载链路。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return 0

        aliases = self._build_keyword_aliases(keyword)
        total_queued = 0
        result = None

        try:
            result = self.api.illust_recommended()
            self.api_counter.increment()
        except Exception as e:
            self.log(f"[!] 推荐流请求失败: {e}")
            return 0

        while (
            result
            and getattr(result, "illusts", None)
            and not self._should_stop()
            and total_queued < self.config.max_count
        ):
            matched = [ill for ill in result.illusts if self._match_keyword_aliases(ill, aliases)]
            if matched:
                self.log(f"[*] 推荐流关键词命中: {len(matched)}")
                remain = max(1, self.config.max_count - total_queued)
                old_limit = self.config.max_count
                self.config.max_count = remain
                queued = self._fetch_and_queue(
                    lambda m=matched: SimpleNamespace(illusts=m, next_url=None),
                    lambda r, qs: None,
                    download_queue,
                    stats,
                    stats_lock,
                    update_status,
                    downloaded_ids,
                )
                self.config.max_count = old_limit
                total_queued += queued

            if total_queued >= self.config.max_count:
                break

            if not getattr(result, "next_url", None):
                break
            next_qs = self.api.parse_qs(result.next_url)
            if not next_qs:
                break
            try:
                result = self.api.illust_recommended(**next_qs)
                self.api_counter.increment()
            except Exception as e:
                self.log(f"[!] 推荐流翻页失败: {e}")
                break
            self._api_sleep()

        return total_queued

    def _build_keyword_aliases(self, keyword):
        """根据输入关键词扩展同义词组，用于角色身份匹配。"""
        raw = (keyword or "").strip().lower()
        if not raw:
            return []

        # 直接命中预定义角色组
        for _, aliases in CHARACTER_ALIASES.items():
            norm_aliases = [a.lower() for a in aliases]
            if raw in norm_aliases:
                return norm_aliases

        # 关键词包含角色名时也扩展组
        for _, aliases in CHARACTER_ALIASES.items():
            norm_aliases = [a.lower() for a in aliases]
            for a in norm_aliases:
                if a in raw or raw in a:
                    return norm_aliases

        return [raw]

    def _match_keyword_aliases(self, illust, aliases):
        """标题或标签匹配同义词组；优先看标签，其次看标题。"""
        if not aliases:
            return False
        try:
            title = (getattr(illust, "title", "") or "").lower()

            tags = getattr(illust, "tags", None)
            tag_names = []
            if tags:
                if isinstance(tags, (list, tuple)):
                    for t in tags:
                        name = ""
                        if isinstance(t, str):
                            name = t
                        else:
                            name = getattr(t, "name", "") or ""
                        if name:
                            tag_names.append(name.lower())
                else:
                    tag_list = getattr(tags, "tags", None) or []
                    for t in tag_list:
                        name = getattr(t, "name", "") or ""
                        if name:
                            tag_names.append(name.lower())

            # 标签完全匹配优先，角色识别更稳。
            for a in aliases:
                if a in tag_names:
                    return True

            # 标签包含匹配。
            for a in aliases:
                for t in tag_names:
                    if a in t:
                        return True

            # 标题包含匹配（长度门槛避免误判，如 "mi"）。
            for a in aliases:
                if len(a) >= 3 and a in title:
                    return True
        except Exception:
            return False
        return False

    def _web_popular_page_to_result(self, keyword, page):
        """请求网页热门页并转换为与 fetch 流程兼容的结果对象。"""
        ua = (
            self.config.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        encoded = quote(keyword)
        url = (
            f"https://www.pixiv.net/ajax/search/artworks/{encoded}"
            f"?word={encoded}&order=popular_d&mode=all&p={page}&s_mode=s_tag_full&type=all&lang=zh"
        )
        headers = {
            "Referer": f"https://www.pixiv.net/tags/{encoded}/artworks",
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cookie": self.config.auth_cookie,
        }

        resp = request_get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        body = data.get("body") or {}
        illust_manga = body.get("illustManga") or {}
        rows = illust_manga.get("data") or []

        illusts = []
        preview_bookmarks = []
        for row in rows:
            if self._should_stop():
                break
            illust_id = row.get("id")
            if not illust_id:
                continue
            bookmark_count = row.get("bookmarkCount", 0) or 0
            ai_type = row.get("aiType", 0) or 0
            preview_bookmarks.append(bookmark_count)
            user_id = row.get("userId")
            user_name = row.get("userName") or ""
            illusts.append(
                SimpleNamespace(
                    id=int(illust_id) if str(illust_id).isdigit() else illust_id,
                    title=row.get("title") or "",
                    total_bookmarks=bookmark_count,
                    illust_ai_type=ai_type,
                    tags=row.get("tags") or [],
                    user=SimpleNamespace(id=str(user_id) if user_id else "", name=user_name),
                    create_date=row.get("createDate"),
                )
            )

        sample = []
        for item in rows[:5]:
            sample.append(str(item.get("bookmarkCount", 0) or 0))
        if sample:
            self.log(f"[*] 网页热门首屏预览(top5): {', '.join(sample)}")

        has_next = bool(illust_manga.get("next")) and len(rows) > 0
        return SimpleNamespace(
            illusts=illusts,
            next_url=f"web_page:{page + 1}" if has_next else None,
            preview_bookmarks=preview_bookmarks,
        )
