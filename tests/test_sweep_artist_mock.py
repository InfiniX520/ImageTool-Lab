"""Mock-based unit tests for the R7-1 ``_sweep_artist`` refactor.

覆盖 ``_sweep_artist`` 5 个核心分支 + ``_find_sweep_cutoff`` /
``_advance_sweep_cursor`` 单元逻辑。所有测试使用 ``_FakeApi`` + 临时
``FollowCrawlStateRepository`` 直接驱动 ``_sweep_artist`` 方法。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.follow_crawl_service import (
    SWEEP_MAX_PAGES,
    FollowCrawlOptions,
    FollowCrawlService,
    _BatchProgress,
    _find_sweep_cutoff,
)
from core.follow_crawl_state import (
    ArtistProgress,
    FollowArtistSnapshot,
    FollowCrawlRun,
    FollowCrawlStateRepository,
)
from core.pixiv_api_policy import PixivApiRequestExecutor


# ============================================================
# Fakes
# ============================================================


class _FakeApi:
    """模拟 pixivpy3 API；支持按画师 ID 配置多页响应。

    ``pages_by_artist``: ``{artist_id: [[illust_id, ...], [illust_id, ...]]}``。
    每个内层 list 代表一页 illusts；非末页自动生成 ``?offset=N`` next_url。
    """

    user_id = "42"

    def __init__(self, pages_by_artist: dict[str, list[list]]):
        self.pages_by_artist = pages_by_artist
        self.user_calls: list[str] = []

    def user_illusts(self, user_id=None, **kwargs):
        artist_id = str(user_id or kwargs.get("user_id"))
        self.user_calls.append(artist_id)
        pages = self.pages_by_artist.get(artist_id, [])
        call_idx = self.user_calls.count(artist_id) - 1
        if not pages or call_idx >= len(pages):
            return SimpleNamespace(illusts=[], next_url=None)
        illusts = [SimpleNamespace(id=v) for v in pages[call_idx]]
        next_url = (
            f"?offset={call_idx + 1}" if call_idx < len(pages) - 1 else None
        )
        return SimpleNamespace(illusts=illusts, next_url=next_url)

    @staticmethod
    def parse_qs(url):
        if not url or "offset=" not in url:
            return {}
        offset = url.split("offset=")[-1].split("&")[0]
        return {"offset": offset}


def _executor(budget: int = 100) -> PixivApiRequestExecutor:
    return PixivApiRequestExecutor(
        request_budget=budget,
        min_interval=0,
        wait=lambda _s: True,
        should_stop=lambda: False,
        log=lambda _m: None,
        jitter=0,
        clock=lambda: 0,
        random_uniform=lambda _l, _h: 0,
    )


def _make_service(
    api: _FakeApi,
    repository: FollowCrawlStateRepository,
    *,
    max_queued: int = 100,
    processed: list | None = None,
) -> FollowCrawlService:
    if processed is None:
        processed = []

    def process(illust, _fetched):
        processed.append(illust.id)
        return SimpleNamespace(queued=1)

    return FollowCrawlService(
        api=api,
        repository=repository,
        request_executor=_executor(),
        options=FollowCrawlOptions(
            start_mode="resume",
            max_artists_per_batch=30,
            max_queued_items=max_queued,
        ),
        process_illust=process,
        should_stop=lambda: False,
        log=lambda _m: None,
    )


def _seed_run(
    repository: FollowCrawlStateRepository,
    *,
    artist_id: str = "100",
) -> FollowCrawlRun:
    """用 ``create_run`` 初始化一个 run + 一个 artist 行。"""
    return repository.create_run(
        account_id="42",
        config_key="cfg",
        artists=[FollowArtistSnapshot(artist_id=artist_id, artist_name=f"Artist {artist_id}")],
        start_mode="resume",
    )


def _make_artist(
    artist_id: str = "100",
    *,
    newest_illust_id: str = "",
    state: str = "completed",
    run_id: str = "run-1",
) -> ArtistProgress:
    """直接构造 ArtistProgress（frozen dataclass）用于 _sweep_artist 测试。"""
    return ArtistProgress(
        run_id=run_id,
        artist_id=artist_id,
        artist_name=f"Artist {artist_id}",
        ordinal=1,
        state=state,
        cursor={},
        item_index=0,
        last_illust_id="",
        attempts=0,
        works_seen=0,
        queued_items=0,
        last_error_kind="",
        last_error_message="",
        retry_at="",
        updated_at="",
        newest_illust_id=newest_illust_id,
        swept_at="",
    )


# ============================================================
# _find_sweep_cutoff 单元测试
# ============================================================


def test_find_sweep_cutoff_returns_len_when_no_cutoff() -> None:
    """无 cutoff（legacy 行）时返回 len(illusts)，即整页都要处理。"""
    illusts = [SimpleNamespace(id="1"), SimpleNamespace(id="2"), SimpleNamespace(id="3")]
    assert _find_sweep_cutoff(illusts, "") == 3
    assert _find_sweep_cutoff(illusts, None) == 3


def test_find_sweep_cutoff_returns_index_when_cutoff_matched() -> None:
    """cutoff 命中时返回其下标，调用方仅处理 [0, index)。"""
    illusts = [SimpleNamespace(id=v) for v in ("10", "9", "8", "7", "6")]
    assert _find_sweep_cutoff(illusts, "8") == 2
    assert _find_sweep_cutoff(illusts, "10") == 0  # 首个即 cutoff


def test_find_sweep_cutoff_returns_len_when_cutoff_not_matched() -> None:
    """cutoff 未匹配时返回 len(illusts)，调用方处理整页并尝试翻页。"""
    illusts = [SimpleNamespace(id=v) for v in ("10", "9", "8")]
    assert _find_sweep_cutoff(illusts, "999") == 3


def test_find_sweep_cutoff_handles_empty_illusts() -> None:
    """空 illusts 列表：无 cutoff 返回 0，有 cutoff 也返回 0。"""
    assert _find_sweep_cutoff([], "") == 0
    assert _find_sweep_cutoff([], "anything") == 0


def test_find_sweep_cutoff_tolerates_missing_id_attr() -> None:
    """illust 缺 id 属性时按 "" 处理，不抛异常。"""
    illusts = [SimpleNamespace(), SimpleNamespace(id="9")]
    assert _find_sweep_cutoff(illusts, "9") == 1


# ============================================================
# _sweep_artist 集成测试 - 5 个核心分支
# ============================================================


def test_sweep_artist_branch_empty_response(tmp_path: Path) -> None:
    """分支1：API 返回空 illusts，立即终止，不调用 process_illust。"""
    api = _FakeApi({"100": [[]]})  # 单页空响应
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    run = _seed_run(repository, artist_id="100")
    processed: list = []
    service = _make_service(api, repository, processed=processed)
    artist = _make_artist("100", newest_illust_id="", run_id=run.run_id)

    batch = _BatchProgress()
    queued = service._sweep_artist(run, artist, batch)

    assert queued == 0
    assert processed == []
    assert api.user_calls == ["100"]
    repository.close()


def test_sweep_artist_branch_cutoff_matched(tmp_path: Path) -> None:
    """分支2：cutoff 在首页命中，处理 [0, stop_at) 后终止，不翻页。

    数据：page1=[10, 9, 8, 7, 6]，cutoff="8"。
    预期：处理 [10, 9]（stop_at=2），不翻页，queued=2。
    """
    api = _FakeApi({"100": [[10, 9, 8, 7, 6]]})
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    run = _seed_run(repository, artist_id="100")
    processed: list = []
    service = _make_service(api, repository, processed=processed)
    artist = _make_artist("100", newest_illust_id="8", run_id=run.run_id)

    batch = _BatchProgress()
    queued = service._sweep_artist(run, artist, batch)

    assert queued == 2
    assert processed == [10, 9]  # 仅处理 cutoff 之前的
    assert api.user_calls == ["100"]  # 未翻页
    repository.close()


def test_sweep_artist_branch_no_cutoff_legacy(tmp_path: Path) -> None:
    """分支3：legacy 行无 cutoff（newest_illust_id=""），仅探测首页后终止。

    数据：page1=[10, 9, 8]，无 cutoff。
    预期：处理整页 [10, 9, 8]，_advance_sweep_cursor 因 cutoff="" 返回 False，终止。
    """
    api = _FakeApi({"100": [[10, 9, 8]]})
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    run = _seed_run(repository, artist_id="100")
    processed: list = []
    service = _make_service(api, repository, processed=processed)
    artist = _make_artist("100", newest_illust_id="", run_id=run.run_id)

    batch = _BatchProgress()
    queued = service._sweep_artist(run, artist, batch)

    assert queued == 3
    assert processed == [10, 9, 8]
    assert api.user_calls == ["100"]  # 仅首页
    repository.close()


def test_sweep_artist_branch_multipage_with_cutoff(tmp_path: Path) -> None:
    """分支4：多页翻页 + cutoff 在后续页命中。

    数据：page1=[10, 9, 8] next_url=offset=1，page2=[7, 6, 5, 4]，cutoff="5"。
    预期：page1 处理 [10,9,8]，翻页；page2 处理 [7,6]（stop_at=2），终止。queued=5。
    """
    api = _FakeApi({"100": [[10, 9, 8], [7, 6, 5, 4]]})
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    run = _seed_run(repository, artist_id="100")
    processed: list = []
    service = _make_service(api, repository, processed=processed)
    artist = _make_artist("100", newest_illust_id="5", run_id=run.run_id)

    batch = _BatchProgress()
    queued = service._sweep_artist(run, artist, batch)

    assert queued == 5
    assert processed == [10, 9, 8, 7, 6]
    assert api.user_calls == ["100", "100"]  # 翻页一次
    repository.close()


def test_sweep_artist_branch_cutoff_not_matched_no_next_url(tmp_path: Path) -> None:
    """分支5：cutoff 未匹配 + 无 next_url，处理整页后终止。

    数据：page1=[10, 9, 8]，cutoff="999"（不存在），无 next_url。
    预期：处理 [10, 9, 8]，_advance_sweep_cursor 因 next_url=None 返回 False。
    """
    api = _FakeApi({"100": [[10, 9, 8]]})  # 单页，无 next_url
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    run = _seed_run(repository, artist_id="100")
    processed: list = []
    service = _make_service(api, repository, processed=processed)
    artist = _make_artist("100", newest_illust_id="999", run_id=run.run_id)

    batch = _BatchProgress()
    queued = service._sweep_artist(run, artist, batch)

    assert queued == 3
    assert processed == [10, 9, 8]
    assert api.user_calls == ["100"]
    repository.close()


def test_sweep_artist_records_newest_seen(tmp_path: Path) -> None:
    """``newest_seen`` 取首页第一个作品的 id（即使 cutoff 命中）。

    数据：page1=[10, 9, 8, 7, 6]，cutoff="8"。
    预期：mark_artist_swept 被调用时 newest_illust_id="10"。
    """
    api = _FakeApi({"100": [[10, 9, 8, 7, 6]]})
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    run = _seed_run(repository, artist_id="100")
    processed: list = []
    service = _make_service(api, repository, processed=processed)

    swept_calls: list[dict] = []
    original_mark = repository.mark_artist_swept

    def spy_mark(run_id, artist_id, *, queued_delta, newest_illust_id):
        swept_calls.append(
            {
                "run_id": run_id,
                "artist_id": artist_id,
                "queued_delta": queued_delta,
                "newest_illust_id": newest_illust_id,
            }
        )
        return original_mark(
            run_id, artist_id, queued_delta=queued_delta, newest_illust_id=newest_illust_id
        )

    repository.mark_artist_swept = spy_mark

    artist = _make_artist("100", newest_illust_id="8", run_id=run.run_id)
    batch = _BatchProgress()
    service._sweep_artist(run, artist, batch)

    assert len(swept_calls) == 1
    assert swept_calls[0]["newest_illust_id"] == "10"
    assert swept_calls[0]["queued_delta"] == 2
    repository.close()


def test_sweep_artist_respects_sweep_max_pages_cap(tmp_path: Path) -> None:
    """``SWEEP_MAX_PAGES`` 上限：cutoff 永不命中时最多翻 SWEEP_MAX_PAGES 页。"""
    pages = [[int(f"{p}{i}") for i in range(3)] for p in range(SWEEP_MAX_PAGES + 2)]
    api = _FakeApi({"100": pages})
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    run = _seed_run(repository, artist_id="100")
    processed: list = []
    service = _make_service(api, repository, processed=processed)
    artist = _make_artist("100", newest_illust_id="never-exists", run_id=run.run_id)

    batch = _BatchProgress()
    service._sweep_artist(run, artist, batch)

    assert len(api.user_calls) == SWEEP_MAX_PAGES
    repository.close()


# ============================================================
# _advance_sweep_cursor 单元测试
# ============================================================


def test_advance_cursor_returns_false_when_cutoff_matched_in_page(tmp_path: Path) -> None:
    """cutoff 命中且 stop_at<len(illusts) → (False, {})：已抵达上次覆盖范围。"""
    api = _FakeApi({"100": [[10, 9, 8, 7, 6]]})
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    _seed_run(repository, artist_id="100")
    service = _make_service(api, repository)
    result = SimpleNamespace(
        illusts=[SimpleNamespace(id=v) for v in (10, 9, 8)], next_url="?offset=1"
    )
    advanced, cursor = service._advance_sweep_cursor(result, "8", 2, result.illusts)
    assert advanced is False
    assert cursor == {}
    repository.close()


def test_advance_cursor_returns_false_when_no_cutoff(tmp_path: Path) -> None:
    """cutoff="" → (False, {})：legacy 行仅探测首页。"""
    api = _FakeApi({"100": [[10, 9]]})
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    _seed_run(repository, artist_id="100")
    service = _make_service(api, repository)
    result = SimpleNamespace(illusts=[SimpleNamespace(id=10)], next_url="?offset=1")
    advanced, cursor = service._advance_sweep_cursor(result, "", 1, result.illusts)
    assert advanced is False
    assert cursor == {}
    repository.close()


def test_advance_cursor_returns_false_when_no_next_url(tmp_path: Path) -> None:
    """cutoff 未命中 + next_url=None → (False, {})：最后一页。"""
    api = _FakeApi({"100": [[10, 9]]})
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    _seed_run(repository, artist_id="100")
    service = _make_service(api, repository)
    result = SimpleNamespace(illusts=[SimpleNamespace(id=10)], next_url=None)
    advanced, cursor = service._advance_sweep_cursor(result, "999", 1, result.illusts)
    assert advanced is False
    assert cursor == {}
    repository.close()


def test_advance_cursor_returns_true_with_cursor_when_all_conditions_met(tmp_path: Path) -> None:
    """cutoff 未命中 + 有 next_url + parse_qs 成功 → (True, cursor)。"""
    api = _FakeApi({"100": [[10, 9]]})
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    _seed_run(repository, artist_id="100")
    service = _make_service(api, repository)
    result = SimpleNamespace(illusts=[SimpleNamespace(id=10)], next_url="?offset=5")
    advanced, cursor = service._advance_sweep_cursor(result, "999", 1, result.illusts)
    assert advanced is True
    assert cursor == {"offset": "5"}
    repository.close()


def test_advance_cursor_returns_false_when_parse_qs_empty(tmp_path: Path) -> None:
    """cutoff 未命中 + next_url 存在但 parse_qs 返回空 → (False, {})。"""
    api = _FakeApi({"100": [[10, 9]]})
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    _seed_run(repository, artist_id="100")
    service = _make_service(api, repository)
    result = SimpleNamespace(illusts=[SimpleNamespace(id=10)], next_url="invalid_url")
    advanced, cursor = service._advance_sweep_cursor(result, "999", 1, result.illusts)
    assert advanced is False
    assert cursor == {}
    repository.close()
