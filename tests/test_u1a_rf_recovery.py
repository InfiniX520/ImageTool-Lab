"""RF-02/04/05 regressions: successors, held semantics and evidence binding."""
from __future__ import annotations

import sqlite3

import pytest

import test_u1a_recovery as h
import test_u1a_reuse_gate as g
from core.library_repository import LibraryRepository


def settle(repo, job):
    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"])


def foreign_hold(repo, root):
    first = g.submit(repo, request_id="first-two")
    ids = g.accept(repo, first["batch_id"], g.source((g.candidate(pages=(0, 1)),)))
    transport = h.Transport(h.Response(), h.Response(status=404))
    assert h.run(repo, first, ids[0], h.executor(repo, root, transport)).state == "completed"
    assert h.run(repo, first, ids[1], h.executor(repo, root, transport)).state == "failed"
    settle(repo, first)
    h.target(root).write_bytes(h.SAME_SIZE_PNG)
    second, (hold,) = h.second_task(repo, root)
    settle(repo, second)
    resumed = repo.resume(first["job_id"], "resume-first")
    assert repo.activate_next()["batch_id"] == resumed["batch_id"]
    assert repo.carry_unresolved_items(resumed["batch_id"])["count"] == 1
    return first, second, resumed, hold


def clone_delivery(conn, source_id, new_id, **changes):
    columns = [row[1] for row in conn.execute("PRAGMA table_info(deliveries)")]
    values = conn.execute(
        "SELECT * FROM deliveries WHERE delivery_id = ?", (source_id,)
    ).fetchone()
    row = dict(zip(columns, values))
    row.update(changes)
    row["delivery_id"] = new_id
    conn.execute(
        "INSERT INTO deliveries(" + ",".join(columns) + ") VALUES("
        + ",".join("?" for _ in columns) + ")",
        [row[column] for column in columns],
    )
    return new_id


def link_page_to(conn, batch_id, delivery_id):
    conn.execute(
        "UPDATE batch_item_pages SET delivery_id = ?, artifact_id = NULL "
        "WHERE batch_id = ? AND illust_id = ? AND page_index = 0",
        (delivery_id, batch_id, h.ARTWORK),
    )


def test_negative_evidence_changes_no_row(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, hold, _ = h.parked_hold(repo, tmp_path)
        h.target(tmp_path).unlink()
        observe = repo._observe_artifact

        def altered(*args, **kwargs):
            record = observe(*args, **kwargs)
            from dataclasses import replace

            return replace(
                record,
                expectation=replace(record.expectation, artifact_id="nonexistent-artifact"),
            )

        monkeypatch.setattr(repo, "_observe_artifact", altered)
        with sqlite3.connect(repo.database_path) as conn:
            def snapshot():
                return {
                    table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                    for table in ("deliveries", "artifacts", "batch_item_pages", "batch_items")
                }
            before = snapshot()
        result = repo.recover_verification_page(job["batch_id"], h.ARTWORK, 0)
        with sqlite3.connect(repo.database_path) as conn:
            after = {
                table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in ("deliveries", "artifacts", "batch_item_pages", "batch_items")
            }
        assert result["outcome"] == "refused"
        assert before == after


def test_successor_in_transfer_is_not_stolen(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            old = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (pending,)
            ).fetchone()
            cancelled_id = clone_delivery(
                conn, pending, "c" * 32, state="cancelled",
                error_code="superseded_by_recovery_reuse", generation=old["generation"] + 1,
            )
            conn.execute(
                "UPDATE deliveries SET state = 'failed' WHERE delivery_id = ?", (pending,)
            )
            inflight_id = clone_delivery(
                conn, pending, "d" * 32, state="transferring", error_code=None,
                generation=old["generation"] + 2,
            )
            link_page_to(conn, job["batch_id"], cancelled_id)
            conn.commit()
        result = repo.recover_verification_page(job["batch_id"], h.ARTWORK, 0)
        assert result["outcome"] == "refused"
        assert result["reason"] == "output_blocked_by_other_task"
        with sqlite3.connect(repo.database_path) as conn:
            link = conn.execute(
                "SELECT delivery_id, artifact_id FROM batch_item_pages "
                "WHERE batch_id = ? AND illust_id = ? AND page_index = 0",
                (job["batch_id"], h.ARTWORK),
            ).fetchone()
            state = conn.execute(
                "SELECT state FROM deliveries WHERE delivery_id = ?", (inflight_id,)
            ).fetchone()[0]
        assert link[0] == cancelled_id and link[1] is None
        assert state == "transferring"


def test_deep_supersede_chain_is_treated_as_unresolved(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            previous = pending
            first = None
            for index in range(6):
                new_id = f"{index + 1:032x}"
                clone_delivery(
                    conn, pending, new_id, state="cancelled",
                    error_code="superseded_by_recovery_reuse",
                    generation=100 + index,
                )
                if first is None:
                    first = new_id
                previous = new_id
            link_page_to(conn, job["batch_id"], first)
            conn.commit()
        result = repo.recover_verification_page(job["batch_id"], h.ARTWORK, 0)
        assert result["outcome"] == "refused"
        assert result["reason"] == "superseded_unresolved"


def test_a_pending_successor_is_adopted_without_a_new_generation(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            old = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (pending,)
            ).fetchone()
            cancelled_id = clone_delivery(
                conn, pending, "e" * 32, state="cancelled",
                error_code="superseded_by_recovery_reuse",
                generation=old["generation"] + 1,
            )
            conn.execute(
                "UPDATE deliveries SET state = 'failed' WHERE delivery_id = ?", (pending,)
            )
            successor_id = clone_delivery(
                conn, pending, "f" * 32, state="pending", error_code=None,
                generation=old["generation"] + 2,
            )
            link_page_to(conn, job["batch_id"], cancelled_id)
            conn.commit()
            before = conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        result = repo.recover_verification_page(job["batch_id"], h.ARTWORK, 0)
        assert result["outcome"] == "repair_queued", result
        assert result["delivery_id"] == successor_id
        with sqlite3.connect(repo.database_path) as conn:
            link = conn.execute(
                "SELECT delivery_id FROM batch_item_pages WHERE batch_id = ? "
                "AND illust_id = ? AND page_index = 0",
                (job["batch_id"], h.ARTWORK),
            ).fetchone()[0]
            after = conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        assert link == successor_id
        assert after == before, "adopting a successor must not create another generation"
