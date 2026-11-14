"""One Tk poll owner; commands are Futures and late query versions are ignored."""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime

from core.collection_scope import scope_fingerprint
from core.collection_settings import CollectionSettings, SettingsError
from core.library_queries import LibraryQuery
from core.log_view import LogView, ViewDelta
from core.task_contracts import PathRef
from gui.collection_dialogs import NativeDialogs
from gui.verification_panel import describe_result, reason_text, row_key, summary_text

_PHASE = {'queued':'等待开始','running':'收集中','cooling':'等待冷却','pausing':'正在暂停',
    'cancelling':'正在取消','settling':'正在收尾','paused':'已暂停','blocked':'需要处理',
    'partial':'部分完成','completed':'已完成','cancelled':'已取消'}
_RESULT = {'completed':'已完成','partial':'部分完成','failed':'失败','pending':'待处理',
    'needs_verification':'待核验','filtered':'条件过滤','skipped_existing':'已有跳过',
    'unavailable':'作品不可用','unsupported':'暂不支持','metadata_unknown':'信息待确认','cancelled':'已取消'}
_ERROR = {'output_relative':'保存目录须为项目内相对路径，且不能使用程序或记录目录',
    'workers':'下载并发请填写 1–8 的整数','min_bookmarks':'最低收藏请填写非负整数',
    'artist_ids':'请填写画师 ID 或完整的 Pixiv 画师链接','start_artist_id':'起始画师请填写数字 ID',
    'start_ordinal':'起始序号请填写正整数','starting_point_conflict':'指定画师、起始画师和起始序号只能选一种',
    'authorization_missing':'项目中没有可用的已有授权，请按项目授权说明补充',
    'authorization_failed':'已有授权校验未通过，请检查网络和授权文件',
    'authorization_account_mismatch':'当前授权账号与任务账号不同，任务已停止',
    'metadata_access_denied':'接口暂时拒绝访问，请检查账号或稍后再试',
    'metadata_rate_limited':'请求受到限流，请等待冷却',
    'image_access_denied':'图片服务器拒绝访问，请检查当前网络后手动继续',
    'source_runner_failed':'来源暂时无法继续，请核对账号和网络后再操作'}
_ERROR.update({'file_access_failed':'无法访问保存目录；请检查目录权限，处理后可继续',
    'disk_full':'保存位置的可用空间不足；清出空间后可继续',
    'output_file_conflict':'目标位置已有无法确认归属的文件；请检查后再继续'})
# Result buckets for one verification pass (contract v5 sec.3).  "verified"
# and "repair_queued" are kept apart because a queued repair is not a verified
# recovery, and an unrecognised outcome is its own bucket, never a success.
_VERIFY_BUCKETS = ('verified', 'repair_queued', 'held', 'refused', 'unknown', 'error')
_VERIFY_BUCKET_BY_OUTCOME = {
    'reused': 'verified', 'completed': 'verified', 'repair_queued': 'repair_queued',
    'held': 'held', 'refused': 'refused'}
_VERIFY_BUCKET_TEXT = {'verified': '已复用', 'repair_queued': '已排补下',
    'held': '保留待定', 'refused': '已拒绝', 'unknown': '无法识别', 'error': '失败'}
_TONE_RANK = {'muted': 0, 'success': 0, 'warning': 1, 'danger': 2}
_ERROR.update({
    'api_interval':'查询间隔请填写 0–60 秒，可填小数。',
    'image_interval':'图片请求间隔请填写 0–60 秒，可填小数。',
    'authorization_format':'授权内容格式不正确，请粘贴完整的 Refresh Token。',
    'authorization_config_invalid':'现有授权文件格式不正确，已保留原文件，请先修复后再更新。',
    'authorization_config_changed':'授权文件刚被其他程序修改，请重新操作。',
    'authorization_task_active':'请先暂停当前任务，待保存完成后再更新授权。',
    'demo_authorization_disabled':'离线演示不保存账号授权。',
    'name_template':'命名规则无效，请使用说明中的字段，不要填写目录或扩展名。',
    'search_word':'请填写搜索关键词。',
    'search_options':'请选择有效的匹配方式和排序。',
    'ranking_mode':'请选择榜单类型。',
    'related_illust_id':'请填写用于查找相似作品的作品 ID 或 Pixiv 作品链接。',
    'intersection_sources':'请至少勾选两个来源；结果取它们已查询作品的交集。',
    'per_source_limit':'每来源候选数量请填写 1–5000 的整数。',
    'invalid_query_parameters':'来源条件不完整或格式有误。请检查关键词、画师 ID 和日期；日榜范围最多 31 天。',
    'metadata_unknown':'作品信息仍不完整，已保留为待核验，可在下载名单中查看。',
    'source_request_budget':'本批查询预算已用完，断点已保存，可继续。',
    'work_budget':'本批作品名额已用完，可继续下一批。',
    'source_time_budget':'本批查询时间已用完，断点已保存。',
    'source_limit_reached':'已处理每个来源限定数量内的交集；其余范围尚未查询。',
    'no_intersection_matches':'已查询范围内没有共同作品，可调整条件或扩大候选数量。',
    'coverage_unconfirmed':'来源未能确认完整覆盖，已有记录已保存。',
    'output_relative':'请选择图片保存文件夹，不能使用程序、配置或下载记录目录。',
    'location_invalid':'保存位置无效，请重新选择一个本地文件夹。',
    'location_not_directory':'所选文件夹不可用，请确认磁盘已连接后重新选择。',
    'location_overlap':'此位置包含已有的保存根目录。请选择原来的文件夹、它的子目录或另一个位置。',
    'location_mapping_invalid':'外部保存位置的配置无法读取，请检查位置配置文件。',
    'location_limit':'保存位置数量已达到上限，请先使用已选的位置。',
    'location_changed':'保存位置在操作期间发生变化，请重新选择。',
    'export_extension':'下载名单请使用 .md，日志请使用 .txt 或 .log。',
    'export_protected':'请将导出文件保存在其他位置，避免覆盖程序、配置或记录。',
    'export_location':'导出文件夹不可用，请重新选择保存位置。',
    'artist_file':'请选择包含画师 ID 或画师链接的 .txt 文本文件。',
    'artist_file_size':'导入文本超过 128 KB，请保留本次需要的画师。',
    'artist_file_encoding':'请将画师名单保存为 UTF-8 文本后重新导入。',
    'artist_file_empty':'文件里没有画师 ID。每行可放一个 ID 或 Pixiv 画师链接。',
})


def _display_time(value):
    try:return datetime.fromisoformat(value).astimezone().strftime('%m-%d %H:%M')
    except (TypeError,ValueError):return '—'


# One verification list request and one recovery pass stay small on purpose:
# the list is a bounded read, and a pass is a serial chain of single-page
# commands so the single I/O owner is never flooded (its queue holds four).
_VERIFY_PAGE_SIZE = 50
_VERIFY_BATCH_LIMIT = 20


def _scope_description(query):
    if query.get('source','follow')!='follow':
        from core.source_settings import source_description
        return source_description(query)
    import json
    try:parameters=json.loads(query.get('parameters_json','{}'))
    except (TypeError,ValueError):parameters={}
    parts=['关注新作' if query.get('mode')=='new' else '关注历史补全']
    if parameters.get('artist_ids'):parts.append(f"指定 {len(parameters['artist_ids'])} 位画师")
    elif parameters.get('start_ordinal'):parts.append(f"从第 {parameters['start_ordinal']} 位画师开始")
    elif parameters.get('start_artist_id'):parts.append(f"从画师 {parameters['start_artist_id']} 开始")
    else:parts.append('全部关注画师')
    filters=query.get('filters') or {}
    if filters.get('exclude_ai'):parts.append('排除 AI')
    if filters.get('min_bookmarks') is not None:parts.append(f"收藏 ≥ {filters['min_bookmarks']}")
    return ' · '.join(parts)


class CollectionController:
    def __init__(self, root, view, session, *, clock=time.monotonic, dialogs=None):
        self.root, self.view, self.session, self.clock = root, view, session, clock
        self.dialogs = dialogs if dialogs is not None else NativeDialogs(root)
        self.runtime = self.log_view = None
        self._ui_thread = threading.get_ident()
        self._pending = OrderedDict()
        self._after = None
        self._closed = self._closing = False
        self._close_thread = None
        self._close_result = None
        self._next_overview = 0.0
        self._last_revision = None
        self._overview_value = None
        self._snapshot_value = None
        self._task_rows = {}; self._record_rows = {}
        self._requests = OrderedDict()
        self._current_job = self._current_batch = None
        self._selected_record = None
        self._overview_cursor = None; self._task_cursors = [None]
        self._record_cursors = [None]; self._record_next = None
        self._record_query = None; self._record_version = 0
        self._log_open = self._log_paused = False
        self._page = 'follow'
        self._busy = set()
        # Verification page (W5). _verify_rows is the action table, keyed by
        # the frozen page identity; _verify_display is what the panel shows.
        self._verify_rows = {}
        self._verify_display = []
        self._verify_summary = {}
        self._verify_truncated = False
        self._verify_queue = []
        self._verify_total = 0
        self._verify_done = 0
        self._verify_skipped = 0
        self._verify_busy = False
        # Scope identity (contract v5 sec.3): every verification read captures
        # the selection it was issued for.  A result that arrives for an older
        # epoch or another batch is dropped instead of being shown under the
        # new selection, and a running pass keeps the batch it started with.
        self._verification_epoch = 0
        self._verification_batch = None
        self._verify_pass_epoch = None
        self._verify_pass_batch = None
        self._verify_stale_remaining = 0
        self._verify_counts = {}
        self._verify_last_reason = None
        self._verify_worst = None
        self._verify_dropped = 0

    def _assert_ui(self):
        if threading.get_ident() != self._ui_thread:
            raise RuntimeError('Desktop controller must run on its Tk owner')

    def start(self):
        self._assert_ui()
        self._watch('startup', self.session.startup(), self._ready)
        self._schedule(0)

    def _watch(self, key, future, success, *, version=None):
        old = self._pending.pop(key, None)
        if old is not None:
            old[0].cancel()
        if len(self._pending) >= 32:
            future.cancel(); self.view.set_status('操作较多，请等待当前操作完成', 'warning')
            return
        self._pending[key] = (future, success, version)

    def _ready(self, result):
        self.runtime = self.session.runtime
        self.log_view = LogView(self.session.log)
        self.view.set_follow_form(result['settings']); self.view.set_settings_form(result['settings'])
        self.view.set_account('沿用已有授权 · 开始时校验')
        self.view.configure_action('update_authorization', enabled=getattr(self.session,'allow_authorization_updates',True))
        self.view.set_status('准备就绪，历史补全为默认模式')
        self.view.set_summary({'title':'开始收集关注画师的历史作品',
            'description':'按原图保存全部图片页；可选条件独立生效。',
            'counts':'相同方案会沿断点继续；调整条件后会新建任务。'})
        self._refresh_overview()

    def _schedule(self, delay=100):
        if not self._closed:
            self._after = self.root.after(delay, self._poll)

    def _poll(self):
        self._assert_ui(); self._after = None
        if self._closed:
            return
        before = self.clock()
        for key, item in tuple(self._pending.items()):
            if self.clock()-before >= .008:
                break
            future, callback, version = item
            if not future.done():
                continue
            self._pending.pop(key, None)
            if version is not None and version != self._record_version:
                continue
            try:
                value = future.result()
                if not self._closing:
                    callback(value)
            except Exception as error:
                if not self._closing:
                    verify = str(key).startswith('verify:')
                    quiet = key not in {'overview','snapshot','artists','request','records'}
                    self._show_error(error, notify=quiet and not verify)
                    if verify:
                        self._verification_failed(error)
                if key in self._busy:
                    self._busy.discard(key); self.view.configure_action(key, enabled=True)
                if key in {'verify_account','start_preferences','source_preferences'}:
                    for action in ('start','start_source'):
                        self._busy.discard(action); self.view.configure_action(action,enabled=True)
        if self._closing:
            self._poll_close()
        elif self.runtime is not None:
            now = self.clock()
            if now >= self._next_overview:
                active=self._task_rows.get(self._current_job,{}).get("phase") in {"running","cooling","pausing","cancelling","settling","queued"}
                self._next_overview = now+(1 if active else 3)
                self._refresh_overview()
            if self._log_open and self.log_view is not None and not self._log_paused:
                self.log_view.set_follow(self.view.log_at_bottom())
                delta = self.log_view.drain()
                self.view.apply_log_delta(delta)
                state = self.session.log.snapshot()
                missing = f" · 文件诊断略过 {state['file_dropped']} 条" if state['file_dropped'] else ''
                self.view.set_log_notice(f'显示最近 {self.log_view.line_count} 行{missing}')
        self._schedule(100 if self._pending or self._log_open or self._closing else 250)

    def _show_error(self, error, *, notify=False):
        code = getattr(error, 'code', None)
        if isinstance(error, SettingsError):
            code = str(error)
        text = _ERROR.get(code, '操作未完成，请检查当前条件、网络或诊断状态')
        if code in {'max_artists','max_requests','max_works'}:
            text = '每批预算请填写正整数'
        if isinstance(error, PermissionError):
            text = '没有权限访问所选位置，请选择可写的文件夹后重试。'
        elif isinstance(error, FileNotFoundError):
            text = '所选文件或文件夹已不存在，请重新选择。'
        self.view.set_status(text, 'error')
        if notify:
            self.dialogs.error(text)

    def _refresh_overview(self):
        if self.runtime is None or 'overview' in self._pending:
            return
        self._watch('overview', self.runtime.overview(after=self._task_cursors[-1]), self._overview)
        if self._current_job is not None and 'snapshot' not in self._pending:
            self._watch('snapshot', self.runtime.snapshot(self._current_job), self._snapshot)
        if self._page=='follow' and self.session.identity and 'artists' not in self._pending:
            self._watch('artists',self.runtime.artists('pixiv:'+self.session.identity['account_id']),
                lambda values:self.view.set_artists([{'id':row['artist_id'],'name':row['display_name'],
                    'state':'已关注' if row['is_following'] else '已取消关注',
                    'counts':'查看本批作品'} for row in values]))

    def _overview(self, value):
        if self._overview_value == value:
            return
        self._overview_value = value
        revision_changed = self._last_revision != value['revision']
        self._last_revision=value['revision']
        self._overview_cursor = value['next_cursor']
        self._task_rows = {row['job_id']: row for row in value['rows']}
        rows = []
        for row in value['rows']:
            mode = row['query']['mode']
            from core.source_settings import SOURCES
            source=row['query'].get('source','follow')
            title=('关注历史补全' if mode=='history' else '关注新作') if source=='follow' else _scope_description(row['query'])
            results=row.get('results')
            counts=f"{results['works'].get('completed',0)} 个作品 · {results['pages'].get('completed',0)} 张" if results is not None else '查看本批名单'
            rows.append({'id':row['job_id'],'title':title,
                'source':'关注' if source=='follow' else SOURCES.get(source,'来源'),
                'state':'范围已结束' if row.get('stop_reason')=='source_limit_reached' and row['phase'] in {'paused','partial'} else _PHASE.get(row['phase'], '等待'),
                'counts':counts,'counts_authoritative':results is not None,'reason':_ERROR.get(row.get('stop_reason')) or '创建于 '+_display_time(row.get('created_at'))})
        self.view.set_tasks(rows)
        if self._current_job and hasattr(self.view,'select_task'):
            self.view.select_task(self._current_job)
        if hasattr(self.view,'set_task_info'):
            self.view.set_task_info(f'第 {len(self._task_cursors)} 页 · {len(rows)} 项'+(' · 还有下一页' if value['next_cursor'] else ''))
            self.view.configure_action('task_next',enabled=bool(value['next_cursor']))
            self.view.configure_action('task_previous',enabled=len(self._task_cursors)>1)
        if self._current_job is None and value['rows']:
            self._select_task(value['rows'][0]['job_id'])
        if revision_changed and self._page=='records' and 'records' not in self._pending:
            if self._record_query is None:
                self._query_records(reset=True)
            else:
                self._watch('records',self.runtime.fetch(self._record_query,cursor=self._record_cursors[-1]),
                    self._records,version=self._record_version)

    def _select_task(self, job_id):
        if job_id not in self._task_rows:
            return
        row = self._task_rows[job_id]
        changed = self._current_job != job_id or self._current_batch != row['batch_id']
        self._current_job, self._current_batch = job_id, row['batch_id']
        if hasattr(self.view,'select_task'):self.view.select_task(job_id)
        if changed:
            self._invalidate_verification()
            self._snapshot_value=None
            for action in ('pause','resume','retry_failed','cancel','results'):
                self.view.configure_action(action,enabled=False)
            self._record_version += 1; self._record_cursors = [None]
            self._record_query=None; self._record_next=None
        self._refresh_output_label()
        self._watch('request', self.runtime.request(job_id), lambda request:self._remember_request(job_id,request))
        self._watch('snapshot', self.runtime.snapshot(job_id), self._snapshot)

    def _remember_request(self, job_id, request):
        if job_id != self._current_job:
            return
        self._requests[job_id] = request
        self._refresh_output_label()
        while len(self._requests)>100:
            self._requests.popitem(last=False)

    def _snapshot(self, value):
        if value['job_id'] != self._current_job:
            return
        if self._snapshot_value == value:
            return
        self._snapshot_value = value
        if value['batch_id'] != self._current_batch:
            # The same job can open a new batch (resume after a pause); the old
            # verification read belongs to the old batch and must not survive.
            self._invalidate_verification()
        self._current_batch = value['batch_id']
        result = value.get('results') or {}
        pages = result.get('pages', {})
        works = result.get('works', {})
        text = f"已保存 {pages.get('completed',0)} 页 · 已有 {pages.get('existing',0)} 页 · 待处理 {pages.get('pending',0)} 页"
        phase = value['phase']
        limited_scope = value.get('stop_reason') == 'source_limit_reached' and phase in {'paused','partial','completed'}
        phase_text='范围已结束' if limited_scope else _PHASE.get(phase,'等待')
        titles={'running':'正在收集喜欢的作品','paused':'从上次停下的地方继续','completed':'本批作品已收集完成','partial':'已有成果保存，继续补齐缺页','blocked':'进度已保存，需要处理后继续','cooling':'正在等待下一次请求','queued':'准备开始收集'}
        self.view.set_summary({'title':'本次范围内的筛选已结束' if limited_scope else titles.get(phase,_PHASE.get(phase,'当前任务')),'description':text,
            'counts':'可查看本批名单；扩大候选数量后重新开始。' if limited_scope else '相同方案会沿断点继续；调整条件后会新建任务。',
            'stat_works':works.get('completed',0),'stat_pages':pages.get('completed',0),
            'stat_pending':sum(works.get(key,0) for key in ('pending','partial','failed','needs_verification','metadata_unknown')),
            'resume_state':phase_text,'resume_position':_scope_description(self._task_rows.get(value['job_id'],{}).get('query',{}))})
        if hasattr(self.view,'set_task_progress'):
            self.view.set_task_progress(value['job_id'],f"{works.get('completed',0)} 个作品 · {pages.get('completed',0)} 张")
        scope=_scope_description(self._task_rows.get(value['job_id'],{}).get('query',{}))
        detail=f"{scope}\n{phase_text} · {text}\n多页作品全部保存后，才计为作品完成。"
        reason=_ERROR.get(value.get('stop_reason'))
        if reason:detail+='\n'+reason
        self.view.set_task_detail(value['job_id'],detail)
        if getattr(self,'_status_phase',None)!=(value['job_id'],phase):
            self._status_phase=(value['job_id'],phase)
            notices={'completed':'本批作品已收集完成，可查看名单或导出记录。',
                'paused':'进度已保存，随时可以继续。','partial':'已有成果保存，仍有未完整的作品。',
                'blocked':reason or '进度已保存，请查看任务详情后继续。',
                'running':'正在收集作品，完成结果会持续保存。',
                'cooling':'正在等待冷却，进度已保存。','cancelled':'任务已取消，已保存的图片保留。'}
            if phase in notices:
                self.view.set_status(reason if limited_scope else notices[phase],'success' if phase=='completed' else 'warning' if phase in {'partial','blocked'} else 'muted')
        for action, enabled in {'pause':phase in {'running','cooling'},
                'resume':phase in {'paused','partial','blocked'} and not limited_scope, 'retry_failed':phase in {'partial','blocked','paused'},
                'cancel':phase in {'queued','running','cooling','paused','partial','blocked'},'results':bool(self._current_batch)}.items():
            if action not in self._busy:
                self.view.configure_action(action,enabled=enabled)

    def _mutation(self, action, future, success):
        self._busy.add(action); self.view.configure_action(action,enabled=False)
        def completed(value):
            self._busy.discard(action); self.view.configure_action(action,enabled=True)
            success(value); self._next_overview=0
        self._watch(action,future,completed)

    def _verify_then(self, action):
        if self.session.identity is not None:
            action(); return
        self.view.set_status('正在校验已有授权…')
        self._mutation('verify_account',self.session.verify_account(),lambda value:self._verified(value,action))

    def _verified(self, value, action=None):
        self.view.set_account(f"账号 {value['account_id']} · {'会员' if value['premium'] else '已授权'}")
        if action is None:
            self.view.set_status('已有授权校验通过')
        else:
            action()

    def _update_authorization(self):
        if self._busy.intersection({'start','start_source','verify_account'}):
            self.view.set_status('请等待当前账号操作完成后再更新授权。','warning'); return
        credential = self.dialogs.authorization()
        if credential is None or self._closing:
            return
        def saved(_):
            self.view.set_account('授权已更新 · 待校验')
            self.view.set_status('授权已保存，可点击“检查账号状态”。','success')
        self._mutation('update_authorization',self.session.update_authorization(credential),saved)

    def _start(self, form, *, refresh_following=False, from_head=False):
        if self._busy.intersection({'start','start_source'}):
            return
        settings = CollectionSettings.from_form(form,previous=self.session.settings)
        # Validate fields before making the explicit authorization request.
        settings.request('draft','offline-account',form)
        self._busy.add('start'); self.view.configure_action('start',enabled=False)
        def submit():
            account = 'pixiv:'+self.session.identity['account_id']
            request = settings.request(uuid.uuid4().hex,account,form)
            old = self._requests.get(self._current_job)
            row = self._task_rows.get(self._current_job,{})
            if (old is not None and not refresh_following and not from_head
                    and row.get('phase') in {'paused','partial','blocked'}
                    and scope_fingerprint(old)==scope_fingerprint(request)):
                future = self.runtime.resume(self._current_job,uuid.uuid4().hex,budget=request.budget)
            else:
                future = self.runtime.submit(request,uuid.uuid4().hex,
                    refresh_following=refresh_following,from_head=from_head)
            self._mutation('start',future,self._submitted)
        self.view.set_status('正在保存本次收集方案…')
        def saved(values):
            self.view.set_settings_form(values)
            self._verify_then(submit)
        self._watch('start_preferences',self.session.save(settings.form()),saved)

    def _settings_saved(self,values):
        self.view.set_follow_form({key:values[key] for key in
            ('output_relative','output_kind','output_root_id','output_display','folder_by_artist') if key in values})
        self._refresh_output_label()
        self.view.set_status('设置已保存；并发数下次打开时生效')

    def _start_source(self,form):
        from core.source_settings import source_request
        if self._busy.intersection({'start','start_source'}):return
        settings=CollectionSettings.from_form(form,previous=self.session.settings)
        source_request(form,settings,'draft','offline-account')
        self._busy.add('start_source');self.view.configure_action('start_source',enabled=False)
        def submit():
            request=source_request(form,settings,uuid.uuid4().hex,'pixiv:'+self.session.identity['account_id'])
            old=self._requests.get(self._current_job);row=self._task_rows.get(self._current_job,{})
            if old is not None and row.get('phase') in {'paused','partial','blocked'} and row.get('stop_reason')!='source_limit_reached' and scope_fingerprint(old)==scope_fingerprint(request):
                future=self.runtime.resume(self._current_job,uuid.uuid4().hex,budget=request.budget)
            else:future=self.runtime.submit(request,uuid.uuid4().hex)
            def submitted(value):
                self._submitted(value)
                self.view.show_page('tasks');self._page='tasks'
            self._mutation('start_source',future,submitted)
        def saved(values):
            self.view.set_settings_form(values);self._verify_then(submit)
        self.view.set_status('正在保存本次来源与筛选方案…')
        self._watch('source_preferences',self.session.save(settings.form()),saved)

    def _selected_output(self, scope=None):
        scope = scope or self._page
        if scope in {'follow', 'sources', 'settings'}:
            form = self.view.get_settings_form() if scope == 'settings' else self.view.get_follow_form()
            return PathRef(kind=form.get('output_kind','project'), root_id=form.get('output_root_id','project'),
                           relative_path=form.get('output_relative','downl_Pixiv'))
        request = self._requests.get(self._current_job)
        return request.output.root_ref if request else None if self._current_job else self.session.settings.output_ref

    def _refresh_output_label(self):
        if not hasattr(self.session, 'resolver'):
            return
        try:
            ref = self._selected_output()
            self.view.set_output_label('保存至 ' + self.session.resolver.display(ref) if ref else '正在读取任务保存位置…')
        except ValueError:
            pass

    def _choose_directory(self, scope):
        form = self.view.get_settings_form() if scope == 'settings' else self.view.get_follow_form()
        def choose(initialdir):
            selected = self.dialogs.choose_directory(initialdir)
            if not selected or self._closing:
                return
            def chosen(values):
                setter = self.view.set_settings_form if scope == 'settings' else self.view.set_follow_form
                setter(values)
                self._refresh_output_label()
                self.view.set_status('已选择保存位置。点击“保存设置”后生效。' if scope == 'settings' else
                    '已选择保存位置，下次开始时使用；当前任务保持原目录。')
            self._mutation('browse_output', self.session.select_directory(selected), chosen)
        self._mutation('browse_output', self.session.dialog_directory(form), choose)

    def _choose_export(self, kind):
        action = 'export_records' if kind == 'records' else 'export_log'
        query = self._record_query
        if kind == 'records' and query is None:
            self.dialogs.info('还没有下载名单', '请先打开本批名单或查询累计下载记录。')
            return
        def choose(initialdir):
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = ('下载名单_' + stamp + '.md') if kind == 'records' else ('会话日志_' + stamp + '.txt')
            selected = self.dialogs.choose_export(kind, initialdir, filename)
            if not selected or self._closing:
                return
            def prepared(target):
                future = (self.runtime.export_markdown(query, target.ref, target.resolver) if kind == 'records'
                          else self.runtime.export_log(target.ref, target.resolver))
                def saved(result):
                    text = (f'已导出 {result.row_count} 行下载记录。' if kind == 'records' else '已导出当前保留的会话日志。')
                    self.view.set_status(text + ' ' + target.display_path, 'success')
                    self.dialogs.info('导出完成', text + '\n\n' + target.display_path)
                self._mutation(action, future, saved)
            self._mutation(action, self.session.export_target(selected, kind), prepared)
        self._mutation(action, self.session.dialog_directory({'output_relative':'exports'}), choose)

    def _import_artists(self):
        def choose(initialdir):
            selected = self.dialogs.choose_artists(initialdir)
            if not selected or self._closing:
                return
            def imported(ids):
                import json
                form = self.view.get_follow_form()
                old = CollectionSettings().request('import', 'offline', {'artist_ids':form.get('artist_ids','')})
                existing = json.loads(old.query.parameters_json).get('artist_ids', [])
                combined = list(dict.fromkeys((*existing, *ids)))
                if len(combined) > 200:
                    raise SettingsError('artist_ids')
                self.view.set_follow_form({'mode':'history','artist_ids':', '.join(combined),
                                           'start_artist_id':'','start_ordinal':''})
                text = f'已合并 {len(combined)} 位画师，重复 ID 已跳过。'
                self.view.set_status(text, 'success')
                self.dialogs.info('导入完成', text + '\n可检查名单后再开始下载。')
            self._mutation('import_artists', self.session.import_artist_ids(selected), imported)
        self._mutation('import_artists', self.session.dialog_directory({}), choose)

    def _submitted(self, value):
        old=self._pending.pop('snapshot',None)
        if old is not None:old[0].cancel()
        self._invalidate_verification()
        self._current_job, self._current_batch = value['job_id'], value['batch_id']
        self._record_version += 1
        self._record_query=None; self._record_next=None; self._record_cursors=[None]
        self._watch('dispatch',self.runtime.run_queued(),lambda _:self.view.set_status('任务已开始，结果将持续保存'))
        self._watch('request',self.runtime.request(self._current_job),lambda request:self._remember_request(value['job_id'],request))

    def _query_records(self, *, reset=False):
        if reset:
            self._record_version += 1; self._record_cursors=[None]
        form = self.view.get_record_form()
        if form['scope']=='batch' and not self._current_batch:
            self.view.set_records([],page_text='还没有本批名单'); return
        result = form.get('result','all')
        if form['scope']=='history':
            if result not in {'all','completed'}:
                self.view.set_records([],page_text='累计历史只包含已完成图片页'); return
            query=LibraryQuery(kind='history',text=form.get('search',''))
        else:
            query=LibraryQuery(batch_id=self._current_batch,text=form.get('search',''),
                states=() if result=='all' else (result,))
        self._record_query=query
        self._watch('records',self.runtime.fetch(query,cursor=self._record_cursors[-1]),
            self._records,version=self._record_version)

    def _records(self, page):
        self._record_next=page.next_cursor; rows=[]; self._record_rows={}
        for row in page.rows:
            identity=row.get('artifact_id') or row['illust_id']
            self._record_rows[identity]=row
            required=row.get('required',1); completed=row.get('completed',1)
            rows.append({'id':identity,'title':row.get('title') or row['illust_id'],
                'artist':row.get('artist_name') or row.get('artist_id') or '未知',
                'pages':f'{completed}/{required}' if row.get('kind')=='batch' else f"p{row['page_index']}",
                'result':_RESULT.get(row.get('state','completed'),'待处理'),
                'time':_display_time(row.get('completed_at') or row.get('created_at'))})
        self.view.set_records(rows,page_text=f'第 {len(self._record_cursors)} 页 · {len(rows)} 行'+(' · 还有下一页' if page.next_cursor else ''))
        self.view.configure_action('record_next',enabled=bool(page.next_cursor))
        self.view.configure_action('record_previous',enabled=len(self._record_cursors)>1)

    def handle(self, action, payload=None):
        self._assert_ui(); payload=payload or {}
        if self._closed or self._closing:
            return
        if action in self._busy:
            return
        if 'update_authorization' in self._busy and action in {'start','start_source','verify_account'}:
            self.view.set_status('正在保存授权，请稍候。'); return
        if action=='navigate':
            self._page=payload['page']
            self._refresh_output_label()
            if self._page=='records' and self.runtime:self._query_records(reset=True)
            elif self._page=='verify' and self.runtime:self._refresh_verification()
            return
        if self.runtime is None:
            self.view.set_status('正在准备，请稍候'); return
        try:
            if action in {'start','sync_following','restart_scope'}:
                self._start(dict(self.view.get_follow_form()),refresh_following=action=='sync_following',from_head=action=='restart_scope')
            elif action=='start_source':
                self._start_source(dict(self.view.get_source_form()))
            elif action=='verify_account':
                self._mutation(action,self.session.verify_account(),self._verified)
            elif action=='update_authorization':
                self._update_authorization()
            elif action=='save_settings':
                self._mutation(action,self.session.save(self.view.get_settings_form()),
                    self._settings_saved)
            elif action=='browse_output':
                self._choose_directory(payload.get('scope', 'follow'))
            elif action=='import_artists':
                self._import_artists()
            elif action=='select_task':self._select_task(payload.get('id'))
            elif action=='task_next' and self._overview_cursor:
                self._overview_value=None
                self._task_cursors.append(self._overview_cursor)
                old=self._pending.pop('overview',None)
                if old is not None:old[0].cancel()
                self._refresh_overview()
            elif action=='task_previous' and len(self._task_cursors)>1:
                self._overview_value=None
                self._task_cursors.pop()
                old=self._pending.pop('overview',None)
                if old is not None:old[0].cancel()
                self._refresh_overview()
            elif action in {'pause','cancel'} and self._current_job:
                job_id = self._current_job
                if action == 'cancel' and not self.dialogs.confirm_cancel('取消选中的下载任务'):
                    return
                self._mutation(action,getattr(self.runtime,action)(job_id,uuid.uuid4().hex),
                    lambda value:self.view.set_status('正在暂停…' if action=='pause' else '正在取消…'))
            elif action in {'resume','retry_failed'} and self._current_job:
                self._mutation(action,self.runtime.resume(self._current_job,uuid.uuid4().hex,
                    retry_failed=action=='retry_failed',failed_pages_only=action=='retry_failed'),self._submitted)
            elif action=='results':
                self.view.set_record_form({'scope':'batch','search':'','result':'all'})
                self.view.show_page('records'); self._page='records'; self._query_records(reset=True)
            elif action=='record_query':self._query_records(reset=True)
            elif action=='verification_refresh':self._refresh_verification()
            elif action=='verification_recover':self._recover_verification([payload.get('key')])
            elif action=='verification_batch':
                self._recover_verification(list(payload.get('keys') or ()))
            elif action=='record_next' and self._record_next:
                self._record_cursors.append(self._record_next); self._record_version+=1; self._query_records()
            elif action=='record_previous' and len(self._record_cursors)>1:
                self._record_cursors.pop(); self._record_version+=1; self._query_records()
            elif action=='select_record':
                self._selected_record=payload.get('id'); row=self._record_rows.get(self._selected_record,{})
                self.view.set_detail(f"{row.get('title','')}\n作品 {row.get('illust_id','')}\n画师 {row.get('artist_name') or row.get('artist_id','')}\n"+
                    ('已通过原图结构检查' if row.get('verification')=='structure_checked' else _RESULT.get(row.get('state'),'详情见页结果')))
            elif action=='open_artwork' and self._selected_record in self._record_rows:
                self._watch('open_artwork',self.session.open_artwork(self._record_rows[self._selected_record]['illust_id']),lambda _:None)
            elif action=='export_records':
                self._choose_export('records')
            elif action=='cancel_export':self.runtime.cancel_export()
            elif action=='open_output':
                ref=self._selected_output(payload.get('scope'))
                if ref is None:
                    self.view.set_status('正在读取任务保存位置，请稍候');return
                self._watch(action,self.session.open_output(ref),lambda _:None)
            elif action=='log_toggle':
                self._log_open=bool(payload.get('open')); self.view.set_log_open(self._log_open)
            elif action=='log_clear':
                self.log_view.clear(); self.view.clear_log_text(); self.view.set_log_notice('显示已清空，保留日志不变')
            elif action=='log_pause':
                self._log_paused=not self._log_paused; self.log_view.pause(self._log_paused)
                self.view.set_log_notice('日志显示已暂停' if self._log_paused else '继续显示最近日志')
            elif action=='log_follow':
                self.log_view.set_follow(True); self.view.apply_log_delta(ViewDelta(follow=True))
            elif action=='export_log':
                self._choose_export('log')
        except Exception as error:
            self._show_error(error, notify=True)
            if action in {'start','sync_following','restart_scope','start_source'}:
                key='start_source' if action=='start_source' else 'start'
                self._busy.discard(key); self.view.configure_action(key,enabled=True)

    # ------------------------------------------------------------------
    # Verification page (W5): a bounded read plus an exactly-once recovery
    # ------------------------------------------------------------------

    def _invalidate_verification(self):
        """Forget the previous selection's verification read and its rows.

        Called from every path that can change which batch the page is about:
        selecting another task, a snapshot that opens a new batch, a submitted
        or resumed job, and teardown.  Dropping the pending entries is what
        lets the new selection issue its own read instead of being skipped as
        "already in flight".
        """
        self._verification_epoch += 1
        self._verification_batch = None
        for key in ('verification_pages', 'verification_summary'):
            # Drop the watcher, never cancel the producer: a cancelled future
            # is not a completion order guarantee, and the scope check already
            # rejects whatever it eventually returns.
            self._pending.pop(key, None)
        self._verify_rows = {}
        self._verify_display = []
        self._verify_summary = {}
        self._verify_truncated = False
        self._verify_dropped = 0
        if self._verify_busy and self._verify_queue:
            # The page already in flight belongs to the old batch and runs to
            # its own verdict; the rest of that selection is not dispatched.
            self._verify_stale_remaining += len(self._verify_queue)
            self._verify_queue = []

    def _verification_scope_ok(self, epoch, batch):
        """True when a finished read still describes what the user is looking at."""
        if self._closed or self._closing:
            return False
        return epoch == self._verification_epoch and batch == self._current_batch

    def _row_in_scope(self, row):
        """Second gate for the action table: the row must belong to this view."""
        if not isinstance(row, dict):
            return False
        if row.get('_epoch') != self._verification_epoch:
            return False
        if self._current_batch is not None and row.get('_batch') != self._current_batch:
            return False
        return True

    def _record_verification_result(self, value, tone):
        """Keep a bounded tally of the pass; the last success never hides a hold."""
        outcome = value.get('outcome') if isinstance(value, dict) else None
        bucket = _VERIFY_BUCKET_BY_OUTCOME.get(outcome)
        if bucket is None:
            bucket = 'unknown' if isinstance(value, dict) else 'error'
        self._verify_counts[bucket] = self._verify_counts.get(bucket, 0) + 1
        reason = value.get('reason') if isinstance(value, dict) else None
        if isinstance(reason, str) and reason and bucket != 'verified':
            self._verify_last_reason = reason
        if _TONE_RANK.get(tone, 1) > _TONE_RANK.get(self._verify_worst or 'muted', 0):
            self._verify_worst = tone

    def _refresh_verification(self):
        """Read the summary and one bounded page of held rows.

        Both requests run on the shared query owner.  The Tk thread only sees
        finished Futures through _poll, so no widget is ever built or updated
        from a query thread.  Each request carries the epoch and batch it was
        issued for, and its callback is dropped when either has moved on.
        """
        if self.runtime is None or self._verify_busy:
            return
        epoch, batch = self._verification_epoch, self._current_batch
        self._verification_batch = batch
        if 'verification_summary' not in self._pending:
            self._watch('verification_summary',
                self.runtime.verification_summary(batch_id=batch),
                lambda value: self._verification_summary(value, epoch=epoch, batch=batch))
        if 'verification_pages' not in self._pending:
            self._watch('verification_pages',
                self.runtime.verification_pages(batch_id=batch,
                    limit=_VERIFY_PAGE_SIZE),
                lambda value: self._verification_pages(value, epoch=epoch, batch=batch))

    def _verification_summary(self, value, *, epoch=None, batch=None):
        if epoch is not None and not self._verification_scope_ok(epoch, batch):
            return
        self._verify_summary = value if isinstance(value, dict) else {}
        self._render_verification()

    def _verification_pages(self, value, *, epoch=None, batch=None):
        if epoch is not None and not self._verification_scope_ok(epoch, batch):
            return
        rows = value.get('rows') if isinstance(value, dict) else ()
        self._verify_rows = {}
        display = []
        dropped = 0
        for row in rows or ():
            if not isinstance(row, dict):
                continue
            identity = row.get('batch_id')
            if batch is not None and identity != batch:
                # A row for another batch is not ours to show or to act on.
                dropped += 1
                continue
            key = row_key(identity, row.get('illust_id'),
                row.get('page_index'), row.get('delivery_id'))
            if key is None:
                continue
            frozen = dict(row)
            frozen['_epoch'] = epoch if epoch is not None else self._verification_epoch
            frozen['_batch'] = identity
            self._verify_rows[key] = frozen
            display.append({
                'key': key,
                'batch_id': identity,
                'illust_id': row.get('illust_id'),
                'page_index': row.get('page_index'),
                'delivery_id': row.get('delivery_id'),
                'state': row.get('state'),
                'error_code': row.get('error_code'),
                'blocked_reason': row.get('blocked_reason'),
                'batch_phase': row.get('batch_phase'),
                'attempts': row.get('attempts'),
                'retry_at': row.get('retry_at'),
                'output_key': row.get('output_key'),
                'time': _display_time(row.get('updated_at') or row.get('retry_at')),
                'recoverable': row.get('recoverable') is True,
            })
        self._verify_display = display
        self._verify_dropped = dropped
        self._verify_truncated = bool(isinstance(value, dict) and value.get('truncated'))
        self._render_verification()

    def _render_verification(self):
        parts = []
        text = summary_text(self._verify_summary)
        if text:
            parts.append(text)
        if self._verify_display:
            parts.append('列出 %d 行' % len(self._verify_display))
        if self._verify_dropped:
            parts.append('另有 %d 行属于其他批次，已忽略' % self._verify_dropped)
        if self._verify_truncated:
            parts.append('还有更多未列出')
        self.view.set_verification_rows(self._verify_display,
            page_text=' · '.join(parts))

    def _recover_verification(self, keys):
        """Start one recovery pass over the frozen rows named by the panel."""
        if self._verify_busy:
            self.view.set_verification_result('正在核验，请等这一轮结束。', 'warning')
            return
        targets = []
        skipped = 0
        for key in keys:
            row = self._verify_rows.get(key) if isinstance(key, str) else None
            if row is None or not self._row_in_scope(row):
                # A key from a previous selection (or from another batch) is
                # not an instruction for this one.
                skipped += 1
                continue
            targets.append(row)
        if not targets:
            self.view.set_verification_result('这一页已不在待核验列表里，已刷新。', 'warning')
            self._refresh_verification()
            return
        self._verify_queue = targets[:_VERIFY_BATCH_LIMIT]
        self._verify_skipped = skipped + max(0, len(targets) - _VERIFY_BATCH_LIMIT)
        self._verify_total = len(self._verify_queue)
        self._verify_done = 0
        self._verify_counts = {}
        self._verify_last_reason = None
        self._verify_worst = None
        self._verify_stale_remaining = 0
        # The pass owns the batch it started with; switching tasks later must
        # not rewrite a command that is already in flight.
        self._verify_pass_epoch = self._verification_epoch
        self._verify_pass_batch = (
            self._current_batch if self._current_batch is not None
            else targets[0].get('_batch'))
        self._verify_busy = True
        self.view.set_verification_busy(True, text='已完成 0/%d' % self._verify_total)
        self._verify_step()

    def _verify_step(self):
        """Dispatch exactly one page, then chain the next from its callback.

        A serial chain keeps at most one recovery queued on the single I/O
        owner, so a full queue cannot turn a batch into a partial mess.
        """
        if self._closed or self._closing:
            self._verify_queue = []
            self._verify_busy = False
            self._verify_pass_epoch = None
            self._verify_pass_batch = None
            return
        if (self._verify_pass_epoch is not None
                and self._verify_pass_epoch != self._verification_epoch):
            # The user moved to another selection: the rest of this pass is not
            # dispatched, and the page already in flight still finishes.
            self._verify_stale_remaining += len(self._verify_queue)
            self._verify_queue = []
        if not self._verify_queue:
            self._finish_verification()
            return
        row = self._verify_queue.pop(0)
        key = 'verify:' + str(row_key(row.get('batch_id'), row.get('illust_id'),
            row.get('page_index'), row.get('delivery_id')))
        command_id = uuid.uuid4().hex
        try:
            future = self.session.recover_verification_page(row['batch_id'],
                row['illust_id'], row['page_index'], command_id=command_id)
        except Exception as error:
            self._verification_failed(error)
            return
        self._watch(key, future, lambda value: self._verify_finished(row, value))

    def _verify_finished(self, row, value):
        self._verify_done += 1
        text, tone = describe_result(value)
        self._record_verification_result(value, tone)
        self.view.set_verification_result(text, tone)
        if self._verify_total > 1:
            self.view.set_verification_busy(True,
                text='已完成 %d/%d' % (self._verify_done, self._verify_total))
        self._verify_step()

    def _finish_verification(self):
        """Close one pass and let the last visible line carry its worst outcome.

        "Processed" and "verified" are different facts: a held page, a refused
        page and an unrecognised answer all finish the pass, and none of them is
        a success.  The tally is bounded by the batch limit, so no extra state
        is needed beyond this pass.
        """
        self._verify_busy = False
        done, total, skipped = self._verify_done, self._verify_total, self._verify_skipped
        counts = dict(self._verify_counts)
        self.view.set_verification_busy(False, text='')
        stale = (self._verify_pass_epoch is not None
                 and self._verify_pass_epoch != self._verification_epoch)
        pass_batch, remaining = self._verify_pass_batch, self._verify_stale_remaining
        self._verify_pass_epoch = None
        self._verify_pass_batch = None
        self._verify_stale_remaining = 0
        if stale:
            self.view.set_verification_result(
                '上一批（批次 %s）已停止：还剩 %d 页未处理；没有改动任何文件。'
                % (pass_batch or '未知', remaining), 'warning')
            return
        failed = any(counts.get(bucket) for bucket in
                     ('repair_queued', 'held', 'refused', 'unknown', 'error'))
        if done and not failed and counts.get('verified', 0) == done:
            text = '本轮核验完成：%d/%d 页。' % (done, total)
            tone = 'success'
        else:
            parts = ['%s %d' % (_VERIFY_BUCKET_TEXT[bucket], counts[bucket])
                     for bucket in _VERIFY_BUCKETS if counts.get(bucket)]
            text = '本轮处理 %d/%d 页：%s。' % (done, total, '、'.join(parts) or '没有结果')
            if self._verify_last_reason:
                text += ' 最后一条原因：' + reason_text(self._verify_last_reason)
            tone = self._verify_worst or 'warning'
        if skipped:
            text += ' 另有 %d 页已不在列表里，已跳过。' % skipped
        self.view.set_verification_result(text, tone)
        self._next_overview = 0
        self._refresh_verification()

    def _verification_failed(self, error):
        """Stop the pass on the first failure; no file was touched."""
        self._verify_queue = []
        self._verify_busy = False
        self.view.set_verification_busy(False, text='')
        stale = (self._verify_pass_epoch is not None
                 and self._verify_pass_epoch != self._verification_epoch)
        pass_batch = self._verify_pass_batch
        self._verify_pass_epoch = None
        self._verify_pass_batch = None
        text = _ERROR.get(getattr(error, 'code', None)) or (
            '核验没有完成。请刷新后重试；若持续出现，请查看诊断日志。')
        if stale:
            text = '上一批（批次 %s）：%s' % (pass_batch or '未知', text)
        if self._verify_total:
            text += '（本轮已完成 %d/%d 页）' % (self._verify_done, self._verify_total)
        self.view.set_verification_result(text, 'danger')
        self._refresh_verification()

    def request_close(self):
        self._assert_ui()
        if self._closed or self._closing:return
        self._closing=True; self.view.set_status('正在安全暂停并关闭…')
        self._launch_close()

    def _launch_close(self):
        self._close_result=None
        def close():
            try:self.session.close(timeout=20);self._close_result=True
            except Exception as error:self._close_result=error
        self._close_thread=threading.Thread(target=close,name='collection-desktop-close',daemon=False)
        self._close_thread.start()

    def _poll_close(self):
        if self._close_thread is None or self._close_thread.is_alive():return
        if self._close_result is True:
            self.dispose(); self.root.destroy()
        else:
            self.view.set_status('仍在等待后台收尾，请保持窗口打开', 'warning')
            # One bounded close owner at a time. A timed-out owner has already
            # returned; retrying waits on the same live resources.
            self._launch_close()

    def dispose(self):
        self._assert_ui(); self._closed=True
        self._invalidate_verification()
        self._verify_queue=[]; self._verify_busy=False; self._busy.clear()
        if self._after is not None:
            self.root.after_cancel(self._after); self._after=None
        for future,_,_ in self._pending.values():future.cancel()
        self._pending.clear(); self.view.dispose()
