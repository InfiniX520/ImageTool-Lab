"""R2 counter-examples as assertions: asset-level history and real directory aliases.

Offline and synthetic: temporary directories, one migrated copy of an empty v1
library, real bytes read by LocalObserver, no network, account, production database
or real image library.  The two audited defects behind this module are
docs/audits/2026-11-13-a1-acceptance/FINDINGS.md AR4-04 (a lagging hardlink name
and a renamed object each re-derived an occupied content generation) and AR4-05
(one Windows directory entry reached through another spelling became a second
archive asset, and plan save then refused the alias).

The rule under test: generations belong to the *logical asset* the enumerator
derived from the physical object, and identity belongs to the *directory entry*,
never to the spelling a caller typed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from core.archive_inventory import (
    ArchiveEntry, ArtworkGroup, CapacityPolicy, InventoryPolicy,
    build_archive_inventory, plan_archive_capacity,
)
from core.archive_plan_store import load_archive_plan, save_archive_plan
from core.archive_schema import (
    ArchiveBindingConflictError, apply_content_resolution, load_content_binding,
    migrate_archive_copy, resolve_observed_content, validate_archive_facts,
    _content_key,
)
from core.archive_scope import (
    CandidateSnapshot, ResolutionKind, ScopeEnumerator, ScopeRequest, consume_scope_pages,
    observe_candidate,
)
from core.library_repository import LibraryRepository
from core.local_observation import LocalObserver
from core.preservation_policy import ContentVersion
from core.task_contracts import CollectionRequest, PathRef

TIME = "2026-11-12T09:00:00+00:00"
T1 = "2026-11-12T09:10:00+00:00"
T2 = "2026-11-12T09:20:00+00:00"
T3 = "2026-11-12T09:30:00+00:00"


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


def _consume(source):
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
        root, ScopeRequest(("gallery",), root_revision=revision, page_size=100),
    ))


def select_files(root: Path, relatives, *, revision: int = 1):
    """The real "user picked this file" route: explicit file selections."""
    return _consume(ScopeEnumerator(
        root, ScopeRequest((), tuple(relatives), root_revision=revision),
    ))


def select_one(root: Path, relative: str, *, revision: int = 1):
    receipt, entries, facts = select_files(root, (relative,), revision=revision)
    assert len(entries) == 1, [entry.path.relative_path for entry in entries]
    assert receipt.scope_complete
    return receipt, entries[0], facts[0]


def by_path(entries, facts, relative: str):
    for entry, fact in zip(entries, facts):
        if entry.path.relative_path == relative:
            return entry, fact
    raise AssertionError(
        "candidate not enumerated: %s (have %r)"
        % (relative, [entry.path.relative_path for entry in entries]))


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
        acceptance = apply_content_resolution(
            conn, resolution, observed=observed, bound_at=bound_at,
        )
    return resolution, acceptance


def path_of(relative: str) -> PathRef:
    return PathRef(kind="project", root_id="project", relative_path=relative)


def byte_generations(conn, asset_id):
    """The generations that hold bytes; revision 0 is the unknown placeholder."""
    return {
        row[0]: (row[1], row[2]) for row in conn.execute(
            "SELECT revision,size_bytes,sha256 FROM archive_contents "
            "WHERE asset_id=? AND revision > 0 ORDER BY revision", (asset_id,),
        )
    }


def placeholder(conn, asset_id):
    return conn.execute(
        "SELECT size_bytes,sha256 FROM archive_contents WHERE asset_id=? AND revision=0",
        (asset_id,),
    ).fetchone()


def binding_pairs(conn):
    return conn.execute(
        "SELECT path_key,asset_id,revision,acceptance_id FROM archive_path_bindings "
        "ORDER BY path_key"
    ).fetchall()


def state(conn):
    return {
        "contents": conn.execute(
            "SELECT content_key,asset_id,revision,size_bytes,sha256 FROM archive_contents "
            "ORDER BY asset_id,revision").fetchall(),
        "assets": conn.execute(
            "SELECT content_key,asset_id,revision,path_key,path_json,source_kind,size_bytes,"
            "sha256 FROM archive_assets ORDER BY asset_id,revision,path_key").fetchall(),
        "bindings": binding_pairs(conn),
        "acceptances": conn.execute(
            "SELECT acceptance_id,path_key,content_key,root_revision,check_id "
            "FROM archive_path_acceptances ORDER BY acceptance_id").fetchall(),
        "checks": conn.execute(
            "SELECT check_id,path_key,content_key,root_revision FROM archive_local_checks "
            "ORDER BY check_id").fetchall(),
    }


# ---------------------------------------------------------------------------
# AR4-04: the next free generation belongs to the asset, not to one path.
# ---------------------------------------------------------------------------


def test_lagging_hardlink_allocates_the_asset_next_generation(library, tmp_path):
    conn, root = library[1], tmp_path / "hardlink-root"
    first = write(root, "gallery/a.bin", b"AAAA")
    try:
        os.link(first, root / "gallery" / "b.bin")
    except OSError as exc:
        pytest.skip("Synthetic hardlink unavailable: " + type(exc).__name__)

    _, entries, facts = enumerate_gallery(root)
    a_entry, a_fact = by_path(entries, facts, "gallery/a.bin")
    b_entry, b_fact = by_path(entries, facts, "gallery/b.bin")
    assert a_entry.physical_identity == b_entry.physical_identity

    a_receipt, a_1 = accept(conn, observe(
        conn, root, a_entry, a_fact, enumerate_gallery(root)[0],
        run_id="run-hl1", check_id="check-hl1"))
    _, b_1 = accept(conn, observe(
        conn, root, b_entry, b_fact, enumerate_gallery(root)[0],
        run_id="run-hl2", check_id="check-hl2"))
    assert a_1.content == b_1.content
    asset_id = a_1.content.asset_id
    assert a_1.content.revision == 1 and a_1.content.sha256 == sha(b"AAAA")

    # A advances to revision 2 while B keeps pointing at revision 1.
    rewrite(first, b"BBBB")
    _, a_entry2, a_observed2 = scan_one(conn, root, "gallery/a.bin",
                                        run_id="run-hl3", check_id="check-hl3")
    a_2, _ = accept(conn, a_observed2)
    assert a_2.content.asset_id == asset_id and a_2.content.revision == 2
    assert a_2.content.sha256 == sha(b"BBBB")

    # The shared object changes again and only B is rescanned: B must continue the
    # asset chain from revision 2 instead of re-deriving an occupied generation.
    rewrite(root / "gallery" / "b.bin", b"CCCC")
    _, b_entry2, b_observed2 = scan_one(conn, root, "gallery/b.bin",
                                        run_id="run-hl4", check_id="check-hl4")
    lagging = resolve_observed_content(conn, b_observed2)
    assert lagging.kind is ResolutionKind.NEW_REVISION
    assert lagging.content.revision == 3
    b_3, _ = accept(conn, b_observed2)
    assert b_3.content.asset_id == asset_id and b_3.content.revision == 3
    assert b_3.content.sha256 == sha(b"CCCC")

    history = byte_generations(conn, asset_id)
    assert history == {1: (4, sha(b"AAAA")), 2: (4, sha(b"BBBB")), 3: (4, sha(b"CCCC"))}
    assert placeholder(conn, asset_id) == (4, None)
    assert load_content_binding(conn, a_entry2.path).content == a_2.content
    assert load_content_binding(conn, b_entry2.path).content == b_3.content

    # A rescan of the lagging name is a REUSE of its own last generation.
    _, _, b_observed3 = scan_one(conn, root, "gallery/b.bin",
                                 run_id="run-hl5", check_id="check-hl5")
    again = resolve_observed_content(conn, b_observed3)
    assert again.kind is ResolutionKind.REUSE and again.content == b_3.content
    validate_archive_facts(conn)


def test_renamed_file_continues_the_asset_history(library, tmp_path):
    conn, root = library[1], tmp_path / "rename-root"
    target = write(root, "gallery/a.bin", b"AAAA")
    _, entry, first = accept_and_entry(conn, root, "gallery/a.bin", tag="rn1")
    asset_id = first.content.asset_id
    assert first.content.revision == 1

    rewrite(target, b"BBBB")
    os.replace(target, target.with_name("c.bin"))
    _, c_entry, c_observed = scan_one(conn, root, "gallery/c.bin",
                                      run_id="run-rn2", check_id="check-rn2")
    assert c_entry.physical_identity == entry.physical_identity
    decision = resolve_observed_content(conn, c_observed)
    assert decision.kind is ResolutionKind.BASELINE
    assert decision.content.revision == 2, (
        "the renamed object re-derived revision %d instead of continuing the asset"
        % decision.content.revision)
    _, second = accept(conn, c_observed)
    assert second.content.asset_id == asset_id and second.content.revision == 2
    assert second.content.sha256 == sha(b"BBBB")
    assert byte_generations(conn, asset_id) == {
        1: (4, sha(b"AAAA")), 2: (4, sha(b"BBBB")),
    }
    assert placeholder(conn, asset_id) == (4, None)
    old = load_content_binding(conn, entry.path)
    assert old is not None and old.content.revision == 1, (
        "the rename rewrote the old path's binding")
    validate_archive_facts(conn)


def accept_and_entry(conn, root, relative, *, tag):
    _, entry, observed = scan_one(conn, root, relative,
                                  run_id="run-" + tag, check_id="check-" + tag)
    resolution, acceptance = accept(conn, observed)
    return resolution, entry, acceptance


def test_both_hardlink_names_can_be_replaced_independently(library, tmp_path):
    conn, root = library[1], tmp_path / "hardlink-replace-root"
    first = write(root, "gallery/a.bin", b"AAAA")
    try:
        os.link(first, root / "gallery" / "b.bin")
    except OSError as exc:
        pytest.skip("Synthetic hardlink unavailable: " + type(exc).__name__)
    receipt, entries, facts = enumerate_gallery(root)
    a_entry, a_fact = by_path(entries, facts, "gallery/a.bin")
    b_entry, b_fact = by_path(entries, facts, "gallery/b.bin")
    accept(conn, observe(conn, root, a_entry, a_fact, receipt,
                         run_id="run-hr1", check_id="check-hr1"))
    _, first_b = accept(conn, observe(
        conn, root, b_entry, b_fact, receipt, run_id="run-hr2", check_id="check-hr2"))
    asset_id = first_b.content.asset_id

    # Each name gets its own new physical object, one at a time.  Each replacement
    # is the next byte generation of the same logical asset.
    for step, (relative, payload) in enumerate(
            (("gallery/a.bin", b"BBBB"), ("gallery/b.bin", b"CCCC")), start=1):
        target = root.joinpath(*relative.split("/"))
        incoming = target.with_name(".incoming-hr%d" % step)
        incoming.write_bytes(payload)
        os.replace(incoming, target)
        bump_mtime(target)
        _, _, observed = scan_one(conn, root, relative,
                                  run_id="run-hr%d" % (step + 2),
                                  check_id="check-hr%d" % (step + 2))
        pending = resolve_observed_content(conn, observed)
        assert pending.kind is ResolutionKind.NEEDS_CONFIRMATION
        resolution, acceptance = accept(conn, observed, confirmed=True)
        assert resolution.kind is ResolutionKind.REPLACEMENT
        assert resolution.content.asset_id == asset_id
        assert resolution.content.revision == step + 1, (
            "replacement %d allocated revision %d" % (step, resolution.content.revision))
        assert resolution.content.sha256 == sha(payload)
        binding = load_content_binding(conn, PathRef("project", "project", relative))
        assert binding.physical_identity == observed.observed_identity
        assert binding.content.revision == step + 1
    assert byte_generations(conn, asset_id) == {
        1: (4, sha(b"AAAA")), 2: (4, sha(b"BBBB")), 3: (4, sha(b"CCCC")),
    }
    validate_archive_facts(conn)


def test_content_round_trip_keeps_every_generation_of_one_asset(library, tmp_path):
    conn, root = library[1], tmp_path / "roundtrip-root"
    target = write(root, "gallery/a.bin", b"AAAA")
    _, entry, first = accept_and_entry(conn, root, "gallery/a.bin", tag="rt1")
    asset_id = first.content.asset_id
    for step, payload in ((2, b"BBBB"), (3, b"AAAA")):
        time.sleep(0.02)
        rewrite(target, payload)
        _, _, observed = scan_one(conn, root, "gallery/a.bin",
                                  run_id="run-rt%d" % step, check_id="check-rt%d" % step)
        resolution, _ = accept(conn, observed)
        assert resolution.kind is ResolutionKind.NEW_REVISION
        assert resolution.content.revision == step, (
            "round trip step %d allocated revision %d" % (step, resolution.content.revision))
    assert byte_generations(conn, asset_id) == {
        1: (4, sha(b"AAAA")), 2: (4, sha(b"BBBB")), 3: (4, sha(b"AAAA")),
    }
    assert placeholder(conn, asset_id) == (4, None)
    validate_archive_facts(conn)


def test_apply_refuses_a_generation_allocated_by_another_path(library, tmp_path):
    """Two paths of one asset both resolved revision 2; only the first may keep it."""
    conn, root = library[1], tmp_path / "concurrent-generation-root"
    first = write(root, "gallery/a.bin", b"AAAA")
    try:
        os.link(first, root / "gallery" / "b.bin")
    except OSError as exc:
        pytest.skip("Synthetic hardlink unavailable: " + type(exc).__name__)
    receipt, entries, facts = enumerate_gallery(root)
    a_entry, a_fact = by_path(entries, facts, "gallery/a.bin")
    b_entry, b_fact = by_path(entries, facts, "gallery/b.bin")
    _, a_first = accept(conn, observe(conn, root, a_entry, a_fact, receipt,
                                      run_id="run-cg1", check_id="check-cg1"))
    _, b_first = accept(conn, observe(conn, root, b_entry, b_fact, receipt,
                                      run_id="run-cg2", check_id="check-cg2"))
    asset_id = a_first.content.asset_id

    # A is observed with BBBB, then the object changes again and B is observed with
    # CCCC.  Both decisions see asset maximum 1, so both would claim revision 2.
    rewrite(first, b"BBBB")
    _, _, a_observed = scan_one(conn, root, "gallery/a.bin",
                                run_id="run-cg3", check_id="check-cg3")
    a_resolution = resolve_observed_content(conn, a_observed)
    assert a_resolution.content.revision == 2 and a_resolution.content.sha256 == sha(b"BBBB")
    time.sleep(0.02)
    rewrite(first, b"CCCC")
    _, b_entry3, b_observed = scan_one(conn, root, "gallery/b.bin",
                                       run_id="run-cg4", check_id="check-cg4")
    b_resolution = resolve_observed_content(conn, b_observed)
    assert b_resolution.content.revision == 2 and b_resolution.content.sha256 == sha(b"CCCC")

    # B wins the generation; A's stale decision must be refused before any write.
    accept(conn, b_observed)
    before = state(conn)
    with pytest.raises(ArchiveBindingConflictError) as caught:
        with transaction(conn):
            apply_content_resolution(
                conn, a_resolution, observed=a_observed, bound_at=T1)
    assert caught.value.reason == "asset_revision_allocated_concurrently"
    assert state(conn) == before
    assert byte_generations(conn, asset_id)[2] == (4, sha(b"CCCC"))
    validate_archive_facts(conn)

    # Re-observing A allocates the next free generation instead of failing.
    _, _, a_again = scan_one(conn, root, "gallery/a.bin",
                             run_id="run-cg5", check_id="check-cg5")
    retry = resolve_observed_content(conn, a_again)
    assert retry.content.revision == 3
    accept(conn, a_again)
    validate_archive_facts(conn)


# ---------------------------------------------------------------------------
# AR4-05: one directory entry, one identity; no global case folding.
# ---------------------------------------------------------------------------


def test_ascii_case_alias_of_one_entry_stays_one_asset(library, tmp_path):
    conn, root = library[1], tmp_path / "alias-ascii-root"
    write(root, "gallery/A.bin", b"AAAA")
    receipt_upper, entry_upper, fact_upper = select_one(root, "gallery/A.bin")
    assert entry_upper.path.relative_path == "gallery/A.bin"
    _, first = accept(conn, observe(conn, root, entry_upper, fact_upper, receipt_upper,
                                    run_id="run-al1", check_id="check-al1"))

    if not os.path.samefile(root / "gallery" / "A.bin", root / "gallery" / "a.bin"):
        pytest.skip("The synthetic root is case-sensitive; a case alias cannot be tested")
    receipt_lower, entry_lower, fact_lower = select_one(root, "gallery/a.bin")
    # The entry carries the real directory spelling, so both requests agree.
    assert entry_lower.path.relative_path == "gallery/A.bin"
    assert entry_lower.path.identity_key == entry_upper.path.identity_key
    assert entry_lower.physical_identity == entry_upper.physical_identity

    observed = observe(conn, root, entry_lower, fact_lower, receipt_lower,
                       run_id="run-al2", check_id="check-al2")
    decision = resolve_observed_content(conn, observed)
    assert decision.kind is ResolutionKind.REUSE
    _, acceptance = accept(conn, observed)
    assert acceptance.content == first.content
    assert len(binding_pairs(conn)) == 1
    spellings = {
        json.loads(row[4])["relative_path"] for row in conn.execute(
            "SELECT content_key,asset_id,revision,path_key,path_json FROM archive_assets")
    }
    assert spellings == {"gallery/A.bin"}, (
        "the stored row lost the first display spelling: %r" % (spellings,))
    sources = {row[0] for row in conn.execute("SELECT source_kind FROM archive_assets")}
    assert sources == {"observed_existing"}

    inventory = build_archive_inventory(
        (ArtworkGroup("work-1", (
            ArchiveEntry(first.content, entry_upper.path, 0, entry_upper.physical_identity),
        )),),
        batch_id="batch-alias-ascii",
        policy=InventoryPolicy(target_package_bytes=1024, batch_budget_bytes=10 ** 9),
    )
    capacity = plan_archive_capacity(
        inventory, available_additional_bytes=10 ** 9,
        policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
    )
    save_archive_plan(conn, inventory, capacity, receipt=receipt_lower, root_revision=1)
    rows = conn.execute(
        "SELECT path_json,source_kind FROM archive_assets WHERE path_key=?",
        (entry_upper.path.identity_key,)).fetchall()
    assert {json.loads(row[0])["relative_path"] for row in rows} == {"gallery/A.bin"}
    assert {row[1] for row in rows} == {"observed_existing"}
    assert load_archive_plan(conn, "batch-alias-ascii").inventory == inventory
    validate_archive_facts(conn)


def test_mixed_script_case_alias_is_one_directory_entry(library, tmp_path):
    conn, root = library[1], tmp_path / "alias-mixed-root"
    write(root, "gallery/画A.bin", b"AAAA")
    receipt_upper, entry_upper, fact_upper = select_one(root, "gallery/画A.bin")
    assert entry_upper.path.relative_path == "gallery/画A.bin"
    _, first = accept(conn, observe(conn, root, entry_upper, fact_upper, receipt_upper,
                                    run_id="run-mx1", check_id="check-mx1"))

    if not os.path.samefile(root / "gallery" / "画A.bin", root / "gallery" / "画a.bin"):
        pytest.skip("The synthetic root is case-sensitive; a case alias cannot be tested")
    receipt_lower, entry_lower, fact_lower = select_one(root, "gallery/画a.bin")
    assert entry_lower.path.relative_path == "gallery/画A.bin", (
        "the mixed-script alias was not normalised to the real directory entry")
    assert entry_lower.path.identity_key == entry_upper.path.identity_key, (
        "two selections of one Windows directory entry produced two archive identities")
    observed = observe(conn, root, entry_lower, fact_lower, receipt_lower,
                       run_id="run-mx2", check_id="check-mx2")
    assert resolve_observed_content(conn, observed).kind is ResolutionKind.REUSE
    accept(conn, observed)
    assert len(binding_pairs(conn)) == 1
    assert load_content_binding(conn, entry_lower.path).content == first.content

    inventory = build_archive_inventory(
        (ArtworkGroup("work-1", (
            ArchiveEntry(first.content, entry_upper.path, 0, entry_upper.physical_identity),
        )),),
        batch_id="batch-alias-mixed",
        policy=InventoryPolicy(target_package_bytes=1024, batch_budget_bytes=10 ** 9),
    )
    capacity = plan_archive_capacity(
        inventory, available_additional_bytes=10 ** 9,
        policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
    )
    save_archive_plan(conn, inventory, capacity, receipt=receipt_lower, root_revision=1)
    assert load_archive_plan(conn, "batch-alias-mixed").inventory == inventory
    validate_archive_facts(conn)


def test_case_folding_never_crosses_a_real_directory_entry(library, tmp_path):
    conn, root = library[1], tmp_path / "alias-negative-root"
    write(root, "gallery/straße.bin", b"AAAA")
    write(root, "gallery/strasse.bin", b"BBBB")
    receipt, entries, _facts = enumerate_gallery(root)
    spellings = {entry.path.relative_path: entry for entry in entries}
    assert set(spellings) == {"gallery/straße.bin", "gallery/strasse.bin"}
    assert spellings["gallery/straße.bin"].path.identity_key != (
        spellings["gallery/strasse.bin"].path.identity_key)

    # NFC and NFD are different byte sequences for NTFS, so they stay distinct.
    nfc = path_of("gallery/caf\u00e9.bin")
    nfd = path_of("gallery/cafe\u0301.bin")
    assert nfc.identity_key != nfd.identity_key

    # Selecting the uppercase spelling still resolves to the real directory entry.
    receipt_upper, entry_upper, _ = select_one(root, "gallery/STRASSE.bin")
    assert entry_upper.path.relative_path == "gallery/strasse.bin"
    assert entry_upper.path.identity_key == path_of("gallery/strasse.bin").identity_key
    assert receipt_upper.scope_complete
    validate_archive_facts(conn)


def test_saving_an_alias_plan_never_rewrites_the_stored_spelling(library, tmp_path):
    conn = library[1]
    root = tmp_path / "alias-plan-root"
    write(root, "gallery/A.bin", b"AAAA")
    receipt, entry, fact = select_one(root, "gallery/A.bin")
    _, first = accept(conn, observe(conn, root, entry, fact, receipt,
                                    run_id="run-ap1", check_id="check-ap1"))
    inventory = build_archive_inventory(
        (ArtworkGroup("work-1", (
            ArchiveEntry(first.content, entry.path, 0, entry.physical_identity),
        )),),
        batch_id="batch-alias-repeat",
        policy=InventoryPolicy(target_package_bytes=1024, batch_budget_bytes=10 ** 9),
    )
    capacity = plan_archive_capacity(
        inventory, available_additional_bytes=10 ** 9,
        policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
    )
    save_archive_plan(conn, inventory, capacity, receipt=receipt, root_revision=1)
    stored_before = conn.execute(
        "SELECT path_json,source_kind FROM archive_assets ORDER BY path_key").fetchall()
    assets_before = conn.execute("SELECT COUNT(*) FROM archive_assets").fetchone()[0]

    if not os.path.samefile(root / "gallery" / "A.bin", root / "gallery" / "a.bin"):
        pytest.skip("The synthetic root is case-sensitive; a case alias cannot be tested")
    # A second selection through the alias spelling prepares another plan for the
    # same entry.  It must reuse the stored row, keep its spelling and source kind,
    # and never claim a user-selected asset over an observed one.
    receipt_lower, alias_entry, _ = select_one(root, "gallery/a.bin")
    assert alias_entry.path.relative_path == "gallery/A.bin"
    alias_inventory = build_archive_inventory(
        (ArtworkGroup("work-1", (
            ArchiveEntry(first.content, alias_entry.path, 0, alias_entry.physical_identity),
        )),),
        batch_id="batch-alias-second",
        policy=InventoryPolicy(target_package_bytes=1024, batch_budget_bytes=10 ** 9),
    )
    alias_capacity = plan_archive_capacity(
        alias_inventory, available_additional_bytes=10 ** 9,
        policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
    )
    saved = save_archive_plan(
        conn, alias_inventory, alias_capacity, receipt=receipt_lower, root_revision=1)
    assert saved.replayed is False
    stored_after = conn.execute(
        "SELECT path_json,source_kind FROM archive_assets ORDER BY path_key").fetchall()
    assert stored_after == stored_before, (
        "the alias plan added or rewrote a stored path row: %r" % (stored_after,))
    assets_after = conn.execute("SELECT COUNT(*) FROM archive_assets").fetchone()[0]
    assert assets_after == assets_before, (
        "the alias plan inserted another asset row for one directory entry")
    assert load_archive_plan(conn, "batch-alias-repeat").inventory == inventory
    assert load_archive_plan(conn, "batch-alias-second").inventory == alias_inventory
    validate_archive_facts(conn)
