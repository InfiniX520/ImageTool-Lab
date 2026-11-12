"""Reviewable native desktop entry; original launcher remains unchanged.

Default is an entirely offline demo in an explicitly selected project root.
Real mode is opt-in and should only be used after the phase acceptance gates.
"""
from __future__ import annotations

import argparse
import json
import struct
import tkinter as tk
import zlib
from concurrent.futures import Future
from pathlib import Path

from core.app_paths import AppPaths
from core.candidate_filter import ArtworkMetadata
from core.collection_desktop import DesktopSession
from core.follow_contracts import ArtworkPage, FollowArtist, FollowingPage
from core.page_delivery import PageDeliveryExecutor
from gui.collection_controller import CollectionController


def _png():
    def chunk(kind, value):
        return struct.pack('>I',len(value))+kind+value+struct.pack('>I',zlib.crc32(kind+value)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(b'\x00\x20\x60\xa0\xff'))+chunk(b'IEND',b'')


class DemoSource:
    account_ref='pixiv:7'
    partitions=('illust',)
    new_partitions=('public','private')
    def following(self,cursor='null'):
        return FollowingPage(tuple(FollowArtist(str(value),name) for value,name in
            ((101,'山间记录'),(102,'雨天速写'),(103,'光与远方'))),exhausted=True,expected_total=3)
    def artist_works(self,artist_id,cursor='null',*,partition='illust'):
        if partition=='manga':return ArtworkPage((),exhausted=True)
        values=[]
        for index in range(4):
            work=str(int(artist_id)*100+index)
            values.append(ArtworkMetadata(work,artist_id,'示例画师 '+artist_id,'离线示例作品 '+str(index+1),
                kind='illust',page_count=1,bookmarks=(200,900,2000,4000)[index],ai_flag=(False,True,False,None)[index],available=True,
                original_urls=(f'https://i.pximg.net/img-original/demo/{work}_p0.png',)))
        return ArtworkPage(tuple(values),exhausted=True)
    def new_works(self,cursor='null',*,partition='public'):
        return self.artist_works('101') if partition=='public' else ArtworkPage((),exhausted=True)
    def details(self,illust_id):
        for artist in ('101','102','103'):
            for artwork in self.artist_works(artist).artworks:
                if artwork.illust_id==illust_id:return artwork
        from core.candidate_filter import SourceResponseError
        raise SourceResponseError('artwork_not_found')
    def query_page(self,source,parameters_json='{}',cursor_json='null',*,requested_sort='source_default'):
        from core.pixiv_query_source import QueryPage
        values=tuple(item for artist in ('101','102','103') for item in self.artist_works(artist).artworks)
        params=json.loads(parameters_json)
        actual=source
        if source=='ranking':values=values[:8]
        elif source=='search':values=values[2:10]
        elif source=='artist':
            if params.get('partition',params.get('type','illust'))=='manga':values=()
            else:
                ids=params.get('artist_ids',[params.get('artist_id',params.get('user_id','101'))])
                values=tuple(item for item in values if item.artist_id in ids)
        elif source=='recommendations':
            values=values[-6:];actual='related' if params.get('kind')=='related' else 'personalized'
        return QueryPage(ArtworkPage(values,exhausted=True),actual,
            'ranking' if source=='ranking' else requested_sort if source=='search' else 'source_default',
            params.get('date') if source=='ranking' else None,'{"mode":"offline_demo"}')
    def close(self):pass


class DemoResponse:
    status_code=200
    def __init__(self):
        self.data=_png();self.headers={'Content-Type':'image/png','Content-Length':str(len(self.data))}
    def iter_content(self,chunk_size):yield self.data
    def close(self):pass


class DemoSession(DesktopSession):
    allow_authorization_updates = False

    def __init__(self,paths):
        super().__init__(paths,source_factory=lambda request,stop:DemoSource(),
            executor_factory=lambda repository,resolver:PageDeliveryExecutor(repository,resolver,
                get=lambda *args,**kwargs:DemoResponse()))
    def verify_account(self):
        self.identity={'account_id':'7','premium':True}
        future=Future();future.set_result(dict(self.identity));return future


def build_app(root,paths,*,demo=True):
    from gui.collection_view import CollectionView
    session=DemoSession(paths) if demo else DesktopSession(paths)
    reference={}
    def action(name,payload):
        if 'controller' in reference:reference['controller'].handle(name,payload)
    view=CollectionView(root,action);view.pack(fill='both',expand=True)
    controller=CollectionController(root,view,session);reference['controller']=controller
    root.protocol('WM_DELETE_WINDOW',controller.request_close)
    root.title('JPGPNG · 离线演示' if demo else 'JPGPNG')
    width=min(1280,max(760,root.winfo_screenwidth()-80))
    height=min(800,max(600,root.winfo_screenheight()-110))
    root.geometry(f'{width}x{height}');root.minsize(600,440)
    controller.start()
    return controller,view,session


def main(argv=None):
    parser=argparse.ArgumentParser(description='JPGPNG 原生界面候选，默认离线演示，不使用旧启动入口')
    parser.add_argument('--project-root',required=True,type=Path,help='项目资料根目录；演示请指定独立目录')
    parser.add_argument('--live',action='store_true',help='显式接真实授权和新库；验收前请仅在隔离目录使用')
    args=parser.parse_args(argv)
    paths=AppPaths(args.project_root)
    if not args.live and any((paths.root/name).exists() for name in ('pixiv_config.json','www.pixiv.net_cookies.txt')):
        parser.error('演示目录含已有授权，请选择独立目录，避免把模拟记录写入真实资料')
    root=tk.Tk()
    build_app(root,paths,demo=not args.live)
    root.mainloop()


if __name__=='__main__':main()
