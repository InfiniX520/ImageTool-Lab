"""Unit tests for search/ranking mixin pure-logic functions.

R6-7 覆盖率提升：聚焦 ``pixiv_fetcher_search_mixin`` 与
``pixiv_fetcher_ranking_mixin`` 中的纯逻辑分支（阈值判定 / 关键词别名 /
首屏预览 / 步长判定），避开需要真实 API 调用的入口。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from core.pixiv_fetcher import FetchConfig, PixivFetcher


def _make_fetcher(config: FetchConfig | None = None) -> PixivFetcher:
    """构造一个仅用于纯逻辑测试的 fetcher（api 不调用）。"""
    return PixivFetcher(
        api=object(),
        config=config or FetchConfig(),
        log_callback=lambda _msg: None,
        stop_event=threading.Event(),
    )


# ---------- _is_popular_preview_below_threshold ----------


def test_popular_preview_returns_false_when_threshold_zero() -> None:
    """未设阈值（min_bookmarks=0）时不参与预判，直接返回 False。"""
    fetcher = _make_fetcher(FetchConfig(min_bookmarks=0))
    result = SimpleNamespace(illusts=[SimpleNamespace(total_bookmarks=0)])
    assert fetcher._is_popular_preview_below_threshold(result) is False


def test_popular_preview_returns_true_when_empty_result() -> None:
    """空结果或无 illusts 字段时，有阈值即视为低于阈值。"""
    fetcher = _make_fetcher(FetchConfig(min_bookmarks=1000))
    assert fetcher._is_popular_preview_below_threshold(None) is True
    assert fetcher._is_popular_preview_below_threshold(SimpleNamespace(illusts=[])) is True


def test_popular_preview_compares_max_bookmark_against_threshold() -> None:
    """取首屏前 5 个作品的 max 收藏与阈值比较。"""
    fetcher = _make_fetcher(FetchConfig(min_bookmarks=1000))
    below = SimpleNamespace(
        illusts=[SimpleNamespace(total_bookmarks=v) for v in (10, 200, 50, 800, 999)]
    )
    above = SimpleNamespace(
        illusts=[SimpleNamespace(total_bookmarks=v) for v in (10, 200, 50, 800, 1500)]
    )
    assert fetcher._is_popular_preview_below_threshold(below) is True
    assert fetcher._is_popular_preview_below_threshold(above) is False


def test_popular_preview_tolerates_missing_bookmark_attr() -> None:
    """illust 缺 total_bookmarks 属性时按 0 处理，不抛异常。"""
    fetcher = _make_fetcher(FetchConfig(min_bookmarks=100))
    result = SimpleNamespace(illusts=[SimpleNamespace(), SimpleNamespace()])
    assert fetcher._is_popular_preview_below_threshold(result) is True


# ---------- _build_keyword_aliases ----------


def test_build_aliases_matches_predefined_character() -> None:
    """预定义角色组任意别名命中后返回整组（小写化）。"""
    fetcher = _make_fetcher()
    aliases = fetcher._build_keyword_aliases("miku")
    assert "初音未来" in aliases
    assert "miku" in aliases
    assert all(isinstance(a, str) for a in aliases)


def test_build_aliases_returns_lowercase_for_unknown_keyword() -> None:
    """未命中预定义组时返回 [keyword.lower()]。"""
    fetcher = _make_fetcher()
    assert fetcher._build_keyword_aliases("UnknownTag") == ["unknowntag"]
    assert fetcher._build_keyword_aliases("") == []
    assert fetcher._build_keyword_aliases(None) == []


def test_build_aliases_expands_when_keyword_contains_character_name() -> None:
    """关键词包含预定义角色名时也扩展整组（如 "初音未来 wall"）。"""
    fetcher = _make_fetcher()
    aliases = fetcher._build_keyword_aliases("初音未来 wall")
    assert "初音未来" in aliases


# ---------- _match_keyword_aliases ----------


def test_match_aliases_returns_false_for_empty_aliases() -> None:
    fetcher = _make_fetcher()
    assert fetcher._match_keyword_aliases(SimpleNamespace(title="x", tags=[]), []) is False


def test_match_aliases_matches_tag_string_list_exact() -> None:
    """标签为字符串列表时按完全匹配。"""
    fetcher = _make_fetcher()
    illust = SimpleNamespace(title="artwork", tags=["初音未来", "vocaloid"])
    assert fetcher._match_keyword_aliases(illust, ["初音未来", "miku"]) is True
    assert fetcher._match_keyword_aliases(illust, ["miku"]) is False


def test_match_aliases_matches_tag_object_list_with_name_attr() -> None:
    """标签为对象列表（pixivpy3 风格）时取 .name 属性。"""
    fetcher = _make_fetcher()
    illust = SimpleNamespace(
        title="artwork",
        tags=[SimpleNamespace(name="初音ミク"), SimpleNamespace(name="vocaloid")],
    )
    assert fetcher._match_keyword_aliases(illust, ["初音ミク"]) is True


def test_match_aliases_matches_tag_object_with_tags_attribute() -> None:
    """标签对象有 .tags 子列表时递归取 name。"""
    fetcher = _make_fetcher()
    illust = SimpleNamespace(
        title="artwork",
        tags=SimpleNamespace(tags=[SimpleNamespace(name="洛天依")]),
    )
    assert fetcher._match_keyword_aliases(illust, ["洛天依"]) is True


def test_match_aliases_falls_back_to_title_substring() -> None:
    """标签未命中时回退到标题子串匹配（长度 ≥3 才参与）。"""
    fetcher = _make_fetcher()
    illust = SimpleNamespace(title="初音未来的新作品", tags=[])
    assert fetcher._match_keyword_aliases(illust, ["初音未来"]) is True
    # 短别名（<3）不应触发标题匹配，避免 "mi" 误判
    short = SimpleNamespace(title="mission impossible", tags=[])
    assert fetcher._match_keyword_aliases(short, ["mi"]) is False


def test_match_aliases_returns_false_on_exception() -> None:
    """illust 字段读取异常时返回 False，不向上抛。"""
    fetcher = _make_fetcher()

    class _Bad:
        @property
        def title(self):
            raise RuntimeError("boom")

        tags = []

    assert fetcher._match_keyword_aliases(_Bad(), ["miku"]) is False


# ---------- _get_ranking_step ----------


def test_get_ranking_step_returns_month_for_month_modes() -> None:
    fetcher = _make_fetcher(FetchConfig(rank_mode="month"))
    assert fetcher._get_ranking_step() == "month"
    fetcher.config.rank_mode = "month_manga"
    assert fetcher._get_ranking_step() == "month"


def test_get_ranking_step_returns_week_for_week_modes() -> None:
    fetcher = _make_fetcher(FetchConfig(rank_mode="week"))
    assert fetcher._get_ranking_step() == "week"
    fetcher.config.rank_mode = "week_manga"
    assert fetcher._get_ranking_step() == "week"


def test_get_ranking_step_returns_day_for_other_modes() -> None:
    fetcher = _make_fetcher(FetchConfig(rank_mode="day"))
    assert fetcher._get_ranking_step() == "day"
    fetcher.config.rank_mode = "day_male"
    assert fetcher._get_ranking_step() == "day"
    fetcher.config.rank_mode = "rookie"
    assert fetcher._get_ranking_step() == "day"


# ---------- _log_search_preview ----------


def test_log_search_preview_handles_empty_result_without_raising() -> None:
    """空结果或无 illusts 字段时记录 "首屏为空"，不抛异常。"""
    logs: list[str] = []
    fetcher = PixivFetcher(
        api=object(),
        config=FetchConfig(min_bookmarks=0),
        log_callback=logs.append,
        stop_event=threading.Event(),
    )
    fetcher._search_runtime = {}
    fetcher._log_search_preview(None)
    fetcher._log_search_preview(SimpleNamespace(illusts=[]))
    assert any("首屏为空" in message for message in logs)


def test_log_search_preview_warns_when_top_max_below_threshold() -> None:
    """popular_desc + 阈值 > 0 且首屏 max < 阈值时输出建议。"""
    logs: list[str] = []
    fetcher = PixivFetcher(
        api=object(),
        config=FetchConfig(min_bookmarks=1000, search_sort="popular_desc"),
        log_callback=logs.append,
        stop_event=threading.Event(),
    )
    fetcher._search_runtime = {"sort": "popular_desc"}
    result = SimpleNamespace(
        illusts=[SimpleNamespace(total_bookmarks=v) for v in (10, 200, 50, 800, 999)]
    )
    fetcher._log_search_preview(result)
    assert any("低于你的收藏阈值" in message for message in logs)


def test_log_search_preview_silent_when_threshold_zero() -> None:
    """阈值=0 时不输出建议，仅输出预览。"""
    logs: list[str] = []
    fetcher = PixivFetcher(
        api=object(),
        config=FetchConfig(min_bookmarks=0, search_sort="popular_desc"),
        log_callback=logs.append,
        stop_event=threading.Event(),
    )
    fetcher._search_runtime = {"sort": "popular_desc"}
    result = SimpleNamespace(
        illusts=[SimpleNamespace(total_bookmarks=v) for v in (10, 200, 50)]
    )
    fetcher._log_search_preview(result)
    assert any("首屏收藏预览" in message for message in logs)
    assert not any("低于你的收藏阈值" in message for message in logs)
