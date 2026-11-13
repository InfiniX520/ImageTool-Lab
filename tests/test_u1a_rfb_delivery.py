"""RFB-03/RFB-05: bounded cleanup scans and ledger-fault classification.

Every case is synthetic: one temporary ledger, temporary output root, synthetic
PNG bytes and the real executor.  No network, account or real gallery is used.

Contract v4 under test:

* section 4: a cleanup scan reports EOF and budget exhaustion separately, each
  tier has its own cursor, and only a proven EOF wraps a cursor;
* section 5: a readable ledger with a refused compensation write is reported as
  recovery_needed, while an unreadable ledger raises the typed storage fault.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import test_u1a_recovery as h
import test_u1a_rfa_delivery as d
from core.library_repository import (
    CorruptDataError,
    InvalidStateError,
    LibraryRepository,
)
from core.page_delivery import _Fault, _StorageFault


def _bad_prefix(repo, source, count):
    """Clone cancelled rows in front of the queue with unusable evidence."""
    idents = []
    with sqlite3.connect(repo.database_path) as conn:
        conn.row_factory = sqlite3.Row
        for index in range(count):
            ident = f"{index + 1:032x}"
            d.clone_cancelled(conn, source, ident, index + 2)
            conn.execute(
                "UPDATE deliveries SET prepared_json='{invalid' WHERE delivery_id=?",
                (ident,),
            )
            idents.append(ident)
        conn.commit()
    return idents


def _append_good_row(repo, source, ident):
    with sqlite3.connect(repo.database_path) as conn:
        conn.row_factory = sqlite3.Row
        d.clone_cancelled(conn, source, ident, 9000)
        conn.commit()


@pytest.mark.parametrize("limit", [1, 200])
def test_the_public_window_reports_a_capped_scan_not_an_empty_queue(tmp_path, limit):
    """A full examination budget is not evidence that the queue is empty."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        first, service = d.cancelled(repo, root)
        assert service.cleanup_delivery(first)
        cap = min(limit * 5, 2000)
        _bad_prefix(repo, first, 2 * cap + 1)
        stats = {}
        facts = repo.list_cleanup_candidates(limit=limit, stats=stats)
        assert facts == ()
        assert stats["examined"] == cap
        assert stats["skipped"] == cap
        assert stats["tier0_eof"] is False
        assert stats["tier0_budget_exhausted"] is True
        assert stats["next_rowid"] > 0, "the cursor must pass the rows it examined"


@pytest.mark.parametrize("limit", [1, 200])
def test_a_bad_prefix_beyond_the_cap_still_reaches_the_tail(tmp_path, monkeypatch, limit):
    """Five bounded sweeps must reach the valid row behind the bad prefix."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        first, service = d.cancelled(repo, root)
        assert service.cleanup_delivery(first)
        cap = min(limit * 5, 2000)
        _bad_prefix(repo, first, 2 * cap + 1)
        tail = "f" * 32
        _append_good_row(repo, first, tail)
        visits = []
        real = service.cleanup_cancelled_delivery

        def counted(delivery_id):
            visits.append(delivery_id)
            return real(delivery_id)

        monkeypatch.setattr(service, "cleanup_cancelled_delivery", counted)
        cursors = []
        for _ in range(5):
            service.recover_local(limit=limit)
            cursors.append(service._cleanup_cursor)
        print("CAPPED_PREFIX", limit, visits, cursors)
        assert tail in visits, "the valid row behind the bad prefix is starved"
        # Every sweep before the tail is found advances monotonically; only the
        # sweep that proved EOF may wrap to zero.
        first_zero = cursors.index(0)
        assert all(a <= b for a, b in zip(cursors[:first_zero], cursors[1:first_zero]))
        assert cursors[first_zero - 1] > 0


def test_each_tier_keeps_its_own_cursor_and_budget(tmp_path):
    """Fresh and retried rows page independently and report their own state."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        first, service = d.cancelled(repo, root)
        assert service.cleanup_delivery(first)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            fresh = [
                d.clone_cancelled(conn, first, f"{index + 1:032x}", index + 2)
                for index in range(10)
            ]
            retried = d.clone_cancelled(conn, first, "e" * 32, 500)
            d.insert_cleanup_event(
                conn,
                retried,
                d.cleanup_event_payload(d.fact_for(conn, retried), outcome="failed"),
            )
            conn.commit()
        stats = {}
        facts = repo.list_cleanup_candidates(limit=8, stats=stats)
        offered = [fact["delivery_id"] for fact in facts]
        print("TIERS", offered, stats)
        assert stats["reserve"] == 1
        assert retried in offered
        assert len([item for item in offered if item in fresh]) == 7
        assert stats["tier0_eof"] is False
        with sqlite3.connect(repo.database_path) as conn:
            last_fresh_rowid = conn.execute(
                "SELECT rowid FROM deliveries WHERE delivery_id = ?", (fresh[6],)
            ).fetchone()[0]
            retried_rowid = conn.execute(
                "SELECT rowid FROM deliveries WHERE delivery_id = ?", (retried,)
            ).fetchone()[0]
        assert stats["next_rowid"] == last_fresh_rowid
        assert stats["next_retry_rowid"] == retried_rowid
        # The fresh tier alone exceeds one window, so its budget is not spent.
        assert stats["tier0_budget_exhausted"] is False


def test_a_cursor_wraps_only_after_a_proven_end_of_queue(tmp_path):
    """The sweep restarts from zero only when it actually reached the tail."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        first, service = d.cancelled(repo, root)
        service.recover_local(limit=200)
        assert service._cleanup_cursor == 0
        assert service._cleanup_retry_cursor == 0
        second, _ = d.cancelled(repo, root, "456", "second")
        service.recover_local(limit=200)
        assert d.cleanup_events(repo, second)[-1]["outcome"] == "cleaned"


def test_a_readable_ledger_with_a_refused_compensation_stays_recoverable(tmp_path):
    """A refused compensation write is a result, not a storage fault."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, (delivery,) = d.prepared(repo, root, "refused-compensation")

        def refuse(*args, **kwargs):
            raise InvalidStateError("synthetic compensation refusal")

        repo.set_delivery_issue = refuse

        def fail(stage, row):
            if stage == "before_publish":
                raise _Fault("synthetic local failure")

        service = h.executor(repo, root, h.Transport(), fault_hook=fail)
        result = service.recover_local()
        print("REFUSED_COMPENSATION", result, repo.get_delivery(delivery)["state"])
        assert result == ((delivery, "recovery_needed"),)
        assert repo.get_delivery(delivery)["state"] == "prepared"


def test_an_unreadable_ledger_stops_recovery_and_keeps_every_byte(tmp_path, monkeypatch):
    """The public loop must raise, not report an ordinary recoverable row."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, (delivery,) = d.prepared(repo, root, "unreadable-ledger")
        parts = list(root.rglob("*.part"))
        assert len(parts) == 1
        before = parts[0].read_bytes()

        def unreadable(*args, **kwargs):
            raise CorruptDataError("synthetic publication ledger is unreadable")

        monkeypatch.setattr(repo, "publish_authorized", unreadable)
        with pytest.raises(_StorageFault):
            h.executor(repo, root, h.Transport()).recover_local()
        print(
            "UNREADABLE_LEDGER",
            repo.get_delivery(delivery)["state"],
            len(parts),
            parts[0].exists(),
        )
        assert repo.get_delivery(delivery)["state"] == "prepared"
        assert parts[0].exists() and parts[0].read_bytes() == before
        assert d.cleanup_events(repo, delivery) == []


def test_cleanup_failures_never_delete_a_published_or_foreign_file(tmp_path, monkeypatch):
    """A refused cleanup leaves both the final image and a foreign part."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        original_unlink = Path.unlink

        def locked(path, *args, **kwargs):
            if path.suffix == ".part":
                raise PermissionError("synthetic sharing violation")
            return original_unlink(path, *args, **kwargs)

        with monkeypatch.context() as scope:
            scope.setattr(Path, "unlink", locked)
            first, service = d.cancelled(repo, root)
        part = next(root.rglob("*.part"))
        part.write_bytes(b"foreign-bytes")
        assert service.cleanup_delivery(first) is False
        assert part.read_bytes() == b"foreign-bytes"
        final = h.target(root)
        final.write_bytes(h.PNG_BYTES)
        assert service.cleanup_delivery(first) is False
        assert final.read_bytes() == h.PNG_BYTES
