"""User actions through the real runtime, source and single-writer library."""
import json
from types import SimpleNamespace
import pytest

from core.app_paths import AppPaths
from core.collection_settings import CollectionSettings
from gui.collection_app import DemoSession
from gui.collection_controller import CollectionController
from test_collection_controller import Root, View, Dialogs, drive


class SourceView(View):
    def get_source_form(self):return dict(self.form)


@pytest.fixture(autouse=True)
def dialogs(monkeypatch):
    monkeypatch.setattr('gui.collection_controller.NativeDialogs', Dialogs)


@pytest.mark.parametrize('fields, expected_source', [
    ({'source':'search','search_word':'風景','search_sort':'热门排序（会员）'}, 'search'),
    ({'source':'ranking','ranking_start_date':'2026-10-20','ranking_end_date':'2026-10-21'}, 'ranking'),
    ({'source':'artist','source_artist_ids':'101'}, 'artist'),
    ({'source':'recommendations'}, 'recommendations'),
    ({'source':'intersection','include_ranking':True,'include_search':True,'search_word':'風景'}, 'intersection'),
    ({'source':'intersection','include_follow':True,'include_ranking':True,'per_source_limit':'3'}, 'intersection'),
])
def test_start_source_creates_query_and_keeps_combined_filters(tmp_path, fields, expected_source):
    root, view = Root(), SourceView()
    session = DemoSession(AppPaths(tmp_path))
    controller = CollectionController(root, view, session)
    try:
        controller.start(); drive(root, lambda: controller.runtime is not None)
        view.form.update(fields, min_bookmarks_enabled=True, min_bookmarks='1000', exclude_ai=True)
        controller.handle('start_source')
        drive(root, lambda: controller._current_job is not None)
        request = session.runtime.request(controller._current_job).result(5)
        assert request.query.source == expected_source
        assert request.query.filters.min_bookmarks == 1000 and request.query.filters.exclude_ai
        drive(root, lambda: session.runtime.repository.get_batch(controller._current_batch)['phase'] in {'completed','partial','paused'})
        snapshot = session.runtime.snapshot(controller._current_job).result(5)
        assert snapshot['results']['works']['completed'] > 0
        if fields.get('per_source_limit')=='3':
            assert snapshot['stop_reason']=='source_limit_reached'
            controller._snapshot(snapshot)
            assert not view.enabled['resume']
        assert any(e[0]=='show_page' and e[1]==('tasks',) for e in view.events)
        overview=session.runtime.overview().result(5)
        assert len(overview['rows']) == 1
        assert overview['rows'][0]['results']=={'works':snapshot['results']['works'],'pages':snapshot['results']['pages']}
    finally:
        controller.request_close();drive(root, lambda:root.destroyed)
    assert session.runtime.status()['threads_alive'] == 0


def test_invalid_source_form_shows_error_before_save_or_auth():
    calls=[];root,view=Root(),SourceView()
    view.form.update(source='intersection',include_follow=True)
    session=SimpleNamespace(settings=CollectionSettings(),identity=None,
        save=lambda *a:calls.append('save'),verify_account=lambda:calls.append('auth'))
    controller=CollectionController(root,view,session);controller.runtime=SimpleNamespace()
    try:
        controller.handle('start_source')
        assert calls == [] and controller._busy == set()
        assert controller.dialogs.calls[-1][0] == 'error'
    finally:controller.dispose()


def test_overview_reuses_unchanged_revision_and_returns_independent_data(tmp_path, monkeypatch):
    session=DemoSession(AppPaths(tmp_path))
    try:
        session.startup().result(5)
        from core.task_contracts import CollectionRequest
        session.runtime.submit(CollectionRequest('cache','pixiv:7'),'cache-submit').result(5)
        repo=session.runtime.repository;original=repo._results_counts;calls=[]
        def counted(*args):calls.append(1);return original(*args)
        monkeypatch.setattr(repo,'_results_counts',counted)
        first=session.runtime.overview().result(5)
        first['rows'][0]['results']['works']['completed']=9999
        second=session.runtime.overview().result(5)
        assert second['rows'][0]['results']['works']['completed']==0
        assert len(calls)==1
    finally:session.close()


def test_synchronous_save_failure_releases_source_button():
    root,view=Root(),SourceView();view.form.update(source='search',search_word='風景')
    def failed_save(*args):raise OSError('synthetic')
    controller=CollectionController(root,view,SimpleNamespace(settings=CollectionSettings(),identity=None,save=failed_save))
    controller.runtime=SimpleNamespace()
    try:
        controller.handle('start_source')
        assert 'start_source' not in controller._busy and view.enabled['start_source']
    finally:controller.dispose()


def test_completed_finite_scope_does_not_offer_an_endless_continue():
    root,view=Root(),SourceView()
    controller=CollectionController(root,view,SimpleNamespace(settings=CollectionSettings(),identity=None))
    controller._current_job='job';controller._current_batch='batch'
    try:
        controller._snapshot({'job_id':'job','batch_id':'batch','phase':'paused',
            'stop_reason':'source_limit_reached','results':{'works':{'completed':2},'pages':{'completed':3}}})
        assert not view.enabled['resume']
        summary=next(e[1][0] for e in reversed(view.events) if e[0]=='set_summary')
        assert summary['title']=='本次范围内的筛选已结束'
        assert '扩大候选数量' in summary['counts']
    finally:controller.dispose()
