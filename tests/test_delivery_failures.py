"""Storage faults, altered checkpoints and shared server cooldowns."""
import errno
import os
import sqlite3
import threading

import pytest

import core.page_delivery as delivery_module
from core.page_delivery import TransferPolicy
from delivery_fixtures import Response, Transport, executor, make_job, run
from image_fixtures import PNG_BYTES
from test_page_delivery import assert_completed, final_path, repo


def test_disk_full_checkpoint_never_publishes_or_counts_success(repo,tmp_path,monkeypatch):
    job,ids,_=make_job(repo)
    def full(*args):raise OSError(errno.ENOSPC,'synthetic')
    monkeypatch.setattr(delivery_module.os,'fsync',full)
    stop=threading.Event()
    result=run(repo,job,ids[0],executor(repo,tmp_path,Transport(Response())),stop)
    assert result.error_code=='disk_full' and stop.is_set()
    assert repo.get_artifact(ids[0]) is None
    assert not final_path(tmp_path).exists()
    assert repo.batch_results(job['batch_id'])['pages']['completed']==0


def test_publish_permission_failure_keeps_prepared_file_locally_recoverable(repo,tmp_path,monkeypatch):
    job,ids,_=make_job(repo)
    with monkeypatch.context() as patch:
        def denied(*args):raise PermissionError('synthetic')
        patch.setattr(delivery_module.os,'link',denied)
        result=run(repo,job,ids[0],executor(repo,tmp_path,Transport(Response())))
    assert result.state=='recovery_needed'
    assert not final_path(tmp_path).exists() and repo.get_artifact(ids[0]) is None
    assert executor(repo,tmp_path,Transport()).recover_local()==((ids[0],'completed'),)
    assert_completed(repo,ids[0],final_path(tmp_path))


def test_prepare_transaction_failure_never_creates_a_formal_file(repo,tmp_path,monkeypatch):
    job,ids,_=make_job(repo)
    def fail(*args,**kwargs):raise sqlite3.OperationalError('synthetic')
    monkeypatch.setattr(repo,'prepare_delivery',fail)
    stop=threading.Event()
    result=run(repo,job,ids[0],executor(repo,tmp_path,Transport(Response())),stop)
    assert result.state=='blocked' and stop.is_set()
    assert repo.get_artifact(ids[0]) is None and not final_path(tmp_path).exists()
    assert list((tmp_path/'downl_Pixiv').glob('*.part'))


@pytest.mark.parametrize('change',['prefix','inode','unconfirmed_tail'])
def test_resumed_checkpoint_modifies_only_the_reverified_owned_prefix(repo,tmp_path,change):
    job,ids,_=make_job(repo)
    stop=threading.Event()
    interrupted=Response(headers={'Content-Type':'image/png','Content-Length':str(len(PNG_BYTES)),'ETag':'"v1"'},
                         chunks=[PNG_BYTES[:20],stop.set,PNG_BYTES[20:]])
    assert run(repo,job,ids[0],executor(repo,tmp_path,Transport(interrupted)),stop).state=='suspended'
    checkpoint=repo.get_delivery(ids[0])['checkpoint']
    fragment=tmp_path/checkpoint['temp_ref']['relative_path']
    assert fragment.read_bytes()==PNG_BYTES[:20]
    if change=='prefix':
        fragment.write_bytes(b'x'*20+b'unknown-tail')
    elif change=='inode':
        replacement=fragment.with_name('unrelated-file')
        replacement.write_bytes(PNG_BYTES[:20]+b'unknown-tail')
        replacement.replace(fragment)
        assert fragment.stat().st_ino!=checkpoint['file_id']['inode']
    else:
        with fragment.open('ab') as stream:stream.write(b'unconfirmed-tail')
    saved=fragment.read_bytes()
    if change=='unconfirmed_tail':
        response=Response(PNG_BYTES[20:],status=206,headers={'Content-Type':'image/png','ETag':'"v1"',
            'Content-Length':str(len(PNG_BYTES)-20),'Content-Range':f'bytes 20-{len(PNG_BYTES)-1}/{len(PNG_BYTES)}'})
    else:
        response=Response()
    transport=Transport(response)
    assert run(repo,job,ids[0],executor(repo,tmp_path,transport)).state=='completed'
    assert_completed(repo,ids[0],final_path(tmp_path))
    headers=transport.calls[0][1]['headers']
    if change=='unconfirmed_tail':
        assert headers['Range']=='bytes=20-'
    else:
        assert 'Range' not in headers
        assert fragment.read_bytes()==saved


@pytest.mark.parametrize('status,range_header',[(416,None),(206,None),(206,'bytes 21-30/69')])
def test_unusable_resume_response_gets_one_full_fallback(repo,tmp_path,status,range_header):
    job,ids,_=make_job(repo)
    first=Response(headers={'Content-Type':'image/png','Content-Length':str(len(PNG_BYTES)),'ETag':'"v1"'},
                   chunks=[PNG_BYTES[:20],delivery_module.requests.ReadTimeout('synthetic')])
    headers={'Content-Type':'image/png','ETag':'"v1"'}
    if range_header:headers['Content-Range']=range_header
    transport=Transport(first,Response(PNG_BYTES[20:],status=status,headers=headers),Response())
    assert run(repo,job,ids[0],executor(repo,tmp_path,transport)).state=='completed'
    assert len(transport.calls)==3
    assert 'Range' not in transport.calls[-1][1]['headers']
    assert_completed(repo,ids[0],final_path(tmp_path))


def test_multiple_exact_ranges_complete_only_after_the_whole_object(repo,tmp_path):
    job,ids,_=make_job(repo)
    headers={'Content-Type':'image/png','Content-Length':str(len(PNG_BYTES)),'ETag':'"v1"'}
    responses=[Response(headers=headers,chunks=[PNG_BYTES[:20],delivery_module.requests.ReadTimeout('synthetic')])]
    for start,end in [(20,39),(40,len(PNG_BYTES)-1)]:
        responses.append(Response(PNG_BYTES[start:end+1],status=206,headers={'Content-Type':'image/png',
            'Content-Length':str(end-start+1),'Content-Range':f'bytes {start}-{end}/{len(PNG_BYTES)}','ETag':'"v1"'}))
    transport=Transport(*responses)
    assert run(repo,job,ids[0],executor(repo,tmp_path,transport)).state=='completed'
    assert [call[1]['headers'].get('Range') for call in transport.calls]==[None,'bytes=20-','bytes=40-']
    assert_completed(repo,ids[0],final_path(tmp_path))


class AdvancingStop:
    """Advance virtual monotonic time when the executor waits; no real sleep."""
    def __init__(self,clock,on_wait=None):
        self.clock,self.on_wait,self.stopped=clock,on_wait,False

    def is_set(self):return self.stopped

    def set(self):self.stopped=True

    def wait(self,seconds):
        self.clock[0]+=seconds
        if self.on_wait:self.on_wait()
        return self.stopped


def test_shared_cooldown_honors_a_later_extension_by_another_worker(repo,tmp_path):
    job,ids,_=make_job(repo)
    clock=[0.0];times=[];extended=[False]
    transport=Transport(Response(status=429,headers={'Retry-After':'2'}),Response())
    def get(url,**kwargs):
        times.append(clock[0])
        return transport(url,**kwargs)
    service=executor(repo,tmp_path,get,clock=lambda:clock[0])
    def extend():
        if clock[0]>=1 and not extended[0]:
            extended[0]=True
            service._rate_limit(Response(status=503,headers={'Retry-After':'5'}))
    result=run(repo,job,ids[0],service,AdvancingStop(clock,extend))
    assert result.state=='completed'
    assert len(times)==2 and times[1]>=6


@pytest.mark.parametrize('mode',['cancel','deadline'])
def test_server_cooldown_is_cancellable_and_obeys_the_total_deadline(repo,tmp_path,mode):
    job,ids,_=make_job(repo)
    clock=[0.0]
    stop=AdvancingStop(clock)
    if mode=='cancel':stop.on_wait=stop.set
    transport=Transport(Response(status=503,headers={'Retry-After':'120'}))
    service=executor(repo,tmp_path,transport,clock=lambda:clock[0],policy=TransferPolicy(max_seconds=2,retry_delay=0))
    result=run(repo,job,ids[0],service,stop)
    assert result.state=='suspended' and len(transport.calls)==1
    assert result.error_code==('stopped' if mode=='cancel' else 'transfer_deadline')
    assert repo.get_artifact(ids[0]) is None and not final_path(tmp_path).exists()
    assert clock[0]<=2.2


@pytest.mark.parametrize('status',[401,403])
def test_image_authorization_error_blocks_the_batch_without_retry(repo,tmp_path,status):
    job,ids,_=make_job(repo)
    transport=Transport(Response(status=status))
    stop=threading.Event()
    result=run(repo,job,ids[0],executor(repo,tmp_path,transport),stop)
    assert result.state=='suspended' and result.error_code=='image_access_denied'
    assert stop.is_set() and len(transport.calls)==1
    assert repo.get_batch(job['batch_id'])['phase']=='pausing'
    assert repo.get_artifact(ids[0]) is None
