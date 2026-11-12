import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from core.library_repository import (
    BudgetExceeded, CursorConflict, InvalidStateError, LibraryRepository, SourceCooling,
)
from core.task_contracts import (
    BatchBudget, CandidateDecision, CollectionRequest, QuerySpec, SourcePage,
    PathRef, PageSpec, OutputSpec, QualitySpec, canonical_json,
)


def _candidate(illust_id, *, artist='7'):
    page = PageSpec(
        page_index=0,
        output_ref=PathRef(relative_path=f'library/out/{illust_id}_p0.jpg'),
        url=f'https://i.pximg.net/img-original/{illust_id}_p0.jpg',
    )
    return CandidateDecision(
        illust_id=illust_id, decision='download', artist_id=artist,
        artist_name='artist', title='title', page_count=1, pages=(page,),
    )


def _request(request_id, source='intersection', parameters=None):
    if parameters is None and source == 'intersection':
        parameters = {'sources': [
            {'source': 'ranking', 'parameters': {'mode': 'day'}},
            {'source': 'search', 'parameters': {'word': 'cat', 'search_target': 'partial_match_for_tags'}},
        ]}
    return CollectionRequest(
        request_id, 'account-test',
        query=QuerySpec(source=source, mode='query',
                        parameters_json=canonical_json(parameters or {})),
        output=OutputSpec(folder_by_artist=False),
        quality=QualitySpec(),
        budget=BatchBudget(max_works=50, max_requests=10, max_artists=10),
    )


def _activate(repo, request):
    submitted = repo.submit(request, 'submit-' + request.request_id)
    active = repo.activate_next()
    assert active is not None and active['batch_id'] == submitted['batch_id']
    return submitted


def _page(candidates, *, source='query.intersection.0', pass_id='ranking',
          entity='2024-01-01', page_identity='p1', before='null',
          after='{"api_cursor":"next","offset":0,"page_digest":null}',
          exhausted=False, actual_sort='ranking', coverage=None):
    return SourcePage(
        source, pass_id, entity, page_identity, tuple(candidates),
        before, canonical_json(after) if isinstance(after, dict) else after,
        exhausted, actual_sort,
        canonical_json(coverage or {'coverage_complete': exhausted}),
    )


def test_query_schema_and_request_reservation_share_the_single_writer(tmp_path):
    with LibraryRepository(tmp_path / 'library.sqlite3') as repo:
        job = _activate(repo, _request('q-one'))
        assert repo.reserve_source_request(job['batch_id'], kind='query_page') is not None
        status = repo.source_budget_status(job['batch_id'])
        assert status['reserved_requests'] == 1
        with sqlite3.connect(repo.database_path) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert {'query_members', 'query_source_pages'} <= tables


def test_stage_query_page_is_idempotent_and_cursor_bound(tmp_path):
    with LibraryRepository(tmp_path / 'library.sqlite3') as repo:
        job = _activate(repo, _request('q-stage'))
        page = _page((_candidate('100'),), after={'api_cursor': 'next', 'offset': 0, 'page_digest': None})
        assert repo.stage_query_page(job['batch_id'], page) == 1
        assert repo.query_member_count(job['batch_id'], 'query.intersection.0') == 1
        assert repo.stage_query_page(job['batch_id'], page) == 0
        with pytest.raises(CursorConflict):
            repo.stage_query_page(job['batch_id'], _page(
                (_candidate('101'),), page_identity='p2', before='null',
                after={'api_cursor': 'other', 'offset': 0, 'page_digest': None},
            ))
        with sqlite3.connect(repo.database_path) as conn:
            rows = conn.execute(
                'SELECT source_id,entity_id,payload FROM source_items'
            ).fetchall()
        assert len(rows) == 1 and rows[0][0] == 'query.intersection.0'


def test_intersection_candidates_use_distinct_logical_source_membership(tmp_path):
    with LibraryRepository(tmp_path / 'library.sqlite3') as repo:
        job = _activate(repo, _request('q-join'))
        first = _page((_candidate('100'), _candidate('101')), source='query.intersection.0',
                      page_identity='a1', exhausted=True)
        second = _page((_candidate('100'),), source='query.intersection.1',
                       page_identity='b1', exhausted=True)
        repo.stage_query_page(job['batch_id'], first)
        repo.stage_query_page(job['batch_id'], second)
        rows = repo.query_intersection_candidates(
            job['batch_id'], ['query.intersection.0', 'query.intersection.1']
        )
        assert [row['illust_id'] for row in rows] == ['100']
        assert rows[0]['source_count'] == 2
        assert json.loads(rows[0]['candidate_json'])['illust_id'] == '100'

        # A second unit of the same logical source does not change membership.
        extra = _page((_candidate('100'),), source='query.intersection.0',
                      pass_id='ranking', entity='2024-01-02', page_identity='a2',
                      exhausted=True)
        repo.stage_query_page(job['batch_id'], extra)
        rows = repo.query_intersection_candidates(
            job['batch_id'], ['query.intersection.0', 'query.intersection.1']
        )
        assert [row['illust_id'] for row in rows] == ['100']
        assert rows[0]['source_count'] == 2


def test_query_staging_is_rejected_for_non_intersection_jobs(tmp_path):
    with LibraryRepository(tmp_path / 'library.sqlite3') as repo:
        job = _activate(repo, _request('q-ranking', source='ranking', parameters={'mode': 'day'}))
        with pytest.raises(InvalidStateError):
            repo.stage_query_page(job['batch_id'], _page((_candidate('100'),)))
        assert repo.query_member_count(job['batch_id'], 'query.intersection.0') == 0


def test_query_reservation_budget_and_cooldown_are_persistent(tmp_path):
    with LibraryRepository(tmp_path / 'library.sqlite3') as repo:
        request = _request('q-budget')
        request = replace(request, budget=BatchBudget(max_works=50, max_requests=1, max_artists=10))
        job = _activate(repo, request)
        repo.reserve_source_request(job['batch_id'], kind='query_page')
        with pytest.raises(BudgetExceeded):
            repo.reserve_source_request(job['batch_id'], kind='query_page')
        repo.record_query_cooldown(job['batch_id'], 30, reason_code='metadata_rate_limited')
        with pytest.raises(SourceCooling):
            repo.reserve_source_request(job['batch_id'], kind='detail')


def test_empty_intersection_is_bounded_and_safe(tmp_path):
    with LibraryRepository(tmp_path / 'library.sqlite3') as repo:
        job = _activate(repo, _request('q-empty'))
        repo.stage_query_page(job['batch_id'], _page((), source='query.intersection.0',
                                                     exhausted=True))
        repo.stage_query_page(job['batch_id'], _page((), source='query.intersection.1',
                                                     exhausted=True))
        assert repo.query_intersection_candidates(
            job['batch_id'], ['query.intersection.0', 'query.intersection.1']
        ) == ()
        assert repo.query_member_count(job['batch_id'], 'query.intersection.0') == 0
