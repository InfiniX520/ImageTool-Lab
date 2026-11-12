"""Existing-browser session adapter; no Cookie or CSRF export."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

from organizer.common import OrganizerError, groups, identity, integer, safe_path


class BrowserConsole:
    def __init__(self, browser_pid=0, temporary_root=None):
        self.browser_pid = integer(browser_pid, 0, 2**31-1)
        self.folder = Path(__file__).resolve().parent
        self.temporary_root = Path(temporary_root) if temporary_root else self.folder.parent / '.runtime/organizer_bridge'

    def _run(self, extra, timeout=50):
        if os.name != 'nt':
            raise OrganizerError('windows_browser_bridge_required')
        try:
            result = subprocess.run(['powershell.exe','-NoProfile','-STA','-File',
                str(self.folder/'browser_console.ps1'), *extra], capture_output=True, timeout=timeout,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            # PowerShell's console encoding is set by the UTF-8 wrapper below.
            raw = result.stdout.decode('utf-8-sig')
            if len(raw) > 2100000:
                raise OrganizerError('browser_result_too_large')
            value = json.loads(raw)
        except subprocess.TimeoutExpired:
            raise OrganizerError('browser_result_timeout') from None
        except (OSError, ValueError, UnicodeError):
            raise OrganizerError('browser_bridge_failed') from None
        if result.returncode and isinstance(value,dict):
            raise OrganizerError(value.get('code','browser_bridge_failed'))
        return value

    def windows(self):
        return self._run(['-Mode','list'], timeout=15)

    def execute(self, command):
        command = dict(command, nonce=uuid.uuid4().hex)
        source = (self.folder/'page_bridge.js').read_text(encoding='utf-8')
        # A string literal keeps DevTools from rendering thousands of syntax nodes per call.
        script = 'void (0,eval)(' + json.dumps(source,ensure_ascii=True) + ')(' + json.dumps(command,ensure_ascii=True) + ');'
        folder = safe_path(self.temporary_root.parent, self.temporary_root.name)
        folder.mkdir(parents=True,exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix='jpgpng-browser-',suffix='.js',dir=folder)
        try:
            with os.fdopen(descriptor,'w',encoding='utf-8',newline='\n') as stream:
                stream.write(script)
            value = self._run(['-Mode','execute','-ScriptPath',name,'-Nonce',command['nonce'],
                              '-BrowserProcessId',str(self.browser_pid)])
            if not isinstance(value,dict) or value.get('nonce')!=command['nonce']:
                raise OrganizerError('browser_result_mismatch')
            return value
        finally:
            Path(name).unlink(missing_ok=True)


class WebClient:
    def __init__(self, bridge, budget, account_id=None):
        self.bridge, self.budget = bridge, budget
        self.account_id = identity(account_id) if account_id else None

    def call(self, op, **params):
        self.budget.reserve({'identity':0,'add':2,'remove':2,'catalog':2,'observe':3}.get(op,1))
        result = self.bridge.execute(dict(params, op=op, account_id=self.account_id))
        trace = result.get('trace',[])
        self.budget.observed += len(trace)
        if not result.get('ok'):
            raise OrganizerError(result.get('code','browser_request_failed'))
        account = identity(result.get('account_id'))
        if self.account_id is not None and account != self.account_id:
            raise OrganizerError('account_mismatch')
        self.account_id = account
        return result['body']

    def connect(self):
        return self.call('identity')

    def following(self, visibility, offset, tag=None):
        params={} if tag is None else {'tag':groups([tag])[0]}
        result = self.call('following',visibility=visibility,offset=integer(offset),**params)
        result['total'] = integer(result['total'])
        result['catalog'] = groups(result['catalog'],maximum=1000)
        users = result['users']
        if not isinstance(users,list) or len(users)>30:
            raise OrganizerError('invalid_follow_page')
        for row in users:
            row['artist_id'] = identity(row['artist_id'])
        if len({r['artist_id'] for r in users})!=len(users):
            raise OrganizerError('duplicate_follow_page')
        return result

    def catalog(self):
        result=self.call('catalog')
        if not isinstance(result,dict) or set(result)!={'show','hide'}:
            raise OrganizerError('invalid_catalog')
        return {side:groups(result[side],maximum=1000) for side in ('show','hide')}

    def observe(self,artist_id):
        artist_id=identity(artist_id)
        result=self.call('observe',artist_id=artist_id)
        detail=result['detail'];catalog=result['catalog']
        if detail.get('artist_id')!=artist_id or detail.get('visibility') not in ('0','1') or set(catalog)!={'show','hide'}:
            raise OrganizerError('invalid_observation')
        detail['groups']=groups(detail['groups'])
        return detail,{side:groups(catalog[side],maximum=1000) for side in ('show','hide')}

    def detail(self, artist_id):
        artist_id = identity(artist_id)
        result = self.call('detail',artist_id=artist_id)
        if result.get('artist_id')!=artist_id or result.get('visibility') not in ('0','1'):
            raise OrganizerError('detail_identity_mismatch')
        result['groups'] = groups(result['groups'])
        return result

    def mutate(self, action, artist_id, tag, before, visibility):
        if action not in ('add','remove'):
            raise OrganizerError('invalid_action')
        return self.call(action,artist_id=identity(artist_id),tag=groups([tag])[0],before=groups(before),visibility=visibility)
