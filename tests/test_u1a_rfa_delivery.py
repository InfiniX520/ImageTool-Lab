"""RFA delivery closure: presence evidence, cleanup facts and recovery races.

Every test is synthetic: one temporary SQLite ledger, one temporary output
root, synthetic PNG bytes from a fake transport and the real
PageDeliveryExecutor.  No network, account, production library or real gallery
is touched, and no test rewrites a historical artifact or a completed row.

The contract under test (U1A_STATE_CONTRACT_V3 section 2):

* an unreachable root is not proof that a staged file is absent (RFA-02);
* the cleanup fact producer and the candidate query share one shape (RFA-03);
* a replaced staged file is never deleted from a stale observation (RFA-06);
* a losing recovery reports the winner instead of aborting the loop (RFA-07).
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

import test_u1a_recovery as h
from core.app_paths import AppPaths
from core.delivery_cleanup import (
    cleanup_event_payload,
    cleanup_fact,
    cleanup_is_terminal,
)
from core.file_presence import Presence, probe_ref
from core.library_paths import PathResolver
from core.library_repository import InvalidStateError, LibraryError, LibraryRepository
from core.page_delivery import _Fault
from core.task_contracts import PathRef, canonical_json


class _Crash(BaseException):
    """A hard stop that bypasses every handler, like a killed process."""


def settle(repo, job, complete=True):
    repo.finish_source(job["batch_id"], coverage_complete=complete)
    assert repo.settle_batch(job["batch_id"])


def cancelled(repo, root, ident="123", request_id="cancel"):
    """One cancelled delivery with durable prepared evidence."""
    job = h.submit(repo, request_id=request_id)
    (delivery,) = h.accept(repo, job["batch_id"], h.source((h.candidate(ident),)))

    def exclude(stage, row):
        if stage == "before_publish":
            repo.record_removal_intent(
                job["batch_id"],
                ident,
                0,
                PathRef(relative_path=f"downl_Pixiv/{ident}_p0.png"),
                intent="excluded",
                command_id="exclude-" + request_id,
            )

    service = h.executor(repo, root, h.Transport(h.Response()), fault_hook=exclude)
    assert h.run(repo, job, delivery, service).state == "cancelled"
    settle(repo, job)
    return delivery, service


def prepared(repo, root, request_id="prepared", artworks=(h.ARTWORK,)):
    """Deliveries left durable in state prepared by a hard crash."""
    job = h.submit(repo, request_id=request_id)
    ids = h.accept(
        repo, job["batch_id"], h.source(tuple(h.candidate(a) for a in artworks))
    )

    def crash(stage, row):
        if stage == "before_publish":
            raise _Crash()

    for delivery in ids:
        with pytest.raises(_Crash):
            h.run(
                repo,
                job,
                delivery,
                h.executor(repo, root, h.Transport(h.Response()), fault_hook=crash),
            )
        assert repo.get_delivery(delivery)["state"] == "prepared"
    return job, ids


def clone_cancelled(conn, source_id, new_id, generation):
    """Copy one cancelled row with prepared evidence naming the new delivery."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(deliveries)")]
    values = conn.execute(
        "SELECT * FROM deliveries WHERE delivery_id = ?", (source_id,)
    ).fetchone()
    row = dict(zip(columns, values))
    prepared_evidence = json.loads(row["prepared_json"])
    prepared_evidence["delivery_id"] = new_id
    temp_ref = dict(prepared_evidence["temp_ref"])
    head, _, _tail = temp_ref["relative_path"].rpartition("/")
    temp_ref["relative_path"] = f"{head}/.{new_id}.audit.part"
    prepared_evidence["temp_ref"] = temp_ref
    row.update(
        delivery_id=new_id,
        generation=int(generation),
        prepared_json=canonical_json(prepared_evidence),
        checkpoint_json=None,
    )
    conn.execute(
        "INSERT INTO deliveries(" + ",".join(columns) + ") VALUES("
        + ",".join("?" for _ in columns) + ")",
        [row[column] for column in columns],
    )
    return new_id


def fact_for(conn, delivery_id):
    row = conn.execute(
        "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
    ).fetchone()
    return cleanup_fact(
        {
            "delivery_id": row["delivery_id"],
            "current_batch_id": row["current_batch_id"],
            "job_id": row["job_id"],
            "state": row["state"],
            "prepared": json.loads(row["prepared_json"]),
            "generation": row["generation"],
        }
    )


def insert_cleanup_event(conn, delivery_id, payload):
    row = conn.execute(
        "SELECT job_id, current_batch_id FROM deliveries WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO events(event_uuid, job_id, batch_id, kind, safe_payload, "
        "created_at) VALUES (?, ?, ?, 'delivery_cleanup', ?, ?)",
        (
            uuid.uuid4().hex,
            row["job_id"],
            row["current_batch_id"],
            canonical_json(payload),
            datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        ),
    )


def cleanup_events(repo, delivery_id):
    with sqlite3.connect(repo.database_path) as conn:
        rows = conn.execute(
            "SELECT safe_payload FROM events WHERE kind = 'delivery_cleanup' "
            "ORDER BY event_seq"
        ).fetchall()
    found = []
    for (payload,) in rows:
        try:
            info = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(info, dict) and info.get("delivery_id") == delivery_id:
            found.append(info)
    return found


# ---------------------------------------------------------------------------
# RFA-03: one cleanup protocol for producer, query and queue
# ---------------------------------------------------------------------------


def test_a_mixed_cleanup_window_advances_at_every_limit(tmp_path):
    """201 cancelled rows: terminal, legacy, failed, corrupt and clean."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        first, _service = cancelled(repo, root)
        clones = [f"{index + 1:032x}" for index in range(200)]
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            for index, new_id in enumerate(clones):
                clone_cancelled(conn, first, new_id, index + 2)
            # The very first two rows cannot be parsed at all.
            for bad in (first, clones[0]):
                conn.execute(
                    "UPDATE deliveries SET prepared_json = '{not json' "
                    "WHERE delivery_id = ?",
                    (bad,),
                )
            for delivery in clones[1:41]:  # legacy: outcome only
                insert_cleanup_event(
                    conn,
                    delivery,
                    {
                        "delivery_id": delivery,
                        "batch_id": "legacy-batch",
                        "outcome": "cleaned",
                        "error_code": "",
                    },
                )
            for delivery in clones[41:81]:  # current: prepared identity
                insert_cleanup_event(
                    conn,
                    delivery,
                    cleanup_event_payload(
                        fact_for(conn, delivery), outcome="cleaned"
                    ),
                )
            for delivery in clones[81:121]:  # retryable failure
                insert_cleanup_event(
                    conn,
                    delivery,
                    cleanup_event_payload(
                        fact_for(conn, delivery),
                        outcome="failed",
                        error_code="cleanup_failed",
                    ),
                )
            conn.commit()

        measured = {}
        for limit in (1, 2, 200):
            stats = {}
            facts = repo.list_cleanup_candidates(limit=limit, stats=stats)
            measured[limit] = (len(facts), dict(stats))
            assert all("rowid" in fact for fact in facts)
            if limit < 200:
                assert len(facts) == limit
        assert measured[1][0] == 1
        assert measured[1][1]["examined"] == 3
        assert measured[1][1]["skipped"] == 2
        assert measured[2][1]["skipped"] == 2
        assert measured[2][1]["examined"] == 4
        assert measured[200][0] == 119
        assert measured[200][1]["returned"] == 119
        assert measured[200][1]["skipped"] == 2
        assert measured[200][1]["examined"] == 121
        print("CLEANUP_WINDOW", measured)

        # Forward pages are stable and duplicate-free: fresh rows first, and a
        # retried row is only offered once the fresh work ahead of it is gone.
        def paged():
            found, cursor = [], 0
            while True:
                stats = {}
                page = repo.list_cleanup_candidates(
                    limit=7, after_rowid=cursor, stats=stats
                )
                if not page:
                    return found
                found.extend(fact["delivery_id"] for fact in page)
                assert stats["next_rowid"] > cursor
                cursor = int(stats["next_rowid"])

        fresh = paged()
        assert len(fresh) == 79
        assert len(set(fresh)) == 79
        whole = [fact["delivery_id"] for fact in repo.list_cleanup_candidates(limit=200)]
        assert whole[-40:] == sorted(whole[-40:]), "retries are paced after fresh work"
        assert set(fresh) == set(whole) - set(whole[-40:])
        for delivery in fresh:
            assert repo.mark_cleanup_completed(delivery, outcome="cleaned") is True
        assert paged() == whole[-40:]
        assert len(paged()) == 40

        # None of the excluded or unparsable rows leaks into the queue.
        excluded = {first} | set(clones[:1]) | set(clones[1:81])
        assert not (set(whole) & excluded)
        assert repo.get_delivery(clones[1])["state"] == "cancelled"


def test_the_cleanup_window_rotates_past_a_stuck_row(tmp_path, monkeypatch):
    """A permanent failure at the head of the queue never hides the rest."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        original_unlink = Path.unlink

        def lock_part(path, *args, **kwargs):
            if path.suffix == ".part":
                raise PermissionError("synthetic sharing violation")
            return original_unlink(path, *args, **kwargs)

        with monkeypatch.context() as scope:
            scope.setattr(Path, "unlink", lock_part)
            stuck, _service = cancelled(repo, root, "123", "stuck")
            service = h.executor(repo, root, h.Transport())
            service._sweep_cleanup(200)
        assert len(list(root.rglob("*.part"))) == 1
        assert cleanup_events(repo, stuck)[-1]["outcome"] == "failed"

        second, _ = cancelled(repo, root, "456", "second")
        third, _ = cancelled(repo, root, "789", "third")

        def lock_first(path, *args, **kwargs):
            if path.suffix == ".part" and stuck in path.name:
                raise PermissionError("synthetic sharing violation")
            return original_unlink(path, *args, **kwargs)

        with monkeypatch.context() as scope:
            scope.setattr(Path, "unlink", lock_first)
            # A row that already failed once is paced behind fresh rows, but it
            # still appears in the same bounded window.
            first_pass = repo.list_cleanup_candidates(limit=1, after_rowid=0)
            assert [fact["delivery_id"] for fact in first_pass] == [second]
            window = repo.list_cleanup_candidates(limit=200)
            assert [fact["delivery_id"] for fact in window] == [second, third, stuck]
            service = h.executor(repo, root, h.Transport())
            service._sweep_cleanup(200)
        assert repo.get_delivery(stuck)["state"] == "cancelled"
        assert cleanup_events(repo, stuck)[-1]["outcome"] == "failed"
        assert cleanup_events(repo, second)[-1]["outcome"] == "cleaned"
        assert cleanup_events(repo, third)[-1]["outcome"] == "cleaned"
        # The cursor moved past the whole window, so the stuck row is retried
        # on the next pass instead of occupying every window.
        assert service._cleanup_cursor >= max(int(fact["rowid"]) for fact in window)
        service._sweep_cleanup(200)
        assert cleanup_events(repo, stuck)[-1]["outcome"] == "cleaned"
        assert not list(root.rglob("*.part"))


def test_cleanup_refuses_an_identity_it_did_not_observe(tmp_path):
    """A terminal fact must be bound to the prepared bytes it claims."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        delivery, service = cancelled(repo, root)
        evidence = repo.get_delivery(delivery)["prepared"]
        with pytest.raises(InvalidStateError):
            repo.mark_cleanup_completed(
                delivery,
                outcome="cleaned",
                expected_sha256="0" * 64,
                expected_size=int(evidence["size"]),
            )
        with pytest.raises(InvalidStateError):
            repo.mark_cleanup_completed(
                delivery,
                outcome="cleaned",
                expected_sha256=str(evidence["sha256"]),
                expected_size=int(evidence["size"]) + 1,
            )
        assert cleanup_events(repo, delivery) == []
        assert repo.mark_cleanup_completed(
            delivery,
            outcome="cleaned",
            expected_sha256=str(evidence["sha256"]),
            expected_size=int(evidence["size"]),
        ) is True
        assert cleanup_events(repo, delivery)[-1]["cleanup_completed"] is True
        assert repo.list_cleanup_candidates() == ()


def test_the_query_and_the_reader_agree_on_terminal_facts(tmp_path):
    """Both shapes end the queue; a failed attempt never does."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        legacy, _ = cancelled(repo, root, "123", "legacy")
        current, _ = cancelled(repo, root, "456", "current")
        failed, _ = cancelled(repo, root, "789", "failed")
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            insert_cleanup_event(
                conn,
                legacy,
                {
                    "delivery_id": legacy,
                    "batch_id": "legacy-batch",
                    "outcome": "cleaned",
                    "error_code": "",
                },
            )
            insert_cleanup_event(
                conn, current, cleanup_event_payload(fact_for(conn, current), outcome="cleaned")
            )
            insert_cleanup_event(
                conn,
                failed,
                cleanup_event_payload(
                    fact_for(conn, failed), outcome="failed", error_code="cleanup_failed"
                ),
            )
            conn.commit()
        assert cleanup_is_terminal({"outcome": "cleaned"}) is True
        assert cleanup_is_terminal({"outcome": "cleaned", "cleanup_completed": True}) is True
        assert cleanup_is_terminal({"outcome": "cleaned", "cleanup_completed": False}) is False
        assert cleanup_is_terminal({"outcome": "failed", "cleanup_completed": False}) is False
        assert cleanup_is_terminal({}) is False
        assert [fact["delivery_id"] for fact in repo.list_cleanup_candidates()] == [
            failed
        ]


# ---------------------------------------------------------------------------
# RFA-02: presence evidence separates absent from unobservable
# ---------------------------------------------------------------------------


def test_presence_separates_bound_unbound_and_unreadable_roots(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    resolver = PathResolver(AppPaths(library))
    project_ref = PathRef(relative_path="downl_Pixiv/123_p0.png")

    missing = tmp_path / "missing"
    absent_resolver = PathResolver(AppPaths(missing))
    bound = probe_ref(absent_resolver, project_ref)
    assert bound.state is Presence.ROOT_UNAVAILABLE
    assert bound.detail == "missing_bound"
    assert probe_ref(absent_resolver, project_ref).is_absent is False

    unbound_ref = PathRef(kind="external", root_id="volume", relative_path="x.png")
    unbound = probe_ref(resolver, unbound_ref)
    assert unbound.state is Presence.ROOT_UNAVAILABLE
    assert unbound.detail == "unbound"

    assert probe_ref(resolver, project_ref).state in {
        Presence.ABSENT,
        Presence.PRESENT,
    }
    assert probe_ref(resolver, project_ref).detail in {"ok", "parent_not_a_directory"}

    target = resolver.resolve(project_ref)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    assert probe_ref(resolver, project_ref).state is Presence.PRESENT

    real_stat = os.stat

    def denied(path, *args, **kwargs):
        if Path(path) == target:
            raise PermissionError("synthetic acl denial")
        return real_stat(path, *args, **kwargs)

    import core.file_presence as presence_module

    with pytest.MonkeyPatch.context() as scope:
        scope.setattr(presence_module.os, "stat", denied)
        unreadable = probe_ref(resolver, project_ref)
    assert unreadable.state is Presence.UNREADABLE
    assert unreadable.detail in {"permission_denied", "resolve_failed", "stat_failed"}
    assert unreadable.is_absent is False


def test_cleanup_offline_root_keeps_the_fact_retryable(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        original_unlink = Path.unlink

        def locked(path, *args, **kwargs):
            if path.suffix == ".part":
                raise PermissionError("synthetic sharing violation")
            return original_unlink(path, *args, **kwargs)

        with pytest.MonkeyPatch.context() as scope:
            scope.setattr(Path, "unlink", locked)
            delivery, service = cancelled(repo, root)
        assert len(list(root.rglob("*.part"))) == 1
        offline = tmp_path / "offline"
        root.rename(offline)
        assert service.cleanup_delivery(delivery) is False
        offline.rename(root)
        facts = cleanup_events(repo, delivery)
        assert facts[-1]["outcome"] == "failed"
        assert facts[-1]["error_code"] == "root_unavailable"
        assert all(fact.get("cleanup_completed") is not True for fact in facts)
        assert [fact["delivery_id"] for fact in repo.list_cleanup_candidates()] == [
            delivery
        ]
        assert service.cleanup_delivery(delivery) is True
        assert cleanup_events(repo, delivery)[-1]["outcome"] == "cleaned"


def test_a_published_page_survives_an_unplugged_root(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    offline = tmp_path / "offline"
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job = h.submit(repo)
        (delivery,) = h.accept(repo, job["batch_id"], h.source((h.candidate(),)))

        def crash(stage, row):
            if stage == "after_publish":
                raise _Fault("audit_after_link")

        assert (
            h.run(
                repo,
                job,
                delivery,
                h.executor(repo, root, h.Transport(h.Response()), fault_hook=crash),
            ).state
            == "recovery_needed"
        )
        assert h.target(root).read_bytes() == h.PNG_BYTES
        repo.record_removal_intent(
            job["batch_id"],
            h.ARTWORK,
            0,
            h.OUTPUT_REF,
            intent="excluded",
            command_id="exclude-published",
        )

        def unplug(stage, row):
            if stage == "before_publish":
                root.rename(offline)

        recovery = h.executor(repo, root, h.Transport(), fault_hook=unplug)
        assert recovery.recover_local() == ((delivery, "recovery_needed"),)
        row = repo.get_delivery(delivery)
        assert row["state"] == "recovery_needed"
        assert repo.get_artifact(delivery) is None

        offline.rename(root)
        assert h.executor(repo, root, h.Transport()).recover_local() == (
            (delivery, "completed"),
        )
        assert repo.get_delivery(delivery)["state"] == "completed"
        assert repo.get_artifact(delivery) is not None
        assert h.target(root).read_bytes() == h.PNG_BYTES


def test_an_unplugged_root_does_not_cancel_prepared_bytes(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    offline = tmp_path / "offline"
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job = h.submit(repo)
        (delivery,) = h.accept(repo, job["batch_id"], h.source((h.candidate(),)))

        def unplug(stage, row):
            if stage == "before_publish":
                repo.record_removal_intent(
                    job["batch_id"],
                    h.ARTWORK,
                    0,
                    h.OUTPUT_REF,
                    intent="excluded",
                    command_id="exclude-before-unplug",
                )
                root.rename(offline)

        service = h.executor(repo, root, h.Transport(h.Response()), fault_hook=unplug)
        result = h.run(repo, job, delivery, service)
        assert result.state == "recovery_needed"
        assert repo.get_delivery(delivery)["state"] == "recovery_needed"
        assert len(list(offline.rglob("*.part"))) == 1
        offline.rename(root)
        assert h.executor(repo, root, h.Transport()).recover_local() == (
            (delivery, "cancelled"),
        )
        assert not list(root.rglob("*.part"))


# ---------------------------------------------------------------------------
# RFA-06: one deletion implementation, re-verified after the writer round-trip
# ---------------------------------------------------------------------------


def test_a_replaced_staged_file_survives_the_cancel_round_trip(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        original = repo.cancel_unpublished_delivery
        replacement = []

        def replace_before_cas(*args, **kwargs):
            part = next(root.rglob("*.part"))
            part.unlink()
            part.write_bytes(b"foreign-replacement-after-observation")
            replacement.append(part)
            return original(*args, **kwargs)

        monkeypatch.setattr(repo, "cancel_unpublished_delivery", replace_before_cas)
        delivery, service = cancelled(repo, root)
        assert replacement and replacement[0].exists()
        assert replacement[0].read_bytes() == b"foreign-replacement-after-observation"
        facts = cleanup_events(repo, delivery)
        assert facts[-1]["outcome"] == "failed"
        assert facts[-1]["error_code"] == "temp_replaced_after_observation"
        assert service.cleanup_delivery(delivery) is False
        assert replacement[0].exists(), "a foreign file is never deleted"
        assert cleanup_events(repo, delivery)[-1]["error_code"] == "unowned_staged_temp"


def test_completed_cleanup_refuses_a_replaced_staged_file(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job = h.submit(repo)
        (delivery,) = h.accept(repo, job["batch_id"], h.source((h.candidate(),)))

        def crash(stage, row):
            if stage == "after_confirm":
                raise _Crash()

        with pytest.raises(_Crash):
            h.run(
                repo,
                job,
                delivery,
                h.executor(repo, root, h.Transport(h.Response()), fault_hook=crash),
            )
        assert repo.get_delivery(delivery)["state"] == "completed"
        service = h.executor(repo, root, h.Transport())
        parts = list(root.rglob("*.part"))
        assert parts, "a crash after confirmation leaves the owned temp behind"
        parts[0].unlink()
        parts[0].write_bytes(b"foreign-after-confirm")
        assert service.cleanup_delivery(delivery) is False
        assert parts[0].exists()


# ---------------------------------------------------------------------------
# RFA-07: a losing recovery reports the winner and never aborts the loop
# ---------------------------------------------------------------------------


def test_two_interleaved_recoveries_report_the_winner(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        root = tmp_path / "library"
        root.mkdir()
        _job, (delivery,) = prepared(repo, root, "interleaved")
        winner = h.executor(repo, root, h.Transport())

        def interleave(stage, row):
            if stage == "before_publish":
                assert winner.recover_local() == ((delivery, "completed"),)
                raise _Fault("loser_stage_fault")

        loser = h.executor(repo, root, h.Transport(), fault_hook=interleave)
        assert loser.recover_local() == ((delivery, "completed"),)
        assert repo.get_delivery(delivery)["state"] == "completed"
        assert repo.get_artifact(delivery) is not None
        with sqlite3.connect(repo.database_path) as conn:
            issues = conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind = 'delivery_issue' "
                "AND safe_payload LIKE ?",
                (f'%"delivery_id":"{delivery}"%',),
            ).fetchone()[0]
        assert issues == 0, "a losing recovery must not overwrite the winner"


def test_compensation_cas_failure_reports_the_completed_winner(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _job, (delivery,) = prepared(repo, root, "compensation-cas")
        winner = h.executor(repo, root, h.Transport())
        original = repo.set_delivery_issue
        moved = {"done": False}

        def move_then_write(*args, **kwargs):
            if not moved["done"]:
                moved["done"] = True
                assert winner.recover_local() == ((delivery, "completed"),)
            return original(*args, **kwargs)

        monkeypatch.setattr(repo, "set_delivery_issue", move_then_write)

        def fault(stage, row):
            if stage == "before_publish":
                raise _Fault("loser_stage_fault")

        loser = h.executor(repo, root, h.Transport(), fault_hook=fault)
        assert loser.recover_local() == ((delivery, "completed"),)
        assert repo.get_delivery(delivery)["state"] == "completed"
        assert repo.get_artifact(delivery) is not None


def test_compensation_write_failure_keeps_every_row_recoverable(
    tmp_path, monkeypatch
):
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _job, ids = prepared(repo, root, "compensation-write", (h.ARTWORK, "456"))

        def refuse(*args, **kwargs):
            raise LibraryError("synthetic writer outage")

        def fault(stage, row):
            if stage == "before_publish":
                raise _Fault("loser_stage_fault")

        loser = h.executor(repo, root, h.Transport(), fault_hook=fault)
        with monkeypatch.context() as scope:
            scope.setattr(repo, "set_delivery_issue", refuse)
            result = loser.recover_local()
        assert result == tuple((delivery, "recovery_needed") for delivery in ids)
        for delivery in ids:
            assert repo.get_delivery(delivery)["state"] == "prepared"
        assert len(list(root.rglob("*.part"))) == 2

        assert h.executor(repo, root, h.Transport()).recover_local() == tuple(
            (delivery, "completed") for delivery in ids
        )
        for delivery in ids:
            assert repo.get_artifact(delivery) is not None
