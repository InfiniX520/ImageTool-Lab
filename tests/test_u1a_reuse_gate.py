"""W2/W3 gates: current health is required on every delivery reuse path.

All files, databases and transports are synthetic temporary objects.  No
network, account, production library or real gallery is touched.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.app_paths import AppPaths
from core.artifact_health import (
    ArtifactExpectation, ArtifactHealth, ArtifactHealthPolicy, HealthPolicyMode,
)
from core.library_paths import PathResolver
from core.library_repository import LibraryError, LibraryRepository
from core.preservation_policy import LocalState
from core.task_contracts import (
    BatchBudget,
    CandidateDecision,
    CollectionRequest,
    OutputSpec,
    PageSpec,
    PathRef,
    QualitySpec,
    SourcePage,
)
from delivery_fixtures import Response, Transport, executor, run
from image_fixtures import PNG_BYTES

ARTWORK = "123"
URL = "https://i.pximg.net/img-original/2026/123_p0.png"


def request(request_id="req", *, max_works=10, dedup_policy="trusted", all_pages=True):
    return CollectionRequest(
        request_id,
        "offline-account-synthetic",
        output=OutputSpec(folder_by_artist=False),
        quality=QualitySpec(all_pages=all_pages),
        budget=BatchBudget(max_works=max_works, max_requests=10, max_artists=5),
        dedup_policy=dedup_policy,
    )


def candidate(*, pages=(0,), page_count=None):
    specs = tuple(
        PageSpec(index, PathRef(relative_path=f"downl_Pixiv/123_p{index}.png"), URL)
        for index in pages
    )
    return CandidateDecision(
        ARTWORK,
        "download",
        artist_id="7",
        page_count=page_count if page_count is not None else len(specs),
        pages=specs,
    )


def source(candidates, *, page_identity="page-1", before="null", after='"cursor-1"'):
    return SourcePage(
        "follow",
        "history",
        page_identity=page_identity,
        candidates=tuple(candidates),
        entity_id="source-1",
        cursor_before_json=before,
        cursor_after_json=after,
        exhausted=True,
    )


def submit(repo, *, max_works=10, dedup_policy="trusted", request_id="req"):
    submitted = repo.submit(
        request(request_id, max_works=max_works, dedup_policy=dedup_policy),
        "submit-" + request_id,
    )
    batch = repo.activate_next()
    assert batch is not None and batch["batch_id"] == submitted["batch_id"]
    return submitted


def accept(repo, batch_id, page):
    return repo.accept_source_page(batch_id, page)


def target(root: Path) -> Path:
    return root / "downl_Pixiv/123_p0.png"


def first_delivery(repo, root):
    job = submit(repo, request_id="first")
    ids = accept(repo, job["batch_id"], source((candidate(),)))
    assert len(ids) == 1
    assert run(repo, job, ids[0], executor(repo, root, Transport(Response()))).state == "completed"
    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"]) is True
    return job, ids[0]


def second(repo, *, max_works=10, request_id="second"):
    job = submit(repo, max_works=max_works, request_id=request_id)
    accepted = accept(repo, job["batch_id"], source((candidate(),), page_identity="page-2"))
    return job, accepted


def counts_work(repo, batch_id):
    connection = sqlite3.connect(repo.database_path.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(
            "SELECT counts_work FROM batch_items WHERE batch_id = ?", (batch_id,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


def test_reopened_repository_without_root_binding_fails_closed(tmp_path):
    database = tmp_path / "ledger.sqlite3"
    with LibraryRepository(database) as repo:
        _, delivery = first_delivery(repo, tmp_path)
        assert target(tmp_path).exists()
    with LibraryRepository(database) as reopened:
        job, accepted = second(reopened)
        counts = reopened.batch_results(job["batch_id"])["pages"]
        assert counts["completed"] == 0
        assert counts["existing"] == 0
        assert counts["pending"] == 0
        assert counts["needs_verification"] == 1
        assert len(accepted) == 1
        assert reopened.get_delivery(accepted[0])["state"] == "needs_verification"


def test_writer_rejects_a_forged_health_policy(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        repo.bind_path_resolver(PathResolver(AppPaths(tmp_path)))
        path = PathRef(relative_path="downl_Pixiv/123_p0.png")
        expectation = ArtifactExpectation(
            artifact_id="a" * 32, delivery_id="d" * 32, job_id="j" * 32,
            illust_id="123", page_index=0, output_key=path.target_key, path=path,
            root_revision=1, size_bytes=len(PNG_BYTES),
            sha256=hashlib.sha256(PNG_BYTES).hexdigest(), media_type="image/png",
            quality_key="q" * 64,
        )
        forged = ArtifactHealth(
            expectation=expectation,
            policy=ArtifactHealthPolicy(mode=HealthPolicyMode.QUICK, max_age_seconds=86400),
            state=LocalState.QUICK_MATCH, checked_at="2026-11-13T10:00:00+08:00",
            reason="quick_metadata", observed_size=len(PNG_BYTES),
        )
        assert repo._health_map([forged.to_payload()]) == {}
        valid = ArtifactHealth(
            expectation=expectation, policy=ArtifactHealthPolicy(),
            state=LocalState.HASH_MATCH,
            checked_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            reason="full_sha256_match", observed_size=len(PNG_BYTES),
            observed_sha256=expectation.sha256,
        )
        assert expectation.artifact_id in repo._health_map([valid.to_payload()])


def test_binding_refuses_a_different_project_root(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        repo.bind_path_resolver(PathResolver(AppPaths(tmp_path / "one")))
        with pytest.raises(LibraryError):
            repo.bind_path_resolver(PathResolver(AppPaths(tmp_path / "two")))


def test_stale_preflight_cannot_reuse_an_intact_file(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, delivery = first_delivery(repo, tmp_path)
        original = repo._preflight_page_health

        def stale(batch_id, page):
            return tuple(
                replace(record, checked_at="2020-01-01T00:00:00+00:00")
                for record in original(batch_id, page)
            )

        monkeypatch.setattr(repo, "_preflight_page_health", stale)
        job, accepted = second(repo)
        counts = repo.batch_results(job["batch_id"])["pages"]
        assert counts["existing"] == 0
        assert counts["needs_verification"] == 1
        assert accepted and repo.get_delivery(accepted[0])["state"] == "needs_verification"


def test_preflight_runs_outside_the_repository_writer_thread(tmp_path, monkeypatch):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, delivery = first_delivery(repo, tmp_path)
        from core.local_observation import LocalObserver

        original = LocalObserver.observe
        threads = []

        def observe(self, request):
            threads.append(threading.current_thread().name)
            return original(self, request)

        monkeypatch.setattr(LocalObserver, "observe", observe)
        job, accepted = second(repo)
        assert accepted == ()
        assert threads
        assert all(not name.startswith("jpgpng-library-writer") for name in threads)


def test_work_budget_uses_the_same_health_decision(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        first_delivery(repo, tmp_path)

        intact_job, _ = second(repo, max_works=1, request_id="second-intact")
        assert counts_work(repo, intact_job["batch_id"]) == 0
        assert repo.batch_results(intact_job["batch_id"])["pages"]["existing"] == 1
        repo.finish_source(intact_job["batch_id"], coverage_complete=True)
        assert repo.settle_batch(intact_job["batch_id"]) is True

        target(tmp_path).unlink()
        missing_job, accepted = second(repo, max_works=1, request_id="second-missing")
        assert counts_work(repo, missing_job["batch_id"]) == 1
        assert len(accepted) == 1
        assert repo.batch_results(missing_job["batch_id"])["pages"]["pending"] == 1


def test_claim_without_accept_does_not_copy_an_unverified_completed_page(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job = submit(repo, request_id="claim-only-first")
        ids = accept(repo, job["batch_id"], source((candidate(pages=(0, 1)),)))
        transport = Transport(Response(), Response(status=404))
        assert run(repo, job, ids[0], executor(repo, tmp_path, transport)).state == "completed"
        assert run(repo, job, ids[1], executor(repo, tmp_path, transport)).state == "failed"
        repo.finish_source(job["batch_id"], coverage_complete=True)
        assert repo.settle_batch(job["batch_id"]) is True
        artifact = repo.get_artifact(ids[0])
        target(tmp_path).unlink()

        resumed = repo.resume(job["job_id"], "resume-claim-only", retry_failed=True)
        batch = repo.activate_next()
        assert batch is not None and batch["batch_id"] == resumed["batch_id"]
        (carried,) = repo.list_dispatchable(job["job_id"])
        assert repo.claim_delivery(carried, resumed["batch_id"]) is not None
        counts = repo.batch_results(resumed["batch_id"])["pages"]
        assert counts["completed"] == 0
        assert counts["existing"] == 0
        assert counts["needs_verification"] == 1
        assert counts["pending"] == 1
        assert not target(tmp_path).exists()
        assert repo.get_artifact(ids[0]) == artifact

def test_new_task_supersedes_a_dormant_verification_hold(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        first_delivery(repo, tmp_path)
        target(tmp_path).write_bytes(b"X" * len(PNG_BYTES))
        second_job, (old_hold,) = second(repo, request_id="hold-one")
        assert repo.get_delivery(old_hold)["state"] == "needs_verification"
        repo.finish_source(second_job["batch_id"], coverage_complete=True)
        assert repo.settle_batch(second_job["batch_id"]) is True
        assert repo.get_batch(second_job["batch_id"])["phase"] == "partial"

        third_job, (new_hold,) = second(repo, request_id="hold-two")
        assert new_hold != old_hold
        assert repo.get_delivery(new_hold)["state"] == "needs_verification"
        old = repo.get_delivery(old_hold)
        assert old["state"] == "failed"
        assert old["error_code"] == "superseded_by_new_task"
        assert repo.batch_results(third_job["batch_id"])["pages"]["needs_verification"] == 1

def test_carry_keeps_an_intact_completed_page_as_existing(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job = submit(repo, request_id="carry-intact")
        ids = accept(repo, job["batch_id"], source((candidate(pages=(0, 1)),)))
        transport = Transport(Response(), Response(status=404))
        assert run(repo, job, ids[0], executor(repo, tmp_path, transport)).state == "completed"
        assert run(repo, job, ids[1], executor(repo, tmp_path, transport)).state == "failed"
        repo.finish_source(job["batch_id"], coverage_complete=True)
        assert repo.settle_batch(job["batch_id"]) is True
        assert repo.get_batch(job["batch_id"])["phase"] == "partial"

        resumed = repo.resume(job["job_id"], "resume-carry-intact")
        batch = repo.activate_next()
        assert batch is not None and batch["batch_id"] == resumed["batch_id"]
        carried = repo.carry_unresolved_items(batch["batch_id"])
        assert carried["count"] == 1
        counts = repo.batch_results(batch["batch_id"])["pages"]
        assert counts["completed"] == 1
        assert counts["existing"] == 1
        assert counts["failed"] == 1
        assert counts["needs_verification"] == 0
        assert target(tmp_path).exists()

def test_changed_page_needs_verification_without_consuming_work_budget(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        first_delivery(repo, tmp_path)
        target(tmp_path).write_bytes(b"X" * len(PNG_BYTES))
        job, accepted = second(repo, max_works=1, request_id="second-changed")
        assert counts_work(repo, job["batch_id"]) == 0
        counts = repo.batch_results(job["batch_id"])["pages"]
        assert counts["needs_verification"] == 1
        assert counts["pending"] == 0
        assert counts["existing"] == 0
        assert len(accepted) == 1
        assert repo.get_delivery(accepted[0])["state"] == "needs_verification"
        assert repo.list_dispatchable(job["job_id"]) == ()

def test_same_job_completed_delivery_is_not_reused_after_resume(tmp_path):
    """The same-job replay path must use health, not the completed receipt."""
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job = submit(repo, request_id="first-two")
        ids = accept(repo, job["batch_id"], source((candidate(pages=(0, 1)),)))
        assert len(ids) == 2
        transport = Transport(Response(), Response(status=404))
        assert run(repo, job, ids[0], executor(repo, tmp_path, transport)).state == "completed"
        assert run(repo, job, ids[1], executor(repo, tmp_path, transport)).state == "failed"
        repo.finish_source(job["batch_id"], coverage_complete=True)
        assert repo.settle_batch(job["batch_id"]) is True
        assert repo.get_batch(job["batch_id"])["phase"] == "partial"

        first_artifact = repo.get_artifact(ids[0])
        first_delivery_row = repo.get_delivery(ids[0])
        target(tmp_path).unlink()

        resumed = repo.resume(job["job_id"], "resume-1")
        batch = repo.activate_next()
        assert batch is not None and batch["batch_id"] == resumed["batch_id"]
        repaired = accept(
            repo,
            resumed["batch_id"],
            source((candidate(page_count=1),), page_identity="page-repair",
                   before='"cursor-1"', after='"cursor-2"'),
        )
        assert len(repaired) == 1
        assert repaired[0] != ids[0]
        counts = repo.batch_results(resumed["batch_id"])["pages"]
        assert counts["completed"] == 0
        assert counts["existing"] == 0
        assert counts["pending"] == 1
        assert repo.get_delivery(repaired[0])["state"] == "pending"
        assert repo.get_artifact(ids[0]) == first_artifact
        assert repo.get_delivery(ids[0]) == first_delivery_row

        resumed_job = {**job, "batch_id": resumed["batch_id"]}
        assert run(repo, resumed_job, repaired[0],
                   executor(repo, tmp_path, Transport(Response()))).state == "completed"
        assert target(tmp_path).read_bytes() == PNG_BYTES
        after = repo.batch_results(resumed["batch_id"])["pages"]
        assert after["completed"] == 1
        assert after["new_completed"] == 1
        assert after["existing"] == 0
        assert repo.get_artifact(ids[0]) == first_artifact
