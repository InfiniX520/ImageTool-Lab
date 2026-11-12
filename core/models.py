"""Application DTOs shared by fetch, download and presentation adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterator


@dataclass(frozen=True, slots=True)
class DownloadTask:
    """A queued image download.

    ``__iter__`` intentionally preserves the historical 11-value unpacking
    contract used by third-party scripts and the legacy GUI while giving new
    code named, typed fields.
    """

    url: str
    save_path: str
    illust_id: object
    log_dir: str
    is_ai: bool | None
    bookmarks: object
    create_date: str | datetime | None
    page_index: int
    title: str
    artist_id: str
    artist_name: str
    original_urls: tuple[str, ...] = ()
    known_page_count: int = 0
    download_root: str = ""
    folder_by_user: bool | None = None
    original_enabled: bool | None = None
    multi_page: bool | None = None

    def __iter__(self) -> Iterator[object]:
        yield from self._legacy_values()

    def _legacy_values(self) -> tuple[object, ...]:
        return (
            self.url,
            self.save_path,
            self.illust_id,
            self.log_dir,
            self.is_ai,
            self.bookmarks,
            self.create_date,
            self.page_index,
            self.title,
            self.artist_id,
            self.artist_name,
        )

    def __len__(self) -> int:
        return 11

    def __getitem__(self, index):
        return self._legacy_values()[index]


__all__ = ["DownloadTask"]
