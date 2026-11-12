"""Canonical application paths.

The downloader historically assembled paths independently in the GUI and in
the service layer.  Keeping the small set of *application-owned* paths here
lets callers share one root without changing where existing user data lives.
The module is deliberately side-effect free: constructing :class:`AppPaths`
never creates or writes a file.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_COOKIE_FILENAME = "www.pixiv.net_cookies.txt"
DEFAULT_LEGACY_CONFIG_FILENAME = "pixiv_config.json"
DEFAULT_DOWNLOADED_IDS_FILENAME = "downloaded_ids.txt"
DEFAULT_DOWNLOAD_LOG_DIRNAME = "download_logs"


def resolve_project_root(project_root: str | Path | None = None) -> Path:
    """Return an absolute project root.

    ``None`` resolves to the repository/application directory (the parent of
    ``core``), which is also the location used by the legacy implementation.
    ``Path.resolve`` is used for an explicit root so that all derived paths
    compare consistently even when the caller passes a relative path.
    """

    if project_root is None:
        # In a PyInstaller build ``__file__`` points into the temporary
        # ``_internal`` tree.  User-owned configuration/data must live next
        # to the executable so an update cannot silently discard it.
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parents[1]
    return Path(project_root).expanduser().resolve()


@dataclass(frozen=True, init=False)
class AppPaths:
    """Paths owned by the downloader application.

    The constructor accepts either a positional root, ``project_root=`` or
    ``root=`` for compatibility with small integration scripts.  No path is
    created as a side effect.
    """

    project_root: Path

    def __init__(
        self,
        project_root: str | Path | None = None,
        *,
        root: str | Path | None = None,
    ) -> None:
        if project_root is not None and root is not None:
            raise TypeError("pass either project_root or root, not both")
        object.__setattr__(
            self,
            "project_root",
            resolve_project_root(project_root if project_root is not None else root),
        )

    @property
    def root(self) -> Path:
        """Alias for :attr:`project_root`."""

        return self.project_root

    @property
    def config_dir(self) -> Path:
        return self.project_root / "config"

    @property
    def cookie_file(self) -> Path:
        """Preferred Netscape cookie export path."""

        return self.project_root / DEFAULT_COOKIE_FILENAME

    @property
    def cookies_file(self) -> Path:
        """Plural alias used by a few integrations."""

        return self.cookie_file

    @property
    def cookie_path(self) -> Path:
        return self.cookie_file

    @property
    def legacy_config_file(self) -> Path:
        return self.project_root / DEFAULT_LEGACY_CONFIG_FILENAME

    @property
    def legacy_config_path(self) -> Path:
        return self.legacy_config_file

    @property
    def downloaded_ids_file(self) -> Path:
        return self.project_root / DEFAULT_DOWNLOADED_IDS_FILENAME

    @property
    def downloaded_ids_path(self) -> Path:
        return self.downloaded_ids_file

    @property
    def download_logs_dir(self) -> Path:
        return self.project_root / DEFAULT_DOWNLOAD_LOG_DIRNAME

    @property
    def central_log_dir(self) -> Path:
        return self.download_logs_dir

    @property
    def download_logs_path(self) -> Path:
        return self.download_logs_dir

    @property
    def cookie_candidates(self) -> tuple[Path, ...]:
        """Cookie paths in priority order.

        Credentials are intentionally restricted to the application data
        directory.  Looking in an arbitrary current working directory could
        let an unrelated file silently override the user's session.
        Duplicate paths are removed while preserving order.
        """

        candidates = (self.cookie_file,)
        unique: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            resolved = candidate.expanduser().resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique.append(resolved)
        return tuple(unique)

    def resolve(self, path: str | Path) -> Path:
        """Resolve a path relative to the project root when necessary."""

        target = Path(path).expanduser()
        if not target.is_absolute():
            target = self.project_root / target
        return target.resolve()


def get_app_paths(project_root: str | Path | None = None) -> AppPaths:
    """Return :class:`AppPaths` for ``project_root`` (or the default root)."""

    return AppPaths(project_root)


__all__ = [
    "AppPaths",
    "DEFAULT_COOKIE_FILENAME",
    "DEFAULT_DOWNLOADED_IDS_FILENAME",
    "DEFAULT_DOWNLOAD_LOG_DIRNAME",
    "DEFAULT_LEGACY_CONFIG_FILENAME",
    "get_app_paths",
    "resolve_project_root",
]
