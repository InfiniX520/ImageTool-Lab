import pytest
from core.candidate_filter import SourceResponseError
from test_pixiv_query_source import Api, client, artwork, ranking_url, search_url


def test_resume_can_reread_a_previously_observed_page_before_durable_acceptance():
    api=Api();source=client(api)
    api.result={'illusts':[artwork(1)],'next_url':search_url()}
    params='{"word":"cat","search_target":"partial_match_for_tags"}'
    first=source.query_page('search',params)
    api.result={'illusts':[artwork(2)],'next_url':None}
    observed=source.query_page('search',params,first.page.next_cursor_json)
    # Pause/crash before durable acceptance requires a legal re-read. The
    # adapter has no authority to call a successful HTTP response committed.
    repeated=source.query_page('search',params,first.page.next_cursor_json)
    assert repeated==observed and len(api.calls)==3


@pytest.mark.parametrize('requested,response,next_date',[
    (None,'2024-02-29','2024-03-01'),
    ('2024-02-29','2024-03-01',None),
    ('2024-02-29','2024-03-01','2024-02-29'),
])
def test_contradictory_ranking_dates_are_never_recorded_as_one_snapshot(requested,response,next_date):
    import json
    api=Api();source=client(api)
    api.result={'illusts':[artwork(1)],'date':response,
                'next_url':None if next_date is None else 'https://app-api.pixiv.net/v1/illust/ranking?mode=day&offset=30&date='+next_date}
    params={'mode':'day'}
    if requested:params['date']=requested
    with pytest.raises(SourceResponseError,match='date_mismatch'):
        source.query_page('ranking',json.dumps(params))
    assert len(api.calls)==1


def test_last_page_cannot_switch_the_date_pinned_by_first_page():
    api=Api();source=client(api)
    api.result={'illusts':[artwork(1)],'next_url':'https://app-api.pixiv.net/v1/illust/ranking?mode=day&offset=30&date=2024-02-29'}
    first=source.query_page('ranking','{"mode":"day"}')
    api.result={'illusts':[artwork(2)],'date':'2024-03-01','next_url':None}
    with pytest.raises(SourceResponseError,match='date_mismatch'):
        source.query_page('ranking','{"mode":"day"}',first.page.next_cursor_json)


def test_blank_search_is_rejected_before_http():
    api=Api();source=client(api)
    with pytest.raises(SourceResponseError):
        source.query_page('search','{"word":"   ","search_target":"partial_match_for_tags"}')
    assert api.calls==[]


def test_invalid_response_date_is_not_silently_treated_as_unknown():
    api=Api();source=client(api)
    api.result={'illusts':[], 'next_url':None, 'date':'2024-02-30'}
    with pytest.raises(SourceResponseError,match='invalid_ranking_date'):
        source.query_page('ranking','{"mode":"day"}')
