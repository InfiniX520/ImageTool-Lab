#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P6 轻量原生收藏界面（隔离样机）。

只使用 Tk/ttk 与标准库：没有图片、缩略图、头像、字体下载、网络、目录扫描、
后台线程、after 定时器或业务读写。所有页面数据都由外部 controller 通过
纯数据方法传入，本视图只负责显示与把用户动作回传。

``CollectionView`` 不 pack/grid 顶层自身，由调用者决定放置；不创建第二个
Tk 根窗口；所有公开方法必须在创建它的唯一 Tk 主线程调用。
"""

from __future__ import annotations

import threading
import tkinter as tk
from itertools import islice
from tkinter import ttk
from typing import Any, Callable

from core.log_view import ViewDelta
from gui.collection_theme import apply_collection_styles, scale_px
from gui.verification_panel import VerificationPanel
from core.source_settings import filter_description

PAGE_KEYS = ("follow", "tasks", "sources", "records", "verify", "settings")
PAGE_LABELS = {
    "follow": "关注下载",
    "tasks": "下载任务",
    "sources": "其他来源",
    "records": "下载记录",
    "verify": "核验",
    "settings": "设置",
}
TABLE_ROW_LIMIT = 200
TABLE_SCAN_LIMIT = 201
FOLLOW_MODES = ("history", "new")
RESULT_CHOICES: tuple[tuple[str, str], ...] = (
    ("all", "全部结果"),
    ("completed", "已完成"),
    ("skipped_existing", "已存在跳过"),
    ("partial", "部分完成"),
    ("failed", "失败"),
    ("needs_verification", "待核验"),
    ("filtered", "已过滤"),
    ("metadata_unknown", "信息待核验"),
)
RESULT_LABELS = {label: code for code, label in RESULT_CHOICES}
RESULT_CODES = {code for code, _label in RESULT_CHOICES}
NAV_ACTIVE_MARK = "▍"

ACTION_LABELS = {
    "start_source": "开始下载  →",
    "start": "开始历史补全",
    "sync_following": "同步关注列表",
    "restart_scope": "重新检查历史条件",
    "pause": "暂停",
    "resume": "继续",
    "cancel": "取消",
    "retry_failed": "只重试失败",
    "results": "查看本批名单",
    "task_previous": "上一页",
    "task_next": "下一页",
    "record_query": "查询",
    "record_previous": "上一页",
    "record_next": "下一页",
    "export_records": "导出记录",
    "open_artwork": "打开作品",
    "log_clear": "清空",
    "log_pause": "暂停",
    "log_follow": "跟随",
    "export_log": "导出日志",
    "log_toggle": "日志",
    "save_settings": "保存设置",
    "verify_account": "校验账号状态",
    "update_authorization": "更新授权…",
    "open_output": "打开输出目录",
    "browse_output": "选择…",
    "import_artists": "从文本导入画师…",
}
SELECT_ACTIONS = {
    "artists": "select_artist",
    "tasks": "select_task",
    "records": "select_record",
}
STATUS_STYLES = {
    "muted": "CollectionStatusMuted.TLabel",
    "success": "CollectionStatusSuccess.TLabel",
    "warning": "CollectionStatusWarning.TLabel",
    "danger": "CollectionStatusDanger.TLabel",
    "error": "CollectionStatusDanger.TLabel",
    "accent": "CollectionStatusAccent.TLabel",
    "info": "CollectionStatusAccent.TLabel",
}
DETAIL_PLACEHOLDER = "选择一条记录查看详情。"
LOG_PLACEHOLDER = ""

_TREE_COLUMNS = {
    "artists": (
        ("name", "画师", 300, True),
        ("state", "状态", 120, False),
        ("counts", "计数", 220, True),
    ),
    "tasks": (
        ("title", "任务", 330, True),
        ("source", "来源", 90, False),
        ("state", "状态", 110, False),
        ("counts", "完成情况", 230, True),
        ("reason", "当前情况", 210, True),
    ),
    "records": (
        ("title", "作品", 300, True),
        ("artist", "画师", 160, False),
        ("pages", "页数", 110, False),
        ("result", "结果", 130, False),
        ("time", "时间", 150, False),
    ),
}


def _display(value: Any) -> str:
    """Render controller-provided (already localized) content as one cell."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, dict):
        return " · ".join(_display(item) for item in value.values() if item not in (None, ""))
    if isinstance(value, (list, tuple, set)):
        return " · ".join(_display(item) for item in value if item not in (None, ""))
    return str(value)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _bounded_rows(rows: Any) -> list[Any]:
    """Consume at most ``TABLE_SCAN_LIMIT`` rows without copying huge input."""
    if rows is None:
        return []
    if isinstance(rows, list):
        return rows[:TABLE_SCAN_LIMIT][:TABLE_ROW_LIMIT]
    if isinstance(rows, tuple):
        return list(rows[:TABLE_SCAN_LIMIT])[:TABLE_ROW_LIMIT]
    try:
        return list(islice(rows, TABLE_SCAN_LIMIT))[:TABLE_ROW_LIMIT]
    except TypeError:
        return []


class _ScrollColumn(ttk.Frame):
    """Native Tk scroll viewport for a form column (no custom page drawing)."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        palette: dict,
        bind_event: Callable[[tk.Misc, str, Callable], str],
    ) -> None:
        super().__init__(master, style="CollectionPage.TFrame")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            self,
            background=palette["page"],
            highlightthickness=0,
            borderwidth=0,
            takefocus=False,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self.canvas.yview,
            style="Collection.Vertical.TScrollbar",
        )
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.body = ttk.Frame(self.canvas, style="CollectionPage.TFrame")
        self._window = self.canvas.create_window(
            (0, 0), window=self.body, anchor="nw"
        )
        bind_event(self.body, "<Configure>", self._on_body_configure)
        bind_event(self.canvas, "<Configure>", self._on_canvas_configure)

    def _on_body_configure(self,event):
        try:
            region=self.canvas.bbox('all')
            if getattr(self,'_last_region',None)!=region:
                self._last_region=region
                self.canvas.configure(scrollregion=region)
            visible=self.body.winfo_reqheight()>self.canvas.winfo_height()+1
            if visible and not self.scrollbar.winfo_manager():self.scrollbar.grid()
            elif not visible and self.scrollbar.winfo_manager():self.scrollbar.grid_remove()
        except tk.TclError:pass


    def _on_canvas_configure(self, event: tk.Event) -> None:
        try:
            width=max(1,int(event.width))
            if getattr(self,"_last_body_width",None)!=width:
                self._last_body_width=width
                self.canvas.itemconfigure(self._window,width=width)
        except tk.TclError:  # pragma: no cover - widget being destroyed
            pass


class CollectionView(ttk.Frame):
    """Bounded native collection view; controller supplies all data."""

    def __init__(self, master: tk.Misc, on_action: Callable[[str, dict], None]) -> None:
        if not callable(on_action):
            raise ValueError("on_action must be callable")
        super().__init__(master, style="CollectionPage.TFrame")
        theme = apply_collection_styles(self)
        self._palette = theme["colors"]
        self._fonts = theme["fonts"]
        self._breakpoint = int(theme["breakpoint"])
        self._on_action: Callable[[str, dict], None] | None = on_action
        self._owner_thread = threading.get_ident()
        self._destroyed = False

        self._disposed = False
        self._variables: list[tk.Variable] = []
        self._owned_widgets: list[tk.Misc] = []
        self._bindings: list[tuple[tk.Misc, str, str]] = []
        self._action_buttons: dict[str, list[ttk.Button]] = {}
        self._action_enabled: dict[str, bool] = {}
        self._action_default_labels = dict(ACTION_LABELS)
        self._action_label_overrides: dict[str, str] = {}
        self._last_selected = {"artists": None, "tasks": None, "records": None}
        self._suspend_select = False
        self._trees: dict[str, ttk.Treeview] = {}
        self._tree_hints: dict[str, ttk.Label] = {}
        self._hint_specs: dict[str, tuple[tk.Misc, int, int, int]] = {}
        self._table_signatures: dict[str, tuple] = {}
        self._active_page: str | None = None
        self._follow_wide: bool | None = None
        self._header_wide: bool | None = None
        self._last_width = 0
        self._log_open = False
        self._log_follow = True
        self._log_line_count = 0

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        self._build_state_variables()
        self._build_header()
        self._build_content()
        self._build_bottom()
        self._apply_follow_layout(True)
        self._update_follow_advanced()
        self._sync_min_bookmarks_state()
        self._show_detail(DETAIL_PLACEHOLDER)
        self._refresh_log_toggle()
        self._bind_event(self, "<Configure>", self._on_self_configure)
        self._bind_event(self, "<Destroy>", self._on_self_destroy_event)
        self._snapshot_owned_widgets()
        self.show_page("follow")

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_state_variables(self) -> None:
        default = lambda value: self._owned_variable(tk.StringVar, value)  # noqa: E731
        boolean = lambda value: self._owned_variable(tk.BooleanVar, value)  # noqa: E731
        self._follow_mode = default(value="history")
        self._follow_min_enabled = boolean(value=False)
        self._follow_min_text = default(value="")
        self._follow_exclude_ai = boolean(value=False)
        self._follow_max_artists = default(value="30")
        self._follow_max_requests = default(value="80")
        self._follow_max_works = default(value="100")
        self._follow_artist_ids = default(value="")
        self._follow_start_artist = default(value="")
        self._follow_start_ordinal = default(value="")
        self._follow_output = default(value="downl_Pixiv")
        self._follow_output_display = default(value="./downl_Pixiv")
        self._follow_location = {'output_kind': 'project', 'output_root_id': 'project'}
        self._follow_folder_by_artist = boolean(value=True)
        self._follow_more = boolean(value=False)

        self._record_scope = default(value="batch")
        self._record_search = default(value="")
        self._record_result = default(value="全部结果")

        self._settings_output = default(value="downl_Pixiv")
        self._settings_output_display = default(value="./downl_Pixiv")
        self._settings_location = {'output_kind': 'project', 'output_root_id': 'project'}
        self._settings_folder = boolean(value=True)
        self._settings_workers = default(value="3")
        self._settings_name_template = default(value="{illust_id}_p{page_index}")
        self._settings_api_interval = default(value="0.5")
        self._settings_image_interval = default(value="0.5")
        self._settings_user_agent = default(value="")

    def _owned_variable(self, factory, value):
        variable = factory(master=self, value=value)
        self._variables.append(variable)
        return variable

    def _build_header(self):
        s=lambda x:scale_px(self,x)
        self._header=ttk.Frame(self,style='CollectionHeader.TFrame')
        self._header.grid(row=0,column=0,sticky='ew',padx=s(30))
        self._brand_frame=ttk.Frame(self._header,style='CollectionHeader.TFrame')
        self._title_label=ttk.Label(self._brand_frame,text='JpgPng',style='CollectionBrand.TLabel')
        self._title_label.grid(row=0,column=0,sticky='w')
        self._separator(self._brand_frame,orient='vertical',style='Collection.TSeparator').grid(
            row=0,column=1,sticky='ns',padx=s(22),pady=s(7))
        ttk.Label(self._brand_frame,text='Pixiv 下载工具',style='CollectionMuted.TLabel').grid(row=0,column=2,sticky='w')
        self._nav_frame=ttk.Frame(self._header,style='CollectionHeader.TFrame')
        self._nav_buttons={}; self._nav_lines={}
        for index,key in enumerate(PAGE_KEYS):
            slot=ttk.Frame(self._nav_frame,style='CollectionHeader.TFrame')
            slot.grid(row=0,column=index,padx=(s(26) if index else 0,0),sticky='ns')
            button=ttk.Button(slot,text=PAGE_LABELS[key],style='CollectionNav.TButton',
                command=lambda key=key:self._navigate(key),takefocus=True)
            button.grid(row=0,column=0,sticky='ew')
            line=tk.Frame(slot,height=s(2),background=self._palette['page'],borderwidth=0)
            line.grid(row=1,column=0,sticky='ew')
            self._bind_event(button,'<Return>',lambda event,key=key:self._run_nav_return(key))
            self._nav_buttons[key]=button;self._nav_lines[key]=line
        self._update_header_layout(True)
        self._separator(self,style='Collection.TSeparator').grid(row=1,column=0,sticky='ew')


    def _update_header_layout(self,wide):
        if not hasattr(self,'_header') or self._header_wide==bool(wide):return
        self._header_wide=bool(wide);s=lambda x:scale_px(self,x)
        self._brand_frame.grid_forget();self._nav_frame.grid_forget()
        self._header.columnconfigure(0,weight=0 if wide else 1)
        self._header.columnconfigure(1,weight=1 if wide else 0)
        self._brand_frame.grid(row=0,column=0,sticky='w',pady=(s(12),s(12) if wide else 0))
        self._nav_frame.grid(row=0 if wide else 1,column=1 if wide else 0,
            columnspan=1 if wide else 2,sticky='' if wide else 'w')


    def _build_content(self) -> None:
        content = ttk.Frame(self, style="CollectionPage.TFrame")
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        self._content = content
        self._pages: dict[str, ttk.Frame] = {}
        for key in PAGE_KEYS:
            page = ttk.Frame(content, style="CollectionPage.TFrame", name=f"{key}_page")
            page.grid(row=0, column=0, sticky="nsew")
            page.grid_remove()
            self._pages[key] = page
        self._build_follow_page(self._pages["follow"])
        self._build_tasks_page(self._pages["tasks"])
        self._build_sources_page(self._pages["sources"])
        self._build_records_page(self._pages["records"])
        self._build_verify_page(self._pages["verify"])
        self._build_settings_page(self._pages["settings"])

    # -- shared widgets -------------------------------------------------

    def _bind_event(self, widget: tk.Misc, sequence: str, callback: Callable) -> str:
        funcid = widget.bind(sequence, callback, add="+")
        if funcid:
            self._bindings.append((widget, sequence, funcid))
        return funcid

    def _make_action_button(
        self,
        parent: tk.Misc,
        action: str,
        text: str | None = None,
        *,
        style: str = "CollectionSecondary.TButton",
    ) -> ttk.Button:
        label = text if text is not None else ACTION_LABELS.get(action, action)
        button = ttk.Button(
            parent,
            text=label,
            style=style,
            takefocus=True,
            command=lambda act=action: self._run_action(act),
        )
        self._action_buttons.setdefault(action, []).append(button)
        self._action_default_labels.setdefault(action, label)
        self._action_enabled.setdefault(action, True)
        self._bind_event(
            button,
            "<Return>",
            lambda _event, act=action: self._run_action_return(act),
        )
        self._refresh_action(action)
        return button

    def _form_row(
        self,
        parent: tk.Misc,
        row: int,
        label: str,
        variable: tk.Variable,
        *,
        width: int = 12,
    ) -> ttk.Entry:
        ttk.Label(parent, text=label, style="Collection.TLabel").grid(
            row=row, column=0, sticky="w", pady=(0, scale_px(self, 5))
        )
        entry = ttk.Entry(
            parent, textvariable=variable, width=width, style="Collection.TEntry"
        )
        entry.grid(
            row=row,
            column=1,
            sticky="ew",
            padx=(scale_px(self, 10), 0),
            pady=(0, scale_px(self, 5)),
        )
        parent.columnconfigure(1, weight=1)
        return entry

    def _make_hint(
        self, parent: tk.Misc, row: int, text: str, *, padx: int = 0
    ) -> ttk.Label:
        label = ttk.Label(parent, text=text, style="CollectionHint.TLabel")
        self._hint_specs[id(label)] = (
            parent,
            row,
            scale_px(self, padx),
            scale_px(self, 4),
        )
        label.grid(
            row=row,
            column=0,
            sticky="w",
            padx=(scale_px(self, padx), 0),
            pady=(0, scale_px(self, 4)),
        )
        return label

    def _show_hint(self, kind: str, visible: bool) -> None:
        hint = self._tree_hints.get(kind)
        if hint is None:
            return
        spec = self._hint_specs.get(id(hint))
        try:
            if visible:
                if spec is None:
                    hint.grid()
                else:
                    parent, row, padx, pady = spec
                    hint.grid(
                        row=row, column=0, sticky="w", padx=(padx, 0), pady=(0, pady)
                    )
            else:
                hint.grid_remove()
        except tk.TclError:  # pragma: no cover - widget being destroyed
            pass

    def _make_table(
        self,
        parent: tk.Misc,
        kind: str,
        *,
        height: int,
        name: str,
    ) -> tuple[ttk.Frame, ttk.Treeview]:
        columns = _TREE_COLUMNS[kind]
        wrap = ttk.Frame(parent, style="CollectionPage.TFrame")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            wrap,
            columns=tuple(item[0] for item in columns),
            show="headings",
            height=height,
            selectmode="browse",
            style="Collection.Treeview",
            name=name,
            takefocus=True,
        )
        for column, heading, width, stretch in columns:
            tree.heading(column, text=heading, anchor="w")
            tree.column(
                column,
                width=scale_px(self, width),
                minwidth=scale_px(self, 48),
                stretch=stretch,
                anchor="w",
            )
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            wrap,
            orient="vertical",
            command=tree.yview,
            style="Collection.Vertical.TScrollbar",
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        hscrollbar = ttk.Scrollbar(
            wrap,
            orient="horizontal",
            command=tree.xview,
            style="Collection.Horizontal.TScrollbar",
        )
        hscrollbar.grid(row=1, column=0, sticky="ew")
        tree.configure(
            yscrollcommand=lambda first,last:self._scrollbar_set(scrollbar,first,last),
            xscrollcommand=lambda first,last:self._scrollbar_set(hscrollbar,first,last),
        )
        self._bind_event(
            tree,
            "<<TreeviewSelect>>",
            lambda _event, table_kind=kind: self._on_tree_select(table_kind),
        )
        tree.tag_configure("alternate",background="#F9FBFC")
        self._trees[kind] = tree
        return wrap, tree

    # -- follow page ----------------------------------------------------

    def _build_follow_page(self,page):
        page.columnconfigure(0,weight=1);page.rowconfigure(0,weight=1)
        scroll=_ScrollColumn(page,palette=self._palette,bind_event=self._bind_event)
        scroll.grid(row=0,column=0,sticky='nsew')
        self._follow_scroll=scroll
        body=scroll.body;body.columnconfigure(0,weight=1)
        body.configure(padding=(scale_px(self,30),0,scale_px(self,30),scale_px(self,24)))
        self._page_heading(body,'关注下载','慢慢收全喜欢的作品，从上次停下的地方继续。')
        self._follow_grid=ttk.Frame(body,style='CollectionPage.TFrame')
        self._follow_grid.grid(row=1,column=0,sticky='nsew')
        self._follow_left=ttk.Frame(self._follow_grid,style='CollectionPage.TFrame')
        self._follow_filters=ttk.Frame(self._follow_grid,style='CollectionPage.TFrame')
        self._build_follow_left(self._follow_left)
        self._build_follow_filters(self._follow_filters)
        self._bind_event(self.winfo_toplevel(),'<MouseWheel>',self._scroll_page)


    def _build_follow_left(self,left):
        s=lambda x:scale_px(self,x)
        left.columnconfigure(0,weight=1)
        tabs=ttk.Frame(left,style='CollectionPage.TFrame');tabs.grid(row=0,column=0,sticky='ew')
        self._mode_buttons={};self._mode_lines={}
        for index,(key,label) in enumerate((('history','继续历史下载'),('new','关注新作'),('artist','指定画师'))):
            slot=ttk.Frame(tabs,style='CollectionPage.TFrame');slot.grid(row=0,column=index,padx=(0,s(34)))
            button=ttk.Button(slot,text=label,style='CollectionTab.TButton',command=lambda key=key:self._choose_follow_mode(key))
            button.grid(row=0,column=0,sticky='ew')
            line=tk.Frame(slot,height=s(2),background=self._palette['page']);line.grid(row=1,column=0,sticky='ew')
            self._mode_buttons[key]=button;self._mode_lines[key]=line
        self._follow_mode_history=self._mode_buttons['history'];self._follow_mode_new=self._mode_buttons['new']
        self._rule(left,1,pady=(0,s(20)))
        outer=ttk.Frame(left,style='CollectionPage.TFrame');outer.grid(row=2,column=0,sticky='ew');outer.columnconfigure(0,weight=1)
        shadow=tk.Frame(outer,background=self._palette['shadow'],borderwidth=0)
        shadow.grid(row=0,column=0,sticky='nsew',padx=(s(6),0),pady=(s(6),0))
        hero=ttk.Frame(outer,style='CollectionHero.TFrame',padding=(s(22),s(18),s(22),s(20)))
        hero.grid(row=0,column=0,sticky='ew',padx=(0,s(6)),pady=(0,s(6)));hero.columnconfigure(0,weight=1)
        tk.Frame(outer,height=1,background='#BDD8E8').grid(row=0,column=0,sticky='new',padx=(0,s(6)))
        self._hero=hero
        self._hero_mode=ttk.Label(hero,text='历史作品 · 逐步收全',style='CollectionHeroAccent.TLabel')
        self._hero_mode.grid(row=0,column=0,sticky='w')
        self._resume_state=ttk.Label(hero,text='尚未开始',style='CollectionHeroSmall.TLabel')
        self._resume_state.grid(row=0,column=1,sticky='e')
        self._summary_title=ttk.Label(hero,text='从喜欢的画师开始收藏',style='CollectionHeroTitle.TLabel',wraplength=s(550),justify='left')
        self._summary_title.grid(row=1,column=0,columnspan=2,sticky='w',pady=(s(14),s(7)))
        self._resume_position=ttk.Label(hero,text='历史作品会分批保存，随时可以暂停。',style='CollectionHeroBody.TLabel',wraplength=s(450),justify='left')
        self._resume_position.grid(row=2,column=0,columnspan=2,sticky='w')
        self._summary_description=ttk.Label(hero,text='已完成的记录会自动跳过。',style='CollectionHeroSmall.TLabel',wraplength=s(410),justify='left')
        self._summary_description.grid(row=3,column=0,sticky='w',pady=(s(10),0))
        self._start_button=self._make_action_button(hero,'start','开始历史补全',style='CollectionPrimary.TButton')
        self._start_button.grid(row=3,column=1,sticky='e',padx=(s(18),0),pady=(s(8),0))
        self._bind_event(hero,'<Configure>',self._fit_hero)
        head=ttk.Frame(left,style='CollectionPage.TFrame');head.grid(row=3,column=0,sticky='ew',pady=(s(22),s(8)));head.columnconfigure(0,weight=1)
        ttk.Label(head,text='本批记录',style='CollectionSection.TLabel').grid(row=0,column=0,sticky='w')
        self._make_action_button(head,'results',style='CollectionQuiet.TButton').grid(row=0,column=1,sticky='e')
        self._rule(left,4)
        metrics=ttk.Frame(left,style='CollectionPage.TFrame');metrics.grid(row=5,column=0,sticky='ew',pady=(s(16),s(15)))
        self._stat_labels={}
        for index,(key,label,unit) in enumerate((('stat_works','新增完成','个作品'),('stat_pages','图片已保存','张'),('stat_pending','待处理','个作品'))):
            metrics.columnconfigure(index,weight=1,uniform='metric')
            slot=ttk.Frame(metrics,style='CollectionPage.TFrame');slot.grid(row=0,column=index,sticky='nsew',padx=(s(20) if index else 0,s(10)))
            if index:self._separator(metrics,orient='vertical',style='Collection.TSeparator').grid(row=0,column=index,sticky='nsw',pady=s(3))
            ttk.Label(slot,text=label,style='CollectionMuted.TLabel').grid(row=0,column=0,columnspan=2,sticky='w')
            value=ttk.Label(slot,text='—',style='CollectionNumber.TLabel');value.grid(row=1,column=0,sticky='w',pady=(s(4),0))
            ttk.Label(slot,text=unit,style='CollectionMuted.TLabel').grid(row=1,column=1,sticky='sw',padx=(s(10),0),pady=(0,s(7)))
            self._stat_labels[key]=value
        self._rule(left,6)
        self._summary_counts=ttk.Label(left,text='还没有本批结果。开始后可在这里查看完成情况。',style='CollectionMuted.TLabel',wraplength=s(650))
        self._summary_counts.grid(row=7,column=0,sticky='w',pady=(s(13),s(13)))
        self._rule(left,8)
        support=ttk.Frame(left,style='CollectionPage.TFrame');support.grid(row=9,column=0,sticky='ew',pady=(s(14),s(5)));support.columnconfigure(0,weight=1)
        self._artists_toggle=ttk.Button(support,text='查看画师进度  ＋',style='CollectionQuiet.TButton',command=self._toggle_artists)
        self._artists_toggle.grid(row=0,column=0,sticky='w')
        self._artist_count=ttk.Label(support,text='开始后载入',style='CollectionMuted.TLabel');self._artist_count.grid(row=0,column=1,sticky='e')
        section=ttk.Frame(left,style='CollectionPage.TFrame');section.grid(row=10,column=0,sticky='ew');section.columnconfigure(0,weight=1)
        self._artists_section=section
        self._tree_hints['artists']=self._make_hint(section,0,'还没有载入关注画师')
        wrap,tree=self._make_table(section,'artists',height=3,name='artists_tree');wrap.grid(row=1,column=0,sticky='ew')
        section.grid_remove()
        self._paint_follow_mode()


    def _build_follow_filters(self,filters):
        s=lambda x:scale_px(self,x);filters.columnconfigure(1,weight=1)
        ttk.Label(filters,text='下载选项',style='CollectionSection.TLabel').grid(row=0,column=0,columnspan=2,sticky='w',pady=(s(12),s(4)))
        ttk.Label(filters,text='沿用已保存的方案',style='CollectionMuted.TLabel').grid(row=1,column=0,columnspan=2,sticky='w',pady=(0,s(14)))
        for row,(title,description) in enumerate((('原图 · 全部图片页','保留原始格式，完整保存多页作品'),('跳过已完成记录','继续收集，保留已有成果')),start=2):
            item=ttk.Frame(filters,style='CollectionPage.TFrame');item.grid(row=row,column=0,columnspan=2,sticky='ew',pady=(0,s(12)))
            ttk.Label(item,text='✓',style='CollectionLink.TLabel').grid(row=0,column=0,sticky='w',padx=(0,s(9)))
            ttk.Label(item,text=title,style='Collection.TLabel').grid(row=0,column=1,sticky='w')
            ttk.Label(item,text=description,style='CollectionMuted.TLabel').grid(row=1,column=1,sticky='w',pady=(s(4),0))
        self._rule(filters,4,columnspan=2,pady=(0,s(10)))
        ttk.Label(filters,text='本批最多新增',style='Collection.TLabel').grid(row=9,column=0,columnspan=2,sticky='w',pady=(0,s(7)))
        budget=ttk.Frame(filters,style='CollectionPage.TFrame');budget.grid(row=10,column=0,columnspan=2,sticky='ew',pady=(0,s(12)));budget.columnconfigure(0,weight=1)
        ttk.Entry(budget,textvariable=self._follow_max_works,width=10,style='Collection.TEntry').grid(row=0,column=0,sticky='ew')
        ttk.Label(budget,text='个作品',style='CollectionMuted.TLabel').grid(row=0,column=1,padx=(s(10),0))
        self._exclude_ai_checkbox=ttk.Checkbutton(filters,text='排除 AI 作品',variable=self._follow_exclude_ai,style='Collection.TCheckbutton')
        self._exclude_ai_checkbox.grid(row=11,column=0,columnspan=2,sticky='w',pady=(0,s(8)))
        self._min_checkbox=ttk.Checkbutton(filters,text='最低收藏',variable=self._follow_min_enabled,command=self._sync_min_bookmarks_state,style='Collection.TCheckbutton')
        self._min_checkbox.grid(row=12,column=0,sticky='w')
        self._min_entry=ttk.Entry(filters,textvariable=self._follow_min_text,width=8,style='Collection.TEntry')
        self._min_entry.grid(row=12,column=1,sticky='ew',padx=(s(12),0))
        self._filter_summary=self._owned_variable(tk.StringVar,'')
        ttk.Label(filters,textvariable=self._filter_summary,style='CollectionMuted.TLabel',wraplength=s(330)).grid(row=13,column=0,columnspan=2,sticky='w',pady=(s(8),0))
        for variable in (self._follow_min_enabled,self._follow_min_text,self._follow_exclude_ai):
            variable.trace_add('write',lambda *args:self._refresh_filter_summary())
        self._refresh_filter_summary()
        self._rule(filters,14,columnspan=2,pady=(s(13),s(8)))
        location=ttk.Frame(filters,style='CollectionPage.TFrame');location.grid(row=5,column=0,columnspan=2,sticky='ew');location.columnconfigure(0,weight=1)
        ttk.Label(location,text='保存位置',style='Collection.TLabel').grid(row=0,column=0,sticky='w')
        self._make_action_button(location,'browse_output',style='CollectionQuiet.TButton').grid(row=0,column=1,sticky='e')
        ttk.Entry(filters,textvariable=self._follow_output_display,state='readonly',width=12,style='Collection.TEntry').grid(row=6,column=0,columnspan=2,sticky='ew',pady=(s(6),s(8)))
        self._folder_checkbox=ttk.Checkbutton(filters,text='按画师分文件夹',variable=self._follow_folder_by_artist,style='Collection.TCheckbutton')
        self._folder_checkbox.grid(row=7,column=0,columnspan=2,sticky='w')
        self._rule(filters,8,columnspan=2,pady=(s(12),s(12)))
        self._more_checkbox=ttk.Checkbutton(filters,text='更多设置 · 画师与起点',variable=self._follow_more,command=self._update_follow_advanced,style='Collection.TCheckbutton')
        self._more_checkbox.grid(row=15,column=0,columnspan=2,sticky='w')
        advanced=ttk.Frame(filters,style='CollectionPage.TFrame');advanced.grid(row=16,column=0,columnspan=2,sticky='ew',pady=(s(14),0));advanced.columnconfigure(1,weight=1)
        self._follow_advanced=advanced
        self._form_row(advanced,0,'每批画师',self._follow_max_artists,width=8)
        self._form_row(advanced,1,'查询请求',self._follow_max_requests,width=8)
        self._artist_entry=self._form_row(advanced,2,'指定画师',self._follow_artist_ids,width=12)
        self._form_row(advanced,3,'起始画师',self._follow_start_artist,width=12)
        self._form_row(advanced,4,'起始序号',self._follow_start_ordinal,width=8)
        self._make_action_button(advanced,'import_artists',style='CollectionQuiet.TButton').grid(row=5,column=0,columnspan=2,sticky='w',pady=(s(4),s(8)))
        self._sync_button=self._make_action_button(advanced,'sync_following',style='CollectionQuiet.TButton');self._sync_button.grid(row=6,column=0,columnspan=2,sticky='w')
        self._restart_button=self._make_action_button(advanced,'restart_scope',style='CollectionQuiet.TButton');self._restart_button.grid(row=7,column=0,columnspan=2,sticky='w')


    def _sync_min_bookmarks_state(self) -> None:
        if self._disposed:
            return
        try:
            if bool(self._follow_min_enabled.get()):
                self._min_entry.state(["!disabled"])
            else:
                self._min_entry.state(["disabled"])
        except tk.TclError:  # pragma: no cover - widget being destroyed
            pass

    def _update_follow_advanced(self):
        if self._disposed:return
        if self._follow_more.get():self._follow_advanced.grid(row=12,column=0,columnspan=2,sticky='ew',pady=(scale_px(self,14),0))
        else:self._follow_advanced.grid_remove()


    # -- tasks page -----------------------------------------------------

    def _build_tasks_page(self,page):
        s=lambda x:scale_px(self,x)
        page.columnconfigure(0,weight=1);page.rowconfigure(0,weight=1)
        scroll=_ScrollColumn(page,palette=self._palette,bind_event=self._bind_event);scroll.grid(row=0,column=0,sticky='nsew');self._tasks_scroll=scroll
        body=scroll.body;body.columnconfigure(0,weight=1);body.configure(padding=(s(30),0,s(30),s(24)))
        self._page_heading(body,'下载任务','查看进度，继续未完成的下载。')
        bar=ttk.Frame(body,style='CollectionPage.TFrame');bar.grid(row=1,column=0,sticky='ew');bar.columnconfigure(0,weight=1)
        tabs=ttk.Frame(bar,style='CollectionPage.TFrame');tabs.grid(row=0,column=0,sticky='w')
        self._task_tabs={};self._task_lines={};self._task_filter='all';self._task_rows_cache=[];self._task_controls=[];self._task_progress={}
        for index,(key,label) in enumerate((('all','全部'),('running','进行中'),('paused','已暂停'),('finished','已结束'))):
            slot=ttk.Frame(tabs,style='CollectionPage.TFrame');slot.grid(row=0,column=index,padx=(0,s(30)))
            button=ttk.Button(slot,text=label,style='CollectionTab.TButton',command=lambda key=key:self._choose_task_filter(key));button.grid(row=0,column=0)
            line=tk.Frame(slot,height=s(2),background=self._palette['page']);line.grid(row=1,column=0,sticky='ew')
            self._task_tabs[key]=button;self._task_lines[key]=line
        self._task_search=self._owned_variable(tk.StringVar,'')
        group=ttk.Frame(bar,style='CollectionPage.TFrame');group.grid(row=0,column=1,sticky='e')
        self._task_search_group=group
        self._task_search_entry=ttk.Entry(group,textvariable=self._task_search,width=19,style='Collection.TEntry')
        self._task_search_entry.grid(row=0,column=0,sticky='e')
        search=ttk.Button(group,text='搜索本页',style='CollectionQuiet.TButton',command=self._filter_tasks);search.grid(row=0,column=1,padx=(s(10),0))
        self._bind_event(self._task_search_entry,'<Return>',lambda event:self._filter_tasks())
        self._rule(body,2)
        listing=ttk.Frame(body,style='CollectionPage.TFrame');listing.grid(row=3,column=0,sticky='ew');listing.columnconfigure(0,weight=1)
        self._tree_hints['tasks']=self._make_hint(listing,0,'还没有下载任务。回到关注下载，开始收集喜欢的作品。')
        wrap,tree=self._make_table(listing,'tasks',height=3,name='tasks_tree');wrap.grid(row=1,column=0,sticky='ew')
        self._rule(body,4)
        pager=ttk.Frame(body,style='CollectionPage.TFrame');pager.grid(row=5,column=0,sticky='ew',pady=(s(3),s(20)));pager.columnconfigure(0,weight=1)
        self._task_page_label=ttk.Label(pager,text='本页 0 项',style='CollectionMuted.TLabel');self._task_page_label.grid(row=0,column=0,sticky='w')
        for index,action in enumerate(('task_previous','task_next'),start=1):self._make_action_button(pager,action,style='CollectionQuiet.TButton').grid(row=0,column=index,padx=(s(20),0))
        detail=ttk.Frame(body,style='CollectionPage.TFrame');detail.grid(row=6,column=0,sticky='ew');detail.columnconfigure(0,weight=1)
        ttk.Label(detail,text='任务详情',style='CollectionSection.TLabel').grid(row=0,column=0,sticky='w')
        actions=ttk.Frame(detail,style='CollectionPage.TFrame');actions.grid(row=0,column=1,sticky='e')
        for index,action in enumerate(('pause','resume','retry_failed','results','cancel')):
            button=self._make_action_button(actions,action,style='CollectionQuiet.TButton')
            button.grid(row=0,column=index,padx=(s(18),0));self._task_controls.append(button)
        self._task_detail_text=tk.Text(detail,height=5,wrap='word',relief='flat',borderwidth=0,highlightthickness=0,
            background=self._palette['page'],foreground=self._palette['ink'],font=self._fonts['body'],
            padx=0,pady=s(12),spacing1=s(4),spacing3=s(6),state='disabled')
        self._task_detail_text.grid(row=1,column=0,columnspan=2,sticky='ew')
        self._rule(body,7,pady=(s(6),s(15)))
        ttk.Label(body,text='详细活动可在底部展开日志。',style='CollectionMuted.TLabel').grid(row=8,column=0,sticky='w')
        self._filter_tasks()


    # -- sources page ---------------------------------------------------

    def _build_sources_page(self, page):
        from gui.collection_sources import SourcePanel
        s=lambda value:scale_px(self,value)
        page.columnconfigure(0,weight=1);page.rowconfigure(0,weight=1)
        scroll=_ScrollColumn(page,palette=self._palette,bind_event=self._bind_event)
        scroll.grid(row=0,column=0,sticky='nsew');self._sources_scroll=scroll
        body=scroll.body;body.columnconfigure(0,weight=1);body.configure(padding=(s(30),0,s(30),s(24)))
        heading=self._page_heading(body,'其他来源','选好来源和条件，按原图保存喜欢的作品。')
        self._make_action_button(heading,'start_source',style='CollectionPrimary.TButton').grid(row=0,column=1,rowspan=2,sticky='e',padx=(s(16),0))
        self._source_panel=SourcePanel(self,body)
        self._source_buttons=list(self._source_panel.tabs.values())


    # -- records page ---------------------------------------------------

    def _build_records_page(self, page):
        s=lambda value:scale_px(self,value)
        page.columnconfigure(0,weight=1);page.rowconfigure(0,weight=1)
        scroll=_ScrollColumn(page,palette=self._palette,bind_event=self._bind_event)
        scroll.grid(row=0,column=0,sticky='nsew');self._records_scroll=scroll
        body=scroll.body;body.columnconfigure(0,weight=1);body.rowconfigure(4,weight=1)
        body.configure(padding=(s(30),0,s(30),s(20)))
        self._page_heading(body,'下载记录','每一次收集都有迹可循。查看本批结果，或查找已经保存的作品。')
        filters=ttk.Frame(body,style='CollectionPage.TFrame');filters.grid(row=1,column=0,sticky='ew',pady=(0,s(12)))
        filters.columnconfigure(0,weight=1);self._record_filters_bar=filters
        scopes=ttk.Frame(filters,style='CollectionPage.TFrame');scopes.grid(row=0,column=0,sticky='w')
        for column,(key,label) in enumerate((('batch','本批名单'),('history','累计下载'))):
            button=ttk.Radiobutton(scopes,text=label,value=key,variable=self._record_scope,
                style='CollectionScope.TRadiobutton',takefocus=True)
            button.grid(row=0,column=column,sticky='w',padx=(0,s(24)))
            if key=='batch':self._record_scope_batch=button
            else:self._record_scope_history=button
        search=ttk.Frame(filters,style='CollectionPage.TFrame');search.grid(row=0,column=1,sticky='e')
        self._record_search_group=search
        ttk.Label(search,text='作品 / 画师',style='CollectionMuted.TLabel').grid(row=0,column=0,padx=(0,s(10)))
        self._record_search_entry=ttk.Entry(search,textvariable=self._record_search,width=18,style='Collection.TEntry')
        self._record_search_entry.grid(row=0,column=1)
        self._bind_event(self._record_search_entry,'<Return>',lambda event:self._submit_record_query())
        self._record_result_combo=ttk.Combobox(search,textvariable=self._record_result,
            values=[label for _,label in RESULT_CHOICES],state='readonly',width=10,style='Collection.TCombobox')
        self._record_result_combo.grid(row=0,column=2,padx=(s(12),s(12)))
        self._record_query_button=self._make_action_button(search,'record_query','查询',style='CollectionQuiet.TButton')
        self._record_query_button.grid(row=0,column=3)
        self._rule(body,2)
        self._tree_hints['records']=self._make_hint(body,3,'暂无记录。开始收集后，这里会保存每个作品的结果。')
        wrap,tree=self._make_table(body,'records',height=5,name='records_tree')
        wrap.grid(row=4,column=0,sticky='nsew')
        self._rule(body,5)
        pager=ttk.Frame(body,style='CollectionPage.TFrame');pager.grid(row=6,column=0,sticky='ew',pady=(s(6),s(18)))
        pager.columnconfigure(0,weight=1)
        self._records_page_label=ttk.Label(pager,text='',style='CollectionMuted.TLabel');self._records_page_label.grid(row=0,column=0,sticky='w')
        for column,action in enumerate(('record_previous','record_next','export_records'),start=1):
            self._make_action_button(pager,action,style='CollectionQuiet.TButton').grid(row=0,column=column,padx=(s(20),0))
        detail=ttk.Frame(body,style='CollectionPage.TFrame');detail.grid(row=7,column=0,sticky='ew');detail.columnconfigure(0,weight=1)
        ttk.Label(detail,text='作品详情',style='CollectionSection.TLabel').grid(row=0,column=0,sticky='w')
        self._make_action_button(detail,'open_artwork','在 Pixiv 打开作品',style='CollectionQuiet.TButton').grid(row=0,column=1,sticky='e')
        self._detail_text=tk.Text(detail,height=4,wrap='word',state='disabled',background=self._palette['page'],
            foreground=self._palette['ink'],selectbackground=self._palette['accent_wash'],relief='flat',borderwidth=0,
            highlightthickness=0,font=self._fonts['body'],padx=0,pady=s(10),spacing1=s(3),spacing3=s(4))
        self._detail_text.grid(row=1,column=0,columnspan=2,sticky='ew')


    def _submit_record_query(self) -> None:
        self._run_action("record_query")

    # -- verification page ----------------------------------------------

    def _build_verify_page(self, page):
        """Mount the pending-verification panel on its own page.

        The panel owns its table, buttons and wording; this page only supplies
        the heading and the geometry.  All data arrives through
        set_verification_rows from the controller's poll, never from a worker.
        """
        s = lambda value: scale_px(self, value)  # noqa: E731
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=1)
        body = ttk.Frame(page, style="CollectionPage.TFrame")
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        body.configure(padding=(s(30), 0, s(30), s(24)))
        self._page_heading(body, "核验与修复",
            "已经记录、但当前磁盘找不到一致字节的图片页，在这里逐页核验。核验只读磁盘，不会删除或覆盖文件。")
        self._verification_panel = VerificationPanel(body, on_action=self._emit)
        self._verification_panel.grid(row=1, column=0, sticky="nsew")

    # -- settings page --------------------------------------------------

    def _build_settings_page(self, page):
        s=lambda value:scale_px(self,value)
        page.columnconfigure(0,weight=1);page.rowconfigure(0,weight=1)
        scroll=_ScrollColumn(page,palette=self._palette,bind_event=self._bind_event)
        scroll.grid(row=0,column=0,sticky='nsew');self._settings_scroll=scroll
        body=scroll.body;body.columnconfigure(0,weight=1);body.configure(padding=(s(30),0,s(30),s(24)))
        self._page_heading(body,'设置','用顺手的方式收藏。保存位置、下载速度和账号，都在这里。')
        columns=ttk.Frame(body,style='CollectionPage.TFrame');columns.grid(row=1,column=0,sticky='ew')
        columns.columnconfigure(0,weight=2);columns.columnconfigure(1,weight=1)
        self._settings_columns=columns
        form=ttk.Frame(columns,style='CollectionPage.TFrame');form.grid(row=0,column=0,sticky='new',padx=(0,s(40)))
        form.columnconfigure(1,weight=1);self._settings_main=form
        ttk.Label(form,text='保存与下载',style='CollectionSection.TLabel').grid(row=0,column=0,columnspan=2,sticky='w',pady=(s(6),s(16)))
        ttk.Label(form,text='保存位置',style='Collection.TLabel').grid(row=1,column=0,sticky='w',padx=(0,s(12)))
        location=ttk.Frame(form,style='CollectionPage.TFrame');location.grid(row=1,column=1,sticky='ew',pady=(0,s(10)));location.columnconfigure(0,weight=1)
        ttk.Entry(location,textvariable=self._settings_output_display,state='readonly',width=20,style='Collection.TEntry').grid(row=0,column=0,sticky='ew')
        self._make_action_button(location,'browse_output',style='CollectionQuiet.TButton').grid(row=0,column=1,padx=(s(12),0))
        ttk.Label(form,text='选择文件夹即可。修改后用于下次开始的任务。',style='CollectionMuted.TLabel',wraplength=s(340),justify='left').grid(row=2,column=1,sticky='w',pady=(0,s(12)))
        self._settings_folder_checkbox=ttk.Checkbutton(form,text='按画师分文件夹',variable=self._settings_folder,style='Collection.TCheckbutton')
        self._settings_folder_checkbox.grid(row=3,column=1,sticky='w',pady=(0,s(15)))
        self._rule(form,4,columnspan=2,pady=(0,s(15)))
        self._form_row(form,5,'同时下载',self._settings_workers,width=8)
        ttk.Label(form,text='1–8 个下载线程，下次打开时生效。',style='CollectionMuted.TLabel').grid(row=6,column=1,sticky='w',pady=(0,s(16)))
        self._rule(form,7,columnspan=2,pady=(0,s(12)))
        naming=ttk.Frame(form,style='CollectionPage.TFrame')
        naming.grid(row=8,column=0,columnspan=2,sticky='ew',pady=(0,s(16)));naming.columnconfigure(1,weight=1)
        self._form_row(naming,0,'文件命名',self._settings_name_template,width=28)
        ttk.Label(naming,text='可用 {id}、{title}、{user_id}、{user}、{page}、{date}。\n保留原始扩展名；缺少 ID 或页码时自动补充。',
            style='CollectionMuted.TLabel',wraplength=s(360),justify='left').grid(row=1,column=1,sticky='w',pady=(s(6),0))
        self._settings_connection_open=self._owned_variable(tk.BooleanVar,False)
        ttk.Checkbutton(form,text='连接选项',variable=self._settings_connection_open,command=self._toggle_connection_options,
            style='Collection.TCheckbutton').grid(row=9,column=0,columnspan=2,sticky='w')
        extra=ttk.Frame(form,style='CollectionPage.TFrame');extra.grid(row=10,column=0,columnspan=2,sticky='ew',pady=s(10));extra.columnconfigure(1,weight=1)
        self._settings_connection=extra
        self._form_row(extra,0,'User-Agent',self._settings_user_agent,width=30)
        ttk.Label(extra,text='留空时沿用默认值。',style='CollectionMuted.TLabel').grid(row=1,column=1,sticky='w')
        self._form_row(extra,2,'查询间隔 / 秒',self._settings_api_interval,width=8)
        self._form_row(extra,3,'图片间隔 / 秒',self._settings_image_interval,width=8)
        ttk.Label(extra,text='0–60 秒，可填小数。间隔与并发下次打开时生效。',
            style='CollectionMuted.TLabel',wraplength=s(340)).grid(row=4,column=1,sticky='w')
        extra.grid_remove()
        actions=ttk.Frame(form,style='CollectionPage.TFrame');actions.grid(row=11,column=0,columnspan=2,sticky='w',pady=(s(22),0))
        self._make_action_button(actions,'save_settings',style='CollectionPrimary.TButton').grid(row=0,column=0)
        self._make_action_button(actions,'open_output','打开保存位置',style='CollectionQuiet.TButton').grid(row=0,column=1,padx=(s(24),0))
        side=ttk.Frame(columns,style='CollectionPage.TFrame',padding=(s(28),s(6),0,0))
        side.grid(row=0,column=1,sticky='new');side.columnconfigure(0,weight=1);self._settings_side=side
        ttk.Label(side,text='Pixiv 账号',style='CollectionSection.TLabel').grid(row=0,column=0,sticky='w',pady=(0,s(12)))
        ttk.Label(side,text='沿用已有授权。开始下载时会校验，也可以在这里检查账号状态。',style='CollectionSubtitle.TLabel',
            wraplength=s(290),justify='left').grid(row=1,column=0,sticky='w')
        account_actions=ttk.Frame(side,style='CollectionPage.TFrame');account_actions.grid(row=2,column=0,sticky='w',pady=(s(12),s(22)))
        self._make_action_button(account_actions,'verify_account','检查账号状态',style='CollectionQuiet.TButton').grid(row=0,column=0,sticky='w')
        self._make_action_button(account_actions,'update_authorization',style='CollectionQuiet.TButton').grid(row=1,column=0,sticky='w',pady=(s(8),0))
        self._rule(side,3,pady=(0,s(20)))
        ttk.Label(side,text='每一页，都完整保存',style='CollectionSection.TLabel').grid(row=4,column=0,sticky='w',pady=(0,s(12)))
        ttk.Label(side,text='保留原图格式，保存多页作品。\n确认完整交付后，才计入下载记录。',style='CollectionMuted.TLabel',
            justify='left',wraplength=s(290)).grid(row=5,column=0,sticky='w')


    # -- bottom status and log -----------------------------------------

    def _build_bottom(self) -> None:
        self._separator(
            self, orient="horizontal", style="Collection.TSeparator"
        ).grid(row=3, column=0, sticky="ew")
        bottom = ttk.Frame(self, style="CollectionPage.TFrame")
        bottom.grid(row=4, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        pad = scale_px(self, 14)
        status_row = ttk.Frame(bottom, style="CollectionPage.TFrame")
        status_row.grid(row=0, column=0, sticky="ew")
        status_row.columnconfigure(0, weight=1)
        self._status_label = ttk.Label(
            status_row,
            text="就绪",
            style="CollectionStatusMuted.TLabel",
            anchor="w",
            wraplength=scale_px(self, 520),
            justify="left",
        )
        self._status_label.grid(
            row=0, column=0, sticky="ew", padx=(pad, scale_px(self, 8)), pady=scale_px(self, 3)
        )
        self._log_toggle = self._make_action_button(
            status_row,
            "log_toggle",
            ACTION_LABELS["log_toggle"],
            style="CollectionQuiet.TButton",
        )
        self._log_toggle.grid(
            row=0, column=1, sticky="e", padx=(0, pad), pady=scale_px(self, 2)
        )

        log_body = ttk.Frame(bottom, style="CollectionPage.TFrame")
        log_body.grid(row=1, column=0, sticky="ew")
        log_body.columnconfigure(0, weight=1)
        self._log_body = log_body
        toolbar = ttk.Frame(log_body, style="CollectionPage.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", padx=(pad, pad))
        for index, action in enumerate(
            ("log_clear", "log_pause", "log_follow", "export_log")
        ):
            button = self._make_action_button(
                toolbar, action, ACTION_LABELS[action], style="CollectionQuiet.TButton"
            )
            button.grid(
                row=0, column=index, sticky="w", padx=(0, scale_px(self, 4))
            )
        self._log_notice = ttk.Label(
            log_body,
            text="",
            style="CollectionMuted.TLabel",
            wraplength=scale_px(self, 520),
            justify="left",
        )
        self._log_notice.grid(
            row=1, column=0, sticky="w", padx=(pad, pad), pady=(0, scale_px(self, 2))
        )
        text_wrap = ttk.Frame(log_body, style="CollectionPaper.TFrame")
        text_wrap.grid(
            row=2,
            column=0,
            sticky="ew",
            padx=(pad, pad),
            pady=(scale_px(self, 2), scale_px(self, 6)),
        )
        text_wrap.columnconfigure(0, weight=1)
        self._log_text = tk.Text(
            text_wrap,
            height=7,
            wrap="word",
            state="disabled",
            background=self._palette["paper"],
            foreground=self._palette["ink"],
            insertbackground=self._palette["ink"],
            selectbackground=self._palette["accent_wash"],
            selectforeground=self._palette["ink"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=scale_px(self, 8),
            pady=scale_px(self, 5),
            font=self._fonts["mono"],
        )
        self._log_text.grid(row=0, column=0, sticky="ew")
        log_scroll = ttk.Scrollbar(
            text_wrap,
            orient="vertical",
            command=self._log_text.yview,
            style="Collection.Vertical.TScrollbar",
        )
        log_scroll.grid(row=0, column=1, sticky="ns")
        self._log_text.configure(yscrollcommand=log_scroll.set)
        self._log_body.grid_remove()
        footer=ttk.Frame(bottom,style="CollectionPage.TFrame")
        footer.grid(row=2,column=0,sticky="ew",padx=scale_px(self,30),pady=(scale_px(self,3),scale_px(self,12)))
        footer.columnconfigure(2,weight=1)
        self._footer_output=ttk.Label(footer,text="保存至 ./downl_Pixiv",style="CollectionMuted.TLabel")
        self._footer_output.grid(row=0,column=0,sticky="w")
        self._make_action_button(footer,"open_output","打开文件夹",style="CollectionQuiet.TButton").grid(row=0,column=1,padx=(scale_px(self,24),0))
        self._account_label=ttk.Label(footer,text="账号 · 开始时校验",style="CollectionMuted.TLabel")
        self._account_label.grid(row=0,column=3,sticky="e")
        self._footer_quality=ttk.Label(footer,text="原图 / 全部图片页",style="CollectionLink.TLabel")
        self._footer_quality.grid(row=0,column=4,sticky="e",padx=(scale_px(self,24),0))

    # ------------------------------------------------------------------
    # Responsive layout (Tk main-thread Configure only; no after/timer)
    # ------------------------------------------------------------------

    def _on_self_configure(self, event: tk.Event) -> None:
        width = int(getattr(event, "width", 0) or 0)
        if width <= 1:
            try:
                width = self.winfo_width()
            except tk.TclError:
                return
        # A withdrawn/unrealized window reports a placeholder width; don't let
        # that alone flip a real layout decision below a usable desktop width.
        if width < scale_px(self, 200):
            return
        self._last_width = width
        content_width = max(1, width - scale_px(self, 28))
        self._update_header_layout(content_width >= self._breakpoint)
        self._update_follow_layout(width)
        self._layout_auxiliary(width)

    def _layout_auxiliary(self,width):
        if getattr(self,'_auxiliary_width',None)==width:return
        self._auxiliary_width=width
        s=lambda value:scale_px(self,value)
        compact=width<s(980)
        for group in (self._record_search_group,self._task_search_group):
            group.grid(row=1 if compact else 0,column=0 if compact else 1,
                sticky='w' if compact else 'e',pady=(s(12) if compact else 0,0))
        self._settings_columns.columnconfigure(1,weight=0 if compact else 1)
        self._settings_main.grid_configure(padx=(0,0 if compact else s(40)))
        self._settings_side.grid(row=1 if compact else 0,column=0 if compact else 1,
            sticky='new',pady=(s(32) if compact else 0,0))
        self._settings_side.configure(padding=(0 if compact else s(28),s(6),0,0))
        self._account_label.grid(row=1 if compact else 0,column=0 if compact else 3,
            columnspan=2 if compact else 1,sticky='w' if compact else 'e',pady=(s(6) if compact else 0,0))
        self._footer_quality.grid(row=1 if compact else 0,column=4,sticky='e',pady=(s(6) if compact else 0,0))
        self._footer_output.configure(wraplength=max(s(180),width-s(650)) if not compact else max(s(180),width-s(210)))
        self._status_label.configure(wraplength=max(s(200),width-s(170)))

    def _toggle_connection_options(self):
        if self._settings_connection_open.get():self._settings_connection.grid()
        else:self._settings_connection.grid_remove()

    def _update_follow_layout(self, view_width: int) -> None:
        content_width = max(1, int(view_width) - scale_px(self, 28))
        wide = content_width >= self._breakpoint
        if wide == self._follow_wide:
            return
        self._apply_follow_layout(wide)

    def _apply_follow_layout(self,wide):
        if not hasattr(self,'_follow_grid'):return
        self._follow_wide=bool(wide);s=lambda x:scale_px(self,x)
        self._follow_grid.columnconfigure(0,weight=2,minsize=0)
        self._follow_grid.columnconfigure(1,weight=1 if wide else 0,minsize=s(300) if wide else 0)
        self._follow_left.grid(row=0,column=0,sticky='new',padx=(0,s(38) if wide else 0))
        self._follow_filters.grid(row=0 if wide else 1,column=1 if wide else 0,sticky='new',pady=(0 if wide else s(22),0))
        if not hasattr(self,'_options_divider'):
            self._options_divider=self._separator(self._follow_grid,orient='vertical',style='Collection.TSeparator')
        if wide:self._options_divider.grid(row=0,column=1,sticky='nsw',padx=(0,0))
        else:self._options_divider.grid_remove()
        self._follow_filters.configure(padding=(s(28) if wide else 0,0,0,0))


    # ------------------------------------------------------------------
    # Public API: navigation and static text
    # ------------------------------------------------------------------

    def show_page(self,key):
        if key not in PAGE_KEYS:raise ValueError('unknown page key')
        if self._disposed:return
        self._active_page=key
        for current,page in self._pages.items():
            if current==key:page.grid(row=0,column=0,sticky='nsew')
            else:page.grid_remove()
        for current,button in self._nav_buttons.items():
            active=current==key
            button.configure(text=PAGE_LABELS[current],style='CollectionNavActive.TButton' if active else 'CollectionNav.TButton')
            self._nav_lines[current].configure(background=self._palette['accent' if active else 'page'])
        if key=='follow' and self._last_width>1:self._update_follow_layout(self._last_width)


    def _navigate(self, key: str) -> None:
        if self._disposed:
            return
        self.show_page(key)
        self._emit("navigate", {"page": key})

    def _run_nav_return(self, key: str) -> str:
        self._navigate(key)
        return "break"

    def set_account(self, text: Any) -> None:
        if self._disposed:
            return
        self._account_label.configure(text=_as_text(text))

    def set_output_label(self, text):
        if not self._disposed:
            self._footer_output.configure(text=_as_text(text))

    def set_status(self, text: Any, tone: str = "muted") -> None:
        if self._disposed:
            return
        style = STATUS_STYLES.get(tone, STATUS_STYLES["muted"])
        try:
            self._status_label.configure(text=_as_text(text), style=style)
        except tk.TclError:  # pragma: no cover - widget being destroyed
            pass

    def set_summary(self,values):
        if not isinstance(values,dict):raise ValueError('summary values must be a dict')
        if self._disposed:return
        for key,widget in (('title',self._summary_title),('description',self._summary_description),('counts',self._summary_counts),('resume_state',self._resume_state),('resume_position',self._resume_position)):
            if key in values:widget.configure(text=_display(values[key]))
        for key,label in self._stat_labels.items():
            if key in values:label.configure(text='—' if values[key] is None else _display(values[key]))
        if values.get('button_label'):self._set_action_label('start',_display(values['button_label']))


    def set_detail(self, text: Any) -> None:
        if self._disposed:
            return
        self._show_detail(_as_text(text) or DETAIL_PLACEHOLDER)

    def _show_detail(self,text):
        widget=getattr(self,'_detail_text',None)
        if widget is not None:
            widget.configure(state='normal');widget.delete('1.0','end');widget.insert('1.0',text);widget.configure(state='disabled')

    def set_task_detail(self,job_id,text):
        if self._disposed:return
        if not hasattr(self,'_task_details'):self._task_details={}
        self._task_details[str(job_id)]=_as_text(text)
        while len(self._task_details)>TABLE_ROW_LIMIT:
            self._task_details.pop(next(iter(self._task_details)))
        self._render_task_detail()

    def follow_action_label(self):
        if getattr(self,'_chosen_mode',None)=='artist':return '收集指定画师'
        return '收集关注新作' if self._follow_mode.get()=='new' else '开始 / 继续补全'



    # ------------------------------------------------------------------
    # Public API: actions
    # ------------------------------------------------------------------

    def configure_action(
        self, action: str, *, enabled: bool, label: str | None = None
    ) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        if label is not None and not isinstance(label, str):
            raise ValueError("label must be a string or None")
        self._action_enabled[action] = enabled
        if label is not None:
            self._action_label_overrides[action] = label
        self._refresh_action(action)

    def _set_action_label(self, action: str, label: str) -> None:
        self._action_label_overrides[action] = label
        self._refresh_action(action)

    def _action_text(self, action: str) -> str:
        override = self._action_label_overrides.get(action)
        if action == "log_toggle":
            base = override or "日志"
            if override is None:
                return "收起日志 ▴" if self._log_open else "展开日志 ▾"
            return f"{base} {'▴' if self._log_open else '▾'}"
        if override is not None:
            return override
        return self._action_default_labels.get(action, action)

    def _refresh_action(self,action):
        enabled=self._action_enabled.get(action,True)
        for button in self._action_buttons.get(action,()):
            try:
                if not button.winfo_exists():continue
                button.configure(text=self._action_text(action))
                available=enabled and (button not in getattr(self,'_task_controls',()) or self.selected_id('tasks') is not None)
                button.state(['!disabled'] if available else ['disabled'])
            except tk.TclError:pass


    def _run_action(self, action: str) -> None:
        if self._disposed:
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:  # pragma: no cover - widget being destroyed
            return
        if not self._action_enabled.get(action, True):
            return
        if action == 'start' and getattr(self,'_chosen_mode',None)=='artist' and not self._follow_artist_ids.get().strip():
            self.set_status('请先填写画师 ID 或 Pixiv 画师链接','warning')
            self._artist_entry.focus_set()
            return
        payload: dict[str, Any] = {}
        if action == "open_artwork":
            record_id = self.selected_id("records")
            if record_id:
                payload["id"] = record_id
        elif action == "log_toggle":
            payload["open"] = not self._log_open
        elif action in {'open_output','browse_output'} and self._active_page=='settings':
            payload['scope']='settings'
        elif action in {'open_output','browse_output'} and self._active_page in {'follow','sources'}:
            payload['scope']=self._active_page
        self._emit(action, payload)

    def _run_action_return(self, action: str) -> str:
        self._run_action(action)
        return "break"

    def get_source_form(self):
        return {**self.get_follow_form(),**self._source_panel.get_form()}

    def set_source_form(self,values):
        self._source_panel.set_form(values)

    def _refresh_filter_summary(self):
        if self._disposed:return
        self._filter_summary.set(filter_description({'min_bookmarks_enabled':self._follow_min_enabled.get(),
            'min_bookmarks':self._follow_min_text.get(),'exclude_ai':self._follow_exclude_ai.get()}))

    def _emit(self, action: str, payload: dict | None = None) -> None:
        if self._disposed:
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:  # pragma: no cover - widget being destroyed
            return
        callback = self._on_action
        if callback is None:
            return
        callback(action, dict(payload or {}))

    # ------------------------------------------------------------------
    # Public API: follow form
    # ------------------------------------------------------------------

    def get_follow_form(self) -> dict[str, Any]:
        return {
            "mode": self._follow_mode.get(),
            "min_bookmarks_enabled": bool(self._follow_min_enabled.get()),
            "min_bookmarks": self._follow_min_text.get(),
            "exclude_ai": bool(self._follow_exclude_ai.get()),
            "max_artists": self._follow_max_artists.get(),
            "max_requests": self._follow_max_requests.get(),
            "max_works": self._follow_max_works.get(),
            "artist_ids": self._follow_artist_ids.get(),
            "start_artist_id": self._follow_start_artist.get(),
            "start_ordinal": self._follow_start_ordinal.get(),
            "output_relative": self._follow_output.get(),
            **self._follow_location,
            "folder_by_artist": bool(self._follow_folder_by_artist.get()),
        }

    def set_follow_form(self, values: dict) -> None:
        if not isinstance(values, dict):
            raise ValueError("follow form values must be a dict")
        if self._disposed:
            return
        if "mode" in values and values["mode"] in FOLLOW_MODES:
            self._follow_mode.set(values["mode"])
        if "min_bookmarks_enabled" in values:
            self._follow_min_enabled.set(bool(values["min_bookmarks_enabled"]))
        if "min_bookmarks" in values:
            self._follow_min_text.set(_as_text(values["min_bookmarks"]))
        if "exclude_ai" in values:
            self._follow_exclude_ai.set(bool(values["exclude_ai"]))
        for key, variable in (
            ("max_artists", self._follow_max_artists),
            ("max_requests", self._follow_max_requests),
            ("max_works", self._follow_max_works),
            ("artist_ids", self._follow_artist_ids),
            ("start_artist_id", self._follow_start_artist),
            ("start_ordinal", self._follow_start_ordinal),
            ("output_relative", self._follow_output),
        ):
            if key in values:
                variable.set(_as_text(values[key]))
        if "folder_by_artist" in values:
            self._follow_folder_by_artist.set(bool(values["folder_by_artist"]))
        self._update_location('follow', values)
        self._sync_min_bookmarks_state()
        if 'mode' in values:self._chosen_mode = self._follow_mode.get()
        self._paint_follow_mode()
        self._set_action_label('start',self.follow_action_label())
        if any(variable.get().strip() for variable in (self._follow_artist_ids,self._follow_start_artist,self._follow_start_ordinal)):
            self._follow_more.set(True);self._update_follow_advanced()
        self._footer_output.configure(text="保存至 "+self._follow_output_display.get())

    # ------------------------------------------------------------------
    # Public API: records form
    # ------------------------------------------------------------------

    def get_record_form(self) -> dict[str, Any]:
        scope = self._record_scope.get()
        if scope not in ("batch", "history"):
            scope = "batch"
        label = self._record_result.get()
        code = RESULT_LABELS.get(label, label)
        if code not in RESULT_CODES:
            code = "all"
        return {
            "scope": scope,
            "search": self._record_search.get(),
            "result": code,
        }

    def set_record_form(self, values):
        if self._disposed:return
        if values.get('scope') in ('batch','history'):
            self._record_scope.set(values['scope'])
        if 'search' in values:self._record_search.set(_as_text(values['search']))
        if values.get('result') in RESULT_CODES:
            self._record_result.set(dict(RESULT_CHOICES)[values['result']])

    def set_records_info(self, text: Any) -> None:
        if self._disposed:
            return
        try:
            self._records_page_label.configure(text=_as_text(text))
        except tk.TclError:  # pragma: no cover - widget being destroyed
            pass

    def set_task_info(self, text: Any) -> None:
        if self._disposed:
            return
        try:
            self._task_page_label.configure(text=_as_text(text))
        except tk.TclError:  # pragma: no cover - widget being destroyed
            pass

    # ------------------------------------------------------------------
    # Public API: settings form
    # ------------------------------------------------------------------

    def get_settings_form(self) -> dict[str, Any]:
        return {
            "output_relative": self._settings_output.get(),
            **self._settings_location,
            "folder_by_artist": bool(self._settings_folder.get()),
            "workers": self._settings_workers.get(),
            "name_template": self._settings_name_template.get(),
            "api_interval": self._settings_api_interval.get(),
            "image_interval": self._settings_image_interval.get(),
            "user_agent": self._settings_user_agent.get(),
        }

    def set_settings_form(self, values: dict) -> None:
        if not isinstance(values, dict):
            raise ValueError("settings values must be a dict")
        if self._disposed:
            return
        if "output_relative" in values:
            self._settings_output.set(_as_text(values["output_relative"]))
        if "folder_by_artist" in values:
            self._settings_folder.set(bool(values["folder_by_artist"]))
        if "workers" in values:
            self._settings_workers.set(_as_text(values["workers"]))
        if "name_template" in values:
            self._settings_name_template.set(_as_text(values["name_template"]))
        if "api_interval" in values:
            self._settings_api_interval.set(_as_text(values["api_interval"]))
        if "image_interval" in values:
            self._settings_image_interval.set(_as_text(values["image_interval"]))
        if "user_agent" in values:
            self._settings_user_agent.set(_as_text(values["user_agent"]))
        self._update_location('settings', values)

    def _update_location(self, scope, values):
        location = getattr(self, '_' + scope + '_location')
        for key in ('output_kind', 'output_root_id'):
            if key in values:
                location[key] = _as_text(values[key])
        display = getattr(self, '_' + scope + '_output_display')
        relative = getattr(self, '_' + scope + '_output').get()
        if 'output_display' in values:
            display.set(_as_text(values['output_display']))
        elif location['output_kind'] == 'project':
            display.set('./' + relative)

    # ------------------------------------------------------------------
    # Public API: native tables
    # ------------------------------------------------------------------

    def set_artists(self,rows):
        if self._disposed:return
        self._fill_tree('artists',rows)
        count=len(self._trees['artists'].get_children())
        self._artist_count.configure(text=f'已载入 {count} 位' if count else '开始后载入')


    def set_tasks(self,rows):
        if self._disposed:return
        self._task_rows_cache=[dict(row) for row in _bounded_rows(rows) if isinstance(row,dict)]
        if not hasattr(self,'_task_progress'):self._task_progress={}
        ids={str(row.get('id')) for row in self._task_rows_cache}
        self._task_progress={key:value for key,value in self._task_progress.items() if key in ids}
        self._task_details={key:value for key,value in getattr(self,'_task_details',{}).items() if key in ids}
        for row in self._task_rows_cache:
            if row.get('counts_authoritative'):
                self._task_progress.pop(str(row.get('id')),None)
            else:
                row.update(self._task_progress.get(str(row.get('id')),{}))
        self._filter_tasks()
        self._task_page_label.configure(text=f'本页 {len(self._task_rows_cache)} 项')



    def set_records(self, rows: list[dict], *, page_text: str = "") -> None:
        self._fill_tree("records", rows)
        self.set_records_info(page_text)

    @property
    def verification_slot(self):
        """The mounted verification panel, or None before construction."""
        return getattr(self, "_verification_panel", None)

    def set_verification_rows(self, rows, *, page_text: str = "") -> None:
        if self._disposed:
            return
        panel = getattr(self, "_verification_panel", None)
        if panel is not None:
            panel.set_rows(rows, page_text=page_text)

    def set_verification_result(self, text: Any, tone: str = "muted") -> None:
        if self._disposed:
            return
        panel = getattr(self, "_verification_panel", None)
        if panel is not None:
            panel.set_result(text, tone=tone)

    def set_verification_busy(self, busy: bool, *, text: str = "") -> None:
        """Disable the panel actions while one recovery pass is running."""
        if self._disposed:
            return
        panel = getattr(self, "_verification_panel", None)
        if panel is not None:
            panel.set_busy(bool(busy), text=text or None)

    def selected_id(self, kind: str) -> str | None:
        tree = self._trees.get(kind)
        if tree is None:
            raise ValueError("kind must be artists, tasks or records")
        try:
            selection = tree.selection()
        except tk.TclError:  # pragma: no cover - widget being destroyed
            return None
        if not selection:
            return None
        return str(selection[0])

    def _fill_tree(self, kind: str, rows: list[dict] | None) -> None:
        if self._disposed:
            return
        tree = self._trees.get(kind)
        if tree is None:
            raise ValueError("unknown table kind")
        columns = _TREE_COLUMNS[kind]
        prepared: list[tuple[str, tuple[str, ...]]] = []
        seen: set[str] = set()
        for row in _bounded_rows(rows):
            if not isinstance(row, dict):
                continue
            row_id = row.get("id")
            if row_id is None:
                continue
            iid = str(row_id)
            if iid in seen:
                continue
            seen.add(iid)
            prepared.append(
                (
                    iid,
                    tuple(_display(row.get(column, "")) for column, *_ in columns),
                )
            )
            if len(prepared) >= TABLE_ROW_LIMIT:
                break
        signature = tuple(prepared)
        children = tree.get_children()
        if (
            self._table_signatures.get(kind) == signature
            and len(children) == len(prepared)
        ):
            # Same bounded data: keep the widget, selection and scroll intact.
            # No delete/insert means no flashing on periodic identical refresh.
            self._show_hint(kind, not prepared)
            return
        self._table_signatures[kind] = signature
        selected = self.selected_id(kind)
        scroll_fraction = None
        if children:
            try:
                scroll_fraction = tree.yview()[0]
            except tk.TclError:  # pragma: no cover - widget being destroyed
                scroll_fraction = None
        self._suspend_select = True
        try:
            if children:
                tree.delete(*children)
            for index,(iid,values) in enumerate(prepared):
                tree.insert("", "end", iid=iid, values=values,tags=("alternate",) if index % 2 else ())
            if selected and tree.exists(selected):
                tree.selection_set(selected)
            if scroll_fraction is not None and tree.get_children():
                tree.yview_moveto(scroll_fraction)
        finally:
            self._suspend_select = False
        still_selected = selected if selected and tree.exists(selected) else None
        self._last_selected[kind] = still_selected
        self._show_hint(kind, not tree.get_children())
        if kind == "records" and still_selected is None:
            self._show_detail(DETAIL_PLACEHOLDER)

    def _on_tree_select(self, kind: str) -> None:
        if self._suspend_select or self._disposed:
            return
        tree = self._trees.get(kind)
        if tree is None:
            return
        try:
            selection = tree.selection()
        except tk.TclError:  # pragma: no cover - widget being destroyed
            return
        if not selection:
            self._last_selected[kind] = None
            if kind == "records":
                self._show_detail(DETAIL_PLACEHOLDER)
            return
        row_id = str(selection[0])
        if row_id == self._last_selected.get(kind):
            return
        self._last_selected[kind] = row_id
        if kind == "tasks":
            self._refresh_task_actions();self._render_task_detail()
        self._emit(SELECT_ACTIONS[kind], {"id": row_id})

    # ------------------------------------------------------------------
    # Public API: bounded log view
    # ------------------------------------------------------------------

    def set_log_open(self, open_: bool) -> None:
        if type(open_) is not bool:
            raise ValueError("set_log_open expects a bool")
        self._log_open = open_
        if self._disposed:
            return
        try:
            if open_:
                self._log_body.grid(row=1, column=0, sticky="ew")
            else:
                self._log_body.grid_remove()
        except tk.TclError:  # pragma: no cover - widget being destroyed
            return
        self._refresh_log_toggle()
        if open_ and self._log_follow:
            try:
                self._log_text.see("end")
            except tk.TclError:
                pass

    def log_at_bottom(self) -> bool:
        if self._disposed:
            return True
        if self._log_line_count == 0:
            return True
        try:
            _first, bottom = self._log_text.yview()
        except tk.TclError:  # pragma: no cover - widget being destroyed
            return True
        if bottom >= 0.999:
            return True
        try:
            top_line = int(self._log_text.index("@0,0").split(".")[0])
        except tk.TclError:  # pragma: no cover - widget being destroyed
            return True
        return top_line >= self._log_line_count

    def clear_log_text(self) -> None:
        if self._disposed:
            return
        try:
            self._log_text.configure(state="normal")
            self._log_text.delete("1.0", "end")
            self._log_text.configure(state="disabled")
        except tk.TclError:  # pragma: no cover - widget being destroyed
            return
        self._log_line_count = 0

    def set_log_notice(self, text: Any) -> None:
        if self._disposed:
            return
        try:
            self._log_notice.configure(text=_as_text(text))
        except tk.TclError:  # pragma: no cover - widget being destroyed
            pass

    def set_log_follow(self, follow: bool) -> None:
        if type(follow) is not bool:
            raise ValueError("follow must be boolean")
        self._log_follow = follow

    def apply_log_delta(self, delta: ViewDelta) -> None:
        if self._disposed:
            return
        if not all(
            hasattr(delta, field)
            for field in ("append_lines", "remove_lines", "follow")
        ):
            raise ValueError("apply_log_delta expects a ViewDelta")
        follow = bool(delta.follow)
        self._log_follow = follow
        append_lines = [
            _as_text(line).replace("\r", " ").replace("\n", " ")
            for line in delta.append_lines
        ]
        remove_lines = int(delta.remove_lines)
        if remove_lines < 0:
            remove_lines = 0
        removed = min(remove_lines, self._log_line_count)
        if removed == 0 and not append_lines:
            return
        top_line = None
        try:
            if removed:
                top_line = int(self._log_text.index("@0,0").split(".")[0])
            self._log_text.configure(state="normal")
            try:
                if removed >= self._log_line_count:
                    self._log_text.delete("1.0", "end")
                elif removed:
                    self._log_text.delete("1.0", f"{removed + 1}.0")
                if append_lines:
                    remaining = self._log_line_count - removed
                    content = "\n".join(append_lines)
                    if remaining > 0:
                        self._log_text.insert("end", "\n" + content)
                    else:
                        self._log_text.insert("end", content)
            finally:
                self._log_text.configure(state="disabled")
        except tk.TclError:  # pragma: no cover - widget being destroyed
            return
        self._log_line_count = max(0, self._log_line_count - removed) + len(append_lines)
        if follow:
            try:
                self._log_text.see("end")
            except tk.TclError:  # pragma: no cover - widget being destroyed
                pass
        elif removed and top_line is not None:
            # Keep reading position: the same content line moved up by the
            # number of removed oldest lines.
            new_top = max(1, top_line - removed)
            try:
                self._log_text.yview(f"{new_top}.0")
            except tk.TclError:  # pragma: no cover - widget being destroyed
                pass

    def _refresh_log_toggle(self) -> None:
        self._refresh_action("log_toggle")

    # ------------------------------------------------------------------
    # Public API: disposal
    # ------------------------------------------------------------------

    def dispose(self) -> None:
        """Release Tk variables, callbacks and bindings on the Tk owner thread.

        The view itself and the master are not destroyed here.  All Tcl
        operations happen synchronously on the owner thread so no Tk variable
        finalizer can later run inside a background GC.
        """
        if self._disposed:
            return
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError(
                "CollectionView.dispose must run on the Tk owner thread"
            )
        self._dispose_impl()

    def destroy(self) -> None:
        """Idempotent destroy that always goes through the owner-thread dispose."""
        if self._destroyed:
            return
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError(
                "CollectionView.destroy must run on the Tk owner thread"
            )
        try:
            self._dispose_impl()
        except Exception:  # pragma: no cover - defensive teardown
            self._fallback_release_variables()
        self._destroyed = True
        try:
            super().destroy()
        except (tk.TclError, RuntimeError):  # pragma: no cover - already gone
            pass
        finally:
            self._clear_references()

    def _dispose_impl(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._on_action = None
        panel = getattr(self, "_verification_panel", None)
        if panel is not None:
            panel.dispose()
        self._unbind_all()
        self._clear_widget_commands()
        self._release_variables()

    def _on_self_destroy_event(self, event: tk.Event) -> None:
        """Handle root destruction even if the caller never calls destroy()."""
        if getattr(event, "widget", None) is not self:
            return
        if not self._disposed:
            try:
                self._dispose_impl()
            except Exception:  # pragma: no cover - defensive teardown
                self._fallback_release_variables()
        self._destroyed = True
        self._clear_references()

    # -- teardown helpers (owner thread only) ---------------------------

    def _snapshot_owned_widgets(self) -> None:
        stack = [self]
        seen: set[int] = set()
        owned: list[tk.Misc] = []
        while stack:
            widget = stack.pop()
            if id(widget) in seen:
                continue
            seen.add(id(widget))
            owned.append(widget)
            try:
                stack.extend(widget.winfo_children())
            except tk.TclError:  # pragma: no cover - defensive
                continue
        self._owned_widgets = owned

    def _unbind_all(self) -> None:
        bindings = self._bindings
        self._bindings = []
        for widget, sequence, funcid in bindings:
            # Keep the lifecycle hook until the widget actually goes away so a
            # later root.destroy() can still detach and release Tk references.
            if sequence == "<Destroy>":
                continue
            try:
                if widget.winfo_exists():
                    widget.unbind(sequence, funcid)
            except tk.TclError:  # pragma: no cover - widget being destroyed
                continue

    def _clear_widget_commands(self) -> None:
        """Drop Tcl command callbacks so Python widgets/callbacks can be freed."""
        stack = [self]
        seen: set[int] = set()
        while stack:
            widget = stack.pop()
            if id(widget) in seen:
                continue
            seen.add(id(widget))
            try:
                stack.extend(widget.winfo_children())
            except tk.TclError:  # pragma: no cover - widget being destroyed
                continue
            try:
                keys = widget.keys()
            except tk.TclError:  # pragma: no cover - widget being destroyed
                continue
            for option in ("command", "yscrollcommand", "xscrollcommand"):
                if option in keys:
                    try:
                        widget.configure(**{option: ""})
                    except tk.TclError:  # pragma: no cover - defensive
                        continue

    def _release_variables(self) -> None:
        variables = self._variables
        self._variables = []
        for variable in variables:
            self._release_variable(variable)

    @staticmethod
    def _release_variable(variable: tk.Variable) -> None:
        tk_obj = getattr(variable, "_tk", None)
        name = getattr(variable, "_name", None)
        try:
            if tk_obj is not None and name:
                exists = tk_obj.getboolean(tk_obj.call("info", "exists", name))
                if exists:
                    tk_obj.globalunsetvar(name)
                commands = getattr(variable, "_tclCommands", None)
                if commands:
                    for command in list(commands):
                        try:
                            tk_obj.deletecommand(command)
                        except (tk.TclError, RuntimeError):
                            pass
        except (tk.TclError, RuntimeError, AttributeError):
            pass
        finally:
            # Variable.__del__ returns immediately when _tk is None; later
            # background GC therefore never touches Tcl.
            variable._tk = None
            variable._root = None
            variable._name = None
            variable._tclCommands = None

    def _fallback_release_variables(self) -> None:
        for variable in list(self._variables):
            try:
                variable._tk = None
                variable._root = None
                variable._name = None
                variable._tclCommands = None
            except Exception:  # pragma: no cover - defensive teardown
                pass
        self._variables.clear()

    def _clear_references(self) -> None:
        """Release our references to already-destroyed widgets and variables."""
        widgets = self._owned_widgets
        self._owned_widgets = []
        for widget in widgets:
            try:
                tk_obj = getattr(widget, "tk", None)
                commands = getattr(widget, "_tclCommands", None)
                if tk_obj is not None and commands:
                    for command in list(commands):
                        try:
                            tk_obj.deletecommand(command)
                        except (tk.TclError, RuntimeError):
                            pass
                widget._tclCommands = None
            except Exception:  # pragma: no cover - defensive teardown
                pass
            if widget is self:
                continue
            try:
                widget.master = None
            except Exception:  # pragma: no cover - defensive teardown
                pass
            try:
                widget.children.clear()
            except Exception:  # pragma: no cover - defensive teardown
                pass
            try:
                widget.tk = None
            except Exception:  # pragma: no cover - defensive teardown
                pass
        try:
            master = self.master
            if master is not None:
                master.children.pop(self._name, None)
        except Exception:  # pragma: no cover - defensive teardown
            pass
        try:
            self.children.clear()
        except Exception:  # pragma: no cover - defensive teardown
            pass
        try:
            self._tclCommands = None
        except Exception:  # pragma: no cover - defensive teardown
            pass
        self._on_action = None
        self._bindings = []
        self._variables = []
        self._action_buttons.clear()
        self._nav_buttons.clear()
        self._source_buttons.clear()
        self._source_panel=None
        self._trees.clear()
        self._tree_hints.clear()
        self._hint_specs.clear()
        self._table_signatures.clear()
        self._pages.clear()
        for name in (
            "_header",
            "_title_label",
            "_account_label",
            "_nav_frame",
            "_content",
            "_follow_grid",
            "_follow_left",
            "_follow_filters",
            "_follow_advanced",
            "_follow_mode_history",
            "_follow_mode_new",
            "_min_checkbox",
            "_min_entry",
            "_exclude_ai_checkbox",
            "_more_checkbox",
            "_folder_checkbox",
            "_sync_button",
            "_restart_button",
            "_start_button",
            "_summary_title",
            "_summary_description",
            "_summary_counts",
            "_detail_text",
            "_records_page_label",
            "_task_page_label",
            "_record_search_entry",
            "_record_query_button",
            "_record_result_combo",
            "_record_scope_batch",
            "_record_scope_history",
            "_settings_folder_checkbox",
            "_status_label",
            "_log_toggle",
            "_log_notice",
            "_log_text",
            "_log_body",
            "_follow_mode",
            "_follow_min_enabled",
            "_filter_summary",
            "_follow_min_text",
            "_follow_exclude_ai",
            "_follow_max_artists",
            "_follow_max_requests",
            "_follow_max_works",
            "_follow_artist_ids",
            "_follow_start_artist",
            "_follow_start_ordinal",
            "_follow_output",
            "_follow_output_display",
            "_follow_folder_by_artist",
            "_follow_more",
            "_record_scope",
            "_record_search",
            "_record_result",
            "_settings_output",
            "_settings_output_display",
            "_settings_folder",
            "_settings_workers",
            "_settings_name_template",
            "_settings_api_interval",
            "_settings_image_interval",
            "_settings_user_agent",
        ):
            if hasattr(self, name):
                setattr(self, name, None)

    def _separator(self,parent,orient='horizontal',style=None):
        return tk.Frame(parent,background=self._palette['line'],borderwidth=0,
            height=scale_px(self,1),width=scale_px(self,1),takefocus=False)

    def _rule(self, parent, row, *, columnspan=1, pady=0):
        self._separator(parent, style='Collection.TSeparator').grid(
            row=row,column=0,columnspan=columnspan,sticky='ew',pady=pady)

    def _page_heading(self, parent, title, description):
        frame=ttk.Frame(parent,style='CollectionPage.TFrame')
        frame.grid(row=0,column=0,sticky='ew',pady=(scale_px(self,24),scale_px(self,18)))
        frame.columnconfigure(0,weight=1)
        ttk.Label(frame,text=title,style='CollectionTitle.TLabel').grid(row=0,column=0,sticky='w')
        ttk.Label(frame,text=description,style='CollectionSubtitle.TLabel').grid(
            row=1,column=0,sticky='w',pady=(scale_px(self,5),0))
        return frame

    def _scroll_page(self,event):
        if self._disposed:return
        kind=getattr(event.widget,'winfo_class',lambda:'')()
        if kind=='TCombobox':return
        if kind in ('Text','Treeview'):
            first,last=event.widget.yview()
            if float(first)>0.0001 or float(last)<.9999:return
        scroll=getattr(self,'_'+self._active_page+'_scroll',None)
        if scroll is not None and scroll.body.winfo_height()>scroll.canvas.winfo_height():
            scroll.canvas.yview_scroll(-int(event.delta/120),'units')
            return 'break'

    def _fit_hero(self,event):
        if self._disposed:return
        s=lambda x:scale_px(self,x)
        width=max(s(280),event.width-s(44))
        if getattr(self,'_hero_width',None)==width:return
        self._hero_width=width
        self._summary_title.configure(wraplength=width)
        self._resume_position.configure(wraplength=width)
        self._summary_description.configure(wraplength=max(s(120),width-self._start_button.winfo_reqwidth()-s(26)))

    def _choose_follow_mode(self,mode):
        self._follow_mode.set('new' if mode=='new' else 'history')
        self._chosen_mode=mode
        if mode=='artist':
            self._follow_more.set(True);self._update_follow_advanced();self._artist_entry.focus_set()
        self._paint_follow_mode()
        self._set_action_label('start',self.follow_action_label())
        if mode!='artist' and any(variable.get().strip() for variable in (self._follow_artist_ids,self._follow_start_artist,self._follow_start_ordinal)):
            self._follow_more.set(True);self._update_follow_advanced()
            self.set_status('已切换模式；更多设置中填写的画师范围和起点仍然生效。')

    def _paint_follow_mode(self):
        mode=getattr(self,'_chosen_mode',self._follow_mode.get())
        if mode!='artist':mode=self._follow_mode.get()
        self._hero_mode.configure(text={'history':'历史作品 · 逐步收全','new':'关注新作 · 持续收集','artist':'指定画师 · 历史补全'}[mode])
        for key,button in self._mode_buttons.items():
            active=key==mode
            button.configure(style='CollectionTabActive.TButton' if active else 'CollectionTab.TButton')
            self._mode_lines[key].configure(background=self._palette['accent' if active else 'page'])

    def _toggle_artists(self):
        opened=bool(self._artists_section.winfo_manager())
        if opened:self._artists_section.grid_remove()
        else:self._artists_section.grid()
        self._artists_toggle.configure(text='查看画师进度  ＋' if opened else '收起画师进度  −')

    def _choose_task_filter(self,key):
        self._task_filter=key;self._filter_tasks()

    def _filter_tasks(self):
        query=self._task_search.get().strip().casefold()
        groups={'running':{'下载中','查找中','运行中','收集中','正在收尾','冷却中','等待冷却','正在暂停','正在取消'},'paused':{'已暂停','部分完成','需要处理'},'finished':{'已完成','已取消','范围已结束'}}
        rows=self._task_rows_cache
        for key,button in self._task_tabs.items():
            count=len(rows) if key=='all' else sum(row.get('state') in groups[key] for row in rows)
            button.configure(text={'all':'全部','running':'进行中','paused':'已暂停','finished':'已结束'}[key]+f' {count}',
                style='CollectionTabActive.TButton' if key==self._task_filter else 'CollectionTab.TButton')
            self._task_lines[key].configure(background=self._palette['accent' if key==self._task_filter else 'page'])
        filtered=[row for row in rows if (self._task_filter=='all' or row.get('state') in groups[self._task_filter])
            and (not query or query in ' '.join(_display(row.get(k,'')) for k in ('title','source','reason','state')).casefold())]
        self._fill_tree('tasks',filtered)
        self._trees['tasks'].configure(height=max(2,min(6,len(filtered))))
        self._refresh_task_actions();self._render_task_detail()

    def _scrollbar_set(self,bar,first,last):
        bar.set(first,last)
        visible=float(first)>0.0001 or float(last)<.9999
        if visible and not bar.winfo_manager():bar.grid()
        elif not visible and bar.winfo_manager():bar.grid_remove()

    def set_task_progress(self,job_id,counts):
        if self._disposed:return
        for row in self._task_rows_cache:
            if str(row.get('id'))==str(job_id):
                if row.get('counts')==counts:return
                row['counts']=counts
                self._task_progress[str(job_id)]={'counts':counts}
                self._filter_tasks()
                return

    def select_task(self,job_id):
        tree=self._trees['tasks']
        if tree.exists(str(job_id)):
            tree.selection_set(str(job_id));self._last_selected['tasks']=str(job_id)
            self._refresh_task_actions();self._render_task_detail()

    def _refresh_task_actions(self):
        for action in ('pause','resume','retry_failed','results','cancel'):self._refresh_action(action)

    def _render_task_detail(self):
        widget=getattr(self,'_task_detail_text',None)
        if widget is None:return
        selected=self.selected_id('tasks')
        text=getattr(self,'_task_details',{}).get(selected,'正在读取这个任务的详情…') if selected else '选择一个任务，查看进度和本批结果。'
        if widget.get('1.0','end-1c')==text:return
        widget.configure(state='normal');widget.delete('1.0','end');widget.insert('1.0',text);widget.configure(state='disabled')
        widget.see('1.0')




__all__ = ["CollectionView"]

