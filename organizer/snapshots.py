"""Resumable, explicit public/private follow inventory."""
import uuid

from organizer.common import OrganizerError, now


def anchor(page):
    return {'total':page['total'],'catalog':page['catalog'],'ids':[u['artist_id'] for u in page['users']]}


def collect_memberships(store, web, data):
    """Enumerate each named group; never infer membership from the tag catalog."""
    partitions=data.setdefault('memberships',{'show':{},'hide':{}})
    for side in ('show','hide'):
        for tag in data['anchors'][side]['catalog']:
            saved=partitions[side].get(tag)
            if saved is not None and 'anchor' in saved and anchor(web.following(side,0,tag))!=saved['anchor']:
                raise OrganizerError('snapshot_changed_start_new')
            if saved is None:
                saved={'offset':0,'ids':[],'complete':False}
                partitions[side][tag]=saved
            while not saved['complete']:
                page=web.following(side,saved['offset'],tag)
                if 'anchor' not in saved:
                    saved['anchor']=anchor(page)
                if (page['total']!=saved['anchor']['total'] or page['catalog']!=data['anchors'][side]['catalog']
                    or saved['offset']+len(page['users'])>page['total'] or (not page['users'] and saved['offset']<page['total'])):
                    raise OrganizerError('snapshot_changed_start_new')
                incoming=[row['artist_id'] for row in page['users']]
                if len(set(incoming))!=len(incoming) or set(incoming)&set(saved['ids']):
                    raise OrganizerError('following_duplicate_or_reordered')
                if any(i not in data['artists'] or data['artists'][i]['visibility']!=('0' if side=='show' else '1') for i in incoming):
                    raise OrganizerError('group_member_outside_snapshot')
                saved['ids'].extend(incoming);saved['offset']+=len(incoming)
                saved['complete']=saved['offset']==page['total']
                store.put('snapshot',data['id'],data)
    for side in ('show','hide'):
        for tag,saved in partitions[side].items():
            if anchor(web.following(side,0,tag))!=saved['anchor']:
                raise OrganizerError('snapshot_changed_start_new')
    for artist in data['artists'].values():
        side='show' if artist['visibility']=='0' else 'hide'
        artist['groups']=sorted(tag for tag,member in partitions[side].items() if artist['artist_id'] in member['ids'])
        if len(artist['groups'])>5:
            raise OrganizerError('snapshot_changed_start_new')
    data['memberships_complete']=True


def snapshot(store, web, snapshot_id=None):
    if snapshot_id:
        data = store.get('snapshot',snapshot_id)
        if data['account_id'] != web.account_id:
            raise OrganizerError('account_mismatch')
        if data['status']=='complete' and data.get('memberships_complete'):
            return data
        if data['status']=='conflict':
            raise OrganizerError('snapshot_changed_start_new')
        for side, initial in data['anchors'].items():
            if anchor(web.following(side,0))!=initial:
                data['status']='conflict'; store.put('snapshot',snapshot_id,data)
                raise OrganizerError('snapshot_changed_start_new')
    else:
        snapshot_id = uuid.uuid4().hex
        data = {'id':snapshot_id,'account_id':web.account_id,'created_at':now(),'status':'collecting',
                'anchors':{},'offsets':{'show':0,'hide':0},'finished':[], 'artists':{},'non_atomic':True}
        store.put('snapshot',snapshot_id,data,create_only=True)
    try:
        for side in ('show','hide'):
            if side in data['finished']:
                continue
            while True:
                offset = data['offsets'][side]
                page = web.following(side,offset)
                if side not in data['anchors']:
                    data['anchors'][side] = anchor(page)
                baseline = data['anchors'][side]
                if page['total']!=baseline['total'] or page['catalog']!=baseline['catalog']:
                    raise OrganizerError('snapshot_changed_start_new')
                rows = page['users']
                if offset+len(rows)>page['total'] or (not rows and offset<page['total']):
                    raise OrganizerError('following_coverage_invalid')
                for row in rows:
                    artist = row['artist_id']
                    if artist in data['artists']:
                        raise OrganizerError('following_duplicate_or_reordered')
                    data['artists'][artist] = dict(row, visibility='0' if side=='show' else '1')
                data['offsets'][side] += len(rows)
                if data['offsets'][side]==page['total']:
                    data['finished'].append(side)
                store.put('snapshot',snapshot_id,data)
                if side in data['finished']:
                    break
        collect_memberships(store,web,data)
        for side in ('show','hide'):
            if anchor(web.following(side,0))!=data['anchors'][side]:
                raise OrganizerError('snapshot_changed_start_new')
        data['status']='complete';data['completed_at']=now();data.pop('stop_code',None)
        store.put('snapshot',snapshot_id,data)
        return data
    except OrganizerError as exc:
        data['stop_code']=exc.code
        if exc.code in ('snapshot_changed_start_new','following_coverage_invalid','following_duplicate_or_reordered','group_member_outside_snapshot'):
            data['status']='conflict'
        store.put('snapshot',snapshot_id,data)
        raise
