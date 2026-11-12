"""Portable desktop preferences; no credentials, authentication or import-time I/O."""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, fields

from core.library_paths import PathResolver
from core.task_contracts import BatchBudget, CollectionRequest, FilterSpec, OutputSpec, PathRef, QuerySpec, canonical_json

_SETTINGS_REF = PathRef(relative_path='config/collection_v2.json')
_MAX_SETTINGS_BYTES = 32 * 1024


class SettingsError(ValueError):
    pass


def _number(value, name, minimum=1, maximum=1_000_000):
    if type(value) is int:
        number = value
    elif isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        number = int(value.strip())
    else:
        raise SettingsError(name)
    if not minimum <= number <= maximum:
        raise SettingsError(name)
    return number


@dataclass(frozen=True, slots=True)
class CollectionSettings:
    output_relative: str = 'downl_Pixiv'
    output_kind: str = 'project'
    output_root_id: str = 'project'
    folder_by_artist: bool = True
    name_template: str = '{illust_id}_p{page_index}'
    workers: int = 3
    api_interval: float = .5
    image_interval: float = .5
    user_agent: str = ''
    mode: str = 'history'
    min_bookmarks_enabled: bool = False
    min_bookmarks: str = '1000'
    exclude_ai: bool = False
    max_artists: int = 30
    max_requests: int = 80
    max_works: int = 100

    def __post_init__(self):
        try:
            ref = self.output_ref
        except ValueError:
            raise SettingsError('output_relative') from None
        if ref.kind == 'project' and ref.relative_path.split('/')[0].casefold() in {
            'core', 'gui', 'utils', 'config', 'tests', 'download_logs', 'runtime_logs',
            'record_backups', '.git', '.runtime', 'docs',
        }:
            raise SettingsError('output_relative')
        object.__setattr__(self, 'output_relative', ref.relative_path)
        try:
            OutputSpec(name_template=self.name_template)
        except (ValueError, TypeError):
            raise SettingsError('name_template') from None
        for name in ('folder_by_artist', 'min_bookmarks_enabled', 'exclude_ai'):
            if type(getattr(self, name)) is not bool:
                raise SettingsError(name)
        for name in ('max_artists', 'max_requests', 'max_works'):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        object.__setattr__(self, 'workers', _number(self.workers, 'workers', maximum=8))
        for name in ('api_interval','image_interval'):
            value=getattr(self,name)
            try:
                if type(value) not in (int,float,str):raise ValueError()
                value=float(value)
                if not math.isfinite(value) or not 0<=value<=60:raise ValueError()
            except (ValueError,TypeError,OverflowError):raise SettingsError(name) from None
            object.__setattr__(self,name,value)
        if self.mode not in {'history', 'new'}:
            raise SettingsError('mode')
        if (not isinstance(self.min_bookmarks, str) or len(self.min_bookmarks) > 20
                or (self.min_bookmarks_enabled and not self.min_bookmarks.strip())):
            raise SettingsError('min_bookmarks')
        if self.min_bookmarks_enabled:
            _number(self.min_bookmarks, 'min_bookmarks', minimum=0, maximum=1_000_000_000)
        if (not isinstance(self.user_agent, str) or len(self.user_agent) > 512
                or any(ord(char) < 32 for char in self.user_agent)):
            raise SettingsError('user_agent')

    @classmethod
    def from_form(cls, form, *, previous=None):
        if not isinstance(form, dict):
            raise SettingsError('settings')
        allowed = {field.name for field in fields(cls)}
        values = asdict(previous or cls())
        values.update({key: value for key, value in form.items() if key in allowed})
        return cls(**values)

    def form(self):
        return asdict(self)

    @property
    def output_ref(self):
        return PathRef(kind=self.output_kind, root_id=self.output_root_id, relative_path=self.output_relative)

    def request(self, request_id, account_ref, form=None):
        form = {} if form is None else dict(form)
        settings = self.from_form(form, previous=self)
        parameters = {}
        raw = form.get('artist_ids', '').strip()
        if raw:
            import re
            values = []
            for item in re.split(r'[,，;；\s]+', raw):
                match = re.fullmatch(r'(?:https://www\.pixiv\.net/(?:[a-z]{2}/)?users/)?([0-9]{1,32})/?', item)
                if match is None:
                    raise SettingsError('artist_ids')
                if match.group(1) not in values:
                    values.append(match.group(1))
            if len(values) > 200:
                raise SettingsError('artist_ids')
            parameters['artist_ids'] = values
        start_id = str(form.get('start_artist_id', '')).strip()
        ordinal = str(form.get('start_ordinal', '')).strip()
        if sum(bool(value) for value in (raw, start_id, ordinal)) > 1:
            raise SettingsError('starting_point_conflict')
        if start_id:
            if not start_id.isascii() or not start_id.isdecimal() or len(start_id) > 32:
                raise SettingsError('start_artist_id')
            parameters['start_artist_id'] = start_id
        if ordinal:
            parameters['start_ordinal'] = _number(ordinal, 'start_ordinal')
        return CollectionRequest(request_id, account_ref,
            query=QuerySpec(mode=settings.mode, filters=FilterSpec(
                min_bookmarks=_number(settings.min_bookmarks, 'min_bookmarks', minimum=0,
                    maximum=1_000_000_000) if settings.min_bookmarks_enabled else None,
                exclude_ai=settings.exclude_ai), parameters_json=canonical_json(parameters)),
            output=OutputSpec(root_ref=settings.output_ref,
                folder_by_artist=settings.folder_by_artist, name_template=settings.name_template),
            budget=BatchBudget(max_artists=settings.max_artists, max_requests=settings.max_requests,
                max_works=settings.max_works))


def load_settings(resolver: PathResolver):
    path = resolver.resolve(_SETTINGS_REF)
    try:
        with path.open('rb') as stream:
            raw = stream.read(_MAX_SETTINGS_BYTES + 1)
    except FileNotFoundError:
        return CollectionSettings()
    if len(raw) > _MAX_SETTINGS_BYTES:
        raise SettingsError('settings_too_large')
    try:
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get('version') != 1
                or set(data) != {'version', 'preferences'} or not isinstance(data['preferences'], dict)):
            raise SettingsError('settings_format')
        allowed = {field.name for field in fields(CollectionSettings)}
        if set(data['preferences']) - allowed:
            raise SettingsError('settings_unknown_fields')
        return CollectionSettings(**data['preferences'])
    except (TypeError, ValueError) as error:
        if isinstance(error, SettingsError):
            raise
        raise SettingsError('settings_format') from None


def save_settings(resolver: PathResolver, settings: CollectionSettings):
    if not isinstance(settings, CollectionSettings):
        raise SettingsError('settings')
    final = resolver.resolve(_SETTINGS_REF)
    final.parent.mkdir(parents=True, exist_ok=True)
    final = resolver.resolve(_SETTINGS_REF)
    descriptor, name = tempfile.mkstemp(prefix='.collection-', suffix='.tmp', dir=final.parent)
    temp = final.parent / name
    identity = None
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            info = os.fstat(stream.fileno()); identity = (info.st_dev, info.st_ino)
            data = json.dumps({'version': 1, 'preferences': asdict(settings)}, ensure_ascii=False, indent=2)
            stream.write((data + '\n').encode('utf-8')); stream.flush(); os.fsync(stream.fileno())
        info = temp.stat()
        if (info.st_dev, info.st_ino) != identity or resolver.resolve(_SETTINGS_REF) != final:
            raise SettingsError('settings_path_changed')
        os.replace(temp, final)
    finally:
        try:
            info = temp.lstat()
            if identity is not None and (info.st_dev, info.st_ino) == identity and not temp.is_symlink():
                resolver.resolve(_SETTINGS_REF)
                temp.unlink()
        except (OSError, ValueError):
            pass
