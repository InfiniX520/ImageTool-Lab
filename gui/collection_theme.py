"""Quiet blue/white native styles. No image assets, downloads or animation loops."""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont, ttk

PALETTE = {
    'page':'#FFFFFF', 'paper':'#FFFFFF', 'paper_soft':'#F4F7F9',
    'ink':'#243340', 'muted':'#647783', 'quiet':'#8999A3',
    'line':'#DBE5EB', 'line_soft':'#E9EFF3',
    'accent':'#287EAE', 'accent_hover':'#226F9B', 'accent_active':'#1D6088',
    'accent_wash':'#EFF6FA', 'hero':'#F5F9FC', 'shadow':'#E8F0F5',
    'success':'#39796D', 'success_bg':'#EDF6F1',
    'warning':'#956B27', 'warning_bg':'#FCF6E9',
    'danger':'#AE4949', 'danger_bg':'#FBEEEE', 'disabled':'#B3C0C9',
}
UI_FONT_FAMILIES=('Microsoft YaHei UI','Microsoft YaHei','Segoe UI','Noto Sans CJK SC','PingFang SC')
MONO_FONT_FAMILIES=('Consolas','Cascadia Mono','Courier New')
TITLE_POINTS=23
SECTION_POINTS=11
BODY_POINTS=10
SMALL_POINTS=9
MONO_POINTS=9
TWO_COLUMN_LOGICAL_PX=980

def _families(root):
    return set(tkfont.families(root))

def choose_ui_family(root):
    names=_families(root)
    return next((name for name in UI_FONT_FAMILIES if name in names),'TkDefaultFont')

def choose_mono_family(root):
    names=_families(root)
    return next((name for name in MONO_FONT_FAMILIES if name in names),choose_ui_family(root))

def ui_scale(root):
    try:return max(.5,float(root.tk.call('tk','scaling')))
    except (tk.TclError,ValueError):return 96/72

def scale_px(root,logical_px):
    return max(1,round(float(logical_px)*ui_scale(root)*.75))

def collection_fonts(root):
    ui,mono=choose_ui_family(root),choose_mono_family(root)
    return {'title':(ui,TITLE_POINTS),'brand':('Segoe UI',24),
        'hero':(ui,17),'number':('Segoe UI',25),
        'section':(ui,SECTION_POINTS,'bold'),
        'body':(ui,BODY_POINTS),'body_bold':(ui,BODY_POINTS,'bold'),
        'small':(ui,SMALL_POINTS),'small_bold':(ui,SMALL_POINTS,'bold'),
        'mono':(mono,MONO_POINTS),'mono_bold':(mono,MONO_POINTS,'bold'),
        'ui_family':ui,'mono_family':mono}

def two_column_breakpoint(root):
    return scale_px(root,TWO_COLUMN_LOGICAL_PX)

def apply_collection_styles(root):
    style=ttk.Style(root)
    if 'clam' in style.theme_names():style.theme_use('clam')
    c,f=PALETTE,collection_fonts(root)
    s=lambda x:scale_px(root,x)
    for prefix,color in (('Page','page'),('Header','page'),('Paper','paper'),('Bar','paper_soft'),('Hero','hero')):
        style.configure(f'Collection{prefix}.TFrame',background=c[color])
    labels={
        '':('body','ink','page'), 'Title':('title','ink','page'),
        'Brand':('brand','ink','page'), 'Section':('section','ink','page'),
        'Muted':('small','muted','page'), 'Subtitle':('body','muted','page'),
        'Eyebrow':('small','accent','page'), 'Hint':('small','quiet','page'),
        'Number':('number','ink','page'), 'Link':('small','accent','page'),
        'HeroTitle':('hero','ink','hero'), 'HeroBody':('body','muted','hero'),
        'HeroSmall':('small','muted','hero'), 'HeroAccent':('small','accent','hero'),
        'StatusMuted':('small','muted','page'), 'StatusSuccess':('small','success','page'),
        'StatusWarning':('small','warning','page'), 'StatusDanger':('small','danger','page'),
        'StatusAccent':('small','accent','page'),
    }
    for prefix,(font,fg,bg) in labels.items():
        style.configure(f'Collection{prefix}.TLabel',font=f[font],foreground=c[fg],background=c[bg],padding=0)
    # A padding/label layout removes the platform's raised 3D button border.
    # Keyboard focus remains a thin outline; hover never changes geometry.
    layout=[('Button.border',{'sticky':'nswe','children':[
        ('Button.focus',{'sticky':'nswe','children':[
            ('Button.padding',{'sticky':'nswe','children':[('Button.label',{'sticky':'nswe'})]})]})]})]
    specs={
        'Primary':('accent','#FFFFFF','accent',(18,9),f['body']),
        'Secondary':('page',c['ink'],'line',(15,9),f['body']),
        'Quiet':('page',c['accent'],'page',(2,4),f['small']),
        'Nav':('page',c['muted'],'page',(4,20),f['body']),
        'NavActive':('page',c['accent'],'page',(4,20),f['body']),
        'Tab':('page',c['muted'],'page',(0,12),f['body']),
        'TabActive':('page',c['accent'],'page',(0,12),f['body']),
    }
    for prefix,(bg,fg,border,pad,font) in specs.items():
        name=f'Collection{prefix}.TButton'
        style.layout(name,layout)
        style.configure(name,background=c[bg],foreground=fg,bordercolor=c[border],
            lightcolor=c[bg],darkcolor=c[bg],borderwidth=1,relief='flat',
            focusthickness=1,focuscolor=c[bg],anchor='center',font=font,
            padding=(s(pad[0]),s(pad[1])),width=18 if prefix=="Primary" else 0)
        primary=prefix=='Primary'
        style.map(name,background=[('disabled',c['paper_soft'] if prefix in ('Primary','Secondary') else c[bg]),('pressed',c['accent_active'] if primary else c['accent_wash']),('active',c['accent_hover'] if primary else c[bg])],
            foreground=[('disabled',c['quiet']),('active','#FFFFFF' if primary else c['accent'])],
            focuscolor=[('focus',c['line'] if primary else c['accent'])],
            bordercolor=[('focus',c['accent']),('active',c['accent'] if prefix in ('Primary','Secondary') else c[border])])
    for name in ('Collection.TCheckbutton','Collection.TRadiobutton'):
        style.configure(name,background=c['page'],foreground=c['ink'],font=f['body'],
            padding=(0,s(4)),indicatorsize=s(15),indicatormargin=(0,s(3),s(6),s(3)),upperbordercolor=c['line'],lowerbordercolor=c['line'],indicatorbackground=c['paper'],indicatorforeground=c['accent'])
        style.map(name,background=[('active',c['page'])],
            indicatorbackground=[('selected',c['accent'])],indicatorforeground=[('selected','#FFFFFF')],
            foreground=[('disabled',c['quiet'])])
    style.layout('CollectionScope.TRadiobutton',[('Radiobutton.padding',{'sticky':'nswe','children':[
        ('Radiobutton.focus',{'sticky':'nswe','children':[('Radiobutton.label',{'sticky':'we'})]})]})])
    style.configure('CollectionScope.TRadiobutton',background=c['page'],foreground=c['muted'],
        font=f['body'],padding=(0,s(12)),focuscolor=c['accent'],focusthickness=1)
    style.map('CollectionScope.TRadiobutton',foreground=[('selected',c['accent'])],background=[('active',c['page'])])
    style.configure('Collection.TEntry',fieldbackground=c['paper'],foreground=c['ink'],
        bordercolor=c['line'],lightcolor=c['line'],darkcolor=c['line'],
        insertcolor=c['ink'],padding=(s(10),s(8)),font=f['body'])
    style.map('Collection.TEntry',bordercolor=[('focus',c['accent'])],
        lightcolor=[('focus',c['accent'])],darkcolor=[('focus',c['accent'])],
        fieldbackground=[('disabled',c['paper_soft'])],foreground=[('disabled',c['quiet'])])
    style.configure('Collection.TCombobox',background=c['paper'],fieldbackground=c['paper'],
        foreground=c['ink'],arrowcolor=c['accent'],bordercolor=c['line'],
        lightcolor=c['line'],darkcolor=c['line'],padding=(s(8),s(7)),font=f['body'])
    style.map('Collection.TCombobox',fieldbackground=[('readonly',c['paper'])],bordercolor=[('focus',c['accent'])])
    style.layout('Collection.Treeview',[('Treeview.treearea',{'sticky':'nswe'})])
    style.configure('Collection.Treeview',background=c['paper'],fieldbackground=c['paper'],
        foreground=c['ink'],borderwidth=0,rowheight=s(48),font=f['body'])
    style.map('Collection.Treeview',background=[('selected',c['accent_wash'])],foreground=[('selected',c['accent'])])
    style.layout('Collection.Treeview.Heading',[('Treeheading.padding',{'sticky':'nswe','children':[
        ('Treeheading.text',{'sticky':'we'})]})])
    style.configure('Collection.Treeview.Heading',background=c['page'],foreground=c['muted'],
        relief='flat',padding=(s(12),s(10)),font=f['small'])
    style.configure('Collection.TSeparator',background=c['line'],borderwidth=0)
    for orient in ('Vertical','Horizontal'):
        name=f'Collection.{orient}.TScrollbar'
        style.configure(name,background=c['line'],troughcolor=c['page'],bordercolor=c['page'],
            arrowcolor=c['muted'],lightcolor=c['line'],darkcolor=c['line'],gripcount=0,arrowsize=s(10))
        style.map(name,background=[('active',c['disabled'])])
    return {'colors':c,'fonts':f,'breakpoint':two_column_breakpoint(root)}
