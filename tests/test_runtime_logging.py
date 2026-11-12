"""Runtime session log persistence contracts."""

from __future__ import annotations

import time

from utils.logger import SessionLogQueue


def test_session_log_queue_rotates_and_keeps_queue_contract(tmp_path) -> None:
    log_queue = SessionLogQueue(tmp_path, max_bytes=1024, backup_count=2)
    try:
        for index in range(80):
            log_queue.put(f"[+] message-{index}-" + "x" * 40)
        log_queue.flush()

        assert log_queue.get_nowait().startswith("[+] message-0")
        assert log_queue.session_path.exists()
        assert len(list(tmp_path.glob(f"{log_queue.session_path.name}*"))) <= 3
        assert "message-79" in log_queue.read_session_text()
    finally:
        log_queue.close_log()


def test_session_log_flushes_small_batches_periodically(tmp_path) -> None:
    log_queue = SessionLogQueue(tmp_path, flush_interval=0.05)
    try:
        log_queue.put("[*] survives-without-50-lines")
        deadline = time.monotonic() + 1.0
        persisted = ""
        while time.monotonic() < deadline:
            persisted = log_queue.session_path.read_text(encoding="utf-8")
            if "survives-without-50-lines" in persisted:
                break
            time.sleep(0.01)
        assert "survives-without-50-lines" in persisted
    finally:
        log_queue.close_log()
