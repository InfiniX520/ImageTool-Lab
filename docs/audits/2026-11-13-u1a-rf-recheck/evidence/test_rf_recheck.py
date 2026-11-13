"""Independent RF recheck; synthetic SQLite/PNG only, no network or credentials."""
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(os.environ.get('PIXIVKEEP_AUDIT_REPO', Path(__file__).resolve().parents[4])).resolve()
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

import pytest
import test_u1a_recovery as h
import test_u1a_reuse_gate as g
from test_u1a_rf_recovery import clone_delivery
from core.delivery_lineage import resolve_successor
from core.observation_snapshot import PageBudget, PreflightCancelled
from core.task_contracts import PathRef


def settle(repo, job, complete=True):
    repo.finish_source(job['batch_id'], coverage_complete=complete)
    assert repo.settle_batch(job['batch_id'])


def cancelled(repo, root, ident='123', request_id='cancel'):
    job = h.submit(repo, request_id=request_id)
    (delivery,) = h.accept(repo, job['batch_id'], h.source((h.candidate(ident),)))
    def exclude(stage, row):
        if stage == 'before_publish':
            repo.record_removal_intent(job['batch_id'], ident, 0,
                PathRef(relative_path=f'downl_Pixiv/{ident}_p0.png'),
                intent='excluded', command_id='exclude-' + request_id)
    service = h.executor(repo, root, h.Transport(h.Response()), fault_hook=exclude)
    assert h.run(repo, job, delivery, service).state == 'cancelled'
    settle(repo, job)
    return delivery, service


def test_cleanup_fact_written_by_real_executor_is_excluded_from_candidates(tmp_path):
    with h.LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        delivery, service = cancelled(repo, tmp_path)
        assert service.cleanup_delivery(delivery)
        with sqlite3.connect(repo.database_path) as conn:
            events = [json.loads(r[0]) for r in conn.execute(
                "SELECT safe_payload FROM events WHERE kind='delivery_cleanup'")]
        ids = [r['delivery_id'] for r in repo.list_cleanup_candidates()]
        print('CLEANUP_COMPLETED', events, ids)
        assert delivery not in ids, 'a completed cleanup must leave the retry queue'


def test_cleanup_window_advances_past_a_cleaned_delivery(tmp_path):
    with h.LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        first, service = cancelled(repo, tmp_path)
        assert service.cleanup_delivery(first)
        second, _ = cancelled(repo, tmp_path, '456', 'second-cancel')
        ids = [r['delivery_id'] for r in repo.list_cleanup_candidates(limit=1)]
        print('CLEANUP_WINDOW', first, second, ids)
        assert ids == [second]


def test_default_page_budget_can_progress_a_33_image_work_after_resume(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        seed = h.submit(repo, request_id='seed-33')
        page = h.source((g.candidate(pages=tuple(range(33))),))
        ids = h.accept(repo, seed['batch_id'], page)
        service = h.executor(repo, tmp_path, h.Transport(*(h.Response() for _ in ids)))
        for delivery in ids:
            assert h.run(repo, seed, delivery, service).state == 'completed'
        settle(repo, seed)
        job = h.submit(repo, request_id='reuse-33')
        calls, outcomes = [], []
        observe = repo._observe_artifact
        def counted(*args, **kwargs):
            calls.append(args[0]['artifact_id'])
            return observe(*args, **kwargs)
        monkeypatch.setattr(repo, '_observe_artifact', counted)
        for attempt in range(2):
            try:
                repo.accept_source_page(job['batch_id'], page, page_budget=PageBudget())
                outcomes.append('committed')
                break
            except PreflightCancelled as exc:
                outcomes.append(exc.reason)
                assert repo.get_checkpoint(job['job_id'], 'follow', 'history', 'source-1') is None
                settle(repo, job, complete=False)
                job = repo.resume(job['job_id'], f'resume-{attempt}')
                assert repo.activate_next()['batch_id'] == job['batch_id']
        print('PAGE_BUDGET_PROGRESS', outcomes, len(calls), len(set(calls)))
        assert 'committed' in outcomes, 'retry/resume must make progress beyond the first 32 files'


def handover(repo, root):
    first = g.submit(repo, request_id='handover-first')
    ids = g.accept(repo, first['batch_id'], g.source((g.candidate(pages=(0, 1)),)))
    service = h.executor(repo, root, h.Transport(h.Response(), h.Response(status=404)))
    assert h.run(repo, first, ids[0], service).state == 'completed'
    assert h.run(repo, first, ids[1], service).state == 'failed'
    settle(repo, first)
    h.target(root).write_bytes(h.SAME_SIZE_PNG)
    second, (hold,) = h.second_task(repo, root)
    settle(repo, second)
    resumed = repo.resume(first['job_id'], 'handover-resume')
    assert repo.activate_next()['batch_id'] == resumed['batch_id']
    repo.carry_unresolved_items(resumed['batch_id'])
    h.target(root).write_bytes(h.PNG_BYTES)
    result = repo.recover_verification_page(resumed['batch_id'], h.ARTWORK, 0)
    assert result['outcome'] == 'reused'
    settle(repo, resumed)
    return second, hold, ids[0]


def test_explicit_handover_is_authoritative_over_a_new_unrelated_pending(tmp_path):
    with h.LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        second, hold, completed = handover(repo, tmp_path)
        third = h.submit(repo, request_id='unrelated', dedup_policy='redownload')
        (pending,) = h.accept(repo, third['batch_id'], h.source((h.candidate(),)))
        assert repo.get_delivery(pending)['state'] == 'pending'
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            old = conn.execute('SELECT * FROM deliveries WHERE delivery_id=?', (hold,)).fetchone()
            successor = resolve_successor(conn, old)
        print('LINEAGE_PRIORITY', completed, pending, successor)
        assert successor.delivery_id == completed


def test_cancel_preserves_temp_replaced_while_waiting_for_writer(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        original = repo.cancel_unpublished_delivery
        replaced = []
        def replace_before_cas(*args, **kwargs):
            part = next(tmp_path.rglob('*.part'))
            part.unlink()
            part.write_bytes(b'foreign-replacement-after-observation')
            replaced.append(part)
            return original(*args, **kwargs)
        monkeypatch.setattr(repo, 'cancel_unpublished_delivery', replace_before_cas)
        cancelled(repo, tmp_path)
        print('CANCEL_TEMP_REPLACED', [p.exists() for p in replaced])
        assert replaced and replaced[0].exists(), 'cancellation must not unlink a replacement file'


def move_synthetic_root(root, destination, scope):
    assert root.resolve().is_relative_to(scope.resolve())
    assert destination.resolve().is_relative_to(scope.resolve())
    assert not root.is_symlink() and not destination.exists()
    root.rename(destination)


def test_offline_root_does_not_prove_an_unpublished_delivery(tmp_path):
    root, offline = tmp_path / 'library', tmp_path / 'offline'
    root.mkdir()
    with h.LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        job = h.submit(repo)
        (delivery,) = h.accept(repo, job['batch_id'], h.source((h.candidate(),)))
        def unplug(stage, row):
            if stage == 'before_publish':
                repo.record_removal_intent(job['batch_id'], h.ARTWORK, 0, h.OUTPUT_REF,
                    intent='excluded', command_id='exclude-before-unplug')
                move_synthetic_root(root, offline, tmp_path)
        service = h.executor(repo, root, h.Transport(h.Response()), fault_hook=unplug)
        result = h.run(repo, job, delivery, service)
        print('OFFLINE_CANCEL', result, len(list(offline.rglob('*.part'))))
        assert repo.get_delivery(delivery)['state'] == 'recovery_needed', 'missing root is not unpublished proof'


def test_cleanup_offline_root_does_not_claim_completion(tmp_path, monkeypatch):
    root, offline = tmp_path / 'library', tmp_path / 'offline'
    root.mkdir()
    with h.LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        unlink = Path.unlink
        def locked(path, *a, **kw):
            if path.suffix == '.part':
                raise PermissionError('synthetic sharing violation')
            return unlink(path, *a, **kw)
        with monkeypatch.context() as scope:
            scope.setattr(Path, 'unlink', locked)
            delivery, service = cancelled(repo, root)
        assert len(list(root.rglob('*.part'))) == 1
        move_synthetic_root(root, offline, tmp_path)
        result = service.cleanup_delivery(delivery)
        move_synthetic_root(offline, root, tmp_path)
        print('OFFLINE_CLEANUP', result, len(list(root.rglob('*.part'))))
        assert result is False, 'unmounted is not already absent'


def test_lineage_resolves_each_candidate_at_most_once_per_operation(tmp_path):
    with h.LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        _, hold, expected = handover(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            for i in range(16):
                clone_delivery(conn, hold, f'{i+1:032x}', generation=20+i)
            conn.commit()
            row = conn.execute('SELECT * FROM deliveries WHERE delivery_id=?', (hold,)).fetchone()
            scans = []
            def trace(sql):
                if 'AND rowid > (' in sql:
                    scans.append(sql)
            conn.set_trace_callback(trace)
            result = resolve_successor(conn, row)
            conn.set_trace_callback(None)
        print('LINEAGE_REPEAT_SCANS', len(scans), len(set(scans)))
        assert result.delivery_id == expected
        assert len(scans) == len(set(scans)), 'the same descendant must not be recursively expanded thousands of times'


def test_foreign_temp_present_before_verification_is_preserved(tmp_path, monkeypatch):
    with h.LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        job = h.submit(repo)
        (delivery,) = h.accept(repo, job['batch_id'], h.source((h.candidate(),)))
        replaced = []
        def foreign(stage, row):
            if stage == 'before_publish':
                repo.record_removal_intent(job['batch_id'], h.ARTWORK, 0, h.OUTPUT_REF,
                    intent='excluded', command_id='foreign-before-check')
                part = next(tmp_path.rglob('*.part'))
                part.write_bytes(b'foreign-before-observation')
                replaced.append(part)
        service = h.executor(repo, tmp_path, h.Transport(h.Response()), fault_hook=foreign)
        result = h.run(repo, job, delivery, service)
        assert result.state == 'recovery_needed'
        assert replaced[0].read_bytes() == b'foreign-before-observation'


def test_already_published_file_is_not_cancelled_when_root_goes_offline(tmp_path):
    from core.page_delivery import _Fault
    root, offline = tmp_path / 'library', tmp_path / 'offline'
    root.mkdir()
    with h.LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        job = h.submit(repo)
        (delivery,) = h.accept(repo, job['batch_id'], h.source((h.candidate(),)))
        def crash(stage, row):
            if stage == 'after_publish':
                raise _Fault('audit_after_link')
        service = h.executor(repo, root, h.Transport(h.Response()), fault_hook=crash)
        assert h.run(repo, job, delivery, service).state == 'recovery_needed'
        assert h.target(root).read_bytes() == h.PNG_BYTES
        repo.record_removal_intent(job['batch_id'], h.ARTWORK, 0, h.OUTPUT_REF,
            intent='excluded', command_id='exclude-published-recovery')
        def unplug(stage, row):
            if stage == 'before_publish':
                move_synthetic_root(root, offline, tmp_path)
        recovery = h.executor(repo, root, h.Transport(), fault_hook=unplug)
        first = recovery.recover_local()
        move_synthetic_root(offline, root, tmp_path)
        second = h.executor(repo, root, h.Transport()).recover_local()
        print('PUBLISHED_OFFLINE_CANCEL', first, second, repo.get_delivery(delivery)['state'],
              h.target(root).exists(), repo.get_artifact(delivery))
        assert repo.get_delivery(delivery)['state'] == 'completed', 'a published file must remain recoverable after reconnection'


def test_gate_wins_and_history_controls_still_pass(tmp_path, monkeypatch):
    from test_u1a_fix_boundaries import test_publication_gate_wins_over_a_later_exclusion_boundary_probe
    test_publication_gate_wins_over_a_later_exclusion_boundary_probe(tmp_path, monkeypatch)


def test_local_recovery_fault_handler_tolerates_another_recovery_winner(tmp_path):
    from core.page_delivery import _Fault
    with h.LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        job = h.submit(repo)
        (delivery,) = h.accept(repo, job['batch_id'], h.source((h.candidate(),)))
        def fail_before_link(stage, row):
            if stage == 'before_publish':
                raise _Fault('audit_stage_fault')
        seed = h.executor(repo, tmp_path, h.Transport(h.Response()), fault_hook=fail_before_link)
        assert h.run(repo, job, delivery, seed).state == 'recovery_needed'
        winner = h.executor(repo, tmp_path, h.Transport())
        def interleave(stage, row):
            if stage == 'before_publish':
                assert winner.recover_local() == ((delivery, 'completed'),)
                raise _Fault('audit_loser_fault')
        loser = h.executor(repo, tmp_path, h.Transport(), fault_hook=interleave)
        result = loser.recover_local()
        print('RECOVERY_WINNER', result, repo.get_delivery(delivery)['state'])
        assert repo.get_delivery(delivery)['state'] == 'completed'
        assert result == ((delivery, 'completed'),)
