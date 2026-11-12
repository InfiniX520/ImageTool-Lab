"""Offline contracts for the single-call P7 ranking/search query adapter."""
from __future__ import annotations

import json

import pytest

from core.candidate_filter import SourceResponseError
from core.follow_contracts import ArtworkPage
from core.pixiv_query_source import PixivQuerySource, QueryPage


class Requests:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Api:
    requests_kwargs = {'timeout': (5, 15), 'allow_redirects': False}

    def __init__(self):
        self.calls = []
        self.requests = Requests()
        self.result = {'illusts': [], 'next_url': None}
        self.error = None

    def illust_ranking(self, **kwargs):
        self.calls.append(('illust_ranking', kwargs))
        if self.error is not None:
            raise self.error
        return self.result

    def search_illust(self, **kwargs):
        self.calls.append(('search_illust', kwargs))
        if self.error is not None:
            raise self.error
        return self.result

    def illust_detail(self, **kwargs):
        self.calls.append(('illust_detail', kwargs))
        return {'illust': artwork(kwargs['illust_id'])}

    def user_illusts(self, **kwargs):
        self.calls.append(('user_illusts', kwargs))
        if self.error is not None:
            raise self.error
        return self.result

    def illust_recommended(self, **kwargs):
        self.calls.append(('illust_recommended', kwargs))
        if self.error is not None:
            raise self.error
        return self.result

    def illust_related(self, **kwargs):
        self.calls.append(('illust_related', kwargs))
        if self.error is not None:
            raise self.error
        return self.result


def artwork(value, *, artist=7, kind='illust'):
    return {
        'id': value,
        'user': {'id': artist, 'name': '画师'},
        'type': kind,
        'page_count': 1,
        'meta_single_page': {'original_image_url': f'https://i.pximg.net/{value}.jpg'},
        'total_bookmarks': 10,
        'illust_ai_type': 1,
        'visible': True,
    }


def client(api):
    return PixivQuerySource(api, account_id='9', account_ref='offline-account')


def ranking_url(offset=30, *, mode='day', date_value=None):
    url = f'https://app-api.pixiv.net/v1/illust/ranking?mode={mode}&filter=for_ios&offset={offset}'
    if date_value is not None:
        url += f'&date={date_value}'
    return url


def search_url(
    offset=30,
    *,
    word='cat',
    target='partial_match_for_tags',
    sort='date_desc',
    start_date=None,
    end_date=None,
):
    url = (
        'https://app-api.pixiv.net/v1/search/illust'
        f'?word={word}&search_target={target}&sort={sort}&filter=for_ios&offset={offset}'
    )
    if start_date is not None:
        url += f'&start_date={start_date}'
    if end_date is not None:
        url += f'&end_date={end_date}'
    return url


def test_ranking_first_page_is_one_call_with_actual_date_and_provenance():
    api = Api()
    api.result = {
        'illusts': [artwork(1)],
        'next_url': ranking_url(date_value='2024-02-29'),
        'date': '2024-02-29',
    }
    page = client(api).query_page(
        'ranking', '{"mode":"day","date":"2024-02-29"}'
    )
    assert len(api.calls) == 1
    assert api.calls[0] == (
        'illust_ranking',
        {'mode': 'day', 'filter': 'for_ios', 'date': '2024-02-29', 'offset': 0},
    )
    assert page.actual_source == 'ranking'
    assert page.actual_sort == 'ranking'
    assert page.actual_date == '2024-02-29'
    assert [item.illust_id for item in page.page.artworks] == ['1']
    assert not page.page.exhausted
    cursor = json.loads(page.page.next_cursor_json)
    assert cursor['source'] == 'ranking'
    assert cursor['params'] == {'offset': 30, 'date': '2024-02-29'}
    provenance = json.loads(page.provenance_json)
    assert provenance['rank_basis'] == 'response_order'
    assert provenance['requested_date'] == '2024-02-29'
    assert provenance['actual_date'] == '2024-02-29'
    assert provenance['call']['method'] == 'illust_ranking'


def test_ranking_second_page_keeps_date_and_advances_offset():
    api = Api()
    api.result = {
        'illusts': [artwork(1)],
        'next_url': ranking_url(date_value='2024-02-29'),
        'date': '2024-02-29',
    }
    first = client(api).query_page(
        'ranking', '{"mode":"day","date":"2024-02-29"}'
    )
    api.result = {'illusts': [], 'next_url': None, 'date': '2024-02-29'}
    second = client(api).query_page(
        'ranking',
        '{"mode":"day","date":"2024-02-29"}',
        first.page.next_cursor_json,
    )
    assert len(api.calls) == 2
    assert api.calls[-1][1] == {
        'mode': 'day',
        'filter': 'for_ios',
        'date': '2024-02-29',
        'offset': 30,
    }
    assert second.page.exhausted
    assert second.page.next_cursor_json == 'null'


def test_ranking_requested_date_without_response_evidence_is_null():
    api = Api()
    api.result = {'illusts': [], 'next_url': None}
    page = client(api).query_page(
        'ranking', '{"mode":"week","date":"2024-02-29"}'
    )
    assert page.actual_date is None
    provenance = json.loads(page.provenance_json)
    assert provenance['requested_date'] == '2024-02-29'
    assert provenance['actual_date'] is None


def test_ranking_empty_legal_page_is_immediate_exhaustion_without_retry():
    api = Api()
    api.result = {'illusts': [], 'next_url': None}
    page = client(api).query_page('ranking', '{"mode":"day"}')
    assert page.page.exhausted
    assert page.page.artworks == ()
    assert page.page.next_cursor_json == 'null'
    assert len(api.calls) == 1


@pytest.mark.parametrize(
    'parameters, sort',
    [
        ('{"mode":"not_a_mode"}', 'source_default'),
        ('{"mode":"day","date":"2023-02-29"}', 'source_default'),
        ('{"mode":"day","date":"2024-2-1"}', 'source_default'),
        ('{"mode":"day","extra":1}', 'source_default'),
        ('{"mode":"day"}', 'date_desc'),
    ],
)
def test_ranking_rejects_invalid_parameters_or_sort_without_sdk_call(parameters, sort):
    api = Api()
    with pytest.raises(SourceResponseError):
        client(api).query_page('ranking', parameters, requested_sort=sort)
    assert api.calls == []


def test_ranking_response_date_is_carried_into_next_page():
    api = Api()
    api.result = {
        'illusts': [artwork(4)],
        'next_url': ranking_url(),
        'date': '2024-02-29',
    }
    source = client(api)
    first = source.query_page('ranking', '{"mode":"day"}')
    assert first.actual_date == '2024-02-29'
    cursor = json.loads(first.page.next_cursor_json)
    assert cursor['params']['date'] == '2024-02-29'
    api.result = {'illusts': [], 'next_url': None}
    second = source.query_page(
        'ranking', '{"mode":"day"}', first.page.next_cursor_json
    )
    assert api.calls[-1][1] == {
        'mode': 'day',
        'filter': 'for_ios',
        'offset': 30,
        'date': '2024-02-29',
    }
    assert second.actual_date == '2024-02-29'


def test_search_default_sort_dates_and_actual_source():
    api = Api()
    api.result = {
        'illusts': [artwork(2)],
        'next_url': search_url(start_date='2024-01-01', end_date='2024-02-01'),
    }
    page = client(api).query_page(
        'search',
        '{"word":"cat","search_target":"partial_match_for_tags",'
        '"start_date":"2024-01-01","end_date":"2024-02-01"}',
    )
    assert len(api.calls) == 1
    assert api.calls[0][1] == {
        'word': 'cat',
        'search_target': 'partial_match_for_tags',
        'sort': 'date_desc',
        'filter': 'for_ios',
        'start_date': '2024-01-01',
        'end_date': '2024-02-01',
        'offset': 0,
    }
    assert page.actual_source == 'search'
    assert page.actual_sort == 'date_desc'
    assert page.actual_date is None
    provenance = json.loads(page.provenance_json)
    assert provenance['sort_evidence'] == 'request_accepted'
    assert provenance['requested']['requested_sort'] == 'source_default'
    cursor = json.loads(page.page.next_cursor_json)
    assert cursor['source'] == 'search'
    assert cursor['params'] == {'offset': 30}


def test_search_explicit_sort_and_second_page_preserve_parameters():
    api = Api()
    api.result = {
        'illusts': [artwork(3)],
        'next_url': search_url(sort='popular_desc', target='title_and_caption'),
    }
    first = client(api).query_page(
        'search',
        '{"word":"cat","search_target":"title_and_caption"}',
        requested_sort='popular_desc',
    )
    assert api.calls[-1][1]['sort'] == 'popular_desc'
    assert first.actual_sort == 'popular_desc'
    api.result = {'illusts': [], 'next_url': None}
    second = client(api).query_page(
        'search',
        '{"word":"cat","search_target":"title_and_caption"}',
        first.page.next_cursor_json,
        requested_sort='popular_desc',
    )
    assert api.calls[-1][1] == {
        'word': 'cat',
        'search_target': 'title_and_caption',
        'sort': 'popular_desc',
        'filter': 'for_ios',
        'offset': 30,
    }
    assert second.page.exhausted


@pytest.mark.parametrize(
    'parameters, sort',
    [
        ('{"word":"","search_target":"partial_match_for_tags"}', 'source_default'),
        ('{"word":"cat","search_target":"keyword"}', 'source_default'),
        (
            '{"word":"cat","search_target":"partial_match_for_tags",'
            '"start_date":"2024-03-01","end_date":"2024-02-01"}',
            'source_default',
        ),
        (
            '{"word":"cat","search_target":"partial_match_for_tags",'
            '"start_date":"2024-02-30"}',
            'source_default',
        ),
        ('{"word":"cat","search_target":"partial_match_for_tags"}', 'latest'),
        ('{"word":"cat","search_target":"partial_match_for_tags","extra":1}', 'source_default'),
    ],
)
def test_search_rejects_invalid_parameters_without_sdk_call(parameters, sort):
    api = Api()
    with pytest.raises(SourceResponseError):
        client(api).query_page('search', parameters, requested_sort=sort)
    assert api.calls == []


@pytest.mark.parametrize('source', ['unknown', 'user'])
def test_unknown_sources_are_rejected_without_sdk_calls(source):
    api = Api()
    with pytest.raises(SourceResponseError) as error:
        client(api).query_page(source, '{}')
    assert error.value.code == 'unsupported_query_source'
    assert api.calls == []


def test_artist_query_uses_user_illusts_and_preserves_partition():
    api = Api()
    api.result = {
        'illusts': [artwork(1, kind='manga')],
        'next_url': (
            'https://app-api.pixiv.net/v1/user/illusts'
            '?user_id=7&type=manga&filter=for_ios&offset=30'
        ),
    }
    first = client(api).query_page(
        'artist', '{"artist_id":"7","partition":"manga"}'
    )
    assert api.calls[-1] == (
        'user_illusts',
        {'user_id': '7', 'type': 'manga', 'filter': 'for_ios', 'offset': 0},
    )
    assert first.actual_source == 'artist'
    assert first.actual_sort == 'source_default'
    api.result = {'illusts': [], 'next_url': None}
    second = client(api).query_page(
        'artist', '{"artist_id":"7","partition":"manga"}',
        first.page.next_cursor_json,
    )
    assert second.page.exhausted
    assert api.calls[-1][1]['offset'] == 30


def test_personalized_recommendation_keeps_bounded_cursor_fields():
    api = Api()
    api.result = {
        'illusts': [artwork(1)],
        'next_url': (
            'https://app-api.pixiv.net/v1/illust/recommended'
            '?content_type=illust&include_ranking_label=true&filter=for_ios'
            '&max_bookmark_id_for_recommend=90&min_bookmark_id_for_recent_illust=10'
            '&offset=30&viewed[]=111&viewed[]=222'
        ),
    }
    first = client(api).query_page(
        'recommendations', '{"kind":"personalized"}'
    )
    assert api.calls[-1] == (
        'illust_recommended',
        {'content_type': 'illust', 'filter': 'for_ios', 'offset': 0},
    )
    assert first.actual_source == 'personalized'
    api.result = {'illusts': [], 'next_url': None}
    second = client(api).query_page(
        'recommendations', '{"kind":"personalized"}',
        first.page.next_cursor_json,
    )
    assert second.page.exhausted
    assert api.calls[-1][1] == {
        'content_type': 'illust',
        'filter': 'for_ios',
        'offset': 30,
        'max_bookmark_id_for_recommend': '90',
        'min_bookmark_id_for_recent_illust': '10',
        'viewed': ['111', '222'],
    }


def test_related_recommendation_uses_seed_and_rejects_changed_seed():
    api = Api()
    api.result = {
        'illusts': [artwork(1)],
        'next_url': (
            'https://app-api.pixiv.net/v2/illust/related'
            '?illust_id=7&filter=for_ios&offset=30&seed_illust_ids[]=7'
        ),
    }
    first = client(api).query_page(
        'recommendations', '{"kind":"related","illust_id":"7"}'
    )
    assert api.calls[-1] == (
        'illust_related',
        {'illust_id': '7', 'filter': 'for_ios', 'offset': 0},
    )
    assert first.actual_source == 'related'
    api.result = {'illusts': [], 'next_url': None}
    second = client(api).query_page(
        'recommendations', '{"kind":"related","illust_id":"7"}',
        first.page.next_cursor_json,
    )
    assert second.page.exhausted

    api.result = {
        'illusts': [],
        'next_url': (
            'https://app-api.pixiv.net/v2/illust/related'
            '?illust_id=7&filter=for_ios&offset=30&seed_illust_ids[]=8'
        ),
    }
    with pytest.raises(SourceResponseError):
        client(api).query_page(
            'recommendations', '{"kind":"related","illust_id":"7"}'
        )


@pytest.mark.parametrize(
    'url',
    [
        'https://evil.invalid/v1/search/illust?word=cat&search_target=partial_match_for_tags&sort=date_desc&offset=30',
        'https://user:pass@app-api.pixiv.net/v1/search/illust?word=cat&search_target=partial_match_for_tags&sort=date_desc&offset=30',
        'https://app-api.pixiv.net/v1/search/illust?word=cat&search_target=partial_match_for_tags&sort=date_desc&offset=30#fragment',
        'https://app-api.pixiv.net/v1/illust/ranking?mode=day&offset=30',
        'https://app-api.pixiv.net/v1/search/illust?word=cat&search_target=partial_match_for_tags&sort=date_desc&offset=30&offset=60',
        'https://app-api.pixiv.net/v1/search/illust?word=cat&search_target=partial_match_for_tags&sort=date_desc&offset=30&unknown=1',
        'https://app-api.pixiv.net/v1/search/illust?word=cat&search_target=partial_match_for_tags&sort=date_desc&offset=30&access_token=synthetic-secret',
        'https://app-api.pixiv.net/v1/search/illust?word=cat&search_target=partial_match_for_tags&sort=date_desc&offset=30\n',
    ],
)
def test_next_url_rejects_unsafe_or_extra_fields_and_never_leaks_secret(url):
    api = Api()
    api.result = {'illusts': [], 'next_url': url}
    with pytest.raises(SourceResponseError) as error:
        client(api).query_page(
            'search', '{"word":"cat","search_target":"partial_match_for_tags"}'
        )
    assert len(api.calls) == 1
    assert 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize(
    'url',
    [
        search_url(word='dog'),
        search_url(target='exact_match_for_tags'),
        search_url(sort='date_asc'),
        search_url(start_date='2024-01-01'),
        search_url(offset=0),
    ],
)
def test_search_next_url_must_preserve_semantics_and_advance_offset(url):
    api = Api()
    api.result = {'illusts': [], 'next_url': url}
    with pytest.raises(SourceResponseError):
        client(api).query_page(
            'search', '{"word":"cat","search_target":"partial_match_for_tags"}'
        )
    assert len(api.calls) == 1


@pytest.mark.parametrize(
    'url',
    [
        ranking_url(mode='week'),
        ranking_url(date_value='2024-02-28'),
        ranking_url(offset=0),
    ],
)
def test_ranking_next_url_must_preserve_mode_date_and_advance(url):
    api = Api()
    api.result = {'illusts': [], 'next_url': url}
    with pytest.raises(SourceResponseError):
        client(api).query_page(
            'ranking', '{"mode":"day","date":"2024-02-29"}'
        )
    assert len(api.calls) == 1


def test_cursor_is_bound_to_query_and_rejects_fingerprint_tampering():
    api = Api()
    api.result = {
        'illusts': [artwork(1)],
        'next_url': search_url(),
    }
    first = client(api).query_page(
        'search', '{"word":"cat","search_target":"partial_match_for_tags"}'
    )
    cursor = first.page.next_cursor_json
    with pytest.raises(SourceResponseError) as error:
        client(api).query_page(
            'search', '{"word":"dog","search_target":"partial_match_for_tags"}', cursor
        )
    assert error.value.code == 'cursor_query_mismatch'
    assert len(api.calls) == 1

    value = json.loads(cursor)
    value['fingerprint'] = '0' * 64
    with pytest.raises(SourceResponseError):
        client(api).query_page(
            'search',
            '{"word":"cat","search_target":"partial_match_for_tags"}',
            json.dumps(value),
        )
    assert len(api.calls) == 1

    value = json.loads(cursor)
    value['params']['offset'] = 60
    with pytest.raises(SourceResponseError):
        client(api).query_page(
            'search',
            '{"word":"cat","search_target":"partial_match_for_tags"}',
            json.dumps(value),
        )
    assert len(api.calls) == 1


def test_cursor_cycle_and_replayed_page_are_rejected():
    client_instance = client(Api())
    api = client_instance._api
    api.result = {'illusts': [], 'next_url': search_url(offset=30)}
    first = client_instance.query_page(
        'search', '{"word":"cat","search_target":"partial_match_for_tags"}'
    )
    api.result = {'illusts': [], 'next_url': search_url(offset=60)}
    second = client_instance.query_page(
        'search',
        '{"word":"cat","search_target":"partial_match_for_tags"}',
        first.page.next_cursor_json,
    )
    api.result = {'illusts': [], 'next_url': search_url(offset=30)}
    with pytest.raises(SourceResponseError) as error:
        client_instance.query_page(
            'search',
            '{"word":"cat","search_target":"partial_match_for_tags"}',
            second.page.next_cursor_json,
        )
    assert error.value.code == 'pagination_cycle'
    assert len(api.calls) == 3


@pytest.mark.parametrize(
    'result',
    [
        {'next_url': None},
        {'illusts': []},
        {'illusts': [], 'next_url': None, 'error': {'message': 'synthetic-secret'}},
    ],
)
def test_missing_metadata_or_error_payload_is_not_an_empty_result(result):
    api = Api()
    api.result = result
    with pytest.raises(SourceResponseError) as error:
        client(api).query_page('search', '{"word":"cat","search_target":"partial_match_for_tags"}')
    assert len(api.calls) == 1
    assert 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize(
    'values',
    [
        [artwork(1), artwork(1)],
        [{'id': 'not-an-id'}],
    ],
)
def test_duplicate_or_invalid_artwork_pages_are_rejected(values):
    api = Api()
    api.result = {'illusts': values, 'next_url': None}
    with pytest.raises(SourceResponseError):
        client(api).query_page('search', '{"word":"cat","search_target":"partial_match_for_tags"}')
    assert len(api.calls) == 1


def test_oversized_page_is_rejected_before_normalization():
    api = Api()
    api.result = {
        'illusts': [artwork(index) for index in range(2001)],
        'next_url': None,
    }
    with pytest.raises(SourceResponseError):
        client(api).query_page('search', '{"word":"cat","search_target":"partial_match_for_tags"}')
    assert len(api.calls) == 1


def test_transport_exception_is_mapped_without_raw_message():
    api = Api()
    api.error = RuntimeError('synthetic-secret')
    with pytest.raises(SourceResponseError) as error:
        client(api).query_page('ranking', '{"mode":"day"}')
    assert error.value.code == 'source_transport_failed'
    assert 'synthetic-secret' not in str(error.value)
    assert len(api.calls) == 1


def test_details_and_close_are_inherited_from_follow_source():
    api = Api()
    source = client(api)
    detail = source.details(7)
    assert detail.illust_id == '7'
    assert api.calls[-1][0] == 'illust_detail'
    source.close()
    assert api.requests.closed is True


def test_installed_app_client_is_compatible_without_network():
    from pixivpy3 import AppPixivAPI

    api = AppPixivAPI(timeout=(5, 15), allow_redirects=False)
    try:
        source = PixivQuerySource(api, account_id='9', account_ref='offline-account')
        assert source.account_id == '9'
    finally:
        api.requests.close()


def test_query_page_rejects_invalid_actual_metadata():
    with pytest.raises(SourceResponseError):
        QueryPage(ArtworkPage(()), 'unknown', 'ranking')
    with pytest.raises(SourceResponseError):
        QueryPage(ArtworkPage(()), 'ranking', 'ranking', '2024-02-30')
    with pytest.raises(SourceResponseError):
        QueryPage(ArtworkPage(()), 'ranking', 'ranking', None, '[]')
