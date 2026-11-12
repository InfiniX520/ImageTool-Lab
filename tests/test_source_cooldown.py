from datetime import datetime,timedelta

import pytest

import core.library_repository as library_module
from core.api_transport import BoundedMetadataSession
from core.candidate_filter import SourceResponseError
from core.follow_collection import FollowCollection
from core.follow_contracts import ArtworkPage
from core.library_repository import LibraryRepository,SourceCooling
from core.pixiv_follow_source import PixivFollowSource
from core.task_contracts import CollectionRequest
from core.task_coordinator import TaskCoordinator
from delivery_fixtures import Response,Transport,executor
from test_api_transport import response
from test_follow_collection import Source,meta,resume,settled,submit


def test_sdk_wrapped_rate_limit_reaches_source_as_safe_code_and_retry_time():
    from pixivpy3 import AppPixivAPI
    api=AppPixivAPI(timeout=(5,15),allow_redirects=False)
    api.requests.close();calls=[]
    def send(*args,**kwargs):
        calls.append(True)
        return response(status=429,headers={'Retry-After':'120','Set-Cookie':'synthetic-private'})
    api.requests=BoundedMetadataSession(send=send)
    api.set_auth('synthetic-private')
    try:
        source=PixivFollowSource(api,account_id='7',account_ref='offline-account')
        with pytest.raises(SourceResponseError) as error:source.artist_works('8')
        assert error.value.code=='metadata_rate_limited'
        assert error.value.retry_after_seconds==120
        assert 'synthetic-private' not in str(error.value) and len(calls)==1
    finally:api.requests.close()


def test_cooldown_survives_new_batch_and_reopen_and_expires_without_network_probe(tmp_path,monkeypatch):
    path=tmp_path/'ledger.sqlite3';source=Source()
    source.pages[('7','null')]=SourceResponseError('metadata_rate_limited',retry_after_seconds=120)
    repo=LibraryRepository(path)
    with TaskCoordinator(repo,executor(repo,tmp_path,Transport()),workers=1) as owner:
        first=submit(owner);pump=FollowCollection(owner,source)
        assert pump.run(first['batch_id']).reason_code=='metadata_rate_limited'
        settled(repo,first,'blocked')
        until=repo.source_cooldown('offline-account')
        assert until is not None and repo.source_cooldown('another-account') is None
    repo=LibraryRepository(path)
    with TaskCoordinator(repo,executor(repo,tmp_path,Transport(Response())),workers=1) as owner:
        again=resume(owner,first,'continue')
        before=len(source.calls)
        pump=FollowCollection(owner,source)
        assert pump.run(again['batch_id']).reason_code=='source_cooling'
        settled(repo,again,'paused')
        assert len(source.calls)==before and repo.source_budget_status(again['batch_id'])['reserved_requests']==0
        after=(datetime.fromisoformat(until)+timedelta(seconds=1)).isoformat(timespec='microseconds')
        monkeypatch.setattr(library_module,'_utcnow',lambda:after)
        assert repo.source_cooldown('offline-account') is None
        source.pages[('7','null')]=ArtworkPage((meta(123),),exhausted=True)
        last=resume(owner,first,'continue-after-time')
        assert pump.run(last['batch_id']).coverage_complete
        settled(repo,last)


def test_late_response_cooldown_keeps_explicit_cancellation_and_is_account_scoped(tmp_path):
    with LibraryRepository(tmp_path/'ledger.sqlite3') as repo:
        job=repo.submit(CollectionRequest('first','account'),'first');repo.activate_next()
        repo.request_stop(job['job_id'],'cancel',cancel=True);repo.settle_stop(job['batch_id'])
        repo.record_source_cooldown(job['batch_id'],30,reason_code='metadata_rate_limited')
        assert repo.get_batch(job['batch_id'])['phase']=='cancelled'
        next_job=repo.submit(CollectionRequest('next','account'),'next');repo.activate_next()
        with pytest.raises(SourceCooling):repo.reserve_source_request(next_job['batch_id'],kind='detail')
        assert repo.source_cooldown('different-account') is None
