"""N1/N2 counter-examples as assertions: logical identity, expected-old, acceptance.

Every case is synthetic and offline: temporary directories, one migrated copy of an
empty v1 library per test, real bytes read by LocalObserver, and no network,
account, production database or real image library.

The three audited defects behind this module (docs/audits/2026-11-12-a1-final):

* AR3-01 - identical bytes made two independent files one logical asset, so the
  second file's later edit collided with the first file's revision.
* AR3-02 - apply_content_resolution bound the just-read current key as its
  expected old value and accepted writes on an autocommit connection.
* AR3-03 - load_content_binding guessed the accepted physical object from the
  newest observation row, so merely recording a changed diagnostic changed which
  object the resolver considered accepted.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from core.archive_inventory import (
    ArchiveEntry, ArtworkGroup, CapacityPolicy, InventoryPolicy,
    build_archive_inventory, plan_archive_capacity,
)
from core.archive_plan_store import load_archive_plan, save_archive_plan
from core.archive_repository import ArchiveLibraryRepository
from core.archive_schema import (
    ARCHIVE_SCHEMA_VERSION, ArchiveBindingConflictError, ArchiveIdentityConfirmationRequired,
    ArchiveMigrationError, ArchiveSchemaError, ArchiveTransactionRequiredError,
    apply_content_resolution, bind_archive_path, ensure_archive_asset, load_content_binding,
    migrate_archive_copy, record_archive_observation, record_local_observation,
    record_path_acceptance, record_release_event, record_transfer_intent,
    resolve_observed_content, validate_archive_facts,
)
from core.archive_scope import (
    CandidateSnapshot, ResolutionKind, ScopeEnumerator, ScopeRequest, consume_scope_pages,
    observe_candidate,
)
from core.library_repository import LibraryRepository, SchemaVersionError
from core.local_observation import (
    FileIdentity, LocalObservation, LocalObserver, ObservationRequest, ObservationStatus,
)
from core.preservation_policy import (
    ArchiveEvidence, ArchiveState, ContentVersion, LocalEvidence, LocalState, RemovalIntent,
)
from core.task_contracts import CollectionRequest, PathRef

TIME = "2026-11-12T09:00:00+00:00"
# Diagnostics written by these tests must be the newest rows in the table whatever
# the wall clock says, otherwise "latest observation" ordering would be untested.
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
    """A transaction the caller owns.

    The archive writers never open, commit or roll back a transaction, and they
    refuse to run while no transaction is open, so every case here begins one
    explicitly instead of relying on an implicit autocommit statement.
    """
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


def baseline(conn, root, relative, data, *, tag):
    target = write(root, relative, data)
    receipt, entry, observed = scan_one(
        conn, root, relative, run_id="run-" + tag, check_id="check-" + tag,
    )
    resolution, acceptance_ = accept(conn, observed)
    assert resolution.kind is ResolutionKind.BASELINE
    return target, resolution, acceptance_


def path_of(relative: str) -> PathRef:
    return PathRef(kind="project", root_id="project", relative_path=relative)


def counts(conn):
    return {
        table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
        for table in (
            "archive_contents", "archive_assets", "archive_path_bindings",
            "archive_local_checks", "archive_path_acceptances", "archive_batches",
        )
    }


# ---------------------------------------------------------------------------
# AR3-01: byte equality is not logical asset identity
# ---------------------------------------------------------------------------


def test_independent_same_byte_files_keep_separate_logical_assets(library, tmp_path):
    conn, root = library[1], tmp_path / "duplicate-root"
    write(root, "gallery/a.bin", b"AAAA")
    write(root, "gallery/b.bin", b"AAAA")
    receipt, entries, facts = enumerate_gallery(root)
    a_entry, a_fact = by_path(receipt, entries, facts, "gallery/a.bin")
    b_entry, b_fact = by_path(receipt, entries, facts, "gallery/b.bin")
    assert a_entry.physical_identity != b_entry.physical_identity

    a_resolution, _ = accept(conn, observe(
        conn, root, a_entry, a_fact, receipt, run_id="run-a1", check_id="check-a1"))
    b_resolution, _ = accept(conn, observe(
        conn, root, b_entry, b_fact, receipt, run_id="run-b1", check_id="check-b1"))
    assert a_resolution.kind is ResolutionKind.BASELINE
    assert b_resolution.kind is ResolutionKind.BASELINE
    assert b_resolution.content.asset_id != a_resolution.content.asset_id
    assert b_resolution.content.revision == 1

    rewrite(root / "gallery" / "a.bin", b"BBBB")
    _, a_entry2, a_observed2 = scan_one(
        conn, root, "gallery/a.bin", run_id="run-a2", check_id="check-a2")
    a_resolution2, _ = accept(conn, a_observed2)
    assert a_resolution2.kind is ResolutionKind.NEW_REVISION
    assert a_resolution2.content.revision == 2
    assert a_resolution2.content.sha256 == sha(b"BBBB")

    time.sleep(0.02)
    rewrite(root / "gallery" / "b.bin", b"CCCC")
    _, b_entry2, b_observed2 = scan_one(
        conn, root, "gallery/b.bin", run_id="run-b2", check_id="check-b2")
    b_resolution2, _ = accept(conn, b_observed2)
    assert b_resolution2.kind is ResolutionKind.NEW_REVISION
    assert b_resolution2.content.asset_id == b_resolution.content.asset_id
    assert b_resolution2.content.revision == 2
    assert b_resolution2.content.sha256 == sha(b"CCCC")

    assert load_content_binding(conn, a_entry.path).content == a_resolution2.content
    assert load_content_binding(conn, b_entry.path).content == b_resolution2.content
    # Two independent rows hold those identical bytes; byte de-duplication did not
    # merge the two logical assets.
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_contents WHERE sha256=? AND size_bytes=4",
        (sha(b"AAAA"),),
    ).fetchone()[0] == 2
    validate_archive_facts(conn)


def test_hardlink_paths_share_one_logical_asset_and_revision(library, tmp_path):
    conn, root = library[1], tmp_path / "hardlink-root"
    first = write(root, "gallery/a.bin", b"hardlinked payload")
    try:
        os.link(first, root / "gallery" / "b.bin")
    except OSError as exc:
        pytest.skip("Synthetic hardlink unavailable: " + type(exc).__name__)
    receipt, entries, facts = enumerate_gallery(root)
    a_entry, a_fact = by_path(receipt, entries, facts, "gallery/a.bin")
    b_entry, b_fact = by_path(receipt, entries, facts, "gallery/b.bin")
    assert a_entry.physical_identity == b_entry.physical_identity

    a_resolution, _ = accept(conn, observe(
        conn, root, a_entry, a_fact, receipt, run_id="run-ha", check_id="check-ha"))
    b_resolution, _ = accept(conn, observe(
        conn, root, b_entry, b_fact, receipt, run_id="run-hb", check_id="check-hb"))
    assert a_resolution.kind is ResolutionKind.BASELINE
    assert b_resolution.kind is ResolutionKind.REUSE
    assert b_resolution.content == a_resolution.content
    assert load_content_binding(conn, a_entry.path).content == a_resolution.content
    assert load_content_binding(conn, b_entry.path).content == a_resolution.content
    validate_archive_facts(conn)


def test_copy_then_independent_edit_of_the_copy(library, tmp_path):
    conn, root = library[1], tmp_path / "copy-root"
    source = write(root, "gallery/a.bin", b"original")
    copy = root / "gallery" / "b.bin"
    copy.write_bytes(source.read_bytes())
    receipt, entries, facts = enumerate_gallery(root)
    a_entry, a_fact = by_path(receipt, entries, facts, "gallery/a.bin")
    b_entry, b_fact = by_path(receipt, entries, facts, "gallery/b.bin")
    assert a_entry.physical_identity != b_entry.physical_identity

    accept(conn, observe(conn, root, a_entry, a_fact, receipt,
                         run_id="run-ca", check_id="check-ca"))
    b_resolution, _ = accept(conn, observe(
        conn, root, b_entry, b_fact, receipt, run_id="run-cb", check_id="check-cb"))
    assert b_resolution.kind is ResolutionKind.BASELINE

    time.sleep(0.02)
    rewrite(copy, b"edited copy")
    _, b_entry2, b_observed2 = scan_one(
        conn, root, "gallery/b.bin", run_id="run-cb2", check_id="check-cb2")
    b_resolution2, _ = accept(conn, b_observed2)
    assert b_resolution2.kind is ResolutionKind.NEW_REVISION
    assert b_resolution2.content.revision == 2
    assert b_resolution2.content.sha256 == sha(b"edited copy")
    assert load_content_binding(conn, a_entry.path).content.sha256 == sha(b"original")
    assert source.read_bytes() == b"original"
    validate_archive_facts(conn)


def test_content_round_trip_a_b_a_advances_monotonically(library, tmp_path):
    conn, root = library[1], tmp_path / "roundtrip-root"
    target, first, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="rt1")
    assert first.content.revision == 1

    time.sleep(0.02)
    rewrite(target, b"BBBB")
    _, _, observed = scan_one(conn, root, "gallery/a.bin",
                              run_id="run-rt2", check_id="check-rt2")
    second, _ = accept(conn, observed)
    assert second.kind is ResolutionKind.NEW_REVISION
    assert second.content.revision == 2 and second.content.sha256 == sha(b"BBBB")

    time.sleep(0.02)
    rewrite(target, b"AAAA")
    _, entry3, observed3 = scan_one(conn, root, "gallery/a.bin",
                                    run_id="run-rt3", check_id="check-rt3")
    third, _ = accept(conn, observed3)
    assert third.kind is ResolutionKind.NEW_REVISION
    assert third.content.revision == 3
    assert third.content.sha256 == sha(b"AAAA")
    assert load_content_binding(conn, entry3.path).content == third.content
    # Both earlier generations survive; the round trip does not resurrect revision 1.
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_contents WHERE sha256=? AND asset_id=?",
        (sha(b"AAAA"), third.content.asset_id),
    ).fetchone()[0] == 2
    validate_archive_facts(conn)


def test_same_name_replacement_keeps_the_asset_chain_and_old_rows(library, tmp_path):
    conn, root = library[1], tmp_path / "replace-root"
    target, first, _ = baseline(conn, root, "gallery/art.png", b"AAAA", tag="rp1")
    receipt2, old_entry, _ = scan_one(
        conn, root, "gallery/art.png", run_id="run-rp1b", check_id="check-rp1b")
    incoming = root / "gallery" / ".incoming.tmp"
    incoming.write_bytes(b"CCCC")
    os.replace(incoming, target)
    time.sleep(0.02)
    _, entry2, observed2 = scan_one(
        conn, root, "gallery/art.png", run_id="run-rp2", check_id="check-rp2")
    assert entry2.physical_identity != old_entry.physical_identity

    unconfirmed = resolve_observed_content(conn, observed2)
    assert unconfirmed.kind is ResolutionKind.NEEDS_CONFIRMATION
    assert unconfirmed.reason == "physical_object_replaced"
    before = counts(conn)
    with pytest.raises(ArchiveIdentityConfirmationRequired):
        with transaction(conn):
            apply_content_resolution(conn, unconfirmed, observed=observed2, bound_at=TIME)
    assert counts(conn) == before
    assert load_content_binding(conn, entry2.path).content == first.content

    confirmed, acceptance_ = accept(conn, observed2, confirmed=True)
    assert confirmed.kind is ResolutionKind.REPLACEMENT
    assert confirmed.content.asset_id == first.content.asset_id
    assert confirmed.content.revision == first.content.revision + 1
    assert confirmed.content.sha256 == sha(b"CCCC")
    assert load_content_binding(conn, entry2.path).content == confirmed.content
    assert acceptance_.check_id == "check-rp2"
    # The replaced bytes are never deleted or overwritten.
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_contents WHERE sha256=?", (sha(b"AAAA"),)
    ).fetchone()[0] == 1
    validate_archive_facts(conn)


# ---------------------------------------------------------------------------
# AR3-02: expected-old comes from the resolution, and writes need a transaction
# ---------------------------------------------------------------------------


def test_apply_refuses_an_autocommit_connection_before_writing(library, tmp_path):
    conn, root = library[1], tmp_path / "autocommit-root"
    _, first, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="ac1")
    time.sleep(0.02)
    rewrite(root / "gallery" / "a.bin", b"BBBB")
    _, entry, observed = scan_one(conn, root, "gallery/a.bin",
                                  run_id="run-ac2", check_id="check-ac2")
    resolution = resolve_observed_content(conn, observed)
    assert resolution.kind is ResolutionKind.NEW_REVISION
    before = counts(conn)
    conn.isolation_level = None
    try:
        assert conn.in_transaction is False
        with pytest.raises(ArchiveTransactionRequiredError):
            apply_content_resolution(conn, resolution, observed=observed, bound_at=TIME)
    finally:
        conn.isolation_level = ""
    assert counts(conn) == before
    assert load_content_binding(conn, entry.path).content == first.content


def test_low_level_writers_refuse_an_autocommit_connection(library, tmp_path):
    conn, root = library[1], tmp_path / "low-level-root"
    _, first, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="ll1")
    content = first.content
    path = path_of("gallery/a.bin")
    newer = ContentVersion(content.asset_id, content.revision + 1, 4, sha(b"BBBB"))
    evidence = LocalEvidence(
        content=content, path=path, root_revision=1, run_id="run-ll",
        check_id="check-ll", checked_at=LATER, state=LocalState.HASH_MATCH,
        observed_size=4, observed_sha256=content.sha256,
    )
    before = counts(conn)
    conn.isolation_level = None
    try:
        assert conn.in_transaction is False
        for call in (
            lambda: ensure_archive_asset(conn, content, path, source_kind="observed_existing"),
            lambda: bind_archive_path(conn, newer, path, bound_at=TIME),
            lambda: record_local_observation(conn, evidence),
            lambda: record_transfer_intent(
                conn, content, path, intent_id="intent-autocommit", root_revision=1,
                intent=RemovalIntent.NONE, source="user_reported", observed_at=TIME,
            ),
            lambda: record_archive_observation(
                conn, ArchiveEvidence(content, ArchiveState.UPLOAD_REPORTED, "archive-1", TIME),
            ),
            lambda: record_release_event(
                conn, content, operation_id="release-op", event_id="release-1",
                sequence=1, event="prepared", local_check_id="check-ll",
                archive_check_id="archive-1", observed_at=TIME,
            ),
        ):
            with pytest.raises(ArchiveTransactionRequiredError):
                call()
    finally:
        conn.isolation_level = ""
    assert counts(conn) == before


def test_apply_rejects_a_stale_expected_revision(library, tmp_path):
    """Fault injection (audit probe stale_cas): explicit stale expected input."""
    conn, root = library[1], tmp_path / "stale-cas-root"
    target, first, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="sc1")
    for step, payload in ((2, b"BBBB"), (3, b"CCCC")):
        time.sleep(0.02)
        rewrite(target, payload)
        _, _, observed = scan_one(
            conn, root, "gallery/a.bin",
            run_id="run-sc" + str(step), check_id="check-sc" + str(step),
        )
        accept(conn, observed)
    current = load_content_binding(conn, path_of("gallery/a.bin"))
    assert current.content.revision == 3

    time.sleep(0.02)
    rewrite(target, b"DDDD")
    _, _, observed4 = scan_one(conn, root, "gallery/a.bin",
                               run_id="run-sc4", check_id="check-sc4")
    fresh = resolve_observed_content(conn, observed4)
    assert fresh.kind is ResolutionKind.NEW_REVISION and fresh.content.revision == 4
    stale = replace(fresh, previous_content=first.content)
    before = counts(conn)
    with pytest.raises(ArchiveBindingConflictError) as caught:
        with transaction(conn):
            apply_content_resolution(conn, stale, observed=observed4, bound_at=TIME)
    assert caught.value.expected_content_key is not None
    assert caught.value.actual_content_key is not None
    assert counts(conn) == before
    assert load_content_binding(conn, observed4.candidate.path).content == current.content


def test_apply_rejects_a_violated_absence_expectation(library, tmp_path):
    """Fault injection: a resolution that said 'must not exist' after another write."""
    conn, root = library[1], tmp_path / "absent-root"
    target, first, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="ab1")
    time.sleep(0.02)
    rewrite(target, b"BBBB")
    _, _, observed2 = scan_one(conn, root, "gallery/a.bin",
                               run_id="run-ab2", check_id="check-ab2")
    fresh = resolve_observed_content(conn, observed2)
    assert fresh.kind is ResolutionKind.NEW_REVISION
    absent = replace(fresh, kind=ResolutionKind.REPLACEMENT, previous_content=None)
    before = counts(conn)
    with pytest.raises(ArchiveBindingConflictError) as caught:
        with transaction(conn):
            apply_content_resolution(conn, absent, observed=observed2, bound_at=TIME)
    assert caught.value.expected_content_key is None
    assert counts(conn) == before
    assert load_content_binding(conn, observed2.candidate.path).content == first.content


def test_natural_absence_expectation_is_rechecked_inside_the_transaction(library, tmp_path):
    """The audited natural interleaving: resolve B while unbound, bind B, then apply."""
    conn, root = library[1], tmp_path / "natural-absent-root"
    target_a, _, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="na1")
    time.sleep(0.02)
    rewrite(target_a, b"BBBB")
    _, _, observed_a = scan_one(conn, root, "gallery/a.bin",
                                run_id="run-na2", check_id="check-na2")
    accept(conn, observed_a)

    target_b = write(root, "gallery/b.bin", b"BBBB")
    _, _, held = scan_one(conn, root, "gallery/b.bin",
                          run_id="run-na3", check_id="check-na3")
    held_resolution = resolve_observed_content(conn, held)
    assert held_resolution.expected_binding_absent is True

    # Another writer binds the path before the held resolution is applied.
    time.sleep(0.02)
    rewrite(target_b, b"AAAA")
    _, _, observed_b = scan_one(conn, root, "gallery/b.bin",
                                run_id="run-na4", check_id="check-na4")
    fresh, _ = accept(conn, observed_b)
    assert fresh.kind is ResolutionKind.BASELINE
    before = counts(conn)
    with pytest.raises(ArchiveBindingConflictError) as caught:
        with transaction(conn):
            apply_content_resolution(conn, held_resolution, observed=held, bound_at=LATER)
    assert caught.value.expected_content_key is None
    assert counts(conn) == before
    assert load_content_binding(conn, held.candidate.path).content == fresh.content


def test_failed_apply_rolls_back_every_fact_and_the_writer_continues(library, tmp_path):
    conn, root = library[1], tmp_path / "rollback-root"
    target, first, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="rb1")
    time.sleep(0.02)
    rewrite(target, b"BBBB")
    _, entry, observed = scan_one(conn, root, "gallery/a.bin",
                                  run_id="run-rb2", check_id="check-rb2")
    resolution = resolve_observed_content(conn, observed)
    # The same check ID already carries different facts, so the diagnostic insert
    # fails only after the content/asset rows were written in this transaction.
    conflicting = replace(observed.observation.evidence, root_revision=7)
    with transaction(conn):
        record_local_observation(conn, conflicting, result=replace(
            observed.observation, evidence=conflicting))
    before = counts(conn)
    with pytest.raises(ArchiveSchemaError):
        with transaction(conn):
            apply_content_resolution(conn, resolution, observed=observed, bound_at=TIME)
    assert counts(conn) == before
    assert load_content_binding(conn, entry.path).content == first.content

    # The connection is still usable for a corrected observation.
    _, entry2, corrected = scan_one(conn, root, "gallery/a.bin",
                                    run_id="run-rb3", check_id="check-rb3")
    fixed, _ = accept(conn, corrected)
    assert fixed.kind is ResolutionKind.NEW_REVISION
    assert load_content_binding(conn, entry2.path).content == fixed.content
    validate_archive_facts(conn)


def test_replaying_the_same_observation_is_idempotent(library, tmp_path):
    conn, root = library[1], tmp_path / "replay-root"
    target, _, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="pl1")
    time.sleep(0.02)
    rewrite(target, b"BBBB")
    _, entry, observed = scan_one(conn, root, "gallery/a.bin",
                                  run_id="run-pl2", check_id="check-pl2")
    resolution, acceptance_ = accept(conn, observed)
    assert resolution.kind is ResolutionKind.NEW_REVISION
    after_first = counts(conn)
    with transaction(conn):
        replay = apply_content_resolution(conn, resolution, observed=observed, bound_at=LATER)
    assert replay.acceptance_id == acceptance_.acceptance_id
    assert counts(conn) == after_first
    assert load_content_binding(conn, entry.path).content == resolution.content


def test_same_check_id_with_different_facts_is_rejected(library, tmp_path):
    conn, root = library[1], tmp_path / "same-id-root"
    _, first, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="si1")
    evidence = LocalEvidence(
        content=first.content, path=path_of("gallery/a.bin"), root_revision=1,
        run_id="run-si", check_id="check-si-extra", checked_at=LATER,
        state=LocalState.HASH_MATCH, observed_size=4,
        observed_sha256=first.content.sha256,
    )
    with transaction(conn):
        record_local_observation(conn, evidence)
    before = counts(conn)
    with pytest.raises(ArchiveSchemaError, match="check"):
        with transaction(conn):
            record_local_observation(conn, replace(evidence, checked_at=LATEST))
    assert counts(conn) == before


# ---------------------------------------------------------------------------
# AR3-03: diagnostics never move the accepted pointer
# ---------------------------------------------------------------------------


def accepted_binding(conn, relative):
    return load_content_binding(conn, path_of(relative))


def diagnose(conn, observation):
    with transaction(conn):
        record_local_observation(conn, observation.evidence, result=observation)


def test_recording_a_changed_diagnostic_does_not_authorize_a_replacement(library, tmp_path):
    conn, root = library[1], tmp_path / "diag-root"
    target, first, acceptance_ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="dg1")
    before = accepted_binding(conn, "gallery/a.bin")
    assert before.physical_identity is not None
    assert before.acceptance_id == acceptance_.acceptance_id

    incoming = root / "gallery" / ".incoming"
    incoming.write_bytes(b"BBBB")
    os.replace(incoming, target)
    time.sleep(0.02)
    _, entry, observed = scan_one(conn, root, "gallery/a.bin",
                                  run_id="run-dg2", check_id="check-dg2")
    assert observed.observed_identity != before.physical_identity
    unresolve = resolve_observed_content(conn, observed)
    assert unresolve.kind is ResolutionKind.NEEDS_CONFIRMATION
    assert unresolve.reason == "physical_object_replaced"

    diagnose(conn, observed.observation)

    after = accepted_binding(conn, "gallery/a.bin")
    assert after == before
    still = resolve_observed_content(conn, observed)
    assert still.kind is ResolutionKind.NEEDS_CONFIRMATION
    assert still.reason == "physical_object_replaced"
    assert conn.execute("SELECT COUNT(*) FROM archive_local_checks").fetchone()[0] == 2
    validate_archive_facts(conn)


def test_missing_and_cancelled_diagnostics_keep_the_pointer(library, tmp_path):
    conn, root = library[1], tmp_path / "diag-negative-root"
    target, first, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="dn1")
    before = accepted_binding(conn, "gallery/a.bin")
    path = path_of("gallery/a.bin")
    observer = LocalObserver(root, root_id="project", root_revision=1)

    class Stop:
        def is_set(self):
            return True

    target.unlink()
    missing = observer.observe(ObservationRequest(
        first.content, path, "run-dn2", "check-dn2"))
    assert missing.status is ObservationStatus.MISSING
    diagnose(conn, missing)
    cancelled = observer.observe(ObservationRequest(
        first.content, path, "run-dn3", "check-dn3", cancellation=Stop()))
    assert not cancelled.digest_complete
    diagnose(conn, cancelled)

    assert accepted_binding(conn, "gallery/a.bin") == before
    assert load_content_binding(conn, path).content == first.content
    assert conn.execute("SELECT COUNT(*) FROM archive_local_checks").fetchone()[0] == 3
    validate_archive_facts(conn)


def test_diagnostic_from_an_older_root_generation_does_not_move_the_pointer(library, tmp_path):
    conn, root = library[1], tmp_path / "root-order-root"
    target, first, acceptance_ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="ro1")
    before = accepted_binding(conn, "gallery/a.bin")
    assert before.root_revision == 1

    observer = LocalObserver(
        root, root_id="project", root_revision=1, _clock=lambda: LATER)
    late = observer.observe(ObservationRequest(
        first.content, path_of("gallery/a.bin"), "run-ro2", "zzz-late"))
    assert late.status is ObservationStatus.HASH_MATCH
    diagnose(conn, late)

    after = accepted_binding(conn, "gallery/a.bin")
    assert after == before
    assert after.acceptance_id == acceptance_.acceptance_id
    validate_archive_facts(conn)


def test_confirmed_replacement_moves_the_pointer_only_after_confirmation(library, tmp_path):
    conn, root = library[1], tmp_path / "pointer-root"
    target, _, first_acceptance = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="pm1")
    incoming = root / "gallery" / ".incoming"
    incoming.write_bytes(b"CCCC")
    os.replace(incoming, target)
    time.sleep(0.02)
    _, entry, observed = scan_one(conn, root, "gallery/a.bin",
                                  run_id="run-pm2", check_id="check-pm2")
    diagnose(conn, observed.observation)
    assert accepted_binding(conn, "gallery/a.bin").acceptance_id == first_acceptance.acceptance_id

    resolution, second_acceptance = accept(conn, observed, confirmed=True)
    binding = accepted_binding(conn, "gallery/a.bin")
    assert binding.acceptance_id == second_acceptance.acceptance_id
    assert binding.physical_identity == observed.observed_identity
    assert binding.content == resolution.content
    assert first_acceptance.acceptance_id != second_acceptance.acceptance_id
    # Both acceptances are kept: the history of accepted evidence is append-only.
    assert conn.execute("SELECT COUNT(*) FROM archive_path_acceptances").fetchone()[0] == 2
    validate_archive_facts(conn)


def test_acceptance_rows_are_recomputed_by_the_maintenance_audit(library, tmp_path):
    """A tampered acceptance cannot survive the full-library audit."""
    conn, root = library[1], tmp_path / "audit-root"
    _, first, acceptance_ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="au1")
    validate_archive_facts(conn)

    row = conn.execute(
        "SELECT check_id,file_device,root_revision FROM archive_path_acceptances"
    ).fetchone()
    assert row == (acceptance_.check_id, str(acceptance_.physical_identity.device),
                   acceptance_.root_revision)

    # The accepted physical object must be the one the check observed.
    with transaction(conn):
        conn.execute(
            "UPDATE archive_path_acceptances SET file_device=?",
            (str(int(row[1]) + 1),),
        )
    with pytest.raises(ArchiveSchemaError, match="acceptance"):
        validate_archive_facts(conn)
    with transaction(conn):
        conn.execute(
            "UPDATE archive_path_acceptances SET file_device=?", (row[1],)
        )
    validate_archive_facts(conn)

    # Dropping the acceptance reference is not a false claim, it is a weaker one:
    # the binding then names no accepted physical object and a later content change
    # has to be confirmed again.
    with transaction(conn):
        conn.execute("UPDATE archive_path_bindings SET acceptance_id=NULL")
    weakened = load_content_binding(conn, path_of("gallery/a.bin"))
    assert weakened.acceptance_id is None and weakened.physical_identity is None
    assert weakened.content == acceptance_.content
    validate_archive_facts(conn)
    with transaction(conn):
        conn.execute(
            "UPDATE archive_path_bindings SET acceptance_id=?",
            (acceptance_.acceptance_id,),
        )
    assert load_content_binding(conn, path_of("gallery/a.bin")).acceptance_id == (
        acceptance_.acceptance_id)
    validate_archive_facts(conn)


def test_acceptance_cannot_reuse_a_check_of_other_bytes(library, tmp_path):
    """An acceptance must be justified by a check of exactly these bytes."""
    conn, root = library[1], tmp_path / "accept-check-root"
    target, first, first_acceptance = baseline(
        conn, root, "gallery/a.bin", b"AAAA", tag="ac1")
    first_acceptance_check = first_acceptance.check_id
    time.sleep(0.02)
    rewrite(target, b"BBBB")
    _, entry, observed = scan_one(conn, root, "gallery/a.bin",
                                  run_id="run-ac2", check_id="check-ac2")
    resolution = resolve_observed_content(conn, observed)
    assert resolution.content is not None and resolution.content.sha256 == sha(b"BBBB")
    accepted, acceptance_ = accept(conn, observed)
    assert accepted.content == resolution.content
    before = conn.execute("SELECT COUNT(*) FROM archive_path_acceptances").fetchone()[0]
    with pytest.raises(ArchiveSchemaError, match="exact content"):
        with transaction(conn):
            record_path_acceptance(
                conn, resolution.content, entry.path, check_id=first_acceptance_check,
                root_revision=1, expected_content_key=_content_key_of(first.content),
                accepted_at=LATER, reason="synthetic_acceptance",
            )
    assert conn.execute("SELECT COUNT(*) FROM archive_path_acceptances").fetchone()[0] == before
    assert load_content_binding(conn, entry.path).content == resolution.content


def _content_key_of(content):
    from core.archive_schema import _content_key
    return _content_key(content)


def test_same_timestamp_diagnostics_are_not_a_tie_breaker(library, tmp_path):
    conn, root = library[1], tmp_path / "tie-root"
    target, first, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="ti1")
    before = accepted_binding(conn, "gallery/a.bin")
    other = FileIdentity(before.physical_identity.device,
                         before.physical_identity.file_index + 1)
    for suffix in ("aaa", "zzz"):
        evidence = LocalEvidence(
            content=first.content, path=path_of("gallery/a.bin"), root_revision=1,
            run_id="run-ti", check_id="check-tie-" + suffix, checked_at=LATER,
            state=LocalState.HASH_MATCH, observed_size=4,
            observed_sha256=first.content.sha256,
        )
        diagnose(conn, LocalObservation(
            status=ObservationStatus.HASH_MATCH, reason="full_sha256_match",
            evidence=evidence, digest_complete=True, bytes_read=4, size_bytes=4,
            mtime_ns=target.stat().st_mtime_ns, identity=other,
        ))
    assert accepted_binding(conn, "gallery/a.bin") == before


# ---------------------------------------------------------------------------
# Schema 3 -> 4, plan compatibility and the dedicated repository command
# ---------------------------------------------------------------------------


def test_schema_version_is_four_and_old_copies_are_refused(library, tmp_path):
    source, conn = library
    assert ARCHIVE_SCHEMA_VERSION == "4"
    assert conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()[0] == "4"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    validate_archive_facts(conn)

    with closing(sqlite3.connect(source)) as raw:
        raw.execute("UPDATE schema_meta SET value='3' WHERE key='schema_version'")
        raw.commit()
    with pytest.raises(SchemaVersionError):
        LibraryRepository(source)
    with pytest.raises(SchemaVersionError):
        ArchiveLibraryRepository(source, temporary_copy_confirmed=True)
    with pytest.raises(ArchiveMigrationError):
        migrate_archive_copy(source, tmp_path / "refused.backup", temporary_copy_confirmed=True)
    assert not (tmp_path / "refused.backup").exists()

    with closing(sqlite3.connect(source)) as raw:
        assert raw.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "3"
        raw.execute("UPDATE schema_meta SET value='4' WHERE key='schema_version'")
        raw.commit()
    with ArchiveLibraryRepository(source, temporary_copy_confirmed=True):
        pass


def test_schema_four_migration_backup_and_standalone_reopen(library, tmp_path):
    """The 3 -> 4 upgrade keeps a v1 backup, a self-contained copy and old plans."""
    source, conn = library
    backup = tmp_path / "before-migration.sqlite3"
    with closing(sqlite3.connect(backup)) as raw:
        assert raw.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "1"
        tables = {
            row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "archive_path_acceptances" not in tables
        assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    assert ARCHIVE_SCHEMA_VERSION == "4"
    assert conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()[0] == "4"
    assert conn.execute("SELECT COUNT(*) FROM archive_path_acceptances").fetchone()[0] == 0

    root = tmp_path / "schema-root"
    target, first, acceptance_ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="s4a")
    receipt, entries, facts = enumerate_gallery(root)
    entry = entries[0]
    inventory = build_archive_inventory(
        (ArtworkGroup("work-1", (ArchiveEntry(first.content, entry.path, 0,
                                              entry.physical_identity),)),),
        batch_id="batch-schema-four",
        policy=InventoryPolicy(target_package_bytes=1024, batch_budget_bytes=10 ** 9),
    )
    capacity = plan_archive_capacity(
        inventory, available_additional_bytes=10 ** 9,
        policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
    )
    save_archive_plan(conn, inventory, capacity, receipt=receipt, root_revision=1)

    # Copy only the main database file: after a checkpoint it must stand alone.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    copied = tmp_path / "copied-only.sqlite3"
    shutil.copy2(source, copied)
    with closing(sqlite3.connect(copied)) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert raw.execute(
            "SELECT COUNT(*) FROM archive_path_acceptances"
        ).fetchone()[0] == 1
        validate_archive_facts(raw)
        assert load_archive_plan(raw, "batch-schema-four").inventory == inventory
        stored = raw.execute(
            """SELECT b.acceptance_id,a.check_id
               FROM archive_path_bindings AS b
               JOIN archive_path_acceptances AS a
                 ON a.acceptance_id=b.acceptance_id"""
        ).fetchone()
        assert stored == (acceptance_.acceptance_id, acceptance_.check_id)

    second = migrate_archive_copy(
        source, tmp_path / "second.backup", temporary_copy_confirmed=True,
    )
    assert second.status == "already_current"
    assert second.source_version == "4"
    assert second.backup_path.is_file()


def test_repository_accepts_one_batch_atomically_or_not_at_all(library, tmp_path):
    conn, root = library[1], tmp_path / "repository-root"
    source = library[0]
    a_target, a_first, _ = baseline(conn, root, "gallery/a.bin", b"AAAA", tag="ra1")
    b_target, b_first, _ = baseline(conn, root, "gallery/b.bin", b"BBBB", tag="rb1")

    time.sleep(0.02)
    rewrite(a_target, b"CCCC")
    rewrite(b_target, b"DDDD")
    _, a_entry, a_observed = scan_one(conn, root, "gallery/a.bin",
                                      run_id="run-ra2", check_id="check-ra2")
    _, b_entry, b_observed = scan_one(conn, root, "gallery/b.bin",
                                      run_id="run-rb2", check_id="check-rb2")
    a_resolution = resolve_observed_content(conn, a_observed)
    b_resolution = resolve_observed_content(conn, b_observed)
    assert a_resolution.kind is ResolutionKind.NEW_REVISION
    assert b_resolution.kind is ResolutionKind.NEW_REVISION
    conflicting = replace(
        a_resolution, kind=ResolutionKind.REPLACEMENT, previous_content=None,
        content=ContentVersion(a_first.content.asset_id, 9, 4, sha(b"CCCC")),
    )
    before = counts(conn)
    with ArchiveLibraryRepository(source, temporary_copy_confirmed=True) as repository:
        with pytest.raises(ArchiveBindingConflictError):
            repository.apply_archive_resolutions(
                ((a_resolution, a_observed), (conflicting, a_observed)),
            )
        assert counts(conn) == before
        assert load_content_binding(conn, a_entry.path).content == a_first.content
        assert load_content_binding(conn, b_entry.path).content == b_first.content

        acceptances = repository.apply_archive_resolutions(
            ((b_resolution, b_observed), (a_resolution, a_observed)),
        )
    assert [item.path_key for item in acceptances] == sorted(
        item.path_key for item in acceptances)
    assert load_content_binding(conn, a_entry.path).content == a_resolution.content
    assert load_content_binding(conn, b_entry.path).content == b_resolution.content
    validate_archive_facts(conn)
