#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pixiv 作品获取模块

功能:
    - 封装各种下载模式的获取逻辑
    - 支持关键词搜索、用户作品、关注新作、排行榜等
    - 与 GUI 解耦，可独立使用

依赖:
    - pixivpy3
    - pixiv_utils
"""

import time
import random
import re
import os
from pathlib import Path

from pixiv_utils import (
    sanitize_filename,
    APICallCounter,
    DEBUG_MODE
)

import pixiv_utils  # 用于访问 DEBUG_MODE（可能被动态修改）

# ============================================================
# 配置类
# ============================================================

class FetchConfig:
    """获取配置"""
    
    def __init__(self, **kwargs):
        # 基础配置
        self.save_dir = kwargs.get('save_dir', '')
        self.max_count = kwargs.get('max_count', 100)
        self.api_delay = kwargs.get('api_delay', 0.3)
        self.min_bookmarks = kwargs.get('min_bookmarks', 0)
        
        # 筛选选项
        self.skip_ai = kwargs.get('skip_ai', False)
        self.original = kwargs.get('original', True)
        self.multi_page = kwargs.get('multi_page', True)
        self.folder_by_user = kwargs.get('folder_by_user', False)
        
        # 命名模板
        self.name_template = kwargs.get('name_template', '{id}_{title}_p{page}')
        
        # 模式参数
        self.mode = kwargs.get('mode', '')  # 当前下载模式
        self.keyword = kwargs.get('keyword', '')
        self.user_ids = kwargs.get('user_ids', [])
        self.rank_mode = kwargs.get('rank_mode', 'day')
        self.rank_date = kwargs.get('rank_date', '')  # 排行榜开始日期 (YYYY-MM-DD)
        self.rank_date_end = kwargs.get('rank_date_end', '')  # 排行榜结束日期 (YYYY-MM-DD)


# ============================================================
# 获取器类
# ============================================================

class PixivFetcher:
    """Pixiv 作品获取器"""
    
    def __init__(self, api, config, log_callback=None, stop_event=None):
        """
        初始化获取器
        
        参数:
            api: AppPixivAPI 实例
            config: FetchConfig 配置对象
            log_callback: 日志回调函数
            stop_event: 停止事件 (threading.Event)
        """
        self.api = api
        self.config = config
        self.log = log_callback or (lambda msg: print(msg))
        self.stop_event = stop_event
        
        # API 请求计数器
        self.api_counter = APICallCounter(log_callback=self.log)

    # --------------------------------------------------------
    # 公共接口
    # --------------------------------------------------------
    
    def fetch_by_mode(self, mode, download_queue, stats, stats_lock, update_status, downloaded_ids=None):
        """
        根据模式获取作品
        
        参数:
            mode: 下载模式 (search/user/follow/follow_all/ranking)
            download_queue: 下载队列
            stats: 统计字典
            stats_lock: 统计锁
            update_status: 状态更新回调
            downloaded_ids: 已下载作品ID集合
        
        返回:
            获取的作品数量
        """
        if downloaded_ids is None:
            downloaded_ids = set()
        
        if mode == 'search':
            if not self.config.keyword:
                self.log("[!] 请输入搜索关键词")
                return 0
            self.log(f"[*] 搜索: {self.config.keyword}")
            return self._fetch_and_queue(
                lambda: self.api.search_illust(self.config.keyword, search_target='partial_match_for_tags'),
                lambda r, qs: self.api.search_illust(**qs),
                download_queue, stats, stats_lock, update_status, downloaded_ids
            )
        
        elif mode == 'user':
            if not self.config.user_ids:
                self.log("[!] 请输入用户ID")
                return 0
            self.log(f"[*] 共 {len(self.config.user_ids)} 个用户，开始下载...")
            total = 0
            max_total = self.config.max_count
            skipped_users = []  # 记录被跳过的用户
            
            for i, user_id in enumerate(self.config.user_ids):
                if self._should_stop():
                    break
                if total >= max_total:
                    self.log(f"[*] 已达到下载数量限制 {max_total}，停止获取")
                    break
                self.log(f"[*] [{i+1}/{len(self.config.user_ids)}] 获取用户 {user_id} 的作品")
                # 临时修改 max_count 为剩余数量
                self.config.max_count = max_total - total
                count = self._fetch_and_queue(
                    lambda uid=user_id: self.api.user_illusts(uid),
                    lambda r, qs: self.api.user_illusts(**qs),
                    download_queue, stats, stats_lock, update_status, downloaded_ids
                )
                
                # 记录被跳过的用户
                if count == 0:
                    skipped_users.append(user_id)
                
                total += count
                
                # 每个用户之间增加延迟，防止 API 限流
                if i < len(self.config.user_ids) - 1:  # 不是最后一个用户
                    delay = random.uniform(10, 20)  # 10-20秒随机延迟
                    self.log(f"[*] 等待 {delay:.1f} 秒后获取下一个用户...")
                    time.sleep(delay)
            
            self.config.max_count = max_total
            
            # 保存被跳过的用户到文件
            if skipped_users:
                self._save_skipped_users(skipped_users)
            
            return total
        
        elif mode == 'ranking':
            date_start = self.config.rank_date.strip() if self.config.rank_date else None
            date_end = self.config.rank_date_end.strip() if self.config.rank_date_end else None
            
            # 如果有日期范围，循环获取每一天
            if date_start and date_end:
                return self._fetch_ranking_range(date_start, date_end, download_queue, stats, stats_lock, update_status, downloaded_ids)
            else:
                # 单日获取
                if date_start:
                    self.log(f"[*] 获取排行榜: {self.config.rank_mode} (日期: {date_start})")
                else:
                    self.log(f"[*] 获取排行榜: {self.config.rank_mode} (今天)")
                return self._fetch_and_queue(
                    lambda: self.api.illust_ranking(mode=self.config.rank_mode, date=date_start) if date_start else self.api.illust_ranking(mode=self.config.rank_mode),
                    lambda r, qs: self.api.illust_ranking(**qs),
                    download_queue, stats, stats_lock, update_status, downloaded_ids
                )
        
        elif mode == 'follow':
            self.log("[*] 获取关注用户新作")
            return self._fetch_and_queue(
                lambda: self.api.illust_follow(),
                lambda r, qs: self.api.illust_follow(**qs),
                download_queue, stats, stats_lock, update_status, downloaded_ids
            )
        
        elif mode == 'follow_all':
            return self._fetch_follow_all(download_queue, stats, stats_lock, update_status, downloaded_ids)
        
        return 0
    
    # --------------------------------------------------------
    # 内部方法
    # --------------------------------------------------------
    
    def _should_stop(self):
        """检查是否应该停止"""
        return self.stop_event and self.stop_event.is_set()
    
    def _get_ranking_step(self):
        """根据排行榜类型返回时间步长"""
        mode = self.config.rank_mode
        if mode in ('month', 'month_manga'):
            return 'month'
        elif mode.startswith('week'):
            return 'week'
        else:
            return 'day'
    
    def _fetch_ranking_range(self, date_start, date_end, download_queue, stats, stats_lock, update_status, downloaded_ids):
        """获取日期范围内的排行榜"""
        from datetime import datetime, timedelta
        from calendar import monthrange
        
        try:
            start = datetime.strptime(date_start, '%Y-%m-%d')
            end = datetime.strptime(date_end, '%Y-%m-%d')
        except ValueError:
            self.log("[-] 日期格式错误，请使用 YYYY-MM-DD 格式")
            return 0
        
        if start > end:
            start, end = end, start  # 自动交换
        
        step = self._get_ranking_step()
        
        # 计算总数
        if step == 'day':
            total_count = (end - start).days + 1
            step_name = "天"
        elif step == 'week':
            total_count = ((end - start).days // 7) + 1
            step_name = "周"
        else:  # month
            total_count = (end.year - start.year) * 12 + (end.month - start.month) + 1
            step_name = "月"
        
        self.log(f"[*] 批量获取排行榜: {self.config.rank_mode}")
        self.log(f"[*] 日期范围: {date_start} ~ {date_end} (共 {total_count} {step_name})")
        
        total_queued = 0
        current = start
        count_idx = 0
        
        while current <= end:
            if self._should_stop():
                break
            
            count_idx += 1
            date_str = current.strftime('%Y-%m-%d')
            self.log(f"[*] [{count_idx}/{total_count}] 获取 {date_str} 的排行榜")
            
            count = self._fetch_and_queue(
                lambda d=date_str: self.api.illust_ranking(mode=self.config.rank_mode, date=d),
                lambda r, qs: self.api.illust_ranking(**qs),
                download_queue, stats, stats_lock, update_status, downloaded_ids
            )
            
            total_queued += count
            self.log(f"[*] {date_str} 添加了 {count} 个到队列，累计 {total_queued}")
            
            # 根据步长计算下一个日期
            if step == 'day':
                current += timedelta(days=1)
            elif step == 'week':
                current += timedelta(weeks=1)
            else:  # month
                # 跳到下个月同一天（或月末）
                if current.month == 12:
                    next_year = current.year + 1
                    next_month = 1
                else:
                    next_year = current.year
                    next_month = current.month + 1
                # 处理月末情况（如1月31日 -> 2月28日）
                max_day = monthrange(next_year, next_month)[1]
                next_day = min(current.day, max_day)
                current = current.replace(year=next_year, month=next_month, day=next_day)
            
            # 每次之间增加延迟
            if current <= end and not self._should_stop():
                delay = random.uniform(3, 6)
                self.log(f"[*] 等待 {delay:.1f} 秒后获取下一个...")
                time.sleep(delay)
        
        return total_queued
    
    def _fetch_follow_all(self, download_queue, stats, stats_lock, update_status, downloaded_ids):
        """获取所有关注用户的作品"""
        self.log("[*] 获取关注用户列表...")
        following_ids = self._get_following_users()
        if not following_ids:
            self.log("[!] 未获取到关注用户")
            return 0
        
        self.log(f"[*] 共 {len(following_ids)} 个关注用户")
        total_queued = 0
        max_total = self.config.max_count  # 保存原始限制
        skipped_users = []  # 记录被跳过的用户
        
        for i, user_id in enumerate(following_ids):
            if self._should_stop():
                break
            if total_queued >= max_total:
                self.log(f"[*] 已达到下载数量限制 {max_total}，停止获取")
                break
            
            self.log(f"[*] [{i+1}/{len(following_ids)}] 获取用户 {user_id} 的作品")
            remaining = max_total - total_queued
            
            # 临时修改 max_count 为剩余数量
            self.config.max_count = remaining
            
            user_added = self._fetch_and_queue(
                lambda uid=user_id: self.api.user_illusts(uid),
                lambda r, qs: self.api.user_illusts(**qs),
                download_queue, stats, stats_lock, update_status, downloaded_ids
            )
            
            # 记录被跳过的用户（获取到0个作品且不是因为已下载）
            if user_added == 0:
                skipped_users.append(user_id)
            
            total_queued += user_added
            self.log(f"[*] 用户 {user_id} 添加了 {user_added} 个到队列，累计 {total_queued}/{max_total}")
            
            # 每个用户之间增加延迟，防止 API 限流
            if i < len(following_ids) - 1:  # 不是最后一个用户
                delay = random.uniform(10, 20)  # 10-20秒随机延迟
                self.log(f"[*] 等待 {delay:.1f} 秒后获取下一个用户...")
                time.sleep(delay)
        
        # 恢复原始限制
        self.config.max_count = max_total
        
        # 保存被跳过的用户到文件
        if skipped_users:
            self._save_skipped_users(skipped_users)
        
        return total_queued

    def _get_following_users(self):
        """获取关注用户列表"""
        user_ids = []
        try:
            if pixiv_utils.DEBUG_MODE:
                self.log(f"[D] 开始获取关注用户列表，用户ID: {self.api.user_id}")
            result = self.api.user_following(self.api.user_id)
            page = 1
            while result:
                if self._should_stop():
                    break
                if not hasattr(result, 'user_previews') or not result.user_previews:
                    if pixiv_utils.DEBUG_MODE:
                        self.log(f"[D] 第 {page} 页无用户数据")
                    break
                for user_preview in result.user_previews:
                    if hasattr(user_preview, 'user') and user_preview.user:
                        user_ids.append(str(user_preview.user.id))
                if pixiv_utils.DEBUG_MODE:
                    self.log(f"[D] 第 {page} 页获取 {len(result.user_previews)} 个用户，累计 {len(user_ids)}")
                if result.next_url:
                    next_qs = self.api.parse_qs(result.next_url)
                    result = self.api.user_following(**next_qs)
                    page += 1
                    time.sleep(0.5)
                else:
                    break
        except Exception as e:
            self.log(f"[-] 获取关注列表失败: {e}")
            if pixiv_utils.DEBUG_MODE:
                import traceback
                self.log(f"[D] {traceback.format_exc()}")
        return user_ids
    
    def _save_skipped_users(self, skipped_users):
        """保存被跳过的用户到文件"""
        if not skipped_users:
            return
        
        skipped_file = os.path.join(self.config.save_dir, "skipped_users.txt")
        try:
            # 读取已有的跳过用户（避免重复）
            existing = set()
            if os.path.exists(skipped_file):
                with open(skipped_file, 'r', encoding='utf-8') as f:
                    existing = set(line.strip() for line in f if line.strip())
            
            # 添加新的跳过用户
            new_skipped = [uid for uid in skipped_users if uid not in existing]
            if new_skipped:
                with open(skipped_file, 'a', encoding='utf-8') as f:
                    for uid in new_skipped:
                        f.write(f"{uid}\n")
                self.log(f"[!] {len(new_skipped)} 个用户被跳过，已保存到 skipped_users.txt")
                self.log(f"[*] 提示: 稍后可以用「用户作品」模式输入这些ID重新下载")
        except Exception as e:
            self.log(f"[-] 保存跳过用户失败: {e}")
    
    def _fetch_and_queue(self, first_fetch, next_fetch, download_queue, stats, stats_lock, update_status, downloaded_ids):
        """
        获取作品并添加到下载队列
        
        返回: 实际添加到队列的数量
        """
        queued_count = 0
        skipped_count = 0  # 跟踪跳过的数量
        
        if pixiv_utils.DEBUG_MODE:
            self.log(f"[D] 开始获取，最大数量: {self.config.max_count}")
        
        try:
            result = first_fetch()
        except Exception as e:
            self.log(f"[-] 获取失败: {e}")
            self._log_debug_exception()
            return 0
        
        if result is None:
            self.log("[-] API 返回空值")
            return 0
        
        if not hasattr(result, 'illusts') or not result.illusts:
            self.log(f"[!] API 返回空数据，开始重试...")
            
            # API 返回空数据，可能是被限流了，等待后重试
            retry_delays = [15, 30, 60]  # 15/30/60秒重试延迟
            for retry, delay in enumerate(retry_delays):
                self.log(f"[!] 等待 {delay} 秒后重试 ({retry + 1}/{len(retry_delays)})...")
                time.sleep(delay)
                try:
                    result = first_fetch()
                    if result and hasattr(result, 'illusts') and result.illusts:
                        self.log(f"[+] 重试成功，获取到 {len(result.illusts)} 个作品")
                        break
                    else:
                        self.log(f"[!] 重试 {retry + 1} 仍返回空数据")
                except Exception as e:
                    self.log(f"[-] 重试失败: {e}")
            
            # 重试后仍然为空
            if not result or not hasattr(result, 'illusts') or not result.illusts:
                self.log(f"[!] 该用户作品列表为空或被限流，跳过")
                return 0
        
        if pixiv_utils.DEBUG_MODE:
            illust_count = len(result.illusts)
            self.log(f"[D] 首次请求返回 {illust_count} 个作品")
        
        fetched = 0
        
        while result and hasattr(result, 'illusts') and result.illusts and fetched < self.config.max_count:
            if self._should_stop():
                break
            
            page_processed = 0  # 本页处理的作品数（包括跳过的）
            
            for illust in result.illusts:
                if fetched >= self.config.max_count or self._should_stop():
                    break
                
                page_processed += 1
                
                # 断点续传检查
                if downloaded_ids and str(illust.id) in downloaded_ids:
                    skipped_count += 1
                    with stats_lock:
                        stats['skipped_downloaded'] = stats.get('skipped_downloaded', 0) + 1
                    update_status()
                    if pixiv_utils.DEBUG_MODE:
                        self.log(f"[D] 跳过已下载: {illust.id}")
                    continue
                
                # 先做轻量预筛：用列表页已有字段先判断，避免对明显不符合条件的作品请求详情。
                pre_ai_type = getattr(illust, 'illust_ai_type', None)
                pre_bookmarks = getattr(illust, 'total_bookmarks', None)

                if self.config.skip_ai and pre_ai_type == 2:
                    title = getattr(illust, 'title', '') or ''
                    self.log(f"[*] 跳过AI作品: {illust.id} {title}")
                    fetched += 1
                    with stats_lock:
                        stats['fetched'] = fetched
                        stats['skipped'] = stats.get('skipped', 0) + 1
                    update_status()
                    self._api_sleep()
                    continue

                if self.config.min_bookmarks > 0 and pre_bookmarks is not None:
                    bookmarks = pre_bookmarks or 0
                    if bookmarks < self.config.min_bookmarks:
                        title = getattr(illust, 'title', '') or ''
                        if pixiv_utils.DEBUG_MODE:
                            self.log(f"[D] 收藏不足({bookmarks}<{self.config.min_bookmarks}): {illust.id} {title}")
                        else:
                            self.log(f"[*] 收藏不足({bookmarks}<{self.config.min_bookmarks}): {illust.id} {title}")
                        fetched += 1
                        with stats_lock:
                            stats['fetched'] = fetched
                            stats['low_bookmarks'] = stats.get('low_bookmarks', 0) + 1
                        update_status()
                        self._api_sleep()
                        continue

                # 只有通过预筛后才请求详情，减少无效 API 调用。
                try:
                    detail = self.api.illust_detail(illust.id)
                    if detail and hasattr(detail, 'illust') and detail.illust:
                        illust = detail.illust
                    self.api_counter.increment()
                    if pixiv_utils.DEBUG_MODE:
                        bookmarks = getattr(illust, 'total_bookmarks', 0) or 0
                        ai_type = getattr(illust, 'illust_ai_type', 0) or 0
                        self.log(f"[D] 作品详情: {illust.id} 收藏={bookmarks} AI类型={ai_type}")
                except Exception as e:
                    self.log(f"[!] 获取 {illust.id} 详情失败: {e}")

                # 详情后再做一次最终校验，避免列表页字段缺失或不准确。
                if self.config.skip_ai:
                    ai_type = getattr(illust, 'illust_ai_type', 0) or 0
                    if ai_type == 2:
                        title = getattr(illust, 'title', '') or ''
                        self.log(f"[*] 跳过AI作品: {illust.id} {title}")
                        fetched += 1
                        with stats_lock:
                            stats['fetched'] = fetched
                            stats['skipped'] = stats.get('skipped', 0) + 1
                        update_status()
                        self._api_sleep()
                        continue

                if self.config.min_bookmarks > 0:
                    bookmarks = getattr(illust, 'total_bookmarks', 0) or 0
                    if bookmarks < self.config.min_bookmarks:
                        title = getattr(illust, 'title', '') or ''
                        if pixiv_utils.DEBUG_MODE:
                            self.log(f"[D] 收藏不足({bookmarks}<{self.config.min_bookmarks}): {illust.id} {title}")
                        else:
                            self.log(f"[*] 收藏不足({bookmarks}<{self.config.min_bookmarks}): {illust.id} {title}")
                        fetched += 1
                        with stats_lock:
                            stats['fetched'] = fetched
                            stats['low_bookmarks'] = stats.get('low_bookmarks', 0) + 1
                        update_status()
                        self._api_sleep()
                        continue
                
                # 获取图片URL并加入队列
                urls = self._get_image_urls(illust)
                if pixiv_utils.DEBUG_MODE:
                    self.log(f"[D] 作品 {illust.id} 获取到 {len(urls)} 个图片URL")
                
                is_ai = getattr(illust, 'illust_ai_type', 0) == 2
                bookmarks = getattr(illust, 'total_bookmarks', 0) or 0
                create_date = getattr(illust, 'create_date', None)
                title = getattr(illust, 'title', '') or ''
                artist_id = ''
                artist_name = ''
                if hasattr(illust, 'user') and illust.user:
                    artist_id = str(getattr(illust.user, 'id', '') or '')
                    artist_name = getattr(illust.user, 'name', '') or ''
                
                for url, page_index in urls:
                    save_path = self._get_save_path(illust, url)
                    # 排行榜模式：log_dir 应该是实际保存目录（年/月/ 或 年/月/画师/）
                    if self.config.mode == 'ranking':
                        log_dir = os.path.dirname(save_path)
                    else:
                        log_dir = os.path.dirname(save_path) if self.config.folder_by_user else self.config.save_dir
                    download_queue.put(
                        (
                            url,
                            save_path,
                            illust.id,
                            log_dir,
                            is_ai,
                            bookmarks,
                            create_date,
                            page_index,
                            title,
                            artist_id,
                            artist_name,
                        )
                    )
                    queued_count += 1
                
                fetched += 1
                with stats_lock:
                    stats['fetched'] = fetched
                update_status()
                self._api_sleep()
            
            # 如果本页所有作品都被跳过，且还有下一页，继续获取
            if pixiv_utils.DEBUG_MODE and page_processed > 0 and queued_count == 0:
                self.log(f"[D] 本页 {page_processed} 个作品全部被跳过")
            
            # 获取下一页
            if result.next_url and fetched < self.config.max_count:
                next_qs = self.api.parse_qs(result.next_url)
                if pixiv_utils.DEBUG_MODE:
                    self.log(f"[D] 获取下一页，当前已获取 {fetched} 个，已跳过 {skipped_count} 个")
                try:
                    result = next_fetch(result, next_qs)
                    self.api_counter.increment()
                except Exception as e:
                    self.log(f"[-] 获取下一页失败: {e}")
                    self._log_debug_exception()
                    break
                if not result or not hasattr(result, 'illusts') or not result.illusts:
                    if pixiv_utils.DEBUG_MODE:
                        self.log(f"[D] API 返回空数据，已获取 {fetched} 条，已跳过 {skipped_count} 条")
                    break
                if pixiv_utils.DEBUG_MODE:
                    self.log(f"[D] 下一页返回 {len(result.illusts)} 个作品")
                self._api_sleep()
            else:
                if not result.next_url:
                    if pixiv_utils.DEBUG_MODE or skipped_count > 0:
                        self.log(f"[!] API 分页结束，已获取 {fetched} 条，已跳过 {skipped_count} 条")
                break
        
        if pixiv_utils.DEBUG_MODE:
            self.log(f"[D] 获取完成，共添加 {queued_count} 个到队列，跳过 {skipped_count} 个")
        
        return queued_count

    def _api_sleep(self):
        """API 请求延迟"""
        actual_delay = self.config.api_delay + random.uniform(0, 0.5)
        time.sleep(actual_delay)
    
    def _log_debug_exception(self):
        """记录调试异常"""
        if pixiv_utils.DEBUG_MODE:
            import traceback
            self.log(f"[D] {traceback.format_exc()}")
    
    # --------------------------------------------------------
    # 文件路径处理
    # --------------------------------------------------------
    
    def _get_image_urls(self, illust):
        """获取作品的图片URL列表"""
        urls = []
        
        if self.config.original:
            # 原图模式统一只入队首张，后续是否扩展多P由下载层基于 Ajax pageCount 决定。
            if hasattr(illust, 'meta_pages') and illust.meta_pages:
                first_page = illust.meta_pages[0]
                if hasattr(first_page, 'image_urls') and hasattr(first_page.image_urls, 'original') and first_page.image_urls.original:
                    urls.append((first_page.image_urls.original, 0))
            elif hasattr(illust, 'meta_single_page') and illust.meta_single_page:
                original_url = illust.meta_single_page.get('original_image_url')
                if original_url:
                    urls.append((original_url, 0))
            
            # 兜底
            if not urls and hasattr(illust, 'image_urls'):
                if hasattr(illust.image_urls, 'original') and illust.image_urls.original:
                    urls.append((illust.image_urls.original, 0))
                elif hasattr(illust.image_urls, 'large') and illust.image_urls.large:
                    urls.append((illust.image_urls.large, 0))
                    self.log(f"[!] 作品 {illust.id} 无法获取原图，使用大图")
        else:
            if hasattr(illust, 'image_urls') and hasattr(illust.image_urls, 'large') and illust.image_urls.large:
                urls.append((illust.image_urls.large, 0))
        
        return urls
    
    def _make_filename(self, illust, url):
        """根据模板生成文件名"""
        ext = url.split('.')[-1].split('?')[0]
        
        # 提取页码
        match = re.search(r'_p(\d+)', url)
        page = match.group(1) if match else "0"
        
        # 提取日期
        date_str = ""
        create_date = getattr(illust, 'create_date', None)
        if create_date:
            date_str = create_date[:10].replace('-', '')
        else:
            date_match = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', url)
            if date_match:
                date_str = date_match.group(1) + date_match.group(2) + date_match.group(3)
        
        # 获取画师信息
        user_name = ""
        user_id = ""
        if hasattr(illust, 'user') and illust.user:
            user_name = sanitize_filename(getattr(illust.user, 'name', '') or '')[:30]
            user_id = str(getattr(illust.user, 'id', '') or '')
        
        title = sanitize_filename(getattr(illust, 'title', '') or '')[:50]
        
        # 应用模板
        template = self.config.name_template or "{id}_{title}_p{page}"
        filename = template.format(
            id=illust.id,
            title=title,
            user=user_name,
            user_id=user_id,
            page=page,
            date=date_str
        )
        
        filename = sanitize_filename(filename)
        if not filename:
            filename = str(illust.id)
        
        return f"{filename}.{ext}"
    
    def _get_save_path(self, illust, url):
        """获取保存路径"""
        filename = self._make_filename(illust, url)
        
        # 排行榜模式：按年/月分文件夹
        if self.config.mode == 'ranking':
            # 获取作品发布日期
            create_date = getattr(illust, 'create_date', None)
            if create_date:
                # 格式: 2024-12-30T12:00:00+09:00
                year = create_date[:4]
                month = create_date[5:7]
            else:
                # 从URL提取日期作为备选
                import re
                date_match = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', url)
                if date_match:
                    year = date_match.group(1)
                    month = date_match.group(2)
                else:
                    # 使用当前日期
                    import datetime
                    now = datetime.datetime.now()
                    year = str(now.year)
                    month = f"{now.month:02d}"
            
            # 构建路径
            if self.config.folder_by_user and hasattr(illust, 'user') and illust.user:
                # 年/月/画师/
                user_id = getattr(illust.user, 'id', '') or ''
                user_name = getattr(illust.user, 'name', '') or ''
                user_folder = sanitize_filename(f"{user_id}_{user_name}")[:50]
                if not user_folder:
                    user_folder = str(user_id) if user_id else "unknown"
                folder_path = os.path.join(self.config.save_dir, year, month, user_folder)
            else:
                # 年/月/
                folder_path = os.path.join(self.config.save_dir, year, month)
            
            if not os.path.exists(folder_path):
                os.makedirs(folder_path)
            return os.path.join(folder_path, filename)
        
        # 其他模式：原有逻辑
        if self.config.folder_by_user and hasattr(illust, 'user') and illust.user:
            user_id = getattr(illust.user, 'id', '') or ''
            user_name = getattr(illust.user, 'name', '') or ''
            user_folder = sanitize_filename(f"{user_id}_{user_name}")[:50]
            if not user_folder:
                user_folder = str(user_id) if user_id else "unknown"
            folder_path = os.path.join(self.config.save_dir, user_folder)
            if not os.path.exists(folder_path):
                os.makedirs(folder_path)
            return os.path.join(folder_path, filename)
        else:
            return os.path.join(self.config.save_dir, filename)
