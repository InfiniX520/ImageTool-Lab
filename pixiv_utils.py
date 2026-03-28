#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pixiv 工具模块

功能:
    - 统一日志输出
    - 文件名清理
    - 图片下载（支持查重、重试）
    - 下载成功日志记录（success_log.md）
    - 已下载作品ID管理（断点续传）
    - API 请求计数与防封等待
    - 用户文件夹检查

依赖:
    pip install requests

使用:
    from pixiv_utils import (
        log_message, download_image, write_success_log, 
        load_downloaded_ids, save_downloaded_id, APICallCounter
    )
"""

# ============================================================
# 导入模块
# ============================================================

# 标准库
import os
import re
import json
import time
import random
import string
import urllib.parse
import threading
from datetime import datetime
from pathlib import Path

# 第三方库
import requests

# ============================================================
# 常量配置
# ============================================================

# 模块根目录
ROOT = Path(__file__).parent

# HTTP 请求头
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://www.pixiv.net/'
}

AJAX_ACCEPT = 'application/json'
IMAGE_ACCEPT = 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8'
ACCEPT_LANGUAGE = 'zh-CN,zh;q=0.9,en;q=0.8'

# 下载记录文件名
GLOBAL_ID_RECORD_FILE = 'downloaded_id_record.json'
ARTIST_ID_RECORD_FILE = 'downloaded_record.json'
GLOBAL_MD_RECORD_FILE = '全局下载总记录.md'

# 兼容旧版日志开关：默认关闭，避免与新记录体系重复。
ENABLE_LEGACY_SUCCESS_LOG = False

# 文件名最大长度
MAX_FILENAME_LENGTH = 200

# 防封配置
API_CALL_THRESHOLD_MIN = 50   # 最小请求次数阈值
API_CALL_THRESHOLD_MAX = 100  # 最大请求次数阈值
API_PAUSE_MIN = 5             # 最小暂停秒数
API_PAUSE_MAX = 15            # 最大暂停秒数

# 日志级别标记
LOG_SUCCESS = "[+]"  # 成功/完成
LOG_ERROR = "[-]"    # 错误/失败
LOG_WARNING = "[!]"  # 警告
LOG_INFO = "[*]"     # 信息
LOG_DEBUG = "[D]"    # 调试

# 调试模式开关
DEBUG_MODE = False

# 线程安全锁
_RECORD_LOCK = threading.Lock()
_META_CACHE_LOCK = threading.Lock()
_META_CACHE = {}


# ============================================================
# 日志输出
# ============================================================

def log_message(msg, level="info", callback=None):
    """
    统一日志输出函数
    
    Args:
        msg: 日志消息
        level: 日志级别 ("success", "error", "warning", "info", "debug")
        callback: 日志回调（队列或函数），为 None 时打印到控制台
        
    Returns:
        str: 格式化后的日志消息
    """
    # 调试级别在非调试模式下跳过
    if level == "debug" and not DEBUG_MODE:
        return ""
    
    # 选择日志标记
    prefix_map = {
        "success": LOG_SUCCESS,
        "error": LOG_ERROR,
        "warning": LOG_WARNING,
        "info": LOG_INFO,
        "debug": LOG_DEBUG,
    }
    prefix = prefix_map.get(level, LOG_INFO)
    formatted = f"{prefix} {msg}"
    
    # 调试模式下始终输出到控制台
    if DEBUG_MODE:
        print(formatted)
    
    # 输出到回调（GUI 日志队列等）
    if callback is None:
        if not DEBUG_MODE:  # 非调试模式下才打印（避免重复）
            print(formatted)
    elif hasattr(callback, 'put'):
        callback.put(formatted)
    elif callable(callback):
        callback(formatted)
    
    return formatted


def log_exception(e, context="", callback=None):
    """
    记录异常的详细信息
    
    Args:
        e: 异常对象
        context: 上下文描述
        callback: 日志回调
    """
    import traceback
    
    # 基本错误信息
    error_msg = f"{context}: {type(e).__name__}: {e}" if context else f"{type(e).__name__}: {e}"
    log_message(error_msg, "error", callback)
    
    # 调试模式下输出完整堆栈
    if DEBUG_MODE:
        stack = traceback.format_exc()
        for line in stack.strip().split('\n'):
            log_message(line, "debug", callback)


# ============================================================
# API 请求计数器（防封）
# ============================================================

class APICallCounter:
    """
    API 请求计数器，用于防封
    
    功能:
        每隔 50~100 次请求（随机），暂停 5~15 秒（随机）
        
    使用:
        counter = APICallCounter(log_callback=print)
        for i in range(200):
            counter.increment()  # 自动在阈值处暂停
    """
    
    def __init__(self, log_callback=None):
        """
        初始化计数器
        
        Args:
            log_callback: 日志回调函数或队列
        """
        self.count = 0
        self.threshold = random.randint(API_CALL_THRESHOLD_MIN, API_CALL_THRESHOLD_MAX)
        self.log_callback = log_callback
    
    def increment(self):
        """
        增加计数，达到阈值时暂停
        
        Returns:
            bool: 是否触发了暂停
        """
        self.count += 1
        
        if self.count >= self.threshold:
            pause_time = random.randint(API_PAUSE_MIN, API_PAUSE_MAX)
            log_message(f"已请求 {self.count} 次，暂停 {pause_time} 秒防封...", "warning", self.log_callback)
            time.sleep(pause_time)
            
            # 重置计数器和阈值
            self.count = 0
            self.threshold = random.randint(API_CALL_THRESHOLD_MIN, API_CALL_THRESHOLD_MAX)
            
            log_message(f"继续下载，下次暂停阈值: {self.threshold}", "info", self.log_callback)
            return True
        
        return False
    
    def reset(self):
        """重置计数器和阈值"""
        self.count = 0
        self.threshold = random.randint(API_CALL_THRESHOLD_MIN, API_CALL_THRESHOLD_MAX)


# ============================================================
# 文件名处理
# ============================================================

def sanitize_filename(filename):
    """
    清理文件名，移除非法字符
    
    Args:
        filename: 原始文件名
        
    Returns:
        str: 清理后的安全文件名（保留中日文字符）
    """
    if not filename:
        return "untitled"
    
    filename = urllib.parse.unquote(filename)
    valid_chars = f"-_.() {string.ascii_letters}{string.digits}"
    
    # 保留中日文字符
    sanitized = ''.join(
        c for c in filename 
        if c in valid_chars or '\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff'
    )
    
    # 限制长度
    if len(sanitized) > MAX_FILENAME_LENGTH:
        sanitized = sanitized[:MAX_FILENAME_LENGTH]
    
    return sanitized or "untitled"


# ============================================================
# 图片下载
# ============================================================

def _safe_int(value, default=None):
    """安全转换整数，失败时返回默认值。"""
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _load_json(path, default):
    """读取 JSON，失败时返回默认值。"""
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    """写入 JSON（自动创建目录）。"""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _escape_md(text):
    """转义 Markdown 表格中的特殊字符。"""
    if text is None:
        return ''
    return str(text).replace('|', '\\|').replace('\n', ' ')


def _extract_page_index(url):
    """从 URL 中提取页码，未匹配时返回 0。"""
    if not url:
        return 0
    match = re.search(r'_p(\d+)', url)
    return int(match.group(1)) if match else 0


def _build_common_headers(illust_id=None, user_agent=None, cookie=None):
    """构建请求头，保持与网页请求一致。"""
    ua = user_agent or HEADERS.get('User-Agent')
    referer = f'https://www.pixiv.net/artworks/{illust_id}' if illust_id else HEADERS.get('Referer')
    headers = {
        'User-Agent': ua,
        'Referer': referer,
        'Accept-Language': ACCEPT_LANGUAGE,
    }
    if cookie:
        headers['Cookie'] = cookie
    return headers


def _fetch_original_meta(illust_id, log_callback=None, user_agent=None, cookie=None):
    """
    通过 Pixiv 官方 Ajax 接口获取原图元数据。
    仅使用 body.urls.original 与 body.pageCount。
    """
    cache_key = f'{illust_id}|{user_agent or ""}|{cookie or ""}'
    with _META_CACHE_LOCK:
        cached = _META_CACHE.get(cache_key)
        if cached:
            return cached

    headers = _build_common_headers(illust_id=illust_id, user_agent=user_agent, cookie=cookie)
    headers['Accept'] = AJAX_ACCEPT
    url = f'https://www.pixiv.net/ajax/illust/{illust_id}'

    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    body = data.get('body') or {}
    urls = body.get('urls') or {}
    original = urls.get('original')
    page_count = _safe_int(body.get('pageCount'), 1) or 1

    if not original:
        raise ValueError(f'作品 {illust_id} 的 body.urls.original 为空')

    result = {'original': original, 'page_count': page_count}
    with _META_CACHE_LOCK:
        _META_CACHE[cache_key] = result
    return result


def _build_original_url_for_page(original_url, page_index):
    """根据页码生成目标页的 original 原图 URL。"""
    return re.sub(r'_p\d+', f'_p{page_index}', original_url)


def _build_save_path_for_page(save_path, page_index):
    """根据页码生成对应保存路径，优先替换文件名中的 _pN。"""
    base, ext = os.path.splitext(save_path)
    if re.search(r'_p\d+$', base):
        return re.sub(r'_p\d+$', f'_p{page_index}', base) + ext
    if page_index == 0:
        return save_path
    return f'{base}_p{page_index}{ext}'


def _get_remote_content_length(url, headers):
    """获取远程文件大小（字节），失败时返回 None。"""
    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=30)
        resp.raise_for_status()
        length = _safe_int(resp.headers.get('Content-Length'))
        resp.close()
        return length
    except Exception:
        return None


def _write_markdown_table(md_path, rows):
    """按表格格式重写 Markdown 下载记录。"""
    os.makedirs(os.path.dirname(md_path) or '.', exist_ok=True)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write('# 下载记录\n\n')
        f.write('| 文件名 | 作品ID | 作品链接 | 作品标题 | 画师ID | 画师名称 | 收藏数 | 是否AI生成 | 发布时间 | 原图链接 | 下载时间 | 下载状态 |\n')
        f.write('|--------|--------|----------|----------|--------|----------|--------|------------|----------|----------|----------|----------|\n')
        for row in rows:
            f.write(
                f"| {_escape_md(row.get('filename', ''))} | {_escape_md(row.get('illust_id', ''))} | "
                f"{_escape_md(row.get('illust_link', ''))} | {_escape_md(row.get('title', ''))} | "
                f"{_escape_md(row.get('artist_id', ''))} | {_escape_md(row.get('artist_name', ''))} | "
                f"{_escape_md(row.get('bookmarks', ''))} | {_escape_md(row.get('ai_flag', ''))} | "
                f"{_escape_md(row.get('publish_time', ''))} | {_escape_md(row.get('original_url', ''))} | "
                f"{_escape_md(row.get('download_time', ''))} | {_escape_md(row.get('status', ''))} |\n"
            )


def _record_item_to_md_row(item):
    """将 JSON 防重记录项转换为 Markdown 行。"""
    illust_id = item.get('illust_id', '')
    original_url = item.get('original_url', '')
    return {
        'filename': os.path.basename(item.get('file_path', '') or ''),
        'illust_id': str(illust_id),
        'illust_link': f'[链接](https://www.pixiv.net/artworks/{illust_id})' if illust_id else '',
        'title': item.get('title', '') or '',
        'artist_id': str(item.get('artist_id', '') or ''),
        'artist_name': item.get('artist_name', '') or '',
        'bookmarks': item.get('bookmarks', ''),
        'ai_flag': '是' if item.get('is_ai') else '否',
        'publish_time': item.get('publish_time', '') or '',
        'original_url': f'[链接]({original_url})' if original_url else '',
        'download_time': item.get('updated_at', '') or '',
        'status': item.get('status', '') or '',
    }


def _refresh_markdown_from_record(record_path, md_path):
    """根据 JSON 防重记录重建 Markdown（按时间倒序）。"""
    data = _load_json(record_path, {})
    rows = []
    for _, item in data.items():
        if isinstance(item, dict):
            rows.append(_record_item_to_md_row(item))
    rows.sort(key=lambda x: x.get('download_time', ''), reverse=True)
    _write_markdown_table(md_path, rows)


def _update_id_record(record_path, record_key, record_item):
    """更新作品页级防重 JSON 记录。"""
    data = _load_json(record_path, {})
    data[record_key] = record_item
    _save_json(record_path, data)


def _is_completed_record_valid(record_item):
    """检查记录项是否为有效完成状态。"""
    if not isinstance(record_item, dict):
        return False
    if record_item.get('status') != '下载完成':
        return False
    path = record_item.get('file_path')
    size = _safe_int(record_item.get('file_size'), 0) or 0
    return bool(path and os.path.exists(path) and os.path.getsize(path) == size and size > 0)


def _record_download_event(
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
):
    """同步写入 JSON 防重记录和 Markdown 下载记录。"""
    if not download_root:
        return

    # 核心记录键：作品ID + 页码
    record_key = f'{illust_id}_p{page_index}'
    file_size = os.path.getsize(save_path) if os.path.exists(save_path) else 0
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 标准化记录内容
    record_item = {
        'file_path': save_path,
        'status': status,
        'file_size': file_size,
        'updated_at': now,
        'illust_id': str(illust_id),
        'page': page_index,
        'title': title or '',
        'artist_id': str(artist_id or ''),
        'artist_name': artist_name or '',
        'bookmarks': bookmarks if bookmarks is not None else '',
        'is_ai': bool(is_ai),
        'publish_time': create_date[:10] if create_date else '',
        'original_url': original_url or '',
    }

    with _RECORD_LOCK:
        # 全局 JSON 防重记录
        global_record_path = os.path.join(download_root, GLOBAL_ID_RECORD_FILE)
        _update_id_record(global_record_path, record_key, record_item)

        # 全局 Markdown 总记录（由全局 JSON 重建，避免额外冗余 json 侧车文件）
        global_md_path = os.path.join(download_root, GLOBAL_MD_RECORD_FILE)
        _refresh_markdown_from_record(global_record_path, global_md_path)

        # 按画师分文件夹时写入画师级 JSON/MD
        if folder_by_user:
            artist_dir = os.path.dirname(save_path)
            artist_record_path = os.path.join(artist_dir, ARTIST_ID_RECORD_FILE)
            _update_id_record(artist_record_path, record_key, record_item)

            safe_artist_name = sanitize_filename(artist_name) or str(artist_id or 'unknown')
            artist_md_path = os.path.join(artist_dir, f'{safe_artist_name}_作品下载记录.md')
            _refresh_markdown_from_record(artist_record_path, artist_md_path)


def _check_record_skip(download_root, illust_id, page_index):
    """前置防重：根据 JSON 记录判断是否可直接跳过。"""
    if not download_root or not illust_id:
        return False
    record_key = f'{illust_id}_p{page_index}'
    record_path = os.path.join(download_root, GLOBAL_ID_RECORD_FILE)
    data = _load_json(record_path, {})
    item = data.get(record_key)
    return _is_completed_record_valid(item)


def clear_download_records(save_dir):
    """清理下载目录中的 JSON 防重记录和 Markdown 下载记录。"""
    if not save_dir or not os.path.exists(save_dir):
        return

    for root, _, files in os.walk(save_dir):
        for name in files:
            should_delete = (
                name in {GLOBAL_ID_RECORD_FILE, ARTIST_ID_RECORD_FILE, GLOBAL_MD_RECORD_FILE, GLOBAL_MD_RECORD_FILE + '.json'}
                or name == 'success_log.md'
                or name.endswith('_作品下载记录.md')
                or name.endswith('_作品下载记录.md.json')
            )
            if should_delete:
                try:
                    os.remove(os.path.join(root, name))
                except Exception:
                    pass

def download_image(
    url,
    save_path,
    log_callback=None,
    referer='https://www.pixiv.net/',
    retries=3,
    success_log_dir=None,
    illust_id=None,
    is_ai=None,
    bookmarks=None,
    create_date=None,
    page_index=None,
    title=None,
    artist_id=None,
    artist_name=None,
    download_root=None,
    folder_by_user=False,
    resume_enabled=False,
    original_enabled=False,
    multi_page=True,
    auth_cookie=None,
    user_agent=None,
    _expand_pages=True,
):
    """
    下载单张图片（支持查重、重试、PNG/JPG 自动切换）
    
    Args:
        url: 图片URL
        save_path: 保存路径
        log_callback: 日志回调函数或队列（可选）
        referer: Referer 请求头
        retries: 重试次数
        success_log_dir: 成功日志目录（可选，传入则记录到 success_log.md）
        illust_id: 作品ID（可选，用于生成 Pixiv 链接）
        is_ai: 是否AI作品（可选）
        bookmarks: 收藏数（可选）
        create_date: 作品发布时间（可选）
        
    Returns:
        bool: 是否下载成功
    """
    # 统一上下文数据
    page_idx = _safe_int(page_index, _extract_page_index(url)) or 0
    title = title or ''
    artist_id = str(artist_id or '')
    artist_name = artist_name or ''
    download_root = download_root or success_log_dir or os.path.dirname(save_path)
    status = '下载失败'

    # 第一层防重前置：在任何网络请求前先检查作品ID+页码记录。
    if resume_enabled and illust_id and _check_record_skip(download_root, illust_id, page_idx):
        log_message(f'记录命中，跳过下载: {illust_id}_p{page_idx}', 'info', log_callback)
        status = '跳过'
        _record_download_event(
            download_root, folder_by_user, save_path, illust_id, page_idx, title,
            artist_id, artist_name, bookmarks, is_ai, create_date, url, status
        )
        return True

    # 原图模式：强制走 Pixiv 官方 Ajax 接口 body.urls.original
    try_url = url
    if original_enabled and illust_id:
        try:
            if not auth_cookie:
                # 按需求严格执行：原图请求必须携带完整 Cookie。
                raise ValueError('原图模式缺少 Cookie，已中止下载。请在 pixiv_config.json 中配置 cookie 字段。')
            meta = _fetch_original_meta(illust_id, log_callback=log_callback, user_agent=user_agent, cookie=auth_cookie)
            page_count = _safe_int(meta.get('page_count'), 1) or 1

            # 多图模式由 Ajax pageCount 驱动，逐页触发下载。
            if _expand_pages and multi_page and page_idx == 0 and page_count > 1:
                results = []
                for idx in range(page_count):
                    page_save_path = _build_save_path_for_page(save_path, idx)
                    ok = download_image(
                        url,
                        page_save_path,
                        log_callback=log_callback,
                        referer=referer,
                        retries=retries,
                        success_log_dir=success_log_dir,
                        illust_id=illust_id,
                        is_ai=is_ai,
                        bookmarks=bookmarks,
                        create_date=create_date,
                        page_index=idx,
                        title=title,
                        artist_id=artist_id,
                        artist_name=artist_name,
                        download_root=download_root,
                        folder_by_user=folder_by_user,
                        resume_enabled=resume_enabled,
                        original_enabled=True,
                        multi_page=multi_page,
                        auth_cookie=auth_cookie,
                        user_agent=user_agent,
                        _expand_pages=False,
                    )
                    results.append(ok)
                return all(results)

            if page_idx >= page_count:
                log_message(f'页码超出范围，跳过: {illust_id}_p{page_idx}', 'warning', log_callback)
                status = '跳过'
                _record_download_event(
                    download_root, folder_by_user, save_path, illust_id, page_idx, title,
                    artist_id, artist_name, bookmarks, is_ai, create_date, '', status
                )
                return True

            if not multi_page and page_idx > 0:
                status = '跳过'
                _record_download_event(
                    download_root, folder_by_user, save_path, illust_id, page_idx, title,
                    artist_id, artist_name, bookmarks, is_ai, create_date, '', status
                )
                return True

            try_url = _build_original_url_for_page(meta['original'], page_idx)
        except Exception as e:
            log_message(f'获取官方原图地址失败: {e}', 'error', log_callback)
            _record_download_event(
                download_root, folder_by_user, save_path, illust_id, page_idx, title,
                artist_id, artist_name, bookmarks, is_ai, create_date, '', status
            )
            return False

    # 保留原有逻辑：按文件名存在且大小>0直接跳过。
    if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
        log_message(f'已存在，跳过: {os.path.basename(save_path)}', 'info', log_callback)
        status = '跳过'
        _record_download_event(
            download_root, folder_by_user, save_path, illust_id, page_idx, title,
            artist_id, artist_name, bookmarks, is_ai, create_date, try_url, status
        )
        return True

    # 下载请求头
    req_headers = _build_common_headers(illust_id=illust_id, user_agent=user_agent, cookie=auth_cookie)
    req_headers['Accept'] = IMAGE_ACCEPT
    if referer and referer != 'https://www.pixiv.net/':
        req_headers['Referer'] = referer

    # 第二层防重：本地文件大小与远程长度一致时直接跳过
    expected_size = _get_remote_content_length(try_url, req_headers)
    if os.path.exists(save_path) and expected_size and os.path.getsize(save_path) == expected_size:
        log_message(f'文件完整，跳过: {os.path.basename(save_path)}', 'info', log_callback)
        status = '跳过'
        _record_download_event(
            download_root, folder_by_user, save_path, illust_id, page_idx, title,
            artist_id, artist_name, bookmarks, is_ai, create_date, try_url, status
        )
        return True

    # 执行下载（支持 Range 断点续传）
    for attempt in range(1, retries + 1):
        try:
            os.makedirs(os.path.dirname(save_path) or '.', exist_ok=True)

            headers = dict(req_headers)
            write_mode = 'wb'
            local_size = os.path.getsize(save_path) if os.path.exists(save_path) else 0

            if resume_enabled and local_size > 0 and expected_size:
                if local_size < expected_size:
                    headers['Range'] = f'bytes={local_size}-'
                    write_mode = 'ab'
                elif local_size == expected_size:
                    status = '跳过'
                    _record_download_event(
                        download_root, folder_by_user, save_path, illust_id, page_idx, title,
                        artist_id, artist_name, bookmarks, is_ai, create_date, try_url, status
                    )
                    return True
                else:
                    os.remove(save_path)
                    local_size = 0

            resp = requests.get(try_url, headers=headers, timeout=60, stream=True)
            resp.raise_for_status()

            # 断点续传时要求服务端支持 206 或可容错 200
            if write_mode == 'ab' and resp.status_code == 200:
                # 服务端未返回分段，回退整文件重下
                write_mode = 'wb'

            with open(save_path, write_mode) as f:
                for chunk in resp.iter_content(8192):
                    if chunk:
                        f.write(chunk)

            current_size = os.path.getsize(save_path) if os.path.exists(save_path) else 0
            if expected_size and current_size != expected_size:
                raise ValueError(f'文件大小校验失败: local={current_size}, remote={expected_size}')
            if current_size <= 0:
                raise ValueError('文件大小为0')

            size_mb = current_size / 1024 / 1024
            log_message(f'下载成功: {os.path.basename(save_path)} ({size_mb:.2f} MB)', 'success', log_callback)

            # 兼容原有 success_log.md：默认关闭，避免与新记录体系重复。
            if ENABLE_LEGACY_SUCCESS_LOG and success_log_dir:
                write_success_log(success_log_dir, os.path.basename(save_path), try_url, illust_id, is_ai, bookmarks, create_date)

            status = '下载完成'
            _record_download_event(
                download_root, folder_by_user, save_path, illust_id, page_idx, title,
                artist_id, artist_name, bookmarks, is_ai, create_date, try_url, status
            )
            return True

        except Exception as e:
            log_message(f'下载失败 ({attempt}/{retries}): {e}', 'error', log_callback)
            if attempt < retries:
                time.sleep(2)

    log_message(f'最终失败: {try_url}', 'error', log_callback)
    status = '下载失败'
    _record_download_event(
        download_root, folder_by_user, save_path, illust_id, page_idx, title,
        artist_id, artist_name, bookmarks, is_ai, create_date, try_url, status
    )
    return False


# ============================================================
# 下载成功日志
# ============================================================

def write_success_log(log_dir, filename, url, illust_id=None, is_ai=None, bookmarks=None, create_date=None):
    """
    记录下载成功的文件到 success_log.md
    
    Args:
        log_dir: 日志文件所在目录（通常是画师文件夹或下载目录）
        filename: 下载的文件名
        url: 文件的原始URL
        illust_id: 作品ID（可选，用于生成 Pixiv 链接）
        is_ai: 是否AI作品（可选，True/False/None）
        bookmarks: 收藏数（可选）
        create_date: 作品发布时间（可选，格式如 2024-01-15T12:00:00+09:00）
        
    Returns:
        bool: 是否写入成功
    """
    if not log_dir:
        return False
    
    try:
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "success_log.md")
        
        # 检查文件是否存在，不存在则创建表头
        file_exists = os.path.exists(log_path)
        
        with open(log_path, 'a', encoding='utf-8') as f:
            if not file_exists:
                f.write("# 下载记录\n\n")
                f.write("| 文件名 | 作品链接 | 收藏数 | AI | 发布时间 | 原图 |\n")
                f.write("|--------|----------|--------|----|---------|----- |\n")
            
            # 生成 Pixiv 作品链接
            pixiv_link = ""
            if illust_id:
                pixiv_link = f"[{illust_id}](https://www.pixiv.net/artworks/{illust_id})"
            
            # AI作品标记
            ai_mark = ""
            if is_ai is True:
                ai_mark = "是"
            elif is_ai is False:
                ai_mark = "否"
            
            # 收藏数
            bookmark_str = str(bookmarks) if bookmarks is not None else ""
            
            # 发布时间（只取日期部分）
            date_str = ""
            if create_date:
                date_str = create_date[:10]  # 取 YYYY-MM-DD 部分
            
            # 写入记录
            f.write(f"| {filename} | {pixiv_link} | {bookmark_str} | {ai_mark} | {date_str} | [链接]({url}) |\n")
        
        return True
    except Exception:
        return False


# ============================================================
# 已下载作品ID管理（断点续传）
# ============================================================

def load_downloaded_ids(file_path):
    """
    加载已下载的作品ID列表
    
    Args:
        file_path: ID记录文件路径
        
    Returns:
        set: 已下载的作品ID集合（字符串）
    """
    downloaded_ids = set()
    
    if not file_path or not os.path.exists(file_path):
        return downloaded_ids
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and line.isdigit():
                    downloaded_ids.add(line)
    except Exception:
        pass
    
    return downloaded_ids


def save_downloaded_id(file_path, illust_id):
    """
    保存已下载的作品ID（追加模式）
    
    Args:
        file_path: ID记录文件路径
        illust_id: 作品ID
        
    Returns:
        bool: 是否保存成功
    """
    if not file_path or not illust_id:
        return False
    
    try:
        with open(file_path, 'a', encoding='utf-8') as f:
            f.write(f"{illust_id}\n")
        return True
    except Exception:
        return False


# ============================================================
# 用户文件夹检查
# ============================================================

def check_user_folder_exists(save_dir, user_id, user_name=None):
    """
    检查用户文件夹是否已存在
    
    Args:
        save_dir: 保存目录
        user_id: 用户ID
        user_name: 用户名（可选，未使用）
        
    Returns:
        str or None: 存在则返回文件夹路径，否则返回 None
    """
    if not save_dir or not os.path.exists(save_dir):
        return None
    
    user_id_str = str(user_id)
    
    # 检查以用户ID开头的文件夹
    for folder in os.listdir(save_dir):
        folder_path = os.path.join(save_dir, folder)
        if os.path.isdir(folder_path):
            if folder.startswith(f"{user_id_str}_") or folder == user_id_str:
                return folder_path
    
    return None


def get_existing_user_folders(save_dir):
    """
    获取保存目录中所有已存在的用户ID
    
    Args:
        save_dir: 保存目录
        
    Returns:
        set: 已存在的用户ID集合（字符串）
    """
    existing_ids = set()
    
    if not save_dir or not os.path.exists(save_dir):
        return existing_ids
    
    for folder in os.listdir(save_dir):
        folder_path = os.path.join(save_dir, folder)
        if os.path.isdir(folder_path):
            # 尝试提取用户ID（文件夹名格式：用户ID_用户名）
            parts = folder.split('_', 1)
            if parts[0].isdigit():
                existing_ids.add(parts[0])
    
    return existing_ids


# ============================================================
# 模块测试
# ============================================================

if __name__ == "__main__":
    # 测试日志函数
    log_message("这是一条成功消息", "success")
    log_message("这是一条错误消息", "error")
    log_message("这是一条警告消息", "warning")
    log_message("这是一条信息消息", "info")
    
    # 测试文件名清理
    print(f"\n文件名清理测试:")
    print(f"  'test<>file.jpg' -> '{sanitize_filename('test<>file.jpg')}'")
    print(f"  '测试文件.png' -> '{sanitize_filename('测试文件.png')}'")
    
    # 测试 API 计数器
    print(f"\nAPI 计数器测试:")
    counter = APICallCounter()
    print(f"  初始阈值: {counter.threshold}")
    counter.reset()
    print(f"  重置后阈值: {counter.threshold}")
