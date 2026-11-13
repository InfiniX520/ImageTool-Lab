"""UA-11: an accepted exclusion stops publication before the final link.

The gate is deliberately tested at the executor's own hook, because that is
where the linearization point lives: the transfer may already have streamed
every byte, but no final name may appear after an EXCLUDED intent was accepted.
These tests are red until the publish gate lands (Phase 2.5); they are not
marked xfail/skip.
"""
from __future__ import annotations

import sqlite3

import test_u1a_recovery as h
from core.library_repository import LibraryRepository


def _exclude(repo, batch_id, command_id):
    return repo.record_removal_intent(
        batch_id,
        h.ARTWORK,
        0,
        h.OUTPUT_REF,
        intent="excluded",
        command_id=command_id,
    )


def test_exclusion_recorded_before_publish_aborts_the_link(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        h.first_delivery(repo, root)
        h.target(root).unlink()
        job, (pending,) = h.second_task(repo, root)
        batch_id = job["batch_id"]
        recorded = []

        def hook(stage, row):
            if stage == "before_publish" and not recorded:
                _exclude(repo, batch_id, "exclude-at-publish")
                recorded.append(row["delivery_id"])

        transport = h.Transport(h.Response())
        outcome = h.run(
            repo, job, pending, h.executor(repo, root, transport, fault_hook=hook)
        )
        assert recorded, "the publish hook never ran"
        assert not h.target(root).exists(), "an excluded page was published"
        row = repo.get_delivery(pending)
        assert row["state"] == "cancelled", outcome
        assert row["error_code"] == "intent_excluded"
        assert repo.get_artifact(pending) is None
        assert len(transport.calls) == 1


def test_publish_authorized_follows_the_latest_intent(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        h.first_delivery(repo, root)
        h.target(root).unlink()
        job, (pending,) = h.second_task(repo, root)
        before = repo.publish_authorized(pending)
        assert before["allowed"] is True
        _exclude(repo, job["batch_id"], "exclude-before-authorization")
        after = repo.publish_authorized(pending)
        assert after["allowed"] is False
        assert after["reason"] == "intent_excluded"


def test_corrupt_intent_denies_publication(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        h.first_delivery(repo, root)
        h.target(root).unlink()
        job, (pending,) = h.second_task(repo, root)
        _exclude(repo, job["batch_id"], "exclude-then-corrupt")
        with sqlite3.connect(repo.database_path) as conn:
            conn.execute(
                "UPDATE events SET safe_payload = ? WHERE kind = 'removal_intent'",
                ("{broken",),
            )
        denied = repo.publish_authorized(pending)
        assert denied["allowed"] is False
        assert denied["reason"] == "intent_unreadable"
