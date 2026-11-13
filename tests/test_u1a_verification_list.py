"""U1-A/W5: the read-only page list and summary the recovery panel is built on.

Every test is synthetic: one temporary SQLite ledger, one temporary output root,
a fake transport serving a real PNG.  No network, account, production library,
real gallery or Tk widget is touched, and no test writes to a live delivery:
the states under test come from the product commands (claim, release,
set_delivery_issue, request_stop/settle_stop) or from an explicit fixture insert
for the bulk and NULL-owner cases.  Each test also checks that reading changed
nothing.

The contract under test (core/delivery_repository.py):

* a row is one selected page whose delivery is in one of the requested states;
* recoverable/actions follow the recovery decision table and need a running batch;
* the cursor is the page primary key plus the delivery id, ordered the same way;
* one short read transaction per call, at most two statements for a page and
  three for a summary, no filesystem access, no hashing, no plaintext path.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from core.delivery_recovery import RecoveryOutcome, decide_page_recovery
from core.library_paths import PathResolver
from core.library_repository import LibraryError, LibraryRepository
from core.preservation_policy import LocalState
from core.task_contracts import (
    CandidateDecision,
    CollectionRequest,
    OutputSpec,
    PageSpec,
    PathRef,
    SourcePage,
)
from core.app_paths import AppPaths

ARTIST = "7"
LISTABLE_STATES = (
    "needs_verification",
    "recovery_needed",
    "suspended",
    "pending",
    "retry_wait",
)


# ---------------------------------------------------------------------------
# Fixtures: a real repository, a real running batch, real page links
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "ledger.sqlite3"


@pytest.fixture
def repo(db_path):
    with LibraryRepository(db_path) as value:
        yield value


def _submit(repo, request_id="w5-list"):
    request = CollectionRequest(
        request_id, "offline-account", output=OutputSpec(folder_by_artist=False)
    )
    job = repo.submit(request, "submit-" + request_id)
    assert repo.activate_next()["batch_id"] == job["batch_id"]
    return job


def _accept(repo, batch_id, artworks, *, pages=1, source_id="source-1"):
    candidates = tuple(
        CandidateDecision(
            str(artwork),
            "download",
            artist_id=ARTIST,
            page_count=pages,
            pages=tuple(
                PageSpec(
                    index,
                    PathRef(relative_path=f"downl_Pixiv/{artwork}_p{index}.png"),
                    f"https://i.pximg.net/img-original/2026/{artwork}_p{index}.png",
                )
                for index in range(pages)
            ),
        )
        for artwork in artworks
    )
    source = SourcePage("follow", "history", ARTIST, source_id, candidates, exhausted=True)
    return repo.accept_source_page(batch_id, source)


def _page_rows(repo, batch_id):
    """Read the page links straight from the ledger; a fixture, not the API."""
    conn = repo._readonly_connection()
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT bip.batch_id, bip.illust_id, bip.page_index, bip.delivery_id,"
                " d.state FROM batch_item_pages AS bip"
                " JOIN deliveries AS d ON d.delivery_id = bip.delivery_id"
                " WHERE bip.batch_id = ? ORDER BY bip.illust_id, bip.page_index",
                (batch_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


def _claim(repo, delivery_id, batch_id):
    row = repo.claim_delivery(delivery_id, batch_id)
    assert row is not None, "the delivery was not claimable"
    return row


def _hold(repo, delivery_id, batch_id, *, error_code="artifact_missing"):
    """Move one pending page to needs_verification through the product command."""
    _claim(repo, delivery_id, batch_id)
    repo.set_delivery_issue(
        delivery_id, batch_id, state="needs_verification", error_code=error_code
    )


def _suspend(repo, delivery_id, batch_id, *, state="suspended", error_code="paused"):
    _claim(repo, delivery_id, batch_id)
    repo.release_delivery(delivery_id, batch_id, state=state, error_code=error_code)


def _seed_pages(
    db_path,
    *,
    batch_id,
    job_id,
    start=1,
    count=1,
    state="pending",
    current_batch_null=False,
):
    """Insert synthetic pages directly; only the read path is under test here.

    The accept pipeline needs several bounded acceptance calls for thousands of
    pages, which would make a read-path scale test about the planner instead.
    Every inserted row satisfies the ledger constraints; the delivery texts are
    never parsed by the queries under test.
    """
    now = "2026-11-14T00:00:00+00:00"
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        with conn:
            for index in range(start, start + count):
                artwork = f"{index:08d}"
                relative = f"downl_Pixiv/{artwork}_p0.png"
                key = hashlib.sha256(relative.encode("utf-8")).hexdigest()
                delivery_id = "d-" + artwork
                conn.execute(
                    "INSERT INTO batch_items (batch_id, illust_id, artist_id,"
                    " page_count, decision, reason_codes_json, required_pages_json,"
                    " result_state, provenance_json, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, artwork, ARTIST, 1, "download", "[]", "[]",
                     "pending", "{}", now, now),
                )
                conn.execute(
                    "INSERT INTO deliveries (delivery_id, job_id, origin_batch_id,"
                    " current_batch_id, illust_id, page_index, output_key,"
                    " output_ref_json, quality_json, quality_key, payload_json,"
                    " state, generation, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (delivery_id, job_id, batch_id,
                     None if current_batch_null else batch_id,
                     artwork, 0, key,
                     json.dumps({"kind": "project", "root_id": "project",
                                 "relative_path": relative}),
                     "{}", "q", "{}", state, 1, now, now),
                )
                conn.execute(
                    "INSERT INTO batch_item_pages (batch_id, illust_id, page_index,"
                    " delivery_id, output_key, quality_json) VALUES (?,?,?,?,?,?)",
                    (batch_id, artwork, 0, delivery_id, key, "{}"),
                )
    finally:
        conn.close()


def _insert_paused_batch(db_path, job_id, batch_id, *, created_at):
    """One extra paused batch on the same job, inserted as a fixture row."""
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        with conn:
            conn.execute(
                "INSERT INTO batches (batch_id, job_id, command_id, budget_json, phase,"
                " created_at, ended_at) VALUES (?,?,?,?,?,?,?)",
                (batch_id, job_id, "cmd-" + batch_id, "{}", "paused", created_at, created_at),
            )
    finally:
        conn.close()


def _traced(repo, monkeypatch):
    """Record every statement the repository executes on its read connection."""
    seen: list[str] = []
    original = repo._readonly_connection

    def factory():
        conn = original()
        conn.set_trace_callback(lambda text: seen.append(" ".join(str(text).split())))
        return conn

    monkeypatch.setattr(repo, "_readonly_connection", factory)
    return seen


def _selects(seen):
    return [text for text in seen if text.upper().startswith("SELECT")]


# ---------------------------------------------------------------------------
# Empty library, validation and limits
# ---------------------------------------------------------------------------


def test_an_empty_library_lists_no_pages_and_a_zero_summary(repo):
    page = repo.list_verification_pages()
    assert page == {
        "rows": [],
        "next_cursor": None,
        "truncated": False,
        "stats": {
            "batch_id": None,
            "states": ["needs_verification", "recovery_needed"],
            "limit": 50,
            "total": 0,
            "returned": 0,
            "truncated": False,
        },
    }
    summary = repo.verification_summary()
    assert summary["counts"] == {state: 0 for state in LISTABLE_STATES}
    assert summary["total_pages"] == 0
    assert summary["linked_pages"] == 0
    assert summary["recoverable"] == 0
    assert summary["latest_batch_id"] is None
    assert summary["phase"] is None
    assert summary["known"] is False
    assert summary["truncated"] is False


def test_pending_pages_are_listed_with_the_verify_action(repo):
    job = _submit(repo)
    _accept(repo, job["batch_id"], ("123",), pages=3)
    page = repo.list_verification_pages(states=("pending",), limit=10)
    assert page["truncated"] is False
    assert page["next_cursor"] is None
    assert len(page["rows"]) == 3
    for row in page["rows"]:
        assert row["batch_id"] == job["batch_id"]
        assert row["job_id"] == job["job_id"]
        assert row["illust_id"] == "123"
        assert row["state"] == "pending"
        assert row["batch_phase"] == "running"
        assert row["recoverable"] is True
        assert row["actions"] == ["verify"]
        assert row["blocked_reason"] is None
        assert type(row["page_index"]) is int
        assert type(row["generation"]) is int
        assert isinstance(row["delivery_id"], str) and row["delivery_id"]
        assert isinstance(row["updated_at"], str) and row["updated_at"]
    assert [row["page_index"] for row in page["rows"]] == [0, 1, 2]
    assert page["stats"]["total"] == 3
    assert page["stats"]["returned"] == 3


def test_the_default_filter_covers_only_the_stopped_pair(repo):
    job = _submit(repo)
    batch_id = job["batch_id"]
    _accept(repo, batch_id, ("123",), pages=3)
    rows = _page_rows(repo, batch_id)
    _hold(repo, rows[0]["delivery_id"], batch_id)
    _suspend(repo, rows[1]["delivery_id"], batch_id)
    page = repo.list_verification_pages()
    assert [row["state"] for row in page["rows"]] == ["needs_verification"]
    assert page["rows"][0]["illust_id"] == rows[0]["illust_id"]
    assert page["rows"][0]["error_code"] == "artifact_missing"
    assert page["stats"]["total"] == 1
    suspended = repo.list_verification_pages(states=("suspended",))
    assert [row["state"] for row in suspended["rows"]] == ["suspended"]
    assert suspended["rows"][0]["recoverable"] is False
    assert suspended["rows"][0]["actions"] == []
    assert suspended["rows"][0]["blocked_reason"] is None


def test_the_state_filter_accepts_every_listable_state(repo):
    job = _submit(repo)
    batch_id = job["batch_id"]
    _accept(repo, batch_id, ("123",), pages=5)
    rows = _page_rows(repo, batch_id)
    _hold(repo, rows[0]["delivery_id"], batch_id)
    _claim(repo, rows[1]["delivery_id"], batch_id)
    repo.set_delivery_issue(
        rows[1]["delivery_id"], batch_id, state="recovery_needed", error_code="cleanup_failed"
    )
    _suspend(repo, rows[2]["delivery_id"], batch_id)
    _suspend(repo, rows[3]["delivery_id"], batch_id, state="retry_wait")
    page = repo.list_verification_pages(states=LISTABLE_STATES, limit=50)
    assert len(page["rows"]) == 5
    states = {row["state"] for row in page["rows"]}
    assert states == set(LISTABLE_STATES)
    assert page["stats"]["states"] == list(LISTABLE_STATES)
    recovered = {
        row["state"]: row["recoverable"] for row in page["rows"]
    }
    assert recovered["needs_verification"] is True
    assert recovered["pending"] is True
    assert recovered["recovery_needed"] is False
    assert recovered["suspended"] is False
    assert recovered["retry_wait"] is False


def test_an_unsupported_state_is_refused(repo):
    for states in (("failed",), ("completed",), ("transferring",), ("NEEDS_VERIFICATION",)):
        with pytest.raises(LibraryError):
            repo.list_verification_pages(states=states)


def test_a_bare_string_state_is_refused(repo):
    with pytest.raises(LibraryError):
        repo.list_verification_pages(states="suspended")


def test_an_empty_state_sequence_is_refused(repo):
    for states in ((), []):
        with pytest.raises(LibraryError):
            repo.list_verification_pages(states=states)


def test_the_limit_is_bounded_and_reports_truncation(repo):
    job = _submit(repo)
    _accept(repo, job["batch_id"], ("123",), pages=3)
    page = repo.list_verification_pages(states=("pending",), limit=2)
    assert len(page["rows"]) == 2
    assert page["truncated"] is True
    assert page["next_cursor"] is not None
    assert page["next_cursor"] == (
        page["rows"][-1]["batch_id"],
        page["rows"][-1]["illust_id"],
        page["rows"][-1]["page_index"],
        page["rows"][-1]["delivery_id"],
    )
    exact = repo.list_verification_pages(states=("pending",), limit=3)
    assert len(exact["rows"]) == 3
    assert exact["truncated"] is False
    assert exact["next_cursor"] is None
    for bad in (201, -1):
        with pytest.raises(LibraryError):
            repo.list_verification_pages(limit=bad)
    for bad in ("5", 5.0, True, None):
        with pytest.raises(LibraryError):
            repo.list_verification_pages(limit=bad)


def test_zero_limit_returns_counts_without_rows(repo):
    job = _submit(repo)
    _accept(repo, job["batch_id"], ("123",), pages=2)
    page = repo.list_verification_pages(states=("pending",), limit=0)
    assert page["rows"] == []
    assert page["truncated"] is False
    assert page["next_cursor"] is None
    assert page["stats"]["total"] == 2
    assert page["stats"]["returned"] == 0


# ---------------------------------------------------------------------------
# Cursor stability
# ---------------------------------------------------------------------------


def test_the_cursor_walks_every_row_exactly_once(repo):
    job = _submit(repo)
    batch_id = job["batch_id"]
    _accept(repo, batch_id, ("123", "456", "789"), pages=2)
    everything = repo.list_verification_pages(states=("pending",), limit=200)
    assert len(everything["rows"]) == 6
    walked = []
    cursor = None
    calls = 0
    while True:
        page = repo.list_verification_pages(
            states=("pending",), after=cursor, limit=2
        )
        calls += 1
        assert calls < 10
        walked.extend(page["rows"])
        if not page["truncated"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
    keys = [(row["illust_id"], row["page_index"]) for row in walked]
    assert keys == [(row["illust_id"], row["page_index"]) for row in everything["rows"]]
    assert len(set(keys)) == 6


def test_a_row_inserted_before_the_cursor_is_not_repeated(repo, db_path):
    job = _submit(repo)
    batch_id = job["batch_id"]
    _accept(repo, batch_id, ("500", "600", "700"), pages=1)
    first = repo.list_verification_pages(states=("pending",), limit=1)
    assert [row["illust_id"] for row in first["rows"]] == ["500"]
    _seed_pages(db_path, batch_id=batch_id, job_id=job["job_id"], start=100, count=1)
    walked = [row["illust_id"] for row in first["rows"]]
    cursor = first["next_cursor"]
    while cursor is not None:
        page = repo.list_verification_pages(states=("pending",), after=cursor, limit=1)
        walked.extend(row["illust_id"] for row in page["rows"])
        cursor = page["next_cursor"]
    assert walked == ["500", "600", "700"]
    assert len(set(walked)) == len(walked)


def test_a_row_inserted_after_the_cursor_is_seen_once(repo, db_path):
    job = _submit(repo)
    batch_id = job["batch_id"]
    _accept(repo, batch_id, ("500", "600", "700"), pages=1)
    first = repo.list_verification_pages(states=("pending",), limit=2)
    assert [row["illust_id"] for row in first["rows"]] == ["500", "600"]
    _seed_pages(db_path, batch_id=batch_id, job_id=job["job_id"], start=99999999, count=1)
    walked = [row["illust_id"] for row in first["rows"]]
    cursor = first["next_cursor"]
    while cursor is not None:
        page = repo.list_verification_pages(states=("pending",), after=cursor, limit=2)
        walked.extend(row["illust_id"] for row in page["rows"])
        cursor = page["next_cursor"]
    assert walked == ["500", "600", "700", "99999999"]
    assert len(set(walked)) == 4


def test_a_bad_cursor_is_refused(repo):
    for bad in (("a", "b", 0), ("a", "b", 0, 1), ("a", "b", -1, "d"), ("", "b", 0, "d")):
        with pytest.raises(LibraryError):
            repo.list_verification_pages(after=bad)
    for bad in (["a", "b", 0, "d"], "abcd"):
        with pytest.raises(LibraryError):
            repo.list_verification_pages(after=bad)


def test_two_thousand_pages_walk_once_with_two_statements_per_call(
    repo, db_path, monkeypatch
):
    job = _submit(repo)
    batch_id = job["batch_id"]
    _seed_pages(
        db_path, batch_id=batch_id, job_id=job["job_id"], start=1, count=2000
    )
    seen = _traced(repo, monkeypatch)
    walked = []
    cursor = None
    calls = 0
    while True:
        seen.clear()
        page = repo.list_verification_pages(states=("pending",), after=cursor, limit=200)
        calls += 1
        assert calls <= 10
        assert len(_selects(seen)) == 2
        assert page["stats"]["total"] == 2000
        walked.extend(row["delivery_id"] for row in page["rows"])
        if not page["truncated"]:
            break
        cursor = page["next_cursor"]
    assert calls == 10
    assert len(walked) == 2000
    assert len(set(walked)) == 2000
    assert walked[0] == "d-00000001"
    assert walked[-1] == "d-00002000"


def test_a_null_current_batch_is_paged_without_loss_or_repetition(
    repo, db_path, monkeypatch
):
    """deliveries.current_batch_id is nullable; the list key never uses it."""
    job = _submit(repo)
    batch_id = job["batch_id"]
    _accept(repo, batch_id, ("500",), pages=1)
    _seed_pages(
        db_path, batch_id=batch_id, job_id=job["job_id"], start=10, count=4,
        current_batch_null=True,
    )
    _seed_pages(db_path, batch_id=batch_id, job_id=job["job_id"], start=20, count=2)
    conn = repo._readonly_connection()
    try:
        null_owners = int(
            conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE current_batch_id IS NULL"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert null_owners == 4
    everything = repo.list_verification_pages(states=("pending",), limit=200)
    expected = [(row["illust_id"], row["page_index"]) for row in everything["rows"]]
    assert len(expected) == 7
    walked = []
    cursor = None
    while True:
        page = repo.list_verification_pages(states=("pending",), after=cursor, limit=1)
        walked.extend((row["illust_id"], row["page_index"]) for row in page["rows"])
        if not page["truncated"]:
            break
        cursor = page["next_cursor"]
    assert walked == expected
    assert len(set(walked)) == 7


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_the_summary_counts_match_the_ledger(repo):
    job = _submit(repo)
    batch_id = job["batch_id"]
    _accept(repo, batch_id, ("123", "456"), pages=3)
    rows = _page_rows(repo, batch_id)
    _hold(repo, rows[0]["delivery_id"], batch_id)
    _suspend(repo, rows[1]["delivery_id"], batch_id)
    _suspend(repo, rows[2]["delivery_id"], batch_id, state="retry_wait")
    _claim(repo, rows[3]["delivery_id"], batch_id)
    repo.set_delivery_issue(
        rows[3]["delivery_id"], batch_id, state="recovery_needed", error_code="cleanup_failed"
    )
    summary = repo.verification_summary(batch_id=batch_id)
    conn = repo._readonly_connection()
    try:
        truth = dict(
            conn.execute(
                "SELECT d.state, COUNT(*) FROM batch_item_pages AS bip"
                " JOIN deliveries AS d ON d.delivery_id = bip.delivery_id"
                " WHERE bip.batch_id = ? GROUP BY d.state",
                (batch_id,),
            ).fetchall()
        )
        links = int(
            conn.execute(
                "SELECT COUNT(*) FROM batch_item_pages WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    for state, number in truth.items():
        assert summary["counts"][state] == number
    for state in LISTABLE_STATES:
        assert summary["counts"][state] == truth.get(state, 0)
    assert summary["total_pages"] == links == 6
    assert summary["linked_pages"] == sum(truth.values()) == 6
    # Two untouched pending pages plus the held one; a suspended or
    # retry_wait page is not something the recovery entry may act on.
    assert summary["recoverable"] == 3
    assert summary["phase"] == "running"
    assert summary["known"] is True
    assert summary["latest_batch_id"] == batch_id
    assert summary["batch_id"] == batch_id


def test_the_summary_reports_the_latest_batch_and_its_phase(repo, db_path):
    job = _submit(repo)
    _accept(repo, job["batch_id"], ("123",), pages=1)
    _insert_paused_batch(
        db_path, job["job_id"], "batch-later", created_at="2030-01-01T00:00:00+00:00"
    )
    summary = repo.verification_summary()
    assert summary["latest_batch_id"] == "batch-later"
    assert summary["phase"] == "paused"
    assert summary["counts"]["pending"] == 1
    assert summary["total_pages"] == 1
    assert summary["recoverable"] == 1  # the page stays in its own running batch
    scoped = repo.verification_summary(batch_id="batch-later")
    assert scoped["known"] is True
    assert scoped["phase"] == "paused"
    assert scoped["total_pages"] == 0
    assert scoped["counts"]["pending"] == 0


def test_the_summary_of_an_unknown_batch_is_empty_not_an_error(repo):
    job = _submit(repo)
    _accept(repo, job["batch_id"], ("123",), pages=1)
    summary = repo.verification_summary(batch_id="missing-batch")
    assert summary["known"] is False
    assert summary["phase"] is None
    assert summary["batch_id"] == "missing-batch"
    assert summary["total_pages"] == 0
    assert summary["linked_pages"] == 0
    assert summary["recoverable"] == 0
    assert summary["counts"] == {state: 0 for state in LISTABLE_STATES}


def test_the_list_of_an_unknown_batch_is_empty_not_an_error(repo):
    job = _submit(repo)
    _accept(repo, job["batch_id"], ("123",), pages=1)
    page = repo.list_verification_pages(batch_id="missing-batch", states=("pending",))
    assert page["rows"] == []
    assert page["stats"]["total"] == 0
    assert page["truncated"] is False


def test_a_batch_filter_keeps_other_batches_out(repo, db_path):
    job = _submit(repo)
    _accept(repo, job["batch_id"], ("500",), pages=1)
    _insert_paused_batch(
        db_path, job["job_id"], "batch-other", created_at="2031-01-01T00:00:00+00:00"
    )
    _seed_pages(
        db_path,
        batch_id="batch-other",
        job_id=job["job_id"],
        start=900,
        count=2,
        state="needs_verification",
    )
    page = repo.list_verification_pages(batch_id="batch-other", states=("needs_verification",))
    assert [row["illust_id"] for row in page["rows"]] == ["00000900", "00000901"]
    assert {row["batch_id"] for row in page["rows"]} == {"batch-other"}
    assert page["rows"][0]["batch_phase"] == "paused"
    assert page["rows"][0]["recoverable"] is False
    assert page["rows"][0]["blocked_reason"] == "batch_not_running"
    both = repo.list_verification_pages(states=("needs_verification",))
    assert {row["batch_id"] for row in both["rows"]} == {"batch-other"}


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def test_the_rows_never_carry_a_plaintext_path(repo):
    job = _submit(repo)
    _accept(repo, job["batch_id"], ("123",), pages=2)
    page = repo.list_verification_pages(states=("pending",))
    assert page["rows"]
    for row in page["rows"]:
        assert "downl_Pixiv" not in json.dumps(row, ensure_ascii=False)
        assert "downl_Pixiv" not in row["output_key"]
        assert row["output_key"]
        assert set(row) == {
            "batch_id", "job_id", "illust_id", "page_index", "delivery_id",
            "state", "error_code", "generation", "updated_at", "output_key",
            "batch_phase", "recoverable", "actions", "blocked_reason",
        }


def test_reading_does_not_modify_any_row(repo):
    job = _submit(repo)
    batch_id = job["batch_id"]
    _accept(repo, batch_id, ("123",), pages=2)
    rows = _page_rows(repo, batch_id)
    _hold(repo, rows[0]["delivery_id"], batch_id)

    def snapshot():
        conn = repo._readonly_connection()
        try:
            return (
                [tuple(row) for row in conn.execute("SELECT * FROM deliveries ORDER BY rowid")],
                [tuple(row) for row in conn.execute(
                    "SELECT * FROM batch_item_pages ORDER BY rowid")],
                [tuple(row) for row in conn.execute("SELECT * FROM batches ORDER BY rowid")],
                int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
            )
        finally:
            conn.close()

    before = snapshot()
    repo.list_verification_pages(states=LISTABLE_STATES)
    repo.verification_summary()
    repo.verification_summary(batch_id=batch_id)
    assert snapshot() == before


def test_an_uncommitted_write_is_not_visible_and_does_not_block(repo, db_path):
    job = _submit(repo)
    batch_id = job["batch_id"]
    _accept(repo, batch_id, ("123",), pages=1)
    rows = _page_rows(repo, batch_id)
    _hold(repo, rows[0]["delivery_id"], batch_id)
    writer = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    writer.execute("PRAGMA busy_timeout=30000")
    writer.execute("BEGIN IMMEDIATE")
    try:
        writer.execute(
            "UPDATE deliveries SET state = 'suspended' WHERE delivery_id = ?",
            (rows[0]["delivery_id"],),
        )
        started = time.monotonic()
        page = repo.list_verification_pages(states=LISTABLE_STATES)
        elapsed = time.monotonic() - started
        assert elapsed < 5, "a read must not wait for the writer to commit"
        assert [row["state"] for row in page["rows"]] == ["needs_verification"]
        assert repo.verification_summary()["counts"]["needs_verification"] == 1
        assert repo.verification_summary()["counts"]["suspended"] == 0
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    after = repo.list_verification_pages(states=LISTABLE_STATES)
    assert [row["state"] for row in after["rows"]] == ["needs_verification"]


# ---------------------------------------------------------------------------
# Batch phase and the recovery decision table
# ---------------------------------------------------------------------------


def test_a_held_page_in_a_paused_batch_is_listed_but_not_recoverable(repo):
    job = _submit(repo)
    batch_id = job["batch_id"]
    _accept(repo, batch_id, ("123",), pages=2)
    rows = _page_rows(repo, batch_id)
    _hold(repo, rows[0]["delivery_id"], batch_id)
    repo.request_stop(job["job_id"], "stop-paused")
    assert repo.settle_stop(batch_id) is True
    page = repo.list_verification_pages(states=("needs_verification",))
    assert len(page["rows"]) == 1
    row = page["rows"][0]
    assert row["state"] == "needs_verification"
    assert row["batch_phase"] == "paused"
    assert row["recoverable"] is False
    assert row["actions"] == []
    assert row["blocked_reason"] == "batch_not_running"
    asserted = repo.verification_summary(batch_id=batch_id)
    assert asserted["phase"] == "paused"
    assert asserted["counts"]["needs_verification"] == 1
    assert asserted["counts"]["suspended"] == 1
    assert asserted["recoverable"] == 0


def test_the_listed_page_coordinates_drive_the_recovery_entry(repo, tmp_path):
    repo.bind_path_resolver(PathResolver(AppPaths(tmp_path)))
    job = _submit(repo)
    batch_id = job["batch_id"]
    _accept(repo, batch_id, ("123",), pages=2)
    rows = _page_rows(repo, batch_id)
    _hold(repo, rows[0]["delivery_id"], batch_id)
    page = repo.list_verification_pages(states=("needs_verification",))
    row = page["rows"][0]
    result = repo.recover_verification_page(
        row["batch_id"], row["illust_id"], row["page_index"]
    )
    assert result["outcome"] == "held"
    # No artifact was ever recorded for the page, so the truthful reason is
    # the missing evidence itself; the row stays a hold either way.
    assert result["reason"] == "no_recorded_artifact"
    assert result["state"] == "needs_verification"
    assert result["previous_delivery_id"] == row["delivery_id"]
    again = repo.list_verification_pages(states=("needs_verification",))
    assert [item["delivery_id"] for item in again["rows"]] == [row["delivery_id"]]
    pending = repo.list_verification_pages(states=("pending",))
    assert len(pending["rows"]) == 1
    queue = repo.recover_verification_page(
        pending["rows"][0]["batch_id"],
        pending["rows"][0]["illust_id"],
        pending["rows"][0]["page_index"],
    )
    assert queue["outcome"] == "repair_queued"
    assert queue["reason"] == "repair_already_pending"


def test_the_recoverable_states_match_the_recovery_decision_table():
    from core.delivery_repository import (
        _RECOVERABLE_PAGE_STATES,
        _VERIFICATION_DEFAULT_STATES,
        _VERIFICATION_LIST_STATES,
        _VERIFICATION_MAX_LIMIT,
    )

    assert _VERIFICATION_LIST_STATES == LISTABLE_STATES
    assert _VERIFICATION_DEFAULT_STATES == ("needs_verification", "recovery_needed")
    assert _VERIFICATION_MAX_LIMIT == 200
    decidable = {
        state
        for state in LISTABLE_STATES
        if decide_page_recovery(
            state, LocalState.HASH_MATCH, reusable=True
        ).outcome
        is not RecoveryOutcome.REFUSED
    }
    assert decidable == {"needs_verification", "pending"}
    assert set(_RECOVERABLE_PAGE_STATES) == decidable
