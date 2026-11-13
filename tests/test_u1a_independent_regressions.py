"""Independent expected-behavior regressions, pinned to the audit baseline.

Imported from docs/audits/2026-11-13-u1a-independent/evidence/test_independent_u1a.py
(at 892a140).  Only the repository-root lookup changed; every behavioral
assertion is the audit's.  Twelve of these tests fail until UA-01..UA-11 are
fixed: they state required behaviour, are not marked xfail/skip, and must not
be relaxed.  Only temporary SQLite ledgers and fake HTTP fixtures are used.
"""
import os
import sys
import json
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import pytest
import test_u1a_recovery as h
import test_u1a_reuse_gate as g
from core.library_repository import LibraryRepository


def settle(repo, job):
    repo.finish_source(job['batch_id'], coverage_complete=True)
    assert repo.settle_batch(job['batch_id'])


def test_excluded_from_historical_task_stops_other_task_pending_repair(tmp_path):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        first, _ = h.first_delivery(repo, tmp_path)
        h.target(tmp_path).unlink()
        second, (pending,) = h.second_task(repo, tmp_path)
        repo.record_removal_intent(first['batch_id'], h.ARTWORK, 0, h.OUTPUT_REF,
                                  intent='excluded', command_id='exclude-from-history')
        actual = repo.get_delivery(pending)['state']
        transport = h.Transport(h.Response())
        if repo.list_dispatchable(second['job_id']):
            outcome = h.run(repo, second, pending, h.executor(repo, tmp_path, transport))
            print('CROSS_JOB_INTENT', json.dumps({'before_execution': actual, 'outcome': outcome.state,
                  'fake_gets': len(transport.calls), 'file_recreated': h.target(tmp_path).exists()}))
        assert not h.target(tmp_path).exists(), 'EXCLUDED accepted from historical task but another task recreated its file'
        assert repo.list_dispatchable(second['job_id']) == ()


def test_excluded_from_historical_task_prevents_recovery_repair(tmp_path):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        first, _ = h.first_delivery(repo, tmp_path)
        h.target(tmp_path).write_bytes(h.SAME_SIZE_PNG)
        second, (hold,) = h.second_task(repo, tmp_path)
        repo.record_removal_intent(first['batch_id'], h.ARTWORK, 0, h.OUTPUT_REF,
                                  intent='excluded', command_id='exclude-before-recovery')
        h.target(tmp_path).unlink()
        result = repo.recover_verification_page(second['batch_id'], h.ARTWORK, 0)
        print('EXCLUDED_RECOVERY', result['outcome'], result['reason'])
        assert result['outcome'] != 'repair_queued', 'Recovery ignored a current global EXCLUDED intent'
        assert repo.list_dispatchable(second['job_id']) == ()


def test_recovery_uses_matching_older_artifact_when_latest_version_differs(tmp_path):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        first, original = h.first_delivery(repo, tmp_path)
        h.target(tmp_path).unlink()
        second, (repair,) = h.second_task(repo, tmp_path)
        assert h.run(repo, second, repair, h.executor(repo, tmp_path, h.Transport(h.Response(h.SAME_SIZE_PNG)))).state == 'completed'
        settle(repo, second)
        h.target(tmp_path).write_bytes(b'X' * len(h.PNG_BYTES))
        third, (hold,) = h.second_task(repo, tmp_path, request_id='third')
        assert repo.get_delivery(hold)['state'] == 'needs_verification'
        h.target(tmp_path).write_bytes(h.PNG_BYTES)
        result = repo.recover_verification_page(third['batch_id'], h.ARTWORK, 0)
        print('OLDER_VERSION', result['outcome'], result['reason'], result['artifact_id'])
        assert result['outcome'] == 'reused', 'A matching recorded older version must be reusable'
        assert result['artifact_id'] == repo.get_artifact(original)['artifact_id']
        assert h.target(tmp_path).read_bytes() == h.PNG_BYTES


def test_recovery_rejects_stale_missing_evidence(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        second, hold, _ = h.parked_hold(repo, tmp_path)
        h.target(tmp_path).unlink()
        observe = repo._observe_artifact
        def stale(*args, **kwargs):
            record = observe(*args, **kwargs)
            return replace(record, checked_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
        monkeypatch.setattr(repo, '_observe_artifact', stale)
        result = repo.recover_verification_page(second['batch_id'], h.ARTWORK, 0)
        print('STALE_MISSING', result['outcome'], result['reason'])
        assert result['outcome'] != 'repair_queued', 'Missing evidence must pass the same freshness gate as hash_match'
        assert repo.get_delivery(hold)['state'] == 'needs_verification'


def test_carried_foreign_hold_can_be_recovered_by_its_current_batch(tmp_path):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        first = g.submit(repo, request_id='first-two')
        ids = g.accept(repo, first['batch_id'], g.source((g.candidate(pages=(0, 1)),)))
        transport = h.Transport(h.Response(), h.Response(status=404))
        assert h.run(repo, first, ids[0], h.executor(repo, tmp_path, transport)).state == 'completed'
        assert h.run(repo, first, ids[1], h.executor(repo, tmp_path, transport)).state == 'failed'
        settle(repo, first)
        h.target(tmp_path).write_bytes(h.SAME_SIZE_PNG)
        second, (foreign_hold,) = h.second_task(repo, tmp_path)
        settle(repo, second)
        resumed = repo.resume(first['job_id'], 'resume-first')
        assert repo.activate_next()['batch_id'] == resumed['batch_id']
        assert repo.carry_unresolved_items(resumed['batch_id'])['count'] == 1
        h.target(tmp_path).write_bytes(h.PNG_BYTES)
        result = repo.recover_verification_page(resumed['batch_id'], h.ARTWORK, 0)
        print('FOREIGN_HOLD', result['outcome'], result['reason'], repo.get_delivery(foreign_hold)['job_id'] != first['job_id'])
        assert result['outcome'] == 'reused', 'Carry linked a foreign hold which recovery cannot own'


def test_pending_recovery_with_root_offline_is_really_held(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        second, pending, _ = h.queued_repair(repo, root)
        offline = tmp_path / 'library-offline'
        assert root.resolve().is_relative_to(tmp_path.resolve())
        assert offline.resolve().is_relative_to(tmp_path.resolve())
        assert not root.is_symlink() and not root.is_junction()
        root.rename(offline)
        result = repo.recover_verification_page(second['batch_id'], h.ARTWORK, 0)
        print('OFFLINE_PENDING', result['outcome'], result['reason'], result['state'], repo.list_dispatchable(second['job_id']))
        assert result['outcome'] == 'held'
        if repo.list_dispatchable(second['job_id']):
            transport = h.Transport(h.Response())
            outcome = h.run(repo, second, pending, h.executor(repo, root, transport))
            print('OFFLINE_ROOT_RECREATED', root.exists(), outcome.state, len(transport.calls))
        assert not root.exists(), 'A held offline root was recreated and downloaded into'
        assert repo.list_dispatchable(second['job_id']) == (), 'Held result still leaves repair dispatchable with root unavailable'


def test_same_page_receipt_replay_does_not_hash_again(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        h.first_delivery(repo, tmp_path)
        second = h.submit(repo, request_id='replay')
        page = h.source((h.candidate(),), page_identity='replay-page')
        observe = repo._observe_artifact
        calls = []
        def count(*args, **kwargs):
            calls.append(str(args[0]['artifact_id']))
            return observe(*args, **kwargs)
        monkeypatch.setattr(repo, '_observe_artifact', count)
        one = repo.accept_source_page(second['batch_id'], page)
        two = repo.accept_source_page(second['batch_id'], page)
        assert one == two
        print('REPLAY_HASH_COUNT', len(calls))
        assert len(calls) == 1, 'An accepted idempotent page replay rehashed the same artifact'


def test_intact_recovery_preserves_original_receipt(tmp_path):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        second, hold, original = h.parked_hold(repo, tmp_path)
        previous = h.receipt(repo, original)
        h.target(tmp_path).write_bytes(h.PNG_BYTES)
        result = repo.recover_verification_page(second['batch_id'], h.ARTWORK, 0)
        assert result['outcome'] == 'reused'
        assert h.receipt(repo, original) == previous


def test_changed_recovery_preserves_foreign_bytes(tmp_path):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        second, hold, _ = h.parked_hold(repo, tmp_path)
        result = repo.recover_verification_page(second['batch_id'], h.ARTWORK, 0)
        assert result['outcome'] == 'held'
        assert repo.get_delivery(hold)['state'] == 'needs_verification'
        assert h.target(tmp_path).read_bytes() == h.SAME_SIZE_PNG


def test_corrupt_intent_does_not_silently_allow_automatic_repair(tmp_path):
    import sqlite3
    from core.library_repository import CorruptDataError
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        first, _ = h.first_delivery(repo, tmp_path)
        h.target(tmp_path).unlink()
        repo.record_removal_intent(first['batch_id'], h.ARTWORK, 0, h.OUTPUT_REF,
                                  intent='excluded', command_id='corrupt-intent')
        # Fault injection into the synthetic ledger, while no writer command runs.
        with sqlite3.connect(repo.database_path) as conn:
            conn.execute("UPDATE events SET safe_payload = ? WHERE kind = 'removal_intent'", ('{broken',))
        second = h.submit(repo, request_id='after-corruption')
        try:
            repo.accept_source_page(second['batch_id'], h.source((h.candidate(),)))
        except CorruptDataError:
            return
        print('CORRUPT_INTENT_DISPATCHABLE', repo.list_dispatchable(second['job_id']))
        assert repo.list_dispatchable(second['job_id']) == (), 'Unreadable exclusion silently became permission to repair'


def test_exclusion_during_transfer_prevents_publication(tmp_path):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        h.first_delivery(repo, tmp_path)
        h.target(tmp_path).unlink()
        second, (pending,) = h.second_task(repo, tmp_path)
        def exclude():
            repo.record_removal_intent(second['batch_id'], h.ARTWORK, 0, h.OUTPUT_REF,
                                      intent='excluded', command_id='exclude-in-transfer')
        transport = h.Transport(h.Response(chunks=[exclude, h.PNG_BYTES]))
        outcome = h.run(repo, second, pending, h.executor(repo, tmp_path, transport))
        print('EXCLUDE_TRANSFER', outcome.state, h.target(tmp_path).exists())
        assert not h.target(tmp_path).exists(), 'Accepted EXCLUDED intent did not prevent final publication'


def test_hash_preflight_does_not_block_coordinator_control_commands(tmp_path, monkeypatch):
    import threading
    import time
    from core.task_coordinator import TaskCoordinator
    entered, release = threading.Event(), threading.Event()
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        h.first_delivery(repo, tmp_path)
        service = h.executor(repo, tmp_path, h.Transport())
        coordinator = TaskCoordinator(repo, service, workers=1)
        try:
            coordinator.start().result(5)
            second = coordinator.submit(h.request('coordinator-probe'), 'submit-coordinator-probe').result(5)
            coordinator.run_queued().result(5)
            deadline = time.monotonic() + 5
            while coordinator.active_batch_id != second['batch_id'] and time.monotonic() < deadline:
                time.sleep(0.01)
            assert coordinator.active_batch_id == second['batch_id']
            observe = repo._observe_artifact
            def delayed(*args, **kwargs):
                entered.set()
                assert release.wait(5), 'Probe did not release its bounded I/O latch'
                return observe(*args, **kwargs)
            monkeypatch.setattr(repo, '_observe_artifact', delayed)
            accepted = coordinator.accept_page(second['batch_id'], h.source((h.candidate(),)))
            assert entered.wait(3)
            snapshot = coordinator.snapshot(second['job_id'])
            pause = coordinator.pause(second['job_id'], 'pause-during-io')
            time.sleep(0.15)
            control_responded = snapshot.done() and pause.done()
            print('CONTROL_WHILE_HASHING', snapshot.done(), pause.done())
            release.set()
            accepted.result(5)
            snapshot.result(5)
            pause.result(5)
            assert control_responded, 'Disk observation owns the only control loop and delays snapshot/pause commands'
        finally:
            release.set()
            coordinator.close(timeout=10)


def test_completion_between_preflight_and_accept_does_not_create_a_false_hold(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        job = h.submit(repo, request_id='completion-race')
        (pending,) = repo.accept_source_page(job['batch_id'], h.source((h.candidate(),)))
        transport = h.Transport(h.Response())
        service = h.executor(repo, tmp_path, transport)
        preflight = repo._preflight_page_health
        observed = []
        def finish_after_snapshot(batch_id, page):
            health = preflight(batch_id, page)
            observed.append(len(health))
            assert h.run(repo, job, pending, service).state == 'completed'
            return health
        monkeypatch.setattr(repo, '_preflight_page_health', finish_after_snapshot)
        repo.accept_source_page(job['batch_id'], h.source((h.candidate(),), page_identity='second-source-page',
                                before='"cursor-1"', after='"cursor-2"'))
        settle(repo, job)
        counts = repo.batch_results(job['batch_id'])['pages']
        print('COMPLETION_RACE', observed, counts, repo.get_batch(job['batch_id'])['phase'])
        assert h.target(tmp_path).read_bytes() == h.PNG_BYTES
        assert len(transport.calls) == 1
        assert counts['needs_verification'] == 0, 'A valid concurrent completion became an artifact_unknown hold'
        assert repo.get_batch(job['batch_id'])['phase'] == 'completed'


def test_carry_parses_the_batch_health_map_once(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        job = h.submit(repo, request_id='carry-parsing')
        candidates = []
        for illust in ('123', '456', '789'):
            one, two = h.candidate(illust, page_index=0, page_count=2), h.candidate(illust, page_index=1, page_count=2)
            candidates.append(replace(one, pages=one.pages + two.pages))
        ids = repo.accept_source_page(job['batch_id'], h.source(candidates))
        transport = h.Transport(*[response for _ in candidates for response in (h.Response(), h.Response(status=404))])
        service = h.executor(repo, tmp_path, transport)
        for index, delivery in enumerate(ids):
            assert h.run(repo, job, delivery, service).state == ('completed' if index % 2 == 0 else 'failed')
        settle(repo, job)
        resumed = repo.resume(job['job_id'], 'resume-parsing')
        assert repo.activate_next()['batch_id'] == resumed['batch_id']
        parse = repo._health_map
        sizes = []
        def count(value):
            sizes.append(len(value))
            return parse(value)
        monkeypatch.setattr(repo, '_health_map', count)
        result = repo.carry_unresolved_items(resumed['batch_id'])
        assert result['count'] == 3
        print('CARRY_HEALTH_REPARSE', sizes)
        assert len(sizes) == 1, 'Entire health map is reparsed once per carried work inside the write transaction'