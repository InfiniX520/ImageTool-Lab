"""F3 handoff tests: candidate -> real observation -> content generation.

Every case is synthetic and offline: temporary directories, a migrated copy of
an empty v1 library, and no network, account, production database or real image
library.  The tests exercise the actual LocalObserver bytes, not hand-built
ContentVersion replacements.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest

from core.archive_inventory import (
    ArchiveEntry, ArtworkGroup, CapacityPolicy, InventoryPolicy,
    build_archive_inventory, plan_archive_capacity,
)
from core.archive_plan_store import (
    ArchiveConfirmationRequiredError, ArchivePlanStoreError, load_archive_plan,
    prepare_archive_plan, save_archive_plan,
)
from core.archive_schema import (
    ArchiveIdentityConfirmationRequired, apply_content_resolution,
    load_content_binding, migrate_archive_copy, resolve_observed_content,
    validate_archive_facts,
)
from core.archive_scope import (
    CandidateSnapshot, ResolutionKind, SCOPE_RECEIPT_SCHEMA_VERSION, ScopeEnumerator,
    ScopeReceipt, ScopeRequest, consume_scope_pages, observe_candidate,
    resolve_content_version,
)
from core.local_observation import FileIdentity, LocalObserver, ObservationStatus
from core.library_repository import LibraryRepository
from core.preservation_policy import ContentVersion
from core.task_contracts import CollectionRequest, ContractError, PathRef

TIME = "2026-11-12T08:00:00+00:00"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def conn(tmp_path):
    source = tmp_path / "library.sqlite3"
    with LibraryRepository(source) as repository:
        repository.submit(CollectionRequest("request-1", "synthetic-account"), "submit-1")
    migrate_archive_copy(
        source, tmp_path / "before-migration.sqlite3", temporary_copy_confirmed=True,
    )
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
    finally:
        connection.close()


def write(root: Path, relative: str, data: bytes) -> Path:
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def bump_mtime(target: Path) -> None:
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))


def enumerate_gallery(root: Path, *, revision: int = 1, page_size: int = 100):
    source = ScopeEnumerator(
        root, ScopeRequest(("gallery",), root_revision=revision, page_size=page_size),
    )
    pages = []
    page = source.page()
    pages.append(page)
    while page.cursor is not None:
        page = source.page(page.cursor)
        pages.append(page)
    receipt = consume_scope_pages(pages, source=source)
    assert receipt.scope_complete and receipt.snapshot_verified
    entries = tuple(entry for item in pages for entry in item.entries)
    facts = tuple(fact for item in pages for fact in item.entry_facts)
    assert len(entries) == len(facts) == receipt.consumed_entries
    return pages, receipt, entries, facts


def observe_entry(
    conn: sqlite3.Connection, root: Path, entry: ArchiveEntry, fact, receipt: ScopeReceipt,
    *, run_id: str, check_id: str, expected_content: ContentVersion | None = None,
):
    if expected_content is None:
        binding = load_content_binding(conn, entry.path)
        expected_content = binding.content if binding is not None else entry.content
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
        candidate, observer, expected_content=expected_content,
        run_id=run_id, check_id=check_id,
    )


def resolve_and_apply(
    conn: sqlite3.Connection, root: Path, entry: ArchiveEntry, fact, receipt: ScopeReceipt,
    *, run_id: str, check_id: str, expected_content: ContentVersion | None = None,
    replacement_confirmed: bool = False,
):
    observed = observe_entry(
        conn, root, entry, fact, receipt, run_id=run_id, check_id=check_id,
        expected_content=expected_content,
    )
    resolution = resolve_observed_content(
        conn, observed, replacement_confirmed=replacement_confirmed,
    )
    if resolution.requires_attention:
        return resolution, observed
    apply_content_resolution(
        conn, resolution, observed=observed, bound_at=TIME,
        source_kind="observed_existing",
    )
    conn.commit()
    return resolution, observed


def plan_entry(entry: ArchiveEntry, content: ContentVersion) -> ArchiveEntry:
    return ArchiveEntry(content, entry.path, entry.page_index, entry.physical_identity)


def inventory_for(entries, batch_id: str):
    inventory = build_archive_inventory(
        (ArtworkGroup("work-1", tuple(entries)),),
        batch_id=batch_id,
        policy=InventoryPolicy(target_package_bytes=1024, batch_budget_bytes=10**9),
    )
    capacity = plan_archive_capacity(
        inventory, available_additional_bytes=10**9,
        policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
    )
    return inventory, capacity


def save_batch(conn, entries, batch_id: str, receipt: ScopeReceipt, *,
               root_revision: int = 1):
    inventory, capacity = inventory_for(entries, batch_id)
    result = save_archive_plan(
        conn, inventory, capacity, receipt=receipt, root_revision=root_revision,
    )
    return result, inventory


def test_first_baseline_creates_observation_and_content_generation(conn, tmp_path):
    root = tmp_path / "baseline-root"
    payload = b"AAAA"
    write(root, "gallery/art.png", payload)
    pages, receipt, entries, facts = enumerate_gallery(root)
    assert len(entries) == 1
    entry, fact = entries[0], facts[0]
    assert fact.physical_identity == entry.physical_identity
    assert receipt.schema_version == SCOPE_RECEIPT_SCHEMA_VERSION == 2
    assert receipt.entry_facts == (fact,)

    resolution, observed = resolve_and_apply(
        conn, root, entry, fact, receipt, run_id="run-baseline", check_id="check-baseline",
    )
    assert observed.digest_complete
    assert observed.observation.status is ObservationStatus.BASELINE_OBSERVED
    assert resolution.kind is ResolutionKind.BASELINE
    assert resolution.content is not None and resolution.content.revision == 1
    assert resolution.content.sha256 == sha(payload)
    assert resolution.previous_content is not None and resolution.previous_content.sha256 is None

    binding = load_content_binding(conn, entry.path)
    assert binding is not None and binding.content == resolution.content
    assert binding.physical_identity == entry.physical_identity
    observation_row = conn.execute(
        "SELECT state,observation_status,digest_complete,file_device,file_index "
        "FROM archive_local_checks"
    ).fetchone()
    assert observation_row[0] == "unknown"
    assert observation_row[1] == "baseline_observed"
    assert observation_row[2] == 1
    assert int(observation_row[3]) == entry.physical_identity.device
    assert int(observation_row[4]) == entry.physical_identity.file_index

    saved, inventory = save_batch(
        conn, [plan_entry(entry, resolution.content)], "batch-baseline", receipt,
    )
    assert not saved.replayed and saved.plan.inventory == inventory
    assert load_archive_plan(conn, "batch-baseline").inventory == inventory
    assert saved.plan.scope_receipt["entry_facts"][0]["mtime_ns"] == fact.mtime_ns
    assert saved.plan.scope_receipt["entry_facts"][0]["content_sha256"] == sha(payload)
    validate_archive_facts(conn)


def test_repeat_reuses_the_same_generation_and_old_plan_still_loads(conn, tmp_path):
    root = tmp_path / "reuse-root"
    payload = b"AAAA"
    write(root, "gallery/art.png", payload)
    pages, receipt, entries, facts = enumerate_gallery(root)
    entry, fact = entries[0], facts[0]
    first, _ = resolve_and_apply(
        conn, root, entry, fact, receipt, run_id="run-first", check_id="check-first",
    )
    first_saved, first_inventory = save_batch(
        conn, [plan_entry(entry, first.content)], "batch-reuse-one", receipt,
    )

    pages2, receipt2, entries2, facts2 = enumerate_gallery(root)
    assert receipt2.snapshot_id != receipt.snapshot_id
    resolution, observed = resolve_and_apply(
        conn, root, entries2[0], facts2[0], receipt2,
        run_id="run-second", check_id="check-second",
    )
    assert observed.observation.status is ObservationStatus.HASH_MATCH
    assert resolution.kind is ResolutionKind.REUSE
    assert resolution.content == first.content
    assert load_content_binding(conn, entry.path).content == first.content
    assert conn.execute("SELECT COUNT(*) FROM archive_contents").fetchone()[0] == 2
    second_saved, second_inventory = save_batch(
        conn, [plan_entry(entries2[0], resolution.content)], "batch-reuse-two", receipt2,
    )
    assert second_saved.plan.inventory == second_inventory
    assert load_archive_plan(conn, "batch-reuse-one").inventory == first_inventory
    assert load_archive_plan(conn, "batch-reuse-two").inventory == second_inventory
    validate_archive_facts(conn)


def test_in_place_same_size_rewrite_advances_revision_and_keeps_old_plan(conn, tmp_path):
    root = tmp_path / "rewrite-root"
    target = write(root, "gallery/art.png", b"AAAA")
    pages, receipt, entries, facts = enumerate_gallery(root)
    entry = entries[0]
    first, _ = resolve_and_apply(
        conn, root, entry, facts[0], receipt, run_id="run-one", check_id="check-one",
    )
    first_saved, first_inventory = save_batch(
        conn, [plan_entry(entry, first.content)], "batch-rewrite-one", receipt,
    )
    time.sleep(0.02)
    target.write_bytes(b"BBBB")
    bump_mtime(target)
    pages2, receipt2, entries2, facts2 = enumerate_gallery(root)
    assert entries2[0].content.size_bytes == 4

    resolution, observed = resolve_and_apply(
        conn, root, entries2[0], facts2[0], receipt2,
        run_id="run-two", check_id="check-two",
    )
    assert observed.observation.status is ObservationStatus.CHANGED
    assert observed.observation.evidence.observed_sha256 == sha(b"BBBB")
    assert resolution.kind is ResolutionKind.NEW_REVISION
    assert resolution.content.revision == first.content.revision + 1
    assert resolution.content.sha256 == sha(b"BBBB")
    assert resolution.previous_content == first.content
    assert load_content_binding(conn, entry.path).content == resolution.content

    second_saved, second_inventory = save_batch(
        conn, [plan_entry(entries2[0], resolution.content)], "batch-rewrite-two", receipt2,
    )
    old = load_archive_plan(conn, "batch-rewrite-one")
    assert old.inventory == first_inventory
    assert old.inventory.assets[0].content.sha256 == sha(b"AAAA")
    assert second_saved.plan.inventory == second_inventory
    assert second_saved.plan.inventory.assets[0].content.sha256 == sha(b"BBBB")
    assert conn.execute("SELECT COUNT(*) FROM archive_contents").fetchone()[0] == 3
    changed = conn.execute(
        "SELECT state,observation_status,observed_sha256 FROM archive_local_checks "
        "WHERE check_id='check-two'"
    ).fetchone()
    assert changed[0] == "changed" and changed[1] == "changed"
    assert changed[2] == sha(b"BBBB")
    validate_archive_facts(conn)


def test_size_change_advances_revision_and_recalculates_capacity(conn, tmp_path):
    root = tmp_path / "size-change-root"
    target = write(root, "gallery/art.png", b"AAAA")
    pages, receipt, entries, facts = enumerate_gallery(root)
    entry = entries[0]
    first, _ = resolve_and_apply(
        conn, root, entry, facts[0], receipt, run_id="run-size-one", check_id="check-size-one",
    )
    save_batch(conn, [plan_entry(entry, first.content)], "batch-size-one", receipt)
    time.sleep(0.02)
    target.write_bytes(b"BBBBBB")
    bump_mtime(target)
    pages2, receipt2, entries2, facts2 = enumerate_gallery(root)
    assert entries2[0].content.size_bytes == 6
    resolution, observed = resolve_and_apply(
        conn, root, entries2[0], facts2[0], receipt2,
        run_id="run-size-two", check_id="check-size-two",
    )
    assert observed.digest_complete
    assert observed.observed_size == 6
    assert resolution.kind is ResolutionKind.NEW_REVISION
    assert resolution.content.size_bytes == 6
    assert resolution.content.revision == first.content.revision + 1
    saved, inventory = save_batch(
        conn, [plan_entry(entries2[0], resolution.content)], "batch-size-two", receipt2,
    )
    assert inventory.member_payload_bytes == 6
    assert saved.plan.inventory.member_payload_bytes == 6
    assert load_archive_plan(conn, "batch-size-one").inventory.assets[0].content.size_bytes == 4
    validate_archive_facts(conn)


def test_os_replace_same_name_returns_structured_confirmation_then_replacement(conn, tmp_path):
    root = tmp_path / "replace-root"
    target = write(root, "gallery/art.png", b"AAAA")
    pages, receipt, entries, facts = enumerate_gallery(root)
    entry = entries[0]
    first, _ = resolve_and_apply(
        conn, root, entry, facts[0], receipt, run_id="run-replace-one", check_id="check-replace-one",
    )
    save_batch(conn, [plan_entry(entry, first.content)], "batch-replace-one", receipt)

    replacement = root / "gallery" / ".replacement.tmp"
    replacement.write_bytes(b"CCCC")
    os.replace(replacement, target)
    pages2, receipt2, entries2, facts2 = enumerate_gallery(root)
    new_entry = entries2[0]
    assert new_entry.physical_identity != entry.physical_identity

    resolution, observed = resolve_and_apply(
        conn, root, new_entry, facts2[0], receipt2,
        run_id="run-replace-two", check_id="check-replace-two",
    )
    assert resolution.kind is ResolutionKind.NEEDS_CONFIRMATION
    assert resolution.reason == "physical_object_replaced"
    assert resolution.requires_confirmation and not resolution.scope_complete
    assert resolution.next_action == "confirm_same_name_replacement"
    assert load_content_binding(conn, entry.path).content == first.content
    with pytest.raises(ArchiveIdentityConfirmationRequired):
        apply_content_resolution(
            conn, resolution, observed=observed, bound_at=TIME,
            source_kind="observed_existing",
        )
    conn.rollback()

    confirmed_content = ContentVersion(
        new_entry.content.asset_id, 0, new_entry.content.size_bytes, sha(b"CCCC"),
    )
    unconfirmed_inventory, unconfirmed_capacity = inventory_for(
        [ArchiveEntry(confirmed_content, new_entry.path, 0, new_entry.physical_identity)],
        "batch-replace-rejected",
    )
    with pytest.raises(ArchiveConfirmationRequiredError) as rejected:
        save_archive_plan(
            conn, unconfirmed_inventory, unconfirmed_capacity, receipt=receipt2,
            root_revision=1,
        )
    assert rejected.value.scope_complete is False
    assert rejected.value.conflicts[0].relative_path == "gallery/art.png"
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_batches WHERE batch_id='batch-replace-rejected'"
    ).fetchone()[0] == 0
    assert load_content_binding(conn, entry.path).content == first.content

    replacement_resolution, replacement_observed = resolve_and_apply(
        conn, root, new_entry, facts2[0], receipt2,
        run_id="run-replace-confirmed", check_id="check-replace-confirmed",
        replacement_confirmed=True,
    )
    assert replacement_resolution.kind is ResolutionKind.REPLACEMENT
    assert replacement_resolution.content.asset_id == first.content.asset_id
    assert replacement_resolution.content.revision == first.content.revision + 1
    assert replacement_resolution.content.sha256 == sha(b"CCCC")
    saved, inventory = save_batch(
        conn, [plan_entry(new_entry, replacement_resolution.content)],
        "batch-replace-two", receipt2,
    )
    assert saved.plan.inventory.assets[0].content.revision == 2
    assert load_archive_plan(conn, "batch-replace-one").inventory.assets[0].content.sha256 == sha(b"AAAA")
    validate_archive_facts(conn)


def test_path_alias_hardlink_reuses_one_content_for_both_paths(conn, tmp_path):
    root = tmp_path / "alias-root"
    payload = b"hardlink payload"
    first_path = write(root, "gallery/a.bin", payload)
    try:
        os.link(first_path, root / "gallery" / "b.bin")
    except OSError as exc:
        pytest.skip("Synthetic hardlink unavailable: " + type(exc).__name__)
    pages, receipt, entries, facts = enumerate_gallery(root)
    assert len(entries) == 2
    assert len([alias for page in pages for alias in page.aliases]) == 1
    by_path = {entry.path.relative_path: (entry, fact)
               for entry, fact in zip(entries, facts)}
    a_entry, a_fact = by_path["gallery/a.bin"]
    b_entry, b_fact = by_path["gallery/b.bin"]
    assert a_entry.physical_identity == b_entry.physical_identity
    a_resolution, _ = resolve_and_apply(
        conn, root, a_entry, a_fact, receipt, run_id="run-alias-a", check_id="check-alias-a",
    )
    assert a_resolution.kind is ResolutionKind.BASELINE
    b_resolution, b_observed = resolve_and_apply(
        conn, root, b_entry, b_fact, receipt, run_id="run-alias-b", check_id="check-alias-b",
    )
    assert b_observed.observation.status is ObservationStatus.BASELINE_OBSERVED
    assert b_resolution.kind is ResolutionKind.REUSE
    assert b_resolution.content == a_resolution.content
    a_binding = load_content_binding(conn, a_entry.path)
    b_binding = load_content_binding(conn, b_entry.path)
    assert a_binding.content == b_binding.content == a_resolution.content
    # a has an unknown rev 0 baseline probe plus the observed rev 1; b reuses rev 1.
    assert conn.execute("SELECT COUNT(*) FROM archive_contents").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM archive_assets").fetchone()[0] == 3
    saved, inventory = save_batch(
        conn,
        [plan_entry(a_entry, a_resolution.content), plan_entry(b_entry, b_resolution.content)],
        "batch-alias", receipt,
    )
    assert saved.plan.inventory.assets[0].content == saved.plan.inventory.assets[1].content
    assert load_archive_plan(conn, "batch-alias").inventory == inventory
    validate_archive_facts(conn)


def test_root_mapping_change_rejects_the_old_receipt_and_accepts_the_new_scan(conn, tmp_path):
    root = tmp_path / "root-revision-root"
    write(root, "gallery/art.png", b"AAAA")
    pages, receipt_v1, entries, facts = enumerate_gallery(root, revision=1)
    entry = entries[0]
    resolution, _ = resolve_and_apply(
        conn, root, entry, facts[0], receipt_v1,
        run_id="run-root-one", check_id="check-root-one",
    )
    save_batch(conn, [plan_entry(entry, resolution.content)], "batch-root-one", receipt_v1)

    pages2, receipt_v2, entries2, facts2 = enumerate_gallery(root, revision=2)
    assert receipt_v2.root_revision == 2
    assert receipt_v2.entry_facts[0].root_revision == 2
    inventory, capacity = inventory_for(
        [plan_entry(entries2[0], resolution.content)], "batch-root-rejected",
    )
    with pytest.raises(ArchivePlanStoreError, match="root mapping revision"):
        save_archive_plan(
            conn, inventory, capacity, receipt=receipt_v1, root_revision=2,
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_batches WHERE batch_id='batch-root-rejected'"
    ).fetchone()[0] == 0
    new_resolution, _ = resolve_and_apply(
        conn, root, entries2[0], facts2[0], receipt_v2,
        run_id="run-root-two", check_id="check-root-two",
    )
    assert new_resolution.kind is ResolutionKind.REUSE
    saved, _ = save_batch(
        conn, [plan_entry(entries2[0], new_resolution.content)], "batch-root-two", receipt_v2,
        root_revision=2,
    )
    assert saved.plan.inventory.assets[0].content == resolution.content
    validate_archive_facts(conn)


def test_identity_key_is_per_segment_and_target_key_is_unchanged():
    mixed_a = PathRef(kind="external", root_id="Synthetic", relative_path="画/A.png")
    mixed_b = PathRef(kind="external", root_id="synthetic", relative_path="画/a.png")
    assert mixed_a.identity_key == mixed_b.identity_key
    assert mixed_a.relative_path == "画/A.png"
    assert PathRef(relative_path="gallery/é.png").identity_key != PathRef(
        relative_path="gallery/é.png").identity_key
    assert PathRef(relative_path="gallery/straße.png").identity_key != PathRef(
        relative_path="gallery/strasse.png").identity_key
    expected_target = hashlib.sha256(json.dumps(
        [mixed_a.kind, mixed_a.root_id.casefold(), mixed_a.relative_path.casefold()],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert mixed_a.target_key == expected_target


def test_physical_identity_tampering_is_rejected_by_receipt_and_plan(conn, tmp_path):
    root = tmp_path / "tamper-root"
    payload = b"AAAA"
    write(root, "gallery/art.png", payload)
    pages, receipt, entries, facts = enumerate_gallery(root)
    entry, fact = entries[0], facts[0]
    assert receipt.matches_entries([entry])
    other_identity = FileIdentity(entry.physical_identity.device,
                                  entry.physical_identity.file_index + 1)
    tampered_entry = replace(entry, physical_identity=other_identity)
    assert not receipt.matches_entries([tampered_entry])
    forged_content = replace(entry, content=replace(entry.content, sha256="0" * 64))
    assert not receipt.matches_entries([forged_content])
    with pytest.raises(ContractError, match="physical identity"):
        replace(pages[0], entries=(tampered_entry,))

    content = ContentVersion(entry.content.asset_id, 0, len(payload), sha(payload))
    tampered_inventory = build_archive_inventory(
        (ArtworkGroup("work-1", (
            ArchiveEntry(content, entry.path, 0, other_identity),
        )),),
        batch_id="batch-tampered",
        policy=InventoryPolicy(target_package_bytes=1024),
    )
    capacity = plan_archive_capacity(
        tampered_inventory, available_additional_bytes=10**9,
        policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
    )
    with pytest.raises(ArchivePlanStoreError, match="entries differ|physical"):
        prepare_archive_plan(
            tampered_inventory, capacity, receipt=receipt,
        )

    payload_json = json.loads(receipt.to_json())
    payload_json["entry_facts"][0]["device"] = other_identity.device
    payload_json["entry_facts"][0]["file_index"] = other_identity.file_index
    with pytest.raises(ContractError, match="digest|facts"):
        ScopeReceipt.from_json(json.dumps(payload_json))


def test_change_after_enumeration_is_a_conflict_and_writes_nothing(conn, tmp_path):
    root = tmp_path / "changed-root"
    target = write(root, "gallery/art.png", b"AAAA")
    pages, receipt, entries, facts = enumerate_gallery(root)
    entry, fact = entries[0], facts[0]
    time.sleep(0.02)
    target.write_bytes(b"BBBB")
    bump_mtime(target)
    observed = observe_entry(
        conn, root, entry, fact, receipt,
        run_id="run-changed", check_id="check-changed", expected_content=entry.content,
    )
    assert observed.digest_complete
    resolution = resolve_observed_content(conn, observed)
    assert resolution.kind is ResolutionKind.CONFLICT
    assert resolution.reason == "mtime_changed_during_observation"
    assert resolution.requires_confirmation and not resolution.scope_complete
    assert conn.execute("SELECT COUNT(*) FROM archive_batches").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM archive_assets").fetchone()[0] == 0
    other_candidate = replace(observed.candidate, relative_path="gallery/other.png")
    mismatch = resolve_content_version(other_candidate, observed)
    assert mismatch.kind is ResolutionKind.CONFLICT
    assert mismatch.reason == "candidate_mismatch"


def test_v1_receipt_is_explicitly_rejected(conn, tmp_path):
    root = tmp_path / "v1-root"
    write(root, "gallery/art.png", b"AAAA")
    pages, receipt, entries, facts = enumerate_gallery(root)
    payload = json.loads(receipt.to_json())
    payload["schema"] = "jpgpng.archive-scope.receipt.v1"
    payload["schema_version"] = 1
    payload.pop("entry_facts", None)
    with pytest.raises(ContractError, match="v1"):
        ScopeReceipt.from_json(json.dumps(payload))
