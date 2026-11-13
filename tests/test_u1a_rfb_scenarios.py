"""Independent RFB acceptance scenarios (T lane, contract v4).

Every case below pins one requirement of
`docs/optimization/2026-11-14/U1A_STATE_CONTRACT_V4.md` and is expected to be
RED at the round base (0b2b29c) because the product does not implement that
requirement yet -- never because of a fixture or syntax problem.

Synthetic SQLite, synthetic PNG and temporary directories only.  No network, no
account, no production library and no real gallery is touched.

Mapping (contract section -> test):

* Sec.2.2   an in-place equal-size edit is held even with the mtime restored
* Sec.2.2   the confirm pass opens every reused artifact once; a replay opens none
* Sec.3.2   a failed progress write finishes the page in one attempt
* Sec.3.2   a truncated progress cache cannot turn a page into an endless retry
* Sec.3.2/6 32/33/64/256/257 files commit inside the page budget
* Sec.6     a single file cannot silently exceed the page byte budget
* Sec.4     the examine budget is not the end of the cleanup queue
* Sec.4     two tiers advance behind their own bad prefixes
* Sec.5     an unreadable ledger is a storage fault, not a recovery state
* Sec.7     a three layer handover reaches the older completed artifact
* Sec.7     a corrupt intermediate target is refused, never replaced by rowid
* probes    the imported RFA/RFB audit probes keep their original functions
"""
from __future__ import annotations

import ast
import os
import sqlite3
from pathlib import Path

import pytest

import test_u1a_recovery as h
import test_u1a_reuse_gate as g
import test_u1a_rfa_delivery as d
import test_u1a_rfa_lineage as l
import test_u1a_rfa_scenarios as s
from core.delivery_lineage import resolve_successor_ex
from core.library_repository import LibraryError, LibraryRepository
from core.observation_snapshot import PageBudget, PreflightCancelled
from test_u1a_rf_recovery import clone_delivery

TARGET = h.ARTWORK
PROBE_DIR = Path(__file__).resolve().parents[1]


def accept(repo, batch_id, page, budget=None):
    """One acceptance call; the budget defaults to the product's own default."""
    return repo.accept_source_page(
        batch_id, page, page_budget=PageBudget() if budget is None else budget
    )


def delivery_row(repo, delivery_id):
    with sqlite3.connect(repo.database_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()


def test_an_in_place_equal_size_edit_is_held_even_when_mtime_is_restored(tmp_path, monkeypatch):
    """Sec.2.2: a cached hash never approves the bytes that are on disk now."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, seed_ids = s.seed(repo, root, pages=(0, 1), request_id="seed-inplace")
        page_one_artifact = repo.get_artifact(seed_ids[1])["artifact_id"]
        job = h.submit(repo, request_id="inplace-reuse")
        page = h.source((g.candidate(pages=(0, 1)),))
        with pytest.raises(PreflightCancelled):
            accept(repo, job["batch_id"], page, PageBudget(max_files=1, max_slices=1))

        path = h.target(root)
        before = path.stat()
        path.write_bytes(h.SAME_SIZE_PNG)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        assert (before.st_dev, before.st_ino, before.st_size) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ), "precondition: the edit keeps the inode, the size and the mtime"

        resumed = s.resume_job(repo, job, "resume-inplace")
        accept(repo, resumed["batch_id"], page)

        rows = s.page_links(repo, resumed["batch_id"])
        assert len(rows) == 2, rows
        held, reused = rows[0], rows[1]
        assert held[1] is None and reused[0] is None, (
            "the edited page is a delivery hold, the untouched page is an "
            f"artifact link: {rows!r}"
        )
        held_row = delivery_row(repo, held[0])
        assert held_row["state"] == "needs_verification", dict(held_row)
        assert held_row["error_code"] in {
            "artifact_changed",
            "artifact_changed_during_check",
        }, held_row["error_code"]
        assert reused[1] == page_one_artifact, (
            "Sec.2.2: the untouched page must be reused (same artifact), not "
            f"downloaded again; got {reused[1]!r}"
        )
        assert h.target(root).read_bytes() == h.SAME_SIZE_PNG
        assert s.checkpoint(repo, resumed) is not None


def test_the_confirm_pass_opens_each_reused_artifact_once_and_replay_opens_none(
    tmp_path, monkeypatch
):
    """Sec.2.1/2.2: 32 hints + 1 slice = 33 opens; a receipt replay opens none."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        s.seed(repo, root, pages=tuple(range(33)), request_id="seed-confirm")
        job = h.submit(repo, request_id="confirm-33")
        page = h.source((g.candidate(pages=tuple(range(33))),))
        with pytest.raises(PreflightCancelled) as first:
            accept(repo, job["batch_id"], page, PageBudget(max_files=32, max_slices=1))
        assert first.value.reason == "page_slice_limit"
        assert s.checkpoint(repo, job) is None

        resumed = s.resume_job(repo, job, "resume-confirm")
        phases = s.instrument_phases(repo, monkeypatch)
        accept(repo, resumed["batch_id"], page)
        assert len(phases["slice"]) == 1, phases
        assert len(phases["confirm"]) == 32, phases
        assert set(phases["slice"]) | set(phases["confirm"]) == set(
            s.all_artifacts(repo)
        )
        assert s.checkpoint(repo, resumed) is not None
        assert len(s.page_links(repo, resumed["batch_id"])) == 33

        phases["boundary"] = False
        phases["slice"].clear()
        phases["confirm"].clear()
        accept(repo, resumed["batch_id"], page)
        assert phases["slice"] == [] and phases["confirm"] == [], (
            "Sec.2.1: an accepted receipt replays with zero hashing; got "
            f"{phases}"
        )


def test_a_failed_progress_write_finishes_the_page_in_one_attempt(tmp_path, monkeypatch):
    """Sec.3.2: without durable progress a slice cap could never converge."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        s.seed(repo, root, pages=tuple(range(6)), request_id="seed-write")
        job = h.submit(repo, request_id="progress-write")
        page = h.source((g.candidate(pages=tuple(range(6))),))
        seen = s.observe(repo, monkeypatch)

        def unavailable(*args, **kwargs):
            raise LibraryError("synthetic progress write refusal")

        monkeypatch.setattr(repo, "record_page_observation", unavailable)
        outcomes = []
        for attempt in range(4):
            try:
                accept(repo, job["batch_id"], page, PageBudget(max_files=1, max_slices=2))
                outcomes.append("committed")
                break
            except PreflightCancelled as exc:
                outcomes.append(exc.reason)
                job = s.resume_job(repo, job, f"resume-write-{attempt}")
        assert outcomes == ["committed"], (
            "Sec.3.2: a failed progress write must finish the page in one "
            f"attempt, not produce another retryable slice result; got {outcomes}"
        )
        assert s.checkpoint(repo, job) is not None
        assert len(s.page_links(repo, job["batch_id"])) == 6
        assert len(seen) == 6


def test_a_truncated_progress_cache_cannot_turn_a_page_into_an_endless_retry(
    tmp_path, monkeypatch
):
    """Sec.3.2/3.3: a cache smaller than the page must not repeat forever."""
    import core.library_repository as lib

    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        s.seed(repo, root, pages=tuple(range(6)), request_id="seed-cap")
        job = h.submit(repo, request_id="progress-cap")
        page = h.source((g.candidate(pages=tuple(range(6))),))
        monkeypatch.setattr(lib, "MAX_PROGRESS_RECORDS", 3)
        outcomes = []
        for attempt in range(8):
            try:
                accept(repo, job["batch_id"], page, PageBudget(max_files=1, max_slices=2))
                outcomes.append("committed")
                break
            except PreflightCancelled as exc:
                outcomes.append(exc.reason)
                job = s.resume_job(repo, job, f"resume-cap-{attempt}")
        assert "committed" in outcomes, (
            "Sec.3.2: a retained capacity below the page size must not turn the "
            f"page into an endless retry; got {outcomes}"
        )


@pytest.mark.parametrize("pages,calls", [(32, 1), (33, 1), (64, 1), (256, 1), (257, 2)])
def test_page_sizes_commit_inside_the_page_budget(tmp_path, pages, calls):
    """Sec.3.2/6: eight 32 file slices cover exactly 256; one more needs a call."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        s.seed(repo, root, pages=tuple(range(pages)), request_id=f"seed-size-{pages}")
        job = h.submit(repo, request_id=f"size-{pages}")
        page = h.source((g.candidate(pages=tuple(range(pages))),))
        for call in range(calls):
            try:
                accept(repo, job["batch_id"], page)
            except PreflightCancelled as exc:
                assert exc.reason == "page_slice_limit", exc.reason
                assert call + 1 < calls, (
                    f"{pages} files must commit within {calls} calls, "
                    f"stopped at {exc.reason!r}"
                )
                assert s.checkpoint(repo, job) is None
                job = s.resume_job(repo, job, f"resume-size-{pages}-{call}")
        assert s.checkpoint(repo, job) is not None
        assert len(s.page_links(repo, job["batch_id"])) == pages


def test_a_file_cannot_silently_exceed_the_page_byte_budget(tmp_path):
    """Sec.6: the page byte budget is checked before the file is opened."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        s.seed(repo, root, pages=(0,), request_id="seed-byte")
        job = h.submit(repo, request_id="byte-budget")
        page = h.source((g.candidate(pages=(0,)),))
        size = h.target(root).stat().st_size
        with pytest.raises(PreflightCancelled, match="page_byte_budget"):
            accept(repo, job["batch_id"], page, PageBudget(max_bytes=size - 1))
        assert s.checkpoint(repo, job) is None
        try:
            accept(repo, job["batch_id"], page, PageBudget(max_bytes=size))
        except PreflightCancelled as exc:
            pytest.fail(
                "Sec.6: a page that fits the exact byte budget must commit, got "
                f"{exc.reason!r}"
            )
        assert s.checkpoint(repo, job) is not None


@pytest.mark.parametrize("limit", [1, 200])
def test_an_examine_budget_is_not_the_end_of_the_cleanup_queue(tmp_path, monkeypatch, limit):
    """Sec.4: a full examine window advances the cursor, it does not wrap it."""
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        first, service = d.cancelled(repo, tmp_path)
        assert service.cleanup_delivery(first)
        bad = 2 * min(limit * 5, 2000) + 1
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            for index in range(bad + 1):
                ident = f"{index + 1:032x}"
                d.clone_cancelled(conn, first, ident, index + 2)
                if index < bad:
                    conn.execute(
                        "UPDATE deliveries SET prepared_json='{invalid' WHERE delivery_id=?",
                        (ident,),
                    )
            tail = ident
            conn.commit()

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
        assert tail in visits, (
            "Sec.4: a bad prefix longer than the examine budget must not hide "
            f"the valid tail; visits={visits!r} cursors={cursors!r}"
        )
        # A wrap is legal in the very scan that proved EOF, so the property is
        # "strictly advancing until the first wrap", not "the wrap is late".
        first_zero = cursors.index(0) if 0 in cursors else len(cursors)
        prefix = cursors[:first_zero]
        assert first_zero > 0, (
            "Sec.4: the cursor must not wrap before a capped scan advanced it; "
            f"got {cursors!r}"
        )
        assert all(a < b for a, b in zip(prefix, prefix[1:])), (
            "Sec.4: every capped scan must advance the cursor, and a wrap is "
            f"legal only after that scan proved EOF; got {cursors!r}"
        )


def test_two_cleanup_tiers_advance_behind_their_own_bad_prefixes(tmp_path, monkeypatch):
    """Sec.4: tier0 exhaustion keeps the cursor and does not eat tier1."""
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        first, service = d.cancelled(repo, tmp_path)
        assert service.cleanup_delivery(first)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            for index in range(51):
                ident = f"{index + 1:032x}"
                d.clone_cancelled(conn, first, ident, index + 2)
                if index < 50:
                    conn.execute(
                        "UPDATE deliveries SET prepared_json='{invalid' WHERE delivery_id=?",
                        (ident,),
                    )
                else:
                    good0 = ident
            for index in range(4):
                ident = f"{index + 100:032x}"
                d.clone_cancelled(conn, first, ident, index + 200)
                if index < 3:
                    conn.execute(
                        "UPDATE deliveries SET prepared_json='{invalid' WHERE delivery_id=?",
                        (ident,),
                    )
                else:
                    good1 = ident
            conn.commit()

        s.write_cleanup_fact(repo, good1, outcome="failed", error_code="cleanup_failed")
        stats1 = {}
        first_pass = repo.list_cleanup_candidates(limit=8, stats=stats1)
        assert stats1["tier0_budget_exhausted"] is True, stats1
        assert good0 not in {item["delivery_id"] for item in first_pass}
        assert good1 in {item["delivery_id"] for item in first_pass}, (
            "Sec.4: the failed row behind its own bad prefix must still be "
            f"offered; got {[item['delivery_id'] for item in first_pass]!r}"
        )
        stats2 = {}
        second_pass = repo.list_cleanup_candidates(
            limit=8, after_rowid=stats1["next_rowid"], stats=stats2
        )
        assert good0 in {item["delivery_id"] for item in second_pass}, (
            "Sec.4: the next window must continue after the examined prefix; "
            f"got {[item['delivery_id'] for item in second_pass]!r} stats={stats2!r}"
        )
        assert stats2["next_rowid"] > stats1["next_rowid"]


def test_an_unreadable_ledger_is_a_storage_fault_and_not_a_recovery_state(
    tmp_path, monkeypatch
):
    """Sec.5: a readable CAS loser is a classification, an unreadable ledger is not."""
    from core.page_delivery import _StorageFault

    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, (ident,) = d.prepared(repo, tmp_path, request_id="unreadable-ledger")
        staged = list(tmp_path.rglob("*.part"))
        assert staged, "precondition: a prepared delivery keeps its staged bytes"
        service = h.executor(repo, tmp_path, h.Transport())

        def unreadable(*args, **kwargs):
            raise LibraryError("synthetic ledger is unreadable")

        monkeypatch.setattr(repo, "publish_authorized", unreadable)
        with pytest.raises(_StorageFault):
            service.recover_local()
        assert repo.get_delivery(ident)["state"] == "prepared", (
            "Sec.5: an unreadable ledger must not rewrite the row"
        )
        assert all(path.exists() for path in staged)

        # Contrast: the same ledger is readable, only the compensation write is
        # refused; that is a recovery classification, not a storage fault.
        row = repo.get_delivery(ident)

        def refuse(*args, **kwargs):
            raise LibraryError("synthetic compensation refusal")

        monkeypatch.setattr(repo, "set_delivery_issue", refuse)
        outcome, _code = service._classify_after_race(row, "cancel_state_changed")
        assert outcome == "recovery_needed", (
            "Sec.5: a readable ledger with a refused compensation write reports "
            f"recovery_needed; got {outcome!r}"
        )


def _three_layer_chain(repo, root, *, target_artifact):
    """A (older completed) <- B (superseded, handover) for the anchor A."""
    job, anchor, completed = h.queued_repair(repo, root)
    artifact = target_artifact(repo, completed)
    with sqlite3.connect(repo.database_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE deliveries SET state='cancelled', "
            "error_code='superseded_by_recovery_repair' WHERE delivery_id=?",
            (anchor,),
        )
        middle = clone_delivery(
            conn,
            anchor,
            "c" * 32,
            generation=2,
            state="cancelled",
            error_code="superseded_by_recovery_reuse",
        )
        l.insert_event(
            conn,
            job_id=job["job_id"],
            batch_id=job["batch_id"],
            payload={"delivery_id": middle, "artifact_id": artifact, "action": "reuse"},
        )
        conn.commit()
        anchor_row = l.row(conn, anchor)
    return job, anchor, middle, completed, artifact, anchor_row


def test_a_superseded_intermediate_handover_reaches_the_older_completed_artifact(
    tmp_path,
):
    """Sec.7: the bounded node set must follow an intermediate node's event."""
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, anchor, middle, completed, artifact, anchor_row = _three_layer_chain(
            repo, tmp_path, target_artifact=l.artifact_id_of
        )
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            middle_result = resolve_successor_ex(conn, l.row(conn, middle))
            anchor_result = resolve_successor_ex(conn, anchor_row)
        assert middle_result.successor is not None
        assert middle_result.successor.delivery_id == completed
        assert anchor_result.successor is not None, (
            "Sec.7: A must resolve through B's recorded handover instead of "
            f"reporting no successor; got {anchor_result!r}"
        )
        assert anchor_result.successor.delivery_id == completed

        h.target(tmp_path).write_bytes(h.PNG_BYTES)
        result = repo.recover_verification_page(job["batch_id"], TARGET, 0)
        assert result["outcome"] == "reused", (
            "Sec.7: the public recovery entry must follow the retained chain; "
            f"got {result!r}"
        )
        assert result["artifact_id"] == artifact


def test_a_corrupt_intermediate_target_is_refused_and_never_replaced_by_rowid(
    tmp_path,
):
    """Sec.7: an explicit bad target is a refusal, not a silent fallback."""
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, anchor, middle, completed, _artifact, anchor_row = _three_layer_chain(
            repo,
            tmp_path,
            target_artifact=lambda _repo, delivery: "f" * 32,
        )
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            anchor_result = resolve_successor_ex(conn, anchor_row)
        assert anchor_result.successor is None, (
            "Sec.7: an unusable handover target must not be replaced by another "
            f"completed row; got {anchor_result!r}"
        )
        assert anchor_result.reason == "event_target_unusable", anchor_result.reason

        h.target(tmp_path).write_bytes(h.PNG_BYTES)
        result = repo.recover_verification_page(job["batch_id"], TARGET, 0)
        assert result["outcome"] == "refused", (
            "Sec.7: the public entry must refuse instead of guessing; got "
            f"{result!r}"
        )


def test_the_imported_audit_probes_keep_their_original_functions():
    """The imported probes may change only their module header and ROOT line."""
    pairs = [
        (
            PROBE_DIR / "docs/audits/2026-11-13-u1a-rf-recheck/evidence/test_rf_recheck.py",
            PROBE_DIR / "tests/test_u1a_rfa_boundaries.py",
        ),
        (
            PROBE_DIR / "docs/audits/2026-11-14-u1a-rfa-recheck/evidence/test_rfa_recheck.py",
            PROBE_DIR / "tests/test_u1a_rfb_boundaries.py",
        ),
    ]

    def functions(path):
        return {
            node.name: ast.dump(node, include_attributes=False)
            for node in ast.parse(path.read_text(encoding="utf-8")).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    for original, imported in pairs:
        before, after = functions(original), functions(imported)
        assert before == after, (
            f"{imported.name} must keep every audit function verbatim; "
            f"different={sorted(set(before) ^ set(after))} "
            f"changed={sorted(name for name in before.keys() & after.keys() if before[name] != after[name])}"
        )

