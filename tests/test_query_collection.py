import json
import threading

import pytest

from core.candidate_filter import ArtworkMetadata, SourceResponseError
from core.collection_runtime import CollectionRuntime
from core.follow_contracts import ArtworkPage
from core.library_repository import LibraryRepository
from core.pixiv_query_source import QueryPage
from core.query_collection import QueryCollection
from core.task_contracts import (
    BatchBudget, CollectionRequest, FilterSpec, OutputSpec, QuerySpec, canonical_json,
)
from core.task_coordinator import DeliveryOutcome, TaskCoordinator
from delivery_fixtures import Response, Transport, executor
from test_delivery_coordinator import wait_for


def meta(value, artist='7', *, kind='illust', **kwargs):
    return ArtworkMetadata(
        str(value), artist, 'artist-' + artist, 'title-' + str(value),
        kind=kind, page_count=1,
        original_urls=(f'https://i.pximg.net/img-original/2026/{value}_p0.png',),
        **kwargs,
    )


def artwork_page(values, *, next_cursor='null', exhausted=True, coverage=True):
    return ArtworkPage(tuple(values), next_cursor, exhausted, coverage)


def query_page(values, source, sort, *, date_value=None, next_cursor='null',
               exhausted=True, coverage=True):
    return QueryPage(
        artwork_page(values, next_cursor=next_cursor, exhausted=exhausted,
                     coverage=coverage),
        source, sort, date_value,
        canonical_json({'actual_source': source, 'actual_sort': sort}),
    )


class Source:
    account_ref = 'offline-account'

    def __init__(self):
        self.query_pages = {}
        self.artist_pages = {}
        self.new_pages = {}
        self.detail_values = {}
        self.calls = []
        self.fail_next = None

    def query_page(self, source, parameters_json, cursor='null', *, requested_sort='source_default'):
        self.calls.append(('query', source, parameters_json, cursor, requested_sort))
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        key = (source, parameters_json, cursor)
        result = self.query_pages[key]
        if isinstance(result, BaseException):
            raise result
        return result

    def artist_works(self, artist, cursor, *, partition):
        self.calls.append(('artist', artist, cursor, partition))
        return self.artist_pages[(artist, cursor, partition)]

    def new_works(self, cursor, *, partition):
        self.calls.append(('new', partition, cursor))
        return self.new_pages[(partition, cursor)]

    def details(self, illust_id):
        self.calls.append(('detail', illust_id))
        return self.detail_values[illust_id]


class Owner:
    def __init__(self, tmp_path, responses=10):
        self.repo = LibraryRepository(tmp_path / 'library.sqlite3')
        self.transport = Transport(*(Response() for _ in range(responses)))
        self.owner = TaskCoordinator(
            self.repo, executor(self.repo, tmp_path, self.transport), workers=1
        )
        self.owner.start().result(5)

    def close(self):
        self.owner.close()

    def submit(self, source, parameters, *, budget=None, request_id='query', filters=None,
               requested_sort='source_default'):
        request = CollectionRequest(
            request_id, 'offline-account',
            query=QuerySpec(source=source, mode='query',
                            parameters_json=canonical_json(parameters or {}),
                            requested_sort=requested_sort,
                            filters=filters or FilterSpec()),
            output=OutputSpec(folder_by_artist=False),
            budget=budget or BatchBudget(max_works=20, max_requests=40, max_artists=20),
        )
        job = self.owner.submit(request, 'submit-' + request_id).result(5)
        self.owner.run_queued().result(5)
        wait_for(lambda: self.owner.active_batch_id == job['batch_id'])
        return job

    def resume(self, job, command, *, budget=None):
        result = self.owner.resume(job['job_id'], command, budget).result(5)
        self.owner.run_queued().result(5)
        wait_for(lambda: self.owner.active_batch_id == result['batch_id'])
        return result


def run_query(owner, source, job):
    report = QueryCollection(owner.owner, source).run(job['batch_id'])
    wait_for(lambda: owner.repo.get_batch(job['batch_id'])['phase'] in {
        'completed', 'partial', 'paused', 'blocked', 'cancelled'
    })
    return report


def test_ranking_range_deduplicates_works_and_records_dates(tmp_path):
    owner = Owner(tmp_path, responses=2)
    source = Source()
    source.query_pages[('ranking', canonical_json({'mode': 'day', 'date': '2024-01-02'}), 'null')] = query_page(
        [meta(100)], 'ranking', 'ranking', date_value='2024-01-02'
    )
    source.query_pages[('ranking', canonical_json({'mode': 'day', 'date': '2024-01-01'}), 'null')] = query_page(
        [meta(100), meta(101)], 'ranking', 'ranking', date_value='2024-01-01'
    )
    try:
        job = owner.submit('ranking', {
            'mode': 'day', 'start_date': '2024-01-01', 'end_date': '2024-01-02',
        })
        report = run_query(owner, source, job)
        assert report.coverage_complete and report.reason_code == 'source_exhausted'
        wait_for(lambda: owner.repo.get_batch(job['batch_id'])['phase'] == 'completed')
        result = owner.repo.batch_results(job['batch_id'])
        assert result['works']['download'] == 2
        assert len(owner.transport.calls) == 2
        calls = [item for item in source.calls if item[0] == 'query']
        assert [item[2] for item in calls][0] == canonical_json({'mode': 'day', 'date': '2024-01-02'})
        assert len(calls) == 2
    finally:
        owner.close()


def test_work_budget_mid_page_resumes_without_losing_later_candidates(tmp_path):
    owner = Owner(tmp_path, responses=3)
    source = Source()
    key = ('ranking', canonical_json({'mode': 'day'}), 'null')
    source.query_pages[key] = query_page([meta(100), meta(101), meta(102)], 'ranking', 'ranking')
    try:
        job = owner.submit('ranking', {'mode': 'day'},
                           budget=BatchBudget(max_works=1, max_requests=20, max_artists=20))
        first = run_query(owner, source, job)
        assert first.reason_code == 'work_budget' and not first.coverage_complete
        second = owner.resume(job, 'resume-query',
                              budget=BatchBudget(max_works=10, max_requests=20, max_artists=20))
        report = run_query(owner, source, second)
        assert report.coverage_complete
        wait_for(lambda: owner.repo.get_batch(second['batch_id'])['phase'] == 'completed')
        with __import__('sqlite3').connect(owner.repo.database_path) as conn:
            rows = conn.execute(
                'SELECT DISTINCT illust_id, result_state FROM batch_items ORDER BY illust_id'
            ).fetchall()
        assert [row[0] for row in rows] == ['100', '101', '102']
        assert all(row[1] == 'completed' for row in rows)
        assert len(owner.transport.calls) == 3
        assert len([item for item in source.calls if item[0] == 'query']) == 2
    finally:
        owner.close()


def test_request_budget_resumes_before_detail_and_retains_cursor(tmp_path):
    owner = Owner(tmp_path, responses=1)
    source = Source()
    key = ('search', canonical_json({
        'word': 'cat', 'search_target': 'partial_match_for_tags'
    }), 'null')
    source.query_pages[key] = query_page(
        [meta(100, bookmarks=None)], 'search', 'date_desc'
    )
    source.detail_values['100'] = meta(100, bookmarks=50)
    try:
        job = owner.submit('search', {
            'word': 'cat', 'search_target': 'partial_match_for_tags',
        }, budget=BatchBudget(max_works=10, max_requests=1, max_artists=10),
            filters=FilterSpec(min_bookmarks=10))
        first = run_query(owner, source, job)
        assert first.reason_code == 'source_request_budget'
        assert owner.repo.get_checkpoint(
            job['job_id'], 'query.search', 'search',
            __import__('core.query_collection', fromlist=['_digest'])._digest({
                'search': {'word': 'cat', 'search_target': 'partial_match_for_tags'},
                'sort': 'source_default',
            }),
        ) is None
        second = owner.resume(job, 'resume-detail',
                              budget=BatchBudget(max_works=10, max_requests=5, max_artists=10))
        report = run_query(owner, source, second)
        assert report.coverage_complete
        wait_for(lambda: owner.repo.get_batch(second['batch_id'])['phase'] == 'completed')
        assert [item for item in source.calls if item[0] == 'detail'] == [('detail', '100')]
    finally:
        owner.close()


def test_intersection_admits_only_works_present_in_every_logical_source(tmp_path):
    owner = Owner(tmp_path, responses=1)
    source = Source()
    ranking_params = canonical_json({'mode': 'day'})
    search_params = canonical_json({
        'word': 'cat', 'search_target': 'partial_match_for_tags'
    })
    source.query_pages[('ranking', ranking_params, 'null')] = query_page(
        [meta(100), meta(101)], 'ranking', 'ranking', date_value='2024-01-01'
    )
    source.query_pages[('search', search_params, 'null')] = query_page(
        [meta(100)], 'search', 'date_desc'
    )
    try:
        job = owner.submit('intersection', {
            'sources': [
                {'source': 'ranking', 'parameters': {'mode': 'day'}},
                {'source': 'search', 'parameters': {
                    'word': 'cat', 'search_target': 'partial_match_for_tags'
                }},
            ],
            'per_source_limit': 100,
        })
        report = run_query(owner, source, job)
        assert report.coverage_complete and report.reason_code == 'source_exhausted'
        wait_for(lambda: owner.repo.get_batch(job['batch_id'])['phase'] == 'completed')
        result = owner.repo.batch_results(job['batch_id'])
        assert result['works']['download'] == 1
        assert len(owner.transport.calls) == 1
        with __import__('sqlite3').connect(owner.repo.database_path) as conn:
            ids = [row[0] for row in conn.execute(
                'SELECT DISTINCT illust_id FROM batch_items ORDER BY illust_id'
            )]
        assert ids == ['100']
    finally:
        owner.close()


def test_intersection_without_matches_reports_a_bounded_reason(tmp_path):
    owner = Owner(tmp_path, responses=0)
    source = Source()
    ranking_params = canonical_json({'mode': 'day'})
    search_params = canonical_json({
        'word': 'cat', 'search_target': 'partial_match_for_tags'
    })
    source.query_pages[('ranking', ranking_params, 'null')] = query_page(
        [meta(100)], 'ranking', 'ranking', date_value='2024-01-01'
    )
    source.query_pages[('search', search_params, 'null')] = query_page(
        [meta(101)], 'search', 'date_desc'
    )
    try:
        job = owner.submit('intersection', {
            'sources': [
                {'source': 'ranking', 'parameters': {'mode': 'day'}},
                {'source': 'search', 'parameters': {
                    'word': 'cat', 'search_target': 'partial_match_for_tags'
                }},
            ],
        })
        report = run_query(owner, source, job)
        assert report.coverage_complete
        assert report.reason_code == 'no_intersection_matches'
        wait_for(lambda: owner.repo.get_batch(job['batch_id'])['phase'] == 'completed')
        assert owner.transport.calls == []
    finally:
        owner.close()


def test_intersection_source_failure_blocks_without_admitting_partial_members(tmp_path):
    owner = Owner(tmp_path, responses=0)
    source = Source()
    source.query_pages[('ranking', canonical_json({'mode': 'day'}), 'null')] = query_page(
        [meta(100)], 'ranking', 'ranking', date_value='2024-01-01'
    )
    try:
        job = owner.submit('intersection', {
            'sources': [
                {'source': 'ranking', 'parameters': {'mode': 'day'}},
                {'source': 'search', 'parameters': {
                    'word': 'cat', 'search_target': 'partial_match_for_tags'
                }},
            ],
        })
        # The first source is ranking, the second is search; make search fail.
        search_params = canonical_json({
            'word': 'cat', 'search_target': 'partial_match_for_tags'
        })
        source.query_pages[('search', search_params, 'null')] = SourceResponseError(
            'source_transport_failed'
        )
        report = run_query(owner, source, job)
        assert report.reason_code == 'source_transport_failed'
        assert not report.coverage_complete
        wait_for(lambda: owner.repo.get_batch(job['batch_id'])['phase'] == 'blocked')
        with __import__('sqlite3').connect(owner.repo.database_path) as conn:
            assert conn.execute('SELECT COUNT(*) FROM batch_items').fetchone()[0] == 0
    finally:
        owner.close()


def test_intersection_per_source_limit_is_reported_and_not_complete(tmp_path):
    owner = Owner(tmp_path, responses=1)
    source = Source()
    source.query_pages[('ranking', canonical_json({'mode': 'day'}), 'null')] = query_page(
        [meta(100), meta(101)], 'ranking', 'ranking', date_value='2024-01-01'
    )
    source.query_pages[('search', canonical_json({
        'word': 'cat', 'search_target': 'partial_match_for_tags'
    }), 'null')] = query_page([meta(100)], 'search', 'date_desc')
    try:
        job = owner.submit('intersection', {
            'sources': [
                {'source': 'ranking', 'parameters': {'mode': 'day'}},
                {'source': 'search', 'parameters': {
                    'word': 'cat', 'search_target': 'partial_match_for_tags'
                }},
            ],
            'per_source_limit': 1,
        })
        report = run_query(owner, source, job)
        assert not report.coverage_complete
        assert report.reason_code == 'source_limit_reached'
        wait_for(lambda: owner.repo.get_batch(job['batch_id'])['phase'] not in {
            'running', 'settling', 'pausing', 'cancelling'
        })
        assert len(owner.transport.calls) == 1
        assert owner.repo.query_member_count(job['batch_id'], 'query.intersection.1') == 1
    finally:
        owner.close()


def test_artist_partitions_use_parent_artist_works_and_empty_page_is_final(tmp_path):
    owner = Owner(tmp_path, responses=2)
    source = Source()
    source.artist_pages[('7', 'null', 'illust')] = artwork_page([meta(100)])
    source.artist_pages[('7', 'null', 'manga')] = artwork_page([meta(101, kind='manga')])
    try:
        job = owner.submit('artist', {'artist_ids': ['7']})
        report = run_query(owner, source, job)
        assert report.coverage_complete
        wait_for(lambda: owner.repo.get_batch(job['batch_id'])['phase'] == 'completed')
        calls = [item for item in source.calls if item[0] == 'artist']
        assert [item[3] for item in calls] == ['illust', 'manga']
        assert len(owner.transport.calls) == 2
    finally:
        owner.close()


def test_search_respects_requested_popular_sort_and_source_actual_sort(tmp_path):
    owner = Owner(tmp_path, responses=1)
    source = Source()
    params = canonical_json({
        'word': 'cat', 'search_target': 'title_and_caption'
    })
    source.query_pages[('search', params, 'null')] = query_page(
        [meta(150)], 'search', 'popular_desc'
    )
    try:
        job = owner.submit('search', {
            'word': 'cat', 'search_target': 'title_and_caption',
        }, request_id='query-sort', requested_sort='popular_desc')
        report = run_query(owner, source, job)
        assert report.coverage_complete
        wait_for(lambda: owner.repo.get_batch(job['batch_id'])['phase'] == 'completed')
        calls = [item for item in source.calls if item[0] == 'query']
        assert calls[0][4] == 'popular_desc'
    finally:
        owner.close()


def test_recommendations_query_runs_single_page_via_query_collection(tmp_path):
    owner = Owner(tmp_path, responses=1)
    source = Source()
    params = canonical_json({'kind': 'personalized', 'content_type': 'illust'})
    source.query_pages[('recommendations', params, 'null')] = query_page(
        [meta(160)], 'personalized', 'source_default'
    )
    try:
        job = owner.submit('recommendations', {'kind': 'personalized'},
                           request_id='query-recs')
        report = run_query(owner, source, job)
        assert report.coverage_complete
        wait_for(lambda: owner.repo.get_batch(job['batch_id'])['phase'] == 'completed')
        calls = [item for item in source.calls if item[0] == 'query']
        assert calls[0][1] == 'recommendations'
        assert owner.transport.calls
    finally:
        owner.close()


def test_intersection_pause_and_resume_keeps_staged_source_members(tmp_path):
    owner = Owner(tmp_path, responses=2)
    source = Source()
    ranking = canonical_json({'mode': 'day'})
    search = canonical_json({
        'word': 'cat', 'search_target': 'partial_match_for_tags'
    })
    source.query_pages[('ranking', ranking, 'null')] = query_page(
        [meta(200)], 'ranking', 'ranking', date_value='2024-01-01'
    )
    source.query_pages[('search', search, 'null')] = query_page(
        [meta(200)], 'search', 'date_desc'
    )
    stop = threading.Event()
    original_query = source.query_page

    def interrupting_page(source_name, parameters_json, cursor='null', *, requested_sort='source_default'):
        if source_name == 'search' and not stop.is_set():
            stop.set()
        return original_query(source_name, parameters_json, cursor,
                              requested_sort=requested_sort)

    source.query_page = interrupting_page
    try:
        job = owner.submit('intersection', {
            'sources': [
                {'source': 'ranking', 'parameters': {'mode': 'day'}},
                {'source': 'search', 'parameters': {
                    'word': 'cat', 'search_target': 'partial_match_for_tags'
                }},
            ],
        }, request_id='query-pause')
        first = QueryCollection(owner.owner, source).run(job['batch_id'], stop_event=stop)
        assert first.reason_code == 'stopped'
        wait_for(lambda: owner.repo.get_batch(job['batch_id'])['phase'] == 'paused')
        assert owner.repo.query_member_count(job['batch_id'], 'query.intersection.0') == 1
        stop.clear()
        # Restore the source for the resumed run; the first source is durable
        # and must not be queried again.
        source.query_page = original_query
        second = owner.resume(job, 'resume-intersection',
                              budget=BatchBudget(max_works=10, max_requests=20, max_artists=20))
        report = run_query(owner, source, second)
        assert report.coverage_complete and report.reason_code == 'source_exhausted'
        wait_for(lambda: owner.repo.get_batch(second['batch_id'])['phase'] == 'completed')
        ranking_calls = [item for item in source.calls
                          if item[0] == 'query' and item[1] == 'ranking']
        assert len(ranking_calls) == 1
    finally:
        owner.close()


def test_intersection_artist_partition_and_search_dedupe_same_work(tmp_path):
    owner = Owner(tmp_path, responses=2)
    source = Source()
    source.artist_pages[('7', 'null', 'illust')] = artwork_page([meta(210)])
    source.artist_pages[('7', 'null', 'manga')] = artwork_page([meta(210, kind='manga')])
    search = canonical_json({
        'word': 'cat', 'search_target': 'partial_match_for_tags'
    })
    source.query_pages[('search', search, 'null')] = query_page(
        [meta(210)], 'search', 'date_desc'
    )
    try:
        job = owner.submit('intersection', {
            'sources': [
                {'source': 'artist', 'parameters': {'artist_ids': ['7']}},
                {'source': 'search', 'parameters': {
                    'word': 'cat', 'search_target': 'partial_match_for_tags'
                }},
            ],
        }, request_id='query-partitions')
        report = run_query(owner, source, job)
        assert report.coverage_complete
        wait_for(lambda: owner.repo.get_batch(job['batch_id'])['phase'] == 'completed')
        with __import__('sqlite3').connect(owner.repo.database_path) as conn:
            ids = [row[0] for row in conn.execute(
                'SELECT DISTINCT illust_id FROM batch_items ORDER BY illust_id'
            )]
        assert ids == ['210']
        # Two artist partitions and one search page still form two logical
        # sources in the JOIN, not three.
        assert owner.repo.query_member_count(job['batch_id'], 'query.intersection.0') == 1
    finally:
        owner.close()


def test_collection_runtime_routes_query_requests_to_query_collection(tmp_path):
    repo = LibraryRepository(tmp_path / 'runtime.sqlite3')
    source = Source()
    source.query_pages[('search', canonical_json({
        'word': 'runtime', 'search_target': 'partial_match_for_tags'
    }), 'null')] = query_page([], 'search', 'date_desc')
    coordinator = TaskCoordinator(
        repo, executor(repo, tmp_path, Transport(Response())), workers=1
    )
    runtime = CollectionRuntime(coordinator, lambda request, stop: source)
    try:
        runtime.start().result(5)
        request = CollectionRequest(
            'runtime-query', 'offline-account',
            query=QuerySpec(source='search', mode='query',
                            parameters_json=canonical_json({
                                'word': 'runtime',
                                'search_target': 'partial_match_for_tags',
                            })),
            output=OutputSpec(folder_by_artist=False),
        )
        job = runtime.submit(request, 'runtime-submit').result(5)
        runtime.run_queued().result(5)
        wait_for(lambda: runtime.source_report(job['batch_id']) is not None)
        assert runtime.source_report(job['batch_id']).coverage_complete
        wait_for(lambda: repo.get_batch(job['batch_id'])['phase'] == 'completed')
    finally:
        runtime.close()


def test_empty_legal_search_page_is_exhausted_without_retry(tmp_path):
    owner = Owner(tmp_path, responses=0)
    source = Source()
    source.query_pages[('search', canonical_json({
        'word': 'cat', 'search_target': 'partial_match_for_tags'
    }), 'null')] = query_page([], 'search', 'date_desc')
    try:
        job = owner.submit('search', {'word': 'cat', 'search_target': 'partial_match_for_tags'})
        report = run_query(owner, source, job)
        assert report.coverage_complete and report.reason_code == 'source_exhausted'
        wait_for(lambda: owner.repo.get_batch(job['batch_id'])['phase'] == 'completed')
        assert owner.transport.calls == []
    finally:
        owner.close()


def test_retry_failed_only_and_from_head_do_not_requery_source(tmp_path):
    owner = Owner(tmp_path, responses=1)
    source = Source()
    try:
        job = owner.submit('search', {'word': 'cat', 'search_target': 'partial_match_for_tags'})
        report = QueryCollection(owner.owner, source).run(job['batch_id'], retry_failed_only=True)
        assert report.reason_code == 'failed_pages_only'
        assert source.calls == []

        source.query_pages[('search', canonical_json({
            'word': 'cat', 'search_target': 'partial_match_for_tags'
        }), 'null')] = query_page([meta(200), meta(201)], 'search', 'date_desc')
        job2 = owner.submit('search', {'word': 'cat', 'search_target': 'partial_match_for_tags'},
                            request_id='query-head',
                            budget=BatchBudget(max_works=1, max_requests=10, max_artists=10))
        first = run_query(owner, source, job2)
        assert first.reason_code == 'work_budget'
        wait_for(lambda: owner.repo.get_batch(job2['batch_id'])['phase'] == 'paused')
        calls_before = list(source.calls)
        resumed = owner.resume(job2, 'resume-head',
                               budget=BatchBudget(max_works=10, max_requests=10, max_artists=10))
        with pytest.raises(ValueError):
            QueryCollection(owner.owner, source).run(resumed['batch_id'], from_head=True)
        assert source.calls == calls_before
    finally:
        owner.close()
