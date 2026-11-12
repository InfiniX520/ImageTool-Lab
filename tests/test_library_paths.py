"""Check actual filesystem identity/locks without touching production data."""
import os
import subprocess
import sys

import pytest

from core.app_paths import AppPaths
from core.library_paths import AlreadyRunningError, InstanceLock, PathResolver
from core.task_contracts import ContractError, PathRef


def test_project_move_and_unrelated_cwd_preserve_identity(tmp_path, monkeypatch):
    first, second = tmp_path / "old", tmp_path / "new"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(tmp_path)
    ref = PathRef(relative_path="downl_Pixiv/artist/42_p0.png")
    assert PathResolver(AppPaths(first)).resolve(ref) == first / ref.relative_path
    assert PathResolver(AppPaths(second)).resolve(ref) == second / ref.relative_path
    assert not (first / "downl_Pixiv").exists()


def test_external_root_needs_explicit_mapping_and_rejects_aliases(tmp_path):
    paths = AppPaths(tmp_path / "project")
    ref = PathRef(kind="external", root_id="archive", relative_path="artist/42.png")
    with pytest.raises(ContractError, match="not been mapped"):
        PathResolver(paths).resolve(ref)
    resolver = PathResolver(paths, {"archive": tmp_path / "external"})
    assert resolver.resolve(ref) == tmp_path / "external/artist/42.png"
    with pytest.raises(ContractError, match="Overlapping"):
        PathResolver(paths, {"archive": paths.root / "download"})
    with pytest.raises(ContractError, match="Overlapping"):
        PathResolver(paths, {"a": tmp_path / "external", "b": tmp_path / "external/subfolder"})


def test_existing_symlink_cannot_redirect_output(tmp_path):
    root, outside = tmp_path / "project", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "redirect").symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("OS account cannot create a test symlink")
        import _winapi
        _winapi.CreateJunction(str(outside), str(root / "redirect"))
        assert (root / "redirect").is_junction()
    with pytest.raises(ContractError, match="Linked"):
        PathResolver(AppPaths(root)).resolve(PathRef(relative_path="redirect/a.png"))


def test_lock_excludes_second_writer_and_allows_reopen(tmp_path):
    path = tmp_path / ".writer.lock"
    with InstanceLock(path):
        with pytest.raises(AlreadyRunningError):
            InstanceLock(path).acquire()
    assert path.exists()
    with InstanceLock(path):
        pass


def test_process_exit_releases_real_os_lock(tmp_path):
    path = tmp_path / ".writer.lock"
    script = (
        "import os,sys;from core.library_paths import InstanceLock;"
        "lock=InstanceLock(sys.argv[1]);lock.acquire();os._exit(0)"
    )
    result = subprocess.run([sys.executable, "-s", "-B", "-c", script, str(path)],
                            cwd=os.getcwd(), capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    with InstanceLock(path):
        pass
