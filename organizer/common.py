"""Validated business values and portable organizer-owned paths."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import stat
import time
from pathlib import Path


class OrganizerError(ValueError):
    def __init__(self, code):
        self.code = code if re.fullmatch(r'[a-z0-9_]{1,80}', str(code)) else 'organizer_error'
        super().__init__(self.code)


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(value):
    if type(value) not in (str, int) or not re.fullmatch(r'[1-9][0-9]{0,19}', str(value)):
        raise OrganizerError('invalid_identity')
    return str(value)


def integer(value, minimum=0, maximum=1000000):
    if type(value) is str and re.fullmatch(r'0|[1-9][0-9]{0,9}', value):
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        raise OrganizerError('invalid_integer')
    return value


def text(value, limit=500):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise OrganizerError('invalid_text')
    return value.strip()


def groups(value, maximum=5):
    if not isinstance(value, list) or len(value) > maximum:
        raise OrganizerError('invalid_groups')
    names = []
    for item in value:
        name = text(item, 20)
        try:
            if len(name.encode('utf-16-le')) // 2 > 20:
                raise OrganizerError('group_name_too_long')
        except UnicodeError:
            raise OrganizerError('invalid_group_name') from None
        names.append(name)
    if len(set(names)) != len(names):
        raise OrganizerError('duplicate_groups')
    return sorted(names)


def safe_path(root, relative):
    root = Path(root).absolute()
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise OrganizerError('invalid_relative_path')
    target = root / rel
    for part in [target, *target.parents]:
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise OrganizerError('linked_path_refused')
    if not target.resolve().is_relative_to(root.resolve()):
        raise OrganizerError('path_outside_data')
    return target


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise OrganizerError('input_too_large')
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise OrganizerError('invalid_json') from None
    if not isinstance(value, dict):
        raise OrganizerError('json_object_required')
    return value


class Budget:
    def __init__(self, requests=80, byte_limit=32 * 1024 * 1024, seconds=600, interval=0.5):
        self.limit = integer(requests, 1, 2000)
        self.byte_limit = integer(byte_limit, 1, 512 * 1024 * 1024)
        self.seconds = integer(seconds, 1, 7200)
        self.interval = max(0.5, float(interval))
        self.started = time.monotonic()
        self.last = 0.0
        self.reserved = self.observed = self.bytes = 0

    def check(self):
        if time.monotonic() - self.started >= self.seconds:
            raise OrganizerError('time_budget_exhausted')

    def reserve(self, count=1):
        self.check()
        if self.reserved + count > self.limit:
            raise OrganizerError('request_budget_exhausted')
        wait = self.interval - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.check()
        self.last = time.monotonic()
        self.reserved += count

    def consume(self, count):
        self.check()
        self.bytes += count
        if self.bytes > self.byte_limit:
            raise OrganizerError('byte_budget_exhausted')

    def summary(self):
        return {'reserved_requests': self.reserved, 'observed_requests': self.observed,
                'received_image_bytes': self.bytes, 'elapsed_seconds': round(time.monotonic() - self.started, 3)}
