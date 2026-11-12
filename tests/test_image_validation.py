"""Real format samples, every truncation point, corruption and resource caps."""
import hashlib
import struct
import threading
import tracemalloc
import zlib

import pytest

from core.image_validation import (
    ImageNeedsVerification, ImageValidationError, ImageValidationStopped, inspect_image,
)

try:
    from image_fixtures import GIF_BYTES, JPEG_BYTES, PNG_BYTES, WEBP_SIGNATURE_BYTES
    from structural_fixtures import WEBP_ALPHA_BYTES, WEBP_OPAQUE_BYTES
except ImportError:
    from tests.image_fixtures import GIF_BYTES, JPEG_BYTES, PNG_BYTES, WEBP_SIGNATURE_BYTES
    from tests.structural_fixtures import WEBP_ALPHA_BYTES, WEBP_OPAQUE_BYTES


SAMPLES = [(PNG_BYTES, "image/png", 1, 1), (JPEG_BYTES, "image/jpeg", 1, 1),
           (GIF_BYTES, "image/gif", 1, 1), (WEBP_OPAQUE_BYTES, "image/webp", 2, 2),
           (WEBP_ALPHA_BYTES, "image/webp", 2, 2)]


def chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def png(plain, *, width=1, height=1, color=6, bits=8, interlace=0, before_data=b""):
    header = struct.pack(">IIBBBBB", width, height, bits, color, 0, 0, interlace)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + before_data + chunk(b"IDAT", zlib.compress(plain)) + chunk(b"IEND", b"")


@pytest.mark.parametrize("data,media,width,height", SAMPLES)
def test_format_evidence_preserves_source_bytes_and_reports_limited_grade(tmp_path, data, media, width, height):
    path = tmp_path / "owned.part"
    path.write_bytes(data)
    evidence = inspect_image(path, expected_size=len(data), expected_media_type=media)
    assert (evidence.media_type, evidence.width, evidence.height, evidence.frames) == (media, width, height, 1)
    assert evidence.size == len(data) and evidence.sha256 == hashlib.sha256(data).hexdigest()
    assert evidence.verification == "structure_checked"
    assert path.read_bytes() == data


@pytest.mark.parametrize("data,media,width,height", SAMPLES)
def test_every_truncation_of_reference_samples_is_rejected(tmp_path, data, media, width, height):
    path = tmp_path / "owned.part"
    for offset in range(len(data)):
        path.write_bytes(data[:offset])
        with pytest.raises(ImageValidationError):
            inspect_image(path)


@pytest.mark.parametrize("data,media,width,height", SAMPLES)
def test_trailing_content_is_not_part_of_a_complete_image(tmp_path, data, media, width, height):
    path = tmp_path / "owned.part"
    path.write_bytes(data + b"<html>unexpected content</html>")
    with pytest.raises(ImageValidationError):
        inspect_image(path)


@pytest.mark.parametrize("data", [b"<html>HTTP 200</html>", b'{"error":true}', b"\xff\xd8\xff\xd9", WEBP_SIGNATURE_BYTES])
def test_error_body_and_signature_only_files_are_rejected(tmp_path, data):
    path = tmp_path / "owned.part"
    path.write_bytes(data)
    with pytest.raises(ImageValidationError):
        inspect_image(path)


def test_png_crc_deflate_filter_and_scanline_counts_are_independent_checks(tmp_path):
    original = png(b"\x00\xff\x00\x00\xff")
    crc_corrupt = original[:-1] + bytes([original[-1] ^ 1])
    header = original[:33]
    invalid_deflate = header + chunk(b"IDAT", b"not a zlib stream") + chunk(b"IEND", b"")
    cases = [crc_corrupt, invalid_deflate, png(b"\x05\xff\x00\x00\xff"),
             png(b"\x00\xff\x00\x00"), png(b"\x00\xff\x00\x00\xff\x00")]
    path = tmp_path / "owned.part"
    for data in cases:
        path.write_bytes(data)
        with pytest.raises(ImageValidationError):
            inspect_image(path)
        assert path.read_bytes() == data


def test_png_palette_and_adam7_rows_are_supported(tmp_path):
    path = tmp_path / "owned.part"
    path.write_bytes(png(b"\x00\x00", color=3, bits=1, before_data=chunk(b"PLTE", b"\xff\x00\x00")))
    assert inspect_image(path).width == 1
    red = b"\xff\x00\x00\xff"
    adam7 = b"\x00" + red + b"\x00" + red + b"\x00" + red * 2
    path.write_bytes(png(adam7, width=2, height=2, interlace=1))
    assert (inspect_image(path).width, inspect_image(path).height) == (2, 2)
    path.write_bytes(png(b"\x00\x00", color=3, bits=1))
    with pytest.raises(ImageValidationError, match="chunk_order"):
        inspect_image(path)


def test_unsupported_animation_is_retained_as_unverified(tmp_path):
    path = tmp_path / "owned.part"
    animated = PNG_BYTES[:33] + chunk(b"acTL", struct.pack(">II", 1, 0)) + PNG_BYTES[33:]
    path.write_bytes(animated)
    with pytest.raises(ImageNeedsVerification, match="apng"):
        inspect_image(path)
    assert path.read_bytes() == animated
    webp = bytearray(WEBP_OPAQUE_BYTES)
    webp[20] |= 2
    path.write_bytes(webp)
    with pytest.raises(ImageNeedsVerification, match="animation"):
        inspect_image(path)


def test_gif_animation_requires_each_frame_block_and_final_trailer(tmp_path):
    start = GIF_BYTES.index(b",")
    animated = GIF_BYTES[:start] + GIF_BYTES[start:-1] * 2 + b";"
    path = tmp_path / "owned.part"
    path.write_bytes(animated)
    assert inspect_image(path).frames == 2
    path.write_bytes(animated[:-2] + b";")
    with pytest.raises(ImageValidationError):
        inspect_image(path)


def test_size_mime_cancellation_and_work_limits(tmp_path):
    path = tmp_path / "owned.part"
    path.write_bytes(PNG_BYTES)
    for settings in ({"expected_size": len(PNG_BYTES) + 1}, {"expected_media_type": "text/html"}, {"max_chunks": 1}):
        with pytest.raises(ImageValidationError):
            inspect_image(path, **settings)
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(ImageValidationStopped):
        inspect_image(path, stop_event=stopped)
    assert path.read_bytes() == PNG_BYTES


def test_large_png_is_checked_without_allocating_a_pixel_buffer(tmp_path):
    path = tmp_path / "owned.part"
    path.write_bytes(png((b"\0" * 4097) * 1024, width=1024, height=1024))
    tracemalloc.start()
    try:
        evidence = inspect_image(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert evidence.width == evidence.height == 1024
    assert peak < 2 * 1024 * 1024
