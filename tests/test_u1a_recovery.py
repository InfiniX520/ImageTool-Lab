"""U1-A/W5: the explicit offline recovery entry for verification holds.

Every test is synthetic: one temporary SQLite ledger, one temporary output
root, synthetic PNG bytes served by a fake transport and published by the real
PageDeliveryExecutor.  No network, account, production library, real gallery or
Tk widget is touched.  No test deletes, rewrites or renumbers a historical
artifact or a completed delivery.

The recovery contract under test (core/delivery_recovery.py):

* hash_match  -> the page is reused again; the hold is retired as cancelled
* missing     -> one new pending repair generation; the hold is retired
* changed     -> the hold stays needs_verification; the file is preserved
* unreadable / root_unavailable / changed_during_check -> nothing is guessed
* repeated calls and command ids never create a second generation
"""
from __future__ import annotations

import hashlib
import sqlite3
import struct
import threading
import zlib
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.artifact_health import (
    ArtifactExpectation,
    ArtifactHealth,
    ArtifactHealthPolicy,
)
from core.delivery_recovery import (
    ACTIVE_OUTPUT_STATES,
    RecoveryAction,
    RecoveryDecision,
    RecoveryOutcome,
    decide_page_recovery,
)
from core.library_repository import (
    CommandConflict,
    LibraryError,
    LibraryRepository,
    NotFoundError,
)
from core.local_observation import LocalObserver
from core.preservation_policy import LocalState
from core.task_contracts import (
    BatchBudget,
    CandidateDecision,
    CollectionRequest,
    ContractError,
    OutputSpec,
    PageSpec,
    PathRef,
    QualitySpec,
    SourcePage,
)
from delivery_fixtures import Response, Transport, executor, run
from image_fixtures import PNG_BYTES

ARTWORK = "123"
OTHER_ARTWORK = "456"
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
)


# ---------------------------------------------------------------------------
# Synthetic images: a real PNG, a same-length PNG and non-image bytes
# ---------------------------------------------------------------------------


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _png(red: int, green: int, blue: int, alpha: int = 255) -> bytes:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes((0, red, green, blue, alpha)), 9))
        + _chunk(b"IEND", b"")
    )


def _same_size_png() -> bytes:
    for red in range(0, 256, 17):
        for green in range(0, 256, 17):
            for blue in range(0, 256, 51):
                candidate = _png(red, green, blue)
                if len(candidate) == len(PNG_BYTES) and candidate != PNG_BYTES:
                    return candidate
    raise AssertionError("no same-length alternative PNG in the fixed palette")


SAME_SIZE_PNG = _same_size_png()
NOT_AN_IMAGE = b"X" * len(PNG_BYTES)


# ---------------------------------------------------------------------------
# Repository / task fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as value:
        yield value


@pytest.fixture
def library_root(tmp_path):
    """The output root is a child of tmp_path so it can be taken offline alone."""
    root = tmp_path / "library"
    root.mkdir()
    return root


def request(request_id="req", *, max_works=10, dedup_policy="trusted"):
    return CollectionRequest(
        request_id,
        "offline-account-synthetic",
        output=OutputSpec(folder_by_artist=False),
        quality=QualitySpec(all_pages=True),
        budget=BatchBudget(max_works=max_works, max_requests=10, max_artists=5),
        dedup_policy=dedup_policy,
    )


def candidate(illust_id=ARTWORK, *, page_index=0, page_count=1):
    return CandidateDecision(
        illust_id,
        "download",
        artist_id="7",
        page_count=page_count,
        pages=(
            PageSpec(
                page_index,
                PathRef(relative_path=f"downl_Pixiv/{illust_id}_p{page_index}.png"),
                f"https://i.pximg.net/img-original/2026/{illust_id}_p{page_index}.png",
            ),
        ),
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


def submit(repo, *, request_id="req", max_works=10, dedup_policy="trusted"):
    submitted = repo.submit(
        request(request_id, max_works=max_works, dedup_policy=dedup_policy),
        "submit-" + request_id,
    )
    batch = repo.activate_next()
    assert batch is not None and batch["batch_id"] == submitted["batch_id"]
    return submitted


def accept(repo, batch_id, page):
    return repo.accept_source_page(batch_id, page)


def target(root: Path, illust_id: str = ARTWORK) -> Path:
    return root / "downl_Pixiv" / f"{illust_id}_p0.png"


def pages(repo, job):
    return repo.batch_results(job["batch_id"])["pages"]


def first_delivery(repo, root):
    """Deliver one synthetic PNG through the real executor, then settle."""
    job = submit(repo, request_id="first")
    ids = accept(repo, job["batch_id"], source((candidate(),)))
    assert len(ids) == 1
    outcome = run(repo, job, ids[0], executor(repo, root, Transport(Response())))
    assert outcome.state == "completed", outcome
    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"]) is True
    assert repo.get_batch(job["batch_id"])["phase"] == "completed"
    return job, ids[0]


def second_task(repo, root, *, request_id="second", max_works=10, candidates=None):
    job = submit(repo, request_id=request_id, max_works=max_works)
    accepted = accept(
        repo,
        job["batch_id"],
        source(
            (candidate(),) if candidates is None else candidates,
            page_identity="page-" + request_id,
        ),
    )
    return job, accepted


def parked_hold(repo, root):
    """A same-size replacement parks the second task in needs_verification."""
    _, first = first_delivery(repo, root)
    target(root).write_bytes(SAME_SIZE_PNG)
    job, accepted = second_task(repo, root)
    assert len(accepted) == 1
    assert repo.get_delivery(accepted[0])["state"] == "needs_verification"
    return job, accepted[0], first


def queued_repair(repo, root):
    """A deleted file makes the second task reserve one pending repair."""
    _, first = first_delivery(repo, root)
    target(root).unlink()
    job, accepted = second_task(repo, root)
    assert len(accepted) == 1
    assert repo.get_delivery(accepted[0])["state"] == "pending"
    return job, accepted[0], first


# ---------------------------------------------------------------------------
# Read-only assertions about durable history
# ---------------------------------------------------------------------------


def receipt(repo, delivery_id):
    """Everything the first task durably recorded: artifact, delivery, batch."""
    artifact = repo.get_artifact(delivery_id)
    delivery = repo.get_delivery(delivery_id)
    assert artifact is not None and delivery["state"] == "completed"
    return (
        {name: artifact[name] for name in ARTIFACT_FIELDS},
        {name: delivery[name] for name in DELIVERY_FIELDS},
        repo.batch_results(delivery["completed_batch_id"])["pages"],
    )


def query(database: Path, sql: str, parameters=()):
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return [dict(row) for row in connection.execute(sql, parameters).fetchall()]
    finally:
        connection.close()


def artifact_rows(database: Path):
    return query(
        database,
        "SELECT artifact_id, delivery_id, job_id, size, sha256, verification "
        "FROM artifacts WHERE illust_id = ? AND page_index = 0 ORDER BY artifact_id",
        (ARTWORK,),
    )


def delivery_rows(database: Path, illust_id: str = ARTWORK):
    return query(
        database,
        "SELECT delivery_id, job_id, origin_batch_id, state, generation, error_code, "
        "checkpoint_json, prepared_json FROM deliveries "
        "WHERE illust_id = ? AND page_index = 0 ORDER BY generation, rowid",
        (illust_id,),
    )


def active_output_rows(database: Path, output_key: str):
    marks = ",".join("?" for _ in ACTIVE_OUTPUT_STATES)
    return query(
        database,
        f"SELECT delivery_id, state FROM deliveries WHERE output_key = ? "
        f"AND state IN ({marks})",
        (output_key, *ACTIVE_OUTPUT_STATES),
    )


# ---------------------------------------------------------------------------
# The pure state machine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "previous,health,reusable,unavailable,expected",
    [
        (
            "completed", None, False, "observation_unknown",
            (RecoveryOutcome.COMPLETED, RecoveryAction.NONE, "already_completed"),
        ),
        (
            "completed", LocalState.HASH_MATCH, True, "observation_unknown",
            (RecoveryOutcome.COMPLETED, RecoveryAction.NONE, "already_completed"),
        ),
        (
            "needs_verification", LocalState.HASH_MATCH, True, "observation_unknown",
            (RecoveryOutcome.REUSED, RecoveryAction.REUSE, "hash_match"),
        ),
        (
            "needs_verification", LocalState.HASH_MATCH, False, "observation_unknown",
            (RecoveryOutcome.HELD, RecoveryAction.NONE, "observation_unknown"),
        ),
        (
            "needs_verification", LocalState.MISSING, False, "observation_unknown",
            (RecoveryOutcome.REPAIR_QUEUED, RecoveryAction.REPAIR, "artifact_missing"),
        ),
        (
            "pending", LocalState.MISSING, False, "observation_unknown",
            (RecoveryOutcome.REPAIR_QUEUED, RecoveryAction.NONE, "repair_already_pending"),
        ),
        (
            "needs_verification", LocalState.CHANGED, False, "observation_unknown",
            (RecoveryOutcome.HELD, RecoveryAction.NONE, "artifact_changed"),
        ),
        (
            "pending", LocalState.CHANGED, False, "observation_unknown",
            (RecoveryOutcome.HELD, RecoveryAction.PARK, "artifact_changed"),
        ),
        (
            "needs_verification", LocalState.UNREADABLE, False, "observation_unknown",
            (RecoveryOutcome.HELD, RecoveryAction.NONE, "artifact_unreadable"),
        ),
        (
            "needs_verification", LocalState.ROOT_UNAVAILABLE, False, "observation_unknown",
            (RecoveryOutcome.HELD, RecoveryAction.NONE, "artifact_root_unavailable"),
        ),
        (
            "needs_verification", LocalState.CHANGED_DURING_CHECK, False,
            "observation_unknown",
            (RecoveryOutcome.HELD, RecoveryAction.NONE, "artifact_changed_during_check"),
        ),
        (
            "needs_verification", LocalState.UNKNOWN, False, "observation_unknown",
            (RecoveryOutcome.HELD, RecoveryAction.NONE, "observation_unknown"),
        ),
        (
            "pending", LocalState.QUICK_MATCH, False, "observation_unknown",
            (RecoveryOutcome.HELD, RecoveryAction.NONE, "observation_unknown"),
        ),
        (
            "needs_verification", None, False, "no_recorded_artifact",
            (RecoveryOutcome.HELD, RecoveryAction.NONE, "no_recorded_artifact"),
        ),
        (
            "failed", LocalState.HASH_MATCH, True, "observation_unknown",
            (RecoveryOutcome.REFUSED, RecoveryAction.NONE, "unsupported_previous_state"),
        ),
        (
            "cancelled", None, False, "observation_unknown",
            (RecoveryOutcome.REFUSED, RecoveryAction.NONE, "unsupported_previous_state"),
        ),
        (
            "transferring", None, False, "observation_unknown",
            (RecoveryOutcome.REFUSED, RecoveryAction.NONE, "unsupported_previous_state"),
        ),
    ],
)
def test_the_recovery_state_machine_is_explicit(previous, health, reusable, unavailable, expected):
    decision = decide_page_recovery(
        previous, health, reusable=reusable, unavailable_reason=unavailable
    )
    assert (decision.outcome, decision.action, decision.reason) == expected


def test_the_decision_rejects_invalid_evidence():
    with pytest.raises(ContractError):
        decide_page_recovery("needs_verification", "hash_match", reusable=True)
    with pytest.raises(ContractError):
        decide_page_recovery("needs_verification", LocalState.MISSING, reusable=1)
    with pytest.raises(ContractError):
        decide_page_recovery(
            "needs_verification", LocalState.MISSING, reusable=False,
            unavailable_reason="Not_A_Code",
        )
    with pytest.raises(ContractError):
        RecoveryDecision("reused", RecoveryAction.REUSE, "hash_match")
    with pytest.raises(ContractError):
        RecoveryDecision(RecoveryOutcome.HELD, "none", "artifact_changed")


# ---------------------------------------------------------------------------
# hash_match: reuse without a download
# ---------------------------------------------------------------------------


def test_hash_match_restores_the_page_without_any_download(repo, library_root):
    job, hold_id, first = parked_hold(repo, library_root)
    recorded = receipt(repo, first)
    hold = repo.get_delivery(hold_id)
    assert hold["state"] == "needs_verification"
    assert pages(repo, job)["needs_verification"] == 1

    target(library_root).write_bytes(PNG_BYTES)
    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

    assert result["outcome"] == "reused", result
    assert result["reason"] == "hash_match"
    assert result["state"] == "completed"
    assert result["previous_delivery_id"] == hold_id
    assert result["previous_state"] == "needs_verification"
    assert result["artifact_id"] == recorded[0]["artifact_id"]
    assert result["repair_delivery_id"] is None
    assert result["observed_sha256"] == PNG_SHA256

    counts = pages(repo, job)
    assert counts["completed"] == 1 and counts["existing"] == 1, counts
    assert counts["new_completed"] == 0 and counts["needs_verification"] == 0, counts
    assert counts["pending"] == 0 and counts["cancelled"] == 0, counts
    assert repo.list_dispatchable(job["job_id"]) == ()

    retired = repo.get_delivery(hold_id)
    assert retired["state"] == "cancelled", retired
    assert retired["error_code"] == "recovered_hash_match", retired
    assert receipt(repo, first) == recorded
    assert len(artifact_rows(repo.database_path)) == 1
    assert active_output_rows(repo.database_path, recorded[0]["output_key"]) == []
    assert target(library_root).read_bytes() == PNG_BYTES

    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"]) is True
    assert repo.get_batch(job["batch_id"])["phase"] == "completed"
    assert repo.batch_results(job["batch_id"])["works"]["completed"] == 1
    assert receipt(repo, first) == recorded


def test_a_page_downloaded_by_this_task_stays_a_new_completion(repo, library_root):
    """Same-task replay: the recovered artifact belongs to this very batch."""
    job = submit(repo, request_id="same-job")
    first = accept(repo, job["batch_id"], source((candidate(),), page_identity="page-a"))
    assert len(first) == 1
    outcome = run(repo, job, first[0], executor(repo, library_root, Transport(Response())))
    assert outcome.state == "completed", outcome
    assert pages(repo, job)["new_completed"] == 1

    target(library_root).write_bytes(SAME_SIZE_PNG)
    second = accept(
        repo,
        job["batch_id"],
        source(
            (candidate(),),
            page_identity="page-b",
            before='"cursor-1"',
            after='"cursor-2"',
        ),
    )
    assert len(second) == 1
    assert repo.get_delivery(second[0])["state"] == "needs_verification"
    assert repo.get_delivery(second[0])["job_id"] == job["job_id"]

    target(library_root).write_bytes(PNG_BYTES)
    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

    assert result["outcome"] == "reused", result
    counts = pages(repo, job)
    assert counts["completed"] == 1 and counts["new_completed"] == 1, counts
    assert counts["existing"] == 0 and counts["needs_verification"] == 0, counts
    assert repo.get_delivery(second[0])["state"] == "cancelled"
    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"]) is True
    assert repo.get_batch(job["batch_id"])["phase"] == "completed"


# ---------------------------------------------------------------------------
# missing: one pending repair generation, then publish
# ---------------------------------------------------------------------------


def test_missing_on_a_hold_queues_one_repair_generation(repo, library_root):
    job, hold_id, first = parked_hold(repo, library_root)
    recorded = receipt(repo, first)
    hold = repo.get_delivery(hold_id)
    target(library_root).unlink()

    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

    assert result["outcome"] == "repair_queued", result
    assert result["reason"] == "artifact_missing"
    assert result["state"] == "pending"
    repair_id = result["repair_delivery_id"]
    assert repair_id is not None and repair_id != hold_id
    assert result["delivery_id"] == repair_id
    repair = repo.get_delivery(repair_id)
    assert repair["generation"] == hold["generation"] + 1
    assert repair["state"] == "pending" and repair["http_attempts"] == 0
    assert repair["checkpoint_json"] is None and repair["prepared_json"] is None
    assert repair["output_key"] == hold["output_key"]
    assert repair["payload_json"] == hold["payload_json"]

    retired = repo.get_delivery(hold_id)
    assert retired["state"] == "cancelled", retired
    assert retired["error_code"] == "superseded_by_recovery_repair", retired
    assert repo.list_dispatchable(job["job_id"]) == (repair_id,)
    assert pages(repo, job)["pending"] == 1
    assert receipt(repo, first) == recorded
    assert len(active_output_rows(repo.database_path, recorded[0]["output_key"])) == 1

    outcome = run(repo, job, repair_id, executor(repo, library_root, Transport(Response())))
    assert outcome.state == "completed", outcome
    assert target(library_root).read_bytes() == PNG_BYTES
    rows = artifact_rows(repo.database_path)
    assert len(rows) == 2 and {row["sha256"] for row in rows} == {PNG_SHA256}, rows
    after = pages(repo, job)
    assert after["completed"] == 1 and after["new_completed"] == 1, after
    assert after["existing"] == 0 and after["pending"] == 0, after
    assert receipt(repo, first) == recorded
    assert active_output_rows(repo.database_path, recorded[0]["output_key"]) == []

    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"]) is True
    assert repo.get_batch(job["batch_id"])["phase"] == "completed"


# ---------------------------------------------------------------------------
# changed: keep the hold, preserve the bytes, report the reason
# ---------------------------------------------------------------------------


def test_changed_keeps_the_hold_and_reports_a_reason(repo, library_root):
    job, hold_id, first = parked_hold(repo, library_root)
    recorded = receipt(repo, first)
    before = target(library_root).read_bytes()
    generations = len(delivery_rows(repo.database_path))

    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

    assert result["outcome"] == "held", result
    assert result["reason"] == "artifact_changed"
    assert result["state"] == "needs_verification"
    assert result["observed_sha256"] == hashlib.sha256(SAME_SIZE_PNG).hexdigest()
    assert result["delivery_id"] == hold_id
    assert result["repair_delivery_id"] is None
    assert repo.get_delivery(hold_id)["state"] == "needs_verification"
    assert len(delivery_rows(repo.database_path)) == generations
    assert target(library_root).read_bytes() == before
    assert receipt(repo, first) == recorded
    assert len(artifact_rows(repo.database_path)) == 1

    again = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)
    assert again["outcome"] == "held" and again["reason"] == "artifact_changed"
    assert len(delivery_rows(repo.database_path)) == generations

    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"]) is True
    assert repo.get_batch(job["batch_id"])["phase"] == "partial"
    assert target(library_root).read_bytes() == before


# ---------------------------------------------------------------------------
# root_unavailable: never guess loss
# ---------------------------------------------------------------------------


def test_root_unavailable_is_not_reported_as_loss(repo, library_root, tmp_path):
    _, first = first_delivery(repo, library_root)
    recorded = receipt(repo, first)
    offline = tmp_path / "library-offline"
    library_root.rename(offline)

    job, accepted = second_task(repo, library_root)
    assert repo.get_delivery(accepted[0])["state"] == "needs_verification"
    generations = len(delivery_rows(repo.database_path))

    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

    assert result["outcome"] == "held", result
    assert result["reason"] == "artifact_root_unavailable"
    assert result["state"] == "needs_verification"
    assert result["repair_delivery_id"] is None
    assert repo.get_delivery(accepted[0])["state"] == "needs_verification"
    assert len(delivery_rows(repo.database_path)) == generations
    assert pages(repo, job)["pending"] == 0
    assert repo.list_dispatchable(job["job_id"]) == ()
    assert receipt(repo, first) == recorded

    offline.rename(library_root)
    assert target(library_root).read_bytes() == PNG_BYTES
    recovered = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)
    assert recovered["outcome"] == "reused", recovered
    assert pages(repo, job)["existing"] == 1
    assert receipt(repo, first) == recorded


def test_a_replaced_path_type_is_never_treated_as_loss(repo, library_root):
    job, hold_id, first = parked_hold(repo, library_root)
    recorded = receipt(repo, first)
    path = target(library_root)
    # A directory in place of the file is not a regular file and not "missing".
    path.unlink()
    path.mkdir()

    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

    assert result["outcome"] == "held", result
    assert result["reason"] == "observation_unknown", result
    assert result["health"]["reason"] == "not_regular_file", result
    assert result["state"] == "needs_verification"
    assert result["repair_delivery_id"] is None
    assert repo.get_delivery(hold_id)["state"] == "needs_verification"
    assert path.is_dir()
    assert receipt(repo, first) == recorded


# ---------------------------------------------------------------------------
# pending repairs: idempotent, releasable, parkable
# ---------------------------------------------------------------------------


def test_a_missing_page_is_already_queued_and_stays_idempotent(repo, library_root):
    job, repair_id, first = queued_repair(repo, library_root)
    recorded = receipt(repo, first)
    before = delivery_rows(repo.database_path)

    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

    assert result["outcome"] == "repair_queued", result
    assert result["reason"] == "repair_already_pending"
    assert result["previous_state"] == "pending"
    assert result["repair_delivery_id"] == repair_id
    assert result["state"] == "pending"
    assert delivery_rows(repo.database_path) == before
    assert repo.list_dispatchable(job["job_id"]) == (repair_id,)
    assert receipt(repo, first) == recorded

    outcome = run(repo, job, repair_id, executor(repo, library_root, Transport(Response())))
    assert outcome.state == "completed", outcome
    assert target(library_root).read_bytes() == PNG_BYTES


def test_a_restored_file_releases_a_queued_repair(repo, library_root):
    job, repair_id, first = queued_repair(repo, library_root)
    recorded = receipt(repo, first)
    target(library_root).write_bytes(PNG_BYTES)

    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

    assert result["outcome"] == "reused", result
    assert result["previous_delivery_id"] == repair_id
    assert result["previous_state"] == "pending"
    assert repo.get_delivery(repair_id)["state"] == "cancelled"
    assert repo.get_delivery(repair_id)["error_code"] == "recovered_hash_match"
    assert repo.list_dispatchable(job["job_id"]) == ()
    assert pages(repo, job)["existing"] == 1
    assert pages(repo, job)["pending"] == 0
    assert receipt(repo, first) == recorded
    assert len(artifact_rows(repo.database_path)) == 1
    assert target(library_root).read_bytes() == PNG_BYTES


def test_a_queued_repair_is_parked_when_a_foreign_file_appears(repo, library_root):
    job, repair_id, first = queued_repair(repo, library_root)
    recorded = receipt(repo, first)
    target(library_root).write_bytes(NOT_AN_IMAGE)

    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

    assert result["outcome"] == "held", result
    assert result["reason"] == "artifact_changed"
    assert result["state"] == "needs_verification"
    row = repo.get_delivery(repair_id)
    assert row["state"] == "needs_verification", row
    assert row["error_code"] == "artifact_changed", row
    assert repo.list_dispatchable(job["job_id"]) == ()
    assert target(library_root).read_bytes() == NOT_AN_IMAGE
    assert receipt(repo, first) == recorded

    again = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)
    assert again["outcome"] == "held"
    assert again["state"] == "needs_verification"
    assert target(library_root).read_bytes() == NOT_AN_IMAGE

    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"]) is True
    assert repo.get_batch(job["batch_id"])["phase"] == "partial"


# ---------------------------------------------------------------------------
# Repeated calls, command ids and an already completed page
# ---------------------------------------------------------------------------


def test_command_ids_replay_and_never_create_a_second_generation(repo, library_root):
    job, hold_id, first = parked_hold(repo, library_root)
    target(library_root).unlink()
    before = delivery_rows(repo.database_path)

    first_result = repo.recover_verification_page(
        job["batch_id"], ARTWORK, 0, command_id="recover-1"
    )
    assert first_result["outcome"] == "repair_queued"
    after = delivery_rows(repo.database_path)
    assert len(after) == len(before) + 1

    replay = repo.recover_verification_page(
        job["batch_id"], ARTWORK, 0, command_id="recover-1"
    )
    assert replay == first_result
    assert delivery_rows(repo.database_path) == after

    again = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)
    assert again["outcome"] == "repair_queued"
    assert again["reason"] == "repair_already_pending"
    assert again["repair_delivery_id"] == first_result["repair_delivery_id"]
    assert delivery_rows(repo.database_path) == after

    other_batch = repo.get_delivery(first)["completed_batch_id"]
    with pytest.raises(CommandConflict):
        repo.recover_verification_page(other_batch, ARTWORK, 0, command_id="recover-1")


def test_an_already_completed_page_reports_completed(repo, library_root):
    job, hold_id, first = parked_hold(repo, library_root)
    target(library_root).write_bytes(PNG_BYTES)
    assert repo.recover_verification_page(job["batch_id"], ARTWORK, 0)["outcome"] == "reused"

    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)
    assert result["outcome"] == "completed", result
    assert result["reason"] == "already_completed"
    assert result["state"] == "completed"
    assert result["artifact_id"] == repo.get_artifact(first)["artifact_id"]
    assert result["repair_delivery_id"] is None
    assert pages(repo, job)["existing"] == 1


# ---------------------------------------------------------------------------
# Scope, bindings and writer hardening
# ---------------------------------------------------------------------------


def test_the_entry_is_scoped_to_a_running_batch(repo, library_root):
    job, hold_id, first = parked_hold(repo, library_root)
    repo.finish_source(job["batch_id"], coverage_complete=True)
    assert repo.settle_batch(job["batch_id"]) is True
    assert repo.get_batch(job["batch_id"])["phase"] == "partial"

    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)
    assert result["outcome"] == "refused", result
    assert result["reason"] == "batch_not_running"
    assert repo.get_delivery(hold_id)["state"] == "needs_verification"

    with pytest.raises(NotFoundError):
        repo.recover_verification_page(job["batch_id"], OTHER_ARTWORK, 0)
    with pytest.raises(NotFoundError):
        repo.recover_verification_page("not-a-batch", ARTWORK, 0)
    with pytest.raises(LibraryError):
        repo.recover_verification_page(job["batch_id"], ARTWORK, -1)


def test_a_repair_beyond_the_work_budget_stays_parked(repo, library_root):
    _, first = first_delivery(repo, library_root)
    recorded = receipt(repo, first)
    target(library_root).write_bytes(SAME_SIZE_PNG)

    job = submit(repo, request_id="second", max_works=1)
    accepted = accept(
        repo,
        job["batch_id"],
        source(
            (candidate(ARTWORK), candidate(OTHER_ARTWORK)),
            page_identity="page-budget",
        ),
    )
    assert len(accepted) == 2, accepted
    assert pages(repo, job)["needs_verification"] == 1
    assert pages(repo, job)["pending"] == 1
    hold_id = next(
        value for value in accepted
        if repo.get_delivery(value)["state"] == "needs_verification"
    )

    target(library_root).unlink()
    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

    assert result["outcome"] == "refused", result
    assert result["reason"] == "work_budget_exhausted"
    assert result["state"] == "needs_verification"
    assert result["repair_delivery_id"] is None
    assert repo.get_delivery(hold_id)["state"] == "needs_verification"
    assert pages(repo, job)["needs_verification"] == 1
    assert pages(repo, job)["pending"] == 1
    assert receipt(repo, first) == recorded


def test_a_redownload_task_never_reuses_another_tasks_artifact(repo, library_root):
    """A redownload batch opts out of cross-task reuse; the reason says so."""
    _, first = first_delivery(repo, library_root)
    recorded = receipt(repo, first)
    assert target(library_root).read_bytes() == PNG_BYTES

    job = submit(repo, request_id="redownload", dedup_policy="redownload")
    accepted = accept(repo, job["batch_id"], source((candidate(),), page_identity="page-rd"))
    assert len(accepted) == 1
    queued = repo.get_delivery(accepted[0])
    assert queued["state"] == "pending", queued

    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

    assert result["outcome"] == "held", result
    assert result["reason"] == "cross_task_reuse_disabled"
    assert result["state"] == "pending"
    assert result["repair_delivery_id"] is None
    assert repo.get_delivery(accepted[0])["state"] == "pending"
    assert repo.list_dispatchable(job["job_id"]) == (accepted[0],)
    assert receipt(repo, first) == recorded
    assert target(library_root).read_bytes() == PNG_BYTES


def test_recovery_without_a_bound_root_fails_closed(tmp_path):
    database = tmp_path / "ledger.sqlite3"
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(database) as repo:
        _, first = first_delivery(repo, root)
        recorded = receipt(repo, first)
        target(root).write_bytes(SAME_SIZE_PNG)
        job, accepted = second_task(repo, root)
    with LibraryRepository(database) as reopened:
        result = reopened.recover_verification_page(job["batch_id"], ARTWORK, 0)
        assert result["outcome"] == "held", result
        assert result["reason"] == "observation_unavailable"
        assert result["state"] == "needs_verification"
        assert reopened.get_delivery(accepted[0])["state"] == "needs_verification"
        assert receipt(reopened, first) == recorded


def test_a_stale_preflight_cannot_reuse_a_page(repo, library_root, monkeypatch):
    job, hold_id, first = parked_hold(repo, library_root)
    recorded = receipt(repo, first)
    target(library_root).write_bytes(PNG_BYTES)
    original = repo._observe_artifact

    def stale(artifact, *, resolver, run_id, quality_key):
        record = original(artifact, resolver=resolver, run_id=run_id, quality_key=quality_key)
        if record is None:
            return None
        return replace(record, checked_at="2020-01-01T00:00:00+00:00")

    monkeypatch.setattr(repo, "_observe_artifact", stale)
    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

    assert result["outcome"] == "refused", result
    assert result["reason"] == "stale_preflight"
    assert repo.get_delivery(hold_id)["state"] == "needs_verification"
    assert pages(repo, job)["needs_verification"] == 1
    assert pages(repo, job)["existing"] == 0
    assert receipt(repo, first) == recorded


def test_the_preflight_never_hashes_inside_a_write_transaction(repo, library_root, monkeypatch):
    job, hold_id, first = parked_hold(repo, library_root)
    target(library_root).write_bytes(PNG_BYTES)
    flag = threading.local()
    observations = []
    original_transaction = LibraryRepository._transaction
    original_observe = LocalObserver.observe

    @contextmanager
    def traced(self, conn):
        previous = getattr(flag, "in_transaction", False)
        flag.in_transaction = True
        try:
            with original_transaction(self, conn):
                yield
        finally:
            flag.in_transaction = previous

    def observe(self, request):
        observations.append(
            (threading.current_thread().name, getattr(flag, "in_transaction", False))
        )
        return original_observe(self, request)

    monkeypatch.setattr(LibraryRepository, "_transaction", traced)
    monkeypatch.setattr(LocalObserver, "observe", observe)

    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

    assert result["outcome"] == "reused", result
    assert observations, "the preflight never observed the recorded file"
    assert all(not inside for _, inside in observations), observations
    assert all(
        not name.startswith("jpgpng-library-writer") for name, _ in observations
    ), observations
    assert repo.get_delivery(hold_id)["state"] == "cancelled"


def _forged_reuse(job, hold_id, artifact_id, health):
    return {
        "batch_id": job["batch_id"],
        "illust_id": ARTWORK,
        "page_index": 0,
        "command_id": None,
        "previous_delivery_id": hold_id,
        "previous_state": "needs_verification",
        "action": "reuse",
        "artifact_id": artifact_id,
        "health": health,
        "draft": {"outcome": "reused", "reason": "hash_match"},
    }


def test_a_reuse_command_without_frozen_health_is_refused(repo, library_root):
    job, hold_id, first = parked_hold(repo, library_root)
    recorded = receipt(repo, first)
    target(library_root).write_bytes(PNG_BYTES)

    forged = _forged_reuse(job, hold_id, recorded[0]["artifact_id"], [])
    result = repo._enqueue("recover_verification_page", forged)

    assert result["outcome"] == "refused", result
    assert result["reason"] == "stale_preflight"
    assert repo.get_delivery(hold_id)["state"] == "needs_verification"
    assert pages(repo, job)["needs_verification"] == 1
    assert pages(repo, job)["existing"] == 0


def test_an_inconsistent_draft_is_rejected_before_any_write(repo, library_root):
    job, hold_id, first = parked_hold(repo, library_root)
    recorded = receipt(repo, first)
    target(library_root).write_bytes(PNG_BYTES)
    forged = _forged_reuse(job, hold_id, recorded[0]["artifact_id"], [])
    forged["draft"] = {"outcome": "held", "reason": "artifact_changed"}

    with pytest.raises(LibraryError):
        repo._enqueue("recover_verification_page", forged)

    assert repo.get_delivery(hold_id)["state"] == "needs_verification"
    assert receipt(repo, first) == recorded


def test_a_frozen_health_record_for_other_bytes_cannot_authorize_reuse(repo, library_root):
    """Even a self-consistent digest cannot replace the recorded artifact."""
    job, hold_id, first = parked_hold(repo, library_root)
    recorded = receipt(repo, first)
    target(library_root).write_bytes(NOT_AN_IMAGE)
    other_sha = hashlib.sha256(NOT_AN_IMAGE).hexdigest()

    artifact = repo.get_artifact(first)
    expectation = ArtifactExpectation.from_artifact_row(
        artifact, path=OUTPUT_REF, root_revision=1
    )
    forged_expectation = replace(
        expectation, size_bytes=len(NOT_AN_IMAGE), sha256=other_sha
    )
    forged_record = ArtifactHealth(
        expectation=forged_expectation,
        policy=ArtifactHealthPolicy(),
        state=LocalState.HASH_MATCH,
        checked_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        reason="full_sha256_match",
        observed_size=len(NOT_AN_IMAGE),
        observed_sha256=other_sha,
    )

    forged = _forged_reuse(
        job, hold_id, recorded[0]["artifact_id"], [forged_record.to_payload()]
    )
    result = repo._enqueue("recover_verification_page", forged)

    assert result["outcome"] == "refused", result
    assert result["reason"] == "stale_preflight"
    assert repo.get_delivery(hold_id)["state"] == "needs_verification"
    assert pages(repo, job)["needs_verification"] == 1
    assert pages(repo, job)["existing"] == 0
    assert target(library_root).read_bytes() == NOT_AN_IMAGE


@pytest.mark.parametrize("removal", ["restored", "deleted", "changed"])
def test_history_is_never_rewritten_by_any_recovery(repo, library_root, removal):
    job, hold_id, first = parked_hold(repo, library_root)
    recorded = receipt(repo, first)
    path = target(library_root)
    if removal == "restored":
        path.write_bytes(PNG_BYTES)
    elif removal == "deleted":
        path.unlink()
    else:
        path.write_bytes(NOT_AN_IMAGE)

    result = repo.recover_verification_page(job["batch_id"], ARTWORK, 0)

    expected = {"restored": "reused", "deleted": "repair_queued", "changed": "held"}
    assert result["outcome"] == expected[removal], result
    current = receipt(repo, first)
    assert current[0] == recorded[0], "the historical artifact row was rewritten"
    assert current[1] == recorded[1], "the historical delivery row was rewritten"
    assert current[2] == recorded[2], "the finished batch counts changed"
    rows = delivery_rows(repo.database_path)
    historical = [row for row in rows if row["delivery_id"] == first]
    assert len(historical) == 1
    assert historical[0]["state"] == "completed"
    assert historical[0]["generation"] == recorded[1]["generation"]
    if removal != "restored":
        assert repo.get_delivery(hold_id)["state"] != "completed"
        assert len(rows) == 2 or (removal == "deleted" and len(rows) == 3)
