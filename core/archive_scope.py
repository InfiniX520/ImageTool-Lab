"""Read-only, bounded enumeration of an explicit local archive scope.

The first page request builds one bounded metadata snapshot; later pages read
that immutable snapshot.  A cursor is not a resumable filesystem scan: partial
snapshots stay partial.  Callers explicitly pass returned ArchiveEntry objects
to LocalObserver/ObservationRequest.  This module never invokes an observer,
opens file contents, calculates a content hash, or writes business data.

Every entry point - recursive prefixes, explicit files, and explicit directory
prefixes - passes the same eligibility policy: the canonical root-relative
path, every ancestor, the sensitive-file rules, and the configured output
roots.  An explicit user selection never disables credential or managed-output
protection, and every exclusion carries a reason.

Names that look alike are not the same file.  Sorting keys are normalised for a
deterministic order only; file identity is decided by the operating-system
identity (device, file index) and by the exact relative path.  Two physically
distinct files whose names differ only by Unicode composition are both kept,
and hardlink aliases share one content identity while every path is retained.

Two secondary costs are bounded explicitly instead of being repeated inside a
loop.  Explicit file selections resolve their exact raw spelling through one
directory read per parent (grouped by parent, bound to the directory identity
and node state observed around that read), so adding another selected file in
an already indexed directory costs no further directory pass.  Between-page
stability verification is a session with a stated total cost: each page
verifies a bounded rotating slice of the recorded directories, and the page
that can complete a whole-snapshot receipt re-verifies every directory before
that receipt can exist.  Directory replacement, root replacement, and real
membership changes are still refused.

Directory identity and metadata checks detect ordinary enumeration races; they
are not an atomic defence against an adversarial filesystem writer.  Final
content identity and stability remain LocalObserver's responsibility.

Cancellation and the execution time budget are checked before and after every
filesystem call, at every directory entry, and once more before a page is
returned.  Only work performed inside one bounded operation (one enumeration or
one page verification) is charged: time a caller spends idle between two page
requests is never part of an execution budget.  A single blocking operating
system call cannot be interrupted from inside this module, so the honest bound
is "no further work between two calls"; that limit is stated, not hidden.
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
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field, replace
from enum import Enum, StrEnum
from pathlib import Path
from typing import ContextManager

from core.archive_inventory import ArchiveEntry
from core.local_observation import (
    FileIdentity, LocalObservation, ObservationBudget, ObservationRequest,
    ObservationStatus,
)
from core.preservation_policy import ContentVersion, LocalEvidence, LocalState
from core.task_contracts import ContractError, PathRef, canonical_json, normalize_json

SCOPE_SCHEMA_VERSION = 1
SCOPE_RECEIPT_SCHEMA_VERSION = 2
_CURSOR_SCHEMA = "jpgpng.archive-scope.cursor.v1"
_RECEIPT_SCHEMA = "jpgpng.archive-scope.receipt.v2"
_RECEIPT_SCHEMA_V1 = "jpgpng.archive-scope.receipt.v1"
_MAX_CURSOR_BYTES = 16384
_RECEIPT_FIELD_LIMIT = 128
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
# Bounds for the two auxiliary structures this module keeps while it reads.
# They are only reachable by a scope that cannot be verified inside its own
# budgets, so they stop with an explicit issue instead of growing unbounded.
_MAX_INDEXED_DIRECTORIES = 4096
_MAX_INDEXED_NAMES = 2000000
_MAX_NAME_INDEX_RESCANS = 4
_MAX_MEMBERSHIP_RESCANS = 8

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
    """Deterministic sort/display key.  Never a file identity or dedup key.

    Two physically distinct files can share one such key (NFC versus NFD, or a
    case-insensitive filesystem).  Only sorting, cursor boundary labels, and
    matching configured output-root names may use it.
    """
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
        if os.name == "nt" and str(candidate).startswith("\\"):
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


def _output_root_rules(value: object) -> tuple[str, ...]:
    """Configured output directories are root-relative paths, not name globs."""

    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        raise ContractError("Invalid output root rules")
    if len(value) > 128:
        raise ContractError("Too many output root rules")
    result = []
    for raw in value:
        if (not isinstance(raw, str) or not raw or len(raw) > 2048
                or any(ord(char) < 32 for char in raw)):
            raise ContractError("Invalid output root rules")
        normalized = raw.replace("\\", "/")
        pieces = normalized.split("/")
        if normalized.startswith("/") or any(piece in {"", ".", ".."} for piece in pieces):
            raise ContractError("Invalid output root rules")
        result.append(normalized)
    return tuple(sorted(set(result), key=lambda item: (_path_key(item), item)))


@dataclass(frozen=True, slots=True)
class ScopePolicy:
    """Deny rules are matched case-insensitively against individual names.

    Additional rules extend, rather than disable, output/sensitive defaults.
    `output_roots` names this scope's own output directories as root-relative
    paths, so a configured writer directory is protected even when it is not
    called `archive_store`.  No extension or image-format whitelist silently
    narrows a selected scope.  At most one terminal issue beyond max_issues is
    retained to explain an issue-budget stop.

    Entry, directory, issue, and exclusion budgets cover the entire snapshot.
    The time budget bounds the work of one bounded operation - one enumeration,
    or one page's validity check - and is checked before and after every
    filesystem call, so idle time between page requests is never charged as
    execution time.  A blocking operating-system call cannot be interrupted
    from inside this module; the budget stops the next call instead.
    """

    additional_directory_excludes: tuple[str, ...] = ()
    additional_file_excludes: tuple[str, ...] = ()
    max_entries: int = 10000
    max_directories: int = 2000
    max_seconds: float = 30.0
    max_issues: int = 512
    max_exclusions: int = 10000
    output_roots: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "additional_directory_excludes", _rules(
            self.additional_directory_excludes, "directory exclusion rules"))
        object.__setattr__(self, "additional_file_excludes", _rules(
            self.additional_file_excludes, "file exclusion rules"))
        object.__setattr__(self, "output_roots", _output_root_rules(self.output_roots))
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
    def output_root_rules(self) -> tuple[str, ...]:
        """Default managed-output names plus explicit directory exclusions."""
        return _OUTPUT_DIRECTORY_RULES + self.additional_directory_excludes

    @property
    def file_rules(self) -> tuple[str, ...]:
        return _SENSITIVE_FILE_RULES + self.additional_file_excludes

    def _digest_data(self) -> dict[str, object]:
        return {
            "schema_version": SCOPE_SCHEMA_VERSION,
            "directory_rules": self.directory_rules,
            "file_rules": self.file_rules,
            "output_roots": self.output_roots,
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
    NAME_INDEX_BUDGET_EXHAUSTED = "name_index_budget_exhausted"
    CANCELLED = "cancelled"


class ScopeState(StrEnum):
    COMPLETE = "complete"
    COMPLETE_WITH_ISSUES = "complete_with_issues"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"


class ScopeVerdict(StrEnum):
    ACCEPT = "accept"
    EXCLUDE = "exclude"


class ScopeExclusionReason(StrEnum):
    """Every exclusion states why the path is not part of the scope."""

    EXCLUDED_DIRECTORY = "excluded_directory"
    EXCLUDED_ANCESTOR_DIRECTORY = "excluded_ancestor_directory"
    EXCLUDED_FILE = "excluded_file"
    DUPLICATE_SELECTION = "duplicate_selection"
    DUPLICATE_CANDIDATE = "duplicate_candidate"
    DUPLICATE_DIRECTORY_IDENTITY = "duplicate_directory_identity"


@dataclass(frozen=True, slots=True)
class ScopeEligibility:
    """Structured accept/exclude decision from the single eligibility policy."""

    verdict: ScopeVerdict
    reason: ScopeExclusionReason | None = None
    rule: str | None = None
    related_path: str | None = None

    def __post_init__(self):
        if not isinstance(self.verdict, ScopeVerdict):
            raise ContractError("Invalid scope eligibility verdict")
        if self.verdict is ScopeVerdict.EXCLUDE and self.reason is None:
            raise ContractError("An exclusion requires an explicit reason")
        if self.verdict is ScopeVerdict.ACCEPT and (
                self.reason is not None or self.rule is not None
                or self.related_path is not None):
            raise ContractError("An accepted path carries no exclusion facts")

    @property
    def accepted(self) -> bool:
        return self.verdict is ScopeVerdict.ACCEPT


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
class ScopeFileAlias:
    """Two paths for one physical file; content is shared, paths are not lost."""

    relative_path: str
    alias_of: str
    identity: FileIdentity
    asset_id: str

    def __post_init__(self):
        if not isinstance(self.identity, FileIdentity):
            raise ContractError("Invalid alias identity")
        if self.relative_path == self.alias_of:
            raise ContractError("An alias must name a different path")


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
    aliases: tuple[ScopeFileAlias, ...] = ()
    selection_digest: str = ""
    entry_facts: tuple[ScopeEntryFact, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "entries", tuple(self.entries))
        object.__setattr__(self, "exclusions", tuple(self.exclusions))
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(self, "aliases", tuple(self.aliases))
        facts = tuple(self.entry_facts)
        if len(facts) != len(self.entries):
            raise ContractError("A scope page needs one entry fact per entry")
        for entry, fact in zip(self.entries, facts):
            if not isinstance(fact, ScopeEntryFact) or not isinstance(entry.path, PathRef):
                raise ContractError("Invalid scope page entry fact")
            if (fact.relative_path != entry.path.relative_path
                    or fact.kind != entry.path.kind
                    or fact.root_id != entry.path.root_id):
                raise ContractError("Scope page entry fact differs from its entry")
            identity = entry.physical_identity
            if fact.physical_identity != identity:
                raise ContractError("Scope page entry fact physical identity differs")
            if (entry.content is None or fact.size_bytes != entry.content.size_bytes
                    or fact.content_asset_id != entry.content.asset_id
                    or fact.content_revision != entry.content.revision
                    or fact.content_sha256 != entry.content.sha256):
                raise ContractError("Scope page entry fact content differs")
        object.__setattr__(self, "entry_facts", facts)
        if (self.cursor is not None) != self.has_next_page:
            raise ContractError("Cursor and next-page state disagree")
        if self.cursor is not None and not self.completion.snapshot_id:
            raise ContractError("A cursor requires a snapshot")


class ScopeSelectionKind(StrEnum):
    SNAPSHOT = "snapshot"
    SUB_BATCH = "sub_batch"


@dataclass(frozen=True, slots=True)
class ScopeSubBatch:
    """An explicit, described, page-aligned batch of one snapshot.

    A sub-batch has its own range description and can never carry the
    whole-snapshot completion mark.
    """

    limit: int
    description: str

    def __post_init__(self):
        _integer(self.limit, "sub-batch entry limit", 1, 1000000)
        if (not isinstance(self.description, str) or not 1 <= len(self.description) <= 200
                or any(ord(char) < 32 for char in self.description)):
            raise ContractError("Invalid sub-batch description")


def _digest_text(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ScopeEntryFact:
    """One enumerated file as the exact path/root/physical object of that scan.

    This is the v2 receipt entry fact.  It deliberately carries no content
    digest: scope enumeration never reads bytes.  ``size_bytes`` is the
    enumerated size; ``mtime_ns`` and the OS identity bind the candidate to the
    object the observer must later open.  A receipt that only repeated a path
    and a size could not say which physical file those facts described.
    """

    kind: str
    root_id: str
    root_revision: int
    relative_path: str
    device: int | None
    file_index: int | None
    size_bytes: int
    mtime_ns: int
    content_asset_id: str | None = None
    content_revision: int | None = None
    content_sha256: str | None = None

    def __post_init__(self):
        if self.kind not in {"project", "external"}:
            raise ContractError("Invalid scope entry kind")
        if not isinstance(self.root_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,64}", self.root_id):
            raise ContractError("Invalid scope entry root identity")
        _integer(self.root_revision, "scope entry root revision", 0, 2**63 - 1)
        try:
            path = PathRef(self.kind, self.root_id, self.relative_path)
        except ContractError as exc:
            raise ContractError("Invalid scope entry path") from exc
        if path.relative_path == ".":
            raise ContractError("A scope entry must name a file")
        object.__setattr__(self, "relative_path", path.relative_path)
        if (self.device is None) != (self.file_index is None):
            raise ContractError("A scope entry needs both device and file index")
        if self.device is not None:
            FileIdentity(self.device, self.file_index)
        _integer(self.size_bytes, "scope entry size", 0, 2**63 - 1)
        if (type(self.mtime_ns) is not int
                or not -(2**63) <= self.mtime_ns <= 2**63 - 1):
            raise ContractError("Invalid scope entry modification time")
        if self.content_asset_id is None:
            if self.content_revision is not None or self.content_sha256 is not None:
                raise ContractError("Scope entry content facts are incomplete")
        else:
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.content_asset_id):
                raise ContractError("Invalid scope entry content asset")
            _integer(self.content_revision, "scope entry content revision", 0, 2**63 - 1)
            if self.content_sha256 is not None and not re.fullmatch(
                    r"[0-9a-f]{64}", self.content_sha256):
                raise ContractError("Invalid scope entry content digest")

    @property
    def physical_identity(self) -> FileIdentity | None:
        if self.device is None:
            return None
        return FileIdentity(self.device, self.file_index)

    def row(self) -> list[object]:
        return [
            self.kind, self.root_id, self.root_revision, self.relative_path,
            self.device, self.file_index, self.size_bytes, self.mtime_ns,
            self.content_asset_id, self.content_revision, self.content_sha256,
        ]

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind, "root_id": self.root_id,
            "root_revision": self.root_revision,
            "relative_path": self.relative_path,
            "device": self.device, "file_index": self.file_index,
            "size_bytes": self.size_bytes, "mtime_ns": self.mtime_ns,
            "content_asset_id": self.content_asset_id,
            "content_revision": self.content_revision,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> ScopeEntryFact:
        if not isinstance(value, dict) or set(value) != {
            "kind", "root_id", "root_revision", "relative_path", "device",
            "file_index", "size_bytes", "mtime_ns", "content_asset_id",
            "content_revision", "content_sha256",
        }:
            raise ContractError("Invalid scope entry fact fields")
        return cls(**value)


def _facts_digest(facts: Iterable[ScopeEntryFact]) -> str:
    return _digest_text([fact.row() for fact in facts])


def _entry_fact(entry: ArchiveEntry, *, root_revision: int,
                mtime_ns: int = 0) -> ScopeEntryFact:
    path = entry.path
    if not isinstance(path, PathRef) or entry.content is None:
        raise ContractError("A scope entry fact requires a PathRef and content size")
    identity = entry.physical_identity
    return ScopeEntryFact(
        path.kind, path.root_id, root_revision, path.relative_path,
        None if identity is None else identity.device,
        None if identity is None else identity.file_index,
        entry.content.size_bytes, mtime_ns, entry.content.asset_id,
        entry.content.revision, entry.content.sha256,
    )


def _entries_digest(entries: Iterable[ArchiveEntry], *, root_revision: int = 0,
                    mtime_ns: int = 0) -> str:
    """Compatibility helper: digest explicit plan entries as v2 facts."""
    return _facts_digest(
        _entry_fact(entry, root_revision=root_revision, mtime_ns=mtime_ns)
        for entry in entries
    )


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    """One enumerated candidate handed to a concrete file observer.

    The enumerator freezes the raw path spelling, root mapping revision and the
    OS physical facts of the directory entry.  The observer receives this
    object, never only a path plus a size, so a replaced object cannot silently
    inherit the old candidate's receipt.
    """

    kind: str
    root_id: str
    root_revision: int
    relative_path: str
    physical_identity: FileIdentity | None
    size_bytes: int
    mtime_ns: int
    snapshot_id: str
    run_id: str

    def __post_init__(self):
        if self.kind not in {"project", "external"}:
            raise ContractError("Invalid candidate kind")
        if not isinstance(self.root_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,64}", self.root_id):
            raise ContractError("Invalid candidate root identity")
        _integer(self.root_revision, "candidate root revision", 0, 2**63 - 1)
        try:
            path = PathRef(self.kind, self.root_id, self.relative_path)
        except ContractError as exc:
            raise ContractError("Invalid candidate path") from exc
        if path.relative_path == ".":
            raise ContractError("A candidate must name a file")
        object.__setattr__(self, "relative_path", path.relative_path)
        if self.physical_identity is not None and not isinstance(
                self.physical_identity, FileIdentity):
            raise ContractError("Invalid candidate physical identity")
        _integer(self.size_bytes, "candidate size", 0, 2**63 - 1)
        if (type(self.mtime_ns) is not int
                or not -(2**63) <= self.mtime_ns <= 2**63 - 1):
            raise ContractError("Invalid candidate modification time")
        _token_text(self.snapshot_id, "candidate snapshot ID")
        _token_text(self.run_id, "candidate run ID")

    @property
    def path(self) -> PathRef:
        return PathRef(self.kind, self.root_id, self.relative_path)

    def entry_fact(self) -> ScopeEntryFact:
        identity = self.physical_identity
        return ScopeEntryFact(
            self.kind, self.root_id, self.root_revision, self.relative_path,
            None if identity is None else identity.device,
            None if identity is None else identity.file_index,
            self.size_bytes, self.mtime_ns,
        )


@dataclass(frozen=True, slots=True)
class ObservedFile:
    """The concrete read result for one candidate, with no inferred identity."""

    candidate: CandidateSnapshot
    expected_content: ContentVersion
    content: ContentVersion | None
    digest_complete: bool
    bytes_read: int
    observed_size: int | None
    observed_mtime_ns: int | None
    observed_identity: FileIdentity | None
    observation: LocalObservation | None = field(default=None, compare=False, repr=False)

    def __post_init__(self):
        if not isinstance(self.candidate, CandidateSnapshot):
            raise ContractError("Invalid observed candidate")
        if not isinstance(self.expected_content, ContentVersion):
            raise ContractError("Invalid expected content for observation")
        if type(self.digest_complete) is not bool:
            raise ContractError("Invalid observation completion flag")
        _integer(self.bytes_read, "observed bytes read", 0, 2**63 - 1)
        if self.observed_size is not None and (
                type(self.observed_size) is not int or self.observed_size < 0):
            raise ContractError("Invalid observed size")
        if self.observed_mtime_ns is not None and (
                type(self.observed_mtime_ns) is not int
                or not -(2**63) <= self.observed_mtime_ns <= 2**63 - 1):
            raise ContractError("Invalid observed modification time")
        if self.observed_identity is not None and not isinstance(
                self.observed_identity, FileIdentity):
            raise ContractError("Invalid observed physical identity")
        if self.digest_complete:
            if (self.content is None or self.content.sha256 is None
                    or self.observed_size is None or self.observed_identity is None
                    or self.observed_mtime_ns is None):
                raise ContractError(
                    "A complete observation needs content, size, mtime and identity")
            if (self.content.size_bytes != self.observed_size
                    or self.content.asset_id != self.expected_content.asset_id
                    or self.content.revision != self.expected_content.revision):
                raise ContractError("Complete observation facts disagree")
        elif self.content is not None:
            raise ContractError("An incomplete observation cannot assert content")
        if self.observation is not None:
            if not isinstance(self.observation, LocalObservation):
                raise ContractError("Invalid local observation result")
            if self.observation.evidence.path != self.candidate.path:
                raise ContractError("Observation path differs from its candidate")


class ResolutionKind(StrEnum):
    REUSE = "reuse"
    NEW_REVISION = "new_revision"
    REPLACEMENT = "replacement"
    BASELINE = "baseline"
    CONFLICT = "conflict"
    NEEDS_CONFIRMATION = "needs_confirmation"


@dataclass(frozen=True, slots=True)
class ContentBinding:
    """The current path binding and the last *accepted* physical evidence.

    ``physical_identity`` and ``root_revision`` come from the acceptance the
    binding points at (``acceptance_id``), never from the newest diagnostic
    observation of this path.  A binding created by a plan has no acceptance, so
    its physical object stays explicitly unknown and a later content change still
    needs a human decision rather than a guessed "same object".
    """

    content: ContentVersion
    physical_identity: FileIdentity | None = None
    acceptance_id: str | None = None
    root_revision: int | None = None

    def __post_init__(self):
        if not isinstance(self.content, ContentVersion):
            raise ContractError("Invalid bound content")
        if self.physical_identity is not None and not isinstance(
                self.physical_identity, FileIdentity):
            raise ContractError("Invalid bound physical identity")
        if self.acceptance_id is not None and (
                not isinstance(self.acceptance_id, str) or not self.acceptance_id):
            raise ContractError("Invalid accepted evidence reference")
        if self.root_revision is not None:
            _integer(self.root_revision, "binding root revision", 0, 2**63 - 1)
        if self.physical_identity is not None and self.root_revision is None:
            raise ContractError("An accepted physical object needs its root revision")


@dataclass(frozen=True, slots=True)
class ContentResolution:
    """One explicit decision from candidate/observation to a content revision."""

    kind: ResolutionKind
    candidate: CandidateSnapshot
    content: ContentVersion | None
    previous_content: ContentVersion | None
    reason: str
    next_action: str = "none"
    requires_confirmation: bool = False
    scope_complete: bool = True
    # True when the resolver saw no binding at all for this path, so the expected
    # old state is "must not exist".  previous_content is then only the unknown
    # historical placeholder the observation was probed with; it is not a binding
    # and must never be used as an expected old value.
    expected_binding_absent: bool = False
    # The exact acceptance the binding pointed at when this decision was resolved.
    # Together with previous_content and expected_root_revision it is the immutable
    # expectation token the writer compares before inserting any row.  None means
    # "the binding had no acceptance" (an index-only binding created by a plan),
    # never "do not check": "there was no binding" is expected_binding_absent.
    expected_acceptance_id: str | None = None
    # The root mapping generation that acceptance recorded.  None belongs only to
    # an index-only binding, which never claimed a physical object.
    expected_root_revision: int | None = None

    def __post_init__(self):
        if not isinstance(self.kind, ResolutionKind):
            raise ContractError("Invalid content resolution kind")
        if type(self.expected_binding_absent) is not bool:
            raise ContractError("Invalid absent-binding flag")
        if self.expected_acceptance_id is not None and (
                not isinstance(self.expected_acceptance_id, str)
                or re.fullmatch(r"[0-9a-f]{64}", self.expected_acceptance_id) is None):
            raise ContractError("Invalid expected acceptance identity")
        if self.expected_root_revision is not None:
            _integer(
                self.expected_root_revision, "expected binding root revision",
                0, 2**63 - 1,
            )
        if self.expected_binding_absent:
            if (self.expected_acceptance_id is not None
                    or self.expected_root_revision is not None):
                raise ContractError(
                    "An absent binding cannot carry an acceptance token")
        elif ((self.expected_acceptance_id is None)
                != (self.expected_root_revision is None)):
            raise ContractError(
                "A binding expectation needs both its acceptance and root generation")
        if not isinstance(self.candidate, CandidateSnapshot):
            raise ContractError("Invalid content resolution candidate")
        for value, name in ((self.content, "content"),
                            (self.previous_content, "previous content")):
            if value is not None and not isinstance(value, ContentVersion):
                raise ContractError("Invalid " + name)
        _token_text(self.reason, "content resolution reason")
        _token_text(self.next_action, "content resolution next action")
        if type(self.requires_confirmation) is not bool:
            raise ContractError("Invalid confirmation flag")
        if type(self.scope_complete) is not bool:
            raise ContractError("Invalid resolution completeness flag")
        resolved = self.kind in {
            ResolutionKind.REUSE, ResolutionKind.NEW_REVISION,
            ResolutionKind.REPLACEMENT, ResolutionKind.BASELINE,
        }
        if resolved:
            if (self.content is None or self.requires_confirmation
                    or not self.scope_complete or self.next_action != "none"):
                raise ContractError("A resolved content decision must be complete")
        elif self.scope_complete or not self.requires_confirmation:
            raise ContractError("An unresolved content decision cannot claim a complete scope")
        if (self.kind is ResolutionKind.NEW_REVISION
                and (self.previous_content is None or self.content is None
                     or self.content.asset_id != self.previous_content.asset_id
                     or self.content.revision <= self.previous_content.revision)):
            raise ContractError("A new revision needs the same asset and a higher revision")

    @property
    def requires_attention(self) -> bool:
        return self.requires_confirmation or not self.scope_complete


def _resolution(
    kind: ResolutionKind, candidate: CandidateSnapshot,
    content: ContentVersion | None, previous: ContentVersion | None,
    reason: str, *, binding: ContentBinding | None, next_action: str = "none",
    requires_confirmation: bool = False, scope_complete: bool = True,
) -> ContentResolution:
    """Build one decision, freezing the binding generation it was computed from.

    ``binding`` is the current binding the resolver read, or None when the path had
    none.  The token is derived here and only here, so every resolved decision
    carries the exact acceptance and root generation the writer must still see.
    """
    if binding is not None and not isinstance(binding, ContentBinding):
        raise ContractError("Invalid existing content binding")
    return ContentResolution(
        kind, candidate, content, previous, reason, next_action,
        requires_confirmation, scope_complete,
        expected_binding_absent=binding is None,
        expected_acceptance_id=None if binding is None else binding.acceptance_id,
        expected_root_revision=None if binding is None else binding.root_revision,
    )


def _unresolved(
    kind: ResolutionKind, candidate: CandidateSnapshot, reason: str,
    next_action: str, *, binding: ContentBinding | None,
) -> ContentResolution:
    return _resolution(
        kind, candidate, None, None, reason, binding=binding, next_action=next_action,
        requires_confirmation=True, scope_complete=False,
    )


def resolve_content_version(
    candidate: CandidateSnapshot, observed: ObservedFile, *,
    existing: ContentBinding | None = None, digest_known: bool = False,
    known_content: ContentVersion | None = None,
    replacement_confirmed: bool = False,
    asset_max_revision: int | None = None,
) -> ContentResolution:
    """Central, pure candidate/observation to content-generation resolver.

    Order is fixed: prove the observed bytes belong to the enumerated physical
    object first, then reuse, advance a revision, or stop for a replacement
    decision.  Nothing here writes a database or guesses a missing digest.

    Logical asset identity is separate from byte de-duplication.  ``known_content``
    is evidence that "these exact bytes are already indexed"; it may be reused only
    when it belongs to this candidate's own logical asset.  A second, independent
    file with identical bytes therefore always establishes its own generation
    instead of inheriting another asset's revision, and ``digest_known`` alone can
    never authorize adopting a generation whose owner is unknown.  Two paths over
    one physical object (hardlinks) carry the same asset identity from the
    enumerator, so they still reuse one generation.

    Every returned decision freezes the exact binding generation it was computed
    from - the content key, the acceptance it points at and that acceptance's root
    mapping generation.  The writer compares that token before inserting anything,
    so a decision produced before another acceptance, root remap or same-content
    physical replacement is refused instead of silently overwriting the newer
    evidence.  "No binding" is its own value (``expected_binding_absent``), never a
    None that also means "do not check".

    Revisions are allocated from the asset's own history: ``asset_max_revision`` is
    the highest generation already stored for this logical asset, so a renamed
    object or a hardlink name that lagged behind continues that chain instead of
    re-deriving a revision another path already occupies.  Identical bytes in a
    different asset never take part in that floor, and the writer rechecks it
    inside its transaction before inserting anything.
    """
    if not isinstance(candidate, CandidateSnapshot) or not isinstance(observed, ObservedFile):
        raise ContractError("Expected a candidate and its observation")

    def decision(*arguments: object, **keywords: object) -> ContentResolution:
        return _resolution(*arguments, binding=existing, **keywords)

    def refusal(*arguments: object, **keywords: object) -> ContentResolution:
        return _unresolved(*arguments, binding=existing, **keywords)

    # The asset's own history, not just this path's binding, decides the next free
    # generation.  A renamed object and a lagging hardlink name keep the same scope
    # asset id, so their new bytes must continue that asset's chain instead of
    # re-deriving a revision another path already holds.
    if asset_max_revision is not None:
        _integer(asset_max_revision, "asset maximum revision", 0, 2**63 - 1)
    history_floor = 0 if asset_max_revision is None else asset_max_revision

    if observed.candidate != candidate:
        return refusal(
            ResolutionKind.CONFLICT, candidate, "candidate_mismatch", "reobserve")
    if not observed.digest_complete or observed.content is None:
        return refusal(
            ResolutionKind.CONFLICT, candidate, "observation_incomplete", "reobserve")
    if (observed.content.sha256 is None or observed.observed_size is None
            or observed.observed_mtime_ns is None or observed.observed_identity is None):
        return refusal(
            ResolutionKind.NEEDS_CONFIRMATION, candidate,
            "physical_facts_unavailable", "reobserve")
    if candidate.physical_identity is None:
        return refusal(
            ResolutionKind.NEEDS_CONFIRMATION, candidate,
            "candidate_physical_identity_missing", "reobserve")
    if observed.observed_identity != candidate.physical_identity:
        return refusal(
            ResolutionKind.CONFLICT, candidate,
            "physical_identity_changed_during_observation", "reobserve")
    if observed.observed_size != candidate.size_bytes:
        return refusal(
            ResolutionKind.CONFLICT, candidate, "size_changed_during_observation",
            "reobserve")
    if observed.observed_mtime_ns != candidate.mtime_ns:
        return refusal(
            ResolutionKind.CONFLICT, candidate, "mtime_changed_during_observation",
            "reobserve")
    observed_content = observed.content
    if observed_content.size_bytes != observed.observed_size:
        return refusal(
            ResolutionKind.CONFLICT, candidate, "observed_content_size_mismatch",
            "reobserve")

    if existing is None:
        # Byte de-duplication may reuse a generation only when it belongs to this
        # exact logical asset.  Another asset's identical bytes are evidence, not
        # an identity: adopting them would give one asset two owners.
        if (known_content is not None
                and known_content.asset_id == observed_content.asset_id
                and known_content.sha256 == observed_content.sha256
                and known_content.size_bytes == observed_content.size_bytes):
            return decision(
                ResolutionKind.REUSE, candidate, known_content, None,
                "known_content_first_binding")
        baseline_revision = max(
            observed.expected_content.revision, observed_content.revision,
            history_floor,
        ) + 1
        baseline = ContentVersion(
            observed_content.asset_id, baseline_revision,
            observed_content.size_bytes, observed_content.sha256,
        )
        # No previous binding exists, so previous_content stays None: the probe the
        # observation was taken with is not a bound generation.
        return decision(
            ResolutionKind.BASELINE, candidate, baseline, None,
            "first_observation_baseline")

    if not isinstance(existing, ContentBinding):
        raise ContractError("Invalid existing content binding")
    previous = existing.content
    content_matches = (
        previous.sha256 is not None
        and previous.sha256 == observed_content.sha256
        and previous.size_bytes == observed_content.size_bytes
    )
    # The accepted physical object is only comparable inside the same root
    # generation: after a remap the same path may name a different directory, so
    # a recycled device/file index is not proof of "the same file".
    root_generation_differs = (
        existing.root_revision is not None
        and existing.root_revision != candidate.root_revision
    )
    same_object = (
        None if existing.physical_identity is None or root_generation_differs
        else existing.physical_identity == observed.observed_identity
    )
    if same_object is False:
        if not replacement_confirmed:
            return refusal(
                ResolutionKind.NEEDS_CONFIRMATION, candidate,
                "physical_object_replaced", "confirm_same_name_replacement")
        if content_matches:
            return decision(
                ResolutionKind.REPLACEMENT, candidate, previous, previous,
                "confirmed_replacement_same_content")
        revision = max(
            previous.revision, observed.expected_content.revision,
            observed_content.revision, history_floor,
        ) + 1
        replacement = ContentVersion(
            previous.asset_id, revision, observed_content.size_bytes,
            observed_content.sha256,
        )
        return decision(
            ResolutionKind.REPLACEMENT, candidate, replacement, previous,
            "confirmed_physical_replacement")
    if previous.sha256 is None:
        revision = max(
            previous.revision, observed.expected_content.revision,
            observed_content.revision, history_floor,
        ) + 1
        baseline = ContentVersion(
            previous.asset_id, revision, observed_content.size_bytes,
            observed_content.sha256,
        )
        return decision(
            ResolutionKind.BASELINE, candidate, baseline, previous,
            "unknown_history_baseline")
    if content_matches:
        return decision(
            ResolutionKind.REUSE, candidate, previous, previous,
            "same_physical_object_same_content")
    if same_object is None:
        if replacement_confirmed:
            revision = max(
                previous.revision, observed_content.revision, history_floor) + 1
            replacement = ContentVersion(
                previous.asset_id, revision, observed_content.size_bytes,
                observed_content.sha256,
            )
            return decision(
                ResolutionKind.REPLACEMENT, candidate, replacement, previous,
                "confirmed_replacement_unknown_prior_identity")
        return refusal(
            ResolutionKind.NEEDS_CONFIRMATION, candidate,
            "root_generation_changed" if root_generation_differs
            else "previous_physical_identity_unknown",
            "reobserve_or_confirm")
    revision = max(previous.revision, observed_content.revision, history_floor) + 1
    newer = ContentVersion(
        previous.asset_id, revision, observed_content.size_bytes,
        observed_content.sha256,
    )
    return decision(
        ResolutionKind.NEW_REVISION, candidate, newer, previous,
        "same_physical_object_content_changed")


def _observation_probe(
    expected: ContentVersion, candidate: CandidateSnapshot,
) -> ContentVersion:
    """Keep the expected digest but use the enumerated size for one read.

    LocalObserver rejects a size mismatch before opening the file.  A size
    change is exactly what this resolver must explain, so the probe keeps the
    historical asset/revision and digest while adopting the newly enumerated
    size.  It is a read contract, not a stored content row.
    """
    if expected.size_bytes == candidate.size_bytes:
        return expected
    return ContentVersion(
        expected.asset_id, expected.revision, candidate.size_bytes, expected.sha256,
    )


def observe_candidate(
    candidate: CandidateSnapshot, observer, *, expected_content: ContentVersion,
    run_id: str, check_id: str, budget: ObservationBudget | None = None,
    cancellation=None,
) -> ObservedFile:
    """Observe exactly one candidate and return its explicit handoff facts."""
    if not isinstance(candidate, CandidateSnapshot):
        raise ContractError("Expected a CandidateSnapshot")
    if not isinstance(expected_content, ContentVersion):
        raise ContractError("Expected the content version to probe")
    if not isinstance(observer, object) or not callable(getattr(observer, "observe", None)):
        raise ContractError("Expected a local observer")
    if budget is None:
        budget = ObservationBudget()
    if not isinstance(budget, ObservationBudget):
        raise ContractError("Invalid observation budget")
    probe = _observation_probe(expected_content, candidate)
    result = observer.observe(ObservationRequest(
        probe, candidate.path, run_id, check_id, budget, cancellation,
    ))
    if not isinstance(result, LocalObservation):
        raise ContractError("The observer returned an invalid result")
    # The handoff may only verify the observer's own facts.  A candidate from root
    # generation 2 handed to an observer bound to generation 1 used to be relabelled
    # with the candidate's value, so an old observation became evidence for a new
    # root; refuse the pairing instead of rewriting it.
    if (
        result.evidence.root_revision != candidate.root_revision
        or result.evidence.run_id != run_id
        or result.evidence.check_id != check_id
        or result.evidence.path != candidate.path
    ):
        raise ContractError("The observation does not belong to this candidate")
    content = None
    if result.digest_complete:
        if result.evidence.observed_sha256 is None or result.size_bytes is None:
            raise ContractError("A complete observation lacks its digest or size")
        content = ContentVersion(
            probe.asset_id, probe.revision, result.size_bytes,
            result.evidence.observed_sha256,
        )
    return ObservedFile(
        candidate=candidate, expected_content=probe, content=content,
        digest_complete=result.digest_complete, bytes_read=result.bytes_read,
        observed_size=result.size_bytes, observed_mtime_ns=result.mtime_ns,
        observed_identity=result.identity, observation=result,
    )


def rebind_observation(
    observed: ObservedFile, resolution: ContentResolution,
) -> LocalObservation | None:
    """Bind one real read to the content row the resolution can persist.

    The bytes, byte count, identity and digest are taken from the actual
    observation.  Only the evidence's content reference is rebound: a baseline
    stays ``unknown`` against the unknown historical row, a reused digest stays
    ``hash_match`` against the reused content, and a changed object is stored as
    ``changed`` against its previous revision.  No historical hash is invented.
    """
    if observed.observation is None or not observed.digest_complete:
        return None
    if resolution.requires_attention or resolution.content is None:
        return None
    raw = observed.observation
    if raw.evidence.root_revision != observed.candidate.root_revision:
        raise ContractError("Observation root generation differs from its candidate")
    observed_sha = raw.evidence.observed_sha256
    if observed_sha is None or raw.size_bytes is None:
        return None
    if resolution.kind is ResolutionKind.BASELINE:
        target = resolution.previous_content or observed.expected_content
        if target.sha256 is not None:
            return None
        state, status, reason = LocalState.UNKNOWN, ObservationStatus.BASELINE_OBSERVED, "current_baseline"
    elif (resolution.kind is ResolutionKind.REUSE
          and resolution.content.sha256 == observed_sha
          and resolution.content.size_bytes == raw.size_bytes):
        target = resolution.content
        state, status, reason = LocalState.HASH_MATCH, ObservationStatus.HASH_MATCH, "full_sha256_match"
    else:
        target = resolution.previous_content or observed.expected_content
        if target.sha256 == observed_sha and target.size_bytes == raw.size_bytes:
            state, status, reason = LocalState.HASH_MATCH, ObservationStatus.HASH_MATCH, "full_sha256_match"
        else:
            state, status, reason = LocalState.CHANGED, ObservationStatus.CHANGED, "hash_mismatch"
    evidence = LocalEvidence(
        content=target, path=observed.candidate.path,
        root_revision=raw.evidence.root_revision, run_id=raw.evidence.run_id,
        check_id=raw.evidence.check_id, checked_at=raw.evidence.checked_at,
        state=state, observed_size=raw.size_bytes, observed_sha256=observed_sha,
    )
    return LocalObservation(
        status=status, reason=reason, evidence=evidence, digest_complete=True,
        bytes_read=raw.bytes_read, size_bytes=raw.size_bytes,
        mtime_ns=raw.mtime_ns, identity=raw.identity,
    )


def _exclusions_digest(exclusions: Iterable[ScopeExclusion]) -> str:
    return _digest_text([
        [item.relative_path, item.reason, item.rule, item.related_path,
         None if item.identity is None else [item.identity.device, item.identity.file_index]]
        for item in exclusions
    ])


def _hex(value: object, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ContractError("Invalid " + name)
    return value


def _token_text(value: object, name: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > _RECEIPT_FIELD_LIMIT
            or any(ord(char) < 32 for char in value)):
        raise ContractError("Invalid " + name)
    return value


@dataclass(frozen=True, slots=True)
class ScopeReceipt:
    """Verifiable evidence that one scope snapshot was consumed completely.

    Four facts stay separate instead of collapsing into one full flag:

    `enumeration_state` / `enumeration_complete`
        what the enumerator's own snapshot proved about the whole selection;
    `consumption_complete`
        whether the caller delivered every page of that snapshot, in order,
        with no duplicate, no gap, no foreign page, and no remaining cursor
        (for a sub-batch: exactly the described page-aligned prefix);
    `scope_complete`
        both of the above for a whole-snapshot selection, the only state a plan
        layer may treat as a complete range;
    content verification and plan readiness
        never claimed here: this module never reads file contents, so
        `content_verified` is always False.

    `digest` is a SHA-256 over every fact of the canonical payload, so a
    receipt whose facts were edited after construction is rejected.  It is a
    consistency proof, not a signature: a writer able to rewrite every field
    could recompute it.  Use `signed`/`verify_mac` with a shared key when
    the in-process handoff needs authenticity, and keep the receiver's own
    re-check: a receipt never replaces content hashing.
    """

    schema_version: int
    kind: str
    root_id: str
    root_revision: int
    rules_digest: str
    selection_digest: str
    selection_kind: ScopeSelectionKind
    sub_batch_limit: int | None
    sub_batch_description: str | None
    snapshot_id: str
    snapshot_digest: str
    root_identity: FileIdentity
    entries_in_snapshot: int
    exclusions_in_snapshot: int
    issues_in_snapshot: int
    enumeration_state: ScopeState
    page_count: int
    first_page_index: int
    last_page_index: int
    expected_page_count: int | None
    last_page_has_next: bool
    consumed_entries: int
    consumed_exclusions: int
    consumed_issues: int
    remaining_entries: int
    aborted: bool
    entries_digest: str
    entry_facts: tuple[ScopeEntryFact, ...]
    exclusions_digest: str
    snapshot_verified: bool
    digest: str | None = None
    mac: str | None = None

    def __post_init__(self):
        if self.schema_version != SCOPE_RECEIPT_SCHEMA_VERSION:
            raise ContractError("Unsupported scope receipt schema version")
        if self.kind not in {"project", "external"}:
            raise ContractError("Invalid scope receipt root kind")
        if not isinstance(self.root_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,64}", self.root_id):
            raise ContractError("Invalid scope receipt root identity")
        _integer(self.root_revision, "scope receipt root revision", 0, 2**63 - 1)
        for name in ("rules_digest", "selection_digest", "snapshot_digest",
                     "entries_digest", "exclusions_digest"):
            _hex(getattr(self, name), "scope receipt " + name)
        _token_text(self.snapshot_id, "scope receipt snapshot ID")
        if not isinstance(self.root_identity, FileIdentity):
            raise ContractError("A scope receipt requires a stable root identity")
        if not isinstance(self.selection_kind, ScopeSelectionKind):
            raise ContractError("Invalid scope receipt selection kind")
        if not isinstance(self.enumeration_state, ScopeState):
            raise ContractError("Invalid scope receipt enumeration state")
        for name in ("entries_in_snapshot", "exclusions_in_snapshot", "issues_in_snapshot",
                     "page_count", "first_page_index", "last_page_index",
                     "consumed_entries", "consumed_exclusions", "consumed_issues",
                     "remaining_entries"):
            _integer(getattr(self, name), "scope receipt " + name, 0, 2**63 - 1)
        facts = tuple(self.entry_facts)
        if len(facts) != self.consumed_entries:
            raise ContractError("A scope receipt needs one entry fact per consumed entry")
        for fact in facts:
            if not isinstance(fact, ScopeEntryFact):
                raise ContractError("Invalid scope receipt entry fact")
            if (fact.kind != self.kind or fact.root_id != self.root_id
                    or fact.root_revision != self.root_revision):
                raise ContractError("Scope receipt entry fact differs from its root")
        object.__setattr__(self, "entry_facts", facts)
        if hmac.compare_digest(_facts_digest(facts), self.entries_digest) is False:
            raise ContractError("Scope receipt entry facts do not match entries_digest")
        if self.page_count < 1:
            raise ContractError("A scope receipt requires at least one page")
        if self.last_page_index < self.first_page_index:
            raise ContractError("Invalid scope receipt page span")
        if self.expected_page_count is not None:
            _integer(self.expected_page_count, "scope receipt expected page count", 1, 2**31)
        if self.selection_kind is ScopeSelectionKind.SNAPSHOT:
            if self.sub_batch_limit is not None or self.sub_batch_description is not None:
                raise ContractError("A snapshot receipt carries no sub-batch description")
        else:
            _integer(self.sub_batch_limit, "scope receipt sub-batch limit", 1, 1000000)
            if (not isinstance(self.sub_batch_description, str)
                    or not 1 <= len(self.sub_batch_description) <= 200):
                raise ContractError("A sub-batch receipt needs its own description")
        if self.consumed_entries > self.entries_in_snapshot:
            raise ContractError("A receipt cannot consume more entries than its snapshot")
        if self.remaining_entries != self.entries_in_snapshot - self.consumed_entries:
            raise ContractError("Scope receipt remaining count disagrees with its facts")
        if self.consumed_exclusions != self.exclusions_in_snapshot:
            raise ContractError("Scope receipt exclusion count disagrees with its snapshot")
        if (type(self.aborted) is not bool or type(self.snapshot_verified) is not bool
                or type(self.last_page_has_next) is not bool):
            raise ContractError("Invalid scope receipt flags")
        expected = self._compute_digest()
        if self.digest is None:
            object.__setattr__(self, "digest", expected)
        elif self.digest != expected:
            raise ContractError("Scope receipt digest does not match its facts")
        if self.mac is not None:
            _hex(self.mac, "scope receipt mac")

    @property
    def enumeration_complete(self) -> bool:
        """The snapshot itself enumerated the whole selection without issues."""
        return self.enumeration_state is ScopeState.COMPLETE

    @property
    def consumption_complete(self) -> bool:
        """Every page of the snapshot (or the page-aligned sub-batch) arrived."""
        if self.aborted or self.enumeration_state is not ScopeState.COMPLETE:
            return False
        if self.first_page_index != 0 or self.last_page_index != self.page_count - 1:
            return False
        if self.selection_kind is ScopeSelectionKind.SUB_BATCH:
            limit = self.sub_batch_limit or 0
            return self.consumed_entries == min(limit, self.entries_in_snapshot)
        if self.last_page_has_next:
            # A page delivery that still advertises a next page is not finished.
            return False
        if self.remaining_entries != 0 or self.consumed_entries != self.entries_in_snapshot:
            return False
        if self.expected_page_count is not None and self.expected_page_count != self.page_count:
            return False
        return True

    @property
    def scope_complete(self) -> bool:
        """A whole-snapshot range that was enumerated and consumed completely."""
        return (self.selection_kind is ScopeSelectionKind.SNAPSHOT
                and self.consumption_complete)

    @property
    def content_verified(self) -> bool:
        """Always False: scope enumeration never hashes or decodes file content."""
        return False

    @property
    def receipt_id(self) -> str:
        return "scope-" + (self.digest or "")[:16]

    def _payload(self) -> dict[str, object]:
        return {
            "schema": _RECEIPT_SCHEMA,
            "schema_version": self.schema_version,
            "kind": self.kind,
            "root_id": self.root_id,
            "root_revision": self.root_revision,
            "rules_digest": self.rules_digest,
            "selection_digest": self.selection_digest,
            "selection_kind": self.selection_kind.value,
            "sub_batch_limit": self.sub_batch_limit,
            "sub_batch_description": self.sub_batch_description,
            "snapshot_id": self.snapshot_id,
            "snapshot_digest": self.snapshot_digest,
            "root_identity": asdict(self.root_identity),
            "entries_in_snapshot": self.entries_in_snapshot,
            "exclusions_in_snapshot": self.exclusions_in_snapshot,
            "issues_in_snapshot": self.issues_in_snapshot,
            "enumeration_state": self.enumeration_state.value,
            "page_count": self.page_count,
            "first_page_index": self.first_page_index,
            "last_page_index": self.last_page_index,
            "expected_page_count": self.expected_page_count,
            "last_page_has_next": self.last_page_has_next,
            "consumed_entries": self.consumed_entries,
            "consumed_exclusions": self.consumed_exclusions,
            "consumed_issues": self.consumed_issues,
            "remaining_entries": self.remaining_entries,
            "aborted": self.aborted,
            "entries_digest": self.entries_digest,
            "entry_facts": [fact.to_dict() for fact in self.entry_facts],
            "exclusions_digest": self.exclusions_digest,
            "snapshot_verified": self.snapshot_verified,
            "enumeration_complete": self.enumeration_complete,
            "consumption_complete": self.consumption_complete,
            "scope_complete": self.scope_complete,
        }

    def _compute_digest(self) -> str:
        return _digest_text(self._payload())

    def _mac_message(self) -> bytes:
        payload = self._payload()
        payload["digest"] = self.digest
        return canonical_json(payload).encode("utf-8")

    def to_json(self) -> str:
        payload = self._payload()
        payload["digest"] = self.digest
        if self.mac is not None:
            payload["mac"] = self.mac
        return canonical_json(payload)

    @classmethod
    def from_json(cls, text: str) -> ScopeReceipt:
        payload = json.loads(normalize_json(text, object_only=True))
        schema = payload.pop("schema", None)
        if schema == _RECEIPT_SCHEMA_V1 or payload.get("schema_version") == 1:
            raise ContractError(
                "Scope receipt v1 is not supported; enumerate and consume the scope again")
        if schema != _RECEIPT_SCHEMA:
            raise ContractError("Unexpected scope receipt schema")
        raw_facts = payload.get("entry_facts")
        if not isinstance(raw_facts, list):
            raise ContractError("A version 2 scope receipt needs its entry facts")
        payload["entry_facts"] = tuple(
            ScopeEntryFact.from_dict(item) for item in raw_facts)
        digest = payload.pop("digest", None)
        mac = payload.pop("mac", None)
        derived = ("enumeration_complete", "consumption_complete", "scope_complete")
        declared = {name: payload.pop(name) for name in derived if name in payload}
        if set(payload) != _RECEIPT_FIELDS or set(declared) != set(derived):
            raise ContractError("Unexpected scope receipt fields")
        try:
            payload["root_identity"] = FileIdentity(**payload["root_identity"])
            payload["selection_kind"] = ScopeSelectionKind(payload["selection_kind"])
            payload["enumeration_state"] = ScopeState(payload["enumeration_state"])
            receipt = cls(**payload, digest=digest, mac=mac)
        except ContractError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("Invalid scope receipt") from exc
        if any(declared[name] is not getattr(receipt, name) for name in derived):
            raise ContractError("Scope receipt completion flags disagree with its facts")
        return receipt

    def verify(self) -> None:
        """Raise ContractError unless the stored facts still match the digest."""
        if self.digest != self._compute_digest():
            raise ContractError("Scope receipt digest does not match its facts")

    def signed(self, key: bytes) -> ScopeReceipt:
        """Return a copy carrying an HMAC over its facts and digest."""
        if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
            raise ContractError("A receipt key needs at least 16 bytes")
        mac = hmac.digest(bytes(key), self._mac_message(), "sha256").hex()
        return replace(self, mac=mac)

    def verify_mac(self, key: bytes) -> None:
        if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
            raise ContractError("A receipt key needs at least 16 bytes")
        if self.mac is None:
            raise ContractError("Scope receipt is not signed")
        expected = hmac.digest(bytes(key), self._mac_message(), "sha256").hex()
        if not hmac.compare_digest(self.mac, expected):
            raise ContractError("Scope receipt signature does not match its key")

    def matches_entries(self, entries: Iterable[ArchiveEntry]) -> bool:
        """True when these entries are exactly the consumed set, in order.

        Physical facts are compared explicitly instead of relying on
        ``ArchiveEntry`` dataclass equality: ``physical_identity`` is excluded
        from that equality by design, so a path-and-size comparison alone would
        accept another device's file at the same name.  A content digest that
        the receipt has not bound is not treated as a wildcard: an entry whose
        SHA-256 is known cannot match an unbound scope fact.  Plan preparation
        binds the observed content explicitly before persisting the receipt.
        """
        items = tuple(entries)
        if not self.matches_physical_entries(items):
            return False
        for entry, fact in zip(items, self.entry_facts):
            content = entry.content
            if fact.content_sha256 is None:
                # Scope enumeration has no digest; the plan must bind it.  A
                # forged digest on a plan entry is not accepted by a scope-only
                # receipt.
                if content.sha256 is not None:
                    return False
            elif (content.asset_id != fact.content_asset_id
                  or content.revision != fact.content_revision
                  or content.sha256 != fact.content_sha256):
                return False
        return hmac.compare_digest(_facts_digest(self.entry_facts), self.entries_digest)

    def matches_physical_entries(self, entries: Iterable[ArchiveEntry]) -> bool:
        """Compare path/root/physical facts without requiring a content digest."""
        items = tuple(entries)
        if len(items) != len(self.entry_facts):
            return False
        for entry, fact in zip(items, self.entry_facts):
            path = entry.path
            if not isinstance(path, PathRef):
                return False
            if (path.kind != fact.kind
                    or path.root_id.casefold() != fact.root_id.casefold()
                    or path.relative_path != fact.relative_path):
                return False
            if entry.physical_identity != fact.physical_identity:
                return False
            if entry.content is None or entry.content.size_bytes != fact.size_bytes:
                return False
        return True

    def bind_content(self, entries: Iterable[ArchiveEntry]) -> ScopeReceipt:
        """Return a receipt whose entry facts carry the observed content facts.

        The physical comparison runs first, so content can never be rebound to
        another path, device or size.  Binding is one-way: a receipt that already
        names a digest refuses to replace it with a different one.
        """
        items = tuple(entries)
        if not self.matches_physical_entries(items):
            raise ContractError("ScopeReceipt physical entries differ from the plan")
        if any(fact.content_sha256 is not None for fact in self.entry_facts):
            raise ContractError("ScopeReceipt already binds content facts")
        facts = tuple(
            replace(
                fact, content_asset_id=entry.content.asset_id,
                content_revision=entry.content.revision,
                content_sha256=entry.content.sha256,
            )
            for entry, fact in zip(items, self.entry_facts)
        )
        return replace(
            self, entry_facts=facts, entries_digest=_facts_digest(facts),
            digest=None, mac=None,
        )


_RECEIPT_FIELDS = frozenset(
    name for name in ScopeReceipt.__dataclass_fields__ if name not in {"digest", "mac"})


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
class _DirectorySnapshot:
    """One directory reached by the scope.

    `names=None` means the directory was only reached as an ancestor of a
    selected path: its identity and type still guard path safety, but it was
    never enumerated, so no membership snapshot exists for it.
    """

    relative_path: str
    state: _NodeState
    names: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _Snapshot:
    snapshot_id: str | None
    entries: tuple[_FileSnapshot, ...]
    exclusions: tuple[ScopeExclusion, ...]
    issues: tuple[ScopeIssue, ...]
    state: ScopeState
    root_state: _NodeState | None
    ancestors: tuple[tuple[Path, _NodeState], ...]
    directories: tuple[_DirectorySnapshot, ...]
    aliases: tuple[ScopeFileAlias, ...]
    selection_digest: str
    digest: str
    cursor: _CursorState
    cursor_key: bytes


@dataclass(frozen=True, slots=True)
class _FileOperations:
    lstat: Callable[[Path], os.stat_result]
    scandir: Callable[[Path], ContextManager[Iterator[object]]]


_DEFAULT_IO = _FileOperations(lambda path: path.lstat(), os.scandir)


class _IdentityIndex:
    """O(1) physical-identity index with a measurable comparison counter.

    The counter grows once per file candidate, so a test can prove linear
    growth instead of relying on wall-clock noise.
    """

    __slots__ = ("_accepted", "comparisons")

    def __init__(self):
        self._accepted: dict[FileIdentity, _FileSnapshot] = {}
        self.comparisons = 0

    def find(self, identity: FileIdentity) -> _FileSnapshot | None:
        self.comparisons += 1
        return self._accepted.get(identity)

    def add(self, identity: FileIdentity, item: _FileSnapshot) -> None:
        self._accepted[identity] = item


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
        ScopeIssueCode.NAME_INDEX_BUDGET_EXHAUSTED: "The raw-name index budget was exhausted.",
        ScopeIssueCode.CANCELLED: "Enumeration was cancelled; this is not a complete scope.",
    }
    return ScopeIssue(code, path, messages[code])


class _Halt(Exception):
    def __init__(self, issue: ScopeIssue, state: ScopeState = ScopeState.PARTIAL):
        self.issue = issue
        self.state = state


class _WorkGuard:
    """Cancellation plus a work budget for one bounded scope operation.

    One guard is opened per enumeration or per page delivery, so the time
    budget only charges work performed inside that operation.  A caller may
    hold a cursor for hours without spending the scope's execution budget,
    which is exactly the difference between "this scan is too slow" and "the
    user walked away".

    `checkpoint` runs before and after every filesystem call, at every directory
    entry, and once more before a page is returned.  A single blocking
    operating-system call cannot be interrupted from inside this module; what
    is guaranteed is that no further call starts and no result is returned once
    a cancellation arrives or the budget is spent.
    """

    __slots__ = ("checks", "_cancel", "_clock", "_max_seconds", "_started")

    def __init__(self, clock, cancellation, max_seconds):
        self._clock = clock
        self._cancel = cancellation
        self._max_seconds = max_seconds
        self.checks = 0
        self._started = self._now()

    def _now(self):
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ContractError("Scope clock returned an invalid value")
        return now

    def checkpoint(self) -> None:
        self.checks += 1
        if self._cancel is not None and self._cancel():
            raise _Halt(_issue(ScopeIssueCode.CANCELLED, "."), ScopeState.CANCELLED)
        if self._now() - self._started > self._max_seconds:
            raise _Halt(_issue(ScopeIssueCode.TIME_BUDGET_EXHAUSTED, "."),
                        ScopeState.BUDGET_EXHAUSTED)


@dataclass(slots=True)
class _DirectoryNames:
    """Raw child names of one directory with the identity that produced them."""

    identity: FileIdentity
    state: _NodeState
    names: frozenset[str]
    rescans: int


class _RawNameIndex:
    """One bounded directory pass per parent, revalidated before every use.

    Explicit selections need the exact spelling of each path segment: a name
    that differs only by case or Unicode composition shares one display key but
    is not the same directory entry.  Asking the filesystem once per selection
    (the previous behaviour) made the cost grow with selections times directory
    size, because every selection re-read the same parent.

    This index reads each parent once, keeps the raw child names, and records
    the directory identity and node state observed immediately before and after
    that read.  Every lookup revalidates the record with one lstat:

    * a different identity at the same path drops the record and reads again -
      the cached names belong to a directory that is no longer there;
    * a changed state (size or modification time) reads the directory again and
      keeps the new names only when the read is confirmed by a matching state;
    * a read that is not confirmed by the surrounding states is never kept, and
      repeated instability stops the scope with `directory_changed` instead of
      pretending the raw spelling is settled.

    The index lives for exactly one enumeration, so it never carries a name set
    across snapshots.  Directories the eligibility policy excludes are never
    read: an excluded output or credential directory must not be opened just to
    answer a spelling question about a path that will be excluded anyway.
    """

    __slots__ = ("_allowed", "_by_identity", "_entries", "_guard", "_io", "_root",
                 "directory_reads", "names_read")

    def __init__(self, io, root, guard, allowed):
        self._io = io
        self._root = root
        self._guard = guard
        self._allowed = allowed
        self._entries: dict[str, _DirectoryNames] = {}
        # Two spellings of one directory ("gallery" and "GALLERY" on a
        # case-insensitive volume) must not be read twice.
        self._by_identity: dict[FileIdentity, _DirectoryNames] = {}
        self.directory_reads = 0
        self.names_read = 0

    def index(self, relative: str) -> frozenset[str] | None:
        """Raw child names of one directory, or None when it cannot be read."""
        if relative != "." and not self._allowed(relative):
            return None
        record = self._entries.get(relative)
        if record is None:
            return self._read(relative, 0)
        current = self._observe(relative)
        if (current is not None and current.identity == record.identity
                and (current.size_bytes, current.mtime_ns)
                == (record.state.size_bytes, record.state.mtime_ns)):
            return record.names
        self._entries.pop(relative, None)
        if current is None:
            return None
        if record.rescans >= _MAX_NAME_INDEX_RESCANS:
            raise _Halt(_issue(ScopeIssueCode.DIRECTORY_CHANGED, relative))
        return self._read(relative, record.rescans + 1)

    def _path(self, relative: str) -> Path:
        return (self._root if relative == "."
                else self._root.joinpath(*relative.split("/")))

    def _observe(self, relative: str) -> _NodeState | None:
        self._guard.checkpoint()
        try:
            info = self._io.lstat(self._path(relative))
        except OSError:
            return None
        finally:
            self._guard.checkpoint()
        if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            return None
        return _node_state(info)

    def _read(self, relative: str, rescans: int) -> frozenset[str] | None:
        if relative not in self._entries and len(self._entries) >= _MAX_INDEXED_DIRECTORIES:
            raise _Halt(_issue(ScopeIssueCode.NAME_INDEX_BUDGET_EXHAUSTED, relative),
                        ScopeState.BUDGET_EXHAUSTED)
        before = self._observe(relative)
        if before is None:
            return None
        known = self._by_identity.get(before.identity)
        if (known is not None and (known.state.size_bytes, known.state.mtime_ns)
                == (before.size_bytes, before.mtime_ns)):
            # The same physical directory was already read under another
            # spelling; its names cannot differ.
            self._entries[relative] = _DirectoryNames(before.identity, before,
                                                      known.names, rescans)
            return known.names
        target = self._path(relative)
        names: set[str] = set()
        try:
            with self._io.scandir(target) as children:
                for child in children:
                    self._guard.checkpoint()
                    name = child.name if hasattr(child, "name") else child
                    if not isinstance(name, str) or not name or len(name) > 255:
                        raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, relative))
                    names.add(name)
                    self.names_read += 1
                    if len(names) > _MAX_INDEXED_NAMES:
                        raise _Halt(_issue(ScopeIssueCode.NAME_INDEX_BUDGET_EXHAUSTED,
                                           relative), ScopeState.BUDGET_EXHAUSTED)
        except _Halt:
            raise
        except (OSError, ValueError, TypeError):
            self._entries.pop(relative, None)
            return None
        after = self._observe(relative)
        if (after is None or after.identity != before.identity
                or (after.size_bytes, after.mtime_ns)
                != (before.size_bytes, before.mtime_ns)):
            # The directory changed while it was read: a half-observed name set
            # is never a usable answer.
            self._entries.pop(relative, None)
            if rescans >= _MAX_NAME_INDEX_RESCANS:
                raise _Halt(_issue(ScopeIssueCode.DIRECTORY_CHANGED, relative))
            return self._read(relative, rescans + 1)
        self.directory_reads += 1
        record = _DirectoryNames(before.identity, before, frozenset(names), rescans)
        self._entries[relative] = record
        self._by_identity[before.identity] = record
        return record.names


@dataclass(slots=True)
class _StabilitySession:
    """Between-page verification state with one stated total cost.

    For a delivery of P pages over D recorded directories this session visits
    each directory about once: every page except the last verifies a rotating
    slice of `ceil(D / P)` directories, and the page that can complete the
    receipt verifies all of them.  Total directory checks are therefore about
    `2 * D` for the whole delivery instead of `D * P`.

    `observed` holds the last state whose child names were confirmed identical
    to the snapshot.  A directory whose modification time moved but whose
    membership did not is not re-read on every later page; repeated instability
    is capped by `_MAX_MEMBERSHIP_RESCANS`, after which the scope refuses to
    certify the range.
    """

    snapshot_digest: str
    rotation: int = 0
    sweeps: int = 0
    checks: int = 0
    observed: dict[str, _NodeState] = field(default_factory=dict)
    rescans: dict[str, int] = field(default_factory=dict)


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
        self._stability: _StabilitySession | None = None
        self._identity_comparisons = 0
        self._rules_digest = hashlib.sha256(canonical_json({
            "policy": self._policy._digest_data(),
            "selections": request._selection_data(),
        }).encode("utf-8")).hexdigest()
        self._selection_digest = hashlib.sha256(canonical_json({
            "schema_version": SCOPE_SCHEMA_VERSION,
            "kind": request.kind,
            "root_id": request.root_id,
            "root_revision": request.root_revision,
            "selection_count": len(request.prefixes) + len(request.files),
            "selections": request._selection_data(),
        }).encode("utf-8")).hexdigest()

    @property
    def identity_comparisons(self) -> int:
        """Physical-identity comparisons performed by the last enumeration."""
        return self._identity_comparisons

    def page(self, cursor: str | None = None, *, cancellation=None) -> ScopePage:
        if cancellation is not None and not callable(getattr(cancellation, "is_set", None)):
            raise ContractError("Cancellation must provide is_set()")
        cancelled = None if cancellation is None else cancellation.is_set
        # One guard per bounded operation: an enumeration for the first page, a
        # validity check for every later page.  Time spent waiting for the next
        # request is not part of either budget.
        guard = _WorkGuard(self._monotonic, cancelled, self._policy.max_seconds)
        if cursor is None:
            if self._snapshot is not None:
                raise ContractError("The first-page cursor cannot restart a live enumerator")
            self._snapshot = self._enumerate(cancelled, guard)
            if self._snapshot.root_state is None:
                return self._empty_result(self._snapshot, cancellation)
        snapshot = self._snapshot
        if snapshot is None:
            raise ContractError("Invalid, foreign, or altered scope cursor")
        start = 0 if cursor is None else self._decode_cursor(cursor, snapshot)
        current_issues = list(snapshot.issues)
        state = snapshot.state
        # A snapshot that already stopped (partial, cancelled, over budget) has
        # no continuous range to verify: its pages only carry that verdict and
        # deliver no entries.
        deliverable = state in (ScopeState.COMPLETE, ScopeState.COMPLETE_WITH_ISSUES)
        if cancelled is not None and cancelled():
            self._note(current_issues, _issue(ScopeIssueCode.CANCELLED, "."))
            state = ScopeState.CANCELLED
        elif deliverable:
            try:
                self._check_snapshot_root(snapshot, guard, start)
            except _Halt as halt:
                self._note(current_issues, halt.issue)
                state = self._halt_state(halt)
        if state is not snapshot.state:
            return self._make_page(snapshot, (), None, tuple(current_issues), state, start=start,
                                   has_next=False)
        if deliverable:
            try:
                # Last checkpoint before any result leaves this module: a
                # cancellation or an exhausted budget during the work above must
                # never be reported as a complete page.
                guard.checkpoint()
            except _Halt as halt:
                self._note(current_issues, halt.issue)
                return self._make_page(snapshot, (), None, tuple(current_issues),
                                       self._halt_state(halt), start=start, has_next=False)
        selected = snapshot.entries[start:start + self._request.page_size]
        end = start + len(selected)
        next_cursor = self._encode_cursor(snapshot, end) if end < len(snapshot.entries) else None
        facts = tuple(
            ScopeEntryFact(
                self._request.kind, self._request.root_id, self._request.root_revision,
                item.entry.path.relative_path, item.identity.device,
                item.identity.file_index, item.entry.content.size_bytes, item.mtime_ns,
                item.entry.content.asset_id, item.entry.content.revision,
                item.entry.content.sha256,
            )
            for item in selected
        )
        result = self._make_page(snapshot, tuple(item.entry for item in selected),
                                 next_cursor, tuple(current_issues), state, start=start,
                                 has_next=next_cursor is not None, facts=facts)
        self._snapshot = replace(
            snapshot, cursor=_CursorState(end if next_cursor else 0, next_cursor is not None),
            cursor_key=snapshot.cursor_key,
        )
        return result

    @staticmethod
    def _halt_state(halt: _Halt) -> ScopeState:
        """A cancelled operation stays cancelled; every other stop is partial."""
        return ScopeState.CANCELLED if halt.state is ScopeState.CANCELLED else ScopeState.PARTIAL

    @staticmethod
    def _note(issues: list[ScopeIssue], issue: ScopeIssue) -> None:
        """Record one stop reason without repeating an identical verdict."""
        if issues and (issues[-1].code, issues[-1].relative_path) == (issue.code,
                                                                     issue.relative_path):
            return
        issues.append(issue)

    def _make_page(self, snapshot: _Snapshot, entries, cursor, issues, state, *, start,
                   has_next, facts=()):
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
            aliases=snapshot.aliases, selection_digest=snapshot.selection_digest,
            entry_facts=tuple(facts),
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
            "boundary_path": previous.relative_path,
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
            canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
            if not hmac.compare_digest(canonical, cursor):
                raise ValueError("Cursor encoding is not canonical")
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
                "boundary_path": previous.relative_path,
            }
            if (payload != expected or not 0 < payload["next_ordinal"] < len(snapshot.entries)
                    or not snapshot.cursor.valid
                    or payload["next_ordinal"] != snapshot.cursor.ordinal):
                raise ValueError("Cursor binding")
            return payload["next_ordinal"]
        except (ValueError, TypeError, KeyError, IndexError, UnicodeError,
                binascii.Error, ContractError) as exc:
            raise ContractError("Invalid, foreign, or altered scope cursor") from exc

    # --- one eligibility policy for recursion, explicit files and prefixes ---

    def _configured_output_root(self, relative: str) -> str | None:
        if not self._policy.output_roots:
            return None
        key = _path_key(relative)
        for configured in self._policy.output_roots:
            if _path_key(configured) == key:
                return configured
        return None

    def _directory_eligibility(self, relative: str, name: str) -> ScopeEligibility:
        rule = _rule_for(name, self._policy.output_root_rules)
        if rule is not None:
            return ScopeEligibility(ScopeVerdict.EXCLUDE,
                                    ScopeExclusionReason.EXCLUDED_DIRECTORY, rule)
        configured = self._configured_output_root(relative)
        if configured is not None:
            return ScopeEligibility(ScopeVerdict.EXCLUDE,
                                    ScopeExclusionReason.EXCLUDED_DIRECTORY, configured)
        return ScopeEligibility(ScopeVerdict.ACCEPT)

    def _ancestor_eligibility(self, relative: str) -> ScopeEligibility:
        prefix = ""
        for piece in relative.split("/")[:-1]:
            prefix = piece if not prefix else prefix + "/" + piece
            verdict = self._directory_eligibility(prefix, piece)
            if not verdict.accepted:
                return ScopeEligibility(
                    ScopeVerdict.EXCLUDE, ScopeExclusionReason.EXCLUDED_ANCESTOR_DIRECTORY,
                    verdict.rule, prefix)
        return ScopeEligibility(ScopeVerdict.ACCEPT)

    def _file_eligibility(self, relative: str) -> ScopeEligibility:
        verdict = self._ancestor_eligibility(relative)
        if not verdict.accepted:
            return verdict
        rule = _rule_for(relative.rsplit("/", 1)[-1], self._policy.file_rules)
        if rule is not None:
            return ScopeEligibility(ScopeVerdict.EXCLUDE, ScopeExclusionReason.EXCLUDED_FILE, rule)
        return ScopeEligibility(ScopeVerdict.ACCEPT)

    def _enumerate(self, cancelled, guard: _WorkGuard) -> _Snapshot | None:
        entries: list[_FileSnapshot] = []
        exclusions: list[ScopeExclusion] = []
        issues: list[ScopeIssue] = []
        aliases: list[ScopeFileAlias] = []
        identities = _IdentityIndex()
        directory_identities: dict[FileIdentity, str] = {}
        directories: dict[str, _NodeState] = {}
        directory_names: dict[str, tuple[str, ...]] = {}
        used_entries = 0
        used_directories = 0
        # One raw-name index for this enumeration only: an explicit selection
        # never re-reads a parent directory it already resolved, and no name set
        # survives into another snapshot.
        name_index = _RawNameIndex(self._io, self._root, guard,
                                   self._indexable_directory)

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
            if related == path:
                # A duplicate of one relative path has no other path to point at.
                related = None
            exclusions.append(ScopeExclusion(path, ScopeExclusionReason(reason).value, rule,
                                             related, identity))

        def add_verdict(path, verdict, identity=None):
            add_exclusion(path, verdict.reason, verdict.rule, verdict.related_path, identity)

        def consume_entry(path):
            nonlocal used_entries
            if used_entries >= self._policy.max_entries:
                raise _Halt(_issue(ScopeIssueCode.ENTRY_BUDGET_EXHAUSTED, path),
                            ScopeState.BUDGET_EXHAUSTED)
            used_entries += 1

        def lstat(path, relative, *, root=False, guarded=True):
            if guarded:
                guard.checkpoint()
            try:
                info = self._io.lstat(path)
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
            finally:
                # A cancellation or spent budget that arrived during the call
                # must stop the next call, not be reported as a healthy path.
                if guarded:
                    guard.checkpoint()
            return info

        def root_chain(*, guarded=True):
            # `guarded=False` is used by exactly one caller: the handler that
            # must still report which root an aborted snapshot was bound to.
            # That read is bounded by the path depth, scans no directory, and
            # certifies nothing beyond the identity of the root itself.
            chain = []
            for directory in reversed((self._root, *self._root.parents)):
                info = lstat(directory, ".", root=True, guarded=guarded)
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

        def record_directory(relative, state, names):
            directories.setdefault(relative, state)
            if names is not None:
                directory_names[relative] = names

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
                record_directory(child_relative, state, None)

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
            directory = (self._root if relative == "."
                         else self._root.joinpath(*relative.split("/")))
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
            previous = directory_identities.get(before.identity)
            if previous is not None and previous != relative:
                add_exclusion(relative, ScopeExclusionReason.DUPLICATE_DIRECTORY_IDENTITY,
                              related=previous, identity=before.identity)
                return ()
            directory_identities[before.identity] = relative
            record_directory(relative, before, None)
            names = []
            try:
                with self._io.scandir(directory) as children:
                    for child in children:
                        guard.checkpoint()
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
            ordered = tuple(sorted(names, key=lambda value: (_path_key(value), value)))
            directory_names[relative] = ordered
            return ordered

        def selection_relative(raw, index):
            if isinstance(raw, PathRef):
                if (raw.kind != self._request.kind
                        or raw.root_id.casefold() != self._request.root_id.casefold()):
                    add_issue(ScopeIssueCode.ROOT_BINDING_MISMATCH,
                              "<selection-%04d>" % index)
                    return None
                relative = raw.relative_path
            elif not raw or len(raw) > 2048 or any(ord(char) < 32 for char in raw):
                add_issue(ScopeIssueCode.INVALID_PATH, "<selection-%04d>" % index)
                return None
            else:
                relative = raw.replace("\\", "/")
            if relative == ".":
                return "."
            path_ref = scoped_path(relative)
            return None if path_ref is None else path_ref.relative_path

        def prepare_prefix(relative):
            """Explicit prefix route: the same eligibility policy as recursion."""
            if relative == ".":
                return True
            verdict = self._ancestor_eligibility(relative)
            if not verdict.accepted:
                add_verdict(relative, verdict)
                return False
            check_ancestors(relative)
            path_ref = scoped_path(relative)
            if path_ref is None:
                return False
            target = self._root.joinpath(*relative.split("/"))
            info = lstat(target, relative)
            if info is None:
                return False
            if _is_reparse(info):
                add_issue(ScopeIssueCode.REPARSE_POINT, relative)
                return False
            if not stat.S_ISDIR(info.st_mode):
                add_issue(ScopeIssueCode.PREFIX_NOT_DIRECTORY, relative)
                return False
            state = _node_state(info)
            if state is None:
                raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, relative))
            verdict = self._directory_eligibility(relative, relative.rsplit("/", 1)[-1])
            if not verdict.accepted:
                add_verdict(relative, verdict, state.identity)
                return False
            # Do not reject a second spelling here: the raw-directory spelling
            # tie-break runs before traversal and must still see both.  A real
            # duplicate identity is excluded by read_directory below.
            record_directory(relative, state, None)
            return True

        try:
            guard.checkpoint()
            ancestors = root_chain()
            selections: list[tuple[str, str, bool]] = []
            prefixes_seen: set[str] = set()
            files_seen: set[str] = set()
            prefix_candidates: list[str] = []
            file_candidates: list[str] = []
            validated: list[tuple[bool, str]] = []
            for index, (is_prefix, raw) in enumerate(
                    [(True, value) for value in self._request.prefixes]
                    + [(False, value) for value in self._request.files]):
                guard.checkpoint()
                relative = selection_relative(raw, index)
                if relative is not None:
                    validated.append((is_prefix, relative))
            # An explicit selection names a real directory entry: normalise every
            # spelling before dedup, eligibility and identity are computed, so
            # "gallery/a.bin", "gallery/A.bin" and "gallery/画a.bin" cannot become
            # different assets.  All selections share one listing per parent.
            resolved_spellings = self._resolve_spellings(
                [relative for _is_prefix, relative in validated], name_index)
            for (is_prefix, _raw_relative), relative in zip(validated, resolved_spellings):
                if relative == "." and not is_prefix:
                    add_issue(ScopeIssueCode.INVALID_PATH, relative)
                    continue
                seen = prefixes_seen if is_prefix else files_seen
                if relative in seen:
                    # The duplicate is this same relative path; there is no
                    # second path to name as its related location.
                    add_exclusion(relative, ScopeExclusionReason.DUPLICATE_SELECTION)
                    continue
                seen.add(relative)
                if is_prefix:
                    prefix_candidates.append(relative)
                else:
                    file_candidates.append(relative)

            def covers(prefix: str, relative: str) -> bool:
                # Exact relative paths only.  A normalised or Unicode-collapsed
                # comparison would fold NFC/NFD look-alikes that can be distinct
                # physical files; case aliases are settled by directory identity.
                if prefix == ".":
                    return True
                return relative == prefix or relative.startswith(prefix + "/")

            # Outer prefixes first: a parent prefix has already collected every
            # descendant, so a child prefix or a file below it is one redundant
            # selection, not a second traversal that doubles the budgets.
            prefix_candidates.sort(key=lambda value: (
                value.count("/"), _path_key(value), value))
            kept_prefixes: list[str] = []
            for relative in prefix_candidates:
                covering = next((prefix for prefix in kept_prefixes
                                 if covers(prefix, relative)), None)
                if covering is not None:
                    add_exclusion(relative, ScopeExclusionReason.DUPLICATE_SELECTION,
                                  related=covering)
                    continue
                if not prepare_prefix(relative):
                    continue
                consume_entry("<selection>")
                kept_prefixes.append(relative)
                selections.append((_path_key(relative), relative, True))
            for relative in file_candidates:
                covering = next((prefix for prefix in kept_prefixes
                                 if covers(prefix, relative)), None)
                if covering is not None:
                    add_exclusion(relative, ScopeExclusionReason.DUPLICATE_SELECTION,
                                  related=covering)
                    continue
                consume_entry("<selection>")
                selections.append((_path_key(relative), relative, False))
            # Prefer the spelling that actually appears in the directory when
            # two selections name one case-insensitive entry.  This keeps the
            # real path and lets the alternate spelling be an explicit
            # duplicate exclusion instead of becoming a second restorable path.
            #
            # The spelling flag only breaks a tie inside one display key, so it
            # is resolved only for keys that actually collide: grouping the
            # colliding selections by parent directory turns "one directory scan
            # per selection" into one bounded scan per distinct parent.
            collisions: dict[str, list[str]] = {}
            for key, relative, _is_prefix in selections:
                collisions.setdefault(key, []).append(relative)
            wanted = [relative for group in collisions.values() if len(group) > 1
                      for relative in group]
            present = self._raw_entry_presence(wanted, name_index) if wanted else {}
            selections.sort(key=lambda value: (
                value[0], not present.get(value[1], False), value[1], not value[2],
            ))
            pending_files = deque(
                (relative, key) for key, relative, is_prefix in selections if not is_prefix)
            pending_directories = deque(
                (relative, key) for key, relative, is_prefix in selections if is_prefix)
            directory_queue: deque[str] = deque()
            while pending_files or pending_directories or directory_queue:
                guard.checkpoint()
                if pending_files:
                    relative, _key = pending_files.popleft()
                    verdict = self._file_eligibility(relative)
                    if not verdict.accepted:
                        add_verdict(relative, verdict)
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
                    self._capture_file(relative, path_ref, info, entries, add_issue,
                                       add_exclusion, identities, aliases,
                                       ScopeExclusionReason.DUPLICATE_SELECTION, name_index)
                    continue
                if not directory_queue:
                    relative, _key = pending_directories.popleft()
                    directory_queue.append(relative)
                relative = directory_queue.pop()
                # Discovery and traversal are distinct: a queued directory still
                # needs read_directory, without scheduling a duplicate traversal.
                if relative != ".":
                    verdict = self._ancestor_eligibility(relative)
                    if not verdict.accepted:
                        add_verdict(relative, verdict)
                        continue
                    verdict = self._directory_eligibility(relative,
                                                          relative.rsplit("/", 1)[-1])
                    if not verdict.accepted:
                        add_verdict(relative, verdict)
                        continue
                    check_ancestors(relative)
                children = read_directory(relative)
                child_directories = []
                for child_relative in children:
                    guard.checkpoint()
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
                    if stat.S_ISDIR(info.st_mode):
                        verdict = self._directory_eligibility(
                            child_relative, child_relative.rsplit("/", 1)[-1])
                        if not verdict.accepted:
                            add_verdict(child_relative, verdict)
                        else:
                            child_directories.append(child_relative)
                        continue
                    verdict = self._file_eligibility(child_relative)
                    if not verdict.accepted:
                        add_verdict(child_relative, verdict)
                        continue
                    self._capture_file(child_relative, path_ref, info, entries, add_issue,
                                       add_exclusion, identities, aliases,
                                       ScopeExclusionReason.DUPLICATE_CANDIDATE, name_index)
                directory_queue.extend(reversed(child_directories))
            for item in entries:
                guard.checkpoint()
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
            # Ancestors are safety identity, not scope membership.  An unrelated
            # file appearing next to the root (or in a temporary parent) changes
            # that parent's size/mtime but does not replace the root path.  Only
            # a different physical identity along the chain is a replacement.
            if ([state.identity for _, state in final_chain]
                    != [state.identity for _, state in ancestors]):
                raise _Halt(_issue(ScopeIssueCode.ROOT_CHANGED, "."))
            # The root's own membership is scope-relevant and keeps its full
            # state check; only ancestors above it are identity-only.
            if final_chain[-1][1] != ancestors[-1][1]:
                raise _Halt(_issue(ScopeIssueCode.ROOT_CHANGED, "."))
            for relative, before in directories.items():
                guard.checkpoint()
                target = (self._root if relative == "."
                          else self._root.joinpath(*relative.split("/")))
                info = lstat(target, relative, root=relative == ".")
                current = None if info is None else _node_state(info)
                if (current != before or _is_reparse(info)
                        or not stat.S_ISDIR(info.st_mode)):
                    raise _Halt(_issue(ScopeIssueCode.DIRECTORY_CHANGED, relative))
            guard.checkpoint()
            state = ScopeState.COMPLETE_WITH_ISSUES if issues else ScopeState.COMPLETE
        except _Halt as halt:
            self._identity_comparisons = identities.comparisons
            add_issue(halt.issue.code, halt.issue.relative_path, terminal=True)
            state = halt.state
            try:
                ancestors = root_chain(guarded=False)
                root_state = ancestors[-1][1]
            except _Halt as root_halt:
                add_issue(root_halt.issue.code, root_halt.issue.relative_path, terminal=True)
                return _Snapshot(
                    snapshot_id=None, entries=(), exclusions=tuple(exclusions),
                    issues=tuple(issues), state=ScopeState.PARTIAL, root_state=None,
                    ancestors=(), directories=(), aliases=(),
                    selection_digest=self._selection_digest, digest="",
                    cursor=_CursorState(0, False), cursor_key=self._key,
                )
        self._identity_comparisons = identities.comparisons
        entries.sort(key=lambda item: (
            _path_key(item.entry.path.relative_path), item.entry.path.relative_path))
        exclusions.sort(key=lambda item: (_path_key(item.relative_path),
                                          item.relative_path, item.reason))
        issues.sort(key=lambda item: (_path_key(item.relative_path),
                                      item.relative_path, item.code.value))
        alias_items = tuple(sorted(aliases, key=lambda item: (
            _path_key(item.relative_path), item.relative_path)))
        snapshot_id = secrets.token_hex(16)
        if state is not ScopeState.COMPLETE:
            entries.clear()
            exclusions.clear()
            alias_items = ()
        root_state = ancestors[-1][1]
        directory_items = tuple(
            _DirectorySnapshot(relative, state_item, directory_names.get(relative))
            for relative, state_item in sorted(directories.items()))
        digest = hashlib.sha256(canonical_json({
            "schema_version": SCOPE_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "state": state.value,
            "root": asdict(root_state),
            "ancestors": [[str(path), asdict(value)] for path, value in ancestors],
            "directories": [
                {"relative_path": item.relative_path, "state": asdict(item.state),
                 "names": None if item.names is None else list(item.names)}
                for item in directory_items
            ],
            "entries": [self._file_data(item) for item in entries],
            "aliases": [_plain_json(asdict(item)) for item in alias_items],
            "exclusions": [_plain_json(asdict(item)) for item in exclusions],
            "issues": [_plain_json(asdict(item)) for item in issues],
            "selection_digest": self._selection_digest,
            "rules_digest": self._rules_digest,
        }).encode("utf-8")).hexdigest()
        return _Snapshot(
            snapshot_id=snapshot_id, entries=tuple(entries), exclusions=tuple(exclusions),
            issues=tuple(issues), state=state, root_state=root_state,
            ancestors=tuple(ancestors), directories=directory_items, aliases=alias_items,
            selection_digest=self._selection_digest, digest=digest,
            cursor=_CursorState(0, True), cursor_key=self._key,
        )

    @staticmethod
    def _file_data(item: _FileSnapshot) -> dict[str, object]:
        return {"entry": asdict(item.entry), "identity": asdict(item.identity),
                "mtime_ns": item.mtime_ns, "attributes": item.attributes}

    def _capture_file(self, relative, path_ref, info, entries, add_issue, add_exclusion,
                      identities, aliases, duplicate_reason, name_index):
        if not stat.S_ISREG(info.st_mode):
            add_issue(ScopeIssueCode.NOT_REGULAR_FILE, relative)
            return
        state = _node_state(info)
        if state is None:
            add_issue(ScopeIssueCode.FILE_IDENTITY_UNAVAILABLE, relative)
            return
        previous = identities.find(state.identity)
        self._identity_comparisons = identities.comparisons
        if previous is not None:
            previous_path = previous.entry.path.relative_path
            if previous_path == relative:
                # The same raw path was selected twice; there is no second path
                # to preserve, so record the duplicate selection explicitly.
                add_exclusion(relative, duplicate_reason, identity=state.identity)
                return
            # Same identity can be either a real hardlink (two directory
            # entries) or merely another spelling of one case-insensitive
            # directory entry.  Only a raw path that exists name-by-name is a
            # restorable alias; the other spelling is a duplicate selection.
            if not self._raw_entry_exists(relative, name_index):
                add_exclusion(relative, duplicate_reason, related=previous_path,
                              identity=state.identity)
                return
            content = previous.entry.content
            entries.append(_FileSnapshot(ArchiveEntry(
                content, path_ref, physical_identity=state.identity,
            ), state.identity,
                                         state.mtime_ns, state.attributes))
            aliases.append(ScopeFileAlias(relative, previous_path, state.identity,
                                          content.asset_id))
            return
        asset = "scope_" + hashlib.sha256(canonical_json([
            "archive-scope-v2", self._request.kind, self._request.root_id.casefold(),
            state.identity.device, state.identity.file_index,
        ]).encode("utf-8")).hexdigest()
        content = ContentVersion(asset, 0, state.size_bytes, None)
        item = _FileSnapshot(ArchiveEntry(
            content, path_ref, physical_identity=state.identity,
        ), state.identity,
                             state.mtime_ns, state.attributes)
        entries.append(item)
        identities.add(state.identity, item)

    def _indexable_directory(self, relative: str) -> bool:
        """False for a directory the eligibility policy would exclude anyway.

        Resolving the raw spelling of a path must never open a managed output
        or credential directory - nor one below such a directory - because that
        path is excluded later by the same policy: its listing decides nothing,
        and reading it would contradict the exclusion.  The ancestor check is
        what keeps a path such as `archive_store/nested` closed even though its
        own last segment is an ordinary name.
        """
        if relative == ".":
            return True
        if not self._ancestor_eligibility(relative).accepted:
            return False
        return self._directory_eligibility(relative, relative.rsplit("/", 1)[-1]).accepted

    def _resolve_spellings(
        self, relatives: list[str], name_index: _RawNameIndex,
    ) -> list[str]:
        """Resolve many selected spellings with one listing per distinct parent."""
        needed: set[str] = set()
        for relative in relatives:
            if relative == ".":
                continue
            typed = "."
            for piece in relative.split("/"):
                needed.add(typed)
                typed = piece if typed == "." else typed + "/" + piece
        listings = {directory: name_index.index(directory) for directory in needed}
        return [self._spell(listings, relative) for relative in relatives]

    def _spell(self, listings: dict[str, frozenset[str] | None], relative: str) -> str:
        """Rewrite one selection to the real child names its parents list."""
        if relative == ".":
            return relative
        resolved: list[str] = []
        typed = "."
        for piece in relative.split("/"):
            names = listings.get(typed)
            if names is None:
                return relative
            folded = os.path.normcase(piece)
            matches = [name for name in names if os.path.normcase(name) == folded]
            if len(matches) != 1:
                return relative
            resolved.append(matches[0])
            typed = piece if typed == "." else typed + "/" + piece
        return "/".join(resolved)

    def _real_entry_spelling(self, relative: str, name_index: _RawNameIndex) -> str:
        """Resolve one selected path to the spelling its directory entries have.

        Windows matches names case-insensitively, so ``gallery/a.bin`` and
        ``gallery/A.bin`` can name one entry, and a mixed-script name such as
        ``画A.bin`` has no case-free ASCII spelling at all.  The archive identity is
        that entry, not what the caller typed: each segment is matched against the
        parent's real child names with ``os.path.normcase``, which folds case on
        Windows and stays exact on POSIX.  ``normcase`` is not a casefold, so NFC/NFD
        and ``straße`` versus ``strasse`` remain different entries.

        Anything the index cannot answer - a missing name, an unreadable or
        policy-excluded directory, an exhausted name budget - keeps the requested
        spelling, so this never invents a name the enumerator did not read.  The
        listing itself is shared per enumeration, so selecting many files in one
        directory reads that directory once, never once per file.
        """
        return self._resolve_spellings([relative], name_index)[0]

    def _raw_entry_presence(self, relatives: list[str],
                            name_index: _RawNameIndex) -> dict[str, bool]:
        """Exact-spelling presence for many selections with one pass per parent.

        Every selection is split into its parent directories and the child name
        that must appear in each; those needs are grouped by directory, so a
        directory shared by hundreds of selections is read once.  A directory
        that cannot be read (missing, replaced, excluded) yields `False` rather
        than an exception: the selection's own metadata read reports why.
        """
        needed: dict[str, set[str]] = {}
        for relative in relatives:
            current = "."
            for piece in relative.split("/"):
                needed.setdefault(current, set()).add(piece)
                current = piece if current == "." else current + "/" + piece
        listings = {directory: name_index.index(directory) for directory in needed}
        presence = {}
        for relative in relatives:
            current = "."
            found = True
            for piece in relative.split("/"):
                names = listings.get(current)
                if names is None or piece not in names:
                    found = False
                    break
                current = piece if current == "." else current + "/" + piece
            presence[relative] = found
        return presence

    def _raw_entry_exists(self, relative: str, name_index: _RawNameIndex) -> bool:
        """True only when every raw path segment appears in its directory listing."""
        return self._raw_entry_presence([relative], name_index).get(relative, False)

    def _stability_session(self, snapshot: _Snapshot) -> _StabilitySession:
        session = self._stability
        if session is None or session.snapshot_digest != snapshot.digest:
            session = _StabilitySession(snapshot.digest)
            self._stability = session
        return session

    def _check_snapshot_root(self, snapshot: _Snapshot, guard: _WorkGuard,
                             start: int) -> None:
        """Between pages: prove path safety, then prove membership is unchanged.

        Ancestors above the root only prove that the path was not replaced, made
        into a reparse point, or taken offline; their own mtime is unrelated
        scope activity.  The root is checked on every page, and the enumerated
        directories are checked in a bounded rotating slice: about two passes
        over them for a whole delivery instead of one full pass per page.  The
        page that can complete a receipt verifies all of them before that
        receipt can exist.
        """
        ancestors = snapshot.ancestors
        if not ancestors:
            raise _Halt(_issue(ScopeIssueCode.ROOT_UNAVAILABLE, "."))
        root_path, root_before = ancestors[-1]
        for directory, before in ancestors[:-1]:
            guard.checkpoint()
            info = self._lstat_quiet(directory)
            guard.checkpoint()
            if info is None:
                raise _Halt(_issue(ScopeIssueCode.ROOT_UNAVAILABLE, "."))
            if _is_reparse(info):
                raise _Halt(_issue(ScopeIssueCode.ROOT_REPARSE_POINT, "."))
            if not stat.S_ISDIR(info.st_mode):
                raise _Halt(_issue(ScopeIssueCode.ROOT_UNAVAILABLE, "."))
            current = _node_state(info)
            if current is None or current.identity != before.identity:
                raise _Halt(_issue(ScopeIssueCode.ROOT_CHANGED, "."))
        guard.checkpoint()
        info = self._lstat_quiet(root_path)
        guard.checkpoint()
        if info is None:
            raise _Halt(_issue(ScopeIssueCode.ROOT_UNAVAILABLE, "."))
        if _is_reparse(info):
            raise _Halt(_issue(ScopeIssueCode.ROOT_REPARSE_POINT, "."))
        if not stat.S_ISDIR(info.st_mode):
            raise _Halt(_issue(ScopeIssueCode.ROOT_UNAVAILABLE, "."))
        current = _node_state(info)
        if current is None or current.identity != root_before.identity:
            raise _Halt(_issue(ScopeIssueCode.ROOT_CHANGED, "."))
        self._check_directory_membership(snapshot, root_path, guard, start)

    def _lstat_quiet(self, path: Path):
        try:
            return self._io.lstat(path)
        except OSError:
            return None

    def _check_directory_membership(self, snapshot: _Snapshot, root_path: Path,
                                    guard: _WorkGuard, start: int) -> None:
        """Verify a bounded, rotating slice of the recorded directories.

        Every page except the last checks `ceil(directories / pages)`, so one
        delivery pays about two passes over the directories in total
        (`O(directories)`) instead of one full pass per page
        (`O(directories * pages)`).  The page that ends a delivery verifies all
        of them, and it is the only page that can complete a whole-snapshot
        receipt: no receipt exists before a complete re-verification.
        """
        items = snapshot.directories
        if not items:
            return
        count = len(items)
        session = self._stability_session(snapshot)
        pages = max(1, -(-len(snapshot.entries) // self._request.page_size))
        terminal = len(snapshot.entries) - start <= self._request.page_size
        if terminal:
            first, size = 0, count
            session.sweeps += 1
        else:
            first = session.rotation % count
            size = min(count, max(1, -(-count // pages)))
        for offset in range(size):
            position = (first + offset) % count
            guard.checkpoint()
            self._verify_directory(items[position], root_path, guard, session)
            session.checks += 1
            session.rotation = (position + 1) % count

    def _verify_directory(self, item: _DirectorySnapshot, root_path: Path,
                          guard: _WorkGuard, session: _StabilitySession) -> None:
        relative = item.relative_path
        is_root = relative == "."
        target = root_path if is_root else root_path.joinpath(*relative.split("/"))
        guard.checkpoint()
        info = self._lstat_quiet(target)
        guard.checkpoint()
        if info is None:
            raise _Halt(_issue(
                ScopeIssueCode.ROOT_UNAVAILABLE if is_root else
                ScopeIssueCode.DIRECTORY_UNREADABLE, relative))
        if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise _Halt(_issue(
                ScopeIssueCode.ROOT_REPARSE_POINT if is_root else
                ScopeIssueCode.DIRECTORY_CHANGED, relative))
        current = _node_state(info)
        if current is None or current.identity != item.state.identity:
            raise _Halt(_issue(
                ScopeIssueCode.ROOT_CHANGED if is_root else
                ScopeIssueCode.DIRECTORY_CHANGED, relative))
        if item.names is None:
            # Ancestor-only record: identity guard, no membership snapshot.
            return
        confirmed = session.observed.get(relative, item.state)
        if (current.size_bytes, current.mtime_ns) == (confirmed.size_bytes,
                                                      confirmed.mtime_ns):
            return
        if self._read_names(target, relative, guard, item.names) != item.names:
            raise _Halt(_issue(
                ScopeIssueCode.ROOT_CHANGED if is_root else
                ScopeIssueCode.DIRECTORY_CHANGED, relative))
        rescans = session.rescans.get(relative, 0) + 1
        session.rescans[relative] = rescans
        if rescans > _MAX_MEMBERSHIP_RESCANS:
            # Membership was re-confirmed again and again: the directory is not
            # stable enough to certify, so the range is refused, not widened.
            raise _Halt(_issue(
                ScopeIssueCode.ROOT_CHANGED if is_root else
                ScopeIssueCode.DIRECTORY_CHANGED, relative))
        session.observed[relative] = current

    def _read_names(self, target: Path, relative: str, guard: _WorkGuard,
                    expected: tuple[str, ...] | None = None) -> tuple[str, ...] | None:
        """Read one directory's names, stopping at the first unexpected entry.

        `expected` is the snapshot's own name tuple.  A name the snapshot did not
        contain already proves the membership changed, so the read stops there
        instead of finishing a directory it must reject anyway; `None` reports
        that proof without reading the rest.
        """
        names = []
        expected_names = None if expected is None else frozenset(expected)
        try:
            with self._io.scandir(target) as children:
                for child in children:
                    guard.checkpoint()
                    name = child.name if hasattr(child, "name") else child
                    if not isinstance(name, str) or not name:
                        raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, relative))
                    full = name if relative == "." else relative + "/" + name
                    if expected_names is not None and full not in expected_names:
                        return None
                    names.append(full)
        except _Halt:
            raise
        except OSError:
            raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, relative)) from None
        guard.checkpoint()
        return tuple(sorted(names, key=lambda value: (_path_key(value), value)))

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
            selection_digest=self._selection_digest,
        )


def _page_reference(page: ScopePage) -> tuple:
    completion = page.completion
    return (
        completion.snapshot_id, completion.snapshot_digest, completion.root_identity,
        completion.entries_in_snapshot, completion.exclusions_in_snapshot,
        page.rules_digest, page.kind, page.root_id, page.root_revision,
        page.selection_digest, page.schema_version,
    )


def consume_scope_pages(pages, *, source=None, sub_batch=None,
                        signing_key: bytes | None = None) -> ScopeReceipt:
    """Verify one page delivery and return a versioned ScopeReceipt.

    Inconsistent deliveries raise ContractError: pages from different
    snapshots, rules or roots, a repeated or missing page, entries out of
    snapshot order, a sub-batch boundary that is not page aligned, pages that
    disagree about exclusions, or a delivery that continues after an aborted
    page.  A delivery that stops early, is cancelled, or is budget-limited
    returns a receipt whose `consumption_complete` and `scope_complete` are
    False; incompleteness is never raised away or upgraded silently.

    `source` is the ScopeEnumerator that produced the pages.  When given, the
    consumed entries are compared with the live snapshot entry by entry, which
    upgrades `snapshot_verified` from "self-consistent" to "matches the
    enumerator's own snapshot".
    """
    if isinstance(pages, ScopePage):
        raise ContractError("Expected a sequence of scope pages")
    delivered = tuple(pages)
    if not delivered:
        raise ContractError("At least one scope page is required")
    if any(not isinstance(page, ScopePage) for page in delivered):
        raise ContractError("Expected scope pages")
    if sub_batch is not None and not isinstance(sub_batch, ScopeSubBatch):
        raise ContractError("Invalid scope sub-batch")
    snapshot = None
    if source is not None:
        if not isinstance(source, ScopeEnumerator):
            raise ContractError("Expected the scope enumerator that produced the pages")
        snapshot = source._snapshot
        if snapshot is None:
            raise ContractError("The scope enumerator has no snapshot to verify")

    first = delivered[0]
    reference = _page_reference(first)
    if any(_page_reference(page) != reference for page in delivered[1:]):
        raise ContractError("Scope pages do not belong to one snapshot and rule set")
    if first.completion.snapshot_id is None or first.completion.snapshot_digest is None:
        raise ContractError("A scope receipt requires a frozen snapshot")
    if first.completion.root_identity is None:
        raise ContractError("A scope receipt requires a stable root identity")

    exclusions = first.exclusions
    collected: list[ArchiveEntry] = []
    collected_facts: list[ScopeEntryFact] = []
    seen: set[str] = set()
    previous_key = None
    index = 0
    aborted = False
    consumed_issues = 0
    for page in delivered:
        if page.exclusions != exclusions:
            raise ContractError("Scope pages disagree about exclusions")
        if aborted:
            raise ContractError("Scope pages continue after an aborted page")
        terminal_abort = (
            not page.entries and not page.has_next_page
            and page.completion.state not in {ScopeState.COMPLETE,
                                              ScopeState.COMPLETE_WITH_ISSUES})
        if terminal_abort:
            aborted = True
            consumed_issues = len(page.issues)
            break
        if page.page_index != index:
            raise ContractError("Scope pages are missing, duplicated, or out of order")
        index += 1
        consumed_issues = len(page.issues)
        if len(page.entry_facts) != len(page.entries):
            raise ContractError("A scope page lacks its entry facts")
        for entry, fact in zip(page.entries, page.entry_facts):
            path = entry.path
            if not isinstance(path, PathRef):
                raise ContractError("Invalid scope entry path")
            if (fact.kind != page.kind or fact.root_id != page.root_id
                    or fact.root_revision != page.root_revision
                    or fact.relative_path != path.relative_path
                    or fact.physical_identity != entry.physical_identity
                    or entry.content is None
                    or fact.size_bytes != entry.content.size_bytes
                    or fact.content_asset_id != entry.content.asset_id
                    or fact.content_revision != entry.content.revision
                    or fact.content_sha256 != entry.content.sha256):
                raise ContractError("Scope entry facts disagree with the page")
            relative = path.relative_path
            if relative in seen:
                raise ContractError("A scope entry was delivered twice")
            seen.add(relative)
            key = (_path_key(relative), relative)
            if previous_key is not None and key < previous_key:
                raise ContractError("Scope entries are not in snapshot order")
            previous_key = key
            collected.append(entry)
            collected_facts.append(fact)

    total = first.completion.entries_in_snapshot
    consumed = len(collected)
    if consumed > total:
        raise ContractError("A receipt cannot consume more entries than its snapshot")
    page_size = len(delivered[0].entries)
    expected_page_count = -(-total // page_size) if total and page_size else None
    if sub_batch is None:
        selection_kind = ScopeSelectionKind.SNAPSHOT
        selection_digest = first.selection_digest
        sub_batch_limit = None
        sub_batch_description = None
        limit = None
    else:
        selection_kind = ScopeSelectionKind.SUB_BATCH
        sub_batch_limit = sub_batch.limit
        sub_batch_description = sub_batch.description
        selection_digest = _digest_text([
            "sub_batch", first.selection_digest, sub_batch.limit, sub_batch.description,
        ])
        limit = min(sub_batch.limit, total)
        if not aborted and consumed != limit:
            raise ContractError("A sub-batch must end on its described page boundary")

    snapshot_verified = False
    if snapshot is not None:
        if (snapshot.snapshot_id != first.completion.snapshot_id
                or snapshot.digest != first.completion.snapshot_digest
                or snapshot.selection_digest != first.selection_digest):
            raise ContractError("Scope pages do not match the enumerator snapshot")
        for position, (entry, fact) in enumerate(zip(collected, collected_facts)):
            if position >= len(snapshot.entries):
                raise ContractError("Consumed entries exceed the snapshot")
            expected = snapshot.entries[position]
            if expected.entry.path != entry.path or expected.entry.content != entry.content:
                raise ContractError("Consumed entries do not match the snapshot")
            if (fact.physical_identity != expected.identity
                    or fact.mtime_ns != expected.mtime_ns
                    or fact.size_bytes != expected.entry.content.size_bytes):
                raise ContractError("Consumed entry physical facts do not match the snapshot")
        snapshot_verified = True

    receipt = ScopeReceipt(
        schema_version=SCOPE_RECEIPT_SCHEMA_VERSION,
        kind=first.kind,
        root_id=first.root_id,
        root_revision=first.root_revision,
        rules_digest=first.rules_digest,
        selection_digest=selection_digest,
        selection_kind=selection_kind,
        sub_batch_limit=sub_batch_limit,
        sub_batch_description=sub_batch_description,
        snapshot_id=first.completion.snapshot_id,
        snapshot_digest=first.completion.snapshot_digest,
        root_identity=first.completion.root_identity,
        entries_in_snapshot=total,
        exclusions_in_snapshot=first.completion.exclusions_in_snapshot,
        issues_in_snapshot=first.completion.issues_in_snapshot,
        enumeration_state=first.completion.state,
        page_count=len(delivered),
        first_page_index=delivered[0].page_index,
        # An aborted page is still the page the caller attempted; reporting -1
        # for a first-page abort made a valid incomplete delivery raise instead.
        last_page_index=max(index - 1, delivered[-1].page_index),
        expected_page_count=expected_page_count,
        last_page_has_next=delivered[-1].has_next_page,
        consumed_entries=consumed,
        consumed_exclusions=len(exclusions),
        consumed_issues=consumed_issues,
        remaining_entries=total - consumed,
        aborted=aborted,
        entries_digest=_facts_digest(collected_facts),
        entry_facts=tuple(collected_facts),
        exclusions_digest=_exclusions_digest(exclusions),
        snapshot_verified=snapshot_verified,
    )
    return receipt if signing_key is None else receipt.signed(signing_key)


def enumerate_scope(root, request: ScopeRequest, policy: ScopePolicy | None = None,
                    *, cancellation=None) -> ScopePage:
    """Convenience first-page entry point; retain ScopeEnumerator for more pages."""
    if cancellation is not None and not callable(getattr(cancellation, "is_set", None)):
        raise ContractError("Cancellation must provide is_set()")
    return ScopeEnumerator(root, request, policy).page(cancellation=cancellation)
