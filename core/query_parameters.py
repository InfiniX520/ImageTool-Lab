"""Pure validation for user-supplied query source parameters.

The module performs no network or credential access.  It is the single place
the UI and the source pump use to turn bounded JSON into a normalized query
description before any SDK call or batch submission.
"""
from __future__ import annotations

import json
from datetime import date

from core.candidate_filter import SourceResponseError, stable_id
from core.task_contracts import ContractError, normalize_json

FILTER = 'for_ios'
RANKING_MODES = frozenset({
    'day', 'week', 'month', 'day_male', 'day_female', 'week_original',
    'week_rookie', 'day_manga', 'day_r18', 'day_male_r18', 'day_female_r18',
    'week_r18', 'week_r18g',
})
DAY_RANKING_MODES = frozenset({
    'day', 'day_male', 'day_female', 'day_manga', 'day_r18',
    'day_male_r18', 'day_female_r18',
})
SEARCH_TARGETS = frozenset({
    'partial_match_for_tags', 'exact_match_for_tags', 'title_and_caption',
})
SEARCH_SORTS = frozenset({'source_default', 'date_desc', 'date_asc', 'popular_desc'})
SOURCES = frozenset({'artist', 'ranking', 'search', 'recommendations', 'intersection'})
INTERSECTION_SOURCES = frozenset({'artist', 'ranking', 'search', 'follow'})
MAX_RANGE_DAYS = 31
MAX_ARTISTS = 200
MAX_INTERSECTION_SOURCES = 4
MIN_INTERSECTION_SOURCES = 2
MAX_PER_SOURCE_LIMIT = 5000
MAX_PARAMETERS_BYTES = 8192


def _error():
    return SourceResponseError('invalid_query_parameters')


def _load(value):
    if isinstance(value, str):
        try:
            if len(value.encode('utf-8')) > MAX_PARAMETERS_BYTES:
                raise _error()
            return json.loads(normalize_json(value, object_only=True))
        except (ContractError, UnicodeError, ValueError):
            raise _error() from None
    if isinstance(value, dict):
        try:
            # Reuse the canonical check without accepting arbitrary objects.
            text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':'))
            if len(text.encode('utf-8')) > MAX_PARAMETERS_BYTES:
                raise _error()
            return json.loads(normalize_json(text, object_only=True))
        except (ContractError, TypeError, UnicodeError, ValueError):
            raise _error() from None
    raise _error()


def _date(value):
    if not isinstance(value, str) or len(value) != 10:
        raise _error()
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise _error() from None
    if parsed.isoformat() != value:
        raise _error()
    return value


def _id(value):
    try:
        return stable_id(value)
    except SourceResponseError:
        raise _error() from None


def _text(value, maximum=500):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _error()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _error()
    return value.strip()


def _reject(value, required, optional=()):
    keys = set(value)
    if not required <= keys or keys - required - set(optional):
        raise _error()


def _sort(value, allowed=('source_default',)):
    if not isinstance(value, str) or value not in allowed:
        raise _error()
    return value


def _artist_parameters(value):
    _reject(value, {'artist_ids'})
    selected = value['artist_ids']
    if not isinstance(selected, list) or not selected:
        raise _error()
    result = []
    for item in selected:
        ident = _id(item)
        if ident not in result:
            result.append(ident)
    if not 1 <= len(result) <= MAX_ARTISTS:
        raise _error()
    return {'artist_ids': result}


def _ranking_parameters(value, sort):
    _reject(value, {'mode'}, {'date', 'start_date', 'end_date'})
    if sort != 'source_default':
        raise _error()
    mode = value['mode']
    if not isinstance(mode, str) or mode not in RANKING_MODES:
        raise _error()
    has_date = 'date' in value
    has_start = 'start_date' in value
    has_end = 'end_date' in value
    if has_date and (has_start or has_end):
        raise _error()
    if has_start != has_end:
        raise _error()
    result = {'mode': mode}
    if has_date:
        result['date'] = _date(value['date'])
    if has_start:
        start, end = _date(value['start_date']), _date(value['end_date'])
        if start > end:
            raise _error()
        if (date.fromisoformat(end) - date.fromisoformat(start)).days + 1 > MAX_RANGE_DAYS:
            raise _error()
        if mode not in DAY_RANKING_MODES:
            raise _error()
        result['start_date'] = start
        result['end_date'] = end
    return result


def _search_parameters(value, sort):
    _reject(value, {'word', 'search_target'}, {'start_date', 'end_date'})
    sort = _sort(sort, SEARCH_SORTS)
    word = _text(value['word'])
    target = value['search_target']
    if not isinstance(target, str) or target not in SEARCH_TARGETS:
        raise _error()
    result = {'word': word, 'search_target': target,
              'requested_sort': sort}
    start = end = None
    if 'start_date' in value:
        start = _date(value['start_date'])
    if 'end_date' in value:
        end = _date(value['end_date'])
    if (start is None) != (end is None):
        raise _error()
    if start is not None and start > end:
        raise _error()
    if start is not None:
        result['start_date'] = start
        result['end_date'] = end
    return result


def _recommendation_parameters(value, sort):
    if sort != 'source_default':
        raise _error()
    if not isinstance(value, dict) or 'kind' not in value:
        raise _error()
    kind = value['kind']
    if kind == 'personalized':
        _reject(value, {'kind'}, {'content_type'})
        content_type = value.get('content_type', 'illust')
        if content_type not in {'illust', 'manga'}:
            raise _error()
        return {'kind': 'personalized', 'content_type': content_type}
    if kind == 'related':
        _reject(value, {'kind', 'illust_id'})
        return {'kind': 'related', 'illust_id': _id(value['illust_id'])}
    raise _error()


def _normalize_source(source, value, requested_sort='source_default'):
    if source == 'artist':
        return _artist_parameters(value), _sort(requested_sort)
    if source == 'ranking':
        return _ranking_parameters(value, requested_sort), _sort(requested_sort)
    if source == 'search':
        normalized = _search_parameters(value, requested_sort)
        # requested_sort is part of the search runtime description, not the
        # API parameter object consumed by the SDK.
        sort = normalized.pop('requested_sort')
        return normalized, sort
    if source == 'recommendations':
        return _recommendation_parameters(value, requested_sort), _sort(requested_sort)
    if source == 'follow':
        if value != {} or requested_sort != 'source_default':
            raise _error()
        return {}, 'source_default'
    raise _error()


def validate_query_parameters(source, parameters_json, requested_sort='source_default'):
    """Return a normalized dict or raise ``SourceResponseError`` safely."""
    if not isinstance(source, str) or source not in SOURCES:
        raise SourceResponseError('unsupported_query_source')
    if not isinstance(requested_sort, str):
        raise _error()
    value = _load(parameters_json)
    if source == 'intersection':
        _sort(requested_sort)
        if set(value) - {'sources', 'per_source_limit'} or 'sources' not in value:
            raise _error()
        sources = value['sources']
        if (
            not isinstance(sources, list)
            or not MIN_INTERSECTION_SOURCES <= len(sources) <= MAX_INTERSECTION_SOURCES
        ):
            raise _error()
        per_source_limit = value.get('per_source_limit', 200)
        if type(per_source_limit) is not int or not 1 <= per_source_limit <= MAX_PER_SOURCE_LIMIT:
            raise _error()
        normalized_sources = []
        seen = set()
        for item in sources:
            if not isinstance(item, dict) or set(item) - {'source', 'parameters', 'requested_sort'}:
                raise _error()
            if 'source' not in item or 'parameters' not in item:
                raise _error()
            sub_source = item['source']
            if not isinstance(sub_source, str) or sub_source not in INTERSECTION_SOURCES:
                raise _error()
            sub_sort = item.get('requested_sort', 'source_default')
            if not isinstance(sub_sort, str):
                raise _error()
            sub_params, sub_sort = _normalize_source(
                sub_source, _load(item['parameters']), sub_sort
            )
            descriptor = {'source': sub_source, 'parameters': sub_params,
                          'requested_sort': sub_sort}
            key = json.dumps(descriptor, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'))
            if key in seen:
                raise _error()
            seen.add(key)
            normalized_sources.append(descriptor)
        return {'sources': normalized_sources, 'per_source_limit': per_source_limit}
    if source == 'search':
        params, _sort_value = _normalize_source(source, value, requested_sort)
        return params
    if source in {'artist', 'ranking', 'recommendations'}:
        params, _sort_value = _normalize_source(source, value, requested_sort)
        return params
    raise _error()


__all__ = [
    'DAY_RANKING_MODES', 'FILTER', 'MAX_RANGE_DAYS', 'RANKING_MODES',
    'SEARCH_SORTS', 'SEARCH_TARGETS', 'validate_query_parameters',
]
