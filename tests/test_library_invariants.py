"""Independent adversarial cases, beyond the repository author's own suite."""
import sqlite3
import hashlib
from dataclasses import replace

import pytest

from core.library_repository import BudgetExceeded, LibraryRepository, SchemaVersionError
from core.task_contracts import BatchBudget, CandidateDecision, CollectionRequest, PageSpec, PathRef, SourcePage


def candidate(number):
    return CandidateDecision(str(number), "download", artist_id="7", page_count=1, pages=(
        PageSpec(0, PathRef(relative_path=f"downl_Pixiv/{number}_p0.png"), f"https://i.pximg.net/{number}_p0.png"),))


def open_job(repo, size=3):
    request = CollectionRequest("request", "account", budget=BatchBudget(max_works=max(size, 100)))
    submitted = repo.submit(request, "submit")
    repo.activate_next()
    page = SourcePage("follow", "history", "artist-7", "page-1", tuple(candidate(100 + n) for n in range(size)),
                      cursor_after_json='{"offset":1}')
    ids = repo.accept_source_page(submitted["batch_id"], page)
    return submitted, page, ids


def test_page_receipt_is_namespaced_by_source_pass_and_entity(tmp_path):
    with LibraryRepository(tmp_path / "library.sqlite3") as repo:
        submitted, page, ids = open_job(repo, 1)
        second = replace(page, entity_id="artist-8", candidates=(candidate(200),))
        accepted = repo.accept_source_page(submitted["batch_id"], second)
        assert len(accepted) == 1 and accepted != ids
        assert repo.get_checkpoint(submitted["job_id"], "follow", "history", "artist-7")
        assert repo.get_checkpoint(submitted["job_id"], "follow", "history", "artist-8")
        assert repo.accept_source_page(submitted["batch_id"], second) == accepted


def test_resume_budget_counts_recovered_works_before_claiming(tmp_path):
    with LibraryRepository(tmp_path / "library.sqlite3") as repo:
        submitted, _, ids = open_job(repo)
        repo.request_stop(submitted["job_id"], "pause")
        assert repo.settle_stop(submitted["batch_id"])
        resumed = repo.resume(submitted["job_id"], "resume", BatchBudget(max_works=1))
        repo.activate_next()
        assert repo.claim_delivery(ids[0], resumed["batch_id"])
        repo.release_delivery(ids[0], resumed["batch_id"], state="failed", error_code="simulated")
        with pytest.raises(BudgetExceeded):
            repo.claim_delivery(ids[1], resumed["batch_id"])
        assert repo.get_delivery(ids[1])["state"] == "suspended"
        assert repo.get_delivery(ids[1])["attempts"] == 0


def test_cancel_new_batch_cancels_its_unclaimed_old_pending_scope(tmp_path):
    with LibraryRepository(tmp_path / "library.sqlite3") as repo:
        submitted, _, ids = open_job(repo)
        repo.request_stop(submitted["job_id"], "pause")
        repo.settle_stop(submitted["batch_id"])
        resumed = repo.resume(submitted["job_id"], "resume")
        repo.activate_next()
        repo.request_stop(submitted["job_id"], "cancel", cancel=True)
        assert repo.settle_stop(resumed["batch_id"])
        assert all(repo.get_delivery(item)["state"] == "cancelled" for item in ids)
        assert repo.list_dispatchable(submitted["job_id"]) == ()
        assert repo.get_batch(submitted["batch_id"])["phase"] == "paused"


def test_future_retries_do_not_hide_due_pending_after_first_200_rows(tmp_path):
    database = tmp_path / "library.sqlite3"
    with LibraryRepository(database) as repo:
        submitted, _, _ = open_job(repo, 201)
        with sqlite3.connect(database) as injection:
            ids = [row[0] for row in injection.execute("SELECT delivery_id FROM deliveries ORDER BY created_at,delivery_id")]
            injection.executemany("UPDATE deliveries SET state='retry_wait',retry_at='2099-01-01T00:00:00+00:00' WHERE delivery_id=?",
                                  [(item,) for item in ids[:200]])
        assert repo.list_dispatchable(submitted["job_id"], limit=1) == (ids[-1],)


def test_acceptance_trigger_failure_rolls_back_cursor_items_and_page_receipt(tmp_path):
    database = tmp_path / "library.sqlite3"
    with LibraryRepository(database) as repo:
        submitted = repo.submit(CollectionRequest("request", "account"), "submit")
        repo.activate_next()
        page = SourcePage("follow", "history", "artist-7", "page-1", (candidate(100), candidate(101)), cursor_after_json='{"offset":2}')
        with sqlite3.connect(database) as injection:
            injection.execute("CREATE TRIGGER simulated_disk_fault BEFORE INSERT ON checkpoints BEGIN SELECT RAISE(ABORT,'simulated persistence fault'); END")
        with pytest.raises(Exception):
            repo.accept_source_page(submitted["batch_id"], page)
        assert repo.get_checkpoint(submitted["job_id"], "follow", "history", "artist-7") is None
        assert repo.list_dispatchable(submitted["job_id"]) == ()
        with sqlite3.connect(database) as injection:
            assert injection.execute("SELECT COUNT(*) FROM batch_items").fetchone()[0] == 0
            assert injection.execute("SELECT COUNT(*) FROM source_pages").fetchone()[0] == 0
            injection.execute("DROP TRIGGER simulated_disk_fault")
        assert len(repo.accept_source_page(submitted["batch_id"], page)) == 2


def test_read_error_cannot_fall_back_to_a_stale_immutable_main_file(tmp_path, monkeypatch):
    database = tmp_path / "library.sqlite3"
    with LibraryRepository(database) as repo:
        submitted, _, _ = open_job(repo, 1)
        with sqlite3.connect(database) as checkpoint:
            checkpoint.execute("PRAGMA wal_checkpoint(FULL)")
        repo.request_stop(submitted["job_id"], "pause-in-wal")
        original_connect = sqlite3.connect

        def unavailable_wal_connection(value, *args, **kwargs):
            text = str(value)
            if kwargs.get("uri") and "mode=ro" in text and "immutable" not in text:
                raise sqlite3.OperationalError("simulated inability to read the current WAL")
            return original_connect(value, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", unavailable_wal_connection)
        with pytest.raises(sqlite3.OperationalError):
            repo.get_batch(submitted["batch_id"])


def test_snapshot_revision_and_phase_belong_to_one_read_transaction(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "library.sqlite3") as repo:
        submitted, _, _ = open_job(repo, 1)
        original_read = repo._readonly_connection
        injected = []

        def reading_connection():
            connection = original_read()

            def before_query(sql):
                if not injected and "MAX(EVENT_SEQ)" in sql.upper():
                    injected.append(True)
                    repo.request_stop(submitted["job_id"], "pause-between-read-queries")

            connection.set_trace_callback(before_query)
            return connection

        monkeypatch.setattr(repo, "_readonly_connection", reading_connection)
        earlier = repo.snapshot(submitted["job_id"])
        later = repo.snapshot(submitted["job_id"])
        assert injected
        assert earlier["phase"] == "running" and later["phase"] == "pausing"
        assert later["revision"] > earlier["revision"]


def test_same_version_with_incompatible_unique_constraint_is_refused_readonly(tmp_path):
    database = tmp_path / "library.sqlite3"
    with LibraryRepository(database) as repo:
        repo.submit(CollectionRequest("request", "account"), "submit")
    with sqlite3.connect(database) as wrong_schema:
        wrong_schema.execute("DROP INDEX ux_batches_single_active")
        wrong_schema.execute("CREATE INDEX ux_batches_single_active ON batches(active_slot)")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(SchemaVersionError, match="schema object"):
        LibraryRepository(database)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
