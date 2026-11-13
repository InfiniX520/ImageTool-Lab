"""Four-state presence evidence for one declared path reference.

A missing file and an unreachable root are different facts.  Cancellation and
cleanup may only conclude "the staged temp is gone" when the root is reachable
and the directory entry is provably absent; an unmounted volume, an unbound
root identity or a permission error keeps the delivery recoverable (RFA-02).

This module never creates a directory and never turns an exception into
"absent": it reports what it saw, and the caller decides what that means.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum

from core.library_paths import PathResolver, RootState
from core.task_contracts import ContractError, PathRef

_DETAIL_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class Presence(StrEnum):
    """What one directory entry proves right now."""

    PRESENT = "present"
    ABSENT = "absent"
    UNREADABLE = "unreadable"
    ROOT_UNAVAILABLE = "root_unavailable"


@dataclass(frozen=True, slots=True)
class PresenceResult:
    """One observation of one PathRef under the current root mapping."""

    state: Presence
    ref: PathRef
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.state, Presence):
            raise ContractError("Presence state must be a Presence value")
        if not isinstance(self.ref, PathRef):
            raise ContractError("Presence requires a validated path reference")
        if not isinstance(self.detail, str) or _DETAIL_RE.fullmatch(self.detail) is None:
            raise ContractError("Invalid presence detail")

    @property
    def is_observable(self) -> bool:
        """True when the root was reachable and the entry itself was decided."""
        return self.state in (Presence.PRESENT, Presence.ABSENT)

    @property
    def is_absent(self) -> bool:
        """True only for "the root is reachable and the entry does not exist"."""
        return self.state is Presence.ABSENT


def probe_ref(resolver: PathResolver, ref: PathRef) -> PresenceResult:
    """Observe one path without creating or deleting anything.

    The root classification comes from the immutable mapping, so an unplugged
    volume or a moved library is reported as ROOT_UNAVAILABLE instead of a
    missing file.  Only a live root is probed with os.stat.
    """
    if not isinstance(resolver, PathResolver):
        raise ContractError("Presence requires a PathResolver")
    if not isinstance(ref, PathRef):
        raise ContractError("Presence requires a validated path reference")
    try:
        root_state = resolver.root_state(ref)
    except OSError:
        # The mapping is readable but the directory entry is not.
        return PresenceResult(Presence.ROOT_UNAVAILABLE, ref, "root_state_failed")
    if root_state is RootState.MISSING_BOUND:
        return PresenceResult(Presence.ROOT_UNAVAILABLE, ref, "missing_bound")
    if root_state is not RootState.AVAILABLE:
        return PresenceResult(Presence.ROOT_UNAVAILABLE, ref, "unbound")
    try:
        target = resolver.resolve(ref)
    except OSError:
        return PresenceResult(Presence.UNREADABLE, ref, "resolve_failed")
    try:
        os.stat(target)
    except FileNotFoundError:
        return PresenceResult(Presence.ABSENT, ref, "ok")
    except NotADirectoryError:
        # A parent component is a file; the entry cannot exist.
        return PresenceResult(Presence.ABSENT, ref, "parent_not_a_directory")
    except PermissionError:
        return PresenceResult(Presence.UNREADABLE, ref, "permission_denied")
    except OSError:
        return PresenceResult(Presence.UNREADABLE, ref, "io_error")
    return PresenceResult(Presence.PRESENT, ref, "ok")
