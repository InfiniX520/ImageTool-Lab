"""Owned desktop ports for the new core; Tk never calls network, SQLite or file I/O."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import logging
import os
import queue
import threading
import time
import webbrowser
from concurrent.futures import Future

from core.api_transport import BoundedMetadataSession, metadata_error
from core.app_paths import AppPaths
from core.collection_runtime import CollectionRuntime, RuntimeClosed, RuntimeQueueFull, _failed
from core.collection_settings import CollectionSettings, load_settings, save_settings
from core.diagnostic_logging import DiagnosticLog
from core.download_engine import build_common_headers
from core.library_paths import PathResolver
from core.library_repository import LibraryRepository
from core.output_locations import OutputLocations, read_artist_ids
from core.page_delivery import PageDeliveryExecutor
from core.pixiv_follow_source import PixivFollowSource
from core.pixiv_query_source import PixivQuerySource
from core.request_pacing import RequestPacer
from core.task_contracts import PathRef
from pixivpy3 import AppPixivAPI


class AccountError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _authorized_client(paths, stop, api_factory=AppPixivAPI, before_request=None):
    """One explicit existing-token authorization; no save, retries or raw errors."""
    api = None
    try:
        with paths.legacy_config_file.open('rb') as stream:
            raw = stream.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise AccountError('authorization_file_too_large')
        values = json.loads(raw.decode('utf-8-sig'))
        credential = values.get('token') if isinstance(values, dict) else None
        if not isinstance(credential, str) or not credential.strip():
            raise AccountError('authorization_missing')
        if stop.is_set():
            raise AccountError('authorization_stopped')
        api = api_factory(timeout=(5, 15), allow_redirects=False)
        api.requests.close()
        api.requests = BoundedMetadataSession(stop_event=stop,before_request=before_request)
        # The installed SDK can emit a diagnostic while authenticating. Keep
        # exception text and response payload out of all application logging.
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            response = api.auth(refresh_token=credential.strip())
        credential = None
        user_id = str(api.user_id)
        if not user_id.isascii() or not user_id.isdecimal() or len(user_id) > 32:
            raise AccountError('authorization_identity_missing')
        return api, {'account_id': user_id, 'premium': bool(
            isinstance(response, dict) and isinstance(response.get('user'), dict)
            and response['user'].get('is_premium') is True)}
    except Exception as error:
        if api is not None:
            api.requests.close()
        fact = metadata_error(error)
        if fact is not None:
            raise AccountError(fact.code) from None
        if isinstance(error, AccountError):
            raise
        raise AccountError('authorization_failed') from None


class DesktopSourceFactory:
    """Called only by the source owner after an explicitly queued job starts."""
    def __init__(self, paths, api_factory=AppPixivAPI, before_request=None):
        self.paths, self.api_factory = paths, api_factory
        self.before_request=before_request

    def __call__(self, request, stop):
        api, identity = _authorized_client(self.paths, stop, self.api_factory,self.before_request)
        if request.account_ref != 'pixiv:' + identity['account_id']:
            api.requests.close()
            raise AccountError('authorization_account_mismatch')
        return PixivQuerySource(api, account_id=identity['account_id'], account_ref=request.account_ref)


class DesktopRuntime(CollectionRuntime):
    """Read ports reuse the fixed short-query owner; never add per-view pools."""
    def overview(self, *, after=None, limit=100):
        return self._query_command(self._queries, self._overview, after=after, limit=limit)

    def snapshot(self, job_id):
        return self._query_command(self._queries, self.repository.snapshot, job_id)

    def request(self, job_id):
        return self._query_command(self._queries, self.repository.get_request, job_id)

    def artists(self, account_ref):
        return self._query_command(self._queries, self.repository.follow_artists, account_ref, limit=100)

    def verification_pages(self, *, batch_id=None, states=None, after=None, limit=50):
        """Bounded read of the pages parked for verification, on the query owner."""
        return self._query_command(self._queries, self._verification_pages,
            batch_id=batch_id, states=states, after=after, limit=limit)

    def verification_summary(self, *, batch_id=None):
        """Counts per delivery state for the panel header, on the query owner."""
        return self._query_command(self._queries, self._verification_summary,
            batch_id=batch_id)

    def _verification_pages(self, *, batch_id=None, states=None, after=None, limit=50):
        return self.repository.list_verification_pages(batch_id=batch_id,
            states=states, after=after, limit=limit)

    def _verification_summary(self, *, batch_id=None):
        return self.repository.verification_summary(batch_id=batch_id)

    def _overview(self, *, after=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid task page size')
        if after is not None and (not isinstance(after, tuple) or len(after) != 2
                or not all(isinstance(value, str) and len(value) <= 128 for value in after)):
            raise ValueError('Invalid task page cursor')
        with contextlib.closing(self.repository._readonly_connection()) as connection:
            revision = int(connection.execute('SELECT COALESCE(MAX(event_seq),0) FROM events').fetchone()[0])
            cache_key = (after, limit, revision)
            cached = getattr(self, '_overview_cache', None)
            if cached is not None and cached[0] == cache_key:
                return copy.deepcopy(cached[1])
            condition = '' if after is None else ' WHERE (j.created_at,j.job_id)<(?,?)'
            params = () if after is None else after
            rows = connection.execute(
                'SELECT j.job_id,j.account_ref,j.state,j.created_at,j.request_json,'
                ' b.batch_id,b.phase,b.stop_reason FROM jobs j LEFT JOIN batches b ON b.rowid='
                '(SELECT b2.rowid FROM batches b2 WHERE b2.job_id=j.job_id ORDER BY b2.rowid DESC LIMIT 1)'
                + condition + ' ORDER BY j.created_at DESC,j.job_id DESC LIMIT ?', (*params, limit+1)).fetchall()
            more = len(rows) > limit
            values = tuple(dict(row) for row in rows[:limit])
            for value in values:
                value['query'] = json.loads(value.pop('request_json'))['query']
                value['results'] = self.repository._results_counts(connection, value['batch_id']) if value['batch_id'] else {'pages':{}, 'works':{}}
            cursor = (values[-1]['created_at'], values[-1]['job_id']) if values and more else None
            result = {'rows': values, 'next_cursor': cursor, 'revision': revision}
            self._overview_cache = (cache_key, result)
            return copy.deepcopy(result)


class DesktopSession:
    """One bounded account/settings owner, plus the fixed core owners.

    Construction is inert. startup() and all I/O commands return Futures.
    close() is used by a background close owner, never the Tk event handler.
    """
    allow_authorization_updates = True

    def __init__(self, paths: AppPaths, *, api_factory=AppPixivAPI, source_factory=None,
                 executor_factory=None, opener=None):
        self.paths, self.resolver = paths, PathResolver(paths)
        self.locations = OutputLocations(paths, self.resolver)
        self.api_factory, self.source_factory = api_factory, source_factory
        self.executor_factory, self.opener = executor_factory, opener
        self.runtime = self.log = None
        self.settings = CollectionSettings()
        self.identity = None
        self._guard = threading.RLock()
        self._closing = threading.Event()
        self._commands = queue.Queue(4)
        self._ready = Future()
        self._started = self._closed = False
        self._worker = threading.Thread(target=self._io_loop, name='collection-desktop-io', daemon=False)

    def startup(self):
        with self._guard:
            if self._closing.is_set():
                return _failed(RuntimeClosed('Desktop is closing'))
            if not self._started:
                self._started = True
                self._worker.start()
            return self._ready

    def _io_loop(self):
        try:
            self.locations.load()
            self.settings = load_settings(self.resolver)
            self._api_pacer=RequestPacer(self.settings.api_interval)
            self._image_pacer=RequestPacer(self.settings.image_interval)
            if self._closing.is_set():
                raise RuntimeClosed('Desktop closed during startup')
            self.log = DiagnosticLog(self.paths.root / 'runtime_logs/v2')
            repository = LibraryRepository(self.paths.download_logs_dir / 'library.sqlite3')
            try:
                from core.task_coordinator import TaskCoordinator
                delivery = (self.executor_factory(repository, self.resolver) if self.executor_factory else
                    PageDeliveryExecutor(repository, self.resolver,
                        before_request=self._image_pacer,
                        headers_factory=lambda url: build_common_headers(user_agent=self.settings.user_agent)))
                self.runtime = DesktopRuntime(TaskCoordinator(repository, delivery, workers=self.settings.workers),
                    self.source_factory or DesktopSourceFactory(self.paths, self.api_factory,self._api_pacer), diagnostic=self.log)
                self.runtime.start().result(20)
            except BaseException:
                if self.runtime is None:
                    repository.close()
                raise
            self._ready.set_result({'settings': self.locations.form(self.settings), 'authorization': 'not_checked'})
            while not self._closing.is_set():
                try:
                    future, method, args = self._commands.get(timeout=.1)
                except queue.Empty:
                    continue
                try:
                    if self._closing.is_set():
                        future.cancel()
                    elif future.set_running_or_notify_cancel():
                        try:
                            future.set_result(method(*args))
                        except Exception as error:
                            future.set_exception(error)
                finally:
                    self._commands.task_done()
        except BaseException as error:
            if not self._ready.done():
                self._ready.set_exception(error)
        finally:
            while True:
                try:
                    future, _, _ = self._commands.get_nowait()
                except queue.Empty:
                    break
                future.cancel(); self._commands.task_done()

    def _command(self, method, *args):
        result = Future()
        with self._guard:
            if not self._ready.done() or self._ready.exception() is not None or self._closing.is_set():
                return _failed(RuntimeClosed('Desktop is not accepting I/O commands'))
            try:
                self._commands.put_nowait((result, method, args))
            except queue.Full:
                return _failed(RuntimeQueueFull('Account/settings capacity reached'))
        return result

    def verify_account(self):
        return self._command(self._verify)

    def update_authorization(self, credential):
        return self._command(self._update_authorization, credential)

    def _update_authorization(self, credential):
        from core.authorization_settings import save_authorization
        if not self.allow_authorization_updates:
            raise AccountError('demo_authorization_disabled')
        if self.runtime.coordinator.active_batch_id is not None:
            raise AccountError('authorization_task_active')
        result = save_authorization(self.resolver, credential)
        with self._guard:
            self.identity = None
        return result

    def _verify(self):
        api, identity = _authorized_client(self.paths, self._closing, self.api_factory,self._api_pacer)
        try:
            with self._guard:
                self.identity = identity
            return dict(identity)
        finally:
            api.requests.close()

    def recover_verification_page(self, batch_id, illust_id, page_index, *,
                                 command_id=None):
        """Re-check one held page on the single I/O owner.

        The recovery entry observes files and compare-and-sets rows; both run
        on this owner so the Tk thread never hashes bytes or opens a database
        transaction.  A repeated call with the same command_id replays the
        first accepted result instead of reading the file again.
        """
        return self._command(self._recover_verification_page, batch_id, illust_id,
            page_index, command_id)

    def _recover_verification_page(self, batch_id, illust_id, page_index, command_id):
        runtime = self.runtime
        if runtime is None:
            raise RuntimeClosed('Desktop is not ready')
        return runtime.repository.recover_verification_page(batch_id, illust_id,
            page_index, command_id=command_id)

    def save(self, preferences):
        return self._command(self._save, preferences)

    def _save(self, preferences):
        settings = CollectionSettings.from_form(preferences, previous=self.settings)
        self.resolver.resolve(settings.output_ref)
        save_settings(self.resolver, settings)
        with self._guard:
            self.settings = settings
        return self.locations.form(settings)

    def dialog_directory(self, preferences):
        return self._command(self.locations.dialog_directory, dict(preferences))

    def select_directory(self, selected):
        return self._command(self.locations.select_directory, selected)

    def export_target(self, selected, kind):
        return self._command(self.locations.export_target, selected, kind)

    def import_artist_ids(self, selected):
        return self._command(read_artist_ids, selected)

    def open_output(self, output_ref):
        return self._command(self._open_output, output_ref)

    def _open_output(self, output_ref):
        target = self.resolver.resolve(output_ref)
        if not target.is_dir():
            raise FileNotFoundError('Output directory has not been created')
        (self.opener or os.startfile)(str(target))

    def open_artwork(self, illust_id):
        if not isinstance(illust_id, str) or not illust_id.isascii() or not illust_id.isdecimal() or len(illust_id) > 32:
            return _failed(ValueError('Invalid artwork id'))
        return self._command(self.opener or webbrowser.open, 'https://www.pixiv.net/artworks/' + illust_id)

    def close(self, timeout=20):
        deadline = time.monotonic() + timeout
        self._closing.set()
        if self._started:
            self._worker.join(max(0, deadline-time.monotonic()))
            if self._worker.is_alive():
                raise TimeoutError('Account/settings owner is still closing')
        if self.runtime is not None:
            self.runtime.close(timeout=max(0, deadline-time.monotonic()))
        elif self.log is not None and not self.log.close(timeout=max(0, deadline-time.monotonic())):
            raise TimeoutError('Diagnostic owner is still closing')
        self._closed = True

    def __enter__(self):
        try:
            self.startup().result(20)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_):
        self.close()
