"""RFB-04: the bounded node set keeps every recorded handover edge.

Contract U1A_STATE_CONTRACT_V4 section 7 is the specification.  The resolver
must read the handover events of the whole bounded node set, not only of the
anchor: an intermediate superseded row may point at a completed delivery whose
rowid is older than the anchor, and dropping that edge made the public recovery
entry refuse a legal history.

Every scenario is synthetic: temporary SQLite rows and synthetic PNG bytes, no
network, no account, no real gallery and no production database.
"""
from __future__ import annotations

import sqlite3

import pytest

import test_u1a_recovery as h
from test_u1a_rf_recovery import clone_delivery
from test_u1a_rfa_lineage import (
    artifact_id_of,
    chain,
    clone_completed,
    insert_event,
    row,
    settle,
)

from core.delivery_lineage import (
    MAX_EVENTS,
    MAX_LINEAGE_QUERIES,
    MAX_SUPERSEDE_NODES,
    WINDOW_LIMIT,
    resolve_successor_ex,
)


# ---------------------------------------------------------------------------
# Synthetic history helpers
# ---------------------------------------------------------------------------


def retire(conn, delivery_id, *, code="superseded_by_recovery_repair"):
    conn.execute(
        "UPDATE deliveries SET state='cancelled', error_code=? WHERE delivery_id=?",
        (code, delivery_id),
    )


def middle(conn, anchor, delivery_id, *, generation=2, code="superseded_by_recovery_reuse"):
    clone_delivery(
        conn,
        anchor,
        delivery_id,
        state="cancelled",
        error_code=code,
        generation=generation,
    )
    return delivery_id


def handover_event(conn, job, node, artifact_id, *, action="reuse"):
    insert_event(
        conn,
        job_id=job["job_id"],
        batch_id=job["batch_id"],
        payload={"delivery_id": node, "artifact_id": artifact_id, "action": action},
    )


class _Trace:
    """Count every statement and every window scan of one resolution."""

    def __init__(self, conn):
        self._conn = conn
        self.statements: list[str] = []
        self.scans: list[str] = []

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


def three_layer(repo, root):
    """C completed (older) <- B superseded with a handover <- A superseded.

    A is the second task's queued repair; B is a newer superseded clone of A
    that recorded the reuse handover to C's artifact.  A has no event of its
    own, so only the intermediate node can decide the page.
    """
    job, anchor, completed = h.queued_repair(repo, root)
    artifact = artifact_id_of(repo, completed)
    with sqlite3.connect(repo.database_path) as conn:
        conn.row_factory = sqlite3.Row
        retire(conn, anchor)
        node = middle(conn, anchor, "c" * 32, generation=3)
        handover_event(conn, job, node, artifact)
        conn.commit()
    return job, anchor, completed, artifact, "c" * 32


# ---------------------------------------------------------------------------
# The audit probe, with one extra intermediate node
# ---------------------------------------------------------------------------


def test_a_middle_handover_decides_the_anchor_and_public_recovery_reuses(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, anchor, completed = h.queued_repair(repo, tmp_path)
        artifact = artifact_id_of(repo, completed)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            retire(conn, anchor)
            # An intermediate node with no event of its own, then the one that
            # recorded the handover.  Both are newer than the anchor.
            middle(conn, anchor, "b" * 32, generation=2)
            node = middle(conn, anchor, "c" * 32, generation=3)
            handover_event(conn, job, node, artifact)
            conn.commit()
            result = resolve_successor_ex(conn, row(conn, anchor))
            direct = resolve_successor_ex(conn, row(conn, node))
        print("RFB_MIDDLE_HANDOVER", result.reason, result.successor, result.queries, result.nodes)
        assert result.successor is not None
        assert result.successor.delivery_id == completed
        assert result.reason == "supersede_chain", result
        assert "via:" + "c" * 32 in result.diagnostics
        assert result.queries <= MAX_LINEAGE_QUERIES
        assert direct.successor is not None and direct.successor.delivery_id == completed
        # The public entry must follow the same retained chain.
        h.target(tmp_path).write_bytes(h.PNG_BYTES)
        recovery = repo.recover_verification_page(job["batch_id"], h.ARTWORK, 0)
        print("RFB_MIDDLE_RECOVERY", recovery)
        assert recovery["outcome"] == "reused", recovery
        assert recovery["artifact_id"] == artifact


def test_one_step_resolution_cannot_disagree_with_the_window(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, anchor, completed, artifact, node = three_layer(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            first = resolve_successor_ex(conn, row(conn, anchor))
            second = resolve_successor_ex(conn, row(conn, anchor))
            # A brand new completed row on the same page must win afterwards:
            # nothing is cached across calls.
            newer = "d" * 32
            clone_completed(conn, completed, newer, "e" * 32, generation=99)
            conn.commit()
            third = resolve_successor_ex(conn, row(conn, anchor))
        print("RFB_TWO_STEP", first.successor, second.successor, third.successor)
        assert first.successor == second.successor, "two calls must agree"
        assert first.successor is not None and first.successor.delivery_id == completed
        assert third.successor is not None and third.successor.delivery_id == newer
        assert third.reason == "rowid_completed", third


# ---------------------------------------------------------------------------
# Refusals: a named but unusable edge is never replaced by another row
# ---------------------------------------------------------------------------


def test_an_unusable_middle_handover_is_refused_not_replaced(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, anchor, completed = h.queued_repair(repo, tmp_path)
        artifact = artifact_id_of(repo, completed)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            retire(conn, anchor)
            usable = middle(conn, anchor, "b" * 32, generation=2)
            handover_event(conn, job, usable, artifact)
            newest = middle(conn, anchor, "c" * 32, generation=3)
            # The newest node names an artifact that no longer exists; that is
            # an explicit decision that cannot be carried out, so the whole
            # resolution must refuse instead of falling back to "b".
            handover_event(conn, job, newest, "f" * 32)
            conn.commit()
            result = resolve_successor_ex(conn, row(conn, anchor))
        print("RFB_MIDDLE_UNUSABLE", result.reason, result.successor, result.diagnostics)
        assert result.successor is None, result
        assert result.reason == "event_target_unusable", result
        assert "event:artifact_missing" in result.diagnostics


def test_a_middle_handover_into_another_quality_scope_is_refused(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, anchor, completed = h.queued_repair(repo, tmp_path)
        artifact = artifact_id_of(repo, completed)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            retire(conn, anchor)
            node = middle(conn, anchor, "c" * 32, generation=2)
            handover_event(conn, job, node, artifact)
            conn.execute(
                "UPDATE deliveries SET quality_key = ? WHERE delivery_id = ?",
                ("0" * 64, completed),
            )
            conn.commit()
            result = resolve_successor_ex(conn, row(conn, anchor))
        print("RFB_MIDDLE_SCOPE", result.reason, result.successor, result.diagnostics)
        assert result.successor is None
        assert result.reason == "event_target_unusable", result
        assert "event:quality_key_mismatch" in result.diagnostics


def test_a_middle_handover_to_a_non_completed_owner_is_refused(tmp_path):
    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, anchor, completed = h.queued_repair(repo, tmp_path)
        artifact = artifact_id_of(repo, completed)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            retire(conn, anchor)
            middle(conn, anchor, "b" * 32, generation=2)
            newest = middle(conn, anchor, "c" * 32, generation=3)
            handover_event(conn, job, newest, artifact)
            # The named artifact still exists, but its owning delivery is no
            # longer completed: the edge cannot be carried out, so the walk
            # must refuse instead of inventing another successor.
            conn.execute(
                "UPDATE deliveries SET state='cancelled' WHERE delivery_id = ?",
                (completed,),
            )
            conn.commit()
            usable = row(conn, anchor)
            with _Trace(conn) as trace:
                result = resolve_successor_ex(conn, usable)
            statements = len(trace.statements)
        print("RFB_MIDDLE_NOT_COMPLETED", result.reason, result.successor, result.queries, statements)
        assert result.successor is None
        assert result.reason == "event_target_unusable", result
        assert "event:target_not_completed" in result.diagnostics
        assert statements == result.queries <= MAX_LINEAGE_QUERIES
        assert result.nodes <= MAX_SUPERSEDE_NODES


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_the_statement_budget_stops_before_a_middle_edge_is_reported(tmp_path, monkeypatch):
    import core.delivery_lineage as module

    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, anchor, completed, artifact, node = three_layer(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            monkeypatch.setattr(module, "MAX_LINEAGE_QUERIES", 3)
            result = resolve_successor_ex(conn, row(conn, anchor))
        print("RFB_QUERY_BUDGET", result.reason, result.successor, result.queries)
        assert result.successor is None
        assert result.reason == "budget_exhausted", result
        assert "statement_budget" in result.diagnostics
        assert result.queries == 3


def test_the_event_budget_limits_how_many_middle_edges_are_read(tmp_path, monkeypatch):
    import core.delivery_lineage as module

    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, anchor, completed = h.queued_repair(repo, tmp_path)
        artifact = artifact_id_of(repo, completed)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            retire(conn, anchor)
            usable = middle(conn, anchor, "b" * 32, generation=2)
            handover_event(conn, job, usable, artifact)
            newest = middle(conn, anchor, "c" * 32, generation=3)
            handover_event(conn, job, newest, "f" * 32)
            conn.commit()
            monkeypatch.setattr(module, "MAX_EVENTS", 1)
            result = resolve_successor_ex(conn, row(conn, anchor))
        print("RFB_EVENT_BUDGET", result.reason, result.successor, result.events)
        # Only the newest event is read, and it is unusable, so the refusal
        # proves the cap was applied instead of silently reading everything.
        assert result.reason == "event_target_unusable", result
        assert result.events == 1, result


def test_the_node_budget_bounds_a_long_supersede_history(tmp_path, monkeypatch):
    import core.delivery_lineage as module

    with h.LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, anchor, _ = h.queued_repair(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            chain(conn, anchor, 6)
            conn.commit()
            monkeypatch.setattr(module, "MAX_SUPERSEDE_NODES", 3)
            result = resolve_successor_ex(conn, row(conn, anchor))
        print("RFB_NODE_BUDGET", result.reason, result.successor, result.nodes)
        assert result.successor is None
        assert result.reason == "budget_exhausted", result
        assert result.nodes <= 4


@pytest.mark.parametrize("history", [8, 16, 24, 32, 64])
def test_a_chain_without_events_stays_bounded(history, tmp_path):
    with h.LibraryRepository(tmp_path / ("rfb-chain-" + str(history) + ".sqlite3")) as repo:
        _, anchor, _ = h.queued_repair(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            chain(conn, anchor, history)
            conn.commit()
            usable = row(conn, anchor)
            with _Trace(conn) as trace:
                result = resolve_successor_ex(conn, usable)
            statements = len(trace.statements)
            scans = len(trace.scans)
        print(
            "RFB_CHAIN_COST",
            "history=" + str(history),
            "statements=" + str(statements),
            "scans=" + str(scans),
            "nodes=" + str(result.nodes),
            "events=" + str(result.events),
            "reason=" + result.reason,
        )
        assert result.successor is None
        assert scans == 1, "one window read, never one per node"
        assert statements == result.queries <= MAX_LINEAGE_QUERIES
        assert result.nodes == min(history, WINDOW_LIMIT) <= MAX_SUPERSEDE_NODES
        assert result.events == 0
        if history < WINDOW_LIMIT:
            assert result.reason == "none", result
            assert "superseded_only:" + str(history) in result.diagnostics
        else:
            assert result.reason == "budget_exhausted", result
            assert "window_full" in result.diagnostics
