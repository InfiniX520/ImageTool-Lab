"""Original-page delivery: real temporary SQLite/files and synthetic HTTP."""
from dataclasses import replace
import hashlib
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading

import pytest
import requests

import core.page_delivery as delivery_module
from core.image_validation import ImageValidationStopped
from core.library_repository import LibraryRepository
from core.page_delivery import TransferPolicy
from delivery_fixtures import Response, Transport, executor, make_job, run
from image_fixtures import PNG_BYTES


@pytest.fixture
def repo(tmp_path):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as value:
        yield value


def final_path(root, artwork='123', page=0, extension='png'):
    return root / 'downl_Pixiv' / f'{artwork}_p{page}.{extension}'


def assert_completed(repo, delivery, target):
    assert target.read_bytes() == PNG_BYTES
    assert repo.get_delivery(delivery)['state'] == 'completed'
    artifact = repo.get_artifact(delivery)
    assert artifact['sha256'] == hashlib.sha256(PNG_BYTES).hexdigest()
    assert artifact['size'] == len(PNG_BYTES)


def test_valid_source_bytes_are_published_once_and_owned_temp_is_cleaned(repo, tmp_path):
    job, ids, _ = make_job(repo)
    transport = Transport(Response())
    assert run(repo, job, ids[0], executor(repo, tmp_path, transport)).state == 'completed'
    assert_completed(repo, ids[0], final_path(tmp_path))
    assert len(transport.calls) == 1
    assert not list((tmp_path / 'downl_Pixiv').glob('*.part'))
    result = repo.batch_results(job['batch_id'])
    assert result['pages']['new_completed'] == result['pages']['completed'] == 1


@pytest.mark.parametrize('body,headers,status', [
    (b'<html>maintenance</html>', {'Content-Type':'text/html'}, 200),
    (b'{"error":true}', {'Content-Type':'application/octet-stream'}, 200),
    (PNG_BYTES, {'Content-Type':'image/png','Content-Encoding':'gzip'}, 200),
    (PNG_BYTES, {'Content-Type':'image/png','Content-Length':str(len(PNG_BYTES)),'Transfer-Encoding':'chunked'}, 200),
    (b'', {'Content-Type':'image/png','Content-Length':'0'}, 200),
    (PNG_BYTES, {'Content-Type':'image/png'}, 404),
    (PNG_BYTES, {'Content-Type':'image/png'}, 302),
])
def test_error_or_ambiguous_response_never_creates_success(repo,tmp_path,body,headers,status):
    job,ids,_=make_job(repo)
    transport=Transport(*(Response(body,status=status,headers=dict(headers)) for _ in range(3)))
    result=run(repo,job,ids[0],executor(repo,tmp_path,transport))
    assert result.state=='failed'
    assert not final_path(tmp_path).exists()
    assert repo.get_artifact(ids[0]) is None
    assert repo.get_delivery(ids[0])['state']=='failed'
    assert len(transport.calls)<=3


@pytest.mark.parametrize('header,extension', [('image/jpeg','png'),('image/png','jpg')])
def test_conflicting_format_is_retained_for_review(repo,tmp_path,header,extension):
    job,ids,_=make_job(repo,extension=extension)
    transport=Transport(Response(headers={'Content-Type':header,'Content-Length':str(len(PNG_BYTES))}))
    result=run(repo,job,ids[0],executor(repo,tmp_path,transport))
    assert result.state=='needs_verification'
    assert repo.get_delivery(ids[0])['state']=='needs_verification'
    assert repo.get_artifact(ids[0]) is None
    assert not final_path(tmp_path,extension=extension).exists()
    assert len(transport.calls)==1


def test_unknown_length_png_requires_full_structure_and_can_complete(repo,tmp_path):
    job,ids,_=make_job(repo)
    transport=Transport(Response(headers={'Content-Type':'image/png'}))
    assert run(repo,job,ids[0],executor(repo,tmp_path,transport)).state=='completed'
    assert_completed(repo,ids[0],final_path(tmp_path))


def test_known_truncation_never_becomes_completed(repo,tmp_path):
    job,ids,_=make_job(repo)
    transport=Transport(*(Response(PNG_BYTES[:-5],headers={'Content-Type':'image/png','Content-Length':str(len(PNG_BYTES))}) for _ in range(3)))
    assert run(repo,job,ids[0],executor(repo,tmp_path,transport)).state=='failed'
    assert not final_path(tmp_path).exists()
    assert repo.get_artifact(ids[0]) is None
    assert repo.get_delivery(ids[0])['http_attempts']==3


def test_matching_file_size_without_delivery_ownership_never_skips_get(repo,tmp_path):
    target=final_path(tmp_path);target.parent.mkdir();target.write_bytes(b'x'*len(PNG_BYTES))
    job,ids,_=make_job(repo)
    transport=Transport()
    stop=threading.Event()
    result=run(repo,job,ids[0],executor(repo,tmp_path,transport),stop)
    assert result.state=='suspended' and stop.is_set()
    assert target.read_bytes()==b'x'*len(PNG_BYTES)
    assert transport.calls==[] and repo.get_artifact(ids[0]) is None


@pytest.mark.parametrize('etag,second_status,offset_adjust,second_etag', [
    ('"v1"',206,0,'"v1"'),
    ('"v1"',200,0,'"v2"'),
    ('"v1"',206,1,'"v1"'),
    ('"v1"',206,0,'"v2"'),
    (None,200,0,None),
])
def test_range_resume_or_bounded_full_fallback(repo,tmp_path,etag,second_status,offset_adjust,second_etag):
    job,ids,_=make_job(repo)
    prefix=20
    first_headers={'Content-Type':'image/png','Content-Length':str(len(PNG_BYTES))}
    if etag:first_headers['ETag']=etag
    first=Response(headers=first_headers,chunks=[PNG_BYTES[:prefix],requests.ReadTimeout()])
    second_headers={'Content-Type':'image/png'}
    if second_etag:second_headers['ETag']=second_etag
    if second_status==206:
        second_body=PNG_BYTES[prefix:]
        second_headers.update({'Content-Length':str(len(second_body)),'Content-Range':f'bytes {prefix+offset_adjust}-{len(PNG_BYTES)-1}/{len(PNG_BYTES)}'})
    else:
        second_body=PNG_BYTES;second_headers['Content-Length']=str(len(PNG_BYTES))
    second=Response(second_body,status=second_status,headers=second_headers)
    transport=Transport(first,second,Response())
    assert run(repo,job,ids[0],executor(repo,tmp_path,transport)).state=='completed'
    assert_completed(repo,ids[0],final_path(tmp_path))
    expected=3 if second_status==206 and (offset_adjust or etag!=second_etag) else 2
    assert len(transport.calls)==expected
    sent=transport.calls[1][1]['headers']
    if etag:assert sent['Range']==f'bytes={prefix}-' and sent['If-Range']==etag
    else:assert 'Range' not in sent
    if expected==3:assert 'Range' not in transport.calls[2][1]['headers']


def test_unsolicited_partial_is_rejected_then_retried_as_one_full_get(repo,tmp_path):
    job,ids,_=make_job(repo)
    wrong=Response(PNG_BYTES,status=206,headers={'Content-Type':'image/png','Content-Length':str(len(PNG_BYTES)),'Content-Range':f'bytes 0-{len(PNG_BYTES)-1}/{len(PNG_BYTES)}','ETag':'"v1"'})
    transport=Transport(wrong,Response())
    assert run(repo,job,ids[0],executor(repo,tmp_path,transport)).state=='completed'
    assert_completed(repo,ids[0],final_path(tmp_path))
    assert len(transport.calls)==2


def test_transport_exception_after_full_checkpoint_does_not_redownload(repo,tmp_path):
    job,ids,_=make_job(repo)
    transport=Transport(Response(chunks=[PNG_BYTES,requests.ReadTimeout()]))
    assert run(repo,job,ids[0],executor(repo,tmp_path,transport)).state=='completed'
    assert_completed(repo,ids[0],final_path(tmp_path))
    assert len(transport.calls)==1


def test_network_attempt_budget_is_shared_by_all_retries(repo,tmp_path):
    job,ids,_=make_job(repo)
    transport=Transport(*(requests.ConnectionError('synthetic') for _ in range(3)))
    assert run(repo,job,ids[0],executor(repo,tmp_path,transport)).state=='failed'
    assert len(transport.calls)==3
    assert repo.get_delivery(ids[0])['http_attempts']==3
    assert repo.get_artifact(ids[0]) is None


def test_user_stop_before_first_request_has_no_side_effects(repo,tmp_path):
    job,ids,_=make_job(repo)
    stop=threading.Event();stop.set();transport=Transport()
    assert run(repo,job,ids[0],executor(repo,tmp_path,transport),stop).state=='suspended'
    assert transport.calls==[]
    assert repo.get_delivery(ids[0])['http_attempts']==0
    assert not final_path(tmp_path).exists()


def test_deadline_during_structure_check_stops_batch_instead_of_redispatch(repo,tmp_path,monkeypatch):
    job,ids,_=make_job(repo)
    clock=[0.0]
    def expire(*args,**kwargs):
        clock[0]=2.0
        raise ImageValidationStopped()
    monkeypatch.setattr(delivery_module,'inspect_image',expire)
    transport=Transport(Response());stop=threading.Event()
    service=executor(repo,tmp_path,transport,clock=lambda:clock[0],policy=TransferPolicy(max_seconds=1,retry_delay=0))
    result=run(repo,job,ids[0],service,stop)
    assert stop.is_set()
    assert result.error_code=='transfer_deadline'
    assert repo.get_batch(job['batch_id'])['phase']=='pausing'
    assert repo.get_artifact(ids[0]) is None


@pytest.mark.parametrize('stage', ['before_publish','before_confirm'])
def test_same_inode_same_size_tampering_before_commit_never_records_success(repo,tmp_path,stage):
    job,ids,_=make_job(repo)
    def tamper(point,row):
        if point!=stage:return
        current=repo.get_delivery(row['delivery_id'])
        ref=current['prepared']['temp_ref'] if stage=='before_publish' else current['prepared']['final_ref']
        path=tmp_path/ref['relative_path']
        data=bytearray(path.read_bytes());data[-5]^=1;path.write_bytes(data)
    result=run(repo,job,ids[0],executor(repo,tmp_path,Transport(Response()),fault_hook=tamper))
    assert result.state=='recovery_needed'
    assert repo.get_artifact(ids[0]) is None
    assert repo.get_delivery(ids[0])['state']=='recovery_needed'
    if stage=='before_publish':assert not final_path(tmp_path).exists()


def test_target_created_during_publish_is_preserved(repo,tmp_path):
    job,ids,_=make_job(repo)
    def collide(point,row):
        if point=='before_publish':final_path(tmp_path).write_bytes(b'user-owned')
    result=run(repo,job,ids[0],executor(repo,tmp_path,Transport(Response()),fault_hook=collide))
    assert result.state=='recovery_needed'
    assert final_path(tmp_path).read_bytes()==b'user-owned'
    assert repo.get_artifact(ids[0]) is None


def test_confirmation_error_retains_published_bytes_for_local_recovery(repo,tmp_path,monkeypatch):
    job,ids,_=make_job(repo)
    original=repo.confirm_delivery
    def fail(*args,**kwargs):raise sqlite3.OperationalError('synthetic')
    monkeypatch.setattr(repo,'confirm_delivery',fail)
    stop=threading.Event();transport=Transport(Response())
    result=run(repo,job,ids[0],executor(repo,tmp_path,transport),stop)
    assert result.state=='recovery_needed' and stop.is_set()
    assert final_path(tmp_path).read_bytes()==PNG_BYTES
    assert repo.get_artifact(ids[0]) is None
    monkeypatch.setattr(repo,'confirm_delivery',original)
    assert executor(repo,tmp_path,Transport()).recover_local()==((ids[0],'completed'),)
    assert_completed(repo,ids[0],final_path(tmp_path))
    assert len(transport.calls)==1


def test_cleanup_failure_cannot_undo_durable_success(repo,tmp_path,monkeypatch):
    job,ids,_=make_job(repo)
    service=executor(repo,tmp_path,Transport(Response()))
    def fail(*args,**kwargs):raise PermissionError('synthetic')
    monkeypatch.setattr(service,'cleanup_delivery',fail)
    assert run(repo,job,ids[0],service).state=='completed'
    assert_completed(repo,ids[0],final_path(tmp_path))
    assert executor(repo,tmp_path,Transport()).cleanup_delivery(ids[0]) is True


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize('point', ['before_prepare','after_prepare','before_publish','after_publish','before_confirm','after_confirm','before_cleanup','after_cleanup'])
def test_each_durable_boundary_recovers_without_http(tmp_path,point):
    database=tmp_path/'ledger.sqlite3'
    with LibraryRepository(database) as first:
        job,ids,_=make_job(first)
        def crash(stage,row):
            if stage==point:raise SimulatedCrash()
        with pytest.raises(SimulatedCrash):run(first,job,ids[0],executor(first,tmp_path,Transport(Response()),fault_hook=crash))
    with LibraryRepository(database) as second:
        second.recover_interrupted()
        local=executor(second,tmp_path,Transport())
        local.recover_local()
        if point=='before_prepare':
            assert second.get_artifact(ids[0]) is None
            assert not final_path(tmp_path).exists()
        else:
            assert_completed(second,ids[0],final_path(tmp_path))
            local.cleanup_delivery(ids[0])


def _crash_process(root,point):
    root=Path(root)
    repository=LibraryRepository(root/'ledger.sqlite3')
    job,ids,_=make_job(repository)
    def crash(stage,row):
        if stage==point:os._exit(86)
    run(repository,job,ids[0],executor(repository,root,Transport(Response()),fault_hook=crash))
    os._exit(87)


@pytest.mark.parametrize('point', ['after_prepare','after_publish'])
def test_actual_process_exit_recovers_wal_and_owned_file_locally(tmp_path,point):
    child=multiprocessing.get_context('spawn').Process(target=_crash_process,args=(str(tmp_path),point))
    child.start();child.join(20)
    if child.is_alive():
        child.terminate();child.join(5)
        pytest.fail('Synthetic child did not reach its crash point')
    assert child.exitcode==86
    with LibraryRepository(tmp_path/'ledger.sqlite3') as repository:
        repository.recover_interrupted()
        result=executor(repository,tmp_path,Transport()).recover_local()
        assert len(result)==1 and result[0][1]=='completed'
        assert_completed(repository,result[0][0],final_path(tmp_path))
