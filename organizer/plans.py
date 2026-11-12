"""Reviewable plans, conservative remote writes, readback and compensation."""
from __future__ import annotations

import uuid

from organizer.common import OrganizerError, groups, identity, integer, now


def make_plan(store,web,review_ids,*,replace=False):
    if not 1<=len(review_ids)<=20 or len(set(review_ids))!=len(review_ids):
        raise OrganizerError('invalid_review_batch')
    reviews=[store.get('review',review_id) for review_id in review_ids]
    if len({row['artist_id'] for row in reviews})!=len(reviews):
        raise OrganizerError('duplicate_artist_reviews')
    for review in reviews:
        if review['account_id']!=web.account_id or review['needs_more']:
            raise OrganizerError('review_not_ready_for_account')
    catalog=web.catalog()
    rows=[]
    for review in reviews:
        current=web.detail(review['artist_id'])
        target=groups(review['proposed_groups'] if replace else sorted(set(current['groups'])|set(review['proposed_groups'])))
        rows.append(dict(current,target=target,review_id=review['id']))
    if web.catalog()!=catalog:
        raise OrganizerError('catalog_changed_during_planning')
    plan={'id':uuid.uuid4().hex,'account_id':web.account_id,'created_at':now(),'catalog':catalog,
          'mode':'replace' if replace else 'additive','artists':rows}
    store.put('plan',plan['id'],plan,create_only=True)
    return plan


def steps(plan):
    result=[]
    for row in plan['artists']:
        before=groups(row['groups']);target=groups(row['target'])
        actions=[('remove',tag) for tag in before if tag not in target]+[('add',tag) for tag in target if tag not in before]
        for number,(action,tag) in enumerate(actions):
            after=sorted(set(before)-{tag}) if action=='remove' else sorted(set(before)|{tag})
            result.append({'id':plan['id']+'_'+row['artist_id']+'_'+str(number), 'plan_id':plan['id'],
                'account_id':plan['account_id'],'artist_id':identity(row['artist_id']),'visibility':row['visibility'],
                'action':action,'tag':tag,'before':before,'after':after})
            before=after
    return result


def expected_catalog(store,plan):
    confirmed=[op for op in store.operations(plan['id']) if op['state']=='confirmed']
    if not confirmed:
        return plan['catalog']
    try:
        return store.get('catalog_receipt',confirmed[-1]['id'])['catalog']
    except KeyError:
        raise OrganizerError('confirmed_receipt_missing') from None


def catalog_after_allowed(before,after,operation):
    if set(before)!=set(after) or set(before)!= {'show','hide'}:
        return False
    side='show' if operation['visibility']=='0' else 'hide'
    for partition in ('show','hide'):
        previous=set(before[partition]);actual=set(after[partition]);tag=operation['tag']
        if operation['action']=='add':
            if actual not in (previous,previous|{tag}) or (partition==side and tag not in actual):
                return False
        elif actual not in (previous,previous-{tag}):
            return False
    return True


def inspect_operation(store,web,operation):
    current,catalog=web.observe(operation['artist_id'])
    if current['visibility']!=operation['visibility']:
        status='conflict'
    elif current['groups']==operation['after'] and catalog_after_allowed(operation['catalog_before'],catalog,operation):
        status='confirmed'
        store.put('catalog_receipt',operation['id'],{'catalog':catalog,'observed_at':now()})
    elif current['groups']==operation['before'] and catalog==operation['catalog_before']:
        status='not_applied'
    else:
        status='conflict'
    store.set_operation_state(operation['id'],status,observed=current['groups'],code=None if status=='confirmed' else status)
    return status


def reconcile(store,web,plan_id):
    plan=store.get('plan',plan_id)
    if plan['account_id']!=web.account_id:
        raise OrganizerError('account_mismatch')
    results=[]
    for operation in store.operations(plan_id):
        if operation['state'] not in ('intent','unknown'):
            continue
        status=inspect_operation(store,web,operation)
        results.append({'id':operation['id'],'state':status})
        if status!='confirmed':
            break
    return {'plan_id':plan_id,'reconciled':results,'writes':0}


def apply_plan(store,web,plan_id,*,max_artists=10,max_writes=20):
    plan=store.get('plan',plan_id)
    if plan['account_id']!=web.account_id:
        raise OrganizerError('account_mismatch')
    max_artists=integer(max_artists,1,20);max_writes=integer(max_writes,1,100)
    existing={op['id']:op for op in store.operations(plan_id)}
    pending=[op for op in existing.values() if op['state'] in ('intent','unknown')]
    if pending:
        reconcile(store,web,plan_id)
        return {'plan_id':plan_id,'status':'reconciled_only_run_again_after_review','writes':0}
    if any(op['state']!='confirmed' for op in existing.values()):
        raise OrganizerError('plan_has_conflict_or_unapplied_create_new')
    writes=0;artists=set()
    for operation in steps(plan):
        if operation['id'] in existing:
            continue
        if writes>=max_writes or (operation['artist_id'] not in artists and len(artists)>=max_artists):
            return {'plan_id':plan_id,'status':'batch_limit','writes':writes}
        # Reserve enough budget for preconditions, one write and its independent readback.
        if web.budget.limit-web.budget.reserved<8:
            raise OrganizerError('request_budget_exhausted')
        current,catalog=web.observe(operation['artist_id'])
        expected=expected_catalog(store,plan)
        if current['groups']!=operation['before'] or current['visibility']!=operation['visibility'] or catalog!=expected:
            store.put('conflict',uuid.uuid4().hex,{'plan_id':plan_id,'artist_id':operation['artist_id'],
                'observed':current,'catalog':catalog,'at':now(),'code':'website_changed'})
            raise OrganizerError('website_changed_replan_required')
        operation['catalog_before']=catalog
        store.prepare_operation(operation)
        try:
            web.mutate(operation['action'],operation['artist_id'],operation['tag'],operation['before'],operation['visibility'])
            writes+=1;artists.add(operation['artist_id'])
            status=inspect_operation(store,web,operation)
            if status!='confirmed':
                raise OrganizerError('write_readback_not_confirmed')
        except BaseException as exc:
            saved=store.get('operation',operation['id'])
            if saved['state']=='intent':
                store.set_operation_state(operation['id'],'unknown',code=exc.code if isinstance(exc,OrganizerError) else 'interrupted')
            raise
    # Terminal status requires a current read of every planned artist, including no-op rows.
    for row in plan['artists']:
        current=web.detail(row['artist_id'])
        if current['groups']!=row['target'] or current['visibility']!=row['visibility']:
            raise OrganizerError('website_changed_replan_required')
    if web.catalog()!=expected_catalog(store,plan):
        raise OrganizerError('website_changed_replan_required')
    store.put('completion',plan_id,{'plan_id':plan_id,'verified_at':now(),'status':'website_verified'})
    return {'plan_id':plan_id,'status':'website_verified','writes':writes}


def undo_plan(store,web,plan_id):
    original=store.get('plan',plan_id)
    if original['account_id']!=web.account_id:
        raise OrganizerError('account_mismatch')
    operations=store.operations(plan_id)
    if not operations or any(op['state']!='confirmed' for op in operations):
        raise OrganizerError('undo_requires_confirmed_operations')
    catalog=web.catalog()
    if catalog!=expected_catalog(store,original):
        raise OrganizerError('website_changed_replan_required')
    rows=[]
    for artist in dict.fromkeys(op['artist_id'] for op in operations):
        done=[op for op in operations if op['artist_id']==artist]
        current=web.detail(artist)
        if current['groups']!=done[-1]['after'] or current['visibility']!=done[-1]['visibility']:
            raise OrganizerError('website_changed_replan_required')
        rows.append(dict(current,target=done[0]['before'],review_id=None))
    plan={'id':uuid.uuid4().hex,'account_id':web.account_id,'created_at':now(),
          'catalog':catalog,'mode':'compensation','undo_of':plan_id,'artists':rows}
    store.put('plan',plan['id'],plan,create_only=True)
    return plan
