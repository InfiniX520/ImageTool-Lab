"""Read-only preflight and file-set backups for the library database.

The download core validates every declared SQL object character by character
before it opens a library (LibraryRepository._inspect_existing), so an operator
needs one answer before switching builds: will this build accept this file, and
what does going back look like if it does not.  This module answers that
without writing to the inspected database and produces a backup a human can
restore by hand.

Safety rules this module keeps:

- inspect_library, verify_library_copy and switch_plan open the database
  through a 'file:...?mode=ro' URI.  They never execute a write PRAGMA (no
  wal_checkpoint, no journal_mode=, no VACUUM, no BEGIN IMMEDIATE) and they
  never create the file.  Opening a WAL database read-only still makes SQLite
  create its own bookkeeping beside it, an empty -wal and the -shm
  shared-memory index (measured).  Neither carries library content, both are
  rebuilt on the next open, and the database bytes never move.
- The declared schema is imported from core.library_repository.  A second copy
  of 49 statements would drift on the next schema change, and the drift would
  be invisible exactly where the check has to be exact.
- A backup has two modes.  'online_backup' is the default: it asks SQLite for
  a consistent snapshot through a read-only source connection, so a live app
  keeps writing while the snapshot stays consistent and uncommitted rows stay
  out.  'file_group' copies main, -wal and -shm byte for byte and therefore
  requires a quiet source.  Either way the manifest is written last and any
  failure removes the run's own directory, so a directory that contains a
  manifest is a directory that verified.

What "compatible" means here is what the code will actually do, not a tighter
rule of our own: extra objects are reported and tolerated (the repository only
iterates its own declaration), while a missing object, a one-character text
difference, a wrong format/version marker or a failed quick_check is not
compatible.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import sqlite3
import struct
import sys
import tempfile
import time
import uuid
from pathlib import Path

from core.library_repository import (
    LibraryError,
    _DDL,
    _LIBRARY_FORMAT,
    _REQUIRED_TABLES,
    _SCHEMA_VERSION,
    _validate_database_path,
)
from core.task_contracts import ContractError

MANIFEST_VERSION = 2
SUPPORTED_MANIFEST_VERSIONS = (1, 2)
MANIFEST_NAME = "manifest.json"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
FINGERPRINT_VERSION = 1
FINGERPRINT_CHUNK_ROWS = 1000
_CHUNK_BYTES = 1024 * 1024
_READ_TIMEOUT_SECONDS = 2.0
_BACKUP_SIBLINGS = ("", "-wal", "-shm")
_BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_MAX_COLLISION = 1000
_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
_OBJECT_RE = re.compile(
    r"\s*CREATE\s+(?:(UNIQUE)\s+)?(TABLE|INDEX)\s+(\w+)", re.IGNORECASE
)
_EXIT_BY_STATUS = {
    "compatible": 0,
    "missing": 0,
    "empty": 0,
    "incompatible": 2,
    "unreadable": 3,
}
_EXIT_REFUSED = 4
_EXIT_INCOMPLETE = 5
# Legacy rows only: a record that never carried a digest or a size cannot be
# verified by anyone, so --allow-unknown may skip exactly these reasons.  A
# malformed value stays a reason to report, never something to wave through.
_LEGACY_UNKNOWN_REASONS = frozenset({"no_recorded_size", "no_recorded_digest"})


class LibraryMaintenanceError(RuntimeError):
    """A preflight or backup that must never be reported as success."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else "%s: %s" % (code, detail))


class UnsupportedPathError(LibraryMaintenanceError):
    """A path this module refuses to open, copy from or write to."""


class BackupError(LibraryMaintenanceError):
    """A backup that could not be produced or verified."""


def _declared_objects() -> dict[str, tuple[str, str]]:
    """Map declared object name to (kind, normalised statement).

    The statements come from core.library_repository._DDL so this module and
    the open path can never disagree about the expected text.
    """
    declared: dict[str, tuple[str, str]] = {}
    for statement in _DDL:
        match = _OBJECT_RE.match(statement)
        if match is None:
            raise LibraryMaintenanceError("schema_declaration_unreadable")
        name = match.group(3)
        if name in declared:
            raise LibraryMaintenanceError("schema_declaration_duplicated", name)
        declared[name] = (match.group(2).upper(), " ".join(statement.split()))
    return declared


_DECLARED = _declared_objects()
_DECLARED_TABLES = tuple(
    name for name, (kind, _) in _DECLARED.items() if kind == "TABLE"
)


# ---------------------------------------------------------------------------
# Paths and connections
# ---------------------------------------------------------------------------


def _resolve_target(path: str | Path) -> Path:
    """Absolute, link-free path, using the same guard as the open path."""
    try:
        return _validate_database_path(path)
    except LibraryError as error:
        raise UnsupportedPathError("unsupported_path", str(error)) from None


def _resolve_destination(destination_dir: str | Path, source: Path) -> Path:
    raw = Path(destination_dir).expanduser()
    text = str(raw)
    if text.startswith("\\\\") or text.startswith("//"):
        raise UnsupportedPathError("unsupported_path", "UNC destinations are not supported")
    absolute = Path(os.path.abspath(text))
    for item in (absolute, *absolute.parents):
        if item.is_symlink() or (
            hasattr(item, "is_junction") and item.is_junction()
        ):
            raise UnsupportedPathError("unsupported_path", "Linked destinations are not supported")
    resolved = absolute.resolve()
    if resolved == source.parent or source.parent in resolved.parents:
        raise BackupError(
            "destination_inside_library",
            "the backup directory must live outside %s" % source.parent,
        )
    if resolved.exists() and not resolved.is_dir():
        raise BackupError("destination_not_a_directory", str(resolved))
    return resolved


def _connect_readonly(path: Path, *, snapshot: bool = True) -> sqlite3.Connection:
    """One read-only connection; SQLite may not create or write the file."""
    connection = sqlite3.connect(
        path.as_uri() + "?mode=ro",
        uri=True,
        timeout=_READ_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA busy_timeout=%d" % int(_READ_TIMEOUT_SECONDS * 1000))
        connection.execute("PRAGMA query_only=ON")
        # One read transaction keeps counters, objects and checks in a single
        # snapshot instead of three drifting reads.  The online backup API
        # needs a source connection with no open transaction, so it asks for
        # snapshot=False.
        if snapshot:
            connection.execute("BEGIN")
    except BaseException:
        connection.close()
        raise
    return connection


def _sibling_stats(path: Path) -> dict[str, dict[str, int] | None]:
    stats: dict[str, dict[str, int] | None] = {}
    for suffix in _BACKUP_SIBLINGS:
        candidate = Path(str(path) + suffix)
        try:
            info = candidate.stat()
        except FileNotFoundError:
            stats[suffix] = None
            continue
        except OSError as error:
            raise LibraryMaintenanceError("library_unavailable", str(error)) from None
        if not candidate.is_file():
            raise LibraryMaintenanceError("library_unavailable", str(candidate))
        stats[suffix] = {"bytes": info.st_size, "mtime_ns": info.st_mtime_ns}
    return stats


def _sqlite_reason(error: BaseException) -> str:
    text = str(error).lower()
    if "not a database" in text or "file is encrypted" in text:
        return "not_a_sqlite_file"
    if "locked" in text or "busy" in text:
        return "library_locked"
    if "unable to open" in text or "cannot open" in text:
        return "library_unavailable"
    return "library_unreadable"


def _is_internal_object(name: object) -> bool:
    """True for SQLite's own schema objects, judged by its literal prefix.

    SQLite reserves the "sqlite_" prefix and compares it case insensitively,
    but the underscore is an ordinary character.  A LIKE pattern would read it
    as a single-character wildcard, so a legal user table such as
    "sqliteXuser" would silently drop out of every digest and two databases
    with different content in it would compare equal (PA-02).  Every object
    enumeration in this module goes through this one predicate.
    """
    return str(name)[:7].lower() == "sqlite_"


# ---------------------------------------------------------------------------
# Reading facts
# ---------------------------------------------------------------------------


def _normalized_objects(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): " ".join(str(row[1]).split())
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table','index')"
        )
        if not _is_internal_object(row[0])
    }


def _all_ok(rows: list[str]) -> bool:
    return bool(rows) and all(str(row) == "ok" for row in rows)


def _facts(connection: sqlite3.Connection) -> dict[str, object]:
    """Read every fact the verdict needs; sqlite errors escape to the caller."""
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    facts: dict[str, object] = {"tables": tables, "warnings": []}
    meta: dict[str, str] = {}
    if "schema_meta" in tables:
        for row in connection.execute('SELECT "key", "value" FROM schema_meta'):
            key = str(row[0])
            if key in meta:
                facts["warnings"].append("duplicate_schema_marker")
                continue
            meta[key] = str(row[1])
    facts["format"] = meta.get("format")
    facts["schema_version"] = meta.get("schema_version")
    # A read-only query: no value is assigned, so no journal is converted.
    facts["journal_mode"] = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    if "schema_meta" not in tables:
        facts["warnings"].append("no_library_schema_marker")
    actual = _normalized_objects(connection)
    facts["objects_actual"] = len(actual)
    facts["missing_objects"] = sorted(set(_DECLARED) - set(actual))
    facts["extra_objects"] = sorted(set(actual) - set(_DECLARED))
    facts["mismatched_objects"] = sorted(
        name
        for name in set(_DECLARED) & set(actual)
        if _DECLARED[name][1] != actual[name]
    )
    facts["missing_required_tables"] = sorted(_REQUIRED_TABLES - tables)
    facts["quick_check"] = [
        str(row[0]) for row in connection.execute("PRAGMA quick_check")
    ]
    facts["integrity_check"] = [
        str(row[0]) for row in connection.execute("PRAGMA integrity_check")
    ]
    facts["foreign_key_violations"] = len(
        connection.execute("PRAGMA foreign_key_check").fetchall()
    )
    counts: dict[str, int | None] = {}
    for name in _DECLARED_TABLES:
        if name not in tables:
            continue
        try:
            counts[name] = int(
                connection.execute('SELECT COUNT(*) FROM "%s"' % name).fetchone()[0]
            )
        except sqlite3.Error:
            counts[name] = None
            facts["warnings"].append("counts_unavailable")
    facts["counts"] = counts
    return facts


def inspect_library(
    path: str | Path, *, connection: sqlite3.Connection | None = None
) -> dict[str, object]:
    """Read-only verdict on one library file.

    status is 'missing', 'empty', 'compatible', 'incompatible' or
    'unreadable'.  'healthy' covers the file's own integrity only, so an
    incompatible-but-intact database still reports healthy true and a corrupted
    one reports a reason instead of a plain verdict.

    'connection' lets a caller that already holds one read transaction ask for
    the health facts of that same snapshot; the default opens a read-only
    connection of its own, which is what a plain inspect wants.
    """
    target = _resolve_target(path)
    report: dict[str, object] = {
        "path": str(target),
        "status": "missing",
        "format": None,
        "schema_version": None,
        "journal_mode": None,
        "objects_declared": len(_DECLARED),
        "objects_actual": 0,
        "missing_objects": [],
        "extra_objects": [],
        "mismatched_objects": [],
        "missing_required_tables": [],
        "quick_check": [],
        "integrity_check": [],
        "foreign_key_violations": 0,
        "counts": {},
        "size_bytes": 0,
        "wal_bytes": 0,
        "shm_bytes": 0,
        "healthy": False,
        "reasons": [],
        "warnings": [],
    }
    siblings = _sibling_stats(target)
    for key, suffix in (("size_bytes", ""), ("wal_bytes", "-wal"), ("shm_bytes", "-shm")):
        entry = siblings[suffix]
        report[key] = 0 if entry is None else int(entry["bytes"])
    if siblings[""] is None:
        report["warnings"].append("library_missing")
        return report
    if report["size_bytes"] == 0:
        report["status"] = "empty"
        report["warnings"].append("empty_file")
        return report
    try:
        if connection is not None:
            facts = _facts(connection)
        else:
            with contextlib.closing(_connect_readonly(target)) as opened:
                facts = _facts(opened)
    except sqlite3.Error as error:
        report["status"] = "unreadable"
        report["warnings"].append(_sqlite_reason(error))
        return report
    report.update(facts)
    tables = report.pop("tables", set())
    if not (set(tables) - {"sqlite_sequence"}):
        # The open path treats a database without tables of its own as a new
        # library and initialises it, so the preflight has to say the same
        # instead of calling an empty file incompatible.
        report["status"] = "empty"
        report["warnings"] = sorted(
            {str(item) for item in report["warnings"]} | {"sqlite_file_without_tables"}
        )
        report["healthy"] = True
        return report
    report["warnings"] = sorted({str(item) for item in report["warnings"]})
    report["healthy"] = (
        _all_ok(report["quick_check"])
        and _all_ok(report["integrity_check"])
        and int(report["foreign_key_violations"]) == 0
    )
    reasons: list[str] = []
    if report["format"] != _LIBRARY_FORMAT:
        reasons.append("format_mismatch")
    if report["schema_version"] != _SCHEMA_VERSION:
        reasons.append("schema_version_mismatch")
    if report["missing_required_tables"]:
        reasons.append("missing_required_tables")
    if report["missing_objects"]:
        reasons.append("missing_objects")
    if report["mismatched_objects"]:
        reasons.append("mismatched_objects")
    if int(report["foreign_key_violations"]):
        reasons.append("foreign_key_violations")
    if not _all_ok(report["quick_check"]):
        reasons.append("quick_check_failed")
    if not _all_ok(report["integrity_check"]):
        reasons.append("integrity_check_failed")
    if report["extra_objects"]:
        report["warnings"] = sorted(
            set(report["warnings"]) | {"extra_objects", "tolerated_by_open_path"}
        )
    report["reasons"] = reasons
    report["status"] = "compatible" if not reasons else "incompatible"
    report.pop("tables", None)
    return report


# ---------------------------------------------------------------------------
# Source snapshots and file copies
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _file_fact(path: Path) -> dict[str, object]:
    """Stat one group file, and hash it when the bytes are readable.

    A live writer may hold the WAL open exclusively on Windows, so hashing is
    best effort: a permission error is reported as an unreadable hash instead
    of escaping.  The file-group path treats that as proof the source is busy.
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return {"name": path.name, "present": False, "bytes": 0, "mtime_ns": 0,
                "sha256": None}
    except OSError as error:
        raise LibraryMaintenanceError("library_unavailable", str(error)) from None
    fact: dict[str, object] = {
        "name": path.name,
        "present": True,
        "bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "sha256": None,
    }
    try:
        fact["sha256"] = _sha256_file(path)
    except OSError:
        fact["sha256"] = None
    return fact


def _group(path: Path) -> dict[str, dict[str, object]]:
    return {suffix: _file_fact(Path(str(path) + suffix)) for suffix in _BACKUP_SIBLINGS}


def _comparable(group: dict[str, dict[str, object]]) -> dict[str, object]:
    """What must not move: bytes and mtime of the database and its WAL.

    Two files are deliberately outside the comparison, both measured rather
    than assumed.  The -shm shared-memory index is excluded because SQLite
    rewrites its header and lock bytes whenever any connection opens or closes
    and rebuilds it from the WAL.  An empty -wal is treated like an absent one
    because a reader creates it and the last close can remove it again; it
    carries no frames, so it cannot hold a fact the copy would miss.  A WAL
    with content is compared by byte count and mtime.
    """
    comparable: dict[str, object] = {}
    for suffix in ("", "-wal"):
        entry = group[suffix]
        size = int(entry["bytes"])
        comparable[suffix] = (
            None if (not entry["present"] or size == 0)
            else (size, entry["mtime_ns"])
        )
    return comparable


def _copy_file(source: Path, destination: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as reader, destination.open("xb") as writer:
        while True:
            chunk = reader.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            writer.write(chunk)
            total += len(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    return {"name": destination.name, "bytes": total, "sha256": digest.hexdigest()}


def _discard(directory: Path) -> None:
    """Remove only what this run created; never follow a link out of it."""
    for entry in os.scandir(directory):
        if entry.is_dir(follow_symlinks=False):
            _discard(Path(entry.path))
        else:
            os.unlink(entry.path)
    os.rmdir(directory)


def _backup_name(label: str | None, destination_root: Path) -> Path:
    if label is not None:
        if not isinstance(label, str) or _BACKUP_NAME_RE.fullmatch(label) is None:
            raise BackupError(
                "invalid_label", "use 1-63 letters, digits, dot, dash or underscore"
            )
        target = destination_root / label
        if target.exists():
            raise BackupError("backup_target_exists", str(target))
        return target
    stem = "library-backup-" + datetime.datetime.now().strftime(_TIMESTAMP_FORMAT)
    for index in range(_MAX_COLLISION):
        candidate = destination_root / (
            stem if index == 0 else "%s-%d" % (stem, index + 1)
        )
        if not candidate.exists():
            return candidate
    raise BackupError("backup_target_exists", stem)


def _source_counts(path: Path) -> dict[str, int] | None:
    try:
        with contextlib.closing(_connect_readonly(path)) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            counts: dict[str, int] = {}
            for name in _DECLARED_TABLES:
                if name in tables:
                    counts[name] = int(
                        connection.execute(
                            'SELECT COUNT(*) FROM "%s"' % name
                        ).fetchone()[0]
                    )
            return counts
    except (sqlite3.Error, OSError):
        return None


def _write_manifest(directory: Path, manifest: dict[str, object]) -> Path:
    data = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = directory / (MANIFEST_NAME + ".tmp")
    final = directory / MANIFEST_NAME
    with temporary.open("wb") as stream:
        stream.write(data.encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, final)
    return final


def _restore_steps(
    source: Path, name: str, mode: str, files: list[dict[str, object]]
) -> list[str]:
    """Restore instructions that always keep a way back.

    The previous text told the operator to overwrite the database and then
    delete the -wal/-shm beside it.  That destroys the only copy of whatever
    the old WAL still held before anyone proved the replacement opens, and it
    cannot be undone.  This protocol moves the current group aside first, then
    puts a verified group in its place, so every step is reversible (WR-01).
    """
    group = ", ".join(str(item["name"]) for item in files)
    if mode == "file_group":
        prepared = "整套文件（%s）" % group
        placement = (
            "file_group 备份必须整套一起放回；禁止只放主文件，也禁止用新主库配旧 WAL。"
        )
    else:
        prepared = "自包含的 %s（online 快照）" % name
        placement = "online 快照是自包含单文件，放回后旁边不得再留 -wal/-shm。"
    return [
        "1. 关闭 JpgPng 主程序与所有辅助进程，确认没有进程或连接还持有 %s；"
        "maintain.py inspect 报 library_locked 就说明还没关干净，不要继续。" % name,
        "2. 先校验备份本身：python maintain.py verify <备份目录>，必须 ok=true；"
        "记下清单里的模式（%s）与成员（%s）。" % (mode, group),
        "3. 在暂存目录准备与备份模式匹配的恢复组：%s；对暂存副本运行 inspect 与 compare，"
        "确认可用后才进入下一步。" % prepared,
        "4. 把当前库文件组（%s 以及同目录的 -wal/-shm）整组移入同卷独立保留目录"
        "（例如 record_backups/restore_rollback_<时间>）：移动，不删除、不覆盖。" % name,
        "5. 只把第 3 步校验过的恢复组放进原位置：%s" % placement,
        "6. 重新打开并对照：maintain.py inspect %s 须 compatible；"
        "maintain.py compare <恢复后库> <备份里的库> 须内容一致；"
        "restore_check 须无 orphan_wal。" % source,
        "7. 任一步失败：把第 4 步保留的整组移回原位置，保留保留目录与报告，不删除现场。",
    ]


def _journal_header(path: Path) -> tuple[int, int] | None:
    """The two journal-format bytes of a database header (offsets 18 and 19).

    1 is the legacy rollback journal, 2 is write-ahead logging.  This is a
    fact about the file itself; PRAGMA journal_mode reports "wal" as soon as a
    -wal sits beside a database SQLite can open with one, which is exactly the
    mixed group the restore check has to refuse.
    """
    try:
        with path.open("rb") as stream:
            header = stream.read(20)
    except OSError:
        return None
    if len(header) < 20 or header[:15] != b"SQLite format 3":
        return None
    return (header[18], header[19])


def restore_check(path: str | Path) -> dict[str, object]:
    """Is this file group one consistent state that may be opened?

    A rollback-journal database with a non-empty -wal beside it is a mixed
    group: SQLite ignores that log for the current content, so the files on
    disk no longer describe one state, and the next writer would inherit a log
    that belongs to another file.  A restore must never leave one behind, so
    the check reports it instead of guessing which side is right.
    """
    target = _resolve_target(path)
    report = inspect_library(target)
    wal = Path(str(target) + "-wal")
    wal_bytes = 0
    try:
        if wal.is_file():
            wal_bytes = int(wal.stat().st_size)
    except OSError:
        wal_bytes = 0
    header = _journal_header(target)
    wal_mode = header == (2, 2)
    reasons: list[str] = []
    if report["status"] == "missing":
        state = "library_missing"
        reasons.append("library_missing")
    elif not wal_mode and wal_bytes:
        state = "orphan_wal"
        reasons.append("orphan_wal")
    else:
        state = "ok"
    return {
        "path": str(target),
        "state": state,
        "status": report["status"],
        "journal_mode": report["journal_mode"],
        "wal_mode": wal_mode,
        "header_journal": list(header) if header is not None else None,
        "wal_bytes": wal_bytes,
        "reasons": reasons,
    }


def _online_snapshot(source: Path, target: Path) -> list[dict[str, object]]:
    """Consistent snapshot through the SQLite online backup API.

    The source connection is read-only and holds no transaction, so the API
    reads the last committed state while other connections keep writing: a
    writer's uncommitted rows are not part of the snapshot, and a source that
    is mid-commit still yields one consistent state.  The source is never
    checkpointed, journalled or written.
    """
    destination = sqlite3.connect(
        str(target), timeout=_READ_TIMEOUT_SECONDS, isolation_level=None
    )
    try:
        with contextlib.closing(_connect_readonly(source, snapshot=False)) as origin:
            origin.backup(destination)
        # Pages are copied verbatim, so the snapshot inherits the source's
        # journal header.  A restored backup must never depend on a -wal that
        # belongs to a different file, so the snapshot is normalised into one
        # self-contained rollback-journal database.
        destination.execute("PRAGMA journal_mode=DELETE")
    finally:
        destination.close()
    leftovers = [
        suffix
        for suffix in ("-wal", "-shm")
        if Path(str(target) + suffix).exists()
        and Path(str(target) + suffix).stat().st_size > 0
    ]
    if leftovers:
        raise BackupError("snapshot_not_self_contained", ",".join(leftovers))
    return [_file_fact(target)]


def _file_group_copy(
    source: Path, target: Path, group: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    """Byte-for-byte copy of the database file set; needs a quiet source."""
    for suffix in ("", "-wal"):
        entry = group[suffix]
        if entry["present"] and int(entry["bytes"]) and entry["sha256"] is None:
            raise BackupError(
                "source_busy",
                "the source %s cannot be read whole; close the application first"
                % entry["name"],
            )
    files: list[dict[str, object]] = []
    for suffix in _BACKUP_SIBLINGS:
        entry = group[suffix]
        if not entry["present"]:
            continue
        try:
            files.append(
                _copy_file(Path(str(source) + suffix), target / (source.name + suffix))
            )
        except FileNotFoundError:
            if suffix == "-shm":
                # The shared-memory index is rebuilt from the WAL on the next
                # open; it is not library content and already sits outside the
                # comparison, so its disappearance loses nothing.
                continue
            if suffix == "-wal" and int(entry["bytes"]) == 0:
                # An empty -wal can disappear when the reader that created it
                # closes.  It carried no frames, so there is nothing to lose.
                continue
            # A WAL that held frames, or the database itself, cannot vanish
            # without the source having moved; no copy may vouch for that.
            raise
    return files


def backup_library(
    path: str | Path,
    destination_dir: str | Path,
    *,
    label: str | None = None,
    mode: str = "auto",
) -> dict[str, object]:
    """Copy the library into a fresh, verified directory.

    Two modes exist because they answer different questions:

    'online_backup' (the default through 'auto') asks SQLite for a consistent
    snapshot.  It is the only safe choice while the application is running:
    a writer's uncommitted rows stay out, and no byte of the source is
    touched.  The result is one self-contained file.

    'file_group' copies main, -wal and -shm byte for byte.  It preserves the
    source exactly, including a pending WAL, and therefore requires a quiet
    source: the caller must close the application.  'auto' picks it only when
    the source is not healthy enough for the online API.

    The manifest is written last, so its presence proves the copy verified.
    Any failure removes the run's own directory; a failed backup never looks
    like a successful one.
    """
    if mode not in {"auto", "online_backup", "file_group"}:
        raise BackupError("invalid_mode", mode)
    source = _resolve_target(path)
    destination_root = _resolve_destination(destination_dir, source)
    siblings = _sibling_stats(source)
    if siblings[""] is None:
        raise BackupError("library_missing", str(source))
    if int(siblings[""]["bytes"]) == 0:
        raise BackupError("library_empty", str(source))
    source_report = inspect_library(source)
    if source_report["status"] == "unreadable":
        # A database this module cannot read cannot be verified either, so it
        # refuses instead of producing a manifest nobody can check.  A plain
        # file copy is the honest tool for that case.
        raise BackupError(
            "source_unreadable",
            "copy %s with the file manager; this tool cannot verify it" % source,
        )
    effective = mode
    if mode == "auto":
        effective = "online_backup" if source_report["healthy"] else "file_group"
    try:
        destination_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BackupError("destination_unavailable", str(error)) from None
    target = _backup_name(label, destination_root)
    before = _group(source)
    before_counts = _source_counts(source)
    try:
        target.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        raise BackupError("backup_target_exists", str(target)) from None
    except OSError as error:
        raise BackupError("backup_directory_failed", str(error)) from None
    copy_path = target / source.name
    try:
        if effective == "online_backup":
            files = _online_snapshot(source, copy_path)
        else:
            files = _file_group_copy(source, target, before)
    except BaseException as error:
        _discard(target)
        if isinstance(error, LibraryMaintenanceError):
            raise
        if not isinstance(error, Exception):
            # A real interrupt keeps its own type after the cleanup; turning it
            # into a BackupError would look like a failed backup to a caller
            # that is trying to stop.
            raise
        raise BackupError(
            "backup_failed", "%s: %s" % (type(error).__name__, error)
        ) from None
    after = _group(source)
    after_counts = _source_counts(source)
    copy_report = inspect_library(copy_path)
    # Compare the two sides with each other, not each side against the shared
    # declaration: two databases can deviate from it in different ways and
    # still look identical to a name-set comparison (WC-01).
    source_definitions = _definitions_or_none(source, source_report["status"])
    copy_definitions = _definitions_or_none(copy_path, copy_report["status"])
    objects_match = (
        source_definitions is not None and source_definitions == copy_definitions
    )
    counts_match = (
        before_counts is not None and copy_report["counts"] == before_counts
    )
    main_unchanged = (
        before[""]["bytes"] == after[""]["bytes"]
        and before[""]["mtime_ns"] == after[""]["mtime_ns"]
        and before[""]["sha256"] == after[""]["sha256"]
    )
    group_unchanged = _comparable(before) == _comparable(after)
    warnings: list[str] = []
    problems: list[str] = []
    checks_match = (
        copy_report["quick_check"] == source_report["quick_check"]
        and copy_report["integrity_check"] == source_report["integrity_check"]
        and copy_report["foreign_key_violations"]
        == source_report["foreign_key_violations"]
    )
    if copy_report["status"] in {"missing", "empty", "unreadable"}:
        problems.append("copy_unreadable")
    if not objects_match:
        problems.append("copy_objects_mismatch")
    if not checks_match:
        # A faithful copy reproduces the source's integrity facts exactly.  A
        # damaged source therefore still gets a usable backup; what is refused
        # is a copy whose checks disagree with the source it came from.
        problems.append("copy_integrity_differs")
    if not main_unchanged:
        # Only a concurrent checkpoint by another connection can move the main
        # file while we read it; the online API has no way to write the source.
        if effective == "file_group":
            problems.append("library_changed_during_copy")
        else:
            warnings.append("source_changed_during_backup")
    if not group_unchanged and effective == "file_group":
        problems.append("library_changed_during_copy")
    if not counts_match:
        if effective == "file_group":
            problems.append("counts_mismatch")
        else:
            warnings.append("counts_advanced_during_backup")
    for item in files:
        # -shm is SQLite bookkeeping rebuilt on the next open; every other
        # member carries content, so it is sealed byte for byte.
        item["sealed"] = not str(item["name"]).endswith("-shm")
    try:
        fingerprint = logical_fingerprint(copy_path)
    except LibraryMaintenanceError as error:
        fingerprint = None
        problems.append("copy_logical_unreadable")
        warnings.append(getattr(error, "code", "library_unreadable"))
    if problems:
        _discard(target)
        raise BackupError("backup_verification_failed", ",".join(sorted(set(problems))))
    manifest: dict[str, object] = {
        "manifest_version": MANIFEST_VERSION,
        "status": "ok",
        "mode": effective,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "label": label,
        "source": str(source),
        "library_name": source.name,
        "source_bytes": int(siblings[""]["bytes"]),
        "source_wal_bytes": 0
        if siblings["-wal"] is None
        else int(siblings["-wal"]["bytes"]),
        "source_shm_bytes": 0
        if siblings["-shm"] is None
        else int(siblings["-shm"]["bytes"]),
        "source_mtime_ns": int(siblings[""]["mtime_ns"]),
        "journal_mode_source": source_report["journal_mode"],
        "journal_mode_copy": copy_report["journal_mode"],
        "files": files,
        "members": [
            {
                "name": str(item["name"]),
                "bytes": int(item["bytes"]),
                "sha256": item["sha256"],
            }
            for item in files
        ],
        "sealed_members": [
            str(item["name"]) for item in files if item.get("sealed")
        ],
        "logical_fingerprint": fingerprint,
        "copy_name": source.name,
        "copy_bytes": sum(int(item["bytes"]) for item in files),
        "objects_declared": copy_report["objects_declared"],
        "objects_actual": copy_report["objects_actual"],
        "missing_objects": copy_report["missing_objects"],
        "extra_objects": copy_report["extra_objects"],
        "mismatched_objects": copy_report["mismatched_objects"],
        "counts": copy_report["counts"],
        "quick_check": copy_report["quick_check"],
        "integrity_check": copy_report["integrity_check"],
        "foreign_key_violations": copy_report["foreign_key_violations"],
        "healthy": bool(copy_report["healthy"]),
        "verified": True,
        "objects_match_source": bool(objects_match),
        "counts_match_source": bool(counts_match),
        "source_checks": {
            "main_unchanged": bool(main_unchanged),
            "group_unchanged": bool(group_unchanged),
            "main_sha256_before": before[""]["sha256"],
            "main_sha256_after": after[""]["sha256"],
            "wal_bytes_before": before["-wal"]["bytes"],
            "wal_bytes_after": after["-wal"]["bytes"],
            "shm_excluded": True,
        },
        "reasons": [],
        "warnings": sorted(set(warnings)),
        "finalizer": {
            "format": MANIFEST_NAME,
            "written_at": _now_utc(),
            "sealed_by": "backup_library",
        },
        "restore_steps": _restore_steps(source, source.name, effective, files),
    }
    manifest["manifest_path"] = str(_write_manifest(target, manifest))
    return manifest


def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# Logical content fingerprint (contract v5 section 1.3)
# ---------------------------------------------------------------------------

_TAG_NULL = b"\x00"
_TAG_INTEGER = b"\x01"
_TAG_REAL = b"\x02"
_TAG_TEXT = b"\x03"
_TAG_BLOB = b"\x04"


def _quote_identifier(name: object) -> str:
    return '"%s"' % str(name).replace('"', '""')


def _quote_literal(name: object) -> str:
    return "'%s'" % str(name).replace("'", "''")


def _length_prefix(payload: bytes) -> bytes:
    return len(payload).to_bytes(8, "big") + payload


def _encode_value(value: object) -> bytes:
    """Type-preserving encoding: NULL, '' and b'' must never collide."""
    if value is None:
        return _TAG_NULL + _length_prefix(b"")
    if isinstance(value, bool):
        # SQLite has no boolean storage class; a bool never comes back from a
        # cursor, so encode it as the integer it means instead of guessing.
        value = int(value)
    if isinstance(value, int):
        try:
            payload = int(value).to_bytes(8, "big", signed=True)
        except OverflowError:
            raise LibraryMaintenanceError("integer_out_of_range") from None
        return _TAG_INTEGER + _length_prefix(payload)
    if isinstance(value, float):
        return _TAG_REAL + _length_prefix(struct.pack(">d", float(value)))
    if isinstance(value, str):
        return _TAG_TEXT + _length_prefix(value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _TAG_BLOB + _length_prefix(bytes(value))
    raise LibraryMaintenanceError("unsupported_value_type", type(value).__name__)


def _object_rows(connection: sqlite3.Connection) -> list[tuple[str, str, str, str | None]]:
    """Every non-internal schema object, in a deterministic order.

    The internal-name test is applied here rather than in SQL, so tables,
    views, triggers and indexes all use the same definition (PA-02).
    """
    return [
        (str(row[0]), str(row[1]), str(row[2]), None if row[3] is None else str(row[3]))
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        )
        if not _is_internal_object(row[1])
    ]


def _objects_digest(connection: sqlite3.Connection) -> tuple[int, str]:
    """Fingerprint the schema definitions, triggers and views included."""
    digest = hashlib.sha256()
    rows = _object_rows(connection)
    for kind, name, table, sql in rows:
        for part in (kind, name, table, sql):
            digest.update(_encode_value(part))
    return len(rows), digest.hexdigest()


def _object_definitions(connection: sqlite3.Connection) -> dict[str, dict[str, str | None]]:
    """Name to full definition, so two sides are compared with each other."""
    return {
        name: {"type": kind, "table": table, "sql": sql}
        for kind, name, table, sql in _object_rows(connection)
    }


def _table_columns(connection: sqlite3.Connection, name: str) -> list[tuple[str, int]]:
    return [
        (str(row[1]), int(row[5]))
        for row in connection.execute(
            "PRAGMA table_info(%s)" % _quote_literal(name)
        )
    ]


def _has_rowid(connection: sqlite3.Connection, name: str) -> bool:
    try:
        connection.execute(
            "SELECT rowid FROM %s LIMIT 0" % _quote_identifier(name)
        ).fetchall()
    except sqlite3.Error:
        return False
    return True


def _table_order(connection: sqlite3.Connection, name: str) -> tuple[str, str]:
    """Deterministic row order for one table, named for the record."""
    if _has_rowid(connection, name):
        return "rowid", "ORDER BY rowid"
    columns = _table_columns(connection, name)
    primary = [column for column, position in columns if position > 0]
    if primary:
        return "primary_key", "ORDER BY " + ", ".join(
            _quote_identifier(column) for column in primary
        )
    return "all_columns", "ORDER BY " + ", ".join(
        _quote_identifier(column) for column, _ in columns
    )


def _table_digest(
    connection: sqlite3.Connection, name: str, order_sql: str
) -> tuple[int, str]:
    """Stream one table in bounded batches; never fetch the whole table."""
    cursor = connection.execute(
        "SELECT * FROM %s %s" % (_quote_identifier(name), order_sql)
    )
    digest = hashlib.sha256()
    rows = 0
    while True:
        batch = cursor.fetchmany(FINGERPRINT_CHUNK_ROWS)
        if not batch:
            break
        for record in batch:
            rows += 1
            for value in record:
                digest.update(_encode_value(value))
    return rows, digest.hexdigest()


def _fingerprint_from_connection(connection: sqlite3.Connection) -> dict[str, object]:
    objects_count, objects_digest = _objects_digest(connection)
    tables: dict[str, dict[str, object]] = {}
    ordered: list[tuple[str, int, str]] = []
    for kind, name, _table, _sql in _object_rows(connection):
        if kind != "table":
            continue
        order, order_sql = _table_order(connection, name)
        rows, digest = _table_digest(connection, name, order_sql)
        tables[name] = {"rows": rows, "digest": digest, "order": order}
        ordered.append((name, rows, digest))
    combined = hashlib.sha256()
    combined.update(_encode_value("fp%d" % FINGERPRINT_VERSION))
    for name, rows, digest in sorted(ordered):
        combined.update(_encode_value(name))
        combined.update(_encode_value(rows))
        combined.update(_encode_value(digest))
    combined.update(_encode_value(objects_digest))
    return {
        "version": FINGERPRINT_VERSION,
        "digest": combined.hexdigest(),
        "rows": {name: int(item["rows"]) for name, item in sorted(tables.items())},
        "objects_digest": objects_digest,
        "objects_count": objects_count,
        "computed_at": _now_utc(),
        "tables": tables,
    }


def logical_fingerprint(
    path: str | Path, *, connection: sqlite3.Connection | None = None
) -> dict[str, object]:
    """Full logical content of one database, readable by any SQLite build.

    The digest covers the object definitions and, for every table, the typed
    encoding of every value in a deterministic row order.  Two databases that
    hold the same content therefore produce the same digest whatever journal
    mode, page layout or file size they ended up with.
    """
    if connection is not None:
        return _fingerprint_from_connection(connection)
    target = _resolve_target(path)
    try:
        with contextlib.closing(_connect_readonly(target)) as opened:
            return _fingerprint_from_connection(opened)
    except sqlite3.Error as error:
        raise LibraryMaintenanceError(_sqlite_reason(error), str(error)) from None


def _definitions(path: Path) -> dict[str, dict[str, str | None]]:
    with contextlib.closing(_connect_readonly(path)) as opened:
        return _object_definitions(opened)


def _definitions_or_none(
    path: Path, status: object
) -> dict[str, dict[str, str | None]] | None:
    """Definitions of a readable database, or None instead of an exception."""
    if status not in {"compatible", "incompatible"}:
        return None
    try:
        return _definitions(path)
    except (sqlite3.Error, LibraryMaintenanceError, OSError):
        return None


# ---------------------------------------------------------------------------
# Manifest reading and sealing checks (contract v5 section 1.4)
# ---------------------------------------------------------------------------


def _load_manifest(directory: Path) -> dict[str, object]:
    path = directory / MANIFEST_NAME
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise BackupError("manifest_missing", str(path)) from None
    except OSError as error:
        raise BackupError("manifest_unreadable", str(error)) from None
    if size > MAX_MANIFEST_BYTES:
        raise BackupError("manifest_too_large", str(path))
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise BackupError("manifest_unreadable", str(path)) from None
    if not isinstance(manifest, dict):
        raise BackupError("manifest_unreadable", str(path))
    version = manifest.get("manifest_version")
    if type(version) is not int or version not in SUPPORTED_MANIFEST_VERSIONS:
        raise BackupError("manifest_version_unsupported", str(version))
    return manifest


def _safe_member(directory: Path, name: object) -> Path:
    """Resolve one member name inside the backup directory, or refuse it."""
    if not isinstance(name, str) or not name or len(name) > 200:
        raise BackupError("manifest_member_invalid", repr(name)[:80])
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise BackupError("manifest_member_invalid", name)
    if Path(name).is_absolute() or (len(name) > 1 and name[1] == ":"):
        raise BackupError("manifest_member_invalid", name)
    candidate = directory / name
    try:
        resolved = candidate.resolve()
    except OSError as error:
        raise BackupError("manifest_member_invalid", str(error)) from None
    if resolved != directory.resolve() and directory.resolve() not in resolved.parents:
        raise BackupError("manifest_member_outside", name)
    if candidate.is_symlink() or (
        hasattr(candidate, "is_junction") and candidate.is_junction()
    ):
        raise BackupError("manifest_member_linked", name)
    return candidate


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_BACKUP_MODES = ("online_backup", "file_group")


def _recorded_bytes(item: dict[str, object], position: int) -> int:
    """The recorded size, or a typed refusal; a string is never a byte count."""
    value = item.get("bytes")
    if type(value) is not int or value < 0:
        raise BackupError(
            "manifest_unreadable",
            "member[%d].bytes is not a byte count: %r" % (position, value),
        )
    return int(value)


def _recorded_digest(item: dict[str, object], position: int) -> str | None:
    value = item.get("sha256")
    if value is None:
        return None
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise BackupError(
            "manifest_unreadable",
            "member[%d].sha256 is not a sha256 digest: %r" % (position, value),
        )
    return value


def _member_entries(
    directory: Path, manifest: dict[str, object]
) -> list[tuple[dict[str, object], Path]]:
    """Validated (record, path) pairs; duplicate or colliding names refuse.

    Every field a later stage relies on is checked here, before any file is
    opened or copied, so a malformed manifest becomes one typed error instead
    of a ValueError from whichever stage reads the field first (WR-04).
    """
    raw = manifest.get("members")
    if not isinstance(raw, list):
        raw = manifest.get("files")
    if not isinstance(raw, list) or not raw:
        raise BackupError("manifest_without_members")
    seen: set[str] = set()
    entries: list[tuple[dict[str, object], Path]] = []
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise BackupError("manifest_member_invalid", repr(item)[:80])
        name = item.get("name")
        path = _safe_member(directory, name)
        key = str(name).casefold()
        if key in seen:
            raise BackupError("manifest_member_duplicate", str(name))
        seen.add(key)
        _recorded_bytes(item, position)
        _recorded_digest(item, position)
        entries.append((item, path))
    return entries


def _sealed_names(
    manifest: dict[str, object],
    entries: list[tuple[dict[str, object], Path]],
    library_name: str,
    version: int,
) -> list[str]:
    """The sealed member list, checked against the member list itself."""
    raw = manifest.get("sealed_members")
    if raw is None or not raw:
        return []
    if version < 2:
        raise BackupError("manifest_unreadable", "a v1 manifest cannot seal members")
    if not isinstance(raw, list):
        raise BackupError("manifest_unreadable", "sealed_members is not a list")
    known = {str(item.get("name")) for item, _ in entries}
    sealed: list[str] = []
    for name in raw:
        if not isinstance(name, str):
            raise BackupError(
                "manifest_unreadable", "sealed_members holds a non-string entry"
            )
        if name not in known:
            raise BackupError("manifest_member_missing", name)
        if name.endswith("-shm"):
            raise BackupError(
                "manifest_unreadable",
                "%s cannot be sealed: SQLite rebuilds the shared-memory file" % name,
            )
        if name in sealed:
            raise BackupError("manifest_unreadable", "sealed_members repeats %s" % name)
        sealed.append(name)
    if library_name not in sealed:
        # Sealing a marker file beside an unsealed database proves nothing
        # about the database, which is the only member that matters (WR-04).
        raise BackupError(
            "manifest_member_missing",
            "%s is not sealed; sealing other files cannot vouch for the database"
            % library_name,
        )
    return sealed


def _check_mode_members(
    manifest: dict[str, object],
    entries: list[tuple[dict[str, object], Path]],
    library_name: str,
    version: int,
) -> None:
    """The recorded mode decides which sidecars may appear in the members."""
    if version < 2:
        return
    mode = manifest.get("mode")
    if mode not in _BACKUP_MODES:
        raise BackupError(
            "manifest_unreadable",
            "mode is not one of %s: %r" % (", ".join(_BACKUP_MODES), mode),
        )
    if library_name.endswith(("-wal", "-shm")):
        raise BackupError(
            "manifest_unreadable",
            "copy_name %s is a SQLite sidecar, not the database" % library_name,
        )
    if mode == "online_backup":
        for item, _ in entries:
            name = str(item.get("name"))
            if name.endswith("-wal") and int(item["bytes"]):
                raise BackupError(
                    "manifest_unreadable",
                    "an online snapshot is self-contained; %s cannot hold frames"
                    % name,
                )


def _check_fingerprint(recorded: object, version: int) -> None:
    """Shape of the recorded fingerprint, before it is compared with anything."""
    if version < 2 or recorded is None:
        return
    if not isinstance(recorded, dict):
        raise BackupError("manifest_unreadable", "logical_fingerprint is not an object")
    value = recorded.get("version")
    if type(value) is not int or value != FINGERPRINT_VERSION:
        raise BackupError(
            "manifest_unreadable",
            "logical_fingerprint.version is not %d: %r" % (FINGERPRINT_VERSION, value),
        )
    digest = recorded.get("digest")
    if not isinstance(digest, str) or _HEX64_RE.fullmatch(digest) is None:
        raise BackupError(
            "manifest_unreadable", "logical_fingerprint.digest is not a sha256 digest"
        )
    tables = recorded.get("tables")
    if not isinstance(tables, dict):
        raise BackupError(
            "manifest_unreadable", "logical_fingerprint.tables is not an object"
        )
    for name, entry in tables.items():
        if not isinstance(entry, dict):
            raise BackupError(
                "manifest_unreadable",
                "logical_fingerprint.tables[%r] is not an object" % str(name),
            )
        entry_digest = entry.get("digest")
        if not isinstance(entry_digest, str) or _HEX64_RE.fullmatch(entry_digest) is None:
            raise BackupError(
                "manifest_unreadable",
                "logical_fingerprint.tables[%r] has no sha256 digest" % str(name),
            )


def _differing_tables(
    recorded: dict[str, object], actual: dict[str, object]
) -> list[str]:
    """Table names whose per-table digest differs from the sealed one."""
    recorded_tables = recorded.get("tables")
    actual_tables = actual.get("tables")
    names: set[str] = set(actual_tables or {})
    if isinstance(recorded_tables, dict):
        names |= set(recorded_tables)
    differing: list[str] = []
    for name in sorted(names):
        left = None
        right = None
        if isinstance(recorded_tables, dict):
            entry = recorded_tables.get(name)
            if isinstance(entry, dict):
                left = entry.get("digest")
        if isinstance(actual_tables, dict):
            entry = actual_tables.get(name)
            if isinstance(entry, dict):
                right = entry.get("digest")
        if left != right:
            differing.append(name)
    return differing


def _identity(info: os.stat_result) -> dict[str, int]:
    """The four facts that must not move while one member is read."""
    return {
        "device": int(getattr(info, "st_dev", 0)),
        "file_id": int(getattr(info, "st_ino", 0)),
        "bytes": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
    }


def _path_identity(path: Path) -> dict[str, object]:
    """Identity of the name that still points at the member, or why it is gone."""
    try:
        return _identity(os.stat(path))
    except FileNotFoundError:
        return {"present": False}
    except OSError as error:
        return {"present": None, "error": type(error).__name__}


def _copy_and_hash(source: Path, destination: Path) -> dict[str, object]:
    """Copy one member byte for byte and hash exactly the bytes written.

    The identity of the open file is read before and after the copy, and the
    name is stat-ed once more afterwards.  A member rewritten while it is
    being read therefore reports identities that disagree instead of a digest
    assembled from two versions (PA-01).
    """
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as reader:
        before = _identity(os.fstat(reader.fileno()))
        with destination.open("xb") as writer:
            while True:
                chunk = reader.read(_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                writer.write(chunk)
                total += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        after = _identity(os.fstat(reader.fileno()))
    return {
        "name": destination.name,
        "bytes": total,
        "sha256": digest.hexdigest(),
        "before": before,
        "after": after,
        "path_now": _path_identity(source),
    }


def _readback_digest(path: Path) -> str:
    """Hash the bytes as they now sit on disk, not as they were streamed."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _verification_workspace(root: Path) -> tuple[Path, str]:
    """A private directory for the copy, preferring the backup's own volume.

    A backup on a read-only volume (an external disk mounted read-only, an
    ACL that allows only reading) still has to be verifiable, so the system
    temporary directory is the fallback.  The returned label says where the
    copy really was, because "which volume did this proof land on" is part of
    the evidence.  Neither location is ever inside the verified directory.
    """
    name = "%s.verify-%s" % (root.name, uuid.uuid4().hex)
    try:
        sibling = root.parent / name
        sibling.mkdir(parents=False, exist_ok=False)
    except OSError:
        try:
            return Path(tempfile.mkdtemp(prefix="pixivkeep-verify-")), "system_temp"
        except OSError as error:
            raise OSError(
                "no writable place for the verification copy: %s" % error
            ) from None
    return sibling, "sibling"


def _seal_members(
    entries: list[tuple[dict[str, object], Path]],
    copy_names: list[str],
    sealed_names: list[str],
    workspace: Path,
) -> tuple[list[dict[str, object]], str, dict[str, object]]:
    """Copy each member once, then judge exactly the bytes that were read.

    Every copied member gets a record; only the sealed ones carry a verdict.
    'changed' means the bytes or the file identity moved while the copy was
    taken, so no single version can be described and the check must fail.
    """
    by_name = {str(item.get("name")): (item, path) for item, path in entries}
    sealed = set(sealed_names)
    facts: list[dict[str, object]] = []
    states: list[str] = []
    before: dict[str, object] = {}
    after: dict[str, object] = {}
    copy_seconds = 0.0
    hash_seconds = 0.0
    copy_bytes = 0
    for name in copy_names:
        record, path = by_name[name]
        recorded_bytes = int(record["bytes"])
        recorded_digest = record.get("sha256")
        before[name] = _path_identity(path)
        fact: dict[str, object] = {
            "name": name,
            "state": "missing",
            "bytes_recorded": recorded_bytes,
            "bytes_actual": 0,
            "sha256_recorded": recorded_digest,
            "sha256_actual": None,
        }
        if not path.is_file():
            after[name] = _path_identity(path)
            facts.append(fact)
            states.append("missing")
            continue
        started = time.monotonic()
        try:
            copied = _copy_and_hash(path, workspace / name)
        except FileNotFoundError:
            after[name] = _path_identity(path)
            facts.append(fact)
            states.append("missing")
            continue
        except OSError as error:
            # An unreadable member is an expected external condition, not a
            # crash: it enters the public error protocol under the existing
            # "this file cannot be read" code so the documented exit codes hold.
            raise LibraryMaintenanceError(
                "library_unavailable", "%s: %s" % (name, error)
            ) from None
        copy_seconds += time.monotonic() - started
        copy_bytes += int(copied["bytes"])
        after[name] = copied["path_now"]
        started = time.monotonic()
        readback = _readback_digest(workspace / name)
        hash_seconds += time.monotonic() - started
        fact["bytes_actual"] = int(copied["bytes"])
        fact["sha256_actual"] = copied["sha256"]
        if (
            copied["before"] != copied["after"]
            or copied["path_now"] != copied["before"]
            or readback != copied["sha256"]
        ):
            fact["state"] = "changed"
        elif name not in sealed:
            fact["state"] = "not_checked"
        elif int(copied["bytes"]) != recorded_bytes or copied["sha256"] != recorded_digest:
            fact["state"] = "mismatch"
        else:
            fact["state"] = "ok"
        states.append(str(fact["state"]))
        facts.append(fact)
    changed = any(state == "changed" for state in states)
    if not changed:
        # One more look after every member was copied: a writer that moved an
        # earlier member while a later one was being read is caught here.
        for index, name in enumerate(copy_names):
            current = _path_identity(by_name[name][1])
            after[name] = current
            if current != before[name]:
                changed = True
                if states[index] == "ok":
                    states[index] = "changed"
                    facts[index]["state"] = "changed"
    timings: dict[str, object] = {
        "before": before,
        "after": after,
        "copy_seconds": round(copy_seconds, 6),
        "hash_seconds": round(hash_seconds, 6),
        "copy_bytes": copy_bytes,
    }
    return facts, ("changed" if changed else "stable"), timings


def _sealed_verdict(
    facts: list[dict[str, object]], sealed_names: list[str]
) -> tuple[str, list[dict[str, object]]]:
    """The worst state among the sealed members, and those members."""
    sealed = set(sealed_names)
    members = [fact for fact in facts if fact["name"] in sealed]
    states = [str(fact["state"]) for fact in members]
    if "changed" in states:
        return "changed", members
    if "missing" in states:
        return "missing", members
    if "mismatch" in states:
        return "mismatch", members
    if states and all(state == "ok" for state in states):
        return "ok", members
    return "not_checked", members


def _recheck_members(
    entries: list[tuple[dict[str, object], Path]],
    copy_names: list[str],
    before: dict[str, object],
) -> tuple[bool, dict[str, object]]:
    """Compare each member's identity with the one recorded before the copy."""
    by_name = {str(item.get("name")): path for item, path in entries}
    moved = False
    after: dict[str, object] = {}
    for name in copy_names:
        current = _path_identity(by_name[name])
        after[name] = current
        if current != before.get(name):
            moved = True
    return moved, after


def verify_backup(
    directory: str | Path, *, source: str | Path | None = None
) -> dict[str, object]:
    """Does this backup still hold what its manifest sealed?

    Four facts are reported apart and none of them defaults to true:

    sealed      every sealed member still matches the manifest byte for byte
    logical     the copy still holds the sealed object definitions and the
                sealed value of every row, at the same row counts
    health      the copy passes this build's own integrity checks
    compatible  this build would open the copy

    All of them describe one isolated copy: the member group is copied once,
    hashed while it is read, and every later question is asked of that copy
    through a single read transaction.  Reading the backup three times instead
    let a file that changed between the reads satisfy each stage with a
    different version, which is the PA-01 defect.

    The guarantee ends with the copy.  A write that lands after the copy was
    taken is outside this check; the report says so and names the copy it
    describes, so nobody can read it as a statement about a later moment.

    'ok' is true only when all four ran and passed on a copy that did not move
    while it was taken.  'source_equivalent' is a fifth, separate question
    (does the *current* source still hold the sealed content) and never turns
    a sound backup into a broken one.
    """
    root = Path(directory).expanduser()
    if not root.exists():
        raise BackupError("backup_directory_missing", str(root))
    if not root.is_dir():
        raise BackupError("backup_not_a_directory", str(root))
    root = root.resolve()
    started = time.monotonic()
    manifest = _load_manifest(root)
    version = int(manifest["manifest_version"])
    reasons: list[str] = []
    warnings: list[str] = []
    entries = _member_entries(root, manifest)
    library_name = manifest.get("copy_name") or manifest.get("library_name")
    if not isinstance(library_name, str):
        raise BackupError("manifest_without_library_name")
    # Resolved with the same guard as every other member; a name that leaves
    # the directory is refused before anything is opened.
    _safe_member(root, library_name)
    known = {str(item.get("name")) for item, _ in entries}
    if library_name not in known:
        raise BackupError(
            "manifest_member_missing",
            "the database %s is not in the member list" % library_name,
        )
    sealed_names = _sealed_names(manifest, entries, library_name, version)
    _check_mode_members(manifest, entries, library_name, version)
    recorded = manifest.get("logical_fingerprint")
    _check_fingerprint(recorded, version)

    evidence: dict[str, object] = {
        "scope": "manifest_only",
        "snapshot_id": uuid.uuid4().hex,
        "workspace": "none",
        "path": None,
        "cleanup": "not_needed",
        "stability": "unknown",
        "copy_seconds": 0.0,
        "hash_seconds": 0.0,
        "read_seconds": 0.0,
        "copy_bytes": 0,
        "source_state_before": {},
        "source_state_after": {},
        "started_at": _now_utc(),
    }
    sealed_state = "not_checked"
    sealed_members: list[dict[str, object]] = []
    stability = "unknown"
    copy_report: dict[str, object] | None = None
    actual_fingerprint: dict[str, object] | None = None
    logical_error: str | None = None

    unexpected: list[str] = []
    try:
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if entry.name in known:
                continue
            if entry.name in {MANIFEST_NAME, MANIFEST_NAME + ".tmp"}:
                continue
            unexpected.append(entry.name)
    except OSError:
        unexpected = []
    if unexpected:
        # A stranger in the directory is not a change to the sealed facts, so
        # it is reported and never deleted.
        warnings.append("unexpected_members")
    evidence["unexpected_members"] = unexpected

    workspace: Path | None = None
    try:
        try:
            workspace, where = _verification_workspace(root)
        except OSError as error:
            reasons.append("verify_workspace_unavailable")
            warnings.append("verify_workspace_unavailable")
            evidence["workspace"] = "unavailable"
            evidence["detail"] = str(error)
        else:
            evidence["workspace"] = where
            evidence["path"] = str(workspace)
            copy_names = sealed_names or [library_name]
            facts, stability, timings = _seal_members(
                entries, copy_names, sealed_names, workspace
            )
            evidence["source_state_before"] = timings["before"]
            evidence["source_state_after"] = timings["after"]
            evidence["copy_seconds"] = timings["copy_seconds"]
            evidence["hash_seconds"] = timings["hash_seconds"]
            evidence["copy_bytes"] = timings["copy_bytes"]
            evidence["stability"] = stability
            evidence["scope"] = "isolated_copy"
            isolated = workspace / library_name
            if isolated.is_file():
                read_started = time.monotonic()
                try:
                    with contextlib.closing(_connect_readonly(isolated)) as opened:
                        # One connection and one read transaction: the object
                        # and row digest and the integrity checks describe the
                        # same snapshot of the same copy.
                        if version >= 2 and isinstance(recorded, dict):
                            actual_fingerprint = _fingerprint_from_connection(opened)
                        copy_report = inspect_library(isolated, connection=opened)
                except sqlite3.Error as error:
                    logical_error = _sqlite_reason(error)
                    warnings.append("copy_unreadable")
                except LibraryMaintenanceError as error:
                    logical_error = str(getattr(error, "code", "library_unreadable"))
                    warnings.append("copy_logical_unreadable")
                evidence["read_seconds"] = round(time.monotonic() - read_started, 6)
            else:
                warnings.append("copy_missing")
            # The last look comes after every read, so a member rewritten at any
            # point of this check is reported as changed instead of described.
            moved, final_states = _recheck_members(
                entries, copy_names, timings["before"]
            )
            evidence["source_state_after"] = final_states
            if moved and stability != "changed":
                stability = "changed"
                for fact in facts:
                    if fact["state"] == "ok":
                        fact["state"] = "changed"
            evidence["stability"] = stability
            evidence["scope"] = "isolated_copy"
            evidence["member_states"] = {
                str(fact["name"]): str(fact["state"]) for fact in facts
            }
            sealed_state, sealed_members = _sealed_verdict(facts, sealed_names)
            if version >= 2 and not sealed_names:
                warnings.append("manifest_without_sealed_members")
            if sealed_state != "ok":
                reasons.append("sealed_" + sealed_state)
            if stability == "changed":
                reasons.append("changed_during_check")
    finally:
        if workspace is not None:
            try:
                _discard(workspace)
            except OSError as error:
                evidence["cleanup"] = "failed"
                evidence["left_behind"] = str(workspace)
                warnings.append("verify_cleanup_failed")
                warnings.append("verify_cleanup_failed:" + type(error).__name__)
            else:
                evidence["cleanup"] = "ok"
    if sealed_state == "not_checked":
        reasons.append("sealed_not_checked")

    logical_state = "not_checked"
    logical: dict[str, object] = {"state": "not_checked"}
    if version < 2:
        logical = {"state": "not_checked", "reason": "manifest_too_old"}
        reasons.append("manifest_too_old")
    elif not isinstance(recorded, dict):
        logical = {"state": "not_checked", "reason": "manifest_without_fingerprint"}
    elif copy_report is None:
        logical = {
            "state": "not_checked",
            "reason": logical_error or "copy_missing",
        }
    elif actual_fingerprint is None:
        logical = {
            "state": "not_checked",
            "reason": logical_error or "copy_logical_unreadable",
            "recorded_digest": recorded.get("digest"),
        }
    else:
        recorded_digest = recorded.get("digest")
        digest_ok = recorded_digest == actual_fingerprint["digest"]
        logical_state = "ok" if digest_ok else "mismatch"
        logical = {
            "state": logical_state,
            "recorded_digest": recorded_digest,
            "actual_digest": actual_fingerprint["digest"],
            "rows_recorded": recorded.get("rows"),
            "rows_actual": actual_fingerprint["rows"],
            "tables_differing": _differing_tables(recorded, actual_fingerprint),
            "checked_at": actual_fingerprint["computed_at"],
        }
        if logical_state == "mismatch":
            reasons.append("logical_mismatch")

    if copy_report is None:
        health_state = "not_checked"
        compatible_ok = False
        health: dict[str, object] = {
            "state": "not_checked",
            "reason": logical_error or "copy_unreadable",
        }
        compatible: dict[str, object] = {
            "state": "not_checked",
            "declared_ok": False,
            "reasons": [],
        }
    else:
        health_state = "ok" if copy_report["healthy"] else "failed"
        if health_state != "ok":
            reasons.append("copy_unhealthy")
        compatible_ok = copy_report["status"] == "compatible"
        if not compatible_ok:
            reasons.append("copy_not_compatible")
        health = {
            "state": health_state,
            "status": copy_report["status"],
            "quick_check": copy_report["quick_check"],
            "integrity_check": copy_report["integrity_check"],
            "foreign_key_violations": copy_report["foreign_key_violations"],
        }
        compatible = {
            "state": "ok" if compatible_ok else "mismatch",
            "status": copy_report["status"],
            "declared_ok": bool(compatible_ok),
            "reasons": copy_report["reasons"],
        }

    source_equivalent: dict[str, object] = {"state": "not_checked",
                                            "reason": "no_source_given"}
    if source is not None:
        source_path = _resolve_target(source)
        anchor = recorded.get("digest") if isinstance(recorded, dict) else None
        if not isinstance(anchor, str):
            source_equivalent = {
                "state": "not_checked",
                "reason": "manifest_without_fingerprint",
            }
        else:
            try:
                source_fingerprint = logical_fingerprint(source_path)
            except LibraryMaintenanceError as error:
                warnings.append("source_unreadable")
                source_equivalent = {
                    "state": "not_checked",
                    "reason": getattr(error, "code", "library_unreadable"),
                }
            else:
                same = source_fingerprint["digest"] == anchor
                source_equivalent = {
                    "state": "ok" if same else "mismatch",
                    "source": str(source_path),
                    "source_digest": source_fingerprint["digest"],
                    "sealed_digest": anchor,
                }
                if not same:
                    warnings.append("source_changed_since_backup")

    ok = (
        sealed_state == "ok"
        and logical_state == "ok"
        and health_state == "ok"
        and compatible_ok
        and stability == "stable"
    )
    evidence["seconds"] = round(time.monotonic() - started, 6)
    evidence["checked_at"] = _now_utc()
    return {
        "mode": "manifest",
        "question": "sealed_backup",
        "backup": str(root),
        "manifest_path": str(root / MANIFEST_NAME),
        "manifest_version": version,
        "mode_recorded": manifest.get("mode"),
        "created_at": manifest.get("created_at"),
        "label": manifest.get("label"),
        "library_name": library_name,
        "member_count": len(entries),
        "sealed": {"state": sealed_state, "members": sealed_members},
        "logical": logical,
        "health": health,
        "compatible": compatible,
        "source_equivalent": source_equivalent,
        "evidence": evidence,
        "ok": bool(ok),
        "reasons": sorted(set(reasons)),
        "warnings": sorted(set(warnings)),
        "checked_at": _now_utc(),
    }


def compare_library(
    source: str | Path, copy_path: str | Path
) -> dict[str, object]:
    """Do these two databases hold the same content right now?

    This is the operator's comparison, not the restore gate: call
    verify_backup(directory) when the question is whether a sealed backup can
    be trusted.  Object definitions are compared with each other (not each
    side against the declaration), so two databases that deviate in different
    ways are still reported as different.
    """
    source_path = _resolve_target(source)
    copy = _resolve_target(copy_path)
    source_report = inspect_library(source_path)
    copy_report = inspect_library(copy)
    reasons: list[str] = []
    warnings: list[str] = []
    readable = (
        source_report["status"] in {"compatible", "incompatible"}
        and copy_report["status"] in {"compatible", "incompatible"}
    )
    objects_identical = False
    logical_state = "not_checked"
    logical: dict[str, object] = {"state": "not_checked",
                                  "reason": "database_unreadable"}
    if readable:
        objects_identical = _definitions(source_path) == _definitions(copy)
        source_fingerprint = logical_fingerprint(source_path)
        copy_fingerprint = logical_fingerprint(copy)
        digest_ok = source_fingerprint["digest"] == copy_fingerprint["digest"]
        logical_state = "ok" if digest_ok else "mismatch"
        logical = {
            "state": logical_state,
            "source_digest": source_fingerprint["digest"],
            "copy_digest": copy_fingerprint["digest"],
            "rows": copy_fingerprint["rows"],
            "tables_differing": _differing_tables(source_fingerprint,
                                                   copy_fingerprint),
            "checked_at": _now_utc(),
        }
        if not objects_identical:
            reasons.append("copy_objects_mismatch")
        if not digest_ok:
            reasons.append("copy_content_mismatch")
    else:
        reasons.append("database_unreadable")

    counts_match = source_report["counts"] == copy_report["counts"]
    if not counts_match:
        reasons.append("counts_mismatch")
    checks_match = (
        copy_report["quick_check"] == source_report["quick_check"]
        and copy_report["integrity_check"] == source_report["integrity_check"]
        and copy_report["foreign_key_violations"]
        == source_report["foreign_key_violations"]
    )
    if not checks_match:
        reasons.append("copy_integrity_differs")
    declared_ok = (
        copy_report["format"] == _LIBRARY_FORMAT
        and copy_report["schema_version"] == _SCHEMA_VERSION
        and not copy_report["missing_required_tables"]
        and not copy_report["missing_objects"]
        and not copy_report["mismatched_objects"]
    )
    main_sha256_match: bool | None = None
    try:
        main_sha256_match = _sha256_file(source_path) == _sha256_file(copy)
    except OSError:
        warnings.append("copy_unreadable_bytes")
    healthy_ok = bool(copy_report["healthy"])
    if not healthy_ok:
        reasons.append("copy_unhealthy")
    compatible_ok = copy_report["status"] == "compatible"
    if not compatible_ok:
        reasons.append("copy_not_compatible")
    ok = bool(
        objects_identical
        and logical_state == "ok"
        and healthy_ok
        and compatible_ok
    )
    return {
        "mode": "compare",
        "question": "same_content_as_source",
        "source": str(source_path),
        "copy": str(copy),
        "source_status": source_report["status"],
        "copy_status": copy_report["status"],
        "sealed": {"state": "not_checked", "reason": "no_manifest"},
        "logical": logical,
        "health": {
            "state": "ok" if healthy_ok else "failed",
            "quick_check": copy_report["quick_check"],
            "integrity_check": copy_report["integrity_check"],
            "foreign_key_violations": copy_report["foreign_key_violations"],
        },
        "compatible": {
            "state": "ok" if compatible_ok else "mismatch",
            "declared_ok": bool(declared_ok),
        },
        "source_equivalent": {"state": "not_checked", "reason": "is_the_source"},
        "objects_match_source": bool(objects_identical),
        "logical_state": logical_state,
        "declared_ok": bool(declared_ok),
        "checks_match": bool(checks_match),
        "healthy": healthy_ok,
        "counts_match": bool(counts_match),
        "main_sha256_match": main_sha256_match,
        "counts": copy_report["counts"],
        "quick_check": copy_report["quick_check"],
        "integrity_check": copy_report["integrity_check"],
        "reasons": sorted(set(reasons)),
        "warnings": sorted(set(warnings)),
        "ok": ok,
        "checked_at": _now_utc(),
    }


def verify_library_copy(
    path: str | Path, copy_path: str | Path
) -> dict[str, object]:
    """Deprecated alias of compare_library; it is not a backup gate.

    It answers whether two databases hold the same content right now.  It
    cannot answer whether a backup is trustworthy, because a copy nobody
    sealed can still match a source that has since drifted.  Use
    verify_backup(directory) for the restore decision.
    """
    return compare_library(path, copy_path)


def switch_plan(path: str | Path) -> dict[str, object]:
    """Ordered, read-only switch instructions for one library file."""
    report = inspect_library(path)
    status = str(report["status"])
    library = str(report["path"])
    steps: list[str] = []
    blockers: list[str] = []
    if status in {"missing", "empty"}:
        steps = [
            "库还不存在（或为 0 字节）：首次启动会初始化，不需要备份。",
            "如果这里原本有记录，先确认路径是否正确，再继续。",
        ]
    elif status == "compatible":
        steps = [
            "关闭 JpgPng 主程序，确认没有下载/整理任务在运行。",
            "运行 python maintain.py backup %s <备份目录> --label before-w5 生成快照。"
            % library,
            "运行 python maintain.py verify <备份目录>，必须 ok=true：它按清单校验封存内容，"
            "而不是只比表计数。",
            "在副本上运行 python maintain.py inspect <备份目录>/<库文件名>，"
            "预期 status=compatible。",
            "记录当前版本（git rev-parse HEAD）与本文件字节数 %d。" % report["size_bytes"],
            "切换入口后启动；启动后再次运行 maintain.py inspect，状态仍须是 compatible。",
            "回退：关闭程序后按恢复协议执行——先把当前文件组（主库与同目录的 -wal/-shm）"
            "整组移入同卷保留目录，再放回备份组（online 快照是自包含单文件，file_group "
            "整组放回）；不要只覆盖主文件，也不要直接删除 -wal。",
        ]
        if report["wal_bytes"]:
            steps.append(
                "默认的 online_backup 会读取已提交状态并把快照整理成单文件；"
                "当前 WAL 非空（%d 字节），这属于正常情况。" % report["wal_bytes"]
            )
        if report["extra_objects"]:
            steps.append(
                "库里有本版本未声明的对象（%s）：新代码容忍，旧代码可能不认识。"
                % ", ".join(str(item) for item in report["extra_objects"])
            )
    else:
        blockers = [str(item) for item in report["reasons"]] or [status]
        steps = [
            "先做字节备份：python maintain.py backup %s <备份目录> --mode file_group。"
            % library,
            "用仍能打开它的版本修复，或换回上一个已知良好的备份。",
            "重新运行 python maintain.py inspect %s，直到 status=compatible。" % library,
        ]
    return {
        "path": library,
        "status": status,
        "compatible": status in {"compatible", "missing", "empty"},
        "healthy": bool(report["healthy"]),
        "blockers": blockers,
        "steps": steps,
        "size_bytes": report["size_bytes"],
        "wal_bytes": report["wal_bytes"],
        "journal_mode": report["journal_mode"],
        "objects_declared": report["objects_declared"],
        "objects_actual": report["objects_actual"],
        "reasons": report["reasons"],
        "warnings": report["warnings"],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_COMMON = argparse.ArgumentParser(add_help=False)
_COMMON.add_argument(
    "--json", action="store_true", default=argparse.SUPPRESS, help="输出 JSON，便于机器读取"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maintain.py",
        description="库预检、备份、副本校验与切换计划（只读、不联网、不写库）",
    )
    parser.add_argument(
        "--json", action="store_true", default=False, help="输出 JSON，便于机器读取"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser(
        "inspect", parents=[_COMMON], help="只读检查库是否与本版本兼容"
    )
    inspect.add_argument("database", type=Path)
    backup = commands.add_parser(
        "backup", parents=[_COMMON], help="生成可还原的备份（默认在线快照）"
    )
    backup.add_argument("database", type=Path)
    backup.add_argument("destination", type=Path)
    backup.add_argument("--label", help="目标目录名（1-63 位字母数字点横线）；已存在则拒绝")
    backup.add_argument(
        "--mode",
        choices=("auto", "online_backup", "file_group"),
        default="auto",
        help="auto=健康库走在线快照、损坏库走整组字节复制",
    )
    verify = commands.add_parser(
        "verify",
        parents=[_COMMON],
        help="按清单校验备份是否仍与封存时一致（恢复门槛）",
    )
    verify.add_argument("target", type=Path, help="备份目录（含 manifest.json）")
    verify.add_argument(
        "copy",
        type=Path,
        nargs="?",
        default=None,
        help="已弃用：给出第二个路径时按 compare 处理",
    )
    verify.add_argument(
        "--source",
        type=Path,
        default=None,
        help="同时对照当前源库是否仍与封存内容相同（不影响 ok）",
    )
    compare = commands.add_parser(
        "compare",
        parents=[_COMMON],
        help="比较两个库当前内容是否相同（不是备份门槛）",
    )
    compare.add_argument("source", type=Path)
    compare.add_argument("copy", type=Path)
    plan = commands.add_parser(
        "plan", parents=[_COMMON], help="输出切换与回退顺序，不做任何写操作"
    )
    plan.add_argument("database", type=Path)
    scan = commands.add_parser(
        "scan", parents=[_COMMON], help="只读巡检已交付的文件是否还和记录一致"
    )
    scan.add_argument("database", type=Path)
    scan.add_argument(
        "--root", type=Path, default=None, help="输出图库根目录；默认取库文件所在项目的根"
    )
    scan.add_argument(
        "--limit", type=int, default=200, help="本次最多核验多少行（1-200，默认 200）"
    )
    scan.add_argument(
        "--after", default=None, help="续跑游标：上一次输出的 next_cursor（completed_at,artifact_id）"
    )
    scan.add_argument("--max-bytes", type=int, default=None, help="本轮最多读多少字节")
    scan.add_argument("--max-seconds", type=float, default=None, help="本轮最多用多少秒")
    scan.add_argument(
        "--allow-unknown",
        action="store_true",
        default=False,
        help="只有“无尺寸/无摘要”的旧行可被跳过；坏记录与未覆盖仍然不通过",
    )
    return parser


def _emit(payload: dict[str, object], as_json: bool, lines: list[str]) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
        return
    for line in lines:
        print(line)


def _inspect_lines(report: dict[str, object]) -> list[str]:
    lines = [
        "库: %s" % report["path"],
        "状态: %s" % report["status"],
        "格式: %s   schema: %s   journal_mode: %s"
        % (report["format"], report["schema_version"], report["journal_mode"]),
        "对象: %s/%s  缺失 %d  文本不符 %d"
        % (
            report["objects_actual"],
            report["objects_declared"],
            len(report["missing_objects"]),
            len(report["mismatched_objects"]),
        ),
        "完整性: quick_check=%s integrity_check=%s 外键冲突=%s"
        % (
            ",".join(str(item) for item in report["quick_check"]) or "-",
            ",".join(str(item) for item in report["integrity_check"]) or "-",
            report["foreign_key_violations"],
        ),
        "字节: 主 %s  WAL %s  SHM %s"
        % (report["size_bytes"], report["wal_bytes"], report["shm_bytes"]),
    ]
    counts = report["counts"]
    if counts:
        lines.append(
            "表计数: "
            + ", ".join("%s=%s" % (name, counts[name]) for name in sorted(counts))
        )
    if report["reasons"]:
        lines.append("原因: " + ", ".join(str(item) for item in report["reasons"]))
    if report["warnings"]:
        lines.append("提示: " + ", ".join(str(item) for item in report["warnings"]))
    return lines


_SCAN_FINDINGS = (
    "missing",
    "changed",
    "unreadable",
    "root_unavailable",
    "changed_during_check",
)


def _scan_coverage(report: dict[str, object]) -> str:
    """The engine's own coverage verdict, or a conservative reading of it.

    The audit module reports coverage itself from contract v5 on.  While that
    fix is still in flight the field can be absent, and the fallback must never
    claim more coverage than the evidence supports.
    """
    coverage = report.get("coverage")
    if isinstance(coverage, str) and coverage:
        return coverage
    stats = report.get("stats") or {}
    if stats.get("budget_exhausted_reason"):
        return "budget"
    if int(stats.get("examined") or 0) == 0:
        return "empty"
    return "page" if report.get("truncated") else "complete"


def _scan_is_empty(report: dict[str, object], *, continuation: bool = False) -> bool:
    """True only when the ledger itself holds no delivered artifact.

    Coverage 'empty' can also mean "the cursor sits at the end", so the row
    count is the fact that decides whether the library holds any work at all.
    """
    stats = report.get("stats") or {}
    library_rows = stats.get("library_rows")
    if type(library_rows) is int:
        return library_rows == 0
    # On a continuation page the row count is not computed, so an empty
    # page only means "the cursor is at the end", never "the library is empty".
    return _scan_coverage(report) == "empty" and not continuation


def _scan_findings(report: dict[str, object]) -> list[dict[str, object]]:
    return [
        row
        for row in (report.get("rows") or [])
        if row.get("verdict") in _SCAN_FINDINGS
    ]


def _scan_unknown_reasons(report: dict[str, object]) -> dict[str, int]:
    """Reason histogram for the rows nobody could verify.

    The audit module reports this itself from contract v5 on.  The per-row
    reasons are the same facts, so the CLI also reads them directly: a summary
    that cannot name why a row was skipped is a summary nobody can act on.
    """
    stats = report.get("stats") or {}
    raw = stats.get("unknown_reasons")
    if isinstance(raw, dict) and raw:
        counts: dict[str, int] = {}
        for key, value in raw.items():
            try:
                counts[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return counts
    counts = {}
    for row in report.get("rows") or []:
        if row.get("verdict") != "unknown":
            continue
        reason = str(row.get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _scan_lines(report: dict[str, object], *, continuation: bool = False) -> list[str]:
    """Report exactly what was verified; never upgrade unknown into a match."""
    stats = report.get("stats") or {}
    coverage = _scan_coverage(report)
    examined = int(stats.get("examined") or 0)
    matched = int(stats.get("matched") or 0)
    excluded = int(stats.get("excluded") or 0)
    unknown = int(stats.get("unknown") or 0)
    considered = int(stats.get("considered") or examined)
    findings = _scan_findings(report)
    ledger = report.get("ledger")
    if isinstance(ledger, dict) and ledger.get("state") == "error":
        cursor = ledger.get("last_safe_cursor")
        return [
            "巡检失败: 账本读不出来（%s）。" % (ledger.get("code") or "ledger_error"),
            "最后安全游标: %s"
            % ("%s,%s" % (cursor[0], cursor[1]) if cursor else "-"),
            "本次没有给出任何核验结论。",
        ]
    if _scan_is_empty(report, continuation=continuation):
        return ["库里没有已登记的成品可核验。"]
    if _scan_coverage(report) == "empty" and continuation:
        return ["游标已在末尾，本次没有可核验的行。"]
    lines = [
        "核验: 覆盖 %d 行（匹配 %d，排除 %d，无法核验 %d）"
        % (examined, matched, excluded, unknown),
        "字节: 读取 %s，用时 %.2f 秒"
        % (stats.get("bytes_hashed", 0), float(stats.get("seconds") or 0)),
    ]
    if unknown:
        reasons = _scan_unknown_reasons(report)
        detail = "，".join(
            "%s: %d" % (name, count) for name, count in sorted(reasons.items())
        )
        lines.append("无法核验: " + (detail or "原因未记录"))
    if findings:
        lines.append("需要你知道的 %d 行:" % len(findings))
        for row in findings[:20]:
            lines.append(
                "  %s p%s  %s  %s"
                % (row["illust_id"], row["page_index"], row["verdict"], row["reason"])
            )
        if len(findings) > 20:
            lines.append("  … 还有 %d 行未列出" % (len(findings) - 20))
    if coverage != "complete":
        cursor = report.get("next_cursor")
        lines.append(
            "覆盖: 未完成（%s）；续跑游标 %s"
            % (coverage, "%s,%s" % (cursor[0], cursor[1]) if cursor else "-")
        )
    if findings:
        tail = "结论: 本次核验发现 %d 行与记录不一致" % len(findings)
        if unknown:
            tail += "；另有 %d 行无法核验" % unknown
        lines.append(tail + "。")
    elif coverage == "complete" and unknown == 0:
        lines.append("结论: 本次核验覆盖全部 %d 行，全部与记录一致。" % examined)
    else:
        lines.append(
            "结论: 本次只核验了 %d/%d 行，%d 行无法核验。"
            % (matched + excluded, max(considered, examined), unknown)
        )
    return lines


def _scan_outcome(
    report: dict[str, object], *, allow_unknown: bool
) -> tuple[int, list[dict[str, object]]]:
    """Exit code and findings for one scan report (contract v5 section 2.3)."""
    stats = report.get("stats") or {}
    ledger = report.get("ledger")
    findings = _scan_findings(report)
    if isinstance(ledger, dict) and ledger.get("state") == "error":
        return 3, findings
    if _scan_coverage(report) == "error":
        return 3, findings
    if findings:
        return 2, findings
    coverage = _scan_coverage(report)
    unknown = int(stats.get("unknown") or 0)
    if coverage == "empty" or (coverage == "complete" and unknown == 0):
        return 0, findings
    if allow_unknown and unknown and coverage == "complete":
        reasons = set(_scan_unknown_reasons(report))
        if reasons and reasons <= _LEGACY_UNKNOWN_REASONS:
            return 0, findings
    return _EXIT_INCOMPLETE, findings


def _verify_lines(result: dict[str, object]) -> list[str]:
    sealed = result["sealed"]
    logical = result["logical"]
    members = sealed.get("members") or []
    lines = [
        "备份: %s（清单 v%s，生成于 %s）"
        % (
            result["backup"],
            result["manifest_version"],
            result.get("created_at") or "-",
        ),
        "成员: %d 个，封存 %d 个，封存校验 %s"
        % (int(result.get("member_count") or 0), len(members), sealed["state"]),
    ]
    for member in members:
        if member["state"] == "ok":
            continue
        lines.append(
            "  %s  %s  记录 %s 字节 / 实际 %s 字节"
            % (
                member["name"],
                member["state"],
                member["bytes_recorded"],
                member["bytes_actual"],
            )
        )
    lines.append(
        "逻辑内容: %s%s"
        % (
            logical["state"],
            ("（%s）" % logical["reason"]) if logical.get("reason") else "",
        )
    )
    differing = logical.get("tables_differing") or []
    if differing:
        lines.append("  内容不同的表: " + ", ".join(str(name) for name in differing))
    evidence = result.get("evidence") or {}
    if evidence.get("scope") == "isolated_copy":
        lines.append(
            "证据: 隔离副本（%s）已复制 %d 字节，复制 %.3f 秒 / 回读哈希 %.3f 秒 / 读取 %.3f 秒"
            % (
                "同级目录" if evidence.get("workspace") == "sibling" else "系统临时目录",
                int(evidence.get("copy_bytes") or 0),
                float(evidence.get("copy_seconds") or 0.0),
                float(evidence.get("hash_seconds") or 0.0),
                float(evidence.get("read_seconds") or 0.0),
            )
        )
        if evidence.get("stability") == "changed":
            lines.append("注意: 复制期间备份文件被改动，本次核验不能描述任何一个版本。")
        if evidence.get("cleanup") == "failed":
            lines.append("提示: 隔离副本未能删除：%s" % evidence.get("left_behind"))
    lines.append("健康: %s" % result["health"]["state"])
    lines.append("兼容: %s" % result["compatible"]["state"])
    lines.append("与当前源: %s" % result["source_equivalent"]["state"])
    if result["reasons"]:
        lines.append("原因: " + ", ".join(result["reasons"]))
    if result["warnings"]:
        lines.append("提示: " + ", ".join(result["warnings"]))
    lines.append(
        "边界: 以上结论描述那份隔离副本；核验返回之后备份目录再被改动，不在本次保证内。"
    )
    lines.append("结论: ok=%s" % result["ok"])
    if int(result["manifest_version"]) < 2:
        lines.append(
            "提示: 这份备份的清单是旧版本，无法证明内容一致；请重新运行 backup。"
        )
    return lines


def _compare_lines(result: dict[str, object]) -> list[str]:
    lines = [
        "源: %s (%s)" % (result["source"], result["source_status"]),
        "副本: %s (%s)" % (result["copy"], result["copy_status"]),
        "对象定义一致=%s 内容一致=%s 计数一致=%s 完整性一致=%s 本版本可打开=%s"
        % (
            result["objects_match_source"],
            result["logical"]["state"] == "ok",
            result["counts_match"],
            result["checks_match"],
            result["declared_ok"],
        ),
        "健康: %s" % result["health"]["state"],
        "结论: ok=%s" % result["ok"],
    ]
    differing = result["logical"].get("tables_differing") or []
    if differing:
        lines.append("内容不同的表: " + ", ".join(str(name) for name in differing))
    if result["reasons"]:
        lines.append("原因: " + ", ".join(result["reasons"]))
    if result["warnings"]:
        lines.append("提示: " + ", ".join(result["warnings"]))
    return lines


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    as_json = bool(getattr(arguments, "json", False))
    try:
        if arguments.command == "inspect":
            report = inspect_library(arguments.database)
            _emit(report, as_json, _inspect_lines(report))
            return _EXIT_BY_STATUS.get(str(report["status"]), _EXIT_REFUSED)
        if arguments.command == "backup":
            manifest = backup_library(
                arguments.database,
                arguments.destination,
                label=arguments.label,
                mode=arguments.mode,
            )
            _emit(
                manifest,
                as_json,
                [
                    "备份: %s" % Path(str(manifest["manifest_path"])).parent,
                    "方式: %s" % manifest["mode"],
                    "文件: "
                    + ", ".join(str(item["name"]) for item in manifest["files"]),
                    "对象: %s/%s  表计数 %d 项"
                    % (
                        manifest["objects_actual"],
                        manifest["objects_declared"],
                        len(manifest["counts"]),
                    ),
                    "校验: verified=%s 对象一致=%s 计数一致=%s"
                    % (
                        manifest["verified"],
                        manifest["objects_match_source"],
                        manifest["counts_match_source"],
                    ),
                    "源库: 主文件未变=%s  整组未变=%s"
                    % (
                        manifest["source_checks"]["main_unchanged"],
                        manifest["source_checks"]["group_unchanged"],
                    ),
                ],
            )
            return 0
        if arguments.command == "verify":
            if arguments.copy is not None:
                # The old two-path form was a comparison, not a seal check.
                result = compare_library(arguments.target, arguments.copy)
                _emit(
                    result,
                    as_json,
                    _compare_lines(result)
                    + ["提示: verify 的两个路径写法已弃用，请改用 compare。"],
                )
                return 0 if result["ok"] else 2
            result = verify_backup(arguments.target, source=arguments.source)
            _emit(result, as_json, _verify_lines(result))
            return 0 if result["ok"] else 2
        if arguments.command == "compare":
            result = compare_library(arguments.source, arguments.copy)
            _emit(result, as_json, _compare_lines(result))
            return 0 if result["ok"] else 2
        if arguments.command == "scan":
            from core.app_paths import AppPaths
            from core.library_audit import LibraryAuditError, ScanBudget, scan_library_artifacts
            from core.library_paths import PathResolver

            database = Path(arguments.database).expanduser().resolve()
            raw_root = arguments.root if arguments.root is not None else database.parent.parent
            root = Path(raw_root).expanduser().resolve()
            budget = ScanBudget(
                **{
                    key: value
                    for key, value in (
                        ("max_bytes", arguments.max_bytes),
                        ("max_seconds", arguments.max_seconds),
                    )
                    if value is not None
                }
            )
            cursor = None
            if arguments.after:
                parts = str(arguments.after).split(",", 1)
                if len(parts) != 2 or not all(parts):
                    raise LibraryMaintenanceError(
                        "scan_cursor_invalid", "游标格式应为 completed_at,artifact_id"
                    )
                cursor = (parts[0].strip(), parts[1].strip())
            try:
                report = scan_library_artifacts(
                    (database, PathResolver(AppPaths(root))),
                    after=cursor,
                    limit=arguments.limit,
                    budget=budget,
                )
            except LibraryAuditError as error:
                if as_json:
                    print(
                        json.dumps(
                            {
                                "status": "error",
                                "code": "scan_ledger_unreadable",
                                "detail": str(error),
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    print("巡检失败: 账本读不出来（%s）" % error, file=sys.stderr)
                return 3
            continuation = arguments.after is not None
            code, findings = _scan_outcome(report, allow_unknown=arguments.allow_unknown)
            report["exit_code"] = code
            report["findings"] = len(findings)
            if code == 0 and arguments.allow_unknown and int(
                (report.get("stats") or {}).get("unknown") or 0
            ):
                _emit(
                    report,
                    as_json,
                    _scan_lines(report, continuation=continuation)
                    + [
                        "提示: 已按 --allow-unknown 跳过 %d 行无摘要旧行。"
                        % int((report.get("stats") or {}).get("unknown") or 0)
                    ],
                )
            else:
                _emit(report, as_json, _scan_lines(report, continuation=continuation))
            return code
        plan = switch_plan(arguments.database)
        _emit(
            plan,
            as_json,
            ["库: %s" % plan["path"], "状态: %s" % plan["status"]]
            + (["阻塞: " + ", ".join(plan["blockers"])] if plan["blockers"] else [])
            + [""]
            + ["%d. %s" % (index + 1, step) for index, step in enumerate(plan["steps"])],
        )
        return 0 if plan["compatible"] else 2
    except ContractError as error:
        # A rejected argument (bad limit, bad budget) is not a crash: the
        # documented exit codes are 0/2/3/4 and a traceback would break them.
        if as_json:
            print(
                json.dumps(
                    {"status": "error", "code": "invalid_argument", "detail": str(error)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print("参数被拒: %s" % error, file=sys.stderr)
        return _EXIT_REFUSED
    except LibraryMaintenanceError as error:
        if as_json:
            print(
                json.dumps(
                    {"status": "error", "code": error.code, "detail": error.detail},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(
                "失败: %s%s" % (error.code, (" " + error.detail) if error.detail else ""),
                file=sys.stderr,
            )
            if error.code in {"backup_failed", "backup_verification_failed"}:
                print("提示: 可改用 --mode file_group 做整组字节复制。", file=sys.stderr)
        if error.code in {
            "library_missing",
            "library_unavailable",
            "library_locked",
            "library_unreadable",
            "not_a_sqlite_file",
            "source_unreadable",
            "backup_directory_missing",
            "backup_not_a_directory",
            "manifest_missing",
            "manifest_unreadable",
            "manifest_too_large",
            "manifest_version_unsupported",
            "manifest_without_members",
            "manifest_without_library_name",
            "manifest_member_missing",
        }:
            return 3
        return _EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover - thin script guard
    raise SystemExit(main())
