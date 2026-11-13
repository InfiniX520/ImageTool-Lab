"""Pure current-health contract for an already delivered artifact.

An artifact row is a historical delivery fact.  This module never treats that
row as proof that the bytes are still present.  A health record binds one
expectation (artifact, path, root revision, size, digest, policy) to one
observation (full SHA-256, size, identity, time) and is the only evidence the
reuse paths may consume.

No file I/O, database access, download or deletion happens here.  The file I/O
owner is :mod:`core.local_observation`; the writer validates these frozen
records in its short transaction.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from core.local_observation import FileIdentity, LocalObservation, ObservationStatus
from core.preservation_policy import ContentVersion, LocalState
from core.task_contracts import ContractError, PathRef

_SUPPORTED_MEDIA = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")


class HealthPolicyMode(StrEnum):
    FULL = "full"
    QUICK = "quick"


@dataclass(frozen=True, slots=True)
class ArtifactHealthPolicy:
    """Versioned policy; a quick result can never be upgraded to a full one."""

    mode: HealthPolicyMode = HealthPolicyMode.FULL
    version: int = 1
    max_age_seconds: int = 900

    def __post_init__(self):
        if not isinstance(self.mode, HealthPolicyMode):
            raise ContractError("Invalid artifact health policy mode")
        if type(self.version) is not int or self.version != 1:
            raise ContractError("Unsupported artifact health policy version")
        if type(self.max_age_seconds) is not int or not 1 <= self.max_age_seconds <= 86400:
            raise ContractError("Invalid artifact health age limit")


@dataclass(frozen=True, slots=True)
class ArtifactExpectation:
    """The immutable facts a current observation must match to be usable."""

    artifact_id: str
    delivery_id: str
    job_id: str
    illust_id: str
    page_index: int
    output_key: str
    path: PathRef
    root_revision: int
    size_bytes: int
    sha256: str
    media_type: str
    quality_key: str = ""
    policy_version: int = 1

    def __post_init__(self):
        for value, name in (
            (self.artifact_id, "artifact ID"),
            (self.delivery_id, "delivery ID"),
            (self.job_id, "job ID"),
            (self.illust_id, "illust ID"),
            (self.output_key, "output key"),
        ):
            _token(value, name)
        if not isinstance(self.path, PathRef):
            raise ContractError("Invalid artifact path binding")
        if self.path.relative_path == ".":
            raise ContractError("Artifact path must name a file")
        _number(self.root_revision, "root revision")
        _number(self.page_index, "page index")
        _number(self.size_bytes, "artifact size")
        if self.size_bytes <= 0:
            raise ContractError("Artifact size must be positive")
        if not isinstance(self.sha256, str) or _DIGEST_RE.fullmatch(self.sha256) is None:
            raise ContractError("Invalid artifact digest")
        if self.media_type not in _SUPPORTED_MEDIA:
            raise ContractError("Unsupported artifact media type")
        if self.quality_key and _TOKEN_RE.fullmatch(self.quality_key) is None:
            raise ContractError("Invalid artifact quality key")
        if type(self.policy_version) is not int or self.policy_version != 1:
            raise ContractError("Unsupported artifact policy version")

    @property
    def content(self) -> ContentVersion:
        return ContentVersion(
            asset_id=self.artifact_id,
            revision=1,
            size_bytes=self.size_bytes,
            sha256=self.sha256,
        )

    @classmethod
    def from_artifact_row(
        cls,
        row: Any,
        *,
        path: PathRef,
        root_revision: int,
        quality_key: str = "",
        policy_version: int = 1,
    ) -> "ArtifactExpectation":
        return cls(
            artifact_id=str(row["artifact_id"]),
            delivery_id=str(row["delivery_id"]),
            job_id=str(row["job_id"]),
            illust_id=str(row["illust_id"]),
            page_index=int(row["page_index"]),
            output_key=str(row["output_key"]),
            path=path,
            root_revision=root_revision,
            size_bytes=int(row["size"]),
            sha256=str(row["sha256"]),
            media_type=str(row["media_type"]),
            quality_key=quality_key,
            policy_version=policy_version,
        )

    def matches_row(self, row: Any) -> bool:
        try:
            return (
                str(row["artifact_id"]) == self.artifact_id
                and str(row["delivery_id"]) == self.delivery_id
                and str(row["job_id"]) == self.job_id
                and str(row["illust_id"]) == self.illust_id
                and int(row["page_index"]) == self.page_index
                and str(row["output_key"]) == self.output_key
                and int(row["size"]) == self.size_bytes
                and str(row["sha256"]) == self.sha256
                and str(row["media_type"]) == self.media_type
            )
        except (KeyError, TypeError, ValueError):
            return False


@dataclass(frozen=True, slots=True)
class ArtifactHealth:
    """One frozen expectation/observation pair; not a permanent file claim."""

    expectation: ArtifactExpectation
    policy: ArtifactHealthPolicy
    state: LocalState
    checked_at: str
    reason: str
    observed_size: int | None = None
    observed_sha256: str | None = None
    bytes_read: int = 0
    identity: FileIdentity | None = None

    def __post_init__(self):
        if not isinstance(self.expectation, ArtifactExpectation):
            raise ContractError("Invalid artifact health expectation")
        if not isinstance(self.policy, ArtifactHealthPolicy):
            raise ContractError("Invalid artifact health policy")
        if not isinstance(self.state, LocalState):
            raise ContractError("Invalid artifact health state")
        _timestamp(self.checked_at)
        if not isinstance(self.reason, str) or _REASON_RE.fullmatch(self.reason) is None:
            raise ContractError("Invalid artifact health reason")
        if self.observed_size is not None:
            _number(self.observed_size, "observed size")
        if self.observed_sha256 is not None and (
            not isinstance(self.observed_sha256, str)
            or _DIGEST_RE.fullmatch(self.observed_sha256) is None
        ):
            raise ContractError("Invalid observed digest")
        if type(self.bytes_read) is not int or self.bytes_read < 0:
            raise ContractError("Invalid observed byte count")
        if self.identity is not None and not isinstance(self.identity, FileIdentity):
            raise ContractError("Invalid observed file identity")
        if self.state is LocalState.HASH_MATCH:
            if (
                self.observed_size != self.expectation.size_bytes
                or self.observed_sha256 != self.expectation.sha256
            ):
                raise ContractError("Hash match must bind the expected size and digest")
        if self.state is LocalState.QUICK_MATCH and self.observed_sha256 is not None:
            raise ContractError("Quick match must not claim a full digest")
        if self.state in {
            LocalState.MISSING,
            LocalState.UNREADABLE,
            LocalState.ROOT_UNAVAILABLE,
            LocalState.CHANGED_DURING_CHECK,
            LocalState.UNKNOWN,
        } and self.observed_sha256 is not None:
            raise ContractError("Incomplete health cannot contain a digest")

    @property
    def reusable(self) -> bool:
        if self.state is LocalState.HASH_MATCH:
            return self.policy.mode is HealthPolicyMode.FULL
        if self.state is LocalState.QUICK_MATCH:
            return self.policy.mode is HealthPolicyMode.QUICK
        return False

    def is_fresh(self, now: datetime | str | None = None) -> bool:
        checked = _parse_timestamp(self.checked_at)
        current = _parse_timestamp(now) if isinstance(now, str) else (
            now if isinstance(now, datetime) else datetime.now(timezone.utc)
        )
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return 0 <= (current - checked).total_seconds() <= self.policy.max_age_seconds

    def to_payload(self) -> dict[str, Any]:
        return {
            "artifact_id": self.expectation.artifact_id,
            "delivery_id": self.expectation.delivery_id,
            "job_id": self.expectation.job_id,
            "illust_id": self.expectation.illust_id,
            "page_index": self.expectation.page_index,
            "output_key": self.expectation.output_key,
            "path": asdict(self.expectation.path),
            "root_revision": self.expectation.root_revision,
            "size_bytes": self.expectation.size_bytes,
            "sha256": self.expectation.sha256,
            "media_type": self.expectation.media_type,
            "quality_key": self.expectation.quality_key,
            "policy_version": self.expectation.policy_version,
            "state": self.state.value,
            "checked_at": self.checked_at,
            "reason": self.reason,
            "mode": self.policy.mode.value,
            "max_age_seconds": self.policy.max_age_seconds,
            "observed_size": self.observed_size,
            "observed_sha256": self.observed_sha256,
            "bytes_read": self.bytes_read,
            "device": None if self.identity is None else self.identity.device,
            "file_index": None if self.identity is None else self.identity.file_index,
        }

    @classmethod
    def from_payload(cls, value: object) -> "ArtifactHealth":
        if not isinstance(value, dict) or set(value) != _PAYLOAD_KEYS:
            raise ContractError("Invalid artifact health payload")
        try:
            path = PathRef(**value["path"])
        except (TypeError, KeyError):
            raise ContractError("Invalid artifact health path") from None
        expectation = ArtifactExpectation(
            artifact_id=value["artifact_id"],
            delivery_id=value["delivery_id"],
            job_id=value["job_id"],
            illust_id=value["illust_id"],
            page_index=value["page_index"],
            output_key=value["output_key"],
            path=path,
            root_revision=value["root_revision"],
            size_bytes=value["size_bytes"],
            sha256=value["sha256"],
            media_type=value["media_type"],
            quality_key=value["quality_key"],
            policy_version=value["policy_version"],
        )
        try:
            mode = HealthPolicyMode(value["mode"])
            state = LocalState(value["state"])
        except ValueError:
            raise ContractError("Invalid artifact health state or mode") from None
        device = value["device"]
        file_index = value["file_index"]
        if (device is None) != (file_index is None):
            raise ContractError("Invalid artifact health identity")
        identity = None
        if device is not None:
            identity = FileIdentity(device, file_index)
        return cls(
            expectation=expectation,
            policy=ArtifactHealthPolicy(
                mode=mode,
                version=expectation.policy_version,
                max_age_seconds=value["max_age_seconds"],
            ),
            state=state,
            checked_at=value["checked_at"],
            reason=value["reason"],
            observed_size=value["observed_size"],
            observed_sha256=value["observed_sha256"],
            bytes_read=value["bytes_read"],
            identity=identity,
        )


_PAYLOAD_KEYS = frozenset({
    "artifact_id", "delivery_id", "job_id", "illust_id", "page_index",
    "output_key", "path", "root_revision", "size_bytes", "sha256",
    "media_type", "quality_key", "policy_version", "state", "checked_at",
    "reason", "mode", "max_age_seconds", "observed_size", "observed_sha256",
    "bytes_read", "device", "file_index",
})


def assess_observation(
    expectation: ArtifactExpectation,
    observation: LocalObservation,
    policy: ArtifactHealthPolicy = ArtifactHealthPolicy(),
) -> ArtifactHealth:
    """Map one observer result to a health record; mismatched scope is unknown."""
    if not isinstance(expectation, ArtifactExpectation):
        raise ContractError("Invalid artifact health expectation")
    if not isinstance(observation, LocalObservation):
        raise ContractError("Invalid artifact health observation")
    if not isinstance(policy, ArtifactHealthPolicy):
        raise ContractError("Invalid artifact health policy")
    evidence = observation.evidence
    if (
        evidence.content != expectation.content
        or evidence.path.target_key != expectation.path.target_key
        or evidence.root_revision != expectation.root_revision
    ):
        state = LocalState.UNKNOWN
        reason = "observation_scope_mismatch"
        observed_size = None
        observed_sha256 = None
        bytes_read = 0
        identity = None
    else:
        state = _STATE_BY_STATUS.get(observation.status, LocalState.UNKNOWN)
        reason = observation.reason
        observed_size = evidence.observed_size
        observed_sha256 = evidence.observed_sha256
        bytes_read = observation.bytes_read
        identity = observation.identity
        if state is LocalState.HASH_MATCH and (
            observed_size != expectation.size_bytes
            or observed_sha256 != expectation.sha256
        ):
            state = LocalState.UNKNOWN
            reason = "hash_match_scope_mismatch"
    return ArtifactHealth(
        expectation=expectation,
        policy=policy,
        state=state,
        checked_at=evidence.checked_at,
        reason=reason,
        observed_size=observed_size,
        observed_sha256=observed_sha256,
        bytes_read=bytes_read,
        identity=identity,
    )


def unobserved_health(
    expectation: ArtifactExpectation,
    *,
    state: LocalState,
    reason: str,
    checked_at: str,
    policy: ArtifactHealthPolicy = ArtifactHealthPolicy(),
) -> ArtifactHealth:
    """A fail-closed record for an unreadable/unmapped path."""
    if state in {LocalState.HASH_MATCH, LocalState.QUICK_MATCH}:
        raise ContractError("An unobserved artifact cannot be reusable")
    return ArtifactHealth(
        expectation=expectation,
        policy=policy,
        state=state,
        checked_at=checked_at,
        reason=reason,
    )


def artifact_health_allows_reuse(
    health: ArtifactHealth,
    row: Any,
    *,
    now: datetime | str | None,
    root_revision: int,
) -> bool:
    """The writer's final gate: fresh, same row, same root mapping, reusable."""
    return (
        isinstance(health, ArtifactHealth)
        and health.reusable
        and health.is_fresh(now)
        and health.expectation.root_revision == root_revision
        and health.expectation.matches_row(row)
    )


_STATE_BY_STATUS = {
    ObservationStatus.HASH_MATCH: LocalState.HASH_MATCH,
    ObservationStatus.BASELINE_OBSERVED: LocalState.UNKNOWN,
    ObservationStatus.CHANGED: LocalState.CHANGED,
    ObservationStatus.MISSING: LocalState.MISSING,
    ObservationStatus.ROOT_UNAVAILABLE: LocalState.ROOT_UNAVAILABLE,
    ObservationStatus.UNREADABLE: LocalState.UNREADABLE,
    ObservationStatus.PATH_REJECTED: LocalState.UNKNOWN,
    ObservationStatus.CHANGED_DURING_CHECK: LocalState.CHANGED_DURING_CHECK,
    ObservationStatus.CANCELLED: LocalState.UNKNOWN,
    ObservationStatus.TIME_BUDGET_EXHAUSTED: LocalState.UNKNOWN,
    ObservationStatus.BYTE_BUDGET_EXHAUSTED: LocalState.UNKNOWN,
}


def _token(value: object, name: str) -> None:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise ContractError(f"Invalid {name}")


def _number(value: object, name: str) -> None:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ContractError(f"Invalid {name}")


def _timestamp(value: object) -> None:
    if not isinstance(value, str) or len(value) > 64:
        raise ContractError("Invalid artifact health time")
    _parse_timestamp(value)


def _parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ContractError("Invalid artifact health time") from exc
    else:
        raise ContractError("Invalid artifact health time")
    if parsed.utcoffset() is None:
        raise ContractError("Artifact health time requires a timezone")
    return parsed
