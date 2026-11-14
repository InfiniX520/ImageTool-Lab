"""WP3: the read-only library audit reports what the disk holds right now.

Every case is synthetic: one temporary ledger, one temporary output root,
synthetic PNG bytes published by the real executor, and the real
core.library_audit module.  No network, account, production library, real
gallery or Tk widget is touched.

The frozen behaviour under test (U1A_W5_W6_SCOPE.md section 5.3):

* a verdict always comes from the bytes that exist now, never from the
  historical artifact row;
* "missing" is not an accident report and never authorizes a repair; a recorded
  EXCLUDED intent is reported as the user's own decision;
* an unplugged root, an unreadable file and a file that changed during the read
  stay separate facts;
* budgets stop a pass with truncated and a resume cursor, and a cancelled pass
  never pretends to be complete;
* the ledger is closed before the first byte is hashed, and nothing at all is
  written - not the ledger, not the gallery.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

import core.library_audit as audit
import test_u1a_recovery as h
from core.app_paths import AppPaths
from core.library_paths import PathResolver
from core.library_repository import LibraryRepository
from core.local_observation import LocalObserver, _FileOperations, _open_readonly
from core.task_contracts import ContractError, PathRef, canonical_json
from delivery_fixtures import Response, Transport, executor, run

ARTWORK = h.ARTWORK
OUTPUT_KEY_PREFIX = "downl_Pixiv"
REASON = re.compile(r"[a-z][a-z0-9_]*")
FROZEN_ROW_KEYS = {
    "artifact_id", "delivery_id", "illust_id", "page_index", "verdict", "reason",
    "size_recorded", "size_observed", "sha256_recorded", "observed_sha256",
    "checked_at", "output_key",
    # contract v5 section 2: which stored value made a row unusable, and the
    # type it was stored as.  Both are always present, None when not applicable.
    "reason_detail", "size_recorded_raw_type",
}
FROZEN_STATS_KEYS = {
    "examined", "matched", "missing", "changed", "unreadable", "root_unavailable",
    "changed_during_check", "unknown", "excluded", "bytes_hashed", "seconds",
    "budget_exhausted_reason",
    # contract v5 section 2: how much of the ledger this pass covered.
    "considered", "library_rows", "unknown_reasons", "stopped_at",
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


def publish(repo, root, *illust_ids, request_id="scan"):
    """Deliver one synthetic PNG per artwork and settle the batch."""
    job = h.submit(repo, request_id=request_id)
    page = h.source(tuple(h.candidate(str(value)) for value in illust_ids))
    ids = h.accept(repo, job["batch_id"], page)
    assert len(ids) == len(illust_ids)
    service = h.executor(repo, root, Transport(*[Response() for _ in ids]))
    for delivery in ids:
        outcome = run(repo, job, delivery, service)
        assert outcome.state == "completed", outcome
    repo.finish_source(job["batch_id"], coverage_complete=True)
    repo.settle_batch(job["batch_id"])
    return job, ids


def artifact_row(repo, delivery_id):
    rows = h.query(
        repo.database_path,
        "SELECT * FROM artifacts WHERE delivery_id = ?",
        (delivery_id,),
    )
    assert len(rows) == 1
    return rows[0]


def artifact_ref(row):
    return PathRef(**json.loads(row["output_ref_json"]))


def target_path(root, row):
    return root.joinpath(*artifact_ref(row).relative_path.split("/"))


def scan(repo, **kwargs):
    return audit.scan_library_artifacts(repo, **kwargs)


def only_row(result):
    assert len(result["rows"]) == 1, result["rows"]
    return result["rows"][0]


def record_exclusion(repo, batch_id, row, intent="excluded"):
    """Record the user's own removal decision for this page."""
    return repo.record_removal_intent(
        str(batch_id),
        str(row["illust_id"]),
        int(row["page_index"]),
        artifact_ref(row),
        intent=intent,
        command_id="intent-" + str(row["artifact_id"])[:12],
    )


def sql(repo, statement, parameters=()):
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute(statement, parameters)
        connection.commit()


# ---------------------------------------------------------------------------
# One file at a time
# ---------------------------------------------------------------------------


def test_a_clean_delivered_file_is_a_full_hash_match(library):
    repo, root = library
    _, ids = publish(repo, root, ARTWORK)
    row = artifact_row(repo, ids[0])

    result = scan(repo)

    fact = only_row(result)
    assert fact["verdict"] == "match"
    assert fact["reason"] == "full_sha256_match"
    assert fact["observed_sha256"] == row["sha256"]
    assert fact["size_observed"] == row["size"]
    assert fact["sha256_recorded"] == row["sha256"]
    assert result["stats"]["bytes_hashed"] == row["size"]
    assert result["truncated"] is False
    assert result["next_cursor"] == (row["completed_at"], row["artifact_id"])


def test_an_equal_size_edit_with_a_restored_mtime_is_changed(library):
    repo, root = library
    _, ids = publish(repo, root, ARTWORK)
    row = artifact_row(repo, ids[0])
    path = target_path(root, row)
    original = path.stat()
    path.write_bytes(h.SAME_SIZE_PNG)
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert path.stat().st_size == original.st_size
    assert path.stat().st_mtime_ns == original.st_mtime_ns

    fact = only_row(scan(repo))

    assert fact["verdict"] == "changed"
    assert fact["reason"] == "hash_mismatch"
    assert fact["observed_sha256"] != row["sha256"]


def test_a_recorded_size_that_does_not_match_the_file_is_changed(library):
    repo, root = library
    _, ids = publish(repo, root, ARTWORK)
    row = artifact_row(repo, ids[0])
    sql(repo, "UPDATE artifacts SET size = ? WHERE artifact_id = ?",
        (int(row["size"]) + 1, row["artifact_id"]))

    result = scan(repo)

    fact = only_row(result)
    assert fact["verdict"] == "changed"
    assert fact["reason"] == "size_mismatch"
    assert result["stats"]["bytes_hashed"] == 0


def test_a_deleted_file_is_missing_and_changes_nothing(library):
    repo, root = library
    _, ids = publish(repo, root, ARTWORK)
    row = artifact_row(repo, ids[0])
    target_path(root, row).unlink()

    result = scan(repo)

    fact = only_row(result)
    assert fact["verdict"] == "missing"
    assert fact["reason"] == "path_not_found"
    assert fact["observed_sha256"] is None
    assert result["stats"]["bytes_hashed"] == 0
    # The row is durable history; the audit only reports, it never repairs.
    assert artifact_row(repo, ids[0]) == row
    assert repo.get_delivery(ids[0])["state"] == "completed"


def test_a_renamed_output_root_is_root_unavailable_then_matches_again(library):
    repo, root = library
    _, ids = publish(repo, root, ARTWORK)
    moved = root.parent / "library-unplugged"
    root.rename(moved)
    try:
        fact = only_row(scan(repo))
        assert fact["verdict"] == "root_unavailable"
        assert fact["reason"] == "missing_bound"
    finally:
        moved.rename(root)

    assert only_row(scan(repo))["verdict"] == "match"


def test_a_bound_but_unmapped_root_reports_unbound(library):
    repo, root = library
    _, ids = publish(repo, root, ARTWORK)
    row = artifact_row(repo, ids[0])
    external = PathRef(kind="external", root_id="disk2", relative_path="downl_Pixiv/x.png")
    sql(repo, "UPDATE artifacts SET output_ref_json = ?, output_key = ? WHERE artifact_id = ?",
        (canonical_json(asdict(external)), external.target_key, row["artifact_id"]))

    fact = only_row(scan(repo))

    assert fact["verdict"] == "root_unavailable"
    assert fact["reason"] == "unbound"


def test_an_unreadable_file_is_unreadable_and_the_bytes_are_kept(library, monkeypatch):
    repo, root = library
    _, ids = publish(repo, root, ARTWORK)
    row = artifact_row(repo, ids[0])

    def denied(path):
        raise PermissionError(13, "denied")

    inject_observer(
        monkeypatch,
        artifact_ref(row).relative_path,
        io=_FileOperations(denied, lambda path: path.lstat(), os.fstat),
    )

    fact = only_row(scan(repo))

    assert fact["verdict"] == "unreadable"
    assert fact["reason"] == "open_failed"
    assert target_path(root, row).read_bytes() == h.PNG_BYTES


def test_a_directory_entry_that_cannot_be_stat_ed_is_unreadable(library, monkeypatch):
    """A stat failure is unreadable, never a missing file and never a match."""
    from core.file_presence import Presence, PresenceResult

    repo, root = library
    _, ids = publish(repo, root, ARTWORK)
    real = audit.probe_ref

    def probe(resolver, ref):
        if ref.relative_path.endswith("_p0.png"):
            return PresenceResult(Presence.UNREADABLE, ref, "permission_denied")
        return real(resolver, ref)

    monkeypatch.setattr(audit, "probe_ref", probe)

    fact = only_row(scan(repo))

    assert fact["verdict"] == "unreadable"
    assert fact["reason"] == "permission_denied"
    assert fact["observed_sha256"] is None


def test_a_file_that_changes_during_the_read_is_not_judged(library, monkeypatch):
    repo, root = library
    _, ids = publish(repo, root, ARTWORK)
    row = artifact_row(repo, ids[0])

    class Stat:
        def __init__(self, info, mtime_ns):
            self.st_mode = info.st_mode
            self.st_size = info.st_size
            self.st_mtime_ns = mtime_ns
            self.st_dev = info.st_dev
            self.st_ino = info.st_ino
            self.st_file_attributes = getattr(info, "st_file_attributes", 0)

    calls = {"count": 0}

    def drifting(stream):
        info = os.fstat(stream.fileno())
        calls["count"] += 1
        offset = 1 if calls["count"] >= 2 else 0
        return Stat(info, info.st_mtime_ns + offset)

    inject_observer(
        monkeypatch,
        artifact_ref(row).relative_path,
        io=_FileOperations(_open_readonly, lambda path: path.lstat(), drifting),
    )

    fact = only_row(scan(repo))

    assert fact["verdict"] == "changed_during_check"
    assert fact["reason"] == "metadata_changed_while_reading"
    assert fact["observed_sha256"] is None


def inject_observer(monkeypatch, relative_path, *, io=None, monotonic=None):
    """Give the observer a test adapter without adding a production seam."""
    real = audit._observer

    def factory(observers, resolver, ref, revision):
        if relative_path is not None and ref.relative_path != relative_path:
            return real(observers, resolver, ref, revision)
        extra = {}
        if io is not None:
            extra["_io"] = io
        if monotonic is not None:
            extra["_monotonic"] = monotonic
        root = resolver.root_path(ref)
        return LocalObserver(
            root,
            root_id=ref.root_id,
            kind=ref.kind,
            root_revision=revision,
            **extra,
        )

    monkeypatch.setattr(audit, "_observer", factory)


# ---------------------------------------------------------------------------
# Legacy rows and malformed records
# ---------------------------------------------------------------------------


def test_a_legacy_row_without_a_digest_is_unknown_without_hashing(library):
    repo, root = library
    _, ids = publish(repo, root, ARTWORK)
    row = artifact_row(repo, ids[0])
    sql(repo, "UPDATE artifacts SET sha256 = NULL WHERE artifact_id = ?", (row["artifact_id"],))

    result = scan(repo)

    fact = only_row(result)
    assert fact["verdict"] == "unknown"
    assert fact["reason"] == "no_recorded_digest"
    assert fact["sha256_recorded"] is None
    assert fact["observed_sha256"] is None
    assert result["stats"]["bytes_hashed"] == 0


def test_a_legacy_row_without_a_size_is_unknown_without_hashing(library):
    repo, root = library
    _, ids = publish(repo, root, ARTWORK)
    row = artifact_row(repo, ids[0])
    sql(repo, "UPDATE artifacts SET size = NULL WHERE artifact_id = ?", (row["artifact_id"],))

    result = scan(repo)

    fact = only_row(result)
    assert fact["verdict"] == "unknown"
    assert fact["reason"] == "no_recorded_size"
    assert result["stats"]["bytes_hashed"] == 0


def test_a_row_with_a_broken_output_reference_is_unknown(library):
    repo, root = library
    _, ids = publish(repo, root, ARTWORK)
    row = artifact_row(repo, ids[0])
    sql(repo, "UPDATE artifacts SET output_ref_json = ? WHERE artifact_id = ?",
        ('{"kind": "project"', row["artifact_id"]))

    fact = only_row(scan(repo))

    assert fact["verdict"] == "unknown"
    assert fact["reason"] == "artifact_path_invalid"


def test_an_artifact_record_the_health_contract_rejects_is_unknown(library):
    repo, root = library
    _, ids = publish(repo, root, ARTWORK)
    row = artifact_row(repo, ids[0])
    sql(repo, "UPDATE artifacts SET media_type = 'image/tiff' WHERE artifact_id = ?",
        (row["artifact_id"],))

    result = scan(repo)

    fact = only_row(result)
    assert fact["verdict"] == "unknown"
    assert fact["reason"] == "artifact_record_invalid"
    assert result["stats"]["bytes_hashed"] == 0


# ---------------------------------------------------------------------------
# The user's own decision is not an accident
# ---------------------------------------------------------------------------


def test_an_excluded_file_is_reported_as_the_users_own_decision(library):
    repo, root = library
    job, ids = publish(repo, root, ARTWORK)
    row = artifact_row(repo, ids[0])
    assert record_exclusion(repo, job["batch_id"], row)["intent"] == "excluded"
    target_path(root, row).unlink()

    result = scan(repo)

    fact = only_row(result)
    assert fact["verdict"] == "excluded"
    assert fact["reason"] == "intent_excluded"
    assert result["stats"]["excluded"] == 1
    assert result["stats"]["missing"] == 0


def test_an_excluded_file_that_still_exists_is_judged_by_its_bytes(library):
    repo, root = library
    job, ids = publish(repo, root, ARTWORK)
    row = artifact_row(repo, ids[0])
    record_exclusion(repo, job["batch_id"], row)

    assert only_row(scan(repo))["verdict"] == "match", "an intent never outvotes bytes"


def test_an_unreadable_intent_record_never_turns_absence_into_missing(library):
    repo, root = library
    _, ids = publish(repo, root, ARTWORK)
    row = artifact_row(repo, ids[0])
    target_path(root, row).unlink()
    sql(repo,
        "INSERT INTO events(event_uuid, job_id, batch_id, kind, safe_payload, "
        "created_at) VALUES (?, ?, NULL, 'removal_intent', ?, ?)",
        ("corrupt-intent-event", row["job_id"], "not json",
         datetime.now(timezone.utc).isoformat(timespec="microseconds")))

    fact = only_row(scan(repo))

    assert fact["verdict"] == "unknown"
    assert fact["reason"] == "corrupt_removal_intent"


# ---------------------------------------------------------------------------
# Budgets, cancellation and pagination
# ---------------------------------------------------------------------------


class Cancel:
    """Deterministic cancellation: the k-th check is the first to say yes."""

    def __init__(self, after):
        self.after = after
        self.calls = 0

    def is_set(self):
        self.calls += 1
        return self.calls > self.after


def test_the_vocabulary_and_row_shape_are_frozen(library, tmp_path):
    repo, root = library
    publish(repo, root, ARTWORK, "456")

    result = scan(repo)

    assert set(result) == {
        "rows", "next_cursor", "truncated", "coverage", "ledger", "stats",
    }
    assert set(result["stats"]) == FROZEN_STATS_KEYS
    assert result["coverage"] in audit.COVERAGES
    assert set(result["ledger"]) == {"state", "code", "last_safe_cursor"}
    assert result["ledger"]["state"] == "ok" and result["ledger"]["code"] is None
    assert audit.VERDICTS == {
        "match", "missing", "changed", "unreadable", "root_unavailable",
        "changed_during_check", "unknown", "excluded",
    }
    for fact in result["rows"]:
        assert set(fact) == FROZEN_ROW_KEYS
        assert fact["verdict"] in audit.VERDICTS
        assert REASON.fullmatch(fact["reason"]), fact["reason"]
    payload = json.dumps(result)
    assert OUTPUT_KEY_PREFIX not in payload, "a plaintext path leaked into the report"
    assert str(root) not in payload
    assert str(tmp_path) not in payload


def test_pagination_visits_every_artifact_exactly_once(library):
    repo, root = library
    publish(repo, root, "101", "102", "103", "104", "105")
    expected = sorted(
        item["artifact_id"]
        for item in h.query(repo.database_path, "SELECT artifact_id FROM artifacts")
    )
    assert len(expected) == 5

    seen = []
    cursor = None
    pages = 0
    while True:
        result = scan(repo, after=cursor, limit=2)
        pages += 1
        seen.extend(item["artifact_id"] for item in result["rows"])
        if not result["truncated"]:
            break
        cursor = result["next_cursor"]
        assert cursor is not None
    assert pages == 3
    assert sorted(seen) == expected
    assert len(set(seen)) == 5


def test_the_file_budget_stops_the_pass_at_a_row_boundary(library):
    repo, root = library
    publish(repo, root, "101", "102", "103")

    result = scan(repo, limit=3, budget=audit.ScanBudget(max_files=1))

    assert result["stats"]["examined"] == 1
    assert result["truncated"] is True
    assert result["stats"]["budget_exhausted_reason"] == "max_files"
    resumed = scan(repo, after=result["next_cursor"], limit=3)
    assert resumed["stats"]["examined"] == 2
    first = {row["artifact_id"] for row in result["rows"]}
    assert first.isdisjoint({row["artifact_id"] for row in resumed["rows"]})


def test_the_byte_budget_stops_the_pass_and_the_cursor_resumes(library):
    repo, root = library
    publish(repo, root, "101", "102", "103")
    size = len(h.PNG_BYTES)

    result = scan(repo, limit=3, budget=audit.ScanBudget(max_bytes=size + size // 2))

    assert result["stats"]["bytes_hashed"] == size
    assert result["truncated"] is True
    assert result["stats"]["budget_exhausted_reason"] == "max_bytes"
    assert result["rows"][0]["verdict"] == "match"
    assert result["rows"][1]["verdict"] == "unknown"
    assert result["rows"][1]["reason"] == "byte_budget_exhausted"
    stopped = result["rows"][1]["artifact_id"]
    resumed = scan(repo, after=result["next_cursor"], limit=3)
    assert resumed["rows"][0]["artifact_id"] == stopped, "the stopped row is re-examined"
    assert resumed["rows"][0]["verdict"] == "match"
    assert resumed["stats"]["examined"] == 2


def test_a_file_larger_than_the_byte_budget_is_uncheckable_but_does_not_block(library):
    repo, root = library
    publish(repo, root, "101", "102")
    size = len(h.PNG_BYTES)

    result = scan(repo, limit=2, budget=audit.ScanBudget(max_bytes=size - 1))

    assert result["stats"]["examined"] == 2, "an uncheckable file must not starve the next"
    assert {row["verdict"] for row in result["rows"]} == {"unknown"}
    assert {row["reason"] for row in result["rows"]} == {"file_exceeds_budget"}
    assert result["truncated"] is False
    assert result["stats"]["budget_exhausted_reason"] is None
    assert result["stats"]["bytes_hashed"] == 0


def test_the_time_budget_stops_the_pass_before_the_first_file(library, monkeypatch):
    repo, root = library
    publish(repo, root, "101")
    ticks = iter([0.0, 100.0, 200.0, 300.0, 400.0, 500.0])
    monkeypatch.setattr(audit.time, "monotonic", lambda: next(ticks))

    result = scan(repo, budget=audit.ScanBudget(max_seconds=1.0))

    assert result["rows"] == []
    assert result["truncated"] is True
    assert result["stats"]["budget_exhausted_reason"] == "max_seconds"
    assert result["stats"]["examined"] == 0
    # Nothing was consumed, so the cursor repeats the incoming one.
    assert result["next_cursor"] is None
    monkeypatch.undo()
    assert len(scan(repo)["rows"]) == 1, "a full budget still audits the row"


def test_a_per_file_time_cap_does_not_truncate_the_pass(library, monkeypatch):
    repo, root = library
    publish(repo, root, "101", "102")
    ticks = {"count": 0}

    def clock():
        ticks["count"] += 1
        return float(ticks["count"])

    inject_observer(monkeypatch, None, monotonic=clock)

    result = scan(repo, limit=2, budget=audit.ScanBudget(per_file_seconds=1e-09))

    assert result["stats"]["examined"] == 2
    assert {row["reason"] for row in result["rows"]} == {"time_budget_exhausted"}
    assert result["stats"]["budget_exhausted_reason"] is None
    assert result["truncated"] is False


def test_a_cancelled_pass_returns_partial_evidence_with_a_resume_cursor(library):
    repo, root = library
    publish(repo, root, "101", "102", "103")
    order = [
        dict(item)
        for item in h.query(
            repo.database_path,
            "SELECT artifact_id, completed_at FROM artifacts "
            "ORDER BY completed_at ASC, artifact_id ASC",
        )
    ]
    cancel = Cancel(after=2)

    result = scan(repo, limit=3, cancel=cancel)

    assert [row["artifact_id"] for row in result["rows"]] == [
        item["artifact_id"] for item in order[:2]
    ]
    assert result["truncated"] is True
    assert result["stats"]["budget_exhausted_reason"] == "cancel_requested"
    # The cursor stays on the last row that reached a verdict: the cancelled
    # row is examined again by the next pass instead of being skipped.
    assert result["next_cursor"] == (order[1]["completed_at"], order[1]["artifact_id"])
    resumed = scan(repo, after=result["next_cursor"], limit=3)
    assert [row["artifact_id"] for row in resumed["rows"]] == [
        item["artifact_id"] for item in order[2:]
    ]


# ---------------------------------------------------------------------------
# Read-only discipline and cost
# ---------------------------------------------------------------------------


class ConnectionProxy:
    def __init__(self, connection, counter):
        self._connection = connection
        self._counter = counter

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._connection.row_factory = value

    def execute(self, *args):
        return self._connection.execute(*args)

    def close(self):
        self._counter["live"] -= 1
        self._connection.close()


class CountingRepository:
    """A repository facade that counts open read connections."""

    def __init__(self, repo):
        self._repo = repo
        self.counter = {"opened": 0, "live": 0}

    def _path_resolver_snapshot(self):
        return self._repo._path_resolver_snapshot()

    def _readonly_connection(self):
        connection = self._repo._readonly_connection()
        self.counter["opened"] += 1
        self.counter["live"] += 1
        return ConnectionProxy(connection, self.counter)


def test_the_scan_never_holds_a_read_connection_while_hashing(library, monkeypatch):
    repo, root = library
    publish(repo, root, "101", "102")
    counted = CountingRepository(repo)
    real = audit._observer

    def factory(observers, resolver, ref, revision):
        observer = real(observers, resolver, ref, revision)

        class Guarded:
            def observe(self, request):
                assert counted.counter["live"] == 0, "the ledger was open while hashing"
                return observer.observe(request)

        return Guarded()

    monkeypatch.setattr(audit, "_observer", factory)

    result = audit.scan_library_artifacts(counted, limit=2)

    assert [row["verdict"] for row in result["rows"]] == ["match", "match"]
    assert counted.counter["opened"] >= 1
    assert counted.counter["live"] == 0


def test_the_scan_leaves_the_ledger_and_the_gallery_untouched(library):
    repo, root = library
    publish(repo, root, "101", "102")
    database = repo.database_path

    def snapshot(base):
        return {
            str(path.relative_to(base)): (path.is_file(), path.stat().st_mtime_ns)
            for path in sorted(base.rglob("*"))
        }

    gallery = root / OUTPUT_KEY_PREFIX
    before_gallery = snapshot(gallery)
    before_ledger = (database.stat().st_size, database.stat().st_mtime_ns)

    scan(repo, limit=2)

    assert snapshot(gallery) == before_gallery
    assert (database.stat().st_size, database.stat().st_mtime_ns) == before_ledger
    assert not list(gallery.rglob("*.part"))
    assert not list(gallery.rglob("*.tmp"))


def test_the_database_source_form_reports_the_same_verdicts(library):
    repo, root = library
    publish(repo, root, "101")

    result = audit.scan_library_artifacts(
        (repo.database_path, repo._path_resolver_snapshot()), limit=10
    )

    assert only_row(result)["verdict"] == "match"


def test_a_repository_without_a_bound_resolver_is_rejected(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        with pytest.raises(ContractError):
            scan(repo)


def test_invalid_arguments_are_rejected(library):
    repo, root = library
    publish(repo, root, "101")

    for kwargs in ({"limit": 0}, {"limit": 201}, {"limit": "10"},
                   {"after": ("only-one",)}, {"after": 5},
                   {"budget": {"max_files": 1}}, {"cancel": object()},
                   {"stats": []}):
        with pytest.raises(ContractError):
            scan(repo, **kwargs)
    with pytest.raises(ContractError):
        audit.scan_library_artifacts(object())
    with pytest.raises(ContractError):
        audit.scan_library_artifacts((repo.database_path,))
    with pytest.raises(ContractError):
        audit.ScanBudget(max_files=500)
    with pytest.raises(ContractError):
        audit.ScanBudget(max_bytes=-1)
    with pytest.raises(ContractError):
        audit.ScanBudget(max_seconds=0)


# ---------------------------------------------------------------------------
# Scale: the ledger is read in pages, never row by row
# ---------------------------------------------------------------------------


def clone_artifacts(repo, root, count):
    """Publish one real page, then clone it into count sibling facts."""
    _, ids = publish(repo, root, "777", request_id="bulk")
    with sqlite3.connect(repo.database_path) as connection:
        connection.row_factory = sqlite3.Row
        artifact = dict(connection.execute(
            "SELECT * FROM artifacts WHERE delivery_id = ?", (ids[0],)
        ).fetchone())
        delivery = dict(connection.execute(
            "SELECT * FROM deliveries WHERE delivery_id = ?", (ids[0],)
        ).fetchone())
        folder = root / OUTPUT_KEY_PREFIX / "bulk"
        folder.mkdir(parents=True, exist_ok=True)
        artifact_columns = list(artifact)
        delivery_columns = list(delivery)
        for index in range(count):
            body = b"bulk-%04d-" % index + b"x" * 32
            name = "b%04d.png" % index
            (folder / name).write_bytes(body)
            ref = PathRef(relative_path=OUTPUT_KEY_PREFIX + "/bulk/" + name)
            ident = str(900000 + index)
            delivery_id = "%032x" % (index + 1)
            row = dict(delivery)
            row.update(
                delivery_id=delivery_id,
                illust_id=ident,
                output_key=ref.target_key,
                output_ref_json=canonical_json(asdict(ref)),
                state="completed",
                error_code=None,
            )
            connection.execute(
                "INSERT INTO deliveries(" + ",".join(delivery_columns) + ") VALUES("
                + ",".join("?" for _ in delivery_columns) + ")",
                [row[column] for column in delivery_columns],
            )
            record = dict(artifact)
            record.update(
                artifact_id="%032x" % (0x10000 + index),
                delivery_id=delivery_id,
                illust_id=ident,
                output_key=ref.target_key,
                output_ref_json=canonical_json(asdict(ref)),
                size=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
                completed_at="2026-11-14T00:00:00.%06d+00:00" % index,
            )
            connection.execute(
                "INSERT INTO artifacts(" + ",".join(artifact_columns) + ") VALUES("
                + ",".join("?" for _ in artifact_columns) + ")",
                [record[column] for column in artifact_columns],
            )
        connection.commit()
    return ids


def test_two_thousand_rows_use_a_bounded_number_of_statements(library, monkeypatch):
    repo, root = library
    clone_artifacts(repo, root, 2000)
    statements = []
    open_read_only = audit._open_readonly

    def traced(path):
        connection = open_read_only(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(audit, "_open_readonly", traced)
    source = (repo.database_path, repo._path_resolver_snapshot())

    seen = []
    cursor = None
    pages = 0
    while True:
        result = audit.scan_library_artifacts(source, after=cursor, limit=200)
        pages += 1
        seen.extend(row["artifact_id"] for row in result["rows"])
        if not result["truncated"]:
            break
        cursor = result["next_cursor"]

    selects = [
        text for text in statements if text.lstrip().upper().startswith("SELECT")
    ]
    assert len(seen) == 2001
    assert len(set(seen)) == 2001
    assert pages == 11
    assert len(selects) <= 2 * pages + 2, (len(selects), pages)
    assert len(selects) < 40, "the ledger must not be read row by row"

# ---------------------------------------------------------------------------
# Contract v5 section 2: broken recorded values and honest coverage
# ---------------------------------------------------------------------------


def ordered_rows(repo, ids):
    """The artifacts in the order the scan walks them."""
    rows = [artifact_row(repo, delivery_id) for delivery_id in ids]
    return sorted(rows, key=lambda item: (item["completed_at"], item["artifact_id"]))


def test_a_text_size_is_reported_on_its_row_and_never_stops_the_pass(library):
    repo, root = library
    _, ids = publish(repo, root, "101", "102")
    rows = ordered_rows(repo, ids)
    sql(
        repo,
        "UPDATE artifacts SET size = 'broken-size' WHERE artifact_id = ?",
        (rows[1]["artifact_id"],),
    )

    result = scan(repo)

    facts = {fact["artifact_id"]: fact for fact in result["rows"]}
    good, bad = rows[0]["artifact_id"], rows[1]["artifact_id"]
    assert facts[good]["verdict"] == "match"
    assert facts[bad]["verdict"] == "unknown"
    assert facts[bad]["reason"] == "artifact_record_invalid"
    assert facts[bad]["reason_detail"] == "record_size_invalid"
    assert facts[bad]["size_recorded"] is None
    assert facts[bad]["size_recorded_raw_type"] == "text"
    assert facts[bad]["size_observed"] is None
    assert result["coverage"] == "complete"
    assert result["ledger"]["state"] == "ok"
    assert result["stats"]["unknown_reasons"] == {"artifact_record_invalid": 1}


def test_a_blob_or_real_size_is_reported_with_the_stored_type(library):
    repo, root = library
    _, ids = publish(repo, root, "101", "102")
    rows = ordered_rows(repo, ids)
    sql(
        repo,
        "UPDATE artifacts SET size = ? WHERE artifact_id = ?",
        (b"blob-size", rows[0]["artifact_id"]),
    )
    sql(
        repo,
        "UPDATE artifacts SET size = 1.5 WHERE artifact_id = ?",
        (rows[1]["artifact_id"],),
    )

    result = scan(repo)

    facts = {fact["artifact_id"]: fact for fact in result["rows"]}
    assert facts[rows[0]["artifact_id"]]["size_recorded_raw_type"] == "blob"
    assert facts[rows[1]["artifact_id"]]["size_recorded_raw_type"] == "real"
    assert {fact["reason_detail"] for fact in result["rows"]} == {"record_size_invalid"}


def test_the_row_reader_reports_a_non_positive_size_without_coercing_it():
    # The shipped DDL forbids a non-positive integer size, so this branch only
    # fires on a drifted or foreign ledger; it is proved here directly.
    base = {
        "artifact_id": "artifact-1",
        "completed_at": "2026-11-14T00:00:00+00:00",
        "delivery_id": "delivery-1",
        "illust_id": "101",
        "page_index": 0,
        "output_key": "key-1",
        "size": -5,
        "sha256": None,
    }

    fields, detail = audit._row_fields(base)
    assert detail == "record_size_invalid"
    assert fields["size_recorded"] is None
    assert fields["size_recorded_raw_type"] == "negative"
    for value, raw in ((0, "negative"), (1.25, "real"), (b"x", "blob"), ("9", "text")):
        fields, detail = audit._row_fields(dict(base, size=value))
        assert detail == "record_size_invalid"
        assert fields["size_recorded_raw_type"] == raw


def test_a_null_size_stays_the_legacy_reason(library):
    repo, root = library
    _, ids = publish(repo, root, ARTWORK)
    row = artifact_row(repo, ids[0])
    sql(
        repo,
        "UPDATE artifacts SET size = NULL WHERE artifact_id = ?",
        (row["artifact_id"],),
    )

    fact = only_row(scan(repo))

    assert fact["verdict"] == "unknown"
    assert fact["reason"] == "no_recorded_size"
    assert fact["reason_detail"] is None
    assert fact["size_recorded_raw_type"] == "null"


def test_the_row_reader_flags_a_digest_that_is_not_a_digest():
    # Same reason as the size case: the DDL check keeps a malformed digest out
    # of a well-formed ledger, so the reader is exercised directly.
    base = {
        "artifact_id": "artifact-1",
        "completed_at": "2026-11-14T00:00:00+00:00",
        "delivery_id": "delivery-1",
        "illust_id": "101",
        "page_index": 0,
        "output_key": "key-1",
        "size": 70,
        "sha256": "A" * 64,
    }
    for digest in ("A" * 64, "abc", "", 123, b"x" * 64):
        fields, detail = audit._row_fields(dict(base, sha256=digest))
        assert detail == "record_digest_invalid", digest
        assert fields["sha256_recorded"] is None


def test_a_broken_page_index_is_a_record_finding_not_a_crash(library):
    # artifacts.page_index is INTEGER NOT NULL with a 0..9999 CHECK, so no
    # value written through this schema can be out of range: the shipped DDL
    # already prevents it.  The reader still has to survive a drifted ledger,
    # so the refusal is proved through the reader itself and the pass is proved
    # to keep its other rows and its cursor.
    base = {
        "artifact_id": "artifact-1",
        "completed_at": "2026-11-14T00:00:00+00:00",
        "delivery_id": "delivery-1",
        "illust_id": "101",
        "page_index": 10000,
        "output_key": "key-1",
        "size": 70,
        "sha256": None,
    }
    for value in (10000, -1, "0", 1.0, None):
        fields, detail = audit._row_fields(dict(base, page_index=value))
        assert detail == "record_page_invalid", value
        assert fields["page_index"] is None
    repo, root = library
    publish(repo, root, "101", "102")
    assert len(scan(repo)["rows"]) == 2


def test_the_row_reader_keeps_a_valid_page_index():
    base = {
        "artifact_id": "artifact-1",
        "completed_at": "2026-11-14T00:00:00+00:00",
        "delivery_id": "delivery-1",
        "illust_id": "101",
        "page_index": 0,
        "output_key": "key-1",
        "size": 70,
        "sha256": None,
    }
    for value in (0, 1, 9999):
        fields, detail = audit._row_fields(dict(base, page_index=value))
        assert detail is None, value
        assert fields["page_index"] == value


def test_the_row_reader_refuses_a_row_without_a_resume_key():
    base = {
        "artifact_id": "artifact-1",
        "completed_at": "2026-11-14T00:00:00+00:00",
        "delivery_id": "delivery-1",
        "illust_id": "101",
        "page_index": 0,
        "output_key": "key-1",
        "size": 70,
        "sha256": None,
    }
    assert audit._row_fields(base)[1] is None
    for broken in (
        dict(base, artifact_id=None),
        dict(base, artifact_id=""),
        dict(base, artifact_id="x" * 129),
        dict(base, artifact_id=7),
        dict(base, completed_at=""),
        dict(base, completed_at=None),
    ):
        with pytest.raises(audit._CursorUnavailable):
            audit._row_fields(broken)


@pytest.mark.parametrize("broken_id", [None, ""])
def test_an_unnameable_row_stops_with_the_last_safe_cursor(library, broken_id):
    repo, root = library
    _, ids = publish(repo, root, "101", "102")
    rows = ordered_rows(repo, ids)
    # The catalog order stays completed_at first, so the broken row still
    # sorts last even when its id becomes the empty string.
    sql(
        repo,
        "UPDATE artifacts SET artifact_id = ? WHERE artifact_id = ?",
        (broken_id, rows[1]["artifact_id"]),
    )

    result = scan(repo)

    assert result["coverage"] == "error"
    assert result["ledger"]["state"] == "error"
    assert result["ledger"]["code"] == "cursor_unavailable"
    assert result["stats"]["stopped_at"] == "ledger_error"
    assert result["stats"]["budget_exhausted_reason"] is None
    assert len(result["rows"]) == 1
    assert result["rows"][0]["artifact_id"] == rows[0]["artifact_id"]
    expected = (rows[0]["completed_at"], rows[0]["artifact_id"])
    assert result["next_cursor"] == expected
    assert result["ledger"]["last_safe_cursor"] == list(expected)
    assert result["truncated"] is True


def test_a_ledger_that_fails_mid_pass_reports_error_coverage(library, monkeypatch):
    repo, root = library
    publish(repo, root, ARTWORK)

    def blow_up(self, after, fetch):
        raise audit.LibraryAuditError("the ledger went away")

    monkeypatch.setattr(audit._RepositoryPages, "read", blow_up, raising=True)

    result = scan(repo)

    assert result["coverage"] == "error"
    assert result["ledger"]["code"] == "ledger_unreadable"
    assert result["rows"] == []
    assert result["stats"]["stopped_at"] == "ledger_error"
    assert result["truncated"] is True


def test_an_empty_ledger_is_empty_and_not_complete(tmp_path):
    root = tmp_path / "empty-root"
    root.mkdir()
    with LibraryRepository(tmp_path / "empty.sqlite3") as repo:
        repo.bind_path_resolver(PathResolver(AppPaths(root)))

        result = audit.scan_library_artifacts(repo)

    assert result["rows"] == []
    assert result["coverage"] == "empty"
    assert result["stats"]["library_rows"] == 0
    assert result["stats"]["considered"] == 0
    assert result["stats"]["examined"] == 0
    assert result["truncated"] is False


def test_a_page_limit_stop_says_page_and_names_the_cursor(library):
    repo, root = library
    publish(repo, root, "101", "102", "103")

    result = scan(repo, limit=1)

    assert result["coverage"] == "page"
    assert result["stats"]["stopped_at"] == "page_limit"
    assert result["stats"]["library_rows"] == 3
    assert result["stats"]["considered"] == 1
    assert result["truncated"] is True
    assert len(result["rows"]) == 1


def test_a_byte_budget_stop_says_budget(library):
    repo, root = library
    publish(repo, root, "101", "102")

    result = scan(repo, budget=audit.ScanBudget(max_bytes=100))

    assert result["coverage"] == "budget"
    assert result["stats"]["stopped_at"] == "max_bytes"
    assert result["stats"]["budget_exhausted_reason"] == "max_bytes"
    assert result["rows"][-1]["reason"] == "byte_budget_exhausted"
    assert result["truncated"] is True


def test_a_cancel_says_cancelled(library):
    repo, root = library
    publish(repo, root, "101", "102")
    cancel = Cancel(after=1)

    result = scan(repo, cancel=cancel)

    assert result["coverage"] == "cancelled"
    assert result["stats"]["stopped_at"] == "cancel"
    assert result["stats"]["budget_exhausted_reason"] == "cancel_requested"
    assert len(result["rows"]) == 1


def test_unknown_reasons_count_every_unknown_row(library):
    repo, root = library
    _, ids = publish(repo, root, "101", "102")
    rows = ordered_rows(repo, ids)
    sql(
        repo,
        "UPDATE artifacts SET sha256 = NULL WHERE artifact_id = ?",
        (rows[0]["artifact_id"],),
    )

    result = scan(repo, budget=audit.ScanBudget(max_bytes=1))

    assert result["stats"]["unknown"] == 2
    assert result["stats"]["matched"] == 0
    assert result["stats"]["unknown_reasons"] == {
        "file_exceeds_budget": 1,
        "no_recorded_digest": 1,
    }
    assert "每一行都与记录一致" not in json.dumps(result["stats"])


def test_a_resumed_page_at_the_end_is_empty_and_does_not_count(library):
    repo, root = library
    publish(repo, root, ARTWORK)
    first = scan(repo, limit=1)
    assert first["coverage"] == "complete"

    result = scan(repo, after=first["next_cursor"], limit=1)

    assert result["coverage"] == "empty"
    assert result["stats"]["library_rows"] is None
    assert result["rows"] == []
    assert result["next_cursor"] == first["next_cursor"]
    assert result["truncated"] is False


def test_every_row_carries_the_detail_slots_and_stays_json_safe(library):
    repo, root = library
    publish(repo, root, ARTWORK)

    result = scan(repo)
    fact = only_row(result)

    assert fact["reason_detail"] is None
    assert fact["size_recorded_raw_type"] is None
    assert json.loads(json.dumps(result))["coverage"] == result["coverage"]
