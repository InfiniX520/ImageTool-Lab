"""Bound App JSON responses before the existing SDK decodes them.

One request means one transport attempt: redirects and adapter retries are off.
Only the owner thread uses this Session. No authentication is performed here.
"""
from __future__ import annotations

import io
import re
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict


_BLOCK=64*1024
_CODES=frozenset({'metadata_request_failed','metadata_invalid_request','metadata_host_refused',
    'metadata_stopped','metadata_deadline','metadata_redirect_refused','metadata_access_denied',
    'metadata_not_found','metadata_rate_limited','metadata_server_unavailable','metadata_http_error',
    'metadata_response_too_large','metadata_invalid_length','metadata_response_truncated',
    'metadata_non_json','metadata_invalid_encoding','metadata_invalid_chunk'})


class MetadataRequestError(requests.RequestException):
    def __init__(self,code,*,retry_after_seconds=None):
        self.code=code if code in _CODES else 'metadata_request_failed'
        self.retry_after_seconds=retry_after_seconds
        super().__init__(self.code)


@dataclass(frozen=True,slots=True)
class MetadataPolicy:
    max_response_bytes:int=8*1024*1024
    max_seconds:float=30
    connect_timeout:float=5
    read_timeout:float=15

    def __post_init__(self):
        if type(self.max_response_bytes) is not int or not 1<=self.max_response_bytes<=64*1024*1024:
            raise ValueError('Invalid metadata byte limit')
        for value in (self.max_seconds,self.connect_timeout,self.read_timeout):
            if type(value) not in (int,float) or not 0<value<=300:
                raise ValueError('Invalid metadata time limit')


def _retry_after(headers):
    value=headers.get('Retry-After','')
    if not isinstance(value,str) or len(value)>1024:
        return 30
    if value.isascii() and value.isdecimal() and len(value)<=9:
        return max(1,min(86400,int(value)))
    try:
        moment=parsedate_to_datetime(value)
        if moment.tzinfo is None:moment=moment.replace(tzinfo=timezone.utc)
        return max(1,min(86400,int((moment-datetime.now(timezone.utc)).total_seconds())))
    except (TypeError,ValueError,OverflowError):
        return 30


class BoundedMetadataSession(requests.Session):
    def __init__(self,*,policy=MetadataPolicy(),stop_event=None,clock=time.monotonic,send=None,before_request=None):
        super().__init__()
        self.policy,self.stop_event,self.clock=policy,stop_event,clock
        self._send=send if send is not None else super().request
        self.mount('https://',HTTPAdapter(pool_connections=2,pool_maxsize=2,max_retries=0,pool_block=True))
        self._closed_by_owner=False
        self.before_request=before_request

    def _check(self,deadline):
        if self._closed_by_owner or (self.stop_event is not None and self.stop_event.is_set()):
            raise MetadataRequestError('metadata_stopped')
        if self.clock()>=deadline:
            raise MetadataRequestError('metadata_deadline')

    def request(self,method,url,**kwargs):
        response=None
        try:
            try:
                parsed=urlsplit(url)
                valid=(parsed.scheme=='https' and parsed.hostname in {'app-api.pixiv.net','oauth.secure.pixiv.net'}
                    and parsed.port in (None,443) and not parsed.username and not parsed.password and not parsed.fragment)
            except (TypeError,ValueError):
                valid=False
            if not valid:raise MetadataRequestError('metadata_host_refused')
            if method.upper() not in {'GET','POST'}:
                raise MetadataRequestError('metadata_invalid_request')
            if self.before_request is not None and not self.before_request(self.stop_event):
                raise MetadataRequestError('metadata_stopped')
            deadline=self.clock()+self.policy.max_seconds
            self._check(deadline)
            headers=CaseInsensitiveDict(kwargs.pop('headers',None) or {})
            headers['Accept-Encoding']='gzip, identity'
            supplied=kwargs.pop('timeout',None)
            if supplied is None:
                supplied=(self.policy.connect_timeout,self.policy.read_timeout)
            supplied=supplied if isinstance(supplied,tuple) and len(supplied)==2 else (supplied,supplied)
            if any(type(value) not in (int,float) or value<=0 for value in supplied):
                raise MetadataRequestError('metadata_invalid_request')
            remaining=max(0.001,deadline-self.clock())
            kwargs.update(headers=headers,stream=True,allow_redirects=False,
                timeout=(min(self.policy.connect_timeout,supplied[0],remaining),min(self.policy.read_timeout,supplied[1],remaining)))
            response=self._send(method,url,**kwargs)
            self._check(deadline)
            status=response.status_code
            if status==429:
                raise MetadataRequestError('metadata_rate_limited',retry_after_seconds=_retry_after(response.headers))
            if status==503:
                raise MetadataRequestError('metadata_server_unavailable',retry_after_seconds=_retry_after(response.headers))
            if status in (401,403):raise MetadataRequestError('metadata_access_denied')
            if status==404:raise MetadataRequestError('metadata_not_found')
            if 300<=status<400:raise MetadataRequestError('metadata_redirect_refused')
            if status!=200:raise MetadataRequestError('metadata_http_error')
            content_type=response.headers.get('Content-Type','').split(';',1)[0].strip().lower()
            if content_type not in {'','application/json','text/json','application/octet-stream'}:
                raise MetadataRequestError('metadata_non_json')
            length=response.headers.get('Content-Length')
            if length is not None:
                if not isinstance(length,str) or re.fullmatch('[0-9]{1,20}',length) is None:
                    raise MetadataRequestError('metadata_invalid_length')
                length=int(length)
                if length>self.policy.max_response_bytes:
                    raise MetadataRequestError('metadata_response_too_large')
                if response.headers.get('Transfer-Encoding'):
                    raise MetadataRequestError('metadata_invalid_length')
            encoding=response.headers.get('Content-Encoding','').strip().lower()
            if encoding not in {'','identity','gzip'}:
                raise MetadataRequestError('metadata_invalid_encoding')
            decoder=zlib.decompressobj(16+zlib.MAX_WBITS) if encoding=='gzip' else None
            output=io.BytesIO();wire_bytes=0
            try:
                for data in response.raw.stream(_BLOCK,decode_content=False):
                    self._check(deadline)
                    if not isinstance(data,bytes) or len(data)>_BLOCK:
                        raise MetadataRequestError('metadata_invalid_chunk')
                    wire_bytes+=len(data)
                    if wire_bytes>self.policy.max_response_bytes:
                        raise MetadataRequestError('metadata_response_too_large')
                    pending=data
                    while pending:
                        self._check(deadline)
                        room=min(_BLOCK,self.policy.max_response_bytes-output.tell()+1)
                        piece=decoder.decompress(pending,room) if decoder is not None else pending
                        pending=decoder.unconsumed_tail if decoder is not None else b''
                        if output.tell()+len(piece)>self.policy.max_response_bytes:
                            raise MetadataRequestError('metadata_response_too_large')
                        output.write(piece)
                        if decoder is not None and decoder.unused_data:
                            raise MetadataRequestError('metadata_invalid_encoding')
                if length is not None and wire_bytes!=length:
                    raise MetadataRequestError('metadata_response_truncated')
                if decoder is not None and not decoder.eof:
                    raise MetadataRequestError('metadata_response_truncated')
                body=output.getvalue()
            finally:
                output.close()
            self._check(deadline)
            prefix=body.lstrip()
            if not prefix.startswith((b'{',b'[')):
                raise MetadataRequestError('metadata_non_json')
            response._content=body
            response._content_consumed=True
            return response
        except MetadataRequestError:
            raise
        except Exception:
            raise MetadataRequestError('metadata_request_failed') from None
        finally:
            if response is not None:
                response.close()

    def close(self):
        self._closed_by_owner=True
        super().close()


def metadata_error(exc):
    """The SDK wraps transport errors; return only our allowlisted safe facts."""
    for _ in range(5):
        if isinstance(exc,MetadataRequestError):
            return exc
        exc=getattr(exc,'__cause__',None) or getattr(exc,'__context__',None)
        if exc is None:break
    return None
