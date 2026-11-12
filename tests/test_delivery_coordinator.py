"""P3 lifecycle through the actual coordinator, ledger, executor and files."""
from dataclasses import replace
import threading
import time

import pytest
import requests

from core.library_repository import LibraryRepository
from core.task_contracts import CandidateDecision, CollectionRequest, OutputSpec, PageSpec, PathRef, SourcePage
from core.task_coordinator import DeliveryOutcome, TaskCoordinator
from delivery_fixtures import Response, Transport, executor, make_job, run
from image_fixtures import PNG_BYTES
from test_page_delivery import SimulatedCrash, assert_completed, final_path


def wait_for(check,timeout=8):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        result=check()
        if result:return result
        time.sleep(0.01)
    pytest.fail('Coordinator did not reach the expected state within the bounded wait')


def submit_source(coordinator,count=1,decision='download'):
    request=CollectionRequest('coordinator-test','offline-account',output=OutputSpec(folder_by_artist=False))
    job=coordinator.submit(request,'submit-test').result(5)
    coordinator.run_queued().result(5)
    wait_for(lambda:coordinator.active_batch_id==job['batch_id'])
    pages=tuple(PageSpec(i,PathRef(relative_path=f'downl_Pixiv/123_p{i}.png'),f'https://i.pximg.net/img-original/2026/123_p{i}.png') for i in range(count))
    candidate=CandidateDecision('123',decision,artist_id='7',page_count=count,pages=pages if decision=='download' else ())
    ids=coordinator.accept_page(job['batch_id'],SourcePage('follow','history','7','source-1',(candidate,),exhausted=True)).result(5)
    return job,ids


def finish_and_wait(coordinator,repo,batch_id,*,complete=True,expected='completed'):
    coordinator.finish_source(batch_id,coverage_complete=complete).result(5)
    wait_for(lambda:repo.get_batch(batch_id)['phase']==expected)
    return repo.batch_results(batch_id)


def test_full_multipage_source_settles_exactly_once_and_closes_threads(tmp_path):
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    transport=Transport(Response(),Response(),Response())
    owner=TaskCoordinator(repo,executor(repo,tmp_path,transport),workers=1,queue_capacity=2)
    with owner:
        job,ids=submit_source(owner,3)
        results=finish_and_wait(owner,repo,job['batch_id'])
        assert results['pages']['new_completed']==3
        assert results['works']['completed']==1
        assert len(transport.calls)==3
        for index,delivery in enumerate(ids):assert_completed(repo,delivery,final_path(tmp_path,page=index))
    assert not owner._owner.is_alive()
    assert all(not thread.is_alive() for thread in owner._workers)


def test_partial_work_and_explicit_failed_only_retry_preserve_completed_pages(tmp_path):
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    calls=[];failing=[True]
    def get(url,**kwargs):
        calls.append(url)
        if failing[0] and '_p1.' in url:raise requests.ReadTimeout('synthetic')
        return Response()
    with TaskCoordinator(repo,executor(repo,tmp_path,get),workers=1) as owner:
        job,ids=submit_source(owner,3)
        results=finish_and_wait(owner,repo,job['batch_id'],expected='partial')
        assert results['pages']['completed']==2 and results['pages']['failed']==1
        assert results['works']['partial']==1
        assert sum('_p1.' in url for url in calls)==3
        failing[0]=False
        resumed=owner.resume(job['job_id'],'retry-failed',retry_failed=True).result(5)
        owner.run_queued().result(5)
        wait_for(lambda:owner.active_batch_id==resumed['batch_id'])
        second=finish_and_wait(owner,repo,resumed['batch_id'])
        assert second['pages']['new_completed']==1 and second['pages']['existing']==2
        assert second['works']['completed']==1
        assert sum('_p0.' in url for url in calls)==sum('_p2.' in url for url in calls)==1
        assert sum('_p1.' in url for url in calls)==4
        for index,delivery in enumerate(ids):assert_completed(repo,delivery,final_path(tmp_path,page=index))


def test_incomplete_source_coverage_cannot_claim_collection_completed(tmp_path):
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    with TaskCoordinator(repo,executor(repo,tmp_path,Transport(Response())),workers=1) as owner:
        job,_=submit_source(owner)
        results=finish_and_wait(owner,repo,job['batch_id'],complete=False,expected='paused')
        assert results['pages']['completed']==1


def test_unknown_metadata_is_partial_without_any_http(tmp_path):
    repo=LibraryRepository(tmp_path/'ledger.sqlite3');transport=Transport()
    with TaskCoordinator(repo,executor(repo,tmp_path,transport),workers=1) as owner:
        job,_=submit_source(owner,decision='metadata_unknown')
        finish_and_wait(owner,repo,job['batch_id'],expected='partial')
        assert transport.calls==[]


def test_executor_claim_of_success_requires_a_durable_artifact(tmp_path):
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    with TaskCoordinator(repo,lambda row,stop:DeliveryOutcome('completed'),workers=1) as owner:
        job,ids=submit_source(owner)
        results=finish_and_wait(owner,repo,job['batch_id'],expected='partial')
        assert repo.get_delivery(ids[0])['error_code']=='uncommitted_result'
        assert repo.get_artifact(ids[0]) is None
        assert results['pages']['new_completed']==0
        assert not final_path(tmp_path).exists()


def test_startup_recovers_prepared_locally_but_never_dispatches_pending(tmp_path):
    database=tmp_path/'ledger.sqlite3'
    with LibraryRepository(database) as first:
        job,ids,_=make_job(first,count=2)
        def crash(point,row):
            if point=='after_prepare':raise SimulatedCrash()
        with pytest.raises(SimulatedCrash):run(first,job,ids[0],executor(first,tmp_path,Transport(Response()),fault_hook=crash))
    repo=LibraryRepository(database);transport=Transport()
    with TaskCoordinator(repo,executor(repo,tmp_path,transport),workers=1) as owner:
        assert_completed(repo,ids[0],final_path(tmp_path))
        assert repo.get_artifact(ids[1]) is None
        assert transport.calls==[]
        assert owner.active_batch_id is None


@pytest.mark.parametrize('cancel',[False,True])
def test_pause_or_cancel_in_flight_persists_only_owned_partial_bytes(tmp_path,cancel):
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    entered=threading.Event();release=threading.Event()
    def hold():
        entered.set()
        assert release.wait(5)
    transport=Transport(Response(headers={'Content-Type':'image/png','Content-Length':str(len(PNG_BYTES)),'ETag':'"v1"'},chunks=[PNG_BYTES[:20],hold,PNG_BYTES[20:]]))
    with TaskCoordinator(repo,executor(repo,tmp_path,transport),workers=1) as owner:
        job,ids=submit_source(owner)
        assert entered.wait(5)
        try:
            command=owner.cancel(job['job_id'],'cancel-test') if cancel else owner.pause(job['job_id'],'pause-test')
            command.result(5)
        finally:
            release.set()
        phase='cancelled' if cancel else 'paused'
        wait_for(lambda:repo.get_batch(job['batch_id'])['phase']==phase)
        row=repo.get_delivery(ids[0])
        assert row['checkpoint']['verified_bytes']==20
        assert repo.get_artifact(ids[0]) is None
        assert not final_path(tmp_path).exists()
        assert len(transport.calls)==1
