"""Spacing is shared by owners, cancellation wins before transport begins."""
import threading
import time

import pytest

from core.api_transport import BoundedMetadataSession,MetadataRequestError
from core.app_paths import AppPaths
from core.collection_desktop import DesktopSession,DesktopSourceFactory
from core.collection_settings import CollectionSettings,SettingsError
from core.request_pacing import RequestPacer
from core.page_delivery import PageDeliveryExecutor
from test_api_transport import response,URL
from delivery_fixtures import make_job,run,Transport,Response
from core.library_repository import LibraryRepository
from core.library_paths import PathResolver


class Clock:
    now=100.0
    def __call__(self):return self.now
    def sleep(self,seconds):self.now+=seconds


def test_shared_metadata_pacer_survives_session_recreation():
    clock=Clock();pacer=RequestPacer(.5,clock=clock,sleep=clock.sleep);calls=[]
    def send(*args,**kwargs):calls.append(clock.now);return response()
    for _ in range(3):
        with BoundedMetadataSession(send=send,before_request=pacer) as session:
            session.get(URL)
    assert calls==[100.0,100.5,101.0]


def test_cancellation_in_spacing_never_sends_and_does_not_reserve_future_slot():
    clock=Clock();stop=threading.Event();calls=[]
    def sleep(seconds):clock.sleep(seconds);stop.set()
    pacer=RequestPacer(60,clock=clock,sleep=sleep)
    assert pacer(stop)
    with BoundedMetadataSession(send=lambda *a,**kw:calls.append('send'),before_request=pacer,stop_event=stop) as session:
        with pytest.raises(MetadataRequestError,match='metadata_stopped'):session.get(URL)
    assert calls==[] and clock.now<100.1
    stop.clear();clock.now=160.0
    assert pacer(stop) and clock.now==160.0


def test_pacer_serializes_simultaneous_owners_without_extra_threads():
    pacer=RequestPacer(.04);barrier=threading.Barrier(4);times=[];guard=threading.Lock()
    def caller():
        barrier.wait();assert pacer()
        with guard:times.append(time.monotonic())
    workers=[threading.Thread(target=caller) for _ in range(4)]
    for worker in workers:worker.start()
    for worker in workers:worker.join(3);assert not worker.is_alive()
    assert len(times)==4 and times[-1]-times[0]>=.10


def test_original_delivery_can_pause_while_waiting_without_consuming_attempt(tmp_path):
    stop=threading.Event();transport=Transport(Response())
    with LibraryRepository(tmp_path/'library.sqlite3') as repo:
        job,ids,pages=make_job(repo)
        def gate(token):stop.set();return False
        execute=PageDeliveryExecutor(repo,PathResolver(AppPaths(tmp_path)),get=transport,before_request=gate)
        result=run(repo,job,ids[0],execute,stop=stop)
        assert result.state=='suspended' and transport.calls==[]
        row=repo.get_delivery(ids[0])
        # Claiming a worker is recorded; no HTTP attempt was started.
        assert row['attempts']==1 and row['http_attempts']==0


def test_desktop_uses_one_api_pacer_for_verification_and_source(tmp_path):
    with DesktopSession(AppPaths(tmp_path)) as session:
        factory=session.runtime.source_factory
        assert isinstance(factory,DesktopSourceFactory)
        assert factory.before_request is session._api_pacer
        assert session._api_pacer.interval==session.settings.api_interval


@pytest.mark.parametrize('value',['NaN','inf',-1,61,True])
def test_invalid_intervals_do_not_silently_fall_back(value):
    with pytest.raises(SettingsError,match='api_interval'):CollectionSettings(api_interval=value)
    with pytest.raises(SettingsError,match='image_interval'):CollectionSettings(image_interval=value)
