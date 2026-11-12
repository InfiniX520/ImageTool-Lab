"""Translate desktop source forms before authorization; no network or Tk imports."""
from __future__ import annotations

import re
from dataclasses import replace

from core.collection_settings import CollectionSettings, SettingsError, _number
from core.task_contracts import QuerySpec, canonical_json

RANKINGS = {'日榜':'day','周榜':'week','月榜':'month','男性向日榜':'day_male',
            '女性向日榜':'day_female','原创周榜':'week_original','新人周榜':'week_rookie',
            '漫画日榜':'day_manga','R-18 日榜':'day_r18','R-18 周榜':'week_r18'}
SEARCH_TARGETS = {'标签部分匹配':'partial_match_for_tags','标签完全匹配':'exact_match_for_tags',
                  '标题与简介':'title_and_caption'}
SORTS = {'最新发布':'date_desc','最早发布':'date_asc','热门排序（会员）':'popular_desc'}
SOURCES = {'artist':'指定画师','search':'搜索','ranking':'排行榜','recommendations':'推荐','intersection':'组合筛选','follow':'关注新作'}


def filter_description(form):
    parts = []
    if form.get('min_bookmarks_enabled'):
        value = str(form.get('min_bookmarks','')).strip()
        parts.append('收藏 ≥ '+value if value.isascii() and value.isdecimal() else '最低收藏待填写')
    if form.get('exclude_ai'):
        parts.append('排除 AI · 标记未知待核验')
    return '同时满足：'+'；'.join(parts) if parts else '不限制收藏数与 AI 标记'


def parse_artist_ids(text):
    if not isinstance(text,str) or len(text)>10000:
        raise SettingsError('artist_ids')
    values=[]
    for item in re.split(r'[,，;；\s]+',text.strip()):
        if not item:continue
        match=re.fullmatch(r'(?:https://www\.pixiv\.net/(?:[a-z]{2}/)?users/)?([0-9]{1,32})/?',item)
        if match is None:raise SettingsError('artist_ids')
        if match[1] not in values:values.append(match[1])
    if not 1<=len(values)<=200:raise SettingsError('artist_ids')
    return values


def source_query(form):
    """Return a fully validated immutable query, including disabled-filter semantics."""
    from core.query_parameters import validate_query_parameters
    source=form.get('source','search')
    if source not in SOURCES or source=='follow':raise SettingsError('query_source')
    def unit(kind):
        sort='source_default'
        if kind=='ranking':
            mode=RANKINGS.get(form.get('ranking_mode','日榜'))
            if mode is None:raise SettingsError('ranking_mode')
            params={'mode':mode}
            for field in ('date','start_date','end_date'):
                value=str(form.get('ranking_'+field,'')).strip()
                if value:params[field]=value
        elif kind=='search':
            target=SEARCH_TARGETS.get(form.get('search_target','标签部分匹配'))
            sort=SORTS.get(form.get('search_sort','最新发布'))
            if target is None or sort is None:raise SettingsError('search_options')
            word=str(form.get('search_word','')).strip()
            if not word:raise SettingsError('search_word')
            params={'word':word,'search_target':target}
            for field in ('start_date','end_date'):
                value=str(form.get('search_'+field,'')).strip()
                if value:params[field]=value
        elif kind=='artist':params={'artist_ids':parse_artist_ids(form.get('source_artist_ids',''))}
        elif kind=='recommendations':
            choice=form.get('recommendation_kind','个性化推荐')
            if choice not in {'个性化推荐','相似作品'}:raise SettingsError('recommendation_kind')
            params={'kind':'related' if choice=='相似作品' else 'personalized'}
            if params['kind']=='related':
                raw=str(form.get('related_illust_id','')).strip()
                match=re.fullmatch(r'(?:https://www\.pixiv\.net/(?:[a-z]{2}/)?artworks/)?([0-9]{1,32})/?',raw)
                if match is None:raise SettingsError('related_illust_id')
                params['illust_id']=match[1]
        elif kind=='follow':params={}
        else:raise SettingsError('query_source')
        return {'source':kind,'parameters':params,'requested_sort':sort}
    if source=='intersection':
        selected=[unit(kind) for kind in ('follow','ranking','search','artist') if form.get('include_'+kind) is True]
        if len(selected)<2:raise SettingsError('intersection_sources')
        params={'sources':selected,'per_source_limit':_number(form.get('per_source_limit','200'),'per_source_limit',maximum=5000)}
        sort='source_default'
    else:
        item=unit(source);params,sort=item['parameters'],item['requested_sort']
    normalized=validate_query_parameters(source,canonical_json(params),sort)
    settings=CollectionSettings.from_form(form)
    filters=settings.request('draft','offline').query.filters
    return QuerySpec(source=source,mode='query',parameters_json=canonical_json(normalized),requested_sort=sort,filters=filters)


def source_request(form,settings,request_id,account_ref):
    preferences=CollectionSettings.from_form(form,previous=settings)
    return replace(preferences.request(request_id,account_ref),query=source_query(form))


def source_description(query):
    import json
    source=query.get('source','follow')
    try:params=json.loads(query.get('parameters_json','{}'))
    except (TypeError,ValueError):params={}
    parts=[SOURCES.get(source,source)]
    if source=='search':
        parts.append(str(params.get('word',''))[:60])
        parts.append({v:k for k,v in SORTS.items()}.get(query.get('requested_sort'),'默认排序'))
    elif source=='ranking':
        parts.append({v:k for k,v in RANKINGS.items()}.get(params.get('mode'),'榜单'))
        parts.append(params.get('date') or (f"{params.get('start_date')} — {params.get('end_date')}" if params.get('start_date') else '最近已发布'))
    elif source=='artist':parts.append(f"{len(params.get('artist_ids',[]))} 位画师")
    elif source=='recommendations':parts.append('相似作品' if params.get('kind')=='related' else '个性化')
    elif source=='intersection':
        parts.append(' ∩ '.join(SOURCES.get(item.get('source'),'来源') for item in params.get('sources',[])))
        parts.append(f"每来源最多 {params.get('per_source_limit',200)} 个候选")
    filters=query.get('filters') or {}
    if filters.get('min_bookmarks') is not None:parts.append(f"收藏 ≥ {filters['min_bookmarks']}")
    if filters.get('exclude_ai'):parts.append('排除 AI')
    return ' · '.join(parts)
