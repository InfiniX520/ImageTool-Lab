"""Bounded single-call ranking/search/artist/recommendation queries.

The adapter performs
at most one SDK call per ``query_page`` and never retries, falls back, logs in,
schedules work, or forwards an unvalidated ``next_url`` as request kwargs.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, urlsplit

from core.candidate_filter import SourceResponseError, normalize_artwork, stable_id
from core.follow_contracts import ArtworkPage
from core.pixiv_follow_source import PixivFollowSource
from core.task_contracts import ContractError, canonical_json, normalize_json

_FILTER = 'for_ios'
_RANKING_MODES = frozenset({
    'day', 'week', 'month', 'day_male', 'day_female', 'week_original',
    'week_rookie', 'day_manga', 'day_r18', 'day_male_r18', 'day_female_r18',
    'week_r18', 'week_r18g',
})
_SEARCH_TARGETS = frozenset({
    'partial_match_for_tags', 'exact_match_for_tags', 'title_and_caption',
})
_SEARCH_SORTS = frozenset({'source_default', 'date_desc', 'date_asc', 'popular_desc'})
_SORT_MAP = {
    'source_default': 'date_desc',
    'date_desc': 'date_desc',
    'date_asc': 'date_asc',
    'popular_desc': 'popular_desc',
}
_ACTUAL_SOURCES = frozenset({'artist', 'ranking', 'search', 'personalized', 'related'})
_ACTUAL_SORTS = frozenset({'source_default', 'ranking', 'date_desc', 'date_asc', 'popular_desc'})
_CURSOR_VERSION = 1
_MAX_PAGE = 2000
_MAX_URL = 64 * 1024
_MAX_PARAMETERS = 8192
_MAX_CURSOR = 64 * 1024
_MAX_CURSOR_PARAMS = 48 * 1024
_MAX_PROVENANCE = 64 * 1024
_MAX_WORD = 500
_MAX_HISTORY = 64
_HISTORY_RE = re.compile(r'[0-9a-f]{16}\Z')
_FINGERPRINT_RE = re.compile(r'[0-9a-f]{64}\Z')
_CURSOR_KEYS = frozenset({'v', 'source', 'fingerprint', 'params', 'history', 'check'})
_DATE_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}\Z')
_RANKING_ENDPOINT = '/v1/illust/ranking'
_SEARCH_ENDPOINT = '/v1/search/illust'


def _normalize_indexed_arrays(query, names):
    """Accept Pixiv's indexed lists, with no sparse or ambiguous encodings."""
    result = dict(query)
    for name in names:
        indexed = {}
        for key in tuple(result):
            match = re.fullmatch(re.escape(name) + r'\[(0|[1-9][0-9]{0,2})\]', key)
            if match is None:
                continue
            index = int(match[1])
            values = result.pop(key)
            if len(values) != 1:
                raise SourceResponseError('duplicate_pagination_field')
            indexed[index] = values[0]
        if indexed:
            if name + '[]' in result or sorted(indexed) != list(range(len(indexed))):
                raise SourceResponseError('invalid_next_page')
            result[name + '[]'] = [indexed[i] for i in range(len(indexed))]
    return result


def _safe_text(value, maximum, code):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SourceResponseError(code)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SourceResponseError(code)
    return value


def _iso_date(value):
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == value else None


def _required_iso(value, code):
    parsed = _iso_date(value)
    if parsed is None:
        raise SourceResponseError(code)
    return parsed


def _offset(value, code='invalid_next_page'):
    if type(value) is int and 0 <= value <= 10_000_000:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 8:
        number = int(value)
        if number <= 10_000_000:
            return number
    raise SourceResponseError(code)


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()[:16]


def _stable_id(value, code):
    try:
        return stable_id(value)
    except SourceResponseError:
        raise SourceResponseError(code) from None


def _pagination_id(value, *, allow_end_marker=False):
    # The recommendation endpoint uses -1 when this bookmark stream is exhausted.
    if allow_end_marker and (value == '-1' or type(value) is int and value == -1):
        return '-1'
    if type(value) is int and 0 <= value <= 10**18:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 20:
        number = int(value)
        if number <= 10**18:
            return str(number)
    raise SourceResponseError('invalid_next_page')


def _stable_list(values):
    if not isinstance(values, list) or not values or len(values) > 1000:
        raise SourceResponseError('invalid_next_page')
    result = []
    for value in values:
        item = _stable_id(value, 'invalid_next_page')
        if item not in result:
            result.append(item)
    try:
        if len(canonical_json(result).encode('utf-8')) > _MAX_CURSOR_PARAMS:
            raise SourceResponseError('invalid_next_page')
    except ContractError:
        raise SourceResponseError('invalid_next_page') from None
    return sorted(result)


@dataclass(frozen=True, slots=True)
class QueryPage:
    page: ArtworkPage
    actual_source: str
    actual_sort: str
    actual_date: str | None = None
    provenance_json: str = '{}'

    def __post_init__(self):
        if not isinstance(self.page, ArtworkPage):
            raise SourceResponseError('invalid_query_page')
        if not isinstance(self.actual_source, str) or self.actual_source not in _ACTUAL_SOURCES:
            raise SourceResponseError('invalid_query_page')
        if not isinstance(self.actual_sort, str) or self.actual_sort not in _ACTUAL_SORTS:
            raise SourceResponseError('invalid_query_page')
        if self.actual_date is not None and _iso_date(self.actual_date) is None:
            raise SourceResponseError('invalid_query_page')
        try:
            provenance = normalize_json(self.provenance_json, object_only=True)
        except ContractError:
            raise SourceResponseError('invalid_query_page') from None
        if len(provenance.encode('utf-8')) > _MAX_PROVENANCE:
            raise SourceResponseError('invalid_query_page')
        object.__setattr__(self, 'provenance_json', provenance)


@dataclass(frozen=True, slots=True)
class _QueryPlan:
    actual_source: str
    actual_sort: str
    endpoint: str
    method: str
    identity_json: str
    base_params: tuple[tuple[str, object], ...]
    requested_date: str | None = None


class PixivQuerySource(PixivFollowSource):
    """One page of ranking/search, reusing the parent client and transport checks."""

    def query_page(
        self,
        source: str,
        parameters_json: str = '{}',
        cursor_json: str = 'null',
        *,
        requested_sort: str = 'source_default',
    ) -> QueryPage:
        if not isinstance(source, str):
            raise SourceResponseError('unsupported_query_source')
        if source not in {'ranking', 'search', 'artist', 'recommendations'}:
            raise SourceResponseError('unsupported_query_source')
        plan = self._plan(source, parameters_json, requested_sort)
        fingerprint = hashlib.sha256(plan.identity_json.encode('utf-8')).hexdigest()
        current_params, history = self._parse_cursor(cursor_json, plan, fingerprint)
        kwargs = dict(plan.base_params)
        kwargs['offset'] = current_params.get('offset', 0)
        if plan.actual_source == 'ranking' and plan.requested_date is None:
            if 'date' in current_params:
                kwargs['date'] = current_params['date']
        if plan.actual_source == 'personalized':
            for key in ('max_bookmark_id_for_recommend', 'min_bookmark_id_for_recent_illust',
                        'include_ranking_illusts', 'include_privacy_policy'):
                if key in current_params:
                    kwargs[key] = current_params[key]
            if 'viewed' in current_params:
                kwargs['viewed'] = list(current_params['viewed'])
        elif plan.actual_source == 'related':
            if 'viewed' in current_params:
                kwargs['viewed'] = list(current_params['viewed'])

        result = self._call(plan.method, **kwargs)
        values = result.get('illusts')
        if not isinstance(values, list):
            raise SourceResponseError('artwork_list_missing')
        if len(values) > _MAX_PAGE:
            raise SourceResponseError('artwork_page_too_large')
        if 'next_url' not in result:
            raise SourceResponseError('pagination_metadata_missing')
        artworks = tuple(normalize_artwork(value) for value in values)
        if plan.actual_source == 'artist':
            base = dict(plan.base_params)
            if any(meta.artist_id and meta.artist_id != base['user_id'] for meta in artworks):
                raise SourceResponseError('artist_page_identity_mismatch')
            if any(meta.kind not in {base['type'], 'unknown'} for meta in artworks):
                raise SourceResponseError('artwork_partition_mismatch')
        response_date = _iso_date(result.get('date')) if plan.actual_source == 'ranking' else None
        if plan.actual_source == 'ranking' and result.get('date') is not None and response_date is None:
            raise SourceResponseError('invalid_ranking_date')
        next_params, actual_date = self._next_page(
            result['next_url'], plan, current_params, history, response_date
        )
        if (
            plan.actual_source == 'ranking'
            and actual_date is None
            and plan.requested_date is None
            and 'date' in current_params
        ):
            actual_date = current_params['date']
        if next_params is None:
            cursor = 'null'
            exhausted = True
        else:
            cursor = self._new_cursor(plan, current_params, next_params, history)
            exhausted = False
        provenance = self._provenance(plan, kwargs, actual_date)
        page = QueryPage(
            ArtworkPage(artworks, cursor, exhausted),
            plan.actual_source,
            plan.actual_sort,
            actual_date,
            provenance,
        )
        return page

    # -- input plan -----------------------------------------------------

    @staticmethod
    def _load_parameters(parameters_json):
        if not isinstance(parameters_json, str):
            raise SourceResponseError('invalid_query_parameters')
        try:
            if len(parameters_json.encode('utf-8')) > _MAX_PARAMETERS:
                raise SourceResponseError('invalid_query_parameters')
            normalized = normalize_json(parameters_json, object_only=True)
            value = json.loads(normalized)
        except (ContractError, UnicodeError, ValueError):
            raise SourceResponseError('invalid_query_parameters') from None
        if type(value) is not dict:
            raise SourceResponseError('invalid_query_parameters')
        return value

    @staticmethod
    def _reject_keys(value, required, optional=()):
        keys = set(value)
        if not required <= keys or keys - required - set(optional):
            raise SourceResponseError('invalid_query_parameters')

    def _plan(self, source, parameters_json, requested_sort):
        if not isinstance(requested_sort, str):
            raise SourceResponseError('invalid_requested_sort')
        if source == 'ranking' and requested_sort != 'source_default':
            raise SourceResponseError('unsupported_requested_sort')
        if source == 'search' and requested_sort not in _SEARCH_SORTS:
            raise SourceResponseError('invalid_requested_sort')
        raw = self._load_parameters(parameters_json)
        if source == 'ranking':
            self._reject_keys(raw, {'mode'}, {'date'})
            mode = raw['mode']
            if not isinstance(mode, str) or mode not in _RANKING_MODES:
                raise SourceResponseError('invalid_query_parameters')
            requested_date = None
            if 'date' in raw:
                requested_date = _required_iso(raw['date'], 'invalid_query_parameters')
            base = [('mode', mode), ('filter', _FILTER)]
            if requested_date is not None:
                base.append(('date', requested_date))
            identity = {
                'source': 'ranking',
                'mode': mode,
                'date': requested_date,
                'requested_sort': 'source_default',
            }
            return _QueryPlan(
                'ranking', 'ranking', _RANKING_ENDPOINT, 'illust_ranking',
                canonical_json(identity), tuple(base), requested_date,
            )

        if source == 'artist':
            if requested_sort != 'source_default':
                raise SourceResponseError('unsupported_requested_sort')
            self._reject_keys(raw, {'artist_id'}, {'partition'})
            artist_id = _stable_id(raw['artist_id'], 'invalid_query_parameters')
            partition = raw.get('partition', 'illust')
            if partition not in {'illust', 'manga', 'ugoira'}:
                raise SourceResponseError('invalid_query_parameters')
            identity = {
                'source': 'artist',
                'artist_id': artist_id,
                'partition': partition,
                'requested_sort': 'source_default',
            }
            return _QueryPlan(
                'artist', 'source_default', '/v1/user/illusts', 'user_illusts',
                canonical_json(identity),
                (('user_id', artist_id), ('type', partition), ('filter', _FILTER)),
            )

        if source == 'recommendations':
            if requested_sort != 'source_default':
                raise SourceResponseError('unsupported_requested_sort')
            kind = raw.get('kind')
            if kind not in {'personalized', 'related'}:
                raise SourceResponseError('invalid_query_parameters')
            if kind == 'personalized':
                self._reject_keys(raw, {'kind'}, {'content_type'})
                content_type = raw.get('content_type', 'illust')
                if content_type not in {'illust', 'manga'}:
                    raise SourceResponseError('invalid_query_parameters')
                identity = {
                    'source': 'personalized',
                    'kind': 'personalized',
                    'content_type': content_type,
                }
                return _QueryPlan(
                    'personalized', 'source_default', '/v1/illust/recommended',
                    'illust_recommended', canonical_json(identity),
                    (('content_type', content_type), ('filter', _FILTER)),
                )
            self._reject_keys(raw, {'kind', 'illust_id'})
            illust_id = _stable_id(raw['illust_id'], 'invalid_query_parameters')
            identity = {
                'source': 'related',
                'kind': 'related',
                'illust_id': illust_id,
            }
            return _QueryPlan(
                'related', 'source_default', '/v2/illust/related',
                'illust_related', canonical_json(identity),
                (('illust_id', illust_id), ('filter', _FILTER)),
            )

        self._reject_keys(raw, {'word', 'search_target'}, {'start_date', 'end_date'})
        word = _safe_text(raw['word'], _MAX_WORD, 'invalid_query_parameters')
        target = raw['search_target']
        if not isinstance(target, str) or target not in _SEARCH_TARGETS:
            raise SourceResponseError('invalid_query_parameters')
        start_date = end_date = None
        if 'start_date' in raw:
            start_date = _required_iso(raw['start_date'], 'invalid_query_parameters')
        if 'end_date' in raw:
            end_date = _required_iso(raw['end_date'], 'invalid_query_parameters')
        if start_date is not None and end_date is not None and start_date > end_date:
            raise SourceResponseError('invalid_query_parameters')
        actual_sort = _SORT_MAP[requested_sort]
        base = [
            ('word', word), ('search_target', target),
            ('sort', actual_sort), ('filter', _FILTER),
        ]
        if start_date is not None:
            base.append(('start_date', start_date))
        if end_date is not None:
            base.append(('end_date', end_date))
        identity = {
            'source': 'search',
            'word': word,
            'search_target': target,
            'start_date': start_date,
            'end_date': end_date,
            'requested_sort': requested_sort,
            'actual_sort': actual_sort,
        }
        return _QueryPlan(
            'search', actual_sort, _SEARCH_ENDPOINT, 'search_illust',
            canonical_json(identity), tuple(base),
        )

    # -- cursor ---------------------------------------------------------

    def _parse_cursor(self, cursor_json, plan, fingerprint):
        if not isinstance(cursor_json, str):
            raise SourceResponseError('invalid_source_cursor')
        try:
            if len(cursor_json.encode('utf-8')) > _MAX_CURSOR:
                raise SourceResponseError('invalid_source_cursor')
            normalized = normalize_json(cursor_json)
            value = json.loads(normalized)
        except (ContractError, UnicodeError, ValueError):
            raise SourceResponseError('invalid_source_cursor') from None
        if value is None:
            return {}, []
        if type(value) is not dict or set(value) != _CURSOR_KEYS:
            raise SourceResponseError('invalid_source_cursor')
        if value['v'] != _CURSOR_VERSION or type(value['v']) is not int:
            raise SourceResponseError('invalid_source_cursor')
        if value['source'] != plan.actual_source:
            raise SourceResponseError('cursor_query_mismatch')
        if (
            not isinstance(value['fingerprint'], str)
            or _FINGERPRINT_RE.fullmatch(value['fingerprint']) is None
            or value['fingerprint'] != fingerprint
        ):
            raise SourceResponseError('cursor_query_mismatch')
        check_payload = {key: value[key] for key in _CURSOR_KEYS if key != 'check'}
        expected_check = hashlib.sha256(
            canonical_json(check_payload).encode('utf-8')
        ).hexdigest()
        if value['check'] != expected_check:
            raise SourceResponseError('invalid_source_cursor')
        params = self._cursor_params(value['params'], plan)
        history = self._cursor_history(value['history'])
        return params, history

    @staticmethod
    def _cursor_params(value, plan):
        if type(value) is not dict or not value:
            raise SourceResponseError('invalid_source_cursor')
        if plan.actual_source == 'ranking':
            allowed = {'offset', 'date'}
        elif plan.actual_source == 'personalized':
            allowed = {'offset', 'max_bookmark_id_for_recommend',
                       'min_bookmark_id_for_recent_illust', 'viewed',
                       'include_ranking_illusts', 'include_privacy_policy'}
        elif plan.actual_source == 'related':
            allowed = {'offset', 'viewed'}
        else:
            allowed = {'offset'}
        if set(value) - allowed:
            raise SourceResponseError('invalid_source_cursor')
        result = {}
        if 'offset' in value:
            result['offset'] = _offset(value['offset'], 'invalid_source_cursor')
        if plan.actual_source in {'artist', 'ranking', 'search', 'related'} and 'offset' not in result:
            raise SourceResponseError('invalid_source_cursor')
        if 'date' in value:
            if plan.actual_source != 'ranking':
                raise SourceResponseError('invalid_source_cursor')
            cursor_date = _required_iso(value['date'], 'invalid_source_cursor')
            if plan.requested_date is not None and cursor_date != plan.requested_date:
                raise SourceResponseError('cursor_query_mismatch')
            result['date'] = cursor_date
        for key in ('max_bookmark_id_for_recommend', 'min_bookmark_id_for_recent_illust'):
            if key in value:
                if plan.actual_source != 'personalized':
                    raise SourceResponseError('invalid_source_cursor')
                try:
                    result[key] = _pagination_id(value[key], allow_end_marker=key=='max_bookmark_id_for_recommend')
                except SourceResponseError:
                    raise SourceResponseError('invalid_source_cursor') from None
        for key in ('include_ranking_illusts', 'include_privacy_policy'):
            if key in value:
                if plan.actual_source != 'personalized' or value[key] not in {'true', 'false'}:
                    raise SourceResponseError('invalid_source_cursor')
                result[key] = value[key]
        if 'viewed' in value:
            if plan.actual_source not in {'personalized', 'related'}:
                raise SourceResponseError('invalid_source_cursor')
            try:
                result['viewed'] = _stable_list(value['viewed'])
            except SourceResponseError:
                raise SourceResponseError('invalid_source_cursor') from None
        if not result:
            raise SourceResponseError('invalid_source_cursor')
        try:
            if len(canonical_json(result).encode('utf-8')) > _MAX_CURSOR_PARAMS:
                raise SourceResponseError('invalid_source_cursor')
        except ContractError:
            raise SourceResponseError('invalid_source_cursor') from None
        return result

    @staticmethod
    def _cursor_history(value):
        if not isinstance(value, list) or not value or len(value) > _MAX_HISTORY:
            raise SourceResponseError('invalid_source_cursor')
        result = []
        for item in value:
            if (
                not isinstance(item, str)
                or _HISTORY_RE.fullmatch(item) is None
                or item in result
            ):
                raise SourceResponseError('invalid_source_cursor')
            result.append(item)
        return result

    @staticmethod
    def _new_cursor(plan, current, next_params, history):
        current_signature = _digest(current)
        next_history = list(history)
        if current_signature not in next_history:
            next_history.append(current_signature)
        if len(next_history) > _MAX_HISTORY:
            next_history = next_history[-_MAX_HISTORY:]
        cursor = {
            'v': _CURSOR_VERSION,
            'source': plan.actual_source,
            'fingerprint': hashlib.sha256(plan.identity_json.encode('utf-8')).hexdigest(),
            'params': next_params,
            'history': next_history,
        }
        cursor['check'] = hashlib.sha256(canonical_json(cursor).encode('utf-8')).hexdigest()
        try:
            text = canonical_json(cursor)
        except ContractError:
            raise SourceResponseError('invalid_source_cursor') from None
        if len(text.encode('utf-8')) > _MAX_CURSOR:
            raise SourceResponseError('invalid_source_cursor')
        return text

    # -- response cursor ------------------------------------------------

    def _next_page(self, next_url, plan, current, history, response_date):
        next_params, url_date = (None, None) if next_url is None else self._parse_next_url(next_url, plan)
        actual_date = None
        if plan.actual_source == 'ranking':
            known = {value for value in (plan.requested_date, current.get('date'), response_date, url_date)
                     if value is not None}
            if len(known) > 1:
                raise SourceResponseError('next_page_date_mismatch')
            actual_date = response_date or url_date or current.get('date')
            if next_params is not None and actual_date is not None:
                next_params['date'] = actual_date
        if next_params is None:
            return None, actual_date
        self._check_progress(plan, current, next_params, history)
        return next_params, actual_date

    def _parse_next_url(self, url, plan):
        if (
            not isinstance(url, str)
            or not url
            or len(url) > _MAX_URL
            or any(ord(char) < 32 or ord(char) == 127 for char in url)
        ):
            raise SourceResponseError('invalid_next_page')
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            port = parsed.port
        except (ValueError, UnicodeError):
            raise SourceResponseError('invalid_next_page') from None
        if (
            parsed.scheme != 'https'
            or hostname != 'app-api.pixiv.net'
            or port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.path != plan.endpoint
        ):
            raise SourceResponseError('invalid_next_page')
        try:
            query = parse_qs(parsed.query, keep_blank_values=True)
        except (ValueError, UnicodeError):
            raise SourceResponseError('invalid_next_page') from None
        if plan.actual_source in {'personalized', 'related'}:
            query = _normalize_indexed_arrays(query, ('viewed', 'seed_illust_ids'))
        if plan.actual_source == 'ranking':
            allowed = {'mode', 'filter', 'date', 'offset'}
            array_keys = frozenset()
        elif plan.actual_source == 'search':
            allowed = {'word', 'search_target', 'sort', 'filter', 'start_date', 'end_date', 'offset'}
            array_keys = frozenset()
        elif plan.actual_source == 'artist':
            allowed = {'user_id', 'type', 'filter', 'offset'}
            array_keys = frozenset()
        elif plan.actual_source == 'personalized':
            allowed = {'content_type', 'include_ranking_label', 'include_ranking_illusts',
                       'include_privacy_policy', 'filter',
                       'max_bookmark_id_for_recommend',
                       'min_bookmark_id_for_recent_illust', 'offset', 'viewed[]'}
            array_keys = frozenset({'viewed[]'})
        else:
            allowed = {'illust_id', 'filter', 'offset', 'seed_illust_ids[]', 'viewed[]'}
            array_keys = frozenset({'viewed[]', 'seed_illust_ids[]'})
        if set(query) - allowed:
            raise SourceResponseError('unsupported_pagination_fields')
        for key, values in query.items():
            if key not in array_keys and len(values) != 1:
                raise SourceResponseError('duplicate_pagination_field')
            if not values or len(values) > 1000:
                raise SourceResponseError('invalid_next_page')
            for value in values:
                if len(value) > _MAX_URL or any(ord(char) < 32 or ord(char) == 127 for char in value):
                    raise SourceResponseError('invalid_next_page')

        base = dict(plan.base_params)
        if plan.actual_source == 'ranking':
            self._expect(query, 'mode', base['mode'], 'next_page_mode_mismatch')
            self._expect(query, 'filter', _FILTER, 'next_page_filter_mismatch')
            url_date = None
            if 'date' in query:
                url_date = _required_iso(query['date'][0], 'invalid_next_page')
                if plan.requested_date is not None and url_date != plan.requested_date:
                    raise SourceResponseError('next_page_date_mismatch')
            if 'offset' not in query:
                raise SourceResponseError('pagination_did_not_advance')
            return {'offset': _offset(query['offset'][0])}, url_date
        if plan.actual_source == 'search':
            self._expect(query, 'word', base['word'], 'next_page_query_mismatch')
            self._expect(query, 'search_target', base['search_target'], 'next_page_query_mismatch')
            self._expect(query, 'sort', plan.actual_sort, 'next_page_sort_mismatch')
            self._expect(query, 'filter', _FILTER, 'next_page_filter_mismatch')
            for key in ('start_date', 'end_date'):
                if key in query:
                    value = _required_iso(query[key][0], 'invalid_next_page')
                    if base.get(key) is None or value != base[key]:
                        raise SourceResponseError('next_page_query_mismatch')
            if 'offset' not in query:
                raise SourceResponseError('pagination_did_not_advance')
            return {'offset': _offset(query['offset'][0])}, None
        if plan.actual_source == 'artist':
            self._expect(query, 'user_id', base['user_id'], 'next_page_identity_mismatch')
            self._expect(query, 'type', base['type'], 'next_page_type_mismatch')
            self._expect(query, 'filter', _FILTER, 'next_page_filter_mismatch')
            if 'offset' not in query:
                raise SourceResponseError('pagination_did_not_advance')
            return {'offset': _offset(query['offset'][0])}, None
        if plan.actual_source == 'personalized':
            self._expect(query, 'content_type', base['content_type'], 'next_page_type_mismatch')
            self._expect(query, 'filter', _FILTER, 'next_page_filter_mismatch')
            self._expect(query, 'include_ranking_label', 'true', 'next_page_query_mismatch')
            for key in ('include_privacy_policy', 'include_ranking_illusts'):
                if key in query and query[key][0] not in {'true', 'false'}:
                    raise SourceResponseError('invalid_next_page')
            params = {}
            for key in ('include_privacy_policy', 'include_ranking_illusts'):
                if key in query:
                    params[key] = query[key][0]
            if 'offset' in query:
                params['offset'] = _offset(query['offset'][0])
            for key in ('max_bookmark_id_for_recommend', 'min_bookmark_id_for_recent_illust'):
                if key in query:
                    try:
                        params[key] = _pagination_id(query[key][0], allow_end_marker=key=='max_bookmark_id_for_recommend')
                    except SourceResponseError:
                        raise SourceResponseError('invalid_next_page') from None
            if 'viewed[]' in query:
                params['viewed'] = _stable_list(query['viewed[]'])
            if not params:
                raise SourceResponseError('pagination_did_not_advance')
            return params, None
        self._expect(query, 'illust_id', base['illust_id'], 'next_page_identity_mismatch')
        self._expect(query, 'filter', _FILTER, 'next_page_filter_mismatch')
        seed = base['illust_id']
        if 'seed_illust_ids[]' in query:
            seeds = _stable_list(query['seed_illust_ids[]'])
            if seeds != [seed]:
                raise SourceResponseError('next_page_seed_mismatch')
        if 'offset' not in query:
            raise SourceResponseError('pagination_did_not_advance')
        params = {'offset': _offset(query['offset'][0])}
        if 'viewed[]' in query:
            params['viewed'] = _stable_list(query['viewed[]'])
        return params, None

    @staticmethod
    def _expect(query, key, expected, code):
        if key in query and query[key][0] != expected:
            raise SourceResponseError(code)

    @staticmethod
    def _check_progress(plan, current, next_params, history):
        if not next_params:
            raise SourceResponseError('pagination_did_not_advance')
        current_signature = _digest(current)
        next_signature = _digest(next_params)
        if next_signature == current_signature:
            raise SourceResponseError('pagination_did_not_advance')
        if next_signature in history:
            raise SourceResponseError('pagination_cycle')
        current_offset = current.get('offset', 0)
        next_offset = next_params.get('offset')
        if plan.actual_source == 'personalized':
            if next_offset is not None and next_offset < current_offset:
                raise SourceResponseError('pagination_did_not_advance')
        elif plan.actual_source == 'related':
            if next_offset is None or next_offset < current_offset:
                raise SourceResponseError('pagination_did_not_advance')
            if next_offset == current_offset and not set(current.get('viewed', ())) < set(next_params.get('viewed', ())):
                raise SourceResponseError('pagination_did_not_advance')
        else:
            if next_offset is None or next_offset <= current_offset:
                raise SourceResponseError('pagination_did_not_advance')
        if plan.actual_source == 'ranking' and 'date' in current and next_params.get('date') != current['date']:
            raise SourceResponseError('next_page_date_mismatch')

    # -- provenance -----------------------------------------------------

    @staticmethod
    def _provenance(plan, call_kwargs, actual_date):
        requested = json.loads(plan.identity_json)
        provenance = {
            'requested': requested,
            'requested_source': requested.get('source'),
            'requested_sort': requested.get('requested_sort'),
            'actual_source': plan.actual_source,
            'actual_sort': plan.actual_sort,
            'actual_date': actual_date,
            'filter': _FILTER,
            'call': {'method': plan.method, **call_kwargs},
        }
        if plan.actual_source == 'ranking':
            provenance['rank_basis'] = 'response_order'
            provenance['requested_date'] = plan.requested_date
        elif plan.actual_source == 'search':
            provenance['sort_evidence'] = 'request_accepted'
        elif plan.actual_source in {'personalized', 'related'}:
            provenance['recommendation_kind'] = plan.actual_source
        elif plan.actual_source == 'artist':
            provenance['artist_source'] = True
        try:
            text = canonical_json(provenance)
        except ContractError:
            raise SourceResponseError('invalid_query_parameters') from None
        if len(text.encode('utf-8')) > _MAX_PROVENANCE:
            raise SourceResponseError('invalid_query_parameters')
        return text


__all__ = ['PixivQuerySource', 'QueryPage']
