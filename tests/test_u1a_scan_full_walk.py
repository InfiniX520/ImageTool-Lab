"""Contract v6 section 3.2: one honest answer for a whole-library walk.

The engine already has its own tests; every check here is about the *walk*.
What must hold is that the aggregation cannot promote one page into the
library, cannot let a green last page overwrite a red first one, cannot turn a
bare cursor into "the whole library is fine", and cannot lose a row when a
budget or a cancellation stops it in the middle.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import core.library_audit as audit
import core.library_scan as scan
import test_u1a_library_audit as h
import test_u1a_recovery as recovery
from core.app_paths import AppPaths
from core.library_audit import ScanBudget
from core.library_paths import PathResolver
from core.library_repository import LibraryRepository
from core.output_locations import LocationError, OutputLocations, load_output_roots
from core.task_contracts import ContractError, PathRef

FROZEN_KEYS = {
    "question",
    "scope",
    "coverage",
    "pages_examined",
    "library_rows",
    "examined",
    "stats",
    "findings",
    "unknown_reasons",
    "next_cursor",
    "resume",
    "exit_code",
    "ledger",
    "mapping",
    "roots",
    "snapshot",
}
FROZEN_STATS_KEYS = {
    "examined",
    "considered",
    "matched",
    "missing",
    "changed",
    "unreadable",
    "root_unavailable",
    "changed_during_check",
    "unknown",
    "excluded",
    "unknown_reasons",
    "bytes_hashed",
    "seconds",
    "stopped_at",
    "budget_exhausted_reason",
    "library_rows",
    "pages_examined",
    "findings",
}


# ---------------------------------------------------------------------------
# Fixtures: a real ledger with synthetic published files
# ---------------------------------------------------------------------------


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        repo.bind_path_resolver(PathResolver(AppPaths(root)))
        yield repo, root


def source_of(repo):
    return (repo.database_path, repo._path_resolver_snapshot())


def walk(repo, **kwargs):
    return scan.scan_library_report(source_of(repo), **kwargs)


def ordered(repo):
    """Every artifact in the order a scan walks them."""
    return recovery.query(
        repo.database_path,
        "SELECT * FROM artifacts ORDER BY completed_at ASC, artifact_id ASC",
    )


def drop_file(root, row):
    path = h.target_path(root, row)
    path.unlink()
    return path


def mapping_project(tmp_path, roots, *, payload=None, name="project"):
    project = tmp_path / name
    (project / "config").mkdir(parents=True)
    if payload is None:
        payload = json.dumps({"version": 1, "roots": {k: str(v) for k, v in roots.items()}})
    (project / "config" / "output_roots_v1.json").write_text(payload, encoding="utf-8")
    return project


def external_row(repo, root, outside, *, root_id="folder_test", name="p.jpg"):
    """One published page whose record names an external root instead."""
    body = outside / name
    body.write_bytes(b"external-bytes-" + name.encode("ascii"))
    _, ids = h.publish(repo, root, "777", request_id="external")
    row = h.artifact_row(repo, ids[0])
    ref = PathRef(kind="external", root_id=root_id, relative_path=name)
    h.sql(
        repo,
        "UPDATE artifacts SET output_ref_json = ?, output_key = ?, size = ?, sha256 = ? "
        "WHERE artifact_id = ?",
        (
            json.dumps({"kind": "external", "root_id": root_id, "relative_path": name}),
            ref.target_key,
            body.stat().st_size,
            hashlib.sha256(body.read_bytes()).hexdigest(),
            row["artifact_id"],
        ),
    )
    return row, body.read_bytes()


# ---------------------------------------------------------------------------
# A full walk: every row, once, in one answer
# ---------------------------------------------------------------------------


def test_a_full_walk_of_two_hundred_and_one_rows_visits_every_row_once(library):
    repo, root = library
    h.clone_artifacts(repo, root, 200)
    report = walk(repo)
    assert report["exit_code"] == 0, report
    assert report["scope"] == "full_walk"
    assert report["coverage"] == "complete"
    assert report["pages_examined"] == 2
    assert report["library_rows"] == 201
    assert report["stats"]["examined"] == 201
    assert report["stats"]["matched"] == 201
    assert report["findings"] == []
    assert report["resume"] == {
        "state": "not_needed",
        "complete_prefix": True,
        "exit_code": 0,
    }
    assert len({row["artifact_id"] for row in report["rows"]}) == 201
    assert report["snapshot"] == "one_short_read_transaction_per_page"


def test_a_full_walk_of_four_hundred_and_one_rows_uses_three_pages(library):
    repo, root = library
    h.clone_artifacts(repo, root, 400)
    report = walk(repo)
    assert report["exit_code"] == 0, report
    assert report["pages_examined"] == 3
    assert report["stats"]["examined"] == 401
    assert report["library_rows"] == 401
    assert report["coverage"] == "complete"


def test_the_report_carries_the_frozen_fields(library):
    repo, root = library
    h.clone_artifacts(repo, root, 2)
    report = walk(repo)
    assert set(report) >= FROZEN_KEYS
    assert set(report["stats"]) >= FROZEN_STATS_KEYS
    assert isinstance(report["next_cursor"], list) and len(report["next_cursor"]) == 2
    assert report["ledger"]["state"] == "ok"
    assert report["stats"]["pages_examined"] == report["pages_examined"]
    assert "快照" in report["note"]


# ---------------------------------------------------------------------------
# Severity: an earlier page is never overwritten by a later green one
# ---------------------------------------------------------------------------


def test_a_missing_file_on_the_first_page_keeps_exit_code_two(library):
    repo, root = library
    h.clone_artifacts(repo, root, 200)
    drop_file(root, ordered(repo)[0])
    report = walk(repo)
    assert report["exit_code"] == 2, report
    assert report["coverage"] == "complete"
    assert report["stats"]["examined"] == 201
    assert [row["verdict"] for row in report["findings"]] == ["missing"]
    assert report["stats"]["matched"] == 200


def test_a_missing_file_on_a_later_page_is_not_overwritten_by_a_green_tail(library):
    repo, root = library
    h.clone_artifacts(repo, root, 400)
    drop_file(root, ordered(repo)[250])
    report = walk(repo)
    assert report["exit_code"] == 2, report
    assert report["pages_examined"] == 3
    assert report["stats"]["examined"] == 401
    assert [row["verdict"] for row in report["findings"]] == ["missing"]


def test_a_missing_file_on_the_last_page_still_exits_two(library):
    repo, root = library
    h.clone_artifacts(repo, root, 200)
    drop_file(root, ordered(repo)[-1])
    report = walk(repo)
    assert report["exit_code"] == 2, report
    assert report["coverage"] == "complete"
    assert [row["verdict"] for row in report["findings"]] == ["missing"]


# ---------------------------------------------------------------------------
# External roots: the saved mapping decides, nothing is guessed
# ---------------------------------------------------------------------------


def test_a_saved_external_root_matches_without_guessing(library, tmp_path):
    repo, root = library
    outside = tmp_path / "outside"
    outside.mkdir()
    external_row(repo, root, outside)
    project = mapping_project(tmp_path, {"folder_test": outside})
    report = walk(repo, project_root=project)
    assert report["mapping"] == "file"
    assert report["roots"]["folder_test"]["state"] == "ok"
    assert report["exit_code"] == 0, report
    assert [row["verdict"] for row in report["rows"]] == ["match"]


def test_an_unplugged_external_root_is_reported_per_row(library, tmp_path):
    repo, root = library
    outside = tmp_path / "outside"
    outside.mkdir()
    external_row(repo, root, outside)
    project = mapping_project(tmp_path, {"folder_test": outside})
    assert walk(repo, project_root=project)["exit_code"] == 0
    outside.rename(tmp_path / "moved-away")
    report = walk(repo, project_root=project)
    assert [row["verdict"] for row in report["rows"]] == ["root_unavailable"]
    assert report["roots"]["folder_test"]["state"] == "missing"
    assert report["exit_code"] == 2, report


def test_an_unmapped_root_never_falls_back_to_the_project_root(library, tmp_path):
    repo, root = library
    outside = tmp_path / "outside"
    outside.mkdir()
    row, body = external_row(repo, root, outside)
    # A file that a project-root fallback would happily accept.
    (root / "p.jpg").write_bytes(body)
    report = walk(repo, roots={"folder_other": str(outside)})
    assert [row["verdict"] for row in report["rows"]] == ["root_unavailable"]
    assert report["rows"][0]["reason"] == "unbound"
    # The file a fallback would have hashed is right there under the project
    # root: "unbound" proves it was never consulted.
    assert report["exit_code"] == 2, report


def test_a_missing_mapping_file_means_no_external_roots(library, tmp_path):
    repo, root = library
    project = tmp_path / "empty-project"
    project.mkdir()
    assert load_output_roots(project) == {}
    report = walk(repo, project_root=project)
    assert report["mapping"] == "absent"
    assert report["roots"] == {}
    assert report["exit_code"] == 0, report


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        json.dumps({"version": 2, "roots": {}}),
        json.dumps({"version": 1, "roots": {"a": "relative/dir"}}),
        json.dumps({"version": 1, "roots": {"a": "x", "b": "x/child"}}),
        json.dumps({"version": 1, "roots": {"a": 7}}),
        json.dumps({"version": 1, "roots": {}, "extra": True}),
    ],
)
def test_a_broken_mapping_is_a_typed_mapping_error(library, tmp_path, payload):
    repo, _root = library
    project = mapping_project(tmp_path, {}, payload=payload)
    with pytest.raises(LocationError, match="location_mapping_invalid"):
        load_output_roots(project)
    with pytest.raises(scan.ScanMappingError) as caught:
        walk(repo, project_root=project)
    assert caught.value.code == "mapping_unreadable"
    assert caught.value.detail == "location_mapping_invalid"


def test_a_mapping_file_that_cannot_be_read_is_not_an_empty_mapping(tmp_path):
    project = tmp_path / "project"
    # A directory where the mapping file belongs: it exists, it cannot be read.
    (project / "config" / "output_roots_v1.json").mkdir(parents=True)
    with pytest.raises(LocationError, match="location_mapping_unreadable"):
        load_output_roots(project)
    owner = OutputLocations(AppPaths(project), PathResolver(AppPaths(project)))
    with pytest.raises(LocationError, match="location_mapping_unreadable"):
        owner.load()
    assert owner.resolver.external_roots == {}


def test_loading_a_saved_mapping_creates_and_moves_nothing(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    bare = tmp_path / "bare"
    bare.mkdir()
    assert load_output_roots(bare) == {}
    assert [item.name for item in bare.iterdir()] == []
    project = mapping_project(tmp_path, {"folder_a": outside})
    before = sorted(item.name for item in (project / "config").iterdir())
    assert load_output_roots(project) == {"folder_a": outside.resolve()}
    assert sorted(item.name for item in (project / "config").iterdir()) == before


def test_binding_a_mapping_does_not_mutate_the_callers_resolver(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    resolver = PathResolver(AppPaths(project))
    bound = scan.bind_output_roots(resolver, {"folder_a": outside})
    assert resolver.external_roots == {}
    assert bound.external_roots == {"folder_a": outside.resolve()}
    with pytest.raises(scan.ScanMappingError):
        scan.bind_output_roots(resolver, {"folder_a": project})


# ---------------------------------------------------------------------------
# A bare cursor is not the library
# ---------------------------------------------------------------------------


def test_a_continuation_from_a_later_cursor_is_a_partial_walk(library):
    repo, root = library
    h.clone_artifacts(repo, root, 200)
    first = walk(repo, kind="page")
    assert first["exit_code"] == 5
    assert first["next_cursor"] is not None
    report = walk(repo, after=first["next_cursor"], kind="continuation")
    assert report["scope"] == "continuation"
    assert report["coverage"] == "partial"
    assert report["stats"]["examined"] == 1
    assert report["resume"]["complete_prefix"] is False
    assert report["exit_code"] == 5, report


def test_a_continuation_at_the_end_of_the_ledger_still_exits_five(library):
    repo, root = library
    h.clone_artifacts(repo, root, 200)
    last = ordered(repo)[-1]
    cursor = (last["completed_at"], last["artifact_id"])
    report = walk(repo, after=cursor, kind="continuation")
    assert report["coverage"] == "empty"
    assert report["stats"]["examined"] == 0
    assert report["exit_code"] == 5, report


def fake_page(rows, *, next_cursor=None, truncated=False, library_rows=None,
              coverage="page"):
    """One engine response, shaped like the real one."""
    return {
        "rows": list(rows),
        "next_cursor": next_cursor,
        "truncated": truncated,
        "coverage": coverage,
        "ledger": {"state": "ok", "code": None, "last_safe_cursor": None},
        "stats": {
            "examined": len(rows),
            "considered": len(rows),
            "matched": len(rows),
            "missing": 0,
            "changed": 0,
            "unreadable": 0,
            "root_unavailable": 0,
            "changed_during_check": 0,
            "unknown": 0,
            "excluded": 0,
            "unknown_reasons": {},
            "library_rows": library_rows,
            "bytes_hashed": 0,
            "seconds": 0.0,
            "budget_exhausted_reason": None,
            "stopped_at": None,
        },
    }


def fake_rows(count):
    return [
        {"artifact_id": "a%04d" % index, "verdict": "match", "reason": "synthetic"}
        for index in range(count)
    ]


def test_a_cursor_that_does_not_move_stops_the_walk(monkeypatch):
    calls = []
    stuck_cursor = ("2026-11-14T00:00:00+00:00", "same-row")

    def stuck(source, *, after=None, limit=200, budget=None, cancel=None):
        calls.append(after)
        return fake_page([], next_cursor=stuck_cursor, truncated=True, library_rows=5)

    monkeypatch.setattr(scan, "scan_library_artifacts", stuck)
    report = scan.scan_library_report(object(), limit=200)
    assert len(calls) == 2
    assert report["coverage"] == "partial"
    assert report["stats"]["stopped_at"] == "no_progress"
    assert report["exit_code"] == 5


def test_an_empty_tail_page_is_the_end_of_the_walk_not_an_empty_library(monkeypatch):
    """A ledger that shrinks between pages reached EOF; it did not become new.

    Reporting "the library is empty" here would be worse than useless: the two
    hundred rows that really were examined would disappear from the answer.
    """
    calls = []

    def shrinking(source, *, after=None, limit=200, budget=None, cancel=None):
        calls.append(after)
        if after is None:
            return fake_page(
                fake_rows(2),
                next_cursor=("2026-11-14T00:00:00+00:00", "a0001"),
                truncated=True,
                library_rows=4,
            )
        return fake_page([], next_cursor=after, truncated=False, coverage="empty")

    monkeypatch.setattr(scan, "scan_library_artifacts", shrinking)
    report = scan.scan_library_report(object(), limit=2)
    assert len(calls) == 2
    assert report["coverage"] == "complete"
    assert report["stats"]["examined"] == 2
    assert report["stats"]["matched"] == 2
    assert report["exit_code"] == 0, report


# ---------------------------------------------------------------------------
# Budgets and cancellation: stop without losing a row
# ---------------------------------------------------------------------------


def test_a_byte_budget_stops_the_walk_and_the_next_run_misses_nothing(library):
    repo, root = library
    h.clone_artifacts(repo, root, 200)
    first = walk(repo, budget=ScanBudget(max_bytes=1000))
    assert first["exit_code"] == 5
    assert first["coverage"] == "partial"
    assert first["stats"]["budget_exhausted_reason"] == "max_bytes"
    assert 0 < first["stats"]["examined"] < 201
    seen = {row["artifact_id"] for row in first["rows"]}
    cursor = first["next_cursor"]
    pages = 1
    while cursor and len(seen) < 201:
        page = walk(
            repo,
            after=cursor,
            kind="continuation",
            budget=ScanBudget(max_bytes=1000),
        )
        pages += 1
        assert page["exit_code"] == 5
        if not page["rows"]:
            break
        seen |= {row["artifact_id"] for row in page["rows"]}
        cursor = page["next_cursor"]
        assert pages < 60
    assert len(seen) == 201


def test_a_file_budget_stops_after_the_requested_number_of_rows(library):
    repo, root = library
    h.clone_artifacts(repo, root, 200)
    report = walk(repo, budget=ScanBudget(max_files=10))
    assert report["stats"]["examined"] == 10
    assert report["coverage"] == "partial"
    assert report["stats"]["stopped_at"] == "max_files"
    assert report["next_cursor"] is not None
    assert report["exit_code"] == 5


def test_a_time_budget_that_expires_before_the_first_row_does_not_claim_a_page(
    library, monkeypatch
):
    repo, root = library
    h.clone_artifacts(repo, root, 2)
    ticks = {"value": 0.0}

    def advancing():
        ticks["value"] += 1.0
        return ticks["value"]

    monkeypatch.setattr(scan.time, "monotonic", advancing)
    report = walk(repo, budget=ScanBudget(max_seconds=0.5))
    assert report["pages_examined"] == 0
    assert report["stats"]["examined"] == 0
    assert report["coverage"] == "partial"
    assert report["stats"]["stopped_at"] == "max_seconds"
    assert report["exit_code"] == 5


def test_a_cancelled_walk_reports_cancelled_and_exits_five(library):
    repo, root = library
    h.clone_artifacts(repo, root, 300)

    class CancelAfter:
        def __init__(self, limit):
            self.calls = 0
            self.limit = limit

        def is_set(self):
            self.calls += 1
            return self.calls > self.limit

    report = walk(repo, cancel=CancelAfter(250))
    assert report["coverage"] == "cancelled"
    assert report["stats"]["stopped_at"] == "cancel"
    assert 0 < report["stats"]["examined"] < 301
    assert report["exit_code"] == 5


# ---------------------------------------------------------------------------
# Unreadable and empty ledgers
# ---------------------------------------------------------------------------


def test_a_ledger_that_dies_mid_walk_exits_three_and_keeps_the_earlier_findings(
    library, monkeypatch
):
    repo, root = library
    h.clone_artifacts(repo, root, 200)
    drop_file(root, ordered(repo)[5])
    original = audit.read_projection
    calls = {"count": 0}

    def flaky(connection):
        calls["count"] += 1
        if calls["count"] >= 2:
            raise sqlite3.OperationalError("synthetic ledger failure")
        return original(connection)

    monkeypatch.setattr(audit, "read_projection", flaky)
    report = walk(repo)
    assert report["coverage"] == "error"
    assert report["ledger"]["state"] == "error"
    assert report["stats"]["examined"] == 200
    assert [row["verdict"] for row in report["findings"]] == ["missing"]
    assert report["resume"]["state"] == "blocked"
    assert report["exit_code"] == 3, report


def test_a_library_that_cannot_be_opened_is_unreadable_not_empty(tmp_path):
    resolver = PathResolver(AppPaths(tmp_path))
    report = scan.scan_library_report((tmp_path / "missing.sqlite3", resolver))
    assert report["coverage"] == "error"
    assert report["ledger"]["state"] == "error"
    assert report["pages_examined"] == 0
    assert report["library_rows"] is None
    assert report["exit_code"] == 3


def test_an_empty_library_is_empty_and_exits_zero(library):
    repo, _root = library
    report = walk(repo)
    assert report["coverage"] == "empty"
    assert report["library_rows"] == 0
    assert report["stats"]["examined"] == 0
    assert report["exit_code"] == 0, report
    assert report["resume"]["state"] == "not_needed"


# ---------------------------------------------------------------------------
# Unknown rows: a live gap is not released by --allow-unknown
# ---------------------------------------------------------------------------


def test_a_row_that_exceeds_the_byte_budget_stays_unknown_and_the_walk_continues(library):
    repo, root = library
    h.clone_artifacts(repo, root, 4)
    oversized = ordered(repo)[2]
    h.sql(
        repo,
        "UPDATE artifacts SET size = ? WHERE artifact_id = ?",
        (10 ** 6, oversized["artifact_id"]),
    )
    report = walk(repo, budget=ScanBudget(max_bytes=1000))
    assert report["stats"]["examined"] == 5
    assert report["stats"]["unknown"] == 1
    assert report["unknown_reasons"] == {"file_exceeds_budget": 1}
    assert report["coverage"] == "complete"
    assert report["exit_code"] == 5, report


def test_allow_unknown_releases_only_legacy_rows(library):
    repo, root = library
    h.clone_artifacts(repo, root, 4)
    row = ordered(repo)[0]
    h.sql(
        repo,
        "UPDATE artifacts SET sha256 = NULL WHERE artifact_id = ?",
        (row["artifact_id"],),
    )
    strict = walk(repo)
    assert strict["unknown_reasons"] == {"no_recorded_digest": 1}
    assert strict["exit_code"] == 5
    released = walk(repo, allow_unknown=True)
    assert released["unknown_reasons"] == {"no_recorded_digest": 1}
    assert released["exit_code"] == 0, released
    # A live gap stays fatal even with the flag.
    h.sql(
        repo,
        "UPDATE artifacts SET size = 1.5 WHERE artifact_id = ?",
        (row["artifact_id"],),
    )
    live = walk(repo, allow_unknown=True)
    assert live["unknown_reasons"] == {"artifact_record_invalid": 1}
    assert live["exit_code"] == 5, live


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 201},
        {"limit": "200"},
        {"kind": "everything"},
        {"kind": "continuation"},
        {"kind": "full_walk", "after": ("2026-11-14T00:00:00+00:00", "a")},
        {"after": "a-cursor"},
        {"budget": object()},
    ],
)
def test_invalid_arguments_are_rejected(library, kwargs):
    repo, _root = library
    with pytest.raises(ContractError):
        walk(repo, **kwargs)


def test_a_page_walk_is_one_page_and_never_claims_the_library(library):
    repo, root = library
    h.clone_artifacts(repo, root, 400)
    report = walk(repo, kind="page")
    assert report["pages_examined"] == 1
    assert report["stats"]["examined"] == 200
    assert report["coverage"] == "partial"
    assert report["exit_code"] == 5
