"""Pure metadata normalization and optional AND filters; no HTTP or image IO."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Mapping

from core.page_planner import plan_original_pages
from core.task_contracts import CandidateDecision, CollectionRequest, ContractError, FilterSpec, canonical_json


class SourceResponseError(ValueError):
    """A bounded safe code, never the raw server payload or exception text."""
    def __init__(self, code, *, retry_after_seconds=None):
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)


def stable_id(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,32}', value):
        raise SourceResponseError('invalid_remote_id')
    return value


def display_text(value):
    if not isinstance(value, str):
        return ''
    return ''.join(' ' if ord(char) < 32 else char for char in value)[:4096]


def _optional_number(value, minimum=0, maximum=1_000_000_000):
    return value if type(value) is int and minimum <= value <= maximum else None


@dataclass(frozen=True, slots=True)
class ArtworkMetadata:
    illust_id: str
    artist_id: str = ''
    artist_name: str = ''
    title: str = ''
    kind: str = 'unknown'
    page_count: int | None = None
    bookmarks: int | None = None
    ai_flag: bool | None = None
    original_urls: tuple[str, ...] = ()
    available: bool | None = None
    publication_date: str = ''

    def __post_init__(self):
        object.__setattr__(self, 'illust_id', stable_id(self.illust_id))
        if self.artist_id:
            object.__setattr__(self, 'artist_id', stable_id(self.artist_id))
        for field in ['artist_name', 'title']:
            object.__setattr__(self, field, display_text(getattr(self, field)))
        if self.kind not in {'illust', 'manga', 'ugoira', 'unknown'}:
            raise SourceResponseError('invalid_artwork_kind')
        if self.page_count is not None and _optional_number(self.page_count, 1, 10000) is None:
            raise SourceResponseError('invalid_page_count')
        if self.bookmarks is not None and _optional_number(self.bookmarks) is None:
            raise SourceResponseError('invalid_bookmarks')
        if self.ai_flag is not None and type(self.ai_flag) is not bool:
            raise SourceResponseError('invalid_ai_flag')
        if self.available is not None and type(self.available) is not bool:
            raise SourceResponseError('invalid_availability')
        urls = tuple(self.original_urls)
        if len(urls) > 10000 or not all(isinstance(value, str) and len(value) <= 4096 for value in urls):
            raise SourceResponseError('invalid_original_metadata')
        object.__setattr__(self, 'original_urls', urls)
        if self.publication_date:
            try:date.fromisoformat(self.publication_date)
            except (ValueError, TypeError):raise SourceResponseError('invalid_publication_date') from None


def _publication_date(value):
    if isinstance(value,str) and len(value)<=64:
        try:return date.fromisoformat(value[:10]).isoformat()
        except ValueError:pass
    return ''


@dataclass(frozen=True, slots=True)
class FilterEvaluation:
    state: str
    reasons: tuple[str, ...] = ()


def evaluate_filters(filters: FilterSpec, *, bookmarks=None, ai_flag=None) -> FilterEvaluation:
    """Known rejection wins over unknown fields; disabled fields need no lookup."""
    if not isinstance(filters, FilterSpec):
        raise TypeError('FilterSpec is required')
    rejected, unknown = [], []
    if filters.min_bookmarks is not None:
        if _optional_number(bookmarks) is None:
            unknown.append('bookmarks_unknown')
        elif bookmarks < filters.min_bookmarks:
            rejected.append('bookmarks_below_minimum')
    if filters.exclude_ai:
        if ai_flag is True:
            rejected.append('ai_excluded')
        elif ai_flag is not False:
            unknown.append('ai_flag_unknown')
    if rejected:
        return FilterEvaluation('filtered', tuple(rejected))
    if unknown:
        return FilterEvaluation('metadata_unknown', tuple(unknown))
    return FilterEvaluation('allow')


def normalize_artwork(raw: Mapping) -> ArtworkMetadata:
    """Whitelist App API metadata; never substitute preview/large URLs."""
    if not isinstance(raw, Mapping) or 'error' in raw:
        raise SourceResponseError('invalid_artwork_response')
    artwork = stable_id(raw.get('id'))
    user = raw.get('user')
    artist = stable_id(user.get('id')) if isinstance(user, Mapping) and user.get('id') is not None else ''
    count = _optional_number(raw.get('page_count'), 1, 10000)
    kind = raw.get('type') if raw.get('type') in {'illust', 'manga', 'ugoira'} else 'unknown'
    ai_type = raw.get('illust_ai_type')
    ai = (ai_type == 2) if type(ai_type) is int and ai_type in {1, 2} else None
    urls = ()
    if count == 1:
        single = raw.get('meta_single_page')
        original = single.get('original_image_url') if isinstance(single, Mapping) else None
        if isinstance(original, str) and original:
            urls = (original,)
    elif count is not None:
        pages = raw.get('meta_pages')
        if isinstance(pages, (list, tuple)) and len(pages) == count:
            values = []
            for page in pages:
                images = page.get('image_urls') if isinstance(page, Mapping) else None
                original = images.get('original') if isinstance(images, Mapping) else None
                if not isinstance(original, str) or not original:
                    break
                values.append(original)
            if len(values) == count:
                urls = tuple(values)
    visible = raw.get('visible')
    return ArtworkMetadata(artwork, artist, user.get('name', '') if isinstance(user, Mapping) else '',
        raw.get('title', ''), kind, count, _optional_number(raw.get('total_bookmarks')), ai, urls,
        visible if type(visible) is bool else None, _publication_date(raw.get('create_date')))


def merge_details(original: ArtworkMetadata, detail: ArtworkMetadata) -> ArtworkMetadata:
    if original.illust_id != detail.illust_id:
        raise SourceResponseError('detail_identity_mismatch')
    if original.artist_id and detail.artist_id and original.artist_id != detail.artist_id:
        raise SourceResponseError('detail_artist_mismatch')
    return replace(detail, artist_id=detail.artist_id or original.artist_id,
        artist_name=detail.artist_name or original.artist_name, title=detail.title or original.title,
        kind=detail.kind if detail.kind != 'unknown' else original.kind,
        bookmarks=detail.bookmarks if detail.bookmarks is not None else original.bookmarks,
        ai_flag=detail.ai_flag if detail.ai_flag is not None else original.ai_flag,
        available=detail.available if detail.available is not None else original.available,
        page_count=detail.page_count if detail.page_count is not None else original.page_count,
        original_urls=detail.original_urls or original.original_urls,
        publication_date=detail.publication_date or original.publication_date)


def needs_details(metadata: ArtworkMetadata, request: CollectionRequest) -> bool:
    if metadata.available is False or metadata.kind == 'ugoira':
        return False
    verdict = evaluate_filters(request.query.filters, bookmarks=metadata.bookmarks, ai_flag=metadata.ai_flag)
    if verdict.state == 'filtered':
        return False
    return verdict.state == 'metadata_unknown' or metadata.kind == 'unknown' or metadata.page_count is None or not metadata.original_urls


def make_candidate(metadata: ArtworkMetadata, request: CollectionRequest, *, provenance=None) -> CandidateDecision:
    fields = dict(artist_id=metadata.artist_id, artist_name=metadata.artist_name, title=metadata.title,
        page_count=metadata.page_count, bookmarks=metadata.bookmarks, ai_flag=metadata.ai_flag,
        provenance_json=canonical_json({} if provenance is None else provenance))
    if metadata.available is False:
        return CandidateDecision(metadata.illust_id, 'unavailable', reason_codes=('artwork_unavailable',), **fields)
    if metadata.kind == 'ugoira':
        return CandidateDecision(metadata.illust_id, 'unsupported', reason_codes=('ugoira_unsupported',), **fields)
    verdict = evaluate_filters(request.query.filters, bookmarks=metadata.bookmarks, ai_flag=metadata.ai_flag)
    if verdict.state != 'allow':
        return CandidateDecision(metadata.illust_id, verdict.state, reason_codes=verdict.reasons, **fields)
    if metadata.kind == 'unknown' or metadata.page_count is None or not metadata.original_urls:
        return CandidateDecision(metadata.illust_id, 'metadata_unknown', reason_codes=('original_metadata_unknown',), **fields)
    try:
        pages = plan_original_pages(metadata.illust_id, metadata.page_count, metadata.original_urls,
            request.output, quality=request.quality, artist_id=metadata.artist_id, title=metadata.title,
            artist_name=metadata.artist_name, publication_date=metadata.publication_date)
    except ContractError:
        return CandidateDecision(metadata.illust_id, 'metadata_unknown', reason_codes=('invalid_original_metadata',), **fields)
    return CandidateDecision(metadata.illust_id, 'download', pages=pages, **fields)
