"""JSON CLI for an AI operator. Visual judgement stays explicit and external."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3
import sys
import uuid

from core.library_paths import InstanceLock, AlreadyRunningError
from organizer.common import Budget, OrganizerError, now, read_json, safe_path
from organizer.store import Store
from organizer.web import BrowserConsole, WebClient
from organizer.snapshots import snapshot
from organizer.samples import sample, review, supplement
from organizer.plans import make_plan, apply_plan, reconcile, undo_plan, steps


def parser():
    result=argparse.ArgumentParser(description='Pixiv 账号画师整理：盘点、看图交接、分组计划和受控写回')
    result.add_argument('--project-root',type=Path,default=Path(__file__).resolve().parent.parent)
    result.add_argument('--browser-pid',type=int,default=0,help='多窗口时指定 browser 列出的 Chrome 进程')
    result.add_argument('--account',help='预期账号 ID；初始化后始终与保存身份核对')
    result.add_argument('--max-requests',type=int,default=80)
    result.add_argument('--max-mib',type=int,default=32)
    result.add_argument('--max-seconds',type=int,default=600)
    commands=result.add_subparsers(dest='command',required=True)
    commands.add_parser('browser',help='只列出已有 Pixiv 窗口，不登录或打开新浏览器')
    commands.add_parser('connect',help='核对已有页面账号与会员状态，绑定独立资料目录')
    commands.add_parser('status',help='本地进度，不联网')
    show=commands.add_parser('show',help='输出一项本地快照/样本/审阅/计划/作业')
    show.add_argument('kind',choices=('snapshot','sample','review','plan','job','operation','completion','conflict'))
    show.add_argument('id')
    listing=commands.add_parser('list',help='列出本地资料 ID，不联网')
    listing.add_argument('kind',choices=('snapshot','sample','review','plan','job','operation','completion','conflict'))
    snap=commands.add_parser('snapshot',help='保存公开与非公开关注盘点；限额后可续跑')
    snap.add_argument('--resume')
    samples=commands.add_parser('sample',help='跨作品 ID 范围取样，保存供 AI 实际查看的图片')
    samples.add_argument('--snapshot',required=True);samples.add_argument('--artist',required=True)
    samples.add_argument('--count',type=int,default=16);samples.add_argument('--resume')
    extra=commands.add_parser('sample-page',help='按需要补看已取样作品的其他图片页')
    extra.add_argument('--sample',required=True);extra.add_argument('--work',required=True);extra.add_argument('--page',type=int,required=True)
    reviews=commands.add_parser('review',help='导入实际看图后的结构化判断，不自动生成视觉结论')
    reviews.add_argument('--file',type=Path,required=True);reviews.add_argument('--attest-viewed',action='store_true')
    plan=commands.add_parser('plan',help='读取网站现状，生成不可变改动清单')
    plan.add_argument('--review',action='append',required=True);plan.add_argument('--replace',action='store_true',help='目标组替换原组；默认仅叠加')
    apply=commands.add_parser('apply',help='默认只展示计划；--execute 才写入网站')
    apply.add_argument('--plan',required=True);apply.add_argument('--execute',action='store_true')
    apply.add_argument('--max-artists',type=int,default=10);apply.add_argument('--max-writes',type=int,default=20)
    check=commands.add_parser('reconcile',help='仅通过读取确认中断写入的实际结果')
    check.add_argument('--plan',required=True)
    undo=commands.add_parser('undo-plan',help='核对网站现状后生成补偿计划，不直接撤销')
    undo.add_argument('--plan',required=True)
    export=commands.add_parser('export',help='导出本地资料；已有目标不覆盖')
    export.add_argument('kind',choices=('snapshot','sample','review','plan','job'))
    export.add_argument('id');export.add_argument('--output',type=Path,required=True)
    return result


def emit(value):
    print(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))


def run(args):
    root=args.project_root.absolute()
    data=safe_path(root,'organizer_data')
    database=safe_path(data,'organizer.sqlite3')
    if args.command=='browser':
        return {'windows':BrowserConsole(args.browser_pid).windows()}
    if args.command=='status' and not database.exists():
        return {'initialized':False,'network_requests':0,'next':'connect'}
    network=args.command in ('connect','snapshot','sample','sample-page','plan','reconcile','undo-plan') or (args.command=='apply' and args.execute)
    if not database.exists() and args.command!='connect':
        raise OrganizerError('connect_first')
    with ExitStack() as stack:
        # Existing downloader's process lock prevents simultaneous account request streams.
        if network:
            stack.enter_context(InstanceLock(safe_path(root,'download_logs/.writer.lock')))
        stack.enter_context(InstanceLock(safe_path(data,'.writer.lock')))
        store=stack.enter_context(Store(database))
        if args.command=='status':
            return {'initialized':True,'counts':store.counts(),'last_jobs':store.list('job',5),
                    'network_requests':0,'data_directory':'./organizer_data'}
        if args.command=='show':
            return store.get(args.kind,args.id)
        if args.command=='list':
            return {'items':[{'id':v['id'],'status':v['data'].get('state',v['data'].get('status')),'created_at':v['data'].get('created_at')} for v in store.list(args.kind)]}
        if args.command=='export':
            value=store.get(args.kind,args.id)
            output=args.output if args.output.is_absolute() else root/args.output
            # Explicit exports may be outside the project, but never through a link or onto an existing file.
            safe_path(output.parent,output.name)
            with output.open('x',encoding='utf-8',newline='\n') as stream:
                stream.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
            return {'exported':str(output)}
        if args.command=='review':
            source=args.file if args.file.is_absolute() else root/args.file
            return review(store,data,read_json(source),attest_viewed=args.attest_viewed)
        if args.command=='apply' and not args.execute:
            plan=store.get('plan',args.plan)
            return {'preview_only':True,'plan':plan,'operations':steps(plan),'website_writes':0}
        binding=None
        try:
            binding=store.get('meta','account')['account_id']
        except KeyError:
            if args.command!='connect':
                raise OrganizerError('connect_first') from None
        if binding and args.account and binding!=args.account:
            raise OrganizerError('account_mismatch')
        budget=Budget(args.max_requests,args.max_mib*1024*1024,args.max_seconds)
        web=WebClient(BrowserConsole(args.browser_pid,data/'.bridge'),budget,binding or args.account)
        job={'id':uuid.uuid4().hex,'command':args.command,'created_at':now(),'status':'running','budget':budget.summary()}
        store.put('job',job['id'],job,create_only=True)
        try:
            identity=web.connect()
            if args.command=='connect':
                if not binding:
                    store.put('meta','account',identity,create_only=True)
                result={'connected':True,**identity}
            elif args.command=='snapshot':
                result=snapshot(store,web,args.resume)
                result={'snapshot_id':result['id'],'status':result['status'],'artists':len(result['artists']),
                        'totals':{s:a['total'] for s,a in result['anchors'].items()},'non_atomic':True}
            elif args.command=='sample':
                result=sample(store,web,data,args.snapshot,args.artist,args.count,args.resume)
            elif args.command=='sample-page':
                result=supplement(store,web,data,args.sample,args.work,args.page)
            elif args.command=='plan':
                result=make_plan(store,web,args.review,replace=args.replace)
            elif args.command=='apply':
                result=apply_plan(store,web,args.plan,max_artists=args.max_artists,max_writes=args.max_writes)
            elif args.command=='reconcile':
                result=reconcile(store,web,args.plan)
            elif args.command=='undo-plan':
                result=undo_plan(store,web,args.plan)
            else:
                raise OrganizerError('unsupported_command')
            job.update(status='completed',account_id=web.account_id)
            return {'job_id':job['id'],'result':result,'budget':budget.summary()}
        except BaseException as exc:
            job.update(status='stopped',code=exc.code if isinstance(exc,OrganizerError) else 'interrupted_or_internal_error')
            raise
        finally:
            job.update(finished_at=now(),budget=budget.summary())
            store.put('job',job['id'],job)


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        emit(run(args));return 0
    except OrganizerError as exc:
        emit({'ok':False,'code':exc.code,'next':'status / show 查看保存进度；未知写入先 reconcile'});return 2
    except AlreadyRunningError:
        emit({'ok':False,'code':'close_downloader_or_other_organizer_first'});return 3
    except KeyboardInterrupt:
        emit({'ok':False,'code':'interrupted_progress_saved'});return 130
    except (KeyError,ValueError,OSError,sqlite3.Error):
        emit({'ok':False,'code':'local_input_or_storage_error'});return 2
    except Exception:
        # Browser/HTTP exceptions can contain credential-bearing request objects.
        emit({'ok':False,'code':'internal_error_progress_saved'});return 1
