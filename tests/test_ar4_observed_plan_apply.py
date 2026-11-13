"""R3 counter-examples: one atomic "accept observations + save plan" command.

Offline and synthetic.  The audited defect behind this module is
docs/audits/2026-11-13-a1-acceptance/FINDINGS.md AR4-02: the plan save path advanced
a current path binding on its own (and cleared the accepted evidence) because it
compared revisions instead of an expected token, so a plan prepared from an older
observation could overwrite newer accepted state.

The contract under test:

* ``save_prepared_plan_in_transaction`` may create an index-only binding for a path
  that has none, may leave an existing binding completely untouched, and otherwise
  refuses: advancing a binding is an observation decision, not a plan decision;
* ``ArchiveLibraryRepository.apply_observed_plan`` is the one command that accepts a
  batch of resolutions and writes their plan inside a single transaction, and a
  second call for the same batch replays the plan without accepting anything again.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from core.archive_inventory import ArchiveEntry, ArtworkGroup, InventoryPolicy
from core.archive_observed_plan import ObservedPlanItem, prepare_observed_plan
from core.archive_plan_store import (
    ArchivePlanConflictError, load_archive_plan, save_archive_plan,
)
from core.archive_repository import ArchiveLibraryError, ArchiveLibraryRepository
from core.archive_schema import (
    ArchiveBindingConflictError, apply_content_resolution, bind_archive_path,
    ensure_archive_asset, load_content_binding, migrate_archive_copy,
    resolve_observed_content, validate_archive_facts,
)
from core.archive_scope import (
    CandidateSnapshot, ScopeEnumerator, ScopeRequest, consume_scope_pages,
    observe_candidate,
)
from core.library_repository import LibraryRepository
from core.local_observation import LocalObserver
from core.preservation_policy import ContentVersion
from core.task_contracts import CollectionRequest, PathRef

TIME = "2026-11-12T09:00:00+00:00"
LATER = "2099-01-01T00:00:00+00:00"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def library(tmp_path):
    source = tmp_path / "library.sqlite3"
    with LibraryRepository(source) as repository:
        repository.submit(CollectionRequest("request-1", "synthetic-account"), "submit-1")
    migrate_archive_copy(
        source, tmp_path / "before-migration.sqlite3", temporary_copy_confirmed=True,
    )
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield source, connection
    finally:
        connection.close()


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def write(root: Path, relative: str, data: bytes) -> Path:
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def bump_mtime(target: Path) -> None:
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))


def rewrite(target: Path, data: bytes) -> None:
    target.write_bytes(data)
    bump_mtime(target)


def enumerate_gallery(root: Path, *, revision: int = 1):
    source = ScopeEnumerator(
        root, ScopeRequest(("gallery",), root_revision=revision, page_size=100),
    )
    pages = [source.page()]
    while pages[-1].cursor is not None:
        pages.append(source.page(pages[-1].cursor))
    receipt = consume_scope_pages(pages, source=source)
    assert receipt.scope_complete and receipt.snapshot_verified
    entries = tuple(entry for page in pages for entry in page.entries)
    facts = tuple(fact for page in pages for fact in page.entry_facts)
    return receipt, entries, facts


def by_path(entries, facts, relative: str):
    for entry, fact in zip(entries, facts):
        if entry.path.relative_path == relative:
            return entry, fact
    raise AssertionError("candidate not enumerated: " + relative)


def observe(conn, root, entry, fact, receipt, *, run_id, check_id, expected=None):
    if expected is None:
        binding = load_content_binding(conn, entry.path)
        expected = entry.content if binding is None else binding.content
    candidate = CandidateSnapshot(
        kind=entry.path.kind, root_id=entry.path.root_id,
        root_revision=receipt.root_revision, relative_path=entry.path.relative_path,
        physical_identity=entry.physical_identity, size_bytes=entry.content.size_bytes,
        mtime_ns=fact.mtime_ns, snapshot_id=receipt.snapshot_id, run_id=run_id,
    )
    observer = LocalObserver(
        root, root_id=entry.path.root_id, kind=entry.path.kind,
        root_revision=receipt.root_revision,
    )
    return observe_candidate(
        candidate, observer, expected_content=expected, run_id=run_id, check_id=check_id,
    )


def scan_one(conn, root, relative, *, run_id, check_id, expected=None, revision=1):
    receipt, entries, facts = enumerate_gallery(root, revision=revision)
    entry, fact = by_path(entries, facts, relative)
    observed = observe(
        conn, root, entry, fact, receipt, run_id=run_id, check_id=check_id, expected=expected,
    )
    return receipt, entry, observed


def accept(conn, observed, *, confirmed=False, bound_at=TIME):
    resolution = resolve_observed_content(conn, observed, replacement_confirmed=confirmed)
    assert not resolution.requires_attention, resolution.reason
    with transaction(conn):
        return apply_content_resolution(
            conn, resolution, observed=observed, bound_at=bound_at)


def prepare_batch(conn, root, relatives, *, batch_id):
    """Enumerate, observe and resolve every relative, then freeze one plan."""
    receipt, entries, facts = enumerate_gallery(root)
    items = []
    pairs = []
    for index, relative in enumerate(relatives):
        entry, fact = by_path(entries, facts, relative)
        observed = observe(
            conn, root, entry, fact, receipt,
            run_id="run-%s-%d" % (batch_id, index),
            check_id="check-%s-%d" % (batch_id, index),
        )
        resolution = resolve_observed_content(conn, observed)
        assert not resolution.requires_attention, resolution.reason
        items.append(ObservedPlanItem("work-1", index, resolution))
        pairs.append((resolution, observed))
    plan = prepare_observed_plan(
        items, batch_id=batch_id, receipt=receipt,
        inventory_policy=InventoryPolicy(
            target_package_bytes=1024, batch_budget_bytes=10 ** 9),
        available_additional_bytes=10 ** 9,
    )
    return plan, tuple(pairs)


TABLES = (
    "archive_contents", "archive_assets", "archive_path_bindings",
    "archive_local_checks", "archive_path_acceptances", "archive_batches",
    "archive_plan_segments", "archive_packages",
)


def counts(conn):
    return {
        table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
        for table in TABLES
    }


def bindings(conn):
    return conn.execute(
        "SELECT path_key,asset_id,revision,acceptance_id FROM archive_path_bindings "
        "ORDER BY path_key").fetchall()


def state(conn):
    return {
        "counts": counts(conn),
        "bindings": bindings(conn),
        "acceptances": conn.execute(
            "SELECT acceptance_id,path_key,content_key,check_id "
            "FROM archive_path_acceptances ORDER BY acceptance_id").fetchall(),
        "checks": conn.execute(
            "SELECT check_id,path_key FROM archive_local_checks ORDER BY check_id").fetchall(),
    }


def assert_untouched(conn, before):
    assert state(conn) == before
    validate_archive_facts(conn)


def open_repository(source):
    return ArchiveLibraryRepository(source, temporary_copy_confirmed=True)


def build_plan_inventory(batch_id, entry, content):
    from core.archive_inventory import build_archive_inventory
    return build_archive_inventory(
        (ArtworkGroup("work-1", (
            ArchiveEntry(content, entry.path, 0, entry.physical_identity),
        )),),
        batch_id=batch_id,
        policy=InventoryPolicy(target_package_bytes=1024, batch_budget_bytes=10 ** 9),
    )


def capacity_of(inventory):
    from core.archive_inventory import CapacityPolicy, plan_archive_capacity
    return plan_archive_capacity(
        inventory, available_additional_bytes=10 ** 9,
        policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
    )


def test_apply_observed_plan_accepts_and_saves_in_one_command(library, tmp_path):
    conn, source = library[1], library[0]
    root = tmp_path / "apply-root"
    write(root, "gallery/a.bin", b"AAAA")
    write(root, "gallery/b.bin", b"BBBB")
    plan, pairs = prepare_batch(conn, root, ("gallery/a.bin", "gallery/b.bin"),
                                batch_id="batch-apply-one")
    assert counts(conn)["archive_path_acceptances"] == 0

    with open_repository(source) as repository:
        result, acceptances = repository.apply_observed_plan(plan.prepared, pairs)
    assert result.replayed is False
    assert [item.path_key for item in acceptances] == sorted(
        item.path_key for item in acceptances)
    assert len(acceptances) == 2

    validate_archive_facts(conn)
    for asset in plan.inventory.assets:
        binding = load_content_binding(conn, asset.path)
        assert binding is not None
        assert binding.content == asset.content
        assert binding.acceptance_id is not None
        assert binding.root_revision == 1
    reopen = sqlite3.connect(source)
    reopen.execute("PRAGMA foreign_keys=ON")
    try:
        stored = load_archive_plan(reopen, "batch-apply-one")
        assert stored.inventory == plan.inventory
        validate_archive_facts(reopen)
        rows = reopen.execute(
            "SELECT path_key,acceptance_id FROM archive_path_bindings "
            "ORDER BY path_key").fetchall()
        assert len(rows) == 2 and all(row[1] is not None for row in rows)
    finally:
        reopen.close()


def test_a_failing_item_rolls_the_whole_batch_back_and_the_writer_continues(
    library, tmp_path,
):
    conn, source = library[1], library[0]
    root = tmp_path / "rollback-root"
    write(root, "gallery/a.bin", b"AAAA")
    write(root, "gallery/b.bin", b"BBBB")
    plan, pairs = prepare_batch(conn, root, ("gallery/a.bin", "gallery/b.bin"),
                                batch_id="batch-apply-rollback")

    # Another writer accepts a different state of b.bin after the plan was frozen,
    # so the second queued resolution is stale when the command runs.
    rewrite(root / "gallery" / "b.bin", b"CCCC")
    _, _, newer = scan_one(conn, root, "gallery/b.bin",
                           run_id="run-rb-new", check_id="check-rb-new")
    accept(conn, newer, bound_at=LATER)
    before = state(conn)

    with open_repository(source) as repository:
        with pytest.raises(ArchiveBindingConflictError):
            repository.apply_observed_plan(plan.prepared, pairs)
    assert_untouched(conn, before)
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_batches WHERE batch_id=?",
        ("batch-apply-rollback",)).fetchone()[0] == 0

    # The writer still accepts a correct batch for the same path.
    fresh, fresh_pairs = prepare_batch(
        conn, root, ("gallery/a.bin", "gallery/b.bin"), batch_id="batch-apply-retry")
    with open_repository(source) as repository:
        result, _ = repository.apply_observed_plan(fresh.prepared, fresh_pairs)
    assert result.replayed is False
    validate_archive_facts(conn)


def test_a_failing_plan_save_rolls_the_acceptances_back(library, tmp_path):
    conn, source = library[1], library[0]
    root = tmp_path / "save-failure-root"
    write(root, "gallery/a.bin", b"AAAA")
    plan, pairs = prepare_batch(conn, root, ("gallery/a.bin",), batch_id="batch-save-fail")

    from core.archive_plan_store import _expected_packages
    package_id = _expected_packages(plan.inventory)[0].package_id
    # A historical package with this ID makes the plan's own save refuse, after the
    # accept step already wrote rows in the same transaction.
    with transaction(conn):
        conn.execute(
            """INSERT INTO archive_batches
               (batch_id,protocol_version,scope_json,capacity_json,state,pause_reason,created_at)
               VALUES (?,?,?,?,'planned',NULL,?)""",
            ("batch-historical", 2, "{}", "{}", TIME),
        )
        conn.execute(
            """INSERT INTO archive_packages
               (package_id,batch_id,package_revision,format,tool_version,key_ref,
                size_bytes,sha256,state) VALUES (?,?,1,'7z',NULL,NULL,NULL,NULL,'planned')""",
            (package_id, "batch-historical"),
        )
    before = state(conn)

    with open_repository(source) as repository:
        with pytest.raises(ArchivePlanConflictError):
            repository.apply_observed_plan(plan.prepared, pairs)
    assert_untouched(conn, before)
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_path_bindings").fetchone()[0] == 0
    validate_archive_facts(conn)


def test_a_replay_does_not_accept_anything_again(library, tmp_path):
    conn, source = library[1], library[0]
    root = tmp_path / "replay-root"
    write(root, "gallery/a.bin", b"AAAA")
    plan, pairs = prepare_batch(conn, root, ("gallery/a.bin",), batch_id="batch-replay")
    with open_repository(source) as repository:
        first, acceptances = repository.apply_observed_plan(plan.prepared, pairs)
    assert first.replayed is False and len(acceptances) == 1
    after_first = state(conn)

    with open_repository(source) as repository:
        second, replay_acceptances = repository.apply_observed_plan(plan.prepared, pairs)
    assert second.replayed is True
    assert replay_acceptances == ()
    assert state(conn) == after_first
    validate_archive_facts(conn)


def test_a_stale_plan_is_refused_without_writing_anything(library, tmp_path):
    conn, source = library[1], library[0]
    root = tmp_path / "stale-plan-root"
    write(root, "gallery/a.bin", b"AAAA")
    plan, pairs = prepare_batch(conn, root, ("gallery/a.bin",), batch_id="batch-stale")

    # The path is accepted under another generation before the plan is applied.
    rewrite(root / "gallery" / "a.bin", b"BBBB")
    _, _, newer = scan_one(conn, root, "gallery/a.bin",
                           run_id="run-stale-new", check_id="check-stale-new")
    accept(conn, newer, bound_at=LATER)
    before = state(conn)

    with open_repository(source) as repository:
        with pytest.raises((ArchiveBindingConflictError, ArchivePlanConflictError)):
            repository.apply_observed_plan(plan.prepared, pairs)
    assert_untouched(conn, before)
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_batches").fetchone()[0] == 0


def test_observed_items_must_cover_the_planned_paths(library, tmp_path):
    conn, source = library[1], library[0]
    root = tmp_path / "coverage-root"
    write(root, "gallery/a.bin", b"AAAA")
    write(root, "gallery/b.bin", b"BBBB")
    plan, pairs = prepare_batch(conn, root, ("gallery/a.bin", "gallery/b.bin"),
                                batch_id="batch-coverage")
    before = state(conn)

    with open_repository(source) as repository:
        with pytest.raises(Exception):
            repository.apply_observed_plan(plan.prepared, pairs[:1])
        with pytest.raises(Exception):
            repository.apply_observed_plan(plan.prepared, pairs + (pairs[0],))
    assert_untouched(conn, before)


def test_observed_items_must_come_from_the_plan_snapshot(library, tmp_path):
    conn, source = library[1], library[0]
    root = tmp_path / "snapshot-root"
    write(root, "gallery/a.bin", b"AAAA")
    plan, pairs = prepare_batch(conn, root, ("gallery/a.bin",), batch_id="batch-snapshot")

    # A brand new enumeration is another scope snapshot, even for the same file and
    # the same content; and another root generation is another scope entirely.
    receipt2, entries2, facts2 = enumerate_gallery(root)
    entry2, fact2 = by_path(entries2, facts2, "gallery/a.bin")
    observed2 = observe(conn, root, entry2, fact2, receipt2,
                        run_id="run-snap-2", check_id="check-snap-2")
    resolution2 = resolve_observed_content(conn, observed2)
    assert resolution2.content == pairs[0][0].content
    assert resolution2.candidate.snapshot_id != pairs[0][0].candidate.snapshot_id
    before = state(conn)
    with open_repository(source) as repository:
        with pytest.raises(ArchiveLibraryError, match="scope snapshot"):
            repository.apply_observed_plan(plan.prepared, ((resolution2, observed2),))
        with pytest.raises(ArchiveLibraryError, match="scope snapshot"):
            repository.apply_observed_plan(plan.prepared, pairs + ((resolution2, observed2),))
    assert_untouched(conn, before)

    # A decision observed under root generation 2 cannot enter a generation 1 plan.
    receipt3, entries3, facts3 = enumerate_gallery(root, revision=2)
    entry3, fact3 = by_path(entries3, facts3, "gallery/a.bin")
    observed3 = observe(conn, root, entry3, fact3, receipt3,
                        run_id="run-root-2", check_id="check-root-2")
    resolution3 = resolve_observed_content(conn, observed3)
    assert resolution3.candidate.root_revision == 2
    with open_repository(source) as repository:
        with pytest.raises(ArchiveLibraryError, match="scope snapshot"):
            repository.apply_observed_plan(plan.prepared, ((resolution3, observed3),))
    assert_untouched(conn, before)


def test_plan_save_cannot_advance_an_index_only_binding(library, tmp_path):
    conn = library[1]
    root = tmp_path / "index-only-root"
    target = write(root, "gallery/a.bin", b"AAAA")
    receipt, entry, observed = scan_one(conn, root, "gallery/a.bin",
                                        run_id="run-io1", check_id="check-io1")
    with transaction(conn):
        ensure_archive_asset(conn, observed.content, entry.path,
                             source_kind="user_selected")
        bind_archive_path(conn, observed.content, entry.path, bound_at=TIME)
    before = state(conn)

    higher = ContentVersion(observed.content.asset_id, 2, 4, sha(b"BBBB"))
    inventory = build_plan_inventory("batch-index-only", entry, higher)
    with pytest.raises(ArchivePlanConflictError,
                       match="cannot advance an existing path binding"):
        save_archive_plan(conn, inventory, capacity_of(inventory),
                          receipt=receipt, root_revision=1)
    assert_untouched(conn, before)
    assert target.read_bytes() == b"AAAA"


def test_plan_save_keeps_an_existing_acceptance_for_the_same_content(library, tmp_path):
    conn = library[1]
    root = tmp_path / "same-content-root"
    write(root, "gallery/a.bin", b"AAAA")
    receipt, entry, observed = scan_one(conn, root, "gallery/a.bin",
                                        run_id="run-sc1", check_id="check-sc1")
    acceptance = accept(conn, observed)
    before = state(conn)
    inventory = build_plan_inventory("batch-same-content", entry, acceptance.content)
    saved = save_archive_plan(conn, inventory, capacity_of(inventory),
                              receipt=receipt, root_revision=1)
    assert saved.replayed is False
    assert bindings(conn) == before["bindings"]
    binding = load_content_binding(conn, entry.path)
    assert binding.acceptance_id == acceptance.acceptance_id
    assert binding.root_revision == 1
    validate_archive_facts(conn)
    assert load_archive_plan(conn, "batch-same-content").inventory == inventory


def test_accepted_evidence_without_a_binding_blocks_an_index_only_bind(library, tmp_path):
    conn = library[1]
    root = tmp_path / "orphan-acceptance-root"
    write(root, "gallery/a.bin", b"AAAA")
    receipt, entry, observed = scan_one(conn, root, "gallery/a.bin",
                                        run_id="run-oa1", check_id="check-oa1")
    acceptance = accept(conn, observed)
    with transaction(conn):
        conn.execute("DELETE FROM archive_path_bindings WHERE path_key=?",
                     (entry.path.identity_key,))
    before = state(conn)
    inventory = build_plan_inventory("batch-orphan", entry, acceptance.content)
    with pytest.raises(ArchivePlanConflictError,
                       match="accepted evidence but no binding"):
        save_archive_plan(conn, inventory, capacity_of(inventory),
                          receipt=receipt, root_revision=1)
    assert_untouched(conn, before)
    assert load_content_binding(conn, entry.path) is None
