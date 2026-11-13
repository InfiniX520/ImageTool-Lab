"""Independent recheck of RFA fixes; temporary files/SQLite and fake HTTP only."""
import os
from pathlib import Path
import sqlite3
import sys

ROOT=Path(os.environ.get('PIXIVKEEP_AUDIT_REPO', Path(__file__).resolve().parents[4])).resolve()
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import pytest
import test_u1a_recovery as h
import test_u1a_rfa_observation as o
import test_u1a_rfa_delivery as d
import test_u1a_rfa_lineage as l
import core.library_repository as lib
from core.observation_snapshot import PageBudget, PreflightCancelled
from core.delivery_lineage import resolve_successor_ex


@pytest.mark.parametrize('restore_mtime',[False,True])
def test_cached_hash_cannot_approve_an_in_place_equal_size_edit(tmp_path,monkeypatch,restore_mtime):
    with h.LibraryRepository(tmp_path/'ledger.sqlite3') as repo:
        _,page=o.deliver(repo,tmp_path,pages=(0,1))
        job=h.submit(repo,request_id='cached-reuse')
        with pytest.raises(PreflightCancelled):
            repo.accept_source_page(job['batch_id'],page,page_budget=PageBudget(max_files=1,max_slices=1))
        path=h.target(tmp_path)
        before=path.stat()
        path.write_bytes(h.SAME_SIZE_PNG)
        if restore_mtime:os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
        after=path.stat()
        assert (before.st_dev,before.st_ino,before.st_size)==(after.st_dev,after.st_ino,after.st_size)
        o.settle(repo,job,False)
        job=o.resume(repo,job)
        calls=o.observe_counter(repo,monkeypatch)
        repo.accept_source_page(job['batch_id'],page,page_budget=PageBudget())
        counts=h.pages(repo,job)
        print('CACHED_INPLACE',restore_mtime,len(calls),counts)
        assert counts['needs_verification']==1 and counts['completed']==1


@pytest.mark.parametrize('failure_mode',['cache_cap','progress_write'])
def test_missing_progress_cannot_turn_a_large_page_into_an_endless_retry(tmp_path,monkeypatch,failure_mode):
    with h.LibraryRepository(tmp_path/'ledger.sqlite3') as repo:
        _,page=o.deliver(repo,tmp_path,pages=tuple(range(6)))
        job=h.submit(repo,request_id='bounded-progress')
        if failure_mode=='cache_cap':
            # Scale only the cache size, retaining the production eviction rule.
            monkeypatch.setattr(lib,'MAX_PROGRESS_RECORDS',3)
        else:
            def unavailable(*args,**kwargs):raise lib.LibraryError('synthetic progress write refusal')
            monkeypatch.setattr(repo,'record_page_observation',unavailable)
        calls=o.observe_counter(repo,monkeypatch)
        outcomes=[]
        for attempt in range(8):
            try:
                repo.accept_source_page(job['batch_id'],page,page_budget=PageBudget(max_files=1,max_slices=2))
                outcomes.append('committed')
                break
            except PreflightCancelled as exc:
                outcomes.append(exc.reason)
                o.settle(repo,job,False)
                job=o.resume(repo,job,attempt)
        print('PROGRESS_RETENTION',failure_mode,outcomes,len(calls),len(set(calls)))
        assert 'committed' in outcomes, 'resume must progress, or explicitly reject unsupported capacity rather than repeat forever'


@pytest.mark.parametrize('limit',[1,200])
def test_cleanup_sweep_does_not_treat_examine_budget_as_end_of_queue(tmp_path,monkeypatch,limit):
    with h.LibraryRepository(tmp_path/'ledger.sqlite3') as repo:
        first,service=d.cancelled(repo,tmp_path)
        assert service.cleanup_delivery(first)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory=sqlite3.Row
            bad_count=2*min(limit*5,2000)+1
            for index in range(bad_count+1):
                ident=f'{index+1:032x}'
                d.clone_cancelled(conn,first,ident,index+2)
                if index<bad_count:
                    conn.execute("UPDATE deliveries SET prepared_json='{invalid' WHERE delivery_id=?",(ident,))
            last=ident
            conn.commit()
        visits=[]
        real=service.cleanup_cancelled_delivery
        def counted(ident):
            visits.append(ident)
            return real(ident)
        monkeypatch.setattr(service,'cleanup_cancelled_delivery',counted)
        cursors=[]
        for _ in range(5):
            service.recover_local(limit=limit)
            cursors.append(service._cleanup_cursor)
        print('CLEANUP_EXAMINE_LIMIT',limit,visits,cursors,last)
        assert last in visits


def test_superseded_intermediate_event_can_lead_to_an_older_completed_artifact(tmp_path):
    from test_u1a_rf_recovery import clone_delivery
    with h.LibraryRepository(tmp_path/'ledger.sqlite3') as repo:
        job,anchor,completed=h.queued_repair(repo,tmp_path)
        artifact=l.artifact_id_of(repo,completed)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute("UPDATE deliveries SET state='cancelled',error_code='superseded_by_recovery_repair' WHERE delivery_id=?",(anchor,))
            middle=clone_delivery(conn,anchor,'c'*32,generation=2,
                state='cancelled',error_code='superseded_by_recovery_reuse')
            l.insert_event(conn,job_id=job['job_id'],batch_id=job['batch_id'],
                payload={'delivery_id':middle,'artifact_id':artifact,'action':'reuse'})
            conn.commit()
            target=resolve_successor_ex(conn,l.row(conn,anchor))
            middle_target=resolve_successor_ex(conn,l.row(conn,middle))
        h.target(tmp_path).write_bytes(h.PNG_BYTES)
        recovery=repo.recover_verification_page(job['batch_id'],h.ARTWORK,0)
        print('TRANSITIVE_HANDOVER',target,middle_target,recovery)
        assert middle_target.successor.delivery_id==completed
        assert recovery['outcome']=='reused', 'the public recovery entry must follow the retained chain'
        assert target.successor is not None and target.successor.delivery_id==completed


def test_unreadable_ledger_is_not_reported_as_a_recovery_classification(tmp_path,monkeypatch):
    from core.page_delivery import _StorageFault
    with h.LibraryRepository(tmp_path/'ledger.sqlite3') as repo:
        job,(ident,)=d.prepared(repo,tmp_path)
        row=repo.get_delivery(ident)
        service=h.executor(repo,tmp_path,h.Transport())
        def unreadable(*args,**kwargs):raise lib.CorruptDataError('synthetic ledger is unreadable')
        monkeypatch.setattr(repo,'get_delivery',unreadable)
        with pytest.raises(_StorageFault):
            service._classify_after_race(row,'cancel_state_changed')


def test_recover_local_surfaces_an_unreadable_publication_ledger(tmp_path,monkeypatch):
    from core.page_delivery import _StorageFault
    with h.LibraryRepository(tmp_path/'ledger.sqlite3') as repo:
        job,(ident,)=d.prepared(repo,tmp_path,request_id='public-storage-fault')
        service=h.executor(repo,tmp_path,h.Transport())
        def unreadable(*args,**kwargs):
            raise lib.CorruptDataError('synthetic publication ledger is unreadable')
        monkeypatch.setattr(repo,'publish_authorized',unreadable)
        with pytest.raises(_StorageFault):
            result=service.recover_local()
            print('PUBLIC_STORAGE_FAULT',result,repo.get_delivery(ident)['state'])


def test_a_single_file_cannot_silently_exceed_the_declared_slice_byte_budget(tmp_path):
    with h.LibraryRepository(tmp_path/'ledger.sqlite3') as repo:
        _,page=o.deliver(repo,tmp_path)
        job=h.submit(repo,request_id='tiny-byte-budget')
        with pytest.raises(PreflightCancelled,match='page_byte_budget'):
            repo.accept_source_page(job['batch_id'],page,page_budget=PageBudget(max_bytes=1))
        assert o.checkpoint(repo,job) is None


def test_restored_missing_cache_does_not_overwrite_a_real_file(tmp_path):
    # Positive control for the explicitly proposed MISSING-window tradeoff.
    with h.LibraryRepository(tmp_path/'ledger.sqlite3') as repo:
        _,page=o.deliver(repo,tmp_path,pages=(0,1))
        h.target(tmp_path).unlink()
        job=h.submit(repo,request_id='missing-cache-control')
        with pytest.raises(PreflightCancelled):
            repo.accept_source_page(job['batch_id'],page,page_budget=PageBudget(max_files=1,max_slices=1))
        h.target(tmp_path).write_bytes(h.PNG_BYTES)
        ids=repo.accept_source_page(job['batch_id'],page,page_budget=PageBudget())
        assert len(ids)==1
        transport=h.Transport()
        outcome=h.run(repo,job,ids[0],h.executor(repo,tmp_path,transport))
        print('MISSING_CACHE_RESTORED',outcome,len(transport.calls))
        assert h.target(tmp_path).read_bytes()==h.PNG_BYTES
        assert not transport.calls
        assert outcome.state=='blocked' or outcome.state=='suspended'


def test_original_twelve_rfa_regressions_are_not_weakened():
    import ast
    original=ROOT/'docs/audits/2026-11-13-u1a-rf-recheck/evidence/test_rf_recheck.py'
    imported=ROOT/'tests/test_u1a_rfa_boundaries.py'
    def functions(path):
        return {node.name:ast.dump(node,include_attributes=False) for node in ast.parse(path.read_text(encoding='utf-8')).body
                if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))}
    assert functions(original)==functions(imported)
