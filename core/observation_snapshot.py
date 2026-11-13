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
