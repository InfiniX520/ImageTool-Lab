#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Image samples shared by download-integrity tests.

``PNG_BYTES`` is a deterministic 1x1 RGBA PNG assembled with :mod:`struct`
and :mod:`zlib`; every chunk CRC is computed, and the suite independently
re-parses it (CRC, IEND, decompressed scanline length) before treating it as
a structurally valid sample.

``JPEG_BYTES`` and ``GIF_BYTES`` are small real-format sample byte strings
used only for signature/transfer tests.  This suite does not pixel-decode
them, so it makes no claim about full-file decodability.

``WEBP_SIGNATURE_BYTES`` is a synthetic 12-byte RIFF/WEBP signature sample
with no image payload; it is not a decodable WebP.  It exists only to exercise
the engine's signature gate.
"""

from __future__ import annotations

import base64
import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    """Encode one PNG chunk with its CRC over ``type + payload``."""
    crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", crc)
    )


def _build_minimal_png() -> bytes:
    """Build a 1x1 8-bit RGBA PNG with a single filtered pixel."""
    width = height = 1
    bit_depth = 8
    color_type = 6  # RGBA
    ihdr = struct.pack(
        ">IIBBBBB",
        width,
        height,
        bit_depth,
        color_type,
        0,  # compression method
        0,  # filter method
        0,  # interlace method
    )
    scanline = b"\x00\xff\x00\x00\xff"  # filter 0 + opaque red
    idat = zlib.compress(scanline, level=9)
    return (
        PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def _decode(value: str) -> bytes:
    return base64.b64decode("".join(value.split()), validate=True)


PNG_BYTES = _build_minimal_png()

# Small JPEG/GIF sample bytes for signature/transfer checks only.
JPEG_BYTES = _decode(
    """
    /9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a
    HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy
    MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIA
    AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA
    AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3
    ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm
    p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA
    AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx
    BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK
    U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3
    uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD3+iii
    gD//2Q==
    """
)
GIF_BYTES = _decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")

# Synthetic RIFF/WEBP signature only; not a decodable WebP (no VP8/VP8L chunk).
WEBP_SIGNATURE_BYTES = b"RIFF" + struct.pack("<I", 4) + b"WEBP"
