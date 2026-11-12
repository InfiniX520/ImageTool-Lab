import json

import pytest

from core.candidate_filter import SourceResponseError
from core.query_parameters import validate_query_parameters


def test_ranking_single_and_inclusive_31_day_range_are_normalized():
    assert validate_query_parameters(
        'ranking', '{"mode":"day","date":"2024-02-29"}'
    ) == {'mode': 'day', 'date': '2024-02-29'}
    ranged = validate_query_parameters(
        'ranking',
        '{"mode":"day","start_date":"2024-01-01","end_date":"2024-01-31"}',
    )
    assert ranged == {
        'mode': 'day', 'start_date': '2024-01-01', 'end_date': '2024-01-31'
    }


@pytest.mark.parametrize(
    'params',
    [
        '{"mode":"week","start_date":"2024-01-01","end_date":"2024-01-02"}',
        '{"mode":"month","start_date":"2024-01-01","end_date":"2024-01-02"}',
        '{"mode":"day","start_date":"2024-01-01","end_date":"2024-03-01"}',
        '{"mode":"day","start_date":"2024-02-02","end_date":"2024-02-01"}',
        '{"mode":"day","date":"2024-02-29","start_date":"2024-02-01","end_date":"2024-02-02"}',
        '{"mode":"day","date":"2023-02-29"}',
        '{"mode":"day","start_date":"2024-02-01"}',
        '{"mode":"day","extra":1}',
    ],
)
def test_ranking_rejects_ambiguous_invalid_or_overlong_ranges(params):
    with pytest.raises(SourceResponseError):
        validate_query_parameters('ranking', params)


def test_search_dates_sort_and_target_are_normalized():
    result = validate_query_parameters(
        'search',
        '{"word":"  cat  ","search_target":"exact_match_for_tags",'
        '"start_date":"2024-01-01","end_date":"2024-02-01"}',
        'popular_desc',
    )
    assert result == {
        'word': 'cat',
        'search_target': 'exact_match_for_tags',
        'start_date': '2024-01-01',
        'end_date': '2024-02-01',
    }


@pytest.mark.parametrize(
    'params,sort',
    [
        ('{"word":"","search_target":"partial_match_for_tags"}', 'source_default'),
        ('{"word":"cat","search_target":"keyword"}', 'source_default'),
        ('{"word":"cat","search_target":"partial_match_for_tags","start_date":"2024-02-02","end_date":"2024-02-01"}', 'source_default'),
        ('{"word":"cat","search_target":"partial_match_for_tags","start_date":"2024-02-30"}', 'source_default'),
        ('{"word":"cat","search_target":"partial_match_for_tags"}', 'latest'),
        ('{"word":"cat","search_target":"partial_match_for_tags","extra":1}', 'source_default'),
    ],
)
def test_search_rejects_invalid_values_and_sorts(params, sort):
    with pytest.raises(SourceResponseError):
        validate_query_parameters('search', params, sort)


def test_artist_ids_are_stable_deduplicated_and_bounded():
    result = validate_query_parameters(
        'artist', '{"artist_ids":["7",7,"8","7"]}'
    )
    assert result == {'artist_ids': ['7', '8']}
    with pytest.raises(SourceResponseError):
        validate_query_parameters('artist', '{"artist_ids":[]}')
    with pytest.raises(SourceResponseError):
        validate_query_parameters(
            'artist', json.dumps({'artist_ids': [str(i) for i in range(201)]})
        )


def test_recommendations_validate_kind_and_seed():
    assert validate_query_parameters(
        'recommendations', '{"kind":"personalized"}'
    ) == {'kind': 'personalized', 'content_type': 'illust'}
    assert validate_query_parameters(
        'recommendations', '{"kind":"related","illust_id":"123"}'
    ) == {'kind': 'related', 'illust_id': '123'}
    with pytest.raises(SourceResponseError):
        validate_query_parameters('recommendations', '{"kind":"related"}')
    with pytest.raises(SourceResponseError):
        validate_query_parameters('recommendations', '{"kind":"personalized","illust_id":"1"}')


def test_intersection_normalizes_two_to_four_unique_sources():
    result = validate_query_parameters(
        'intersection',
        json.dumps({
            'sources': [
                {'source': 'ranking', 'parameters': {'mode': 'day'}},
                {'source': 'search', 'parameters': '{"word":"cat","search_target":"partial_match_for_tags"}', 'requested_sort': 'date_asc'},
                {'source': 'follow', 'parameters': {}},
            ],
            'per_source_limit': 50,
        }),
    )
    assert result['per_source_limit'] == 50
    assert len(result['sources']) == 3
    assert result['sources'][2] == {
        'source': 'follow', 'parameters': {}, 'requested_sort': 'source_default'
    }


@pytest.mark.parametrize(
    'sources',
    [
        [],
        [{'source': 'rank', 'parameters': {'mode': 'day'}}],
        [
            {'source': 'ranking', 'parameters': {'mode': 'day'}},
            {'source': 'ranking', 'parameters': {'mode': 'day'}},
        ],
        [
            {'source': 'ranking', 'parameters': {'mode': 'day'}},
            {'source': 'intersection', 'parameters': {'sources': []}},
        ],
        [
            {'source': 'follow', 'parameters': {'history': True}},
            {'source': 'search', 'parameters': {'word': 'cat', 'search_target': 'partial_match_for_tags'}},
        ],
    ],
)
def test_intersection_rejects_invalid_duplicate_or_nested_sources(sources):
    with pytest.raises(SourceResponseError):
        validate_query_parameters('intersection', json.dumps({'sources': sources}))


def test_parameter_errors_are_safe_codes_without_echoing_values():
    secret = 'synthetic-secret-value'
    with pytest.raises(SourceResponseError) as error:
        validate_query_parameters('search', json.dumps({'word': secret, 'search_target': 'bad'}))
    assert secret not in str(error.value)
