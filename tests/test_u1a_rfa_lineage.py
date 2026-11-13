"""RFA-04/05: one authority order and one bounded walk for successors.

Contract U1A_STATE_CONTRACT_V3 section 3 is the specification; these tests pin
it.  Every scenario uses synthetic SQLite rows and synthetic PNG bytes only:
no network, no account, no real gallery and no production database.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

import test_u1a_recovery as h
import test_u1a_reuse_gate as g
from test_u1a_rf_recovery import clone_delivery

from core.delivery_lineage import (
    MAX_LINEAGE_QUERIES,
    MAX_SUPERSEDE_NODES,
    WINDOW_LIMIT,
    LineageError,
    resolve_successor,
    resolve_successor_ex,
)


# ---------------------------------------------------------------------------
# Synthetic history helpers
# ---------------------------------------------------------------------------


def settle(repo, job):
    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"])


def handover(repo, root):
    """A cross-task reuse that records a recovery_owner_transfer event.

    This is the shape the audit used: task one completes page 0 and fails page
    1, task two holds the changed file, task one resumes and reuses its own
    older artifact.  The retired hold therefore records a handover to an
    artifact whose delivery row is older than the hold itself.
    """
    first = g.submit(repo, request_id="lineage-first")
    ids = g.accept(repo, first["batch_id"], g.source((g.candidate(pages=(0, 1)),)))
    transport = h.Transport(h.Response(), h.Response(status=404))
    assert h.run(repo, first, ids[0], h.executor(repo, root, transport)).state == "completed"
    assert h.run(repo, first, ids[1], h.executor(repo, root, transport)).state == "failed"
    settle(repo, first)
    h.target(root).write_bytes(h.SAME_SIZE_PNG)
    second, (hold,) = h.second_task(repo, root)
    settle(repo, second)
    resumed = repo.resume(first["job_id"], "lineage-resume")
    assert repo.activate_next()["batch_id"] == resumed["batch_id"]
    repo.carry_unresolved_items(resumed["batch_id"])
    h.target(root).write_bytes(h.PNG_BYTES)
    result = repo.recover_verification_page(resumed["batch_id"], h.ARTWORK, 0)
    assert result["outcome"] == "reused", result
    settle(repo, resumed)
    return first, second, hold, ids[0]


def row(conn, delivery_id):
    conn.row_factory = sqlite3.Row
    found = conn.execute(
        "SELECT rowid, * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
    ).fetchone()
    assert found is not None
    return found


def clone_completed(conn, source_delivery_id, new_delivery_id, new_artifact_id, *, generation):
    """Clone a completed delivery together with its artifact row."""
    clone_delivery(
        conn,
        source_delivery_id,
        new_delivery_id,
        state="completed",
        error_code=None,
        generation=generation,
    )
    columns = [item[1] for item in conn.execute("PRAGMA table_info(artifacts)")]
    values = conn.execute(
        "SELECT * FROM artifacts WHERE delivery_id = ?", (source_delivery_id,)
    ).fetchone()
    assert values is not None
    copied = dict(zip(columns, values))
    copied["artifact_id"] = new_artifact_id
    copied["delivery_id"] = new_delivery_id
    conn.execute(
        "INSERT INTO artifacts(" + ",".join(columns) + ") VALUES("
        + ",".join("?" for _ in columns)
        + ")",
        [copied[column] for column in columns],
    )


def chain(conn, source_delivery_id, count, *, prefix="c", generation=20):
    """Append count superseded rows newer than the source row."""
    created = []
    for index in range(count):
        new_id = f"{prefix}{index:031x}"
        clone_delivery(
            conn,
            source_delivery_id,
            new_id,
            state="cancelled",
            error_code="superseded_by_recovery_reuse",
            generation=generation + index,
        )
        created.append(new_id)
    return created


def insert_event(conn, *, job_id, batch_id, payload, kind="recovery_owner_transfer"):
    """Append one handover fact in the shape the recovery writer records."""
    text = payload if isinstance(payload, str) else json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )
    conn.execute(
        "INSERT INTO events(event_uuid, accepted_command_id, job_id, batch_id, "
        "kind, safe_payload, created_at) VALUES(?,?,?,?,?,?,?)",
        (
            uuid.uuid4().hex,
            None,
            job_id,
            batch_id,
            kind,
            text,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def artifact_id_of(repo, delivery_id):
    artifact = repo.get_artifact(delivery_id)
    assert artifact is not None
    return artifact["artifact_id"]


class _Trace:
    """Count every statement and every window scan of one resolution."""

    def __init__(self, conn):
        self._conn = conn
        self.statements = []
        self.scans = []

    def __enter__(self):
        def callback(sql):
            self.statements.append(sql)
            if "AND rowid > (" in sql:
                self.scans.append(sql)

        self._conn.set_trace_callback(callback)
        return self

    def __exit__(self, *exc_info):
        self._conn.set_trace_callback(None)
        return False

# ---------------------------------------------------------------------------
# RFA-04: the exact handover is authoritative
# ---------------------------------------------------------------------------


def test_explicit_handover_wins_over_a_newer_unrelated_pending(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, _, hold, completed = handover(repo, tmp_path)
        third = h.submit(repo, request_id="lineage-unrelated", dedup_policy="redownload")
        (pending,) = h.accept(
            repo,
            third["batch_id"],
            h.source((h.candidate(),), page_identity="page-third"),
        )
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            retired = row(conn, hold)
            assert row(conn, pending)["state"] == "pending"
            assert row(conn, pending)["rowid"] > retired["rowid"]
            result = resolve_successor_ex(conn, retired)
        print("LINEAGE_HANDOVER", result.reason, result.successor, result.queries, result.nodes)
        assert result.reason == "event", result
        assert result.successor is not None
        assert result.successor.delivery_id == completed
        assert result.successor.is_completed
        assert result.queries <= MAX_LINEAGE_QUERIES
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            legacy = resolve_successor(conn, row(conn, hold))
        assert legacy is not None and legacy.delivery_id == completed


def test_the_handover_target_may_predate_the_retired_row(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, _, hold, completed = handover(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            retired = row(conn, hold)
            target = row(conn, completed)
            assert target["rowid"] < retired["rowid"]
            window = conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE illust_id = ? AND page_index = 0 "
                "AND output_key = ? AND quality_key = ? AND rowid > ?",
                (
                    retired["illust_id"],
                    retired["output_key"],
                    retired["quality_key"],
                    retired["rowid"],
                ),
            ).fetchone()[0]
            assert window == 0, "rowid alone cannot express this handover"
            result = resolve_successor_ex(conn, retired)
        assert result.reason == "event", result
        assert result.successor is not None and result.successor.delivery_id == completed


def test_a_handover_without_an_artifact_falls_through_to_the_window(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, hold, _ = h.parked_hold(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE deliveries SET state = 'cancelled', error_code = ? "
                "WHERE delivery_id = ?",
                ("superseded_by_recovery_reuse", hold),
            )
            successor_id = clone_delivery(
                conn, hold, "a" * 32, state="pending", error_code=None, generation=50
            )
            insert_event(
                conn,
                job_id=job["job_id"],
                batch_id=job["batch_id"],
                payload={"delivery_id": hold, "artifact_id": None, "action": "successor_hold"},
            )
            conn.commit()
            result = resolve_successor_ex(conn, row(conn, hold))
        print("LINEAGE_NO_ARTIFACT", result.reason, result.successor, result.diagnostics)
        assert result.reason == "rowid_active", result
        assert result.successor is not None and result.successor.delivery_id == successor_id
        assert "event_without_artifact" in result.diagnostics


def _missing_artifact(conn, repo, context):
    insert_event(
        conn,
        job_id=context["job_id"],
        batch_id=context["batch_id"],
        payload={"delivery_id": context["anchor"], "artifact_id": "f" * 32, "action": "reuse"},
    )


def _corrupt_event(conn, repo, context):
    insert_event(
        conn,
        job_id=context["job_id"],
        batch_id=context["batch_id"],
        payload='{"delivery_id":"' + context["anchor"] + '"',
    )


def _corrupt_target(conn, repo, context):
    insert_event(
        conn,
        job_id=context["job_id"],
        batch_id=context["batch_id"],
        payload={"delivery_id": context["anchor"], "artifact_id": 5, "action": "reuse"},
    )


def _quality_mismatch(conn, repo, context):
    conn.execute(
        "UPDATE artifacts SET quality_json = ? WHERE artifact_id = ?",
        ('{"original": false, "validation_policy_version": 1}', context["artifact_id"]),
    )
    insert_event(
        conn,
        job_id=context["job_id"],
        batch_id=context["batch_id"],
        payload={
            "delivery_id": context["anchor"],
            "artifact_id": context["artifact_id"],
            "action": "reuse",
        },
    )


def _artifact_invalid(conn, repo, context):
    conn.execute(
        "UPDATE artifacts SET verification = 'legacy_import' WHERE artifact_id = ?",
        (context["artifact_id"],),
    )
    insert_event(
        conn,
        job_id=context["job_id"],
        batch_id=context["batch_id"],
        payload={
            "delivery_id": context["anchor"],
            "artifact_id": context["artifact_id"],
            "action": "reuse",
        },
    )


def _target_not_completed(conn, repo, context):
    conn.execute(
        "UPDATE artifacts SET delivery_id = ? WHERE artifact_id = ?",
        (context["pending"], context["artifact_id"]),
    )
    insert_event(
        conn,
        job_id=context["job_id"],
        batch_id=context["batch_id"],
        payload={
            "delivery_id": context["anchor"],
            "artifact_id": context["artifact_id"],
            "action": "reuse",
        },
    )


def _self_reference(conn, repo, context):
    insert_event(
        conn,
        job_id=context["job_id"],
        batch_id=context["batch_id"],
        payload={
            "delivery_id": context["completed"],
            "artifact_id": context["artifact_id"],
            "action": "reuse",
        },
    )


_UNUSABLE = {
    "missing_artifact": (_missing_artifact, "event:artifact_missing"),
    "corrupt_event": (_corrupt_event, "corrupt_event"),
    "corrupt_target": (_corrupt_target, "corrupt_event_target"),
    "quality_mismatch": (_quality_mismatch, "event:quality_mismatch"),
    "artifact_invalid": (_artifact_invalid, "event:artifact_invalid"),
    "target_not_completed": (_target_not_completed, "event:target_not_completed"),
    "self_reference": (_self_reference, "event:self_reference"),
}


@pytest.mark.parametrize("case", sorted(_UNUSABLE))
def test_an_unusable_handover_target_is_refused(case, tmp_path):
    prepare, expected = _UNUSABLE[case]
    with h.LibraryRepository(tmp_path / (case + ".sqlite3")) as repo:
        job, hold, completed = h.parked_hold(repo, tmp_path)
        settle(repo, job)
        third = h.submit(repo, request_id="third-" + case, dedup_policy="redownload")
        (pending,) = h.accept(
            repo,
            third["batch_id"],
            h.source((h.candidate(),), page_identity="page-" + case),
        )
        context = {
            "job_id": job["job_id"],
            "batch_id": job["batch_id"],
            "anchor": hold,
            "completed": completed,
            "pending": pending,
            "artifact_id": artifact_id_of(repo, completed),
        }
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            prepare(conn, repo, context)
            conn.commit()
            anchor = row(conn, completed if case == "self_reference" else hold)
            result = resolve_successor_ex(conn, anchor)
        print("LINEAGE_REFUSED", case, result.reason, result.diagnostics)
        assert result.successor is None, "an unusable target must not fall back to rowid"
        assert result.reason == "event_target_unusable", result
        assert expected in result.diagnostics, result.diagnostics


def test_the_first_usable_handover_wins_over_a_newer_unusable_one(tmp_path):
    """Contract 3.1: "the first usable target", newest events examined first.

    A later handover whose target disappeared must not erase the earlier,
    still-valid decision, and it must not be hidden either: the reason stays
    "event" and the unusable attempt is reported as a diagnostic.
    """
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, second, hold, completed = handover(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            insert_event(
                conn,
                job_id=second["job_id"],
                batch_id=second["batch_id"],
                payload={"delivery_id": hold, "artifact_id": "f" * 32, "action": "reuse"},
            )
            conn.commit()
            result = resolve_successor_ex(conn, row(conn, hold))
        print("LINEAGE_FIRST_USABLE", result.reason, result.successor, result.diagnostics)
        assert result.reason == "event", result
        assert result.successor is not None and result.successor.delivery_id == completed
        assert "event:artifact_missing" in result.diagnostics


def test_a_two_delivery_event_cycle_terminates_after_one_hop(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, hold, first = h.parked_hold(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            other_id = "b" * 32
            clone_completed(conn, first, other_id, "c" * 32, generation=70)
            anchor_artifact = artifact_id_of(repo, first)
            insert_event(
                conn,
                job_id=job["job_id"],
                batch_id=job["batch_id"],
                payload={"delivery_id": hold, "artifact_id": "c" * 32, "action": "reuse"},
            )
            insert_event(
                conn,
                job_id=job["job_id"],
                batch_id=job["batch_id"],
                payload={
                    "delivery_id": other_id,
                    "artifact_id": anchor_artifact,
                    "action": "reuse",
                },
            )
            conn.commit()
            result = resolve_successor_ex(conn, row(conn, hold))
            assert row(conn, other_id)["state"] == "completed"
        print("LINEAGE_CYCLE", result.reason, result.successor, result.queries)
        assert result.successor is not None
        assert result.successor.delivery_id == other_id
        assert result.reason == "event"
        assert result.queries <= MAX_LINEAGE_QUERIES, "a cycle must not be followed"

# ---------------------------------------------------------------------------
# RFA-05: one window, one visit per node
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("history", [8, 16, 24, 32, 64])
def test_a_supersede_chain_costs_one_window_read(history, tmp_path):
    with h.LibraryRepository(tmp_path / ("chain-" + str(history) + ".sqlite3")) as repo:
        _, anchor, _ = h.queued_repair(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            chain(conn, anchor, history)
            conn.commit()
            usable = row(conn, anchor)
            with _Trace(conn) as trace:
                result = resolve_successor_ex(conn, usable)
            total = len(trace.statements)
            scans = len(trace.scans)
            unique = len(set(trace.scans))
            print(
                "LINEAGE_COST",
                "history=" + str(history),
                "statements=" + str(total),
                "window_scans=" + str(scans),
                "unique_scans=" + str(unique),
                "nodes=" + str(result.nodes),
                "queries=" + str(result.queries),
                "reason=" + result.reason,
            )
        assert result.successor is None
        assert scans == 1, "the whole chain must be resolved by one window read"
        assert unique == 1, "the same window must not be re-read"
        assert total == result.queries <= MAX_LINEAGE_QUERIES
        assert result.nodes == min(history, WINDOW_LIMIT) <= MAX_SUPERSEDE_NODES
        if history < WINDOW_LIMIT:
            assert result.reason == "none", result.reason
            assert "superseded_only:" + str(history) in result.diagnostics
        else:
            assert result.reason == "budget_exhausted", result.reason
            assert "window_full" in result.diagnostics


def test_a_completed_row_wins_over_a_newer_active_row(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, anchor, first = h.queued_repair(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE deliveries SET state = 'failed', error_code = 'source_failed' "
                "WHERE delivery_id = ?",
                (anchor,),
            )
            clone_completed(conn, first, "d" * 32, "e" * 32, generation=80)
            clone_delivery(
                conn, anchor, "a" * 32, state="pending", error_code=None, generation=81
            )
            conn.commit()
            result = resolve_successor_ex(conn, row(conn, anchor))
        print("LINEAGE_PRECEDENCE", result.reason, result.successor)
        assert result.successor is not None
        assert result.successor.delivery_id == "d" * 32
        assert result.reason == "rowid_completed"


def test_a_completed_row_behind_a_retired_hold_reports_the_chain(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, anchor, first = h.queued_repair(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            clone_completed(conn, first, "d" * 32, "e" * 32, generation=90)
            chain(conn, anchor, 1, prefix="a", generation=91)
            conn.commit()
            result = resolve_successor_ex(conn, row(conn, anchor))
        assert result.successor is not None
        assert result.successor.delivery_id == "d" * 32
        assert result.reason == "supersede_chain", result.reason


def test_a_completed_row_without_an_artifact_is_not_a_successor(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, anchor, first = h.queued_repair(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            clone_delivery(
                conn, first, "d" * 32, state="completed", error_code=None, generation=95
            )
            conn.commit()
            result = resolve_successor_ex(conn, row(conn, anchor))
        assert result.successor is None
        assert result.reason == "none"


def test_the_window_is_scoped_to_the_page_identity(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, anchor, first = h.queued_repair(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            clone_completed(conn, first, "d" * 32, "e" * 32, generation=96)
            conn.execute(
                "UPDATE deliveries SET page_index = 1, output_ref_json = ? "
                "WHERE delivery_id = ?",
                (
                    json.dumps(
                        {
                            "kind": "project",
                            "root_id": "project",
                            "relative_path": "downl_Pixiv/123_p1.png",
                        }
                    ),
                    "d" * 32,
                ),
            )
            conn.commit()
            result = resolve_successor_ex(conn, row(conn, anchor))
        assert result.successor is None, "another page is not this page's successor"
        assert result.reason == "none"


def test_a_deep_chain_is_still_refused_by_the_recovery_entry_point(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            created = chain(conn, pending, 6)
            conn.execute(
                "UPDATE batch_item_pages SET delivery_id = ? WHERE batch_id = ? "
                "AND illust_id = ? AND page_index = 0",
                (created[0], job["batch_id"], h.ARTWORK),
            )
            conn.commit()
        result = repo.recover_verification_page(job["batch_id"], h.ARTWORK, 0)
        assert result["outcome"] == "refused"
        assert result["reason"] == "superseded_unresolved", result


def test_the_legacy_hops_argument_is_validated(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, anchor, _ = h.queued_repair(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            usable = row(conn, anchor)
            with pytest.raises(LineageError):
                resolve_successor(conn, usable, hops=-1)
            first = resolve_successor(conn, usable, hops=0)
            deep = resolve_successor(conn, usable, hops=MAX_SUPERSEDE_NODES)
            assert deep == first, "depth no longer changes the answer"


def test_the_statement_budget_is_enforced(tmp_path, monkeypatch):
    import core.delivery_lineage as module

    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, anchor, first = h.queued_repair(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            clone_completed(conn, first, "d" * 32, "e" * 32, generation=97)
            conn.commit()
            monkeypatch.setattr(module, "MAX_LINEAGE_QUERIES", 1)
            result = resolve_successor_ex(conn, row(conn, anchor))
        print("LINEAGE_BUDGET", result.reason, result.diagnostics, result.queries)
        assert result.successor is None
        assert result.reason == "budget_exhausted"
        assert "statement_budget" in result.diagnostics
        assert result.queries == 1


def test_preflight_and_write_phase_agree_and_then_see_a_change(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, anchor, first = h.queued_repair(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            usable = row(conn, anchor)
            preflight = resolve_successor_ex(conn, usable)
            write_phase = resolve_successor_ex(conn, usable)
            assert preflight == write_phase, "no result may be cached across calls"
            assert preflight.successor is None
            clone_completed(conn, first, "d" * 32, "e" * 32, generation=98)
            conn.commit()
            after = resolve_successor_ex(conn, usable)
        print("LINEAGE_TWO_PHASE", preflight.reason, after.reason, after.successor)
        assert after.successor is not None
        assert after.successor.delivery_id == "d" * 32
        assert after != preflight, "a candidate that appeared in between must be visible"
