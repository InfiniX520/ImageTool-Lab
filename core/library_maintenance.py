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
import sys
from pathlib import Path

from core.library_repository import (
    LibraryError,
    _DDL,
    _LIBRARY_FORMAT,
    _REQUIRED_TABLES,
    _SCHEMA_VERSION,
    _validate_database_path,
)

MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest.json"
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


# ---------------------------------------------------------------------------
# Reading facts
# ---------------------------------------------------------------------------


def _normalized_objects(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): " ".join(str(row[1]).split())
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'"
        )
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


def inspect_library(path: str | Path) -> dict[str, object]:
    """Read-only verdict on one library file.

    status is 'missing', 'empty', 'compatible', 'incompatible' or
    'unreadable'.  'healthy' covers the file's own integrity only, so an
    incompatible-but-intact database still reports healthy true and a corrupted
    one reports a reason instead of a plain verdict.
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
        with contextlib.closing(_connect_readonly(target)) as connection:
            facts = _facts(connection)
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
    """Restore instructions that match how the backup was taken.

    An online snapshot is one self-contained file, so any -wal left beside the
    target belongs to the *old* database and must go: replaying it onto a
    different file is how a restore corrupts itself.  A file-group backup is a
    consistent set instead, so the whole set goes back together and nothing is
    deleted.
    """
    steps = ["关闭 JpgPng 主程序，确认没有下载/整理任务在运行。"]
    if mode == "file_group":
        names = ", ".join(str(item["name"]) for item in files)
        steps.append(
            "把备份目录里的整套文件（%s）一起复制覆盖回 %s。" % (names, source.parent)
        )
        steps.append(
            "不要只复制主文件，也不要删除备份里的 %s-wal：它们属于同一个一致状态。" % name
        )
    else:
        steps.append("把备份目录里的 %s 复制覆盖回 %s。" % (name, source))
        steps.append(
            "如果原路径旁边还留着 %s-wal 或 %s-shm，一并删除：那是旧库的事务日志，"
            "不能配还原后的文件。" % (name, name)
        )
    steps.append("启动前运行 python maintain.py inspect %s，状态须为 compatible。" % source)
    return steps


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


def _objects_match(
    first: dict[str, object], second: dict[str, object]
) -> bool:
    """Two readable databases hold the same SQL objects.

    Comparing each side against the shared declaration is equivalent to
    comparing them with each other, and it needs no second copy of the DDL.
    """
    if first["status"] in {"missing", "empty", "unreadable"}:
        return False
    if second["status"] in {"missing", "empty", "unreadable"}:
        return False
    return all(
        first[key] == second[key]
        for key in (
            "objects_actual",
            "missing_objects",
            "extra_objects",
            "mismatched_objects",
        )
    )


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
    objects_match = _objects_match(source_report, copy_report)
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
        "restore_steps": _restore_steps(source, source.name, effective, files),
    }
    manifest["manifest_path"] = str(_write_manifest(target, manifest))
    return manifest


def verify_library_copy(path: str | Path, copy_path: str | Path) -> dict[str, object]:
    """Compare a copy with its source and with this build's declaration.

    'ok' answers the question a restore actually asks: does this copy hold the
    same SQL objects, the same rows and intact pages as the library it came
    from.  'declared_ok' separately reports whether this build would open it.
    A snapshot copy is logically identical, not byte identical, so differing
    main bytes are reported without failing the copy.
    """
    source = _resolve_target(path)
    copy = _resolve_target(copy_path)
    source_report = inspect_library(source)
    copy_report = inspect_library(copy)
    counts_match: bool | None = None
    reasons: list[str] = []
    warnings: list[str] = []
    if source_report["status"] in {"compatible", "incompatible"}:
        counts_match = source_report["counts"] == copy_report["counts"]
        if not counts_match:
            reasons.append("counts_mismatch")
    else:
        warnings.append("source_unreadable")
    objects_match = _objects_match(source_report, copy_report)
    if not objects_match:
        reasons.append("copy_objects_mismatch")
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
        main_sha256_match = _sha256_file(source) == _sha256_file(copy)
    except OSError:
        warnings.append("copy_unreadable_bytes")
    result: dict[str, object] = {
        "source": str(source),
        "copy": str(copy),
        "source_status": source_report["status"],
        "copy_status": copy_report["status"],
        "objects_match_source": bool(objects_match),
        "declared_ok": bool(declared_ok),
        "checks_match": bool(checks_match),
        "healthy": bool(copy_report["healthy"]),
        "counts_match": counts_match,
        "main_sha256_match": main_sha256_match,
        "counts": copy_report["counts"],
        "quick_check": copy_report["quick_check"],
        "integrity_check": copy_report["integrity_check"],
        "reasons": sorted(set(reasons)),
        "warnings": sorted(set(warnings)),
    }
    result["ok"] = not result["reasons"]
    return result


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
            "运行 python maintain.py verify %s <备份目录>/<库文件名>，必须 ok=true。"
            % library,
            "在副本上运行 python maintain.py inspect <备份目录>/<库文件名>，"
            "预期 status=compatible。",
            "记录当前版本（git rev-parse HEAD）与本文件字节数 %d。" % report["size_bytes"],
            "切换入口后启动；启动后再次运行 maintain.py inspect，状态仍须是 compatible。",
            "回退：关闭程序，把备份目录里的 %s 覆盖回原路径；默认的单文件快照要求一并删除"
            "原路径旁边的 -wal/-shm，而 --mode file_group 的整组备份要整套复制回去。"
            % Path(library).name,
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
        "verify", parents=[_COMMON], help="逐对象、逐表计数校验副本"
    )
    verify.add_argument("database", type=Path)
    verify.add_argument("copy", type=Path)
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
        "--limit", type=int, default=200, help="本次最多核验多少行（1-2000，默认 200）"
    )
    scan.add_argument(
        "--after", default=None, help="续跑游标：上一次输出的 next_cursor（completed_at,artifact_id）"
    )
    scan.add_argument("--max-bytes", type=int, default=None, help="本轮最多读多少字节")
    scan.add_argument("--max-seconds", type=float, default=None, help="本轮最多用多少秒")
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


def _scan_lines(report: dict[str, object]) -> list[str]:
    stats = report["stats"]
    rows = report["rows"]
    lines = [
        "核验: %s 行（匹配 %s，无法核验 %s）"
        % (stats["examined"], stats["matched"], stats["unknown"]),
        "字节: 读取 %s，用时 %.2f 秒" % (stats["bytes_hashed"], float(stats["seconds"])),
    ]
    findings = [row for row in rows if row["verdict"] in _SCAN_FINDINGS]
    if findings:
        lines.append("需要你知道的 %d 行:" % len(findings))
        for row in findings[:20]:
            lines.append(
                "  %s p%s  %s  %s"
                % (row["illust_id"], row["page_index"], row["verdict"], row["reason"])
            )
        if len(findings) > 20:
            lines.append("  … 还有 %d 行未列出" % (len(findings) - 20))
    else:
        lines.append("结论: 本次核验的每一行都与记录一致。")
    if report["truncated"]:
        reason = stats["budget_exhausted_reason"]
        cursor = report["next_cursor"]
        lines.append(
            "提示: 本次没有覆盖整库（%s），续跑游标 %s"
            % (reason or "还有更多行", "%s,%s" % (cursor[0], cursor[1]) if cursor else "-")
        )
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
            result = verify_library_copy(arguments.database, arguments.copy)
            _emit(
                result,
                as_json,
                [
                    "源: %s (%s)" % (result["source"], result["source_status"]),
                    "副本: %s (%s)" % (result["copy"], result["copy_status"]),
                    "对象一致=%s 计数一致=%s 完整性一致=%s 本版本可打开=%s 主文件字节一致=%s"
                    % (
                        result["objects_match_source"],
                        result["counts_match"],
                        result["checks_match"],
                        result["declared_ok"],
                        result["main_sha256_match"],
                    ),
                    "结论: ok=%s" % result["ok"],
                ]
                + (["原因: " + ", ".join(result["reasons"])] if result["reasons"] else []),
            )
            return 0 if result["ok"] else 2
        if arguments.command == "scan":
            from core.app_paths import AppPaths
            from core.library_audit import LibraryAuditError, ScanBudget, scan_library_artifacts
            from core.library_paths import PathResolver

            database = Path(arguments.database)
            root = arguments.root if arguments.root is not None else database.parent.parent
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
            _emit(report, as_json, _scan_lines(report))
            if any(row["verdict"] in _SCAN_FINDINGS for row in report["rows"]):
                return 2
            return 0
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
        }:
            return 3
        return _EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover - thin script guard
    raise SystemExit(main())
