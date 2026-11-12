"""Application service for resumable followed-artist traversal."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.follow_crawl_state import (
    ARTIST_RETRY_STATES,
    ARTIST_UNFOLLOWED_STATE,
    ArtistProgress,
    FollowArtistSnapshot,
    FollowCrawlRun,
    FollowCrawlStateRepository,
)
from core.pixiv_api_policy import (
    ApiAuthenticationRequired,
    ApiRequestCancelled,
    FollowCrawlControl,
    PixivApiRequestExecutor,
    redact_error_message,
)

START_RESUME = "resume"
START_RESTART = "restart"
START_INDEX = "index"
START_ARTIST = "artist"
START_RETRY_FAILED = "retry_failed"
VALID_START_MODES = {
    START_RESUME,
    START_RESTART,
    START_INDEX,
    START_ARTIST,
    START_RETRY_FAILED,
}

# Snapshot pagination failures with these kinds must abort the run rather
# than fall back to a partial follow list.
_ABORT_SNAPSHOT_KINDS = {"cancelled", "request_budget", "queue_budget", "authentication"}

# Hard cap on pages fetched per artist during one incremental sweep.  A
# sweep normally stops at the stored cutoff on page 1; the cap only bounds
# artists that publish faster than the crawl cadence.
SWEEP_MAX_PAGES = 10


@dataclass(frozen=True, slots=True)
class FollowCrawlOptions:
    start_mode: str = START_RESUME
    start_value: str = ""
    max_artists_per_batch: int = 30
    max_queued_items: int = 100

    def normalized(self) -> "FollowCrawlOptions":
        mode = self.start_mode if self.start_mode in VALID_START_MODES else START_RESUME
        return FollowCrawlOptions(
            start_mode=mode,
            start_value=str(self.start_value or "").strip(),
            max_artists_per_batch=max(1, int(self.max_artists_per_batch)),
            max_queued_items=max(1, int(self.max_queued_items)),
        )


@dataclass(frozen=True, slots=True)
class FollowCrawlSummary:
    run_id: str = ""
    status: str = "completed"
    stop_reason: str = ""
    total_artists: int = 0
    attempted_artists: int = 0
    completed_artists: int = 0
    queued_items: int = 0
    api_requests: int = 0


@dataclass(slots=True)
class _BatchProgress:
    attempted: int = 0
    completed: int = 0
    queued: int = 0
    status: str = "completed"
    stop_reason: str = ""

    def pause(self, reason: str, *, status: str = "paused") -> None:
        self.status = status
        self.stop_reason = str(reason or "")


@dataclass(slots=True)
class _ArtistTraversal:
    cursor: dict
    item_index: int
    last_illust_id: str
    works_seen: int
    queued_items: int
    queued_before_resume: int

    @classmethod
    def from_progress(cls, progress: ArtistProgress) -> "_ArtistTraversal":
        queued = max(0, progress.queued_items)
        return cls(
            cursor=dict(progress.cursor),
            item_index=max(0, progress.item_index),
            last_illust_id=progress.last_illust_id,
            works_seen=max(0, progress.works_seen),
            queued_items=queued,
            queued_before_resume=queued,
        )

    @property
    def newly_queued(self) -> int:
        return max(0, self.queued_items - self.queued_before_resume)


def build_follow_config_key(config, *, resume_enabled: bool) -> str:
    """Fingerprint settings that materially affect crawl completeness."""

    save_dir = str(getattr(config, "save_dir", "") or "")
    try:
        normalized_save_dir = str(Path(save_dir).expanduser().resolve()) if save_dir else ""
    except (OSError, RuntimeError):
        normalized_save_dir = save_dir
    payload = {
        "save_dir": normalized_save_dir,
        "original": bool(getattr(config, "original", True)),
        "multi_page": bool(getattr(config, "multi_page", True)),
        "folder_by_user": bool(getattr(config, "folder_by_user", False)),
        "min_bookmarks": int(getattr(config, "min_bookmarks", 0) or 0),
        "skip_ai": bool(getattr(config, "skip_ai", False)),
        "name_template": str(getattr(config, "name_template", "") or ""),
        "resume_enabled": bool(resume_enabled),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _revive_refollowed_artists(
    repository: FollowCrawlStateRepository,
    run_id: str,
    tracked: list[ArtistProgress],
    current_ids: set[str],
) -> list[str]:
    """复活重新关注的画师：原标记为 unfollowed 但当前关注列表中又出现。

    Returns: 复活的 artist_id 列表（已调用 repository.revive_artists 落库）。
    """
    revived = [
        artist.artist_id
        for artist in tracked
        if artist.state == ARTIST_UNFOLLOWED_STATE and artist.artist_id in current_ids
    ]
    if revived:
        repository.revive_artists(run_id, revived)
    return revived


def _append_new_following_artists(
    repository: FollowCrawlStateRepository,
    run_id: str,
    snapshots: list[FollowArtistSnapshot],
    tracked_ids: set[str],
) -> int:
    """追加新关注画师到运行队列，返回实际入库数量。"""
    new_artists = [snapshot for snapshot in snapshots if snapshot.artist_id not in tracked_ids]
    if not new_artists:
        return 0
    return repository.append_artists(run_id, new_artists)


def _mark_unfollowed_artists(
    repository: FollowCrawlStateRepository,
    run_id: str,
    tracked: list[ArtistProgress],
    current_ids: set[str],
    complete: bool,
) -> int | None:
    """处理取消关注的画师。

    Returns:
        - ``int``: 完整刷新时返回标记为 unfollowed 的画师数量（含 0）。
        - ``None``: 部分刷新时不做处理，调用方按需记录警告。
    """
    if not complete:
        return None
    stale = [
        artist.artist_id
        for artist in tracked
        if artist.state != ARTIST_UNFOLLOWED_STATE and artist.artist_id not in current_ids
    ]
    if stale:
        repository.mark_artists_unfollowed(run_id, stale)
    return len(stale)


def _log_unfollowed_summary(log, unfollowed_count: int | None, has_missing: bool) -> None:
    """按完整/部分刷新分支记录取消关注画师的日志。

    提取自 ``_reconcile_snapshot`` 末尾的 ``if/elif`` 分支，避免主函数复杂度上升。
    """
    if unfollowed_count is not None and unfollowed_count > 0:
        log(f"[*] 快照同步：{unfollowed_count} 位画师已取消关注，跳过下载")
    elif unfollowed_count is None and has_missing:
        log("[!] 关注列表不完整，本次不移除任何画师")


def _find_sweep_cutoff(illusts: list, cutoff: str) -> int:
    """在 ``illusts`` 中定位已下载截止作品的位置。

    返回 ``stop_at`` 索引：截止作品位于 ``[0, len-1]`` 时返回其下标，
    未提供 ``cutoff`` 或未匹配到时返回 ``len(illusts)``（即整页都要处理）。
    提取自 ``_sweep_artist`` 中嵌套 ``for + if + break`` 段。
    """
    if not cutoff:
        return len(illusts)
    for index, illust in enumerate(illusts):
        if str(getattr(illust, "id", "") or "") == cutoff:
            return index
    return len(illusts)


class FollowCrawlService:
    """Coordinate snapshots, checkpoints, request policy and artwork handling."""

    def __init__(
        self,
        *,
        api,
        repository: FollowCrawlStateRepository,
        request_executor: PixivApiRequestExecutor,
        options: FollowCrawlOptions,
        process_illust: Callable[[object, int], object],
        should_stop: Callable[[], bool],
        log: Callable[[str], None],
        progress_callback: Callable[[dict], None] | None = None,
    ) -> None:
        self.api = api
        self.repository = repository
        self.request_executor = request_executor
        self.options = options.normalized()
        self.process_illust = process_illust
        self.should_stop = should_stop
        self.log = log
        self.progress_callback = progress_callback or (lambda _payload: None)

    def run(self, *, account_id: str, config_key: str) -> FollowCrawlSummary:
        run, preparation_error = self._prepare_or_error(str(account_id), str(config_key))
        if preparation_error is not None:
            return preparation_error
        assert run is not None

        artists = self._artists_for_run(run)
        if not artists and (
            run.total_artists == 0 or self.options.start_mode == START_RETRY_FAILED
        ):
            return self._finish_empty_run(run)

        progress = _BatchProgress()
        crawled_ids: set[str] = set()
        for artist in artists:
            stop_reason = self._batch_stop_reason(progress)
            if stop_reason:
                progress.pause(stop_reason)
                break
            self._execute_artist(run, artist, progress)
            crawled_ids.add(artist.artist_id)
            if progress.status != "completed":
                break
        if progress.status == "completed" and self.options.start_mode != START_RETRY_FAILED:
            self._sweep_completed_artists(run, progress, exclude_ids=crawled_ids)
        return self._finish_run(run, progress)

    def _prepare_or_error(
        self,
        account_id: str,
        config_key: str,
    ) -> tuple[FollowCrawlRun | None, FollowCrawlSummary | None]:
        try:
            return self._prepare_run(account_id, config_key), None
        except FollowCrawlControl as exc:
            self.log(f"[!] 关注列表尚未建立检查点：{exc}")
            return None, FollowCrawlSummary(
                status="paused",
                stop_reason=exc.kind,
                api_requests=self.request_executor.requests_used,
            )
        except (LookupError, ValueError) as exc:
            self.log(f"[-] 无法建立关注任务：{exc}")
            return None, FollowCrawlSummary(
                status="failed",
                stop_reason=redact_error_message(exc),
                api_requests=self.request_executor.requests_used,
            )

    def _finish_empty_run(self, run: FollowCrawlRun) -> FollowCrawlSummary:
        retry_only = self.options.start_mode == START_RETRY_FAILED
        status = "paused" if retry_only else "completed"
        reason = "没有未完整画师" if retry_only else "没有待处理画师"
        self.repository.mark_run(run.run_id, status, reason)
        self.log(f"[*] 当前任务{reason}")
        return FollowCrawlSummary(
            run_id=run.run_id,
            status=status,
            stop_reason=reason,
            total_artists=run.total_artists,
            api_requests=self.request_executor.requests_used,
        )

    def _batch_stop_reason(self, progress: _BatchProgress) -> str:
        if progress.attempted >= self.options.max_artists_per_batch:
            return f"已达到本批画师上限 {self.options.max_artists_per_batch}"
        if progress.queued >= self.options.max_queued_items:
            return f"已达到本批下载队列上限 {self.options.max_queued_items}"
        if self.should_stop():
            return "用户停止任务"
        return ""

    def _execute_artist(
        self,
        run: FollowCrawlRun,
        artist: ArtistProgress,
        progress: _BatchProgress,
    ) -> None:
        progress.attempted += 1
        self.repository.mark_artist_running(run.run_id, artist.artist_id)
        self.log(
            f"[*] [画师 {artist.ordinal}/{run.total_artists}] "
            f"{artist.artist_name or '未知画师'} ({artist.artist_id})"
        )
        self._publish_progress(
            run, artist, progress.attempted, progress.completed, progress.queued, "running"
        )
        try:
            queued, artist_state = self._crawl_artist(run, artist, batch_queued=progress.queued)
        except FollowCrawlControl as exc:
            self._record_controlled_pause(run, artist, progress, exc)
            return
        except Exception as exc:
            self._record_unexpected_failure(run, artist, progress, exc)
            return

        progress.queued += queued
        progress.completed += 1
        self.log(
            f"[+] 画师 {artist.artist_id} 遍历完成，新增队列 {queued}，"
            f"本批累计 {progress.queued}/{self.options.max_queued_items}"
        )
        self._publish_progress(
            run, artist, progress.attempted, progress.completed, progress.queued, artist_state
        )

    def _record_controlled_pause(
        self,
        run: FollowCrawlRun,
        artist: ArtistProgress,
        progress: _BatchProgress,
        exc: FollowCrawlControl,
    ) -> None:
        progress.queued += self._newly_queued_since(run.run_id, artist)
        progress.pause(str(exc))
        error_kind = (
            "cancelled" if isinstance(exc, ApiRequestCancelled) and self.should_stop() else exc.kind
        )
        self.repository.mark_artist_deferred(
            run.run_id,
            artist.artist_id,
            error_kind=error_kind,
            error_message=redact_error_message(exc),
            retry_at=exc.retry_at,
        )
        if isinstance(exc, ApiAuthenticationRequired):
            self.log("[-] Pixiv API 登录状态失效，已保存断点；刷新登录后可继续")
        else:
            self.log(f"[!] 已保存画师断点，本批安全暂停：{exc}")

    def _record_unexpected_failure(
        self,
        run: FollowCrawlRun,
        artist: ArtistProgress,
        progress: _BatchProgress,
        exc: Exception,
    ) -> None:
        message = redact_error_message(exc)
        progress.queued += self._newly_queued_since(run.run_id, artist)
        progress.pause(f"画师任务异常：{message}", status="partial")
        self.repository.mark_artist_deferred(
            run.run_id,
            artist.artist_id,
            error_kind="unexpected",
            error_message=message,
            state="failed",
        )
        self.log(f"[-] 画师任务异常，已保存断点：{message}")

    def _newly_queued_since(self, run_id: str, artist: ArtistProgress) -> int:
        latest = self._artist_progress(run_id, artist.artist_id)
        return max(0, latest.queued_items - artist.queued_items) if latest is not None else 0

    def _finish_run(self, run: FollowCrawlRun, progress: _BatchProgress) -> FollowCrawlSummary:
        remaining = self.repository.list_artists(
            run.run_id,
            states=ARTIST_RETRY_STATES,
            start_ordinal=run.start_ordinal,
        )
        if progress.status == "completed" and remaining:
            progress.pause("本批范围处理结束，仍有待处理画师")
        self.repository.mark_run(run.run_id, progress.status, progress.stop_reason)

        if progress.status == "completed":
            self.log(f"[+] 关注画师任务完整结束：{run.run_id[:8]}")
        else:
            self.log(
                f"[!] 关注画师任务已保存：{run.run_id[:8]} | {progress.stop_reason} | "
                "下次选择「智能续跑」即可继续"
            )
        return FollowCrawlSummary(
            run_id=run.run_id,
            status=progress.status,
            stop_reason=progress.stop_reason,
            total_artists=run.total_artists,
            attempted_artists=progress.attempted,
            completed_artists=progress.completed,
            queued_items=progress.queued,
            api_requests=self.request_executor.requests_used,
        )

    def _prepare_run(self, account_id: str, config_key: str) -> FollowCrawlRun:
        if not account_id:
            raise ValueError("Pixiv API 当前没有有效账号 ID，请重新登录")
        mode = self.options.start_mode
        if mode in (START_RESUME, START_RETRY_FAILED):
            existing = self.repository.find_resumable_run(account_id, config_key)
            if existing is None and mode == START_RESUME:
                latest = self.repository.find_latest_run(account_id, config_key)
                if latest is not None and latest.status == "completed":
                    self.repository.reopen_run(latest.run_id)
                    existing = self.repository.get_run(latest.run_id)
                    self.log(
                        f"[*] 重新打开已完成任务 {existing.run_id[:8]}，"
                        "同步关注列表并检查画师更新..."
                    )
            if existing is not None:
                self.log(
                    f"[*] 恢复关注任务 {existing.run_id[:8]}，"
                    f"快照共 {existing.total_artists} 位画师"
                )
                return self._reconcile_snapshot(existing)
            if mode == START_RETRY_FAILED:
                raise LookupError("没有可重试的历史画师任务")

        snapshots, snapshot_complete = self._fetch_following_snapshot(account_id)
        if not snapshots:
            raise LookupError("未获取到关注画师")
        if not snapshot_complete:
            self.log("[!] 关注列表快照不完整，已按当前可用部分建立任务")
        start_ordinal = self._resolve_start_ordinal(snapshots)
        run = self.repository.create_run(
            account_id=account_id,
            config_key=config_key,
            artists=snapshots,
            start_mode=mode,
            start_value=self.options.start_value,
            start_ordinal=start_ordinal,
        )
        self.log(
            f"[*] 已创建关注任务 {run.run_id[:8]}：共 {run.total_artists} 位，"
            f"从第 {run.start_ordinal} 位开始"
        )
        return run

    def _reconcile_snapshot(self, run: FollowCrawlRun) -> FollowCrawlRun:
        """Sync a stored snapshot with the current follow list.

        Newly followed artists are appended to the queue, artists that were
        unfollowed are parked in a terminal state (never downloaded again),
        and re-followed artists are revived.  Removals only happen when the
        refreshed follow list is known to be complete; a partial refresh
        can only append, never remove.

        结构（R6-4 重构）：原 D(22) 单体函数拆为3个模块级 helper ——
        ``_revive_refollowed_artists``（复活重新关注）、``_append_new_following_artists``
        （追加新关注）、``_mark_unfollowed_artists``（取消关注清理）。主函数降为
        编排器，依次调用 helper 并按返回值短路日志。

        诊断日志（R7-6）：记录快照获取、复活/追加/清理统计、最终刷新决策。
        """

        try:
            snapshots, complete = self._fetch_following_snapshot(str(run.account_id))
        except FollowCrawlControl:
            raise
        except Exception as exc:
            self.log(f"[!] 关注列表刷新失败，沿用历史快照：{redact_error_message(exc)}")
            return run
        if not snapshots:
            self.log("[!] 关注列表刷新为空，沿用历史快照")
            return run

        self.log(
            f"[D] reconcile_snapshot: snapshots={len(snapshots)} "
            f"complete={complete} run={run.run_id}"
        )
        current_ids = {snapshot.artist_id for snapshot in snapshots}
        tracked = self.repository.list_artists(run.run_id)
        tracked_ids = {artist.artist_id for artist in tracked}
        self.log(
            f"[D] reconcile_snapshot: tracked={len(tracked)} "
            f"current={len(current_ids)} intersection={len(tracked_ids & current_ids)}"
        )

        revived = _revive_refollowed_artists(self.repository, run.run_id, tracked, current_ids)
        if revived:
            self.log(f"[*] 快照同步：{len(revived)} 位画师重新关注，已重新加入队列")

        added = _append_new_following_artists(self.repository, run.run_id, snapshots, tracked_ids)
        if added:
            self.log(f"[*] 快照同步：新增 {added} 位新关注画师")

        unfollowed_count = _mark_unfollowed_artists(
            self.repository, run.run_id, tracked, current_ids, complete
        )
        _log_unfollowed_summary(self.log, unfollowed_count, bool(tracked_ids - current_ids))
        if unfollowed_count is not None:
            self.log(
                f"[D] reconcile_snapshot: unfollowed_marked={unfollowed_count} "
                f"complete={complete}"
            )

        if revived or added or complete:
            self.log(
                f"[D] reconcile_snapshot: 刷新 totals (revived={len(revived)} "
                f"added={added} complete={complete})"
            )
            return self.repository.refresh_run_totals(run.run_id)
        self.log("[D] reconcile_snapshot: 无变更，沿用原 run")
        return run

    def _artists_for_run(self, run: FollowCrawlRun) -> list[ArtistProgress]:
        if self.options.start_mode == START_RETRY_FAILED:
            return self.repository.list_artists(
                run.run_id,
                states=("running", "deferred", "failed"),
            )
        return self.repository.list_artists(
            run.run_id,
            states=ARTIST_RETRY_STATES,
            start_ordinal=run.start_ordinal,
        )

    def _artist_progress(self, run_id: str, artist_id: str) -> ArtistProgress | None:
        for progress in self.repository.list_artists(run_id):
            if progress.artist_id == artist_id:
                return progress
        return None

    def _fetch_following_snapshot(self, account_id: str) -> tuple[list[FollowArtistSnapshot], bool]:
        """Fetch the follow list, tolerating later-page failures.

        Returns ``(snapshots, complete)``.  ``complete`` is ``False`` when a
        subsequent page failed after retries; callers must not treat artists
        missing from a partial list as unfollowed.  A first-page failure
        still aborts, since no usable snapshot exists at all.
        """

        self.log("[*] 获取关注画师列表并建立稳定快照...")
        result = self.request_executor.call(
            "获取关注列表第 1 页",
            lambda: self.api.user_following(account_id),
        )
        snapshots: list[FollowArtistSnapshot] = []
        seen: set[str] = set()
        page = 1
        complete = True
        while result:
            previews = list(getattr(result, "user_previews", None) or [])
            for preview in previews:
                user = getattr(preview, "user", None)
                artist_id = str(getattr(user, "id", "") or "")
                if not artist_id or artist_id in seen:
                    continue
                seen.add(artist_id)
                snapshots.append(
                    FollowArtistSnapshot(
                        artist_id=artist_id,
                        artist_name=str(getattr(user, "name", "") or ""),
                    )
                )
            next_url = getattr(result, "next_url", None)
            if not next_url:
                break
            page += 1
            next_qs = self.api.parse_qs(next_url)
            try:
                result = self.request_executor.call(
                    f"获取关注列表第 {page} 页",
                    lambda qs=next_qs: self.api.user_following(**qs),
                )
            except FollowCrawlControl as exc:
                if exc.kind in _ABORT_SNAPSHOT_KINDS:
                    raise
                complete = False
                self.log(
                    f"[!] 关注列表第 {page} 页获取失败，使用部分快照："
                    f"{redact_error_message(exc)}"
                )
                break
        self.log(f"[*] 关注列表快照完成：{len(snapshots)} 位画师")
        return snapshots, complete

    def _resolve_start_ordinal(self, snapshots: list[FollowArtistSnapshot]) -> int:
        mode = self.options.start_mode
        value = self.options.start_value
        if mode in (START_RESUME, START_RESTART):
            return 1
        if mode == START_INDEX:
            if not value.isdigit():
                raise ValueError("指定序号必须是正整数")
            ordinal = int(value)
            if not 1 <= ordinal <= len(snapshots):
                raise ValueError(f"指定序号超出范围：1～{len(snapshots)}")
            return ordinal
        if mode == START_ARTIST:
            if not value.isdigit():
                raise ValueError("指定画师 ID 必须是数字")
            for ordinal, artist in enumerate(snapshots, start=1):
                if artist.artist_id == value:
                    return ordinal
            raise ValueError(f"关注列表中未找到画师 ID：{value}")
        return 1

    def _crawl_artist(
        self,
        run: FollowCrawlRun,
        artist: ArtistProgress,
        *,
        batch_queued: int,
    ) -> tuple[int, str]:
        traversal = _ArtistTraversal.from_progress(artist)
        while True:
            current_cursor = dict(traversal.cursor)
            result = self._fetch_artist_page(artist.artist_id, current_cursor)
            illusts = list(getattr(result, "illusts", None) or [])
            if not current_cursor and illusts:
                # Artist pages are newest-first: the first item of page 1 is
                # the incremental-sweep cutoff for future runs.  Record it on
                # every page-1 visit (fresh crawl AND mid-crawl resume), not
                # only when works_seen == 0 — a resumed artist otherwise keeps
                # a stale/empty cutoff and degrades to a page-1-only probe.
                newest = str(getattr(illusts[0], "id", "") or "")
                if newest and newest != artist.newest_illust_id:
                    self.repository.update_newest_illust(run.run_id, artist.artist_id, newest)
            self._process_artist_page(run, artist, current_cursor, traversal, illusts, batch_queued)

            next_url = getattr(result, "next_url", None)
            if not next_url:
                state = "empty" if traversal.works_seen == 0 else "completed"
                self.repository.mark_artist_completed(
                    run.run_id,
                    artist.artist_id,
                    works_seen=traversal.works_seen,
                    queued_items=traversal.queued_items,
                    state=state,
                )
                return traversal.newly_queued, state

            traversal.cursor = dict(self.api.parse_qs(next_url) or {})
            traversal.item_index = 0
            traversal.last_illust_id = ""
            self._checkpoint_traversal(run, artist, traversal)

    def _sweep_completed_artists(
        self,
        run: FollowCrawlRun,
        progress: _BatchProgress,
        *,
        exclude_ids: set[str],
    ) -> None:
        """Check previously completed artists for works published since.

        The sweep is intentionally lighter than a full crawl: it does not
        consume the per-batch artist limit (that limit governs full
        traversals), but it still honours cancellation, the download-queue
        budget and the API request budget.
        """

        candidates = [
            artist
            for artist in self.repository.list_artists_for_sweep(run.run_id)
            if artist.artist_id not in exclude_ids
        ]
        if not candidates:
            self.log("[*] 所有关注画师均为最新，无需增量更新")
            return
        self.log(f"[*] 增量检查 {len(candidates)} 位画师的新作品...")
        checked = 0
        new_works = 0
        for artist in candidates:
            if self.should_stop():
                progress.pause("用户停止任务")
                self.log(f"[!] 增量检查已停止（已检查 {checked}/{len(candidates)} 位）")
                return
            if progress.queued >= self.options.max_queued_items:
                progress.pause(f"已达到本批下载队列上限 {self.options.max_queued_items}")
                self.log(f"[!] 增量检查因队列上限暂停（已检查 {checked}/{len(candidates)} 位）")
                return
            try:
                queued = self._sweep_artist(run, artist, progress)
            except FollowCrawlControl as exc:
                # The artist keeps its completed/sweep state so the next
                # resume retries this sweep from page 1.
                progress.pause(str(exc))
                self.log(f"[!] 增量检查已暂停：{exc}；下次「智能续跑」继续")
                return
            except Exception as exc:
                self.log(
                    f"[!] 画师 {artist.artist_id} 增量检查失败，已跳过："
                    f"{redact_error_message(exc)}"
                )
                try:
                    self.repository.mark_artist_swept(
                        run.run_id,
                        artist.artist_id,
                        queued_delta=0,
                        newest_illust_id="",
                    )
                except Exception:
                    self.log("[!] 标记画师清扫状态失败，该画师下次将重新检查")
                continue
            checked += 1
            new_works += queued
            progress.queued += queued
            if queued:
                self.log(
                    f"[+] 画师 {artist.artist_id}（{artist.artist_name or '未知画师'}）"
                    f"发现 {queued} 个新作品"
                )
        self.log(
            f"[*] 增量检查完成：{checked}/{len(candidates)} 位画师，" f"共发现 {new_works} 个新作品"
        )

    def _sweep_artist(
        self, run: FollowCrawlRun, artist: ArtistProgress, progress: _BatchProgress
    ) -> int:
        """Queue works newer than the stored cutoff for one artist.

         结构（R7-1 重构）：原 C(18) 函数拆为3个 helper ——
         模块级 ``_find_sweep_cutoff``（cutoff 位置查找）+ 方法
         ``_queue_sweep_items``（作品入队）+ ``_advance_sweep_cursor``
        （翻页/终止判定）。主函数降为循环编排器。

        诊断日志（R7-4）：记录入口参数、每页处理结果、翻页/终止决策、
        最终入队统计，便于排查增量爬取链路问题。
        """

        cutoff = artist.newest_illust_id
        queued_total = 0
        newest_seen = ""
        cursor: dict = {}
        self.log(
            f"[D] sweep_artist 开始: run={run.run_id} artist={artist.artist_id} "
            f"cutoff={cutoff or '(无)'}"
        )
        for page_index in range(SWEEP_MAX_PAGES):
            result = self._fetch_artist_page(artist.artist_id, cursor)
            illusts = list(getattr(result, "illusts", None) or [])
            if not illusts:
                self.log(
                    f"[D] sweep_artist 页 {page_index + 1} 空响应，终止: "
                    f"artist={artist.artist_id}"
                )
                break
            if not newest_seen:
                newest_seen = str(getattr(illusts[0], "id", "") or "")

            stop_at = _find_sweep_cutoff(illusts, cutoff)
            self.log(
                f"[D] sweep_artist 页 {page_index + 1}: "
                f"illusts={len(illusts)} stop_at={stop_at} "
                f"{'(cutoff命中)' if stop_at < len(illusts) else '(cutoff未命中)'}"
            )
            queued_total += self._queue_sweep_items(illusts, stop_at, progress, queued_total)

            advanced, next_cursor = self._advance_sweep_cursor(result, cutoff, stop_at, illusts)
            if not advanced:
                self.log(
                    f"[D] sweep_artist 翻页终止: artist={artist.artist_id} "
                    f"page={page_index + 1} queued_total={queued_total}"
                )
                break
            cursor = next_cursor
            self.log(
                f"[D] sweep_artist 翻页: artist={artist.artist_id} "
                f"page={page_index + 1} → {page_index + 2} cursor={next_cursor}"
            )

        self.repository.mark_artist_swept(
            run.run_id,
            artist.artist_id,
            queued_delta=queued_total,
            newest_illust_id=newest_seen,
        )
        self.log(
            f"[D] sweep_artist 完成: artist={artist.artist_id} "
            f"queued={queued_total} newest_seen={newest_seen or '(空)'}"
        )
        return queued_total

    def _queue_sweep_items(
        self,
        illusts: list,
        stop_at: int,
        progress: _BatchProgress,
        queued_total: int,
    ) -> int:
        """处理 ``[0, stop_at)`` 范围的作品入队，返回新增的 queued 数量。"""
        delta = 0
        for index in range(stop_at):
            self._guard_sweep_item(progress, queued_total)
            outcome = self.process_illust(illusts[index], index)
            self._guard_sweep_item(progress, queued_total)
            item_queued = max(0, int(getattr(outcome, "queued", 0) or 0))
            delta += item_queued
            if item_queued:
                self.log(
                    f"[D] sweep_queue 入队: index={index} "
                    f"illust_id={getattr(illusts[index], 'id', '?')} "
                    f"delta={item_queued} total={queued_total + delta}"
                )
        return delta

    def _advance_sweep_cursor(
        self,
        result,
        cutoff: str,
        stop_at: int,
        illusts: list,
    ) -> tuple[bool, dict]:
        """判断是否继续翻页，返回 ``(continue, next_cursor)``。

        终止条件（按顺序短路）：
        1. cutoff 命中且 ``stop_at < len(illusts)``：已抵达上次覆盖范围；
        2. 无 cutoff：legacy 行仅探测首页；
        3. 无 ``next_url``：最后一页；
        4. ``parse_qs`` 返回空：游标解析失败。

        诊断日志（R7-4）：每个终止分支记录原因，便于排查翻页/停止逻辑。
        """
        if cutoff and stop_at < len(illusts):
            self.log(
                f"[D] advance_cursor 终止(覆盖范围): cutoff={cutoff} "
                f"stop_at={stop_at} < len={len(illusts)}"
            )
            return False, {}
        if not cutoff:
            self.log("[D] advance_cursor 终止(legacy无cutoff): 仅探测首页")
            return False, {}
        next_url = getattr(result, "next_url", None)
        if not next_url:
            self.log("[D] advance_cursor 终止(无next_url): 最后一页")
            return False, {}
        cursor = dict(self.api.parse_qs(next_url) or {})
        if not cursor:
            self.log(f"[D] advance_cursor 终止(parse_qs空): next_url={next_url}")
            return False, {}
        return True, cursor

    def _guard_sweep_item(self, progress: _BatchProgress, queued_total: int) -> None:
        if self.should_stop():
            raise ApiRequestCancelled("用户停止任务", kind="cancelled")
        if progress.queued + queued_total >= self.options.max_queued_items:
            raise FollowCrawlControl(
                f"已达到本批下载队列上限 {self.options.max_queued_items}",
                kind="queue_budget",
            )

    def _fetch_artist_page(self, artist_id: str, cursor: dict):
        if cursor:
            query = dict(cursor)
            query.setdefault("user_id", artist_id)
            return self.request_executor.call(
                f"获取画师 {artist_id} 作品分页",
                lambda qs=query: self.api.user_illusts(**qs),
            )
        return self.request_executor.call(
            f"获取画师 {artist_id} 作品首页",
            lambda: self.api.user_illusts(artist_id),
        )

    def _process_artist_page(
        self,
        run: FollowCrawlRun,
        artist: ArtistProgress,
        cursor: dict,
        traversal: _ArtistTraversal,
        illusts: list[object],
        batch_queued: int,
    ) -> None:
        start_at = self._resume_item_index(illusts, traversal.item_index, traversal.last_illust_id)
        for index in range(start_at, len(illusts)):
            self._guard_artist_item(run, artist, cursor, traversal, index, batch_queued)
            illust = illusts[index]
            outcome = self.process_illust(illust, traversal.works_seen)
            self._guard_artist_item(run, artist, cursor, traversal, index, batch_queued)
            traversal.works_seen += 1
            traversal.queued_items += max(0, int(getattr(outcome, "queued", 0) or 0))
            traversal.last_illust_id = str(getattr(illust, "id", "") or "")
            traversal.item_index = index + 1
            self._checkpoint_traversal(run, artist, traversal, cursor=cursor)

    def _guard_artist_item(
        self,
        run: FollowCrawlRun,
        artist: ArtistProgress,
        cursor: dict,
        traversal: _ArtistTraversal,
        item_index: int,
        batch_queued: int,
    ) -> None:
        if self.should_stop():
            traversal.item_index = item_index
            self._checkpoint_traversal(run, artist, traversal, cursor=cursor)
            raise ApiRequestCancelled("用户停止任务", kind="cancelled")
        if batch_queued + traversal.newly_queued >= self.options.max_queued_items:
            traversal.item_index = item_index
            self._checkpoint_traversal(run, artist, traversal, cursor=cursor)
            raise FollowCrawlControl(
                f"已达到本批下载队列上限 {self.options.max_queued_items}",
                kind="queue_budget",
            )

    def _checkpoint_traversal(
        self,
        run: FollowCrawlRun,
        artist: ArtistProgress,
        traversal: _ArtistTraversal,
        *,
        cursor: dict | None = None,
    ) -> None:
        self.repository.checkpoint_artist(
            run.run_id,
            artist.artist_id,
            cursor=traversal.cursor if cursor is None else cursor,
            item_index=traversal.item_index,
            last_illust_id=traversal.last_illust_id,
            works_seen=traversal.works_seen,
            queued_items=traversal.queued_items,
        )

    @staticmethod
    def _resume_item_index(illusts: list[object], item_index: int, last_illust_id: str) -> int:
        if last_illust_id:
            for index, illust in enumerate(illusts):
                if str(getattr(illust, "id", "") or "") == last_illust_id:
                    return index + 1
        return min(max(0, int(item_index)), len(illusts))

    def _publish_progress(
        self,
        run: FollowCrawlRun,
        artist: ArtistProgress,
        attempted: int,
        completed: int,
        queued: int,
        state: str,
    ) -> None:
        self.progress_callback(
            {
                "run_id": run.run_id,
                "total_artists": run.total_artists,
                "artist_ordinal": artist.ordinal,
                "artist_id": artist.artist_id,
                "artist_name": artist.artist_name,
                "attempted_artists": attempted,
                "completed_artists": completed,
                "queued_items": queued,
                "api_requests": self.request_executor.requests_used,
                "state": state,
            }
        )


__all__ = [
    "FollowCrawlOptions",
    "FollowCrawlService",
    "FollowCrawlSummary",
    "START_ARTIST",
    "START_INDEX",
    "START_RESTART",
    "START_RESUME",
    "START_RETRY_FAILED",
    "VALID_START_MODES",
    "build_follow_config_key",
]
