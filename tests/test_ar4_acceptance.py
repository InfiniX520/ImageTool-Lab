"""Independent AR4-01..06 regressions on synthetic local copies.

Every case is offline and synthetic: a temporary root, a migrated copy of an
empty v1 library, and real bytes read through LocalObserver.  No network,
account, real gallery, production database, upload, delete or release is used.

The assertions come from the audit's own counterexamples
(docs/audits/2026-11-13-a1-acceptance/FINDINGS.md, AR4-01..06) and the pass
conditions in DEVELOPMENT_GUIDE.md (R1 expected tokens and idempotence, R2
asset-level history and aliases, R3 the only allowed plan-save boundary).  No
probe helper, hand-built ContentResolution or private product helper is reused,
so a product change cannot satisfy a case by keeping a helper alive; only these
public seams are used:

    ScopeEnumerator / ScopeRequest
      -> observe_candidate (a real read of the enumerated candidate)
      -> resolve_observed_content
      -> apply_content_resolution (caller-owned transaction)
      -> prepare_observed_plan / save_archive_plan / load_archive_plan
      -> bind_archive_path / record_path_acceptance (low-level boundary)
      -> validate_archive_facts (maintenance audit)

Two rules keep the suite honest:

* a refusal must be a *structured* product error (ContractError or
  ArchiveMigrationError); a raw sqlite3 error escaping the writer is a defect,
  not a refusal;
* every refusal is checked against durable state (all tables, and the critical
  binding/acceptance/content/asset/batch/segment rows explicitly), never only
  against an exception type or message.

Coverage map:

* AR4-01  a stale accepted event and a delayed REUSE may not move the current
  binding back to a replaced physical object.
* AR4-02  a plan prepared before a confirmed same-name replacement cannot
  advance the current binding or clear its acceptance, and the old plan stays
  readable.
* AR4-03  a replay of one accepted request adds nothing and raises nothing even
  when the delivery time differs; one check ID may not describe two different
  observations.
* AR4-04  a lagging hardlink and a renamed file continue the asset's own
  generation history instead of colliding with an occupied immutable key.
* AR4-05  one Windows directory entry keeps one archive identity through an
  ASCII or mixed-script spelling, and case folding never crosses genuinely
  different directory entries (NFC/NFD, strasse with an eszett).
* AR4-06  the low-level writers refuse a cross-path or cross-generation
  acceptance and an impossible expected generation *before* any write; naming
  the acceptance the binding already has stays a no-op with or without its
  token, while a real pointer move still needs the expected token.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.archive_inventory import CapacityPolicy, InventoryPolicy
from core.archive_observed_plan import ObservedPlanItem, prepare_observed_plan
from core.archive_plan_store import (
    load_archive_plan, save_archive_plan, save_prepared_plan_in_transaction,
)
from core.archive_schema import (
    ArchiveMigrationError, apply_content_resolution, bind_archive_path,
    load_content_binding, migrate_archive_copy, record_local_observation,
    record_path_acceptance, resolve_observed_content, validate_archive_facts,
)
from core.archive_scope import (
    CandidateSnapshot, ResolutionKind, ScopeEnumerator, ScopeRequest,
    consume_scope_pages, observe_candidate,
)
from core.library_repository import LibraryRepository
from core.local_observation import LocalObserver
from core.preservation_policy import ContentVersion
from core.task_contracts import CollectionRequest, ContractError, PathRef

TIME = "2026-11-13T08:00:00+00:00"
T0 = "2026-11-13T08:01:00+00:00"
T1 = "2026-11-13T08:02:00+00:00"
T2 = "2026-11-13T08:03:00+00:00"
T3 = "2026-11-13T08:04:00+00:00"
T4 = "2026-11-13T08:05:00+00:00"
T5 = "2026-11-13T08:06:00+00:00"

# A structured refusal is one of the product's own error families.  A raw
# sqlite3.Error is deliberately absent: the writer must translate a conflict
# into its contract instead of leaking the storage layer.
PRODUCT_ERRORS = (ContractError, ArchiveMigrationError)

CRITICAL_TABLES = (
    "archive_batches", "archive_plan_segments", "archive_path_bindings",
    "archive_path_acceptances", "archive_contents", "archive_assets",
    "archive_local_checks",
)


# ---------------------------------------------------------------------------
# fixtures and small helpers
# ---------------------------------------------------------------------------


@dataclass
class Library:
    """A migrated synthetic library plus the one writer connection under test."""

    path: Path
    conn: sqlite3.Connection

    def reopen(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


@pytest.fixture
def library(tmp_path):
    """A current-schema copy of an empty synthetic v1 library."""
    source = tmp_path / "library.sqlite3"
    with LibraryRepository(source) as repository:
        repository.submit(CollectionRequest("request-1", "synthetic-account"), "submit-1")
    migrate_archive_copy(
        source, tmp_path / "before-migration.sqlite3", temporary_copy_confirmed=True,
    )
    connection = sqlite3.connect(str(source))
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield Library(source, connection)
    finally:
        connection.close()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_root(tmp_path: Path, name: str = "root") -> Path:
    root = tmp_path / name
    (root / "gallery").mkdir(parents=True)
    return root


def write(root: Path, relative: str, data: bytes) -> Path:
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def bump_mtime(target: Path, *, delta_ns: int = 10_000_000) -> None:
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + delta_ns))


def replace_with_same_name(root: Path, relative: str, data: bytes) -> Path:
    """Give one path a brand new physical object under the same display name."""
    target = root.joinpath(*relative.split("/"))
    temporary = target.with_name(".ar4-replacement.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, target)
    bump_mtime(target)
    return target


def _consume(source: ScopeEnumerator):
    pages = [source.page()]
    while pages[-1].cursor is not None:
        pages.append(source.page(pages[-1].cursor))
    receipt = consume_scope_pages(pages, source=source)
    entries = tuple(entry for page in pages for entry in page.entries)
    facts = tuple(fact for page in pages for fact in page.entry_facts)
    assert len(entries) == len(facts)
    return receipt, entries, facts


def enumerate_gallery(root: Path, *, revision: int = 1, prefix: str = "gallery"):
    return _consume(ScopeEnumerator(
        root, ScopeRequest((prefix,), (), root_revision=revision),
    ))


def select_files(root: Path, relatives, *, revision: int = 1):
    """Enumerate explicit file selections, i.e. the real 'user picked this file' path."""
    return _consume(ScopeEnumerator(
        root, ScopeRequest((), tuple(relatives), root_revision=revision),
    ))


def select_one(root: Path, relative: str, *, revision: int = 1):
    receipt, entries, facts = select_files(root, (relative,), revision=revision)
    assert len(entries) == 1, [entry.path.relative_path for entry in entries]
    assert receipt.scope_complete
    return receipt, entries[0], facts[0]


def gallery_entry(root: Path, relative: str, *, revision: int = 1):
    receipt, entries, facts = enumerate_gallery(root, revision=revision)
    for entry, fact in zip(entries, facts):
        if entry.path.relative_path == relative:
            return receipt, entry, fact
    raise AssertionError(
        "gallery enumeration did not contain %r: %r"
        % (relative, [entry.path.relative_path for entry in entries])
    )


def observe(
    conn, root, entry, fact, receipt, *, run_id, check_id,
    expected_content=None, checked_at=None, observer_revision=None, candidate=None,
):
    """Read one enumerated candidate through the real observer."""
    if expected_content is None:
        binding = load_content_binding(conn, entry.path)
        expected_content = entry.content if binding is None else binding.content
    if candidate is None:
        candidate = CandidateSnapshot(
            entry.path.kind, entry.path.root_id, receipt.root_revision,
            entry.path.relative_path, entry.physical_identity, entry.content.size_bytes,
            fact.mtime_ns, receipt.snapshot_id, run_id,
        )
    options = {}
    if checked_at is not None:
        options["_clock"] = lambda: checked_at
    observer = LocalObserver(
        root, root_id=entry.path.root_id, kind=entry.path.kind,
        root_revision=(
            receipt.root_revision if observer_revision is None else observer_revision
        ),
        **options,
    )
    return observe_candidate(
        candidate, observer, expected_content=expected_content,
        run_id=run_id, check_id=check_id,
    )


def observe_path(
    conn, root, relative, *, run_id, check_id, checked_at=None, revision=1,
    expected_content=None,
):
    receipt, entry, fact = gallery_entry(root, relative, revision=revision)
    observed = observe(
        conn, root, entry, fact, receipt, run_id=run_id, check_id=check_id,
        checked_at=checked_at, expected_content=expected_content,
    )
    return receipt, entry, observed


def outcome(action):
    """Run one writer action; return the escaping exception or None."""
    try:
        action()
    except BaseException as exc:  # noqa: BLE001 - returned, never swallowed
        return exc
    return None


def describe(exc) -> str:
    return "%s: %s" % (type(exc).__name__, exc)


def refusal(action, *, label):
    """Return the structured refusal of one action; fail on a raw storage error."""
    exc = outcome(action)
    if exc is None:
        return None
    assert isinstance(exc, PRODUCT_ERRORS), (
        "%s: expected a structured product refusal, got %s" % (label, describe(exc))
    )
    return exc


def apply_resolution(conn, resolution, observed, *, bound_at=TIME):
    """One application inside a caller-owned transaction."""
    with conn:
        return apply_content_resolution(
            conn, resolution, observed=observed, bound_at=bound_at,
            source_kind="observed_existing",
        )


def bind(conn, observed, *, label, bound_at=TIME, **resolve_options):
    """Resolve one real observation and apply it inside a caller transaction."""
    resolution = resolve_observed_content(conn, observed, **resolve_options)
    if resolution.requires_attention:
        raise AssertionError(
            "%s: observation did not resolve to a writable decision (%s/%s)"
            % (label, resolution.kind.value, resolution.reason)
        )
    apply_resolution(conn, resolution, observed, bound_at=bound_at)
    return resolution


def require_bind(conn, observed, *, label, **options):
    """Apply and report the escaping exception instead of a bare traceback."""
    box = {}
    exc = outcome(
        lambda: box.setdefault("resolution", bind(conn, observed, label=label, **options))
    )
    assert exc is None, "%s: %s" % (label, describe(exc))
    return box["resolution"]


def record_diagnostic(conn, observed):
    """Append one diagnostic exactly as the normal diagnostic-first order does."""
    with conn:
        record_local_observation(
            conn, observed.observation.evidence, result=observed.observation,
        )


def save_prepared(conn, prepared):
    """Save one frozen plan under a caller-owned write transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = save_prepared_plan_in_transaction(conn, prepared)
        conn.execute("COMMIT")
        return result
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def observed_plan(resolution, *, batch_id, receipt, work_id="work-1", page_index=0):
    """Freeze one plan from a real resolved decision; nothing is written."""
    return prepare_observed_plan(
        (ObservedPlanItem(work_id, page_index, resolution),),
        batch_id=batch_id,
        receipt=receipt,
        inventory_policy=InventoryPolicy(
            target_package_bytes=1024, batch_budget_bytes=10 ** 9,
        ),
        capacity_policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
        available_additional_bytes=10 ** 9,
    )


# ---------------------------------------------------------------------------
# durable-state readers
# ---------------------------------------------------------------------------


def table_names(conn) -> list:
    return [
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def database_state(conn) -> dict:
    """Every table and row of this synthetic library, for exact rollback checks."""
    return {
        name: sorted(
            repr(tuple(row)) for row in conn.execute('SELECT * FROM "%s"' % name)
        )
        for name in table_names(conn)
    }


def assert_unchanged(conn, full_before, critical_before, label: str) -> None:
    """A refusal must leave the named tables and every other table identical."""
    assert critical_rows(conn) == critical_before, (
        "%s: the binding/acceptance/content/asset/batch/segment rows changed" % label
    )
    assert database_state(conn) == full_before, "%s: library rows changed" % label


def critical_rows(conn) -> dict:
    """The tables an acceptance or plan write can touch, row for row."""
    return {
        name: sorted(
            repr(tuple(row)) for row in conn.execute('SELECT * FROM "%s"' % name)
        )
        for name in CRITICAL_TABLES
    }


def pointer_rows(conn) -> dict:
    """The current pointer: path bindings (without bound_at) and acceptances."""
    return {
        "bindings": sorted(
            repr(tuple(row)) for row in conn.execute(
                "SELECT path_key,asset_id,revision,acceptance_id FROM archive_path_bindings"
            )
        ),
        "acceptances": acceptance_rows(conn),
    }


def binding_pairs(conn) -> dict:
    return {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT path_key,asset_id,revision FROM archive_path_bindings"
        )
    }


def acceptance_rows(conn) -> list:
    return sorted(
        repr(tuple(row)) for row in conn.execute(
            "SELECT * FROM archive_path_acceptances"
        )
    )


def content_rows(conn) -> dict:
    return {
        (row[0], row[1]): (row[2], row[3])
        for row in conn.execute(
            "SELECT asset_id,revision,size_bytes,sha256 FROM archive_contents"
        )
    }


def asset_owners(conn, path_key: str) -> set:
    return {
        row[0] for row in conn.execute(
            "SELECT DISTINCT asset_id FROM archive_assets WHERE path_key=?", (path_key,)
        )
    }


def stored_assets(conn, path_key: str) -> list:
    """Immutable asset rows for one archive identity, oldest revision first."""
    return [
        (row[0], row[1], row[2], row[3])
        for row in conn.execute(
            "SELECT asset_id,revision,path_json,source_kind FROM archive_assets "
            "WHERE path_key=? ORDER BY revision", (path_key,),
        )
    ]


def stored_asset_row(conn, path_key: str, revision: int):
    """One immutable asset row for this archive identity and revision."""
    row = conn.execute(
        "SELECT asset_id,revision,path_json,source_kind FROM archive_assets "
        "WHERE path_key=? AND revision=?", (path_key, revision),
    ).fetchone()
    return None if row is None else tuple(row)


def content_key_of(conn, content: ContentVersion) -> str:
    """Read the persisted content key; the test never re-derives a private key."""
    row = conn.execute(
        "SELECT content_key FROM archive_contents WHERE asset_id=? AND revision=?",
        (content.asset_id, content.revision),
    ).fetchone()
    assert row is not None, "content facts were not indexed: %r" % (content,)
    return row[0]


def check_facts(conn, check_id: str):
    row = conn.execute(
        "SELECT state,observation_status,observed_size,observed_sha256,run_id,"
        "checked_at,root_revision,file_device,file_index "
        "FROM archive_local_checks WHERE check_id=?", (check_id,),
    ).fetchone()
    if row is None:
        return None
    keys = (
        "state", "observation_status", "observed_size", "observed_sha256", "run_id",
        "checked_at", "root_revision", "file_device", "file_index",
    )
    return dict(zip(keys, row))


def list_count(conn, table: str, where: str = "", parameters=()) -> int:
    sql = 'SELECT COUNT(*) FROM "%s"' % table
    if where:
        sql += " WHERE " + where
    return conn.execute(sql, parameters).fetchone()[0]


# ---------------------------------------------------------------------------
# AR4-01: a stale accepted event is not the current binding
# ---------------------------------------------------------------------------


def test_ar4_01_a_stale_accepted_event_cannot_rewrite_the_accepted_binding(library, tmp_path):
    """The paused BBBB decision must not be applied after B/AAAA was accepted.

    Audit counterexample probes02/stale_accepted_event: the old revision-2
    decision was written successfully, advancing the binding to BBBB and
    pointing its accepted physical object back at the replaced A, while the path
    really held B with bytes AAAA.  The maintenance audit stayed green because
    every row still agreed with its own old observation.
    """
    conn = library.conn
    root = make_root(tmp_path)
    target = write(root, "gallery/art.bin", b"AAAA")
    receipt, entry, fact = gallery_entry(root, "gallery/art.bin")
    first = require_bind(
        conn,
        observe(conn, root, entry, fact, receipt,
                run_id="run-ar4-01-1", check_id="check-ar4-01-1", checked_at=T0),
        label="AR4-01 first acceptance of A",
    )
    assert first.content.revision == 1 and first.content.sha256 == sha(b"AAAA")
    a_identity = entry.physical_identity
    a_binding = load_content_binding(conn, entry.path)
    assert a_binding is not None and a_binding.physical_identity == a_identity

    # 2. A becomes BBBB in place; the observation is resolved but not committed.
    write(root, "gallery/art.bin", b"BBBB")
    bump_mtime(target)
    receipt_b, entry_b, fact_b = gallery_entry(root, "gallery/art.bin")
    assert entry_b.physical_identity == a_identity, "the in-place write changed the object"
    stale_observed = observe(
        conn, root, entry_b, fact_b, receipt_b,
        run_id="run-ar4-01-2", check_id="check-ar4-01-2", checked_at=T1,
    )
    stale = resolve_observed_content(conn, stale_observed)
    assert stale.kind is ResolutionKind.NEW_REVISION, (
        "AR4-01: the in-place change resolved as %s/%s"
        % (stale.kind.value, stale.reason)
    )
    assert stale.content.revision == 2 and stale.content.sha256 == sha(b"BBBB")

    # 3. The same name now names a different physical object B with bytes AAAA.
    replace_with_same_name(root, "gallery/art.bin", b"AAAA")
    receipt_c, entry_c, fact_c = gallery_entry(root, "gallery/art.bin")
    assert entry_c.physical_identity != a_identity
    observed_c = observe(
        conn, root, entry_c, fact_c, receipt_c,
        run_id="run-ar4-01-3", check_id="check-ar4-01-3", checked_at=T2,
    )
    pending = resolve_observed_content(conn, observed_c)
    assert pending.kind is ResolutionKind.NEEDS_CONFIRMATION
    assert pending.reason == "physical_object_replaced"
    confirmed = resolve_observed_content(conn, observed_c, replacement_confirmed=True)
    assert confirmed.kind is ResolutionKind.REPLACEMENT
    assert confirmed.content == first.content, "the replacement changed the accepted bytes"
    require_bind(
        conn, observed_c, label="AR4-01 confirmed same-name replacement",
        replacement_confirmed=True,
    )
    accepted = load_content_binding(conn, entry_c.path)
    assert accepted is not None
    assert accepted.content == first.content
    assert accepted.physical_identity == entry_c.physical_identity
    assert accepted.acceptance_id is not None
    assert accepted.acceptance_id != a_binding.acceptance_id

    before = critical_rows(conn)
    full_before = database_state(conn)
    exc = refusal(
        lambda: apply_resolution(conn, stale, stale_observed, bound_at=T3),
        label="AR4-01 stale accepted event",
    )
    assert exc is not None, (
        "AR4-01: the paused revision-2 decision for the replaced object A was "
        "applied after B/AAAA had been accepted (expected: structured refusal)"
    )
    assert_unchanged(conn, full_before, before, "AR4-01 refused stale event")
    assert load_content_binding(conn, entry_c.path) == accepted
    assert check_facts(conn, "check-ar4-01-2") is None, (
        "AR4-01: the refused stale observation was persisted as a diagnostic"
    )
    assert (first.content.asset_id, 2) not in content_rows(conn), (
        "AR4-01: the refused stale decision indexed the unaccepted revision 2"
    )
    validate_archive_facts(conn)

    # the writer is still usable for the current facts
    receipt_d, entry_d, observed_d = observe_path(
        conn, root, "gallery/art.bin",
        run_id="run-ar4-01-4", check_id="check-ar4-01-4", checked_at=T4,
    )
    again = require_bind(conn, observed_d, label="AR4-01 writer after the refusal")
    assert again.content == first.content
    after = load_content_binding(conn, entry_d.path)
    assert after is not None
    assert after.physical_identity == entry_d.physical_identity
    assert after.content == first.content
    validate_archive_facts(conn)


def test_ar4_01_a_delayed_reuse_cannot_resurrect_the_replaced_identity(library, tmp_path):
    """Replaying an already accepted REUSE must not move the pointer back to A.

    Audit counterexample probes02/delayed_reuse_resurrects_identity: the content
    key never changes, so only the accepted evidence distinguishes A from B.
    """
    conn = library.conn
    root = make_root(tmp_path)
    target = write(root, "gallery/art.bin", b"AAAA")
    receipt, entry, fact = gallery_entry(root, "gallery/art.bin")
    require_bind(
        conn,
        observe(conn, root, entry, fact, receipt,
                run_id="run-ar4-reuse-1", check_id="check-ar4-reuse-1", checked_at=T0),
        label="AR4-01 reuse baseline",
    )

    # One complete REUSE of the same physical object, accepted and delivered.
    receipt_2, entry_2, fact_2 = gallery_entry(root, "gallery/art.bin")
    observed_reuse = observe(
        conn, root, entry_2, fact_2, receipt_2,
        run_id="run-ar4-reuse-2", check_id="check-ar4-reuse-2", checked_at=T1,
    )
    reuse = resolve_observed_content(conn, observed_reuse)
    assert reuse.kind is ResolutionKind.REUSE, (
        "AR4-01: the unchanged file resolved as %s/%s" % (reuse.kind.value, reuse.reason)
    )
    apply_resolution(conn, reuse, observed_reuse, bound_at=T1)

    # The same name becomes a different physical object with the same bytes.
    a_identity = entry_2.physical_identity
    replace_with_same_name(root, "gallery/art.bin", b"AAAA")
    receipt_3, entry_3, fact_3 = gallery_entry(root, "gallery/art.bin")
    assert entry_3.physical_identity != a_identity
    observed_b = observe(
        conn, root, entry_3, fact_3, receipt_3,
        run_id="run-ar4-reuse-3", check_id="check-ar4-reuse-3", checked_at=T2,
    )
    assert resolve_observed_content(
        conn, observed_b
    ).kind is ResolutionKind.NEEDS_CONFIRMATION
    require_bind(
        conn, observed_b, label="AR4-01 same-content replacement acceptance",
        replacement_confirmed=True,
    )
    b_binding = load_content_binding(conn, entry_3.path)
    assert b_binding is not None
    assert b_binding.physical_identity == entry_3.physical_identity
    assert b_binding.physical_identity != a_identity

    before = pointer_rows(conn)
    contents_before = content_rows(conn)
    # The delayed replay of the earlier accepted REUSE, with its original
    # delivery time: a true replay may return the first result, never rebind.
    exc = outcome(lambda: apply_resolution(conn, reuse, observed_reuse, bound_at=T1))
    if exc is not None:
        assert isinstance(exc, PRODUCT_ERRORS), (
            "AR4-01: the delayed REUSE escaped as %s" % describe(exc)
        )
    assert pointer_rows(conn) == before, (
        "AR4-01: the delayed REUSE rewrote the current pointer or its history"
    )
    assert content_rows(conn) == contents_before
    after = load_content_binding(conn, entry_3.path)
    assert after == b_binding, (
        "AR4-01: the delayed REUSE moved the accepted evidence back to the "
        "replaced object: %r" % (after,)
    )
    assert after.physical_identity == entry_3.physical_identity
    validate_archive_facts(conn)

    receipt_4, entry_4, observed_4 = observe_path(
        conn, root, "gallery/art.bin",
        run_id="run-ar4-reuse-4", check_id="check-ar4-reuse-4", checked_at=T3,
    )
    require_bind(conn, observed_4, label="AR4-01 writer after the delayed replay")
    assert (
        load_content_binding(conn, entry_4.path).physical_identity
        == entry_4.physical_identity
    )
    validate_archive_facts(conn)


# ---------------------------------------------------------------------------
# AR4-02: plan saving is not a second way to advance the binding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("save_entry", ["command", "prepared"])
def test_ar4_02_a_stale_observed_plan_cannot_advance_the_binding(library, tmp_path, save_entry):
    """A plan frozen before a confirmed replacement may not move the pointer.

    Audit counterexample probes02/stale_plan_bypass: saving the old plan moved
    the binding to BBBB/revision 2 and set acceptance_id to NULL while the path
    really held B/AAAA.  R3 additionally allows a pure plan save that only
    appends an index and never touches the current acceptance; both shapes are
    accepted here, and both must leave the current binding alone.
    """
    conn = library.conn
    root = make_root(tmp_path)
    target = write(root, "gallery/art.bin", b"AAAA")
    receipt, entry, fact = gallery_entry(root, "gallery/art.bin")
    first = require_bind(
        conn,
        observe(conn, root, entry, fact, receipt,
                run_id="run-ar4-02-1", check_id="check-ar4-02-1", checked_at=T0),
        label="AR4-02 baseline",
    )
    history = observed_plan(first, batch_id="batch-ar4-02-history", receipt=receipt)
    saved = save_archive_plan(
        conn, history.inventory, history.capacity, receipt=receipt, root_revision=1,
    )
    assert not saved.replayed

    # 2. A becomes BBBB in place; the observation resolves to revision 2 and a
    #    plan is frozen from it, but nothing is written yet.
    write(root, "gallery/art.bin", b"BBBB")
    bump_mtime(target)
    receipt_b, entry_b, fact_b = gallery_entry(root, "gallery/art.bin")
    observed_b = observe(
        conn, root, entry_b, fact_b, receipt_b,
        run_id="run-ar4-02-2", check_id="check-ar4-02-2", checked_at=T1,
    )
    decision_b = resolve_observed_content(conn, observed_b)
    assert decision_b.kind is ResolutionKind.NEW_REVISION
    assert decision_b.content.revision == 2 and decision_b.content.sha256 == sha(b"BBBB")
    state_before_plan = database_state(conn)
    stale_plan = observed_plan(decision_b, batch_id="batch-ar4-02-stale", receipt=receipt_b)
    assert database_state(conn) == state_before_plan, (
        "AR4-02: preparing a plan wrote to the library"
    )

    # 3. The same name is replaced by a different object holding the old bytes.
    replace_with_same_name(root, "gallery/art.bin", b"AAAA")
    receipt_c, entry_c, fact_c = gallery_entry(root, "gallery/art.bin")
    observed_c = observe(
        conn, root, entry_c, fact_c, receipt_c,
        run_id="run-ar4-02-3", check_id="check-ar4-02-3", checked_at=T2,
    )
    assert resolve_observed_content(conn, observed_c).kind is ResolutionKind.NEEDS_CONFIRMATION
    require_bind(
        conn, observed_c, label="AR4-02 confirmed same-name replacement",
        replacement_confirmed=True,
    )
    accepted = load_content_binding(conn, entry_c.path)
    assert accepted is not None
    assert accepted.content.revision == 1 and accepted.content.sha256 == sha(b"AAAA")
    assert accepted.physical_identity == entry_c.physical_identity
    assert accepted.acceptance_id is not None

    before_pointer = pointer_rows(conn)
    before_critical = critical_rows(conn)
    full_before = database_state(conn)
    if save_entry == "command":
        exc = outcome(lambda: save_archive_plan(
            conn, stale_plan.inventory, stale_plan.capacity,
            receipt=receipt_b, root_revision=1,
        ))
    else:
        exc = outcome(lambda: save_prepared(conn, stale_plan.prepared))

    current = load_content_binding(conn, entry_c.path)
    assert current == accepted, (
        "AR4-02: saving the stale plan moved the current binding to %r "
        "(expected to stay at %r)" % (current, accepted)
    )
    if exc is not None:
        assert isinstance(exc, PRODUCT_ERRORS), (
            "AR4-02: the stale plan save escaped as %s" % describe(exc)
        )
        assert_unchanged(
            conn, full_before, before_critical,
            "AR4-02 refused plan save (%s)" % save_entry,
        )
        assert list_count(conn, "archive_batches", "batch_id=?", ("batch-ar4-02-stale",)) == 0
        assert list_count(
            conn, "archive_plan_segments", "batch_id=?", ("batch-ar4-02-stale",)
        ) == 0
        assert (first.content.asset_id, 2) not in content_rows(conn), (
            "AR4-02: the refused plan indexed the unaccepted revision 2"
        )
    else:
        # R3 also allows a pure index-only save: the plan must then be fully
        # written and readable, not a partial batch.
        stored = load_archive_plan(conn, "batch-ar4-02-stale")
        assert stored.inventory == stale_plan.inventory
        assert stored.scope_receipt is not None
    assert pointer_rows(conn) == before_pointer, (
        "AR4-02: the current pointer or its acceptance history changed"
    )
    validate_archive_facts(conn)

    old = load_archive_plan(conn, "batch-ar4-02-history")
    assert old.inventory == history.inventory, "AR4-02: the earlier plan was rewritten"
    assert old.inventory.assets[0].content.sha256 == sha(b"AAAA")

    receipt_d, entry_d, observed_d = observe_path(
        conn, root, "gallery/art.bin",
        run_id="run-ar4-02-4", check_id="check-ar4-02-4", checked_at=T3,
    )
    require_bind(conn, observed_d, label="AR4-02 writer after the plan refusal")
    assert (
        load_content_binding(conn, entry_d.path).physical_identity
        == entry_d.physical_identity
    )
    validate_archive_facts(conn)


# ---------------------------------------------------------------------------
# AR4-03: idempotence by facts, not by a fast path
# ---------------------------------------------------------------------------


def test_ar4_03_a_reuse_replay_is_idempotent_with_another_delivery_time(library, tmp_path):
    """The same resolution and ObservedFile replay without rows or an error.

    Audit counterexample probes02/reuse_replay: only the delivery time differed
    and the writer raised "Acceptance identity already has different facts",
    because the first delivery time was stored inside the deterministic
    acceptance identity.
    """
    conn = library.conn
    root = make_root(tmp_path)
    write(root, "gallery/art.bin", b"AAAA")
    receipt, entry, fact = gallery_entry(root, "gallery/art.bin")
    require_bind(
        conn,
        observe(conn, root, entry, fact, receipt,
                run_id="run-ar4-replay-1", check_id="check-ar4-replay-1", checked_at=T0),
        label="AR4-03 replay baseline",
    )
    receipt_2, entry_2, observed = observe_path(
        conn, root, "gallery/art.bin",
        run_id="run-ar4-replay-2", check_id="check-ar4-replay-2", checked_at=T1,
    )
    resolution = resolve_observed_content(conn, observed)
    assert resolution.kind is ResolutionKind.REUSE
    apply_resolution(conn, resolution, observed, bound_at=T1)
    bound = load_content_binding(conn, entry_2.path)
    assert bound is not None and bound.acceptance_id is not None

    before_binding = binding_pairs(conn)
    before_acceptances = acceptance_rows(conn)
    before_contents = content_rows(conn)
    before_checks = list_count(conn, "archive_local_checks")

    exc = outcome(lambda: apply_resolution(conn, resolution, observed, bound_at=T2))
    assert exc is None, (
        "AR4-03: replaying one accepted REUSE with another delivery time raised "
        "%s (expected: the first result, no new rows)" % describe(exc)
    )
    assert binding_pairs(conn) == before_binding
    assert acceptance_rows(conn) == before_acceptances, (
        "AR4-03: the replay appended or rewrote acceptance evidence"
    )
    assert content_rows(conn) == before_contents
    assert list_count(conn, "archive_local_checks") == before_checks, (
        "AR4-03: the replay appended a local check"
    )
    assert load_content_binding(conn, entry_2.path) == bound
    assert check_facts(conn, "check-ar4-replay-2")["checked_at"] == T1, (
        "AR4-03: the replay overwrote the first delivery time with the retry time"
    )
    validate_archive_facts(conn)


def test_ar4_03_a_fresh_check_id_is_still_accepted(library, tmp_path):
    """A new observation of the same bytes is an ordinary reuse, not a conflict."""
    conn = library.conn
    root = make_root(tmp_path)
    write(root, "gallery/art.bin", b"AAAA")
    receipt, entry, fact = gallery_entry(root, "gallery/art.bin")
    require_bind(
        conn,
        observe(conn, root, entry, fact, receipt,
                run_id="run-ar4-fresh-1", check_id="check-ar4-fresh-1", checked_at=T0),
        label="AR4-03 fresh-check baseline",
    )
    receipt_2, entry_2, observed = observe_path(
        conn, root, "gallery/art.bin",
        run_id="run-ar4-fresh-2", check_id="check-ar4-fresh-2", checked_at=T1,
    )
    reused = require_bind(conn, observed, label="AR4-03 a second observation")
    assert reused.content.revision == 1
    assert check_facts(conn, "check-ar4-fresh-2")["state"] == "hash_match"
    assert list_count(conn, "archive_path_acceptances") == 2
    assert len(binding_pairs(conn)) == 1
    validate_archive_facts(conn)


@pytest.mark.parametrize(
    "variant", ["run_id", "checked_at", "hash", "size", "identity", "root"],
)
def test_ar4_03_one_check_id_cannot_describe_two_observations(library, tmp_path, variant):
    """One check ID names one observation; different facts are a conflict.

    Audit counterexample probes02/changed_replay_facts: after a successful
    BASELINE, the same check ID with a different run ID was handed to the
    original resolution and the writer returned the old acceptance without
    comparing the new observation at all.  Every variant below must be refused
    before any write, and the stored check must keep its first facts.
    """
    conn = library.conn
    root = make_root(tmp_path)
    target = write(root, "gallery/art.bin", b"AAAA")
    receipt, entry, fact = gallery_entry(root, "gallery/art.bin")
    baseline_observed = observe(
        conn, root, entry, fact, receipt,
        run_id="run-ar4-shared-1", check_id="check-ar4-shared", checked_at=T0,
    )
    baseline = require_bind(conn, baseline_observed, label="AR4-03 shared-check baseline")
    assert baseline.content.revision == 1
    stored = check_facts(conn, "check-ar4-shared")
    assert stored["observed_sha256"] == sha(b"AAAA")
    assert stored["checked_at"] == T0
    bound = load_content_binding(conn, entry.path)

    if variant == "run_id":
        # The original candidate, the original resolution, a second real read of
        # the same bytes with the same check ID but another run.
        observed = observe(
            conn, root, entry, fact, receipt,
            run_id="run-ar4-other-1", check_id="check-ar4-shared", checked_at=T0,
            candidate=baseline_observed.candidate,
        )
        resolution = baseline
    elif variant == "checked_at":
        observed = observe(
            conn, root, entry, fact, receipt,
            run_id="run-ar4-shared-1", check_id="check-ar4-shared", checked_at=T4,
            candidate=baseline_observed.candidate,
        )
        resolution = baseline
    elif variant == "hash":
        write(root, "gallery/art.bin", b"BBBB")
        bump_mtime(target)
        receipt_2, entry_2, observed = observe_path(
            conn, root, "gallery/art.bin",
            run_id="run-ar4-shared-2", check_id="check-ar4-shared", checked_at=T1,
        )
        resolution = resolve_observed_content(conn, observed)
    elif variant == "size":
        write(root, "gallery/art.bin", b"AAAAAA")
        bump_mtime(target)
        receipt_2, entry_2, observed = observe_path(
            conn, root, "gallery/art.bin",
            run_id="run-ar4-shared-3", check_id="check-ar4-shared", checked_at=T1,
        )
        resolution = resolve_observed_content(conn, observed)
    elif variant == "identity":
        replace_with_same_name(root, "gallery/art.bin", b"AAAA")
        receipt_2, entry_2, observed = observe_path(
            conn, root, "gallery/art.bin",
            run_id="run-ar4-shared-4", check_id="check-ar4-shared", checked_at=T1,
        )
        resolution = resolve_observed_content(conn, observed, replacement_confirmed=True)
    else:
        receipt_2, entry_2, observed = observe_path(
            conn, root, "gallery/art.bin", revision=2,
            run_id="run-ar4-shared-5", check_id="check-ar4-shared", checked_at=T1,
        )
        resolution = resolve_observed_content(conn, observed, replacement_confirmed=True)

    before = database_state(conn)
    before_critical = critical_rows(conn)
    exc = refusal(
        lambda: apply_resolution(conn, resolution, observed, bound_at=T2),
        label="AR4-03 same check ID with different facts (%s)" % variant,
    )
    assert exc is not None, (
        "AR4-03 (%s): the same check ID was reused for a different observation "
        "instead of being compared and refused" % variant
    )
    assert_unchanged(
        conn, before, before_critical,
        "AR4-03 refused observation (%s)" % variant,
    )
    assert check_facts(conn, "check-ar4-shared") == stored, (
        "AR4-03 (%s): the stored check no longer names its first observation" % variant
    )
    assert load_content_binding(conn, entry.path) == bound
    validate_archive_facts(conn)


# ---------------------------------------------------------------------------
# AR4-04: asset-level history for lagging links and renames
# ---------------------------------------------------------------------------


def test_ar4_04_a_lagging_hardlink_advances_the_asset_generation(library, tmp_path):
    """A hardlink scanned late gets the asset's next free generation, not rev2.

    Audit counterexample probes02/hardlink_lag: B still pointed at revision 1, so
    its own CCCC change derived revision 2, collided with A's saved BBBB and
    raised "Content identity already has different facts" - and rescanning B
    could not recover because the lagging binding never moved.
    """
    conn = library.conn
    root = make_root(tmp_path)
    first = write(root, "gallery/a.bin", b"AAAA")
    try:
        os.link(first, root / "gallery/b.bin")
    except OSError as exc:
        pytest.skip("Synthetic hardlink unavailable: " + type(exc).__name__)

    receipt, entries, facts = enumerate_gallery(root)
    assert len(entries) == 2
    by_path = {entry.path.relative_path: (entry, fact) for entry, fact in zip(entries, facts)}
    a_entry, a_fact = by_path["gallery/a.bin"]
    b_entry, b_fact = by_path["gallery/b.bin"]
    assert a_entry.physical_identity == b_entry.physical_identity
    a_first = require_bind(
        conn,
        observe(conn, root, a_entry, a_fact, receipt,
                run_id="run-ar4-link-1", check_id="check-ar4-link-1", checked_at=T0),
        label="AR4-04 hardlink first name",
    )
    b_first = require_bind(
        conn,
        observe(conn, root, b_entry, b_fact, receipt,
                run_id="run-ar4-link-2", check_id="check-ar4-link-2", checked_at=T0),
        label="AR4-04 hardlink second name",
    )
    assert b_first.content == a_first.content
    asset_id = a_first.content.asset_id
    assert a_first.content.revision == 1 and a_first.content.sha256 == sha(b"AAAA")

    # A is rewritten in place and only A is rescanned.
    write(root, "gallery/a.bin", b"BBBB")
    bump_mtime(first)
    receipt_a, entry_a2, observed_a2 = observe_path(
        conn, root, "gallery/a.bin",
        run_id="run-ar4-link-3", check_id="check-ar4-link-3", checked_at=T1,
    )
    a_second = require_bind(conn, observed_a2, label="AR4-04 A advances first")
    assert a_second.content.revision == 2 and a_second.content.sha256 == sha(b"BBBB")
    assert a_second.content.asset_id == asset_id

    # B is rewritten in place and only B is rescanned, still bound to revision 1.
    write(root, "gallery/b.bin", b"CCCC")
    bump_mtime(first)
    receipt_b, entry_b2, observed_b2 = observe_path(
        conn, root, "gallery/b.bin",
        run_id="run-ar4-link-4", check_id="check-ar4-link-4", checked_at=T2,
    )
    before = database_state(conn)
    decision = resolve_observed_content(conn, observed_b2)
    if decision.requires_attention:
        # A structured fork decision is allowed, but it must not leave the batch
        # unable to continue: the next attempt uses the explicit confirmation the
        # resolver exposes for a decided fork.
        assert database_state(conn) == before
        decision = resolve_observed_content(conn, observed_b2, replacement_confirmed=True)
        assert not decision.requires_attention, (
            "AR4-04: the lagging hardlink only returns %s/%s; a structured fork "
            "decision must let the batch continue"
            % (decision.kind.value, decision.reason)
        )
    exc = outcome(lambda: apply_resolution(conn, decision, observed_b2, bound_at=T2))
    binding_b = load_content_binding(conn, entry_b2.path)
    assert exc is None, (
        "AR4-04: applying the lagging hardlink's next generation raised %s"
        % describe(exc)
    )
    assert binding_b is not None
    assert binding_b.content.asset_id == asset_id, (
        "AR4-04: the second hardlink name did not continue the same asset"
    )
    assert binding_b.content.revision == 3, (
        "AR4-04: the lagging path was assigned revision %d; revision 2 already "
        "holds %r" % (binding_b.content.revision, sha(b"BBBB"))
    )
    assert binding_b.content.sha256 == sha(b"CCCC")
    a_binding = load_content_binding(conn, entry_a2.path)
    assert a_binding is not None and a_binding.content.revision == 2
    assert a_binding.content.sha256 == sha(b"BBBB")
    history = {
        revision: facts for (owner, revision), facts in content_rows(conn).items()
        if owner == asset_id
    }
    assert history.get(1) == (4, sha(b"AAAA"))
    assert history.get(2) == (4, sha(b"BBBB"))
    assert history.get(3) == (4, sha(b"CCCC")), (
        "AR4-04: the asset history is %r instead of three byte generations"
        % (sorted(history),)
    )
    assert sorted(
        revision for revision, facts in history.items() if facts[1] is not None
    ) == [1, 2, 3], (
        "AR4-04: the unknown-history row was replaced or duplicated: %r"
        % (sorted(history),)
    )
    validate_archive_facts(conn)

    # rescanning B continues normally
    receipt_b2, entry_b3, observed_b3 = observe_path(
        conn, root, "gallery/b.bin",
        run_id="run-ar4-link-5", check_id="check-ar4-link-5", checked_at=T3,
    )
    again = require_bind(conn, observed_b3, label="AR4-04 rescan of the lagging path")
    assert again.content == binding_b.content, (
        "AR4-04: rescanning the lagging path moved it to revision %d"
        % again.content.revision
    )
    rescanned = load_content_binding(conn, entry_b3.path)
    assert rescanned.content == binding_b.content
    assert rescanned.physical_identity == binding_b.physical_identity
    validate_archive_facts(conn)


def test_ar4_04_a_renamed_file_keeps_its_asset_history(library, tmp_path):
    """A rename keeps the physical object, so the asset history must continue.

    Audit counterexample probes02/rename_after_edit: the renamed path had no
    binding, so the first-binding branch allocated revision 1 again and collided
    with the asset's existing revision 1 bytes.
    """
    conn = library.conn
    root = make_root(tmp_path)
    target = write(root, "gallery/a.bin", b"AAAA")
    receipt, entry, fact = gallery_entry(root, "gallery/a.bin")
    first = require_bind(
        conn,
        observe(conn, root, entry, fact, receipt,
                run_id="run-ar4-rename-1", check_id="check-ar4-rename-1", checked_at=T0),
        label="AR4-04 rename baseline",
    )
    asset_id = first.content.asset_id
    assert first.content.revision == 1 and first.content.sha256 == sha(b"AAAA")

    write(root, "gallery/a.bin", b"BBBB")
    bump_mtime(target)
    os.replace(target, target.with_name("c.bin"))
    receipt_c, entry_c, fact_c = gallery_entry(root, "gallery/c.bin")
    assert entry_c.physical_identity == entry.physical_identity, (
        "the rename changed the physical object; this case cannot be tested"
    )
    observed = observe(
        conn, root, entry_c, fact_c, receipt_c,
        run_id="run-ar4-rename-2", check_id="check-ar4-rename-2", checked_at=T1,
    )
    before = database_state(conn)
    decision = resolve_observed_content(conn, observed)
    if decision.requires_attention:
        # A structured decision is accepted, but it may not silently reassign the
        # occupied revision 1 or write anything.
        assert decision.kind is ResolutionKind.NEEDS_CONFIRMATION, (
            "AR4-04: the renamed file resolved to %s/%s"
            % (decision.kind.value, decision.reason)
        )
        assert decision.reason and decision.next_action != "none"
        assert database_state(conn) == before
        assert load_content_binding(conn, entry_c.path) is None
        assert content_rows(conn)[(asset_id, 1)][1] == sha(b"AAAA"), (
            "AR4-04: revision 1 was reassigned to the renamed bytes"
        )
        validate_archive_facts(conn)
        return

    exc = outcome(lambda: apply_resolution(conn, decision, observed, bound_at=T1))
    assert exc is None, (
        "AR4-04: applying the renamed file's decision raised %s" % describe(exc)
    )
    binding = load_content_binding(conn, entry_c.path)
    assert binding is not None
    assert binding.content.asset_id == asset_id, (
        "AR4-04: the renamed path was attributed to a different asset"
    )
    assert binding.content.revision == 2, (
        "AR4-04: the renamed path was assigned revision %d; revision 1 already "
        "holds %r" % (binding.content.revision, sha(b"AAAA"))
    )
    assert binding.content.sha256 == sha(b"BBBB")
    assert content_rows(conn)[(asset_id, 1)] == (4, sha(b"AAAA"))
    assert content_rows(conn)[(asset_id, 2)] == (4, sha(b"BBBB"))
    assert binding.physical_identity == entry_c.physical_identity
    old = load_content_binding(conn, entry.path)
    assert old is not None and old.content.revision == 1, (
        "AR4-04: the old path's binding was rewritten by the rename"
    )
    validate_archive_facts(conn)


# ---------------------------------------------------------------------------
# AR4-05: one directory entry, one identity; no global case folding
# ---------------------------------------------------------------------------


def test_ar4_05_an_ascii_case_alias_keeps_one_asset_through_the_plan(library, tmp_path):
    """A second selection of another spelling is the same asset and can be planned.

    Audit counterexample probes02/alias_plan: resolution and acceptance succeeded
    as REUSE, then plan preparation looked the asset up by its exact display
    spelling, re-inserted it as user_selected and failed with
    "Existing asset has different immutable facts".
    """
    conn = library.conn
    root = make_root(tmp_path)
    write(root, "gallery/A.bin", b"AAAA")
    receipt_upper, entry_upper, fact_upper = select_one(root, "gallery/A.bin")
    first = require_bind(
        conn,
        observe(conn, root, entry_upper, fact_upper, receipt_upper,
                run_id="run-ar4-alias-1", check_id="check-ar4-alias-1", checked_at=T0),
        label="AR4-05 alias first selection",
    )
    assert entry_upper.path.relative_path == "gallery/A.bin"
    assert entry_upper.content.sha256 is None, "enumeration must not hash content"

    receipt_lower, entry_lower, fact_lower = select_one(root, "gallery/a.bin")
    assert entry_lower.path.relative_path == "gallery/a.bin"
    if not os.path.samefile(root / "gallery" / "A.bin", root / "gallery" / "a.bin"):
        pytest.skip("The synthetic root is case-sensitive; a case alias cannot be tested")
    assert entry_lower.path.identity_key == entry_upper.path.identity_key
    assert entry_lower.physical_identity == entry_upper.physical_identity

    observed_lower = observe(
        conn, root, entry_lower, fact_lower, receipt_lower,
        run_id="run-ar4-alias-2", check_id="check-ar4-alias-2", checked_at=T1,
    )
    decision = resolve_observed_content(conn, observed_lower)
    assert decision.kind is ResolutionKind.REUSE, (
        "AR4-05: the alias spelling resolved as %s/%s"
        % (decision.kind.value, decision.reason)
    )
    require_bind(conn, observed_lower, label="AR4-05 alias acceptance")
    binding = load_content_binding(conn, entry_lower.path)
    assert binding is not None and binding.content == first.content
    assert binding.physical_identity == entry_upper.physical_identity
    assert len(binding_pairs(conn)) == 1, (
        "AR4-05: one directory entry produced several path bindings"
    )
    assert asset_owners(conn, entry_upper.path.identity_key) == {first.content.asset_id}
    rows = stored_assets(conn, entry_upper.path.identity_key)
    spellings = {json.loads(row[2])["relative_path"] for row in rows}
    assert spellings == {"gallery/A.bin"}, (
        "AR4-05: the stored rows lost the first display spelling: %r" % (spellings,)
    )
    assert {row[3] for row in rows} == {"observed_existing"}, (
        "AR4-05: the accepted rows changed their source kind"
    )
    accepted_row = stored_asset_row(
        conn, entry_upper.path.identity_key, first.content.revision,
    )
    assert accepted_row is not None and accepted_row[3] == "observed_existing"
    rows_before_save = len(rows)

    plan = observed_plan(decision, batch_id="batch-ar4-05-alias", receipt=receipt_lower)
    saved = save_archive_plan(
        conn, plan.inventory, plan.capacity, receipt=receipt_lower, root_revision=1,
    )
    assert not saved.replayed
    rows = stored_assets(conn, entry_upper.path.identity_key)
    assert len(rows) == rows_before_save, (
        "AR4-05: saving the alias plan added a second spelling of one entry"
    )
    spellings = {json.loads(row[2])["relative_path"] for row in rows}
    assert spellings == {"gallery/A.bin"}, (
        "AR4-05: saving the alias plan rewrote the stored spelling: %r" % (spellings,)
    )
    assert {row[3] for row in rows} == {"observed_existing"}, (
        "AR4-05: saving the alias plan changed the saved source kind"
    )

    conn.close()
    reopened = library.reopen()
    try:
        stored = load_archive_plan(reopened, "batch-ar4-05-alias")
        assert stored.inventory == plan.inventory
        assert stored.scope_receipt is not None
        validate_archive_facts(reopened)
        assert load_content_binding(reopened, entry_lower.path) == binding
        assert len(binding_pairs(reopened)) == 1
        rows = stored_assets(reopened, entry_upper.path.identity_key)
        assert len(rows) == rows_before_save
        assert {json.loads(row[2])["relative_path"] for row in rows} == {"gallery/A.bin"}
        assert {row[3] for row in rows} == {"observed_existing"}
    finally:
        reopened.close()


def test_ar4_05_a_mixed_script_case_alias_is_one_directory_entry(library, tmp_path):
    """A mixed-script name is normalized by its real directory entry, not casefold.

    Audit finding AR4-05: selecting the two spellings of one entry separately
    produced two identity keys, so one Windows entry became two archive assets
    even though the combined physical de-duplication passed.
    """
    conn = library.conn
    root = make_root(tmp_path)
    write(root, "gallery/画A.bin", b"AAAA")
    receipt_upper, entry_upper, fact_upper = select_one(root, "gallery/画A.bin")
    first = require_bind(
        conn,
        observe(conn, root, entry_upper, fact_upper, receipt_upper,
                run_id="run-ar4-mixed-1", check_id="check-ar4-mixed-1", checked_at=T0),
        label="AR4-05 mixed-script first selection",
    )
    assert entry_upper.path.relative_path == "gallery/画A.bin"

    receipt_lower, entry_lower, fact_lower = select_one(root, "gallery/画a.bin")
    assert entry_lower.path.relative_path == "gallery/画a.bin"
    if not os.path.samefile(root / "gallery" / "画A.bin", root / "gallery" / "画a.bin"):
        pytest.skip("The synthetic root is case-sensitive; a case alias cannot be tested")
    assert entry_lower.physical_identity == entry_upper.physical_identity
    assert entry_lower.path.identity_key == entry_upper.path.identity_key, (
        "AR4-05: two selections of one Windows directory entry produced two "
        "archive identities"
    )

    observed_lower = observe(
        conn, root, entry_lower, fact_lower, receipt_lower,
        run_id="run-ar4-mixed-2", check_id="check-ar4-mixed-2", checked_at=T1,
    )
    decision = resolve_observed_content(conn, observed_lower)
    assert decision.kind is ResolutionKind.REUSE, (
        "AR4-05: the mixed-script alias resolved as %s/%s"
        % (decision.kind.value, decision.reason)
    )
    require_bind(conn, observed_lower, label="AR4-05 mixed-script acceptance")
    assert len(binding_pairs(conn)) == 1, (
        "AR4-05: one directory entry produced several path bindings"
    )
    assert asset_owners(conn, entry_upper.path.identity_key) == {first.content.asset_id}
    assert load_content_binding(conn, entry_lower.path).content == first.content

    plan = observed_plan(decision, batch_id="batch-ar4-05-mixed", receipt=receipt_lower)
    saved = save_archive_plan(
        conn, plan.inventory, plan.capacity, receipt=receipt_lower, root_revision=1,
    )
    assert not saved.replayed
    assert load_archive_plan(conn, "batch-ar4-05-mixed").inventory == plan.inventory
    validate_archive_facts(conn)


def test_ar4_05_case_folding_never_crosses_a_real_directory_entry():
    """Folding stops where the filesystem stops: distinct names stay distinct.

    The audit explicitly allows normalizing by the real directory entry instead of
    a global Unicode casefold, but strasse-with-eszett and an NFC/NFD pair are
    different entries on this filesystem and may never share an identity.
    """
    def key(relative: str, *, kind: str = "project", root_id: str = "project") -> str:
        return PathRef(kind, root_id, relative).identity_key

    # ASCII case aliases of one Windows directory entry still fold.
    assert key("gallery/A.bin") == key("gallery/a.bin")
    # An eszett is not ss: that would be a global casefold, not a Windows alias.
    assert key("gallery/straße.bin") != key("gallery/strasse.bin"), (
        "AR4-05: a global casefold folded the eszett into ss, which is not an alias"
    )
    # NFC and NFD are different byte sequences on NTFS.
    assert key("gallery/caf\u00e9.bin") != key("gallery/cafe\u0301.bin"), (
        "AR4-05: NFC and NFD spellings were normalized into one identity"
    )
    # An external root identifier is an ASCII token and folds like one.
    assert key("a.bin", kind="external", root_id="Root1") == key(
        "a.bin", kind="external", root_id="root1"
    )


# ---------------------------------------------------------------------------
# AR4-06: public low-level writers obey their own invariants before writing
# ---------------------------------------------------------------------------


_OMITTED = object()


def bind_with_acceptance(
    conn, content, path, *, acceptance_id, expected_acceptance_id=_OMITTED, bound_at=T2,
):
    """Call the low-level binder, optionally with the expected acceptance token.

    The token argument is omitted entirely unless a case supplies it, so the
    negative calls keep the exact shape a product without the token would see,
    and the no-op controls can state both accepted call shapes explicitly.
    """
    options = {}
    if expected_acceptance_id is not _OMITTED:
        options["expected_acceptance_id"] = expected_acceptance_id
    with conn:
        bind_archive_path(
            conn, content, path, bound_at=bound_at, acceptance_id=acceptance_id,
            **options,
        )


def test_ar4_06_a_binding_cannot_borrow_another_paths_acceptance(library, tmp_path):
    """Binding B with A's acceptance ID must fail before any write.

    Audit counterexample write_audit_boundary01: the interface only checked that
    the acceptance ID existed, wrote successfully, and load_content_binding(B)
    returned A's physical identity; only a whole-library audit noticed.
    """
    conn = library.conn
    root = make_root(tmp_path)
    write(root, "gallery/a.bin", b"AAAA")
    write(root, "gallery/b.bin", b"AAAA")
    receipt, entries, facts = enumerate_gallery(root)
    by_path = {entry.path.relative_path: (entry, fact) for entry, fact in zip(entries, facts)}
    a_entry, a_fact = by_path["gallery/a.bin"]
    b_entry, b_fact = by_path["gallery/b.bin"]
    assert a_entry.physical_identity != b_entry.physical_identity
    a_content = require_bind(
        conn,
        observe(conn, root, a_entry, a_fact, receipt,
                run_id="run-ar4-borrow-1", check_id="check-ar4-borrow-1", checked_at=T0),
        label="AR4-06 borrowing A",
    ).content
    b_content = require_bind(
        conn,
        observe(conn, root, b_entry, b_fact, receipt,
                run_id="run-ar4-borrow-2", check_id="check-ar4-borrow-2", checked_at=T0),
        label="AR4-06 borrowing B",
    ).content
    assert a_content.asset_id != b_content.asset_id
    a_binding = load_content_binding(conn, a_entry.path)
    b_binding = load_content_binding(conn, b_entry.path)
    assert a_binding is not None and b_binding is not None
    assert a_binding.acceptance_id and b_binding.acceptance_id
    assert a_binding.acceptance_id != b_binding.acceptance_id

    before = database_state(conn)
    before_critical = critical_rows(conn)
    exc = refusal(
        lambda: bind_with_acceptance(
            conn, b_content, b_entry.path, acceptance_id=a_binding.acceptance_id,
        ),
        label="AR4-06 B bound with A's acceptance",
    )
    assert exc is not None, (
        "AR4-06: bind_archive_path accepted another path's acceptance ID and wrote"
    )
    assert_unchanged(conn, before, before_critical, "AR4-06 borrowed acceptance")
    after = load_content_binding(conn, b_entry.path)
    assert after == b_binding
    assert after.physical_identity == b_entry.physical_identity, (
        "AR4-06: load_content_binding(B) returned A's physical identity"
    )
    assert after.physical_identity != a_entry.physical_identity

    # The boundary is "this acceptance must describe this path", not "refuse every
    # acceptance": naming the acceptance the binding already has is a no-op, both
    # with the matching expected token and without one.  Neither call may rewrite
    # the binding row.
    no_op_before = pointer_rows(conn)
    control_with_token = outcome(lambda: bind_with_acceptance(
        conn, b_content, b_entry.path, acceptance_id=b_binding.acceptance_id,
        expected_acceptance_id=b_binding.acceptance_id,
    ))
    assert control_with_token is None, (
        "AR4-06: a no-op rebind with the matching expected acceptance token raised %s"
        % describe(control_with_token)
    )
    control_without_token = outcome(lambda: bind_with_acceptance(
        conn, b_content, b_entry.path, acceptance_id=b_binding.acceptance_id,
    ))
    assert control_without_token is None, (
        "AR4-06: renaming the current acceptance is a no-op and must not need a "
        "token; the call raised %s" % describe(control_without_token)
    )
    assert pointer_rows(conn) == no_op_before, (
        "AR4-06: a no-op rebind still rewrote the binding row"
    )
    assert load_content_binding(conn, b_entry.path) == b_binding

    # The relaxation above must not reach a real pointer move: a second, later
    # acceptance for the same bytes exists, and pointing the path back at the
    # older one without an expectation is refused and changes nothing.
    receipt_3, entry_3, observed_3 = observe_path(
        conn, root, "gallery/b.bin",
        run_id="run-ar4-borrow-3", check_id="check-ar4-borrow-3", checked_at=T1,
    )
    require_bind(conn, observed_3, label="AR4-06 B second acceptance")
    latest = load_content_binding(conn, entry_3.path)
    assert latest is not None
    assert latest.acceptance_id != b_binding.acceptance_id, (
        "AR4-06: the second observation reused the first acceptance row"
    )
    move_before = pointer_rows(conn)
    move_exc = refusal(
        lambda: bind_with_acceptance(
            conn, b_content, entry_3.path, acceptance_id=b_binding.acceptance_id,
        ),
        label="AR4-06 pointer move back to the older acceptance without a token",
    )
    assert move_exc is not None, (
        "AR4-06: a real pointer move (newer acceptance to older acceptance) was "
        "allowed without an expected acceptance ID"
    )
    assert pointer_rows(conn) == move_before, (
        "AR4-06: the refused pointer move still rewrote binding or acceptance rows"
    )
    assert load_content_binding(conn, entry_3.path) == latest
    validate_archive_facts(conn)


def test_ar4_06_a_binding_cannot_borrow_an_older_generation_acceptance(library, tmp_path):
    """The same path may not point its new generation at the old acceptance."""
    conn = library.conn
    root = make_root(tmp_path)
    target = write(root, "gallery/art.bin", b"AAAA")
    receipt, entry, fact = gallery_entry(root, "gallery/art.bin")
    require_bind(
        conn,
        observe(conn, root, entry, fact, receipt,
                run_id="run-ar4-gen-1", check_id="check-ar4-gen-1", checked_at=T0),
        label="AR4-06 generation baseline",
    )
    old_acceptance = load_content_binding(conn, entry.path)
    assert old_acceptance is not None and old_acceptance.acceptance_id
    write(root, "gallery/art.bin", b"BBBB")
    bump_mtime(target)
    receipt_2, entry_2, observed_2 = observe_path(
        conn, root, "gallery/art.bin",
        run_id="run-ar4-gen-2", check_id="check-ar4-gen-2", checked_at=T1,
    )
    second = require_bind(conn, observed_2, label="AR4-06 generation advance")
    assert second.content.revision == 2
    current = load_content_binding(conn, entry_2.path)
    assert current is not None and current.acceptance_id != old_acceptance.acceptance_id

    before = database_state(conn)
    before_critical = critical_rows(conn)
    exc = refusal(
        lambda: bind_with_acceptance(
            conn, second.content, entry_2.path,
            acceptance_id=old_acceptance.acceptance_id,
        ),
        label="AR4-06 revision 2 bound with revision 1's acceptance",
    )
    assert exc is not None, (
        "AR4-06: bind_archive_path accepted an older generation's acceptance for "
        "the new revision and wrote"
    )
    assert_unchanged(
        conn, before, before_critical, "AR4-06 older generation acceptance",
    )
    after = load_content_binding(conn, entry_2.path)
    assert after == current
    assert after.acceptance_id == current.acceptance_id
    assert after.content == second.content
    validate_archive_facts(conn)


def record_acceptance(
    conn, content, path, *, check_id, expected_content_key,
    accepted_at=T5, reason="probe_expectation",
):
    with conn:
        return record_path_acceptance(
            conn, content, path, check_id=check_id, root_revision=1,
            expected_content_key=expected_content_key,
            accepted_at=accepted_at, reason=reason,
        )


def acceptance_scene(conn, root):
    """Two generations of one path, their diagnostics, and a second asset."""
    target = write(root, "gallery/art.bin", b"AAAA")
    receipt, entry, fact = gallery_entry(root, "gallery/art.bin")
    first = require_bind(
        conn,
        observe(conn, root, entry, fact, receipt,
                run_id="run-ar4-exp-1", check_id="check-ar4-exp-1", checked_at=T0),
        label="AR4-06 expected baseline",
    )
    receipt_d1, entry_d1, fact_d1 = gallery_entry(root, "gallery/art.bin")
    diagnostic_first = observe(
        conn, root, entry_d1, fact_d1, receipt_d1,
        run_id="run-ar4-exp-2", check_id="check-ar4-exp-diag-1", checked_at=T1,
    )
    record_diagnostic(conn, diagnostic_first)
    assert check_facts(conn, "check-ar4-exp-diag-1")["state"] == "hash_match"

    write(root, "gallery/art.bin", b"BBBB")
    bump_mtime(target)
    receipt_2, entry_2, observed_2 = observe_path(
        conn, root, "gallery/art.bin",
        run_id="run-ar4-exp-3", check_id="check-ar4-exp-2", checked_at=T2,
    )
    second = require_bind(conn, observed_2, label="AR4-06 expected second generation")
    assert second.content.revision == 2 and second.content.sha256 == sha(b"BBBB")
    receipt_d2, entry_d2, fact_d2 = gallery_entry(root, "gallery/art.bin")
    diagnostic_second = observe(
        conn, root, entry_d2, fact_d2, receipt_d2,
        run_id="run-ar4-exp-4", check_id="check-ar4-exp-diag-2", checked_at=T3,
    )
    record_diagnostic(conn, diagnostic_second)
    assert check_facts(conn, "check-ar4-exp-diag-2")["state"] == "hash_match"

    write(root, "gallery/other.bin", b"CCCC")
    receipt_o, other_entry, other_fact = gallery_entry(root, "gallery/other.bin")
    other = require_bind(
        conn,
        observe(conn, root, other_entry, other_fact, receipt_o,
                run_id="run-ar4-exp-5", check_id="check-ar4-exp-3", checked_at=T3),
        label="AR4-06 expected second asset",
    )
    assert other.content.asset_id != second.content.asset_id
    return {"path": entry.path, "first": first, "second": second, "other": other}


def test_ar4_06_an_acceptance_may_expect_an_older_generation_of_its_own_asset(
    library, tmp_path,
):
    """A real older generation is still a legal expectation.

    Control for the three impossible expectations below: refusing every
    acceptance that carries an expected key would satisfy those negatives while
    breaking the audit's own rule that a replaced generation must precede it.
    """
    conn = library.conn
    root = make_root(tmp_path)
    scene = acceptance_scene(conn, root)
    expected = content_key_of(conn, scene["first"].content)
    before = list_count(conn, "archive_path_acceptances")
    exc = outcome(lambda: record_acceptance(
        conn, scene["second"].content, scene["path"],
        check_id="check-ar4-exp-diag-2", expected_content_key=expected,
    ))
    assert exc is None, (
        "AR4-06: a legitimate expected generation was refused: %s" % describe(exc)
    )
    assert list_count(conn, "archive_path_acceptances") == before + 1
    stored = conn.execute(
        "SELECT expected_content_key FROM archive_path_acceptances WHERE check_id=?",
        ("check-ar4-exp-diag-2",),
    ).fetchone()
    assert stored is not None and stored[0] == expected
    validate_archive_facts(conn)


@pytest.mark.parametrize("case", ["self", "future", "foreign_asset"])
def test_ar4_06_an_acceptance_cannot_expect_itself_a_future_generation_or_another_asset(
    library, tmp_path, case,
):
    """record_path_acceptance must share the maintenance audit's expected rule.

    Audit counterexample write_audit_boundary01: recording an acceptance whose
    expected content was its own key wrote successfully and only the whole-library
    audit then reported "Path acceptance expects a generation that cannot precede
    it".  The future-generation and other-asset cases are the same boundary.
    """
    conn = library.conn
    root = make_root(tmp_path)
    scene = acceptance_scene(conn, root)
    if case == "self":
        content = scene["second"].content
        check_id = "check-ar4-exp-diag-2"
        expected = content_key_of(conn, scene["second"].content)
        label = "its own content key"
    elif case == "future":
        content = scene["first"].content
        check_id = "check-ar4-exp-diag-1"
        expected = content_key_of(conn, scene["second"].content)
        label = "a later generation of the same asset"
    else:
        content = scene["second"].content
        check_id = "check-ar4-exp-diag-2"
        expected = content_key_of(conn, scene["other"].content)
        label = "another asset's content"

    before = database_state(conn)
    before_critical = critical_rows(conn)
    acceptances_before = acceptance_rows(conn)
    exc = refusal(
        lambda: record_acceptance(
            conn, content, scene["path"], check_id=check_id,
            expected_content_key=expected,
        ),
        label="AR4-06 acceptance expecting %s" % label,
    )
    assert exc is not None, (
        "AR4-06: record_path_acceptance stored an acceptance expecting %s" % label
    )
    assert_unchanged(
        conn, before, before_critical,
        "AR4-06 impossible acceptance (%s)" % case,
    )
    assert acceptance_rows(conn) == acceptances_before
    assert check_facts(conn, check_id) is not None, (
        "AR4-06: the diagnostic check was removed instead of being kept as evidence"
    )
    validate_archive_facts(conn)
