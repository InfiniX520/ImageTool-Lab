#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pixiv 多数据源交集下载器

功能:
    从多个数据来源获取作品，只下载同时出现在所有来源中的作品（交集）

使用场景:
    - 下载既在关注新作里又上了日榜的作品
    - 下载搜索结果与排行榜的交集
    
依赖:
    pip install pixivpy3 requests
"""

# ============================================================
# 导入模块
# ============================================================

# 标准库
import os
import sys
import json
import time
import random
import queue
import threading

# 第三方库
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 可选模块
try:
    from pixivpy3 import AppPixivAPI
except ImportError:
    AppPixivAPI = None

# 从工具模块导入公共函数
from pixiv_utils import (
    download_image, 
    sanitize_filename, 
    APICallCounter,
    get_existing_user_folders,
    log_message,
    log_exception,
    DEBUG_MODE
)


# ============================================================
# 多数据源交集对话框
# ============================================================

class MultiFilterDialog:
    """多数据源交集下载对话框"""
    
    def __init__(self, parent=None, api=None, config=None):
        self.api = api
        self.config = config or {}
        
        # 创建窗口
        if parent:
            self.root = tk.Toplevel(parent)
        else:
            self.root = tk.Tk()
        
        self.root.title("Pixiv 多数据源交集下载")
        self.root.geometry("650x550")
        
        # 状态变量
        self.running = False
        self.stop_event = threading.Event()
        self.log_queue = queue.Queue()
        
        self._build_ui()
        self.root.after(100, self.process_log_queue)
    
    # --------------------------------------------------------
    # 界面构建
    # --------------------------------------------------------
    
    def _build_ui(self):
        """构建用户界面"""
        # 说明
        ttk.Label(
            self.root, 
            text="多数据源交集：只下载同时出现在所有选中来源中的作品", 
            foreground='blue', 
            font=('', 10)
        ).pack(pady=10, padx=10)
        
        self._build_source_ui()
        self._build_filter_ui()
        self._build_settings_ui()
        self._build_buttons_ui()
    
    def _build_source_ui(self):
        """构建数据来源选择区域"""
        source_frame = ttk.LabelFrame(self.root, text="数据来源（勾选多个，取交集）")
        source_frame.pack(pady=5, padx=10, fill='x')
        
        # 关注新作
        self.follow_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            source_frame, text="关注用户新作", variable=self.follow_var
        ).pack(anchor='w', padx=10, pady=2)
        
        # 排行榜
        row_rank = ttk.Frame(source_frame)
        row_rank.pack(anchor='w', padx=10, pady=2, fill='x')
        self.ranking_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_rank, text="排行榜:", variable=self.ranking_var).pack(side='left')
        self.rank_combo = ttk.Combobox(row_rank, values=[
            'day', 'week', 'month', 'day_male', 'day_female', 
            'week_original', 'week_rookie',
            'day_r18', 'day_male_r18', 'day_female_r18', 'week_r18',
            'day_manga', 'week_manga', 'month_manga'
        ], state='readonly', width=18)
        self.rank_combo.set('day')
        self.rank_combo.pack(side='left', padx=(5,0))
        
        # 搜索
        row_search = ttk.Frame(source_frame)
        row_search.pack(anchor='w', padx=10, pady=2, fill='x')
        self.search_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_search, text="搜索关键词:", variable=self.search_var).pack(side='left')
        self.search_entry = ttk.Entry(row_search, width=30)
        self.search_entry.pack(side='left', padx=(5,0))
        
        # 指定用户
        row_user = ttk.Frame(source_frame)
        row_user.pack(anchor='w', padx=10, pady=2, fill='x')
        self.user_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_user, text="指定用户ID:", variable=self.user_var).pack(side='left')
        self.user_entry = ttk.Entry(row_user, width=30)
        self.user_entry.pack(side='left', padx=(5,0))
        ttk.Label(row_user, text="(多个用逗号分隔)", foreground='gray').pack(side='left', padx=(5,0))
    
    def _build_filter_ui(self):
        """构建筛选条件区域"""
        filter_frame = ttk.LabelFrame(self.root, text="附加筛选（可选）")
        filter_frame.pack(pady=5, padx=10, fill='x')
        
        row_filter = ttk.Frame(filter_frame)
        row_filter.pack(pady=5, padx=10, fill='x')
        ttk.Label(row_filter, text="最少收藏:").pack(side='left')
        self.bookmark_entry = ttk.Entry(row_filter, width=10)
        self.bookmark_entry.pack(side='left', padx=(5,15))
        self.bookmark_entry.insert(0, "0")
        
        self.skip_ai_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_filter, text="跳过AI作品", variable=self.skip_ai_var).pack(side='left')
        
        # 说明：文件查重由下载函数自动处理
        hint_frame = ttk.Frame(filter_frame)
        hint_frame.pack(pady=(0,5), padx=10, fill='x')
        ttk.Label(hint_frame, text="💡 已存在的文件会自动跳过（基于文件名查重）", 
                 foreground='gray').pack(anchor='w')
    
    def _build_settings_ui(self):
        """构建下载设置区域"""
        setting_frame = ttk.Frame(self.root)
        setting_frame.pack(pady=10, padx=10, fill='x')
        
        ttk.Label(setting_frame, text="每个来源获取:").pack(side='left')
        self.count_entry = ttk.Entry(setting_frame, width=8)
        self.count_entry.pack(side='left', padx=(5,15))
        self.count_entry.insert(0, "200")
        ttk.Label(setting_frame, text="(建议200以上，交集后数量会减少)", foreground='gray').pack(side='left')
        
        dir_frame = ttk.Frame(self.root)
        dir_frame.pack(pady=5, padx=10, fill='x')
        ttk.Label(dir_frame, text="保存目录:").pack(side='left')
        self.dir_entry = ttk.Entry(dir_frame)
        self.dir_entry.pack(side='left', padx=(5,5), fill='x', expand=True)
        if self.config.get('save_dir'):
            self.dir_entry.insert(0, self.config['save_dir'])
        ttk.Button(dir_frame, text="浏览", command=self.browse_dir).pack(side='left')
    
    def _build_buttons_ui(self):
        """构建按钮区域"""
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(pady=10)
        self.start_btn = ttk.Button(btn_frame, text="开始交集下载", command=self.start)
        self.start_btn.pack(side='left', padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self.stop, state='disabled')
        self.stop_btn.pack(side='left', padx=5)
        
        # 调试模式
        import pixiv_utils
        self.debug_var = tk.BooleanVar(value=pixiv_utils.DEBUG_MODE)
        ttk.Checkbutton(btn_frame, text="调试日志", variable=self.debug_var,
                       command=self._toggle_debug).pack(side='left', padx=5)
        
        # 状态
        self.status_label = ttk.Label(self.root, text="就绪")
        self.status_label.pack(pady=5)
        
        # 日志
        self.log_text = scrolledtext.ScrolledText(self.root, height=10, state='disabled')
        self.log_text.pack(pady=5, padx=10, fill='both', expand=True)
    
    def _toggle_debug(self):
        """切换调试模式"""
        import pixiv_utils
        pixiv_utils.DEBUG_MODE = self.debug_var.get()
        if self.debug_var.get():
            log_message("调试模式已开启，将显示详细错误信息", "info", self.log_queue)
        else:
            log_message("调试模式已关闭", "info", self.log_queue)

    # --------------------------------------------------------
    # 界面交互
    # --------------------------------------------------------
    
    def browse_dir(self):
        """浏览选择保存目录"""
        d = filedialog.askdirectory()
        if d:
            self.dir_entry.delete(0, tk.END)
            self.dir_entry.insert(0, d)
    
    def start(self):
        """开始下载"""
        if not self.api:
            messagebox.showerror("错误", "未登录，请先在主界面登录")
            return
        
        # 检查至少选了2个数据来源
        sources = []
        if self.follow_var.get():
            sources.append('follow')
        if self.ranking_var.get():
            sources.append('ranking')
        if self.search_var.get() and self.search_entry.get().strip():
            sources.append('search')
        if self.user_var.get() and self.user_entry.get().strip():
            sources.append('user')
        
        if len(sources) < 2:
            messagebox.showwarning("提示", "请至少选择2个数据来源才能取交集\n如果只需要单个来源，请使用主界面")
            return
        
        save_dir = self.dir_entry.get().strip()
        if not save_dir:
            messagebox.showerror("错误", "请选择保存目录")
            return
        
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        
        self.running = True
        self.stop_event.clear()
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        
        self.log_text.config(state='normal')
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state='disabled')
        
        threading.Thread(target=self._run, daemon=True, args=(sources, save_dir)).start()
    
    def stop(self):
        """停止下载（带确认弹窗）"""
        if messagebox.askyesno("确认", "确定要停止下载吗？"):
            self.stop_event.set()
            log_message("正在停止...", "warning", self.log_queue)
            self.stop_btn.config(state='disabled')
    
    def finish(self):
        """完成后的清理"""
        self.running = False
        self.root.after(0, lambda: self.start_btn.config(state='normal'))
        self.root.after(0, lambda: self.stop_btn.config(state='disabled'))
        self.root.after(0, lambda: self.status_label.config(text="就绪"))
    
    def process_log_queue(self):
        """处理日志队列"""
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.config(state='normal')
                self.log_text.insert(tk.END, msg + "\n")
                self.log_text.see(tk.END)
                self.log_text.config(state='disabled')
        except queue.Empty:
            pass
        self.root.after(100, self.process_log_queue)
    
    def run(self):
        """独立运行时的主循环"""
        self.root.mainloop()

    # --------------------------------------------------------
    # 核心逻辑
    # --------------------------------------------------------
    
    def _run(self, sources, save_dir):
        """执行交集下载"""
        try:
            max_count = int(self.count_entry.get() or 200)
            min_bookmarks = int(self.bookmark_entry.get() or 0)
            
            log_message(f"数据来源: {', '.join(sources)}", "info", self.log_queue)
            log_message(f"每个来源获取 {max_count} 个作品", "info", self.log_queue)
            
            # API 请求计数器（防封）
            self.api_counter = APICallCounter(log_callback=lambda msg: self.log_queue.put(msg))
            
            # 从各个来源获取作品ID集合
            source_sets = {}
            source_illusts = {}
            
            for source in sources:
                if self.stop_event.is_set():
                    break
                
                self.root.after(0, lambda s=source: self.status_label.config(text=f"获取: {s}"))
                log_message(f"--- 获取 {source} ---", "info", self.log_queue)
                
                illusts = self._fetch_source(source, max_count)
                ids = set(str(i.id) for i in illusts)
                source_sets[source] = ids
                
                for i in illusts:
                    source_illusts[str(i.id)] = i
                
                log_message(f"{source}: 获取到 {len(ids)} 个作品", "success", self.log_queue)
            
            if self.stop_event.is_set():
                self.finish()
                return
            
            # 计算交集
            log_message("--- 计算交集 ---", "info", self.log_queue)
            intersection = None
            for source, ids in source_sets.items():
                if intersection is None:
                    intersection = ids
                else:
                    intersection = intersection & ids
                log_message(f"与 {source} 交集后: {len(intersection)} 个", "info", self.log_queue)
            
            if not intersection:
                log_message("交集为空，没有同时出现在所有来源中的作品", "warning", self.log_queue)
                messagebox.showinfo("结果", "交集为空\n没有同时出现在所有选中来源中的作品")
                self.finish()
                return
            
            log_message(f"最终交集: {len(intersection)} 个作品", "success", self.log_queue)
            
            # 应用附加筛选
            filtered_ids = []
            for illust_id in intersection:
                illust = source_illusts.get(illust_id)
                if not illust:
                    continue
                
                if min_bookmarks > 0:
                    bookmarks = getattr(illust, 'total_bookmarks', 0) or 0
                    if bookmarks < min_bookmarks:
                        continue
                
                if self.skip_ai_var.get():
                    if getattr(illust, 'illust_ai_type', 0) == 2:
                        continue
                
                filtered_ids.append(illust_id)
            
            if len(filtered_ids) < len(intersection):
                log_message(f"附加筛选后: {len(filtered_ids)} 个作品", "info", self.log_queue)
            
            if not filtered_ids:
                log_message("筛选后没有符合条件的作品", "warning", self.log_queue)
                self.finish()
                return
            
            # 下载
            log_message(f"--- 开始下载 {len(filtered_ids)} 个作品 ---", "info", self.log_queue)
            self._download_all([source_illusts[i] for i in filtered_ids], save_dir)
            
            log_message("完成!", "success", self.log_queue)
            
        except Exception as e:
            log_exception(e, "执行出错", self.log_queue)
        finally:
            self.finish()

    # --------------------------------------------------------
    # 数据获取
    # --------------------------------------------------------
    
    def _fetch_source(self, source, max_count):
        """从指定来源获取作品列表"""
        illusts = []
        
        if source == 'follow':
            try:
                result = self.api.illust_follow()
                if hasattr(self, 'api_counter'):
                    self.api_counter.increment()
            except Exception as e:
                log_message(f"获取关注新作失败: {e}", "error", self.log_queue)
                return illusts
            while result and hasattr(result, 'illusts') and result.illusts and len(illusts) < max_count:
                if self.stop_event.is_set():
                    break
                for illust in result.illusts:
                    if len(illusts) >= max_count:
                        break
                    illusts.append(illust)
                if result.next_url:
                    next_qs = self.api.parse_qs(result.next_url)
                    result = self.api.illust_follow(**next_qs)
                    if hasattr(self, 'api_counter'):
                        self.api_counter.increment()
                    time.sleep(0.3 + random.uniform(0, 0.5))
                else:
                    break
        
        elif source == 'ranking':
            mode = self.rank_combo.get()
            try:
                result = self.api.illust_ranking(mode=mode)
                if hasattr(self, 'api_counter'):
                    self.api_counter.increment()
            except Exception as e:
                log_message(f"获取排行榜失败: {e}", "error", self.log_queue)
                return illusts
            while result and hasattr(result, 'illusts') and result.illusts and len(illusts) < max_count:
                if self.stop_event.is_set():
                    break
                for illust in result.illusts:
                    if len(illusts) >= max_count:
                        break
                    illusts.append(illust)
                if result.next_url:
                    next_qs = self.api.parse_qs(result.next_url)
                    try:
                        result = self.api.illust_ranking(**next_qs)
                        if hasattr(self, 'api_counter'):
                            self.api_counter.increment()
                    except Exception:
                        break
                    time.sleep(0.3 + random.uniform(0, 0.5))
                else:
                    break
        
        elif source == 'search':
            keyword = self.search_entry.get().strip()
            try:
                result = self.api.search_illust(keyword, search_target='partial_match_for_tags')
                if hasattr(self, 'api_counter'):
                    self.api_counter.increment()
            except Exception as e:
                log_message(f"搜索失败: {e}", "error", self.log_queue)
                return illusts
            while result and hasattr(result, 'illusts') and result.illusts and len(illusts) < max_count:
                if self.stop_event.is_set():
                    break
                for illust in result.illusts:
                    if len(illusts) >= max_count:
                        break
                    illusts.append(illust)
                if result.next_url:
                    next_qs = self.api.parse_qs(result.next_url)
                    try:
                        result = self.api.search_illust(**next_qs)
                        if hasattr(self, 'api_counter'):
                            self.api_counter.increment()
                    except Exception:
                        break
                    time.sleep(0.3 + random.uniform(0, 0.5))
                else:
                    break
        
        elif source == 'user':
            user_ids = [u.strip() for u in self.user_entry.get().split(',') if u.strip()]
            for uid in user_ids:
                if self.stop_event.is_set() or len(illusts) >= max_count:
                    break
                try:
                    result = self.api.user_illusts(uid)
                    if hasattr(self, 'api_counter'):
                        self.api_counter.increment()
                except Exception as e:
                    log_message(f"获取用户 {uid} 作品失败: {e}", "error", self.log_queue)
                    continue
                if result and hasattr(result, 'illusts') and result.illusts:
                    for illust in result.illusts:
                        if len(illusts) >= max_count:
                            break
                        illusts.append(illust)
                time.sleep(0.3 + random.uniform(0, 0.5))
        
        return illusts
    
    def _download_all(self, illusts, save_dir):
        """下载所有作品"""
        total = len(illusts)
        downloaded = 0
        failed = 0
        
        for i, illust in enumerate(illusts):
            if self.stop_event.is_set():
                break
            
            self.root.after(0, lambda n=i+1: self.status_label.config(text=f"下载 {n}/{total}"))
            
            # 获取详情
            try:
                detail = self.api.illust_detail(illust.id)
                if detail and detail.illust:
                    illust = detail.illust
                if hasattr(self, 'api_counter'):
                    self.api_counter.increment()
            except Exception:
                pass
            
            # 获取图片URL
            urls = []
            if illust.meta_pages:
                for page in illust.meta_pages:
                    if hasattr(page.image_urls, 'original'):
                        urls.append(page.image_urls.original)
            elif illust.meta_single_page.get('original_image_url'):
                urls.append(illust.meta_single_page['original_image_url'])
            
            if not urls:
                continue
            
            # 下载第一张
            url = urls[0]
            ext = url.split('.')[-1].split('?')[0]
            title = sanitize_filename(illust.title)[:30] if illust.title else ''
            filename = f"{illust.id}_{title}.{ext}"
            save_path = os.path.join(save_dir, filename)
            
            # 获取作品信息
            is_ai = getattr(illust, 'illust_ai_type', 0) == 2
            bookmarks = getattr(illust, 'total_bookmarks', 0) or 0
            create_date = getattr(illust, 'create_date', None)
            
            if download_image(
                url,
                save_path,
                self.log_queue,
                success_log_dir=save_dir,
                illust_id=illust.id,
                is_ai=is_ai,
                bookmarks=bookmarks,
                create_date=create_date,
                page_index=0,
                title=getattr(illust, 'title', '') or '',
                artist_id=str(getattr(getattr(illust, 'user', None), 'id', '') or ''),
                artist_name=getattr(getattr(illust, 'user', None), 'name', '') or '',
                download_root=save_dir,
                folder_by_user=False,
                resume_enabled=False,
                original_enabled=True,
                multi_page=False,
            ):
                downloaded += 1
            else:
                failed += 1
            
            # 下载延迟：与主程序保持一致
            time.sleep(0.5 + random.uniform(0, 1))
        
        log_message(f"下载完成: {downloaded}/{total}" + (f", 失败: {failed}" if failed > 0 else ""), "success", self.log_queue)


# ============================================================
# 公共接口
# ============================================================

def open_multi_filter(parent=None, api=None, config=None):
    """
    从主程序调用此函数打开多条件筛选窗口
    
    Args:
        parent: 父窗口
        api: 已登录的 Pixiv API 对象
        config: 配置字典
        
    Returns:
        MultiFilterDialog 实例
    """
    dialog = MultiFilterDialog(parent, api, config)
    return dialog


# ============================================================
# 程序入口
# ============================================================

if __name__ == '__main__':
    if AppPixivAPI is None:
        print("[-] 请先安装 pixivpy3: pip install pixivpy3")
    else:
        print("[*] 独立运行模式")
        print("[!] 注意: 独立运行时需要手动登录，请从主程序启动")
        print("[*] 主程序: python pixiv_downloader.py")
        print()
        
        # 尝试加载已保存的 token
        from pathlib import Path
        config_file = Path(__file__).parent / "pixiv_config.json"
        api = None
        
        if config_file.exists():
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                token = cfg.get('token', '')
                if token:
                    print("[*] 尝试使用已保存的 token 登录...")
                    api = AppPixivAPI()
                    api.auth(refresh_token=token)
                    print(f"[+] 登录成功! 用户ID: {api.user_id}")
            except Exception as e:
                print(f"[-] 自动登录失败: {e}")
                print("[!] 请从主程序登录后再使用此功能")
        
        dialog = MultiFilterDialog(api=api)
        dialog.run()
