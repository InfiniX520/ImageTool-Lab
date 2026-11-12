"""Release gaps: real account update boundary and saved filename behavior."""
from dataclasses import replace
import json
import os
import threading

import pytest

from core.app_paths import AppPaths
from core.authorization_settings import AuthorizationSettingsError, save_authorization
from core.candidate_filter import make_candidate, normalize_artwork, merge_details
from core.collection_desktop import DesktopSession, AccountError
from core.collection_settings import CollectionSettings, SettingsError, load_settings, save_settings
from core.library_paths import PathResolver
from core.page_planner import plan_original_pages
from core.source_settings import source_request
from core.task_contracts import OutputSpec, CollectionRequest
from gui.collection_app import DemoSession
from gui.collection_controller import CollectionController
from gui.collection_dialogs import NativeDialogs
from test_collection_controller import Root, View, drive


def test_authorization_update_is_local_preserves_other_settings_and_clears_identity(tmp_path):
    path=tmp_path/'pixiv_config.json'
    path.write_text(json.dumps({'token':'old-synthetic-token','count':'5120','unrelated':{'a':1}}),encoding='utf-8')
    calls=[]
    with DesktopSession(AppPaths(tmp_path),api_factory=lambda **kwargs:calls.append('network')) as session:
        session.identity={'account_id':'old-account','premium':True}
        result=session.update_authorization('new-synthetic-token').result(5)
        assert result=={'saved':True} and session.identity is None and calls==[]
        saved=json.loads(path.read_text(encoding='utf-8'))
        assert saved=={'token':'new-synthetic-token','count':'5120','unrelated':{'a':1}}
        assert 'synthetic-token' not in repr(session.log.snapshot())
    assert not tuple(tmp_path.glob('.authorization-*.tmp'))


@pytest.mark.parametrize('content',[b'broken',b'[]',b' '*65537],ids=['invalid-json','wrong-type','oversize'])
def test_malformed_authorization_file_is_not_overwritten(tmp_path,content):
    path=tmp_path/'pixiv_config.json';path.write_bytes(content)
    with pytest.raises(AuthorizationSettingsError,match='authorization_config_invalid'):
        save_authorization(PathResolver(AppPaths(tmp_path)),'synthetic-valid-token')
    assert path.read_bytes()==content


@pytest.mark.parametrize('credential',['', 'bad\nsynthetic-token', 'x'*4097],ids=['empty','multiline','oversize'])
def test_invalid_token_never_enters_error_message_or_file(tmp_path,credential):
    with pytest.raises(AuthorizationSettingsError) as error:
        save_authorization(PathResolver(AppPaths(tmp_path)),credential)
    assert str(error.value)=='authorization_format'
    assert not (tmp_path/'pixiv_config.json').exists()


def test_failed_authorization_publish_preserves_original(tmp_path,monkeypatch):
    path=tmp_path/'pixiv_config.json';before=b'{"token":"old-synthetic-token"}';path.write_bytes(before)
    def reject(*args):raise OSError('controlled failure')
    monkeypatch.setattr(os,'replace',reject)
    with pytest.raises(OSError):save_authorization(PathResolver(AppPaths(tmp_path)),'new-synthetic-token')
    assert path.read_bytes()==before and not tuple(tmp_path.glob('.authorization-*.tmp'))


def test_demo_cannot_save_credentials(tmp_path):
    with DemoSession(AppPaths(tmp_path)) as session:
        with pytest.raises(AccountError,match='demo_authorization_disabled'):
            session.update_authorization('synthetic-private').result(5)
    assert not (tmp_path/'pixiv_config.json').exists()


def test_authorization_dialog_is_masked_and_does_not_prefill_saved_value(monkeypatch):
    calls=[]
    monkeypatch.setattr('gui.collection_dialogs.simpledialog.askstring',lambda **kwargs:calls.append(kwargs))
    NativeDialogs(object()).authorization()
    assert calls[0]['show']=='•' and 'initialvalue' not in calls[0]


def test_controller_cancel_does_nothing_and_save_runs_on_io_owner(tmp_path):
    class Dialogs:
        value=None
        def authorization(self):return self.value
        def error(self,text):raise AssertionError(text)
    root,view,dialogs=Root(),View(),Dialogs()
    session=DesktopSession(AppPaths(tmp_path));controller=CollectionController(root,view,session,dialogs=dialogs)
    controller.start()
    try:
        drive(root,lambda:controller.runtime is not None)
        controller.handle('update_authorization')
        assert not (tmp_path/'pixiv_config.json').exists()
        owner=threading.get_ident();threads=[];original=session._update_authorization
        def save(token):threads.append(threading.get_ident());return original(token)
        session._update_authorization=save
        dialogs.value='synthetic-new-token'
        controller.handle('update_authorization')
        drive(root,lambda:'update_authorization' not in controller._busy)
        assert threads and threads[0]!=owner
        assert json.loads((tmp_path/'pixiv_config.json').read_text())['token']==dialogs.value
        assert dialogs.value not in repr(view.events)
    finally:
        controller.request_close();drive(root,lambda:root.destroyed)


def test_saved_naming_applies_to_follow_and_other_sources_and_survives_restart(tmp_path):
    settings=CollectionSettings(name_template='{id}_{title}_{user_id}_p{page}_{date}',workers=6,max_works=5120,max_requests=666)
    resolver=PathResolver(AppPaths(tmp_path));save_settings(resolver,settings)
    reopened=load_settings(resolver)
    follow=reopened.request('follow','offline')
    search=source_request({'source':'search','search_word':'sample'},reopened,'search','offline')
    assert follow.output.name_template==search.output.name_template==settings.name_template
    assert reopened.workers==6 and reopened.max_requests==666 and reopened.max_works==5120
    urls=tuple(f'https://i.pximg.net/img-original/2026/10/20/00/00/00/123_p{i}.png' for i in range(2))
    pages=plan_original_pages('123',2,urls,follow.output,artist_id='7',title='标题',publication_date='2026-10-19')
    assert [page.output_ref.relative_path for page in pages]==[
        'downl_Pixiv/7/123_标题_7_p0_20261019.png','downl_Pixiv/7/123_标题_7_p1_20261019.png']


def test_published_date_survives_normalization_and_detail_merge():
    raw={'id':123,'user':{'id':7,'name':'名字'},'type':'illust','page_count':1,'create_date':'2026-10-19T14:00:00+09:00',
         'visible':True,'meta_single_page':{'original_image_url':'https://i.pximg.net/img-original/2026/10/20/00/00/00/123_p0.jpg'}}
    metadata=normalize_artwork(raw)
    assert metadata.publication_date=='2026-10-19'
    detail=replace(metadata,publication_date='')
    candidate=make_candidate(merge_details(metadata,detail),CollectionRequest('naming','offline',output=OutputSpec(name_template='{user}_{id}_p{page}_{date}')))
    assert candidate.pages[0].output_ref.relative_path.endswith('名字_123_p0_20261019.jpg')


def test_long_names_keep_distinct_page_identity_after_truncation():
    output=OutputSpec(name_template='{title}_{user}_{id}_p{page}')
    urls=tuple(f'https://i.pximg.net/img-original/2026/10/20/00/00/00/123_p{i}.png' for i in range(2))
    pages=plan_original_pages('123',2,urls,output,title='题'*80,artist_name='名'*80)
    assert len({page.output_ref.target_key for page in pages})==2
    assert pages[0].output_ref.relative_path.endswith('_123_p0.png')


@pytest.mark.parametrize('template',['../{id}', '{title.__class__}', '{id:1000}', '{unknown}', ''])
def test_invalid_naming_rules_are_rejected_before_save(template):
    with pytest.raises(SettingsError,match='name_template'):CollectionSettings(name_template=template)
