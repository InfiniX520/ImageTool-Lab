"""Directory-creation races must not turn a valid output into a false escape."""
import os
from pathlib import Path

import pytest

from core.app_paths import AppPaths
from core.library_paths import PathResolver, _canonical_path
from core.task_contracts import ContractError, PathRef

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows path namespace semantics')


def test_extended_path_return_during_first_directory_creation_is_same_output(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    resolver = PathResolver(AppPaths(root))
    ref = PathRef(relative_path='downl_Pixiv/101/10100_p0.png')
    expected = root / ref.relative_path
    real_resolve = Path.resolve
    # Observed OS interleaving: ERROR_PATH_NOT_FOUND becomes FILE_NOT_FOUND
    # after another page worker creates the parent. realpath keeps \\?\.
    def created_between_lookups(path, *args, **kwargs):
        if path == expected:
            expected.parent.mkdir(parents=True, exist_ok=True)
            return Path('\\\\?\\' + str(expected))
        return real_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', created_between_lookups)
    assert resolver.resolve(ref) == expected
    assert resolver.resolve(ref).parent == expected.parent


def test_extended_prefix_does_not_allow_an_actual_escape(tmp_path, monkeypatch):
    resolver = PathResolver(AppPaths(tmp_path / 'project'))
    outside = tmp_path / 'outside/file.png'
    monkeypatch.setattr(Path, 'resolve', lambda path, **kwargs: Path('\\\\?\\' + str(outside)))
    with pytest.raises(ContractError, match='escapes'):
        resolver.resolve(PathRef(relative_path='downl_Pixiv/file.png'))


def test_unc_aliases_normalize_but_device_paths_stay_distinct():
    assert _canonical_path(Path(r'\\?\UNC\server\share\a.png')) == Path(r'\\server\share\a.png')
    device = Path(r'\\?\GLOBALROOT\Device\HarddiskVolume1\a.png')
    assert _canonical_path(device) == device


def test_extended_external_alias_cannot_bypass_overlapping_root_guard(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    with pytest.raises(ContractError, match='Overlapping'):
        PathResolver(AppPaths(root), {'archive': Path('\\\\?\\' + str(root / 'archive'))})
