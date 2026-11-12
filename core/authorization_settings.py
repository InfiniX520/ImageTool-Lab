"""Explicit local authorization update; never log or return the credential."""
import json
import os
import tempfile

from core.task_contracts import PathRef


class AuthorizationSettingsError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def save_authorization(resolver, credential):
    if (not isinstance(credential, str) or not 8 <= len(credential.strip()) <= 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in credential.strip())):
        raise AuthorizationSettingsError('authorization_format')
    credential = credential.strip()
    ref = PathRef(relative_path='pixiv_config.json')
    target = resolver.resolve(ref)
    try:
        with target.open('rb') as stream:
            before = stream.read(65537)
    except FileNotFoundError:
        before = None
    try:
        values = {} if before is None else json.loads(before.decode('utf-8-sig'))
        if (before is not None and len(before) > 65536) or not isinstance(values, dict):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise AuthorizationSettingsError('authorization_config_invalid') from None
    values['token'] = credential
    data = (json.dumps(values, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if len(data) > 65536:
        raise AuthorizationSettingsError('authorization_config_invalid')
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.authorization-', suffix='.tmp', dir=target.parent)
    temp = target.parent / name
    identity = None
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            info = os.fstat(stream.fileno()); identity = (info.st_dev, info.st_ino)
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        if resolver.resolve(ref) != target:
            raise AuthorizationSettingsError('authorization_config_changed')
        try:
            with target.open('rb') as stream:
                current = stream.read(65537)
        except FileNotFoundError:
            current = None
        info = temp.lstat()
        if current != before or temp.is_symlink() or (info.st_dev, info.st_ino) != identity:
            raise AuthorizationSettingsError('authorization_config_changed')
        os.replace(temp, target)
    finally:
        try:
            info = temp.lstat()
            if not temp.is_symlink() and (info.st_dev, info.st_ino) == identity:
                temp.unlink()
        except FileNotFoundError:
            pass
    return {'saved': True}
