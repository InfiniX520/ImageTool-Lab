"""Compact source forms using the existing Tk theme and owner lifecycle."""
import tkinter as tk
from tkinter import ttk

from core.source_settings import RANKINGS, SEARCH_TARGETS, SORTS
from gui.collection_theme import scale_px


class SourcePanel:
    def __init__(self, view, parent):
        self.view=view
        self.s=lambda value:scale_px(view,value)
        self.variables={}
        self.cards={}
        self.tabs={}
        self.lines={}
        self.frame=ttk.Frame(parent,style='CollectionPage.TFrame')
        self.frame.grid(row=1,column=0,sticky='ew')
        self.frame.columnconfigure(0,weight=1)
        self.variable('source','search')
        nav=ttk.Frame(self.frame,style='CollectionPage.TFrame');nav.grid(row=0,column=0,sticky='w')
        for i,(key,label) in enumerate((('search','搜索'),('ranking','排行榜'),('artist','指定画师'),('recommendations','推荐'),('intersection','组合筛选'))):
            slot=ttk.Frame(nav,style='CollectionPage.TFrame');slot.grid(row=0,column=i,padx=(0,self.s(20)))
            button=ttk.Button(slot,text=label,style='CollectionTab.TButton',command=lambda key=key:self.select(key))
            button.grid(row=0,column=0);self.tabs[key]=button
            line=tk.Frame(slot,height=self.s(2),background=view._palette['page'],borderwidth=0)
            line.grid(row=1,column=0,sticky='ew',pady=(self.s(6),0));self.lines[key]=line
        view._rule(self.frame,1,pady=(self.s(8),self.s(20)))
        self.columns=ttk.Frame(self.frame,style='CollectionPage.TFrame');self.columns.grid(row=2,column=0,sticky='ew')
        self.columns.columnconfigure(0,weight=1)
        self.columns.columnconfigure(1,weight=0)
        self.left=ttk.Frame(self.columns,style='CollectionPage.TFrame');self.left.grid(row=0,column=0,sticky='new',padx=(0,self.s(32)));self.left.columnconfigure(0,weight=1)
        self.right=ttk.Frame(self.columns,style='CollectionPage.TFrame');self.right.grid(row=0,column=1,sticky='new');self.right.columnconfigure(1,weight=1)
        self.selection=ttk.Frame(self.left,style='CollectionPage.TFrame');self.selection.grid(row=0,column=0,sticky='ew')
        ttk.Label(self.selection,text='同时出现在以下来源中',style='CollectionSection.TLabel').grid(row=0,column=0,columnspan=2,sticky='w')
        for i,(key,label) in enumerate((('follow','关注新作'),('ranking','排行榜'),('search','关键词'),('artist','指定画师'))):
            variable=self.variable('include_'+key,key in {'follow','ranking'})
            ttk.Checkbutton(self.selection,text=label,variable=variable,command=self.refresh,style='Collection.TCheckbutton').grid(row=1+i//2,column=i%2,sticky='w',padx=(0,self.s(24)),pady=self.s(8))
        self.entry(self.selection,3,'每来源最多候选','per_source_limit','200')
        ttk.Label(self.selection,text='只求已查询范围的交集；数量越大，查询越久。',style='CollectionMuted.TLabel',wraplength=self.s(430)).grid(row=4,column=0,columnspan=2,sticky='w',pady=(0,self.s(20)))
        search=self.card('search',1,'关键词与排序')
        self.entry(search,1,'关键词','search_word','')
        self.combo(search,2,'匹配方式','search_target',tuple(SEARCH_TARGETS))
        self.combo(search,3,'排序','search_sort',tuple(SORTS))
        self.entry(search,4,'起始日期','search_start_date','')
        self.entry(search,5,'结束日期','search_end_date','')
        ttk.Label(search,text='日期可留空；填写格式为 2026-10-22。',style='CollectionMuted.TLabel').grid(row=6,column=0,columnspan=2,sticky='w',pady=(self.s(4),0))
        ranking=self.card('ranking',2,'榜单与日期')
        self.combo(ranking,1,'榜单','ranking_mode',tuple(RANKINGS))
        self.entry(ranking,2,'指定日期','ranking_date','')
        ttk.Label(ranking,text='留空使用最近已发布榜单。',style='CollectionMuted.TLabel').grid(row=3,column=0,columnspan=2,sticky='w')
        self.entry(ranking,4,'范围起始','ranking_start_date','')
        self.entry(ranking,5,'范围结束','ranking_end_date','')
        ttk.Label(ranking,text='日榜可选最多 31 天；指定日期与日期范围二选一。',style='CollectionMuted.TLabel',wraplength=self.s(430)).grid(row=6,column=0,columnspan=2,sticky='w',pady=(self.s(4),0))
        artist=self.card('artist',3,'画师作品集')
        self.entry(artist,1,'画师 ID / 链接','source_artist_ids','')
        ttk.Label(artist,text='多位画师用逗号分隔；同时收集插画与漫画的全部图片页。',style='CollectionMuted.TLabel',wraplength=self.s(430)).grid(row=2,column=0,columnspan=2,sticky='w')
        rec=self.card('recommendations',4,'推荐来源')
        self.combo(rec,1,'推荐方式','recommendation_kind',('个性化推荐','相似作品'))
        self.related_entry=self.entry(rec,2,'原作品 ID / 链接','related_illust_id','')
        ttk.Label(rec,text='选择相似作品时填写；个性化推荐使用当前账号。',style='CollectionMuted.TLabel',wraplength=self.s(430)).grid(row=3,column=0,columnspan=2,sticky='w')
        self.variables['recommendation_kind'].trace_add('write',lambda *args:self.refresh())
        self._common_options()
        self.wide=None
        view._bind_event(self.frame,'<Configure>',self.resize)
        self.refresh()

    def variable(self,name,value):
        variable=self.view._owned_variable(tk.BooleanVar if type(value) is bool else tk.StringVar,value)
        self.variables[name]=variable
        return variable

    def card(self,key,row,title):
        card=ttk.Frame(self.left,style='CollectionPage.TFrame');card.grid(row=row,column=0,sticky='ew',pady=(0,self.s(24)));card.columnconfigure(1,weight=1)
        ttk.Label(card,text=title,style='CollectionSection.TLabel').grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,self.s(12)))
        self.cards[key]=card
        return card

    def entry(self,parent,row,label,name,value):
        ttk.Label(parent,text=label,style='Collection.TLabel').grid(row=row,column=0,sticky='w',padx=(0,self.s(16)),pady=self.s(8))
        entry=ttk.Entry(parent,textvariable=self.variable(name,value),style='Collection.TEntry',width=18)
        entry.grid(row=row,column=1,sticky='ew',pady=self.s(8))
        return entry

    def combo(self,parent,row,label,name,choices):
        ttk.Label(parent,text=label,style='Collection.TLabel').grid(row=row,column=0,sticky='w',padx=(0,self.s(16)),pady=self.s(8))
        combo=ttk.Combobox(parent,textvariable=self.variable(name,choices[0]),values=choices,state='readonly',width=18,style='Collection.TCombobox')
        combo.grid(row=row,column=1,sticky='ew',pady=self.s(8))
        return combo

    def _common_options(self):
        view=self.view;frame=self.right;s=self.s
        ttk.Label(frame,text='附加筛选',style='CollectionSection.TLabel').grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,s(12)))
        ttk.Checkbutton(frame,text='最低收藏',variable=view._follow_min_enabled,command=view._sync_min_bookmarks_state,style='Collection.TCheckbutton').grid(row=1,column=0,sticky='w')
        self.minimum=ttk.Entry(frame,textvariable=view._follow_min_text,style='Collection.TEntry',width=9);self.minimum.grid(row=1,column=1,sticky='ew',padx=(s(12),0))
        ttk.Checkbutton(frame,text='排除 AI 作品',variable=view._follow_exclude_ai,style='Collection.TCheckbutton').grid(row=2,column=0,columnspan=2,sticky='w',pady=(s(14),s(8)))
        ttk.Label(frame,textvariable=view._filter_summary,style='CollectionMuted.TLabel',wraplength=s(330)).grid(row=3,column=0,columnspan=2,sticky='w',pady=(0,s(10)))
        view._rule(frame,4,columnspan=2,pady=(s(6),s(16)))
        ttk.Label(frame,text='本批最多新增',style='Collection.TLabel').grid(row=5,column=0,sticky='w')
        ttk.Entry(frame,textvariable=view._follow_max_works,style='Collection.TEntry',width=9).grid(row=5,column=1,sticky='ew',padx=(s(12),0))
        ttk.Label(frame,text='最多查询请求',style='Collection.TLabel').grid(row=6,column=0,sticky='w',pady=s(12))
        ttk.Entry(frame,textvariable=view._follow_max_requests,style='Collection.TEntry',width=9).grid(row=6,column=1,sticky='ew',padx=(s(12),0))
        view._rule(frame,7,columnspan=2,pady=(s(4),s(14)))
        ttk.Label(frame,text='保存位置',style='Collection.TLabel').grid(row=8,column=0,sticky='w')
        view._make_action_button(frame,'browse_output',style='CollectionQuiet.TButton').grid(row=8,column=1,sticky='e')
        ttk.Entry(frame,textvariable=view._follow_output_display,state='readonly',style='Collection.TEntry',width=18).grid(row=9,column=0,columnspan=2,sticky='ew',pady=s(8))
        ttk.Checkbutton(frame,text='按画师分文件夹',variable=view._follow_folder_by_artist,style='Collection.TCheckbutton').grid(row=10,column=0,columnspan=2,sticky='w')
        ttk.Label(frame,text='原图 · 全部图片页 · 跳过已完成记录',style='CollectionMuted.TLabel',wraplength=s(330)).grid(row=11,column=0,columnspan=2,sticky='w',pady=(s(16),s(14)))
        ttk.Label(frame,text='保存位置与附加筛选沿用关注页设置。',style='CollectionMuted.TLabel',wraplength=s(330)).grid(row=12,column=0,columnspan=2,sticky='w',pady=(s(6),0))
        view._follow_min_enabled.trace_add('write',lambda *args:self.refresh_minimum())
        self.refresh_minimum()

    def refresh_minimum(self):
        if self.view._disposed:return
        self.minimum.state(['!disabled'] if self.view._follow_min_enabled.get() else ['disabled'])

    def select(self,key):
        self.variables['source'].set(key);self.refresh()

    def refresh(self):
        if self.view._disposed:return
        selected=self.variables['source'].get()
        for key,button in self.tabs.items():
            button.configure(style='CollectionTabActive.TButton' if key==selected else 'CollectionTab.TButton')
            self.lines[key].configure(background=self.view._palette['accent' if key==selected else 'page'])
        if selected=='intersection':self.selection.grid()
        else:self.selection.grid_remove()
        for key,card in self.cards.items():
            show=key==selected or (selected=='intersection' and key in {'ranking','search','artist'} and self.variables['include_'+key].get())
            if show:card.grid()
            else:card.grid_remove()
        self.related_entry.state(['!disabled'] if self.variables['recommendation_kind'].get()=='相似作品' else ['disabled'])

    def resize(self,event):
        wide=event.width>=self.s(870)
        if wide==self.wide:return
        self.wide=wide
        self.right.grid(row=0 if wide else 1,column=1 if wide else 0,sticky='new',pady=(0 if wide else self.s(20),0))
        self.left.grid_configure(padx=(0,self.s(32) if wide else 0))
        self.columns.columnconfigure(1,weight=0)

    def get_form(self):
        return {key:variable.get() for key,variable in self.variables.items()}

    def set_form(self,values):
        for key,variable in self.variables.items():
            if key in values:variable.set(values[key])
        self.refresh()
