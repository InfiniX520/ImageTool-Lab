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

Directory identity and metadata checks detect ordinary enumeration races; they
are not an atomic defence against an adversarial filesystem writer.  Final
content identity and stability remain LocalObserver's responsibility.
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
from dataclasses import asdict, dataclass, replace
from enum import Enum, StrEnum
from pathlib import Path
from typing import ContextManager

from core.archive_inventory import ArchiveEntry
from core.local_observation import FileIdentity
from core.preservation_policy import ContentVersion
from core.task_contracts import ContractError, PathRef, canonical_json, normalize_json

SCOPE_SCHEMA_VERSION = 1
SCOPE_RECEIPT_SCHEMA_VERSION = 1
_CURSOR_SCHEMA = "jpgpng.archive-scope.cursor.v1"
_RECEIPT_SCHEMA = "jpgpng.archive-scope.receipt.v1"
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
    narrows a selected scope.  Budgets cover the entire snapshot, not each
    page.  At most one terminal issue beyond max_issues is retained to explain
    an issue-budget stop.
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

    def __post_init__(self):
        object.__setattr__(self, "entries", tuple(self.entries))
        object.__setattr__(self, "exclusions", tuple(self.exclusions))
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(self, "aliases", tuple(self.aliases))
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


def _entries_digest(entries: Iterable[ArchiveEntry]) -> str:
    rows = []
    for entry in entries:
        path = entry.path
        relative = path.relative_path if isinstance(path, PathRef) else str(path)
        rows.append([relative, None if entry.content is None else entry.content.size_bytes])
    return _digest_text(rows)


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
        if payload.pop("schema", None) != _RECEIPT_SCHEMA:
            raise ContractError("Unexpected scope receipt schema")
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
        """True when these entries are exactly the consumed set, in order."""
        return hmac.compare_digest(_entries_digest(entries), self.entries_digest)


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
            return self._make_page(snapshot, (), None, tuple(current_issues), state, start=0,
                                   has_next=False)
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
            aliases=snapshot.aliases, selection_digest=snapshot.selection_digest,
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

    def _checkpoint(self, started: float, cancelled) -> None:
        if cancelled is not None and cancelled():
            raise _Halt(_issue(ScopeIssueCode.CANCELLED, "."), ScopeState.CANCELLED)
        now = self._monotonic()
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ContractError("Scope clock returned an invalid value")
        if now - started > self._policy.max_seconds:
            raise _Halt(_issue(ScopeIssueCode.TIME_BUDGET_EXHAUSTED, "."),
                        ScopeState.BUDGET_EXHAUSTED)

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

    def _enumerate(self, cancelled) -> _Snapshot | None:
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
            previous = directory_identities.get(state.identity)
            if previous is not None:
                add_exclusion(relative, ScopeExclusionReason.DUPLICATE_SELECTION,
                              related=previous, identity=state.identity)
                return False
            verdict = self._directory_eligibility(relative, relative.rsplit("/", 1)[-1])
            if not verdict.accepted:
                add_verdict(relative, verdict, state.identity)
                return False
            directory_identities[state.identity] = relative
            record_directory(relative, state, None)
            return True

        try:
            self._checkpoint(started, cancelled)
            ancestors = root_chain()
            selections: list[tuple[str, str, bool]] = []
            selected: dict[tuple[bool, str], str] = {}
            for index, (is_prefix, raw) in enumerate(
                    [(True, value) for value in self._request.prefixes]
                    + [(False, value) for value in self._request.files]):
                self._checkpoint(started, cancelled)
                consume_entry("<selection>")
                relative = selection_relative(raw, index)
                if relative is None:
                    continue
                if relative == "." and not is_prefix:
                    add_issue(ScopeIssueCode.INVALID_PATH, relative)
                    continue
                # Exact text only: a normalised name is not a file identity.
                previous = selected.get((is_prefix, relative))
                if previous is not None:
                    add_exclusion(relative, ScopeExclusionReason.DUPLICATE_SELECTION,
                                  related=previous)
                    continue
                if is_prefix and not prepare_prefix(relative):
                    continue
                selected[(is_prefix, relative)] = relative
                selections.append((_path_key(relative), relative, is_prefix))
            # Prefer the spelling that actually appears in the directory when
            # two selections name one case-insensitive entry.  This keeps the
            # real path and lets the alternate spelling be an explicit
            # duplicate exclusion instead of becoming a second restorable path.
            selections.sort(key=lambda value: (
                value[0], not self._raw_entry_exists(value[1]), value[1], not value[2],
            ))
            pending_files = deque(
                (relative, key) for key, relative, is_prefix in selections if not is_prefix)
            pending_directories = deque(
                (relative, key) for key, relative, is_prefix in selections if is_prefix)
            directory_queue: deque[str] = deque()
            while pending_files or pending_directories or directory_queue:
                self._checkpoint(started, cancelled)
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
                                       ScopeExclusionReason.DUPLICATE_SELECTION)
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
                    self._checkpoint(started, cancelled)
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
                                       ScopeExclusionReason.DUPLICATE_CANDIDATE)
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
            for relative, before in directories.items():
                self._checkpoint(started, cancelled)
                target = (self._root if relative == "."
                          else self._root.joinpath(*relative.split("/")))
                info = lstat(target, relative, root=relative == ".")
                current = None if info is None else _node_state(info)
                if (current != before or _is_reparse(info)
                        or not stat.S_ISDIR(info.st_mode)):
                    raise _Halt(_issue(ScopeIssueCode.DIRECTORY_CHANGED, relative))
            self._checkpoint(started, cancelled)
            state = ScopeState.COMPLETE_WITH_ISSUES if issues else ScopeState.COMPLETE
        except _Halt as halt:
            self._identity_comparisons = identities.comparisons
            add_issue(halt.issue.code, halt.issue.relative_path, terminal=True)
            state = halt.state
            try:
                ancestors = root_chain()
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
                      identities, aliases, duplicate_reason):
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
                add_exclusion(relative, duplicate_reason, related=previous_path,
                              identity=state.identity)
                return
            # Same identity can be either a real hardlink (two directory
            # entries) or merely another spelling of one case-insensitive
            # directory entry.  Only a raw path that exists name-by-name is a
            # restorable alias; the other spelling is a duplicate selection.
            if not self._raw_entry_exists(relative):
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

    def _raw_entry_exists(self, relative: str) -> bool:
        """True only when every raw path segment appears in its directory listing."""
        current = self._root
        for piece in relative.split("/"):
            try:
                with self._io.scandir(current) as children:
                    if not any(
                        (child.name if hasattr(child, "name") else child) == piece
                        for child in children
                    ):
                        return False
            except OSError:
                return False
            current = current / piece
        return True

    def _check_snapshot_root(self, snapshot: _Snapshot) -> None:
        """Between pages: prove path safety, then prove membership is unchanged.

        Ancestors above the root only prove that the path was not replaced, made
        into a reparse point, or taken offline; their own mtime is unrelated
        scope activity.  The root and every enumerated directory are compared
        against their recorded child-name snapshot instead of raw mtime, so an
        unrelated write in a parent directory cannot abort paging while a real
        membership change still does.
        """
        ancestors = snapshot.ancestors
        if not ancestors:
            raise _Halt(_issue(ScopeIssueCode.ROOT_UNAVAILABLE, "."))
        root_path, root_before = ancestors[-1]
        for directory, before in ancestors[:-1]:
            info = self._lstat_quiet(directory)
            if info is None:
                raise _Halt(_issue(ScopeIssueCode.ROOT_UNAVAILABLE, "."))
            if _is_reparse(info):
                raise _Halt(_issue(ScopeIssueCode.ROOT_REPARSE_POINT, "."))
            if not stat.S_ISDIR(info.st_mode):
                raise _Halt(_issue(ScopeIssueCode.ROOT_UNAVAILABLE, "."))
            current = _node_state(info)
            if current is None or current.identity != before.identity:
                raise _Halt(_issue(ScopeIssueCode.ROOT_CHANGED, "."))
        info = self._lstat_quiet(root_path)
        if info is None:
            raise _Halt(_issue(ScopeIssueCode.ROOT_UNAVAILABLE, "."))
        if _is_reparse(info):
            raise _Halt(_issue(ScopeIssueCode.ROOT_REPARSE_POINT, "."))
        if not stat.S_ISDIR(info.st_mode):
            raise _Halt(_issue(ScopeIssueCode.ROOT_UNAVAILABLE, "."))
        current = _node_state(info)
        if current is None or current.identity != root_before.identity:
            raise _Halt(_issue(ScopeIssueCode.ROOT_CHANGED, "."))
        self._check_directory_membership(snapshot, root_path)

    def _lstat_quiet(self, path: Path):
        try:
            return self._io.lstat(path)
        except OSError:
            return None

    def _check_directory_membership(self, snapshot: _Snapshot, root_path: Path) -> None:
        for item in snapshot.directories:
            relative = item.relative_path
            is_root = relative == "."
            target = root_path if is_root else root_path.joinpath(*relative.split("/"))
            info = self._lstat_quiet(target)
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
                continue
            if (current.size_bytes, current.mtime_ns) == (item.state.size_bytes,
                                                          item.state.mtime_ns):
                continue
            if self._read_names(target, relative) != item.names:
                raise _Halt(_issue(
                    ScopeIssueCode.ROOT_CHANGED if is_root else
                    ScopeIssueCode.DIRECTORY_CHANGED, relative))

    def _read_names(self, target: Path, relative: str) -> tuple[str, ...]:
        names = []
        try:
            with self._io.scandir(target) as children:
                for child in children:
                    name = child.name if hasattr(child, "name") else child
                    if not isinstance(name, str) or not name:
                        raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, relative))
                    names.append(name if relative == "." else relative + "/" + name)
        except _Halt:
            raise
        except OSError:
            raise _Halt(_issue(ScopeIssueCode.DIRECTORY_UNREADABLE, relative)) from None
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
        for entry in page.entries:
            path = entry.path
            if not isinstance(path, PathRef):
                raise ContractError("Invalid scope entry path")
            relative = path.relative_path
            if relative in seen:
                raise ContractError("A scope entry was delivered twice")
            seen.add(relative)
            key = (_path_key(relative), relative)
            if previous_key is not None and key < previous_key:
                raise ContractError("Scope entries are not in snapshot order")
            previous_key = key
            collected.append(entry)

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
        for position, entry in enumerate(collected):
            if position >= len(snapshot.entries):
                raise ContractError("Consumed entries exceed the snapshot")
            expected = snapshot.entries[position]
            if expected.entry.path != entry.path or expected.entry.content != entry.content:
                raise ContractError("Consumed entries do not match the snapshot")
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
        last_page_index=index - 1,
        expected_page_count=expected_page_count,
        last_page_has_next=delivered[-1].has_next_page,
        consumed_entries=consumed,
        consumed_exclusions=len(exclusions),
        consumed_issues=consumed_issues,
        remaining_entries=total - consumed,
        aborted=aborted,
        entries_digest=_entries_digest(collected),
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
