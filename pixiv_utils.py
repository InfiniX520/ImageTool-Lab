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
import time
import random
import string
import urllib.parse
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

def download_image(url, save_path, log_callback=None, referer='https://www.pixiv.net/', retries=3, success_log_dir=None, illust_id=None, is_ai=None, bookmarks=None, create_date=None):
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
    # 尝试的格式顺序：先 PNG 再 JPG
    urls_to_try = [url]
    if url.endswith('.jpg'):
        urls_to_try.insert(0, url.replace('.jpg', '.png'))
    elif url.endswith('.jpeg'):
        urls_to_try.insert(0, url.replace('.jpeg', '.png'))
    
    headers = HEADERS.copy()
    headers['Referer'] = referer
    
    for try_url in urls_to_try:
        # 根据实际URL调整保存路径的扩展名
        actual_save_path = save_path
        if try_url != url:
            actual_save_path = save_path.rsplit('.', 1)[0] + '.' + try_url.rsplit('.', 1)[1]
        
        # 文件查重：已存在且大小>0 则跳过
        if os.path.exists(actual_save_path) and os.path.getsize(actual_save_path) > 0:
            log_message(f"已存在，跳过: {os.path.basename(actual_save_path)}", "info", log_callback)
            return True
        
        for attempt in range(1, retries + 1):
            try:
                resp = requests.get(try_url, headers=headers, timeout=60, stream=True)
                if resp.status_code == 404 and try_url != url:
                    break  # PNG 不存在，尝试 JPG
                resp.raise_for_status()
                
                # 确保目录存在
                os.makedirs(os.path.dirname(actual_save_path) or '.', exist_ok=True)
                
                with open(actual_save_path, 'wb') as f:
                    for chunk in resp.iter_content(8192):
                        if chunk:
                            f.write(chunk)
                
                if os.path.getsize(actual_save_path) > 0:
                    size_mb = os.path.getsize(actual_save_path) / 1024 / 1024
                    log_message(f"下载成功: {os.path.basename(actual_save_path)} ({size_mb:.2f} MB)", "success", log_callback)
                    
                    # 记录到成功日志
                    if success_log_dir:
                        write_success_log(success_log_dir, os.path.basename(actual_save_path), try_url, illust_id, is_ai, bookmarks, create_date)
                    
                    return True
                else:
                    raise Exception("文件大小为0")
                    
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 404 and try_url != url:
                    break
                log_message(f"下载失败 ({attempt}/{retries}): {e}", "error", log_callback)
                if attempt < retries:
                    time.sleep(2)
            except Exception as e:
                log_message(f"下载失败 ({attempt}/{retries}): {e}", "error", log_callback)
                if attempt < retries:
                    time.sleep(2)
    
    log_message(f"最终失败: {url}", "error", log_callback)
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
