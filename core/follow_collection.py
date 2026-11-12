"""Synchronous, cancellable source pump for the bounded P3 coordinator.

The caller owns the source thread; this class creates no threads, sessions,
authorization or queues. Every response is accepted before moving its cursor.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, replace

from core.candidate_filter import SourceResponseError, make_candidate, merge_details, needs_details
from core.follow_contracts import ArtworkPage, FollowingPage
from core.library_repository import BudgetExceeded, InvalidStateError, LibraryError, SourceCooling
from core.task_contracts import ContractError, SourcePage, canonical_json


class _Stopped(Exception):
    pass


class _Budget(Exception):
    def __init__(self,code):
        self.code=code


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    max_seconds: float = 300
    max_anchor_pages: int = 10

    def __post_init__(self):
        if type(self.max_seconds) not in (int,float) or not 0<self.max_seconds<=86400:
            raise ValueError('Invalid source time limit')
        if type(self.max_anchor_pages) is not int or not 1<=self.max_anchor_pages<=1000:
            raise ValueError('Invalid anchor lookup budget')


@dataclass(frozen=True, slots=True)
class SourceRunReport:
    batch_id: str
    reason_code: str
    coverage_complete: bool
    network_calls_this_run: int
    request_admissions: int


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def validate_follow_parameters(params):
    from core.candidate_filter import stable_id
    if not isinstance(params,dict) or set(params)-{'artist_ids','start_artist_id','start_ordinal','incomplete_from_job'}:
        raise ValueError('Unsupported follow parameters')
    values=dict(params)
    if 'incomplete_from_job' in values:
        import re
        parent=values['incomplete_from_job']
        if not isinstance(parent,str) or re.fullmatch('[0-9a-f]{32}',parent) is None or len(values)!=1:
            raise ValueError('Invalid incomplete artist selection')
    if 'start_ordinal' in values and (type(values['start_ordinal']) is not int or not 0<=values['start_ordinal']<=1_000_000):
        raise ValueError('Invalid start ordinal')
    if 'start_artist_id' in values:
        values['start_artist_id']=stable_id(values['start_artist_id'])
        if 'start_ordinal' in values:
            raise ValueError('Choose a single start position')
    if 'artist_ids' in values:
        selected=values['artist_ids']
        if not isinstance(selected,list) or not selected or len(selected)>2000:
            raise ValueError('Invalid artist selection')
        selected=[stable_id(value) for value in selected]
        if len(set(selected))!=len(selected) or 'start_artist_id' in values or 'start_ordinal' in values:
            raise ValueError('Conflicting artist selection')
        values['artist_ids']=selected
    return values


class FollowCollection:
    def __init__(self, coordinator, source, *, policy=SourcePolicy(), clock=time.monotonic):
        self.coordinator,self.repository,self.source=coordinator,coordinator.repository,source
        self.policy,self.clock=policy,clock
        self._running=False

    def _check(self):
        if self.stop is not None and self.stop.is_set():
            raise _Stopped()
        if self.repository.get_batch(self.batch_id)['phase']!='running':
            raise _Stopped()
        if self.clock()>=self.deadline:
            raise _Budget('source_time_budget')

    def _request(self,kind,call):
        self._check()
        try:
            self.repository.reserve_source_request(self.batch_id,kind=kind)
        except BudgetExceeded:
            raise _Budget('source_request_budget') from None
        except SourceCooling:
            raise _Budget('source_cooling') from None
        self._check()
        self.calls+=1
        try:
            result=call()
        except SourceResponseError:
            raise
        except Exception:
            raise SourceResponseError('source_transport_failed') from None
        self._check()
        return result

    def _accept(self,page):
        self._check()
        try:
            return self.coordinator.accept_page(self.batch_id,page).result(20)
        except BudgetExceeded:
            raise _Budget('work_budget') from None

    def _finish(self,complete,reason):
        if self.repository.get_batch(self.batch_id)['phase']=='running':
            try:
                self.coordinator.finish_source(self.batch_id,coverage_complete=complete,reason_code=reason).result(20)
            except InvalidStateError:
                if self.repository.get_batch(self.batch_id)['phase']=='running':
                    raise

    def _report(self,reason,coverage):
        admissions=self.repository.source_budget_status(self.batch_id)['reserved_requests']
        return SourceRunReport(self.batch_id,reason,coverage,self.calls,admissions)

    def run(self,batch_id,*,stop_event=None,refresh_following=False,from_head=False,retry_failed_only=False):
        if self._running:
            raise RuntimeError('A source pump can run only one batch at a time')
        for value in (refresh_following,from_head,retry_failed_only):
            if type(value) is not bool:
                raise TypeError('Source options must be boolean')
        self._running=True
        self.batch_id,self.stop,self.calls=batch_id,stop_event,0
        try:
            batch=self.repository.get_batch(batch_id)
            self.job_id=batch['job_id']
            self.request=self.repository.get_request(self.job_id)
            if self.request.query.source!='follow' or self.source.account_ref!=self.request.account_ref:
                raise ValueError('Source account or request does not match')
            if self.coordinator.active_batch_id!=batch_id:
                raise ValueError('The caller must explicitly activate this batch first')
            seconds=self.policy.max_seconds
            if self.request.budget.max_seconds is not None:
                seconds=min(seconds,self.request.budget.max_seconds)
            self.deadline=self.clock()+seconds
            self._check()
            if retry_failed_only:
                complete=self._prior_source_complete()
                self._carry_unresolved()
                self._finish(complete,'failed_pages_only')
                return self._report('failed_pages_only',complete)
            params=validate_follow_parameters(json.loads(self.request.query.parameters_json))
            if self.request.query.mode=='new':
                if params:
                    raise ValueError('Artist selectors are only valid for history')
                complete=True
                for partition in self.source.new_partitions:
                    complete=self._collect_partition('feed',partition,from_head=from_head,new=True) and complete
            else:
                complete=self._history(params,refresh_following=refresh_following,from_head=from_head)
            self._mark_scope(complete)
            self._carry_unresolved()
            self._finish(complete,'source_exhausted' if complete else 'coverage_unconfirmed')
            return self._report('source_exhausted' if complete else 'coverage_unconfirmed',complete)
        except _Budget as exc:
            self._finish(False,exc.code)
            return self._report(exc.code,False)
        except _Stopped:
            # The coordinator owns user pause/cancel. A caller-only stop also
            # requests pause so that it cannot leave a live batch behind.
            if self.repository.get_batch(batch_id)['phase']=='running':
                self.coordinator.pause(self.job_id,'source-stop-'+batch_id).result(20)
            return self._report('stopped',False)
        except InvalidStateError:
            if self.repository.get_batch(batch_id)['phase']!='running':
                return self._report('stopped',False)
            raise
        except (SourceResponseError,ContractError) as exc:
            code=exc.code if isinstance(exc,SourceResponseError) else 'invalid_source_metadata'
            if code=='metadata_stopped':
                if self.repository.get_batch(batch_id)['phase']=='running':
                    self.coordinator.pause(self.job_id,'source-stop-'+batch_id).result(20)
                return self._report('stopped',False)
            if isinstance(exc,SourceResponseError) and exc.retry_after_seconds is not None:
                self.repository.record_source_cooldown(batch_id,exc.retry_after_seconds,reason_code=code)
            self.repository.request_block(batch_id,code)
            return self._report(code,False)
        except LibraryError:
            self.repository.request_block(batch_id,'source_record_error')
            raise
        finally:
            self._running=False

    def _history(self,params,*,refresh_following,from_head):
        if 'incomplete_from_job' in params:
            parent=params['incomplete_from_job']
            parent_request=self.repository.get_request(parent)
            if parent_request.account_ref!=self.request.account_ref or parent_request.query.source!='follow' or parent_request.query.mode!='history':
                raise ValueError('Incomplete artist selection belongs to another account or source')
            def incomplete():
                after=''
                while True:
                    values=self.repository.incomplete_artists(parent,after=after,limit=100)
                    if not values:
                        return
                    for artist in values:
                        after=artist['artist_id']
                        yield artist
            return self._collect_artists(incomplete(),from_head=from_head)
        ids=params.get('artist_ids')
        if ids is not None:
            from core.candidate_filter import stable_id
            if not isinstance(ids,list) or not ids or len(ids)>2000:
                raise ValueError('Invalid artist selection')
            values=tuple(dict.fromkeys(stable_id(value) for value in ids))
            if len(values)!=len(ids):
                raise ValueError('Duplicate selected artist')
            if 'start_artist_id' in params or 'start_ordinal' in params:
                raise ValueError('Artist selections cannot be combined with a start position')
            artists=({'artist_id':value,'display_name':''} for value in values)
            return self._collect_artists(artists,from_head=from_head)
        command='snapshot-'+self.batch_id+'-'+str(int(refresh_following))
        snapshot=self.repository.begin_follow_snapshot(self.batch_id,command,refresh=refresh_following)
        while snapshot['state']=='building':
            before=snapshot['cursor_json']
            page=self._request('following',lambda:self.source.following(before))
            if not isinstance(page,FollowingPage):
                raise SourceResponseError('invalid_following_page')
            if not page.exhausted and page.next_cursor_json==before:
                raise SourceResponseError('following_did_not_advance')
            identity=_digest({'before':before,'page':asdict(page)})
            snapshot=self.repository.accept_following_page(self.batch_id,snapshot['snapshot_id'],identity,before,page)
        authoritative=snapshot['state']=='complete'
        if not authoritative and ('start_artist_id' in params or 'start_ordinal' in params):
            return False
        ordinal=params.get('start_ordinal',0)
        if type(ordinal) is not int or not 0<=ordinal<=1_000_000:
            raise ValueError('Invalid start ordinal')
        start_id=params.get('start_artist_id')
        if start_id is not None:
            from core.candidate_filter import stable_id
            start_id=stable_id(start_id)
            if 'start_ordinal' in params:
                raise ValueError('Choose a single start position')
        def members():
            after=-1;found=start_id is None
            while True:
                values=self.repository.follow_artists(self.request.account_ref,after=after,limit=100,
                    snapshot_id=snapshot['snapshot_id'] if authoritative else None)
                if not values:
                    if not found:
                        raise ValueError('Start artist is absent from the selected snapshot')
                    return
                for artist in values:
                    after=artist['ordinal']
                    if not found and artist['artist_id']==start_id:
                        found=True
                    if found and artist['ordinal']>=ordinal:
                        yield artist
        collected=self._collect_artists(members(),from_head=from_head)
        return collected and authoritative

    def _collect_artists(self,artists,*,from_head):
        complete=True
        for artist in artists:
            self._check()
            identity=artist['artist_id']
            checkpoints=[self.repository.get_checkpoint(self.job_id,'follow.history',partition,identity)
                         for partition in self.source.partitions]
            if all(value is not None and value['exhausted'] for value in checkpoints) and not from_head:
                complete=all(json.loads(value['coverage_json']).get('coverage_complete') is True for value in checkpoints) and complete
                continue
            try:
                self.repository.admit_source_artist(self.batch_id,identity)
            except BudgetExceeded:
                raise _Budget('source_artist_budget') from None
            for partition in self.source.partitions:
                complete=self._collect_partition(identity,partition,from_head=from_head,artist_name=artist.get('display_name','')) and complete
        return complete

    def _collect_partition(self,artist_id,partition,*,from_head,new=False,artist_name=''):
        source_id='follow.new' if new else 'follow.history'
        checkpoint=self.repository.get_checkpoint(self.job_id,source_id,partition,artist_id)
        if checkpoint is not None and from_head:
            raise ValueError('From-head checks require a fresh collection job')
        if checkpoint is not None and checkpoint['exhausted']:
            return json.loads(checkpoint['coverage_json']).get('coverage_complete') is True
        prior=None if from_head else self.repository.previous_artist_checkpoint(self.job_id,source_id,partition,artist_id)
        previous_coverage=json.loads(prior['coverage_json']) if prior else {}
        coverage=json.loads(checkpoint['coverage_json']) if checkpoint else {}
        anchors=set(coverage.get('prior_anchors',previous_coverage.get('head_ids',())))
        seen=set(coverage.get('seen_anchors',()))
        saved=json.loads(checkpoint['cursor_json']) if checkpoint else None
        before=checkpoint['cursor_json'] if checkpoint else 'null'
        api_cursor=saved.get('api_cursor','null') if saved else 'null'
        previous_cursor=saved.get('previous_api_cursor') if saved else None
        # Re-read the saved page and one previous page; ID-level acceptance is
        # idempotent, whereas trusting an old array offset can silently skip.
        replay_cursor=previous_cursor if previous_cursor is not None else api_cursor
        head_ids=list(coverage.get('head_ids',()))
        page_count=0
        while True:
            self._check()
            if new:
                page=self._request('new_works',lambda:self.source.new_works(replay_cursor,partition=partition))
            else:
                page=self._request('artist_works',lambda:self.source.artist_works(artist_id,replay_cursor,partition=partition))
            if not isinstance(page,ArtworkPage):
                raise SourceResponseError('invalid_artworks_page')
            if not page.exhausted and page.next_cursor_json==replay_cursor:
                raise SourceResponseError('artworks_did_not_advance')
            page_count+=1
            if not head_ids and replay_cursor=='null':
                head_ids=[meta.illust_id for meta in page.artworks[:3]]
            page_coverage=page.coverage_complete and coverage.get('coverage_complete',True)
            for index,metadata in enumerate(page.artworks):
                if not new and metadata.artist_id and metadata.artist_id!=artist_id:
                    raise SourceResponseError('artist_page_identity_mismatch')
                if not new:
                    metadata=replace(metadata,artist_id=metadata.artist_id or artist_id,artist_name=metadata.artist_name or artist_name)
                self._check()
                if needs_details(metadata,self.request):
                    detail=self._request('detail',lambda:self.source.details(metadata.illust_id))
                    metadata=merge_details(metadata,detail)
                candidate=make_candidate(metadata,self.request,provenance={'source_id':source_id,'partition':partition,'mode':self.request.query.mode})
                seen.add(metadata.illust_id)
                reached=bool(anchors) and anchors<=seen
                last=index==len(page.artworks)-1
                exhausted=reached or (last and page.exhausted)
                confirmed=page_coverage and (not anchors or reached)
                cursor={'api_cursor':replay_cursor if not last or exhausted else page.next_cursor_json,
                    'previous_api_cursor':previous_cursor,'anchor':metadata.illust_id}
                after=canonical_json(cursor)
                info={'coverage_complete':confirmed if exhausted else page_coverage,'head_ids':head_ids,
                    'mode':'incremental' if anchors else 'history','partition':partition,'anchor_complete':not anchors or reached,
                    'prior_anchors':sorted(anchors),'seen_anchors':sorted(anchors&seen)}
                identity=_digest({'before':before,'after':after,'candidate':asdict(candidate),'coverage':info})
                self._accept(SourcePage(source_id,partition,artist_id,identity,(candidate,),before,after,exhausted,
                    coverage_json=canonical_json(info)))
                before=after
                if exhausted:
                    return confirmed
            if not page.artworks or not page.exhausted:
                confirmed=page_coverage and not anchors
                after=canonical_json({'api_cursor':page.next_cursor_json,'previous_api_cursor':replay_cursor,'anchor':None})
                info={'coverage_complete':confirmed if page.exhausted else page_coverage,'head_ids':head_ids,
                    'mode':'incremental' if anchors else 'history','partition':partition,'anchor_complete':not anchors,
                    'prior_anchors':sorted(anchors),'seen_anchors':sorted(anchors&seen)}
                identity=_digest({'before':before,'after':after,'coverage':info})
                self._accept(SourcePage(source_id,partition,artist_id,identity,(),before,after,page.exhausted,coverage_json=canonical_json(info)))
                before=after
                if page.exhausted:
                    return confirmed
            if anchors and page_count>=self.policy.max_anchor_pages:
                raise _Budget('anchor_lookup_budget')
            coverage={'coverage_complete':page_coverage}
            previous_cursor,replay_cursor=replay_cursor,page.next_cursor_json

    def _prior_source_complete(self):
        value=self.repository.get_checkpoint(self.job_id,'follow.scope',self.request.query.mode,self.request.account_ref)
        return value is not None and value['exhausted'] and json.loads(value['coverage_json']).get('coverage_complete') is True

    def _mark_scope(self,complete):
        value=self.repository.get_checkpoint(self.job_id,'follow.scope',self.request.query.mode,self.request.account_ref)
        before=value['cursor_json'] if value is not None else 'null'
        after=canonical_json({'source_complete':complete})
        coverage=canonical_json({'coverage_complete':complete,'parameters':json.loads(self.request.query.parameters_json)})
        identity=_digest({'before':before,'after':after,'coverage':coverage})
        self._accept(SourcePage('follow.scope',self.request.query.mode,self.request.account_ref,identity,(),before,after,
            exhausted=complete,coverage_json=coverage))

    def _carry_unresolved(self):
        after=''
        while True:
            self._check()
            result=self.repository.carry_unresolved_items(self.batch_id,after=after,limit=100)
            if not result['count']:
                return
            after=result['after']
