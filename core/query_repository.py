"""Bounded query intersection staging on the existing single library writer."""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from dataclasses import asdict

from core.task_contracts import SourcePage, canonical_json, normalize_json

QUERY_DDL = (
    """CREATE TABLE query_members (
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        logical_source_id TEXT NOT NULL,
        illust_id TEXT NOT NULL,
        candidate_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (job_id, logical_source_id, illust_id)
    )""",
    """CREATE TABLE query_source_pages (
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        batch_id TEXT NOT NULL REFERENCES batches(batch_id),
        source_id TEXT NOT NULL,
        pass_id TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        page_identity TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
        accepted_at TEXT NOT NULL,
        PRIMARY KEY (job_id, source_id, pass_id, entity_id, page_identity)
    )""",
    "CREATE INDEX idx_query_members_illust ON query_members(job_id, illust_id)",
    "CREATE INDEX idx_query_members_source ON query_members(job_id, logical_source_id, illust_id)",
    "CREATE INDEX idx_query_source_pages_batch ON query_source_pages(batch_id)",
)
QUERY_TABLES = frozenset({'query_members', 'query_source_pages'})
QUERY_COMMANDS = frozenset({
    'reserve_query_request', 'record_query_cooldown', 'admit_query_artist',
    'stage_query_page', 'carry_query_unresolved_items',
})


def _lib():
    from core import library_repository
    return library_repository


def combine_observations(values):
    """Conservative bounded merge; an adverse observation cannot disappear."""
    priority = {'filtered': 0, 'unavailable': 1, 'unsupported': 2,
                'metadata_unknown': 3, 'download': 4, 'skipped_existing': 5}
    result = dict(min(values, key=lambda item: priority[item['decision']]))
    known = [item['bookmarks'] for item in values if item.get('bookmarks') is not None]
    result['bookmarks'] = min(known) if known else None
    flags = [item.get('ai_flag') for item in values]
    result['ai_flag'] = True if True in flags else False if False in flags else None
    conflict = any(len({item[field] for item in values if item.get(field) not in (None, '')}) > 1
                   for field in ('artist_id', 'page_count'))
    if conflict and result['decision'] in {'download', 'metadata_unknown'}:
        result.update(decision='metadata_unknown', pages=[], reason_codes=['conflicting_source_metadata'])
    return result


class QueryRepositoryMixin:
    """Query-only commands. They share the repository writer/lock/schema."""

    # -- public commands ------------------------------------------------

    def reserve_query_request(self, batch_id, *, kind):
        _lib()._validate_identifier(batch_id, 'batch id')
        if kind not in {'query_page', 'detail'}:
            raise _lib().LibraryError('Invalid query request kind')
        return self._enqueue('reserve_query_request', {'batch_id': batch_id, 'kind': kind})

    def record_query_cooldown(self, batch_id, seconds, *, reason_code):
        _lib()._validate_identifier(batch_id, 'batch id')
        if type(seconds) is not int or not 1 <= seconds <= 86400:
            raise _lib().LibraryError('Invalid source cooldown duration')
        if reason_code not in {'metadata_rate_limited', 'metadata_server_unavailable'}:
            raise _lib().LibraryError('Invalid source cooldown code')
        return self._enqueue('record_query_cooldown', {
            'batch_id': batch_id, 'seconds': seconds, 'reason_code': reason_code,
        })

    def admit_query_artist(self, batch_id, artist_id):
        _lib()._validate_identifier(batch_id, 'batch id')
        from core.candidate_filter import stable_id
        return self._enqueue('admit_query_artist', {
            'batch_id': batch_id, 'artist_id': stable_id(artist_id),
        })

    def stage_query_page(self, batch_id, page):
        lib = _lib()
        lib._validate_identifier(batch_id, 'batch id')
        if not isinstance(page, SourcePage):
            raise lib.LibraryError('SourcePage is required')
        page_json = canonical_json(asdict(page))
        digest = hashlib.sha256(page_json.encode('utf-8')).hexdigest()
        payload = {
            'batch_id': batch_id, 'page': page, 'page_digest': digest,
            'page_json': page_json,
        }
        return self._enqueue('stage_query_page', payload, size_payload={
            'batch_id': batch_id, 'page': asdict(page), 'page_digest': digest,
            'page_json': page_json,
        })

    def carry_query_unresolved_items(self, batch_id, *, after='', limit=100):
        lib = _lib()
        lib._validate_identifier(batch_id, 'batch id')
        if not isinstance(after, str) or (after and not after.isdecimal()) or type(limit) is not int or not 1 <= limit <= 200:
            raise lib.LibraryError('Invalid unresolved item page')
        return self._enqueue('carry_query_unresolved_items', {
            'batch_id': batch_id, 'after': after, 'limit': limit,
        })

    def query_member_count(self, batch_id, logical_source_id):
        lib = _lib()
        lib._validate_identifier(batch_id, 'batch id')
        lib._validate_identifier(logical_source_id, 'logical source id')
        self._ensure_open()
        with closing(self._readonly_connection()) as conn:
            batch = self._get_batch_row(conn, batch_id)
            if batch is None:
                raise lib.NotFoundError('Unknown query batch')
            return int(conn.execute(
                'SELECT COUNT(*) FROM query_members WHERE job_id=? AND logical_source_id=?',
                (str(batch['job_id']), logical_source_id),
            ).fetchone()[0])

    def query_member_ids(self, batch_id, logical_source_id, illust_ids):
        """One indexed lookup for at most one remote page, never the full corpus."""
        lib = _lib()
        lib._validate_identifier(batch_id, 'batch id')
        lib._validate_identifier(logical_source_id, 'logical source id')
        if not isinstance(illust_ids, (list, tuple)) or len(illust_ids) > 2000:
            raise lib.LibraryError('Invalid query member page')
        from core.candidate_filter import stable_id
        ids = tuple(stable_id(value) for value in illust_ids)
        if not ids:
            return set()
        self._ensure_open()
        with closing(self._readonly_connection()) as conn:
            batch = self._get_batch_row(conn, batch_id)
            if batch is None:
                raise lib.NotFoundError('Unknown query batch')
            marks = ','.join('?' for _ in ids)
            return {row[0] for row in conn.execute(
                f'SELECT illust_id FROM query_members WHERE job_id=? AND logical_source_id=? AND illust_id IN ({marks})',
                (batch['job_id'], logical_source_id, *ids))}

    def query_intersection_candidates(self, batch_id, logical_source_ids, *, after='', limit=100):
        lib = _lib()
        lib._validate_identifier(batch_id, 'batch id')
        if (
            not isinstance(logical_source_ids, (list, tuple))
            or not 2 <= len(logical_source_ids) <= 4
            or any(not isinstance(value, str) for value in logical_source_ids)
            or len(set(logical_source_ids)) != len(logical_source_ids)
        ):
            raise lib.LibraryError('Invalid intersection source list')
        if not isinstance(after, str) or (after and not after.isdecimal()):
            raise lib.LibraryError('Invalid intersection cursor')
        if type(limit) is not int or not 1 <= limit <= 200:
            raise lib.LibraryError('Invalid intersection page size')
        self._ensure_open()
        placeholders = ','.join('?' for _ in logical_source_ids)
        with closing(self._readonly_connection()) as conn:
            batch = self._get_batch_row(conn, batch_id)
            if batch is None:
                raise lib.NotFoundError('Unknown query batch')
            rows = conn.execute(
                'SELECT qm.illust_id, '
                'COUNT(DISTINCT qm.logical_source_id) AS source_count '
                'FROM query_members qm WHERE qm.job_id=? '
                f'AND qm.logical_source_id IN ({placeholders}) AND qm.illust_id>? '
                'GROUP BY qm.illust_id HAVING COUNT(DISTINCT qm.logical_source_id)=? '
                'ORDER BY qm.illust_id LIMIT ?',
                (str(batch['job_id']), *logical_source_ids, after, len(logical_source_ids), limit),
            ).fetchall()
            result = []
            for row in rows:
                observations = conn.execute(
                    f'SELECT candidate_json FROM query_members WHERE job_id=? AND illust_id=? AND logical_source_id IN ({placeholders}) ORDER BY logical_source_id',
                    (batch['job_id'], row['illust_id'], *logical_source_ids)).fetchall()
                try:
                    values = [json.loads(normalize_json(item[0], object_only=True)) for item in observations]
                    candidate = combine_observations(values)
                    candidate['provenance_json'] = canonical_json({
                        'intersection_sources': list(logical_source_ids),
                        'observations': [json.loads(item['provenance_json']) for item in values],
                    })
                except (KeyError, TypeError, ValueError):
                    raise lib.CorruptDataError('Corrupt query member') from None
                result.append({
                    'illust_id': str(row['illust_id']),
                    'candidate_json': canonical_json(candidate),
                    'source_count': int(row['source_count']),
                    'candidate': candidate,
                })
        return tuple(result)

    # -- writer commands ------------------------------------------------

    def _dispatch_query(self, conn, command):
        return getattr(self, '_' + command.kind)(conn, command.payload)

    def _query_batch(self, conn, batch_id):
        lib = _lib()
        batch = self._get_batch_row(conn, batch_id)
        if batch is None:
            raise lib.NotFoundError('Unknown query batch')
        if batch['phase'] != 'running':
            raise lib.InvalidStateError('Query operation needs a running batch')
        job = self._get_job_row(conn, batch['job_id'])
        if job is None:
            raise lib.CorruptDataError('Query batch has no job')
        request = self._request_from_row(job)
        if request.query.source == 'follow':
            raise lib.InvalidStateError('Query operation cannot use a follow request')
        return batch, job, request

    def _reserve_query_request(self, conn, payload):
        lib = _lib()
        with self._transaction(conn):
            batch, job, request = self._query_batch(conn, payload['batch_id'])
            until = self._source_cooldown_conn(conn, request.account_ref)
            if until is not None:
                raise lib.SourceCooling(until)
            counts = self._source_budget_conn(conn, batch)
            if counts['reserved_requests'] >= counts['max_requests']:
                raise lib.BudgetExceeded('source_request_budget')
            return self._insert_event(
                conn, job_id=job['job_id'], batch_id=batch['batch_id'],
                kind='source_request_reserved',
                payload={'kind': payload['kind'], 'query': True},
            )

    def _record_query_cooldown(self, conn, payload):
        lib = _lib()
        with self._transaction(conn):
            batch, job, request = self._query_batch(conn, payload['batch_id'])
            from datetime import datetime, timedelta, timezone
            until = (
                datetime.fromisoformat(lib._utcnow()) + timedelta(seconds=payload['seconds'])
            ).astimezone(timezone.utc).isoformat(timespec='microseconds')
            self._insert_event(
                conn, job_id=job['job_id'], batch_id=batch['batch_id'],
                kind='source_cooldown',
                payload={'retry_at': until, 'reason_code': payload['reason_code']},
            )
            return until

    def _admit_query_artist(self, conn, payload):
        lib = _lib()
        with self._transaction(conn):
            batch, job, request = self._query_batch(conn, payload['batch_id'])
            exists = conn.execute(
                "SELECT 1 FROM events WHERE batch_id=? AND kind='source_artist_admitted' "
                "AND json_extract(safe_payload,'$.artist_id')=?",
                (batch['batch_id'], payload['artist_id']),
            ).fetchone()
            if exists:
                return False
            counts = self._source_budget_conn(conn, batch)
            if counts['artists'] >= counts['max_artists']:
                raise lib.BudgetExceeded('source_artist_budget')
            self._insert_event(
                conn, job_id=job['job_id'], batch_id=batch['batch_id'],
                kind='source_artist_admitted', payload={'artist_id': payload['artist_id']},
            )
            return True

    def _stage_query_page(self, conn, payload):
        lib = _lib()
        page = payload['page']
        with self._transaction(conn):
            batch, job, request = self._query_batch(conn, payload['batch_id'])
            if request.query.source != 'intersection':
                raise lib.InvalidStateError('Only intersection queries stage members')
            row = conn.execute(
                'SELECT content_digest FROM query_source_pages '
                'WHERE job_id=? AND source_id=? AND pass_id=? AND entity_id=? '
                'AND page_identity=?',
                (job['job_id'], page.source_id, page.pass_id, page.entity_id,
                 page.page_identity),
            ).fetchone()
            if row is not None:
                if row['content_digest'] != payload['page_digest']:
                    raise lib.CursorConflict('Staged query page changed')
                return 0
            checkpoint = conn.execute(
                'SELECT * FROM checkpoints WHERE job_id=? AND source_id=? '
                'AND pass_id=? AND entity_id=?',
                (job['job_id'], page.source_id, page.pass_id, page.entity_id),
            ).fetchone()
            current = checkpoint['cursor_json'] if checkpoint is not None else 'null'
            if normalize_json(current) != page.cursor_before_json:
                raise lib.CursorConflict('Staged query cursor changed')
            if checkpoint is not None and checkpoint['exhausted']:
                raise lib.InvalidStateError('Staged query unit is already exhausted')
            now = lib._utcnow()
            inserted = 0
            from core.query_parameters import validate_query_parameters
            parameters = validate_query_parameters('intersection', request.query.parameters_json)
            if page.source_id not in {f'query.intersection.{i}' for i in range(len(parameters['sources']))}:
                raise lib.LibraryError('Unknown intersection source')
            count = conn.execute('SELECT COUNT(*) FROM query_members WHERE job_id=? AND logical_source_id=?',
                                 (job['job_id'], page.source_id)).fetchone()[0]
            for candidate in page.candidates:
                candidate_json = canonical_json(asdict(candidate))
                existing = conn.execute(
                    'SELECT candidate_json FROM query_members '
                    'WHERE job_id=? AND logical_source_id=? AND illust_id=?',
                    (job['job_id'], page.source_id, candidate.illust_id),
                ).fetchone()
                if existing is None:
                    if count + inserted >= parameters['per_source_limit']:
                        raise lib.LibraryError('Intersection member limit exceeded')
                    conn.execute(
                        'INSERT INTO query_members(job_id,logical_source_id,illust_id,'
                        'candidate_json,created_at) VALUES (?,?,?,?,?)',
                        (job['job_id'], page.source_id, candidate.illust_id,
                         candidate_json, now),
                    )
                    inserted += 1
                elif existing['candidate_json'] != candidate_json:
                    merged = combine_observations([json.loads(existing['candidate_json']), asdict(candidate)])
                    conn.execute('UPDATE query_members SET candidate_json=? WHERE job_id=? AND logical_source_id=? AND illust_id=?',
                                 (canonical_json(merged), job['job_id'], page.source_id, candidate.illust_id))
                occurrence_key = canonical_json([page.entity_id, page.page_identity, candidate.illust_id])
                conn.execute(
                    'INSERT INTO source_items(job_id,source_id,pass_id,occurrence_key,'
                    'entity_type,entity_id,payload,created_at) '
                    "VALUES (?,?,?,?,'illust',?,?,?)",
                    (job['job_id'], page.source_id, page.pass_id, occurrence_key,
                     candidate.illust_id, candidate_json, now))
            if checkpoint is None:
                conn.execute(
                    'INSERT INTO checkpoints(job_id,source_id,pass_id,entity_id,artist_id,ordinal,'
                    'cursor_json,exhausted,coverage_json,actual_sort,updated_at) '
                    'VALUES (?,?,?,?,NULL,NULL,?,?,?,?,?)',
                    (job['job_id'], page.source_id, page.pass_id, page.entity_id,
                     page.cursor_after_json, int(page.exhausted),
                     page.coverage_json, page.actual_sort, now),
                )
            else:
                conn.execute(
                    'UPDATE checkpoints SET cursor_json=?,exhausted=?,coverage_json=?,'
                    'actual_sort=?,updated_at=? WHERE job_id=? AND source_id=? '
                    'AND pass_id=? AND entity_id=?',
                    (page.cursor_after_json, int(page.exhausted), page.coverage_json,
                     page.actual_sort, now, job['job_id'], page.source_id,
                     page.pass_id, page.entity_id),
                )
            conn.execute(
                'INSERT INTO query_source_pages(job_id,batch_id,source_id,pass_id,entity_id,'
                'page_identity,content_digest,candidate_count,accepted_at) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (job['job_id'], batch['batch_id'], page.source_id, page.pass_id,
                 page.entity_id, page.page_identity, payload['page_digest'],
                 len(page.candidates), now),
            )
            self._insert_event(
                conn, job_id=job['job_id'], batch_id=batch['batch_id'],
                kind='query_page_staged',
                payload={'source_id': page.source_id, 'candidates': len(page.candidates),
                         'inserted': inserted},
            )
            return inserted

    def _carry_query_unresolved_items(self, conn, payload):
        lib = _lib()
        with self._transaction(conn):
            batch, job, request = self._query_batch(conn, payload['batch_id'])
            rows = conn.execute(
                'SELECT bi.* FROM batch_items bi JOIN batches b ON b.batch_id=bi.batch_id '
                'WHERE b.job_id=? AND bi.batch_id!=? AND bi.illust_id>? '
                'AND NOT EXISTS (SELECT 1 FROM batch_items newer JOIN batches nb ON nb.batch_id=newer.batch_id '
                'WHERE nb.job_id=b.job_id AND newer.illust_id=bi.illust_id AND nb.rowid>b.rowid) '
                "AND (bi.decision IN ('metadata_unknown','unavailable','unsupported') OR (bi.decision='download' "
                "AND bi.result_state IN ('failed','needs_verification','partial') AND NOT EXISTS ("
                'SELECT 1 FROM batch_item_pages bip JOIN deliveries d ON d.delivery_id=bip.delivery_id '
                "WHERE bip.batch_id=bi.batch_id AND bip.illust_id=bi.illust_id AND d.state IN "
                "('pending','transferring','validating','suspended','retry_wait')))) "
                'ORDER BY bi.illust_id LIMIT ?',
                (job['job_id'], batch['batch_id'], payload['after'], payload['limit']),
            ).fetchall()
            now = lib._utcnow()
            for item in rows:
                reasons = json.loads(item['reason_codes_json'])
                if 'carried_unresolved' not in reasons:
                    reasons.append('carried_unresolved')
                provenance = json.loads(item['provenance_json'])
                provenance['local_carry_from_batch'] = item['batch_id']
                conn.execute(
                    'INSERT INTO batch_items(batch_id,illust_id,artist_id,artist_name,title,'
                    'page_count,decision,reason_codes_json,required_pages_json,result_state,'
                    'bookmarks,ai_flag,provenance_json,created_at,updated_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (batch['batch_id'], item['illust_id'], item['artist_id'],
                     item['artist_name'], item['title'], item['page_count'],
                     item['decision'], canonical_json(reasons),
                     item['required_pages_json'], item['result_state'],
                     item['bookmarks'], item['ai_flag'], canonical_json(provenance),
                     now, now),
                )
                self._copy_batch_item_page_links(
                    conn, item['batch_id'], batch['batch_id'], item['illust_id']
                )
                self._refresh_batch_item_result(
                    conn, batch['batch_id'], item['illust_id'], now
                )
            if rows:
                self._insert_event(
                    conn, job_id=job['job_id'], batch_id=batch['batch_id'],
                    kind='unresolved_items_carried',
                    payload={'count': len(rows),
                             'illust_ids': [item['illust_id'] for item in rows]},
                )
            return {
                'count': len(rows),
                'after': rows[-1]['illust_id'] if rows else None,
            }
