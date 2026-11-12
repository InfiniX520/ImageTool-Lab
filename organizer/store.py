"""Small durable journal. Intent is committed before any remote side effect."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3

from organizer.common import groups, identity, now, safe_path, text


def key(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}',value):
        raise ValueError('invalid_document_key')
    return value


def encode(value):
    if not isinstance(value,dict):
        raise ValueError('document_object_required')
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(raw.encode('utf-8'))>2*1024*1024:
        raise ValueError('document_too_large')
    return raw


class Store:
    def __init__(self,path):
        path=Path(path).absolute()
        safe_path(path.parent,path.name)
        for suffix in ('-wal','-shm','-journal'):
            safe_path(path.parent,path.name+suffix)
        path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=3,isolation_level=None)
        try:
            version=self.db.execute('PRAGMA user_version').fetchone()[0]
            tables=self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            application=self.db.execute('PRAGMA application_id').fetchone()[0]
            if (version not in (0,1) or (version==0 and (tables or application))
                or (version==1 and (application!=0x4A504F31 or {row[0] for row in tables}!={'documents','events'}))):
                raise ValueError('unsupported_organizer_database')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            with self.transaction():
                self.db.execute('CREATE TABLE IF NOT EXISTS documents(kind TEXT NOT NULL,key TEXT NOT NULL,body TEXT NOT NULL,updated INTEGER NOT NULL,PRIMARY KEY(kind,key))')
                self.db.execute('CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,created TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,event TEXT NOT NULL)')
                self.db.execute('PRAGMA user_version=1')
                self.db.execute('PRAGMA application_id=1246777137')
        except BaseException:
            self.db.close();raise

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK');raise

    def _put(self,kind,document_id,value,create_only=False,event='put'):
        kind,document_id=key(kind),key(document_id)
        raw=encode(value)
        if create_only and self.db.execute('SELECT 1 FROM documents WHERE kind=? AND key=?',(kind,document_id)).fetchone():
            raise ValueError('document_already_exists')
        sequence=self.db.execute('INSERT INTO events(created,kind,key,event) VALUES(?,?,?,?)',(now(),kind,document_id,event)).lastrowid
        self.db.execute('INSERT INTO documents VALUES(?,?,?,?) ON CONFLICT(kind,key) DO UPDATE SET body=excluded.body,updated=excluded.updated',
                        (kind,document_id,raw,sequence))

    def put(self,kind,key,value,*,create_only=False):
        if kind=='operation':
            raise ValueError('use_operation_methods')
        with self.transaction():
            self._put(kind,key,value,create_only or kind in ('plan','review'))

    def get(self,kind,document_id):
        row=self.db.execute('SELECT body FROM documents WHERE kind=? AND key=?',(key(kind),key(document_id))).fetchone()
        if row is None:
            raise KeyError(document_id)
        return json.loads(row[0])

    def list(self,kind,limit=100):
        if type(limit) is not int or not 1<=limit<=1000:
            raise ValueError('invalid_limit')
        return [{'id':row[0],'data':json.loads(row[1])} for row in self.db.execute(
            'SELECT key,body FROM documents WHERE kind=? ORDER BY updated DESC LIMIT ?',(key(kind),limit))]

    def prepare_operation(self,value):
        value=dict(value)
        for field in ('id','plan_id'):
            key(value[field])
        for field in ('account_id','artist_id'):
            value[field]=identity(value[field])
        value['before']=groups(value['before']);value['after']=groups(value['after'])
        value['tag']=groups([value['tag']])[0]
        if value['action'] not in ('add','remove'):
            raise ValueError('invalid_action')
        expected=set(value['before'])
        if value['action']=='add':
            if value['tag'] in expected:
                raise ValueError('invalid_add')
            expected.add(value['tag'])
        else:
            if value['tag'] not in expected:
                raise ValueError('invalid_remove')
            expected.remove(value['tag'])
        if sorted(expected)!=value['after']:
            raise ValueError('operation_transition_mismatch')
        value.update(state='intent',created_at=now())
        with self.transaction():
            self._put('operation',value['id'],value,True,'intent')

    def set_operation_state(self,op_id,state,*,observed=None,code=None):
        if observed is not None:
            observed=groups(observed)
        if code is not None and not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}',code):
            raise ValueError('invalid_code')
        allowed={'intent':{'confirmed','unknown','conflict','not_applied','failed'},
                 'unknown':{'confirmed','conflict','not_applied'}}
        with self.transaction():
            value=self.get('operation',op_id)
            if value['state']==state:
                if value.get('observed')!=observed or value.get('code')!=code:
                    raise ValueError('terminal_state_changed')
                return
            if state not in allowed.get(value['state'],set()):
                raise ValueError('invalid_operation_transition')
            value.update(state=state,observed=observed,code=code)
            self._put('operation',op_id,value,event=state)

    def operations(self,plan_id=None):
        # SQL filters the JSON field before the bound, so old plans do not crowd out the requested plan.
        rows=self.db.execute("SELECT body FROM documents WHERE kind='operation'"+
            (" AND json_extract(body,'$.plan_id')=?" if plan_id else '')+' ORDER BY rowid LIMIT 10001',
            (key(plan_id),) if plan_id else ()).fetchall()
        if len(rows)>10000:
            raise ValueError('operation_read_limit')
        return [json.loads(row[0]) for row in rows]

    def counts(self):
        return dict(self.db.execute('SELECT kind,count(*) FROM documents GROUP BY kind'))

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self,*_):
        self.close()
