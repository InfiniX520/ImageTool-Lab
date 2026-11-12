#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载记录管理模块：负责 JSON/Markdown 防重记录与用户目录扫描。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, TextIO

from core.app_paths import get_app_paths
from core.download_engine import sanitize_filename
from core.sqlite_record_repository import SqliteRecordError, SqliteRecordRepository

# ============================================================
# 常量配置
# ============================================================

ROOT = get_app_paths().project_root

GLOBAL_ID_RECORD_FILE = "downloaded_id_record.json"
ARTIST_ID_RECORD_FILE = "downloaded_record.json"
GLOBAL_MD_RECORD_FILE = "global_download_history.md"

CENTRAL_LOG_DIR = ROOT / "download_logs"
CENTRAL_ID_RECORD_FILE = "master_downloaded_record.json"
CENTRAL_RECENT_MD_FILE = "recent_3000_history.md"
CENTRAL_MONTHLY_DIR = "monthly_records"

MAX_MD_ROWS = 3000
TRUST_COMPLETED_RECORD_WITHOUT_FILE = True

# Markdown 全量重建在大批量下载时开销较高：
# 通过批量/时间窗口节流，降低 CPU 与磁盘占用。
MARKDOWN_REFRESH_INTERVAL = 30
MARKDOWN_REFRESH_MIN_SECONDS = 15.0
MAX_PENDING_MARKDOWN_PROJECTIONS = 2048
LEGACY_PROJECTION_BATCH_SIZE = 5000

_RECORD_LOCK = threading.Lock()
_MARKDOWN_PENDING_UPDATES = 0
_MARKDOWN_LAST_REFRESH_TS = 0.0
_PENDING_MARKDOWN_PROJECTIONS: "OrderedDict[tuple[Path, Path, int | None], None]" = OrderedDict()
_PENDING_LEGACY_EVENTS: "OrderedDict[tuple[str, str], PendingRecordEvent]" = OrderedDict()
_SQLITE_REPOSITORY: SqliteRecordRepository | None = None

# ``downloaded_id_record.json`` can grow to tens of megabytes.  Re-parsing it
# for every worker lookup was the dominant hot path in large runs.  The cache
# below stores only the fields needed by ``check_record_skip`` (rather than the
# complete JSON objects), with explicit path/count/size bounds so a long-lived
# GUI process cannot retain an unbounded amount of user data.
RECORD_INDEX_CACHE_MAX_PATHS = 8
RECORD_INDEX_CACHE_MAX_ITEMS = 100_000
RECORD_INDEX_CACHE_MAX_ESTIMATED_BYTES = 8 * 1024 * 1024


class RecordPersistenceError(RuntimeError):
    """Raised when an existing record file cannot be safely decoded."""


@dataclass
class _RecordIndex:
    signature: tuple[int, int, int] | None
    records: dict[str, tuple[object, object, object]]
    estimated_bytes: int


class RecordManager:
    """Compatibility path adapter for the former class-based API."""

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).expanduser().resolve()

    @property
    def central_log_dir(self) -> Path:
        return self.project_root / CENTRAL_LOG_DIR.name

    @property
    def central_record_json(self) -> Path:
        return self.central_log_dir / CENTRAL_ID_RECORD_FILE

    @property
    def central_recent_md(self) -> Path:
        return self.central_log_dir / CENTRAL_RECENT_MD_FILE

    @property
    def central_monthly_dir(self) -> Path:
        return self.central_log_dir / CENTRAL_MONTHLY_DIR

    def ensure_dirs(self) -> None:
        self.central_log_dir.mkdir(parents=True, exist_ok=True)
        self.central_monthly_dir.mkdir(parents=True, exist_ok=True)


_RECORD_INDEX_CACHE: "OrderedDict[Path, _RecordIndex]" = OrderedDict()


@dataclass(frozen=True, slots=True)
class PendingRecordEvent:
    """Normalized, serializable event passed to the persistence boundary."""

    download_root: str
    folder_by_user: bool
    save_path: str
    record_key: str
    record_item: dict[str, object]
    artist_id: str
    artist_name: str


def _get_sqlite_repository() -> SqliteRecordRepository:
    """Return the process repository for the currently configured log root."""
    global _SQLITE_REPOSITORY
    log_root = Path(get_central_log_root()).resolve()
    repository = _SQLITE_REPOSITORY
    if repository is None or repository.log_root != log_root:
        if repository is not None:
            repository.close()
        repository = SqliteRecordRepository(log_root)
        _SQLITE_REPOSITORY = repository
    return repository


# ============================================================
# 基础工具
# ============================================================


def safe_int(value, default=None):
    """安全转换为整数，失败时返回默认值。"""
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def extract_page_index(url: str) -> int:
    """从 URL 中提取页码，未匹配时返回 0。"""
    if not url:
        return 0
    match = re.search(r"_p(\d+)", url)
    return int(match.group(1)) if match else 0


def _file_signature(path: str | Path) -> tuple[int, int, int] | None:
    """Return a cheap change signature for a record file.

    Atomic replacement updates at least the size or mtime on supported local
    filesystems.  The inode component also protects against a same-size,
    same-timestamp replacement on platforms that expose it.
    """

    try:
        stat_result = Path(path).stat()
    except OSError:
        return None

    return (
        int(getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000))),
        int(stat_result.st_size),
        int(getattr(stat_result, "st_ino", 0)),
    )


def _atomic_write_text(path: str | Path, writer: Callable[[TextIO], None]) -> None:
    """Write text to a sibling temporary file and atomically replace ``path``.

    The old file remains intact if serialization or the write fails.  A
    sibling temporary file keeps ``os.replace`` on the same volume, which is
    the only way to make the final rename atomic on Windows and POSIX.
    """

    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        dir=str(target_path.parent),
    )
    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_obj:
            writer(file_obj)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temporary_path, target_path)
    except Exception:
        # ``fdopen`` closes the descriptor on normal entry.  If opening the
        # descriptor itself failed, close it here; cleanup is best effort and
        # must never hide the original serialization/write exception.
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _record_summary(record_item: object) -> tuple[object, object, object]:
    """Extract the minimal skip-check fields from one JSON record."""

    if not isinstance(record_item, dict):
        return (None, None, None)
    return (
        record_item.get("status"),
        record_item.get("file_path"),
        record_item.get("file_size"),
    )


def _estimate_index_size(record_key: object, summary: tuple[object, object, object]) -> int:
    """Conservative byte estimate used to bound the compact index cache."""

    key_size = len(str(record_key).encode("utf-8", errors="replace"))
    path_size = len(str(summary[1] or "").encode("utf-8", errors="replace"))
    # Account for dict/tuple/object overhead with a deliberately conservative
    # fixed allowance.  This is a guardrail, not an accounting API.
    return key_size + path_size + 96


def _build_record_index(data: object) -> tuple[dict[str, tuple[object, object, object]], int]:
    """Build a compact index from a decoded record mapping."""

    if not isinstance(data, dict):
        return {}, 0

    index: dict[str, tuple[object, object, object]] = {}
    estimated_bytes = 0
    for key, value in data.items():
        record_key = str(key)
        summary = _record_summary(value)
        index[record_key] = summary
        estimated_bytes += _estimate_index_size(record_key, summary)
    return index, estimated_bytes


def _load_record_index(path: str | Path) -> dict[str, tuple[object, object, object]]:
    """Load or reuse a bounded compact index for a record JSON file."""

    target_path = Path(path).resolve()
    signature = _file_signature(target_path)
    cached = _RECORD_INDEX_CACHE.get(target_path)
    if cached is not None and cached.signature == signature:
        _RECORD_INDEX_CACHE.move_to_end(target_path)
        return cached.records

    data = load_json(target_path, {})
    index, estimated_bytes = _build_record_index(data)

    # Replace a stale entry before considering the new one.  This prevents a
    # single oversized record file from leaving an old large cache resident.
    _RECORD_INDEX_CACHE.pop(target_path, None)
    if (
        len(index) <= RECORD_INDEX_CACHE_MAX_ITEMS
        and estimated_bytes <= RECORD_INDEX_CACHE_MAX_ESTIMATED_BYTES
    ):
        _RECORD_INDEX_CACHE[target_path] = _RecordIndex(signature, index, estimated_bytes)
        while len(_RECORD_INDEX_CACHE) > RECORD_INDEX_CACHE_MAX_PATHS:
            _RECORD_INDEX_CACHE.popitem(last=False)

    return index


def _update_record_index_after_write(
    path: str | Path,
    record_key: str,
    record_item: dict[str, object],
) -> None:
    """Update an already-cached index after a successful JSON write."""

    target_path = Path(path).resolve()
    cached = _RECORD_INDEX_CACHE.get(target_path)
    if cached is None:
        return

    old_summary = cached.records.get(record_key)
    new_summary = _record_summary(record_item)
    new_estimate = cached.estimated_bytes
    if old_summary is not None:
        new_estimate -= _estimate_index_size(record_key, old_summary)
    new_estimate += _estimate_index_size(record_key, new_summary)

    if new_estimate > RECORD_INDEX_CACHE_MAX_ESTIMATED_BYTES:
        _RECORD_INDEX_CACHE.pop(target_path, None)
        return

    cached.records[record_key] = new_summary
    cached.estimated_bytes = new_estimate
    cached.signature = _file_signature(target_path)
    _RECORD_INDEX_CACHE.move_to_end(target_path)


def clear_record_cache() -> None:
    """Drop compact record indexes (useful after external migrations/tests)."""
    global _SQLITE_REPOSITORY
    with _RECORD_LOCK:
        _RECORD_INDEX_CACHE.clear()
        repository = _SQLITE_REPOSITORY
        _SQLITE_REPOSITORY = None
        if repository is not None:
            repository.close()


def _markdown_projection_key(
    record_path: str | Path,
    markdown_path: str | Path,
    max_rows: int | None,
) -> tuple[Path, Path, int | None]:
    return (Path(record_path).resolve(), Path(markdown_path).resolve(), max_rows)


def _queue_markdown_projection(
    record_path: str | Path,
    markdown_path: str | Path,
    max_rows: int | None,
) -> None:
    key = _markdown_projection_key(record_path, markdown_path, max_rows)
    _PENDING_MARKDOWN_PROJECTIONS[key] = None
    _PENDING_MARKDOWN_PROJECTIONS.move_to_end(key)
    while len(_PENDING_MARKDOWN_PROJECTIONS) > MAX_PENDING_MARKDOWN_PROJECTIONS:
        _PENDING_MARKDOWN_PROJECTIONS.popitem(last=False)


def _discard_markdown_projection(
    record_path: str | Path,
    markdown_path: str | Path,
    max_rows: int | None,
) -> None:
    _PENDING_MARKDOWN_PROJECTIONS.pop(
        _markdown_projection_key(record_path, markdown_path, max_rows),
        None,
    )


def flush_pending_markdown() -> None:
    """Synchronously materialize deferred Markdown projections.

    JSON remains the source of truth during a batch, while this explicit
    boundary guarantees that a stopped/closed batch cannot leave the human
    readable projection permanently behind the persisted records.
    """

    with _RECORD_LOCK:
        try:
            _flush_pending_legacy_events_locked()
        except Exception:
            # SQLite has already committed every event.  A damaged legacy
            # projection must never prevent task cleanup or overwrite its
            # source; retain pending events for a later repair/export.
            pass
        pending = list(_PENDING_MARKDOWN_PROJECTIONS)
        _PENDING_MARKDOWN_PROJECTIONS.clear()
        for record_path, markdown_path, max_rows in pending:
            if not record_path.exists():
                continue
            try:
                records = _load_mapping_for_update(record_path)
                _write_markdown_from_records(records, markdown_path, max_rows)
            except Exception:
                # Never replace a projection from a corrupt source; retain the
                # key so a later repair can retry it.
                _PENDING_MARKDOWN_PROJECTIONS[(record_path, markdown_path, max_rows)] = None


def clear_pending_markdown() -> None:
    """Forget deferred projections after an explicit record deletion."""

    with _RECORD_LOCK:
        _PENDING_MARKDOWN_PROJECTIONS.clear()
        _PENDING_LEGACY_EVENTS.clear()


def load_json(path: str | Path, default):
    """读取 JSON，失败时返回默认值。"""
    target_path = Path(path)
    if not target_path.exists():
        return default

    try:
        with target_path.open("r", encoding="utf-8") as file_obj:
            return json.load(file_obj)
    except Exception:
        return default


def save_json(path: str | Path, data) -> None:
    """写入 JSON，自动创建父目录。"""
    _atomic_write_text(
        path,
        lambda file_obj: json.dump(data, file_obj, ensure_ascii=False, indent=2),
    )


def _escape_markdown(text) -> str:
    """转义 Markdown 表格中的特殊字符。"""
    if text is None:
        return ""
    return str(text).replace("|", "\\|").replace("\n", " ")


# ============================================================
# Markdown 记录构建
# ============================================================


def _write_markdown_table(markdown_path: str | Path, rows: list[dict[str, object]]) -> None:
    """按统一表头重写 Markdown 下载记录。"""

    def write_table(file_obj: TextIO) -> None:
        file_obj.write("# 下载记录\n\n")
        file_obj.write(
            "| 文件名 | 作品ID | 作品链接 | 作品标题 | 画师ID | 画师名称 | 收藏数 | 是否AI生成 | 发布时间 | 原图链接 | 下载时间 | 下载状态 |\n"  # noqa: E501
        )
        file_obj.write(
            "|--------|--------|----------|----------|--------|----------|--------|------------|----------|----------|----------|----------|\n"  # noqa: E501
        )
        for row in rows:
            file_obj.write(
                f"| {_escape_markdown(row.get('filename', ''))} | {_escape_markdown(row.get('illust_id', ''))} | "  # noqa: E501
                f"{_escape_markdown(row.get('illust_link', ''))} | {_escape_markdown(row.get('title', ''))} | "  # noqa: E501
                f"{_escape_markdown(row.get('artist_id', ''))} | {_escape_markdown(row.get('artist_name', ''))} | "  # noqa: E501
                f"{_escape_markdown(row.get('bookmarks', ''))} | {_escape_markdown(row.get('ai_flag', ''))} | "  # noqa: E501
                f"{_escape_markdown(row.get('publish_time', ''))} | {_escape_markdown(row.get('original_url', ''))} | "  # noqa: E501
                f"{_escape_markdown(row.get('download_time', ''))} | {_escape_markdown(row.get('status', ''))} |\n"  # noqa: E501
            )

    _atomic_write_text(markdown_path, write_table)


def _record_item_to_markdown_row(record_item: dict[str, object]) -> dict[str, object]:
    """将 JSON 记录项转换为 Markdown 行。"""
    illust_id = record_item.get("illust_id", "")
    original_url = record_item.get("original_url", "")

    return {
        "filename": Path(str(record_item.get("file_path", "") or "")).name,
        "illust_id": str(illust_id),
        "illust_link": f"[链接](https://www.pixiv.net/artworks/{illust_id})" if illust_id else "",
        "title": record_item.get("title", "") or "",
        "artist_id": str(record_item.get("artist_id", "") or ""),
        "artist_name": record_item.get("artist_name", "") or "",
        "bookmarks": record_item.get("bookmarks", ""),
        "ai_flag": "是" if record_item.get("is_ai") else "否",
        "publish_time": record_item.get("publish_time", "") or "",
        "original_url": f"[链接]({original_url})" if original_url else "",
        "download_time": record_item.get("updated_at", "") or "",
        "status": record_item.get("status", "") or "",
    }


def _write_markdown_from_records(
    records: object,
    markdown_path: str | Path,
    max_rows: int | None = None,
) -> None:
    """从已解码记录重建 Markdown，避免同一事件再次读取 JSON。"""

    rows: list[dict[str, object]] = []
    for item in records.values() if isinstance(records, dict) else ():
        if isinstance(item, dict):
            rows.append(_record_item_to_markdown_row(item))

    rows.sort(key=lambda row: row.get("download_time", ""), reverse=True)
    if max_rows is not None and max_rows > 0:
        rows = rows[:max_rows]

    _write_markdown_table(markdown_path, rows)


def _refresh_markdown_from_record(
    record_path: str | Path, markdown_path: str | Path, max_rows: int | None = None
) -> None:
    """基于 JSON 防重记录重建 Markdown，按更新时间倒序。"""
    _write_markdown_from_records(load_json(record_path, {}), markdown_path, max_rows)


# ============================================================
# 防重记录更新
# ============================================================


def _update_id_record(
    record_path: str | Path,
    record_key: str,
    record_item: dict[str, object],
) -> dict[str, object]:
    """更新页级 JSON 防重记录并返回已更新映射。

    Returning the decoded mapping lets the caller render Markdown from the
    same snapshot, avoiding a second full JSON parse when a refresh is due.
    Existing callers that ignored the former ``None`` return remain
    compatible.
    """

    target_path = Path(record_path)
    data = _load_mapping_for_update(target_path)
    data[record_key] = record_item
    save_json(target_path, data)
    _update_record_index_after_write(target_path, record_key, record_item)
    return data


def _load_mapping_for_update(record_path: str | Path) -> dict[str, object]:
    """Read an existing record mapping without silently resetting corruption."""
    target_path = Path(record_path)
    if not target_path.exists():
        return {}
    try:
        with target_path.open("r", encoding="utf-8") as file_obj:
            loaded = json.load(file_obj)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecordPersistenceError(f"无法安全读取下载记录: {target_path}") from exc
    if not isinstance(loaded, dict):
        raise RecordPersistenceError(f"下载记录格式不是对象: {target_path}")
    return loaded


def _is_completed_record_valid(record_item: dict[str, object] | None) -> bool:
    """检查记录状态为完成且本地文件校验通过。"""
    if not isinstance(record_item, dict):
        return False
    if record_item.get("status") != "下载完成":
        return False

    file_path = record_item.get("file_path")
    file_size = safe_int(record_item.get("file_size"), 0) or 0
    if not file_path or file_size <= 0:
        return False

    target_path = Path(str(file_path))
    try:
        return target_path.is_file() and target_path.stat().st_size == file_size
    except OSError:
        return False


def _is_completed_record(record_item: dict[str, object] | None) -> bool:
    """仅检查状态字段是否为下载完成。"""
    return isinstance(record_item, dict) and record_item.get("status") == "下载完成"


def get_central_log_root() -> str:
    """获取项目级集中日志目录路径。"""
    return str(CENTRAL_LOG_DIR)


def configure_record_storage(project_root: str | Path | None = None) -> Path:
    """Point process-wide record storage at an application root.

    The desktop application owns a single record store.  This explicit hook
    keeps injected/test roots and frozen executable roots aligned with the GUI
    instead of retaining the source checkout selected at import time.
    """

    paths = get_app_paths(project_root)
    flush_pending_markdown()
    global ROOT
    global CENTRAL_LOG_DIR
    global _SQLITE_REPOSITORY
    with _RECORD_LOCK:
        repository = _SQLITE_REPOSITORY
        _SQLITE_REPOSITORY = None
        if repository is not None:
            repository.close()
        ROOT = paths.project_root
        CENTRAL_LOG_DIR = paths.download_logs_dir
        _RECORD_INDEX_CACHE.clear()
    return CENTRAL_LOG_DIR


def reconcile_record_projection() -> bool:
    """Repair a crash-lagged central JSON projection from transactional state."""
    with _RECORD_LOCK:
        return _get_sqlite_repository().reconcile_legacy_projection()


def _should_refresh_markdown(status: str) -> bool:
    """判断本次事件是否触发 Markdown 重建。"""
    global _MARKDOWN_PENDING_UPDATES
    global _MARKDOWN_LAST_REFRESH_TS

    # 跳过事件数量通常最多，记录到 JSON 即可，减少额外 IO 压力。
    if status == "跳过":
        return False

    _MARKDOWN_PENDING_UPDATES += 1
    now_ts = time.monotonic()
    should_refresh = (
        _MARKDOWN_PENDING_UPDATES >= MARKDOWN_REFRESH_INTERVAL
        or _MARKDOWN_LAST_REFRESH_TS <= 0
        or (now_ts - _MARKDOWN_LAST_REFRESH_TS) >= MARKDOWN_REFRESH_MIN_SECONDS
    )

    if should_refresh:
        _MARKDOWN_PENDING_UPDATES = 0
        _MARKDOWN_LAST_REFRESH_TS = now_ts

    return should_refresh


def _record_to_central_logs(
    record_key: str, record_item: dict[str, object], refresh_markdown: bool = True
) -> None:
    """同步写入集中总记录、最近记录和月度记录。"""
    central_root = Path(get_central_log_root())
    central_root.mkdir(parents=True, exist_ok=True)

    central_json = central_root / CENTRAL_ID_RECORD_FILE
    central_records = _update_id_record(central_json, record_key, record_item)
    recent_markdown = central_root / CENTRAL_RECENT_MD_FILE

    if refresh_markdown:
        _write_markdown_from_records(central_records, recent_markdown, max_rows=MAX_MD_ROWS)
        _discard_markdown_projection(central_json, recent_markdown, MAX_MD_ROWS)
    else:
        _queue_markdown_projection(central_json, recent_markdown, MAX_MD_ROWS)
    # Do not retain the full central mapping while the monthly mapping is
    # loaded below; only the compact index is kept between events.
    del central_records

    month = str(record_item.get("updated_at", ""))[:7]
    if not month or len(month) != 7:
        month = datetime.now().strftime("%Y-%m")

    month_root = central_root / CENTRAL_MONTHLY_DIR
    month_root.mkdir(parents=True, exist_ok=True)

    month_json = month_root / f"{month}.json"
    month_records = _update_id_record(month_json, record_key, record_item)
    month_markdown = month_root / f"{month}.md"
    if refresh_markdown:
        _write_markdown_from_records(month_records, month_markdown)
        _discard_markdown_projection(month_json, month_markdown, None)
    else:
        _queue_markdown_projection(month_json, month_markdown, None)


def _make_pending_event(
    download_root: str,
    folder_by_user: bool,
    save_path: str,
    illust_id,
    page_index: int,
    title,
    artist_id,
    artist_name,
    bookmarks,
    is_ai,
    create_date,
    original_url,
    status,
) -> PendingRecordEvent | None:
    if not download_root:
        return None
    save_path_text = str(save_path)
    try:
        target_path = Path(save_path_text)
    except (TypeError, ValueError):
        return None
    try:
        file_size = target_path.stat().st_size if target_path.is_file() else 0
    except OSError:
        # A download can be moved/removed by another process between the
        # success callback and record persistence; retain the event with a
        # zero size rather than failing the worker thread.
        file_size = 0
    create_date_text = (
        create_date.isoformat() if hasattr(create_date, "isoformat") else str(create_date or "")
    )
    record_key = f"{illust_id}_p{page_index}"
    record_item = {
        "file_path": save_path_text,
        "status": status,
        "file_size": file_size,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "illust_id": str(illust_id),
        "page": page_index,
        "title": title or "",
        "artist_id": str(artist_id or ""),
        "artist_name": artist_name or "",
        "bookmarks": bookmarks if bookmarks is not None else "",
        "is_ai": bool(is_ai),
        "publish_time": create_date_text[:10],
        "original_url": original_url or "",
    }
    return PendingRecordEvent(
        download_root=str(download_root),
        folder_by_user=bool(folder_by_user),
        save_path=save_path_text,
        record_key=record_key,
        record_item=record_item,
        artist_id=str(artist_id or ""),
        artist_name=artist_name or "",
    )


def _legacy_projection_specs(
    event: PendingRecordEvent,
) -> tuple[tuple[Path, Path, int | None], ...]:
    """Return every legacy JSON/Markdown projection affected by an event."""
    specs: list[tuple[Path, Path, int | None]] = []
    download_root = Path(event.download_root)
    specs.append(
        (
            download_root / GLOBAL_ID_RECORD_FILE,
            download_root / GLOBAL_MD_RECORD_FILE,
            MAX_MD_ROWS,
        )
    )

    if event.folder_by_user:
        artist_dir = Path(event.save_path).parent
        safe_artist_name = (
            sanitize_filename(event.artist_name or "") or event.artist_id or "unknown"
        )
        specs.append(
            (
                artist_dir / ARTIST_ID_RECORD_FILE,
                artist_dir / f"{safe_artist_name}_download_history.md",
                MAX_MD_ROWS,
            )
        )

    central_root = Path(get_central_log_root())
    specs.append(
        (
            central_root / CENTRAL_ID_RECORD_FILE,
            central_root / CENTRAL_RECENT_MD_FILE,
            MAX_MD_ROWS,
        )
    )
    month = str(event.record_item.get("updated_at", ""))[:7]
    if len(month) != 7:
        month = datetime.now().strftime("%Y-%m")
    month_root = central_root / CENTRAL_MONTHLY_DIR
    specs.append(
        (
            month_root / f"{month}.json",
            month_root / f"{month}.md",
            None,
        )
    )
    return tuple(specs)


def _merge_legacy_record_item(existing: object, incoming: dict[str, object]) -> dict[str, object]:
    """Do not downgrade a completed latest-state row to a transient event."""
    if (
        isinstance(existing, dict)
        and existing.get("status") == "下载完成"
        and incoming.get("status") != "下载完成"
    ):
        return existing
    return incoming


def _flush_pending_legacy_events_locked() -> None:
    """Materialize queued compatibility files with one rewrite per target."""
    if not _PENDING_LEGACY_EVENTS:
        return

    events = list(_PENDING_LEGACY_EVENTS.values())
    grouped: "OrderedDict[Path, dict[str, object]]" = OrderedDict()
    markdown_targets: dict[Path, tuple[Path, int | None]] = {}
    for event in events:
        for record_path, markdown_path, max_rows in _legacy_projection_specs(event):
            target_path = record_path.resolve()
            updates = grouped.setdefault(target_path, {})
            updates[event.record_key] = event.record_item
            markdown_targets[target_path] = (markdown_path, max_rows)

    for record_path, updates in grouped.items():
        records = _load_mapping_for_update(record_path)
        for record_key, incoming in updates.items():
            records[record_key] = _merge_legacy_record_item(records.get(record_key), incoming)
        save_json(record_path, records)
        _RECORD_INDEX_CACHE.pop(record_path, None)
        markdown_path, max_rows = markdown_targets[record_path]
        _write_markdown_from_records(records, markdown_path, max_rows)
        _discard_markdown_projection(record_path, markdown_path, max_rows)

    for event in events:
        _PENDING_LEGACY_EVENTS.pop((event.download_root, event.record_key), None)
    _get_sqlite_repository().mark_legacy_projection_current()


def _record_download_event_now(
    download_root: str,
    folder_by_user: bool,
    save_path: str,
    illust_id,
    page_index: int,
    title,
    artist_id,
    artist_name,
    bookmarks,
    is_ai,
    create_date,
    original_url,
    status,
) -> None:
    """将下载事件写入全局、画师与集中日志记录。"""
    event = _make_pending_event(
        download_root,
        folder_by_user,
        save_path,
        illust_id,
        page_index,
        title,
        artist_id,
        artist_name,
        bookmarks,
        is_ai,
        create_date,
        original_url,
        status,
    )
    if event is None:
        return
    with _RECORD_LOCK:
        # SQLite is committed for every event.  The large JSON/Markdown files
        # are reversible compatibility projections and are rewritten once per
        # batch instead of once per downloaded page.
        _get_sqlite_repository().record(event.record_key, event.record_item)
        pending_key = (event.download_root, event.record_key)
        _PENDING_LEGACY_EVENTS[pending_key] = event
        _PENDING_LEGACY_EVENTS.move_to_end(pending_key)
        if len(_PENDING_LEGACY_EVENTS) >= LEGACY_PROJECTION_BATCH_SIZE:
            try:
                _flush_pending_legacy_events_locked()
            except Exception:
                # The transactional record is already safe. Keep compatibility
                # events queued so a later explicit flush can retry.
                pass


def record_download_event(
    download_root: str,
    folder_by_user: bool,
    save_path: str,
    illust_id,
    page_index: int,
    title,
    artist_id,
    artist_name,
    bookmarks,
    is_ai,
    create_date,
    original_url,
    status,
) -> None:
    """Persist one download event through the stable public API.

    ``download_service`` and third-party integrations historically imported
    this function directly. Keep the compatibility boundary explicit while
    allowing the implementation to evolve behind the private helper.
    """

    _record_download_event_now(
        download_root,
        folder_by_user,
        save_path,
        illust_id,
        page_index,
        title,
        artist_id,
        artist_name,
        bookmarks,
        is_ai,
        create_date,
        original_url,
        status,
    )


def check_record_skip(download_root: str, illust_id, page_index: int) -> bool:
    """根据本地与集中记录判断是否可直接跳过下载。"""
    if not download_root or not illust_id:
        return False

    record_key = f"{illust_id}_p{page_index}"

    local_record_path = Path(download_root) / GLOBAL_ID_RECORD_FILE
    with _RECORD_LOCK:
        try:
            sqlite_summary = _get_sqlite_repository().get_summary(illust_id, page_index)
        except SqliteRecordError:
            sqlite_summary = None
        if sqlite_summary is not None:
            sqlite_item = {
                "status": sqlite_summary[0],
                "file_path": sqlite_summary[1],
                "file_size": sqlite_summary[2],
            }
            if _is_completed_record_valid(sqlite_item):
                return True
            if TRUST_COMPLETED_RECORD_WITHOUT_FILE and _is_completed_record(sqlite_item):
                return True

        local_summary = _load_record_index(local_record_path).get(record_key)

        if local_summary is not None:
            local_item = {
                "status": local_summary[0],
                "file_path": local_summary[1],
                "file_size": local_summary[2],
            }
        else:
            local_item = None

        if _is_completed_record_valid(local_item):
            return True
        if TRUST_COMPLETED_RECORD_WITHOUT_FILE and _is_completed_record(local_item):
            return True

        central_record_path = Path(get_central_log_root()) / CENTRAL_ID_RECORD_FILE
        central_summary = _load_record_index(central_record_path).get(record_key)
        if central_summary is not None:
            central_item = {
                "status": central_summary[0],
                "file_path": central_summary[1],
                "file_size": central_summary[2],
            }
        else:
            central_item = None

        if _is_completed_record_valid(central_item):
            return True
        if TRUST_COMPLETED_RECORD_WITHOUT_FILE and _is_completed_record(central_item):
            return True

    return False
