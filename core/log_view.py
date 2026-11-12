#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bounded, Tk-free view model for the diagnostic log stream.

``LogView`` only consumes :class:`core.diagnostic_logging.DiagnosticEvent`
objects through the log's bounded UI ring.  It keeps a bounded window of
physical text lines (dual line/byte limit), turns each event into physical
lines, carries a bounded remainder of a long multi-line event across drains,
and reports the number of lines/events a paused or lagging view missed.  It
never calls Tk, never touches files and never keeps an unbounded backlog.  It is
a single-consumer view: ``drain``/``clear``/``pause`` are meant to be called
from the UI thread only.

``drain`` enforces three independent budgets: elapsed milliseconds, physical
lines and UTF-8 bytes.  The summed UTF-8 bytes of one delta never exceed the
requested byte budget.  A physical line larger than a whole budget is
display-truncated once with a visible marker when it fits, while the stored
event/full log keeps the complete message; a line that would fit a fresh budget
but not the remaining space stays pending for the next drain instead of being
truncated to fill the gap.  A pending remainder is always drained before newer
events, so limits can never reorder physical lines.  Extremely small byte
budgets (down to one byte) are still handled explicitly and make bounded
progress: the line is clipped to the longest valid UTF-8 prefix that fits, and
a one-byte ASCII placeholder is used when even one Unicode scalar cannot fit;
``0`` means "no work in this call".
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from core.diagnostic_logging import LEVEL_PREFIXES

_LINE_TRUNCATION_MARKER = "…[line truncated]"
_DEFAULT_MAX_LINES = 3000
_DEFAULT_MAX_BYTES = 2 * 1024 * 1024
_MIN_MAX_BYTES = 256
_MIN_DRAIN_BYTES = 4


@dataclass(frozen=True, slots=True)
class ViewDelta:
    """What one ``drain`` added/removed and what it had to skip."""

    append_lines: tuple[str, ...] = ()
    remove_lines: int = 0
    follow: bool = True
    skipped_events: int = 0
    unseen_lines: int = 0


def _line_bytes(line: str) -> int:
    return len(line.encode("utf-8"))


def _truncate_line(line: str, limit: int) -> str:
    data = line.encode("utf-8")
    if len(data) <= limit:
        return line
    marker = _LINE_TRUNCATION_MARKER
    budget = limit - len(marker.encode("utf-8"))
    if budget < 1:
        return data[:limit].decode("utf-8", "ignore")
    cut = data[:budget]
    while cut:
        try:
            kept = cut.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            cut = cut[: exc.start]
    else:
        kept = ""
    return kept + marker


def _safe_utf8_prefix(data: bytes, budget: int) -> str:
    """Decode the longest complete UTF-8 prefix that fits in ``budget`` bytes."""
    cut = data[: max(0, budget)]
    while cut:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError as exc:
            cut = cut[: exc.start]
    return ""


def _clip_line(line: str, limit: int) -> str:
    """Fit one physical line into ``limit`` UTF-8 bytes with a clear marker."""
    data = line.encode("utf-8")
    if len(data) <= limit:
        return line
    marker = _LINE_TRUNCATION_MARKER
    marker_bytes = len(marker.encode("utf-8"))
    if limit >= marker_bytes + _MIN_DRAIN_BYTES:
        return _safe_utf8_prefix(data, limit - marker_bytes) + marker
    clipped = _safe_utf8_prefix(data, limit)
    return clipped or "?"


def _positive_int(value: object, name: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _non_negative(value: object, name: str) -> float:
    if type(value) not in (int, float) or value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return float(value)


class LogView:
    """Bounded physical-line view over a ``DiagnosticLog`` UI ring."""

    def __init__(
        self,
        log,
        *,
        max_lines: int = _DEFAULT_MAX_LINES,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(getattr(log, "events_after", None)):
            raise ValueError("LogView requires a DiagnosticLog-like object")
        self._max_lines = _positive_int(max_lines, "max_lines", 1)
        self._max_bytes = _positive_int(max_bytes, "max_bytes", _MIN_MAX_BYTES)
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._log = log
        self._clock = clock
        self._lines: deque[str] = deque()
        self._line_bytes: deque[int] = deque()
        self._retained_bytes = 0
        self._pending: deque[str] = deque()
        self._cursor = 0
        self._clear_watermark = 0
        self._follow = True
        self._paused = False
        self._unseen_lines = 0

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------

    @property
    def max_lines(self) -> int:
        return self._max_lines

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def line_count(self) -> int:
        return len(self._lines)

    @property
    def retained_bytes(self) -> int:
        return self._retained_bytes

    @property
    def follow(self) -> bool:
        return self._follow

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def unseen_lines(self) -> int:
        return self._unseen_lines

    @property
    def pending_lines(self) -> int:
        return len(self._pending)

    def lines(self) -> tuple[str, ...]:
        """Return the retained window (only when a caller needs a snapshot)."""
        return tuple(self._lines)

    # ------------------------------------------------------------------
    # UI controls
    # ------------------------------------------------------------------

    def clear(self) -> int:
        """Hide everything up to the log's current sequence; return removed lines.

        The watermark suppresses every late or still-buffered event with a
        sequence at or below it, so a clear can never resurrect old text.
        """
        watermark = int(self._log.sequence)
        removed = len(self._lines)
        self._lines.clear()
        self._line_bytes.clear()
        self._retained_bytes = 0
        self._pending.clear()
        self._cursor = max(self._cursor, watermark)
        self._clear_watermark = max(self._clear_watermark, watermark)
        self._unseen_lines = 0
        return removed

    def pause(self, paused: bool = True) -> None:
        if type(paused) is not bool:
            raise ValueError("paused must be boolean")
        self._paused = paused

    def set_follow(self, follow: bool) -> None:
        if type(follow) is not bool:
            raise ValueError("follow must be boolean")
        self._follow = follow
        if follow:
            self._unseen_lines = 0

    # ------------------------------------------------------------------
    # Drain
    # ------------------------------------------------------------------

    def drain(
        self,
        *,
        max_ms: float = 8,
        max_lines: int = 100,
        max_bytes: int = 32 * 1024,
    ) -> ViewDelta:
        max_ms = _non_negative(max_ms, "max_ms")
        max_lines = _positive_int(max_lines, "max_lines", 0)
        max_bytes = _positive_int(max_bytes, "max_bytes", 0)
        if self._paused or max_lines == 0 or max_bytes == 0 or max_ms == 0:
            return ViewDelta(
                append_lines=(),
                remove_lines=0,
                follow=self._follow,
                skipped_events=0,
                unseen_lines=self._unseen_lines,
            )

        started = self._clock()
        deadline = started + max_ms / 1000.0
        appended: list[str] = []
        appended_bytes = 0
        removed_total = 0
        skipped = 0

        def time_up() -> bool:
            return self._clock() >= deadline

        def take(line: str) -> None:
            nonlocal appended_bytes, removed_total
            stored, removed = self._append_window(line)
            appended.append(stored)
            appended_bytes += _line_bytes(stored)
            removed_total += removed
            if not self._follow:
                self._unseen_lines += 1

        def consume(line: str) -> bool:
            """Append the line or keep it pending; never exceed ``max_bytes``."""
            cost = _line_bytes(line)
            remaining = max_bytes - appended_bytes
            if cost <= remaining:
                take(line)
                return True
            if appended:
                # The rest of the current budget cannot hold this line.  When
                # the whole line would fit a fresh budget it must stay pending;
                # never truncate only to fill the remaining gap.
                return False
            if cost > max_bytes:
                # A single physical line larger than the whole budget is
                # display-truncated once, with an explicit marker where room
                # allows.  The stored event/file keeps the full message.
                take(_clip_line(line, max_bytes))
                return True
            return False

        # A multi-line event left over from the previous drain comes first so
        # a limit can never silently reorder or duplicate physical lines.
        while self._pending:
            if len(appended) >= max_lines or time_up():
                break
            line = self._pending[0]
            if not consume(line):
                break
            self._pending.popleft()

        stopped = bool(self._pending)
        while not stopped:
            if len(appended) >= max_lines or time_up():
                break
            batch = self._log.events_after(self._cursor, limit=64)
            if not batch:
                break
            for event in batch:
                if event.sequence <= self._clear_watermark:
                    self._cursor = max(self._cursor, event.sequence)
                    continue
                if event.sequence <= self._cursor:
                    continue
                gap = event.sequence - self._cursor - 1
                if gap > 0:
                    skipped += gap
                lines = self._format_event(event)
                index = 0
                while index < len(lines):
                    if len(appended) >= max_lines or time_up():
                        self._pending.extend(lines[index:])
                        self._cursor = event.sequence
                        stopped = True
                        break
                    if not consume(lines[index]):
                        self._pending.extend(lines[index:])
                        self._cursor = event.sequence
                        stopped = True
                        break
                    index += 1
                if stopped:
                    break
                self._cursor = event.sequence
                if len(appended) >= max_lines or appended_bytes >= max_bytes:
                    stopped = True
                    break
                if time_up():
                    stopped = True
                    break
        return ViewDelta(
            append_lines=tuple(appended),
            remove_lines=removed_total,
            follow=self._follow,
            skipped_events=skipped,
            unseen_lines=self._unseen_lines,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _format_event(self, event) -> tuple[str, ...]:
        prefix = LEVEL_PREFIXES.get(event.level, "[*]")
        message = event.message
        if message == "":
            rendered = [prefix]
        else:
            physical = message.split("\n")
            rendered = [f"{prefix} {physical[0]}"]
            rendered.extend(physical[1:])
        cap = max(1, self._max_lines)
        if len(rendered) > cap:
            omitted = len(rendered) - cap + 1
            rendered = rendered[: cap - 1] + [f"[... {omitted} lines omitted ...]"]
        return tuple(rendered)

    def _append_window(self, line: str) -> tuple[str, int]:
        cost = _line_bytes(line)
        if cost > self._max_bytes:
            line = _truncate_line(line, self._max_bytes)
            cost = _line_bytes(line)
        removed = 0
        while self._lines and (
            len(self._lines) >= self._max_lines
            or self._retained_bytes + cost > self._max_bytes
        ):
            dropped = self._lines.popleft()
            self._retained_bytes -= _line_bytes(dropped)
            removed += 1
        self._lines.append(line)
        self._retained_bytes += cost
        return line, removed


__all__ = ["LogView", "ViewDelta"]
