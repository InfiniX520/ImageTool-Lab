"""Independent regressions against the delivered diagnostic/view/runtime contract."""
import threading
import time

import pytest

from core.collection_runtime import CollectionRuntime
from core.diagnostic_logging import DiagnosticLog
from core.library_repository import LibraryRepository
from core.log_view import LogView
from core.task_coordinator import TaskCoordinator, DeliveryOutcome
from pathlib import Path


def test_byte_limited_multiline_drain_keeps_event_order(tmp_path):
    log = DiagnosticLog(tmp_path / 'logs')
    try:
        log.emit('first\nsecondAA\nthirdBBB')
        log.emit('X')
        view = LogView(log)
        collected = []
        for _ in range(6):
            collected.extend(view.drain(max_bytes=15, max_ms=100).append_lines)
        assert collected == ['[*] first', 'secondAA', 'thirdBBB', '[*] X']
    finally:
        assert log.close()


def test_long_first_line_respects_strict_drain_byte_limit(tmp_path):
    log = DiagnosticLog(tmp_path / 'logs')
    try:
        log.emit('中文' * 1000)
        view = LogView(log)
        delta = view.drain(max_bytes=128, max_ms=100)
        assert sum(len(line.encode('utf-8')) for line in delta.append_lines) <= 128
        assert delta.append_lines
        assert 'truncated' in ''.join(delta.append_lines)
    finally:
        assert log.close()


def test_physical_flush_and_nonblocking_producers_when_writer_is_slow(tmp_path, monkeypatch):
    log = DiagnosticLog(tmp_path / 'logs')
    entered = threading.Event()
    release = threading.Event()
    original = log._write_batch
    def slow(batch):
        entered.set()
        assert release.wait(5)
        return original(batch)
    monkeypatch.setattr(log, '_write_batch', slow)
    try:
        log.emit('first')
        assert entered.wait(5)
        moment = time.monotonic()
        log.emit('second')
        assert log.snapshot()['sequence'] == 2
        assert time.monotonic() - moment < 0.1
        assert log.flush(timeout=0.02) is False
        release.set()
        assert log.flush(timeout=5)
        data = b''.join(path.read_bytes() for path in (tmp_path / 'logs').glob('*.jsonl'))
        assert b'first' in data and b'second' in data
    finally:
        release.set()
        assert log.close()


def test_runtime_does_not_report_closed_when_diagnostics_timeout(tmp_path):
    class Diagnostic:
        calls = 0
        def close(self, timeout):
            self.calls += 1
            return self.calls > 1
    diagnostic = Diagnostic()
    repo = LibraryRepository(tmp_path / 'ledger.sqlite3')
    runtime = CollectionRuntime(TaskCoordinator(repo, lambda row, stop: DeliveryOutcome('failed'), workers=1), diagnostic=diagnostic)
    runtime.start().result(5)
    try:
        with pytest.raises(TimeoutError):
            runtime.close()
        assert runtime.status()['closed'] is False
        assert repo._closed
    finally:
        runtime.close()
    assert diagnostic.calls == 2
    assert runtime.status()['closed']


def test_diagnostic_close_timeout_includes_storage_finalization(tmp_path, monkeypatch):
    log = DiagnosticLog(tmp_path / 'logs')
    entered = threading.Event()
    release = threading.Event()
    result = []
    original = log._finalize_session
    def slow():
        entered.set()
        assert release.wait(5)
        return original()
    monkeypatch.setattr(log, '_finalize_session', slow)
    caller = threading.Thread(target=lambda: result.append(log.close(timeout=0.03)))
    caller.start()
    try:
        assert entered.wait(5)
        caller.join(0.15)
        assert not caller.is_alive()
        assert result == [False]
        assert not log.closed
    finally:
        release.set()
        caller.join(5)
        assert log.close(timeout=5)


def test_cancelled_empty_log_export_preserves_previous_output(tmp_path):
    from core.app_paths import AppPaths
    from core.diagnostic_logging import DiagnosticLogError
    from core.library_paths import PathResolver
    from core.task_contracts import PathRef
    log = DiagnosticLog(tmp_path / 'logs')
    target = tmp_path / 'reports' / 'output.txt'
    target.parent.mkdir()
    target.write_text('previous', encoding='utf-8')
    cancelled = threading.Event()
    cancelled.set()
    try:
        with pytest.raises(DiagnosticLogError):
            log.export_text(PathRef(relative_path='reports/output.txt'), PathResolver(AppPaths(tmp_path)), stop_event=cancelled)
        assert target.read_text(encoding='utf-8') == 'previous'
    finally:
        assert log.close()


def test_rotation_file_delete_cannot_block_emit_or_ui_snapshot(tmp_path, monkeypatch):
    log = DiagnosticLog(tmp_path / 'logs', volume_bytes=1024, backup_count=0)
    entered, release, emitted = threading.Event(), threading.Event(), threading.Event()
    original = Path.unlink
    def slow_unlink(path, *args, **kwargs):
        if path.name.startswith('diagnostic_') and path.suffix == '.jsonl':
            entered.set()
            assert release.wait(5)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', slow_unlink)
    def produce():
        log.emit('producer remains available')
        log.snapshot()
        emitted.set()
    caller = threading.Thread(target=produce)
    try:
        for number in range(8):
            log.emit('volume filler ' + str(number) + 'x' * 500)
        assert entered.wait(5)
        caller.start()
        assert emitted.wait(0.15), 'Slow retention deletion held the producer/UI lock'
    finally:
        release.set()
        if caller.ident is not None:caller.join(5)
        assert log.close()


def test_old_session_file_probe_cannot_block_emit_or_ui_snapshot(tmp_path, monkeypatch):
    old = DiagnosticLog(tmp_path / 'logs')
    old.emit('closed prior session')
    assert old.close()
    old_names = {path.name for path in (tmp_path / 'logs').glob('*.jsonl')}
    log = DiagnosticLog(tmp_path / 'logs')
    entered, release, emitted = threading.Event(), threading.Event(), threading.Event()
    original = Path.is_file
    def slow_probe(path, *args, **kwargs):
        if path.name in old_names:
            entered.set()
            assert release.wait(5)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'is_file', slow_probe)
    maintainer = threading.Thread(target=log._maintain_storage)
    def produce():
        log.emit('producer remains available')
        log.snapshot()
        emitted.set()
    caller = threading.Thread(target=produce)
    maintainer.start()
    try:
        assert entered.wait(5)
        caller.start()
        assert emitted.wait(0.15), 'Slow retention file probe held the producer/UI lock'
    finally:
        release.set()
        maintainer.join(5)
        if caller.ident is not None:caller.join(5)
        assert log.close()


def test_repeated_retention_delete_failure_cannot_grow_volume_manifest(tmp_path, monkeypatch):
    log = DiagnosticLog(tmp_path / 'logs', volume_bytes=1024, backup_count=0)
    original = Path.unlink
    def refuse(path, *args, **kwargs):
        if path.name.startswith('diagnostic_') and path.suffix == '.jsonl':
            raise PermissionError('Synthetic deletion refused')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', refuse)
    try:
        for number in range(80):
            log.emit('volume filler ' + str(number) + 'x' * 700)
            assert log.flush(timeout=5)
        assert len(log._spool) <= 16
        assert len(tuple((tmp_path / 'logs').glob('*.jsonl'))) <= 16
        assert log.snapshot()['file_dropped'] > 0
    finally:
        assert log.close()


def test_manifest_read_is_capped_before_decoding(tmp_path, monkeypatch):
    manifest = tmp_path / 'logs' / 'diagnostic_manifest_v2.json'
    manifest.parent.mkdir()
    manifest.write_bytes(b' ' * (3 * 1024 * 1024))
    original = Path.read_text
    def refuse_unbounded(path, *args, **kwargs):
        if path == manifest:
            raise AssertionError('Manifest must use a bounded read, not read_text')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', refuse_unbounded)
    log = DiagnosticLog(manifest.parent)
    try:
        assert log.snapshot()['health_code'] != 'ok'
        log.emit('bounded recovery')
        assert log.flush(5)
    finally:
        assert log.close()
