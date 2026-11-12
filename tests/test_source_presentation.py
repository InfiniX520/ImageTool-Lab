"""Actual source controls, shared filter drafts and bounded offline examples."""
import tkinter as tk
import pytest

from gui.collection_view import CollectionView
from gui.collection_app import DemoSource
from core.candidate_filter import evaluate_filters
from core.task_contracts import FilterSpec


@pytest.fixture
def ui():
    root=tk.Tk();root.withdraw();calls=[]
    view=CollectionView(root,lambda action,payload:calls.append((action,payload)))
    try:yield root,view,calls
    finally:view.dispose();root.destroy()


def test_shared_conditions_update_both_pages_and_disabled_value_does_not_apply(ui):
    root,view,calls=ui
    view.set_follow_form({'min_bookmarks':'1000','min_bookmarks_enabled':False,'exclude_ai':False})
    panel=view._source_panel
    assert 'disabled' in panel.minimum.state()
    assert '不限制' in view._filter_summary.get()
    view._min_checkbox.invoke()
    view._exclude_ai_checkbox.invoke()
    assert 'disabled' not in panel.minimum.state()
    assert view.get_source_form()['min_bookmarks_enabled'] is True
    assert view.get_source_form()['exclude_ai'] is True
    assert '收藏 ≥ 1000' in view._filter_summary.get()
    assert '未知待核验' in view._filter_summary.get()
    view._min_checkbox.invoke()
    assert '收藏 ≥' not in view._filter_summary.get()
    assert calls==[]


def test_source_switch_keeps_each_draft_and_never_starts_download(ui):
    root,view,calls=ui
    panel=view._source_panel
    view.set_source_form({'search_word':'风景','source_artist_ids':'101,102'})
    panel.tabs['artist'].invoke();panel.tabs['intersection'].invoke()
    assert view.get_source_form()['source']=='intersection'
    assert view.get_source_form()['include_follow'] and view.get_source_form()['include_ranking']
    panel.tabs['search'].invoke()
    assert view.get_source_form()['search_word']=='风景'
    assert view.get_source_form()['source_artist_ids']=='101,102'
    assert calls==[]
    view.show_page('sources');view._action_buttons['browse_output'][-1].invoke()
    assert calls==[('browse_output',{'scope':'sources'})]


def test_related_seed_enabled_only_for_related_recommendations(ui):
    root,view,calls=ui
    panel=view._source_panel
    panel.select('recommendations')
    assert 'disabled' in panel.related_entry.state()
    panel.variables['recommendation_kind'].set('相似作品')
    assert 'disabled' not in panel.related_entry.state()
    assert calls==[]


def test_unknown_metadata_is_selectable_in_batch_records(ui):
    root,view,calls=ui
    view.set_record_form({'scope':'batch','result':'metadata_unknown'})
    assert view.get_record_form()['result']=='metadata_unknown'


def test_demo_filters_produce_distinct_known_unknown_and_filtered_counts():
    source=DemoSource();rows=source.artist_works('101').artworks
    outcomes={}
    for label,spec in [('none',FilterSpec()),('bookmarks',FilterSpec(1000)),('ai',FilterSpec(exclude_ai=True)),('both',FilterSpec(1000,True))]:
        states=[evaluate_filters(spec,bookmarks=row.bookmarks,ai_flag=row.ai_flag).state for row in rows]
        outcomes[label]=(states.count('allow'),states.count('filtered'),states.count('metadata_unknown'))
    assert outcomes=={'none':(4,0,0),'bookmarks':(2,2,0),'ai':(2,1,1),'both':(1,2,1)}
    assert source.details(rows[-1].illust_id).ai_flag is None
