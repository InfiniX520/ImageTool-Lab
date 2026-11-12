"""Backward-compatible hash helper."""

from __future__ import annotations

from pathlib import Path

from utils.common_utils import calculate_file_sha256


def file_sha256(file_path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    return calculate_file_sha256(file_path, chunk_size=chunk_size)


__all__ = ["file_sha256"]
