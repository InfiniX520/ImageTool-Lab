"""RF pipeline probes: cancellation/cleanup lifecycle and carry successors.

Synthetic library, fake transport.  These tests bind the P1 half of the RF
contract: a cancelled delivery owns exactly one cleanup fact, a superseded
link is resolved instead of copied, and startup recovery never fails on a
legal recovery_needed cancellation.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import test_u1a_recovery as h
import test_u1a_reuse_gate as g
from core.library_repository import LibraryRepository
from core.page_delivery import _Fault


def settle(repo, job):
    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"])


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


def _exclude_during_transfer(repo, batch_id, command_id):
    def hook(stage, row):
        if stage == "before_publish":
            repo.record_removal_intent(
                batch_id, h.ARTWORK, 0, h.OUTPUT_REF,
                intent="excluded", command_id=command_id,
            )

    return hook


def test_recovery_needed_exclusion_cancels_and_cleans_the_staged_temp(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)

        def stop_before_publish(stage, row):
            if stage == "before_publish":
                raise _Fault("probe_stop")

        service = h.executor(
            repo, tmp_path, h.Transport(h.Response()),
            fault_hook=stop_before_publish,
        )
        assert h.run(repo, job, pending, service).state == "recovery_needed"
        assert not h.target(tmp_path).exists()
        assert list(tmp_path.rglob("*.part"))

        repo.record_removal_intent(
            job["batch_id"], h.ARTWORK, 0, h.OUTPUT_REF,
            intent="excluded", command_id="exclude-recovery-needed",
        )
        recovered = h.executor(repo, tmp_path, h.Transport())
        result = recovered.recover_local()
        assert (pending, "cancelled") in result
        assert repo.get_delivery(pending)["state"] == "cancelled"
        assert not h.target(tmp_path).exists()
        assert not list(tmp_path.rglob("*.part")), "the owned temp must be cleaned"
        assert cleanup_events(repo, pending)


def test_cleanup_refuses_a_foreign_staged_file(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        original_unlink = Path.unlink

        def fail_part_unlink(path, *args, **kwargs):
            if path.suffix == ".part":
                raise PermissionError("synthetic transient file lock")
            return original_unlink(path, *args, **kwargs)

        with monkeypatch.context() as scope:
            scope.setattr(Path, "unlink", fail_part_unlink)
            service = h.executor(
                repo, tmp_path, h.Transport(h.Response()),
                fault_hook=_exclude_during_transfer(
                    repo, job["batch_id"], "exclude-foreign"
                ),
            )
            assert h.run(repo, job, pending, service).state == "cancelled"
        part = next(tmp_path.rglob("*.part"))
        part.write_bytes(b"foreign-bytes")
        recovered = h.executor(repo, tmp_path, h.Transport())
        assert recovered.cleanup_delivery(pending) is False
        assert part.exists(), "foreign bytes must never be deleted"
        assert cleanup_events(repo, pending)[-1]["outcome"] == "failed"


def test_cleanup_is_idempotent_across_repeated_startup_recovery(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        service = h.executor(
            repo, tmp_path, h.Transport(h.Response()),
            fault_hook=_exclude_during_transfer(repo, job["batch_id"], "exclude-idempotent"),
        )
        assert h.run(repo, job, pending, service).state == "cancelled"
        recovered = h.executor(repo, tmp_path, h.Transport())
        assert recovered.cleanup_delivery(pending) is True
        first = len(cleanup_events(repo, pending))
        recovered.recover_local()
        recovered.recover_local()
        assert recovered.cleanup_delivery(pending) is True
        assert len(cleanup_events(repo, pending)) == first, "repeated cleanup must not spam events"
        assert repo.get_delivery(pending)["state"] == "cancelled"


def test_carry_links_a_completed_successor_instead_of_the_cancelled_hold(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        first = g.submit(repo, request_id="carry-first")
        ids = g.accept(
            repo, first["batch_id"], g.source((g.candidate(pages=(0, 1)),))
        )
        transport = h.Transport(h.Response(), h.Response(status=404))
        assert h.run(repo, first, ids[0], h.executor(repo, tmp_path, transport)).state == "completed"
        assert h.run(repo, first, ids[1], h.executor(repo, tmp_path, transport)).state == "failed"
        settle(repo, first)

        h.target(tmp_path).write_bytes(h.SAME_SIZE_PNG)
        second, (hold,) = h.second_task(repo, tmp_path)
        settle(repo, second)

        resumed = repo.resume(first["job_id"], "resume-carry-first")
        assert repo.activate_next()["batch_id"] == resumed["batch_id"]
        assert repo.carry_unresolved_items(resumed["batch_id"])["count"] == 1
        h.target(tmp_path).write_bytes(h.PNG_BYTES)
        result = repo.recover_verification_page(resumed["batch_id"], h.ARTWORK, 0)
        assert result["outcome"] == "reused"
        settle(repo, resumed)
        assert repo.get_delivery(hold)["state"] == "cancelled"

        original = repo.resume(second["job_id"], "resume-carry-second")
        assert repo.activate_next()["batch_id"] == original["batch_id"]
        carried = repo.carry_unresolved_items(original["batch_id"])
        assert carried["count"] == 1
        pages = h.pages(repo, original)
        with sqlite3.connect(repo.database_path) as conn:
            link = conn.execute(
                "SELECT delivery_id FROM batch_item_pages WHERE batch_id = ? "
                "AND illust_id = ? AND page_index = 0",
                (original["batch_id"], h.ARTWORK),
            ).fetchone()
        carried_delivery = repo.get_delivery(link[0])
        print("CARRY_SUCCESSOR", pages, carried_delivery["state"], carried_delivery["error_code"])
        assert pages["cancelled"] == 0, "it must not copy the cancelled reference"
        # The successor has no health record in this carry preflight, so the
        # honest result is a local hold (recovery verifies the bytes later).
        assert pages["needs_verification"] == 1
        assert carried_delivery["error_code"] == "carried_superseded_unverified"
        assert carried_delivery["job_id"] == second["job_id"]
