"""Independent U1-A (J-01) regression: a receipt is not current disk health.

Baseline 4023fca (product dda6fc1, schema 5). Written from the audit
(docs/audits/2026-11-11/FINDINGS.md, J-01) and the design
(docs/optimization/2026-11-11/DESIGN.md section 2, U1-A), not from the current
implementation: no private audit helper is used and no product code is changed.

Every test is synthetic: one temporary SQLite ledger, one temporary output root,
synthetic PNG bytes served by a fake transport and published by the real
PageDeliveryExecutor. No network, account, production library or production
database is touched.

Hard conditions the red tests impose on the next round (state, never error text):

* A page whose recorded file is missing, unreadable or no longer has the recorded
  digest is never counted as completed/reused ("existing") by a later task.
* The page stays in the batch scope (pages total 1) and is parked in an
  actionable non-complete state (a pending delivery or needs_verification).
* The historical artifact row, its delivery row and the finished batch counts are
  never rewritten or deleted; history stays history.
* The verifier never replaces bytes on disk; a changed file is preserved as is.
* A batch holding such a page cannot settle as "completed".

Green guards that must stay green: an intact file is still reused without a new
GET and without a second artifact, and the product's explicit redownload path
still restores bytes while keeping both artifact generations.

Interface assumption: the reuse decision is observable on the new task's batch
right after ``accept_source_page`` (plan/reuse time). If U1-A instead persists a
verification to-do and decides later, drive that step before asserting the same
states; the "must not be counted complete/reused" conditions may not be weakened.

Not covered here (W phase, see the skipped test at the end): unavailable roots,
symlinks/reparse points and files that change during the digest read.
"""
from __future__ import annotations

import hashlib
import sqlite3
import struct
import zlib
from pathlib import Path

import pytest

from core.image_validation import inspect_image
from core.library_repository import LibraryRepository
from core.preservation_policy import (
    ContentVersion,
    LocalEvidence,
    LocalState,
    PreservationOutcome,
    PreservationPolicy,
    RemovalIntent,
    assess_preservation,
)
from core.task_contracts import (
    CandidateDecision,
    CollectionRequest,
    OutputSpec,
    PageSpec,
    PathRef,
    SourcePage,
)
from delivery_fixtures import Response, Transport, executor, run
from image_fixtures import PNG_BYTES

ARTWORK = "123"
OUTPUT_REF = PathRef(relative_path="downl_Pixiv/123_p0.png")
ORIGINAL_URL = "https://i.pximg.net/img-original/2026/123_p0.png"
PNG_SHA256 = hashlib.sha256(PNG_BYTES).hexdigest()

ARTIFACT_FIELDS = (
    "artifact_id", "delivery_id", "job_id", "illust_id", "page_index", "output_key",
    "media_type", "size", "sha256", "verification", "completed_at", "created_at",
    "legacy_origin",
)
DELIVERY_FIELDS = (
    "delivery_id", "job_id", "origin_batch_id", "completed_batch_id", "state",
    "attempts", "http_attempts", "generation", "error_code", "output_key",
    "page_index", "illust_id", "quality_json", "payload_json", "created_at",
    "updated_at",
)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _png(red: int, green: int, blue: int, alpha: int = 255) -> bytes:
    """A structurally valid 1x1 RGBA PNG assembled from one scanline."""
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes((0, red, green, blue, alpha)), 9))
        + _chunk(b"IEND", b"")
    )


def _same_size_png() -> bytes:
    """A real PNG with exactly the shared sample's length and another digest."""
    for red in range(0, 256, 17):
        for green in range(0, 256, 17):
            for blue in range(0, 256, 51):
                candidate = _png(red, green, blue)
                if len(candidate) == len(PNG_BYTES) and candidate != PNG_BYTES:
                    return candidate
    raise AssertionError("no same-length alternative PNG in the fixed palette")


SAME_SIZE_PNG = _same_size_png()
NOT_AN_IMAGE = b"X" * len(PNG_BYTES)


@pytest.fixture
def repo(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as value:
        yield value


def target_path(root: Path) -> Path:
    return root / "downl_Pixiv" / "123_p0.png"


def submit_task(repo, request_id: str, *, dedup_policy: str = "trusted"):
    request = CollectionRequest(
        request_id,
        "offline-account-synthetic",
        output=OutputSpec(folder_by_artist=False),
        dedup_policy=dedup_policy,
    )
    job = repo.submit(request, f"submit-{request_id}")
    batch = repo.activate_next()
    assert batch is not None and batch["batch_id"] == job["batch_id"], (
        "the new task did not become the active batch: %r" % (batch,)
    )
    return job


def accept_page(repo, batch_id: str):
    page = SourcePage(
        "follow",
        "history",
        "7",
        "source-1",
        (
            CandidateDecision(
                ARTWORK,
                "download",
                artist_id="7",
                page_count=1,
                pages=(PageSpec(0, OUTPUT_REF, ORIGINAL_URL),),
            ),
        ),
        exhausted=True,
    )
    return repo.accept_source_page(batch_id, page)


def settle(repo, job, *, phase: str = "completed"):
    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"]) is True, (
        "the batch retained work and could not settle: %r"
        % (repo.batch_results(job["batch_id"]),)
    )
    assert repo.get_batch(job["batch_id"])["phase"] == phase
    return repo.batch_results(job["batch_id"])


def first_task(repo, root):
    """Deliver one synthetic PNG through the real executor, then settle."""
    job = submit_task(repo, "first")
    delivery_ids = accept_page(repo, job["batch_id"])
    assert len(delivery_ids) == 1
    outcome = run(repo, job, delivery_ids[0], executor(repo, root, Transport(Response())))
    assert outcome.state == "completed"
    settle(repo, job)
    return job, delivery_ids[0]


def second_task(repo, *, request_id: str = "second", dedup_policy: str = "trusted"):
    job = submit_task(repo, request_id, dedup_policy=dedup_policy)
    return job, accept_page(repo, job["batch_id"])


def pages(repo, job):
    return repo.batch_results(job["batch_id"])["pages"]


def state_report(repo, job, root) -> str:
    return (
        f"pages={pages(repo, job)} "
        f"dispatchable={repo.list_dispatchable(job['job_id'])} "
        f"file_exists={target_path(root).exists()}"
    )


def receipt(repo, delivery_id):
    """Everything the first task durably recorded: artifact, delivery, batch."""
    artifact = repo.get_artifact(delivery_id)
    delivery = repo.get_delivery(delivery_id)
    assert artifact is not None and delivery["state"] == "completed", (
        "the first task left no completed receipt to protect"
    )
    return (
        {name: artifact[name] for name in ARTIFACT_FIELDS},
        {name: delivery[name] for name in DELIVERY_FIELDS},
        repo.batch_results(delivery["completed_batch_id"])["pages"],
    )


def artifact_rows(database: Path) -> list:
    """Committed artifact rows for this page, read through a read-only handle."""
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT artifact_id, delivery_id, job_id, size, sha256, verification "
            "FROM artifacts WHERE illust_id = ? AND page_index = 0 "
            "ORDER BY artifact_id",
            (ARTWORK,),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def test_same_size_replacement_is_a_real_png_of_the_same_length(tmp_path):
    """The changed-file case must use real bytes, not arbitrary padding."""
    assert len(SAME_SIZE_PNG) == len(PNG_BYTES)
    assert SAME_SIZE_PNG != PNG_BYTES
    replacement = tmp_path / "same-size.png"
    replacement.write_bytes(SAME_SIZE_PNG)
    evidence = inspect_image(
        replacement, expected_size=len(SAME_SIZE_PNG), expected_media_type="image/png"
    )
    assert evidence.media_type == "image/png"
    assert evidence.verification == "structure_checked"
    assert evidence.sha256 == hashlib.sha256(SAME_SIZE_PNG).hexdigest()
    assert evidence.sha256 != PNG_SHA256


def test_deleted_file_is_not_reused_by_a_new_task(repo, tmp_path):
    """J-01 core case: the file is gone, yet the old receipt still answers."""
    _, first_delivery = first_task(repo, tmp_path)
    recorded = receipt(repo, first_delivery)
    target_path(tmp_path).unlink()

    job, _ = second_task(repo)
    counts = pages(repo, job)
    report = state_report(repo, job, tmp_path)
    assert counts["total"] == 1, (
        "J-01: the required page left the new batch scope; " + report
    )
    assert counts["completed"] == 0, (
        "J-01: the new task counted the page complete although its file is gone; " + report
    )
    assert counts["existing"] == 0, (
        "J-01: the new task reused the old receipt although its file is gone; " + report
    )
    assert counts["pending"] + counts["needs_verification"] == 1, (
        "J-01: the missing page was neither queued for repair nor flagged as unverified; "
        + report
    )

    assert receipt(repo, first_delivery) == recorded
    assert not target_path(tmp_path).exists(), "the verifier recreated a missing file"
    assert len(artifact_rows(repo.database_path)) == 1


def test_missing_page_is_queued_for_repair_and_the_repair_restores_bytes(repo, tmp_path):
    """DESIGN 2.4/2.5: missing keeps history, reserves a repair slot, then repairs."""
    _, first_delivery = first_task(repo, tmp_path)
    recorded = receipt(repo, first_delivery)
    original = target_path(tmp_path).read_bytes()
    target_path(tmp_path).unlink()

    job, _ = second_task(repo)
    counts = pages(repo, job)
    report = state_report(repo, job, tmp_path)
    assert counts["pending"] == 1, (
        "J-01/U1-A: the verified-missing page must reserve a repair delivery in this batch; "
        + report
    )
    queued = repo.list_dispatchable(job["job_id"])
    assert len(queued) == 1, report
    repair = repo.get_delivery(queued[0])
    assert (repair["illust_id"], repair["page_index"]) == (ARTWORK, 0), repair
    assert repair["state"] == "pending", repair

    outcome = run(repo, job, queued[0], executor(repo, tmp_path, Transport(Response())))
    assert outcome.state == "completed", outcome
    assert target_path(tmp_path).read_bytes() == original
    assert hashlib.sha256(target_path(tmp_path).read_bytes()).hexdigest() == PNG_SHA256

    after = pages(repo, job)
    assert after == {
        "total": 1, "completed": 1, "new_completed": 1, "existing": 0,
        "failed": 0, "needs_verification": 0, "pending": 0, "cancelled": 0,
    }, after
    rows = artifact_rows(repo.database_path)
    assert len(rows) == 2 and {row["sha256"] for row in rows} == {PNG_SHA256}, rows
    assert receipt(repo, first_delivery) == recorded


def test_explicit_redownload_still_restores_bytes_and_keeps_both_receipts(repo, tmp_path):
    """Reference path: an explicit redownload repairs without erasing history."""
    _, first_delivery = first_task(repo, tmp_path)
    recorded = receipt(repo, first_delivery)
    original = target_path(tmp_path).read_bytes()
    target_path(tmp_path).unlink()

    job, accepted = second_task(repo, request_id="second-redownload", dedup_policy="redownload")
    assert len(accepted) == 1, accepted
    assert pages(repo, job)["pending"] == 1, state_report(repo, job, tmp_path)

    outcome = run(repo, job, accepted[0], executor(repo, tmp_path, Transport(Response())))
    assert outcome.state == "completed", outcome
    assert target_path(tmp_path).read_bytes() == original
    rows = artifact_rows(repo.database_path)
    assert len(rows) == 2 and {row["sha256"] for row in rows} == {PNG_SHA256}, rows
    assert receipt(repo, first_delivery) == recorded


def test_equal_length_non_image_bytes_are_not_reused(repo, tmp_path):
    """J-01 second input: same size, other content, no image structure."""
    _, first_delivery = first_task(repo, tmp_path)
    recorded = receipt(repo, first_delivery)
    target_path(tmp_path).write_bytes(NOT_AN_IMAGE)

    job, _ = second_task(repo)
    counts = pages(repo, job)
    report = state_report(repo, job, tmp_path)
    assert counts["total"] == 1, report
    assert counts["completed"] == 0, (
        "J-01: the new task counted the page complete although the file holds "
        "non-image bytes of the recorded size; " + report
    )
    assert counts["existing"] == 0, (
        "J-01: the new task reused the old receipt for a file replaced by equal-length "
        "non-image bytes; " + report
    )
    assert counts["pending"] + counts["needs_verification"] == 1, (
        "J-01: the replaced page was neither queued for repair nor flagged as unverified; "
        + report
    )

    assert target_path(tmp_path).read_bytes() == NOT_AN_IMAGE, (
        "the verifier overwrote bytes it does not own"
    )
    assert receipt(repo, first_delivery) == recorded
    assert len(artifact_rows(repo.database_path)) == 1


def test_same_size_png_with_another_digest_is_not_reused_and_is_preserved(repo, tmp_path):
    """J-01 third input: a real PNG of the same size but a different digest."""
    _, first_delivery = first_task(repo, tmp_path)
    recorded = receipt(repo, first_delivery)
    target_path(tmp_path).write_bytes(SAME_SIZE_PNG)

    job, _ = second_task(repo)
    counts = pages(repo, job)
    report = state_report(repo, job, tmp_path)
    assert counts["total"] == 1, report
    assert counts["completed"] == 0, (
        "J-01: the new task counted the page complete although the file no longer holds "
        "the recorded digest; " + report
    )
    assert counts["existing"] == 0, (
        "J-01: the new task reused the old receipt for a same-size file with another digest; "
        + report
    )
    assert counts["pending"] + counts["needs_verification"] == 1, (
        "J-01: the changed page was neither queued nor flagged as unverified; " + report
    )

    assert target_path(tmp_path).read_bytes() == SAME_SIZE_PNG, (
        "a change the verifier noticed must be preserved, never overwritten"
    )
    assert receipt(repo, first_delivery) == recorded
    assert len(artifact_rows(repo.database_path)) == 1

    queued = repo.list_dispatchable(job["job_id"])
    if queued:
        outcome = run(repo, job, queued[0], executor(repo, tmp_path, Transport(Response())))
        assert outcome.state != "completed", outcome
        assert repo.get_artifact(queued[0]) is None
        assert target_path(tmp_path).read_bytes() == SAME_SIZE_PNG
        assert len(artifact_rows(repo.database_path)) == 1


@pytest.mark.parametrize("replacement", ["delete", "non_image", "same_size_png"])
def test_a_missing_or_changed_page_cannot_settle_as_completed(repo, tmp_path, replacement):
    """No claim of "本次范围完整" may be built on an unverified receipt."""
    first_task(repo, tmp_path)
    target = target_path(tmp_path)
    if replacement == "delete":
        target.unlink()
    elif replacement == "non_image":
        target.write_bytes(NOT_AN_IMAGE)
    else:
        target.write_bytes(SAME_SIZE_PNG)

    job, _ = second_task(repo)
    repo.finish_source(job["batch_id"], coverage_complete=True)
    settled = repo.settle_batch(job["batch_id"])
    batch = repo.get_batch(job["batch_id"])
    results = repo.batch_results(job["batch_id"])
    if settled:
        assert batch["phase"] != "completed", (
            "J-01: a batch claimed completion after its page file had changed or vanished "
            f"({replacement}); phase={batch['phase']} results={results!r}"
        )
    else:
        assert batch["phase"] == "settling", (
            "settlement was refused but the batch is not settling: %r" % (batch,)
        )
    assert results["works"]["completed"] == 0, (
        "J-01: the work was reported completed although its page file had changed or "
        f"vanished ({replacement}); results={results!r}"
    )


def test_an_intact_file_is_still_reused_without_a_new_download(repo, tmp_path):
    """Guard against "fix by never reusing": an untouched file is still skipped."""
    _, first_delivery = first_task(repo, tmp_path)
    recorded = receipt(repo, first_delivery)
    before = target_path(tmp_path).read_bytes()

    job, accepted = second_task(repo)
    counts = pages(repo, job)
    report = state_report(repo, job, tmp_path)
    assert counts["pending"] == 0 and counts["failed"] == 0, (
        "U1-A over-corrected: an intact file must not be queued for download; " + report
    )
    assert counts["needs_verification"] == 0, report
    assert counts["completed"] == 1 and counts["existing"] == 1, (
        "U1-A: an intact file must still be reused as an existing completion; " + report
    )
    assert counts["new_completed"] == 0, report
    assert accepted == () and repo.list_dispatchable(job["job_id"]) == (), report
    assert target_path(tmp_path).read_bytes() == before
    assert receipt(repo, first_delivery) == recorded
    assert len(artifact_rows(repo.database_path)) == 1

    finished = settle(repo, job)
    assert finished["pages"]["existing"] == 1
    assert finished["pages"]["new_completed"] == 0
    assert finished["works"]["completed"] == 1


@pytest.mark.parametrize("replacement", ["intact", "delete", "non_image", "same_size_png"])
def test_a_later_task_never_rewrites_or_deletes_the_historical_receipt(repo, tmp_path, replacement):
    """Historical delivery facts keep their meaning; repair adds a generation."""
    _, first_delivery = first_task(repo, tmp_path)
    recorded = receipt(repo, first_delivery)
    target = target_path(tmp_path)
    if replacement == "delete":
        target.unlink()
    elif replacement == "non_image":
        target.write_bytes(NOT_AN_IMAGE)
    elif replacement == "same_size_png":
        target.write_bytes(SAME_SIZE_PNG)

    second_task(repo, request_id=f"second-{replacement}")
    current = receipt(repo, first_delivery)
    assert current[0] == recorded[0], (
        "U1-A: the first artifact row was rewritten: before=%r after=%r"
        % (recorded[0], current[0])
    )
    assert current[1] == recorded[1], (
        "U1-A: the first delivery row was rewritten: before=%r after=%r"
        % (recorded[1], current[1])
    )
    assert current[2] == recorded[2], (
        "U1-A: the finished batch counts changed: before=%r after=%r"
        % (recorded[2], current[2])
    )


def test_intentional_removal_intent_suppresses_automatic_redownload_and_keeps_the_receipt(repo, tmp_path):
    """EXCLUDED is a user decision, not a repair order; revocation is not a match."""
    _, first_delivery = first_task(repo, tmp_path)
    recorded = receipt(repo, first_delivery)
    target_path(tmp_path).unlink()

    job = submit_task(repo, "second-intent")
    repo.record_removal_intent(
        job["batch_id"], ARTWORK, 0, OUTPUT_REF,
        intent="excluded", command_id="intent-excluded",
    )
    accepted = accept_page(repo, job["batch_id"])
    counts = pages(repo, job)
    report = state_report(repo, job, tmp_path)
    assert counts["total"] == 1, report
    assert counts["pending"] == 0, report
    assert counts["needs_verification"] == 0, report
    assert counts["cancelled"] == 1, report
    assert len(accepted) == 1, accepted
    assert repo.get_delivery(accepted[0])["state"] == "cancelled", report
    assert repo.list_dispatchable(job["job_id"]) == (), report
    assert not target_path(tmp_path).exists(), "EXCLUDED must not recreate the file"
    assert receipt(repo, first_delivery) == recorded
    assert len(artifact_rows(repo.database_path)) == 1

    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"]) is True
    assert repo.get_batch(job["batch_id"])["phase"] == "partial"

    third = submit_task(repo, "third-intent")
    repo.record_removal_intent(
        third["batch_id"], ARTWORK, 0, OUTPUT_REF,
        intent="none", command_id="intent-revoked",
    )
    repair_ids = accept_page(repo, third["batch_id"])
    assert pages(repo, third)["pending"] == 1, state_report(repo, third, tmp_path)
    outcome = run(repo, third, repair_ids[0], executor(repo, tmp_path, Transport(Response())))
    assert outcome.state == "completed", outcome
    assert target_path(tmp_path).read_bytes() == PNG_BYTES
    assert pages(repo, third)["new_completed"] == 1
    assert pages(repo, third)["existing"] == 0
    assert receipt(repo, first_delivery) == recorded


def test_removal_intent_decision_never_offers_an_automatic_redownload():
    """The wiring must reuse this decision layer; EXCLUDED is not a repair order."""
    content = ContentVersion("asset-123-p0", 1, len(PNG_BYTES), PNG_SHA256)
    evidence = LocalEvidence(
        content=content,
        path=OUTPUT_REF,
        root_revision=1,
        run_id="run-1",
        check_id="check-1",
        checked_at="2026-11-13T10:00:00+08:00",
        state=LocalState.MISSING,
    )
    automatic = {
        PreservationOutcome.REUSE_LOCAL,
        PreservationOutcome.REUSE_LOCAL_QUICK,
        PreservationOutcome.RESTORE_OFFERED,
        PreservationOutcome.VERIFY_LOCAL,
    }
    for policy in (PreservationPolicy(), PreservationPolicy(required_copy="local")):
        excluded = assess_preservation(
            content, OUTPUT_REF, root_revision=1, run_id="run-1", local=evidence,
            intent=RemovalIntent.EXCLUDED, policy=policy,
        )
        assert excluded.outcome is PreservationOutcome.EXCLUDED, excluded
        assert excluded.local_hash_verified is False
        assert excluded.outcome not in automatic, excluded
    wanted = assess_preservation(
        content, OUTPUT_REF, root_revision=1, run_id="run-1", local=evidence
    )
    assert wanted.outcome is PreservationOutcome.LOCATE_MISSING, wanted


def test_w_phase_local_boundaries_are_not_wired_into_reuse_yet():
    pytest.skip(
        "W 阶段剩余边界（本轮只登记，不强行通过）：根不可用/重解析点/读取中变化"
        "已能进入 needs_verification（见 test_u1a_reuse_gate 与 artifact_health 契约），"
        "但仍缺：①根不可用/外置卷的 UI 与恢复动作，不把整库当成丢失；"
        "②链接/重解析点指向根外时的端到端拒绝断言；"
        "③同一批次同一 artifact 只观察一次的缓存；④跨会话不复用旧缓存；"
        "⑤快速核验不得升级为严格复用的端到端用例。"
    )