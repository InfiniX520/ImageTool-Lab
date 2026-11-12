from concurrent.futures import Future
from types import SimpleNamespace
import threading
import time
from pathlib import Path
import pytest

from core.app_paths import AppPaths
from core.collection_desktop import DesktopSession
from core.collection_settings import CollectionSettings
from core.library_queries import QueryPage
from core.task_contracts import CollectionRequest
from core.task_coordinator import DeliveryOutcome
from gui.collection_controller import CollectionController
from gui.collection_app import DemoSession


class Dialogs:
    def __init__(self, root=None):
        self.calls=[]; self.directory=None; self.export=None; self.artists=None; self.cancel=False
    def choose_directory(self, initialdir):self.calls.append(('directory',initialdir));return self.directory
    def choose_export(self, kind, initialdir, initialfile):
        self.calls.append(('export',kind,initialdir,initialfile));return self.export
    def choose_artists(self, initialdir):self.calls.append(('artists',initialdir));return self.artists
    def error(self, text):self.calls.append(('error',text))
    def info(self, title, text):self.calls.append(('info',title,text))
    def confirm_cancel(self, title):self.calls.append(('cancel',title));return self.cancel


@pytest.fixture(autouse=True)
def dialogs_port(monkeypatch):
    monkeypatch.setattr('gui.collection_controller.NativeDialogs', Dialogs)


class Root:
    def __init__(self):self.callbacks={};self.count=0;self.destroyed=False
    def after(self,delay,callback):
        self.count+=1;self.callbacks[self.count]=callback;return self.count
    def after_cancel(self,identity):self.callbacks.pop(identity,None)
    def destroy(self):self.destroyed=True
    def step(self):
        if self.callbacks:
            key=min(self.callbacks);self.callbacks.pop(key)()


class View:
    def __init__(self):
        self.events=[];self.enabled={};self.form=CollectionSettings().form()
        self.records=[];self.disposed=False;self.thread=threading.get_ident()
        self.record_form={'scope':'history','search':'','result':'all'}
    def __getattr__(self,name):
        if name.startswith(('set_','show_','clear_','apply_')):
            def record(*args,**kwargs):
                assert threading.get_ident()==self.thread
                self.events.append((name,args,kwargs))
                if name=='set_records':self.records=args[0]
            return record
        raise AttributeError(name)
    def configure_action(self,action,*,enabled,label=None):self.enabled[action]=enabled
    def get_follow_form(self):return dict(self.form)
    def get_record_form(self):return dict(self.record_form)
    def get_settings_form(self):return dict(self.form)
    def log_at_bottom(self):return True
    def dispose(self):self.disposed=True


def drive(root,check,timeout=8):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        root.step()
        if check():return
        time.sleep(.005)
    raise AssertionError('Controller did not settle')


def test_startup_and_window_close_are_async_and_leave_no_after_or_live_owners(tmp_path):
    root,view=Root(),View();session=DesktopSession(AppPaths(tmp_path))
    controller=CollectionController(root,view,session)
    controller.start()
    assert len(root.callbacks)==1
    drive(root,lambda:controller.runtime is not None)
    controller.handle('navigate',{'page':'records'})
    drive(root,lambda:any(event[0]=='set_records' for event in view.events))
    controller.request_close()
    drive(root,lambda:root.destroyed)
    assert view.disposed and root.callbacks=={} and session._closed
    assert not session._worker.is_alive() and session.runtime.status()['threads_alive']==0


def test_late_record_query_cannot_replace_a_newer_filter_or_page(tmp_path):
    root,view=Root(),View()
    session=SimpleNamespace(settings=CollectionSettings(),identity=None)
    controller=CollectionController(root,view,session)
    first,second=Future(),Future()
    first.set_running_or_notify_cancel()
    futures=iter((first,second))
    controller.runtime=SimpleNamespace(fetch=lambda *args,**kwargs:next(futures),overview=lambda **kwargs:Future())
    controller._next_overview=float('inf')
    controller._query_records(reset=True)
    view.record_form['search']='new'
    controller._query_records(reset=True)
    second.set_result(QueryPage(({'kind':'history','artifact_id':'new','illust_id':'123','page_index':0,'title':'new'},),None,2))
    controller._schedule(0);root.step()
    first.set_result(QueryPage(({'kind':'history','artifact_id':'old','illust_id':'124','page_index':0,'title':'old'},),None,1))
    root.step()
    assert view.records[0]['title']=='new'
    controller.dispose()
    assert root.callbacks=={}


def test_repeated_start_during_authorization_creates_only_one_command(tmp_path):
    root,view=Root(),View();auth=Future();calls=[]
    saved=Future();saved.set_result(CollectionSettings().form())
    session=SimpleNamespace(settings=CollectionSettings(),identity=None,
        verify_account=lambda:calls.append('auth') or auth,save=lambda preferences:saved)
    controller=CollectionController(root,view,session)
    controller.runtime=SimpleNamespace()
    controller.handle('start');controller.handle('start');controller.handle('start')
    controller._next_overview=float('inf');controller._poll()
    assert calls==['auth'] and view.enabled['start'] is False
    auth.set_exception(ValueError('synthetic failure'))
    controller._next_overview=float('inf');controller._poll()
    assert view.enabled['start']
    controller.dispose()


def test_only_retry_failed_requests_the_failed_page_source_mode(tmp_path):
    root,view=Root(),View();calls=[]
    controller=CollectionController(root,view,SimpleNamespace(settings=CollectionSettings(),identity=None))
    def resume(*args,**kwargs):calls.append(kwargs);return Future()
    controller.runtime=SimpleNamespace(resume=resume)
    controller._current_job='selected-job'
    controller.handle('retry_failed')
    assert calls==[{'retry_failed':True,'failed_pages_only':True}]
    controller.dispose()


def test_controller_demo_completes_original_deliveries_and_opens_a_real_batch_manifest(tmp_path):
    root,view=Root(),View();session=DemoSession(AppPaths(tmp_path))
    controller=CollectionController(root,view,session)
    controller.start();drive(root,lambda:controller.runtime is not None)
    try:
        controller.handle('start')
        drive(root,lambda:controller._current_batch is not None)
        batch=controller._current_batch
        drive(root,lambda:session.runtime.repository.get_batch(batch)['phase']=='completed')
        controller.handle('results')
        drive(root,lambda:len(view.records)==12)
        assert all(value['result']=='已完成' for value in view.records)
        assert len(tuple((tmp_path/'downl_Pixiv').rglob('*.png')))==12
        selected=tmp_path/'chosen-name.md'
        controller.dialogs.export=str(selected)
        controller.handle('export_records')
        drive(root,lambda:'export_records' not in controller._busy)
        assert '下载名单' in selected.read_text(encoding='utf-8')
        assert not (tmp_path/'exports').exists()
    finally:
        controller.request_close();drive(root,lambda:root.destroyed)
    assert session._closed and root.callbacks=={}


def test_batch_manifest_action_resets_history_search_and_uses_the_selected_batch():
    root,view=Root(),View();queries=[]
    view.record_form={'scope':'history','search':'old artist','result':'completed'}
    view.set_record_form=lambda values:view.record_form.update(values)
    controller=CollectionController(root,view,SimpleNamespace(settings=CollectionSettings(),identity=None))
    controller._current_job='job';controller._current_batch='selected-batch'
    controller.runtime=SimpleNamespace(fetch=lambda query,**kwargs:queries.append(query) or Future())
    controller.handle('results')
    assert view.record_form=={'scope':'batch','search':'','result':'all'}
    assert queries[0].batch_id=='selected-batch' and queries[0].kind=='batch'
    controller.dispose()


def test_switching_tasks_disables_old_actions_until_the_new_snapshot_arrives():
    root,view=Root(),View()
    controller=CollectionController(root,view,SimpleNamespace(settings=CollectionSettings(),identity=None))
    controller.runtime=SimpleNamespace(request=lambda job:Future(),snapshot=lambda job:Future())
    controller._current_job='old';controller._current_batch='old-batch'
    controller._task_rows={'new':{'batch_id':'new-batch'}}
    controller._select_task('new')
    assert all(view.enabled[name] is False for name in ('pause','resume','retry_failed','cancel','results'))
    controller._snapshot({'job_id':'old','batch_id':'old-batch','phase':'running'})
    assert view.enabled['pause'] is False
    controller._snapshot({'job_id':'new','batch_id':'new-batch','phase':'paused'})
    assert view.enabled['resume'] and not view.enabled['pause']
    assert [e for e in view.events if e[0]=='set_task_detail'][-1][1][0]=='new'
    controller.dispose()


def test_completed_task_updates_status_once_without_erasing_later_export_notices():
    root,view=Root(),View()
    controller=CollectionController(root,view,SimpleNamespace(settings=CollectionSettings(),identity=None))
    controller._current_job='job';controller._current_batch='batch'
    value={'job_id':'job','batch_id':'batch','phase':'completed','results':{'works':{'completed':2},'pages':{'completed':5}}}
    controller._snapshot(value)
    statuses=[event for event in view.events if event[0]=='set_status']
    assert '已收集完成' in statuses[-1][1][0]
    view.set_status('已导出记录')
    controller._snapshot(dict(value))
    statuses=[event for event in view.events if event[0]=='set_status']
    assert statuses[-1][1][0]=='已导出记录'
    controller.dispose()
