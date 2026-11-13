"""Independent RFA acceptance scenarios (T lane).

These are the *next round's* acceptance input: every case below states a
requirement frozen in docs/optimization/2026-11-14/U1A_STATE_CONTRACT_V3.md and
is expected to be RED at 336541b because the product does not implement that
requirement yet -- never because of a fixture or syntax problem.

Synthetic SQLite, synthetic PNG and temporary directories only.  No network,
no account, no production library and no real gallery is touched.

Mapping (contract section -> test):

* R-A  Sec.1.2/1.4/1.6  progress survives an attempt, a page replays for free
* R-A  Sec.1.1          max_slices is a budget result, never a file verdict
* R-A  Sec.1.4          a page of many multi-page works fits the slice budget
* R-A  Sec.1.3          only fresh progress may replace a real observation
* R-A  Sec.1.1          a pause between slices keeps the finished slice
* R-A  Sec.1.3.1        a moved artifact row invalidates its progress record
* R-A  Sec.1.3.6        a moved disk identity invalidates the progress record
* R-A  Sec.1.3.6        a record without an object identity is not evidence
* R-A  Sec.1.3.6/1.1    a matching identity is reused and its stat costs no slot
* R-B  Sec.2.2          an unreachable root never cancels a delivery
* R-B  Sec.2.3/2.4      cleanup claims nothing while offline, retries after
* R-B  Sec.2.4          legacy "cleaned" facts and bad rows do not hold the window
* R-B  Sec.2.4          the candidate window can advance past its first page
* R-B  Sec.2.6          a losing recovery reports the winner instead of raising
* R-C  Sec.3.1          a handover target of foreign quality is refused
* R-C  Sec.3.1          a handover without its artifact is not replaced by rowid
* R-C  Sec.3.2/3.3      LineageResult carries reason, queries and nodes
* R-C  Sec.3.3          a long chain stays inside the query budget
* R-C  Sec.3.1/3.3      a wide fan expands each node at most once
* R-C  Sec.3.1          the read path follows the recorded handover
* R-C  Sec.3.1          the write path (recovery) follows the same handover

Measured red baseline at 336541b: 21 tests, 21 failed, 0 errors, 42.9 s
(`lanes/tests/run09.xml`), none for a fixture or syntax reason:

* missing interface x3: `PageBudget(max_slices=...)` (two cases) and
  `LineageResult`/`queries` from `core.delivery_lineage`
* no resumable progress x7: the resumed / paused / expired / moved-row /
  moved-identity / anonymous-identity attempts hit `page_file_budget` again
  instead of reusing a persisted slice, a 36 file page fails instead of taking
  two slices, and 32 matching stat rechecks still consume `max_files=1`
* R-B x5: an unreachable root cancels the row, cleanup claims "already absent",
  a legacy `outcome=cleaned` fact stays in the window, an unparsable row
  starves `limit=1`, and a losing compensation raises `_StorageFault`
* R-C x6: a foreign-quality and a missing handover target silently fall back to
  rowid, `LineageResult` is absent, a 64 link chain runs 41,449 scans, a 24
  wide fan runs 12,951, and both the read path and recovery follow the newer
  unrelated pending instead of the recorded handover
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import test_u1a_recovery as h
import test_u1a_reuse_gate as g
from core.library_repository import LibraryRepository
from core.task_contracts import CandidateDecision, PageSpec, PathRef, canonical_json
from test_u1a_rf_recovery import clone_delivery

TARGET = h.ARTWORK
ENTITY = "source-1"


# ---------------------------------------------------------------------------
# Local helpers (synthetic only)
# ---------------------------------------------------------------------------


def settle(repo, job, complete=True):
    repo.finish_source(job["batch_id"], coverage_complete=complete)
    assert repo.settle_batch(job["batch_id"])


def work(illust_id, pages):
    """One multi-page work as the source would deliver it."""
    specs = tuple(
        PageSpec(
            index,
            PathRef(relative_path=f"downl_Pixiv/{illust_id}_p{index}.png"),
            f"https://i.pximg.net/img-original/2026/{illust_id}_p{index}.png",
        )
        for index in pages
    )
    return CandidateDecision(
        illust_id, "download", artist_id="7", page_count=len(specs), pages=specs
    )


def checkpoint(repo, job):
    return repo.get_checkpoint(job["job_id"], "follow", "history", ENTITY)


def page_links(repo, batch_id):
    with sqlite3.connect(repo.database_path) as conn:
        return conn.execute(
            "SELECT delivery_id, artifact_id FROM batch_item_pages "
            "WHERE batch_id = ? ORDER BY illust_id, page_index",
            (batch_id,),
        ).fetchall()


def seed(repo, root, *, pages=None, candidates=None, request_id="seed", max_works=1):
    """Deliver one synthetic source page so later tasks can reuse its artifacts."""
    job = h.submit(repo, request_id=request_id, max_works=max_works)
    page = h.source(
        candidates if candidates is not None else (g.candidate(pages=tuple(pages)),),
        page_identity="seed-" + request_id,
    )
    ids = h.accept(repo, job["batch_id"], page)
    service = h.executor(repo, root, h.Transport(*(h.Response() for _ in ids)))
    for delivery in ids:
        assert h.run(repo, job, delivery, service).state == "completed"
    settle(repo, job)
    return job, ids


def observe(repo, monkeypatch, *, cancel=None, cancel_after=None):
    """Record every artifact the preflight really observes (Sec.1.1/1.3)."""
    seen = []
    original = repo._observe_artifact

    def counted(artifact, **kwargs):
        seen.append(str(artifact["artifact_id"]))
        if cancel is not None and cancel_after is not None and len(seen) == cancel_after:
            cancel.set()
        return original(artifact, **kwargs)

    monkeypatch.setattr(repo, "_observe_artifact", counted)
    return seen


def instrument_observations(repo, monkeypatch, ids, records=None):
    """Wrap _observe_artifact once; the wrapper keeps the real bound method."""
    original = repo._observe_artifact

    def counted(artifact, **kwargs):
        ids.append(str(artifact["artifact_id"]))
        record = original(artifact, **kwargs)
        if records is not None:
            records.append(record)
        return record

    monkeypatch.setattr(repo, "_observe_artifact", counted)
    return counted


def prepared_reuse_page(repo, root, monkeypatch, *, request_id, pages=33):
    """Seed one page, then capture a real 32 file slice of the reuse job.

    The captured records are the product's own observations, so their
    ArtifactHealth.identity is the true (st_dev, st_ino) of the synthetic file
    -- never a hard coded value (contract 1.3 item 6).
    """
    from core.observation_snapshot import PageBudget, PreflightCancelled

    seed(repo, root, pages=tuple(range(pages)), request_id="seed-" + request_id)
    job = h.submit(repo, request_id=request_id)
    page = h.source((g.candidate(pages=tuple(range(pages))),))
    seen, captured = [], []
    instrument_observations(repo, monkeypatch, seen, captured)
    repo._accept_context.budget = PageBudget(max_files=32)
    repo._accept_context.cancel = None
    try:
        with pytest.raises(PreflightCancelled) as caught:
            repo._preflight_page_health(job["batch_id"], page)
    finally:
        repo._accept_context.budget = None
    assert caught.value.reason == "page_file_budget"
    assert len(captured) == pages - 1
    seen.clear()
    return job, page, captured, seen


def resolved_path(root, ref):
    from core.app_paths import AppPaths
    from core.library_paths import PathResolver

    return PathResolver(AppPaths(root)).resolve(ref)


def all_artifacts(repo):
    with sqlite3.connect(repo.database_path) as conn:
        return [
            str(row[0])
            for row in conn.execute("SELECT artifact_id FROM artifacts ORDER BY rowid")
        ]


def resume_job(repo, job, command_id):
    settle(repo, job, complete=False)
    resumed = repo.resume(job["job_id"], command_id)
    active = repo.activate_next()
    assert active is not None and active["batch_id"] == resumed["batch_id"]
    return resumed


def move_synthetic_root(root, destination, scope):
    """Simulate an unplugged volume inside tmp_path; never touches real data."""
    assert root.resolve().is_relative_to(scope.resolve())
    assert destination.resolve().is_relative_to(scope.resolve())
    assert not root.is_symlink() and not destination.exists()
    root.rename(destination)


def cancelled_with_temp(repo, root, monkeypatch, *, ident=TARGET, request_id="cancel"):
    """A cancelled delivery whose own '.part' survives a synthetic file lock."""
    job = h.submit(repo, request_id=request_id, max_works=2)
    page = h.source(
        (h.candidate(ident),), page_identity="cancel-" + request_id
    )
    (delivery,) = h.accept(repo, job["batch_id"], page)

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

    unlink = Path.unlink

    def locked(path, *args, **kwargs):
        if path.suffix == ".part":
            raise PermissionError("synthetic sharing violation")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as scope:
        scope.setattr(Path, "unlink", locked)
        service = h.executor(repo, root, h.Transport(h.Response()), fault_hook=exclude)
        assert h.run(repo, job, delivery, service).state == "cancelled"
    settle(repo, job)
    return job, delivery, service


def cleanup_facts(repo):
    with sqlite3.connect(repo.database_path) as conn:
        rows = conn.execute(
            "SELECT safe_payload FROM events WHERE kind = 'delivery_cleanup'"
        ).fetchall()
    facts = []
    for (payload,) in rows:
        try:
            facts.append(json.loads(payload))
        except (TypeError, ValueError):
            facts.append({"corrupt": True})
    return facts


def write_progress_event(repo, job, page, records, *, age_seconds=0, slice_seq=1):
    """Persist one observation slice as the durable fact described in Sec.1.2."""
    page_digest = hashlib.sha256(
        canonical_json(asdict(page)).encode("utf-8")
    ).hexdigest()
    if age_seconds:
        checked = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
        records = [dict(record, health=dict(record["health"], checked_at=checked)) for record in records]
    payload = {
        "schema_version": 1,
        "page_key": {
            "job_id": job["job_id"],
            "source_id": page.source_id,
            "pass_id": page.pass_id,
            "entity_id": page.entity_id,
            "page_identity": page.page_identity,
            "page_digest": page_digest,
        },
        "records": records,
        "observed_files": len(records),
        "observed_bytes": 0,
        "slice_seq": slice_seq,
        "root_revisions": {"project": 1},
        "policy_version": 1,
    }
    with sqlite3.connect(repo.database_path) as conn:
        conn.execute(
            "INSERT INTO events(event_uuid, job_id, batch_id, kind, safe_payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                job["job_id"],
                job["batch_id"],
                "page_observation_progress",
                canonical_json(payload),
                (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(),
            ),
        )
        conn.commit()
    return payload


def progress_records(records):
    """Trim a captured ArtifactHealth tuple down to the frozen record shape."""
    return [
        {
            "artifact_id": record.expectation.artifact_id,
            "output_key": record.expectation.output_key,
            "quality_key": record.expectation.quality_key,
            "root_revision": record.expectation.root_revision,
            "health": record.to_payload(),
        }
        for record in records
    ]


def record_handover(repo, hold_row, artifact_id, *, job_id):
    """Write the cross-task handover fact exactly as _record_owner_transfer does."""
    with sqlite3.connect(repo.database_path) as conn:
        conn.execute(
            "INSERT INTO events(event_uuid, job_id, batch_id, kind, safe_payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                job_id,
                None,
                "recovery_owner_transfer",
                canonical_json(
                    {
                        "delivery_id": str(hold_row["delivery_id"]),
                        "from_job_id": str(hold_row["job_id"]),
                        "to_job_id": job_id,
                        "from_state": str(hold_row["state"]),
                        "artifact_id": artifact_id,
                        "action": "reuse",
                    }
                ),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def clone_cleanup_row(conn, base_id, new_id, *, generation):
    """Clone a cancelled delivery and rebind its prepared evidence to the clone.

    The prepared payload names its own delivery and staged temp (Sec.2.3), so a
    naive row clone would be rejected as corrupt evidence by the reader.
    """
    clone_delivery(conn, base_id, new_id, generation=generation)
    prepared = json.loads(
        conn.execute(
            "SELECT prepared_json FROM deliveries WHERE delivery_id = ?", (new_id,)
        ).fetchone()[0]
    )
    prepared["delivery_id"] = new_id
    prepared["temp_ref"]["relative_path"] = prepared["temp_ref"][
        "relative_path"
    ].replace(base_id, new_id)
    conn.execute(
        "UPDATE deliveries SET prepared_json = ? WHERE delivery_id = ?",
        (canonical_json(prepared), new_id),
    )
    return new_id


def hold_row(repo, delivery_id):
    with sqlite3.connect(repo.database_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()


def parked_hold_with_artifact(repo, root, *, request_id="second"):
    """One parked hold plus the completed artifact another task produced."""
    _, delivered = h.first_delivery(repo, root)
    h.target(root).write_bytes(h.SAME_SIZE_PNG)
    job, accepted = h.second_task(repo, root, request_id=request_id)
    (hold,) = accepted
    assert repo.get_delivery(hold)["state"] == "needs_verification"
    settle(repo, job)
    h.target(root).write_bytes(h.PNG_BYTES)
    artifact = repo.get_artifact(delivered)
    assert artifact is not None
    return job, hold, delivered, artifact["artifact_id"]


def new_redownload_pending(repo, request_id="unrelated"):
    """A newer, unrelated task that owns the same page and is still pending."""
    job = h.submit(repo, request_id=request_id, dedup_policy="redownload")
    (pending,) = h.accept(
        repo,
        job["batch_id"],
        h.source((h.candidate(),), page_identity="page-" + request_id),
    )
    assert repo.get_delivery(pending)["state"] == "pending"
    return job, pending


# ---------------------------------------------------------------------------
# R-A: resumable observation
# ---------------------------------------------------------------------------


def test_a_resumed_attempt_must_reuse_the_persisted_slice_not_rehash_it(tmp_path, monkeypatch):
    """Sec.1.2/1.6.3: every attempt has to move forward, never re-read the same files."""
    root = tmp_path / "library"
    root.mkdir()
    from core.observation_snapshot import PageBudget, PreflightCancelled

    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        seed(repo, root, pages=tuple(range(64)), request_id="seed-64")
        job = h.submit(repo, request_id="reuse-64")
        page = h.source((g.candidate(pages=tuple(range(64))),))
        budget = PageBudget(max_files=32, max_slices=1)

        seen = observe(repo, monkeypatch)
        with pytest.raises(PreflightCancelled) as caught:
            repo.accept_source_page(job["batch_id"], page, page_budget=budget)
        assert caught.value.reason == "page_slice_limit", (
            "Sec.1.1: one slice ends the call as a budget result"
        )
        first = list(seen)
        assert len(first) == 32, "Sec.1.1: a slice opens at most max_files files"
        assert checkpoint(repo, job) is None, (
            "Sec.1.6.1: a partial page must not advance the cursor"
        )

        resumed = resume_job(repo, job, "resume-64-a")
        seen.clear()
        with pytest.raises(PreflightCancelled) as caught:
            repo.accept_source_page(resumed["batch_id"], page, page_budget=budget)
        assert caught.value.reason == "page_slice_limit", (
            "Sec.1.1: the second slice is still a budget result"
        )
        second = list(seen)
        overlap = sorted(set(first) & set(second))
        assert not overlap, (
            "Sec.1.2/1.6.3: the resumed attempt re-read "
            f"{len(overlap)} already-observed files instead of reusing progress"
        )
        assert len(second) == 32, (
            "Sec.1.4: the resumed attempt must observe the next slice, not nothing"
        )
        assert checkpoint(repo, resumed) is None

        resumed = resume_job(repo, resumed, "resume-64-b")
        seen.clear()
        try:
            repo.accept_source_page(resumed["batch_id"], page, page_budget=budget)
        except PreflightCancelled as exc:
            pytest.fail(
                "Sec.1.4: once every file is observed the page must commit, but it "
                f"raised {exc.reason!r}"
            )
        assert seen == [], (
            "Sec.1.3: the last attempt only has to commit already-observed files"
        )
        current = checkpoint(repo, resumed)
        assert current is not None and current["cursor_json"] == '"cursor-1"', (
            "Sec.1.6.1: the cursor advances only with the complete page"
        )
        assert len(page_links(repo, resumed["batch_id"])) == 64


def test_the_slice_limit_is_a_budget_result_and_an_accepted_page_replays_without_hashing(
    tmp_path, monkeypatch
):
    """Sec.1.1/1.4: max_slices ends the call, and the commit replays for free."""
    root = tmp_path / "library"
    root.mkdir()
    from core.observation_snapshot import PageBudget, PreflightCancelled

    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        seed(repo, root, pages=tuple(range(33)), request_id="seed-33-limit")
        job = h.submit(repo, request_id="reuse-33-limit")
        page = h.source((g.candidate(pages=tuple(range(33))),))
        budget = PageBudget(max_files=32, max_slices=1)
        seen = observe(repo, monkeypatch)

        with pytest.raises(PreflightCancelled) as caught:
            repo.accept_source_page(job["batch_id"], page, page_budget=budget)
        assert caught.value.reason == "page_slice_limit", (
            "Sec.1.1: exhausting max_slices is a budget result, not a file verdict"
        )
        assert len(seen) == 32, "Sec.1.1: one slice observes max_files files"
        assert checkpoint(repo, job) is None

        resumed = resume_job(repo, job, "resume-33-limit")
        seen.clear()
        try:
            repo.accept_source_page(resumed["batch_id"], page, page_budget=budget)
        except PreflightCancelled as exc:
            pytest.fail(
                "Sec.1.4: the second call must commit the one remaining file, "
                f"but it raised {exc.reason!r}"
            )
        assert list(seen) and len(seen) == 1, (
            "Sec.1.3: only the unobserved file may be opened again"
        )
        links = page_links(repo, resumed["batch_id"])
        assert len(links) == 33, "the whole page commits atomically"

        seen.clear()
        repo.accept_source_page(resumed["batch_id"], page, page_budget=budget)
        assert seen == [], (
            "Sec.1.4: an accepted page replays from its receipt with zero hashes"
        )
        assert page_links(repo, resumed["batch_id"]) == links


def test_a_page_with_twelve_multi_page_works_commits_within_the_slice_budget(tmp_path, monkeypatch):
    """Sec.1.4: a page of 12 works (36 files) is two slices, not a failure."""
    root = tmp_path / "library"
    root.mkdir()
    from core.observation_snapshot import PageBudget, PreflightCancelled

    candidates = tuple(work(f"90{index}", (0, 1, 2)) for index in range(12))
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        seed(
            repo,
            root,
            candidates=candidates,
            request_id="seed-works",
            max_works=12,
        )
        job = h.submit(repo, request_id="reuse-works", max_works=12)
        page = h.source(candidates, page_identity="works-page")
        seen = observe(repo, monkeypatch)
        try:
            repo.accept_source_page(job["batch_id"], page, page_budget=PageBudget())
        except PreflightCancelled as exc:
            pytest.fail(
                "Sec.1.4: 36 files fit two slices within max_slices=8, but the "
                f"acceptance raised {exc.reason!r}"
            )
        assert len(seen) == 36, "Sec.1.1/1.4: every file is observed exactly once"
        assert len(page_links(repo, job["batch_id"])) == 36, (
            "all twelve works must be linked in the committed page"
        )
        assert checkpoint(repo, job)["cursor_json"] == '"cursor-1"'


def test_progress_events_are_reused_only_inside_the_health_window(tmp_path, monkeypatch):
    """Sec.1.3: a durable slice replaces re-hashing only while it is fresh."""
    root = tmp_path / "library"
    root.mkdir()
    from core.observation_snapshot import PageBudget, PreflightCancelled

    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        seed(repo, root, pages=tuple(range(33)), request_id="seed-33-fresh")
        page = h.source((g.candidate(pages=tuple(range(33))),))

        job = h.submit(repo, request_id="reuse-33-fresh")
        captured = []
        original = repo._observe_artifact

        def capture(artifact, **kwargs):
            record = original(artifact, **kwargs)
            captured.append(record)
            return record

        monkeypatch.setattr(repo, "_observe_artifact", capture)
        repo._accept_context.budget = PageBudget(max_files=32)
        repo._accept_context.cancel = None
        try:
            with pytest.raises(PreflightCancelled) as caught:
                repo._preflight_page_health(job["batch_id"], page)
        finally:
            repo._accept_context.budget = None
        assert caught.value.reason == "page_file_budget"
        assert len(captured) == 32
        records = progress_records(captured)

        write_progress_event(repo, job, page, records)
        seen = observe(repo, monkeypatch)
        try:
            repo.accept_source_page(job["batch_id"], page, page_budget=PageBudget())
        except PreflightCancelled as exc:
            pytest.fail(
                "Sec.1.2: the persisted slice must be reusable by the next "
                f"acceptance, but it raised {exc.reason!r}"
            )
        assert len(seen) == 1, (
            "Sec.1.3: the 32 recorded files must be reused; "
            f"{len(seen)} files were hashed again"
        )

        # A slice older than max_age_seconds is history, never current evidence.
        # The previous batch must settle first: one active batch at a time.
        settle(repo, job)
        job = h.submit(repo, request_id="reuse-33-aged")
        write_progress_event(repo, job, page, records, age_seconds=1200)
        seen.clear()
        try:
            repo.accept_source_page(job["batch_id"], page, page_budget=PageBudget())
        except PreflightCancelled as exc:
            pytest.fail(
                "Sec.1.4: an expired slice must fall back to fresh observation "
                f"and still commit, but it raised {exc.reason!r}"
            )
        assert len(seen) == 33, (
            "Sec.1.3.4: records older than max_age_seconds must be discarded; "
            f"only {len(seen)} files were observed"
        )


def test_a_pause_between_slices_does_not_reobserve_the_finished_slice(tmp_path, monkeypatch):
    """Sec.1.1/1.2: a pause persists the finished slice for the next run."""
    import threading

    root = tmp_path / "library"
    root.mkdir()
    from core.observation_snapshot import PageBudget, PreflightCancelled

    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        seed(repo, root, pages=tuple(range(33)), request_id="seed-33-pause")
        job = h.submit(repo, request_id="reuse-33-pause")
        page = h.source((g.candidate(pages=tuple(range(33))),))

        stop = threading.Event()
        seen = observe(repo, monkeypatch, cancel=stop, cancel_after=5)
        with pytest.raises(PreflightCancelled) as caught:
            repo.accept_source_page(
                job["batch_id"], page, cancel=stop, page_budget=PageBudget()
            )
        assert caught.value.reason == "cancelled", (
            "Sec.1.1: a pause is a control result, not a budget or file verdict"
        )
        first = list(seen)
        assert len(first) == 5
        assert checkpoint(repo, job) is None

        resumed = resume_job(repo, job, "resume-33-pause")
        seen.clear()
        try:
            repo.accept_source_page(resumed["batch_id"], page, page_budget=PageBudget())
        except PreflightCancelled as exc:
            pytest.fail(
                "Sec.1.2: the resumed run must continue from the paused slice, "
                f"but it raised {exc.reason!r}"
            )
        overlap = sorted(set(first) & set(seen))
        assert not overlap, (
            "Sec.1.2: the resumed run re-observed the paused slice: " + repr(overlap)
        )
        assert len(seen) == 28, (
            "Sec.1.1: resume must observe only the 28 unfinished files, "
            f"not {len(seen)}"
        )


def test_a_newer_artifact_row_invalidates_its_stale_progress_record(tmp_path, monkeypatch):
    """Sec.1.3.1: a progress record belongs to one artifact row identity."""
    import threading

    root = tmp_path / "library"
    root.mkdir()
    from core.observation_snapshot import PageBudget, PreflightCancelled

    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        seed(repo, root, pages=tuple(range(33)), request_id="seed-33-identity")
        job = h.submit(repo, request_id="reuse-33-identity")
        page = h.source((g.candidate(pages=tuple(range(33))),))

        stop = threading.Event()
        seen = observe(repo, monkeypatch, cancel=stop, cancel_after=32)
        with pytest.raises(PreflightCancelled) as caught:
            repo.accept_source_page(
                job["batch_id"], page, cancel=stop, page_budget=PageBudget()
            )
        assert caught.value.reason == "cancelled", (
            "Sec.1.1: the pause is a control result, not a file verdict"
        )
        observed = list(seen)
        assert len(observed) == 32
        moved = observed[0]
        with sqlite3.connect(repo.database_path) as conn:
            conn.execute(
                "UPDATE artifacts SET sha256 = ? WHERE artifact_id = ?",
                ("f" * 64, moved),
            )
            conn.commit()

        resumed = resume_job(repo, job, "resume-33-identity")
        seen.clear()
        try:
            repo.accept_source_page(resumed["batch_id"], page, page_budget=PageBudget())
        except PreflightCancelled as exc:
            pytest.fail(
                "Sec.1.3.1: the one unobserved file must be observed so the page "
                f"can commit, but the acceptance raised {exc.reason!r}"
            )
        expected = {moved} | (set(all_artifacts(repo)) - set(observed))
        assert set(seen) == expected, (
            "Sec.1.3.1: only the artifact whose row identity moved and the file "
            "that was never observed may be opened again; got " + repr(sorted(seen))
        )


def test_a_disk_identity_change_invalidates_the_progress_record(tmp_path, monkeypatch):
    """Sec.1.3.6: the recorded object identity must still be the one on disk."""
    root = tmp_path / "library"
    root.mkdir()
    from core.observation_snapshot import PageBudget, PreflightCancelled

    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, page, captured, seen = prepared_reuse_page(
            repo, root, monkeypatch, request_id="reuse-33-identity-drift"
        )
        records = progress_records(captured)
        moved = records[0]["artifact_id"]
        info = os.stat(resolved_path(root, captured[0].expectation.path))
        assert (info.st_dev, info.st_ino) == (
            captured[0].identity.device,
            captured[0].identity.file_index,
        ), "precondition: the captured record carries the real disk identity"
        assert info.st_size == captured[0].observed_size
        records[0]["health"]["file_index"] = int(captured[0].identity.file_index) + 1
        write_progress_event(repo, job, page, records)
        unobserved = set(all_artifacts(repo)) - {
            record["artifact_id"] for record in records
        }
        assert len(unobserved) == 1

        try:
            repo.accept_source_page(job["batch_id"], page, page_budget=PageBudget())
        except PreflightCancelled as exc:
            pytest.fail(
                "Sec.1.3.6: a record whose disk identity moved must be re-observed "
                f"and the page must still commit, but it raised {exc.reason!r}"
            )
        assert set(seen) == {moved} | unobserved, (
            "Sec.1.3.6: only the drifted record and the never-observed file may be "
            f"opened again; got {sorted(seen)}"
        )
        assert len(page_links(repo, job["batch_id"])) == 33


def test_a_progress_record_without_an_identity_is_not_reused(tmp_path, monkeypatch):
    """Sec.1.3.6: a record without an object identity is not current evidence."""
    root = tmp_path / "library"
    root.mkdir()
    from core.observation_snapshot import PageBudget, PreflightCancelled

    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, page, captured, seen = prepared_reuse_page(
            repo, root, monkeypatch, request_id="reuse-33-identity-absent"
        )
        records = progress_records(captured)
        anonymous = records[0]["artifact_id"]
        records[0]["health"]["device"] = None
        records[0]["health"]["file_index"] = None
        write_progress_event(repo, job, page, records)
        unobserved = set(all_artifacts(repo)) - {
            record["artifact_id"] for record in records
        }
        assert len(unobserved) == 1

        try:
            repo.accept_source_page(job["batch_id"], page, page_budget=PageBudget())
        except PreflightCancelled as exc:
            pytest.fail(
                "Sec.1.3.6: an anonymous record must be re-observed and the page "
                f"must still commit, but it raised {exc.reason!r}"
            )
        assert set(seen) == {anonymous} | unobserved, (
            "Sec.1.3.6: only the anonymous record and the never-observed file may "
            f"be opened again; got {sorted(seen)}"
        )
        assert len(page_links(repo, job["batch_id"])) == 33


def test_a_progress_record_is_still_reused_when_the_disk_identity_matches(tmp_path, monkeypatch):
    """Sec.1.3.6/1.1: a matching stat recheck keeps the record and costs no slot."""
    root = tmp_path / "library"
    root.mkdir()
    from core.observation_snapshot import PageBudget, PreflightCancelled

    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, page, captured, seen = prepared_reuse_page(
            repo, root, monkeypatch, request_id="reuse-33-identity-match"
        )
        records = progress_records(captured)
        info = os.stat(resolved_path(root, captured[0].expectation.path))
        assert (info.st_dev, info.st_ino) == (
            captured[0].identity.device,
            captured[0].identity.file_index,
        ), "precondition: the captured record carries the real disk identity"
        assert info.st_size == captured[0].observed_size
        write_progress_event(repo, job, page, records)
        unobserved = set(all_artifacts(repo)) - {
            record["artifact_id"] for record in records
        }
        assert len(unobserved) == 1

        try:
            repo.accept_source_page(
                job["batch_id"], page, page_budget=PageBudget(max_files=1)
            )
        except PreflightCancelled as exc:
            pytest.fail(
                "Sec.1.1/1.3.6: 32 matching stat rechecks must not consume "
                f"max_files=1, but the acceptance raised {exc.reason!r}"
            )
        assert set(seen) == unobserved and len(seen) == 1, (
            "Sec.1.3.6: every matching record must be reused without opening the "
            f"file again; observed {sorted(seen)}"
        )
        assert len(page_links(repo, job["batch_id"])) == 33


# ---------------------------------------------------------------------------
# R-B: presence evidence, cleanup facts and recovery compensation
# ---------------------------------------------------------------------------


def test_an_unreachable_root_cannot_cancel_a_recovery_needed_delivery(tmp_path):
    """Sec.2.2: a root that cannot be probed proves nothing about the file."""
    from core.page_delivery import _Fault

    root = tmp_path / "library"
    root.mkdir()
    offline = tmp_path / "offline"
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job = h.submit(repo)
        (delivery,) = h.accept(repo, job["batch_id"], h.source((h.candidate(),)))

        def crash(stage, row):
            if stage == "before_publish":
                raise _Fault("audit_stop_before_publish")

        service = h.executor(repo, root, h.Transport(h.Response()), fault_hook=crash)
        assert h.run(repo, job, delivery, service).state == "recovery_needed"
        repo.record_removal_intent(
            job["batch_id"],
            TARGET,
            0,
            h.OUTPUT_REF,
            intent="excluded",
            command_id="exclude-offline-recovery-needed",
        )
        assert len(list(root.rglob("*.part"))) == 1

        move_synthetic_root(root, offline, tmp_path)
        try:
            h.executor(repo, root, h.Transport()).recover_local()
        finally:
            move_synthetic_root(offline, root, tmp_path)

        state = repo.get_delivery(delivery)["state"]
        assert state == "recovery_needed", (
            "Sec.2.2: an unreachable root is not 'the final name does not exist'; "
            f"the delivery became {state!r}"
        )
        assert len(list(root.rglob("*.part"))) == 1, (
            "Sec.2.5: an unverifiable staged file must be kept"
        )

        h.executor(repo, root, h.Transport()).recover_local()
        assert repo.get_delivery(delivery)["state"] == "cancelled", (
            "Sec.2.2: after reconnection the exclusion may cancel the row"
        )
        assert list(root.rglob("*.part")) == [], (
            "Sec.2.5: the owned temp is removed only once it can be verified"
        )


def test_cleanup_reports_nothing_while_the_root_is_offline_and_retries_after_reconnect(
    tmp_path, monkeypatch
):
    """Sec.2.3/2.4: an offline cleanup is not 'already absent' and writes no fact."""
    root = tmp_path / "library"
    root.mkdir()
    offline = tmp_path / "offline"
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, delivery, service = cancelled_with_temp(repo, root, monkeypatch)
        assert len(list(root.rglob("*.part"))) == 1

        move_synthetic_root(root, offline, tmp_path)
        try:
            result = service.cleanup_delivery(delivery)
        finally:
            move_synthetic_root(offline, root, tmp_path)

        assert result is False, (
            "Sec.2.4: an unreachable root cannot be reported as already absent"
        )
        facts = [
            fact
            for fact in cleanup_facts(repo)
            if fact.get("delivery_id") == delivery
        ]
        terminal = [
            fact
            for fact in facts
            if fact.get("cleanup_completed") is True or fact.get("outcome") == "cleaned"
        ]
        assert not terminal, (
            "Sec.2.3: no terminal cleanup fact may be written while the file "
            f"cannot be observed: {terminal}"
        )
        assert delivery in [
            fact["delivery_id"] for fact in repo.list_cleanup_candidates()
        ], "Sec.2.4: the retry queue must still own the row"

        assert service.cleanup_delivery(delivery) is True, (
            "Sec.2.5: after reconnection the owned temp can be removed"
        )
        assert list(root.rglob("*.part")) == []
        assert repo.get_delivery(delivery)["state"] == "cancelled"


def test_the_cleanup_window_skips_legacy_cleaned_and_unparsable_rows(tmp_path, monkeypatch):
    """Sec.2.3/2.4: the reader understands the facts the producer writes."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, base, _ = cancelled_with_temp(repo, root, monkeypatch)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            base_row = conn.execute(
                "SELECT rowid, * FROM deliveries WHERE delivery_id = ?", (base,)
            ).fetchone()
            legacy = clone_cleanup_row(conn, base, "a" * 32, generation=50)
            unparsable = clone_cleanup_row(conn, base, "b" * 32, generation=51)
            failed = clone_cleanup_row(conn, base, "c" * 32, generation=52)
            clean = [clone_cleanup_row(conn, base, f"{index + 1:032x}", generation=100 + index)
                     for index in range(197)]
            conn.execute(
                "UPDATE deliveries SET prepared_json = 'not json' WHERE delivery_id = ?",
                (unparsable,),
            )
            conn.commit()

        def fact(delivery_id, payload):
            with sqlite3.connect(repo.database_path) as conn:
                conn.execute(
                    "INSERT INTO events(event_uuid, job_id, batch_id, kind, safe_payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        job_id,
                        None,
                        "delivery_cleanup",
                        canonical_json(payload),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                conn.commit()

        job_id = base_row["job_id"]
        fact(legacy, {"delivery_id": legacy, "batch_id": None, "outcome": "cleaned"})
        fact(failed, {"delivery_id": failed, "batch_id": None, "outcome": "failed",
                      "error_code": "cleanup_failed"})

        one = [item["delivery_id"] for item in repo.list_cleanup_candidates(limit=1)]
        assert one == [base], (
            "Sec.2.4: a bad row must not consume the whole limit=1 window; got "
            + repr(one)
        )
        two = [item["delivery_id"] for item in repo.list_cleanup_candidates(limit=2)]
        assert two == [base, clean[0]], (
            "Sec.2.3: a legacy 'outcome=cleaned' record is terminal and must not "
            f"be offered again; got {two}"
        )
        window = [item["delivery_id"] for item in repo.list_cleanup_candidates(limit=200)]
        assert len(window) == 199, (
            "Sec.2.4: 201 rows minus one legacy terminal minus one unparsable is "
            f"199 candidates, got {len(window)}"
        )
        assert legacy not in window and unparsable not in window and failed in window


def test_cleanup_paging_can_advance_past_the_first_window(tmp_path, monkeypatch):
    """Sec.2.4: a bounded window has an explicit continuation, not a rescan."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, base, _ = cancelled_with_temp(repo, root, monkeypatch)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            base_rowid = conn.execute(
                "SELECT rowid FROM deliveries WHERE delivery_id = ?", (base,)
            ).fetchone()[0]
            first = clone_cleanup_row(conn, base, "d" * 32, generation=60)
            second = clone_cleanup_row(conn, base, "e" * 32, generation=61)
            conn.execute(
                "UPDATE deliveries SET prepared_json = 'not json' WHERE delivery_id = ?",
                (base,),
            )
            conn.commit()

        starved = [
            item["delivery_id"] for item in repo.list_cleanup_candidates(limit=1)
        ]
        assert starved == [first], (
            "Sec.2.4: an unparsable row must not consume the whole limit=1 window; "
            f"got {starved}"
        )
        page = [
            item["delivery_id"]
            for item in repo.list_cleanup_candidates(limit=1, after_rowid=base_rowid)
        ]
        assert page == [first], (
            "Sec.2.4: after_rowid must continue strictly after the given row; got "
            + repr(page)
        )
        tail = [
            item["delivery_id"]
            for item in repo.list_cleanup_candidates(limit=2, after_rowid=base_rowid)
        ]
        assert tail == [first, second], (
            "Sec.2.4: the continuation must skip the unparsable row without "
            f"dropping the parseable ones; got {tail}"
        )


def test_a_losing_recovery_reports_the_winner_instead_of_raising(tmp_path):
    """Sec.2.6: a compensation write racing a winner is a result, not a fault."""
    from core.page_delivery import _Fault

    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job = h.submit(repo)
        (delivery,) = h.accept(repo, job["batch_id"], h.source((h.candidate(),)))

        def fail_before_confirm(stage, row):
            if stage == "before_confirm":
                raise _Fault("audit_stage_fault")

        seed_service = h.executor(
            repo, tmp_path, h.Transport(h.Response()), fault_hook=fail_before_confirm
        )
        assert h.run(repo, job, delivery, seed_service).state == "recovery_needed"

        winner = h.executor(repo, tmp_path, h.Transport())

        def interleave(stage, row):
            if stage == "before_publish":
                assert winner.recover_local() == ((delivery, "completed"),)
                raise _Fault("audit_loser_fault")

        loser = h.executor(repo, tmp_path, h.Transport(), fault_hook=interleave)
        result = loser.recover_local()
        assert result == ((delivery, "completed"),), (
            "Sec.2.6: losing a compensation race must report the winner's state, "
            f"got {result!r}"
        )
        assert repo.get_delivery(delivery)["state"] == "completed"
        assert [
            fact
            for fact in cleanup_facts(repo)
            if fact.get("delivery_id") == delivery
        ] == []


# ---------------------------------------------------------------------------
# R-C: successor resolution
# ---------------------------------------------------------------------------


def test_handover_target_with_a_foreign_quality_is_refused_without_fallback(tmp_path):
    """Sec.3.1: an unusable handover target is a refusal, never a rowid guess."""
    from core.delivery_lineage import resolve_successor

    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, hold, delivered, artifact_id = parked_hold_with_artifact(repo, tmp_path)
        row = hold_row(repo, hold)
        record_handover(repo, row, artifact_id, job_id=row["job_id"])
        new_redownload_pending(repo)
        with sqlite3.connect(repo.database_path) as conn:
            conn.execute(
                "UPDATE deliveries SET quality_key = ? WHERE delivery_id = ?",
                ("0" * 64, delivered),
            )
            conn.commit()
            conn.row_factory = sqlite3.Row
            current = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (hold,)
            ).fetchone()
            successor = resolve_successor(conn, current)
        assert successor is None, (
            "Sec.3.1: the recorded handover target no longer matches this page's "
            "quality, so resolution must refuse; it returned "
            f"{None if successor is None else successor.delivery_id!r}"
        )


def test_a_handover_event_without_its_artifact_is_not_silently_replaced_by_rowid(tmp_path):
    """Sec.3.1: a damaged handover target is a refusal, not 'no event'."""
    from core.delivery_lineage import resolve_successor

    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, hold, _, artifact_id = parked_hold_with_artifact(repo, tmp_path)
        row = hold_row(repo, hold)
        record_handover(repo, row, artifact_id, job_id=row["job_id"])
        _, pending = new_redownload_pending(repo)
        with sqlite3.connect(repo.database_path) as conn:
            conn.execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))
            conn.commit()
            conn.row_factory = sqlite3.Row
            current = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (hold,)
            ).fetchone()
            successor = resolve_successor(conn, current)
        assert successor is None, (
            "Sec.3.1: a handover whose artifact is gone must not fall back to the "
            f"newer pending {pending!r}; it returned "
            f"{None if successor is None else successor.delivery_id!r}"
        )


def test_lineage_result_reports_reason_and_a_bounded_query_count(tmp_path):
    """Sec.3.2/3.3: the resolver returns typed evidence, not only a row."""
    from core.delivery_lineage import LineageResult, resolve_successor_ex

    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, hold, delivered, artifact_id = parked_hold_with_artifact(repo, tmp_path)
        row = hold_row(repo, hold)
        record_handover(repo, row, artifact_id, job_id=row["job_id"])
        new_redownload_pending(repo)
        queries = []
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            current = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (hold,)
            ).fetchone()
            conn.set_trace_callback(lambda sql: queries.append(sql))
            result = resolve_successor_ex(conn, current)
            conn.set_trace_callback(None)
        assert isinstance(result, LineageResult), (
            "Sec.3.2: resolve_successor_ex must return a LineageResult"
        )
        assert result.successor is not None and result.successor.delivery_id == delivered
        assert result.reason == "event", (
            f"Sec.3.1: the recorded handover is authoritative; reason={result.reason!r}"
        )
        assert result.queries <= 4, (
            "Sec.3.3: one resolution may run at most MAX_LINEAGE_QUERIES=4 "
            f"statements, it ran {result.queries}"
        )
        assert result.nodes >= 1


def test_a_sixty_four_link_supersede_chain_stays_inside_the_query_budget(tmp_path):
    """Sec.3.3: depth is bounded by a real budget, not by a recursive blow-up."""
    from core.delivery_lineage import resolve_successor
    from test_u1a_rf_recovery import link_page_to

    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, pending, _ = h.queued_repair(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            previous = None
            first = None
            for index in range(64):
                new_id = clone_delivery(
                    conn,
                    pending,
                    f"{index + 1:032x}",
                    state="cancelled",
                    error_code="superseded_by_recovery_reuse",
                    generation=200 + index,
                )
                if first is None:
                    first = new_id
                previous = new_id
            assert previous is not None
            link_page_to(conn, job["batch_id"], first)
            conn.commit()
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (pending,)
            ).fetchone()
            scans = []
            conn.set_trace_callback(
                lambda sql: scans.append(sql) if "rowid > (" in sql else None
            )
            result = resolve_successor(conn, row)
            conn.set_trace_callback(None)
        assert result is None, "Sec.3.3: a 64 link chain resolves to nothing"
        assert len(scans) <= 4, (
            "Sec.3.3: resolving 64 superseded links must stay inside the query "
            f"budget of 4, it ran {len(scans)} scans"
        )


def test_a_wide_supersede_fan_expands_each_node_at_most_once(tmp_path):
    """Sec.3.1/3.3: visited de-duplication, not one subtree per sibling."""
    from core.delivery_lineage import resolve_successor

    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, hold, _, _ = parked_hold_with_artifact(repo, tmp_path)
        with sqlite3.connect(repo.database_path) as conn:
            for index in range(24):
                clone_delivery(
                    conn,
                    hold,
                    f"{index + 1:032x}",
                    state="cancelled",
                    error_code="superseded_by_recovery_reuse",
                    generation=300 + index,
                )
            conn.commit()
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (hold,)
            ).fetchone()
            scans = []
            conn.set_trace_callback(
                lambda sql: scans.append(sql) if "rowid > (" in sql else None
            )
            result = resolve_successor(conn, row)
            conn.set_trace_callback(None)
        assert result is None, "Sec.3.1: 24 superseded rows resolve to nothing"
        assert len(scans) <= 4, (
            "Sec.3.3: a 24 wide fan must be resolved with a bounded number of "
            f"reads, it ran {len(scans)} scans"
        )
        assert len(scans) == len(set(scans)), (
            "Sec.3.3: the same descendant window must not be expanded twice "
            f"({len(scans)} scans, {len(set(scans))} distinct)"
        )


def handover_competition(repo, root):
    """A superseded hold with a recorded handover plus a newer unrelated pending.

    This is the state probe RFA-04 builds: the handover artifact predates the
    retired hold, so only the recorded event can point at it (Sec.3.1).
    """
    first = g.submit(repo, request_id="handover-first")
    ids = g.accept(repo, first["batch_id"], g.source((g.candidate(pages=(0, 1)),)))
    transport = h.Transport(h.Response(), h.Response(status=404))
    assert h.run(repo, first, ids[0], h.executor(repo, root, transport)).state == "completed"
    assert h.run(repo, first, ids[1], h.executor(repo, root, transport)).state == "failed"
    settle(repo, first)
    completed_artifact = repo.get_artifact(ids[0])
    assert completed_artifact is not None
    h.target(root).write_bytes(h.SAME_SIZE_PNG)
    second, (hold,) = h.second_task(repo, root)
    settle(repo, second)
    resumed = repo.resume(first["job_id"], "handover-resume")
    assert repo.activate_next()["batch_id"] == resumed["batch_id"]
    assert repo.carry_unresolved_items(resumed["batch_id"])["count"] == 1
    h.target(root).write_bytes(h.PNG_BYTES)
    with sqlite3.connect(repo.database_path) as conn:
        conn.execute(
            "UPDATE deliveries SET state = 'cancelled', "
            "error_code = 'superseded_by_recovery_reuse' WHERE delivery_id = ?",
            (hold,),
        )
        conn.commit()
    row = hold_row(repo, hold)
    record_handover(repo, row, completed_artifact["artifact_id"], job_id=resumed["job_id"])
    with sqlite3.connect(repo.database_path) as conn:
        competitor = clone_delivery(
            conn,
            hold,
            "9" * 32,
            state="pending",
            error_code=None,
            job_id=second["job_id"],
            current_batch_id=second["batch_id"],
            origin_batch_id=second["batch_id"],
            generation=7,
        )
        conn.commit()
    return resumed, ids, completed_artifact["artifact_id"], competitor, hold


def test_the_read_path_follows_the_recorded_handover(tmp_path):
    """Sec.3.1: the recorded handover outranks a newer unrelated pending row."""
    from core.delivery_lineage import resolve_successor

    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        resumed, ids, _artifact_id, competitor, hold = handover_competition(repo, root)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            current = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (hold,)
            ).fetchone()
            read = resolve_successor(conn, current)
        assert read is not None and read.delivery_id == ids[0], (
            "Sec.3.1: the read path must follow the recorded handover instead of "
            f"the newer pending {competitor!r}; it returned {read!r}"
        )
        assert repo.get_batch(resumed["batch_id"])["phase"] == "running", (
            "precondition: the recovery entry is scoped to a running batch"
        )


def test_the_write_path_follows_the_recorded_handover(tmp_path):
    """Sec.3.1: recovery resolves exactly what the read path resolves."""
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        resumed, _ids, artifact_id, competitor, _hold = handover_competition(repo, root)
        result = repo.recover_verification_page(resumed["batch_id"], TARGET, 0)
        assert result["outcome"] == "reused", (
            "Sec.3.1: recovery must reach the recorded handover instead of the "
            f"newer pending {competitor!r}; it returned {result!r}"
        )
        links = page_links(repo, resumed["batch_id"])
        assert links and links[0][1] == artifact_id, (
            "the recovered page must link the handed-over artifact, not "
            f"{links!r}"
        )
