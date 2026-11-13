"""R1 counter-examples as assertions: complete binding tokens and replay facts.

Each case is synthetic and offline: temporary directories, one migrated copy of an
empty v1 library, real bytes read by LocalObserver, and no network, account,
production database or real image library.  The three audited defects behind this
module are docs/audits/2026-11-13-a1-acceptance/FINDINGS.md:

* AR4-01 - the compare-and-swap compared only the content key, so a later
  acceptance of the same content (same-name replacement, root remap) was invisible
  and a paused decision could overwrite the newer evidence;
* AR4-03 - the idempotent shortcut both refused a genuine REUSE replay (a later
  call time made it "different facts") and returned a stale acceptance for a
  reused check ID whose observation facts had changed;
* AR4-06 - the public low-level writers could persist facts the maintenance audit
  then refused: a binding could point at another path's acceptance, and an
  acceptance could name itself as the generation it replaced.

Every refusal asserts persisted state, not just exception text: the counts of
every archive table, the current binding rows, the acceptance chain, the previously
saved plan and the writer's continued usability.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from core.archive_inventory import (
    ArchiveEntry, ArtworkGroup, CapacityPolicy, InventoryPolicy,
    build_archive_inventory, plan_archive_capacity,
)
from core.archive_plan_store import (
    ArchivePlanStoreError, load_archive_plan, save_archive_plan,
)
from core.archive_repository import ArchiveLibraryRepository
from core.archive_schema import (
    UNSET, ArchiveBindingConflictError, ArchiveSchemaError, apply_content_resolution,
    bind_archive_path, ensure_archive_asset, load_content_binding, migrate_archive_copy,
    record_local_observation, record_path_acceptance, resolve_observed_content,
    validate_archive_facts, _content_key,
)
from core.archive_scope import (
    CandidateSnapshot, ContentResolution, ResolutionKind, ScopeEnumerator, ScopeRequest,
    consume_scope_pages, observe_candidate,
)
from core.library_repository import LibraryRepository
from core.local_observation import FileIdentity, LocalObserver
from core.preservation_policy import ContentVersion
from core.task_contracts import CollectionRequest, ContractError, PathRef

TIME = "2026-11-12T09:00:00+00:00"
LATER = "2099-01-01T00:00:00+00:00"
LATEST = "2099-01-01T00:00:01+00:00"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def library(tmp_path):
    """One migrated, empty schema-4 copy plus an explicit writer connection."""
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
    """A transaction the caller owns; archive writers never begin or commit one."""
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


def replace_with(root: Path, relative: str, target: Path, data: bytes) -> None:
    """Replace the directory entry itself, so the physical object changes."""
    directory = root.joinpath(*relative.split("/")).parent
    incoming = directory / (".incoming-" + str(time.time_ns()))
    incoming.write_bytes(data)
    os.replace(incoming, target)
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
    assert len(entries) == len(facts)
    return receipt, entries, facts


def by_path(receipt, entries, facts, relative: str):
    for entry, fact in zip(entries, facts):
        if entry.path.relative_path == relative:
            return entry, fact
    raise AssertionError("candidate not enumerated: " + relative)


def observe(conn, root, entry, fact, receipt, *, run_id, check_id, expected=None):
    if expected is None:
        binding = load_content_binding(conn, entry.path)
        expected = binding.content if binding is not None else entry.content
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
    entry, fact = by_path(receipt, entries, facts, relative)
    observed = observe(
        conn, root, entry, fact, receipt, run_id=run_id, check_id=check_id, expected=expected,
    )
    return receipt, entry, observed


def accept(conn, observed, *, confirmed=False, bound_at=TIME):
    """Resolve outside any transaction, then apply inside exactly one transaction."""
    resolution = resolve_observed_content(conn, observed, replacement_confirmed=confirmed)
    assert not resolution.requires_attention, resolution.reason
    with transaction(conn):
        acceptance = apply_content_resolution(
            conn, resolution, observed=observed, bound_at=bound_at,
        )
    return resolution, acceptance


def baseline(conn, root, relative, data, *, tag, revision=1):
    target = write(root, relative, data)
    _, entry, observed = scan_one(
        conn, root, relative, run_id="run-" + tag, check_id="check-" + tag,
        revision=revision,
    )
    resolution, acceptance = accept(conn, observed)
    assert resolution.kind is ResolutionKind.BASELINE
    return target, resolution, acceptance


def path_of(relative: str) -> PathRef:
    return PathRef(kind="project", root_id="project", relative_path=relative)


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


def binding_row(conn, relative):
    return conn.execute(
        "SELECT asset_id,revision,bound_at,acceptance_id FROM archive_path_bindings "
        "WHERE path_key=?",
        (path_of(relative).identity_key,),
    ).fetchone()


def acceptance_rows(conn):
    return conn.execute(
        "SELECT acceptance_id,path_key,content_key,asset_id,revision,expected_content_key,"
        "root_revision,check_id,file_device,file_index,accepted_at,reason "
        "FROM archive_path_acceptances ORDER BY acceptance_id"
    ).fetchall()


def state(conn):
    """Every persisted fact a refusal must leave untouched."""
    return {
        "counts": counts(conn),
        "bindings": conn.execute(
            "SELECT path_key,asset_id,revision,bound_at,acceptance_id "
            "FROM archive_path_bindings ORDER BY path_key"
        ).fetchall(),
        "acceptances": acceptance_rows(conn),
        "contents": conn.execute(
            "SELECT content_key,asset_id,revision,size_bytes,sha256 "
            "FROM archive_contents ORDER BY content_key"
        ).fetchall(),
    }


def save_plan_for(conn, root, relative, content, *, batch_id, revision=1):
    """Save one immutable plan for a path that already has an accepted binding."""
    receipt, entries, facts = enumerate_gallery(root, revision=revision)
    entry, _ = by_path(receipt, entries, facts, relative)
    inventory = build_archive_inventory(
        (ArtworkGroup("work-1", (
            ArchiveEntry(content, entry.path, 0, entry.physical_identity),
        )),),
        batch_id=batch_id,
        policy=InventoryPolicy(target_package_bytes=1024, batch_budget_bytes=10 ** 9),
    )
    capacity = plan_archive_capacity(
        inventory, available_additional_bytes=10 ** 9,
        policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
    )
    save_archive_plan(conn, inventory, capacity, receipt=receipt, root_revision=revision)
    return inventory


def assert_untouched(conn, before):
    assert state(conn) == before
    validate_archive_facts(conn)


# ---------------------------------------------------------------------------
# AR4-01: the expectation token covers acceptance and root, not only content.
# ---------------------------------------------------------------------------


def test_resolver_freezes_the_complete_binding_token(library, tmp_path):
    conn, root = library[1], tmp_path / "token-root"
    target, first, acceptance = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="tk1")
    held = load_content_binding(conn, path_of("gallery/a.bin"))
    assert held.acceptance_id == acceptance.acceptance_id
    assert held.root_revision == 1

    time.sleep(0.02)
    rewrite(target, b"BBBB")
    _, _, observed = scan_one(conn, root, "gallery/a.bin",
                              run_id="run-tk2", check_id="check-tk2")
    resolution = resolve_observed_content(conn, observed)
    assert resolution.kind is ResolutionKind.NEW_REVISION
    assert resolution.expected_binding_absent is False
    assert resolution.expected_acceptance_id == acceptance.acceptance_id
    assert resolution.expected_root_revision == 1

    # A first observation of an unbound path says so explicitly.  "No binding" is
    # its own value, never a None that also means "do not check".
    other = write(root, "gallery/b.bin", b"CCCC")
    _, _, unbound = scan_one(conn, root, "gallery/b.bin",
                             run_id="run-tk3", check_id="check-tk3")
    first_resolution = resolve_observed_content(conn, unbound)
    assert first_resolution.expected_binding_absent is True
    assert first_resolution.expected_acceptance_id is None
    assert first_resolution.expected_root_revision is None
    assert other.read_bytes() == b"CCCC"

    # The half-token is itself validated: absence and a token cannot coexist, and
    # an acceptance generation without its root generation is incomplete.
    with pytest.raises(ContractError):
        ContentResolution(
            first_resolution.kind, first_resolution.candidate, first_resolution.content,
            first_resolution.previous_content, first_resolution.reason,
            expected_binding_absent=True,
            expected_acceptance_id=acceptance.acceptance_id,
            expected_root_revision=1,
        )
    with pytest.raises(ContractError):
        ContentResolution(
            first_resolution.kind, first_resolution.candidate, first_resolution.content,
            first_resolution.previous_content, first_resolution.reason,
            expected_acceptance_id=acceptance.acceptance_id,
        )


def test_stale_decision_after_a_confirmed_replacement_is_refused(library, tmp_path):
    """AR4-01: A->BBBB resolved, path replaced by B/AAAA and accepted, then A's decision."""
    conn, root = library[1], tmp_path / "stale-decision-root"
    target, first, first_acceptance = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="sd1")
    inventory = save_plan_for(
        conn, root, "gallery/a.bin", first.content, batch_id="batch-stale-decision")

    # Step 2: the file changes to BBBB and a real observation resolves revision 2.
    time.sleep(0.02)
    rewrite(target, b"BBBB")
    _, _, observed = scan_one(conn, root, "gallery/a.bin",
                              run_id="run-sd2", check_id="check-sd2")
    held = resolve_observed_content(conn, observed)
    assert held.kind is ResolutionKind.NEW_REVISION and held.content.revision == 2

    # Step 3: the same name is replaced by another physical object with the old bytes.
    replace_with(root, "gallery/a.bin", target, b"AAAA")
    time.sleep(0.02)
    _, entry, observed_replace = scan_one(conn, root, "gallery/a.bin",
                                          run_id="run-sd3", check_id="check-sd3")
    pending = resolve_observed_content(conn, observed_replace)
    assert pending.kind is ResolutionKind.NEEDS_CONFIRMATION
    resolution, confirmed = accept(conn, observed_replace, confirmed=True)
    assert resolution.kind is ResolutionKind.REPLACEMENT
    assert resolution.content == first.content  # same bytes, same generation
    assert confirmed.acceptance_id != first_acceptance.acceptance_id
    after_replacement = state(conn)
    replaced_binding = binding_row(conn, "gallery/a.bin")
    assert replaced_binding[3] == confirmed.acceptance_id

    # Step 4: the old decision is applied afterwards.  The content key still
    # matches, so only the acceptance half of the token can refuse it.
    with pytest.raises(ArchiveBindingConflictError) as caught:
        with transaction(conn):
            apply_content_resolution(conn, held, observed=observed, bound_at=LATER)
    assert caught.value.reason == "expected_acceptance_changed"
    assert caught.value.expected_content_key == _content_key(first.content)
    assert caught.value.actual_content_key == _content_key(first.content)
    assert_untouched(conn, after_replacement)
    assert binding_row(conn, "gallery/a.bin") == replaced_binding
    assert load_archive_plan(conn, "batch-stale-decision").inventory == inventory

    # The writer is still usable: the file now really is the accepted object, so a
    # fresh observation is a legible REUSE with a new check, not a stale decision.
    _, _, fresh_observed = scan_one(conn, root, "gallery/a.bin",
                                    run_id="run-sd4", check_id="check-sd4")
    fresh = resolve_observed_content(conn, fresh_observed)
    assert fresh.kind is ResolutionKind.REUSE
    assert fresh.content == first.content
    _, fresh_acceptance = accept(conn, fresh_observed)
    assert fresh_acceptance.acceptance_id not in {
        first_acceptance.acceptance_id, confirmed.acceptance_id}
    validate_archive_facts(conn)
    assert counts(conn)["archive_batches"] == 1


def test_delayed_reuse_cannot_resurrect_the_replaced_physical_object(library, tmp_path):
    """A held REUSE must never move the accepted physical identity back."""
    conn, root = library[1], tmp_path / "delayed-reuse-root"
    target, first, first_acceptance = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="dr1")

    # A same-content REUSE decision is resolved but not committed yet.
    _, _, held_observed = scan_one(conn, root, "gallery/a.bin",
                                   run_id="run-dr2", check_id="check-dr2")
    held = resolve_observed_content(conn, held_observed)
    assert held.kind is ResolutionKind.REUSE
    assert held.expected_acceptance_id == first_acceptance.acceptance_id

    # Another writer replaces the object with identical bytes and confirms it.
    replace_with(root, "gallery/a.bin", target, b"AAAA")
    time.sleep(0.02)
    _, _, replaced_observed = scan_one(conn, root, "gallery/a.bin",
                                       run_id="run-dr3", check_id="check-dr3")
    replacement, second_acceptance = accept(conn, replaced_observed, confirmed=True)
    assert replacement.kind is ResolutionKind.REPLACEMENT
    after_replacement = state(conn)
    assert binding_row(conn, "gallery/a.bin")[3] == second_acceptance.acceptance_id

    with pytest.raises(ArchiveBindingConflictError) as caught:
        with transaction(conn):
            apply_content_resolution(conn, held, observed=held_observed, bound_at=LATER)
    assert caught.value.reason == "expected_acceptance_changed"
    assert_untouched(conn, after_replacement)
    binding = load_content_binding(conn, path_of("gallery/a.bin"))
    assert binding.acceptance_id == second_acceptance.acceptance_id
    assert binding.physical_identity == replaced_observed.observed_identity
    assert binding.physical_identity != held_observed.observed_identity


def test_reuse_applied_then_superseded_cannot_be_replayed_over_newer_evidence(
    library, tmp_path,
):
    """The same REUSE succeeds once; replaying it later is a defined refusal."""
    conn, root = library[1], tmp_path / "reuse-superseded-root"
    target, first, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="rs1")
    _, _, reuse_observed = scan_one(conn, root, "gallery/a.bin",
                                    run_id="run-rs2", check_id="check-rs2")
    reuse_resolution, reuse_acceptance = accept(conn, reuse_observed)
    assert reuse_resolution.kind is ResolutionKind.REUSE

    replace_with(root, "gallery/a.bin", target, b"AAAA")
    time.sleep(0.02)
    _, _, replaced_observed = scan_one(conn, root, "gallery/a.bin",
                                       run_id="run-rs3", check_id="check-rs3")
    _, second_acceptance = accept(conn, replaced_observed, confirmed=True)
    after_replacement = state(conn)
    assert binding_row(conn, "gallery/a.bin")[3] == second_acceptance.acceptance_id

    with pytest.raises(ArchiveBindingConflictError) as caught:
        with transaction(conn):
            apply_content_resolution(
                conn, reuse_resolution, observed=reuse_observed, bound_at=LATEST)
    # The acceptance row for this exact operation still exists, so the refusal
    # names the stale expectation instead of pretending the operation is new.
    assert caught.value.reason == "expected_acceptance_changed"
    assert_untouched(conn, after_replacement)
    assert any(
        row[0] == reuse_acceptance.acceptance_id for row in acceptance_rows(conn))


def test_observation_from_an_older_root_generation_cannot_rewrite_the_binding(
    library, tmp_path,
):
    """A stale observation is refused instead of moving the accepted root back."""
    conn, root = library[1], tmp_path / "root-stale-root"
    target, first, first_acceptance = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="rt1")

    # A delayed observation is taken while the root is still generation 1.
    _, _, old_observed = scan_one(conn, root, "gallery/a.bin",
                                  run_id="run-rt2", check_id="check-rt2", revision=1)

    # The same path is re-accepted under root generation 2 before that observation
    # is resolved.
    _, _, new_observed = scan_one(conn, root, "gallery/a.bin",
                                  run_id="run-rt3", check_id="check-rt3", revision=2)
    accept(conn, new_observed)
    assert load_content_binding(conn, path_of("gallery/a.bin")).root_revision == 2
    after_remap = state(conn)

    # Resolving the old observation now reads the current (generation 2) binding,
    # but the observation itself still belongs to generation 1.
    stale = resolve_observed_content(conn, old_observed)
    assert stale.expected_root_revision == 2
    with pytest.raises(ArchiveBindingConflictError) as caught:
        with transaction(conn):
            apply_content_resolution(conn, stale, observed=old_observed, bound_at=LATER)
    assert caught.value.reason == "observation_root_precedes_binding"
    assert_untouched(conn, after_remap)
    assert load_content_binding(conn, path_of("gallery/a.bin")).root_revision == 2


def test_decision_resolved_before_a_competing_root_acceptance_is_refused(library, tmp_path):
    """The token covers the root half through the acceptance it belongs to."""
    conn, root = library[1], tmp_path / "root-competing-root"
    target, first, first_acceptance = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="rc1")
    _, _, observed = scan_one(conn, root, "gallery/a.bin",
                              run_id="run-rc2", check_id="check-rc2", revision=1)
    held = resolve_observed_content(conn, observed)
    assert held.expected_root_revision == 1

    _, _, remapped = scan_one(conn, root, "gallery/a.bin",
                              run_id="run-rc3", check_id="check-rc3", revision=2)
    _, second_acceptance = accept(conn, remapped)
    after_remap = state(conn)
    assert binding_row(conn, "gallery/a.bin")[3] == second_acceptance.acceptance_id

    with pytest.raises(ArchiveBindingConflictError) as caught:
        with transaction(conn):
            apply_content_resolution(conn, held, observed=observed, bound_at=LATER)
    assert caught.value.reason == "expected_acceptance_changed"
    assert_untouched(conn, after_remap)
    assert load_content_binding(conn, path_of("gallery/a.bin")).root_revision == 2


# ---------------------------------------------------------------------------
# AR4-03: replay only when every fact agrees; otherwise a structured refusal.
# ---------------------------------------------------------------------------


def _replay(conn, resolution, observed, *, bound_at):
    with transaction(conn):
        return apply_content_resolution(
            conn, resolution, observed=observed, bound_at=bound_at)


def test_every_resolution_kind_replays_its_first_acceptance(library, tmp_path):
    conn, root = library[1], tmp_path / "replay-kinds-root"
    target = write(root, "gallery/a.bin", b"AAAA")
    _, _, observed = scan_one(conn, root, "gallery/a.bin",
                              run_id="run-rk1", check_id="check-rk1")
    resolution, acceptance = accept(conn, observed)
    assert resolution.kind is ResolutionKind.BASELINE
    before = counts(conn)
    replay = _replay(conn, resolution, observed, bound_at=LATER)
    assert replay.acceptance_id == acceptance.acceptance_id
    assert replay.accepted_at == acceptance.accepted_at == TIME
    assert counts(conn) == before

    # REUSE: the audited false refusal.  Only the call time differs.
    _, _, reuse_observed = scan_one(conn, root, "gallery/a.bin",
                                    run_id="run-rk2", check_id="check-rk2")
    reuse_resolution, reuse_acceptance = accept(conn, reuse_observed)
    assert reuse_resolution.kind is ResolutionKind.REUSE
    before = counts(conn)
    replay = _replay(conn, reuse_resolution, reuse_observed, bound_at=LATER)
    assert replay.acceptance_id == reuse_acceptance.acceptance_id
    assert replay.accepted_at == reuse_acceptance.accepted_at
    assert counts(conn) == before

    # NEW_REVISION
    time.sleep(0.02)
    rewrite(target, b"BBBB")
    _, _, changed_observed = scan_one(conn, root, "gallery/a.bin",
                                      run_id="run-rk3", check_id="check-rk3")
    changed_resolution, changed_acceptance = accept(conn, changed_observed)
    assert changed_resolution.kind is ResolutionKind.NEW_REVISION
    before = counts(conn)
    replayed = _replay(conn, changed_resolution, changed_observed, bound_at=LATER)
    assert replayed.acceptance_id == changed_acceptance.acceptance_id
    assert replayed.accepted_at == changed_acceptance.accepted_at
    assert counts(conn) == before

    # REPLACEMENT that keeps the bytes
    replace_with(root, "gallery/a.bin", target, b"BBBB")
    time.sleep(0.02)
    _, _, same_bytes_observed = scan_one(conn, root, "gallery/a.bin",
                                         run_id="run-rk4", check_id="check-rk4")
    same_bytes_resolution, same_bytes_acceptance = accept(
        conn, same_bytes_observed, confirmed=True)
    assert same_bytes_resolution.kind is ResolutionKind.REPLACEMENT
    before = counts(conn)
    replayed = _replay(conn, same_bytes_resolution, same_bytes_observed, bound_at=LATEST)
    assert replayed.acceptance_id == same_bytes_acceptance.acceptance_id
    assert replayed.accepted_at == same_bytes_acceptance.accepted_at
    assert counts(conn) == before

    # REPLACEMENT that changes the bytes
    replace_with(root, "gallery/a.bin", target, b"CCCC")
    time.sleep(0.02)
    _, _, new_bytes_observed = scan_one(conn, root, "gallery/a.bin",
                                        run_id="run-rk5", check_id="check-rk5")
    new_bytes_resolution, new_bytes_acceptance = accept(
        conn, new_bytes_observed, confirmed=True)
    assert new_bytes_resolution.kind is ResolutionKind.REPLACEMENT
    before = counts(conn)
    replayed = _replay(conn, new_bytes_resolution, new_bytes_observed, bound_at=LATEST)
    assert replayed.acceptance_id == new_bytes_acceptance.acceptance_id
    assert counts(conn) == before

    validate_archive_facts(conn)


def _mutate_run(observation):
    return replace(observation, evidence=replace(observation.evidence, run_id="run-other"))


def _mutate_time(observation):
    return replace(observation, evidence=replace(observation.evidence, checked_at=LATER))


def _mutate_hash(observation):
    return replace(
        observation,
        evidence=replace(observation.evidence, observed_sha256=sha(b"other bytes")),
    )


def _mutate_size(observation):
    return replace(
        observation, size_bytes=observation.size_bytes + 1,
        evidence=replace(observation.evidence, observed_size=observation.size_bytes + 1),
    )


def _mutate_identity(observation):
    identity = observation.identity
    return replace(observation, identity=FileIdentity(identity.device, identity.file_index + 7))


@pytest.mark.parametrize(
    "mutate", (_mutate_run, _mutate_time, _mutate_hash, _mutate_size, _mutate_identity),
    ids=("run", "time", "hash", "size", "identity"),
)
def test_same_check_id_with_different_observation_facts_is_refused(
    library, tmp_path, mutate,
):
    """A reused operation ID may not describe different observation facts."""
    conn, root = library[1], tmp_path / "same-check-root"
    target, first, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="sf1")
    time.sleep(0.02)
    rewrite(target, b"BBBB")
    _, entry, observed = scan_one(conn, root, "gallery/a.bin",
                                  run_id="run-sf2", check_id="check-sf2")
    # The diagnostic is stored first, as a real "record then confirm" flow does.
    with transaction(conn):
        record_local_observation(conn, observed.observation.evidence, result=observed.observation)
    resolution = resolve_observed_content(conn, observed)
    assert resolution.kind is ResolutionKind.NEW_REVISION
    after_diagnostic = state(conn)

    tampered = replace(observed, observation=mutate(observed.observation))
    with pytest.raises(ArchiveSchemaError, match="check"):
        with transaction(conn):
            apply_content_resolution(conn, resolution, observed=tampered, bound_at=LATER)
    assert_untouched(conn, after_diagnostic)
    assert load_content_binding(conn, entry.path).content == first.content

    # The unmodified observation still applies, so only the wrong facts refused.
    accept(conn, observed)
    validate_archive_facts(conn)


def test_same_check_id_with_another_root_generation_is_refused(library, tmp_path):
    conn, root = library[1], tmp_path / "same-check-root-root"
    target, first, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="sr1")
    # The same check ID is reused for an observation of the next root generation.
    _, entry, observed = scan_one(conn, root, "gallery/a.bin",
                                  run_id="run-sr1", check_id="check-sr1", revision=2)
    resolution = resolve_observed_content(conn, observed)
    assert resolution.kind is ResolutionKind.REUSE
    after = state(conn)
    with pytest.raises(ArchiveSchemaError, match="check"):
        with transaction(conn):
            apply_content_resolution(conn, resolution, observed=observed, bound_at=LATER)
    assert_untouched(conn, after)
    assert load_content_binding(conn, entry.path).content == first.content


def test_replaying_a_historical_success_after_a_new_acceptance_keeps_the_pointer(
    library, tmp_path,
):
    conn, root = library[1], tmp_path / "historical-replay-root"
    target, first, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="hr1")

    time.sleep(0.02)
    rewrite(target, b"BBBB")
    _, _, second_observed = scan_one(conn, root, "gallery/a.bin",
                                     run_id="run-hr2", check_id="check-hr2")
    second_resolution, second_acceptance = accept(conn, second_observed)
    assert second_resolution.kind is ResolutionKind.NEW_REVISION

    time.sleep(0.02)
    rewrite(target, b"CCCC")
    _, _, third_observed = scan_one(conn, root, "gallery/a.bin",
                                    run_id="run-hr3", check_id="check-hr3")
    third_resolution, third_acceptance = accept(conn, third_observed)
    assert third_resolution.kind is ResolutionKind.NEW_REVISION
    after_third = state(conn)

    with pytest.raises(ArchiveBindingConflictError) as caught:
        with transaction(conn):
            apply_content_resolution(
                conn, second_resolution, observed=second_observed, bound_at=LATEST)
    assert caught.value.reason == "expected_binding_changed"
    assert_untouched(conn, after_third)
    binding = load_content_binding(conn, path_of("gallery/a.bin"))
    assert binding.content == third_resolution.content
    assert binding.acceptance_id == third_acceptance.acceptance_id
    assert binding.acceptance_id != second_acceptance.acceptance_id


# ---------------------------------------------------------------------------
# AR4-06: low-level writers share the maintenance audit's acceptance facts.
# ---------------------------------------------------------------------------


def test_binding_to_another_paths_acceptance_is_refused_before_write(library, tmp_path):
    conn, root = library[1], tmp_path / "cross-acceptance-root"
    _, a_resolution, a_acceptance = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="ca1")
    b_target, b_resolution, b_acceptance = baseline(
        conn, root, "gallery/b.bin", b"BBBB", tag="cb1")
    after_baselines = state(conn)

    with pytest.raises(ArchiveSchemaError):
        with transaction(conn):
            bind_archive_path(
                conn, b_resolution.content, path_of("gallery/b.bin"), bound_at=LATER,
                acceptance_id=a_acceptance.acceptance_id,
                expected_content_key=_content_key(b_resolution.content),
                expected_acceptance_id=b_acceptance.acceptance_id,
            )
    assert_untouched(conn, after_baselines)
    intact = load_content_binding(conn, path_of("gallery/b.bin"))
    assert intact.acceptance_id == b_acceptance.acceptance_id
    assert intact.content == b_resolution.content

    # A first binding must not inherit another path's accepted evidence either.
    new_path = write(root, "gallery/c.bin", b"CCCC")
    _, c_entry, c_observed = scan_one(conn, root, "gallery/c.bin",
                                      run_id="run-cc1", check_id="check-cc1")
    with transaction(conn):
        ensure_archive_asset(conn, c_observed.content, c_entry.path,
                             source_kind="observed_existing")
    after_c_asset = state(conn)
    with pytest.raises(ArchiveSchemaError):
        with transaction(conn):
            bind_archive_path(
                conn, c_observed.content, c_entry.path, bound_at=LATER,
                acceptance_id=a_acceptance.acceptance_id,
            )
    assert_untouched(conn, after_c_asset)
    assert binding_row(conn, "gallery/c.bin") is None

    # If a foreign writer forces that state anyway, reading fails closed instead
    # of handing back another file's physical identity, and the audit agrees.
    with transaction(conn):
        conn.execute(
            "UPDATE archive_path_bindings SET acceptance_id=? WHERE path_key=?",
            (a_acceptance.acceptance_id, path_of("gallery/b.bin").identity_key),
        )
    with pytest.raises(ArchiveSchemaError):
        load_content_binding(conn, path_of("gallery/b.bin"))
    with pytest.raises(ArchiveSchemaError):
        validate_archive_facts(conn)
    with transaction(conn):
        conn.execute(
            "UPDATE archive_path_bindings SET acceptance_id=? WHERE path_key=?",
            (b_acceptance.acceptance_id, path_of("gallery/b.bin").identity_key),
        )
    validate_archive_facts(conn)
    assert load_content_binding(conn, path_of("gallery/b.bin")).acceptance_id == (
        b_acceptance.acceptance_id)
    assert b_target.read_bytes() == b"BBBB"


def test_binding_to_another_generation_of_the_same_path_is_refused(library, tmp_path):
    conn, root = library[1], tmp_path / "same-path-generation-root"
    target, first, first_acceptance = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="gp1")
    time.sleep(0.02)
    rewrite(target, b"BBBB")
    _, _, observed = scan_one(conn, root, "gallery/a.bin",
                              run_id="run-gp2", check_id="check-gp2")
    second_resolution, second_acceptance = accept(conn, observed)
    assert second_resolution.content.revision == 2
    after = state(conn)

    # Revision 2 pointing at the acceptance of revision 1.
    with pytest.raises(ArchiveSchemaError):
        with transaction(conn):
            bind_archive_path(
                conn, second_resolution.content, path_of("gallery/a.bin"),
                bound_at=LATER,
                acceptance_id=first_acceptance.acceptance_id,
                expected_content_key=_content_key(second_resolution.content),
                expected_acceptance_id=second_acceptance.acceptance_id,
            )
    assert_untouched(conn, after)

    # Revision 1 pointing at the acceptance of revision 2.
    with pytest.raises(ArchiveSchemaError):
        with transaction(conn):
            bind_archive_path(
                conn, first.content, path_of("gallery/a.bin"), bound_at=LATER,
                acceptance_id=second_acceptance.acceptance_id,
                expected_content_key=_content_key(second_resolution.content),
                expected_acceptance_id=second_acceptance.acceptance_id,
            )
    assert_untouched(conn, after)


def test_acceptance_expected_must_be_an_older_generation_of_the_same_asset(library, tmp_path):
    conn, root = library[1], tmp_path / "expected-predecessor-root"
    target, first, first_acceptance = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="ep1")
    _, other_resolution, _ = baseline(conn, root, "gallery/b.bin", b"BBBB", tag="ep2")
    own_key = _content_key(first.content)
    other_key = _content_key(other_resolution.content)

    # A diagnostic check that no acceptance uses yet.
    _, entry, observed = scan_one(conn, root, "gallery/a.bin",
                                  run_id="run-ep3", check_id="check-ep3")
    with transaction(conn):
        record_local_observation(conn, observed.observation.evidence, result=observed.observation)
    future = ContentVersion(first.content.asset_id, 5, 4, sha(b"future bytes"))
    with transaction(conn):
        ensure_archive_asset(conn, future, entry.path, source_kind="observed_existing")
    after_setup = state(conn)

    for expected, message in (
        (own_key, "cannot precede"),
        (other_key, "cannot precede"),
        ("f" * 64, "unknown content"),
    ):
        with pytest.raises(ArchiveSchemaError, match=message):
            with transaction(conn):
                record_path_acceptance(
                    conn, first.content, entry.path, check_id="check-ep3",
                    root_revision=1, expected_content_key=expected,
                    accepted_at=LATER, reason="synthetic_acceptance",
                )
    # A later generation of the same asset is refused as well.
    with pytest.raises(ArchiveSchemaError, match="cannot precede"):
        with transaction(conn):
            record_path_acceptance(
                conn, first.content, entry.path, check_id="check-ep3",
                root_revision=1, expected_content_key=_content_key(future),
                accepted_at=LATER, reason="synthetic_acceptance",
            )
    assert_untouched(conn, after_setup)
    assert conn.execute("SELECT COUNT(*) FROM archive_path_acceptances").fetchone()[0] == 2


def test_index_only_binding_has_an_explicit_token_and_cannot_be_cleared(library, tmp_path):
    conn, root = library[1], tmp_path / "index-binding-root"
    target = write(root, "gallery/a.bin", b"AAAA")
    _, entry, observed = scan_one(conn, root, "gallery/a.bin",
                                  run_id="run-ib1", check_id="check-ib1")
    # A plan saves an index-only binding: no acceptance, physical object unknown.
    with transaction(conn):
        ensure_archive_asset(conn, observed.content, entry.path,
                             source_kind="user_selected")
        bind_archive_path(conn, observed.content, entry.path, bound_at=TIME)
    index_binding = load_content_binding(conn, entry.path)
    assert index_binding.acceptance_id is None and index_binding.root_revision is None

    resolution = resolve_observed_content(conn, observed)
    assert resolution.expected_binding_absent is False
    assert resolution.expected_acceptance_id is None
    assert resolution.expected_root_revision is None
    _, acceptance = accept(conn, observed)
    accepted = load_content_binding(conn, entry.path)
    assert accepted.acceptance_id == acceptance.acceptance_id
    assert accepted.root_revision == 1

    # A second acceptance for the same content is a real pointer move, so it needs
    # the token: a bare call is refused instead of silently replacing evidence.
    _, _, reuse_observed = scan_one(conn, root, "gallery/a.bin",
                                    run_id="run-ib1b", check_id="check-ib1b")
    _, second_acceptance = accept(conn, reuse_observed)
    assert second_acceptance.acceptance_id != acceptance.acceptance_id
    after_accept = state(conn)
    with pytest.raises(ContractError, match="expected acceptance"):
        with transaction(conn):
            bind_archive_path(
                conn, observed.content, entry.path, bound_at=LATER,
                acceptance_id=acceptance.acceptance_id,
                expected_content_key=_content_key(observed.content),
            )
    assert_untouched(conn, after_accept)

    # An explicit None says "there is no acceptance", which is now false.
    with pytest.raises(ContractError):
        with transaction(conn):
            bind_archive_path(
                conn, observed.content, entry.path, bound_at=LATER,
                acceptance_id=acceptance.acceptance_id,
                expected_content_key=_content_key(observed.content),
                expected_acceptance_id=None,
            )
    assert_untouched(conn, after_accept)

    # Advancing the content while dropping the accepted evidence is refused.
    time.sleep(0.02)
    rewrite(target, b"BBBB")
    _, entry2, changed = scan_one(conn, root, "gallery/a.bin",
                                  run_id="run-ib2", check_id="check-ib2")
    changed_resolution = resolve_observed_content(conn, changed)
    with transaction(conn):
        ensure_archive_asset(conn, changed_resolution.content, entry2.path,
                             source_kind="observed_existing")
    after_changed_asset = state(conn)
    with pytest.raises(ContractError, match="index-only"):
        with transaction(conn):
            bind_archive_path(
                conn, changed_resolution.content, entry2.path, bound_at=LATER,
                acceptance_id=None,
                expected_content_key=_content_key(observed.content),
                expected_acceptance_id=second_acceptance.acceptance_id,
            )
    assert_untouched(conn, after_changed_asset)

    # The supported path still works and keeps the accepted evidence chain.  The
    # pre-indexed row above reserved a generation, so the fresh decision allocates
    # the next free one instead of reusing the hand-inserted revision.
    fresh, fresh_acceptance = accept(conn, changed)
    final = load_content_binding(conn, entry2.path)
    assert final.content == fresh.content
    assert final.acceptance_id == fresh_acceptance.acceptance_id
    assert final.content.revision >= changed_resolution.content.revision
    assert final.acceptance_id is not None and final.root_revision == 1
    validate_archive_facts(conn)


def test_plan_save_cannot_silently_drop_accepted_evidence(library, tmp_path):
    """A plan that advances an accepted binding is refused, not weakened to NULL.

    The plan layer still does not carry an expectation token (that is R3/AR4-02),
    but it must not clear the accepted evidence of a path as a side effect: the
    caller has to apply an observed acceptance first.
    """
    conn, root = library[1], tmp_path / "plan-token-root"
    target, first, acceptance = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="pt1")
    time.sleep(0.02)
    rewrite(target, b"BBBB")
    receipt, entry, _ = scan_one(conn, root, "gallery/a.bin",
                                 run_id="run-pt2", check_id="check-pt2")
    newer = ContentVersion(first.content.asset_id, 2, 4, sha(b"BBBB"))
    inventory = build_archive_inventory(
        (ArtworkGroup("work-1", (
            ArchiveEntry(newer, entry.path, 0, entry.physical_identity),
        )),),
        batch_id="batch-plan-token",
        policy=InventoryPolicy(target_package_bytes=1024, batch_budget_bytes=10 ** 9),
    )
    capacity = plan_archive_capacity(
        inventory, available_additional_bytes=10 ** 9,
        policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
    )
    after_baseline = state(conn)
    with pytest.raises(ArchivePlanStoreError):
        save_archive_plan(conn, inventory, capacity, receipt=receipt, root_revision=1)
    assert_untouched(conn, after_baseline)
    assert binding_row(conn, "gallery/a.bin")[3] == acceptance.acceptance_id
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_assets WHERE revision=2").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM archive_batches").fetchone()[0] == 0

    # The supported order still works: accept the observed change, then plan it.
    later_receipt, _, observed = scan_one(conn, root, "gallery/a.bin",
                                          run_id="run-pt3", check_id="check-pt3")
    resolution, second_acceptance = accept(conn, observed)
    assert resolution.content.revision == 2
    save_archive_plan(
        conn, inventory, capacity, receipt=later_receipt, root_revision=1)
    assert binding_row(conn, "gallery/a.bin")[3] == second_acceptance.acceptance_id
    assert load_archive_plan(conn, "batch-plan-token").inventory == inventory
    validate_archive_facts(conn)


def test_rebinding_the_current_acceptance_is_a_no_op_without_a_token(library, tmp_path):
    """Re-naming the acceptance already in place moves nothing, so it needs no token."""
    conn, root = library[1], tmp_path / "bind-noop-root"
    target, first, acceptance = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="bn1")
    before = state(conn)
    row_before = binding_row(conn, "gallery/a.bin")

    with transaction(conn):
        bind_archive_path(
            conn, first.content, path_of("gallery/a.bin"), bound_at=LATER,
            acceptance_id=acceptance.acceptance_id,
            expected_content_key=_content_key(first.content),
        )
    assert state(conn) == before
    assert binding_row(conn, "gallery/a.bin") == row_before

    # Naming no acceptance at all must keep the accepted evidence, not clear it.
    with transaction(conn):
        bind_archive_path(conn, first.content, path_of("gallery/a.bin"), bound_at=LATER)
    assert state(conn) == before
    assert binding_row(conn, "gallery/a.bin") == row_before
    assert load_content_binding(conn, path_of("gallery/a.bin")).acceptance_id == (
        acceptance.acceptance_id)
    validate_archive_facts(conn)


def test_repository_batch_rechecks_tokens_and_rolls_the_whole_batch_back(library, tmp_path):
    conn, root = library[1], tmp_path / "repository-token-root"
    source = library[0]
    a_target, a_first, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="rb1")
    b_target, b_first, b_acceptance = baseline(conn, root, "gallery/b.bin", b"BBBB", tag="rb2")

    time.sleep(0.02)
    rewrite(a_target, b"CCCC")
    rewrite(b_target, b"DDDD")
    _, a_entry, a_observed = scan_one(conn, root, "gallery/a.bin",
                                      run_id="run-rb3", check_id="check-rb3")
    _, b_entry, b_observed = scan_one(conn, root, "gallery/b.bin",
                                      run_id="run-rb4", check_id="check-rb4")
    a_resolution = resolve_observed_content(conn, a_observed)
    b_resolution = resolve_observed_content(conn, b_observed)
    assert a_resolution.kind is ResolutionKind.NEW_REVISION
    assert b_resolution.kind is ResolutionKind.NEW_REVISION

    # Another writer advances A after both decisions were resolved.
    time.sleep(0.02)
    rewrite(a_target, b"EEEE")
    _, _, a_newer = scan_one(conn, root, "gallery/a.bin",
                             run_id="run-rb5", check_id="check-rb5")
    accept(conn, a_newer)
    after_competing = state(conn)

    with ArchiveLibraryRepository(source, temporary_copy_confirmed=True) as repository:
        with pytest.raises(ArchiveBindingConflictError):
            repository.apply_archive_resolutions(
                ((a_resolution, a_observed), (b_resolution, b_observed)),
            )
        assert_untouched(conn, after_competing)
        assert load_content_binding(conn, b_entry.path).content == b_first.content
        assert load_content_binding(conn, b_entry.path).acceptance_id == (
            b_acceptance.acceptance_id)

        # The still-valid decision applies, and the stale one is refused again.
        repository.apply_archive_resolutions(((b_resolution, b_observed),))
        assert load_content_binding(conn, b_entry.path).content == b_resolution.content
        with pytest.raises(ArchiveBindingConflictError):
            repository.apply_archive_resolutions(((a_resolution, a_observed),))

    assert load_content_binding(conn, a_entry.path).content != a_resolution.content
    validate_archive_facts(conn)
    assert a_first.content != a_resolution.content
