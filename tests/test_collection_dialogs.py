import json
import threading
from types import SimpleNamespace
from concurrent.futures import Future

import pytest

from core.app_paths import AppPaths
from core.collection_settings import CollectionSettings, load_settings
from core.library_queries import LibraryQuery
from gui.collection_app import DemoSession
from gui.collection_controller import CollectionController
from gui.collection_dialogs import NativeDialogs
from test_collection_controller import Root, View, Dialogs, drive


class FormView(View):
    def __init__(self):
        super().__init__();self.settings_form=CollectionSettings().form()
    def set_follow_form(self,values):self.form.update(values)
    def set_settings_form(self,values):self.settings_form.update(values)
    def get_settings_form(self):return dict(self.settings_form)


@pytest.fixture
def desktop(tmp_path):
    root,view,dialogs=Root(),FormView(),Dialogs()
    session=DemoSession(AppPaths(tmp_path/'project'))
    controller=CollectionController(root,view,session,dialogs=dialogs)
    controller.start();drive(root,lambda:controller.runtime is not None)
    try:yield controller,root,view,session,dialogs
    finally:
        controller.request_close();drive(root,lambda:root.destroyed)


def test_cancel_folder_picker_has_no_config_or_task_side_effect(desktop):
    controller,root,view,session,dialogs=desktop
    before=view.get_follow_form()
    controller.handle('browse_output')
    drive(root,lambda:'browse_output' not in controller._busy)
    assert dialogs.calls[0][0]=='directory'
    assert view.get_follow_form()==before
    assert not (session.paths.root/'config').exists()
    assert session.runtime.overview().result(3)['rows']==()


def test_selected_external_directory_drives_real_delivery_and_survives_restart(desktop,tmp_path):
    controller,root,view,session,dialogs=desktop
    folder=tmp_path/'用户选中的图片';folder.mkdir();dialogs.directory=str(folder)
    owner=threading.get_ident();calls=[]
    original=session.locations.select_directory
    def select(path):
        calls.append(threading.get_ident());return original(path)
    session.locations.select_directory=select
    controller.handle('browse_output')
    drive(root,lambda:'browse_output' not in controller._busy)
    assert calls and calls[0]!=owner
    assert view.form['output_kind']=='external'
    assert view.form['output_display']==str(folder)
    controller.handle('start')
    drive(root,lambda:controller._current_batch is not None)
    batch=controller._current_batch
    drive(root,lambda:session.runtime.repository.get_batch(batch)['phase']=='completed')
    assert len(tuple(folder.rglob('*.png')))==12
    assert not (session.paths.root/'downl_Pixiv').exists()
    request=session.runtime.repository.get_request(controller._current_job)
    assert request.output.root_ref.kind=='external'
    assert str(folder) not in request.to_json()
    assert load_settings(session.resolver).output_ref==request.output.root_ref
    assert session.resolver.resolve(request.output.root_ref)==folder


def test_settings_picker_changes_only_draft_until_saved(desktop,tmp_path):
    controller,root,view,session,dialogs=desktop
    folder=tmp_path/'设置位置';folder.mkdir();dialogs.directory=str(folder)
    old=view.form['output_relative']
    controller.handle('browse_output',{'scope':'settings'})
    drive(root,lambda:'browse_output' not in controller._busy)
    assert view.settings_form['output_kind']=='external'
    assert view.form['output_relative']==old and view.form['output_kind']=='project'
    assert session.settings.output_kind=='project'
    controller.handle('save_settings')
    drive(root,lambda:'save_settings' not in controller._busy)
    assert view.form['output_root_id']==view.settings_form['output_root_id']
    assert session.settings.output_kind=='external'


def test_open_folder_uses_selected_draft_on_settings_and_original_task_on_task_page(desktop,tmp_path):
    controller,root,view,session,dialogs=desktop
    folder=tmp_path/'新位置';folder.mkdir();dialogs.directory=str(folder)
    opened=[];session.opener=lambda path:opened.append(path)
    controller.handle('browse_output',{'scope':'settings'})
    drive(root,lambda:'browse_output' not in controller._busy)
    view.settings_form['workers']='invalid, unrelated to opening a folder'
    controller.handle('open_output',{'scope':'settings'})
    drive(root,lambda:'open_output' not in controller._pending)
    assert opened==[str(folder)] and session.settings.output_kind=='project'
    old=session.paths.root/'downl_Pixiv';old.mkdir()
    controller._current_job='original'
    controller._requests['original']=CollectionSettings().request('original','offline')
    controller.handle('navigate',{'page':'tasks'})
    controller.handle('open_output')
    drive(root,lambda:'open_output' not in controller._pending)
    assert opened[-1]==str(old)


def test_save_as_cancel_does_not_start_export_and_chosen_filename_is_used(desktop,tmp_path):
    controller,root,view,session,dialogs=desktop
    controller._record_query=LibraryQuery(kind='history')
    calls=[];real=session.runtime.export_markdown
    def export(*args,**kwargs):calls.append(args);return real(*args,**kwargs)
    session.runtime.export_markdown=export
    controller.handle('export_records')
    drive(root,lambda:'export_records' not in controller._busy)
    assert calls==[]
    assert not (session.paths.root/'exports').exists()
    selected=tmp_path/'我选的名单.md';dialogs.export=str(selected)
    controller.handle('export_records')
    drive(root,lambda:'export_records' not in controller._busy)
    assert len(calls)==1 and selected.is_file()
    assert '下载名单' in selected.read_text(encoding='utf-8')
    assert any(call[0]=='info' and str(selected) in call[-1] for call in dialogs.calls)


def test_log_save_as_exports_current_log_to_selected_location(desktop,tmp_path):
    controller,root,view,session,dialogs=desktop
    session.log.emit('dialog-export-check',code='test.dialog');session.log.flush(3)
    target=tmp_path/'选择的日志.txt';dialogs.export=str(target)
    controller.handle('export_log')
    drive(root,lambda:'export_log' not in controller._busy)
    assert 'dialog-export-check' in target.read_text(encoding='utf-8')


def test_import_merges_ids_and_cancel_does_not_erase_current_form(desktop,tmp_path):
    controller,root,view,session,dialogs=desktop
    view.form['artist_ids']='123';view.form['start_ordinal']='7'
    controller.handle('import_artists')
    drive(root,lambda:'import_artists' not in controller._busy)
    assert view.form['artist_ids']=='123'
    text=tmp_path/'画师名单.txt';text.write_text('123\nhttps://www.pixiv.net/users/456\n',encoding='utf-8')
    dialogs.artists=str(text)
    controller.handle('import_artists')
    drive(root,lambda:'import_artists' not in controller._busy)
    assert view.form['artist_ids']=='123, 456' and view.form['start_ordinal']==''
    assert session.runtime.overview().result(3)['rows']==()


def test_invalid_download_form_shows_a_dialog_before_authorization(desktop):
    controller,root,view,session,dialogs=desktop
    view.form['max_works']='不是数量'
    controller.handle('start')
    assert session.identity is None
    assert session.runtime.overview().result(3)['rows']==()
    assert any(call[0]=='error' and '正整数' in call[1] for call in dialogs.calls)


def test_pause_is_immediate_but_cancel_requires_the_explicit_dialog_choice():
    root,view,dialogs=Root(),FormView(),Dialogs();calls=[]
    controller=CollectionController(root,view,SimpleNamespace(),dialogs=dialogs)
    controller._current_job='selected'
    def command(*args):calls.append(args);return Future()
    controller.runtime=SimpleNamespace(cancel=command,pause=command)
    controller.handle('cancel');assert calls==[]
    dialogs.cancel=True;controller.handle('cancel');assert calls[0][0]=='selected'
    controller.handle('pause');assert len(calls)==2
    controller.dispose()


def test_background_query_errors_do_not_open_repeated_modal_dialogs():
    root,view,dialogs=Root(),FormView(),Dialogs()
    controller=CollectionController(root,view,SimpleNamespace(),dialogs=dialogs)
    future=Future();future.set_exception(OSError('sensitive raw details'))
    controller._watch('overview',future,lambda _:None)
    controller._poll()
    assert dialogs.calls==[]
    assert 'sensitive raw details' not in repr(view.events)
    controller.dispose()


def test_native_dialogs_are_parented_cancel_safe_and_confirm_overwrite(monkeypatch):
    root=object();calls=[]
    def pick(**kwargs):calls.append(kwargs);return ''
    monkeypatch.setattr('gui.collection_dialogs.filedialog.askdirectory',pick)
    monkeypatch.setattr('gui.collection_dialogs.filedialog.asksaveasfilename',pick)
    monkeypatch.setattr('gui.collection_dialogs.filedialog.askopenfilename',pick)
    dialogs=NativeDialogs(root)
    assert dialogs.choose_directory('project')==''
    assert calls[-1]['parent'] is root and calls[-1]['mustexist']
    assert dialogs.choose_export('records','project','名单.md')==''
    assert calls[-1]['confirmoverwrite'] and calls[-1]['defaultextension']=='.md'
    dialogs.choose_artists('project');assert calls[-1]['parent'] is root
