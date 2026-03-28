#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载记录管理服务（项目级集中日志）。"""

from __future__ import annotations

from pathlib import Path

from core.pixiv_utils import CENTRAL_ID_RECORD_FILE, CENTRAL_LOG_DIR, CENTRAL_MONTHLY_DIR, CENTRAL_RECENT_MD_FILE


class RecordManager:
    """集中日志目录访问器。"""

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root)

    @property
    def central_log_dir(self) -> Path:
        """集中日志根目录。"""
        configured = Path(CENTRAL_LOG_DIR)
        if configured.is_absolute():
            return self.project_root / configured.name
        return self.project_root / configured

    @property
    def central_record_json(self) -> Path:
        """项目级总记录 JSON。"""
        return self.central_log_dir / CENTRAL_ID_RECORD_FILE

    @property
    def central_recent_md(self) -> Path:
        """最近记录 Markdown。"""
        return self.central_log_dir / CENTRAL_RECENT_MD_FILE

    @property
    def central_monthly_dir(self) -> Path:
        """按月归档目录。"""
        return self.central_log_dir / CENTRAL_MONTHLY_DIR

    def ensure_dirs(self) -> None:
        """确保集中日志目录存在。"""
        self.central_log_dir.mkdir(parents=True, exist_ok=True)
        self.central_monthly_dir.mkdir(parents=True, exist_ok=True)
