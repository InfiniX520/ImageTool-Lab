#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pixiv 图片下载器

功能:
    - 支持关键词搜索、用户作品、关注新作、排行榜等多种下载模式
    - GUI 界面，配置自动保存

依赖:
    pip install pixivpy3 requests

使用:
    python pixiv_downloader.py
"""

# ============================================================
# 导入模块
# ============================================================

import os
import sys
import time
import re
import json
import random
import queue
import threading
import subprocess
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox

# ============================================================
# 路径配置
# ============================================================

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "pixiv_config.json"
DOWNLOADED_IDS_FILE = ROOT / "downloaded_ids.txt"
CONFIG_VERSION = "1.2"

# ============================================================
# 模块导入
# ============================================================

try:
    from pixiv_multi_filter import open_multi_filter
except ImportError:
    open_multi_filter = None

from pixiv_utils import (
    download_image, log_message,
    load_downloaded_ids, save_downloaded_id,
    clear_download_records,
    DEBUG_MODE
)

from pixiv_fetcher import PixivFetcher, FetchConfig

try:
    from pixivpy3 import AppPixivAPI
except ImportError:
    AppPixivAPI = None


# ============================================================
# 主界面类
# ============================================================

class PixivDownloaderGUI:
    """Pixiv 下载器图形界面"""
    
    def __init__(self, root):
        self.root = root
        self.root.title("Pixiv 图片下载器")
        self.root.geometry("850x750")
        
        self.api = None
        self.auth_cookie = ""
        self.user_agent = ""
        self.running = False
        self.stop_event = threading.Event()
        self.log_queue = queue.Queue()
        
        self._build_ui()
        self._load_config()
        self.root.after(100, self._process_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # --------------------------------------------------------
    # 配置管理
    # --------------------------------------------------------
    
    def _load_config(self):
        """加载配置"""
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                
                self._set_entry(self.token_entry, cfg.get('token', ''))
                self._set_entry(self.dir_entry, cfg.get('save_dir', ''))
                self._set_entry(self.count_entry, cfg.get('count', '100'))
                self._set_entry(self.thread_entry, cfg.get('threads', '3'))
                self._set_entry(self.delay_entry, cfg.get('delay', '0.5'))
                self._set_entry(self.api_delay_entry, cfg.get('api_delay', '0.3'))
                self._set_entry(self.min_bookmarks_entry, cfg.get('min_bookmarks', '0'))
                self.auth_cookie = cfg.get('cookie', '')
                self.user_agent = cfg.get('user_agent', '')
                
                if cfg.get('name_template'):
                    self.name_template.set(cfg['name_template'])
                if cfg.get('folder_by_user') is not None:
                    self.folder_var.set(cfg['folder_by_user'])
                if cfg.get('skip_ai') is not None:
                    self.skip_ai_var.set(cfg['skip_ai'])
                if cfg.get('skip_downloaded') is not None:
                    self.skip_downloaded_var.set(cfg['skip_downloaded'])
        except Exception as e:
            log_message(f"加载配置失败: {e}", "warning")
    
    def _save_config(self):
        """保存配置"""
        try:
            # 保留未知字段，避免覆盖用户手动写入的扩展配置（如 cookie）。
            base_cfg = {}
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    base_cfg = json.load(f)

            config = {
                'version': CONFIG_VERSION,
                'token': self.token_entry.get().strip(),
                'save_dir': self.dir_entry.get().strip(),
                'count': self.count_entry.get().strip(),
                'threads': self.thread_entry.get().strip(),
                'delay': self.delay_entry.get().strip(),
                'api_delay': self.api_delay_entry.get().strip(),
                'name_template': self.name_template.get(),
                'folder_by_user': self.folder_var.get(),
                'min_bookmarks': self.min_bookmarks_entry.get().strip(),
                'skip_ai': self.skip_ai_var.get(),
                'skip_downloaded': self.skip_downloaded_var.get(),
                'cookie': base_cfg.get('cookie', self.auth_cookie),
                'user_agent': base_cfg.get('user_agent', self.user_agent),
            }
            base_cfg.update(config)
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(base_cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log_message(f"保存配置失败: {e}", "error")
    
    def _set_entry(self, entry, value):
        """设置输入框值"""
        entry.delete(0, tk.END)
        entry.insert(0, str(value))
    
    def _on_close(self):
        """关闭窗口"""
        self._save_config()
        self.root.destroy()

    # --------------------------------------------------------
    # 界面构建
    # --------------------------------------------------------
    
    def _build_ui(self):
        """构建界面"""
        self._build_login_ui()
        self._build_mode_ui()
        self._build_params_ui()
        self._build_options_ui()
        self._build_buttons_ui()
        self._build_log_ui()
    
    def _build_login_ui(self):
        """登录区域"""
        frame = ttk.Frame(self.root)
        frame.pack(pady=5, padx=10, fill='x')
        
        ttk.Label(frame, text="Refresh Token:").pack(side='left')
        self.token_entry = ttk.Entry(frame, show='*')
        self.token_entry.pack(side='left', fill='x', expand=True, padx=(5,0))
        ttk.Button(frame, text="登录", command=self._login).pack(side='left', padx=(5,0))
        self.login_status = ttk.Label(frame, text="未登录", foreground='red')
        self.login_status.pack(side='left', padx=(10,0))
    
    def _build_mode_ui(self):
        """下载模式区域"""
        mode_frame = ttk.LabelFrame(self.root, text="下载模式")
        mode_frame.pack(pady=5, padx=10, fill='x')
        
        self.mode_var = tk.StringVar(value="search")
        modes = [
            ("关键词搜索", "search"), ("用户作品", "user"),
            ("关注用户新作", "follow"), ("关注用户全部作品", "follow_all"),
            ("排行榜", "ranking")
        ]
        for text, value in modes:
            ttk.Radiobutton(mode_frame, text=text, variable=self.mode_var,
                           value=value, command=self._toggle_mode).pack(anchor='w', padx=10)
        
        # 搜索关键词
        self.search_frame = ttk.Frame(self.root)
        self.search_frame.pack(pady=5, padx=10, fill='x')
        ttk.Label(self.search_frame, text="搜索关键词:").pack(side='left')
        self.keyword_entry = ttk.Entry(self.search_frame)
        self.keyword_entry.pack(side='left', fill='x', expand=True)
        
        # 用户ID
        self.user_frame = ttk.Frame(self.root)
        ttk.Label(self.user_frame, text="用户ID:").pack(side='left')
        self.user_entry = ttk.Entry(self.user_frame)
        self.user_entry.pack(side='left', fill='x', expand=True, padx=(5,5))
        ttk.Button(self.user_frame, text="导入txt", command=self._import_user_ids).pack(side='left')
        
        # 排行榜类型
        self.rank_frame = ttk.Frame(self.root)
        ttk.Label(self.rank_frame, text="排行榜类型:").pack(side='left')
        self.rank_combo = ttk.Combobox(self.rank_frame, values=[
            # 插画榜
            'day', 'week', 'month',
            'day_male', 'day_female',
            'week_original', 'week_rookie',
            # R18榜
            'day_r18', 'day_male_r18', 'day_female_r18',
            'week_r18', 'week_r18g',
            # 漫画榜
            'day_manga', 'week_manga', 'month_manga',
            'week_rookie_manga'
        ], state='readonly', width=18)
        self.rank_combo.set('day')
        self.rank_combo.pack(side='left', padx=(5,15))
        
        # 排行榜日期
        ttk.Label(self.rank_frame, text="日期:").pack(side='left')
        self.rank_date_entry = ttk.Entry(self.rank_frame, width=12)
        self.rank_date_entry.pack(side='left', padx=(5,0))
        self.rank_date_entry.insert(0, "")  # 空表示今天
        ttk.Label(self.rank_frame, text="~").pack(side='left', padx=(3,3))
        self.rank_date_end_entry = ttk.Entry(self.rank_frame, width=12)
        self.rank_date_end_entry.pack(side='left', padx=(0,5))
        self.rank_date_end_entry.insert(0, "")  # 空表示单日
        
        # 日期提示（换行显示）
        self.rank_hint_frame = ttk.Frame(self.root)
        ttk.Label(self.rank_hint_frame, text="💡 日期格式: 2024-12-30 | 留空=今天 | 填两个日期=批量获取 (日榜按天/周榜按周/月榜按月)", 
                 foreground='gray').pack(anchor='w', padx=10)

    def _build_params_ui(self):
        """参数区域"""
        # 数量和收藏数
        frame = ttk.Frame(self.root)
        frame.pack(pady=5, padx=10, fill='x')
        ttk.Label(frame, text="下载数量:").pack(side='left')
        self.count_entry = ttk.Entry(frame, width=10)
        self.count_entry.pack(side='left')
        self.count_entry.insert(0, "100")
        ttk.Label(frame, text="最少收藏:").pack(side='left', padx=(15,0))
        self.min_bookmarks_entry = ttk.Entry(frame, width=10)
        self.min_bookmarks_entry.pack(side='left', padx=(5,0))
        self.min_bookmarks_entry.insert(0, "0")
        
        # 保存目录
        frame = ttk.Frame(self.root)
        frame.pack(pady=5, padx=10, fill='x')
        ttk.Label(frame, text="保存目录:").pack(side='left')
        self.dir_entry = ttk.Entry(frame)
        self.dir_entry.pack(side='left', fill='x', expand=True)
        ttk.Button(frame, text="浏览", command=self._browse_dir).pack(side='left', padx=(5,0))
        ttk.Button(frame, text="打开", command=self._open_dir).pack(side='left', padx=(5,0))
        
        # 下载控制
        ctrl_frame = ttk.LabelFrame(self.root, text="下载控制")
        ctrl_frame.pack(pady=5, padx=10, fill='x')
        frame = ttk.Frame(ctrl_frame)
        frame.pack(pady=5, padx=10, fill='x')
        
        ttk.Label(frame, text="线程数:").pack(side='left')
        self.thread_entry = ttk.Entry(frame, width=6)
        self.thread_entry.pack(side='left', padx=(5,15))
        self.thread_entry.insert(0, "3")
        
        ttk.Label(frame, text="下载间隔(秒):").pack(side='left')
        self.delay_entry = ttk.Entry(frame, width=6)
        self.delay_entry.pack(side='left', padx=(5,15))
        self.delay_entry.insert(0, "0.5")
        
        ttk.Label(frame, text="API间隔(秒):").pack(side='left')
        self.api_delay_entry = ttk.Entry(frame, width=6)
        self.api_delay_entry.pack(side='left')
        self.api_delay_entry.insert(0, "0.3")
        
        # 下载控制提示
        hint_frame = ttk.Frame(ctrl_frame)
        hint_frame.pack(pady=(0,5), padx=10, fill='x')
        ttk.Label(hint_frame, text="💡 实际间隔会加上0~1秒随机值防封 | API间隔会加上0~0.5秒随机值", 
                 foreground='gray').pack(anchor='w')
    
    def _build_options_ui(self):
        """选项区域"""
        opt_frame = ttk.LabelFrame(self.root, text="选项")
        opt_frame.pack(pady=5, padx=10, fill='x')
        
        # 第一行
        row1 = ttk.Frame(opt_frame)
        row1.pack(anchor='w', padx=10, pady=2)
        self.original_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="下载原图", variable=self.original_var).pack(side='left', padx=(0,15))
        self.multi_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="多图全部页", variable=self.multi_var).pack(side='left', padx=(0,15))
        self.folder_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="按画师分文件夹", variable=self.folder_var).pack(side='left')
        
        # 第二行
        row2 = ttk.Frame(opt_frame)
        row2.pack(anchor='w', padx=10, pady=2)
        self.skip_ai_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="跳过AI作品", variable=self.skip_ai_var).pack(side='left', padx=(0,15))
        self.skip_downloaded_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="断点续传", variable=self.skip_downloaded_var).pack(side='left', padx=(0,5))
        ttk.Button(row2, text="清空记录", command=self._clear_downloaded_ids).pack(side='left', padx=(5,0))
        
        # 命名模板
        name_frame = ttk.Frame(opt_frame)
        name_frame.pack(pady=5, padx=10, fill='x')
        ttk.Label(name_frame, text="命名模板:").pack(side='left')
        self.name_template = ttk.Combobox(name_frame, width=35, values=[
            "{id}_{title}_p{page}", "{user}_{id}_{title}_p{page}",
            "{id}_p{page}", "{date}_{id}_{title}_p{page}"
        ])
        self.name_template.pack(side='left', padx=(5,0))
        self.name_template.set("{id}_{title}_p{page}")
        
        # 命名模板提示
        template_hint = ttk.Frame(opt_frame)
        template_hint.pack(pady=(0,5), padx=10, fill='x')
        ttk.Label(template_hint, text="💡 可用变量: {id}作品ID {title}标题 {user}画师名 {user_id}画师ID {page}页码 {date}日期", 
                 foreground='gray').pack(anchor='w')

    def _build_buttons_ui(self):
        """按钮区域"""
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(pady=10)
        
        self.start_btn = ttk.Button(btn_frame, text="开始下载", command=self._start_download)
        self.start_btn.pack(side='left', padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self._stop_download, state='disabled')
        self.stop_btn.pack(side='left', padx=5)
        ttk.Button(btn_frame, text="清空日志", command=self._clear_log).pack(side='left', padx=5)
        ttk.Button(btn_frame, text="导出日志", command=self._export_log).pack(side='left', padx=5)
        ttk.Button(btn_frame, text="多条件筛选", command=self._open_multi_filter).pack(side='left', padx=5)
        
        # 调试模式
        import pixiv_utils
        self.debug_var = tk.BooleanVar(value=pixiv_utils.DEBUG_MODE)
        ttk.Checkbutton(btn_frame, text="调试", variable=self.debug_var,
                       command=self._toggle_debug).pack(side='left', padx=5)
        
        # 状态
        status_frame = ttk.Frame(self.root)
        status_frame.pack(pady=5, padx=10, fill='x')
        self.status_label = ttk.Label(status_frame, text="就绪", font=('', 10))
        self.status_label.pack(side='left')
    
    def _build_log_ui(self):
        """日志区域"""
        self.log_text = scrolledtext.ScrolledText(self.root, state='disabled', wrap='word', height=12)
        self.log_text.pack(pady=5, padx=10, fill='both', expand=True)

    # --------------------------------------------------------
    # 界面交互
    # --------------------------------------------------------
    
    def _toggle_mode(self):
        """切换下载模式"""
        mode = self.mode_var.get()
        self.search_frame.pack_forget()
        self.user_frame.pack_forget()
        self.rank_frame.pack_forget()
        self.rank_hint_frame.pack_forget()
        
        if mode == 'search':
            self.search_frame.pack(pady=5, padx=10, fill='x', after=self.root.children['!labelframe'])
        elif mode == 'user':
            self.user_frame.pack(pady=5, padx=10, fill='x', after=self.root.children['!labelframe'])
        elif mode == 'ranking':
            self.rank_frame.pack(pady=5, padx=10, fill='x', after=self.root.children['!labelframe'])
            self.rank_hint_frame.pack(pady=(0,5), padx=10, fill='x', after=self.rank_frame)
    
    def _browse_dir(self):
        d = filedialog.askdirectory()
        if d:
            self._set_entry(self.dir_entry, d)
    
    def _open_dir(self):
        save_dir = self.dir_entry.get().strip()
        if save_dir and os.path.exists(save_dir):
            if sys.platform == 'win32':
                os.startfile(save_dir)
            elif sys.platform == 'darwin':
                subprocess.run(['open', save_dir])
            else:
                subprocess.run(['xdg-open', save_dir])
    
    def _import_user_ids(self):
        """导入用户ID"""
        file_path = filedialog.askopenfilename(filetypes=[("文本文件", "*.txt")])
        if file_path:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    ids = re.findall(r'\d+', f.read())
                if ids:
                    self._set_entry(self.user_entry, ', '.join(ids))
                    messagebox.showinfo("成功", f"导入了 {len(ids)} 个用户ID")
            except Exception as e:
                messagebox.showerror("错误", f"读取失败: {e}")
    
    def _clear_log(self):
        self.log_text.config(state='normal')
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state='disabled')
    
    def _export_log(self):
        content = self.log_text.get(1.0, tk.END).strip()
        if not content:
            return
        file_path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialfile=f"pixiv_log_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        )
        if file_path:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            messagebox.showinfo("成功", f"日志已导出")
    
    def _toggle_debug(self):
        import pixiv_utils
        pixiv_utils.DEBUG_MODE = self.debug_var.get()
        self.log_queue.put(f"[*] 调试模式: {'开启' if self.debug_var.get() else '关闭'}")
    
    def _clear_downloaded_ids(self):
        if not messagebox.askyesno("确认", "确定清空下载记录？"):
            return

        try:
            if DOWNLOADED_IDS_FILE.exists():
                DOWNLOADED_IDS_FILE.unlink()

            save_dir = self.dir_entry.get().strip()
            if save_dir and os.path.exists(save_dir):
                clear_download_records(save_dir)

            messagebox.showinfo("成功", "已清空")
        except Exception as e:
            messagebox.showerror("错误", f"清空记录失败: {e}")
    
    def _open_multi_filter(self):
        if open_multi_filter is None:
            messagebox.showerror("错误", "多条件筛选模块未找到")
            return
        if self.api is None:
            messagebox.showwarning("提示", "请先登录")
            return
        config = {
            'save_dir': self.dir_entry.get().strip(),
            'threads': self.thread_entry.get().strip(),
            'delay': self.delay_entry.get().strip(),
        }
        open_multi_filter(self.root, self.api, config)
    
    def _process_log_queue(self):
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
        self.root.after(100, self._process_log_queue)

    # --------------------------------------------------------
    # 登录与下载
    # --------------------------------------------------------
    
    def _login(self):
        """登录 Pixiv"""
        if AppPixivAPI is None:
            messagebox.showerror("错误", "请先安装 pixivpy3")
            return
        
        token = self.token_entry.get().strip()
        if not token:
            messagebox.showerror("错误", "请输入 Refresh Token")
            return
        
        self.log_queue.put("[*] 正在登录...")
        
        def do_login():
            try:
                self.api = AppPixivAPI()
                self.api.auth(refresh_token=token)
                self._refresh_auth_context_from_api()
                self.log_queue.put(f"[+] 登录成功! 用户: {self.api.user_id}")
                self.root.after(0, lambda: self.login_status.config(text="已登录", foreground='green'))
            except Exception as e:
                self.log_queue.put(f"[-] 登录失败: {e}")
                self.api = None
        
        threading.Thread(target=do_login, daemon=True).start()

    def _refresh_auth_context_from_api(self):
        """尽量从已登录会话提取 Cookie/UA，提升网页端原图请求成功率。"""
        try:
            # 优先使用配置中的 UA，若没有再从会话中取。
            if not self.user_agent:
                self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

            # pixivpy3 内部字段版本差异较大，使用宽松探测。
            session = getattr(self.api, 'requests', None) or getattr(self.api, '_requests', None)
            if session and hasattr(session, 'cookies') and session.cookies:
                cookie_pairs = []
                for c in session.cookies:
                    if getattr(c, 'name', None) and getattr(c, 'value', None):
                        cookie_pairs.append(f"{c.name}={c.value}")
                if cookie_pairs:
                    self.auth_cookie = '; '.join(cookie_pairs)
                    self.log_queue.put("[*] 已从会话提取 Cookie")
        except Exception as e:
            self.log_queue.put(f"[!] 会话信息提取失败: {e}")
    
    def _start_download(self):
        """开始下载"""
        if self.running or self.api is None:
            if self.api is None:
                messagebox.showerror("错误", "请先登录")
            return
        
        save_dir = self.dir_entry.get().strip()
        if not save_dir:
            messagebox.showerror("错误", "请选择保存目录")
            return
        
        try:
            max_count = int(self.count_entry.get())
            max_workers = max(1, int(self.thread_entry.get()))
            delay = max(0, float(self.delay_entry.get()))
            api_delay = max(0, float(self.api_delay_entry.get()))
            min_bookmarks = max(0, int(self.min_bookmarks_entry.get() or 0))
        except ValueError:
            messagebox.showerror("错误", "参数无效")
            return
        
        os.makedirs(save_dir, exist_ok=True)
        
        self.running = True
        self.stop_event.clear()
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self._clear_log()
        self._save_config()
        
        # 更新状态栏
        self.status_label.config(text="正在准备下载...")
        
        # 构建配置
        config = FetchConfig(
            save_dir=save_dir,
            max_count=max_count,
            api_delay=api_delay,
            min_bookmarks=min_bookmarks,
            skip_ai=self.skip_ai_var.get(),
            original=self.original_var.get(),
            multi_page=self.multi_var.get(),
            folder_by_user=self.folder_var.get(),
            name_template=self.name_template.get(),
            mode=self.mode_var.get(),  # 传递当前下载模式
            keyword=self.keyword_entry.get().strip(),
            user_ids=self._parse_user_ids(),
            rank_mode=self.rank_combo.get(),
            rank_date=self.rank_date_entry.get().strip(),  # 排行榜开始日期
            rank_date_end=self.rank_date_end_entry.get().strip()  # 排行榜结束日期
        )
        
        self.log_queue.put(f"[*] 下载间隔: {delay}~{delay+1}秒 | API间隔: {api_delay}~{api_delay+0.5}秒")
        
        threading.Thread(
            target=self._download_task,
            daemon=True,
            args=(config, max_workers, delay)
        ).start()
    
    def _parse_user_ids(self):
        """解析用户ID"""
        text = self.user_entry.get().strip()
        if not text:
            return []
        return [uid.strip() for uid in re.split(r'[,\s\n]+', text) if uid.strip().isdigit()]
    
    def _stop_download(self):
        """停止下载"""
        if messagebox.askyesno("确认", "确定要停止下载吗？"):
            self.stop_event.set()
            self.log_queue.put("[!] 正在停止...")
            self.stop_btn.config(state='disabled')
    
    def _finish(self):
        """下载完成"""
        self.running = False
        self.root.after(0, lambda: self.start_btn.config(state='normal'))
        self.root.after(0, lambda: self.stop_btn.config(state='disabled'))

    # --------------------------------------------------------
    # 下载任务
    # --------------------------------------------------------
    
    def _download_task(self, config, max_workers, delay):
        """下载任务（生产者-消费者模式）"""
        try:
            download_queue = queue.Queue()
            fetch_done = threading.Event()
            
            stats = {'fetched': 0, 'downloaded': 0, 'failed': 0, 'skipped': 0, 
                    'low_bookmarks': 0, 'skipped_downloaded': 0}
            stats_lock = threading.Lock()
            
            # 加载已下载记录
            downloaded_ids = set()
            if self.skip_downloaded_var.get():
                downloaded_ids = load_downloaded_ids(DOWNLOADED_IDS_FILE)
                if downloaded_ids:
                    self.log_queue.put(f"[*] 已加载 {len(downloaded_ids)} 条下载记录")
            
            def update_status():
                text = f"获取: {stats['fetched']} | 下载: {stats['downloaded']} | 失败: {stats['failed']}"
                if stats['skipped'] > 0:
                    text += f" | 跳过AI: {stats['skipped']}"
                if stats['low_bookmarks'] > 0:
                    text += f" | 收藏不足: {stats['low_bookmarks']}"
                self.root.after(0, lambda: self.status_label.config(text=text))
            
            # 消费者：下载线程
            def downloader():
                while not (fetch_done.is_set() and download_queue.empty()):
                    if self.stop_event.is_set():
                        break
                    try:
                        (
                            url,
                            save_path,
                            illust_id,
                            log_dir,
                            is_ai,
                            bookmarks,
                            create_date,
                            page_index,
                            title,
                            artist_id,
                            artist_name,
                        ) = download_queue.get(timeout=0.5)
                        success = download_image(
                            url,
                            save_path,
                            self.log_queue,
                            success_log_dir=log_dir,
                            illust_id=illust_id,
                            is_ai=is_ai,
                            bookmarks=bookmarks,
                            create_date=create_date,
                            page_index=page_index,
                            title=title,
                            artist_id=artist_id,
                            artist_name=artist_name,
                            download_root=config.save_dir,
                            folder_by_user=config.folder_by_user,
                            resume_enabled=self.skip_downloaded_var.get(),
                            original_enabled=config.original,
                            multi_page=config.multi_page,
                            auth_cookie=self.auth_cookie,
                            user_agent=self.user_agent,
                        )
                        with stats_lock:
                            if success:
                                stats['downloaded'] += 1
                                if self.skip_downloaded_var.get():
                                    save_downloaded_id(DOWNLOADED_IDS_FILE, illust_id)
                            else:
                                stats['failed'] += 1
                        update_status()
                        time.sleep(delay + random.uniform(0, 1))
                    except queue.Empty:
                        continue
            
            # 启动下载线程
            threads = [threading.Thread(target=downloader, daemon=True) for _ in range(max_workers)]
            for t in threads:
                t.start()
            
            # 生产者：获取作品
            fetcher = PixivFetcher(
                api=self.api,
                config=config,
                log_callback=lambda msg: self.log_queue.put(msg),
                stop_event=self.stop_event
            )
            fetcher.fetch_by_mode(
                self.mode_var.get(), download_queue, stats, stats_lock, update_status, downloaded_ids
            )
            
            fetch_done.set()
            for t in threads:
                t.join()
            
            # 完成
            if not self.stop_event.is_set():
                msg = f"[+] 完成! 下载: {stats['downloaded']}, 失败: {stats['failed']}"
                self.log_queue.put(msg)
                self.root.after(0, lambda: self.status_label.config(text=f"完成 | 下载: {stats['downloaded']}"))
            else:
                self.root.after(0, lambda: self.status_label.config(text="已停止"))
                
        except Exception as e:
            self.log_queue.put(f"[-] 错误: {e}")
            import pixiv_utils
            if pixiv_utils.DEBUG_MODE:
                import traceback
                self.log_queue.put(f"[D] {traceback.format_exc()}")
        finally:
            self._finish()


# ============================================================
# 程序入口
# ============================================================

def main():
    if AppPixivAPI is None:
        print("请先安装 pixivpy3: pip install pixivpy3")
        return
    root = tk.Tk()
    PixivDownloaderGUI(root)
    root.mainloop()


if __name__ == '__main__':
    main()



