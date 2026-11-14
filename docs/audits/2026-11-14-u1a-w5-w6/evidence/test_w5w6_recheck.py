"""Independent W5/W6 audit; synthetic data only, expected safety contracts."""
from pathlib import Path
import json
import shutil
import sqlite3
import sys
import os

ROOT = Path(os.environ.get('PIXIVKEEP_AUDIT_REPO', Path(__file__).resolve().parents[4])).resolve()
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import pytest
from core import library_maintenance as m
from core import library_audit as audit
from core.library_repository import LibraryRepository
from core.library_paths import PathResolver
from core.app_paths import AppPaths
import test_u1a_library_maintenance as mt
import test_u1a_library_audit as at
import test_collection_verification_panel as ui


def state(path):
    with sqlite3.connect(path) as conn:
        return conn.execute('SELECT state FROM jobs').fetchone()[0]


def test_wc01_same_counts_changed_backup_is_rejected(tmp_path):
    source = mt.make_library(tmp_path / 'source' / 'library.sqlite3', jobs=1)
    manifest = m.backup_library(source, tmp_path / 'backups', label='snapshot')
    copy = Path(manifest['manifest_path']).parent / source.name
    with sqlite3.connect(copy) as conn:
        conn.execute("UPDATE jobs SET state='cancelled'")
    result = m.verify_library_copy(source, copy)
    print('WC01_ROWS', state(source), state(copy), result)
    assert result['ok'] is False, 'Changed row contents must not verify as a faithful backup'


def test_wc01_same_object_names_different_ddl_is_rejected(tmp_path):
    source = mt.make_library(tmp_path / 'source.sqlite3', jobs=1,
        statements=['CREATE INDEX extra_same_name ON jobs(state)'])
    copy = mt.make_library(tmp_path / 'copy.sqlite3', jobs=1,
        statements=['CREATE INDEX extra_same_name ON jobs(request_id)'])
    result = m.verify_library_copy(source, copy)
    print('WC01_DDL', result)
    assert result['objects_match_source'] is False
    assert result['ok'] is False


def test_control_online_snapshot_wal_is_valid(tmp_path):
    source = tmp_path / 'source' / 'library.sqlite3'
    writer = mt.open_library(source, jobs=1, wal=True)
    try:
        manifest = m.backup_library(source, tmp_path / 'backups', label='online')
        copy = Path(manifest['manifest_path']).parent / source.name
        result = m.verify_library_copy(source, copy)
        print('CONTROL_WAL', result)
        assert result['ok'] is True
        assert state(copy) == 'queued'
    finally:
        writer.close()


def test_control_restore_file_group_does_not_replay_destination_wal(tmp_path):
    source = mt.make_library(tmp_path / 'source' / 'library.sqlite3', jobs=1, wal=True)
    manifest = m.backup_library(source, tmp_path / 'backups', label='filegroup', mode='file_group')
    backup = Path(manifest['manifest_path']).parent
    destination = tmp_path / 'restored' / source.name
    destination.parent.mkdir()
    writer = sqlite3.connect(source, isolation_level=None)
    try:
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("UPDATE jobs SET state='cancelled'")
        # Preserve a crashed destination group in an isolated directory.
        for suffix in ('', '-wal', '-shm'):
            src = Path(str(source) + suffix)
            if src.exists():
                shutil.copy2(src, str(destination) + suffix)
    finally:
        writer.close()
    # Follow the generated file_group instructions exactly: copy all backup files.
    for fact in manifest['files']:
        shutil.copy2(backup / fact['name'], destination.parent / fact['name'])
    actual = state(destination)
    print('CONTROL_RESTORE', manifest['restore_steps'], manifest['files'], actual)
    assert actual == 'queued', 'Restoring the sealed set must not replay an old destination WAL'


def test_wc02_scan_budget_unknown_is_not_all_verified(tmp_path, capsys):
    root = tmp_path / 'gallery'
    root.mkdir()
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        repo.bind_path_resolver(PathResolver(AppPaths(root)))
        at.publish(repo, root, at.ARTWORK)
        result = audit.scan_library_artifacts(repo, budget=audit.ScanBudget(max_bytes=1))
        code = m.main(['scan', str(repo.database_path), '--root', str(root), '--max-bytes', '1'])
        out = capsys.readouterr().out
    print('WC02_SCAN', result, code, out)
    assert result['stats']['matched'] == 0
    assert result['stats']['unknown'] == 1
    assert '每一行都与记录一致' not in out
    assert '旧行或缺摘要' not in out


@pytest.mark.parametrize('value', [
    {'outcome':'held', 'reason':'artifact_changed', 'illust_id':'100', 'page_index':0},
    {'outcome':'refused', 'reason':'batch_not_running', 'illust_id':'100', 'page_index':0},
    {'outcome':'mystery', 'reason':'brand_new_reason', 'illust_id':'100', 'page_index':0},
])
def test_wc03_ui_final_result_preserves_non_success(value):
    controller, view, _ = ui._controller([ui._row()], ui._summary({'needs_verification':1}),
        ui._FakeSession(results={'100':value}))
    ui._open(controller, view)
    controller._recover_verification([ui._key()])
    for _ in range(4):
        controller._poll()
    last = view.named('set_verification_result')[-1][1]
    print('WC03_UI', value, view.named('set_verification_result'))
    assert last[1] != 'success', 'Processing finished is not equivalent to a successful verification'


def test_control_ui_reused_can_report_success():
    controller, view, _ = ui._controller([ui._row()], ui._summary({}), ui._FakeSession())
    ui._open(controller, view)
    controller._recover_verification([ui._key()])
    for _ in range(4):
        controller._poll()
    assert view.named('set_verification_result')[-1][1][1] == 'success'


def test_wc04_ui_ignores_rows_for_previous_selected_batch():
    from concurrent.futures import Future
    session = ui._FakeSession()
    controller, view, _ = ui._controller([], ui._summary({}), session)
    late = Future()
    runtime = controller.runtime
    runtime.verification_pages = lambda **kwargs: late
    runtime.request = lambda job: Future()
    runtime.snapshot = lambda job: Future()
    controller._task_rows = {'job-a':{'batch_id':'batch-a'}, 'job-b':{'batch_id':'batch-b'}}
    controller.handle('select_task', {'id':'job-a'})
    controller.handle('navigate', {'page':'verify'})
    controller.handle('navigate', {'page':'tasks'})
    controller.handle('select_task', {'id':'job-b'})
    controller.handle('navigate', {'page':'verify'})
    late.set_result({'rows':[ui._row(batch='batch-a')], 'truncated':False,
        'stats':{'batch_id':'batch-a'}})
    for _ in range(3):
        controller._poll()
    controller.handle('verification_recover', {'key':ui._key(batch='batch-a')})
    print('WC04_STALE', controller._current_batch, controller._verify_display, session.commands)
    assert not session.commands, 'A late old-batch row must not remain actionable under the new selection'


def test_wc05_scan_malformed_numeric_row_is_reported_not_traceback(tmp_path):
    root = tmp_path / 'gallery'
    root.mkdir()
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        repo.bind_path_resolver(PathResolver(AppPaths(root)))
        at.publish(repo, root, at.ARTWORK)
        with sqlite3.connect(repo.database_path) as conn:
            conn.execute("UPDATE artifacts SET size='broken-size'")
        assert m.inspect_library(repo.database_path)['healthy'] is True
        result = audit.scan_library_artifacts(repo)
        print('WC05_SCAN_ROW', result)
        assert result['rows'][0]['verdict'] == 'unknown'
        assert result['rows'][0]['reason'] == 'artifact_record_invalid'


def test_wc06_summary_accounts_for_artifact_only_pages(tmp_path):
    import test_u1a_recovery as h
    root = tmp_path / 'gallery'
    root.mkdir()
    with LibraryRepository(tmp_path / 'ledger.sqlite3') as repo:
        repo.bind_path_resolver(PathResolver(AppPaths(root)))
        at.publish(repo, root, at.ARTWORK)
        job = h.submit(repo, request_id='reuse-only')
        h.accept(repo, job['batch_id'], h.source((h.candidate(at.ARTWORK),)))
        summary = repo.verification_summary(batch_id=job['batch_id'])
        links = h.query(repo.database_path, 'SELECT delivery_id,artifact_id FROM batch_item_pages WHERE batch_id=?',
            (job['batch_id'],))
        print('WC06_SUMMARY', summary, links, ui.summary_text(summary))
        # Presentation/design gap, not a downloader state-machine failure.
        # A dedicated existing_pages count is also acceptable; do not forge a delivery.
        assert summary['counts'].get('completed', 0) + summary.get('existing_pages', 0) == 1
