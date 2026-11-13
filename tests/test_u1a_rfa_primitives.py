"""S0 primitive: the cancellation CAS is bound to the observed staged file.

The delivery executor verifies one exact '.part' and then waits for the writer.
Between the observation and the destructive unlink the file may be replaced
(RFA-06).  The command therefore carries the prepared digest and the row
generation it observed; the writer refuses a row whose evidence moved.

Synthetic SQLite and synthetic PNG only; no network, account or real gallery.
"""
from __future__ import annotations

import pytest

import test_u1a_recovery as h
from core.library_repository import InvalidStateError, LibraryError, LibraryRepository
from core.page_delivery import _Fault


def prepared_delivery(repo, root):
    """Leave one delivery with durable prepared evidence and no final name."""
    job = h.submit(repo)
    (delivery,) = h.accept(repo, job["batch_id"], h.source((h.candidate(),)))

    def crash(stage, row):
        if stage == "before_publish":
            raise _Fault("audit_stop_before_publish")

    service = h.executor(repo, root, h.Transport(h.Response()), fault_hook=crash)
    assert h.run(repo, job, delivery, service).state == "recovery_needed"
    row = repo.get_delivery(delivery)
    assert row["state"] == "recovery_needed"
    assert isinstance(row["prepared"], dict)
    return job, delivery, row


def test_cancel_cas_accepts_the_observed_evidence(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, delivery, row = prepared_delivery(repo, root)
        repo.cancel_unpublished_delivery(
            delivery,
            job["batch_id"],
            error_code="intent_excluded",
            expected_state="recovery_needed",
            expected_prepared_sha256=row["prepared"]["sha256"],
            expected_generation=int(row["generation"]),
        )
        assert repo.get_delivery(delivery)["state"] == "cancelled"


def test_cancel_cas_refuses_a_moved_digest(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, delivery, row = prepared_delivery(repo, root)
        with pytest.raises(InvalidStateError):
            repo.cancel_unpublished_delivery(
                delivery,
                job["batch_id"],
                error_code="intent_excluded",
                expected_state="recovery_needed",
                expected_prepared_sha256="0" * 64,
            )
        assert repo.get_delivery(delivery)["state"] == "recovery_needed"


def test_cancel_cas_refuses_a_moved_generation(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, delivery, row = prepared_delivery(repo, root)
        with pytest.raises(InvalidStateError):
            repo.cancel_unpublished_delivery(
                delivery,
                job["batch_id"],
                error_code="intent_excluded",
                expected_state="recovery_needed",
                expected_generation=int(row["generation"]) + 1,
            )
        assert repo.get_delivery(delivery)["state"] == "recovery_needed"


def test_cancel_cas_rejects_a_malformed_digest_before_enqueueing(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, delivery, row = prepared_delivery(repo, root)
        for bad in ("ABCD", "0" * 63, "0" * 65, b"0" * 64):
            with pytest.raises(LibraryError):
                repo.cancel_unpublished_delivery(
                    delivery,
                    job["batch_id"],
                    error_code="intent_excluded",
                    expected_state="recovery_needed",
                    expected_prepared_sha256=bad,
                )
        assert repo.get_delivery(delivery)["state"] == "recovery_needed"


def test_cancel_without_expectations_keeps_the_previous_behaviour(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, delivery, row = prepared_delivery(repo, root)
        repo.cancel_unpublished_delivery(
            delivery,
            job["batch_id"],
            error_code="intent_excluded",
            expected_state="recovery_needed",
        )
        assert repo.get_delivery(delivery)["state"] == "cancelled"
