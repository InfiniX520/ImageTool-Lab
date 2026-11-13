"""W4: an explicit exclusion suppresses automatic repair; history stays history."""
from __future__ import annotations

from pathlib import Path

import pytest

from core.library_repository import CommandConflict, LibraryError, LibraryRepository
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

OUTPUT = PathRef(relative_path="downl_Pixiv/123_p0.png")
URL = "https://i.pximg.net/img-original/2026/123_p0.png"


def request(request_id):
    return CollectionRequest(
        request_id,
        "offline-account-synthetic",
        output=OutputSpec(folder_by_artist=False),
        quality=QualitySpec(),
        budget=BatchBudget(max_works=10, max_requests=10, max_artists=5),
    )


def page(page_identity="page-1", before="null", after='"cursor-1"'):
    return SourcePage(
        "follow",
        "history",
        entity_id="source-1",
        page_identity=page_identity,
        candidates=(
            CandidateDecision(
                "123",
                "download",
                artist_id="7",
                page_count=1,
                pages=(PageSpec(0, OUTPUT, URL),),
            ),
        ),
        cursor_before_json=before,
        cursor_after_json=after,
        exhausted=True,
    )


def start(repo, request_id):
    job = repo.submit(request(request_id), "submit-" + request_id)
    batch = repo.activate_next()
    assert batch is not None and batch["batch_id"] == job["batch_id"]
    return job


def first_task(repo, root):
    job = start(repo, "first")
    (delivery,) = repo.accept_source_page(job["batch_id"], page())
    assert run(repo, job, delivery, executor(repo, root, Transport(Response()))).state == "completed"
    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"]) is True
    return job, delivery


def target(root: Path) -> Path:
    return root / "downl_Pixiv/123_p0.png"


def test_exclusion_suppresses_repair_and_revocation_allows_it_again(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        _, first_delivery = first_task(repo, tmp_path)
        artifact = repo.get_artifact(first_delivery)
        delivery_row = repo.get_delivery(first_delivery)
        target(tmp_path).unlink()

        second = start(repo, "second")
        recorded = repo.record_removal_intent(
            second["batch_id"], "123", 0, OUTPUT,
            intent="excluded", command_id="intent-exclude",
        )
        assert recorded["intent"] == "excluded"
        (cancelled,) = repo.accept_source_page(second["batch_id"], page("page-2"))
        counts = repo.batch_results(second["batch_id"])["pages"]
        assert counts["total"] == 1
        assert counts["completed"] == 0
        assert counts["existing"] == 0
        assert counts["pending"] == 0
        assert counts["needs_verification"] == 0
        assert counts["cancelled"] == 1
        assert repo.get_delivery(cancelled)["state"] == "cancelled"
        assert repo.get_delivery(cancelled)["error_code"] == "intent_excluded"
        assert repo.list_dispatchable(second["job_id"]) == ()
        assert not target(tmp_path).exists()
        assert repo.get_artifact(first_delivery) == artifact
        assert repo.get_delivery(first_delivery) == delivery_row

        # Same command id is replayable; reusing it for another intent conflicts.
        assert repo.record_removal_intent(
            second["batch_id"], "123", 0, OUTPUT,
            intent="excluded", command_id="intent-exclude",
        ) == recorded
        with pytest.raises(CommandConflict):
            repo.record_removal_intent(
                second["batch_id"], "123", 0, OUTPUT,
                intent="none", command_id="intent-exclude",
            )
        repo.finish_source(second["batch_id"], coverage_complete=True)
        assert repo.settle_batch(second["batch_id"]) is True
        assert repo.get_batch(second["batch_id"])["phase"] == "partial"

        # A new task after revocation may repair the missing page.
        third = start(repo, "third")
        revoked = repo.record_removal_intent(
            third["batch_id"], "123", 0, OUTPUT,
            intent="none", command_id="intent-revoke",
        )
        assert revoked["intent"] == "none"
        (repair,) = repo.accept_source_page(third["batch_id"], page("page-3"))
        assert repo.batch_results(third["batch_id"])["pages"]["pending"] == 1
        assert run(repo, third, repair, executor(repo, tmp_path, Transport(Response()))).state == "completed"
        assert target(tmp_path).read_bytes() == PNG_BYTES
        assert repo.get_artifact(first_delivery) == artifact
        assert repo.get_delivery(first_delivery) == delivery_row


def test_offloaded_intent_is_not_falsely_advertised(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job = start(repo, "offloaded")
        with pytest.raises(LibraryError):
            repo.record_removal_intent(
                job["batch_id"], "123", 0, OUTPUT,
                intent="offloaded", command_id="intent-offloaded",
            )
