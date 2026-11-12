"""Supervisor acceptance for real pagination shapes and durable filter semantics."""
import json
import sqlite3
from dataclasses import replace
from urllib.parse import urlencode

import pytest

from core.candidate_filter import SourceResponseError
from core.query_collection import QueryCollection
from core.source_settings import source_query, source_request, source_description
from core.collection_settings import CollectionSettings, SettingsError
from core.task_contracts import BatchBudget, FilterSpec, canonical_json
from test_query_collection import Owner, Source, meta, query_page, run_query
from test_delivery_coordinator import wait_for
from test_pixiv_query_source import Api, client


def intersection(limit=200):
    return {'sources': [
        {'source': 'ranking', 'parameters': {'mode': 'day'}},
        {'source': 'search', 'parameters': {'word': 'cat', 'search_target': 'partial_match_for_tags'}},
    ], 'per_source_limit': limit}


def pages(source, rank, search, *, first_cursor='null', rank_done=True):
    source.query_pages['ranking', canonical_json({'mode': 'day'}), 'null'] = query_page(
        rank, 'ranking', 'ranking', next_cursor=first_cursor, exhausted=rank_done)
    source.query_pages['search', canonical_json(intersection()['sources'][1]['parameters']), 'null'] = query_page(
        search, 'search', 'date_desc')


def decisions(owner, batch):
    with sqlite3.connect(owner.repo.database_path) as conn:
        return conn.execute('SELECT illust_id,decision FROM batch_items WHERE batch_id=? ORDER BY illust_id', (batch,)).fetchall()


def test_filtered_candidates_use_the_source_cap_and_stay_in_manifest(tmp_path):
    owner = Owner(tmp_path, responses=0)
    try:
        source = Source()
        pages(source, [meta(100, bookmarks=5), meta(101, bookmarks=9999)], [meta(100, bookmarks=5)])
        job = owner.submit('intersection', intersection(1), filters=FilterSpec(min_bookmarks=1000))
        report = run_query(owner, source, job)
        assert report.reason_code == 'source_limit_reached'
        assert decisions(owner, job['batch_id']) == [('100', 'filtered')]
        assert len(source.calls) == 2 and owner.transport.calls == []
        assert owner.repo.query_member_count(job['batch_id'], 'query.intersection.0') == 1
    finally:
        owner.close()


def test_intersection_only_fetches_unknown_details_for_matching_works(tmp_path):
    owner = Owner(tmp_path, responses=0)
    try:
        source = Source()
        pages(source, [meta(100), meta(101)], [meta(100), meta(102)])
        source.detail_values['100'] = meta(100)
        job = owner.submit('intersection', intersection(), filters=FilterSpec(exclude_ai=True))
        report = run_query(owner, source, job)
        assert report.coverage_complete
        assert decisions(owner, job['batch_id']) == [('100', 'metadata_unknown')]
        assert [c for c in source.calls if c[0] == 'detail'] == [('detail', '100')]
        assert len(owner.transport.calls) == 0
    finally:
        owner.close()


def test_conflicting_ai_observation_cannot_be_selected_as_non_ai(tmp_path):
    owner = Owner(tmp_path, responses=0)
    try:
        source = Source()
        pages(source, [meta(100, ai_flag=False)], [meta(100, ai_flag=True)])
        job = owner.submit('intersection', intersection(), filters=FilterSpec(exclude_ai=True))
        assert run_query(owner, source, job).coverage_complete
        assert decisions(owner, job['batch_id']) == [('100', 'filtered')]
        assert owner.transport.calls == []
    finally:
        owner.close()


def test_unique_cap_does_not_count_repeated_works_across_pages(tmp_path):
    owner = Owner(tmp_path, responses=2)
    try:
        source = Source()
        pages(source, [meta(100)], [meta(100), meta(101)], first_cursor='"next"', rank_done=False)
        source.query_pages['ranking', canonical_json({'mode': 'day'}), '"next"'] = query_page(
            [meta(100), meta(101), meta(102)], 'ranking', 'ranking')
        job = owner.submit('intersection', intersection(2))
        assert run_query(owner, source, job).reason_code == 'source_limit_reached'
        wait_for(lambda: len(owner.transport.calls) == 2)
        assert decisions(owner, job['batch_id']) == [('100', 'download'), ('101', 'download')]
        with sqlite3.connect(owner.repo.database_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM source_items WHERE source_id='query.intersection.0' AND entity_id='100'").fetchone()[0] == 2
    finally:
        owner.close()


def test_intersection_work_budget_resume_carries_total_and_does_not_requery(tmp_path):
    owner = Owner(tmp_path, responses=3)
    try:
        source = Source()
        values = [meta(100), meta(101), meta(102)]
        pages(source, values, values)
        job = owner.submit('intersection', intersection(), budget=BatchBudget(max_works=1, max_requests=10))
        assert run_query(owner, source, job).reason_code == 'work_budget'
        wait_for(lambda: owner.repo.get_batch(job['batch_id'])['phase'] == 'paused')
        second = owner.resume(job, 'resume-remaining', budget=BatchBudget(max_works=10, max_requests=10))
        assert run_query(owner, source, second).coverage_complete
        wait_for(lambda: owner.repo.get_batch(second['batch_id'])['phase'] == 'completed')
        assert len(source.calls) == 2 and len(owner.transport.calls) == 3
        checkpoint = owner.repo.get_checkpoint(job['job_id'], 'query.intersection.result', 'result', 'intersection')
        assert json.loads(checkpoint['coverage_json'])['matches'] == 3
    finally:
        owner.close()


def test_intersection_after_library_reopen_skips_completed_sources(tmp_path):
    owner=Owner(tmp_path, responses=0)
    source=Source();pages(source,[meta(100)],[meta(100)])
    try:
        job=owner.submit('intersection',intersection(),budget=BatchBudget(max_works=10,max_requests=1))
        assert run_query(owner,source,job).reason_code=='source_request_budget'
        wait_for(lambda:owner.repo.get_batch(job['batch_id'])['phase']=='paused')
        assert owner.repo.query_member_count(job['batch_id'],'query.intersection.0')==1
    finally:owner.close()
    reopened=Owner(tmp_path,responses=1)
    try:
        after_restart=Source()
        after_restart.query_pages['search',canonical_json(intersection()['sources'][1]['parameters']),'null']=query_page([meta(100)],'search','date_desc')
        resumed=reopened.resume(job,'resume-after-reopen',budget=BatchBudget(max_works=10,max_requests=5))
        assert run_query(reopened,after_restart,resumed).coverage_complete
        wait_for(lambda:reopened.repo.get_batch(resumed['batch_id'])['phase']=='completed')
        assert len(after_restart.calls)==1 and after_restart.calls[0][1]=='search'
        assert len(reopened.transport.calls)==1
    finally:reopened.close()


def recommended_url(pairs):
    return 'https://app-api.pixiv.net/v1/illust/recommended?' + urlencode(pairs)


def test_real_indexed_recommendation_shape_is_replayable_and_safe():
    api = Api(); source = client(api)
    api.result = {'illusts': [], 'next_url': recommended_url([
        ('offset', '30'), ('content_type', 'illust'), ('filter', 'for_ios'),
        ('include_ranking_label', 'true'), ('include_ranking_illusts', 'true'),
        ('include_privacy_policy', 'true'), ('viewed[1]', '101'), ('viewed[0]', '100')])}
    first = source.query_page('recommendations', '{"kind":"personalized"}')
    api.result = {'illusts': [], 'next_url': None}
    for _ in range(2):
        source.query_page('recommendations', '{"kind":"personalized"}', first.page.next_cursor_json)
        assert api.calls[-1][1]['viewed'] == ['100', '101']
        assert api.calls[-1][1]['offset'] == 30
        assert api.calls[-1][1]['include_privacy_policy'] == 'true'


def test_real_recommendation_end_marker_and_flags_survive_continuation():
    api = Api(); source = client(api)
    api.result = {'illusts': [], 'next_url': recommended_url([
        ('offset', '30'), ('max_bookmark_id_for_recommend', '-1'),
        ('include_ranking_illusts', 'false'), ('include_privacy_policy', 'false')])}
    first = source.query_page('recommendations', '{"kind":"personalized"}')
    api.result = {'illusts': [], 'next_url': None}
    source.query_page('recommendations', '{"kind":"personalized"}', first.page.next_cursor_json)
    assert api.calls[-1][1]['max_bookmark_id_for_recommend'] == '-1'
    assert api.calls[-1][1]['include_ranking_illusts'] == 'false'
    assert api.calls[-1][1]['include_privacy_policy'] == 'false'


def test_related_zero_offset_advances_by_viewed_set_and_rejects_repeats():
    api = Api(); source = client(api)
    url = 'https://app-api.pixiv.net/v2/illust/related?illust_id=7&filter=for_ios&offset=0&seed_illust_ids[0]=7&viewed[0]=100'
    api.result = {'illusts': [], 'next_url': url}
    first = source.query_page('recommendations', '{"kind":"related","illust_id":"7"}')
    with pytest.raises(SourceResponseError):
        source.query_page('recommendations', '{"kind":"related","illust_id":"7"}', first.page.next_cursor_json)
    api.result = {'illusts': [], 'next_url': url+'&viewed[1]=101'}
    next_page = source.query_page('recommendations', '{"kind":"related","illust_id":"7"}', first.page.next_cursor_json)
    assert not next_page.page.exhausted
    assert api.calls[-1][1]['viewed'] == ['100']


def test_long_but_bounded_recommendation_history_survives_multiple_pages():
    api = Api(); source = client(api)
    pairs = [('offset', '30')] + [(f'viewed[{i}]', str(100000000+i)) for i in range(300)]
    url = recommended_url(pairs)
    assert len(url) > 4096
    api.result = {'illusts': [], 'next_url': url}
    first = source.query_page('recommendations', '{"kind":"personalized"}')
    api.result = {'illusts': [], 'next_url': None}
    source.query_page('recommendations', '{"kind":"personalized"}', first.page.next_cursor_json)
    assert len(api.calls[-1][1]['viewed']) == 300


@pytest.mark.parametrize('pairs', [
    [('viewed[1]', '100')], [('viewed[0]', '100'), ('viewed[0]', '101')],
    [('viewed[0]', '100'), ('viewed[]', '101')], [('viewed[00]', '100')],
    [('viewed[0]', '../secret')], [('redirect', 'https://example.org')],
    [('include_privacy_policy', 'secret')],
    [('max_bookmark_id_for_recommend', '-2')], [('min_bookmark_id_for_recent_illust', '-1')],
])
def test_unsafe_recommendation_pagination_stops_without_second_call(pairs):
    api = Api(); source = client(api)
    api.result = {'illusts': [], 'next_url': recommended_url([('offset', '30'), *pairs])}
    with pytest.raises(SourceResponseError):source.query_page('recommendations', '{"kind":"personalized"}')
    assert len(api.calls) == 1


@pytest.mark.parametrize('extra', [
    {'source': 'search', 'search_word': ''},
    {'source': 'ranking', 'ranking_mode': '周榜', 'ranking_start_date': '2026-10-01', 'ranking_end_date': '2026-10-02'},
    {'source': 'ranking', 'ranking_date': '2026-10-01', 'ranking_start_date': '2026-10-01', 'ranking_end_date': '2026-10-02'},
    {'source': 'ranking', 'ranking_date': '2026-02-30'},
    {'source': 'intersection', 'include_follow': True},
    {'source': 'recommendations', 'recommendation_kind': '相似作品', 'related_illust_id': 'bad'},
])
def test_invalid_source_forms_are_rejected_before_account_access(extra):
    with pytest.raises((SettingsError, SourceResponseError)):
        source_query({**CollectionSettings().form(), **extra})


def test_form_retains_combined_filters_and_portable_output():
    form = {**CollectionSettings().form(), 'source': 'intersection', 'include_follow': True,
            'include_search': True, 'search_word': '風景', 'search_sort': '热门排序（会员）',
            'exclude_ai': True, 'min_bookmarks_enabled': True, 'min_bookmarks': '1000',
            'output_relative': 'pictures', 'per_source_limit': '50'}
    request = source_request(form, CollectionSettings(), 'form', 'offline-account')
    assert request.query.filters == FilterSpec(min_bookmarks=1000, exclude_ai=True)
    assert request.output.root_ref.relative_path == 'pictures'
    params = json.loads(request.query.parameters_json)
    assert params['sources'][1]['requested_sort'] == 'popular_desc'
    assert params['per_source_limit'] == 50
    assert '50' in source_description({'source': 'intersection', 'parameters_json': request.query.parameters_json})
