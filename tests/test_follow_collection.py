from dataclasses import replace
import json
import sqlite3
import threading

import pytest
import requests

from core.candidate_filter import ArtworkMetadata
from core.collection_scope import scope_fingerprint
from core.follow_collection import FollowCollection, SourcePolicy
from core.follow_contracts import ArtworkPage, FollowArtist, FollowingPage
from core.library_repository import LibraryRepository
from core.task_contracts import BatchBudget, CollectionRequest, FilterSpec, OutputSpec, QuerySpec, canonical_json
from core.task_coordinator import TaskCoordinator
from delivery_fixtures import Response, Transport, executor
from test_delivery_coordinator import wait_for


def meta(value,artist='7',**kwargs):
    return ArtworkMetadata(str(value),artist,'artist-'+artist,'title-'+str(value),kind='illust',page_count=1,
        original_urls=(f'https://i.pximg.net/img-original/2026/{value}_p0.png',),**kwargs)


class Source:
    account_ref='offline-account'
    partitions=('all',)
    new_partitions=('public','private')

    def __init__(self,*artists):
        self.artists=artists or ('7',)
        self.pages={};self.detail_values={};self.calls=[];self.on_request=None

    def following(self,cursor):
        self.calls.append(('following',cursor))
        return FollowingPage(tuple(FollowArtist(value,'artist-'+value) for value in self.artists),exhausted=True,expected_total=len(self.artists))

    def artist_works(self,artist,cursor,*,partition):
        self.calls.append(('works',artist,cursor,partition))
        if self.on_request:self.on_request()
        result=self.pages[(artist,cursor)]
        if isinstance(result,BaseException):raise result
        return result

    def new_works(self,cursor,*,partition):
        self.calls.append(('new',partition,cursor))
        return self.pages[(partition,cursor)]

    def details(self,artwork):
        self.calls.append(('detail',artwork))
        return self.detail_values[artwork]


def submit(owner,identity='one',*,budget=None,filters=None,parameters=None,mode='history'):
    request=CollectionRequest(identity,'offline-account',output=OutputSpec(folder_by_artist=False),
        query=QuerySpec(mode=mode,filters=filters or FilterSpec(),parameters_json=canonical_json(parameters or {})),
        budget=budget or BatchBudget())
    job=owner.submit(request,'submit-'+identity).result(5)
    owner.run_queued().result(5)
    wait_for(lambda:owner.active_batch_id==job['batch_id'])
    return job


def resume(owner,job,identity,*,retry=False):
    result=owner.resume(job['job_id'],identity,retry_failed=retry).result(5)
    owner.run_queued().result(5)
    wait_for(lambda:owner.active_batch_id==result['batch_id'])
    return result


def settled(repo,job,phase='completed'):
    wait_for(lambda:repo.get_batch(job['batch_id'])['phase']==phase)
    return repo.batch_results(job['batch_id'])


def test_default_history_collects_all_pages_through_the_real_coordinator(tmp_path):
    source=Source();source.pages[('7','null')]=ArtworkPage((meta(123),meta(124)),exhausted=True)
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    transport=Transport(Response(),Response())
    with TaskCoordinator(repo,executor(repo,tmp_path,transport),workers=1) as owner:
        job=submit(owner)
        report=FollowCollection(owner,source).run(job['batch_id'])
        result=settled(repo,job)
        assert report.coverage_complete and report.network_calls_this_run==2
        assert result['pages']['new_completed']==result['works']['completed']==2
        assert len(transport.calls)==2


def test_optional_filters_short_circuit_and_unknown_remains_a_partial_result(tmp_path):
    source=Source()
    source.pages[('7','null')]=ArtworkPage((meta(123,bookmarks=2),meta(124,ai_flag=True),meta(125,bookmarks=200),meta(126)),exhausted=True)
    source.detail_values={'125':meta(125,bookmarks=200,ai_flag=False),'126':meta(126)}
    repo=LibraryRepository(tmp_path/'ledger.sqlite3');transport=Transport(Response())
    with TaskCoordinator(repo,executor(repo,tmp_path,transport),workers=1) as owner:
        job=submit(owner,filters=FilterSpec(100,True))
        FollowCollection(owner,source).run(job['batch_id'])
        result=settled(repo,job,'partial')
        assert [call[1] for call in source.calls if call[0]=='detail']==['125','126']
        assert result['works']['filtered']==2 and result['works']['metadata_unknown']==1
        assert result['pages']['new_completed']==1 and len(transport.calls)==1


def test_work_budget_mid_page_replays_changed_page_without_losing_insertions(tmp_path):
    source=Source();source.pages[('7','null')]=ArtworkPage((meta(123),meta(124)),exhausted=True)
    repo=LibraryRepository(tmp_path/'ledger.sqlite3');transport=Transport(Response(),Response(),Response())
    with TaskCoordinator(repo,executor(repo,tmp_path,transport),workers=1) as owner:
        job=submit(owner,budget=BatchBudget(max_works=1))
        service=FollowCollection(owner,source)
        assert service.run(job['batch_id']).reason_code=='work_budget'
        settled(repo,job,'paused')
        source.pages[('7','null')]=ArtworkPage((meta(125),meta(123),meta(124)),exhausted=True)
        second=resume(owner,job,'resume-two')
        assert service.run(second['batch_id']).reason_code=='work_budget'
        settled(repo,second,'paused')
        third=resume(owner,job,'resume-three')
        assert service.run(third['batch_id']).coverage_complete
        result=settled(repo,third)
        assert result['pages']['new_completed']==1 and result['pages']['existing']==2
        assert len(transport.calls)==3
        assert {url.rsplit('/',1)[-1] for url,_ in transport.calls}=={'123_p0.png','124_p0.png','125_p0.png'}


def test_request_budget_resumes_the_snapshot_without_repeating_the_following_list(tmp_path):
    source=Source();source.pages[('7','null')]=ArtworkPage((meta(123),),exhausted=True)
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    with TaskCoordinator(repo,executor(repo,tmp_path,Transport(Response())),workers=1) as owner:
        job=submit(owner,budget=BatchBudget(max_requests=1))
        service=FollowCollection(owner,source)
        assert service.run(job['batch_id']).reason_code=='source_request_budget'
        settled(repo,job,'paused')
        again=resume(owner,job,'resume')
        assert service.run(again['batch_id']).coverage_complete
        settled(repo,again)
        assert [call[0] for call in source.calls]==['following','works']


def test_artist_budget_skips_fully_enumerated_artists_on_resume(tmp_path):
    source=Source('7','8')
    source.pages={('7','null'):ArtworkPage((meta(123),),exhausted=True),('8','null'):ArtworkPage((meta(124,'8'),),exhausted=True)}
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    with TaskCoordinator(repo,executor(repo,tmp_path,Transport(Response(),Response())),workers=1) as owner:
        job=submit(owner,budget=BatchBudget(max_artists=1))
        service=FollowCollection(owner,source)
        assert service.run(job['batch_id']).reason_code=='source_artist_budget'
        settled(repo,job,'paused')
        again=resume(owner,job,'resume')
        assert service.run(again['batch_id']).coverage_complete
        settled(repo,again)
        assert [call[1] for call in source.calls if call[0]=='works']==['7','8']


def test_provider_failure_blocks_without_advancing_or_leaking_exception_text(tmp_path):
    source=Source();source.pages[('7','null')]=requests.ReadTimeout('synthetic-secret-cookie')
    repo=LibraryRepository(tmp_path/'ledger.sqlite3');transport=Transport()
    with TaskCoordinator(repo,executor(repo,tmp_path,transport),workers=1) as owner:
        job=submit(owner)
        report=FollowCollection(owner,source).run(job['batch_id'])
        settled(repo,job,'blocked')
        assert report.reason_code=='source_transport_failed'
        assert repo.get_checkpoint(job['job_id'],'follow.history','all','7') is None
        assert transport.calls==[]
        with sqlite3.connect(repo.database_path) as conn:
            assert all('synthetic-secret-cookie' not in row[0] for row in conn.execute('SELECT safe_payload FROM events'))


def test_cancelled_source_response_never_advances_past_unaccepted_candidates(tmp_path):
    source=Source();source.pages[('7','null')]=ArtworkPage((meta(123),),exhausted=True)
    stop=threading.Event();source.on_request=stop.set
    repo=LibraryRepository(tmp_path/'ledger.sqlite3');transport=Transport()
    with TaskCoordinator(repo,executor(repo,tmp_path,transport),workers=1) as owner:
        job=submit(owner)
        report=FollowCollection(owner,source).run(job['batch_id'],stop_event=stop)
        settled(repo,job,'paused')
        assert report.reason_code=='stopped'
        assert repo.get_checkpoint(job['job_id'],'follow.history','all','7') is None
        assert transport.calls==[]


def test_next_job_uses_confirmed_id_anchors_and_only_adds_new_bytes(tmp_path):
    source=Source()
    source.pages={('7','null'):ArtworkPage((meta(123),meta(124),meta(125)),'{"offset":3}'),
        ('7','{"offset":3}'):ArtworkPage((meta(126),),exhausted=True)}
    repo=LibraryRepository(tmp_path/'ledger.sqlite3');transport=Transport(*(Response() for _ in range(5)))
    with TaskCoordinator(repo,executor(repo,tmp_path,transport),workers=1) as owner:
        first=submit(owner);service=FollowCollection(owner,source)
        assert service.run(first['batch_id']).coverage_complete
        settled(repo,first)
        source.pages[('7','null')]=ArtworkPage((meta(130),meta(123),meta(124),meta(125)),'{"offset":4}')
        second=submit(owner,'new-job',budget=BatchBudget(max_works=10))
        assert service.run(second['batch_id']).coverage_complete
        result=settled(repo,second)
        assert result['pages']['new_completed']==1 and result['pages']['existing']==3
        assert len([call for call in source.calls if call[0]=='works'])==3
        assert len(transport.calls)==5


def test_missing_incremental_anchors_keep_the_previous_confirmed_watermark(tmp_path):
    source=Source();source.pages[('7','null')]=ArtworkPage((meta(123),meta(124)),exhausted=True)
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    with TaskCoordinator(repo,executor(repo,tmp_path,Transport(*(Response() for _ in range(4)))),workers=1) as owner:
        first=submit(owner);service=FollowCollection(owner,source)
        service.run(first['batch_id']);settled(repo,first)
        source.pages[('7','null')]=ArtworkPage((meta(130),meta(131)),exhausted=True)
        second=submit(owner,'new-job')
        assert service.run(second['batch_id']).coverage_complete is False
        settled(repo,second,'paused')
        next_job=repo.submit(CollectionRequest('next','offline-account',output=OutputSpec(folder_by_artist=False)),'next-submit')
        prior=repo.previous_artist_checkpoint(next_job['job_id'],'follow.history','all','7')
        assert prior['job_id']==first['job_id']


def test_new_works_keeps_both_follow_visibility_windows_separate(tmp_path):
    source=Source()
    source.pages={('public','null'):ArtworkPage((meta(123),),exhausted=True),('private','null'):ArtworkPage((meta(124),),exhausted=True)}
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    with TaskCoordinator(repo,executor(repo,tmp_path,Transport(Response(),Response())),workers=1) as owner:
        job=submit(owner,mode='new')
        report=FollowCollection(owner,source).run(job['batch_id'])
        settled(repo,job)
        assert report.coverage_complete
        assert [call[1] for call in source.calls]==['public','private']
        assert repo.get_checkpoint(job['job_id'],'follow.new','public','feed')['exhausted']
        assert repo.get_checkpoint(job['job_id'],'follow.new','private','feed')['exhausted']


def test_failed_page_only_retry_does_not_query_sources_or_forget_unknown_works(tmp_path):
    source=Source()
    single=meta(123)
    unknown=replace(meta(999),kind='unknown',original_urls=())
    source.pages[('7','null')]=ArtworkPage((single,unknown),exhausted=True)
    source.detail_values={'999':unknown}
    fail=[True];cdn=[]
    def get(url,**kwargs):
        cdn.append(url)
        if fail[0]:raise requests.ReadTimeout('synthetic')
        return Response()
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    with TaskCoordinator(repo,executor(repo,tmp_path,get),workers=1) as owner:
        first=submit(owner);service=FollowCollection(owner,source)
        service.run(first['batch_id']);settled(repo,first,'partial')
        source_calls=len(source.calls);fail[0]=False
        second=resume(owner,first,'retry',retry=True)
        assert service.run(second['batch_id'],retry_failed_only=True).coverage_complete
        result=settled(repo,second,'partial')
        assert len(source.calls)==source_calls and len(cdn)==4
        assert result['pages']['new_completed']==1 and result['works']['metadata_unknown']==1


@pytest.mark.parametrize('parameters',[{'start_ordinal':-1},{'start_ordinal':True},{'artist_ids':[]},{'artist_ids':['7'],'start_ordinal':0}])
def test_invalid_artist_selectors_are_rejected_before_any_network(tmp_path,parameters):
    source=Source();repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    with TaskCoordinator(repo,executor(repo,tmp_path,Transport()),workers=1) as owner:
        job=submit(owner,parameters=parameters)
        with pytest.raises(ValueError):FollowCollection(owner,source).run(job['batch_id'])
        assert source.calls==[]


def test_scope_fingerprint_ignores_ids_and_budgets_but_preserves_semantic_changes():
    base=CollectionRequest('one','account')
    changed=replace(base,request_id='two',budget=BatchBudget(max_works=5,max_requests=2))
    assert scope_fingerprint(base)==scope_fingerprint(changed)
    assert scope_fingerprint(base)!=scope_fingerprint(replace(base,query=QuerySpec(filters=FilterSpec(1,True))))
    assert scope_fingerprint(base)!=scope_fingerprint(replace(base,account_ref='another-account'))


def test_retry_incomplete_artists_only_uses_stable_parent_artist_ids(tmp_path):
    source=Source('7','8')
    incomplete=replace(meta(124,'8'),kind='unknown',original_urls=())
    source.pages={('7','null'):ArtworkPage((meta(123),),exhausted=True),('8','null'):ArtworkPage((incomplete,),exhausted=True)}
    source.detail_values={'124':incomplete}
    repo=LibraryRepository(tmp_path/'ledger.sqlite3');transport=Transport(Response(),Response())
    with TaskCoordinator(repo,executor(repo,tmp_path,transport),workers=1) as owner:
        first=submit(owner);service=FollowCollection(owner,source)
        service.run(first['batch_id']);settled(repo,first,'partial')
        source.pages[('8','null')]=ArtworkPage((meta(124,'8'),),exhausted=True)
        offset=len(source.calls)
        second=submit(owner,'retry-artists',parameters={'incomplete_from_job':first['job_id']})
        assert service.run(second['batch_id']).coverage_complete
        settled(repo,second)
        assert source.calls[offset:]==[('works','8','null','all')]
        assert len(transport.calls)==2


def test_selected_artist_without_remote_user_field_keeps_its_known_identity(tmp_path):
    source=Source();source.pages[('7','null')]=ArtworkPage((replace(meta(123),artist_id='',artist_name=''),),exhausted=True)
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    with TaskCoordinator(repo,executor(repo,tmp_path,Transport(Response())),workers=1) as owner:
        job=submit(owner,parameters={'artist_ids':['7']})
        assert FollowCollection(owner,source).run(job['batch_id']).coverage_complete
        settled(repo,job)
        with sqlite3.connect(repo.database_path) as conn:
            assert conn.execute('SELECT artist_id FROM artifacts').fetchone()[0]=='7'
        assert not any(call[0]=='following' for call in source.calls)


def test_uncertain_refresh_can_process_known_artists_without_claiming_full_coverage(tmp_path):
    from core.follow_contracts import FollowingPage,FollowArtist
    source=Source('7','8')
    source.pages={('7','null'):ArtworkPage((meta(123),),exhausted=True),('8','null'):ArtworkPage((meta(124,'8'),),exhausted=True)}
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    with TaskCoordinator(repo,executor(repo,tmp_path,Transport(Response(),Response())),workers=1) as owner:
        first=submit(owner);service=FollowCollection(owner,source)
        service.run(first['batch_id']);settled(repo,first)
        def uncertain(cursor):
            source.calls.append(('following',cursor))
            return FollowingPage((FollowArtist('8','renamed'),),exhausted=True,expected_total=3)
        source.following=uncertain
        offset=len(source.calls)
        second=submit(owner,'uncertain-refresh')
        report=service.run(second['batch_id'],refresh_following=True)
        result=settled(repo,second,'paused')
        assert report.coverage_complete is False and result['pages']['existing']==2
        assert [call[1] for call in source.calls[offset:] if call[0]=='works']==['7','8']
        assert len(repo.follow_artists('offline-account'))==2
