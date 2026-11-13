"""Follow snapshots and source admission on the existing single writer."""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from core.follow_contracts import FollowingPage
from core.task_contracts import canonical_json, normalize_json


FOLLOW_DDL = (
    """CREATE TABLE source_batch_pages (
        batch_id TEXT NOT NULL REFERENCES batches(batch_id),
        source_id TEXT NOT NULL,
        pass_id TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        page_identity TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        delivery_ids_json TEXT NOT NULL,
        PRIMARY KEY (batch_id,source_id,pass_id,entity_id,page_identity)
    )""",
    """CREATE TABLE follow_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        account_ref TEXT NOT NULL,
        origin_job_id TEXT NOT NULL REFERENCES jobs(job_id),
        state TEXT NOT NULL CHECK (state IN ('building','complete','coverage_uncertain')),
        cursor_json TEXT NOT NULL,
        expected_total INTEGER CHECK (expected_total IS NULL OR expected_total >= 0),
        coverage_complete INTEGER NOT NULL CHECK (coverage_complete IN (0,1)),
        created_at TEXT NOT NULL,
        finished_at TEXT
    )""",
    """CREATE TABLE follow_snapshot_members (
        snapshot_id TEXT NOT NULL REFERENCES follow_snapshots(snapshot_id),
        artist_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        PRIMARY KEY (snapshot_id,artist_id),
        UNIQUE (snapshot_id,ordinal)
    )""",
    """CREATE TABLE follow_artists (
        account_ref TEXT NOT NULL,
        artist_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        is_following INTEGER NOT NULL CHECK (is_following IN (0,1)),
        first_seen_snapshot TEXT NOT NULL REFERENCES follow_snapshots(snapshot_id),
        last_seen_snapshot TEXT NOT NULL REFERENCES follow_snapshots(snapshot_id),
        PRIMARY KEY (account_ref,artist_id),
        UNIQUE (account_ref,ordinal)
    )""",
    """CREATE TABLE follow_snapshot_pages (
        snapshot_id TEXT NOT NULL REFERENCES follow_snapshots(snapshot_id),
        page_identity TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        PRIMARY KEY (snapshot_id,page_identity)
    )""",
    "CREATE UNIQUE INDEX ux_follow_snapshot_building ON follow_snapshots(account_ref) WHERE state='building'",
    "CREATE INDEX idx_follow_snapshot_account ON follow_snapshots(account_ref,created_at,snapshot_id)",
    "CREATE INDEX idx_follow_artists_active ON follow_artists(account_ref,is_following,ordinal)",
)
FOLLOW_TABLES = frozenset({'source_batch_pages','follow_snapshots','follow_snapshot_members','follow_artists','follow_snapshot_pages'})
FOLLOW_COMMANDS = frozenset({'begin_follow_snapshot','accept_following_page','reserve_source_request','admit_source_artist','carry_unresolved_items','record_source_cooldown'})


def _lib():
    from core import library_repository
    return library_repository


def _snapshot(row):
    if row is None:
        return None
    result = dict(row)
    result['coverage_complete'] = bool(row['coverage_complete'])
    normalize_json(result['cursor_json'])
    return result


class FollowRepositoryMixin:
    def source_cooldown(self,account_ref):
        _lib()._validate_identifier(account_ref,'account reference')
        self._ensure_open()
        with closing(self._readonly_connection()) as conn:
            return self._source_cooldown_conn(conn,account_ref)

    def _source_cooldown_conn(self,conn,account_ref):
        row=conn.execute("SELECT MAX(json_extract(e.safe_payload,'$.retry_at')) AS until FROM events e "
            "JOIN jobs j ON j.job_id=e.job_id WHERE j.account_ref=? AND e.kind='source_cooldown'",(account_ref,)).fetchone()
        value=row['until']
        if value is None:
            return None
        try:
            until=datetime.fromisoformat(value)
            now=datetime.fromisoformat(_lib()._utcnow())
            if until.tzinfo is None or now.tzinfo is None:raise ValueError()
        except (ValueError,TypeError):
            raise _lib().CorruptDataError('Corrupt source cooldown') from None
        return value if until>now else None

    def record_source_cooldown(self,batch_id,seconds,*,reason_code):
        _lib()._validate_identifier(batch_id,'batch id')
        if type(seconds) is not int or not 1<=seconds<=86400:
            raise _lib().LibraryError('Invalid source cooldown duration')
        if reason_code not in {'metadata_rate_limited','metadata_server_unavailable'}:
            raise _lib().LibraryError('Invalid source cooldown code')
        return self._enqueue('record_source_cooldown',{'batch_id':batch_id,'seconds':seconds,'reason_code':reason_code})

    def _record_source_cooldown(self,conn,payload):
        lib=_lib()
        with self._transaction(conn):
            batch=self._get_batch_row(conn,payload['batch_id'])
            if batch is None:raise lib.NotFoundError('Unknown cooldown batch')
            job=self._get_job_row(conn,batch['job_id'])
            request=self._request_from_row(job)
            if request.query.source!='follow':raise lib.InvalidStateError('Cooldown source does not match')
            until=(datetime.fromisoformat(lib._utcnow())+timedelta(seconds=payload['seconds'])).astimezone(timezone.utc).isoformat(timespec='microseconds')
            self._insert_event(conn,job_id=job['job_id'],batch_id=batch['batch_id'],kind='source_cooldown',
                payload={'retry_at':until,'reason_code':payload['reason_code']})
            return until

    def incomplete_artists(self,job_id,*,after='',limit=100):
        lib=_lib()
        lib._validate_identifier(job_id,'job id')
        if not isinstance(after,str) or type(limit) is not int or not 1<=limit<=200:
            raise lib.LibraryError('Invalid artist page')
        self._ensure_open()
        with closing(self._readonly_connection()) as conn:
            if self._get_job_row(conn,job_id) is None:
                raise lib.NotFoundError('Unknown source job')
            rows=conn.execute('SELECT bi.artist_id,MAX(bi.artist_name) AS display_name '
                'FROM batch_items bi JOIN batches b ON b.batch_id=bi.batch_id '
                "WHERE b.job_id=? AND bi.artist_id IS NOT NULL AND bi.artist_id!='' AND bi.artist_id>? "
                "AND bi.result_state NOT IN ('completed','filtered','skipped_existing') "
                'AND NOT EXISTS (SELECT 1 FROM batch_items newer JOIN batches nb ON nb.batch_id=newer.batch_id '
                'WHERE nb.job_id=b.job_id AND newer.illust_id=bi.illust_id AND nb.rowid>b.rowid) '
                'GROUP BY bi.artist_id ORDER BY bi.artist_id LIMIT ?', (job_id,after,limit)).fetchall()
        return tuple(dict(row) for row in rows)

    def carry_unresolved_items(self,batch_id,*,after='',limit=100,enforce_health=True,health=None):
        _lib()._validate_identifier(batch_id,'batch id')
        if not isinstance(after,str) or (after and not after.isdecimal()) or type(limit) is not int or not 1<=limit<=200:
            raise _lib().LibraryError('Invalid unresolved item page')
        if enforce_health is not True:
            raise _lib().LibraryError('Carry health verification cannot be disabled')
        if health is not None and not isinstance(health,list):
            raise _lib().LibraryError('Invalid carry health evidence')
        return self._enqueue('carry_unresolved_items',{'batch_id':batch_id,'after':after,'limit':limit,
            'enforce_health':enforce_health,'health':[] if health is None else health})

    def _carry_unresolved_items(self,conn,payload):
        lib=_lib()
        with self._transaction(conn):
            batch,job,request=self._follow_batch(conn,payload['batch_id'])
            rows=conn.execute('SELECT bi.* FROM batch_items bi JOIN batches b ON b.batch_id=bi.batch_id '
                'WHERE b.job_id=? AND bi.batch_id!=? AND bi.illust_id>? '
                'AND NOT EXISTS (SELECT 1 FROM batch_items newer JOIN batches nb ON nb.batch_id=newer.batch_id '
                'WHERE nb.job_id=b.job_id AND newer.illust_id=bi.illust_id AND nb.rowid>b.rowid) '
                "AND (bi.decision IN ('metadata_unknown','unavailable','unsupported') OR (bi.decision='download' "
                "AND bi.result_state IN ('failed','needs_verification','partial') AND NOT EXISTS ("
                'SELECT 1 FROM batch_item_pages bip JOIN deliveries d ON d.delivery_id=bip.delivery_id '
                "WHERE bip.batch_id=bi.batch_id AND bip.illust_id=bi.illust_id AND d.state IN ('pending','transferring','validating','suspended','retry_wait')))) "
                'ORDER BY bi.illust_id LIMIT ?', (job['job_id'],batch['batch_id'],payload['after'],payload['limit'])).fetchall()
            now=lib._utcnow()
            for item in rows:
                reasons=json.loads(item['reason_codes_json'])
                if 'carried_unresolved' not in reasons:
                    reasons.append('carried_unresolved')
                provenance=json.loads(item['provenance_json'])
                provenance['local_carry_from_batch']=item['batch_id']
                conn.execute('INSERT INTO batch_items(batch_id,illust_id,artist_id,artist_name,title,page_count,decision,reason_codes_json,'
                    'required_pages_json,result_state,bookmarks,ai_flag,provenance_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (batch['batch_id'],item['illust_id'],item['artist_id'],item['artist_name'],item['title'],item['page_count'],item['decision'],
                     canonical_json(reasons),item['required_pages_json'],item['result_state'],item['bookmarks'],item['ai_flag'],canonical_json(provenance),now,now))
                self._copy_batch_item_page_links(
                    conn,item['batch_id'],batch['batch_id'],item['illust_id'],
                    health_by_artifact=self._health_map(payload.get('health')),
                    enforce_completed_health=bool(payload.get('enforce_health')),
                )
                self._refresh_batch_item_result(conn,batch['batch_id'],item['illust_id'],now)
            if rows:
                self._insert_event(conn,job_id=job['job_id'],batch_id=batch['batch_id'],kind='unresolved_items_carried',
                    payload={'count':len(rows),'illust_ids':[item['illust_id'] for item in rows]})
            return {'count':len(rows),'after':rows[-1]['illust_id'] if rows else None}

    def previous_artist_checkpoint(self, job_id, source_id, pass_id, artist_id):
        lib = _lib()
        for value in (job_id,source_id,pass_id,artist_id):
            lib._validate_identifier(value,'source identity')
        self._ensure_open()
        with closing(self._readonly_connection()) as conn:
            row = conn.execute('SELECT c.* FROM checkpoints c JOIN jobs j ON j.job_id=c.job_id '
                'JOIN jobs target ON target.job_id=? WHERE j.job_id!=target.job_id '
                'AND j.scope_fingerprint=target.scope_fingerprint AND j.account_ref=target.account_ref '
                'AND c.source_id=? AND c.pass_id=? AND c.entity_id=? AND c.exhausted=1 '
                "AND json_extract(c.coverage_json,'$.coverage_complete')=1 "
                'AND NOT EXISTS (SELECT 1 FROM batch_items bi JOIN batches b ON b.batch_id=bi.batch_id '
                "WHERE b.job_id=j.job_id AND bi.artist_id=? AND bi.result_state IN ('metadata_unknown','unavailable','failed','needs_verification','partial')) "
                'ORDER BY c.updated_at DESC,j.rowid DESC LIMIT 1',
                (job_id,source_id,pass_id,artist_id,artist_id)).fetchone()
        return dict(row) if row is not None else None

    def latest_follow_snapshot(self, account_ref):
        _lib()._validate_identifier(account_ref, 'account reference')
        self._ensure_open()
        with closing(self._readonly_connection()) as conn:
            row = conn.execute('SELECT * FROM follow_snapshots WHERE account_ref=? ORDER BY rowid DESC LIMIT 1', (account_ref,)).fetchone()
        return _snapshot(row)

    def get_follow_snapshot(self, snapshot_id):
        _lib()._validate_identifier(snapshot_id, 'snapshot id')
        self._ensure_open()
        with closing(self._readonly_connection()) as conn:
            row = conn.execute('SELECT * FROM follow_snapshots WHERE snapshot_id=?', (snapshot_id,)).fetchone()
        if row is None:
            raise _lib().NotFoundError('Unknown follow snapshot')
        return _snapshot(row)

    def begin_follow_snapshot(self, batch_id, command_id, *, refresh=False):
        lib = _lib()
        lib._validate_identifier(batch_id, 'batch id')
        lib._validate_command_id(command_id)
        if type(refresh) is not bool:
            raise lib.LibraryError('refresh must be boolean')
        return self._enqueue('begin_follow_snapshot', {'batch_id':batch_id,'command_id':command_id,'refresh':refresh})

    def accept_following_page(self, batch_id, snapshot_id, page_identity, before_json, page):
        lib = _lib()
        for value, name in [(batch_id,'batch id'),(snapshot_id,'snapshot id'),(page_identity,'page identity')]:
            lib._validate_identifier(value, name)
        if not isinstance(page, FollowingPage):
            raise lib.LibraryError('FollowingPage is required')
        before_json = normalize_json(before_json)
        digest = hashlib.sha256(canonical_json({'before':before_json,'page':asdict(page)}).encode()).hexdigest()
        payload = {'batch_id':batch_id,'snapshot_id':snapshot_id,
            'page_identity':page_identity,'before_json':before_json,'page':page,'digest':digest}
        return self._enqueue('accept_following_page', payload, size_payload=dict(payload,page=asdict(page)))

    def follow_artists(self, account_ref, *, after=-1, limit=100, snapshot_id=None, active_only=True):
        lib = _lib()
        lib._validate_identifier(account_ref, 'account reference')
        if type(after) is not int or after < -1 or type(limit) is not int or not 1 <= limit <= 200:
            raise lib.LibraryError('Invalid artist page')
        if type(active_only) is not bool:
            raise lib.LibraryError('active_only must be boolean')
        self._ensure_open()
        with closing(self._readonly_connection()) as conn:
            if snapshot_id is None:
                rows = conn.execute('SELECT artist_id,display_name,ordinal,is_following FROM follow_artists WHERE account_ref=? AND ordinal>? '
                    + ('AND is_following=1 ' if active_only else '') + 'ORDER BY ordinal LIMIT ?', (account_ref,after,limit)).fetchall()
            else:
                lib._validate_identifier(snapshot_id,'snapshot id')
                snapshot = conn.execute('SELECT account_ref FROM follow_snapshots WHERE snapshot_id=?',(snapshot_id,)).fetchone()
                if snapshot is None or snapshot['account_ref'] != account_ref:
                    raise lib.InvalidStateError('Snapshot belongs to another account')
                rows = conn.execute('SELECT m.artist_id,m.display_name,m.ordinal,a.is_following FROM follow_snapshot_members m '
                    'JOIN follow_artists a ON a.account_ref=? AND a.artist_id=m.artist_id '
                    'WHERE m.snapshot_id=? AND m.ordinal>? ' + ('AND a.is_following=1 ' if active_only else '')
                    + 'ORDER BY m.ordinal LIMIT ?', (account_ref,snapshot_id,after,limit)).fetchall()
        return tuple(dict(row) for row in rows)

    def source_budget_status(self, batch_id):
        _lib()._validate_identifier(batch_id,'batch id')
        self._ensure_open()
        with closing(self._readonly_connection()) as conn:
            batch = self._get_batch_row(conn,batch_id)
            if batch is None:
                raise _lib().NotFoundError('Unknown batch')
            return self._source_budget_conn(conn,batch)

    def reserve_source_request(self, batch_id, *, kind):
        _lib()._validate_identifier(batch_id,'batch id')
        if kind not in {'following','artist_works','new_works','detail'}:
            raise _lib().LibraryError('Invalid source request kind')
        return self._enqueue('reserve_source_request',{'batch_id':batch_id,'kind':kind})

    def admit_source_artist(self, batch_id, artist_id):
        _lib()._validate_identifier(batch_id,'batch id')
        from core.candidate_filter import stable_id
        return self._enqueue('admit_source_artist',{'batch_id':batch_id,'artist_id':stable_id(artist_id)})

    def _dispatch_follow(self, conn, command):
        return getattr(self,'_'+command.kind)(conn,command.payload)

    def _follow_batch(self, conn, batch_id):
        lib = _lib()
        batch = self._get_batch_row(conn,batch_id)
        if batch is None:
            raise lib.NotFoundError('Unknown batch')
        if batch['phase'] != 'running':
            raise lib.InvalidStateError('Source operation needs a running batch')
        job = self._get_job_row(conn,batch['job_id'])
        request = self._request_from_row(job)
        if request.query.source != 'follow':
            raise lib.InvalidStateError('Source operation needs a follow request')
        return batch,job,request

    def _begin_follow_snapshot(self, conn, payload):
        lib = _lib()
        with self._transaction(conn):
            batch,job,request = self._follow_batch(conn,payload['batch_id'])
            prior = self._command_event(conn,payload['command_id'])
            fingerprint = canonical_json({'kind':'begin_follow_snapshot','batch_id':payload['batch_id'],'refresh':payload['refresh']})
            if prior is not None:
                if prior.get('fingerprint') != fingerprint:
                    raise lib.CommandConflict('Snapshot command changed')
                return self._get_follow_snapshot_conn(conn,prior['snapshot_id'])
            row = conn.execute('SELECT * FROM follow_snapshots WHERE account_ref=? ORDER BY rowid DESC LIMIT 1', (request.account_ref,)).fetchone()
            if row is None or row['state']=='coverage_uncertain' or (payload['refresh'] and row['state']=='complete'):
                snapshot_id,now = lib._new_id(),lib._utcnow()
                conn.execute("INSERT INTO follow_snapshots VALUES (?,?,?,'building','null',NULL,1,?,NULL)",
                    (snapshot_id,request.account_ref,job['job_id'],now))
            else:
                snapshot_id = row['snapshot_id']
            self._insert_event(conn,job_id=job['job_id'],batch_id=batch['batch_id'],kind='follow_snapshot_selected',
                payload={'fingerprint':fingerprint,'snapshot_id':snapshot_id},accepted_command_id=payload['command_id'])
            return self._get_follow_snapshot_conn(conn,snapshot_id)

    def _get_follow_snapshot_conn(self, conn, snapshot_id):
        return _snapshot(conn.execute('SELECT * FROM follow_snapshots WHERE snapshot_id=?',(snapshot_id,)).fetchone())

    def _accept_following_page(self, conn, payload):
        lib = _lib()
        with self._transaction(conn):
            batch,job,request = self._follow_batch(conn,payload['batch_id'])
            row = conn.execute('SELECT * FROM follow_snapshots WHERE snapshot_id=?',(payload['snapshot_id'],)).fetchone()
            if row is None or row['account_ref'] != request.account_ref:
                raise lib.InvalidStateError('Snapshot belongs to another account')
            receipt = conn.execute('SELECT content_digest FROM follow_snapshot_pages WHERE snapshot_id=? AND page_identity=?',
                (row['snapshot_id'],payload['page_identity'])).fetchone()
            if receipt is not None:
                if receipt['content_digest'] != payload['digest']:
                    raise lib.CursorConflict('Following page identity changed')
                return _snapshot(row)
            if row['state'] != 'building' or normalize_json(row['cursor_json']) != payload['before_json']:
                raise lib.CursorConflict('Following snapshot cursor changed')
            page = payload['page']
            now = lib._utcnow()
            ordinal = conn.execute('SELECT COALESCE(MAX(ordinal),-1)+1 FROM follow_snapshot_members WHERE snapshot_id=?',(row['snapshot_id'],)).fetchone()[0]
            global_ordinal = conn.execute('SELECT COALESCE(MAX(ordinal),-1)+1 FROM follow_artists WHERE account_ref=?',(request.account_ref,)).fetchone()[0]
            for artist in page.artists:
                exists = conn.execute('SELECT 1 FROM follow_snapshot_members WHERE snapshot_id=? AND artist_id=?',(row['snapshot_id'],artist.artist_id)).fetchone()
                if exists is None:
                    conn.execute('INSERT INTO follow_snapshot_members VALUES (?,?,?,?)',(row['snapshot_id'],artist.artist_id,artist.name,ordinal))
                    ordinal += 1
                else:
                    conn.execute('UPDATE follow_snapshot_members SET display_name=? WHERE snapshot_id=? AND artist_id=?',(artist.name,row['snapshot_id'],artist.artist_id))
                existing = conn.execute('SELECT 1 FROM follow_artists WHERE account_ref=? AND artist_id=?',(request.account_ref,artist.artist_id)).fetchone()
                if existing is None:
                    conn.execute('INSERT INTO follow_artists VALUES (?,?,?,?,1,?,?)',(request.account_ref,artist.artist_id,artist.name,global_ordinal,row['snapshot_id'],row['snapshot_id']))
                    global_ordinal += 1
                else:
                    conn.execute('UPDATE follow_artists SET display_name=?,is_following=1,last_seen_snapshot=? WHERE account_ref=? AND artist_id=?',
                        (artist.name,row['snapshot_id'],request.account_ref,artist.artist_id))
            expected = row['expected_total'] if page.expected_total is None else page.expected_total
            coverage = bool(row['coverage_complete']) and page.coverage_complete
            if row['expected_total'] is not None and page.expected_total is not None and row['expected_total'] != page.expected_total:
                coverage = False
            total = conn.execute('SELECT COUNT(*) FROM follow_snapshot_members WHERE snapshot_id=?',(row['snapshot_id'],)).fetchone()[0]
            if page.exhausted and expected is not None and total != expected:
                coverage = False
            state = ('complete' if coverage else 'coverage_uncertain') if page.exhausted else 'building'
            conn.execute('UPDATE follow_snapshots SET state=?,cursor_json=?,expected_total=?,coverage_complete=?,finished_at=? WHERE snapshot_id=?',
                (state,page.next_cursor_json,expected,int(coverage),now if page.exhausted else None,row['snapshot_id']))
            if state == 'complete':
                conn.execute('UPDATE follow_artists SET is_following=0 WHERE account_ref=? AND NOT EXISTS '
                    '(SELECT 1 FROM follow_snapshot_members m WHERE m.snapshot_id=? AND m.artist_id=follow_artists.artist_id)',
                    (request.account_ref,row['snapshot_id']))
            conn.execute('INSERT INTO follow_snapshot_pages VALUES (?,?,?)',(row['snapshot_id'],payload['page_identity'],payload['digest']))
            self._insert_event(conn,job_id=job['job_id'],batch_id=batch['batch_id'],kind='follow_snapshot_page',
                payload={'snapshot_id':row['snapshot_id'],'page_identity':payload['page_identity'],'state':state,'members':total,'coverage_complete':coverage})
            return self._get_follow_snapshot_conn(conn,row['snapshot_id'])

    def _source_budget_conn(self, conn, batch):
        budget = json.loads(batch['budget_json'])
        requests = conn.execute("SELECT COUNT(*) FROM events WHERE batch_id=? AND kind='source_request_reserved'",(batch['batch_id'],)).fetchone()[0]
        artists = conn.execute("SELECT COUNT(*) FROM events WHERE batch_id=? AND kind='source_artist_admitted'",(batch['batch_id'],)).fetchone()[0]
        return {'reserved_requests':requests,'max_requests':budget['max_requests'],'artists':artists,'max_artists':budget['max_artists']}

    def _reserve_source_request(self, conn, payload):
        lib = _lib()
        with self._transaction(conn):
            batch,job,request = self._follow_batch(conn,payload['batch_id'])
            until=self._source_cooldown_conn(conn,request.account_ref)
            if until is not None:
                raise lib.SourceCooling(until)
            counts = self._source_budget_conn(conn,batch)
            if counts['reserved_requests'] >= counts['max_requests']:
                raise lib.BudgetExceeded('source_request_budget')
            return self._insert_event(conn,job_id=job['job_id'],batch_id=batch['batch_id'],kind='source_request_reserved',payload={'kind':payload['kind']})

    def _admit_source_artist(self, conn, payload):
        lib = _lib()
        with self._transaction(conn):
            batch,job,request = self._follow_batch(conn,payload['batch_id'])
            exists = conn.execute("SELECT 1 FROM events WHERE batch_id=? AND kind='source_artist_admitted' AND json_extract(safe_payload,'$.artist_id')=?",
                (batch['batch_id'],payload['artist_id'])).fetchone()
            if exists:
                return False
            counts = self._source_budget_conn(conn,batch)
            if counts['artists'] >= counts['max_artists']:
                raise lib.BudgetExceeded('source_artist_budget')
            self._insert_event(conn,job_id=job['job_id'],batch_id=batch['batch_id'],kind='source_artist_admitted',payload={'artist_id':payload['artist_id']})
            return True

    def _candidate_requires_write(self, conn, candidate, request, health_by_artifact=None,
                                  excluded_pages=None):
        """Decide the work budget from the same frozen health evidence as reuse.

        A missing health map means no current observation exists; fail closed
        instead of trusting a database receipt.  This method never opens a file
        and is called inside the writer transaction.
        """
        if request.dedup_policy == 'redownload':
            return True
        if health_by_artifact is None:
            return True
        excluded_pages = set() if excluded_pages is None else set(excluded_pages)
        for page in candidate.pages:
            if (candidate.illust_id, page.page_index, page.output_ref.target_key) in excluded_pages:
                continue
            reusable = False
            observed_state = None
            for record in health_by_artifact.values():
                expectation = getattr(record, 'expectation', None)
                state = getattr(record, 'state', None)
                if (expectation is not None
                        and expectation.illust_id == candidate.illust_id
                        and expectation.page_index == page.page_index
                        and expectation.output_key == page.output_ref.target_key):
                    observed_state = getattr(state, 'value', state)
                    if observed_state == 'hash_match':
                        row = conn.execute(
                            'SELECT * FROM artifacts WHERE artifact_id = ?',
                            (expectation.artifact_id,),
                        ).fetchone()
                        if row is None:
                            observed_state = None
                        elif expectation.matches_row(row):
                            reusable = True
                            break
            if reusable:
                continue
            if observed_state == 'missing':
                # Only a verified missing page reserves a repair download.
                return True
            if observed_state is not None:
                # Changed/unreadable/root-unavailable/unknown pages need a
                # decision, not an automatic new-work budget slot.
                continue
            # No current observation: an existing candidate is a review item,
            # not a fresh download; otherwise this is a new page.
            candidates = self._find_reusable_artifact_candidates(
                conn,
                job_id='',
                illust_id=candidate.illust_id,
                page_index=page.page_index,
                output_key=page.output_ref.target_key,
                original=request.quality.original,
                validation_policy_version=request.quality.validation_policy_version,
                limit=1,
            )
            if not candidates:
                return True
        return False
