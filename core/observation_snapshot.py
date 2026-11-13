"""Frozen preflight snapshot: the only proof that a page was looked at.

A health list alone cannot distinguish "the preflight observed this page and it
is gone" from "the preflight never saw this page".  The writer used to collapse
both into `artifact_unknown` and overwrite a concurrent, valid completion
(UA-01).  This module keeps the two facts apart:

* `PageSnapshot.generation` / `delivery_id` pin which completed delivery the
  preflight was looking at;
* `observed` carries the frozen health records for that page only;
* `unobserved` names candidates the preflight could not turn into evidence;
* `degraded` says the whole snapshot is incomplete.

The writer may reuse only a record from the same page, the same completed
generation and the same artifact.  Anything else raises `RetryPreflight` so the
caller can hash again outside the write transaction; it must never be turned
into a new `needs_verification` row.

A page may hold more candidates than one observation slice can hash.  The
progress types at the end of this module describe the append-only facts a slice
leaves behind: an `ObservationKey` binds them to the exact page content and job
(never to a batch, so a resume keeps them), a `ProgressRecord` binds one
observation to one artifact row, and `PageBudget.max_slices` bounds how many
slices one call may take.  Records are reusable only as current evidence for
the identical row: the writer re-checks the row, the quality decision, the root
revision, the policy, the age and (with one stat, never a hash) that the object
on disk is the one that was observed.  Anything stale or mismatched is observed
again.

No file or database I/O happens here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from core.artifact_health import (
    ArtifactHealth,
    ArtifactHealthPolicy,
    HealthPolicyMode,
)
from core.task_contracts import ContractError

SNAPSHOT_SCHEMA = 1
MAX_PAGES = 4096
MAX_OBSERVED_PER_PAGE = 32
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")


class SnapshotError(ContractError):
    """The frozen snapshot is malformed or does not bind its own page."""


class RetryPreflight(Exception):
    """The writer saw evidence the snapshot never covered.

    Raised inside the write transaction, caught by the caller *outside* it.
    Nothing is written; the caller runs the bounded preflight again.
    """

    def __init__(self, reason: str = "preflight_incomplete"):
        if _REASON_RE.fullmatch(reason) is None:
            raise SnapshotError("Invalid preflight retry reason")
        self.reason = reason
        super().__init__(reason)


class PreflightUnavailable(Exception):
    """Bounded retries ran out; no row may be written for this page."""

    def __init__(self, reason: str = "preflight_unavailable"):
        if _REASON_RE.fullmatch(reason) is None:
            raise SnapshotError("Invalid preflight failure reason")
        self.reason = reason
        super().__init__(reason)


def _token(value: object, name: str, pattern: re.Pattern[str] = _TOKEN_RE) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SnapshotError(f"Invalid {name}")
    return value


def _index(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 9999:
        raise SnapshotError("Invalid page index")
    return value


@dataclass(frozen=True, slots=True)
class PageKey:
    """One output page inside one job quality decision."""

    illust_id: str
    page_index: int
    output_key: str
    quality_key: str

    def __post_init__(self):
        _token(self.illust_id, "illust id")
        _index(self.page_index)
        _token(self.output_key, "output key", _KEY_RE)
        _token(self.quality_key, "quality key", _KEY_RE)

    @property
    def storage_key(self) -> tuple[str, int, str, str]:
        return (self.illust_id, self.page_index, self.output_key, self.quality_key)

    def to_payload(self) -> dict[str, Any]:
        return {
            "illust_id": self.illust_id,
            "page_index": self.page_index,
            "output_key": self.output_key,
            "quality_key": self.quality_key,
        }

    @classmethod
    def from_payload(cls, value: object) -> "PageKey":
        if not isinstance(value, Mapping):
            raise SnapshotError("Invalid page key")
        try:
            return cls(
                illust_id=str(value["illust_id"]),
                page_index=value["page_index"],
                output_key=str(value["output_key"]),
                quality_key=str(value["quality_key"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SnapshotError("Invalid page key") from exc


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    """What the preflight knew about one page at one instant."""

    key: PageKey
    generation: int
    delivery_id: str | None
    observed: tuple[ArtifactHealth, ...] = ()
    unobserved: tuple[str, ...] = ()
    degraded: bool = False
    reason: str = "ok"

    def __post_init__(self):
        if type(self.generation) is not int or self.generation < 0:
            raise SnapshotError("Invalid completed generation")
        if self.delivery_id is not None:
            _token(self.delivery_id, "completed delivery id")
        if self.delivery_id is None and self.generation:
            raise SnapshotError("A completed generation needs a delivery identity")
        if self.delivery_id is not None and not self.generation:
            raise SnapshotError("A completed delivery needs a positive generation")
        if type(self.degraded) is not bool:
            raise SnapshotError("Invalid degraded flag")
        if _REASON_RE.fullmatch(self.reason) is None:
            raise SnapshotError("Invalid snapshot reason")
        if len(self.observed) > MAX_OBSERVED_PER_PAGE:
            raise SnapshotError("Too many observed artifacts for one page")
        seen: set[str] = set()
        for record in self.observed:
            if not isinstance(record, ArtifactHealth):
                raise SnapshotError("Invalid observed health record")
            expectation = record.expectation
            if (
                expectation.illust_id != self.key.illust_id
                or expectation.page_index != self.key.page_index
                or expectation.output_key != self.key.output_key
                or expectation.quality_key != self.key.quality_key
            ):
                raise SnapshotError("Observed record belongs to another page")
            if expectation.artifact_id in seen:
                raise SnapshotError("Duplicate observed artifact")
            seen.add(expectation.artifact_id)
        if len(set(self.unobserved)) != len(self.unobserved):
            raise SnapshotError("Duplicate unobserved artifact")
        for artifact_id in self.unobserved:
            _token(artifact_id, "unobserved artifact id")
            if artifact_id in seen:
                raise SnapshotError("An artifact cannot be both observed and unobserved")

    @property
    def artifact_ids(self) -> tuple[str, ...]:
        return tuple(record.expectation.artifact_id for record in self.observed)

    def health_for(self, artifact_id: str) -> ArtifactHealth | None:
        for record in self.observed:
            if record.expectation.artifact_id == artifact_id:
                return record
        return None

    def stale_for(self, generation: int, delivery_id: str | None) -> bool:
        """True when the writer is looking at newer evidence than this page."""
        if type(generation) is not int or generation < 0:
            return True
        return generation != self.generation or delivery_id != self.delivery_id

    def covers(self, artifact_id: str) -> bool:
        """True only when the preflight actually produced evidence for it."""
        return self.health_for(artifact_id) is not None

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key.to_payload(),
            "generation": self.generation,
            "delivery_id": self.delivery_id,
            "observed": [record.to_payload() for record in self.observed],
            "unobserved": list(self.unobserved),
            "degraded": self.degraded,
            "reason": self.reason,
        }

    @classmethod
    def from_payload(cls, value: object) -> "PageSnapshot":
        if not isinstance(value, Mapping):
            raise SnapshotError("Invalid page snapshot")
        try:
            observed = tuple(
                ArtifactHealth.from_payload(item) for item in value.get("observed", ())
            )
            unobserved = value.get("unobserved", ())
            if not isinstance(unobserved, (list, tuple)):
                raise SnapshotError("Invalid unobserved list")
            return cls(
                key=PageKey.from_payload(value["key"]),
                generation=value["generation"],
                delivery_id=value.get("delivery_id"),
                observed=observed,
                unobserved=tuple(str(item) for item in unobserved),
                degraded=value.get("degraded", False),
                reason=str(value.get("reason", "ok")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SnapshotError("Invalid page snapshot") from exc


def policy_payload(policy: ArtifactHealthPolicy) -> dict[str, Any]:
    if not isinstance(policy, ArtifactHealthPolicy):
        raise SnapshotError("Invalid health policy")
    return {
        "mode": policy.mode.value,
        "version": policy.version,
        "max_age_seconds": policy.max_age_seconds,
    }


def policy_from_payload(value: object) -> ArtifactHealthPolicy:
    if not isinstance(value, Mapping):
        raise SnapshotError("Invalid health policy")
    try:
        return ArtifactHealthPolicy(
            mode=HealthPolicyMode(str(value["mode"])),
            version=value["version"],
            max_age_seconds=value["max_age_seconds"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotError("Invalid health policy") from exc


@dataclass(frozen=True, slots=True)
class PreflightSnapshot:
    """Every page looked at by one preflight run, plus its completeness."""

    run_id: str
    pages: Mapping[tuple[str, int, str, str], PageSnapshot]
    policy: ArtifactHealthPolicy = ArtifactHealthPolicy()
    degraded: bool = False
    reason: str = "ok"
    schema: int = SNAPSHOT_SCHEMA

    def __post_init__(self):
        _token(self.run_id, "preflight run id")
        if self.schema != SNAPSHOT_SCHEMA:
            raise SnapshotError("Unsupported snapshot schema")
        if not isinstance(self.policy, ArtifactHealthPolicy):
            raise SnapshotError("Invalid snapshot policy")
        if type(self.degraded) is not bool:
            raise SnapshotError("Invalid snapshot degraded flag")
        if _REASON_RE.fullmatch(self.reason) is None:
            raise SnapshotError("Invalid snapshot reason")
        frozen: dict[tuple[str, int, str, str], PageSnapshot] = {}
        for storage_key, page in self.pages.items():
            if not isinstance(page, PageSnapshot):
                raise SnapshotError("Invalid snapshot page")
            if tuple(storage_key) != page.key.storage_key:
                raise SnapshotError("Snapshot page key does not match its record")
            frozen[page.key.storage_key] = page
        if len(frozen) > MAX_PAGES:
            raise SnapshotError("Too many pages in one preflight snapshot")
        object.__setattr__(self, "pages", MappingProxyType(frozen))

    def page(self, key: PageKey) -> PageSnapshot | None:
        return self.pages.get(key.storage_key)

    def __len__(self) -> int:
        """Number of recorded health observations (diagnostics only)."""
        return sum(len(page.observed) for page in self.pages.values())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "policy": policy_payload(self.policy),
            "degraded": self.degraded,
            "reason": self.reason,
            "pages": [page.to_payload() for page in self.pages.values()],
        }

    @classmethod
    def from_payload(cls, value: object) -> "PreflightSnapshot":
        if not isinstance(value, Mapping):
            raise SnapshotError("Invalid preflight snapshot")
        pages = value.get("pages", ())
        if not isinstance(pages, (list, tuple)):
            raise SnapshotError("Invalid snapshot page list")
        try:
            records = [PageSnapshot.from_payload(item) for item in pages]
            return cls(
                run_id=str(value["run_id"]),
                pages={page.key.storage_key: page for page in records},
                policy=policy_from_payload(value.get("policy")),
                degraded=value.get("degraded", False),
                reason=str(value.get("reason", "ok")),
                schema=value.get("schema", 0),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SnapshotError("Invalid preflight snapshot") from exc


def empty_snapshot(run_id: str) -> PreflightSnapshot:
    """A snapshot that claims nothing; the writer must retry on any candidate."""
    return PreflightSnapshot(run_id=run_id, pages={}, degraded=True, reason="no_candidates")


PROGRESS_SCHEMA = 1
PROGRESS_EVENT_KIND = "page_observation_progress"
MAX_PROGRESS_RECORDS = 4096
MAX_PROGRESS_EVENT_RECORDS = 512
MAX_PROGRESS_EVENTS = 200
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ObservationKey:
    """Identity of one page acceptance, stable across resume and batches.

    The batch is deliberately absent: a resume creates a new batch, yet the
    observations taken for the same page must still be usable.  The page digest
    pins the exact page content, so a changed page starts over instead of
    reusing evidence for a different candidate set.
    """

    job_id: str
    source_id: str
    pass_id: str
    entity_id: str
    page_identity: str
    page_digest: str

    def __post_init__(self):
        for value, name in (
            (self.job_id, "job id"),
            (self.source_id, "source id"),
            (self.pass_id, "pass id"),
            (self.entity_id, "entity id"),
            (self.page_identity, "page identity"),
        ):
            _token(value, name)
        if (
            not isinstance(self.page_digest, str)
            or _DIGEST_RE.fullmatch(self.page_digest) is None
        ):
            raise SnapshotError("Invalid page digest")

    def to_payload(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "source_id": self.source_id,
            "pass_id": self.pass_id,
            "entity_id": self.entity_id,
            "page_identity": self.page_identity,
            "page_digest": self.page_digest,
        }

    @classmethod
    def from_payload(cls, value: object) -> "ObservationKey":
        if not isinstance(value, Mapping):
            raise SnapshotError("Invalid observation page key")
        try:
            return cls(
                job_id=str(value["job_id"]),
                source_id=str(value["source_id"]),
                pass_id=str(value["pass_id"]),
                entity_id=str(value["entity_id"]),
                page_identity=str(value["page_identity"]),
                page_digest=str(value["page_digest"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SnapshotError("Invalid observation page key") from exc


@dataclass(frozen=True, slots=True)
class ProgressRecord:
    """One artifact observation a later slice may reuse under §1.3."""

    artifact_id: str
    root_revision: int
    health: ArtifactHealth

    def __post_init__(self):
        _token(self.artifact_id, "artifact id")
        if not isinstance(self.health, ArtifactHealth):
            raise SnapshotError("Invalid observation progress health")
        if self.health.expectation.artifact_id != self.artifact_id:
            raise SnapshotError("Progress record does not bind its artifact")
        if (
            type(self.root_revision) is not int
            or self.root_revision != self.health.expectation.root_revision
        ):
            raise SnapshotError("Progress record does not bind its root revision")

    @property
    def output_key(self) -> str:
        return self.health.expectation.output_key

    @property
    def quality_key(self) -> str:
        return self.health.expectation.quality_key

    def to_payload(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "output_key": self.output_key,
            "quality_key": self.quality_key,
            "root_revision": self.root_revision,
            "health": self.health.to_payload(),
        }

    @classmethod
    def from_payload(cls, value: object) -> "ProgressRecord":
        if not isinstance(value, Mapping):
            raise SnapshotError("Invalid observation progress record")
        try:
            health = ArtifactHealth.from_payload(value["health"])
            record = cls(
                artifact_id=str(value["artifact_id"]),
                root_revision=value["root_revision"],
                health=health,
            )
            if str(value.get("output_key")) != record.output_key:
                raise SnapshotError("Progress record output key does not match its health")
            if str(value.get("quality_key")) != record.quality_key:
                raise SnapshotError(
                    "Progress record quality key does not match its health"
                )
            return record
        except SnapshotError:
            raise
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            raise SnapshotError("Invalid observation progress record") from exc


@dataclass(frozen=True, slots=True)
class PageObservationProgress:
    """Observations an earlier slice already produced for one page."""

    key: ObservationKey
    records: Mapping[str, ProgressRecord]
    schema: int = PROGRESS_SCHEMA
    truncated: bool = False

    def __post_init__(self):
        if not isinstance(self.key, ObservationKey):
            raise SnapshotError("Invalid observation progress key")
        if self.schema != PROGRESS_SCHEMA:
            raise SnapshotError("Unsupported observation progress schema")
        if type(self.truncated) is not bool:
            raise SnapshotError("Invalid observation progress truncation flag")
        frozen: dict[str, ProgressRecord] = {}
        for artifact_id, record in self.records.items():
            if not isinstance(record, ProgressRecord):
                raise SnapshotError("Invalid observation progress record")
            if str(artifact_id) != record.artifact_id:
                raise SnapshotError("Progress record key does not match its artifact")
            frozen[record.artifact_id] = record
        if len(frozen) > MAX_PROGRESS_RECORDS:
            raise SnapshotError("Too many observation progress records")
        object.__setattr__(self, "records", MappingProxyType(frozen))

    def record(self, artifact_id: str) -> ProgressRecord | None:
        return self.records.get(artifact_id)

    def __len__(self) -> int:
        return len(self.records)


def progress_event_payload(
    key: ObservationKey,
    records: Iterable[ProgressRecord],
    *,
    observed_files: int,
    observed_bytes: int,
    slice_seq: int,
    root_revisions: Mapping[str, int],
    policy_version: int,
) -> dict[str, Any]:
    """Freeze one slice of observations as an append-only progress fact."""
    if not isinstance(key, ObservationKey):
        raise SnapshotError("Invalid observation key")
    items = tuple(records)
    if not items or len(items) > MAX_PROGRESS_EVENT_RECORDS:
        raise SnapshotError("Invalid observation progress record count")
    if not all(isinstance(item, ProgressRecord) for item in items):
        raise SnapshotError("Invalid observation progress record")
    for value, name in (
        (observed_files, "observed files"),
        (observed_bytes, "observed bytes"),
        (slice_seq, "slice sequence"),
    ):
        if type(value) is not int or value < 0:
            raise SnapshotError(f"Invalid {name}")
    if type(policy_version) is not int or policy_version < 1:
        raise SnapshotError("Invalid observation policy version")
    roots: dict[str, int] = {}
    for root_id, revision in dict(root_revisions).items():
        if type(revision) is not int or revision < 1:
            raise SnapshotError("Invalid observation root revision")
        roots[_token(str(root_id), "root id")] = revision
    return {
        "schema_version": PROGRESS_SCHEMA,
        "page_key": key.to_payload(),
        "records": [item.to_payload() for item in items],
        "observed_files": observed_files,
        "observed_bytes": observed_bytes,
        "slice_seq": slice_seq,
        "root_revisions": roots,
        "policy_version": policy_version,
    }


def progress_from_event(
    value: object,
) -> tuple[ObservationKey, tuple[ProgressRecord, ...]]:
    """Parse a stored progress fact; a malformed fact is never trusted."""
    if not isinstance(value, Mapping):
        raise SnapshotError("Invalid observation progress event")
    if value.get("schema_version") != PROGRESS_SCHEMA:
        raise SnapshotError("Unsupported observation progress schema")
    records = value.get("records")
    if not isinstance(records, (list, tuple)) or not records:
        raise SnapshotError("Invalid observation progress records")
    if len(records) > MAX_PROGRESS_EVENT_RECORDS:
        raise SnapshotError("Too many observation progress records")
    return (
        ObservationKey.from_payload(value.get("page_key")),
        tuple(ProgressRecord.from_payload(item) for item in records),
    )


@dataclass(frozen=True, slots=True)
class PageBudget:
    """Per-call slice limits plus the per-page hard budget (contract v4 §6).

    max_files/max_slices bound one call: how many files a slice may open and
    how many slices a call may take.  They are a courtesy that keeps a large
    page interruptible, never the page's permanent limit.

    max_bytes/max_seconds bound one *attempt* (one page acceptance, including
    the strict confirmation pass that re-reads a reused candidate).  The
    remaining allowance is clipped into every single-file observation, so a
    file the page cannot pay for is refused before it is opened instead of
    silently overrunning the declared limit.

    max_bytes is the whole page's content ceiling, not one burst: accepting a
    page has to read every candidate once (the slices plus the confirmations),
    so a page whose bytes exceed it can never commit.  The default leaves room
    for a large multi-page work (8 GiB) while still refusing a runaway page;
    max_files/max_slices remain the burst limits.
    """

    max_files: int = 32
    max_bytes: int = 8 * 1024 * 1024 * 1024
    max_seconds: float = 300.0
    max_slices: int = 8

    def __post_init__(self):
        if type(self.max_files) is not int or not 1 <= self.max_files <= 4096:
            raise SnapshotError("Invalid page file budget")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 64 * 1024**3:
            raise SnapshotError("Invalid page byte budget")
        if (
            type(self.max_seconds) is not float
            and type(self.max_seconds) is not int
        ) or not 0.1 <= float(self.max_seconds) <= 3600:
            raise SnapshotError("Invalid page time budget")
        if type(self.max_slices) is not int or not 1 <= self.max_slices <= 1024:
            raise SnapshotError("Invalid page slice budget")


class PreflightCancelled(Exception):
    """The caller cancelled or exhausted the whole-page observation.

    This is a control result, never a file-health verdict: it must not create a
    delivery, advance a cursor or be translated into MISSING/CHANGED.
    """

    def __init__(
        self,
        reason: str = "cancelled",
        details: Mapping[str, int] | None = None,
    ):
        if _REASON_RE.fullmatch(reason) is None:
            raise SnapshotError("Invalid preflight cancellation reason")
        if details is not None:
            if not isinstance(details, Mapping) or not all(
                isinstance(key, str) and type(value) is int and value >= 0
                for key, value in details.items()
            ):
                raise SnapshotError("Invalid preflight cancellation details")
            details = dict(details)
        self.reason = reason
        # A budget result states its net progress; a control result leaves
        # this empty (contract v4 §3.2).
        self.details = details
        super().__init__(reason)
