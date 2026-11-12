"""Pure validation contracts for main-window download requests."""

from __future__ import annotations

from gui.pixiv_downloader_layout_mixin import PixivDownloaderLayoutMixin
from gui.pixiv_downloader_runtime_mixin import PixivDownloaderRuntimeMixin


class _Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Gui(PixivDownloaderRuntimeMixin):
    def __init__(self):
        self.keyword_entry = _Value("")
        self.user_entry = _Value("")
        self.related_entry = _Value("")
        self.rank_date_entry = _Value("")
        self.rank_date_end_entry = _Value("")


def _validate(gui: _Gui, mode: str, template: str = "{id}_{title}_p{page}") -> str:
    return gui._validate_download_request_inputs(
        mode=mode,
        max_count=10,
        max_workers=3,
        delay=0.5,
        api_delay=0.3,
        name_template=template,
    )


def test_mode_specific_inputs_are_validated_before_worker_start() -> None:
    gui = _Gui()
    assert "关键词" in _validate(gui, "search")
    assert "用户 ID" in _validate(gui, "user")
    assert "作品 ID" in _validate(gui, "related")

    gui.keyword_entry.value = "landscape"
    gui.user_entry.value = "123, 456"
    gui.related_entry.value = "789"
    assert _validate(gui, "search") == ""
    assert _validate(gui, "user") == ""
    assert _validate(gui, "related") == ""


def test_ranking_date_and_template_are_validated() -> None:
    gui = _Gui()
    gui.rank_date_entry.value = "2026-07-xx"
    assert "YYYY-MM-DD" in _validate(gui, "ranking")

    gui.rank_date_entry.value = "2026-07-20"
    assert _validate(gui, "ranking") == ""
    assert "未知变量" in _validate(gui, "follow", "{id}_{unknown}")
    assert "必须包含 {id}" in _validate(gui, "follow", "{title}_p{page}")


def test_loading_saved_mode_is_not_overwritten_by_display_default() -> None:
    class Frame:
        def grid_forget(self):
            pass

        def grid(self, **_kwargs):
            pass

    class Label:
        def config(self, **_kwargs):
            pass

    gui = type("Layout", (), {})()
    gui.MODE_LABELS = PixivDownloaderLayoutMixin.MODE_LABELS
    gui.mode_var = _Value("follow")
    gui.mode_display_var = _Value("关键词搜索")
    gui.search_frame = Frame()
    gui.user_frame = Frame()
    gui.rank_frame = Frame()
    gui.related_frame = Frame()
    gui.empty_mode_frame = Frame()
    gui.mode_hint_label = Label()

    PixivDownloaderLayoutMixin._toggle_mode(gui)
    assert gui.mode_var.get() == "follow"
    assert gui.mode_display_var.get() == "关注用户新作"
