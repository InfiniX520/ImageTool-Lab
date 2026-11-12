"""Bounded middle-size samples and explicit human/AI visual review receipts."""
from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
from urllib.parse import urlsplit
import uuid

import requests

from core.image_validation import inspect_image, ImageValidationError
from organizer.common import OrganizerError, digest, groups, identity, integer, now, safe_path, text


def spread(ids, count):
    values = sorted({identity(v) for v in ids},key=int,reverse=True)
    count = min(integer(count,1,20),len(values))
    if count==0:
        return []
    if count==1:
        return values[:1]
    return [values[round(i*(len(values)-1)/(count-1))] for i in range(count)]


def image_url(url):
    if not isinstance(url,str) or len(url)>2048:
        raise OrganizerError('invalid_sample_url')
    try:
        parsed = urlsplit(url)
        if (parsed.scheme!='https' or parsed.hostname!='i.pximg.net' or parsed.port not in (None,443)
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.path.startswith(('/img-master/','/c/','/img-original/'))):
            raise OrganizerError('invalid_sample_url')
    except ValueError:
        raise OrganizerError('invalid_sample_url') from None
    return url


def download_sample(url, target, budget):
    from core.download_engine import build_common_headers
    url=image_url(url)
    maximum=min(8*1024*1024,budget.byte_limit-budget.bytes)
    if maximum<=0:
        raise OrganizerError('byte_budget_exhausted')
    budget.reserve()
    temporary=target.with_suffix('.part')
    if temporary.exists():
        raise OrganizerError('sample_partial_requires_review')
    owned=False
    headers=build_common_headers()
    headers['Accept-Encoding']='identity'
    try:
        with requests.Session() as session:
            with session.get(url,headers=headers,
                             stream=True,timeout=(8,12),allow_redirects=False) as response:
                budget.observed+=1
                if response.status_code!=200:
                    raise OrganizerError('image_http_stop_'+str(response.status_code))
                if response.headers.get('Content-Encoding','identity')!='identity':
                    raise OrganizerError('image_encoding_unsupported')
                size=response.headers.get('Content-Length')
                expected=integer(size,1,maximum) if size is not None else None
                received=0
                with temporary.open('xb') as stream:
                    owned=True
                    for block in response.iter_content(65536):
                        budget.consume(len(block));received+=len(block)
                        if received>maximum:
                            raise OrganizerError('sample_image_limit')
                        stream.write(block)
                    stream.flush();os.fsync(stream.fileno())
                evidence=inspect_image(temporary,expected_size=expected,
                    expected_media_type=response.headers.get('Content-Type'),max_file_bytes=maximum)
        if target.exists():
            raise OrganizerError('sample_target_conflict')
        os.link(temporary,target)
        return asdict(evidence)
    except requests.RequestException:
        raise OrganizerError('image_transport_stop') from None
    except ImageValidationError:
        raise OrganizerError('sample_image_invalid') from None
    finally:
        # This file is created only by this invocation, under the checked data root.
        if owned:
            temporary.unlink(missing_ok=True)


def cache_space(data_root, reserve=8*1024*1024):
    folder=safe_path(data_root,'samples')
    total=count=0
    if folder.exists():
        for path in folder.rglob('*'):
            safe_path(data_root,path.relative_to(data_root))
            count+=1
            if count>20000:
                raise OrganizerError('sample_cache_entry_limit')
            if path.is_file():
                total+=path.stat().st_size
    if total+reserve>512*1024*1024:
        raise OrganizerError('sample_cache_size_limit')


def sample(store, web, data_root, snapshot_id, artist_id, count=16, sample_id=None, downloader=download_sample):
    artist_id=identity(artist_id)
    snap=store.get('snapshot',snapshot_id)
    if snap['account_id']!=web.account_id or artist_id not in snap['artists']:
        raise OrganizerError('artist_not_in_account_snapshot')
    if sample_id:
        data=store.get('sample',sample_id)
        if (data['account_id'],data['artist_id'],data['snapshot_id'])!=(web.account_id,artist_id,snapshot_id):
            raise OrganizerError('sample_identity_mismatch')
    else:
        ids=web.call('works',artist_id=artist_id)['ids']
        if not isinstance(ids,list) or len(ids)>100000:
            raise OrganizerError('invalid_work_inventory')
        sample_id=uuid.uuid4().hex
        data={'id':sample_id,'snapshot_id':snapshot_id,'account_id':web.account_id,'artist_id':artist_id,
              'created_at':now(),'requested':integer(count,1,20),'available_works':len(set(ids)),
              'selected_ids':spread(ids,count),'selection':'evenly_spaced_work_ids_latest_to_earliest',
              'status':'collecting','images':{},'skipped':{}}
        store.put('sample',sample_id,data,create_only=True)
    try:
        for illust_id in data['selected_ids']:
            key=illust_id+'_p0'
            previous=data['images'].get(key)
            if previous:
                path=safe_path(data_root,previous['relative_path'])
                if not path.is_file() or digest(path)!=previous['sha256']:
                    raise OrganizerError('sample_cache_changed')
                continue
            if key in data['skipped']:
                continue
            meta=web.call('artwork',illust_id=illust_id)
            if (meta.get('artist_id'),meta.get('illust_id'))!=(artist_id,illust_id):
                raise OrganizerError('artwork_identity_mismatch')
            if meta.get('kind') not in (0,1):
                data['skipped'][key]='unsupported_artwork_type'
                store.put('sample',sample_id,data);continue
            pages=integer(meta.get('page_count'),1,10000)
            relative='samples/'+sample_id+'/'+key+'.image'
            path=safe_path(data_root,relative);path.parent.mkdir(parents=True,exist_ok=True)
            if path.exists():
                # A crash after file publication but before the receipt must not promote unowned bytes.
                raise OrganizerError('sample_file_without_receipt')
            cache_space(data_root)
            evidence=downloader(meta.get('url'),path,web.budget)
            data['images'][key]=dict(evidence,illust_id=illust_id,page=0,page_count=pages,
                date=str(meta.get('date',''))[:50],relative_path=relative)
            store.put('sample',sample_id,data)
        data['status']='ready_for_review';data.pop('stop_code',None)
        store.put('sample',sample_id,data)
        return data
    except OrganizerError as exc:
        data['stop_code']=exc.code;store.put('sample',sample_id,data);raise


def supplement(store,web,data_root,sample_id,illust_id,page,downloader=download_sample):
    data=store.get('sample',sample_id)
    illust_id=identity(illust_id);page=integer(page,0,9999)
    if data['account_id']!=web.account_id or illust_id not in data['selected_ids']:
        raise OrganizerError('sample_identity_mismatch')
    key=illust_id+'_p'+str(page)
    if key in data['images']:
        evidence=data['images'][key]
        if digest(safe_path(data_root,evidence['relative_path']))!=evidence['sha256']:
            raise OrganizerError('sample_cache_changed')
        return data
    if len(data['images'])>=40:
        raise OrganizerError('sample_page_limit')
    meta=web.call('artwork',illust_id=illust_id)
    if meta['artist_id']!=data['artist_id'] or meta['illust_id']!=illust_id or meta['kind'] not in (0,1):
        raise OrganizerError('artwork_identity_mismatch')
    if page>=integer(meta['page_count'],1,10000):
        raise OrganizerError('sample_page_out_of_range')
    urls=web.call('page',illust_id=illust_id,page=page)
    relative='samples/'+sample_id+'/'+key+'.image';target=safe_path(data_root,relative)
    if target.exists():
        raise OrganizerError('sample_file_without_receipt')
    target.parent.mkdir(parents=True,exist_ok=True);cache_space(data_root)
    evidence=downloader(urls['url'],target,web.budget)
    data['images'][key]=dict(evidence,illust_id=illust_id,page=page,page_count=meta['page_count'],
                            date=str(meta.get('date',''))[:50],relative_path=relative)
    store.put('sample',sample_id,data)
    return data


def review(store, data_root, payload, *, attest_viewed):
    if not attest_viewed:
        raise OrganizerError('explicit_view_attestation_required')
    sample_id=text(payload.get('sample_id'),128)
    data=store.get('sample',sample_id)
    supplied=payload.get('viewed')
    if not isinstance(supplied,list) or not 1<=len(supplied)<=40:
        raise OrganizerError('viewed_images_required')
    viewed=[]
    for row in supplied:
        if not isinstance(row,dict):
            raise OrganizerError('invalid_view_evidence')
        key=identity(row.get('illust_id'))+'_p'+str(integer(row.get('page'),0,9999))
        evidence=data['images'].get(key)
        if (not evidence or row.get('sha256')!=evidence['sha256'] or key in viewed
            or digest(safe_path(data_root,evidence['relative_path']))!=evidence['sha256']):
            raise OrganizerError('view_evidence_mismatch')
        viewed.append(key)
    needs_more=payload.get('needs_more')
    if type(needs_more) is not bool:
        raise OrganizerError('review_completeness_required')
    required=min(12,data['available_works'])
    if not needs_more and (len({data['images'][k]['illust_id'] for k in viewed})<required or required==0):
        raise OrganizerError('insufficient_visual_evidence')
    value={'id':uuid.uuid4().hex,'created_at':now(),'account_id':data['account_id'],
           'artist_id':data['artist_id'],'snapshot_id':data['snapshot_id'],'sample_id':sample_id,'viewed':viewed,
           'observer':text(payload.get('observer'),100),'rationale':text(payload.get('rationale'),2000),
           'rules_version':text(payload.get('rules_version'),100),'coverage_note':text(payload.get('coverage_note'),1000),
           'proposed_groups':groups(payload.get('proposed_groups')),'needs_more':needs_more,
           'evidence':[dict(data['images'][key]) for key in viewed],
           'attestation':'explicit_observer_statement_not_automatic_image_understanding'}
    store.put('review',value['id'],value,create_only=True)
    return value
