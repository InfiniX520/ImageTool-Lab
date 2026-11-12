import json
import threading
import time

import pytest

from core.app_paths import AppPaths
from core.collection_desktop import AccountError, DesktopSession, DesktopSourceFactory, _authorized_client
from core.library_queries import LibraryQuery
from core.task_contracts import CollectionRequest
from core.task_coordinator import DeliveryOutcome


def test_startup_is_offline_and_all_resources_close_on_their_owners(tmp_path):
    calls=[]
    def no_auth(**kwargs):calls.append(True);raise AssertionError('Unexpected login')
    session=DesktopSession(AppPaths(tmp_path),api_factory=no_auth)
    assert not tmp_path.joinpath('download_logs').exists() and calls==[]
    with session:
        assert session.runtime.fetch(LibraryQuery(kind='history')).result(5).rows==()
        jobs=[]
        for number in range(4):
            jobs.append(session.runtime.submit(CollectionRequest(str(number),'offline-account'),'submit'+str(number)).result(5))
        first=session.runtime.overview(limit=2).result(5)
        second=session.runtime.overview(limit=2,after=first['next_cursor']).result(5)
        assert len(first['rows'])==len(second['rows'])==2 and second['next_cursor'] is None
        assert len({row['job_id'] for row in first['rows']+second['rows']})==4
        assert all(row['query']['mode']=='history' for row in first['rows'])
        assert calls==[]
    assert session._closed and session.runtime.status()['threads_alive']==0
    assert not session._worker.is_alive() and session.log.closed


def test_invalid_preferences_fail_before_a_business_writer_is_created(tmp_path):
    path=tmp_path/'config/collection_v2.json';path.parent.mkdir()
    path.write_bytes(b'invalid json')
    session=DesktopSession(AppPaths(tmp_path))
    try:
        with pytest.raises(ValueError):session.startup().result(5)
    finally:session.close()
    assert not (tmp_path/'download_logs/library.sqlite3').exists() and session.runtime is None


class FakeApi:
    created=[]
    account='7'
    def __init__(self, **kwargs):
        self.kwargs=kwargs
        class Original:
            def close(self):pass
        self.requests=Original();self.created.append(self)
    def auth(self, *, refresh_token):
        assert refresh_token=='synthetic-private'
        self.user_id=self.account
        return {'user':{'is_premium':True}}


def test_existing_authorization_verification_closes_session_and_never_saves_credentials(tmp_path):
    path=tmp_path/'pixiv_config.json';raw=json.dumps({'token':'synthetic-private'}).encode();path.write_bytes(raw)
    with DesktopSession(AppPaths(tmp_path),api_factory=FakeApi) as session:
        identity=session.verify_account().result(5)
        assert identity=={'account_id':'7','premium':True}
        assert FakeApi.created[-1].requests._closed_by_owner
        assert path.read_bytes()==raw
        assert 'synthetic-private' not in repr(session.identity)


def test_source_rejects_changed_account_before_accepting_any_candidates(tmp_path):
    (tmp_path/'pixiv_config.json').write_text(json.dumps({'token':'synthetic-private'}),encoding='utf-8')
    factory=DesktopSourceFactory(AppPaths(tmp_path),FakeApi)
    with pytest.raises(AccountError) as raised:
        factory(CollectionRequest('identity','pixiv:8'),threading.Event())
    assert raised.value.code=='authorization_account_mismatch'
    assert FakeApi.created[-1].requests._closed_by_owner


def test_missing_authorization_reports_only_safe_code_and_is_not_retried(tmp_path):
    calls=[]
    with DesktopSession(AppPaths(tmp_path),api_factory=lambda **kwargs:calls.append(True)) as session:
        with pytest.raises(AccountError) as raised:session.verify_account().result(5)
        assert str(raised.value)=='authorization_failed' and calls==[]


def test_slow_account_verification_does_not_block_short_queries_or_fake_closed(tmp_path):
    (tmp_path/'pixiv_config.json').write_text(json.dumps({'token':'synthetic-private'}),encoding='utf-8')
    entered,release=threading.Event(),threading.Event()
    class SlowApi(FakeApi):
        def auth(self,**kwargs):
            entered.set();assert release.wait(5)
            return super().auth(**kwargs)
    session=DesktopSession(AppPaths(tmp_path),api_factory=SlowApi)
    session.startup().result(5)
    active=session.verify_account();assert entered.wait(5)
    try:
        assert session.runtime.fetch(LibraryQuery(kind='history')).result(1).rows==()
        with pytest.raises(TimeoutError):session.close(timeout=.02)
        assert not session.runtime.repository._closed and not session._closed
    finally:
        release.set();active.result(5);session.close()
    assert session._closed
