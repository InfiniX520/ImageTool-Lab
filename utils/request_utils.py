"""Backward-compatible request helper exports."""

from __future__ import annotations

from utils.common_utils import build_pixiv_headers, sleep_with_jitter


def jitter_sleep(base_seconds: float, jitter_seconds: float) -> None:
    sleep_with_jitter(base_seconds, jitter_seconds)


__all__ = ["build_pixiv_headers", "jitter_sleep"]
