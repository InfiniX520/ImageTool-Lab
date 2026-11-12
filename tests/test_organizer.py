import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from organizer.common import OrganizerError, Budget, groups, identity, safe_path, digest
from organizer.store import Store
from organizer.snapshots import snapshot
from organizer.samples import spread, review, sample, supplement, image_url, download_sample
from organizer.plans import make_plan, apply_plan, reconcile, undo_plan, steps
from organizer.web import WebClient
from core.library_paths import InstanceLock


class FastBudget:
    def __init__(self,limit=1000):
        self.limit=limit;self.reserved=self.observed=self.bytes=0
    def reserve(self,n=1):
        if self.reserved+n>self.limit:
            raise OrganizerError('request_budget_exhausted')
        self.reserved+=n


class Web:
    def __init__(self):
        self.account_id='1';self.budget=FastBudget();self.writes=[];self.failure=None
        self.artists={'10':{'artist_id':'10','visibility':'0','groups':['old']},
                      '20':{'artist_id':'20','visibility':'1','groups':[]}}
        self.published=12
    def catalog(self):
        self.budget.reserve(2)
        return {s:sorted({g for a in self.artists.values() if a['visibility']==v for g in a['groups']}) for s,v in [('show','0'),('hide','1')]}
    def detail(self,artist):
        self.budget.reserve();return copy.deepcopy(self.artists[artist])
    def observe(self,artist):
        return self.detail(artist),self.catalog()
    def following(self,side,offset,tag=None):
        self.budget.reserve()
        rows=[{'artist_id':k,'name':'artist '+k} for k,a in self.artists.items() if a['visibility']==('0' if side=='show' else '1') and (tag is None or tag in a['groups'])]
        catalog=sorted({g for a in self.artists.values() if a['visibility']==('0' if side=='show' else '1') for g in a['groups']})
        return {'total':len(rows),'catalog':catalog,'users':rows[offset:offset+30]}
    def mutate(self,action,artist,tag,before,visibility):
        self.budget.reserve(2)
        assert self.artists[artist]['groups']==before
        if self.failure=='before':
            raise OrganizerError('request_timeout')
        self.writes.append((action,artist,tag))
        current=set(before)
        current.add(tag) if action=='add' else current.remove(tag)
        self.artists[artist]['groups']=sorted(current)
        if self.failure=='after':
            raise OrganizerError('request_timeout')
    def call(self,op,**params):
        self.budget.reserve()
        if op=='works':
            return {'ids':[str(100+i) for i in range(self.published)]}
        if op=='artwork':
            return {'illust_id':params['illust_id'],'artist_id':'10','kind':0,'page_count':20,'date':'2026-01-01','url':'https://i.pximg.net/img-master/a.jpg'}
        if op=='page':
            return {'url':'https://i.pximg.net/img-master/b.jpg'}
        raise AssertionError(op)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path/'organizer.sqlite3') as store:
        yield store


def ready_review(store,groups_value=None):
    value={'id':'r1','account_id':'1','artist_id':'10','needs_more':False,'proposed_groups':groups_value or ['new']}
    store.put('review','r1',value,create_only=True)
    return value


def plan_for(store,web,replace=False):
    ready_review(store)
    return make_plan(store,web,['r1'],replace=replace)


def test_snapshot_records_actual_group_members_and_multiple_memberships(store):
    web=Web()
    web.artists['10']['groups']=['one','two']
    web.artists['11']={'artist_id':'11','visibility':'0','groups':['two']}
    web.artists['20']['groups']=['private']
    data=snapshot(store,web)
    assert data['memberships_complete']
    assert data['artists']['10']['groups']==['one','two']
    assert data['artists']['11']['groups']==['two']
    assert data['artists']['20']['groups']==['private']
    assert data['memberships']['show']['one']['ids']==['10']


@pytest.mark.parametrize('limit',[2,3,4])
def test_membership_snapshot_resumes_after_budget_exhaustion(store,limit):
    web=Web();web.budget=FastBudget(limit)
    with pytest.raises(OrganizerError,match='request_budget_exhausted'):
        snapshot(store,web)
    saved=store.list('snapshot')[0]['data']
    assert saved['status']=='collecting'
    web.budget=FastBudget()
    data=snapshot(store,web,saved['id'])
    assert data['status']=='complete' and data['artists']['10']['groups']==['old']


def test_membership_change_without_catalog_change_stops_resume(store):
    web=Web();web.artists['11']={'artist_id':'11','visibility':'0','groups':[]}
    web.budget=FastBudget(3)
    with pytest.raises(OrganizerError):snapshot(store,web)
    saved=store.list('snapshot')[0]['data']
    web.artists['10']['groups']=[];web.artists['11']['groups']=['old']
    web.budget=FastBudget()
    with pytest.raises(OrganizerError,match='snapshot_changed_start_new'):
        snapshot(store,web,saved['id'])
    assert store.get('snapshot',saved['id'])['status']=='conflict'


def test_operation_listing_exposes_pending_state_without_network(tmp_path):
    from organizer.cli import run,parser
    with Store(tmp_path/'organizer_data/organizer.sqlite3') as saved:
        saved.prepare_operation({'id':'pending','plan_id':'p','account_id':'1','artist_id':'10',
            'action':'add','tag':'n','before':[],'after':['n']})
    result=run(parser().parse_args(['--project-root',str(tmp_path),'list','operation']))
    assert result['items'][0]['status']=='intent'


def test_intent_is_durable_and_immutable(tmp_path):
    path=tmp_path/'state.sqlite3'
    with Store(path) as store:
        op={'id':'op','plan_id':'p','account_id':'1','artist_id':'10','action':'add','tag':'n','before':[],'after':['n']}
        store.prepare_operation(op)
        with pytest.raises(ValueError):store.prepare_operation(op)
    with Store(path) as store:
        assert store.get('operation','op')['state']=='intent'
        store.set_operation_state('op','unknown',code='request_timeout')
        store.set_operation_state('op','confirmed',observed=['n'])
        with pytest.raises(ValueError):store.set_operation_state('op','not_applied')
        with pytest.raises(ValueError):store.put('operation','op',{})


def test_document_and_audit_rollback_together(store):
    store.db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON documents BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(sqlite3.IntegrityError):store.put('plan','p',{})
    assert store.db.execute('SELECT count(*) FROM events').fetchone()[0]==0
    assert store.counts()=={}


@pytest.mark.parametrize('value',[{'x':float('nan')},{'x':float('inf')},{'x':'z'*2100000},[]])
def test_invalid_payload_never_partially_persists(store,value):
    with pytest.raises((ValueError,TypeError)):store.put('plan','bad',value)
    assert store.counts()=={}


def test_create_only_and_copies(store):
    store.put('plan','p',{'x':[1]},create_only=True)
    with pytest.raises(ValueError):store.put('plan','p',{'x':[2]},create_only=True)
    store.get('plan','p')['x'].append(3)
    assert store.get('plan','p')=={'x':[1]}


def test_other_database_refused_without_table_changes(tmp_path):
    path=tmp_path/'other.sqlite3'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE unrelated(x)')
    with pytest.raises(ValueError):Store(path)
    with sqlite3.connect(path) as db:assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[('unrelated',)]


@pytest.mark.parametrize('value',[True,0,'00','1/2','1e8','-1',None])
def test_ids_are_strict(value):
    with pytest.raises(OrganizerError):identity(value)


@pytest.mark.parametrize('value',[['😀'*11],['a','a'],['x\ny'],['x']*6,[],['valid','国风']])
def test_groups_use_website_utf16_limit(value):
    if value in ([],['valid','国风']):assert groups(value)==sorted(value)
    else:
        with pytest.raises(OrganizerError):groups(value)


def test_path_escape_and_real_link_rejected(tmp_path):
    with pytest.raises(OrganizerError):safe_path(tmp_path,'../escape')
    target=tmp_path/'target';target.mkdir()
    link=tmp_path/'linked'
    try:link.symlink_to(target,target_is_directory=True)
    except OSError:
        # Windows machines without link creation permissions still exercise the common path guard.
        return
    with pytest.raises(OrganizerError):Store(link/'db.sqlite3')


def test_inventory_resume_and_both_visibilities(store):
    web=Web();web.artists.update({str(i):{'artist_id':str(i),'visibility':'0','groups':[]} for i in range(100,145)})
    web.budget=FastBudget(1)
    with pytest.raises(OrganizerError):snapshot(store,web)
    saved=store.list('snapshot')[0]['data'];assert len(saved['artists'])==30
    web.budget=FastBudget()
    finished=snapshot(store,web,saved['id'])
    assert finished['status']=='complete' and len(finished['artists'])==47
    assert finished['anchors']['hide']['total']==1
    assert finished['non_atomic'] is True


def test_inventory_reorder_conflict_preserves_old_pages(store):
    web=Web();web.budget=FastBudget(1)
    with pytest.raises(OrganizerError):snapshot(store,web)
    saved=store.list('snapshot')[0]['data']
    web.artists['30']={'artist_id':'30','visibility':'0','groups':[]};web.budget=FastBudget()
    with pytest.raises(OrganizerError,match='snapshot_changed'):snapshot(store,web,saved['id'])
    assert store.get('snapshot',saved['id'])['status']=='conflict'


def test_short_page_is_not_full_inventory(store):
    web=Web();method=web.following
    web.following=lambda side,offset: dict(method(side,offset),total=9)
    with pytest.raises(OrganizerError,match='coverage_invalid'):snapshot(store,web)
    assert store.list('snapshot')[0]['data']['status']=='conflict'


def test_additive_plan_preserves_existing_groups_and_repeated_apply_has_no_writes(store):
    web=Web();plan=plan_for(store,web)
    assert plan['artists'][0]['target']==['new','old']
    assert apply_plan(store,web,plan['id'])['status']=='website_verified'
    assert len(web.writes)==1
    assert apply_plan(store,web,plan['id'])['writes']==0
    assert len(web.writes)==1


@pytest.mark.parametrize('failure,expected',[('before','not_applied'),('after','confirmed')])
def test_timeout_is_reconciled_by_reading_before_any_repeat(store, failure,expected):
    web=Web();plan=plan_for(store,web);web.failure=failure
    with pytest.raises(OrganizerError,match='request_timeout'):apply_plan(store,web,plan['id'])
    assert store.operations(plan['id'])[0]['state']=='unknown'
    previous=len(web.writes);web.failure=None
    result=apply_plan(store,web,plan['id'])
    assert result['writes']==0 and len(web.writes)==previous
    assert store.operations(plan['id'])[0]['state']==expected
    if expected=='not_applied':
        with pytest.raises(OrganizerError,match='create_new'):apply_plan(store,web,plan['id'])
    else:assert apply_plan(store,web,plan['id'])['status']=='website_verified'


def test_crash_after_intent_before_post_has_no_automatic_retry(store):
    web=Web();plan=plan_for(store,web)
    op=steps(plan)[0];op['catalog_before']=web.catalog();store.prepare_operation(op)
    result=reconcile(store,web,plan['id'])
    assert result['reconciled'][0]['state']=='not_applied' and web.writes==[]


def test_write_intent_visible_before_remote_side_effect(store):
    web=Web();plan=plan_for(store,web);original=web.mutate
    def mutate(*args):
        with Store(Path(store.db.execute('PRAGMA database_list').fetchone()[2])) as other:
            assert other.operations(plan['id'])[0]['state']=='intent'
        original(*args)
    web.mutate=mutate
    apply_plan(store,web,plan['id'])


@pytest.mark.parametrize('change',['artist','catalog','visibility','account'])
def test_website_manual_edits_are_not_overwritten(store,change):
    web=Web();plan=plan_for(store,web)
    if change=='artist':web.artists['10']['groups']=['manual']
    if change=='catalog':web.artists['20']['groups']=['other']
    if change=='visibility':web.artists['10']['visibility']='1'
    if change=='account':web.account_id='2'
    with pytest.raises(OrganizerError):apply_plan(store,web,plan['id'])
    assert web.writes==[]


def test_partial_replace_and_compensation_preserve_confirmed_scope(store):
    web=Web();plan=plan_for(store,web,replace=True)
    assert apply_plan(store,web,plan['id'],max_writes=1)['status']=='batch_limit'
    assert web.artists['10']['groups']==[]
    undo=undo_plan(store,web,plan['id'])
    assert undo['artists'][0]['target']==['old']
    apply_plan(store,web,undo['id'])
    assert web.artists['10']['groups']==['old']


def test_undo_refuses_intervening_manual_edit(store):
    web=Web();plan=plan_for(store,web);apply_plan(store,web,plan['id'])
    web.artists['10']['groups']=['manual']
    with pytest.raises(OrganizerError):undo_plan(store,web,plan['id'])


def test_budget_exhaustion_before_intent_does_not_dirty_remote_state(store):
    web=Web();plan=plan_for(store,web);web.budget=FastBudget(7)
    with pytest.raises(OrganizerError,match='budget'):apply_plan(store,web,plan['id'])
    assert store.operations(plan['id'])==[] and web.writes==[]


def fake_image(url,target,budget):
    target.write_bytes(b'image-test')
    return {'sha256':digest(target),'size':10,'width':100,'height':200,'media_type':'image/png'}


def sampled(store,web,tmp_path,count=12):
    snap=snapshot(store,web)
    return sample(store,web,tmp_path,snap['id'],'10',count,downloader=fake_image)


def test_sample_reuses_verified_cache_without_network_images(store,tmp_path):
    web=Web();data=sampled(store,web,tmp_path)
    calls=web.budget.reserved
    result=sample(store,web,tmp_path,data['snapshot_id'],'10',12,data['id'],downloader=lambda *_:pytest.fail('redownload'))
    assert result['status']=='ready_for_review' and web.budget.reserved==calls
    assert store.counts().get('review',0)==0


def test_changed_cache_is_not_accepted_or_overwritten(store,tmp_path):
    web=Web();data=sampled(store,web,tmp_path)
    path=tmp_path/next(iter(data['images'].values()))['relative_path'];path.write_bytes(b'changed')
    with pytest.raises(OrganizerError,match='cache_changed'):
        sample(store,web,tmp_path,data['snapshot_id'],'10',12,data['id'],downloader=fake_image)
    assert path.read_bytes()==b'changed'


def review_payload(data,count=None):
    rows=list(data['images'].values())[:count]
    return {'sample_id':data['id'],'viewed':[{'illust_id':r['illust_id'],'page':r['page'],'sha256':r['sha256']} for r in rows],
        'observer':'test observer','rationale':'Synthetic evidence for fault tests only','coverage_note':'Distinct works across the selected range',
        'rules_version':'test-1','proposed_groups':['new'],'needs_more':False}


def test_visual_review_requires_attestation_and_sufficient_distinct_images(store,tmp_path):
    web=Web();data=sampled(store,web,tmp_path)
    with pytest.raises(OrganizerError,match='attestation'):review(store,tmp_path,review_payload(data),attest_viewed=False)
    with pytest.raises(OrganizerError,match='insufficient'):review(store,tmp_path,review_payload(data,1),attest_viewed=True)
    result=review(store,tmp_path,review_payload(data),attest_viewed=True)
    assert len(result['viewed'])==12


def test_supplementary_pages_do_not_fake_distinct_work_coverage(store,tmp_path):
    web=Web();web.published=100;data=sampled(store,web,tmp_path,1)
    for page in range(1,12):
        data=supplement(store,web,tmp_path,data['id'],data['selected_ids'][0],page,downloader=fake_image)
    assert len(data['images'])==12
    with pytest.raises(OrganizerError,match='insufficient'):review(store,tmp_path,review_payload(data),attest_viewed=True)


def test_few_published_works_can_be_explicitly_reviewed(store,tmp_path):
    web=Web();web.published=3;data=sampled(store,web,tmp_path)
    assert len(review(store,tmp_path,review_payload(data),attest_viewed=True)['viewed'])==3


def test_spread_uses_distinct_works_and_both_ends():
    ids=[str(i) for i in range(100,200)]
    selected=spread(ids,16)
    assert len(set(selected))==16 and selected[0]=='199' and selected[-1]=='100'


@pytest.mark.parametrize('url',['http://i.pximg.net/a','https://evil.test/img-master/a','https://i.pximg.net.evil.test/img-master/a',
    'https://user:secret@i.pximg.net/img-master/a','https://i.pximg.net/img-master/a?token=secret','https://i.pximg.net:444/img-master/a'])
def test_sample_urls_do_not_send_requests_to_unexpected_hosts(url):
    with pytest.raises(OrganizerError):image_url(url)


def test_cli_help_and_status_have_no_data_or_network_side_effects(tmp_path):
    entry=Path(__file__).resolve().parents[1]/'organize.py'
    for arguments in (['--help'],['--project-root',str(tmp_path),'status']):
        result=subprocess.run([sys.executable,'-s','-B','-X','utf8',str(entry),*arguments],cwd=tmp_path,capture_output=True)
        assert result.returncode==0, result.stderr
    assert list(tmp_path.iterdir())==[]


def test_cli_requires_closed_downloader_before_network(tmp_path,capsys):
    from organizer.cli import main
    with InstanceLock(tmp_path/'download_logs/.writer.lock'):
        assert main(['--project-root',str(tmp_path),'connect'])==3
    assert 'close_downloader' in capsys.readouterr().out
    assert not (tmp_path/'organizer_data').exists()


def test_web_account_switch_and_error_text_are_safe():
    class Bridge:
        def execute(self,_):return {'ok':False,'code':'Cookie:SECRET','trace':[]}
    with pytest.raises(OrganizerError) as exc:WebClient(Bridge(),FastBudget(),'1').connect()
    assert 'SECRET' not in str(exc.value)


def test_sample_http_failure_does_not_delete_existing_data(monkeypatch,tmp_path):
    import organizer.samples as module
    calls=[]
    class Response:
        status_code=403
        def __enter__(self):return self
        def __exit__(self,*_):pass
    class Session(Response):
        def get(self,*_,**kwargs):calls.append(kwargs);return Response()
    monkeypatch.setattr(module.requests,'Session',Session)
    target=tmp_path/'a.image';target.write_bytes(b'original')
    with pytest.raises(OrganizerError,match='http_stop'):
        download_sample('https://i.pximg.net/img-master/a.jpg',target,Budget(requests=1))
    assert target.read_bytes()==b'original'
    assert len(calls)==1 and calls[0]['headers']['Referer']=='https://www.pixiv.net/'
    assert 'Cookie' not in calls[0]['headers'] and calls[0]['headers']['User-Agent']
    assert calls[0]['allow_redirects'] is False
