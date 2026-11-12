"""Compatibility exports for the pre-2.0 ``core.pixiv_utils`` module.

New code should import the focused download, logging and record modules.  The
facade is intentionally thin so external scripts keep working during the
modular migration.
"""

from __future__ import annotations

import requests  # legacy monkeypatch/import seam

from core.download_engine import (
    ACCEPT_LANGUAGE,
    AJAX_ACCEPT,
    API_CALL_THRESHOLD_MAX,
    API_CALL_THRESHOLD_MIN,
    API_PAUSE_MAX,
    API_PAUSE_MIN,
    DEFAULT_REFERER,
    DEFAULT_USER_AGENT,
    IMAGE_ACCEPT,
    MAX_FILENAME_LENGTH,
    APICallCounter,
    DownloadEngine,
    DownloadRequest,
    build_common_headers,
    build_original_url_for_page,
    build_save_path_for_page,
    download_file,
    fetch_original_meta,
    get_remote_content_length,
    sanitize_filename,
)
from core.download_service import download_image
from core.download_service import set_debug_mode as _set_download_debug_mode
from core.legacy_record_utils import (
    check_user_folder_exists,
    clear_download_records,
    get_existing_user_folders,
    load_downloaded_ids,
    save_downloaded_id,
    write_success_log,
)
from core.record_manager import (
    ARTIST_ID_RECORD_FILE,
    CENTRAL_ID_RECORD_FILE,
    CENTRAL_LOG_DIR,
    CENTRAL_MONTHLY_DIR,
    CENTRAL_RECENT_MD_FILE,
    GLOBAL_ID_RECORD_FILE,
    GLOBAL_MD_RECORD_FILE,
    MAX_MD_ROWS,
    TRUST_COMPLETED_RECORD_WITHOUT_FILE,
    check_record_skip,
    configure_record_storage,
    flush_pending_markdown,
    record_download_event,
)
from utils.logger import (
    DEBUG_MODE,
    LOG_DEBUG,
    LOG_ERROR,
    LOG_INFO,
    LOG_SUCCESS,
    LOG_WARNING,
    get_debug_mode,
    log_exception,
    log_message,
)

HEADERS = {"User-Agent": DEFAULT_USER_AGENT, "Referer": DEFAULT_REFERER}
ENABLE_LEGACY_SUCCESS_LOG = False


def set_debug_mode(enabled: bool) -> bool:
    """Synchronize the legacy module flag with the focused logger service."""

    global DEBUG_MODE
    DEBUG_MODE = _set_download_debug_mode(enabled)
    return DEBUG_MODE


__all__ = [name for name in globals() if not name.startswith("_")]
