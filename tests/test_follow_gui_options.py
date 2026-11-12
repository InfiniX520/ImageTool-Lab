"""GUI-independent validation and persistence for follow-all controls."""

from __future__ import annotations

import json
from pathlib import Path
from types import MethodType, SimpleNamespace

from gui.pixiv_downloader_config_mixin import PixivDownloaderConfigMixin
from gui.pixiv_downloader_runtime_mixin import PixivDownloaderRuntimeMixin


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def delete(self, *_args):
        self.value = ""

    def insert(self, _index, value):
        self.value = str(value)


class ConfigGui(PixivDownloaderConfigMixin):
    FOLLOW_START_LABELS = {
        "resume": "智能续跑（推荐）",
        "restart": "从头开始",
        "index": "从指定序号开始",
        "artist": "从指定画师 ID 开始",
        "retry_failed": "仅重试未完整画师",
    }

    def __init__(self, root: Path):
        self.app_paths = SimpleNamespace(
            project_root=root,
            legacy_config_file=root / "pixiv_config.json",
            downloaded_ids_file=root / "downloaded_ids.txt",
            cookie_file=root / "www.pixiv.net_cookies.txt",
            resolve=lambda value: (root / value).resolve(),
        )
        for name, value in {
            "token_entry": "",
            "dir_entry": str(root / "images"),
            "count_entry": "100",
            "thread_entry": "3",
            "delay_entry": "0.5",
            "api_delay_entry": "0.3",
            "min_bookmarks_entry": "0",
            "keyword_entry": "",
            "user_entry": "",
            "related_entry": "",
            "rank_date_entry": "",
            "rank_date_end_entry": "",
            "follow_start_value_entry": "101",
            "follow_batch_artists_entry": "25",
            "follow_api_budget_entry": "70",
        }.items():
            setattr(self, name, Value(value))
        self.name_template = Value("{id}_{title}_p{page}")
        self.folder_var = Value(False)
        self.original_var = Value(True)
        self.multi_var = Value(True)
        self.skip_ai_var = Value(False)
        self.skip_downloaded_var = Value(True)
        self.mode_var = Value("follow_all")
        self.search_sort_combo = Value("popular_desc")
        self.rank_combo = Value("day")
        self.follow_start_mode_var = Value("从指定序号开始")
        self.auth_cookie = ""
        self.user_agent = ""
        self.cookie_provider = None

    def _follow_start_mode_value(self):
        reverse = {label: value for value, label in self.FOLLOW_START_LABELS.items()}
        return reverse[self.follow_start_mode_var.get()]

    def _load_cookie_file(self, config=None):
        return False

    def _toggle_mode(self):
        return None

    def _toggle_follow_start_input(self):
        return None


def test_follow_options_round_trip(tmp_path: Path) -> None:
    gui = ConfigGui(tmp_path)
    gui._save_config()
    payload = json.loads((tmp_path / "pixiv_config.json").read_text(encoding="utf-8"))
    assert payload["follow_start_mode"] == "index"
    assert payload["follow_start_value"] == "101"
    assert payload["follow_batch_artists"] == "25"
    assert payload["follow_api_budget"] == "70"

    gui.follow_start_mode_var.set("智能续跑（推荐）")
    gui.follow_start_value_entry.value = ""
    gui._load_config()
    assert gui.follow_start_mode_var.get() == "从指定序号开始"
    assert gui.follow_start_value_entry.get() == "101"


def test_follow_manual_start_and_budgets_are_validated() -> None:
    gui = SimpleNamespace(
        keyword_entry=Value(""),
        user_entry=Value(""),
        related_entry=Value(""),
        rank_date_entry=Value(""),
        rank_date_end_entry=Value(""),
    )
    # R8-1 拆分后，_validate_download_request_inputs 委托给5个 helper 方法，
    # 需在 mock 上绑定这些 helper（SimpleNamespace 不触发描述符协议，需 MethodType 显式绑定）。
    for helper in (
        "_validate_numeric_bounds",
        "_validate_mode_inputs",
        "_validate_name_template",
        "_validate_ranking_dates",
        "_validate_follow_all_params",
    ):
        setattr(gui, helper, MethodType(getattr(PixivDownloaderRuntimeMixin, helper), gui))
    validate = PixivDownloaderRuntimeMixin._validate_download_request_inputs
    error = validate(
        gui,
        mode="follow_all",
        max_count=100,
        max_workers=3,
        delay=0.5,
        api_delay=0.3,
        name_template="{id}_{title}",
        follow_start_mode="index",
        follow_start_value="0",
        follow_batch_artists=30,
        follow_api_budget=80,
    )
    assert "画师序号" in error
