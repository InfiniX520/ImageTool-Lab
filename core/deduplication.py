"""Backward-compatible deduplication service facade.

The implementation lives in ``legacy_record_utils`` and ``record_manager``;
this module preserves the public class path used by v2.0 integrations.
"""

from __future__ import annotations

from pathlib import Path

from core.legacy_record_utils import (
    clear_download_records,
    load_downloaded_ids,
    save_downloaded_id,
)


class DeduplicationService:
    """Compatibility adapter for downloaded-ID and record operations."""

    def __init__(self, downloaded_ids_file: str | Path):
        self.downloaded_ids_file = Path(downloaded_ids_file)

    def load_ids(self) -> set[str]:
        return load_downloaded_ids(self.downloaded_ids_file)

    def save_id(self, illust_id: int | str) -> bool:
        return save_downloaded_id(self.downloaded_ids_file, illust_id)

    def clear(self, save_dir: str, clear_central: bool = True) -> None:
        clear_download_records(save_dir, clear_central=clear_central)


__all__ = ["DeduplicationService"]
