"""Presentation changes must preserve honest results and clear action scope."""
import tkinter as tk
import pytest
from gui.collection_view import CollectionView

@pytest.fixture
def screen():
    root=tk.Tk();root.withdraw();calls=[]
    view=CollectionView(root,lambda action,payload:calls.append((action,payload)))
    try:yield root,view,calls
    finally:view.dispose();root.destroy()

def test_filtering_out_the_selection_cannot_operate_on_a_hidden_task(screen):
    root,view,calls=screen
    view.set_tasks([{'id':'one','title':'历史作品','state':'收集中'},
                    {'id':'two','title':'新作','state':'已暂停'}])
    view.select_task('one');root.update()
    assert calls==[]
    view.show_page('tasks');view._choose_task_filter('paused')
    assert view._trees['tasks'].get_children()==('two',)
    assert view.selected_id('tasks') is None
    for button in view._task_controls:
        assert 'disabled' in button.state()
        button.invoke()
    root.update()
    assert calls==[]
    view._trees['tasks'].selection_set('two');root.update()
    assert calls==[('select_task',{'id':'two'})]
    view.configure_action('resume',enabled=True)
    view._action_buttons['resume'][0].invoke()
    assert calls[-1]==('resume',{})

def test_counts_are_supplied_facts_and_survive_metadata_refresh(screen):
    root,view,calls=screen
    assert all(label.cget('text')=='—' for label in view._stat_labels.values())
    view.set_summary({'stat_works':0,'stat_pages':17,'stat_pending':None})
    assert [str(label.cget('text')) for label in view._stat_labels.values()]==['0','17','—']
    rows=[{'id':'one','title':'历史','state':'收集中','counts':'查看本批名单'}]
    view.set_tasks(rows);view.select_task('one')
    view.set_task_progress('one','12 个作品 · 17 张')
    view.set_tasks(rows)
    assert view._trees['tasks'].set('one','counts')=='12 个作品 · 17 张'
    assert view.selected_id('tasks')=='one'
    assert calls==[]


def test_task_details_are_bound_to_the_selected_task_and_never_replace_record_details(screen):
    root,view,calls=screen
    view.set_tasks([{'id':'one','title':'历史','state':'收集中'},
                    {'id':'two','title':'新作','state':'已暂停'}])
    view.set_task_detail('one','第一项正在下载')
    view.select_task('one')
    assert '第一项' in view._task_detail_text.get('1.0','end-1c')
    view.select_task('two')
    assert '第一项' not in view._task_detail_text.get('1.0','end-1c')
    assert '正在读取' in view._task_detail_text.get('1.0','end-1c')
    view.show_page('records');view.set_detail('选中的作品详情')
    view.set_task_detail('two','第二项已经暂停')
    assert view._detail_text.get('1.0','end-1c')=='选中的作品详情'
    view.show_page('tasks')
    assert '第二项' in view._task_detail_text.get('1.0','end-1c')


def test_new_works_mode_labels_match_the_command_and_artist_scope_is_visible(screen):
    root,view,calls=screen
    view._follow_mode_new.invoke()
    assert '新作' in view._start_button.cget('text')
    assert '新作' in view._hero_mode.cget('text')
    view.set_summary({'stat_works':2})
    assert '新作' in view._start_button.cget('text')
    view.set_follow_form({'artist_ids':'101'})
    assert view._follow_more.get()
    view._follow_mode_history.invoke()
    assert '补全' in view._start_button.cget('text')
    assert view.get_follow_form()['artist_ids']=='101'
    assert '仍然生效' in view._status_label.cget('text')
