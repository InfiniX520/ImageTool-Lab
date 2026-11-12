"""Bounded query source pump and finite multi-source intersection."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, asdict
from datetime import date, timedelta

from core.candidate_filter import (
    ArtworkMetadata, SourceResponseError, make_candidate, merge_details, needs_details, stable_id,
)
from core.follow_collection import FollowCollection, SourcePolicy, SourceRunReport, _Budget, _Stopped
from core.follow_contracts import ArtworkPage
from core.library_repository import BudgetExceeded, InvalidStateError, LibraryError, SourceCooling
from core.query_parameters import validate_query_parameters
from core.task_contracts import (
    CandidateDecision, ContractError, PageSpec, PathRef, SourcePage, canonical_json,
)


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()[:16]


def _sort_for_search(value):
    return 'date_desc' if value == 'source_default' else value


@dataclass(frozen=True, slots=True)
class _Unit:
    kind: str
    source_id: str
    pass_id: str
    entity_id: str
    parameters: dict
    requested_sort: str
    actual_sort: str
    logical_source: str
    artist_id: str = ''
    partition: str = ''
    date_value: str | None = None


@dataclass(frozen=True, slots=True)
class _LogicalSource:
    source_id: str
    source: str
    parameters: dict
    requested_sort: str
    units: tuple[_Unit, ...]
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class _LoadedPage:
    page: ArtworkPage
    actual_source: str
    actual_sort: str
    actual_date: str | None
    provenance: dict


class QueryCollection(FollowCollection):
    """One owner-thread query run; creates no threads, sessions or retries."""

    def run(self, batch_id, *, stop_event=None, refresh_following=False,
            from_head=False, retry_failed_only=False):
        if self._running:
            raise RuntimeError('A source pump can run only one batch at a time')
        for value in (refresh_following, from_head, retry_failed_only):
            if type(value) is not bool:
                raise TypeError('Source options must be boolean')
        self._running = True
        self.batch_id, self.stop, self.calls = batch_id, stop_event, 0
        try:
            batch = self.repository.get_batch(batch_id)
            self.job_id = batch['job_id']
            self.request = self.repository.get_request(self.job_id)
            if self.request.query.source == 'follow':
                raise ValueError('QueryCollection cannot run a follow request')
            if self.source.account_ref != self.request.account_ref:
                raise ValueError('Source account or request does not match')
            if self.coordinator.active_batch_id != batch_id:
                raise ValueError('The caller must explicitly activate this batch first')
            seconds = self.policy.max_seconds
            if self.request.budget.max_seconds is not None:
                seconds = min(seconds, self.request.budget.max_seconds)
            self.deadline = self.clock() + seconds
            self._check()
            if retry_failed_only:
                complete = self._prior_query_complete()
                self._carry_unresolved()
                self._finish(complete, 'failed_pages_only')
                return self._report('failed_pages_only', complete)
            plan = self._build_plan()
            if from_head:
                for logical in plan:
                    for unit in logical.units:
                        if self.repository.get_checkpoint(
                            self.job_id, unit.source_id, unit.pass_id, unit.entity_id
                        ) is not None:
                            raise ValueError('From-head checks require a fresh collection job')
            if self.request.query.source == 'intersection':
                coverage, reason = self._run_intersection(plan)
            else:
                coverage = self._run_single(plan)
                reason = 'source_exhausted' if coverage else 'coverage_unconfirmed'
            self._mark_scope(coverage, reason)
            self._carry_unresolved()
            self._finish(coverage, reason)
            return self._report(reason, coverage)
        except _Budget as exc:
            self._finish(False, exc.code)
            return self._report(exc.code, False)
        except _Stopped:
            if self.repository.get_batch(batch_id)['phase'] == 'running':
                self.coordinator.pause(self.job_id, 'query-stop-' + batch_id).result(20)
            return self._report('stopped', False)
        except InvalidStateError:
            if self.repository.get_batch(batch_id)['phase'] != 'running':
                return self._report('stopped', False)
            raise
        except (SourceResponseError, ContractError) as exc:
            code = exc.code if isinstance(exc, SourceResponseError) else 'invalid_source_metadata'
            if code == 'metadata_stopped':
                if self.repository.get_batch(batch_id)['phase'] == 'running':
                    self.coordinator.pause(self.job_id, 'query-stop-' + batch_id).result(20)
                return self._report('stopped', False)
            if isinstance(exc, SourceResponseError) and exc.retry_after_seconds is not None:
                try:
                    self.repository.record_query_cooldown(
                        batch_id, exc.retry_after_seconds, reason_code=code
                    )
                except (ValueError, LibraryError):
                    pass
            self.repository.request_block(batch_id, code)
            return self._report(code, False)
        except LibraryError:
            self.repository.request_block(batch_id, 'source_record_error')
            raise
        finally:
            self._running = False

    # -- planning -------------------------------------------------------

    def _build_plan(self):
        query = self.request.query
        normalized = validate_query_parameters(
            query.source, query.parameters_json, query.requested_sort
        )
        if query.source == 'intersection':
            result = []
            for index, item in enumerate(normalized['sources']):
                source_id = f'query.intersection.{index}'
                units = self._units_for(
                    item['source'], item['parameters'], item['requested_sort'],
                    source_id=source_id, logical_source=source_id,
                )
                result.append(_LogicalSource(
                    source_id, item['source'], item['parameters'],
                    item['requested_sort'], tuple(units), normalized['per_source_limit'],
                ))
            return result
        source_id = f'query.{query.source}'
        return [_LogicalSource(
            source_id, query.source, normalized, query.requested_sort,
            tuple(self._units_for(
                query.source, normalized, query.requested_sort,
                source_id=source_id, logical_source=source_id,
            )),
        )]

    def _units_for(self, source, parameters, requested_sort, *, source_id, logical_source):
        if source == 'ranking':
            dates = self._ranking_dates(parameters)
            units = []
            for value in dates:
                unit_params = {'mode': parameters['mode']}
                if value is not None:
                    unit_params['date'] = value
                units.append(_Unit(
                    'ranking', source_id, 'ranking', value or 'latest', unit_params,
                    'source_default', 'ranking', logical_source,
                ))
            return units
        if source == 'search':
            actual_sort = _sort_for_search(requested_sort)
            return [_Unit(
                'search', source_id, 'search',
                _digest({'search': parameters, 'sort': requested_sort}),
                dict(parameters), requested_sort, actual_sort, logical_source,
            )]
        if source == 'artist':
            units = []
            for artist_id in parameters['artist_ids']:
                for partition in ('illust', 'manga'):
                    units.append(_Unit(
                        'artist', source_id, partition, artist_id,
                        {'artist_id': artist_id, 'partition': partition},
                        'source_default', 'source_default', logical_source,
                        artist_id=artist_id, partition=partition,
                    ))
            return units
        if source == 'recommendations':
            kind = parameters['kind']
            entity = parameters.get('illust_id', 'personalized')
            return [_Unit(
                'recommendations', source_id, kind, entity, dict(parameters),
                'source_default', 'source_default', logical_source,
            )]
        if source == 'follow':
            return [
                _Unit('follow_new', source_id, partition, 'new', {},
                      'source_default', 'source_default', logical_source,
                      partition=partition)
                for partition in ('public', 'private')
            ]
        raise SourceResponseError('unsupported_query_source')

    @staticmethod
    def _ranking_dates(parameters):
        if 'date' in parameters:
            return [parameters['date']]
        if 'start_date' not in parameters:
            return [None]
        end = date.fromisoformat(parameters['end_date'])
        current = date.fromisoformat(parameters['start_date'])
        values = []
        while current <= end:
            values.append(current.isoformat())
            current += timedelta(days=1)
        return list(reversed(values))

    # -- single source --------------------------------------------------

    def _run_single(self, plan):
        complete = True
        for logical in plan:
            for unit in logical.units:
                complete = self._run_unit(unit) and complete
        return complete

    def _run_unit(self, unit):
        checkpoint = self.repository.get_checkpoint(
            self.job_id, unit.source_id, unit.pass_id, unit.entity_id
        )
        if checkpoint is not None and checkpoint['exhausted']:
            return bool(json.loads(checkpoint['coverage_json']).get('coverage_complete'))
        api_cursor, offset, saved_digest, before = self._cursor_state(checkpoint)
        while True:
            self._check()
            loaded = self._load_page(unit, api_cursor)
            page = loaded.page
            page_digest = self._page_digest(page)
            if offset and saved_digest != page_digest:
                offset = 0
            if offset > len(page.artworks):
                offset = 0
            if offset == len(page.artworks):
                after, exhausted = self._after_page(page, api_cursor, page_digest, offset)
                coverage = self._coverage(unit, loaded, page, offset, page_digest,
                                          exhausted and page.coverage_complete)
                self._accept(SourcePage(
                    unit.source_id, unit.pass_id, unit.entity_id,
                    self._page_identity(unit, api_cursor, page_digest, offset, None),
                    (), before, after, exhausted, loaded.actual_sort,
                    canonical_json(coverage),
                ))
                if exhausted:
                    return bool(page.coverage_complete)
                api_cursor, offset, saved_digest = page.next_cursor_json, 0, None
                before = after
                continue
            for index in range(offset, len(page.artworks)):
                metadata = page.artworks[index]
                candidate = self._candidate(unit, loaded, metadata)
                last = index == len(page.artworks) - 1
                if last and page.exhausted:
                    after, exhausted = self._terminal_cursor(), True
                elif last:
                    after, exhausted = self._next_cursor(page.next_cursor_json), False
                else:
                    after, exhausted = canonical_json({
                        'api_cursor': api_cursor, 'offset': index + 1,
                        'page_digest': page_digest,
                    }), False
                coverage = self._coverage(unit, loaded, page, index + 1, page_digest,
                                          exhausted and page.coverage_complete)
                self._accept(SourcePage(
                    unit.source_id, unit.pass_id, unit.entity_id,
                    self._page_identity(unit, api_cursor, page_digest, index, candidate.illust_id),
                    (candidate,), before, after, exhausted, loaded.actual_sort,
                    canonical_json(coverage),
                ))
                before = after
                if last:
                    if exhausted:
                        return bool(page.coverage_complete)
                    api_cursor, offset, saved_digest = page.next_cursor_json, 0, None
                    break
                offset = index + 1
            else:
                continue

    # -- intersection staging ------------------------------------------

    def _run_intersection(self, plan):
        limited_scope = False
        for logical in plan:
            count = self.repository.query_member_count(self.batch_id, logical.source_id)
            if count >= (logical.limit or 0):
                complete_units = all(
                    (value := self.repository.get_checkpoint(
                        self.job_id, unit.source_id, unit.pass_id, unit.entity_id
                    )) is not None
                    and value['exhausted']
                    and bool(json.loads(value['coverage_json']).get('coverage_complete'))
                    for unit in logical.units
                )
                if not complete_units:
                    limited_scope = True
                continue
            for unit in logical.units:
                ok, limited = self._stage_unit(unit, logical.limit or 1)
                if limited:
                    limited_scope = True
                    break
                if not ok:
                    return False, 'coverage_unconfirmed'
        matched = self._admit_intersection(plan)
        if limited_scope:
            return False, 'source_limit_reached'
        if matched == 0:
            return True, 'no_intersection_matches'
        return True, 'source_exhausted'

    def _stage_unit(self, unit, limit):
        checkpoint = self.repository.get_checkpoint(
            self.job_id, unit.source_id, unit.pass_id, unit.entity_id
        )
        if checkpoint is not None and checkpoint['exhausted']:
            payload = json.loads(checkpoint['coverage_json'])
            return bool(payload.get('coverage_complete')), bool(payload.get('limit_reached'))
        api_cursor, offset, saved_digest, before = self._cursor_state(checkpoint)
        count = self.repository.query_member_count(self.batch_id, unit.source_id)
        while True:
            self._check()
            remaining = limit - count
            if remaining <= 0:
                self._stage_empty(unit, before, self._terminal_cursor(), True,
                                  {'coverage_complete': False, 'limit_reached': True},
                                  unit.actual_sort)
                return False, True
            loaded = self._load_page(unit, api_cursor)
            page = loaded.page
            page_digest = self._page_digest(page)
            if offset and saved_digest != page_digest:
                offset = 0
            if offset > len(page.artworks):
                offset = 0
            if offset == len(page.artworks):
                after, exhausted = self._after_page(page, api_cursor, page_digest, offset)
                info = {'coverage_complete': bool(exhausted and page.coverage_complete),
                        'limit_reached': False, 'actual_date': loaded.actual_date,
                        'actual_source': loaded.actual_source, 'evidence': loaded.provenance}
                self._stage_empty(unit, before, after, exhausted, info, unit.actual_sort)
                if exhausted:
                    return bool(page.coverage_complete), False
                api_cursor, offset, saved_digest, before = (
                    page.next_cursor_json, 0, None, after
                )
                continue
            candidates = {}
            existing = self.repository.query_member_ids(
                self.batch_id, unit.source_id, [item.illust_id for item in page.artworks])
            new_ids = set()
            limit_hit = False
            index = offset
            while index < len(page.artworks):
                metadata = page.artworks[index]
                if metadata.illust_id not in existing and metadata.illust_id not in new_ids:
                    if len(new_ids) >= remaining:
                        limit_hit = True
                        break
                    new_ids.add(metadata.illust_id)
                candidate = self._candidate(unit, loaded, metadata, resolve_details=False)
                previous = candidates.get(candidate.illust_id)
                if previous is not None:
                    from core.query_repository import combine_observations
                    candidate = self._candidate_from_json(canonical_json(
                        combine_observations([asdict(previous), asdict(candidate)])))
                candidates[candidate.illust_id] = candidate
                index += 1
            consumed = index >= len(page.artworks)
            if limit_hit:
                after, exhausted = self._terminal_cursor(), True
                info = {'coverage_complete': False, 'limit_reached': True,
                        'logical_source': unit.logical_source}
            elif consumed and page.exhausted:
                after, exhausted = self._terminal_cursor(), True
                info = {'coverage_complete': bool(page.coverage_complete),
                        'limit_reached': False,
                        'logical_source': unit.logical_source}
            elif consumed:
                after, exhausted = self._next_cursor(page.next_cursor_json), False
                info = {'coverage_complete': False, 'limit_reached': False,
                        'logical_source': unit.logical_source}
            else:
                after, exhausted = canonical_json({
                    'api_cursor': api_cursor, 'offset': index,
                    'page_digest': page_digest,
                }), False
                info = {'coverage_complete': False, 'limit_reached': False,
                        'logical_source': unit.logical_source}
            info.update(actual_source=loaded.actual_source, actual_date=loaded.actual_date,
                        actual_sort=loaded.actual_sort, evidence=loaded.provenance)
            self._stage_page(
                unit, list(candidates.values()), before, after, exhausted, info,
                page_digest, api_cursor, index, loaded.actual_sort,
            )
            before = after
            if limit_hit:
                return False, True
            if exhausted:
                return bool(page.coverage_complete), False
            if consumed:
                api_cursor, offset, saved_digest = page.next_cursor_json, 0, None
            else:
                offset, saved_digest = index, page_digest
            count = self.repository.query_member_count(self.batch_id, unit.source_id)
            if count >= limit and not exhausted:
                self._stage_empty(
                    unit, before, self._terminal_cursor(), True,
                    {'coverage_complete': False, 'limit_reached': True},
                    unit.actual_sort,
                )
                return False, True

    def _stage_page(self, unit, candidates, before, after, exhausted, info,
                    page_digest, api_cursor, offset, actual_sort):
        identity = _digest({
            'unit': [unit.source_id, unit.pass_id, unit.entity_id],
            'page': page_digest,
            'cursor': api_cursor,
            'offset': offset,
            'ids': [candidate.illust_id for candidate in candidates],
            'exhausted': exhausted,
        })
        self.repository.stage_query_page(self.batch_id, SourcePage(
            unit.source_id, unit.pass_id, unit.entity_id, identity, tuple(candidates),
            before, after, exhausted, actual_sort, canonical_json(info),
        ))

    def _stage_empty(self, unit, before, after, exhausted, info, actual_sort):
        identity = _digest({
            'unit': [unit.source_id, unit.pass_id, unit.entity_id],
            'empty': True, 'before': before, 'after': after,
        })
        self.repository.stage_query_page(self.batch_id, SourcePage(
            unit.source_id, unit.pass_id, unit.entity_id, identity, (),
            before, after, exhausted, actual_sort, canonical_json(info),
        ))

    def _admit_intersection(self, plan):
        source_ids = [logical.source_id for logical in plan]
        checkpoint = self.repository.get_checkpoint(
            self.job_id, 'query.intersection.result', 'result', 'intersection'
        )
        if checkpoint is not None and checkpoint['exhausted']:
            return int(json.loads(checkpoint['coverage_json']).get('matches', 0))
        before = checkpoint['cursor_json'] if checkpoint is not None else 'null'
        try:
            state = json.loads(before)
        except (TypeError, ValueError):
            raise SourceResponseError('invalid_query_checkpoint') from None
        last = '' if not isinstance(state, dict) else str(state.get('last_illust_id', ''))
        matched = int(state.get('matches', 0)) if isinstance(state, dict) else 0
        while True:
            self._check()
            rows = self.repository.query_intersection_candidates(
                self.batch_id, source_ids, after=last, limit=100
            )
            if not rows:
                self._accept(SourcePage(
                    'query.intersection.result', 'result', 'intersection',
                    _digest({'final': before, 'last': last}), (),
                    before, canonical_json({'last_illust_id': last, 'matches': matched}),
                    True, 'source_default',
                    canonical_json({'coverage_complete': True, 'matches': matched}),
                ))
                return matched
            for row in rows:
                candidate = self._intersection_candidate(row['candidate_json'])
                next_id = row['illust_id']
                after = canonical_json({'last_illust_id': next_id, 'matches': matched + 1})
                self._accept(SourcePage(
                    'query.intersection.result', 'result', 'intersection',
                    _digest({'before': before, 'illust': next_id}), (candidate,),
                    before, after, False, 'source_default',
                    canonical_json({'coverage_complete': False, 'matches': matched + 1}),
                ))
                before, last, matched = after, next_id, matched + 1

    def _intersection_candidate(self, text):
        candidate = self._candidate_from_json(text)
        if candidate.decision != 'metadata_unknown' or 'conflicting_source_metadata' in candidate.reason_codes:
            return candidate
        # Only matched, unresolved candidates need another API request.
        detail = self._request('detail', lambda: self.source.details(candidate.illust_id))
        metadata = ArtworkMetadata(
            candidate.illust_id, candidate.artist_id, candidate.artist_name, candidate.title,
            kind='unknown', page_count=candidate.page_count,
            bookmarks=candidate.bookmarks, ai_flag=candidate.ai_flag)
        metadata = merge_details(metadata, detail)
        return make_candidate(metadata, self.request, provenance=json.loads(candidate.provenance_json))

    # -- source page loading/candidates --------------------------------

    def _load_page(self, unit, cursor):
        if unit.kind == 'ranking':
            page = self._request('query_page', lambda: self.source.query_page(
                'ranking', canonical_json(unit.parameters), cursor,
                requested_sort='source_default',
            ))
        elif unit.kind == 'search':
            page = self._request('query_page', lambda: self.source.query_page(
                'search', canonical_json(unit.parameters), cursor,
                requested_sort=unit.requested_sort,
            ))
        elif unit.kind == 'recommendations':
            page = self._request('query_page', lambda: self.source.query_page(
                'recommendations', canonical_json(unit.parameters), cursor,
                requested_sort='source_default',
            ))
        elif unit.kind == 'artist':
            try:
                self.repository.admit_query_artist(self.batch_id, unit.artist_id)
            except BudgetExceeded:
                raise _Budget('source_artist_budget') from None
            raw_page = self._request('query_page', lambda: self.source.artist_works(
                unit.artist_id, cursor, partition=unit.partition
            ))
            if not isinstance(raw_page, ArtworkPage):
                raise SourceResponseError('invalid_artworks_page')
            return _LoadedPage(raw_page, 'artist', 'source_default', None, {
                'source': 'artist', 'artist_id': unit.artist_id,
                'partition': unit.partition,
            })
        elif unit.kind == 'follow_new':
            raw_page = self._request('query_page', lambda: self.source.new_works(
                cursor, partition=unit.partition
            ))
            if not isinstance(raw_page, ArtworkPage):
                raise SourceResponseError('invalid_artworks_page')
            return _LoadedPage(raw_page, 'follow_new', 'source_default', None, {
                'source': 'follow_new', 'partition': unit.partition,
            })
        else:
            raise SourceResponseError('unsupported_query_source')
        if not hasattr(page, 'page') or not isinstance(page.page, ArtworkPage):
            raise SourceResponseError('invalid_query_page')
        provenance = json.loads(page.provenance_json)
        return _LoadedPage(
            page.page, page.actual_source, page.actual_sort,
            page.actual_date, provenance,
        )

    def _candidate(self, unit, loaded, metadata, *, resolve_details=True):
        if resolve_details and needs_details(metadata, self.request):
            detail = self._request(
                'detail', lambda: self.source.details(metadata.illust_id)
            )
            metadata = merge_details(metadata, detail)
        provenance = {
            'source_id': unit.source_id,
            'logical_source': unit.logical_source,
            'actual_source': loaded.actual_source,
            'actual_sort': loaded.actual_sort,
            'actual_date': loaded.actual_date,
            'parameters': unit.parameters,
            'evidence': loaded.provenance,
        }
        return make_candidate(metadata, self.request, provenance=provenance)

    @staticmethod
    def _candidate_from_json(text):
        try:
            payload = json.loads(text)
            pages = []
            for page in payload['pages']:
                page = dict(page)
                page['output_ref'] = PathRef(**page['output_ref'])
                pages.append(PageSpec(**page))
            return CandidateDecision(
                illust_id=payload['illust_id'],
                decision=payload['decision'],
                artist_id=payload.get('artist_id', ''),
                artist_name=payload.get('artist_name', ''),
                title=payload.get('title', ''),
                page_count=payload.get('page_count'),
                pages=tuple(pages),
                bookmarks=payload.get('bookmarks'),
                ai_flag=payload.get('ai_flag'),
                reason_codes=tuple(payload.get('reason_codes', ())),
                provenance_json=payload.get('provenance_json', '{}'),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceResponseError('invalid_query_member') from None

    # -- cursor/page helpers -------------------------------------------

    @staticmethod
    def _cursor_state(checkpoint):
        if checkpoint is None:
            return 'null', 0, None, 'null'
        before = checkpoint['cursor_json']
        try:
            state = json.loads(before)
        except (TypeError, ValueError):
            raise SourceResponseError('invalid_query_checkpoint') from None
        if state is None:
            return 'null', 0, None, before
        if not isinstance(state, dict) or set(state) - {'api_cursor', 'offset', 'page_digest'}:
            raise SourceResponseError('invalid_query_checkpoint')
        api_cursor = state.get('api_cursor', 'null')
        offset = state.get('offset', 0)
        page_digest = state.get('page_digest')
        if not isinstance(api_cursor, str) or type(offset) is not int or offset < 0:
            raise SourceResponseError('invalid_query_checkpoint')
        if page_digest is not None and (not isinstance(page_digest, str) or len(page_digest) > 64):
            raise SourceResponseError('invalid_query_checkpoint')
        return api_cursor, offset, page_digest, before

    @staticmethod
    def _page_digest(page):
        return _digest({
            'ids': [item.illust_id for item in page.artworks],
            'next': page.next_cursor_json,
            'exhausted': page.exhausted,
        })

    @staticmethod
    def _terminal_cursor():
        return canonical_json({'api_cursor': 'null', 'offset': 0, 'page_digest': None})

    @staticmethod
    def _next_cursor(next_cursor):
        return canonical_json({
            'api_cursor': next_cursor, 'offset': 0, 'page_digest': None,
        })

    @classmethod
    def _after_page(cls, page, api_cursor, page_digest, offset):
        if page.exhausted:
            return cls._terminal_cursor(), True
        if offset >= len(page.artworks):
            return cls._next_cursor(page.next_cursor_json), False
        return canonical_json({
            'api_cursor': api_cursor, 'offset': offset, 'page_digest': page_digest,
        }), False

    @staticmethod
    def _coverage(unit, loaded, page, offset, page_digest, complete):
        return {
            'coverage_complete': bool(complete),
            'logical_source': unit.logical_source,
            'actual_source': loaded.actual_source,
            'actual_sort': loaded.actual_sort,
            'actual_date': loaded.actual_date,
            'page_digest': page_digest,
            'offset': offset,
        }

    @staticmethod
    def _page_identity(unit, api_cursor, page_digest, offset, illust_id):
        return _digest({
            'source': unit.source_id, 'pass': unit.pass_id,
            'entity': unit.entity_id, 'cursor': api_cursor,
            'digest': page_digest, 'offset': offset, 'illust': illust_id,
        })

    # -- scope/carry ----------------------------------------------------

    def _prior_query_complete(self):
        value = self.repository.get_checkpoint(
            self.job_id, 'query.scope', self.request.query.source,
            self.request.account_ref,
        )
        return (
            value is not None and value['exhausted']
            and json.loads(value['coverage_json']).get('coverage_complete') is True
        )

    def _mark_scope(self, complete, reason):
        value = self.repository.get_checkpoint(
            self.job_id, 'query.scope', self.request.query.source,
            self.request.account_ref,
        )
        before = value['cursor_json'] if value is not None else 'null'
        after = canonical_json({'source_complete': bool(complete), 'reason': reason})
        coverage = canonical_json({
            'coverage_complete': bool(complete),
            'reason_code': reason,
            'source': self.request.query.source,
        })
        self._accept(SourcePage(
            'query.scope', self.request.query.source, self.request.account_ref,
            _digest({'before': before, 'after': after}), (), before, after,
            bool(complete), 'source_default', coverage,
        ))


__all__ = ['QueryCollection']
