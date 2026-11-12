"""Read-only, bounded enumeration of an explicit local archive scope.

The first page request builds one bounded metadata snapshot; later pages read
that immutable snapshot.  A cursor is not a resumable filesystem scan: partial
snapshots stay partial.  Callers explicitly pass returned ArchiveEntry objects
to LocalObserver/ObservationRequest.  This module never invokes an observer,
opens file contents, calculates a content hash, or writes business data.

Directory identity/metadata checks detect ordinary enumeration races; they are
not an atomic defence against an adversarial filesystem writer.  Final content
identity and stability remain LocalObserver's responsibility.
"""
from __future__ import annotations

import base64
import binascii
import errno
import fnmatch
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from enum import Enum, StrEnum
from pathlib import Path
from typing import ContextManager

from core.archive_inventory import ArchiveEntry
from core.local_observation import FileIdentity
from core.preservation_policy import ContentVersion
from core.task_contracts import ContractError, PathRef, canonical_json, normalize_json

SCOPE_SCHEMA_VERSION = 1
_CURSOR_SCHEMA = "jpgpng.archive-scope.cursor.v1"
_MAX_CURSOR_BYTES = 16384
_ROOT_ERRNOS = {
    value for name in (
        "EIO", "ENXIO", "ENODEV", "ENETDOWN", "ENETUNREACH", "EHOSTDOWN",
        "EHOSTUNREACH", "ESTALE", "EREMOTEIO",
    ) if (value := getattr(errno, name, None)) is not None
}
_OUTPUT_DIRECTORY_RULES = (
    "output", "outputs", "archive_output", "archive_outputs", "archive_store",
    ".runtime",
)
_SENSITIVE_FILE_RULES = (
    "*.db", "*.db-journal", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3",
    "*.sqlite3-journal", "*.sqlite3-wal", "*.sqlite3-shm", "*.log", "*.log.*",
    "*.tmp", "*.part", "*.partial", "*.crdownload", ".env", ".env.*",
    "*.pem", "*.key", "*.pfx", "*.p12", "*.kdbx", "*cookie*", "*credential*",
    "*secret*", "pixiv_config.json",
)


def _plain_json(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _plain_json(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(child) for child in value]
    return value


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError("Invalid " + name)
    return value


def _path_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _root_text(root: object) -> Path:
    if not isinstance(root, (str, os.PathLike)) or isinstance(root, bytes):
        raise ContractError("Scope root must be a local absolute path")
    try:
        value = os.fspath(root)
        if (not value or len(value) > 4096
                or any(ord(char) < 32 for char in value)):
            raise ContractError("Invalid scope root")
        candidate = Path(value)
        if not candidate.is_absolute():
            raise ContractError("Scope root must be absolute")
        if os.name == "nt" and str(candidate).startswith("\\\\"):
            raise ContractError("UNC and device roots are not supported")
        return Path(os.path.abspath(candidate))
    except (TypeError, ValueError, OSError) as exc:
        raise ContractError("Invalid scope root") from exc


def _rules(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        raise ContractError("Invalid " + name)
    if len(value) > 128:
        raise ContractError("Too many " + name)
    result = []
    for rule in value:
        if (not isinstance(rule, str) or not rule or len(rule) > 128
                or any(ord(char) < 32 for char in rule)
                or any(char in rule for char in "/\\")):
            raise ContractError("Invalid " + name)
        result.append(unicodedata.normalize("NFC", rule).casefold())
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ScopePolicy:
    """Deny rules are matched case-insensitively against individual names.

    Additional rules extend, rather than disable, output/sensitive defaults.
    No extension or image-format whitelist silently narrows a selected scope.
    Budgets cover the entire snapshot, not each page.  At most one terminal
    issue beyond max_issues is retained to explain an issue-budget stop.
    """

    additional_directory_excludes: tuple[str, ...] = ()
    additional_file_excludes: tuple[str, ...] = ()
    max_entries: int = 10000
    max_directories: int = 2000
    max_seconds: float = 30.0
    max_issues: int = 512
    max_exclusions: int = 10000

    def __post_init__(self):
        object.__setattr__(self, "additional_directory_excludes", _rules(
            self.additional_directory_excludes, "directory exclusion rules"))
        object.__setattr__(self, "additional_file_excludes", _rules(
            self.additional_file_excludes, "file exclusion rules"))
        _integer(self.max_entries, "scope entry budget", 1, 1000000)
        _integer(self.max_directories, "scope directory budget", 1, 1000000)
        _integer(self.max_issues, "scope issue budget", 1, 100000)
        _integer(self.max_exclusions, "scope exclusion budget", 1, 1000000)
        if (type(self.max_seconds) not in (int, float)
                or not math.isfinite(self.max_seconds)
                or not 0 < self.max_seconds <= 86400):
            raise ContractError("Invalid scope time budget")
        object.__setattr__(self, "max_seconds", float(self.max_seconds))

    @property
    def directory_rules(self) -> tuple[str, ...]:
        # File-secret globs never exclude a merely similar ordinary directory.
        # Only managed outputs and explicit directory names prune traversal.
        return self.additional_directory_excludes

    @property
    def file_rules(self) -> tuple[str, ...]:
        return _SENSITIVE_FILE_RULES + self.additional_file_excludes

    def _digest_data(self) -> dict[str, object]:
        return {
            "schema_version": SCOPE_SCHEMA_VERSION,
            "directory_rules": self.directory_rules,
            "file_rules": self.file_rules,
            "max_entries": self.max_entries,
            "max_directories": self.max_directories,
            "max_milliseconds": round(self.max_seconds * 1000),
            "max_issues": self.max_issues,
            "max_exclusions": self.max_exclusions,
        }


@dataclass(frozen=True, slots=True)
class ScopeRequest:
    """Explicit recursive directory prefixes and individual file selections.

    Strings are validated as PathRef values during enumeration so malformed
    selections become page issues.  A supplied PathRef must match this root.
    No selection is inferred from the workspace, current directory, or database.
    """

    prefixes: tuple[str | PathRef, ...]
    files: tuple[str | PathRef, ...] = ()
    kind: str = "project"
    root_id: str = "project"
    root_revision: int = 0
    page_size: int = 100

    def __post_init__(self):
        if isinstance(self.prefixes, (str, bytes)) or isinstance(self.files, (str, bytes)):
            raise ContractError("Scope selections must be sequences")
        try:
            prefixes = tuple(self.prefixes)
            files = tuple(self.files)
        except TypeError as exc:
            raise ContractError("Invalid scope selections") from exc
        if not prefixes and not files:
            raise ContractError("An explicit scope selection is required")
        if len(prefixes) + len(files) > 100000:
            raise ContractError("Too many explicit scope selections")
        if any(not isinstance(value, (str, PathRef)) for value in prefixes + files):
            raise ContractError("Invalid scope selection")
        if self.kind not in {"project", "external"}:
            raise ContractError("Invalid scope root kind")
        if not isinstance(self.root_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,64}", self.root_id):
            raise ContractError("Invalid scope root identity")
        if self.kind == "project" and self.root_id != "project":
            raise ContractError("Project paths use the project root identity")
        _integer(self.root_revision, "root mapping revision", 0, 2**63 - 1)
        _integer(self.page_size, "scope page size", 1, 1000)
        object.__setattr__(self, "prefixes", prefixes)
        object.__setattr__(self, "files", files)

    def _selection_data(self) -> list[object]:
        return [
            [kind, value if isinstance(value, str) else {
                "kind": value.kind, "root_id": value.root_id,
                "relative_path": value.relative_path,
            }]
            for kind, values in (("prefix", self.prefixes), ("file", self.files))
            for value in values
        ]


class ScopeIssueCode(StrEnum):
    INVALID_PATH = "invalid_path"
    ADS_PATH = "ads_path"
    ROOT_BINDING_MISMATCH = "root_binding_mismatch"
    ROOT_UNAVAILABLE = "root_unavailable"
    ROOT_NOT_DIRECTORY = "root_not_directory"
    ROOT_REPARSE_POINT = "root_reparse_point"
    ROOT_CHANGED = "root_changed"
    REPARSE_POINT = "reparse_point"
    ANCESTOR_REPARSE_POINT = "ancestor_reparse_point"
    PREFIX_NOT_DIRECTORY = "prefix_not_directory"
    DIRECTORY_AS_FILE = "directory_as_file"
    NOT_REGULAR_FILE = "not_regular_file"
    FILE_UNAVAILABLE = "file_unavailable"
    FILE_IDENTITY_UNAVAILABLE = "file_identity_unavailable"
    UNREADABLE = "unreadable"
    DIRECTORY_UNREADABLE = "directory_unreadable"
    FILE_CHANGED = "file_changed"
    DIRECTORY_CHANGED = "directory_changed"
    ENTRY_BUDGET_EXHAUSTED = "entry_budget_exhausted"
    DIRECTORY_BUDGET_EXHAUSTED = "directory_budget_exhausted"
    TIME_BUDGET_EXHAUSTED = "time_budget_exhausted"
    ISSUE_BUDGET_EXHAUSTED = "issue_budget_exhausted"
    EXCLUSION_BUDGET_EXHAUSTED = "exclusion_budget_exhausted"
    CANCELLED = "cancelled"


class ScopeState(StrEnum):
    COMPLETE = "complete"
    COMPLETE_WITH_ISSUES = "complete_with_issues"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True, slots=True)
class ScopeExclusion:
    relative_path: str
    reason: str
    rule: str | None = None
    related_path: str | None = None
    identity: FileIdentity | None = None


@dataclass(frozen=True, slots=True)
class ScopeIssue:
    code: ScopeIssueCode
    relative_path: str
    message: str


@dataclass(frozen=True, slots=True)
class ScopeCompletion:
    state: ScopeState
    snapshot_id: str | None
    root_identity: FileIdentity | None
    entries_in_snapshot: int
    exclusions_in_snapshot: int
    issues_in_snapshot: int
    snapshot_digest: str | None = None

    @property
    def full(self) -> bool:
        """True only when every in-scope candidate was accepted or excluded."""
        return self.state is ScopeState.COMPLETE


@dataclass(frozen=True, slots=True)
class ScopePage:
    entries: tuple[ArchiveEntry, ...]
    exclusions: tuple[ScopeExclusion, ...]
    issues: tuple[ScopeIssue, ...]
    page_index: int
    cursor: str | None
    completion: ScopeCompletion
    root_id: str
    kind: str
    root_revision: int
    rules_digest: str
    has_next_page: bool = False
    schema_version: int = SCOPE_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "entries", tuple(self.entries))
        object.__setattr__(self, "exclusions", tuple(self.exclusions))
        object.__setattr__(self, "issues", tuple(self.issues))
        if (self.cursor is not None) != self.has_next_page:
            raise ContractError("Cursor and next-page state disagree")
        if self.cursor is not None and not self.completion.snapshot_id:
            raise ContractError("A cursor requires a snapshot")


@dataclass(frozen=True, slots=True)
class _CursorState:
    ordinal: int
    valid: bool


@dataclass(frozen=True, slots=True)
class _NodeState:
    identity: FileIdentity
    size_bytes: int
    mtime_ns: int
    mode: int
    attributes: int


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    entry: ArchiveEntry
    identity: FileIdentity
    mtime_ns: int
    attributes: int


@dataclass(frozen=True, slots=True)
class _Snapshot:
    snapshot_id: str | None
    entries: tuple[_FileSnapshot, ...]
    exclusions: tuple[ScopeExclusion, ...]
    issues: tuple[ScopeIssue, ...]
    state: ScopeState
    root_state: _NodeState | None
    ancestors: tuple[tuple[Path, _NodeState], ...]
    digest: str
    cursor: _CursorState
    cursor_key: bytes


@dataclass(frozen=True, slots=True)
class _FileOperations:
    lstat: Callable[[Path], os.stat_result]
    scandir: Callable[[Path], ContextManager[Iterator[object]]]


_DEFAULT_IO = _FileOperations(lambda path: path.lstat(), os.scandir)


def _is_reparse(info: object) -> bool:
    return bool(stat.S_ISLNK(getattr(info, "st_mode", 0)) or (
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ))


def _identity(info: object) -> FileIdentity | None:
    device = getattr(info, "st_dev", None)
    index = getattr(info, "st_ino", None)
    if (type(device) is not int or device < 0
            or type(index) is not int or index <= 0):
        return None
    return FileIdentity(device, index)


def _node_state(info: object) -> _NodeState | None:
    identity = _identity(info)
    size = getattr(info, "st_size", None)
    modified = getattr(info, "st_mtime_ns", None)
    if identity is None or type(size) is not int or size < 0 or type(modified) is not int:
        return None
    return _NodeState(identity, size, modified, getattr(info, "st_mode", 0),
                      getattr(info, "st_file_attributes", 0))


def _rule_for(name: str, rules: tuple[str, ...]) -> str | None:
    folded = _path_key(name)
    return next((rule for rule in rules if fnmatch.fnmatchcase(folded, rule)), None)


def _issue(code: ScopeIssueCode, path: str) -> ScopeIssue:
    messages = {
        ScopeIssueCode.INVALID_PATH: "The selection is not a safe relative PathRef.",
        ScopeIssueCode.ADS_PATH: "Alternate data stream syntax is forbidden.",
        ScopeIssueCode.ROOT_BINDING_MISMATCH: "The PathRef root binding does not match.",
        ScopeIssueCode.ROOT_UNAVAILABLE: "The explicit root is missing, offline, or inaccessible.",
        ScopeIssueCode.ROOT_NOT_DIRECTORY: "The explicit root is not a directory.",
        ScopeIssueCode.ROOT_REPARSE_POINT: "The root or one of its ancestors is a reparse point.",
        ScopeIssueCode.ROOT_CHANGED: "The root or an ancestor changed from the snapshot.",
        ScopeIssueCode.REPARSE_POINT: "A link, junction, or reparse point was not followed.",
        ScopeIssueCode.ANCESTOR_REPARSE_POINT: "A selected path ancestor is a reparse point.",
        ScopeIssueCode.PREFIX_NOT_DIRECTORY: "A recursive prefix does not identify a directory.",
        ScopeIssueCode.DIRECTORY_AS_FILE: "A directory was supplied as an individual file.",
        ScopeIssueCode.NOT_REGULAR_FILE: "Only ordinary regular files can become entries.",
        ScopeIssueCode.FILE_UNAVAILABLE: "The selected file does not exist or disappeared.",
        ScopeIssueCode.FILE_IDENTITY_UNAVAILABLE: "A stable OS file identity is unavailable.",
        ScopeIssueCode.UNREADABLE: "Path metadata could not be read.",
        ScopeIssueCode.DIRECTORY_UNREADABLE: "A directory could not be completely enumerated.",
        ScopeIssueCode.FILE_CHANGED: "File metadata changed while freezing the snapshot.",
        ScopeIssueCode.DIRECTORY_CHANGED: "A directory changed during enumeration.",
        ScopeIssueCode.ENTRY_BUDGET_EXHAUSTED: "The candidate-entry budget was exhausted.",
        ScopeIssueCode.DIRECTORY_BUDGET_EXHAUSTED: "The directory-visit budget was exhausted.",
        ScopeIssueCode.TIME_BUDGET_EXHAUSTED: "The monotonic time budget was exhausted.",
        ScopeIssueCode.ISSUE_BUDGET_EXHAUSTED: "The retained-issue budget was exhausted.",
        ScopeIssueCode.EXCLUSION_BUDGET_EXHAUSTED: "The retained-exclusion budget was exhausted.",
        ScopeIssueCode.CANCELLED: "Enumeration was cancelled; this is not a complete scope.",
    }
    return ScopeIssue(code, path, messages[code])


class _Halt(Exception):
    def __init__(self, issue: ScopeIssue, state: ScopeState = ScopeState.PARTIAL):
        self.issue = issue
        self.state = state


class ScopeEnumerator:
    """Own a bounded snapshot and authenticated cursors for one explicit root."""

    def __init__(self, root, request: ScopeRequest, policy: ScopePolicy | None = None,
                 *, _io: _FileOperations | None = None, _monotonic=None):
        self._root = _root_text(root)
        if not isinstance(request, ScopeRequest):
            raise ContractError("Expected a ScopeRequest")
        self._request = request
        self._policy = ScopePolicy() if policy is None else policy
        if not isinstance(self._policy, ScopePolicy):
            raise ContractError("Expected a ScopePolicy")
        self._io = _DEFAULT_IO if _io is None else _io
        if not isinstance(self._io, _FileOperations):
            raise ContractError("Invalid scope filesystem adapter")
        self._monotonic = time.monotonic if _monotonic is None else _monotonic
        if not callable(self._monotonic):
            raise ContractError("Invalid scope clock")
        self._key = secrets.token_bytes(32)
        self._snapshot: _Snapshot | None = None
        self._rules_digest = hashlib.sha256(canonical_json({
            "policy": self._policy._digest_data(),
            "selections": request._selection_data(),
        }).encode("utf-8")).hexdigest()

    def page(self, cursor: str | None = None, *, cancellation=None) -> ScopePage:
        if cancellation is not None and not callable(getattr(cancellation, "is_set", None)):
            raise ContractError("Cancellation must provide is_set()")
        cancelled = None if cancellation is None else cancellation.is_set
        if cursor is None:
            if self._snapshot is not None:
                raise ContractError("The first-page cursor cannot restart a live enumerator")
            self._snapshot = self._enumerate(cancelled)
            if self._snapshot.root_state is None:
                return self._empty_result(self._snapshot, cancellation)
        snapshot = self._snapshot
        if snapshot is None:
            raise ContractError("Invalid, foreign, or altered scope cursor")
        start = 0 if cursor is None else self._decode_cursor(cursor, snapshot)
        current_issues = list(snapshot.issues)
        state = snapshot.state
        if cancelled is not None and cancelled():
            current_issues.append(_issue(ScopeIssueCode.CANCELLED, "."))
            state = ScopeState.CANCELLED
        else:
            try:
                self._check_snapshot_root(snapshot)
            except _Halt as halt:
                current_issues.append(halt.issue)
                state = ScopeState.PARTIAL
        if state is not snapshot.state:
            return self._make_page(snapshot, (), None, tuple(current_issues), state, start=0, has_next=False)
        selected = snapshot.entries[start:start + self._request.page_size]
        end = start + len(selected)
        next_cursor = self._encode_cursor(snapshot, end) if end < len(snapshot.entries) else None
        result = self._make_page(snapshot, tuple(item.entry for item in selected),
                                 next_cursor, tuple(current_issues), state, start=start,
                                 has_next=next_cursor is not None)
        self._snapshot = replace(
            snapshot, cursor=_CursorState(end if next_cursor else 0, next_cursor is not None),
            cursor_key=snapshot.cursor_key,
        )
        return result

    def _make_page(self, snapshot: _Snapshot, entries, cursor, issues, state, *, start,
                   has_next):
        completion = ScopeCompletion(
            state, snapshot.snapshot_id,
            None if snapshot.root_state is None else snapshot.root_state.identity,
            len(snapshot.entries), len(snapshot.exclusions), len(issues), snapshot.digest,
        )
        return ScopePage(
            entries=entries, exclusions=snapshot.exclusions, issues=issues,
            page_index=start // self._request.page_size, has_next_page=has_next,
            cursor=cursor, completion=completion,
            root_id=self._request.root_id, kind=self._request.kind,
            root_revision=self._request.root_revision, rules_digest=self._rules_digest,
        )

    def _encode_cursor(self, snapshot: _Snapshot, next_ordinal: int) -> str:
        if not 0 < next_ordinal < len(snapshot.entries):
            raise ContractError("Invalid cursor boundary")
        previous = snapshot.entries[next_ordinal - 1].entry.path
        assert isinstance(previous, PathRef)
        payload = {
            "schema": _CURSOR_SCHEMA,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_digest": snapshot.digest,
            "root_id": self._request.root_id,
            "kind": self._request.kind,
            "root_revision": self._request.root_revision,
            "root_device": snapshot.root_state.identity.device,
            "root_file_index": snapshot.root_state.identity.file_index,
            "rules_digest": self._rules_digest,
            "page_size": self._request.page_size,
            "entry_count": len(snapshot.entries),
            "next_ordinal": next_ordinal,
            "boundary_key": _path_key(previous.relative_path),
        }
        raw = canonical_json(payload).encode("utf-8")
        signature = hmac.digest(self._key, raw, "sha256")
        return base64.urlsafe_b64encode(raw + signature).decode("ascii").rstrip("=")

    def _decode_cursor(self, cursor: str, snapshot: _Snapshot) -> int:
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= _MAX_CURSOR_BYTES:
                raise ValueError("Cursor size")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
                raise ValueError("Cursor alphabet")
            decoded = base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            if len(decoded) <= 32 or len(decoded) > _MAX_CURSOR_BYTES:
                raise ValueError("Cursor payload size")
            raw, signature = decoded[:-32], decoded[-32:]
            if not hmac.compare_digest(signature, hmac.digest(snapshot.cursor_key, raw, "sha256")):
                raise ValueError("Cursor authentication")
            text = normalize_json(raw.decode("utf-8"), object_only=True)
            payload = json.loads(text)
            previous = snapshot.entries[payload["next_ordinal"] - 1].entry.path
            assert isinstance(previous, PathRef)
            expected = {
                "schema": _CURSOR_SCHEMA,
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_digest": snapshot.digest,
                "root_id": self._request.root_id,
                "kind": self._request.kind,
                "root_revision": self._request.root_revision,
                "root_device": snapshot.root_state.identity.device,
                "root_file_index": snapshot.root_state.identity.file_index,
                "rules_digest": self._rules_digest,
                "page_size": self._request.page_size,
                "entry_count": len(snapshot.entries),
                "next_ordinal": payload["next_ordinal"],
                "boundary_key": _path_key(previous.relative_path),
            }
            if (payload != expected or not 0 < payload["next_ordinal"] < len(snapshot.entries)
                    or not snapshot.cursor.valid
                    or payload["next_ordinal"] != snapshot.cursor.ordinal):
                raise ValueError("Cursor binding")
            return payload["next_ordinal"]
        except (ValueError, TypeError, KeyError, IndexError, UnicodeError,
                binascii.Error, ContractError) as exc:
            raise ContractError("Invalid, foreign, or altered scope cursor") from exc

    def _checkpoint(self, started: float, cancelled) -> None:
        if cancelled is not None and cancelled():
            raise _Halt(_issue(ScopeIssueCode.CANCELLED, "."), ScopeState.CANCELLED)
        now = self._monotonic()
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ContractError("Scope clock returned an invalid value")
        if now - started > self._policy.max_seconds:
            raise _Halt(_issue(ScopeIssueCode.TIME_BUDGET_EXHAUSTED, "."),
                        ScopeState.BUDGET_EXHAUSTED)

    def _enumerate(self, cancelled) -> _Snapshot | None:
        entries: list[_FileSnapshot] = []
        exclusions: list[ScopeExclusion] = []
        issues: list[ScopeIssue] = []
        selected_paths: dict[str, str] = {}
        directory_identities: dict[tuple[int, int], str] = {}
        directories: list[tuple[str, _NodeState]] = []
        used_entries = 0
        used_directories = 0
        started = self._monotonic()
        if type(started) not in (int, float) or not math.isfinite(started):
            raise ContractError("Scope clock returned an invalid value")

        def add_issue(code, path, *, terminal=False):
            if len(issues) < self._policy.max_issues:
                issues.append(_issue(code, path))
            elif terminal:
                if issues:
                    issues[-1] = _issue(code, path)
                else:
                    issues.append(_issue(code, path))
            else:
                raise _Halt(_issue(ScopeIssueCode.ISSUE_BUDGET_EXHAUSTED, path),
                            ScopeState.BUDGET_EXHAUSTED)

        def add_exclusion(path, reason, rule=None, related=None, identity=None):
            if len(exclusions) >= self._policy.max_exclusions:
                raise _Halt(_issue(ScopeIssueCode.EXCLUSION_BUDGET_EXHAUSTED, path),
                            ScopeState.BUDGET_EXHAUSTED)
            exclusions.append(ScopeExclusion(path, reason, rule, related, identity))

        def consume_entry(path):
            nonlocal used_entries
            if used_entries >= self._policy.max_entries:
                raise _Halt(_issue(ScopeIssueCode.ENTRY_BUDGET_EXHAUSTED, path),
                            ScopeState.BUDGET_EXHAUSTED)
            used_entries += 1

        def lstat(path, relative, *, root=False):
            try:
                return self._io.lstat(path)
            except (FileNotFoundError, NotADirectoryError):
                if root:
                    raise _Halt(_issue(ScopeIssueCode.ROOT_UNAVAILABLE, ".")) from None
                add_issue(ScopeIssueCode.FILE_UNAVAILABLE, relative)
                return None
            except OSError as exc:
                if root or exc.errno in _ROOT_ERRNOS:
                    raise _Halt(_issue(ScopeIssueCode.ROOT_UNAVAILABLE, ".")) from None
                add_issue(ScopeIssueCode.UNREADABLE, relative)
                return None

        def root_chain():
            chain = []
            for directory in reversed((self._root, *self._root.parents)):
                info = lstat(directory, ".", root=True)
                assert info is not None
                if _is_reparse(info):
                    raise _Halt(_issue(ScopeIssueCode.ROOT_REPARSE_POINT, "."))
                if not stat.S_ISDIR(info.st_mode):
                    raise _Halt(_issue(ScopeIssueCode.ROOT_NOT_DIRECTORY, "."))
                state = _node_state(info)
                if state is None:
                    raise _Halt(_issue(ScopeIssueCode.ROOT_UNAVAILABLE, "."))
                chain.append((directory, state))
            return chain

        def check_ancestors(relative):
            current = self._root
            pieces = () if relative == "." else tuple(relative.split("/")[:-1])
            for part in pieces:
                current = current / part
                child_relative = current.relative_to(self._root).as_posix()
                info = lstat(current, child_relative)
                if info is None:
                    raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, child_relative))
                if _is_reparse(info):
                    add_issue(ScopeIssueCode.ANCESTOR_REPARSE_POINT, child_relative)
                    raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, child_relative))
                if not stat.S_ISDIR(info.st_mode):
                    raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, child_relative))
                state = _node_state(info)
                if state is None:
                    raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, child_relative))
                directories.append((child_relative, state))

        def scoped_path(relative):
            if ":" in relative:
                add_issue(ScopeIssueCode.ADS_PATH, relative)
                return None
            try:
                return PathRef(self._request.kind, self._request.root_id, relative)
            except ContractError:
                add_issue(ScopeIssueCode.INVALID_PATH, relative)
                return None

        def read_directory(relative):
            nonlocal used_directories
            if used_directories >= self._policy.max_directories:
                raise _Halt(_issue(ScopeIssueCode.DIRECTORY_BUDGET_EXHAUSTED, relative),
                            ScopeState.BUDGET_EXHAUSTED)
            used_directories += 1
            directory = self._root if relative == "." else self._root.joinpath(*relative.split("/"))
            info = lstat(directory, relative, root=relative == ".")
            if info is None:
                raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, relative))
            if _is_reparse(info):
                add_issue(ScopeIssueCode.REPARSE_POINT, relative)
                raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, relative))
            if not stat.S_ISDIR(info.st_mode):
                raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, relative))
            before = _node_state(info)
            if before is None:
                raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, relative))
            identity_key = (before.identity.device, before.identity.file_index)
            previous = directory_identities.get(identity_key)
            if previous is not None:
                add_exclusion(relative, "duplicate_directory_identity",
                              related=previous, identity=before.identity)
                return []
            directory_identities[identity_key] = relative
            directories.append((relative, before))
            names = []
            try:
                with self._io.scandir(directory) as children:
                    for child in children:
                        self._checkpoint(started, cancelled)
                        name = child.name if hasattr(child, "name") else child
                        if not isinstance(name, str) or not name or len(name) > 255:
                            raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, relative))
                        child_relative = name if relative == "." else relative + "/" + name
                        consume_entry(child_relative)
                        names.append(child_relative)
            except _Halt:
                raise
            except (OSError, ValueError, TypeError):
                raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, relative)) from None
            return sorted(names, key=lambda value: (_path_key(value), value))

        try:
            self._checkpoint(started, cancelled)
            ancestors = root_chain()
            selections = []
            for index, (is_prefix, raw) in enumerate(
                    [(True, value) for value in self._request.prefixes]
                    + [(False, value) for value in self._request.files]):
                self._checkpoint(started, cancelled)
                consume_entry("<selection>")
                if isinstance(raw, PathRef):
                    if (raw.kind != self._request.kind
                            or raw.root_id.casefold() != self._request.root_id.casefold()):
                        add_issue(ScopeIssueCode.ROOT_BINDING_MISMATCH,
                                  "<selection-%04d>" % index)
                        continue
                    relative = raw.relative_path
                elif not raw or len(raw) > 2048 or any(ord(char) < 32 for char in raw):
                    add_issue(ScopeIssueCode.INVALID_PATH, "<selection-%04d>" % index)
                    continue
                else:
                    relative = raw.replace("\\", "/")
                if relative != ".":
                    path = scoped_path(relative)
                    if path is None:
                        continue
                    relative = path.relative_path
                elif not is_prefix:
                    add_issue(ScopeIssueCode.INVALID_PATH, relative)
                    continue
                key = _path_key(relative)
                previous = selected_paths.get(key)
                if previous is not None:
                    add_exclusion(relative, "duplicate_selection", related=previous)
                    continue
                selected_paths[key] = relative
                selections.append((_path_key(relative), relative, is_prefix))
            selections.sort(key=lambda value: (value[0], value[1], not value[2]))
            pending_files = [(relative, key) for key, relative, prefix in selections if not prefix]
            pending_directories = [(relative, key) for key, relative, prefix in selections if prefix]
            visited_candidates: dict[str, str] = {}
            directory_queue: list[tuple[str, str]] = []
            while pending_files or pending_directories or directory_queue:
                self._checkpoint(started, cancelled)
                if pending_files:
                    relative, key = pending_files.pop(0)
                    if key in visited_candidates:
                        add_exclusion(relative, "duplicate_candidate",
                                      related=visited_candidates[key])
                        continue
                    visited_candidates[key] = relative
                    if relative == ".":
                        add_issue(ScopeIssueCode.DIRECTORY_AS_FILE, relative)
                        continue
                    check_ancestors(relative)
                    path_ref = scoped_path(relative)
                    if path_ref is None:
                        continue
                    target = self._root.joinpath(*relative.split("/"))
                    info = lstat(target, relative)
                    if info is None:
                        continue
                    if _is_reparse(info):
                        add_issue(ScopeIssueCode.REPARSE_POINT, relative)
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        add_issue(ScopeIssueCode.DIRECTORY_AS_FILE, relative)
                        continue
                    self._capture_file(relative, path_ref, info, entries,
                                       add_issue, add_exclusion)
                    continue
                if not directory_queue:
                    relative, key = pending_directories.pop(0)
                    if key in visited_candidates:
                        add_exclusion(relative, "duplicate_candidate",
                                      related=visited_candidates[key])
                        continue
                    visited_candidates[key] = relative
                    directory_queue.append((relative, key))
                relative, key = directory_queue.pop()
                # Discovery and traversal are distinct: the queued directory still
                # needs read_directory, without scheduling a duplicate traversal.

                if relative != ".":
                    check_ancestors(relative)
                    path_ref = scoped_path(relative)
                    if path_ref is None:
                        continue
                    target = self._root.joinpath(*relative.split("/"))
                    info = lstat(target, relative)
                    if info is None:
                        continue
                    if _is_reparse(info):
                        add_issue(ScopeIssueCode.REPARSE_POINT, relative)
                        continue
                    if not stat.S_ISDIR(info.st_mode):
                        add_issue(ScopeIssueCode.PREFIX_NOT_DIRECTORY, relative)
                        continue
                    name = relative.rsplit("/", 1)[-1]
                    rule = _rule_for(name, self._policy.directory_rules)
                    if rule is not None:
                        add_exclusion(relative, "excluded_directory", rule)
                        continue
                name = relative.rsplit("/", 1)[-1]
                rule = _rule_for(name, _OUTPUT_DIRECTORY_RULES)
                if rule is not None:
                    add_exclusion(relative, "excluded_directory", rule)
                    continue
                children = read_directory(relative)
                child_directories = []
                for child_relative in children:
                    self._checkpoint(started, cancelled)
                    child_key = _path_key(child_relative)
                    if child_key in visited_candidates:
                        add_exclusion(child_relative, "duplicate_candidate",
                                      related=visited_candidates[child_key])
                        continue
                    visited_candidates[child_key] = child_relative
                    path_ref = scoped_path(child_relative)
                    if path_ref is None:
                        continue
                    target = self._root.joinpath(*child_relative.split("/"))
                    info = lstat(target, child_relative)
                    if info is None:
                        continue
                    if _is_reparse(info):
                        add_issue(ScopeIssueCode.REPARSE_POINT, child_relative)
                        continue
                    name = child_relative.rsplit("/", 1)[-1]
                    if stat.S_ISDIR(info.st_mode):
                        rule = _rule_for(name, _OUTPUT_DIRECTORY_RULES) or _rule_for(
                            name, self._policy.directory_rules)
                        if rule is not None:
                            add_exclusion(child_relative, "excluded_directory", rule)
                        else:
                            child_directories.append((child_relative, child_key))
                        continue
                    rule = _rule_for(name, self._policy.file_rules)
                    if rule is not None:
                        add_exclusion(child_relative, "excluded_file", rule)
                        continue
                    self._capture_file(child_relative, path_ref, info, entries,
                                       add_issue, add_exclusion)
                directory_queue.extend(reversed(child_directories))
            for item in entries:
                self._checkpoint(started, cancelled)
                path = item.entry.path
                assert isinstance(path, PathRef)
                info = lstat(self._root.joinpath(*path.relative_path.split("/")),
                             path.relative_path)
                current = None if info is None else _node_state(info)
                if (current is None or _is_reparse(info) or not stat.S_ISREG(info.st_mode)
                        or current.identity != item.identity
                        or current.size_bytes != item.entry.content.size_bytes
                        or current.mtime_ns != item.mtime_ns
                        or current.attributes != item.attributes):
                    raise _Halt(_issue(ScopeIssueCode.FILE_CHANGED, path.relative_path))
            final_chain = root_chain()
            if final_chain != ancestors:
                raise _Halt(_issue(ScopeIssueCode.ROOT_CHANGED, "."))
            for relative, before in directories:
                self._checkpoint(started, cancelled)
                target = self._root if relative == "." else self._root.joinpath(*relative.split("/"))
                info = lstat(target, relative, root=relative == ".")
                current = None if info is None else _node_state(info)
                if (current != before or _is_reparse(info)
                        or not stat.S_ISDIR(info.st_mode)):
                    raise _Halt(_issue(ScopeIssueCode.DIRECTORY_CHANGED, relative))
            self._checkpoint(started, cancelled)
            state = ScopeState.COMPLETE_WITH_ISSUES if issues else ScopeState.COMPLETE
        except _Halt as halt:
            add_issue(halt.issue.code, halt.issue.relative_path, terminal=True)
            state = halt.state
            try:
                ancestors = root_chain()
                root_state = ancestors[-1][1]
            except _Halt as root_halt:
                add_issue(root_halt.issue.code, root_halt.issue.relative_path, terminal=True)
                return _Snapshot(None, (), tuple(exclusions), tuple(issues), ScopeState.PARTIAL,
                                 None, (), "", _CursorState(0, False), self._key)
        entries.sort(key=lambda item: (
            _path_key(item.entry.path.relative_path), item.entry.path.relative_path))
        exclusions.sort(key=lambda item: (_path_key(item.relative_path),
                                         item.relative_path, item.reason))
        issues.sort(key=lambda item: (_path_key(item.relative_path),
                                     item.relative_path, item.code.value))
        snapshot_id = secrets.token_hex(16)
        if state is not ScopeState.COMPLETE:
            entries.clear()
            exclusions.clear()
        root_state = ancestors[-1][1]
        digest = hashlib.sha256(canonical_json({
            "schema_version": SCOPE_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "state": state.value,
            "root": asdict(root_state),
            "ancestors": [[str(path), asdict(value)] for path, value in ancestors],
            "entries": [self._file_data(item) for item in entries],
            "exclusions": [_plain_json(asdict(item)) for item in exclusions],
            "issues": [_plain_json(asdict(item)) for item in issues],
            "rules_digest": self._rules_digest,
        }).encode("utf-8")).hexdigest()
        return _Snapshot(snapshot_id, tuple(entries), tuple(exclusions), tuple(issues),
                         state, root_state, tuple(ancestors), digest,
                         _CursorState(0, True), self._key)

    @staticmethod
    def _file_data(item: _FileSnapshot) -> dict[str, object]:
        return {"entry": asdict(item.entry), "identity": asdict(item.identity),
                "mtime_ns": item.mtime_ns, "attributes": item.attributes}

    def _capture_file(self, relative, path_ref, info, entries, add_issue, add_exclusion):
        if not stat.S_ISREG(info.st_mode):
            add_issue(ScopeIssueCode.NOT_REGULAR_FILE, relative)
            return
        state = _node_state(info)
        if state is None:
            add_issue(ScopeIssueCode.FILE_IDENTITY_UNAVAILABLE, relative)
            return
        for item in entries:
            if item.identity == state.identity:
                add_exclusion(relative, "duplicate_file_identity",
                              related=item.entry.path.relative_path, identity=state.identity)
                return
        asset = "scope_" + hashlib.sha256(canonical_json([
            "archive-scope-v1", self._request.kind, self._request.root_id.casefold(),
            path_ref.relative_path,
        ]).encode("utf-8")).hexdigest()
        content = ContentVersion(asset, 0, state.size_bytes, None)
        entries.append(_FileSnapshot(ArchiveEntry(content, path_ref), state.identity,
                                     state.mtime_ns, state.attributes))

    def _check_snapshot_root(self, snapshot: _Snapshot) -> None:
        for directory, before in snapshot.ancestors:
            try:
                info = self._io.lstat(directory)
            except OSError:
                raise _Halt(_issue(ScopeIssueCode.ROOT_UNAVAILABLE, ".")) from None
            if _is_reparse(info):
                raise _Halt(_issue(ScopeIssueCode.ROOT_REPARSE_POINT, "."))
            if not stat.S_ISDIR(info.st_mode):
                raise _Halt(_issue(ScopeIssueCode.ROOT_UNAVAILABLE, "."))
            if _node_state(info) != before:
                raise _Halt(_issue(ScopeIssueCode.ROOT_CHANGED, "."))

    def _empty_result(self, snapshot: _Snapshot, cancellation) -> ScopePage:
        issues = snapshot.issues
        cancelled = cancellation is not None and cancellation.is_set()
        if cancelled:
            issues += (_issue(ScopeIssueCode.CANCELLED, "."),)
        completion = ScopeCompletion(
            ScopeState.CANCELLED if cancelled else ScopeState.PARTIAL,
            None, None, 0, len(snapshot.exclusions), len(issues), None,
        )
        return ScopePage(
            entries=(), exclusions=snapshot.exclusions, issues=issues, page_index=0,
            has_next_page=False, cursor=None, completion=completion,
            root_id=self._request.root_id, kind=self._request.kind,
            root_revision=self._request.root_revision, rules_digest=self._rules_digest,
        )


def enumerate_scope(root, request: ScopeRequest, policy: ScopePolicy | None = None,
                    *, cancellation=None) -> ScopePage:
    """Convenience first-page entry point; retain ScopeEnumerator for more pages."""
    if cancellation is not None and not callable(getattr(cancellation, "is_set", None)):
        raise ContractError("Cancellation must provide is_set()")
    return ScopeEnumerator(root, request, policy).page(cancellation=cancellation)
