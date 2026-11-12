"""Shared local/archive reuse policy; pure decisions, with no I/O or deletion.

Evidence is supplied by trusted observers, not user settings. This module checks
its binding and consistency; it does not perform hashing, prove cloud possession,
authorize file release, or preserve historical download events on its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from core.task_contracts import ContractError, PathRef


class LocalState(StrEnum):
    UNKNOWN = "unknown"
    HASH_MATCH = "hash_match"
    QUICK_MATCH = "quick_match"
    MISSING = "missing"
    CHANGED = "changed"
    UNREADABLE = "unreadable"
    ROOT_UNAVAILABLE = "root_unavailable"
    CHANGED_DURING_CHECK = "changed_during_check"


class ArchiveState(StrEnum):
    UPLOAD_REPORTED = "user_reported_uploaded"
    USER_RETURNED_VERIFIED = "user_returned_verified"
    ADAPTER_READBACK_VERIFIED = "adapter_readback_verified"
    ACCESS_FAILED = "access_failed"


class RemovalIntent(StrEnum):
    NONE = "none"
    OFFLOADED = "offloaded"
    EXCLUDED = "excluded"


class PreservationOutcome(StrEnum):
    REUSE_LOCAL = "reuse_local"
    REUSE_LOCAL_QUICK = "reuse_local_quick"
    SKIPPED_ARCHIVED = "skipped_archived"
    VERIFY_LOCAL = "verify_local"
    REVIEW_CONFLICT = "review_conflict"
    WAIT_STABLE = "wait_stable"
    CHECK_LOCAL_ACCESS = "check_local_access"
    RESTORE_OFFERED = "restore_offered"
    OFFLOADED_UNVERIFIED = "offloaded_unverified"
    EXCLUDED = "excluded"
    LOCATE_MISSING = "locate_missing"
    CHECK_ARCHIVE_ACCESS = "check_archive_access"


_VERIFIED_ARCHIVE = frozenset({
    ArchiveState.USER_RETURNED_VERIFIED, ArchiveState.ADAPTER_READBACK_VERIFIED,
})


def _token(value: object, name: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ContractError(f"Invalid {name}")


def _number(value: object, name: str) -> None:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ContractError(f"Invalid {name}")


def _digest(value: object, name: str) -> None:
    if value is not None and (
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
    ):
        raise ContractError(f"Invalid {name}")


def _timestamp(value: object) -> None:
    if not isinstance(value, str) or len(value) > 64:
        raise ContractError("Invalid observation time")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("Missing timezone")
    except ValueError as exc:
        raise ContractError("Invalid observation time") from exc


@dataclass(frozen=True, slots=True)
class ContentVersion:
    """A precise asset revision; an unknown historical hash stays unknown."""

    asset_id: str
    revision: int
    size_bytes: int
    sha256: str | None = None

    def __post_init__(self):
        _token(self.asset_id, "asset ID")
        _number(self.revision, "content revision")
        _number(self.size_bytes, "content size")
        _digest(self.sha256, "expected SHA-256")


@dataclass(frozen=True, slots=True)
class LocalEvidence:
    """An observation bound to content, path, root mapping and verification run."""

    content: ContentVersion
    path: PathRef
    root_revision: int
    run_id: str
    check_id: str
    checked_at: str
    state: LocalState
    observed_size: int | None = None
    observed_sha256: str | None = None

    def __post_init__(self):
        if not isinstance(self.content, ContentVersion) or not isinstance(self.path, PathRef):
            raise ContractError("Invalid local evidence scope")
        if not isinstance(self.state, LocalState):
            raise ContractError("Invalid local observation state")
        _number(self.root_revision, "root mapping revision")
        _token(self.run_id, "verification run")
        _token(self.check_id, "local check ID")
        _timestamp(self.checked_at)
        if self.observed_size is not None:
            _number(self.observed_size, "observed size")
        _digest(self.observed_sha256, "observed SHA-256")
        if self.state in {LocalState.HASH_MATCH, LocalState.QUICK_MATCH}:
            if self.observed_size != self.content.size_bytes:
                raise ContractError("Matching evidence requires matching byte counts")
        if self.state is LocalState.HASH_MATCH:
            if self.content.sha256 is None or self.observed_sha256 != self.content.sha256:
                raise ContractError("Hash match requires the expected and observed full digest")
        if self.state is LocalState.QUICK_MATCH and self.observed_sha256 is not None:
            raise ContractError("Quick evidence must not present a full hash verification")


@dataclass(frozen=True, slots=True)
class ArchiveEvidence:
    """Latest observation of a selected archive copy of this content revision.

Verified states are reserved for a completed package/member/control verification
protocol. A check ID is a reference to that evidence, not proof in itself. Manual
and adapter readback remain distinct; neither status permits deleting originals.
"""

    content: ContentVersion
    state: ArchiveState
    check_id: str
    checked_at: str
    location_ref: str | None = None

    def __post_init__(self):
        if not isinstance(self.content, ContentVersion) or not isinstance(self.state, ArchiveState):
            raise ContractError("Invalid archive observation")
        _token(self.check_id, "archive check ID")
        _timestamp(self.checked_at)
        if self.location_ref is not None:
            _token(self.location_ref, "archive location reference")
        if self.state in _VERIFIED_ARCHIVE:
            if self.content.sha256 is None or self.location_ref is None:
                raise ContractError("Verified archive requires a content digest and location")


@dataclass(frozen=True, slots=True)
class PreservationPolicy:
    local_check: str = "full"
    required_copy: str = "saved"
    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError("Unsupported preservation policy version")
        if self.local_check not in ("full", "quick"):
            raise ContractError("Invalid local verification policy")
        if self.required_copy not in ("saved", "local"):
            raise ContractError("Invalid required copy policy")


@dataclass(frozen=True, slots=True)
class PreservationDecision:
    outcome: PreservationOutcome
    local_state: LocalState
    archive_state: ArchiveState | None
    local_hash_verified: bool
    archive_verified: bool
    ignored_local_evidence: bool
    ignored_archive_evidence: bool


def assess_preservation(
    content: ContentVersion,
    path: PathRef,
    *,
    root_revision: int,
    run_id: str,
    local: LocalEvidence | None = None,
    archive: ArchiveEvidence | None = None,
    intent: RemovalIntent = RemovalIntent.NONE,
    policy: PreservationPolicy = PreservationPolicy(),
) -> PreservationDecision:
    """Recommend a bounded next step; never schedule downloads, writes or deletion.

The caller supplies the latest observations and a removal intent scoped to this
asset/path/root mapping. Production observers must reject stale file identities
and unstable reads before claiming HASH_MATCH. Their I/O protocol is separate.
"""
    if not isinstance(content, ContentVersion) or not isinstance(path, PathRef):
        raise ContractError("Invalid preservation target")
    if not isinstance(policy, PreservationPolicy) or not isinstance(intent, RemovalIntent):
        raise ContractError("Invalid preservation policy or intent")
    if local is not None and not isinstance(local, LocalEvidence):
        raise ContractError("Invalid local evidence")
    if archive is not None and not isinstance(archive, ArchiveEvidence):
        raise ContractError("Invalid archive evidence")
    _number(root_revision, "root mapping revision")
    _token(run_id, "verification run")

    local_current = local is not None and (
        local.content == content and local.path.target_key == path.target_key
        and local.root_revision == root_revision and local.run_id == run_id
    )
    archive_current = archive is not None and archive.content == content
    state = local.state if local_current else LocalState.UNKNOWN
    archive_state = archive.state if archive_current else None
    archive_verified = archive_state in _VERIFIED_ARCHIVE

    def result(outcome: PreservationOutcome) -> PreservationDecision:
        return PreservationDecision(
            outcome, state, archive_state, state is LocalState.HASH_MATCH,
            archive_verified, local is not None and not local_current,
            archive is not None and not archive_current,
        )

    # Preserve unexpected local changes even if a good archived version exists.
    if state is LocalState.CHANGED:
        return result(PreservationOutcome.REVIEW_CONFLICT)
    if state is LocalState.CHANGED_DURING_CHECK:
        return result(PreservationOutcome.WAIT_STABLE)
    if state is LocalState.HASH_MATCH:
        return result(PreservationOutcome.REUSE_LOCAL)
    if state is LocalState.QUICK_MATCH and policy.local_check == "quick":
        return result(PreservationOutcome.REUSE_LOCAL_QUICK)
    if policy.required_copy == "saved" and archive_verified:
        return result(PreservationOutcome.SKIPPED_ARCHIVED)
    if state in {LocalState.UNREADABLE, LocalState.ROOT_UNAVAILABLE}:
        return result(PreservationOutcome.CHECK_LOCAL_ACCESS)
    if state is LocalState.QUICK_MATCH:
        return result(PreservationOutcome.VERIFY_LOCAL)
    if state is LocalState.MISSING:
        if intent is RemovalIntent.EXCLUDED:
            return result(PreservationOutcome.EXCLUDED)
        if archive_verified:
            return result(PreservationOutcome.RESTORE_OFFERED)
    if archive_state is ArchiveState.ACCESS_FAILED:
        return result(PreservationOutcome.CHECK_ARCHIVE_ACCESS)
    if intent is RemovalIntent.OFFLOADED or archive_state is ArchiveState.UPLOAD_REPORTED:
        return result(PreservationOutcome.OFFLOADED_UNVERIFIED)
    if state is LocalState.MISSING:
        return result(PreservationOutcome.LOCATE_MISSING)
    return result(PreservationOutcome.VERIFY_LOCAL)
