"""A single-call adapter over the installed App client, never automatic login."""
from __future__ import annotations

import json
from collections.abc import Mapping
from urllib.parse import parse_qs, urlsplit

from core.candidate_filter import SourceResponseError, normalize_artwork, stable_id
from core.api_transport import metadata_error
from core.follow_contracts import ArtworkPage, FollowArtist, FollowingPage
from core.task_contracts import canonical_json, normalize_json


def _offset(value):
    if type(value) is int and 0 <= value <= 10_000_000:
        return value
    if isinstance(value,str) and value.isascii() and value.isdecimal() and len(value)<=8:
        return _offset(int(value))
    raise SourceResponseError('invalid_source_offset')


class PixivFollowSource:
    # Separate types avoid trusting an undocumented default to include manga.
    partitions = ('illust','manga','ugoira')
    new_partitions = ('public','private')

    def __init__(self, app_api, *, account_id, account_ref):
        self._api = app_api
        self.account_id,self.account_ref = stable_id(account_id),account_ref
        kwargs = getattr(app_api,'requests_kwargs',None)
        if not isinstance(kwargs,dict) or kwargs.get('allow_redirects') is not False:
            raise ValueError('Source client must disable automatic redirects')
        timeout = kwargs.get('timeout')
        values = timeout if isinstance(timeout,tuple) and len(timeout)==2 else (timeout,)
        if any(type(value) not in (int,float) or not 0<value<=15 for value in values):
            raise ValueError('Source client must use bounded connect/read timeouts')
        transport = getattr(app_api,'requests',None)
        adapter = getattr(transport,'get_adapter',None)
        if callable(adapter) and getattr(adapter('https://app-api.pixiv.net').max_retries,'total',None) not in (0,False):
            raise ValueError('Source transport must disable automatic retries')

    def _call(self, method, **kwargs):
        try:
            result = getattr(self._api,method)(**kwargs)
        except Exception as exc:
            fact=metadata_error(exc)
            if fact is not None:
                raise SourceResponseError(fact.code,retry_after_seconds=fact.retry_after_seconds) from None
            raise SourceResponseError('source_transport_failed') from None
        if not isinstance(result,Mapping) or 'error' in result:
            raise SourceResponseError('source_api_error')
        return result

    @staticmethod
    def _cursor(text, *, visibility=False):
        value = json.loads(normalize_json(text))
        if value is None:
            return {'restrict':'public','offset':0} if visibility else {'offset':0}
        expected = {'restrict','offset'} if visibility else {'offset'}
        if not isinstance(value,dict) or set(value)!=expected:
            raise SourceResponseError('invalid_source_cursor')
        if visibility and value['restrict'] not in {'public','private'}:
            raise SourceResponseError('invalid_follow_visibility')
        value['offset'] = _offset(value['offset'])
        return value

    @staticmethod
    def _next(result, *, endpoint, current, user_id=None, partition=None, visibility=None):
        if 'next_url' not in result:
            raise SourceResponseError('pagination_metadata_missing')
        url = result['next_url']
        if url is None:
            return None
        if not isinstance(url,str) or len(url)>4096:
            raise SourceResponseError('invalid_next_page')
        parsed = urlsplit(url)
        if (parsed.scheme!='https' or parsed.hostname!='app-api.pixiv.net' or parsed.port not in (None,443)
                or parsed.username or parsed.password or parsed.fragment or parsed.path!=endpoint):
            raise SourceResponseError('invalid_next_page')
        params = parse_qs(parsed.query,keep_blank_values=True)
        if set(params)-{'offset','user_id','type','filter','restrict'} or any(len(values)!=1 for values in params.values()):
            raise SourceResponseError('unsupported_pagination_fields')
        if user_id is not None and 'user_id' in params and params['user_id'][0]!=user_id:
            raise SourceResponseError('next_page_identity_mismatch')
        if partition is not None and 'type' in params and params['type'][0]!=partition:
            raise SourceResponseError('next_page_type_mismatch')
        if visibility is not None and 'restrict' in params and params['restrict'][0]!=visibility:
            raise SourceResponseError('next_page_visibility_mismatch')
        if 'filter' in params and params['filter'][0]!='for_ios':
            raise SourceResponseError('next_page_filter_mismatch')
        following = _offset(params.get('offset',[None])[0])
        if following<=current:
            raise SourceResponseError('pagination_did_not_advance')
        return following

    def following(self, cursor_json='null'):
        cursor = self._cursor(cursor_json,visibility=True)
        result = self._call('user_following',user_id=self.account_id,restrict=cursor['restrict'],offset=cursor['offset'])
        values = result.get('user_previews')
        if not isinstance(values,list) or len(values)>2000:
            raise SourceResponseError('following_list_missing')
        artists = []
        for preview in values:
            user = preview.get('user') if isinstance(preview,Mapping) else None
            if not isinstance(user,Mapping):
                raise SourceResponseError('following_user_missing')
            artists.append(FollowArtist(stable_id(user.get('id')),user.get('name','')))
        next_offset = self._next(result,endpoint='/v1/user/following',current=cursor['offset'],user_id=self.account_id,visibility=cursor['restrict'])
        if next_offset is not None:
            after = {'restrict':cursor['restrict'],'offset':next_offset}
        elif cursor['restrict']=='public':
            after = {'restrict':'private','offset':0}
        else:
            after = None
        return FollowingPage(tuple(artists),canonical_json(after),after is None)

    def artist_works(self, artist_id, cursor_json='null', *, partition='illust'):
        artist_id = stable_id(artist_id)
        if partition not in self.partitions:
            raise SourceResponseError('invalid_artwork_partition')
        cursor = self._cursor(cursor_json)
        result = self._call('user_illusts',user_id=artist_id,type=partition,filter='for_ios',offset=cursor['offset'])
        return self._artwork_page(result,endpoint='/v1/user/illusts',cursor=cursor,user_id=artist_id,partition=partition)

    def new_works(self, cursor_json='null', *, partition='public'):
        if partition not in self.new_partitions:
            raise SourceResponseError('invalid_follow_visibility')
        cursor = self._cursor(cursor_json)
        result = self._call('illust_follow',restrict=partition,offset=cursor['offset'])
        return self._artwork_page(result,endpoint='/v2/illust/follow',cursor=cursor,visibility=partition)

    def _artwork_page(self,result,*,endpoint,cursor,user_id=None,partition=None,visibility=None):
        values = result.get('illusts')
        if not isinstance(values,list) or len(values)>2000:
            raise SourceResponseError('artwork_list_missing')
        artworks = tuple(normalize_artwork(value) for value in values)
        if user_id is not None and any(meta.artist_id and meta.artist_id!=user_id for meta in artworks):
            raise SourceResponseError('artist_page_identity_mismatch')
        if partition is not None and any(meta.kind not in {partition,'unknown'} for meta in artworks):
            raise SourceResponseError('artwork_partition_mismatch')
        after = self._next(result,endpoint=endpoint,current=cursor['offset'],user_id=user_id,partition=partition,visibility=visibility)
        return ArtworkPage(artworks,'null' if after is None else canonical_json({'offset':after}),after is None)

    def details(self,illust_id):
        illust_id = stable_id(illust_id)
        result = self._call('illust_detail',illust_id=illust_id)
        raw = result.get('illust')
        meta = normalize_artwork(raw)
        if meta.illust_id!=illust_id:
            raise SourceResponseError('detail_identity_mismatch')
        return meta

    def close(self):
        """Explicit owner disposal; no requests or authentication are performed."""
        self._api.requests.close()
