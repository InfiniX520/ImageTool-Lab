import gzip
import threading
import tracemalloc

import pytest
import requests

from core.api_transport import BoundedMetadataSession, MetadataPolicy, MetadataRequestError, metadata_error


URL='https://app-api.pixiv.net/v1/user/illusts'


class Raw:
    def __init__(self,chunks):self.chunks=chunks;self.reads=0;self.closed=False
    def stream(self,amount,decode_content):
        assert amount==65536 and decode_content is False
        for value in self.chunks:
            self.reads+=1
            if callable(value):value();continue
            if isinstance(value,BaseException):raise value
            yield value
    def close(self):self.closed=True
    def release_conn(self):self.closed=True


def response(body=b'{"illusts":[],"next_url":null}',*,status=200,headers=None,chunks=None):
    value=requests.Response();value.status_code=status;value.encoding='utf-8'
    value.headers.update({'Content-Type':'application/json'} if headers is None else headers)
    value.raw=Raw([body] if chunks is None else chunks)
    return value


def session_for(value,**kwargs):
    calls=[]
    def send(method,url,**options):calls.append((method,url,options));return value
    return BoundedMetadataSession(send=send,**kwargs),calls


def test_valid_json_is_bounded_before_sdk_parsing_and_transport_has_one_attempt():
    raw=response();session,calls=session_for(raw)
    with session:
        result=session.get(URL,timeout=(100,100),allow_redirects=True)
        assert result.json()=={'illusts':[],'next_url':None}
        assert len(calls)==1 and raw.raw.closed
        sent=calls[0][2]
        assert sent['stream'] is True and sent['allow_redirects'] is False
        assert sent['timeout']==(5,15)
        assert session.get_adapter(URL).max_retries.total==0


def test_declared_oversize_is_rejected_before_reading():
    raw=response(headers={'Content-Type':'application/json','Content-Length':'99999'})
    session,calls=session_for(raw,policy=MetadataPolicy(max_response_bytes=1000))
    with session,pytest.raises(MetadataRequestError) as error:session.get(URL)
    assert error.value.code=='metadata_response_too_large' and raw.raw.reads==0
    assert len(calls)==1 and raw.raw.closed


def test_unknown_length_never_collects_more_than_the_body_budget():
    raw=response(chunks=[b'{'+b'x'*599,b'x'*600])
    session,_=session_for(raw,policy=MetadataPolicy(max_response_bytes=1000))
    with session,pytest.raises(MetadataRequestError) as error:session.get(URL)
    assert error.value.code=='metadata_response_too_large' and raw.raw.closed


def test_valid_gzip_is_streamed_once_and_sdk_gets_exact_decoded_json():
    body=b'{"illusts":[],"next_url":null}'
    encoded=gzip.compress(body)
    raw=response(encoded,headers={'Content-Type':'application/json','Content-Encoding':'gzip','Content-Length':str(len(encoded))})
    session,calls=session_for(raw)
    with session:assert session.get(URL).content==body
    assert len(calls)==1 and raw.raw.closed


def test_compressed_expansion_is_capped_before_a_large_allocation():
    compressed=gzip.compress(b'{"value":"'+b'x'*500000+b'"}')
    assert len(compressed)<1024
    raw=response(compressed,headers={'Content-Type':'application/json','Content-Encoding':'gzip'})
    session,_=session_for(raw,policy=MetadataPolicy(max_response_bytes=1024))
    tracemalloc.start()
    try:
        with session,pytest.raises(MetadataRequestError) as error:session.get(URL)
        _,peak=tracemalloc.get_traced_memory()
    finally:tracemalloc.stop()
    assert error.value.code=='metadata_response_too_large'
    assert peak<1024*1024 and raw.raw.closed


@pytest.mark.parametrize('value,code',[
    (response(b'{}',headers={'Content-Length':'3'}),'metadata_response_truncated'),
    (response(b'{}',headers={'Content-Length':'2','Transfer-Encoding':'chunked'}),'metadata_invalid_length'),
    (response(b'{}',headers={'Content-Length':'-1'}),'metadata_invalid_length'),
    (response(b'<html>maintenance</html>'),'metadata_non_json'),
    (response(b'{}',headers={'Content-Type':'text/html'}),'metadata_non_json'),
    (response(b'{}',headers={'Content-Encoding':'br'}),'metadata_invalid_encoding'),
    (response(gzip.compress(b'{}')[:-3],headers={'Content-Encoding':'gzip'}),'metadata_response_truncated'),
])
def test_invalid_responses_never_reach_json_decoding(value,code):
    session,calls=session_for(value)
    with session,pytest.raises(MetadataRequestError) as error:session.get(URL)
    assert error.value.code==code and len(calls)==1 and value.raw.closed


@pytest.mark.parametrize('status,code',[(302,'metadata_redirect_refused'),(401,'metadata_access_denied'),(403,'metadata_access_denied'),
    (404,'metadata_not_found'),(429,'metadata_rate_limited'),(503,'metadata_server_unavailable')])
def test_http_errors_have_safe_codes_and_never_hidden_retries(status,code):
    raw=response(status=status,headers={'Retry-After':'120','Set-Cookie':'synthetic-secret'})
    session,calls=session_for(raw)
    with session,pytest.raises(MetadataRequestError) as error:session.get(URL,headers={'Authorization':'Bearer synthetic-secret'})
    assert str(error.value)==code and len(calls)==1
    assert raw.raw.reads==0 and raw.raw.closed
    if status in (429,503):assert error.value.retry_after_seconds==120


def test_stop_before_request_and_during_response_close_without_a_second_attempt():
    stop=threading.Event();stop.set()
    session,calls=session_for(response(),stop_event=stop)
    with session,pytest.raises(MetadataRequestError) as error:session.get(URL)
    assert error.value.code=='metadata_stopped' and calls==[]
    stop.clear();raw=response(chunks=[b'{',stop.set,b'}'])
    session,calls=session_for(raw,stop_event=stop)
    with session,pytest.raises(MetadataRequestError) as error:session.get(URL)
    assert error.value.code=='metadata_stopped' and raw.raw.closed and len(calls)==1


def test_monotonic_deadline_checks_each_wire_chunk():
    clock=[0.0]
    def expire():clock[0]=2
    raw=response(chunks=[b'{',expire,b'}'])
    session,_=session_for(raw,clock=lambda:clock[0],policy=MetadataPolicy(max_seconds=1))
    with session,pytest.raises(MetadataRequestError) as error:session.get(URL)
    assert error.value.code=='metadata_deadline' and raw.raw.closed


def test_installed_sdk_wraps_transport_error_without_losing_safe_facts():
    from pixivpy3 import AppPixivAPI
    api=AppPixivAPI(timeout=(5,15),allow_redirects=False)
    api.requests.close()
    raw=response(status=429,headers={'Retry-After':'60'})
    api.requests,calls=session_for(raw)
    api.set_auth('synthetic-private-access')
    try:
        with pytest.raises(Exception) as raised:api.user_illusts('7')
        fact=metadata_error(raised.value)
        assert fact.code=='metadata_rate_limited' and fact.retry_after_seconds==60
        assert len(calls)==1 and 'synthetic-private-access' not in str(fact)
    finally:api.requests.close()


@pytest.mark.parametrize('url',['http://app-api.pixiv.net/v1/illusts','https://evil.invalid/x','https://user:synthetic-secret@app-api.pixiv.net/x'])
def test_unapproved_metadata_hosts_are_rejected_before_transport(url):
    session,calls=session_for(response())
    with session,pytest.raises(MetadataRequestError) as error:session.get(url)
    assert calls==[] and error.value.code=='metadata_host_refused'
