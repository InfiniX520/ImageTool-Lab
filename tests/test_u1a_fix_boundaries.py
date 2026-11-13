"""Independent re-audit probes, pinned to the 2026-11-13 re-check.

Imported from docs/audits/2026-11-13-u1a-fix-recheck/evidence/test_fix_boundaries.py;
only the repository-root lookup changed.  Thirteen of these probes fail at the
start of the RF round: they are requirements, not regressions, and must not be
marked xfail or skipped.  Synthetic data only; no network or accounts.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import test_u1a_recovery as h
import test_u1a_reuse_gate as g
from core.library_repository import LibraryRepository
from core.task_coordinator import TaskCoordinator, DeliveryOutcome
from core.page_delivery import _Fault


def settle(repo, job):
    repo.finish_source(job['batch_id'], coverage_complete=True)
    assert repo.settle_batch(job['batch_id'])


def foreign_hold(repo, root):
    first = g.submit(repo, request_id='first-two')
    ids = g.accept(repo, first['batch_id'], g.source((g.candidate(pages=(0, 1)),)))
    transport = h.Transport(h.Response(), h.Response(status=404))
    assert h.run(repo, first, ids[0], h.executor(repo, root, transport)).state == 'completed'
    assert h.run(repo, first, ids[1], h.executor(repo, root, transport)).state == 'failed'
    settle(repo, first)
    h.target(root).write_bytes(h.SAME_SIZE_PNG)
    second, (hold,) = h.second_task(repo, root)
    settle(repo, second)
    resumed = repo.resume(first['job_id'], 'resume-first')
    assert repo.activate_next()['batch_id'] == resumed['batch_id']
    assert repo.carry_unresolved_items(resumed['batch_id'])['count'] == 1
    return first, second, resumed, hold


@pytest.mark.parametrize('operation', ['reuse', 'repair'])
def test_foreign_handover_does_not_strand_original_owner(tmp_path, operation):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        _, second, resumed, hold = foreign_hold(repo, tmp_path)
        if operation == 'reuse':
            h.target(tmp_path).write_bytes(h.PNG_BYTES)
        else:
            h.target(tmp_path).unlink()
        result = repo.recover_verification_page(resumed['batch_id'], h.ARTWORK, 0)
        assert result['outcome'] == ('reused' if operation == 'reuse' else 'repair_queued')
        if operation == 'repair':
            assert h.run(repo, resumed, result['repair_delivery_id'],
                         h.executor(repo, tmp_path, h.Transport(h.Response()))).state == 'completed'
        settle(repo, resumed)
        original_resume = repo.resume(second['job_id'], 'resume-original-owner')
        assert repo.activate_next()['batch_id'] == original_resume['batch_id']
        carried = repo.carry_unresolved_items(original_resume['batch_id'])
        assert carried['count'] == 1
        recovered = repo.recover_verification_page(original_resume['batch_id'], h.ARTWORK, 0)
        print('ORIGINAL_OWNER_AFTER_HANDOVER', operation, recovered['outcome'], recovered['reason'],
              h.pages(repo, original_resume))
        assert recovered['outcome'] in {'reused', 'completed'}, recovered


def test_pending_with_unobservable_artifact_is_not_dispatchable(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        monkeypatch.setattr(repo, '_observe_artifact', lambda *a, **k: None)
        result = repo.recover_verification_page(job['batch_id'], h.ARTWORK, 0)
        dispatched = repo.list_dispatchable(job['job_id'])
        print('UNOBSERVABLE_PENDING', result['outcome'], result['reason'], result['state'], dispatched)
        assert result['outcome'] == 'held'
        assert pending not in dispatched


def test_pending_without_bound_resolver_is_not_dispatchable(tmp_path):
    database = tmp_path / 'ledger.sqlite3'
    with LibraryRepository(database) as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
    with LibraryRepository(database) as reopened:
        result = reopened.recover_verification_page(job['batch_id'], h.ARTWORK, 0)
        dispatched = reopened.list_dispatchable(job['job_id'])
        print('NO_RESOLVER_PENDING', result['outcome'], result['reason'], result['state'], dispatched)
        assert result['outcome'] == 'held'
        assert pending not in dispatched


@pytest.mark.parametrize('mutation', ['artifact_id', 'sha256', 'size_bytes'])
def test_negative_recovery_binds_the_recorded_artifact(tmp_path, monkeypatch, mutation):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        job, hold, _ = h.parked_hold(repo, tmp_path)
        h.target(tmp_path).unlink()
        observe = repo._observe_artifact

        def altered(*args, **kwargs):
            record = observe(*args, **kwargs)
            changed = {'artifact_id': 'nonexistent-artifact', 'sha256': '0' * 64,
                       'size_bytes': record.expectation.size_bytes + 1}[mutation]
            return replace(record, expectation=replace(record.expectation, **{mutation: changed}))

        monkeypatch.setattr(repo, '_observe_artifact', altered)
        result = repo.recover_verification_page(job['batch_id'], h.ARTWORK, 0)
        print('NEGATIVE_BINDING', mutation, result['outcome'], result['reason'])
        assert result['outcome'] == 'refused', result
        assert repo.get_delivery(hold)['state'] == 'needs_verification'


def test_fatal_owner_stops_the_new_service_threads(tmp_path, monkeypatch):
    repo = LibraryRepository(tmp_path / 'ledger.sqlite3')
    service = TaskCoordinator(repo, lambda *_: DeliveryOutcome('failed'), workers=1)

    def fail_start():
        raise RuntimeError('synthetic-recovery-failure')

    monkeypatch.setattr(repo, 'recover_interrupted', fail_start)
    try:
        with pytest.raises(RuntimeError, match='synthetic-recovery-failure'):
            service.start().result(5)
        service._owner.join(1)
        service._observer.join(0.4)
        service._control_thread.join(0.4)
        print('FATAL_OWNER_THREADS', service._shutdown.is_set(), service._observer.is_alive(),
              service._control_thread.is_alive())
        assert not service._observer.is_alive()
        assert not service._control_thread.is_alive()
    finally:
        try:
            service.close(5)
        except RuntimeError:
            pass


def test_pause_admission_cannot_be_based_on_a_stale_database_read(tmp_path, monkeypatch):
    repo = LibraryRepository(tmp_path / 'ledger.sqlite3')
    service = TaskCoordinator(repo, lambda *_: DeliveryOutcome('failed'), workers=1)
    entered, release = threading.Event(), threading.Event()
    activated, settling, settle_release = (threading.Event() for _ in range(3))
    result = []
    error = []
    caller = None
    original_activate = repo.activate_next
    original_settle = repo.settle_stop

    def activate():
        value = original_activate()
        if value:
            activated.set()
        return value

    def settle_later(*args):
        settling.set()
        assert settle_release.wait(5)
        return original_settle(*args)

    monkeypatch.setattr(repo, 'activate_next', activate)
    monkeypatch.setattr(repo, 'settle_stop', settle_later)
    try:
        service.start().result(5)
        job = service.submit(h.request('admission-race'), 'submit-race').result(5)
        service.run_queued().result(5)
        assert activated.wait(5)
        assert repo.get_batch(job['batch_id'])['phase'] == 'running'
        original = repo.get_batch

        def stale_read(batch_id):
            value = original(batch_id)
            if threading.current_thread().name == 'audit-admitter':
                entered.set()
                assert release.wait(5)
            return value

        monkeypatch.setattr(repo, 'get_batch', stale_read)

        def admit():
            try:
                result.append(service.accept_page(job['batch_id'], h.source((h.candidate(),))))
            except BaseException as exc:
                error.append(exc)

        caller = threading.Thread(target=admit, name='audit-admitter')
        caller.start()
        assert entered.wait(5)
        service.pause(job['job_id'], 'pause-before-enqueue').result(5)
        assert settling.wait(5)
        assert original(job['batch_id'])['phase'] == 'pausing'
        release.set()
        caller.join(5)
        assert not caller.is_alive() and not error
        try:
            outcome = result[0].result(5)
        except Exception:
            outcome = None
        print('PAUSE_LATE_ADMISSION', outcome)
        assert outcome is None, 'A page enqueued after durable pause was still accepted'
    finally:
        release.set()
        settle_release.set()
        if caller is not None:
            caller.join(5)
        service.close(5)


def test_accept_corruption_stops_activation_of_later_jobs(tmp_path, monkeypatch):
    repo = LibraryRepository(tmp_path / 'ledger.sqlite3')
    first, _ = h.first_delivery(repo, tmp_path)
    repo.record_removal_intent(first['batch_id'], h.ARTWORK, 0, h.OUTPUT_REF,
                              intent='none', command_id='intent-to-corrupt')
    with sqlite3.connect(repo.database_path) as conn:
        conn.execute("UPDATE events SET safe_payload='{broken' WHERE kind='removal_intent'")
    activated = threading.Event()
    activate = repo.activate_next
    activations = []

    def traced_activate():
        value = activate()
        if value:
            activations.append(value['batch_id'])
            activated.set()
        return value

    monkeypatch.setattr(repo, 'activate_next', traced_activate)
    service = TaskCoordinator(repo, h.executor(repo, tmp_path, h.Transport()), workers=1)
    try:
        service.start().result(5)
        job = service.submit(h.request('corrupt-source'), 'corrupt-source-submit').result(5)
        later = service.submit(h.request('later-source'), 'later-source-submit').result(5)
        service.run_queued().result(5)
        assert activated.wait(5)
        activated.clear()
        from core.library_repository import CorruptDataError
        with pytest.raises(CorruptDataError):
            service.accept_page(job['batch_id'], h.source((h.candidate(),))).result(5)
        # This is precisely CollectionRuntime._source_loop's error handler.
        repo.request_block(job['batch_id'], 'source_runner_failed')
        later_activated = activated.wait(0.5)
        print('CORRUPT_ACCEPT_ACTIVATION', later_activated, repo.get_batch(later['batch_id'])['phase'])
        assert not later_activated
        assert repo.get_batch(later['batch_id'])['phase'] == 'queued'
    finally:
        service.close(5)


def test_pause_cancels_remaining_file_observation(tmp_path, monkeypatch):
    repo = LibraryRepository(tmp_path / 'ledger.sqlite3')
    first = h.submit(repo, request_id='two-completed')
    ids = h.accept(repo, first['batch_id'], h.source((h.candidate(), h.candidate(h.OTHER_ARTWORK))))
    ex = h.executor(repo, tmp_path, h.Transport(h.Response(), h.Response()))
    for value in ids:
        assert h.run(repo, first, value, ex).state == 'completed'
    settle(repo, first)
    service = TaskCoordinator(repo, ex, workers=1)
    entered, release, activated = (threading.Event() for _ in range(3))
    observe, activate = repo._observe_artifact, repo.activate_next
    observed = []

    def traced_observe(*args, **kwargs):
        observed.append(args[0]['artifact_id'])
        if len(observed) == 1:
            entered.set()
            assert release.wait(5)
        return observe(*args, **kwargs)

    def traced_activate():
        value = activate()
        if value:
            activated.set()
        return value

    monkeypatch.setattr(repo, '_observe_artifact', traced_observe)
    monkeypatch.setattr(repo, 'activate_next', traced_activate)
    try:
        service.start().result(5)
        job = service.submit(h.request('pause-hashing'), 'pause-hashing-submit').result(5)
        service.run_queued().result(5)
        assert activated.wait(5)
        future = service.accept_page(job['batch_id'], h.source((h.candidate(), h.candidate(h.OTHER_ARTWORK))))
        assert entered.wait(5)
        service.pause(job['job_id'], 'stop-hashing').result(5)
        release.set()
        try:
            future.result(5)
        except Exception:
            pass
        print('OBSERVATIONS_AFTER_PAUSE', len(observed))
        assert len(observed) <= 1, 'Pause should not start the next image hash'
    finally:
        release.set()
        service.close(5)


def test_publication_gate_wins_over_a_later_exclusion_boundary_probe(tmp_path, monkeypatch):
    """Characterize the chosen gate-wins contract, not a stricter link-wins rule.

    The initial audit hypothesis expected exclusion to win until os.link. The
    frozen contract instead puts the decision at publish_authorized. The
    observed result is therefore a boundary to document, not itself a defect.
    """
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        service = h.executor(repo, tmp_path, h.Transport(h.Response()))
        authorize = repo.publish_authorized
        accepted = []

        def decision_then_exclusion(delivery_id):
            result = authorize(delivery_id)
            accepted.append(repo.record_removal_intent(job['batch_id'], h.ARTWORK, 0,
                h.OUTPUT_REF, intent='excluded', command_id='excluded-after-read-before-link'))
            assert not h.target(tmp_path).exists()
            return result

        monkeypatch.setattr(repo, 'publish_authorized', decision_then_exclusion)
        outcome = h.run(repo, job, pending, service)
        print('READ_LINK_GAP', bool(accepted), outcome.state, h.target(tmp_path).exists())
        assert outcome.state == 'completed'
        assert h.target(tmp_path).exists()
        assert authorize(pending)['allowed'] is False


def test_intact_reuse_and_history_still_work(tmp_path):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        job, hold, first = h.parked_hold(repo, tmp_path)
        history = h.receipt(repo, first)
        h.target(tmp_path).write_bytes(h.PNG_BYTES)
        result = repo.recover_verification_page(job['batch_id'], h.ARTWORK, 0)
        assert result['outcome'] == 'reused'
        assert h.receipt(repo, first) == history


def test_excluded_recovery_needed_can_finish_local_recovery(tmp_path):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)

        def stop_before_publish(stage, row):
            if stage == 'before_publish':
                raise _Fault('synthetic-publish-interruption')

        service = h.executor(repo, tmp_path, h.Transport(h.Response()), fault_hook=stop_before_publish)
        assert h.run(repo, job, pending, service).state == 'recovery_needed'
        assert not h.target(tmp_path).exists()
        repo.record_removal_intent(job['batch_id'], h.ARTWORK, 0, h.OUTPUT_REF,
                                  intent='excluded', command_id='exclude-recovery-needed')
        recovered_service = h.executor(repo, tmp_path, h.Transport())
        try:
            result = recovered_service.recover_local()
        except Exception as exc:
            print('EXCLUDED_RECOVERY_NEEDED_ERROR', type(exc).__name__, repo.get_delivery(pending)['state'])
            raise
        assert (pending, 'cancelled') in result
        assert not h.target(tmp_path).exists()


def test_cancelled_owned_temp_can_be_cleaned_after_transient_unlink_failure(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)

        def exclude(stage, row):
            if stage == 'before_publish':
                repo.record_removal_intent(job['batch_id'], h.ARTWORK, 0, h.OUTPUT_REF,
                                          intent='excluded', command_id='exclude-cleanup-failure')

        service = h.executor(repo, tmp_path, h.Transport(h.Response()), fault_hook=exclude)
        original_unlink = Path.unlink

        def fail_part_unlink(path, *args, **kwargs):
            if path.suffix == '.part':
                raise PermissionError('synthetic transient file lock')
            return original_unlink(path, *args, **kwargs)

        with monkeypatch.context() as scope:
            scope.setattr(Path, 'unlink', fail_part_unlink)
            assert h.run(repo, job, pending, service).state == 'cancelled'
        assert list(tmp_path.rglob('*.part'))
        recovered_service = h.executor(repo, tmp_path, h.Transport())
        result = recovered_service.recover_local()
        cleaned = recovered_service.cleanup_delivery(pending)
        leftovers = list(tmp_path.rglob('*.part'))
        print('CANCELLED_STAGED_CLEANUP', result, cleaned, len(leftovers))
        assert not leftovers, 'The owned part has no retryable cleanup path after cancellation'