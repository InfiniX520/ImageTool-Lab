#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载服务门面：兼容旧入口，内部组合下载引擎与记录管理模块。"""

from __future__ import annotations

import os
from pathlib import Path

import requests  # noqa: F401  legacy monkeypatch/import seam

from core import record_manager
from core.auth_context import resolve_cookie_header
from core.download_engine import (  # noqa: F401  facade re-exports for legacy callers
    DEFAULT_REFERER,
    IMAGE_ACCEPT,
    APICallCounter,
    DownloadStopped,
    build_common_headers,
    build_original_url_for_page,
    build_save_path_for_page,
    download_file,
    fetch_original_meta,
    get_remote_content_length,
    is_original_image_url,
    sanitize_filename,
)
from core.legacy_record_utils import (  # noqa: F401  facade re-exports for legacy callers
    clear_download_records,
    load_downloaded_ids,
    save_downloaded_id,
    write_success_log,
)
from utils.logger import get_debug_mode as _get_logger_debug_mode
from utils.logger import (  # noqa: F401  facade re-export for legacy callers
    log_exception,
    log_message,
)
from utils.logger import set_debug_mode as _set_logger_debug_mode

# 兼容旧开关：默认关闭，避免与新记录体系重复。
ENABLE_LEGACY_SUCCESS_LOG = False
DEBUG_MODE = _get_logger_debug_mode()


def _validated_original_metadata(original_urls, known_page_count) -> dict[str, object] | None:
    """Validate complete App-API metadata before bypassing the Ajax fallback."""
    urls = tuple(str(value or "") for value in (original_urls or ()))
    if not urls or any(not is_original_image_url(value) for value in urls):
        return None
    page_count = record_manager.safe_int(known_page_count, len(urls)) or len(urls)
    if page_count <= 0 or page_count != len(urls):
        return None
    return {
        "original": urls[0],
        "original_urls": urls,
        "page_count": page_count,
    }


def set_debug_mode(enabled: bool) -> bool:
    """设置全局调试模式，并同步到底层日志模块。"""
    global DEBUG_MODE
    DEBUG_MODE = bool(enabled)
    _set_logger_debug_mode(DEBUG_MODE)
    return DEBUG_MODE


def download_image(
    url,
    save_path,
    log_callback=None,
    referer=DEFAULT_REFERER,
    retries=3,
    success_log_dir=None,
    illust_id=None,
    is_ai=None,
    bookmarks=None,
    create_date=None,
    page_index=None,
    title=None,
    artist_id=None,
    artist_name=None,
    download_root=None,
    folder_by_user=False,
    resume_enabled=False,
    original_enabled=False,
    multi_page=True,
    auth_cookie=None,
    user_agent=None,
    stop_event=None,
    _expand_pages=True,
    page_result_callback=None,
    original_urls=None,
    known_page_count=None,
):
    """下载单张图片，保留原有布尔返回契约与调用兼容性。

    ``page_result_callback`` 是可选观测边界：每个实际图片页完成、跳过
    或最终失败时收到一份脱敏结果，不参与下载控制流。

    结构（R6-1 重构）：主函数为编排器，复杂逻辑下沉到两个嵌套函数 ——
    ``_resolve_original_target`` 处理原图元数据与多页扇出，
    ``_perform_download`` 处理请求头构建与下载执行。闭包 ``report_page`` /
    ``record_event`` 捕获上下文保持内联（同 CQ-3 原则）。
    """
    # ``auth_cookie`` remains an explicit override for integrations, while
    # normal callers transparently use the browser export.  Resolve once per
    # invocation; recursive multi-page calls pass the resolved value through.
    auth_cookie = resolve_cookie_header(auth_cookie)
    page_idx = record_manager.safe_int(page_index, record_manager.extract_page_index(url)) or 0
    final_title = title or ""
    final_artist_id = str(artist_id or "")
    final_artist_name = artist_name or ""
    # Preserve the legacy no-directory behavior: a bare filename does not
    # implicitly create record files in the process working directory.
    final_download_root = download_root or success_log_dir or os.path.dirname(save_path)
    status = "下载失败"

    def report_page(page_status: str, *, retried: bool = False, final_url: str = "") -> None:
        if not callable(page_result_callback):
            return
        payload = {
            "illust_id": str(illust_id or ""),
            "page_index": page_idx,
            "status": page_status,
            "retried": bool(retried),
            "file_path": str(save_path),
            "url": str(final_url or ""),
        }
        try:
            page_result_callback(payload)
        except Exception:
            # 统计/展示回调不能让已经完成的文件被判定为失败。
            pass

    def record_event(original_url: str, event_status: str) -> None:
        """Persist one event through the single record boundary."""
        record_manager.record_download_event(
            final_download_root,
            folder_by_user,
            save_path,
            illust_id,
            page_idx,
            final_title,
            final_artist_id,
            final_artist_name,
            bookmarks,
            is_ai,
            create_date,
            original_url,
            event_status,
        )

    root_requires_page_audit = bool(
        original_enabled and multi_page and _expand_pages and page_idx == 0
    )

    def _resolve_original_target():
        """解析原图 URL 与多页扇出。

        Returns ``(target_url, early_return)``：
        - ``early_return is None``  → 继续下载，使用 ``target_url``
        - ``early_return is True``  → 跳过/扇出完成，主函数返回 ``True``
        - ``early_return is False`` → 错误，主函数返回 ``False``
        """
        nonlocal status
        target_url = url
        if not (original_enabled and illust_id):
            return target_url, None

        try:
            meta = _validated_original_metadata(original_urls, known_page_count)
            if meta is None:
                if not auth_cookie:
                    raise ValueError(
                        "原图模式缺少 Cookie，已中止下载。请将浏览器导出的 "
                        "www.pixiv.net_cookies.txt 放在程序目录。"
                    )
                log_message(
                    f"原图元数据未预置，请求 Ajax: illust_id={illust_id}",
                    "debug",
                    log_callback,
                )
                meta = fetch_original_meta(
                    illust_id,
                    log_callback=log_callback,
                    user_agent=user_agent,
                    cookie=auth_cookie,
                    stop_event=stop_event,
                )
            page_count = record_manager.safe_int(meta.get("page_count"), 1) or 1
            log_message(
                f"原图元数据: illust_id={illust_id} page_count={page_count} "
                f"source={'预置' if original_urls else 'Ajax'}",
                "debug",
                log_callback,
            )

            if _expand_pages and multi_page and page_idx == 0 and page_count > 1:
                log_message(
                    f"多页扇出: illust_id={illust_id} page_count={page_count} "
                    f"递归下载所有页",
                    "debug",
                    log_callback,
                )
                result_list = []
                for current_page in range(page_count):
                    page_save_path = build_save_path_for_page(save_path, current_page)
                    success = download_image(
                        url,
                        page_save_path,
                        log_callback=log_callback,
                        referer=referer,
                        retries=retries,
                        success_log_dir=success_log_dir,
                        illust_id=illust_id,
                        is_ai=is_ai,
                        bookmarks=bookmarks,
                        create_date=create_date,
                        page_index=current_page,
                        title=final_title,
                        artist_id=final_artist_id,
                        artist_name=final_artist_name,
                        download_root=final_download_root,
                        folder_by_user=folder_by_user,
                        resume_enabled=resume_enabled,
                        original_enabled=True,
                        multi_page=multi_page,
                        auth_cookie=auth_cookie,
                        user_agent=user_agent,
                        stop_event=stop_event,
                        _expand_pages=False,
                        page_result_callback=page_result_callback,
                        original_urls=meta.get("original_urls"),
                        known_page_count=page_count,
                    )
                    result_list.append(success)
                log_message(
                    f"多页扇出完成: illust_id={illust_id} "
                    f"success={sum(result_list)}/{page_count}",
                    "debug",
                    log_callback,
                )
                return target_url, all(result_list)

            if page_idx >= page_count:
                log_message(f"页码超出范围，跳过: {illust_id}_p{page_idx}", "warning", log_callback)
                status = "跳过"
                record_event("", status)
                report_page("skipped_policy")
                return target_url, True

            if not multi_page and page_idx > 0:
                status = "跳过"
                record_event("", status)
                report_page("skipped_policy")
                return target_url, True

            exact_urls = tuple(meta.get("original_urls") or ())
            if exact_urls:
                target_url = str(exact_urls[page_idx])
            else:
                original_url = str(meta["original"])
                if not is_original_image_url(original_url):
                    raise ValueError("官方原图接口返回的 URL 不是 img-original 地址")
                target_url = build_original_url_for_page(original_url, page_idx)
        except Exception as exception_obj:
            if is_original_image_url(url):
                if root_requires_page_audit:
                    log_message(
                        f"无法获取作品 {illust_id} 的官方页数，不能确认多页完整性: {exception_obj}",
                        "error",
                        log_callback,
                    )
                    record_event("", status)
                    report_page("failed")
                    return target_url, False
                log_message(
                    f"获取官方原图信息失败(可能需要R18鉴权): {exception_obj}；"
                    "保留已确认的 img-original 链接。",
                    "warning",
                    log_callback,
                )
            else:
                log_message(
                    f"获取官方原图地址失败，已拒绝降级到非原图链接: {exception_obj}",
                    "error",
                    log_callback,
                )
                record_event("", status)
                report_page("failed")
                return target_url, False
        return target_url, None

    def _perform_download(target_url):
        """构建请求头、执行下载并处理结果。Returns ``bool``。"""
        nonlocal status
        request_headers = build_common_headers(
            illust_id=illust_id, user_agent=user_agent, cookie=auth_cookie
        )
        request_headers["Accept"] = IMAGE_ACCEPT
        if referer and referer != DEFAULT_REFERER:
            request_headers["Referer"] = referer

        request_headers["Accept-Encoding"] = "identity"
        # The metadata probe must see the same representation as the download;
        # otherwise a compressed probe could report a different expected length.
        expected_size = get_remote_content_length(target_url, request_headers)
        # P1: a local file whose size happens to equal the remote length is not
        # trusted as complete.  Trusted historical skips remain the job of the
        # record_manager checks in the outer orchestration; every other case
        # performs a real, validated full download.
        retry_attempts = 0

        def note_retry(_attempt, _total, _exception) -> None:
            nonlocal retry_attempts
            retry_attempts += 1

        success, downloaded_size = download_file(
            target_url,
            save_path,
            request_headers,
            retries=retries,
            expected_size=expected_size,
            resume_enabled=resume_enabled,
            log_callback=log_callback,
            retry_callback=note_retry,
            stop_event=stop_event,
        )

        if success:
            size_mb = downloaded_size / 1024 / 1024
            log_message(
                f"下载成功: {Path(save_path).name} ({size_mb:.2f} MB)", "success", log_callback
            )

            if ENABLE_LEGACY_SUCCESS_LOG and success_log_dir:
                write_success_log(
                    success_log_dir,
                    Path(save_path).name,
                    target_url,
                    illust_id,
                    is_ai,
                    bookmarks,
                    create_date,
                )

            status = "下载完成"
            record_event(target_url, status)
            report_page("downloaded", retried=retry_attempts > 0, final_url=target_url)
            return True

        log_message(f"最终失败: {target_url}", "error", log_callback)
        record_event(target_url, status)
        report_page("failed", retried=retry_attempts > 0, final_url=target_url)
        return False

    # === 主流程编排 ===

    # 首次记录跳过检查：非多页首图在元数据解析前即可命中记录。
    if (
        resume_enabled
        and illust_id
        and not root_requires_page_audit
        and record_manager.check_record_skip(final_download_root, illust_id, page_idx)
    ):
        log_message(f"记录命中，跳过下载: {illust_id}_p{page_idx}", "info", log_callback)
        status = "跳过"
        record_event(url, status)
        report_page("skipped_record", final_url=url)
        return True

    log_message(
        f"download_image 入口: illust_id={illust_id} page={page_idx} "
        f"original={original_enabled} multi={multi_page} expand={_expand_pages}",
        "debug",
        log_callback,
    )

    # 解析原图目标 URL（含多页扇出）。
    target_url, early_return = _resolve_original_target()
    if early_return is not None:
        log_message(
            f"download_image 早返回: illust_id={illust_id} page={page_idx} "
            f"early_return={early_return}",
            "debug",
            log_callback,
        )
        return early_return

    # 二次记录跳过检查：多页作品在取得 pageCount 后再检查 p0，
    # 避免仅凭 p0 就被误判为整体完成。
    if (
        root_requires_page_audit
        and resume_enabled
        and illust_id
        and record_manager.check_record_skip(final_download_root, illust_id, page_idx)
    ):
        log_message(f"记录命中，跳过下载: {illust_id}_p{page_idx}", "info", log_callback)
        status = "跳过"
        record_event(target_url, status)
        report_page("skipped_record", final_url=target_url)
        return True

    # 执行下载。
    log_message(
        f"download_image 执行下载: illust_id={illust_id} page={page_idx} "
        f"target={target_url[:80]}{'...' if len(target_url) > 80 else ''}",
        "debug",
        log_callback,
    )
    return _perform_download(target_url)


# 初始化时同步一次调试开关。
set_debug_mode(DEBUG_MODE)
