"""Bounded, read-only observation of regular files below one explicit local root.

Only PathRef children of the configured root can be observed. Reparse points,
including those in the root's ancestors, are never followed. A complete digest
requires stable identity/size/mtime across an opened handle and the path; it is
not a claim that mtime or size alone proves content. Legacy files without an
expected digest produce a current baseline, never HASH_MATCH.
"""
from __future__ import annotations

import errno
import hashlib
import math
import os
import re
import stat
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Protocol

from core.preservation_policy import ContentVersion, LocalEvidence, LocalState
from core.task_contracts import ContractError, PathRef


class _Cancellation(Protocol):
    def is_set(self) -> bool: ...


class ObservationStatus(StrEnum):
    HASH_MATCH = 'hash_match'
    BASELINE_OBSERVED = 'baseline_observed'
    CHANGED = 'changed'
    MISSING = 'missing'
    ROOT_UNAVAILABLE = 'root_unavailable'
    UNREADABLE = 'unreadable'
    PATH_REJECTED = 'path_rejected'
    CHANGED_DURING_CHECK = 'changed_during_check'
    CANCELLED = 'cancelled'
    TIME_BUDGET_EXHAUSTED = 'time_budget_exhausted'
    BYTE_BUDGET_EXHAUSTED = 'byte_budget_exhausted'


@dataclass(frozen=True, slots=True)
class ObservationBudget:
    """Per-file limits; max_seconds is measured by a monotonic clock."""

    max_bytes: int = 1024 * 1024 * 1024
    max_seconds: float = 60.0
    chunk_size: int = 1024 * 1024

    def __post_init__(self):
        if type(self.max_bytes) is not int or not 0 <= self.max_bytes <= 2**63 - 1:
            raise ContractError('Invalid observation byte budget')
        if type(self.max_seconds) not in (int, float) or not math.isfinite(self.max_seconds):
            raise ContractError('Invalid observation time budget')
        if not 0 < self.max_seconds <= 86400:
            raise ContractError('Invalid observation time budget')
        if type(self.chunk_size) is not int or not 1 <= self.chunk_size <= 8 * 1024 * 1024:
            raise ContractError('Invalid observation chunk size')
        object.__setattr__(self, 'max_seconds', float(self.max_seconds))


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """The OS file identity when available; absence is not an identity match."""

    device: int
    file_index: int

    def __post_init__(self):
        if type(self.device) is not int or self.device < 0:
            raise ContractError('Invalid file device identity')
        if type(self.file_index) is not int or self.file_index <= 0:
            raise ContractError('Invalid file index identity')


@dataclass(frozen=True, slots=True)
class LocalObservation:
    """One scoped result; digest_complete never means historical HASH_MATCH."""

    status: ObservationStatus
    reason: str
    evidence: LocalEvidence
    digest_complete: bool = False
    bytes_read: int = 0
    size_bytes: int | None = None
    mtime_ns: int | None = None
    identity: FileIdentity | None = None

    def __post_init__(self):
        if not isinstance(self.status, ObservationStatus) or not isinstance(self.evidence, LocalEvidence):
            raise ContractError('Invalid local observation')
        if not isinstance(self.reason, str) or not re.fullmatch(r'[a-z0-9_.:-]{1,128}', self.reason):
            raise ContractError('Invalid observation reason')
        if type(self.digest_complete) is not bool:
            raise ContractError('Invalid digest completion flag')
        if type(self.bytes_read) is not int or self.bytes_read < 0:
            raise ContractError('Invalid observed byte count')
        if self.size_bytes is not None and (type(self.size_bytes) is not int or self.size_bytes < 0):
            raise ContractError('Invalid observed size')
        if self.mtime_ns is not None and type(self.mtime_ns) is not int:
            raise ContractError('Invalid observed mtime')
        if self.identity is not None and not isinstance(self.identity, FileIdentity):
            raise ContractError('Invalid observed identity')
        if self.digest_complete:
            if self.evidence.observed_sha256 is None or self.identity is None:
                raise ContractError('Complete digest requires bytes and stable identity')
        elif self.evidence.observed_sha256 is not None:
            raise ContractError('Incomplete observation cannot contain a digest')
        if self.status is ObservationStatus.HASH_MATCH:
            if not self.digest_complete or self.evidence.state is not LocalState.HASH_MATCH:
                raise ContractError('Invalid hash-match observation')
        elif self.status is ObservationStatus.BASELINE_OBSERVED:
            if (not self.digest_complete or self.evidence.content.sha256 is not None
                    or self.evidence.state is not LocalState.UNKNOWN):
                raise ContractError('A baseline is not a historical hash match')
        elif self.status is ObservationStatus.CHANGED:
            if self.evidence.state is not LocalState.CHANGED:
                raise ContractError('Invalid changed observation')
            if (self.reason == 'hash_mismatch') != self.digest_complete:
                raise ContractError('Invalid change evidence')
        else:
            expected = _EVIDENCE_STATES[self.status]
            if self.digest_complete or self.evidence.state is not expected:
                raise ContractError('Incomplete observation has invalid evidence')
            if self.evidence.observed_size is not None:
                raise ContractError('Incomplete observation cannot assert observed content size')


_EVIDENCE_STATES = {
    ObservationStatus.MISSING: LocalState.MISSING,
    ObservationStatus.ROOT_UNAVAILABLE: LocalState.ROOT_UNAVAILABLE,
    ObservationStatus.UNREADABLE: LocalState.UNREADABLE,
    ObservationStatus.PATH_REJECTED: LocalState.UNKNOWN,
    ObservationStatus.CHANGED_DURING_CHECK: LocalState.CHANGED_DURING_CHECK,
    ObservationStatus.CANCELLED: LocalState.UNKNOWN,
    ObservationStatus.TIME_BUDGET_EXHAUSTED: LocalState.UNKNOWN,
    ObservationStatus.BYTE_BUDGET_EXHAUSTED: LocalState.UNKNOWN,
}


@dataclass(frozen=True, slots=True)
class _FileOperations:
    open_file: Callable[[Path], BinaryIO]
    lstat: Callable[[Path], os.stat_result]
    fstat: Callable[[BinaryIO], os.stat_result]


def _open_readonly(path: Path) -> BinaryIO:
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        return os.fdopen(descriptor, 'rb', buffering=0, closefd=True)
    except BaseException:
        os.close(descriptor)
        raise


def _fstat(stream: BinaryIO) -> os.stat_result:
    return os.fstat(stream.fileno())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


_DEFAULT_IO = _FileOperations(_open_readonly, lambda path: path.lstat(), _fstat)
_ROOT_ERRNOS = {
    value for name in (
        'EIO', 'ENXIO', 'ENODEV', 'ENETDOWN', 'ENETUNREACH', 'EHOSTDOWN',
        'EHOSTUNREACH', 'ESTALE', 'EREMOTEIO',
    ) if (value := getattr(errno, name, None)) is not None
}


@dataclass(frozen=True, slots=True)
class _Metadata:
    size_bytes: int
    mtime_ns: int
    identity: FileIdentity | None


class _Halt(Exception):
    def __init__(self, status: ObservationStatus, reason: str):
        self.status = status
        self.reason = reason


class _CheckControl:
    def __init__(self, budget: ObservationBudget, cancelled, monotonic):
        if cancelled is not None and not callable(cancelled):
            raise ContractError('Cancellation must be callable')
        if not callable(monotonic):
            raise ContractError('Monotonic clock must be callable')
        self.budget = budget
        self.cancelled = cancelled
        self.monotonic = monotonic
        self.bytes_read = 0
        self.started = self._now()
        self.last = self.started

    def _now(self) -> float:
        value = self.monotonic()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ContractError('Invalid monotonic clock reading')
        return float(value)

    def checkpoint(self) -> None:
        if self.cancelled is not None:
            cancelled = self.cancelled()
            if type(cancelled) is not bool:
                raise ContractError('Cancellation must return boolean')
            if cancelled:
                raise _Halt(ObservationStatus.CANCELLED, 'cancel_requested')
        now = self._now()
        if now < self.last:
            raise ContractError('Monotonic clock moved backwards')
        self.last = now
        if now - self.started >= self.budget.max_seconds:
            raise _Halt(ObservationStatus.TIME_BUDGET_EXHAUSTED, 'time_budget_exhausted')

    def consume(self, count: int) -> None:
        self.bytes_read += count
        if self.bytes_read > self.budget.max_bytes:
            raise _Halt(ObservationStatus.BYTE_BUDGET_EXHAUSTED, 'byte_budget_exhausted')


@dataclass(frozen=True, slots=True)
class ObservationRequest:
    """Immutable per-call identity and controls for one local observation."""

    content: ContentVersion
    path: PathRef
    run_id: str
    check_id: str
    budget: ObservationBudget = ObservationBudget()
    cancellation: _Cancellation | None = None

    def __post_init__(self):
        if not isinstance(self.content, ContentVersion) or not isinstance(self.path, PathRef):
            raise ContractError('Invalid observation target')
        for value, name in ((self.run_id, 'verification run'), (self.check_id, 'local check ID')):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
                raise ContractError(f'Invalid {name}')
        if not isinstance(self.budget, ObservationBudget):
            raise ContractError('Invalid observation budget')
        if self.cancellation is not None and not callable(getattr(self.cancellation, 'is_set', None)):
            raise ContractError('Cancellation must provide is_set()')


class LocalObserver:
    """Observe only PathRef targets under an immutable root/kind/root_id binding."""

    def __init__(self, root, *, root_id: str, kind: str = 'project', root_revision: int,
                 _io: _FileOperations | None = None, _clock=None, _monotonic=None):
        if not isinstance(root, (str, os.PathLike)) or isinstance(root, bytes):
            raise ContractError('Observation root must be a local absolute path')
        try:
            root_text = os.fspath(root)
            if not root_text or any(ord(char) < 32 for char in root_text) or len(root_text) > 4096:
                raise ContractError('Invalid observation root')
            candidate = Path(root_text)
            if not candidate.is_absolute():
                raise ContractError('Observation root must be absolute')
            self._root = Path(os.path.abspath(candidate))
        except (TypeError, ValueError, OSError) as exc:
            raise ContractError('Invalid observation root') from exc
        if kind not in {'project', 'external'}:
            raise ContractError('Invalid observation root kind')
        if not isinstance(root_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', root_id):
            raise ContractError('Invalid observation root identity')
        if kind == 'project' and root_id != 'project':
            raise ContractError('Project paths use the project root identity')
        if type(root_revision) is not int or not 0 <= root_revision <= 2**63 - 1:
            raise ContractError('Invalid root mapping revision')
        self._root_id = root_id
        self._kind = kind
        self._root_revision = root_revision
        self._io = _DEFAULT_IO if _io is None else _io
        self._clock = _utc_now if _clock is None else _clock
        self._monotonic = time.monotonic if _monotonic is None else _monotonic
        if not isinstance(self._io, _FileOperations) or not callable(self._clock):
            raise ContractError('Invalid observation test adapter')
        if not callable(self._monotonic):
            raise ContractError('Invalid observation test adapter')

    def observe(self, request: ObservationRequest) -> LocalObservation:
        if not isinstance(request, ObservationRequest):
            raise ContractError('Expected an immutable observation request')
        content = request.content
        path = request.path
        run_id = request.run_id
        check_id = request.check_id
        checked_at = self._clock()
        control = _CheckControl(request.budget, None if request.cancellation is None
                                else request.cancellation.is_set, self._monotonic)
        snapshot = None
        try:
            control.checkpoint()
            parts = self._scoped_parts(path)
            target = self._root.joinpath(*parts)
            self._check_ancestors(parts, during=False)
            snapshot = self._target_metadata(target, during=False)
            self._require_identity(snapshot)
            if snapshot.size_bytes != content.size_bytes:
                raise _Halt(ObservationStatus.CHANGED, 'size_mismatch')
            if snapshot.size_bytes > request.budget.max_bytes:
                raise _Halt(ObservationStatus.BYTE_BUDGET_EXHAUSTED, 'byte_budget_exhausted')
            with closing(self._open(target)) as stream:
                opened = self._stream_metadata(stream)
                self._require_identity(opened)
                if not _same_metadata(snapshot, opened):
                    raise _Halt(ObservationStatus.CHANGED_DURING_CHECK, 'open_metadata_mismatch')
                digest = hashlib.sha256()
                while True:
                    control.checkpoint()
                    chunk = self._read(stream, request.budget.chunk_size)
                    control.consume(len(chunk))
                    control.checkpoint()
                    if control.bytes_read > snapshot.size_bytes:
                        raise _Halt(ObservationStatus.CHANGED_DURING_CHECK, 'size_changed_while_reading')
                    if not chunk:
                        break
                    digest.update(chunk)
                if control.bytes_read != snapshot.size_bytes:
                    raise _Halt(ObservationStatus.CHANGED_DURING_CHECK, 'short_read')
                closed_metadata = self._stream_metadata(stream)
                self._check_ancestors(parts, during=True)
                current = self._target_metadata(target, during=True)
                control.checkpoint()
                if not (_same_metadata(snapshot, closed_metadata)
                        and _same_metadata(snapshot, current)
                        and _same_metadata(closed_metadata, current)):
                    raise _Halt(ObservationStatus.CHANGED_DURING_CHECK, 'metadata_changed_while_reading')
                snapshot = current
                observed_sha256 = digest.hexdigest()
                if content.sha256 is None:
                    return self._result(ObservationStatus.BASELINE_OBSERVED, 'current_baseline',
                                        content, path, run_id, check_id, checked_at, control,
                                        snapshot, observed_sha256, complete=True)
                if observed_sha256 == content.sha256:
                    return self._result(ObservationStatus.HASH_MATCH, 'full_sha256_match',
                                        content, path, run_id, check_id, checked_at, control,
                                        snapshot, observed_sha256, complete=True)
                return self._result(ObservationStatus.CHANGED, 'hash_mismatch',
                                    content, path, run_id, check_id, checked_at, control,
                                    snapshot, observed_sha256, complete=True)
        except _Halt as halt:
            return self._result(halt.status, halt.reason, content, path, run_id, check_id,
                                checked_at, control, snapshot)
        except ContractError:
            raise
        except (OSError, ValueError):
            return self._result(ObservationStatus.UNREADABLE, 'close_failed', content, path,
                                run_id, check_id, checked_at, control, snapshot)

    def _scoped_parts(self, path: PathRef) -> tuple[str, ...]:
        if path.kind != self._kind or path.root_id.casefold() != self._root_id.casefold():
            raise _Halt(ObservationStatus.PATH_REJECTED, 'root_binding_mismatch')
        if path.relative_path == '.':
            raise _Halt(ObservationStatus.PATH_REJECTED, 'target_must_be_a_child_file')
        return tuple(path.relative_path.split('/'))

    def _check_ancestors(self, parts: tuple[str, ...], *, during: bool) -> None:
        if os.name == 'nt' and str(self._root).startswith('\\\\'):
            raise _Halt(ObservationStatus.PATH_REJECTED, 'unc_or_device_root')
        chain = reversed((self._root, *self._root.parents))
        for directory in chain:
            info = self._lstat(directory, root=True, during=during)
            if _is_reparse(info):
                raise _Halt(ObservationStatus.PATH_REJECTED, 'root_reparse_point')
            if not stat.S_ISDIR(info.st_mode):
                raise _Halt(ObservationStatus.ROOT_UNAVAILABLE, 'root_not_directory')
        current = self._root
        for part in parts[:-1]:
            current = current / part
            info = self._lstat(current, root=False, during=during)
            if _is_reparse(info):
                raise _Halt(ObservationStatus.PATH_REJECTED, 'ancestor_reparse_point')
            if not stat.S_ISDIR(info.st_mode):
                status = ObservationStatus.CHANGED_DURING_CHECK if during else ObservationStatus.MISSING
                raise _Halt(status, 'ancestor_not_directory')

    def _lstat(self, path: Path, *, root: bool, during: bool):
        try:
            return self._io.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            if root:
                raise _Halt(ObservationStatus.ROOT_UNAVAILABLE, 'root_missing_or_offline') from None
            if during:
                raise _Halt(ObservationStatus.CHANGED_DURING_CHECK, 'path_disappeared') from None
            raise _Halt(ObservationStatus.MISSING, 'path_not_found') from None
        except OSError as exc:
            if root or exc.errno in _ROOT_ERRNOS:
                raise _Halt(ObservationStatus.ROOT_UNAVAILABLE, 'root_stat_failed') from None
            raise _Halt(ObservationStatus.UNREADABLE, 'path_stat_failed') from None

    def _target_metadata(self, target: Path, *, during: bool) -> _Metadata:
        info = self._lstat(target, root=False, during=during)
        self._require_regular(info)
        return _metadata(info)

    def _require_regular(self, info) -> None:
        if _is_reparse(info):
            raise _Halt(ObservationStatus.PATH_REJECTED, 'reparse_point')
        if not stat.S_ISREG(info.st_mode):
            raise _Halt(ObservationStatus.PATH_REJECTED, 'not_regular_file')

    def _require_identity(self, metadata: _Metadata) -> None:
        if metadata.identity is None:
            raise _Halt(ObservationStatus.UNREADABLE, 'file_identity_unavailable')

    def _open(self, target: Path):
        try:
            return self._io.open_file(target)
        except (FileNotFoundError, NotADirectoryError):
            raise _Halt(ObservationStatus.CHANGED_DURING_CHECK, 'open_target_disappeared') from None
        except OSError as exc:
            if exc.errno in _ROOT_ERRNOS:
                raise _Halt(ObservationStatus.ROOT_UNAVAILABLE, 'root_open_failed') from None
            raise _Halt(ObservationStatus.UNREADABLE, 'open_failed') from None

    def _stream_metadata(self, stream) -> _Metadata:
        try:
            info = self._io.fstat(stream)
        except (OSError, ValueError):
            raise _Halt(ObservationStatus.UNREADABLE, 'handle_stat_failed') from None
        self._require_regular(info)
        return _metadata(info)

    def _read(self, stream, size: int) -> bytes:
        try:
            chunk = stream.read(size)
        except OSError as exc:
            if exc.errno in _ROOT_ERRNOS:
                raise _Halt(ObservationStatus.ROOT_UNAVAILABLE, 'root_read_failed') from None
            raise _Halt(ObservationStatus.UNREADABLE, 'read_failed') from None
        except ValueError:
            raise _Halt(ObservationStatus.UNREADABLE, 'read_failed') from None
        if not isinstance(chunk, bytes):
            raise _Halt(ObservationStatus.UNREADABLE, 'invalid_read_result')
        if len(chunk) > size:
            raise _Halt(ObservationStatus.UNREADABLE, 'oversized_read_result')
        return chunk

    def _result(self, status: ObservationStatus, reason: str, content: ContentVersion,
                path: PathRef, run_id: str, check_id: str, checked_at: str,
                control: _CheckControl, snapshot: _Metadata | None,
                observed_sha256: str | None = None, complete: bool = False) -> LocalObservation:
        observed_size = snapshot.size_bytes if snapshot is not None and status in {
            ObservationStatus.HASH_MATCH, ObservationStatus.BASELINE_OBSERVED, ObservationStatus.CHANGED,
        } else None
        evidence = LocalEvidence(
            content=content, path=path, root_revision=self._root_revision, run_id=run_id,
            check_id=check_id, checked_at=checked_at, state=_state_for(status),
            observed_size=observed_size, observed_sha256=observed_sha256,
        )
        return LocalObservation(
            status=status, reason=reason, evidence=evidence, digest_complete=complete,
            bytes_read=control.bytes_read,
            size_bytes=snapshot.size_bytes if snapshot is not None else None,
            mtime_ns=snapshot.mtime_ns if snapshot is not None else None,
            identity=snapshot.identity if snapshot is not None else None,
        )


def _state_for(status: ObservationStatus) -> LocalState:
    if status is ObservationStatus.HASH_MATCH:
        return LocalState.HASH_MATCH
    if status is ObservationStatus.CHANGED:
        return LocalState.CHANGED
    return _EVIDENCE_STATES.get(status, LocalState.UNKNOWN)


def _metadata(info) -> _Metadata:
    size = getattr(info, 'st_size', None)
    mtime_ns = getattr(info, 'st_mtime_ns', None)
    if type(size) is not int or size < 0 or type(mtime_ns) is not int:
        raise _Halt(ObservationStatus.UNREADABLE, 'metadata_unavailable')
    device = getattr(info, 'st_dev', None)
    index = getattr(info, 'st_ino', None)
    identity = None
    if type(device) is type(index) is int and device >= 0 and index > 0:
        identity = FileIdentity(device, index)
    return _Metadata(size, mtime_ns, identity)


def _same_metadata(left: _Metadata, right: _Metadata) -> bool:
    return (left.size_bytes == right.size_bytes and left.mtime_ns == right.mtime_ns
            and left.identity is not None and left.identity == right.identity)


def _is_reparse(info) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, 'st_file_attributes', 0)
    return bool(attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024))
