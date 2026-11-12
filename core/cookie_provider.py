"""Read Pixiv cookies without requiring manual Cookie-header entry.

The browser export used by this project is the Netscape cookie-jar format.
This provider keeps parsing and credential selection out of the GUI.  It
filters cookies by host and expiry, caches parsed files by metadata, and only
returns a ready-to-use header (or redacted records); cookie values are never
included in diagnostics.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Callable, Iterable
from urllib.parse import urlparse

from core.app_paths import AppPaths, get_app_paths

DEFAULT_COOKIE_HOST = "www.pixiv.net"
COOKIE_FILE_NAME = "www.pixiv.net_cookies.txt"
_MALFORMED_EXPIRY = object()


@dataclass(frozen=True)
class CookieRecord:
    """A parsed Netscape cookie.

    ``value`` is intentionally excluded from the generated representation so
    that accidental debug logging cannot expose credentials.  Callers that
    need to build a request header may still access the field directly.
    """

    domain: str
    include_subdomains: bool
    path: str
    secure: bool
    expires: int | None
    name: str
    value: str = field(repr=False)
    http_only: bool = False
    _order: int = field(default=0, repr=False, compare=False)

    def __repr__(self) -> str:  # pragma: no cover - defensive convenience
        return (
            "CookieRecord("
            f"domain={self.domain!r}, include_subdomains={self.include_subdomains!r}, "
            f"path={self.path!r}, secure={self.secure!r}, expires={self.expires!r}, "
            f"name={self.name!r}, value='<redacted>', http_only={self.http_only!r})"
        )


# Friendly aliases for integrations that use the term ``CookieEntry``.
CookieEntry = CookieRecord


def _normalise_host(host: str) -> str:
    value = str(host or DEFAULT_COOKIE_HOST).strip().lower()
    if "://" in value:
        value = urlparse(value).hostname or ""
    else:
        value = value.split("/", 1)[0]
        # A bare host:port is accepted for convenience; IPv6 is not a Pixiv
        # cookie domain and therefore need not be special-cased here.
        if value.count(":") == 1:
            value = value.rsplit(":", 1)[0]
    return value.lstrip(".").rstrip(".")


def _normalise_domain(domain: str) -> str:
    return str(domain or "").strip().lower().lstrip(".").rstrip(".")


def _domain_matches(domain: str, host: str, include_subdomains: bool) -> bool:
    cookie_domain = _normalise_domain(domain)
    request_host = _normalise_host(host)
    if not cookie_domain or not request_host:
        return False
    if request_host == cookie_domain:
        return True
    return include_subdomains and request_host.endswith("." + cookie_domain)


def _parse_expiry(raw_value: str) -> int | None | object:
    """Parse a Netscape expiry; ``object()`` denotes a malformed value."""

    text = str(raw_value or "").strip()
    if text in {"", "0"}:
        return None
    try:
        value = int(text, 10)
    except (TypeError, ValueError):
        return _MALFORMED_EXPIRY
    if value < 0:
        return _MALFORMED_EXPIRY
    return value


def _contains_header_control(value: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def parse_netscape_cookie_lines(
    lines: Iterable[str],
    *,
    host: str = DEFAULT_COOKIE_HOST,
) -> tuple[CookieRecord, ...]:
    """Parse and host-filter Netscape cookie lines.

    Expiry is intentionally evaluated by :func:`active_cookie_records` so a
    parsed result can be safely cached and re-filtered as time advances.
    Malformed rows are ignored rather than aborting the whole export.
    """

    records: list[CookieRecord] = []
    for order, raw_line in enumerate(lines):
        line = raw_line.rstrip("\r\n")
        if not line or line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue

        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_") :]

        fields = line.split("\t", 6)
        if len(fields) != 7:
            continue

        domain, include_subdomains, path, secure, expiry_raw, name, value = fields
        name = name.strip()
        if not name or _contains_header_control(name) or _contains_header_control(value):
            continue

        expiry = _parse_expiry(expiry_raw)
        if expiry is _MALFORMED_EXPIRY:
            continue

        include_flag = include_subdomains.strip().upper() == "TRUE"
        path_value = path.strip() or "/"
        domain_value = _normalise_domain(domain)
        if not _domain_matches(domain_value, host, include_flag):
            continue

        records.append(
            CookieRecord(
                domain=domain_value,
                include_subdomains=include_flag,
                path=path_value,
                secure=secure.strip().upper() == "TRUE",
                expires=expiry,
                name=name,
                value=value,
                http_only=http_only,
                _order=order,
            )
        )
    return tuple(records)


def parse_netscape_cookie_file(
    path: str | Path,
    *,
    host: str = DEFAULT_COOKIE_HOST,
) -> tuple[CookieRecord, ...]:
    """Read and parse one Netscape cookie file without logging its contents."""

    try:
        with Path(path).open("r", encoding="utf-8-sig", errors="replace") as file_obj:
            return parse_netscape_cookie_lines(file_obj, host=host)
    except (OSError, UnicodeError):
        return ()


# Common spelling aliases retained for small scripts and future adapters.
parse_netscape_cookies = parse_netscape_cookie_file


def active_cookie_records(
    records: Iterable[CookieRecord],
    *,
    now: float | None = None,
) -> tuple[CookieRecord, ...]:
    """Return records that are still valid at ``now``.

    Expiry ``0`` in Netscape files represents a session cookie and is stored as
    ``None``; such cookies remain active for the lifetime of the provider.
    """

    current = time.time() if now is None else float(now)
    return tuple(record for record in records if record.expires is None or record.expires > current)


def _cookie_sort_key(record: CookieRecord) -> tuple[int, int, int]:
    # RFC cookie ordering prefers the most specific domain/path.  Reverse file
    # order makes a later duplicate win, matching browser export behaviour.
    return (-len(record.domain), -len(record.path), -record._order)


def build_cookie_header(records: Iterable[CookieRecord]) -> str:
    """Build a deterministic ``Cookie`` header without duplicate names."""

    selected_records: list[CookieRecord] = []
    seen_names: set[str] = set()
    for record in sorted(records, key=_cookie_sort_key):
        if record.name in seen_names:
            continue
        if _contains_header_control(record.name) or _contains_header_control(record.value):
            continue
        seen_names.add(record.name)
        selected_records.append(record)

    # Specificity determines which duplicate survives, while original export
    # order keeps otherwise unrelated cookies stable and predictable.
    selected_records.sort(key=lambda record: record._order)
    return "; ".join(f"{record.name}={record.value}" for record in selected_records)


def parse_legacy_cookie_header(
    value: str | None, *, host: str = DEFAULT_COOKIE_HOST
) -> tuple[CookieRecord, ...]:
    """Parse the legacy JSON ``cookie`` string into redacted records."""

    if not isinstance(value, str):
        return ()
    records: list[CookieRecord] = []
    for order, part in enumerate(value.split(";")):
        item = part.strip()
        if not item or "=" not in item:
            continue
        name, cookie_value = item.split("=", 1)
        name = name.strip()
        if not name or _contains_header_control(name) or _contains_header_control(cookie_value):
            continue
        records.append(
            CookieRecord(
                domain=_normalise_host(host),
                include_subdomains=True,
                path="/",
                secure=True,
                expires=None,
                name=name,
                value=cookie_value.strip(),
                _order=order,
            )
        )
    return tuple(records)


@dataclass(frozen=True)
class _FileSignature:
    mtime_ns: int
    size: int
    inode: int
    content_digest: bytes = field(repr=False)


@dataclass(frozen=True)
class _ParsedFile:
    signature: _FileSignature | None
    records: tuple[CookieRecord, ...]


def _signature(path: Path) -> _FileSignature | None:
    try:
        stat_result = path.stat()
        hasher = hashlib.blake2b(digest_size=16)
        with path.open("rb") as file_obj:
            for chunk in iter(lambda: file_obj.read(64 * 1024), b""):
                hasher.update(chunk)
    except OSError:
        return None
    return _FileSignature(
        mtime_ns=getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000)),
        size=stat_result.st_size,
        inode=getattr(stat_result, "st_ino", 0),
        content_digest=hasher.digest(),
    )


class CookieProvider:
    """Select and cache Pixiv cookies from the export or legacy config."""

    def __init__(
        self,
        project_root: str | Path | None = None,
        *,
        paths: AppPaths | None = None,
        cookie_path: str | Path | None = None,
        legacy_config_path: str | Path | None = None,
        host: str = DEFAULT_COOKIE_HOST,
        clock: Callable[[], float] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.paths = paths or get_app_paths(project_root)
        self.host = host
        self._clock = clock or time.time
        self._logger = logger
        # An explicit root is an isolation boundary (important for tests,
        # portable releases, and users with more than one profile).  Only the
        # implicit/default provider considers the current working directory as
        # a compatibility candidate.
        if cookie_path is not None:
            candidate_groups: tuple[Iterable[str | Path | None], ...] = (
                [cookie_path],
                (self.paths.cookie_file,),
            )
        elif paths is not None or project_root is not None:
            candidate_groups = ((self.paths.cookie_file,),)
        else:
            candidate_groups = (self.paths.cookie_candidates,)
        self._cookie_paths = self._unique_paths(*candidate_groups)
        self._legacy_config_path = (
            Path(legacy_config_path).expanduser().resolve()
            if legacy_config_path
            else self.paths.legacy_config_file
        )
        self._file_cache: dict[Path, _ParsedFile] = {}
        self._legacy_cache: _ParsedFile | None = None
        self._selected_path: Path | None = None
        self._selected_source: str = "none"
        self._lock = RLock()

    @staticmethod
    def _unique_paths(*groups: Iterable[str | Path | None]) -> tuple[Path, ...]:
        result: list[Path] = []
        seen: set[Path] = set()
        for group in groups:
            for candidate in group:
                if candidate is None:
                    continue
                path = Path(candidate).expanduser().resolve()
                if path not in seen:
                    seen.add(path)
                    result.append(path)
        return tuple(result)

    @property
    def source_path(self) -> Path | None:
        """Path used for the current result (never includes cookie values)."""

        return self._selected_path

    @property
    def source(self) -> str:
        """``netscape``, ``legacy`` or ``none`` for the current result."""

        return self._selected_source

    @property
    def cookie_path(self) -> Path | None:
        return self.source_path

    def invalidate(self) -> None:
        """Drop parsed metadata caches; the next read observes disk state."""
        with self._lock:
            self._file_cache.clear()
            self._legacy_cache = None
            self._selected_path = None
            self._selected_source = "none"

    refresh = invalidate

    def _cached_file(self, path: Path) -> tuple[CookieRecord, ...]:
        signature = _signature(path)
        cached = self._file_cache.get(path)
        if cached is not None and cached.signature == signature:
            return cached.records
        if signature is None:
            records: tuple[CookieRecord, ...] = ()
        else:
            records = parse_netscape_cookie_file(path, host=self.host)
        self._file_cache[path] = _ParsedFile(signature, records)
        return records

    def _cached_legacy(self) -> tuple[CookieRecord, ...]:
        path = self._legacy_config_path
        signature = _signature(path)
        if self._legacy_cache is not None and self._legacy_cache.signature == signature:
            return self._legacy_cache.records

        value: str | None = None
        if signature is not None:
            try:
                with path.open("r", encoding="utf-8-sig") as file_obj:
                    config = json.load(file_obj)
                if isinstance(config, dict):
                    value = config.get("cookie")
            except (OSError, UnicodeError, ValueError, TypeError):
                value = None
        records = parse_legacy_cookie_header(value, host=self.host)
        self._legacy_cache = _ParsedFile(signature, records)
        return records

    def _log_loaded(self, source: str, count: int, path: Path | None = None) -> None:
        logger = self._logger
        if logger is None:
            return
        # Never interpolate records, values, exception text, or the full file.
        location = path.name if path is not None else "legacy-config"
        logger.debug("Loaded %d active Pixiv cookies from %s (%s)", count, location, source)

    def get_records(self) -> tuple[CookieRecord, ...]:
        """Return active, host-filtered records (values redact in ``repr``)."""
        with self._lock:
            now = self._clock()
            for path in self._cookie_paths:
                records = active_cookie_records(self._cached_file(path), now=now)
                if records:
                    self._selected_path = path
                    self._selected_source = "netscape"
                    self._log_loaded("netscape", len(records), path)
                    return records

            records = active_cookie_records(self._cached_legacy(), now=now)
            self._selected_path = self._legacy_config_path if records else None
            self._selected_source = "legacy" if records else "none"
            self._log_loaded(self._selected_source, len(records), self._selected_path)
            return records

    def get_cookie_header(self) -> str:
        """Return the selected ``Cookie`` header, or ``""`` if unavailable."""

        return build_cookie_header(self.get_records())

    load_cookie_header = get_cookie_header

    @property
    def cookie_header(self) -> str:
        return self.get_cookie_header()


def load_cookie_header(
    project_root: str | Path | None = None,
    *,
    paths: AppPaths | None = None,
    cookie_path: str | Path | None = None,
    legacy_config_path: str | Path | None = None,
    host: str = DEFAULT_COOKIE_HOST,
) -> str:
    """One-shot convenience wrapper returning only a header string."""

    return CookieProvider(
        project_root,
        paths=paths,
        cookie_path=cookie_path,
        legacy_config_path=legacy_config_path,
        host=host,
    ).get_cookie_header()


get_cookie_header = load_cookie_header


__all__ = [
    "COOKIE_FILE_NAME",
    "CookieEntry",
    "CookieProvider",
    "CookieRecord",
    "DEFAULT_COOKIE_HOST",
    "active_cookie_records",
    "build_cookie_header",
    "get_cookie_header",
    "load_cookie_header",
    "parse_legacy_cookie_header",
    "parse_netscape_cookie_file",
    "parse_netscape_cookie_lines",
    "parse_netscape_cookies",
]
