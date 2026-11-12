#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旧版记录兼容工具：提供 success_log、txt ID 与用户目录扫描能力。"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

from core.record_manager import (
    ARTIST_ID_RECORD_FILE,
    GLOBAL_ID_RECORD_FILE,
    GLOBAL_MD_RECORD_FILE,
    clear_pending_markdown,
    clear_record_cache,
    get_central_log_root,
)

_DOWNLOADED_IDS_LOCK = threading.Lock()


def clear_download_records(save_dir: str, clear_central: bool = False) -> None:
    """清理下载目录中的防重记录及可选的集中日志。"""
    # Close SQLite/WAL handles before deleting the central directory.  This
    # is required on Windows and also prevents queued compatibility events
    # from recreating files after an explicit user-confirmed clear.
    clear_pending_markdown()
    clear_record_cache()
    target_root = Path(save_dir) if save_dir else None
    if target_root and target_root.exists():
        for root, _, files in os.walk(str(target_root)):
            for file_name in files:
                should_delete = (
                    file_name
                    in {
                        GLOBAL_ID_RECORD_FILE,
                        ARTIST_ID_RECORD_FILE,
                        GLOBAL_MD_RECORD_FILE,
                        "全局下载总记录.md",
                        f"{GLOBAL_MD_RECORD_FILE}.json",
                    }
                    or file_name == "success_log.md"
                    or file_name.endswith("_download_history.md")
                    or file_name.endswith("_作品下载记录.md")
                    or file_name.endswith("_download_history.md.json")
                    or file_name.endswith("_作品下载记录.md.json")
                )
                if should_delete:
                    try:
                        Path(root, file_name).unlink(missing_ok=True)
                    except Exception:
                        pass

    if clear_central:
        central_root = Path(get_central_log_root())
        if central_root.exists():
            try:
                shutil.rmtree(str(central_root))
            except Exception:
                pass

    # The record manager keeps a bounded skip-check index.  Invalidate it
    # after a user-requested cleanup so a subsequent download cannot consult
    # an in-memory snapshot of files that were just removed.
    clear_pending_markdown()
    clear_record_cache()


def write_success_log(
    log_dir: str,
    filename: str,
    url: str,
    illust_id=None,
    is_ai=None,
    bookmarks=None,
    create_date=None,
) -> bool:
    """兼容旧版 success_log.md 结构（默认由上层控制是否启用）。"""
    if not log_dir:
        return False

    try:
        log_root = Path(log_dir)
        log_root.mkdir(parents=True, exist_ok=True)
        log_path = log_root / "success_log.md"
        exists = log_path.exists()

        with log_path.open("a", encoding="utf-8") as file_obj:
            if not exists:
                file_obj.write("# 下载记录\n\n")
                file_obj.write("| 文件名 | 作品链接 | 收藏数 | AI | 发布时间 | 原图 |\n")
                file_obj.write("|--------|----------|--------|----|---------|----- |\n")

            pixiv_link = (
                f"[{illust_id}](https://www.pixiv.net/artworks/{illust_id})" if illust_id else ""
            )
            if is_ai is True:
                ai_mark = "是"
            elif is_ai is False:
                ai_mark = "否"
            else:
                ai_mark = ""

            bookmark_text = str(bookmarks) if bookmarks is not None else ""
            date_text = create_date[:10] if create_date else ""
            file_obj.write(
                f"| {filename} | {pixiv_link} | {bookmark_text} | {ai_mark} | "
                f"{date_text} | [链接]({url}) |\n"
            )

        return True
    except Exception:
        return False


def load_downloaded_ids(file_path: str | Path) -> set[str]:
    """加载旧版 ID 文本记录，返回纯数字 ID 集合。"""
    target_path = Path(file_path)
    downloaded_ids: set[str] = set()

    if not target_path.exists():
        return downloaded_ids

    try:
        with target_path.open("r", encoding="utf-8") as file_obj:
            for raw_line in file_obj:
                line = raw_line.strip()
                if line and line.isdigit():
                    downloaded_ids.add(line)
    except Exception:
        pass

    return downloaded_ids


def save_downloaded_id(file_path: str | Path, illust_id) -> bool:
    """追加保存旧版 ID 文本记录。"""
    if not file_path or not illust_id:
        return False

    try:
        target_path = Path(file_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with _DOWNLOADED_IDS_LOCK:
            with target_path.open("a", encoding="utf-8") as file_obj:
                file_obj.write(f"{illust_id}\n")
                file_obj.flush()
        return True
    except Exception:
        return False


def check_user_folder_exists(save_dir: str, user_id, user_name=None):
    """检查保存目录中是否存在指定用户 ID 的文件夹。"""
    del user_name

    if not save_dir:
        return None

    target_root = Path(save_dir)
    if not target_root.exists():
        return None

    user_id_text = str(user_id)
    for folder_path in target_root.iterdir():
        if folder_path.is_dir() and (
            folder_path.name.startswith(f"{user_id_text}_") or folder_path.name == user_id_text
        ):
            return str(folder_path)

    return None


def get_existing_user_folders(save_dir: str) -> set[str]:
    """获取保存目录中已存在的用户 ID 集合。"""
    existing_ids: set[str] = set()

    if not save_dir:
        return existing_ids

    target_root = Path(save_dir)
    if not target_root.exists():
        return existing_ids

    for folder_path in target_root.iterdir():
        if folder_path.is_dir():
            parts = folder_path.name.split("_", 1)
            if parts[0].isdigit():
                existing_ids.add(parts[0])

    return existing_ids
