from copy import deepcopy
import json

import pytest

from core.candidate_filter import SourceResponseError
from core.pixiv_follow_source import PixivFollowSource


class Api:
    requests_kwargs={'timeout':(5,15),'allow_redirects':False}
    def __init__(self):self.calls=[];self.result={'illusts':[],'next_url':None}
    def user_following(self,**kwargs):
        self.calls.append(('following',kwargs))
        return self.result
    def user_illusts(self,**kwargs):
        self.calls.append(('works',kwargs))
        return self.result
    def illust_follow(self,**kwargs):
        self.calls.append(('new',kwargs))
        return self.result
    def illust_detail(self,**kwargs):
        self.calls.append(('detail',kwargs))
        return self.result


def source(api):return PixivFollowSource(api,account_id='9',account_ref='offline-account')


def test_adapter_construction_has_no_io_and_requires_bounded_transport():
    api=Api();source(api)
    assert api.calls==[]
    for kwargs in [{},{'timeout':None,'allow_redirects':False},{'timeout':(5,16),'allow_redirects':False},{'timeout':10}]:
        api.requests_kwargs=kwargs
        with pytest.raises(ValueError):source(api)


def test_following_covers_public_then_private_without_relying_on_a_short_page():
    api=Api();api.result={'user_previews':[{'user':{'id':7,'name':'name'}}],
        'next_url':'https://app-api.pixiv.net/v1/user/following?user_id=9&restrict=public&offset=30'}
    client=source(api);first=client.following()
    assert not first.exhausted and json.loads(first.next_cursor_json)['offset']==30
    api.result={'user_previews':[],'next_url':None}
    second=client.following(first.next_cursor_json)
    assert not second.exhausted and json.loads(second.next_cursor_json)=={'offset':0,'restrict':'private'}
    third=client.following(second.next_cursor_json)
    assert third.exhausted and len(api.calls)==3
    assert api.calls[-1][1]['restrict']=='private'


@pytest.mark.parametrize('partition',['illust','manga','ugoira'])
def test_artist_types_are_explicit_and_empty_legal_result_is_not_retried(partition):
    api=Api();result=source(api).artist_works('7',partition=partition)
    assert result.exhausted and result.artworks==()
    assert api.calls==[('works',{'user_id':'7','type':partition,'filter':'for_ios','offset':0})]


@pytest.mark.parametrize('url',[
    'https://evil.invalid/v1/user/illusts?offset=30',
    'https://app-api.pixiv.net/v1/user/illusts?user_id=8&offset=30',
    'https://app-api.pixiv.net/v1/user/illusts?type=manga&offset=30',
    'https://app-api.pixiv.net/v1/user/illusts?offset=0',
    'https://app-api.pixiv.net/v1/user/illusts?offset=30&access_token=synthetic-secret',
    'https://app-api.pixiv.net/v1/user/illusts?offset=30&offset=60',
])
def test_next_url_only_contributes_safe_validated_cursor_fields(url):
    api=Api();api.result={'illusts':[],'next_url':url}
    with pytest.raises(SourceResponseError) as error:source(api).artist_works('7')
    assert len(api.calls)==1
    assert 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize('result',[None,{}, {'error':{'message':'synthetic-secret'}},{'illusts':[]}])
def test_invalid_or_error_payload_never_means_empty_artist(result):
    api=Api();api.result=result
    with pytest.raises(SourceResponseError):source(api).artist_works('7')
    assert len(api.calls)==1


def test_new_works_preserves_its_visibility_and_own_cursor():
    api=Api();api.result={'illusts':[],'next_url':'https://app-api.pixiv.net/v2/illust/follow?restrict=private&offset=30'}
    result=source(api).new_works(partition='private')
    assert not result.exhausted and json.loads(result.next_cursor_json)=={'offset':30}
    assert api.calls[0][1]=={'restrict':'private','offset':0}


def test_installed_app_client_accepts_the_required_non_network_configuration():
    from pixivpy3 import AppPixivAPI
    api=AppPixivAPI(timeout=(5,15),allow_redirects=False)
    try:
        client=source(api)
        assert client.account_id=='9'
    finally:
        api.requests.close()
