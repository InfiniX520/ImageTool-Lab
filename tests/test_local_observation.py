"""Offline synthetic coverage for the scoped local observer."""
import errno
import hashlib
import os
import stat
import subprocess
from dataclasses import FrozenInstanceError, dataclass, replace

import pytest

from core.local_observation import (
    FileIdentity, LocalObserver, ObservationBudget, ObservationRequest, ObservationStatus,
    _DEFAULT_IO, _FileOperations,
)
from core.preservation_policy import (
    ContentVersion, LocalState, PreservationOutcome, assess_preservation,
)
from core.task_contracts import ContractError, PathRef


TIME = '2026-11-11T10:00:00+08:00'
PATH = PathRef(relative_path='item.bin')
USE_PAYLOAD = object()


@pytest.fixture
def root(tmp_path):
    result = tmp_path / 'root'
    result.mkdir()
    return result


def content(data, *, historical=True):
    return ContentVersion('asset-1', 1, len(data), hashlib.sha256(data).hexdigest() if historical else None)


def observer(root, **kwargs):
    values = dict(root_id='project', root_revision=7, _clock=lambda: TIME)
    values.update(kwargs)
    return LocalObserver(root, **values)


def request(content_value, path=PATH, **kwargs):
    values = dict(run_id='run-1', check_id='check-1')
    if 'cancelled' in kwargs:
        cancellation = kwargs.pop('cancelled')
        values['cancellation'] = (
            cancellation if callable(getattr(cancellation, 'is_set', None))
            else Cancellation(cancellation)
        )
    values.update(kwargs)
    return ObservationRequest(content_value, path, **values)


def observe(root, data, **kwargs):
    observer_keys = {key: kwargs.pop(key) for key in tuple(kwargs) if key.startswith('_')}
    return observer(root, **observer_keys).observe(request(content(data), **kwargs))


def observe_with(source, content_value, path=PATH, **kwargs):
    return source.observe(request(content_value, path, **kwargs))


def assert_incomplete(result, status):
    assert result.status is status
    assert not result.digest_complete
    assert result.evidence.observed_sha256 is None
    assert result.evidence.observed_size is None


@dataclass(frozen=True)
class FakeStat:
    st_mode: int = stat.S_IFREG
    st_size: int = 4
    st_mtime_ns: int = 123456
    st_dev: int = 7
    st_ino: int = 9
    st_file_attributes: int = 0


class FakeClock:
    def __init__(self, values=()):
        self.values = list(values)
        self.now = 0.0

    def __call__(self):
        return self.values.pop(0) if self.values else self.now

    def advance(self, seconds):
        self.now += seconds


class Cancellation:
    def __init__(self, callback):
        self.callback = callback

    def is_set(self):
        return self.callback()


class ScriptedReader:
    def __init__(self, payload=b'abcd', *, on_read=None, read_error=None,
                 close_error=False, result_override=USE_PAYLOAD, ignore_size=False):
        self.payload = payload
        self.position = 0
        self.on_read = on_read
        self.read_error = read_error
        self.close_error = close_error
        self.result_override = result_override
        self.ignore_size = ignore_size
        self.read_sizes = []
        self.closed = False

    def read(self, size):
        self.read_sizes.append(size)
        if self.on_read is not None:
            self.on_read(self)
        if self.read_error is not None:
            raise self.read_error
        if self.result_override is not USE_PAYLOAD:
            return self.result_override
        if self.position >= len(self.payload):
            return b''
        end = len(self.payload) if self.ignore_size else min(len(self.payload), self.position + size)
        chunk = self.payload[self.position:end]
        self.position = end
        return chunk

    def close(self):
        self.closed = True
        if self.close_error:
            raise OSError(errno.EIO, 'synthetic close failure')


class TrackingReader:
    def __init__(self, stream):
        self.stream = stream
        self.read_sizes = []

    def read(self, size):
        self.read_sizes.append(size)
        return self.stream.read(size)

    def fileno(self):
        return self.stream.fileno()

    def close(self):
        self.stream.close()


def fake_observer(root, payload=b'abcd', *, metadata=None, reader=None,
                  path_results=None, handle_results=None, clock=None, lstat=None):
    target = root / PATH.relative_path
    target.write_bytes(b'synthetic placeholder')
    initial = FakeStat(st_size=len(payload)) if metadata is None else metadata
    path_results = [initial, initial] if path_results is None else list(path_results)
    handle_results = [initial, initial] if handle_results is None else list(handle_results)
    reader = ScriptedReader(payload) if reader is None else reader

    def fake_lstat(path):
        if path == target:
            result = path_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        if lstat is not None:
            return lstat(path)
        return path.lstat()

    def fake_fstat(_stream):
        result = handle_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    operations = _FileOperations(lambda _path: reader, fake_lstat, fake_fstat)
    return observer(root, _io=operations, _monotonic=clock or FakeClock()), reader


def fake_observe(root, payload=b'abcd', *, data=None, **kwargs):
    fake, reader = fake_observer(root, payload, **kwargs)
    result = observe_with(fake, content(payload if data is None else data),
                          run_id='run-fake', check_id='check-fake')
    return result, reader


def reparse_supported(root, link, target, *, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'real reparse point unavailable in isolated test directory: {type(exc).__name__}')


def test_real_hash_match_is_complete_and_directly_accepted_by_preservation_policy(root):
    payload = b'synthetic image bytes'
    target = root / PATH.relative_path
    target.write_bytes(payload)
    before = target.read_bytes()
    result = observe(root, payload)
    assert result.status is ObservationStatus.HASH_MATCH
    assert result.reason == 'full_sha256_match'
    assert result.digest_complete and result.bytes_read == len(payload)
    assert result.evidence.state is LocalState.HASH_MATCH
    assert result.evidence.observed_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.evidence.observed_size == len(payload)
    assert result.evidence.root_revision == 7
    assert result.evidence.run_id == 'run-1' and result.evidence.check_id == 'check-1'
    assert result.evidence.checked_at == TIME
    assert result.identity is not None and result.identity.file_index > 0
    assert target.read_bytes() == before and list(root.iterdir()) == [target]
    decision = assess_preservation(content(payload), PATH, root_revision=7,
                                   run_id='run-1', local=result.evidence)
    assert decision.outcome is PreservationOutcome.REUSE_LOCAL
    assert decision.local_hash_verified


def test_real_read_is_bounded_and_complete_sha256_is_streamed(root):
    payload = b'abcdefghij'
    (root / PATH.relative_path).write_bytes(payload)
    reader = None

    def open_file(path):
        nonlocal reader
        reader = TrackingReader(_DEFAULT_IO.open_file(path))
        return reader

    operations = replace(_DEFAULT_IO, open_file=open_file)
    result = observe_with(observer(root, _io=operations), 
        content(payload), PATH, run_id='run-1', check_id='check-chunks',
        budget=ObservationBudget(chunk_size=3),
    )
    assert result.status is ObservationStatus.HASH_MATCH
    assert reader.read_sizes == [3, 3, 3, 3, 3]
    assert result.bytes_read == len(payload)


def test_same_size_tampering_is_changed_not_a_hash_match(root):
    (root / PATH.relative_path).write_bytes(b'AAAA')
    result = observe(root, b'BBBB')
    assert result.status is ObservationStatus.CHANGED
    assert result.reason == 'hash_mismatch'
    assert result.digest_complete and result.bytes_read == 4
    assert result.evidence.state is LocalState.CHANGED
    assert result.evidence.observed_sha256 == hashlib.sha256(b'AAAA').hexdigest()
    decision = assess_preservation(content(b'BBBB'), PATH, root_revision=7,
                                   run_id='run-1', local=result.evidence)
    assert decision.outcome is PreservationOutcome.REVIEW_CONFLICT


def test_legacy_file_without_history_gets_complete_current_baseline_only(root):
    payload = b'legacy bytes without historical digest'
    (root / PATH.relative_path).write_bytes(payload)
    legacy = content(payload, historical=False)
    result = observe_with(observer(root), legacy, PATH, run_id='run-legacy', check_id='baseline-1')
    assert result.status is ObservationStatus.BASELINE_OBSERVED
    assert result.digest_complete
    assert result.evidence.state is LocalState.UNKNOWN
    assert result.evidence.content.sha256 is None
    assert result.evidence.observed_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.evidence.observed_size == len(payload)
    decision = assess_preservation(legacy, PATH, root_revision=7,
                                   run_id='run-legacy', local=result.evidence)
    assert decision.outcome is PreservationOutcome.VERIFY_LOCAL
    assert not decision.local_hash_verified


def test_legacy_file_with_wrong_size_is_changed_without_a_digest(root):
    (root / PATH.relative_path).write_bytes(b'actual')
    result = observe_with(observer(root), content(b'old', historical=False), PATH,
                                    run_id='run-1', check_id='check-1')
    assert result.status is ObservationStatus.CHANGED
    assert result.reason == 'size_mismatch'
    assert not result.digest_complete and result.evidence.observed_sha256 is None
    assert result.evidence.observed_size == 6


def test_empty_regular_file_can_be_fully_hashed_with_zero_byte_budget(root):
    (root / PATH.relative_path).write_bytes(b'')
    result = observe(root, b'', budget=ObservationBudget(max_bytes=0))
    assert result.status is ObservationStatus.HASH_MATCH
    assert result.evidence.observed_sha256 == hashlib.sha256(b'').hexdigest()
    assert result.bytes_read == 0 and result.digest_complete


def test_missing_target_is_not_read_or_treated_as_unreadable(root):
    result = observe(root, b'absent')
    assert_incomplete(result, ObservationStatus.MISSING)
    assert result.evidence.state is LocalState.MISSING


def test_missing_root_is_root_unavailable_not_missing_target(tmp_path):
    result = observe(tmp_path / 'offline-root', b'absent')
    assert_incomplete(result, ObservationStatus.ROOT_UNAVAILABLE)
    assert result.evidence.state is LocalState.ROOT_UNAVAILABLE


def test_regular_file_cannot_be_an_observation_root(root):
    root_file = root / 'not-a-directory'
    root_file.write_bytes(b'x')
    result = observe(root_file, b'x')
    assert_incomplete(result, ObservationStatus.ROOT_UNAVAILABLE)
    assert result.reason == 'root_not_directory'


def test_missing_or_non_directory_ancestor_is_a_missing_child(root):
    missing = observe_with(observer(root), content(b'x'), PathRef(relative_path='a/b.bin'),
                                     run_id='run-1', check_id='check-1')
    assert_incomplete(missing, ObservationStatus.MISSING)
    (root / 'a').write_bytes(b'not a directory')
    blocked = observe_with(observer(root), content(b'x'), PathRef(relative_path='a/b.bin'),
                                     run_id='run-1', check_id='check-2')
    assert_incomplete(blocked, ObservationStatus.MISSING)
    assert blocked.reason == 'ancestor_not_directory'


def test_directory_target_is_rejected_not_hashed(root):
    (root / PATH.relative_path).mkdir()
    result = observe(root, b'')
    assert_incomplete(result, ObservationStatus.PATH_REJECTED)
    assert result.reason == 'not_regular_file'


@pytest.mark.parametrize('path', [
    PathRef(kind='external', root_id='other', relative_path='item.bin'),
    PathRef(kind='external', root_id='project', relative_path='item.bin'),
])
def test_observer_never_accepts_another_root_binding(root, path):
    (root / 'item.bin').write_bytes(b'xxxx')
    result = observe_with(observer(root), content(b'xxxx'), path, run_id='run-1', check_id='check-1')
    assert_incomplete(result, ObservationStatus.PATH_REJECTED)
    assert result.reason == 'root_binding_mismatch'


def test_external_root_itself_cannot_be_observed_as_a_file(root):
    result = observe_with(LocalObserver(root, root_id='selected', kind='external', root_revision=1), 
        content(b''), PathRef(kind='external', root_id='selected', relative_path='.'),
        run_id='run-1', check_id='check-1',
    )
    assert_incomplete(result, ObservationStatus.PATH_REJECTED)
    assert result.reason == 'target_must_be_a_child_file'


@pytest.mark.parametrize('raw', [
    '../outside', 'a/../../outside', '/absolute/path', r'C:\outside.jpg',
    r'\\server\share\outside.jpg', r'\\?\C:\outside.jpg', r'\\.\PhysicalDrive0',
    'image.jpg:stream', 'NUL', 'con.jpg', 'trailing.', 'trailing ', 'a//b.jpg',
])
def test_existing_path_contract_rejects_escape_absolute_unc_device_ads_and_special_names(raw):
    with pytest.raises(ContractError):
        PathRef(relative_path=raw)


@pytest.mark.parametrize('root_value', ['', '.', 'relative/root'])
def test_observer_requires_an_explicit_absolute_root(root_value):
    with pytest.raises(ContractError):
        LocalObserver(root_value, root_id='project', root_revision=0)


@pytest.mark.skipif(os.name != 'nt', reason='Windows UNC root behavior')
def test_unc_or_device_root_is_rejected_before_open(root):
    unc = LocalObserver(r'\\server\share\isolated', root_id='external', kind='external',
                        root_revision=0, _clock=lambda: TIME, _monotonic=FakeClock())
    result = observe_with(unc, content(b'x'), PathRef(kind='external', root_id='external', relative_path='x'),
                         run_id='run-1', check_id='check-1')
    assert_incomplete(result, ObservationStatus.PATH_REJECTED)
    assert result.reason == 'unc_or_device_root'


def test_fake_root_reparse_attribute_is_rejected_before_open(root):
    opened = []

    def fake_lstat(path):
        info = path.lstat()
        if path == root:
            return FakeStat(st_mode=stat.S_IFDIR, st_file_attributes=1024)
        return info

    operations = _FileOperations(lambda path: opened.append(path), fake_lstat, _DEFAULT_IO.fstat)
    result = observe_with(observer(root, _io=operations), content(b'x'), PATH,
                                                    run_id='run-1', check_id='check-1')
    assert_incomplete(result, ObservationStatus.PATH_REJECTED)
    assert result.reason == 'root_reparse_point' and not opened


def test_fake_ancestor_reparse_attribute_is_rejected_before_open(root):
    ancestor = root / 'linked'
    opened = []

    def fake_lstat(path):
        if path == ancestor:
            return FakeStat(st_mode=stat.S_IFDIR, st_file_attributes=1024)
        return path.lstat()

    operations = _FileOperations(lambda path: opened.append(path), fake_lstat, _DEFAULT_IO.fstat)
    result = observe_with(observer(root, _io=operations), 
        content(b'x'), PathRef(relative_path='linked/item.bin'), run_id='run-1', check_id='check-1')
    assert_incomplete(result, ObservationStatus.PATH_REJECTED)
    assert result.reason == 'ancestor_reparse_point' and not opened


def test_real_symlink_file_is_rejected_when_platform_supports_it(root):
    payload = b'synthetic target'
    (root / 'real.bin').write_bytes(payload)
    reparse_supported(root, root / PATH.relative_path, root / 'real.bin')
    result = observe(root, payload)
    assert_incomplete(result, ObservationStatus.PATH_REJECTED)
    assert result.reason == 'reparse_point'


def test_real_symlink_ancestor_is_rejected_even_when_target_is_inside_root(root):
    real = root / 'real'
    real.mkdir()
    (real / 'item.bin').write_bytes(b'abcd')
    reparse_supported(root, root / 'linked', real, directory=True)
    result = observe_with(observer(root), content(b'abcd'), PathRef(relative_path='linked/item.bin'),
                                    run_id='run-1', check_id='check-1')
    assert_incomplete(result, ObservationStatus.PATH_REJECTED)
    assert result.reason == 'ancestor_reparse_point'


def test_real_symlink_root_is_rejected(tmp_path, root):
    (root / PATH.relative_path).write_bytes(b'abcd')
    linked_root = tmp_path / 'linked-root'
    reparse_supported(tmp_path, linked_root, root, directory=True)
    result = observe(linked_root, b'abcd')
    assert_incomplete(result, ObservationStatus.PATH_REJECTED)
    assert result.reason == 'root_reparse_point'


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction integration')
def test_real_windows_junction_ancestor_is_rejected_in_isolated_directory(root):
    real = root / 'real'
    real.mkdir()
    (real / 'item.bin').write_bytes(b'abcd')
    linked = root / 'junction'
    completed = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(linked), str(real)],
                               capture_output=True, text=True, timeout=10, check=False)
    if completed.returncode:
        pytest.skip('junction creation unavailable for this isolated test user')
    result = observe_with(observer(root), content(b'abcd'), PathRef(relative_path='junction/item.bin'),
                                    run_id='run-1', check_id='junction-1')
    assert_incomplete(result, ObservationStatus.PATH_REJECTED)
    assert result.reason == 'ancestor_reparse_point'


@pytest.mark.parametrize('changes', [
    {'st_ino': 10}, {'st_size': 5}, {'st_mtime_ns': 123457},
])
def test_open_handle_must_match_path_identity_size_and_mtime(root, changes):
    initial = FakeStat()
    result, _ = fake_observe(root, handle_results=[replace(initial, **changes)])
    assert_incomplete(result, ObservationStatus.CHANGED_DURING_CHECK)
    assert result.reason == 'open_metadata_mismatch'


@pytest.mark.parametrize('changes', [
    {'st_ino': 10}, {'st_size': 5}, {'st_mtime_ns': 123457},
])
def test_handle_identity_size_and_mtime_are_rechecked_after_read(root, changes):
    initial = FakeStat()
    result, _ = fake_observe(root, handle_results=[initial, replace(initial, **changes)])
    assert_incomplete(result, ObservationStatus.CHANGED_DURING_CHECK)
    assert result.bytes_read == 4


@pytest.mark.parametrize('changes', [
    {'st_ino': 10}, {'st_size': 5}, {'st_mtime_ns': 123457},
])
def test_final_path_identity_size_and_mtime_must_match_the_read_handle(root, changes):
    initial = FakeStat()
    result, _ = fake_observe(root, path_results=[initial, replace(initial, **changes)])
    assert_incomplete(result, ObservationStatus.CHANGED_DURING_CHECK)
    assert result.reason == 'metadata_changed_while_reading'


def test_short_read_cannot_be_promoted_to_complete_digest(root):
    result, _ = fake_observe(root, payload=b'ab', data=b'abcd', metadata=FakeStat())
    assert_incomplete(result, ObservationStatus.CHANGED_DURING_CHECK)
    assert result.reason == 'short_read' and result.bytes_read == 2


def test_growth_during_read_cannot_be_promoted_to_complete_digest(root):
    result, _ = fake_observe(root, payload=b'abcdef', data=b'abcd', metadata=FakeStat())
    assert_incomplete(result, ObservationStatus.CHANGED_DURING_CHECK)
    assert result.reason == 'size_changed_while_reading'


def test_disappearance_before_open_is_a_race_not_a_clean_missing_observation(root):
    def denied(_path):
        raise FileNotFoundError(errno.ENOENT, 'synthetic race')

    target = root / PATH.relative_path
    target.write_bytes(b'abcd')
    operations = _FileOperations(denied, _DEFAULT_IO.lstat, _DEFAULT_IO.fstat)
    result = observe_with(observer(root, _io=operations), content(b'abcd'), PATH,
                                                    run_id='run-1', check_id='check-1')
    assert_incomplete(result, ObservationStatus.CHANGED_DURING_CHECK)
    assert result.reason == 'open_target_disappeared'


def test_disappearance_after_read_is_a_race_not_a_clean_missing_observation(root):
    initial = FakeStat()
    result, _ = fake_observe(root, path_results=[initial, FileNotFoundError(errno.ENOENT, 'gone')])
    assert_incomplete(result, ObservationStatus.CHANGED_DURING_CHECK)
    assert result.reason == 'path_disappeared'


def test_ancestor_disappearance_after_read_is_a_race(root):
    ancestor = root / 'nested'
    ancestor.mkdir()
    target = ancestor / 'item.bin'
    target.write_bytes(b'abcd')
    info = target.lstat()
    calls = 0

    def fake_lstat(path):
        nonlocal calls
        if path == ancestor:
            calls += 1
            if calls == 2:
                raise FileNotFoundError(errno.ENOENT, 'synthetic race')
        return path.lstat()

    operations = replace(_DEFAULT_IO, lstat=fake_lstat)
    result = observe_with(observer(root, _io=operations), 
        content(b'abcd'), PathRef(relative_path='nested/item.bin'), run_id='run-1', check_id='check-1')
    assert_incomplete(result, ObservationStatus.CHANGED_DURING_CHECK)


def test_reparse_introduced_during_read_is_rejected_without_a_digest(root):
    initial = FakeStat()
    reparse = replace(initial, st_file_attributes=1024)
    result, _ = fake_observe(root, path_results=[initial, reparse])
    assert_incomplete(result, ObservationStatus.PATH_REJECTED)
    assert result.bytes_read == 4


def test_unavailable_path_or_handle_identity_fails_closed(root):
    no_identity = FakeStat(st_ino=0)
    first, _ = fake_observe(root, metadata=no_identity)
    assert_incomplete(first, ObservationStatus.UNREADABLE)
    assert first.reason == 'file_identity_unavailable'
    second, _ = fake_observe(root, handle_results=[no_identity])
    assert_incomplete(second, ObservationStatus.UNREADABLE)


def test_read_permission_failure_is_unreadable(root):
    reader = ScriptedReader(read_error=PermissionError(errno.EACCES, 'synthetic denied'))
    result, _ = fake_observe(root, reader=reader)
    assert_incomplete(result, ObservationStatus.UNREADABLE)
    assert result.reason == 'read_failed' and reader.closed


def test_root_io_failure_is_not_misreported_as_missing_or_changed(root):
    reader = ScriptedReader(read_error=OSError(errno.EIO, 'synthetic volume failure'))
    result, _ = fake_observe(root, reader=reader)
    assert_incomplete(result, ObservationStatus.ROOT_UNAVAILABLE)
    assert result.reason == 'root_read_failed'


def test_open_permission_failure_is_unreadable(root):
    (root / PATH.relative_path).write_bytes(b'abcd')

    def denied(_path):
        raise PermissionError(errno.EACCES, 'synthetic denied')

    operations = replace(_DEFAULT_IO, open_file=denied)
    result = observe_with(observer(root, _io=operations), content(b'abcd'), PATH,
                                                    run_id='run-1', check_id='check-1')
    assert_incomplete(result, ObservationStatus.UNREADABLE)
    assert result.reason == 'open_failed'


def test_root_and_child_access_failures_remain_distinct(root):
    (root / PATH.relative_path).write_bytes(b'abcd')

    def root_denied(path):
        if path == root:
            raise PermissionError(errno.EACCES, 'synthetic root denial')
        return path.lstat()

    result = observe_with(observer(root, _io=replace(_DEFAULT_IO, lstat=root_denied)), 
        content(b'abcd'), PATH, run_id='run-1', check_id='root-denied')
    assert_incomplete(result, ObservationStatus.ROOT_UNAVAILABLE)

    def child_denied(path):
        if path == root / PATH.relative_path:
            raise PermissionError(errno.EACCES, 'synthetic child denial')
        return path.lstat()

    result = observe_with(observer(root, _io=replace(_DEFAULT_IO, lstat=child_denied)), 
        content(b'abcd'), PATH, run_id='run-1', check_id='child-denied')
    assert_incomplete(result, ObservationStatus.UNREADABLE)


@pytest.mark.parametrize('bad_chunk', ['text', bytearray(b'abcd'), None, memoryview(b'abcd')])
def test_non_bytes_read_result_is_not_silently_accepted(root, bad_chunk):
    result, _ = fake_observe(root, reader=ScriptedReader(result_override=bad_chunk))
    assert_incomplete(result, ObservationStatus.UNREADABLE)
    assert result.reason == 'invalid_read_result'


def test_reader_returning_more_than_requested_is_rejected(root):
    fake, _ = fake_observer(root, reader=ScriptedReader(ignore_size=True))
    result = observe_with(fake, content(b'abcd'), PATH, run_id='run-1', check_id='check-1',
                          budget=ObservationBudget(chunk_size=2))
    assert_incomplete(result, ObservationStatus.UNREADABLE)
    assert result.reason == 'oversized_read_result'


def test_close_failure_invalidates_otherwise_complete_observation(root):
    reader = ScriptedReader(close_error=True)
    result, _ = fake_observe(root, reader=reader)
    assert_incomplete(result, ObservationStatus.UNREADABLE)
    assert result.reason == 'close_failed' and reader.closed


def test_known_size_over_byte_budget_stops_before_open(root):
    (root / PATH.relative_path).write_bytes(b'abcd')
    opened = []
    operations = replace(_DEFAULT_IO, open_file=lambda path: opened.append(path))
    result = observe_with(observer(root, _io=operations), 
        content(b'abcd'), PATH, run_id='run-1', check_id='check-bytes',
        budget=ObservationBudget(max_bytes=3),
    )
    assert_incomplete(result, ObservationStatus.BYTE_BUDGET_EXHAUSTED)
    assert result.bytes_read == 0 and not opened


def test_actual_streamed_bytes_enforce_budget_even_if_metadata_was_smaller(root):
    clock = FakeClock()
    fake, _ = fake_observer(root, payload=b'abcd', metadata=FakeStat(st_size=3), clock=clock)
    result = observe_with(fake, content(b'abc'), PATH, run_id='run-1', check_id='check-bytes',
                          budget=ObservationBudget(max_bytes=3, chunk_size=2))
    assert_incomplete(result, ObservationStatus.BYTE_BUDGET_EXHAUSTED)
    assert result.bytes_read == 4


def test_exact_byte_budget_can_complete(root):
    (root / PATH.relative_path).write_bytes(b'abcd')
    result = observe(root, b'abcd', budget=ObservationBudget(max_bytes=4, chunk_size=2))
    assert result.status is ObservationStatus.HASH_MATCH and result.bytes_read == 4


def test_time_budget_can_expire_before_any_root_io(root):
    opened = []
    operations = replace(_DEFAULT_IO, open_file=lambda path: opened.append(path))
    result = observe_with(observer(root, _io=operations, _monotonic=FakeClock([0, 1])),
        content(b'x'), PATH, run_id='run-1', check_id='check-time',
        budget=ObservationBudget(max_seconds=1),
    )
    assert_incomplete(result, ObservationStatus.TIME_BUDGET_EXHAUSTED)
    assert result.bytes_read == 0 and not opened


def test_time_budget_is_checked_after_every_chunk(root):
    clock = FakeClock()
    reader = ScriptedReader(on_read=lambda _reader: clock.advance(1))
    fake, _ = fake_observer(root, reader=reader, clock=clock)
    result = observe_with(fake, content(b'abcd'), PATH, run_id='run-1', check_id='check-time',
                          budget=ObservationBudget(max_seconds=1, chunk_size=2))
    assert_incomplete(result, ObservationStatus.TIME_BUDGET_EXHAUSTED)
    assert result.bytes_read == 2 and result.evidence.observed_sha256 is None


def test_already_cancelled_check_does_no_target_io(root):
    calls = []
    operations = replace(_DEFAULT_IO, lstat=lambda path: calls.append(path))
    result = observe_with(observer(root, _io=operations), 
        content(b'x'), PATH, run_id='run-1', check_id='check-cancel', cancelled=lambda: True)
    assert_incomplete(result, ObservationStatus.CANCELLED)
    assert result.reason == 'cancel_requested' and not calls


def test_cancellation_after_first_chunk_discards_partial_hash(root):
    reader = ScriptedReader()

    def cancel():
        return len(reader.read_sizes) >= 1


    fake, _ = fake_observer(root, reader=reader)
    result = observe_with(fake, content(b'abcd'), PATH, run_id='run-1', check_id='check-cancel',
                          budget=ObservationBudget(chunk_size=2), cancelled=Cancellation(cancel))
    assert_incomplete(result, ObservationStatus.CANCELLED)
    assert result.bytes_read == 2


def test_invalid_cancellation_or_clock_result_is_not_success(root):
    with pytest.raises(ContractError):
        observe(root, b'x', cancelled=lambda: 'yes')
    with pytest.raises(ContractError):
        observe(root, b'x', _monotonic=lambda: float('nan'))


def test_backwards_monotonic_clock_is_rejected(root):
    with pytest.raises(ContractError):
        observe_with(observer(root, _monotonic=FakeClock([2, 1])), 
            content(b'x'), PATH, run_id='run-1', check_id='check-clock')


@pytest.mark.parametrize('factory', [
    lambda: ObservationBudget(max_bytes=True),
    lambda: ObservationBudget(max_bytes=-1),
    lambda: ObservationBudget(max_seconds=0),
    lambda: ObservationBudget(max_seconds=True),
    lambda: ObservationBudget(max_seconds=float('inf')),
    lambda: ObservationBudget(chunk_size=0),
    lambda: ObservationBudget(chunk_size=True),
    lambda: ObservationBudget(chunk_size=8 * 1024 * 1024 + 1),
])
def test_invalid_budgets_fail_closed(factory):
    with pytest.raises(ContractError):
        factory()


@pytest.mark.parametrize('values', [(-1, 1), (0, 0), (True, 1), (1, False)])
def test_invalid_file_identities_fail_closed(values):
    with pytest.raises(ContractError):
        FileIdentity(*values)


@pytest.mark.parametrize('kwargs', [
    {'root_id': 'other'},
    {'root_id': 'project', 'kind': 'invalid'},
    {'root_id': 'project', 'root_revision': True},
    {'root_id': 'project', 'root_revision': -1},
    {'root_id': 'project', '_io': object()},
])
def test_invalid_observer_binding_or_adapter_fails_closed(root, kwargs):
    values = {'root_revision': 1}
    values.update(kwargs)
    with pytest.raises(ContractError):
        LocalObserver(root, **values)


def test_result_and_nested_data_contracts_are_immutable(root):
    (root / PATH.relative_path).write_bytes(b'abcd')
    result = observe(root, b'abcd')
    with pytest.raises(FrozenInstanceError):
        result.status = ObservationStatus.MISSING
    with pytest.raises(FrozenInstanceError):
        result.evidence.state = LocalState.HASH_MATCH
    with pytest.raises(FrozenInstanceError):
        result.identity.file_index = 99


def test_cannot_manually_construct_incomplete_result_with_digest(root):
    (root / PATH.relative_path).write_bytes(b'abcd')
    result = observe(root, b'abcd')
    with pytest.raises(ContractError):
        replace(result, digest_complete=False)
    with pytest.raises(ContractError):
        replace(result, status=ObservationStatus.BASELINE_OBSERVED)
    with pytest.raises(ContractError):
        replace(result, evidence=replace(result.evidence, observed_sha256='b' * 64))


def test_unreadable_and_unstable_results_map_to_non_reuse_policy_outcomes(root):
    (root / PATH.relative_path).write_bytes(b'abcd')

    def denied(_path):
        raise PermissionError(errno.EACCES, 'synthetic denied')

    unreadable = observe_with(observer(root, _io=replace(_DEFAULT_IO, open_file=denied)), 
        content(b'abcd'), PATH, run_id='run-1', check_id='unreadable')
    decision = assess_preservation(content(b'abcd'), PATH, root_revision=7,
                                   run_id='run-1', local=unreadable.evidence)
    assert decision.outcome is PreservationOutcome.CHECK_LOCAL_ACCESS

    unstable, _ = fake_observe(root, handle_results=[FakeStat(st_ino=10)])
    decision = assess_preservation(content(b'abcd'), PATH, root_revision=7,
                                   run_id='run-fake', local=unstable.evidence)
    assert decision.outcome is PreservationOutcome.WAIT_STABLE
    assert not decision.local_hash_verified
