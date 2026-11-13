"""One global, append-only user-intent scope (removal intents).

The accept path already reads intents across the whole library, but recording
an exclusion used to cancel only the *current* job's queue, and claim, repair
and publication never looked at the intent again (UA-02, UA-10, UA-11).  This
module is the single reader for every one of those call sites:

* the key is content-addressed: (illust, page, output key, root revision);
* the newest `event_seq` wins; older events stay as history;
* an unreadable event is a first-class error, never "no intent";
* publication is allowed only when no exclusion is newer than the claim that
  started the transfer.  Exclusion therefore linearizes at the claim boundary.

No I/O and no SQL of its own beyond one ordered read of `events`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from core.preservation_policy import RemovalIntent
from core.task_contracts import ContractError

CANCELLABLE_STATES = ("pending", "suspended", "retry_wait", "needs_verification")
PUBLISHED_STATES = ("prepared", "completed")
ACTIVE_OUTPUT_STATES = (
    "pending",
    "transferring",
    "validating",
    "prepared",
    "needs_verification",
    "suspended",
    "retry_wait",
    "recovery_needed",
)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class IntentError(ContractError):
    """A removal-intent record cannot be used as written."""


class CorruptIntentError(IntentError):
    """An unreadable event must block, not silently mean "not excluded"."""

    def __init__(self, event_seq: int, detail: str):
        if type(event_seq) is not int or event_seq < 0:
            raise IntentError("Invalid corrupt intent event")
        self.event_seq = event_seq
        self.detail = detail
        super().__init__(f"removal intent event {event_seq} is unreadable: {detail}")


def _token(value: object, name: str, pattern: re.Pattern[str] = _TOKEN_RE) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise IntentError(f"Invalid {name}")
    return value


@dataclass(frozen=True, slots=True)
class IntentKey:
    illust_id: str
    page_index: int
    output_key: str
    root_revision: int

    def __post_init__(self):
        _token(self.illust_id, "illust id")
        if type(self.page_index) is not int or not 0 <= self.page_index <= 9999:
            raise IntentError("Invalid page index")
        _token(self.output_key, "output key", _KEY_RE)
        if type(self.root_revision) is not int or self.root_revision < 0:
            raise IntentError("Invalid root revision")

    @property
    def storage_key(self) -> tuple[str, int, str, int]:
        return (self.illust_id, self.page_index, self.output_key, self.root_revision)

    def to_payload(self) -> dict[str, Any]:
        return {
            "illust_id": self.illust_id,
            "page_index": self.page_index,
            "output_key": self.output_key,
            "root_revision": self.root_revision,
        }

    @classmethod
    def from_payload(cls, value: object) -> "IntentKey":
        if not isinstance(value, Mapping):
            raise IntentError("Invalid intent key")
        try:
            return cls(
                illust_id=str(value["illust_id"]),
                page_index=value["page_index"],
                output_key=str(value["output_key"]),
                root_revision=value["root_revision"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise IntentError("Invalid intent key") from exc


@dataclass(frozen=True, slots=True)
class IntentRecord:
    intent: RemovalIntent
    event_seq: int
    command_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.intent, RemovalIntent):
            raise IntentError("Invalid removal intent")
        if type(self.event_seq) is not int or self.event_seq < 0:
            raise IntentError("Invalid intent event sequence")
        if self.command_id is not None:
            _token(self.command_id, "intent command id")


@dataclass(frozen=True, slots=True)
class IntentProjection:
    """Latest intent per key, plus every event that could not be read."""

    records: Mapping[tuple[str, int, str, int], IntentRecord]
    corrupt_events: tuple[int, ...] = ()
    max_seq: int = 0

    def __post_init__(self):
        frozen: dict[tuple[str, int, str, int], IntentRecord] = {}
        for storage_key, record in self.records.items():
            if not isinstance(record, IntentRecord):
                raise IntentError("Invalid intent projection record")
            frozen[tuple(storage_key)] = record
        object.__setattr__(self, "records", MappingProxyType(frozen))
        if len(set(self.corrupt_events)) != len(self.corrupt_events):
            raise IntentError("Duplicate corrupt intent event")

    def record(self, key: IntentKey) -> IntentRecord | None:
        return self.records.get(key.storage_key)

    def state(self, key: IntentKey) -> RemovalIntent:
        record = self.record(key)
        return RemovalIntent.NONE if record is None else record.intent

    def is_excluded(self, key: IntentKey) -> bool:
        return self.state(key) is RemovalIntent.EXCLUDED

    def require_clean(self) -> None:
        if self.corrupt_events:
            raise CorruptIntentError(self.corrupt_events[0], "corrupt payload")

    def blocks_claim(self, key: IntentKey) -> bool:
        """Claim must refuse on an exclusion and fail closed on corruption."""
        return bool(self.corrupt_events) or self.is_excluded(key)

    def blocks_repair(self, key: IntentKey) -> bool:
        return bool(self.corrupt_events) or self.is_excluded(key)

    def publish_allowed(self, key: IntentKey) -> bool:
        """Publication linearizes immediately before the final link.

        The gate runs before the executor creates the final name; a delivery
        that already published bytes is finalized as "completed this time" and
        the exclusion only constrains later work.  An unreadable projection
        fails closed.
        """
        if self.corrupt_events:
            return False
        return not self.is_excluded(key)


def _parse_intent_event(
    payload: object, command_id: object
) -> tuple[tuple[str, int, str, int], IntentRecord]:
    if not isinstance(payload, str):
        raise IntentError("Removal intent payload is not text")
    try:
        info = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise IntentError("Removal intent payload is not JSON") from exc
    if not isinstance(info, Mapping):
        raise IntentError("Removal intent payload is not an object")
    try:
        key = IntentKey(
            illust_id=str(info["illust_id"]),
            page_index=info["page_index"],
            output_key=str(info["output_key"]),
            root_revision=info["root_revision"],
        )
        intent = RemovalIntent(str(info["intent"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise IntentError("Removal intent payload is incomplete") from exc
    return key.storage_key, IntentRecord(
        intent=intent,
        event_seq=0,
        command_id=None if command_id is None else str(command_id),
    )


def read_projection(conn: Any) -> IntentProjection:
    """Read every removal-intent event once, newest wins, never guess.

    `conn` is any object with `execute` returning rows indexable by name.
    """
    rows = conn.execute(
        "SELECT event_seq, safe_payload, accepted_command_id FROM events "
        "WHERE kind = 'removal_intent' ORDER BY event_seq"
    ).fetchall()
    latest: dict[tuple[str, int, str, int], IntentRecord] = {}
    corrupt: list[int] = []
    max_seq = 0
    for row in rows:
        try:
            seq = int(row["event_seq"])
        except (TypeError, ValueError):
            corrupt.append(-1)
            continue
        max_seq = max(max_seq, seq)
        try:
            storage_key, record = _parse_intent_event(
                row["safe_payload"], row["accepted_command_id"]
            )
            latest[storage_key] = IntentRecord(
                intent=record.intent, event_seq=seq, command_id=record.command_id
            )
        except IntentError:
            corrupt.append(seq)
    return IntentProjection(
        records=latest, corrupt_events=tuple(corrupt), max_seq=max_seq
    )
