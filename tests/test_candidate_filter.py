from dataclasses import replace

import pytest

from core.candidate_filter import ArtworkMetadata, SourceResponseError, evaluate_filters, make_candidate, merge_details, needs_details, normalize_artwork
from core.task_contracts import CollectionRequest, FilterSpec, QuerySpec


@pytest.mark.parametrize('minimum,exclude,bookmarks,ai,state',[
    (None,False,None,None,'allow'),(0,False,0,None,'allow'),(10,False,10,None,'allow'),
    (10,False,9,None,'filtered'),(10,False,None,None,'metadata_unknown'),
    (None,True,None,False,'allow'),(None,True,None,True,'filtered'),
    (None,True,None,None,'metadata_unknown'),(10,True,9,None,'filtered'),
    (10,True,None,True,'filtered'),(10,True,10,False,'allow'),
    (0,True,0,False,'allow'),(0,False,False,None,'metadata_unknown'),
])
def test_filters_are_optional_combined_and_keep_unknown_distinct(minimum,exclude,bookmarks,ai,state):
    result=evaluate_filters(FilterSpec(minimum,exclude),bookmarks=bookmarks,ai_flag=ai)
    assert result.state==state
    if state=='filtered':assert all('unknown' not in reason for reason in result.reasons)


def artwork(**values):
    raw={'id':123,'user':{'id':7,'name':'artist'},'title':'source','type':'illust',
        'page_count':1,'total_bookmarks':0,'illust_ai_type':1,
        'meta_single_page':{'original_image_url':'https://i.pximg.net/img-original/2026/123_p0.png'}}
    return normalize_artwork(dict(raw,**values))


def test_default_request_collects_originals_without_preview_or_extra_filters():
    request=CollectionRequest('request','account')
    meta=artwork()
    assert request.query.mode=='history'
    assert not needs_details(meta,request)
    candidate=make_candidate(meta,request)
    assert candidate.decision=='download'
    assert candidate.pages[0].url==meta.original_urls[0]
    assert candidate.bookmarks==0 and candidate.ai_flag is False


def test_preview_only_metadata_is_unknown_and_never_expanded_by_filename_guess():
    meta=artwork(meta_single_page={},image_urls={'large':'https://i.pximg.net/img-master/123_p0.jpg'})
    request=CollectionRequest('request','account')
    assert needs_details(meta,request)
    assert make_candidate(meta,request).decision=='metadata_unknown'


def test_mixed_multipage_extensions_are_preserved_exactly():
    urls=['https://i.pximg.net/img-original/2026/123_p0.jpg','https://i.pximg.net/img-original/2026/123_p1.png']
    meta=artwork(type='manga',page_count=2,meta_pages=[{'image_urls':{'original':url}} for url in urls])
    candidate=make_candidate(meta,CollectionRequest('request','account'))
    assert [page.url for page in candidate.pages]==urls
    assert [page.output_ref.relative_path.rsplit('.',1)[-1] for page in candidate.pages]==['jpg','png']


def test_known_rejection_avoids_detail_lookup_even_with_other_unknown_fields():
    meta=artwork(total_bookmarks=2,illust_ai_type=0,meta_single_page={})
    request=CollectionRequest('request','account',query=QuerySpec(filters=FilterSpec(100,True)))
    assert not needs_details(meta,request)
    candidate=make_candidate(meta,request)
    assert candidate.decision=='filtered' and candidate.reason_codes==('bookmarks_below_minimum',)


@pytest.mark.parametrize('raw_ai,expected',[(0,None),(1,False),(2,True),(False,None),(None,None)])
def test_app_ai_codes_do_not_turn_missing_or_zero_into_non_ai(raw_ai,expected):
    assert artwork(illust_ai_type=raw_ai).ai_flag is expected


@pytest.mark.parametrize('raw',[None,[],{'error':{'message':'synthetic-secret'}},{'id':True},{'id':'../123'}])
def test_malformed_responses_never_become_empty_success(raw):
    with pytest.raises(SourceResponseError) as error:normalize_artwork(raw)
    assert 'synthetic-secret' not in str(error.value)


def test_detail_identity_is_checked_and_known_zero_values_survive_merge():
    original=artwork(total_bookmarks=99,illust_ai_type=2)
    detail=artwork(title='',total_bookmarks=0,illust_ai_type=1)
    merged=merge_details(original,detail)
    assert merged.bookmarks==0 and merged.ai_flag is False and merged.title=='source'
    with pytest.raises(SourceResponseError):merge_details(original,replace(detail,illust_id='124'))
    with pytest.raises(SourceResponseError):merge_details(original,replace(detail,artist_id='8'))


@pytest.mark.parametrize('changes,state',[({'kind':'ugoira'},'unsupported'),({'available':False},'unavailable')])
def test_unsupported_and_inaccessible_works_never_request_previews(changes,state):
    meta=replace(artwork(),**changes)
    request=CollectionRequest('request','account')
    assert not needs_details(meta,request)
    assert make_candidate(meta,request).decision==state


def test_remote_display_controls_are_normalized_without_retaining_arbitrary_payload():
    meta=artwork(title='a\n|b\x00',access_token='synthetic-private')
    assert meta.title=='a |b '
    assert 'synthetic-private' not in repr(meta)
