"""GUI configuration round-trip contracts without creating a Tk window."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from gui.pixiv_downloader_config_mixin import PixivDownloaderConfigMixin


class _Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Entry(_Value):
    def delete(self, *_args):
        self.value = ""

    def insert(self, _index, value):
        self.value = str(value)


class _Gui(PixivDownloaderConfigMixin):
    def __init__(self, root: Path):
        self.app_paths = SimpleNamespace(
            project_root=root,
            legacy_config_file=root / "pixiv_config.json",
            downloaded_ids_file=root / "downloaded_ids.txt",
            cookie_file=root / "www.pixiv.net_cookies.txt",
            resolve=lambda value: (root / value).resolve(),
        )
        self.token_entry = _Entry("token")
        self.dir_entry = _Entry(str(root / "images"))
        self.count_entry = _Entry("100")
        self.thread_entry = _Entry("3")
        self.delay_entry = _Entry("0.5")
        self.api_delay_entry = _Entry("0.3")
        self.min_bookmarks_entry = _Entry("0")
        self.name_template = _Value("{id}_{title}_p{page}")
        self.folder_var = _Value(False)
        self.original_var = _Value(True)
        self.multi_var = _Value(True)
        self.skip_ai_var = _Value(False)
        self.skip_downloaded_var = _Value(True)
        self.mode_var = _Value("follow")
        self.keyword_entry = _Entry("")
        self.search_sort_combo = _Value("popular_desc")
        self.user_entry = _Entry("")
        self.related_entry = _Entry("")
        self.rank_combo = _Value("day")
        self.rank_date_entry = _Entry("")
        self.rank_date_end_entry = _Entry("")
        self.auth_cookie = ""
        self.user_agent = ""
        self.cookie_provider = None

    def _load_cookie_file(self, config=None):
        return False

    def _toggle_mode(self):
        return None


def test_original_and_multi_page_options_round_trip(tmp_path: Path) -> None:
    gui = _Gui(tmp_path)
    gui.original_var.set(False)
    gui.multi_var.set(False)
    gui._save_config()

    payload = json.loads((tmp_path / "pixiv_config.json").read_text(encoding="utf-8"))
    assert payload["original"] is False
    assert payload["multi_page"] is False

    gui.original_var.set(True)
    gui.multi_var.set(True)
    gui._load_config()
    assert gui.original_var.get() is False
    assert gui.multi_var.get() is False
