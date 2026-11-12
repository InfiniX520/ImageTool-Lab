"""Bounded, lossless file inspection; this module never decodes pixel buffers.

Container references: W3C PNG 3 / GIF89a and Google's WebP RIFF specification.
JPEG checks segment framing, scans and EOI, not DCT/Huffman pixel correctness.
"""
from __future__ import annotations

import hashlib
import os
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

_BLOCK = 64 * 1024
_PNG = b"\x89PNG\r\n\x1a\n"


class ImageValidationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ImageNeedsVerification(ImageValidationError):
    """Bytes are retained, but this policy cannot confirm the whole format."""


class ImageValidationStopped(InterruptedError):
    pass


@dataclass(frozen=True, slots=True)
class ImageEvidence:
    media_type: str
    size: int
    sha256: str
    width: int
    height: int
    frames: int = 1
    verification: str = "structure_checked"


class _Reader:
    def __init__(self, stream, stop, max_chunks):
        self.stream, self.stop = stream, stop
        self.buffer, self.offset = b"", 0
        self.position, self.steps, self.max_chunks = 0, 0, max_chunks
        self.hasher = hashlib.sha256()

    def check(self):
        if self.stop is not None and self.stop.is_set():
            raise ImageValidationStopped()
        self.steps += 1
        if self.steps > self.max_chunks:
            raise ImageValidationError("structure_work_limit")

    def fill(self):
        if self.offset == len(self.buffer):
            self.check()
            self.buffer = self.stream.read(_BLOCK)
            self.offset = 0
            self.hasher.update(self.buffer)
        return self.offset < len(self.buffer)

    def take(self, amount):
        if not 0 <= amount <= _BLOCK:
            raise ImageValidationError("structure_block_limit")
        result = bytearray()
        while amount:
            if not self.fill():
                raise ImageValidationError("truncated_image")
            count = min(amount, len(self.buffer) - self.offset)
            result.extend(self.buffer[self.offset:self.offset + count])
            self.offset += count
            self.position += count
            amount -= count
        return bytes(result)

    def consume(self, amount, *, crc=None, consumer=None):
        while amount:
            data = self.take(min(amount, _BLOCK))
            if crc is not None:
                crc = zlib.crc32(data, crc)
            if consumer:
                consumer(data)
            amount -= len(data)
        return crc

    def eof(self):
        return not self.fill()

    def byte(self):
        return self.take(1)[0]

    def scan_marker(self):
        """Scan entropy chunks using bytes.find, avoiding a Python pixel loop."""
        encoded_bytes = 0
        while self.fill():
            index = self.buffer.find(b"\xff", self.offset)
            if index < 0:
                count = len(self.buffer) - self.offset
                encoded_bytes += count
                self.position += count
                self.offset = len(self.buffer)
                continue
            count = index - self.offset
            encoded_bytes += count
            self.position += count + 1
            self.offset = index + 1
            marker = self.byte()
            while marker == 0xFF:
                marker = self.byte()
            if marker == 0 or 0xD0 <= marker <= 0xD7:
                encoded_bytes += 1
                continue
            return marker, encoded_bytes
        raise ImageValidationError("missing_jpeg_eoi")


class _PngRows:
    def __init__(self, width, height, bits, channels, interlace):
        passes = ((0, 0, 1, 1),) if not interlace else (
            (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
            (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
        self.passes = []
        for x, y, dx, dy in passes:
            w, h = max(0, (width - x + dx - 1) // dx), max(0, (height - y + dy - 1) // dy)
            if w and h:
                self.passes.append((1 + (w * bits * channels + 7) // 8, h))
        self.expected = sum(length * rows for length, rows in self.passes)
        if self.expected > 1 << 30:
            raise ImageNeedsVerification("png_expansion_limit")
        self.pass_index, self.row_offset, self.rows_done, self.count = 0, 0, 0, 0

    def feed(self, data):
        self.count += len(data)
        if self.count > self.expected:
            raise ImageValidationError("png_extra_scanline_data")
        offset = 0
        while offset < len(data):
            length, rows = self.passes[self.pass_index]
            if self.row_offset == 0 and data[offset] > 4:
                raise ImageValidationError("png_invalid_filter")
            count = min(length - self.row_offset, len(data) - offset)
            self.row_offset += count
            offset += count
            if self.row_offset == length:
                self.row_offset = 0
                self.rows_done += 1
                if self.rows_done == rows:
                    self.pass_index += 1
                    self.rows_done = 0


def _png(reader, size):
    if reader.take(8) != _PNG:
        raise ImageValidationError("invalid_png_signature")
    width = height = color = bits = None
    seen_data, closed_data, seen_palette, chunks = False, False, False, 0
    inflater, rows = zlib.decompressobj(), None

    def compressed(data):
        pending = data
        try:
            while pending:
                reader.check()
                decoded = inflater.decompress(pending, _BLOCK)
                rows.feed(decoded)
                if inflater.unused_data:
                    raise ImageValidationError("png_extra_compressed_data")
                pending = inflater.unconsumed_tail
        except zlib.error as exc:
            raise ImageValidationError("png_invalid_deflate") from exc

    while True:
        reader.check()
        length, kind = struct.unpack(">I4s", reader.take(8))
        chunks += 1
        if length > 0x7FFFFFFF or length > size - reader.position - 4 or not re.fullmatch(b"[A-Za-z]{4}", kind) or kind[2] & 32:
            raise ImageValidationError("png_invalid_chunk")
        if chunks == 1 and kind != b"IHDR":
            raise ImageValidationError("png_missing_ihdr")
        crc = zlib.crc32(kind)
        if kind == b"IHDR":
            if chunks != 1 or length != 13:
                raise ImageValidationError("png_invalid_ihdr")
            data = reader.take(13)
            crc = zlib.crc32(data, crc)
            width, height, bits, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            valid_bits = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if not 0 < width < 1 << 31 or not 0 < height < 1 << 31 or bits not in valid_bits.get(color, set()) or compression or filtering or interlace not in {0, 1}:
                raise ImageValidationError("png_invalid_header")
            rows = _PngRows(width, height, bits, {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color], interlace)
        elif kind == b"PLTE":
            if seen_palette or seen_data or length == 0 or length > 768 or length % 3 or color in {0, 4} or (color == 3 and length // 3 > 1 << bits):
                raise ImageValidationError("png_invalid_palette")
            seen_palette = True
            crc = reader.consume(length, crc=crc)
        elif kind == b"IDAT":
            if closed_data or (color == 3 and not seen_palette):
                raise ImageValidationError("png_invalid_chunk_order")
            seen_data = True
            crc = reader.consume(length, crc=crc, consumer=compressed)
        else:
            if seen_data:
                closed_data = True
            if kind == b"acTL":
                raise ImageNeedsVerification("apng_frames_require_validation")
            if kind == b"IEND":
                if length or not seen_data or not inflater.eof or rows.count != rows.expected:
                    raise ImageValidationError("png_incomplete_image")
            elif kind[0] & 32 == 0:
                raise ImageNeedsVerification("png_unknown_critical_chunk")
            crc = reader.consume(length, crc=crc)
        if reader.take(4) != struct.pack(">I", crc & 0xFFFFFFFF):
            raise ImageValidationError("png_crc_mismatch")
        if kind == b"IEND":
            if not reader.eof():
                raise ImageValidationError("image_trailing_data")
            return width, height, 1


def _jpeg(reader, size):
    if reader.take(2) != b"\xff\xd8":
        raise ImageValidationError("invalid_jpeg_signature")
    frame, scans, entropy, pending = None, 0, 0, None
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while True:
        reader.check()
        if pending is None:
            if reader.byte() != 0xFF:
                raise ImageValidationError("jpeg_invalid_marker")
            marker = reader.byte()
            while marker == 0xFF:
                marker = reader.byte()
        else:
            marker, pending = pending, None
        if marker == 0xD9:
            if frame is None or scans == 0 or entropy == 0:
                raise ImageValidationError("jpeg_missing_image_data")
            if not reader.eof():
                raise ImageValidationError("image_trailing_data")
            return frame[0], frame[1], 1
        if marker == 0 or marker == 0xD8 or 0xD0 <= marker <= 0xD7:
            raise ImageValidationError("jpeg_invalid_marker")
        if marker == 1:
            continue
        length = struct.unpack(">H", reader.take(2))[0] - 2
        if length < 0 or length > size - reader.position:
            raise ImageValidationError("jpeg_invalid_segment_length")
        data = reader.take(length)
        if marker in sof:
            if frame is not None or len(data) < 6:
                raise ImageNeedsVerification("jpeg_unhandled_frame_structure")
            precision, height, width, components = struct.unpack(">BHHB", data[:6])
            if not width or not height or not 2 <= precision <= 16 or not components or len(data) != 6 + 3 * components:
                raise ImageValidationError("jpeg_invalid_frame")
            component_ids = {data[6 + i * 3] for i in range(components)}
            if len(component_ids) != components:
                raise ImageValidationError("jpeg_duplicate_component")
            frame = (width, height, components, component_ids)
        elif marker == 0xDA:
            if frame is None or not data or not 1 <= data[0] <= frame[2] or len(data) != 4 + 2 * data[0]:
                raise ImageValidationError("jpeg_invalid_scan_header")
            selected = {data[1 + 2 * n] for n in range(data[0])}
            if len(selected) != data[0] or not selected <= frame[3]:
                raise ImageValidationError("jpeg_invalid_scan_components")
            scans += 1
            pending, count = reader.scan_marker()
            if not count:
                raise ImageValidationError("jpeg_empty_scan")
            entropy += count


def _subblocks(reader):
    count = 0
    while True:
        reader.check()
        length = reader.byte()
        if not length:
            return count
        reader.consume(length)
        count += length


def _gif(reader, size):
    if reader.take(6) not in {b"GIF87a", b"GIF89a"}:
        raise ImageValidationError("invalid_gif_signature")
    width, height, packed, _, _ = struct.unpack("<HHBBB", reader.take(7))
    if not width or not height:
        raise ImageValidationError("gif_invalid_dimensions")
    palette = bool(packed & 128)
    if palette:
        reader.consume(3 * (1 << ((packed & 7) + 1)))
    frames = 0
    while True:
        reader.check()
        marker = reader.byte()
        if marker == 0x3B:
            if not frames:
                raise ImageValidationError("gif_missing_image_data")
            if not reader.eof():
                raise ImageValidationError("image_trailing_data")
            return width, height, frames
        if marker == 0x21:
            label = reader.byte()
            if label == 0xF9:
                if reader.byte() != 4:
                    raise ImageValidationError("gif_invalid_control_extension")
                reader.consume(4)
                if reader.byte() != 0:
                    raise ImageValidationError("gif_missing_block_terminator")
            else:
                _subblocks(reader)
            continue
        if marker != 0x2C:
            raise ImageValidationError("gif_invalid_block")
        left, top, w, h, flags = struct.unpack("<HHHHB", reader.take(9))
        if not w or not h or left + w > width or top + h > height:
            raise ImageValidationError("gif_invalid_frame")
        if flags & 128:
            reader.consume(3 * (1 << ((flags & 7) + 1)))
        elif not palette:
            raise ImageNeedsVerification("gif_external_palette")
        if not 2 <= reader.byte() <= 8 or not _subblocks(reader):
            raise ImageValidationError("gif_invalid_image_data")
        frames += 1


def _webp(reader, size):
    riff, declared, tag = struct.unpack("<4sI4s", reader.take(12))
    if riff != b"RIFF" or tag != b"WEBP" or declared + 8 != size or declared & 1:
        raise ImageValidationError("webp_invalid_riff_length")
    dimensions, canvas, seen_image, chunks = None, None, False, 0
    while reader.position < size:
        reader.check()
        kind, length = struct.unpack("<4sI", reader.take(8))
        chunks += 1
        if length + (length & 1) > size - reader.position:
            raise ImageValidationError("webp_invalid_chunk_length")
        prefix = reader.take(min(length, 10))
        reader.consume(length - len(prefix))
        if length & 1 and reader.byte() != 0:
            raise ImageValidationError("webp_invalid_padding")
        if kind == b"VP8X":
            if chunks != 1 or length != 10 or prefix[0] & 0xC1 or prefix[1:4] != b"\0\0\0":
                raise ImageValidationError("webp_invalid_extended_header")
            if prefix[0] & 2:
                raise ImageNeedsVerification("webp_animation_requires_validation")
            canvas = (int.from_bytes(prefix[4:7], "little") + 1, int.from_bytes(prefix[7:10], "little") + 1)
        elif kind in {b"VP8 ", b"VP8L"}:
            if seen_image:
                raise ImageValidationError("webp_duplicate_image")
            seen_image = True
            if kind == b"VP8 ":
                if length <= 10 or prefix[0] & 1 or prefix[3:6] != b"\x9d\x01\x2a":
                    raise ImageValidationError("webp_invalid_vp8_header")
                dimensions = (int.from_bytes(prefix[6:8], "little") & 0x3FFF, int.from_bytes(prefix[8:10], "little") & 0x3FFF)
            else:
                if length <= 5 or prefix[0] != 0x2F:
                    raise ImageValidationError("webp_invalid_vp8l_header")
                bits = int.from_bytes(prefix[1:5], "little")
                if bits >> 29:
                    raise ImageValidationError("webp_unknown_lossless_version")
                dimensions = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        elif kind in {b"ANIM", b"ANMF"}:
            raise ImageNeedsVerification("webp_animation_requires_validation")
    if not seen_image or not all(dimensions) or (canvas and canvas != dimensions) or not reader.eof():
        raise ImageValidationError("webp_missing_or_inconsistent_image")
    return dimensions[0], dimensions[1], 1


def inspect_image(path: str | Path, *, expected_size=None, expected_media_type=None,
                  stop_event=None, max_file_bytes=(1 << 32), max_chunks=1_000_000) -> ImageEvidence:
    """Inspect an owned staging file, preserving its bytes and bounding memory.

    ``structure_checked`` does not mean JPEG/GIF/WebP pixel streams were
    decoded. Unsupported animated PNG/WebP remain explicitly unverified.
    """
    if expected_size is not None and (type(expected_size) is not int or expected_size <= 0):
        raise ValueError("Expected size must be a positive integer")
    if type(max_file_bytes) is not int or max_file_bytes <= 0 or type(max_chunks) is not int or max_chunks <= 0:
        raise ValueError("Invalid validation limits")
    with Path(path).open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not 0 < before.st_size <= max_file_bytes:
            raise ImageNeedsVerification("image_size_limit")
        if expected_size is not None and before.st_size != expected_size:
            raise ImageValidationError("image_size_mismatch")
        reader = _Reader(stream, stop_event, max_chunks)
        reader.fill()
        prefix = reader.buffer[:12]
        if prefix.startswith(_PNG):
            media, parser = "image/png", _png
        elif prefix.startswith(b"\xff\xd8\xff"):
            media, parser = "image/jpeg", _jpeg
        elif prefix.startswith((b"GIF87a", b"GIF89a")):
            media, parser = "image/gif", _gif
        elif prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
            media, parser = "image/webp", _webp
        else:
            raise ImageValidationError("unsupported_image_content")
        aliases = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "image/x-png": "image/png", "image/apng": "image/png"}
        if expected_media_type:
            requested = expected_media_type.split(";", 1)[0].strip().lower()
            requested = aliases.get(requested, requested)
            if requested not in {media, "application/octet-stream", "binary/octet-stream", "application/binary"}:
                raise ImageValidationError("image_media_type_mismatch")
        width, height, frames = parser(reader, before.st_size)
        after = os.fstat(stream.fileno())
        if reader.position != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ImageValidationError("image_changed_during_validation")
        return ImageEvidence(media, reader.position, reader.hasher.hexdigest(), width, height, frames)
