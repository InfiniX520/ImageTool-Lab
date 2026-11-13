"""Independent R3 command-level regressions for the atomic observed-plan command.

The audit (AR4-02/R3) requires one writer unit that accepts a batch of real
observations and persists the plan describing exactly them, with the expected
binding token rechecked inside the transaction and every failure rolled back
completely.  This module exercises only the public command seam:

    ScopeEnumerator / observe_candidate (a real read of the enumerated file)
      -> resolve_observed_content
      -> prepare_observed_plan
      -> ArchiveLibraryRepository.apply_observed_plan(prepared, pairs)
      -> close / reopen -> load_archive_plan
      -> load_content_binding / validate_archive_facts

Every case is offline and synthetic: a temporary root, a migrated copy of an
empty v1 library, and real bytes read through LocalObserver.  No network,
account, production database, upload, delete or release is used.

Coverage map:

* one command accepts two files and writes plan, bindings, acceptances,
  contents and checks together, and they survive close/reopen;
* a plan whose expected acceptance was overtaken by another real acceptance is
  refused with no acceptance, check, content or plan row left behind;
* replaying the same batch re-proves the stored plan, accepts nothing again and
  moves no binding;
* a failing second item and an injected plan-insert failure roll the whole
  transaction back, and the writer keeps serving afterwards;
* the command's own preconditions (unresolved decision, duplicate path,
  incomplete coverage, foreign snapshot) refuse before any write.

Two rules keep the file honest: a refusal must be a product error
(ContractError / ArchiveMigrationError family), never a raw sqlite3 error, and
every refusal is checked against durable state, not only against a message.
An injected storage fault is the one exception: there the assertion is complete
rollback plus a working writer, because a deliberately broken database is not a
contract decision.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from core.archive_inventory import CapacityPolicy, InventoryPolicy
from core.archive_observed_plan import ObservedPlanItem, prepare_observed_plan
from core.archive_repository import ArchiveLibraryRepository
from core.archive_schema import (
    ArchiveMigrationError, load_content_binding, migrate_archive_copy,
    resolve_observed_content, validate_archive_facts,
)
from core.archive_scope import (
    CandidateSnapshot, ResolutionKind, ScopeEnumerator, ScopeRequest,
    consume_scope_pages, observe_candidate,
)
from core.library_repository import LibraryRepository
from core.local_observation import LocalObserver
from core.task_contracts import CollectionRequest, ContractError

T0 = "2026-11-13T09:01:00+00:00"
T1 = "2026-11-13T09:02:00+00:00"
T2 = "2026-11-13T09:03:00+00:00"

PRODUCT_ERRORS = (ContractError, ArchiveMigrationError)

CRITICAL_TABLES = (
    "archive_batches", "archive_plan_segments", "archive_path_bindings",
    "archive_path_acceptances", "archive_contents", "archive_assets",
    "archive_local_checks",
)


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def database(tmp_path):
    """A current-schema copy of an empty synthetic v1 library."""
    source = tmp_path / "library.sqlite3"
    with LibraryRepository(source) as repository:
        repository.submit(CollectionRequest("request-1", "synthetic-account"), "submit-1")
    migrate_archive_copy(
        source, tmp_path / "before-migration.sqlite3", temporary_copy_confirmed=True,
    )
    return source


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


@contextmanager
def repository(database: Path):
    """Open the archive command repository on this synthetic copy."""
    repo = ArchiveLibraryRepository(database, temporary_copy_confirmed=True)
    try:
        yield repo
    finally:
        repo.close()


@contextmanager
def connection(database: Path):
    """A short plain SQLite connection for reading or for fault injection."""
    conn = sqlite3.connect(str(database))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def _consume(source: ScopeEnumerator):
    pages = [source.page()]
    while pages[-1].cursor is not None:
        pages.append(source.page(pages[-1].cursor))
    receipt = consume_scope_pages(pages, source=source)
    entries = tuple(entry for page in pages for entry in page.entries)
    facts = tuple(fact for page in pages for fact in page.entry_facts)
    assert len(entries) == len(facts)
    return receipt, entries, facts


def enumerate_gallery(root: Path, *, revision: int = 1):
    return _consume(ScopeEnumerator(
        root, ScopeRequest(("gallery",), (), root_revision=revision),
    ))


def by_path(entries, facts) -> dict:
    return {entry.path.relative_path: (entry, fact) for entry, fact in zip(entries, facts)}


def observe(
    database: Path, root, entry, fact, receipt, *, run_id, check_id, at=None,
    expected=None,
):
    """Read one enumerated candidate; the expectation comes from the current binding."""
    if expected is None:
        with connection(database) as conn:
            binding = load_content_binding(conn, entry.path)
        expected = entry.content if binding is None else binding.content
    candidate = CandidateSnapshot(
        entry.path.kind, entry.path.root_id, receipt.root_revision,
        entry.path.relative_path, entry.physical_identity, entry.content.size_bytes,
        fact.mtime_ns, receipt.snapshot_id, run_id,
    )
    options = {}
    if at is not None:
        options["_clock"] = lambda: at
    observer = LocalObserver(
        root, root_id=entry.path.root_id, kind=entry.path.kind,
        root_revision=receipt.root_revision, **options,
    )
    return observe_candidate(
        candidate, observer, expected_content=expected, run_id=run_id, check_id=check_id,
    )


def resolve(database: Path, observed, **options):
    """Resolve outside the command transaction, exactly as a caller must."""
    with connection(database) as conn:
        return resolve_observed_content(conn, observed, **options)


def observed_plan(items, *, batch_id, receipt):
    """Freeze one plan from real decisions; nothing is written."""
    return prepare_observed_plan(
        items, batch_id=batch_id, receipt=receipt,
        inventory_policy=InventoryPolicy(
            target_package_bytes=1024, batch_budget_bytes=10 ** 9,
        ),
        capacity_policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
        available_additional_bytes=10 ** 9,
    )


def pairs(*items):
    return tuple(items)


def outcome(action):
    try:
        return None, action()
    except BaseException as exc:  # noqa: BLE001 - returned, never swallowed
        return exc, None


def describe(exc) -> str:
    return "%s: %s" % (type(exc).__name__, exc)


def refusal(exc, *, label):
    assert exc is not None, "%s: expected a structured refusal, the command succeeded" % label
    assert isinstance(exc, PRODUCT_ERRORS), (
        "%s: expected a product error, got %s" % (label, describe(exc))
    )
    return exc


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
    return {
        name: sorted(repr(tuple(row)) for row in conn.execute('SELECT * FROM "%s"' % name))
        for name in table_names(conn)
    }


def critical_rows(conn) -> dict:
    return {
        name: sorted(repr(tuple(row)) for row in conn.execute('SELECT * FROM "%s"' % name))
        for name in CRITICAL_TABLES
    }


def counts(conn, table: str) -> int:
    return conn.execute('SELECT COUNT(*) FROM "%s"' % table).fetchone()[0]


def binding_view(conn) -> dict:
    return {
        row[0]: (row[1], row[2], row[3])
        for row in conn.execute(
            "SELECT path_key,asset_id,revision,acceptance_id FROM archive_path_bindings"
        )
    }


def acceptance_rows(conn) -> list:
    return sorted(
        repr(tuple(row)) for row in conn.execute("SELECT * FROM archive_path_acceptances")
    )


def acceptance_facts(conn) -> dict:
    return {
        row[0]: {
            "content_key": row[1], "expected": row[2], "check_id": row[3],
            "root_revision": row[4], "device": row[5], "index": row[6],
        }
        for row in conn.execute(
            "SELECT path_key,content_key,expected_content_key,check_id,root_revision,"
            "file_device,file_index FROM archive_path_acceptances"
        )
    }


def content_rows(conn) -> dict:
    return {
        (row[0], row[1]): (row[2], row[3])
        for row in conn.execute(
            "SELECT asset_id,revision,size_bytes,sha256 FROM archive_contents"
        )
    }


def asset_rows(conn, path_key: str) -> list:
    return [
        (row[0], row[1], json.loads(row[2])["relative_path"], row[3])
        for row in conn.execute(
            "SELECT asset_id,revision,path_json,source_kind FROM archive_assets "
            "WHERE path_key=? ORDER BY revision", (path_key,),
        )
    ]


def check_ids(conn) -> set:
    return {row[0] for row in conn.execute("SELECT check_id FROM archive_local_checks")}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 1. one command accepts and saves two files
# ---------------------------------------------------------------------------


def test_r3_one_command_accepts_and_saves_a_two_file_plan(database, tmp_path):
    """Plan, bindings, acceptances, contents and checks are written together."""
    root = make_root(tmp_path)
    write(root, "gallery/a.bin", b"AAAA")
    write(root, "gallery/b.bin", b"BBBB")
    receipt, entries, facts = enumerate_gallery(root)
    files = by_path(entries, facts)
    a_entry, a_fact = files["gallery/a.bin"]
    b_entry, b_fact = files["gallery/b.bin"]
    a_observed = observe(database, root, a_entry, a_fact, receipt,
                         run_id="run-r3-1", check_id="check-r3-1", at=T0)
    b_observed = observe(database, root, b_entry, b_fact, receipt,
                         run_id="run-r3-2", check_id="check-r3-2", at=T0)
    a_decision = resolve(database, a_observed)
    b_decision = resolve(database, b_observed)
    assert a_decision.kind is ResolutionKind.BASELINE
    assert b_decision.kind is ResolutionKind.BASELINE
    prepared = observed_plan(
        (ObservedPlanItem("work-a", None, a_decision),
         ObservedPlanItem("work-b", None, b_decision)),
        batch_id="batch-r3-two", receipt=receipt,
    )
    planned = {asset.path_key: asset.content for asset in prepared.inventory.assets}
    assert set(planned) == {a_entry.path.identity_key, b_entry.path.identity_key}

    with repository(database) as repo:
        exc, value = outcome(lambda: repo.apply_observed_plan(
            prepared.prepared, pairs((a_decision, a_observed), (b_decision, b_observed)),
        ))
        assert exc is None, "AR4-02/R3: the atomic command failed: %s" % describe(exc)
        result, acceptances = value
        assert result.replayed is False
        assert len(acceptances) == 2
        assert list(acceptances) == sorted(acceptances, key=lambda item: item.path_key)
        assert {item.path_key: item.content for item in acceptances} == planned
        stored = repo.load_archive_plan("batch-r3-two")
        assert stored.inventory == prepared.inventory
        assert stored.scope_receipt is not None
        assert all(package.state == "planned" for package in stored.packages)

    # close and reopen: the same plan and the same accepted evidence
    with repository(database) as reopened:
        again = reopened.load_archive_plan("batch-r3-two")
        assert again.inventory == stored.inventory
        assert again.scope_receipt == stored.scope_receipt

    with connection(database) as conn:
        validate_archive_facts(conn)
        bindings = binding_view(conn)
        assert set(bindings) == set(planned)
        for path_key, content in planned.items():
            asset_id, revision, acceptance_id = bindings[path_key]
            assert (asset_id, revision) == (content.asset_id, content.revision)
            assert acceptance_id is not None
        assert counts(conn, "archive_path_acceptances") == 2
        assert counts(conn, "archive_local_checks") == 2
        assert counts(conn, "archive_batches") == 1
        assert counts(conn, "archive_plan_segments") >= 1
        facts_by_path = acceptance_facts(conn)
        for path_key, content in planned.items():
            row = facts_by_path[path_key]
            assert row["expected"] is None, "a first acceptance supersedes nothing"
            assert row["root_revision"] == 1
            assert row["check_id"] in {"check-r3-1", "check-r3-2"}
            assert (row["device"] is None) == (row["index"] is None)
        contents = content_rows(conn)
        for content in planned.values():
            assert contents[(content.asset_id, content.revision)] == (
                content.size_bytes, content.sha256)
            # the unknown-history generation the baseline observation was probed with
            assert contents[(content.asset_id, 0)][1] is None
        for path_key in planned:
            assert [row[3] for row in asset_rows(conn, path_key)] == [
                "observed_existing", "observed_existing",
            ]
        assert check_ids(conn) == {"check-r3-1", "check-r3-2"}


# ---------------------------------------------------------------------------
# 2. a stale plan is refused without leaving rows behind
# ---------------------------------------------------------------------------


def test_r3_a_stale_observed_plan_is_refused_without_rows(database, tmp_path):
    """An acceptance that overtakes the plan's token refuses the whole command."""
    root = make_root(tmp_path)
    target = write(root, "gallery/art.bin", b"AAAA")
    receipt, entries, facts = enumerate_gallery(root)
    entry, fact = by_path(entries, facts)["gallery/art.bin"]
    first_observed = observe(database, root, entry, fact, receipt,
                             run_id="run-r3-stale-1", check_id="check-r3-stale-1", at=T0)
    first_decision = resolve(database, first_observed)
    first_plan = observed_plan(
        (ObservedPlanItem("work-1", None, first_decision),),
        batch_id="batch-r3-stale-1", receipt=receipt,
    )
    with repository(database) as repo:
        result, acceptances = repo.apply_observed_plan(
            first_plan.prepared, pairs((first_decision, first_observed)),
        )
        assert result.replayed is False and len(acceptances) == 1
    with connection(database) as conn:
        first_acceptance = load_content_binding(conn, entry.path).acceptance_id
    assert first_acceptance is not None

    # the file changes; a plan for the new generation is frozen but not applied
    write(root, "gallery/art.bin", b"BBBB")
    bump_mtime(target)
    receipt_b, entries_b, facts_b = enumerate_gallery(root)
    entry_b, fact_b = by_path(entries_b, facts_b)["gallery/art.bin"]
    stale_observed = observe(database, root, entry_b, fact_b, receipt_b,
                             run_id="run-r3-stale-2", check_id="check-r3-stale-2", at=T1)
    stale_decision = resolve(database, stale_observed)
    assert stale_decision.kind is ResolutionKind.NEW_REVISION
    assert stale_decision.content.revision == 2
    stale_plan = observed_plan(
        (ObservedPlanItem("work-1", None, stale_decision),),
        batch_id="batch-r3-stale-2", receipt=receipt_b,
    )

    # another real acceptance (same bytes, same object) moves the token
    write(root, "gallery/art.bin", b"AAAA")
    bump_mtime(target)
    receipt_c, entries_c, facts_c = enumerate_gallery(root)
    entry_c, fact_c = by_path(entries_c, facts_c)["gallery/art.bin"]
    overtaking_observed = observe(database, root, entry_c, fact_c, receipt_c,
                                  run_id="run-r3-stale-3", check_id="check-r3-stale-3",
                                  at=T2)
    overtaking_decision = resolve(database, overtaking_observed)
    assert overtaking_decision.kind is ResolutionKind.REUSE
    with repository(database) as repo:
        accepted = repo.apply_archive_resolutions(pairs(
            (overtaking_decision, overtaking_observed),
        ))
        assert len(accepted) == 1
        assert accepted[0].acceptance_id != first_acceptance

    with connection(database) as conn:
        before_state = database_state(conn)
        before_critical = critical_rows(conn)
        before_binding = binding_view(conn)
        before_checks = check_ids(conn)

    with repository(database) as repo:
        exc, value = outcome(lambda: repo.apply_observed_plan(
            stale_plan.prepared, pairs((stale_decision, stale_observed)),
        ))
    refusal(exc, label="AR4-02/R3: a plan overtaken by a newer acceptance")

    with connection(database) as conn:
        assert database_state(conn) == before_state, (
            "AR4-02/R3: the refused command wrote acceptance, check, content or plan rows"
        )
        assert critical_rows(conn) == before_critical
        assert binding_view(conn) == before_binding
        assert check_ids(conn) == before_checks
        assert counts(conn, "archive_batches") == 1, "only the first plan may exist"
        assert counts(conn, "archive_path_acceptances") == 2
        validate_archive_facts(conn)
        current = load_content_binding(conn, entry.path)
        assert current.content.revision == 1 and current.content.sha256 == sha(b"AAAA")

    # the writer still serves: a fresh plan from a fresh observation succeeds
    receipt_d, entries_d, facts_d = enumerate_gallery(root)
    entry_d, fact_d = by_path(entries_d, facts_d)["gallery/art.bin"]
    fresh_observed = observe(database, root, entry_d, fact_d, receipt_d,
                             run_id="run-r3-stale-4", check_id="check-r3-stale-4", at=T2)
    fresh_decision = resolve(database, fresh_observed)
    assert fresh_decision.kind is ResolutionKind.REUSE
    fresh_plan = observed_plan(
        (ObservedPlanItem("work-1", None, fresh_decision),),
        batch_id="batch-r3-stale-3", receipt=receipt_d,
    )
    with repository(database) as repo:
        result, acceptances = repo.apply_observed_plan(
            fresh_plan.prepared, pairs((fresh_decision, fresh_observed)),
        )
        assert result.replayed is False and len(acceptances) == 1
    with connection(database) as conn:
        validate_archive_facts(conn)
        assert counts(conn, "archive_batches") == 2
        assert counts(conn, "archive_path_acceptances") == 3


# ---------------------------------------------------------------------------
# 3. replaying one batch accepts nothing again
# ---------------------------------------------------------------------------


def test_r3_replaying_one_batch_accepts_nothing_again(database, tmp_path):
    """A replay re-proves the stored plan; it never re-accepts or re-binds."""
    root = make_root(tmp_path)
    write(root, "gallery/one.bin", b"AAAA")
    receipt, entries, facts = enumerate_gallery(root)
    entry, fact = by_path(entries, facts)["gallery/one.bin"]
    observed = observe(database, root, entry, fact, receipt,
                       run_id="run-r3-replay-1", check_id="check-r3-replay-1", at=T0)
    decision = resolve(database, observed)
    prepared = observed_plan(
        (ObservedPlanItem("work-1", None, decision),),
        batch_id="batch-r3-replay", receipt=receipt,
    )
    with repository(database) as repo:
        first_result, first_acceptances = repo.apply_observed_plan(
            prepared.prepared, pairs((decision, observed)),
        )
        assert first_result.replayed is False
        assert len(first_acceptances) == 1

    with connection(database) as conn:
        before_state = database_state(conn)
        before_acceptances = acceptance_rows(conn)
        before_binding = binding_view(conn)
        before_checks = check_ids(conn)

    with repository(database) as repo:
        for attempt in ("second", "third"):
            exc, value = outcome(lambda: repo.apply_observed_plan(
                prepared.prepared, pairs((decision, observed)),
            ))
            assert exc is None, (
                "AR4-02/R3: %s replay raised %s" % (attempt, describe(exc))
            )
            result, acceptances = value
            assert result.replayed is True, "%s replay was not reported as a replay" % attempt
            assert acceptances == (), "%s replay accepted evidence again" % attempt
            assert result.plan.inventory == prepared.inventory
        stored = repo.load_archive_plan("batch-r3-replay")

    with connection(database) as conn:
        assert database_state(conn) == before_state, (
            "AR4-02/R3: a replay wrote rows"
        )
        assert acceptance_rows(conn) == before_acceptances
        assert binding_view(conn) == before_binding
        assert check_ids(conn) == before_checks
        validate_archive_facts(conn)
        assert counts(conn, "archive_path_acceptances") == 1
        assert stored.inventory == prepared.inventory


# ---------------------------------------------------------------------------
# 4. a failing item rolls the whole batch back
# ---------------------------------------------------------------------------


def test_r3_a_failing_item_rolls_back_the_whole_batch(database, tmp_path):
    """The first item is accepted inside the transaction, then item two fails."""
    root = make_root(tmp_path)
    write(root, "gallery/a.bin", b"AAAA")
    write(root, "gallery/b.bin", b"BBBB")
    receipt, entries, facts = enumerate_gallery(root)
    files = by_path(entries, facts)
    a_entry, a_fact = files["gallery/a.bin"]
    b_entry, b_fact = files["gallery/b.bin"]
    a_observed = observe(database, root, a_entry, a_fact, receipt,
                         run_id="run-r3-item-1", check_id="check-r3-item-1", at=T0)
    b_observed = observe(database, root, b_entry, b_fact, receipt,
                         run_id="run-r3-item-2", check_id="check-r3-item-2", at=T0)
    a_decision = resolve(database, a_observed)
    b_first = resolve(database, b_observed)
    assert a_decision.kind is ResolutionKind.BASELINE
    assert b_first.kind is ResolutionKind.BASELINE

    # b is accepted on its own, then a second real acceptance overtakes it; both
    # observations keep the snapshot the plan's receipt describes.
    with repository(database) as repo:
        assert len(repo.apply_archive_resolutions(pairs((b_first, b_observed)))) == 1
    # the decision the plan will carry (check-r3-item-3), then a *different*
    # operation (check-r3-item-4) overtakes the binding it expects
    b_reuse_observed = observe(database, root, b_entry, b_fact, receipt,
                               run_id="run-r3-item-3", check_id="check-r3-item-3", at=T1)
    b_reuse = resolve(database, b_reuse_observed)
    assert b_reuse.kind is ResolutionKind.REUSE
    b_overtake_observed = observe(database, root, b_entry, b_fact, receipt,
                                  run_id="run-r3-item-4", check_id="check-r3-item-4", at=T1)
    b_overtake = resolve(database, b_overtake_observed)
    assert b_overtake.kind is ResolutionKind.REUSE
    assert b_overtake.expected_acceptance_id == b_reuse.expected_acceptance_id
    with repository(database) as repo:
        assert len(repo.apply_archive_resolutions(pairs(
            (b_overtake, b_overtake_observed),
        ))) == 1
    prepared = observed_plan(
        (ObservedPlanItem("work-a", None, a_decision),
         ObservedPlanItem("work-b", None, b_reuse)),
        batch_id="batch-r3-item", receipt=receipt,
    )

    with connection(database) as conn:
        before_state = database_state(conn)
        before_critical = critical_rows(conn)
        a_path_key = a_entry.path.identity_key
        assert counts(conn, "archive_path_bindings") == 1, "only b is bound so far"

    with repository(database) as repo:
        exc, value = outcome(lambda: repo.apply_observed_plan(
            prepared.prepared,
            pairs((a_decision, a_observed), (b_reuse, b_reuse_observed)),
        ))
    refusal(exc, label="AR4-02/R3: a plan whose second item was overtaken")

    with connection(database) as conn:
        assert database_state(conn) == before_state, (
            "AR4-02/R3: the first item's acceptance survived a failed second item"
        )
        assert critical_rows(conn) == before_critical
        assert "check-r3-item-1" not in check_ids(conn), (
            "the first item's observation row was committed on its own"
        )
        assert asset_rows(conn, a_path_key) == [], "the first item's asset row survived"
        assert conn.execute(
            "SELECT COUNT(*) FROM archive_batches WHERE batch_id='batch-r3-item'"
        ).fetchone()[0] == 0
        validate_archive_facts(conn)

    # the writer still serves: both files are re-observed and one new plan succeeds
    receipt_c, entries_c, facts_c = enumerate_gallery(root)
    files_c = by_path(entries_c, facts_c)
    a2 = observe(database, root, *files_c["gallery/a.bin"], receipt_c,
                 run_id="run-r3-item-5", check_id="check-r3-item-5", at=T2)
    b2 = observe(database, root, *files_c["gallery/b.bin"], receipt_c,
                 run_id="run-r3-item-6", check_id="check-r3-item-6", at=T2)
    a2_decision = resolve(database, a2)
    b2_decision = resolve(database, b2)
    retry = observed_plan(
        (ObservedPlanItem("work-a", None, a2_decision),
         ObservedPlanItem("work-b", None, b2_decision)),
        batch_id="batch-r3-item-retry", receipt=receipt_c,
    )
    with repository(database) as repo:
        result, acceptances = repo.apply_observed_plan(
            retry.prepared, pairs((a2_decision, a2), (b2_decision, b2)),
        )
        assert result.replayed is False
        assert len(acceptances) == 2
    with connection(database) as conn:
        validate_archive_facts(conn)
        assert counts(conn, "archive_path_bindings") == 2
        assert counts(conn, "archive_batches") == 1


# ---------------------------------------------------------------------------
# 5. an injected plan-insert failure rolls back the acceptance too
# ---------------------------------------------------------------------------


def test_r3_an_injected_plan_insert_failure_rolls_back_the_acceptance(database, tmp_path):
    """The accepted evidence must not survive a failed plan write."""
    root = make_root(tmp_path)
    write(root, "gallery/art.bin", b"AAAA")
    receipt, entries, facts = enumerate_gallery(root)
    entry, fact = by_path(entries, facts)["gallery/art.bin"]
    observed = observe(database, root, entry, fact, receipt,
                       run_id="run-r3-fault-1", check_id="check-r3-fault-1", at=T0)
    decision = resolve(database, observed)
    prepared = observed_plan(
        (ObservedPlanItem("work-1", None, decision),),
        batch_id="batch-r3-fault", receipt=receipt,
    )
    install_plan_insert_fault(database)
    try:
        with repository(database) as repo:
            exc, value = outcome(lambda: repo.apply_observed_plan(
                prepared.prepared, pairs((decision, observed)),
            ))
            assert exc is not None, (
                "AR4-02/R3: the injected plan-insert failure did not surface"
            )
    finally:
        drop_plan_insert_fault(database)

    with connection(database) as conn:
        assert counts(conn, "archive_path_acceptances") == 0
        assert counts(conn, "archive_path_bindings") == 0
        assert counts(conn, "archive_local_checks") == 0
        assert counts(conn, "archive_contents") == 0
        assert counts(conn, "archive_assets") == 0
        assert counts(conn, "archive_batches") == 0
        assert counts(conn, "archive_plan_segments") == 0
        validate_archive_facts(conn)

    # the very same frozen command can be retried once the storage fault is gone
    with repository(database) as repo:
        result, acceptances = repo.apply_observed_plan(
            prepared.prepared, pairs((decision, observed)),
        )
        assert result.replayed is False
        assert len(acceptances) == 1
        stored = repo.load_archive_plan("batch-r3-fault")
        assert stored.inventory == prepared.inventory
    with connection(database) as conn:
        validate_archive_facts(conn)
        binding = load_content_binding(conn, entry.path)
        assert binding is not None and binding.content == decision.content
        assert binding.acceptance_id is not None
        assert counts(conn, "archive_path_acceptances") == 1


def install_plan_insert_fault(database: Path) -> None:
    with connection(database) as conn:
        conn.execute(
            "CREATE TRIGGER r3_plan_insert_fault BEFORE INSERT ON archive_batches "
            "BEGIN SELECT RAISE(ABORT, 'synthetic plan insert failure'); END"
        )
        conn.commit()


def drop_plan_insert_fault(database: Path) -> None:
    with connection(database) as conn:
        conn.execute("DROP TRIGGER r3_plan_insert_fault")
        conn.commit()


# ---------------------------------------------------------------------------
# 6. the command refuses mismatched input before writing
# ---------------------------------------------------------------------------


def test_r3_the_command_refuses_mismatched_pairs_before_writing(database, tmp_path):
    """Unresolved, duplicated, incomplete and foreign-snapshot input is refused."""
    root = make_root(tmp_path)
    write(root, "gallery/a.bin", b"AAAA")
    write(root, "gallery/b.bin", b"BBBB")
    receipt, entries, facts = enumerate_gallery(root)
    files = by_path(entries, facts)
    a_entry, a_fact = files["gallery/a.bin"]
    b_entry, b_fact = files["gallery/b.bin"]
    a_observed = observe(database, root, a_entry, a_fact, receipt,
                         run_id="run-r3-guard-1", check_id="check-r3-guard-1", at=T0)
    b_observed = observe(database, root, b_entry, b_fact, receipt,
                         run_id="run-r3-guard-2", check_id="check-r3-guard-2", at=T0)
    a_decision = resolve(database, a_observed)
    b_decision = resolve(database, b_observed)
    prepared = observed_plan(
        (ObservedPlanItem("work-a", None, a_decision),
         ObservedPlanItem("work-b", None, b_decision)),
        batch_id="batch-r3-guard", receipt=receipt,
    )

    # a is accepted on its own so a same-name replacement is a real decision
    with repository(database) as repo:
        assert len(repo.apply_archive_resolutions(pairs((a_decision, a_observed)))) == 1

    # a replaced physical object: one unresolved decision, one resolved but taken
    # from a different scope snapshot
    replace_with_new_object(root, "gallery/a.bin", b"CCCC")
    receipt_foreign, entries_foreign, facts_foreign = enumerate_gallery(root)
    a_foreign, a_foreign_fact = by_path(entries_foreign, facts_foreign)["gallery/a.bin"]
    unresolved_observed = observe(database, root, a_foreign, a_foreign_fact, receipt_foreign,
                                  run_id="run-r3-guard-3", check_id="check-r3-guard-3", at=T1)
    unresolved = resolve(database, unresolved_observed)
    assert unresolved.requires_attention, "the replaced object must need confirmation"
    foreign = resolve(database, unresolved_observed, replacement_confirmed=True)
    assert foreign.kind is ResolutionKind.REPLACEMENT
    assert foreign.candidate.snapshot_id != receipt.snapshot_id

    with connection(database) as conn:
        before_state = database_state(conn)
        before_critical = critical_rows(conn)

    cases = (
        ("one item only", pairs((a_decision, a_observed))),
        ("the same path twice", pairs((a_decision, a_observed), (a_decision, a_observed))),
        ("a decision from another snapshot", pairs((foreign, unresolved_observed))),
        ("an unresolved decision", pairs((unresolved, unresolved_observed))),
    )
    for label, items in cases:
        with repository(database) as repo:
            exc, value = outcome(lambda: repo.apply_observed_plan(prepared.prepared, items))
        refusal(exc, label="AR4-02/R3 guard (%s)" % label)
        with connection(database) as conn:
            assert database_state(conn) == before_state, (
                "AR4-02/R3 guard (%s): the refused command wrote rows" % label
            )
            assert critical_rows(conn) == before_critical

    with connection(database) as conn:
        assert counts(conn, "archive_batches") == 0
        validate_archive_facts(conn)


def replace_with_new_object(root: Path, relative: str, data: bytes) -> Path:
    """Give one path a brand new physical object under the same display name."""
    target = root.joinpath(*relative.split("/"))
    temporary = target.with_name(".r3-replacement.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, target)
    bump_mtime(target)
    return target
