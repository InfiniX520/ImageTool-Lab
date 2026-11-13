#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline P3 behaviour tests for the SQL delivery protocol.

Only temporary SQLite databases and synthetic contract objects are used.  No
file is staged, hashed from disk, published or fetched: the repository stores
protocol facts and validates evidence while the filesystem adapter owns bytes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from core.app_paths import AppPaths
from core.library_paths import PathResolver
from core.library_repository import (
    CommandConflict,
    CorruptDataError,
    InvalidStateError,
    LibraryError,
    LibraryRepository,
    NotFoundError,
    OutputConflict,
    SchemaVersionError,
)
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

ROOT = "library/out"
DEVICE = 64768
INODE = 4242
SOURCE = "a" * 64
PREFIX = "b" * 64


# ---------------------------------------------------------------------------
# Synthetic contracts and database helpers
# ---------------------------------------------------------------------------


def _request(
    request_id: str = "req-1",
    *,
    root: str = ROOT,
    all_pages: bool = True,
    original: bool = True,
    budget: BatchBudget | None = None,
    dedup_policy: str = "trusted",
) -> CollectionRequest:
    return CollectionRequest(
        request_id=request_id,
        account_ref="account-test",
        output=OutputSpec(root_ref=PathRef(relative_path=root)),
        quality=QualitySpec(original=original, all_pages=all_pages),
        budget=budget or BatchBudget(max_works=10, max_requests=10, max_artists=5),
        dedup_policy=dedup_policy,
    )


def _candidate(
    illust_id: str,
    page_indices: tuple[int, ...] = (0,),
    *,
    root: str = ROOT,
    page_count: int | None = None,
    decision: str = "download",
    artist_id: str = "artist-1",
) -> CandidateDecision:
    pages = tuple(
        PageSpec(
            page_index=index,
            output_ref=PathRef(relative_path=f"{root}/{illust_id}_p{index}.jpg"),
            url=f"https://i.pximg.net/img-original/{illust_id}_p{index}.jpg",
        )
        for index in page_indices
    )
    if decision == "download" and page_count is None:
        page_count = max(page_indices) + 1
    return CandidateDecision(
        illust_id=illust_id,
        decision=decision,
        artist_id=artist_id,
        artist_name="Artist",
        title="Title",
        page_count=page_count,
        pages=pages,
    )


def _source(
    candidates,
    *,
    page_identity: str = "page-1",
    before: str = "null",
    after: str = '"cursor-1"',
    source_id: str = "follow",
    pass_id: str = "pass-1",
    entity_id: str = "artist-1",
    exhausted: bool = True,
) -> SourcePage:
    return SourcePage(
        source_id=source_id,
        pass_id=pass_id,
        entity_id=entity_id,
        page_identity=page_identity,
        candidates=tuple(candidates),
        cursor_before_json=before,
        cursor_after_json=after,
        exhausted=exhausted,
        actual_sort="date_desc",
        coverage_json='{"seen":1}',
    )


def _start(repo: LibraryRepository, request: CollectionRequest | None = None,
           candidates=None, *, page_kwargs=None):
    request = request or _request()
    submitted = repo.submit(request, "submit-" + request.request_id)
    activated = repo.activate_next()
    assert activated is not None
    assert activated["batch_id"] == submitted["batch_id"]
    page = _source(
        tuple(candidates) if candidates is not None else (_candidate("100"),),
        **(page_kwargs or {}),
    )
    ids = repo.accept_source_page(submitted["batch_id"], page)
    return submitted, page, ids


def _sql(db_path, statement: str, params=()) -> None:
    with closing(sqlite3.connect(str(db_path), timeout=10)) as raw:
        raw.execute(statement, params)
        raw.commit()


def _scalar(db_path, statement: str, params=()):
    with closing(sqlite3.connect(str(db_path), timeout=10)) as raw:
        row = raw.execute(statement, params).fetchone()
    return None if row is None else row[0]


def _event_count(db_path, kind: str) -> int:
    return int(_scalar(db_path, "SELECT COUNT(*) FROM events WHERE kind = ?", (kind,)))


def _event_payload(db_path, kind: str) -> dict:
    with closing(sqlite3.connect(str(db_path), timeout=10)) as raw:
        row = raw.execute(
            "SELECT safe_payload FROM events WHERE kind = ? "
            "ORDER BY event_seq DESC LIMIT 1",
            (kind,),
        ).fetchone()
    assert row is not None
    return json.loads(row[0])


def _temp_rel(delivery_id: str, suffix: str = "a") -> str:
    return f"{ROOT}/.{delivery_id}.{suffix}.part"


def _temp_ref(delivery_id: str, suffix: str = "a") -> dict:
    return {"kind": "project", "root_id": "project",
            "relative_path": _temp_rel(delivery_id, suffix)}


def _checkpoint(
    delivery: dict,
    *,
    verified: int = 1000,
    total: int | None = 1000,
    prefix: str = PREFIX,
    source: str = SOURCE,
    etag: str | None = '"etag-1"',
    media: str | None = "image/png",
    inode: int = INODE,
    temp_rel: str | None = None,
    schema_version: int = 1,
    policy: int = 1,
) -> dict:
    return {
        "schema_version": schema_version,
        "delivery_id": delivery["delivery_id"],
        "temp_ref": {
            "kind": "project",
            "root_id": "project",
            "relative_path": temp_rel or _temp_rel(delivery["delivery_id"]),
        },
        "file_id": {"device": DEVICE, "inode": inode},
        "source_identity": source,
        "strong_etag": etag,
        "expected_total": total,
        "verified_bytes": verified,
        "prefix_sha256": prefix,
        "media_type": media,
        "validation_policy_version": policy,
    }


def _prepare_evidence(
    delivery: dict,
    checkpoint: dict,
    *,
    size: int | None = None,
    sha: str | None = None,
    inode: int | None = None,
    source: str | None = None,
    final_rel: str | None = None,
    temp_ref: dict | None = None,
    verification: str = "structure_checked",
    media: str | None = None,
    width: int = 64,
    height: int = 48,
    frames: int = 1,
    schema_version: int = 1,
    policy: int = 1,
) -> dict:
    final_rel = final_rel or (
        f"{ROOT}/{delivery['illust_id']}_p{delivery['page_index']}.jpg"
    )
    return {
        "schema_version": schema_version,
        "delivery_id": delivery["delivery_id"],
        "temp_ref": dict(temp_ref or checkpoint["temp_ref"]),
        "final_ref": {"kind": "project", "root_id": "project",
                      "relative_path": final_rel},
        "file_id": {
            "device": DEVICE,
            "inode": checkpoint["file_id"]["inode"] if inode is None else inode,
        },
        "source_identity": checkpoint["source_identity"] if source is None else source,
        "size": checkpoint["verified_bytes"] if size is None else size,
        "sha256": checkpoint["prefix_sha256"] if sha is None else sha,
        "media_type": media or (checkpoint["media_type"] or "image/png"),
        "verification": verification,
        "validation_policy_version": policy,
        "width": width,
        "height": height,
        "frames": frames,
    }


def _mutated(checkpoint: dict, **changes) -> dict:
    """Deep-copy a valid checkpoint and apply one targeted change."""
    value = json.loads(json.dumps(checkpoint))
    for key, new_value in changes.items():
        if key == "temp_rel":
            value["temp_ref"]["relative_path"] = new_value
        else:
            value[key] = new_value
    return value


def _claim(repo: LibraryRepository, delivery_id: str, batch_id: str) -> dict:
    row = repo.claim_delivery(delivery_id, batch_id)
    assert row is not None
    assert row["state"] == "transferring"
    return row


def _transfer_to_prepared(
    repo: LibraryRepository,
    delivery_id: str,
    batch_id: str,
    *,
    size: int = 1000,
    prefix: str = PREFIX,
    inode: int = INODE,
    source: str = SOURCE,
    media: str = "image/png",
    etag: str = '"etag-1"',
) -> tuple[dict, dict]:
    delivery = repo.get_delivery(delivery_id)
    assert repo.start_transfer_attempt(delivery_id, batch_id) >= 1
    checkpoint = _checkpoint(
        delivery,
        verified=size,
        total=size,
        prefix=prefix,
        source=source,
        inode=inode,
        media=media,
        etag=etag,
    )
    repo.save_transfer_checkpoint(delivery_id, batch_id, checkpoint)
    repo.begin_validation(delivery_id, batch_id)
    evidence = _prepare_evidence(delivery, checkpoint)
    repo.prepare_delivery(delivery_id, batch_id, evidence)
    return checkpoint, evidence


def _complete(repo: LibraryRepository, delivery_id: str, batch_id: str, **kwargs) -> dict:
    _transfer_to_prepared(repo, delivery_id, batch_id, **kwargs)
    return repo.confirm_delivery(delivery_id)


# ---------------------------------------------------------------------------
# Happy path, ownership and transitions
# ---------------------------------------------------------------------------


def test_full_protocol_persists_one_artifact_and_replays_confirm(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, page, ids = _start(repo)
        (delivery_id,) = ids
        delivery = repo.get_delivery(delivery_id)
        assert delivery["http_attempts"] == 0
        assert delivery["state"] == "pending"
        # P2 stored the source cursor in checkpoint_json; it is not evidence.
        assert delivery["checkpoint_json"] == '"cursor-1"'
        assert delivery["checkpoint"] is None
        assert delivery["prepared"] is None

        _claim(repo, delivery_id, job["batch_id"])
        assert repo.start_transfer_attempt(delivery_id, job["batch_id"]) == 1
        checkpoint = _checkpoint(repo.get_delivery(delivery_id))
        repo.save_transfer_checkpoint(delivery_id, job["batch_id"], checkpoint)
        assert repo.get_delivery(delivery_id)["checkpoint"] == checkpoint
        assert repo.get_delivery(delivery_id)["source_identity"] == SOURCE

        repo.begin_validation(delivery_id, job["batch_id"])
        evidence = _prepare_evidence(repo.get_delivery(delivery_id), checkpoint)
        repo.prepare_delivery(delivery_id, job["batch_id"], evidence)
        prepared_row = repo.get_delivery(delivery_id)
        assert prepared_row["state"] == "prepared"
        assert prepared_row["prepared"] == evidence
        assert prepared_row["prepared_json"] == json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

        artifact = repo.confirm_delivery(delivery_id)
        assert artifact["delivery_id"] == delivery_id
        assert artifact["size"] == 1000
        assert artifact["sha256"] == PREFIX
        assert artifact["media_type"] == "image/png"
        assert artifact["verification"] == "structure_checked"
        assert artifact["output_key"] == delivery["output_key"]
        assert artifact["output_ref"] == delivery["output_ref"]
        assert artifact["quality"]["original"] is True
        assert artifact["legacy_origin"] is None

        completed = repo.get_delivery(delivery_id)
        assert completed["state"] == "completed"
        assert completed["completed_batch_id"] == job["batch_id"]
        assert completed["http_attempts"] == 1
        assert completed["error_code"] is None
        assert repo.get_artifact(delivery_id) == artifact

        for kind in (
            "delivery_claimed",
            "delivery_http_attempt",
            "delivery_checkpoint_saved",
            "delivery_validation_started",
            "delivery_prepared",
            "delivery_completed",
        ):
            assert _event_count(db_path, kind) == 1
        attempt_payload = _event_payload(db_path, "delivery_http_attempt")
        assert attempt_payload == {
            "delivery_id": delivery_id,
            "batch_id": job["batch_id"],
            "http_attempts": 1,
            "limit": 3,
        }

        assert repo.confirm_delivery(delivery_id) == artifact
        assert _event_count(db_path, "delivery_completed") == 1
        assert repo.get_delivery(delivery_id)["http_attempts"] == 1

        results = repo.batch_results(job["batch_id"])
        assert results["pages"]["total"] == 1
        assert results["pages"]["completed"] == 1
        assert results["pages"]["new_completed"] == 1
        assert results["pages"]["existing"] == 0
        assert results["pages"]["pending"] == 0
        assert results["works"]["download"] == 1
        assert results["works"]["completed"] == 1
        assert results["coverage_complete"] is None
        assert results["stop_target"] is None


def test_released_retry_wait_is_immediately_dispatchable(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(
            repo, candidates=(_candidate("100", (0, 1), page_count=2),)
        )
        batch_id = job["batch_id"]
        for delivery_id in ids:
            _claim(repo, delivery_id, batch_id)
            repo.release_delivery(delivery_id, batch_id, state="retry_wait")
            assert repo.get_delivery(delivery_id)["retry_at"] is not None
            assert delivery_id in repo.list_dispatchable(job["job_id"])
        assert repo.budget_status(batch_id)["deferred_works"] == 0
        assert set(repo.list_dispatchable(job["job_id"])) == set(ids)


def test_protocol_transitions_are_ordered_and_attempts_bounded(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(repo)
        delivery_id = ids[0]
        batch_id = job["batch_id"]
        with pytest.raises(InvalidStateError):
            repo.start_transfer_attempt(delivery_id, batch_id)
        with pytest.raises(InvalidStateError):
            repo.begin_validation(delivery_id, batch_id)
        with pytest.raises(InvalidStateError):
            repo.confirm_delivery(delivery_id)
        with pytest.raises(LibraryError):
            repo.start_transfer_attempt(delivery_id, batch_id, limit=0)

        _claim(repo, delivery_id, batch_id)
        valid_checkpoint = _checkpoint(repo.get_delivery(delivery_id))
        with pytest.raises(NotFoundError):
            repo.begin_validation(delivery_id, "not-a-batch")

        assert repo.start_transfer_attempt(delivery_id, batch_id) == 1
        assert repo.start_transfer_attempt(delivery_id, batch_id) == 2
        assert repo.start_transfer_attempt(delivery_id, batch_id) == 3
        with pytest.raises(InvalidStateError):
            repo.start_transfer_attempt(delivery_id, batch_id)
        assert repo.get_delivery(delivery_id)["http_attempts"] == 3
        assert repo.get_delivery(delivery_id)["state"] == "transferring"

        repo.save_transfer_checkpoint(delivery_id, batch_id, valid_checkpoint)
        repo.begin_validation(delivery_id, batch_id)
        repo.begin_validation(delivery_id, batch_id)  # idempotent
        assert _event_count(db_path, "delivery_validation_started") == 1
        # A recovered worker may still own an execution that is validating.
        assert repo.start_transfer_attempt(delivery_id, batch_id, limit=5) == 4
        assert repo.get_delivery(delivery_id)["state"] == "transferring"
        repo.begin_validation(delivery_id, batch_id)
        assert repo.get_delivery(delivery_id)["state"] == "validating"
        with pytest.raises(InvalidStateError):
            repo.save_transfer_checkpoint(delivery_id, batch_id, valid_checkpoint)

        evidence = _prepare_evidence(repo.get_delivery(delivery_id), valid_checkpoint)
        repo.prepare_delivery(delivery_id, batch_id, evidence)
        with pytest.raises(InvalidStateError):
            repo.begin_validation(delivery_id, batch_id)
        with pytest.raises(InvalidStateError):
            repo.release_delivery(delivery_id, batch_id, state="suspended")


def test_ownership_is_enforced_after_resume(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(
            repo, candidates=(_candidate("100", (0, 1), page_count=2),)
        )
        old_batch = job["batch_id"]
        first = ids[0]
        _claim(repo, first, old_batch)
        repo.release_delivery(first, old_batch, state="suspended")
        repo.request_stop(job["job_id"], "pause")
        assert repo.settle_stop(old_batch) is True

        resumed = repo.resume(job["job_id"], "resume-ownership")
        assert repo.activate_next()["batch_id"] == resumed["batch_id"]
        new_batch = resumed["batch_id"]
        _claim(repo, first, new_batch)
        delivery = repo.get_delivery(first)
        checkpoint = _checkpoint(delivery)
        evidence = _prepare_evidence(delivery, checkpoint)

        with pytest.raises(InvalidStateError):
            repo.start_transfer_attempt(first, old_batch)
        with pytest.raises(InvalidStateError):
            repo.save_transfer_checkpoint(first, old_batch, checkpoint)
        with pytest.raises(InvalidStateError):
            repo.begin_validation(first, old_batch)
        with pytest.raises(InvalidStateError):
            repo.prepare_delivery(first, old_batch, evidence)
        with pytest.raises(InvalidStateError):
            repo.set_delivery_issue(
                first, old_batch, state="needs_verification", error_code="other_batch"
            )
        assert repo.release_delivery(first, old_batch, state="failed") is None
        assert repo.get_delivery(first)["state"] == "transferring"
        assert repo.start_transfer_attempt(first, new_batch) == 1


# ---------------------------------------------------------------------------
# Checkpoint validation
# ---------------------------------------------------------------------------


def test_checkpoint_validation_and_monotonicity(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(repo)
        delivery_id = ids[0]
        batch_id = job["batch_id"]
        _claim(repo, delivery_id, batch_id)
        delivery = repo.get_delivery(delivery_id)
        base = _checkpoint(delivery)

        extra = dict(base)
        extra["raw_headers"] = "set-cookie"
        missing = {key: value for key, value in base.items() if key != "media_type"}
        bad_checkpoints = [
            extra,
            missing,
            _mutated(base, schema_version=2),
            _mutated(base, delivery_id="other-delivery"),
            _mutated(base, validation_policy_version=2),
            _mutated(base, temp_rel=f"{ROOT}/sub/.{delivery_id}.a.part"),
            _mutated(base, temp_rel=f"{ROOT}/{delivery['illust_id']}_p0.jpg"),
            _mutated(base, temp_rel=f"{ROOT}/other.part"),
            _mutated(base, temp_rel=f"{ROOT}/.{delivery_id}.tmp"),
            _mutated(base, temp_rel="C:/tmp/escape.part"),
            _mutated(base, temp_rel=f"{ROOT}/../.{delivery_id}.part"),
            _mutated(base, strong_etag='W/"weak"'),
            _mutated(base, strong_etag="unquoted"),
            _mutated(base, strong_etag='"line' + chr(10) + 'feed"'),
            _mutated(base, strong_etag='"' + "a" * 512 + '"'),
            _mutated(base, media_type="image/svg+xml"),
            _mutated(base, media_type="image/jpeg; charset=utf-8"),
            _mutated(base, expected_total=0),
            _mutated(base, verified_bytes=-1),
            _mutated(base, verified_bytes=True),
            _mutated(base, verified_bytes=1001),
            _mutated(base, prefix_sha256="c" * 63),
            _mutated(base, prefix_sha256="Z" * 64),
            _mutated(base, source_identity="g" * 64),
            _mutated(base, file_id={"device": DEVICE, "inode": 0}),
            _mutated(base, file_id={"device": DEVICE, "inode": True}),
            _mutated(base, file_id={"device": DEVICE}),
            _mutated(base, **{"token": "secret"}),
        ]
        for bad in bad_checkpoints:
            with pytest.raises(LibraryError):
                repo.save_transfer_checkpoint(delivery_id, batch_id, bad)
            assert repo.get_delivery(delivery_id)["checkpoint"] is None
        with pytest.raises(LibraryError):
            repo.save_transfer_checkpoint(delivery_id, batch_id, "not-a-dict")

        partial = _checkpoint(delivery, verified=400, total=1000)
        repo.save_transfer_checkpoint(delivery_id, batch_id, partial)
        assert repo.get_delivery(delivery_id)["checkpoint"] == partial

        with pytest.raises(InvalidStateError):
            repo.save_transfer_checkpoint(
                delivery_id, batch_id, _mutated(partial, verified_bytes=300)
            )
        with pytest.raises(InvalidStateError):
            repo.save_transfer_checkpoint(
                delivery_id, batch_id, _mutated(partial, source_identity="d" * 64)
            )
        advanced = _mutated(partial, verified_bytes=500)
        repo.save_transfer_checkpoint(delivery_id, batch_id, advanced)
        assert repo.get_delivery(delivery_id)["checkpoint"]["verified_bytes"] == 500

        replaced = _checkpoint(
            delivery, verified=0, total=None, temp_rel=_temp_rel(delivery_id, "b")
        )
        repo.save_transfer_checkpoint(delivery_id, batch_id, replaced)
        checkpoint = repo.get_delivery(delivery_id)["checkpoint"]
        assert checkpoint["temp_ref"]["relative_path"].endswith(".b.part")
        assert checkpoint["verified_bytes"] == 0
        assert _event_count(db_path, "delivery_checkpoint_saved") == 3

        # Unknown length, weak/absent version headers and binary content are
        # legitimate facts; only unsafe explicit values are rejected.
        unknown = _mutated(
            replaced,
            temp_rel=_temp_rel(delivery_id, "c"),
            strong_etag=None,
            media_type=None,
            expected_total=None,
            verified_bytes=0,
        )
        repo.save_transfer_checkpoint(delivery_id, batch_id, unknown)
        parsed = repo.get_delivery(delivery_id)["checkpoint"]
        assert parsed["strong_etag"] is None
        assert parsed["media_type"] is None
        assert parsed["expected_total"] is None


def test_legacy_source_cursor_is_not_a_transfer_checkpoint(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(
            repo,
            candidates=(_candidate("100", (0, 1), page_count=2),),
            page_kwargs={"after": '{"offset":1}'},
        )
        scalar_ids = repo.accept_source_page(
            job["batch_id"],
            _source(
                (_candidate("200", (0,), page_count=1),),
                page_identity="page-2",
                before='{"offset":1}',
                after='"cursor-2"',
            ),
        )
        for delivery_id in ids:
            delivery = repo.get_delivery(delivery_id)
            assert delivery["checkpoint_json"] == '{"offset":1}'
            assert delivery["checkpoint"] is None
        scalar = repo.get_delivery(scalar_ids[0])
        assert scalar["checkpoint_json"] == '"cursor-2"'
        assert scalar["checkpoint"] is None

        first = ids[0]
        _claim(repo, first, job["batch_id"])
        checkpoint = _checkpoint(repo.get_delivery(first))
        repo.save_transfer_checkpoint(first, job["batch_id"], checkpoint)
        assert repo.get_delivery(first)["checkpoint"] == checkpoint
        # The other legacy row still parses as "no transfer checkpoint".
        assert repo.get_delivery(ids[1])["checkpoint"] is None
        assert repo.get_delivery(scalar_ids[0])["checkpoint"] is None


# ---------------------------------------------------------------------------
# Prepared evidence and confirmation
# ---------------------------------------------------------------------------


def test_prepare_evidence_must_match_checkpoint_and_output(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(repo)
        delivery_id = ids[0]
        batch_id = job["batch_id"]
        _claim(repo, delivery_id, batch_id)
        delivery = repo.get_delivery(delivery_id)
        checkpoint = _checkpoint(delivery, verified=400, total=1000)
        repo.save_transfer_checkpoint(delivery_id, batch_id, checkpoint)
        repo.begin_validation(delivery_id, batch_id)
        good = _prepare_evidence(delivery, checkpoint)

        bad = [
            dict(good, size=1000),
            dict(good, sha256="c" * 64),
            dict(good, temp_ref=_temp_ref(delivery_id, "b")),
            dict(good, file_id={"device": DEVICE, "inode": INODE + 1}),
            dict(good, source_identity="d" * 64),
            dict(good, final_ref={"kind": "project", "root_id": "project",
                                  "relative_path": f"{ROOT}/other_p0.jpg"}),
            dict(good, verification="size_checked"),
            dict(good, media_type="image/svg+xml"),
            dict(good, width=0),
            dict(good, height=-1),
            dict(good, frames=0),
            dict(good, schema_version=2),
            dict(good, validation_policy_version=2),
        ]
        missing = {key: value for key, value in good.items() if key != "width"}
        bad.append(missing)
        bad.append({**good, "url": "https://i.pximg.net/secret"})
        bad.append({**good, "authorization": "Bearer secret"})
        for evidence in bad:
            with pytest.raises(LibraryError):
                repo.prepare_delivery(delivery_id, batch_id, evidence)
            assert repo.get_delivery(delivery_id)["state"] == "validating"
        with pytest.raises(LibraryError):
            repo.prepare_delivery(delivery_id, batch_id, "not-a-dict")

        repo.prepare_delivery(delivery_id, batch_id, good)
        prepared = repo.get_delivery(delivery_id)
        assert prepared["state"] == "prepared"
        assert prepared["prepared"] == good

        # Identical replay is idempotent; different evidence for the same
        # delivery is a conflict, never a silent overwrite.
        repo.prepare_delivery(delivery_id, batch_id, good)
        assert _event_count(db_path, "delivery_prepared") == 1
        conflicting = dict(good, media_type="image/jpeg")
        with pytest.raises(InvalidStateError):
            repo.prepare_delivery(delivery_id, batch_id, conflicting)
        assert repo.get_delivery(delivery_id)["prepared"]["media_type"] == "image/png"


def test_confirm_rolls_back_when_event_insert_fails(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(repo)
        delivery_id = ids[0]
        batch_id = job["batch_id"]
        _claim(repo, delivery_id, batch_id)
        _transfer_to_prepared(repo, delivery_id, batch_id)
        prepared_json = repo.get_delivery(delivery_id)["prepared_json"]
        _sql(
            db_path,
            "CREATE TRIGGER fail_complete_event BEFORE INSERT ON events "
            "WHEN NEW.kind = 'delivery_completed' "
            "BEGIN SELECT RAISE(ABORT, 'simulated event fault'); END",
        )
        with pytest.raises(sqlite3.IntegrityError):
            repo.confirm_delivery(delivery_id)
        assert repo.get_artifact(delivery_id) is None
        assert repo.get_delivery(delivery_id)["state"] == "prepared"
        assert repo.get_delivery(delivery_id)["prepared_json"] == prepared_json
        assert _event_count(db_path, "delivery_completed") == 0
        assert repo.batch_results(batch_id)["pages"]["completed"] == 0
        assert repo.batch_results(batch_id)["works"]["pending"] == 1

        _sql(db_path, "DROP TRIGGER fail_complete_event")
        artifact = repo.confirm_delivery(delivery_id)
        assert artifact["size"] == 1000
        assert repo.get_delivery(delivery_id)["state"] == "completed"
        assert repo.batch_results(batch_id)["pages"]["completed"] == 1


def test_confirm_requires_valid_prepared_evidence(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(
            repo, candidates=(_candidate("100", (0, 1), page_count=2),)
        )
        with pytest.raises(InvalidStateError):
            repo.confirm_delivery(ids[0])
        _claim(repo, ids[0], job["batch_id"])
        with pytest.raises(InvalidStateError):
            repo.confirm_delivery(ids[0])
        repo.release_delivery(ids[0], job["batch_id"], state="suspended")
        repo.release_delivery(ids[0], job["batch_id"], state="suspended")

        _claim(repo, ids[1], job["batch_id"])
        _transfer_to_prepared(repo, ids[1], job["batch_id"])
        _sql(
            db_path,
            "UPDATE deliveries SET prepared_json = '{broken' WHERE delivery_id = ?",
            (ids[1],),
        )
        with pytest.raises(CorruptDataError):
            repo.confirm_delivery(ids[1])
        assert repo.get_artifact(ids[1]) is None
        assert _event_count(db_path, "delivery_completed") == 0

        assert repo.get_artifact("unknown-delivery") is None
        with pytest.raises(LibraryError):
            repo.get_artifact("")


# ---------------------------------------------------------------------------
# Issue states, results accounting and recovery
# ---------------------------------------------------------------------------


def test_issue_states_preserve_evidence_and_refresh_results(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(
            repo, candidates=(_candidate("100", (0, 1), page_count=2),)
        )
        batch_id = job["batch_id"]
        _claim(repo, ids[0], batch_id)
        _complete(repo, ids[0], batch_id)

        _claim(repo, ids[1], batch_id)
        checkpoint, _evidence = _transfer_to_prepared(repo, ids[1], batch_id)
        checkpoint_json = repo.get_delivery(ids[1])["checkpoint_json"]
        prepared_json = repo.get_delivery(ids[1])["prepared_json"]
        repo.set_delivery_issue(
            ids[1], batch_id, state="recovery_needed", error_code="local_file_missing"
        )
        row = repo.get_delivery(ids[1])
        assert row["state"] == "recovery_needed"
        assert row["error_code"] == "local_file_missing"
        assert row["checkpoint_json"] == checkpoint_json
        assert row["prepared_json"] == prepared_json
        results = repo.batch_results(batch_id)
        assert results["pages"]["completed"] == 1
        assert results["pages"]["needs_verification"] == 1
        assert results["works"]["partial"] == 1

        with pytest.raises(LibraryError):
            repo.set_delivery_issue(
                ids[1], batch_id, state="failed", error_code="fake_completed"
            )
        with pytest.raises(LibraryError):
            repo.set_delivery_issue(
                ids[1], batch_id, state="needs_verification", error_code="bad code"
            )

        # Local startup recovery may confirm a prepared delivery directly.
        artifact = repo.confirm_delivery(ids[1])
        assert artifact["sha256"] == PREFIX
        completed = repo.get_delivery(ids[1])
        assert completed["state"] == "completed"
        assert completed["error_code"] is None
        results = repo.batch_results(batch_id)
        assert results["pages"]["completed"] == 2
        assert results["pages"]["new_completed"] == 2
        assert results["works"]["completed"] == 1

        with pytest.raises(InvalidStateError):
            repo.set_delivery_issue(
                ids[1], batch_id, state="needs_verification", error_code="immutable"
            )

        repo.finish_source(batch_id, coverage_complete=True)
        assert repo.settle_batch(batch_id) is True
        assert repo.get_batch(batch_id)["phase"] == "completed"

        other, _page, other_ids = _start(
            repo,
            request=_request("req-2"),
            candidates=(_candidate("200", (0,), page_count=1),),
        )
        _claim(repo, other_ids[0], other["batch_id"])
        repo.set_delivery_issue(
            other_ids[0],
            other["batch_id"],
            state="needs_verification",
            error_code="image_extension_mismatch",
        )
        other_row = repo.get_delivery(other_ids[0])
        assert other_row["state"] == "needs_verification"
        assert other_row["prepared_json"] is None
        assert other_row["checkpoint"] is None
        other_results = repo.batch_results(other["batch_id"])
        assert other_results["pages"]["needs_verification"] == 1
        assert other_results["works"]["needs_verification"] == 1


def test_confirm_rejects_duplicate_artifact_atomically(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(repo)
        delivery_id = ids[0]
        batch_id = job["batch_id"]
        _claim(repo, delivery_id, batch_id)
        _transfer_to_prepared(repo, delivery_id, batch_id)
        delivery = repo.get_delivery(delivery_id)
        _sql(
            db_path,
            "INSERT INTO artifacts("
            "artifact_id, delivery_id, job_id, illust_id, page_index, artist_id, "
            "output_key, output_ref_json, quality_json, media_type, size, sha256, "
            "verification, completed_at, legacy_origin, created_at"
            ") VALUES ('pre-existing', ?, ?, ?, ?, NULL, ?, ?, ?, 'image/png', 10, ?, "
            "'structure_checked', '2026-01-01T00:00:00+00:00', NULL, "
            "'2026-01-01T00:00:00+00:00')",
            (
                delivery_id,
                delivery["job_id"],
                delivery["illust_id"],
                delivery["page_index"],
                delivery["output_key"],
                json.dumps(delivery["output_ref"], sort_keys=True, separators=(",", ":")),
                delivery["quality_json"],
                PREFIX,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            repo.confirm_delivery(delivery_id)
        assert repo.get_delivery(delivery_id)["state"] == "prepared"
        assert repo.get_artifact(delivery_id)["artifact_id"] == "pre-existing"
        assert _event_count(db_path, "delivery_completed") == 0


def test_missing_source_page_link_is_rebuilt_on_resume(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(
            repo, candidates=(_candidate("100", (0, 1), page_count=2),)
        )
        batch_id = job["batch_id"]
        _claim(repo, ids[0], batch_id)
        _complete(repo, ids[0], batch_id)
        _sql(
            db_path,
            "DELETE FROM batch_item_pages WHERE batch_id = ? AND page_index = 1",
            (batch_id,),
        )
        repo.request_stop(job["job_id"], "pause-gap")
        assert repo.settle_stop(batch_id) is True
        resumed = repo.resume(job["job_id"], "resume-gap")
        assert repo.activate_next()["batch_id"] == resumed["batch_id"]
        assert repo.claim_delivery(ids[1], resumed["batch_id"]) is not None
        results = repo.batch_results(resumed["batch_id"])
        assert results["pages"]["total"] == 2
        assert results["pages"]["existing"] == 1
        assert results["pages"]["pending"] == 1
        assert results["works"]["partial"] == 1


def test_startup_prepared_blocks_stop_then_confirms_without_active_batch(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    repo = LibraryRepository(db_path)
    try:
        job, _page, ids = _start(
            repo, candidates=(_candidate("100", (0, 1), page_count=2),)
        )
        batch_id = job["batch_id"]
        _claim(repo, ids[0], batch_id)
        _transfer_to_prepared(repo, ids[0], batch_id)
        _claim(repo, ids[1], batch_id)
        _transfer_to_prepared(repo, ids[1], batch_id, size=20, prefix="e" * 64)
        repo.set_delivery_issue(
            ids[1], batch_id, state="recovery_needed", error_code="local_file_missing"
        )
    finally:
        repo.close()

    with LibraryRepository(db_path) as repo:
        recoverable = repo.list_recoverable()
        assert [item["delivery_id"] for item in recoverable] == [ids[0], ids[1]]
        entry = recoverable[0]
        assert entry["state"] == "prepared"
        assert entry["prepared"]["size"] == 1000
        assert entry["checkpoint"]["schema_version"] == 1
        broken = recoverable[1]
        assert broken["state"] == "recovery_needed"
        assert broken["error_code"] == "local_file_missing"
        assert broken["prepared"]["size"] == 20
        assert repo.list_recoverable(limit=0) == ()
        assert repo.list_recoverable(limit=-5) == ()
        assert len(repo.list_recoverable(limit=1)) == 1
        assert len(repo.list_recoverable(limit=10_000)) == 2

        repo.request_stop(job["job_id"], "pause-prepared")
        assert repo.settle_stop(batch_id) is True
        blocked = repo.get_batch(batch_id)
        assert blocked["phase"] == "blocked"
        assert blocked["stop_target"] == "blocked"
        # The stop was requested first, so its safe reason is retained; the
        # held prepared file still forces "blocked" instead of "paused".
        assert blocked["stop_reason"] == "user_pause"

        artifact = repo.confirm_delivery(ids[0])
        assert artifact["size"] == 1000
        assert repo.get_delivery(ids[0])["state"] == "completed"
        assert repo.get_delivery(ids[1])["state"] == "recovery_needed"
        recovered = repo.confirm_delivery(ids[1])
        assert recovered["size"] == 20
        assert repo.get_delivery(ids[1])["state"] == "completed"
        results = repo.batch_results(batch_id)
        assert results["pages"]["completed"] == 2
        assert results["works"]["completed"] == 1
        assert repo.settle_stop(batch_id) is False
        assert repo.get_batch(batch_id)["phase"] == "blocked"


# ---------------------------------------------------------------------------
# Multi-page accounting, resume and retry
# ---------------------------------------------------------------------------


def test_multi_page_retry_only_failed_and_frozen_associations(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, page, ids = _start(
            repo, candidates=(_candidate("100", (0, 1, 2), page_count=3),)
        )
        batch_id = job["batch_id"]
        assert repo.accept_source_page(batch_id, page) == ids
        assert _scalar(
            db_path, "SELECT COUNT(*) FROM batch_item_pages WHERE batch_id = ?", (batch_id,)
        ) == 3

        _claim(repo, ids[0], batch_id)
        _complete(repo, ids[0], batch_id)
        _claim(repo, ids[1], batch_id)
        _complete(repo, ids[1], batch_id)
        _claim(repo, ids[2], batch_id)
        assert repo.start_transfer_attempt(ids[2], batch_id) == 1
        assert repo.start_transfer_attempt(ids[2], batch_id) == 2
        assert repo.start_transfer_attempt(ids[2], batch_id) == 3
        with pytest.raises(InvalidStateError):
            repo.start_transfer_attempt(ids[2], batch_id)
        repo.release_delivery(
            ids[2], batch_id, state="failed", error_code="simulated_transfer"
        )

        results = repo.batch_results(batch_id)
        assert results["pages"] == {
            "total": 3,
            "completed": 2,
            "new_completed": 2,
            "existing": 0,
            "failed": 1,
            "needs_verification": 0,
            "pending": 0,
            "cancelled": 0,
        }
        assert results["works"]["download"] == 1
        assert results["works"]["partial"] == 1
        assert results["works"]["completed"] == 0

        repo.request_stop(job["job_id"], "pause")
        assert repo.settle_stop(batch_id) is True
        resumed = repo.resume(job["job_id"], "resume-retry", retry_failed=True)
        new_batch = resumed["batch_id"]
        failed_delivery = repo.get_delivery(ids[2])
        assert failed_delivery["state"] == "suspended"
        assert failed_delivery["http_attempts"] == 0
        assert failed_delivery["error_code"] is None
        reset_event = _event_payload(db_path, "deliveries_retry_reset")
        assert reset_event["count"] == 1
        assert reset_event["deliveries"] == [
            {
                "delivery_id": ids[2],
                "illust_id": "100",
                "page_index": 2,
                "previous_http_attempts": 3,
            }
        ]

        assert repo.activate_next()["batch_id"] == new_batch
        claimed = _claim(repo, ids[2], new_batch)
        assert claimed["attempts"] == 2
        assert claimed["http_attempts"] == 0
        assert repo.budget_status(new_batch) == {
            "used_works": 1,
            "max_works": 10,
            "deferred_works": 0,
        }

        admitted = repo.batch_results(new_batch)
        assert admitted["pages"]["total"] == 3
        assert admitted["pages"]["completed"] == 2
        assert admitted["pages"]["existing"] == 2
        assert admitted["pages"]["new_completed"] == 0
        assert admitted["pages"]["pending"] == 1
        assert admitted["works"]["partial"] == 1

        _complete(repo, ids[2], new_batch)
        results = repo.batch_results(new_batch)
        assert results["pages"]["completed"] == 3
        assert results["pages"]["new_completed"] == 1
        assert results["pages"]["existing"] == 2
        assert results["works"]["completed"] == 1
        assert repo.get_delivery(ids[2])["completed_batch_id"] == new_batch
        assert repo.get_delivery(ids[0])["completed_batch_id"] == batch_id

        historical = repo.batch_results(batch_id)
        assert historical["pages"]["total"] == 3
        assert historical["pages"]["completed"] == 3
        assert historical["pages"]["new_completed"] == 2
        assert historical["pages"]["existing"] == 1
        assert historical["works"]["completed"] == 1
        assert repo.get_batch(batch_id)["phase"] == "paused"

        repo.finish_source(new_batch, coverage_complete=True)
        assert repo.settle_batch(new_batch) is True
        settled = repo.get_batch(new_batch)
        assert settled["phase"] == "completed"
        assert settled["coverage_complete"] is True
        assert settled["active"] is False


def test_resume_retry_failed_is_idempotent_and_excludes_success(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(
            repo,
            candidates=(
                _candidate("100", (0,), page_count=1),
                _candidate("101", (0,), page_count=1),
                _candidate("102", (0,), page_count=1),
            ),
        )
        batch_id = job["batch_id"]
        _claim(repo, ids[0], batch_id)
        _complete(repo, ids[0], batch_id)

        _claim(repo, ids[1], batch_id)
        assert repo.start_transfer_attempt(ids[1], batch_id) == 1
        assert repo.start_transfer_attempt(ids[1], batch_id) == 2
        repo.release_delivery(ids[1], batch_id, state="failed", error_code="http_transient")

        _claim(repo, ids[2], batch_id)
        _transfer_to_prepared(repo, ids[2], batch_id)
        prepared_json = repo.get_delivery(ids[2])["prepared_json"]

        repo.request_stop(job["job_id"], "pause")
        assert repo.settle_stop(batch_id) is True
        assert repo.get_batch(batch_id)["phase"] == "blocked"

        resumed = repo.resume(job["job_id"], "resume-retry", retry_failed=True)
        new_batch = resumed["batch_id"]
        assert repo.get_delivery(ids[0])["state"] == "completed"
        assert repo.get_delivery(ids[0])["completed_batch_id"] == batch_id
        assert repo.get_delivery(ids[1])["state"] == "suspended"
        assert repo.get_delivery(ids[1])["http_attempts"] == 0
        assert repo.get_delivery(ids[2])["state"] == "prepared"
        assert repo.get_delivery(ids[2])["prepared_json"] == prepared_json
        assert _event_count(db_path, "deliveries_retry_reset") == 1

        assert repo.resume(job["job_id"], "resume-retry", retry_failed=True) == resumed
        assert _event_count(db_path, "deliveries_retry_reset") == 1
        with pytest.raises(CommandConflict):
            repo.resume(job["job_id"], "resume-retry", retry_failed=False)
        with pytest.raises(CommandConflict):
            repo.resume(job["job_id"], "resume-retry", BatchBudget(max_works=2),
                        retry_failed=True)

        # A second explicit retry with a fresh command id replays the queued
        # batch without a second reset (there are no failed deliveries left).
        again = repo.resume(job["job_id"], "resume-retry-2", retry_failed=True)
        assert again["batch_id"] == new_batch
        assert _event_count(db_path, "deliveries_retry_reset") == 1
        command_payload = _event_payload(db_path, "command_resume")
        assert command_payload["retry_failed"] is True
        assert command_payload["result"] == {"job_id": job["job_id"], "batch_id": new_batch}


def test_all_pages_flag_is_not_page_byte_quality(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        first, _page, first_ids = _start(repo, _request("req-1", all_pages=True))
        _claim(repo, first_ids[0], first["batch_id"])
        payload = b"x" * 1000
        digest = hashlib.sha256(payload).hexdigest()
        _complete(repo, first_ids[0], first["batch_id"], size=len(payload), prefix=digest)
        target = tmp_path / ROOT / "100_p0.jpg"
        target.parent.mkdir(parents=True)
        target.write_bytes(payload)
        # Current health needs the same root owner that published the file.
        repo.bind_path_resolver(PathResolver(AppPaths(tmp_path)))
        repo.finish_source(first["batch_id"], coverage_complete=True)
        assert repo.settle_batch(first["batch_id"]) is True

        second, _page, second_ids = _start(
            repo,
            _request("req-2", all_pages=False),
            candidates=(_candidate("100", (0,), page_count=2),),
        )
        # all_pages is a work-selection option, not a page-byte property.
        assert second_ids == ()
        assert _scalar(
            db_path, "SELECT COUNT(*) FROM deliveries WHERE job_id = ?",
            (second["job_id"],),
        ) == 0
        assert _scalar(db_path, "SELECT COUNT(*) FROM artifacts") == 1
        results = repo.batch_results(second["batch_id"])
        assert results["pages"]["total"] == 1
        assert results["pages"]["completed"] == 1
        assert results["pages"]["existing"] == 1
        assert results["pages"]["new_completed"] == 0
        assert results["works"]["completed"] == 1
        repo.finish_source(second["batch_id"], coverage_complete=True)
        assert repo.settle_batch(second["batch_id"]) is True

        third, _page, third_ids = _start(
            repo,
            _request("req-3", all_pages=True, original=False),
            candidates=(_candidate("100", (0,), page_count=1),),
        )
        assert len(third_ids) == 1
        assert third_ids[0] != first_ids[0]
        assert repo.get_delivery(third_ids[0])["state"] == "pending"
        pending = repo.batch_results(third["batch_id"])
        assert pending["pages"]["pending"] == 1
        assert pending["pages"]["existing"] == 0


def test_redownload_skips_artifacts_and_active_owner_conflicts(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        first, _page, first_ids = _start(repo, _request("req-1"))
        _claim(repo, first_ids[0], first["batch_id"])
        _complete(repo, first_ids[0], first["batch_id"])
        repo.finish_source(first["batch_id"], coverage_complete=True)
        assert repo.settle_batch(first["batch_id"]) is True

        second, _page, second_ids = _start(
            repo, _request("req-2", dedup_policy="redownload")
        )
        assert len(second_ids) == 1
        assert second_ids[0] != first_ids[0]
        assert repo.get_delivery(second_ids[0])["state"] == "pending"
        assert _scalar(db_path, "SELECT COUNT(*) FROM artifacts") == 1
        assert repo.batch_results(second["batch_id"])["pages"]["pending"] == 1

        # The unfinished redownload delivery still reserves its output path.
        repo.request_stop(second["job_id"], "pause-redownload")
        assert repo.settle_stop(second["batch_id"]) is True
        assert repo.get_delivery(second_ids[0])["state"] == "suspended"
        third = repo.submit(
            _request("req-3", dedup_policy="redownload"), "submit-req-3"
        )
        assert repo.activate_next()["batch_id"] == third["batch_id"]
        # redownload ignores the completed artifact, and the suspended owner
        # still reserves the live output path.
        with pytest.raises(OutputConflict):
            repo.accept_source_page(
                third["batch_id"],
                _source((_candidate("100"),), page_identity="page-2"),
            )


def test_unverified_legacy_artifact_is_never_trusted(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        first, _page, first_ids = _start(repo, _request("req-1"))
        _claim(repo, first_ids[0], first["batch_id"])
        _complete(repo, first_ids[0], first["batch_id"])
        repo.finish_source(first["batch_id"], coverage_complete=True)
        assert repo.settle_batch(first["batch_id"]) is True
        _sql(
            db_path,
            "UPDATE artifacts SET verification = 'legacy_unknown', size = NULL "
            "WHERE delivery_id = ?",
            (first_ids[0],),
        )
        # The tampered artifact is no longer usable evidence, so job 2 must
        # fetch its own bytes instead of reusing the file name.
        assert repo.batch_results(first["batch_id"])["pages"]["pending"] == 1

        second, _page, second_ids = _start(repo, _request("req-2"))
        assert len(second_ids) == 1
        assert second_ids[0] != first_ids[0]
        assert repo.get_delivery(second_ids[0])["state"] == "pending"
        assert repo.batch_results(second["batch_id"])["pages"]["pending"] == 1


def test_active_output_reserved_by_needs_verification(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        first, _page, first_ids = _start(repo, _request("req-1"))
        _claim(repo, first_ids[0], first["batch_id"])
        repo.set_delivery_issue(
            first_ids[0],
            first["batch_id"],
            state="needs_verification",
            error_code="image_extension_mismatch",
        )
        repo.request_stop(first["job_id"], "pause")
        assert repo.settle_stop(first["batch_id"]) is True
        assert repo.get_delivery(first_ids[0])["state"] == "needs_verification"

        second = repo.submit(_request("req-2"), "submit-req-2")
        assert repo.activate_next()["batch_id"] == second["batch_id"]
        with pytest.raises(OutputConflict):
            repo.accept_source_page(
                second["batch_id"],
                _source((_candidate("100"),), page_identity="page-2"),
            )
        assert repo.list_dispatchable(second["job_id"]) == ()
        snapshot = repo.snapshot(second["job_id"])
        assert snapshot["counts"]["works"]["total"] == 0
        assert snapshot["counts"]["pages"]["total"] == 0


def test_active_output_reserved_by_prepared_delivery(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        first, _page, first_ids = _start(repo, _request("req-1"))
        _claim(repo, first_ids[0], first["batch_id"])
        _transfer_to_prepared(repo, first_ids[0], first["batch_id"])
        repo.request_stop(first["job_id"], "pause")
        assert repo.settle_stop(first["batch_id"]) is True
        assert repo.get_batch(first["batch_id"])["phase"] == "blocked"
        assert repo.get_delivery(first_ids[0])["state"] == "prepared"

        second = repo.submit(_request("req-2"), "submit-req-2")
        assert repo.activate_next()["batch_id"] == second["batch_id"]
        with pytest.raises(OutputConflict):
            repo.accept_source_page(
                second["batch_id"],
                _source((_candidate("100"),), page_identity="page-2"),
            )


# ---------------------------------------------------------------------------
# Blocking, settling and truthful coverage
# ---------------------------------------------------------------------------


def test_block_stop_while_busy_and_cancel_priority(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(
            repo, candidates=(_candidate("100", (0, 1), page_count=2),)
        )
        batch_id = job["batch_id"]
        _claim(repo, ids[0], batch_id)
        with pytest.raises(LibraryError):
            repo.request_block(batch_id, "Bad Reason")
        repo.request_block(batch_id, "auth_required")
        blocked = repo.get_batch(batch_id)
        assert blocked["phase"] == "pausing"
        assert blocked["stop_target"] == "blocked"
        assert blocked["stop_reason"] == "auth_required"
        assert _event_count(db_path, "batch_block_requested") == 1

        assert repo.claim_delivery(ids[1], batch_id) is None
        assert repo.start_transfer_attempt(ids[0], batch_id) == 1
        assert repo.settle_stop(batch_id) is False
        repo.release_delivery(ids[0], batch_id, state="suspended")
        assert repo.settle_stop(batch_id) is True
        final = repo.get_batch(batch_id)
        assert final["phase"] == "blocked"
        assert final["stop_target"] == "blocked"
        assert repo.get_delivery(ids[0])["state"] == "suspended"
        assert _event_count(db_path, "batch_blocked") == 1
        with pytest.raises(NotFoundError):
            repo.request_block("unknown-batch", "auth_required")

        cancel_job, _page, cancel_ids = _start(
            repo,
            request=_request("req-2"),
            candidates=(_candidate("200"),),
        )
        cancel_batch = cancel_job["batch_id"]
        _claim(repo, cancel_ids[0], cancel_batch)
        assert repo.request_stop(
            cancel_job["job_id"], "cancel-1", cancel=True
        )["phase"] == "cancelling"
        repo.request_block(cancel_batch, "record_error")
        still_cancel = repo.get_batch(cancel_batch)
        assert still_cancel["phase"] == "cancelling"
        assert still_cancel["stop_target"] == "cancelled"
        repo.release_delivery(cancel_ids[0], cancel_batch, state="suspended")
        assert repo.settle_stop(cancel_batch) is True
        assert repo.get_batch(cancel_batch)["phase"] == "cancelled"
        assert repo.get_batch(cancel_batch)["stop_target"] == "cancelled"

        done_job = repo.submit(_request("req-3"), "submit-req-3")
        repo.activate_next()
        # A block request on a live batch stops new claims via the phase.
        assert repo.request_block(done_job["batch_id"], "noop") is None
        assert repo.get_batch(done_job["batch_id"])["phase"] == "pausing"
        # A terminal batch is never revived by a late block request.
        assert repo.request_block(cancel_batch, "noop") is None
        assert repo.get_batch(cancel_batch)["phase"] == "cancelled"


def test_settling_drains_dispatch_and_completes_truthfully(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(
            repo, candidates=(_candidate("100", (0, 1), page_count=2),)
        )
        batch_id = job["batch_id"]
        repo.finish_source(batch_id, coverage_complete=True)
        settling = repo.get_batch(batch_id)
        assert settling["phase"] == "settling"
        assert settling["coverage_complete"] is True
        assert settling["active"] is True
        repo.finish_source(batch_id, coverage_complete=True)
        assert _event_count(db_path, "source_finished") == 1
        with pytest.raises(InvalidStateError):
            repo.finish_source(batch_id, coverage_complete=False)
        with pytest.raises(InvalidStateError):
            repo.accept_source_page(
                batch_id,
                _source(
                    (_candidate("300"),),
                    page_identity="late",
                    before='"cursor-1"',
                ),
            )

        assert repo.list_dispatchable(job["job_id"]) == tuple(ids)
        assert repo.settle_batch(batch_id) is False
        _claim(repo, ids[0], batch_id)
        _complete(repo, ids[0], batch_id)
        assert repo.settle_batch(batch_id) is False
        _claim(repo, ids[1], batch_id)
        _complete(repo, ids[1], batch_id)
        assert repo.settle_batch(batch_id) is True
        assert repo.settle_batch(batch_id) is False
        settled = repo.get_batch(batch_id)
        assert settled["phase"] == "completed"
        assert settled["active"] is False
        assert settled["stop_target"] is None
        assert settled["ended_at"] is not None
        assert repo.snapshot(job["job_id"])["job_state"] == "completed"
        results = repo.batch_results(batch_id)
        assert results["pages"]["new_completed"] == 2
        assert results["coverage_complete"] is True


def test_incomplete_coverage_is_paused_not_completed(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(repo)
        batch_id = job["batch_id"]
        repo.finish_source(batch_id, coverage_complete=False)
        _claim(repo, ids[0], batch_id)
        _complete(repo, ids[0], batch_id)
        assert repo.settle_batch(batch_id) is True
        settled = repo.get_batch(batch_id)
        assert settled["phase"] == "paused"
        assert settled["stop_target"] == "paused"
        assert settled["coverage_complete"] is False
        payload = _event_payload(db_path, "batch_settled")
        assert payload["decision"] == "paused"
        assert payload["coverage_complete"] is False


def test_deferred_work_budget_never_completes(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(
            repo,
            _request("req-1"),
            candidates=(
                _candidate("100", (0,), page_count=1),
                _candidate("101", (0,), page_count=1),
            ),
        )
        old_batch = job["batch_id"]
        repo.request_stop(job["job_id"], "pause-old")
        assert repo.settle_stop(old_batch) is True
        resumed = repo.resume(
            job["job_id"],
            "resume-small",
            BatchBudget(max_works=1, max_requests=2, max_artists=1),
        )
        new_batch = resumed["batch_id"]
        assert repo.activate_next()["batch_id"] == new_batch
        # ids are ordered by candidate: 100 first, then 101.
        work_one, work_two = ids
        _claim(repo, work_one, new_batch)
        _complete(repo, work_one, new_batch)
        assert repo.budget_status(new_batch) == {
            "used_works": 1,
            "max_works": 1,
            "deferred_works": 1,
        }
        assert repo.get_delivery(work_two)["state"] == "suspended"

        repo.finish_source(new_batch, coverage_complete=True)
        assert repo.settle_batch(new_batch) is True
        settled = repo.get_batch(new_batch)
        assert settled["phase"] == "paused"
        assert settled["coverage_complete"] is True
        payload = _event_payload(db_path, "batch_settled")
        assert payload["decision"] == "paused"
        assert payload["deferred_works"] == 1
        results = repo.batch_results(new_batch)
        assert results["works"]["total"] == 1
        assert results["pages"]["completed"] == 1


def test_settle_partial_waits_for_every_dispatchable_page(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(
            repo, candidates=(_candidate("100", (0, 1), page_count=2),)
        )
        batch_id = job["batch_id"]
        repo.finish_source(batch_id, coverage_complete=True)
        assert repo.settle_batch(batch_id) is False
        _claim(repo, ids[0], batch_id)
        repo.release_delivery(ids[0], batch_id, state="failed", error_code="http_transient")
        assert repo.settle_batch(batch_id) is False
        _claim(repo, ids[1], batch_id)
        repo.release_delivery(ids[1], batch_id, state="failed", error_code="http_transient")
        assert repo.settle_batch(batch_id) is True
        settled = repo.get_batch(batch_id)
        assert settled["phase"] == "partial"
        assert settled["stop_target"] is None
        results = repo.batch_results(batch_id)
        assert results["pages"]["failed"] == 2
        assert results["works"]["failed"] == 1
        assert repo.get_delivery(ids[0])["state"] == "failed"


def test_snapshot_keeps_legacy_counts_and_adds_results(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    with LibraryRepository(db_path) as repo:
        job, _page, ids = _start(
            repo, candidates=(_candidate("100", (0, 1), page_count=2),)
        )
        snapshot = repo.snapshot(job["job_id"])
        assert snapshot["counts"]["works"]["total"] == 1
        assert snapshot["counts"]["works"]["download"] == 1
        assert snapshot["counts"]["pages"]["total"] == 2
        assert snapshot["counts"]["pages"]["pending"] == 2
        results = snapshot["results"]
        assert results["batch_id"] == job["batch_id"]
        assert results["pages"]["pending"] == 2
        assert results["works"]["download"] == 1
        assert results["works"]["pending"] == 1
        assert results["coverage_complete"] is None
        assert results["stop_target"] is None

        _claim(repo, ids[0], job["batch_id"])
        _complete(repo, ids[0], job["batch_id"])
        snapshot = repo.snapshot(job["job_id"])
        assert snapshot["counts"]["pages"]["completed"] == 1
        assert snapshot["results"]["pages"]["completed"] == 1
        assert snapshot["results"]["pages"]["new_completed"] == 1

        with pytest.raises(NotFoundError):
            repo.batch_results("unknown-batch")
        with pytest.raises(NotFoundError):
            repo.snapshot("unknown-job")


# ---------------------------------------------------------------------------
# Schema safety
# ---------------------------------------------------------------------------


def _legacy_schema(
    *, keep_coverage: bool, keep_delivery_ddl: bool
) -> tuple[str, ...]:
    """Rebuild the pre-P3 DDL that a P2-era trial database would contain."""
    coverage_block = (
        "        coverage_complete INTEGER CHECK (\n"
        "            coverage_complete IS NULL OR coverage_complete IN (0, 1)\n"
        "        ),\n"
    )
    attempt_line = (
        "        http_attempts INTEGER NOT NULL DEFAULT 0 CHECK (http_attempts >= 0),\n"
    )
    statements: list[str] = []
    for statement in _library_ddl():
        if "batch_item_pages" in statement:
            continue
        if attempt_line in statement:
            statement = statement.replace(attempt_line, "")
        if not keep_coverage and coverage_block in statement:
            statement = statement.replace(coverage_block, "")
        statements.append(statement)
    if keep_delivery_ddl:
        statements.extend(_delivery_ddl())
    return tuple(statements)


def _library_ddl() -> tuple[str, ...]:
    from core import library_repository as module

    return tuple(module._DDL)


def _delivery_ddl() -> tuple[str, ...]:
    from core import delivery_repository as module

    return tuple(module.DELIVERY_DDL)


def _create_legacy_database(
    path, *, keep_coverage: bool, keep_delivery_ddl: bool
) -> None:
    with closing(sqlite3.connect(str(path))) as raw:
        for statement in _legacy_schema(
            keep_coverage=keep_coverage, keep_delivery_ddl=keep_delivery_ddl
        ):
            raw.execute(statement)
        raw.execute(
            'INSERT INTO schema_meta("key", "value") VALUES (?, ?)',
            ("format", "jpgpng-library"),
        )
        raw.execute(
            'INSERT INTO schema_meta("key", "value") VALUES (?, ?)',
            ("schema_version", "1"),
        )
        raw.commit()


def test_old_p2_schema_is_refused_without_writes(tmp_path):
    incomplete_db = tmp_path / "p2-trial.sqlite3"
    _create_legacy_database(
        incomplete_db, keep_coverage=False, keep_delivery_ddl=False
    )
    before = hashlib.sha256(incomplete_db.read_bytes()).hexdigest()
    with pytest.raises(SchemaVersionError, match="batch_item_pages"):
        LibraryRepository(incomplete_db)
    assert hashlib.sha256(incomplete_db.read_bytes()).hexdigest() == before
    assert not (tmp_path / "p2-trial.sqlite3-wal").exists()

    upgrade_db = tmp_path / "p2-upgrade.sqlite3"
    _create_legacy_database(
        upgrade_db, keep_coverage=True, keep_delivery_ddl=True
    )
    before = hashlib.sha256(upgrade_db.read_bytes()).hexdigest()
    with pytest.raises(SchemaVersionError, match="deliveries"):
        LibraryRepository(upgrade_db)
    assert hashlib.sha256(upgrade_db.read_bytes()).hexdigest() == before
    assert not (tmp_path / "p2-upgrade.sqlite3-wal").exists()


def test_unrelated_existing_database_is_refused(tmp_path):
    path = tmp_path / "production.sqlite3"
    with closing(sqlite3.connect(str(path))) as raw:
        raw.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, path TEXT)")
        raw.execute("INSERT INTO records(path) VALUES ('old')")
        raw.commit()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(SchemaVersionError):
        LibraryRepository(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
