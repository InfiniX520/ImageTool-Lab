"""Bounded source values independent of the Pixiv client and its credentials."""
from __future__ import annotations

from dataclasses import dataclass
from core.candidate_filter import ArtworkMetadata, SourceResponseError, display_text, stable_id
from core.task_contracts import normalize_json


@dataclass(frozen=True, slots=True)
class FollowArtist:
    artist_id: str
    name: str = ''

    def __post_init__(self):
        object.__setattr__(self, 'artist_id', stable_id(self.artist_id))
        object.__setattr__(self, 'name', display_text(self.name))


@dataclass(frozen=True, slots=True)
class FollowingPage:
    artists: tuple[FollowArtist, ...]
    next_cursor_json: str = 'null'
    exhausted: bool = False
    expected_total: int | None = None
    coverage_complete: bool = True

    def __post_init__(self):
        values = tuple(self.artists)
        if len(values) > 2000 or not all(isinstance(value, FollowArtist) for value in values):
            raise SourceResponseError('invalid_following_page')
        if len({value.artist_id for value in values}) != len(values):
            raise SourceResponseError('duplicate_following_artist')
        object.__setattr__(self, 'artists', values)
        cursor = normalize_json(self.next_cursor_json)
        if type(self.exhausted) is not bool or type(self.coverage_complete) is not bool:
            raise SourceResponseError('invalid_following_coverage')
        if self.exhausted and cursor != 'null':
            raise SourceResponseError('exhausted_following_has_cursor')
        if not self.exhausted and cursor == 'null':
            raise SourceResponseError('following_cursor_missing')
        if self.expected_total is not None and (type(self.expected_total) is not int or not 0 <= self.expected_total <= 1_000_000):
            raise SourceResponseError('invalid_following_total')
        object.__setattr__(self, 'next_cursor_json', cursor)


@dataclass(frozen=True, slots=True)
class ArtworkPage:
    artworks: tuple[ArtworkMetadata, ...]
    next_cursor_json: str = 'null'
    exhausted: bool = False
    coverage_complete: bool = True

    def __post_init__(self):
        values = tuple(self.artworks)
        if len(values) > 2000 or not all(isinstance(value, ArtworkMetadata) for value in values):
            raise SourceResponseError('invalid_artworks_page')
        if len({value.illust_id for value in values}) != len(values):
            raise SourceResponseError('duplicate_page_artwork')
        object.__setattr__(self, 'artworks', values)
        cursor = normalize_json(self.next_cursor_json)
        if type(self.exhausted) is not bool or type(self.coverage_complete) is not bool:
            raise SourceResponseError('invalid_artwork_coverage')
        if (self.exhausted and cursor != 'null') or (not self.exhausted and cursor == 'null'):
            raise SourceResponseError('invalid_artworks_cursor')
        object.__setattr__(self, 'next_cursor_json', cursor)
