from concurrent.futures import CancelledError
import threading
import time

import pytest

import core.http_client as http_module
from core.app_paths import AppPaths
from core.collection_runtime import CollectionRuntime,RuntimeClosed,RuntimeQueueFull
from core.follow_contracts import ArtworkPage
from core.library_paths import PathResolver
from core.library_queries import LibraryQuery
from core.library_repository import LibraryRepository
from core.task_contracts import CollectionRequest,OutputSpec,PathRef
from core.task_coordinator import DeliveryOutcome,TaskCoordinator
from delivery_fixtures import Response,Transport,executor
from test_delivery_coordinator import submit_source,wait_for
from test_follow_collection import Source,meta


def test_runtime_creates_no_factory_io_until_explicit_dispatch_and_closes_on_owner(tmp_path):
    source=Source();source.pages[('7','null')]=ArtworkPage((meta(123),),exhausted=True)
    factories=[];closed=[]
    def factory(request,stop):
        factories.append(threading.current_thread().name)
        source.close=lambda:closed.append(threading.current_thread().name)
        return source
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    owner=TaskCoordinator(repo,executor(repo,tmp_path,Transport(Response())),workers=1)
    runtime=CollectionRuntime(owner,factory)
    assert factories==[] and runtime.status()['threads_alive']==0
    with runtime:
        assert factories==[]
        job=runtime.submit(CollectionRequest('one','offline-account',output=OutputSpec(folder_by_artist=False)),'one').result(5)
        time.sleep(0.05)
        assert factories==[]
        runtime.run_queued().result(5)
        wait_for(lambda:repo.get_batch(job['batch_id'])['phase']=='completed')
        assert runtime.source_report(job['batch_id']).coverage_complete
        query=runtime.fetch(LibraryQuery(batch_id=job['batch_id'])).result(5)
        assert query.rows[0]['new_completed']==1
    assert runtime.status()['closed'] and runtime.status()['threads_alive']==0
    assert factories==closed and factories[0].startswith('collection-source-')
    runtime.close()


def test_page_workers_dispose_only_their_owned_http_sessions_without_waiting_for_gc(tmp_path,monkeypatch):
    sessions=[]
    class Session:
        def __init__(self):self.closed=False;sessions.append(self)
        def mount(self,*args):pass
        def close(self):self.closed=True
    client=http_module.HttpClient()
    monkeypatch.setattr(http_module,'_DEFAULT_CLIENT',client)
    monkeypatch.setattr(http_module.requests,'Session',Session)
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    def consume(row,stop):
        client._session()
        return DeliveryOutcome('failed','synthetic')
    with TaskCoordinator(repo,consume,workers=1) as owner:
        job,_=submit_source(owner)
        owner.finish_source(job['batch_id'],coverage_complete=True).result(5)
        wait_for(lambda:repo.get_batch(job['batch_id'])['phase']=='partial')
        assert len(sessions)==1 and not sessions[0].closed
    assert sessions[0].closed and len(client._sessions)==0


def test_short_queries_remain_available_while_a_separate_export_owner_is_busy(tmp_path,monkeypatch):
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    owner=TaskCoordinator(repo,lambda row,stop:DeliveryOutcome('failed'),workers=1)
    entered=threading.Event();release=threading.Event()
    runtime=CollectionRuntime(owner)
    def long_export(*args,**kwargs):
        entered.set();assert release.wait(5);return 'exported'
    monkeypatch.setattr(runtime.queries,'export_markdown',long_export)
    with runtime:
        future=runtime.export_markdown(LibraryQuery(kind='history'),PathRef(relative_path='reports/output.md'),PathResolver(AppPaths(tmp_path)))
        assert entered.wait(5)
        try:
            assert runtime.fetch(LibraryQuery(kind='history')).result(1).rows==()
        finally:release.set()
        assert future.result(5)=='exported'


def test_full_query_queue_rejects_immediately_and_close_keeps_repository_open_until_owner_exits(tmp_path,monkeypatch):
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    runtime=CollectionRuntime(TaskCoordinator(repo,lambda row,stop:DeliveryOutcome('failed'),workers=1),query_capacity=1)
    entered=threading.Event();release=threading.Event()
    original=runtime.queries.fetch
    def hold(*args,**kwargs):
        entered.set();assert release.wait(5);return original(*args,**kwargs)
    monkeypatch.setattr(runtime.queries,'fetch',hold)
    runtime.start().result(5)
    active=runtime.fetch(LibraryQuery(kind='history'));assert entered.wait(5)
    queued=runtime.fetch(LibraryQuery(kind='history'))
    before=time.monotonic();rejected=runtime.fetch(LibraryQuery(kind='history'))
    with pytest.raises(RuntimeQueueFull):rejected.result(0.1)
    assert time.monotonic()-before<0.1
    try:
        with pytest.raises(TimeoutError):runtime.close(timeout=0.05)
        assert repo._closed is False and runtime.status()['closed'] is False
        with pytest.raises(RuntimeClosed):queued.result(1)
    finally:
        release.set();active.result(5);runtime.close()
    assert repo._closed and runtime.status()['closed']


def test_cancel_export_cancels_queued_work_and_signals_the_active_snapshot(tmp_path,monkeypatch):
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    runtime=CollectionRuntime(TaskCoordinator(repo,lambda row,stop:DeliveryOutcome('failed'),workers=1))
    entered=threading.Event();observed=[]
    def cancellable(*args,stop_event=None,**kwargs):
        entered.set()
        end=time.monotonic()+5
        while not stop_event.is_set() and time.monotonic()<end:time.sleep(0.005)
        assert stop_event.is_set();observed.append(True)
        raise RuntimeError('synthetic_export_cancelled')
    monkeypatch.setattr(runtime.queries,'export_markdown',cancellable)
    with runtime:
        args=(LibraryQuery(kind='history'),PathRef(relative_path='reports/output.md'),PathResolver(AppPaths(tmp_path)))
        active=runtime.export_markdown(*args);assert entered.wait(5)
        queued=runtime.export_markdown(*args)
        runtime.cancel_export()
        with pytest.raises(CancelledError):queued.result(1)
        with pytest.raises(RuntimeError,match='synthetic_export_cancelled'):active.result(5)
        assert observed==[True]


def test_source_factory_error_blocks_once_and_all_runtime_owners_can_close(tmp_path):
    calls=[]
    def broken(*args):calls.append(True);raise ValueError('synthetic-private-value')
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    runtime=CollectionRuntime(TaskCoordinator(repo,lambda row,stop:DeliveryOutcome('failed'),workers=1),broken)
    with runtime:
        job=runtime.submit(CollectionRequest('one','offline-account'),'one').result(5)
        runtime.run_queued().result(5)
        wait_for(lambda:repo.get_batch(job['batch_id'])['phase']=='blocked')
        assert calls==[True] and runtime.status()['source_error']=='source_runner_failed'
        assert 'synthetic-private-value' not in repr(runtime.status())
    assert runtime.status()['threads_alive']==0
