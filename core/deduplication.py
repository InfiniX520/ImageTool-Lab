#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""防重与记录清理服务（兼容旧实现）。"""

from __future__ import annotations

from pathlib import Path

from core.pixiv_utils import clear_download_records, load_downloaded_ids, save_downloaded_id


class DeduplicationService:
    """下载防重服务。"""

    def __init__(self, downloaded_ids_file: str | Path):
        self.downloaded_ids_file = Path(downloaded_ids_file)

    def load_ids(self) -> set[str]:
        """加载历史下载 ID。"""
        return load_downloaded_ids(str(self.downloaded_ids_file))

    def save_id(self, illust_id: int | str) -> bool:
        """保存下载 ID。"""
        return save_downloaded_id(str(self.downloaded_ids_file), str(illust_id))

    def clear(self, save_dir: str, clear_central: bool = True) -> None:
        """清理防重记录。"""
        clear_download_records(save_dir, clear_central=clear_central)
