"""User-selected locations, with portable task references and bounded file I/O."""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from core.app_paths import AppPaths
from core.collection_settings import CollectionSettings, SettingsError
from core.library_paths import PathResolver, _canonical_path, _reject_links
from core.task_contracts import PathRef

_ROOTS_REF = PathRef(relative_path='config/output_roots_v1.json')
_RESERVED = {'core', 'gui', 'utils', 'config', 'tests', 'download_logs',
             'runtime_logs', 'record_backups', '.git', '.runtime', 'docs'}


class LocationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ExportTarget:
    ref: PathRef
    resolver: PathResolver
    display_path: str


def _selected_path(value):
    if not isinstance(value, str) or not value or len(value) > 32760:
        raise LocationError('location_invalid')
    path = Path(value)
    if not path.is_absolute():
        raise LocationError('location_invalid')
    _reject_links(path)
    return _canonical_path(path.resolve())


def _write_mapping(resolver, values):
    final = resolver.resolve(_ROOTS_REF)
    final.parent.mkdir(parents=True, exist_ok=True)
    final = resolver.resolve(_ROOTS_REF)
    descriptor, name = tempfile.mkstemp(prefix='.output-roots-', suffix='.tmp', dir=final.parent)
    temp = Path(name)
    identity = None
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino)
            data = json.dumps({'version': 1, 'roots': {key: str(path) for key, path in values.items()}},
                              ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
            if len(data) > 256 * 1024:
                raise LocationError('location_limit')
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        info = temp.stat()
        if (info.st_dev, info.st_ino) != identity or resolver.resolve(_ROOTS_REF) != final:
            raise LocationError('location_changed')
        _reject_links(temp)
        os.replace(temp, final)
    finally:
        try:
            info = temp.lstat()
            if identity == (info.st_dev, info.st_ino) and not temp.is_symlink():
                resolver.resolve(_ROOTS_REF)
                temp.unlink()
        except (OSError, ValueError):
            pass


class OutputLocations:
    """Called on the desktop I/O owner, never while rendering or polling Tk."""

    def __init__(self, paths, resolver):
        self.paths, self.resolver = paths, resolver

    def load(self):
        try:
            with self.resolver.resolve(_ROOTS_REF).open('rb') as stream:
                raw = stream.read(256 * 1024 + 1)
        except FileNotFoundError:
            return
        try:
            if len(raw) > 256 * 1024:
                raise ValueError()
            data = json.loads(raw)
            if (not isinstance(data, dict) or set(data) != {'version', 'roots'}
                    or type(data['version']) is not int or data['version'] != 1
                    or not isinstance(data['roots'], dict) or len(data['roots']) > 64
                    or any(not isinstance(value, str) for value in data['roots'].values())):
                raise ValueError()
            # Validate the complete mapping before publishing any identity.
            checked = PathResolver(self.paths, data['roots']).external_roots
            for key, path in checked.items():
                self.resolver.register_external(key, path)
        except (ValueError, TypeError, OSError):
            raise LocationError('location_mapping_invalid') from None

    def form(self, settings):
        values = settings.form()
        ref = settings.output_ref
        if ref.kind == 'project':
            display = './' + ref.relative_path
        else:
            display = str(self.resolver.resolve(ref))
        return dict(values, output_display=display)

    def dialog_directory(self, form):
        try:
            ref = CollectionSettings.from_form(form).output_ref
            path = self.resolver.resolve(ref)
        except ValueError:
            path = self.resolver.project_root
        while not path.is_dir() and path != path.parent:
            path = path.parent
        return str(path)

    def select_directory(self, selected):
        path = _selected_path(selected)
        if not path.is_dir():
            raise LocationError('location_not_directory')
        project = self.resolver.project_root
        if path.is_relative_to(project):
            relative = path.relative_to(project).as_posix()
            # CollectionSettings rejects root/program/database directories.
            settings = CollectionSettings(output_relative=relative)
            return {key: value for key, value in self.form(settings).items() if key.startswith('output_')}
        roots = self.resolver.external_roots
        for key, root in roots.items():
            if path.is_relative_to(root):
                settings = CollectionSettings(output_kind='external', output_root_id=key,
                                              output_relative=path.relative_to(root).as_posix())
                return {key: value for key, value in self.form(settings).items() if key.startswith('output_')}
        if len(roots) >= 64:
            raise LocationError('location_limit')
        key = 'folder_' + uuid.uuid4().hex
        try:
            proposed = PathResolver(self.paths, {**roots, key: path})
        except ValueError:
            raise LocationError('location_overlap') from None
        # A failed save must not expose a root that cannot survive restart.
        _write_mapping(self.resolver, proposed.external_roots)
        self.resolver.register_external(key, path)
        settings = CollectionSettings(output_kind='external', output_root_id=key, output_relative='.')
        return {key: value for key, value in self.form(settings).items() if key.startswith('output_')}

    def export_target(self, selected, kind):
        path = _selected_path(selected)
        allowed = {'.md'} if kind == 'records' else {'.txt', '.log'} if kind == 'log' else set()
        if path.suffix.lower() not in allowed:
            raise LocationError('export_extension')
        if path.is_relative_to(self.resolver.project_root):
            relative = path.relative_to(self.resolver.project_root)
            if not relative.parts or relative.parts[0].casefold() in _RESERVED:
                raise LocationError('export_protected')
        if not path.parent.is_dir() or path.is_dir():
            raise LocationError('export_location')
        # An explicit save-as target is a one-off export, not a new download
        # root. Keep it out of persisted task identities and root mappings.
        resolver = PathResolver(AppPaths(path.parent))
        ref = PathRef(relative_path=path.name)
        if resolver.resolve(ref) != path:
            raise LocationError('location_changed')
        return ExportTarget(ref, resolver, str(path))


def read_artist_ids(selected):
    path = _selected_path(selected)
    if path.suffix.lower() != '.txt' or not path.is_file():
        raise LocationError('artist_file')
    with path.open('rb') as stream:
        raw = stream.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise LocationError('artist_file_size')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError:
        raise LocationError('artist_file_encoding') from None
    request = CollectionSettings().request('import', 'offline', {'artist_ids': text})
    ids = json.loads(request.query.parameters_json).get('artist_ids', [])
    if not ids:
        raise LocationError('artist_file_empty')
    return tuple(ids)
