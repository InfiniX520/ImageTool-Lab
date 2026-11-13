"""Fixed source/query/export owners around the accepted coordinator.

Constructing this object performs no IO. The UI starts it explicitly and
submits commands/Futures; it never waits for these workers on the Tk thread.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import Future

from core.follow_collection import FollowCollection
from core.library_queries import LibraryQueries
from core.library_repository import CorruptDataError
from core.query_collection import QueryCollection


class RuntimeClosed(RuntimeError):
    pass


class RuntimeQueueFull(RuntimeError):
    pass


def _failed(error):
    result=Future();result.set_exception(error);return result


class _SourceStop:
    def __init__(self,runtime):self.runtime=runtime
    def is_set(self):
        owner=self.runtime
        with owner._guard:
            current=owner._source_current
            pending=current is not None and owner._pending_stops.get(current[1],0)>0
        return (owner._closing.is_set() or owner._source_stop.is_set() or pending or current is None
            or owner.coordinator.cancellation_requested(current[0]))


class _ExportStop:
    def __init__(self,runtime):self.runtime=runtime;self.event=threading.Event()
    def is_set(self):return self.runtime._closing.is_set() or self.event.is_set()


class CollectionRuntime:
    """One source owner, one short-query owner and one cancellable export owner.

    The source factory is called only for an explicitly activated follow job.
    It returns an owned source/client, used and closed on the source thread.
    Its signature is ``factory(request, stop_token)``; that token stays valid
    when the same account/client is reused between safe batch boundaries.
    """
    def __init__(self,coordinator,source_factory=None,*,diagnostic=None,query_capacity=8,export_capacity=1):
        if type(query_capacity) is not int or not 1<=query_capacity<=32:
            raise ValueError('Invalid query capacity')
        if type(export_capacity) is not int or export_capacity!=1:
            raise ValueError('Exactly one queued export is supported')
        if source_factory is not None and not callable(source_factory):
            raise TypeError('Source factory must be callable')
        self.coordinator,self.repository=coordinator,coordinator.repository
        self.source_factory,self.diagnostic=source_factory,diagnostic
        self.queries=LibraryQueries(self.repository)
        self._queries,self._exports=queue.Queue(query_capacity),queue.Queue(export_capacity)
        self._guard=threading.RLock()
        self._closing,self._wake,self._source_stop,self._export_stop=(threading.Event() for _ in range(4))
        self._started,self._closed,self._business_closed=False,False,False
        self._source_current=None;self._source_options={};self._reports=OrderedDict()
        self._pending_options=0;self._pending_stops={};self._export_tokens={}
        self._source_error=None;self._cleanup_error=None
        suffix=uuid.uuid4().hex[:8]
        self._source_thread=threading.Thread(target=self._source_loop,name='collection-source-'+suffix,daemon=False)
        self._query_thread=threading.Thread(target=self._query_loop,args=(self._queries,),name='collection-query-'+suffix,daemon=False)
        self._export_thread=threading.Thread(target=self._query_loop,args=(self._exports,),name='collection-export-'+suffix,daemon=False)
        self._threads=(self._source_thread,self._query_thread,self._export_thread)

    def _log(self,code,*,level='info',batch_id=None):
        if self.diagnostic is not None:
            try:self.diagnostic.emit(code,level=level,code=code,batch_id=batch_id)
            except Exception:pass

    def start(self):
        with self._guard:
            if self._closing.is_set():raise RuntimeClosed('Runtime is closing')
            if not self._started:
                ready=self.coordinator.start()
                self._started=True
                for thread in self._threads:thread.start()
                return ready
            return self.coordinator.start()

    def _accepting(self):
        return self._started and not self._closing.is_set()

    def submit(self,request,command_id,*,parent_job_id=None,refresh_following=False,from_head=False):
        if type(refresh_following) is not bool or type(from_head) is not bool:
            return _failed(TypeError('Source options must be boolean'))
        with self._guard:
            if not self._accepting():return _failed(RuntimeClosed('Runtime is not accepting commands'))
            if len(self._source_options)+self._pending_options>=32:return _failed(RuntimeQueueFull('Source option capacity reached'))
            self._pending_options+=1
            future=self.coordinator.submit(request,command_id,parent_job_id=parent_job_id)
            self._register_source(future,{'refresh_following':refresh_following,'from_head':from_head})
            return future

    def resume(self,job_id,command_id,budget=None,*,retry_failed=False,failed_pages_only=False):
        if type(retry_failed) is not bool or type(failed_pages_only) is not bool:
            return _failed(TypeError('Source options must be boolean'))
        with self._guard:
            if not self._accepting():return _failed(RuntimeClosed('Runtime is not accepting commands'))
            if len(self._source_options)+self._pending_options>=32:return _failed(RuntimeQueueFull('Source option capacity reached'))
            self._pending_options+=1
            future=self.coordinator.resume(job_id,command_id,budget,retry_failed=retry_failed)
            self._register_source(future,{'retry_failed_only':failed_pages_only})
            return future

    def _register_source(self,future,options):
        def registered(done):
            with self._guard:
                self._pending_options-=1
                if done.cancelled() or done.exception() is not None:return
                if self._closing.is_set():return
                value=done.result()
                self._source_options[value['batch_id']]=options
            self._wake.set()
        future.add_done_callback(registered)

    def run_queued(self):
        with self._guard:
            if not self._accepting():return _failed(RuntimeClosed('Runtime is not accepting commands'))
            return self.coordinator.run_queued()

    def pause(self,job_id,command_id):
        return self._stop_command(job_id,command_id,cancel=False)

    def cancel(self,job_id,command_id):
        return self._stop_command(job_id,command_id,cancel=True)

    def _stop_command(self,job_id,command_id,*,cancel):
        with self._guard:
            self._pending_stops[job_id]=self._pending_stops.get(job_id,0)+1
            future=(self.coordinator.cancel if cancel else self.coordinator.pause)(job_id,command_id)
            def settled(_):
                with self._guard:
                    count=self._pending_stops.get(job_id,1)-1
                    if count:self._pending_stops[job_id]=count
                    else:self._pending_stops.pop(job_id,None)
                self._wake.set()
            future.add_done_callback(settled)
            return future

    def fetch(self,query,**kwargs):
        return self._query_command(self._queries,self.queries.fetch,query,**kwargs)

    def export_markdown(self,query,output_ref,resolver):
        return self._query_command(self._exports,self.queries.export_markdown,query,output_ref,resolver)

    def export_log(self,output_ref,resolver):
        if self.diagnostic is None:return _failed(RuntimeError('No diagnostic session exists'))
        return self._query_command(self._exports,self.diagnostic.export_text,output_ref,resolver)

    def cancel_export(self):
        with self._guard:
            for token,future in list(self._export_tokens.items()):
                token.event.set()
                future.cancel()

    def _query_command(self,channel,method,*args,**kwargs):
        future=Future()
        with self._guard:
            if not self._accepting():return _failed(RuntimeClosed('Runtime is not accepting queries'))
            token=None
            if channel is self._exports:
                token=_ExportStop(self)
                kwargs=dict(kwargs,stop_event=token)
            try:channel.put_nowait((future,method,args,kwargs,token))
            except queue.Full:return _failed(RuntimeQueueFull('Query or export capacity reached'))
            if token is not None:self._export_tokens[token]=future
        return future

    def _query_loop(self,channel):
        while not self._closing.is_set():
            try:future,method,args,kwargs,token=channel.get(timeout=0.1)
            except queue.Empty:continue
            try:
                if self._closing.is_set():
                    if not future.done():future.set_exception(RuntimeClosed('Runtime stopped'))
                    continue
                if not future.set_running_or_notify_cancel():continue
                try:future.set_result(method(*args,**kwargs))
                except BaseException as exc:future.set_exception(exc)
            finally:
                if token is not None:
                    with self._guard:self._export_tokens.pop(token,None)
                channel.task_done()

    def _source_loop(self):
        source=None;source_account=None;last_batch=None
        stop=_SourceStop(self)
        try:
            self.coordinator.start().result(20)
            while not self._closing.is_set():
                with self._guard:
                    batch_id=self.coordinator.active_batch_id
                    if batch_id is not None and batch_id!=last_batch:
                        batch=self.repository.get_batch(batch_id)
                        if batch['phase']!='running':batch_id=None
                    else:batch_id=None
                    if batch_id is not None:
                        self._source_current=(batch_id,batch['job_id'])
                        self._source_stop.clear()
                        options=self._source_options.pop(batch_id,{})
                if batch_id is None:
                    self._wake.wait(0.05);self._wake.clear();continue
                last_batch=batch_id
                try:
                    if stop.is_set():continue
                    request=self.repository.get_request(batch['job_id'])
                    if self.source_factory is None:raise RuntimeError('No source factory exists')
                    if source is None or source_account!=request.account_ref:
                        if source is not None:self._close_source(source)
                        source=self.source_factory(request,stop)
                        source_account=request.account_ref
                    report=FollowCollection(self.coordinator,source).run(batch_id,stop_event=stop,**options) if request.query.source=='follow' else QueryCollection(self.coordinator,source).run(batch_id,stop_event=stop,**options)
                    with self._guard:
                        self._reports[batch_id]=report
                        while len(self._reports)>32:self._reports.popitem(last=False)
                    self._log(report.reason_code,batch_id=batch_id)
                except CorruptDataError:
                    with self._guard:self._source_error='intent_unreadable'
                    self._log('intent_unreadable',level='error',batch_id=batch_id)
                    try:self.repository.request_block(batch_id,'intent_unreadable')
                    except Exception:pass
                except Exception:
                    with self._guard:self._source_error='source_runner_failed'
                    self._log('source_runner_failed',level='error',batch_id=batch_id)
                    try:self.repository.request_block(batch_id,'source_runner_failed')
                    except Exception:pass
                finally:
                    with self._guard:self._source_current=None
        except Exception:
            with self._guard:self._source_error='source_owner_failed'
            self._log('source_owner_failed',level='error')
        finally:
            if source is not None:self._close_source(source)

    def _close_source(self,source):
        close=getattr(source,'close',None)
        if callable(close):
            try:close()
            except Exception:
                with self._guard:self._cleanup_error='source_cleanup_failed'
                self._log('source_cleanup_failed',level='error')

    def source_report(self,batch_id):
        with self._guard:return self._reports.get(batch_id)

    def status(self):
        with self._guard:
            return {'started':self._started,'closing':self._closing.is_set(),'closed':self._closed,
                'source_batch_id':self._source_current[0] if self._source_current else None,
                'source_error':self._source_error,'cleanup_error':self._cleanup_error,
                'queued_queries':self._queries.qsize(),'queued_exports':self._exports.qsize(),
                'threads_alive':sum(thread.is_alive() for thread in self._threads)}

    def close(self,timeout=20):
        if threading.current_thread() in self._threads:raise RuntimeError('Runtime cannot close its own worker')
        deadline=time.monotonic()+timeout
        with self._guard:
            if self._closed:return
            self._closing.set();self._source_stop.set();self._export_stop.set();self._wake.set()
            for token in self._export_tokens:token.event.set()
        if not self._business_closed:
            active=self.coordinator.active_batch_id
            if active is not None:
                batch=self.repository.get_batch(active)
                self.coordinator.pause(batch['job_id'],'runtime-close-'+active)
        for channel in (self._queries,self._exports):
            while True:
                try:future,method,args,kwargs,token=channel.get_nowait()
                except queue.Empty:break
                if not future.done():future.set_exception(RuntimeClosed('Runtime stopped'))
                if token is not None:
                    with self._guard:self._export_tokens.pop(token,None)
                channel.task_done()
        if self._started:
            for thread in self._threads:thread.join(max(0,deadline-time.monotonic()))
            if any(thread.is_alive() for thread in self._threads):
                raise TimeoutError('Source/query/export owners are still settling; repository stays open')
        if self._cleanup_error is not None:raise RuntimeError(self._cleanup_error)
        failure=None
        if not self._business_closed:
            try:
                self.coordinator.close(timeout=max(0,deadline-time.monotonic()))
                self._business_closed=True
            except Exception as exc:
                if not getattr(self.repository,'_closed',False):raise
                self._business_closed=True
                failure=exc
                self._log('coordinator_closed_with_error',level='error')
        if self.diagnostic is not None:
            if self.diagnostic.close(timeout=max(0,deadline-time.monotonic())) is False:
                raise TimeoutError('Diagnostic owner is still settling; runtime remains closing')
        with self._guard:self._closed=True
        if failure is not None:raise RuntimeError('Runtime closed after a coordinator failure') from failure

    def __enter__(self):
        try:self.start().result(20)
        except BaseException:
            try:self.close()
            except Exception:pass
            raise
        return self

    def __exit__(self,*_):self.close()
