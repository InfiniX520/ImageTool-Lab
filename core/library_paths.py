"""Portable path identities and the new ledger's process-owned writer lock."""
from __future__ import annotations

import os
from pathlib import Path
from threading import Lock

from core.app_paths import AppPaths
from core.task_contracts import ContractError, PathRef


class AlreadyRunningError(RuntimeError):
    """Another process or repository instance owns this ledger's writer lock."""


def _reject_links(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ContractError("Linked output and lock paths are not supported")


def _canonical_path(path: Path) -> Path:
    """Use one identity for ordinary and extended-length Windows paths.

    A non-strict Windows realpath can retain the extended prefix when a
    missing parent is created between its two OS lookups. Normalize only
    filesystem drive/UNC prefixes; device namespaces remain distinct.
    """
    if os.name != "nt":
        return path
    raw = str(path)
    if raw.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + raw[8:])
    if (raw.startswith("\\\\?\\") and len(raw) >= 7
            and raw[4].isascii() and raw[4].isalpha() and raw[5:7] == ":\\"):
        return Path(raw[4:])
    return path


class PathResolver:
    """Resolve declared roots independently of the shell's current directory."""

    def __init__(self, paths: AppPaths, external_roots: dict[str, str | Path] | None = None):
        self.project_root = _canonical_path(paths.root)
        _reject_links(self.project_root)
        self._external: dict[str, Path] = {}
        self._external_guard = Lock()
        for root_id, raw in (external_roots or {}).items():
            self.register_external(root_id, raw)

    def register_external(self, root_id: str, raw: str | Path) -> None:
        """Publish an additional root; existing identities can never move."""
        ref = PathRef(kind="external", root_id=root_id, relative_path="probe")
        key = ref.root_id.casefold()
        root = Path(raw).expanduser()
        if not root.is_absolute():
            raise ContractError("External roots must be explicitly absolute")
        _reject_links(root)
        root = _canonical_path(root.resolve())
        with self._external_guard:
            if key in self._external:
                raise ContractError("Duplicate external root identity")
            for previous in (self.project_root, *self._external.values()):
                if root.is_relative_to(previous) or previous.is_relative_to(root):
                    raise ContractError("Overlapping roots must use a single stable identity")
            # Readers see one complete immutable mapping, including workers
            # that were created before the user selected this new directory.
            self._external = {**self._external, key: root}

    @property
    def external_roots(self) -> dict[str, Path]:
        return dict(self._external)

    def display(self, ref: PathRef) -> str:
        """A UI label from the known mapping; no filesystem access."""
        if ref.kind == 'project':
            return './' + ref.relative_path
        root = self._external.get(ref.root_id.casefold())
        if root is None:
            return '外部保存位置尚未连接'
        return str(root.joinpath(*ref.relative_path.split('/')))

    def resolve(self, ref: PathRef) -> Path:
        if not isinstance(ref, PathRef):
            raise ContractError("Expected a validated path reference")
        if ref.kind == "project":
            root = self.project_root
        else:
            try:
                root = self._external[ref.root_id.casefold()]
            except KeyError as exc:
                raise ContractError("External output root has not been mapped") from exc
        target = root.joinpath(*ref.relative_path.split("/"))
        _reject_links(target)
        resolved = _canonical_path(target.resolve())
        if not resolved.is_relative_to(root):
            raise ContractError("Output path escapes its declared root")
        return resolved


class InstanceLock:
    """OS file lock; the remaining file after a crash is harmless."""

    def __init__(self, lock_path: str | Path):
        self.path = Path(lock_path).expanduser().absolute()
        self._handle = None
        self._guard = Lock()

    def acquire(self):
        with self._guard:
            if self._handle is not None:
                raise AlreadyRunningError("Writer lock is already acquired")
            _reject_links(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                raise AlreadyRunningError("The library already has a writer") from exc
            except BaseException:
                handle.close()
                raise
            self._handle = handle
        return self

    def release(self):
        with self._guard:
            handle, self._handle = self._handle, None
            if handle is None:
                return
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()
