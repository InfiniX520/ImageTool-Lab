"""A1 scope boundary regressions: exclusions, identity, receipts, paging.

Every case is synthetic and offline: temporary directories only, no real
library, database, network, observer, upload, or release.

A1-02  one eligibility policy behind recursion, explicit files, and prefixes
A1-03  physical identity of look-alike names and hardlink aliases
A1-04  versioned consumption receipt for a complete page delivery
A1-10  linear identity index instead of a per-file scan
A1-11  ancestor path safety versus selected-directory membership
"""
import json
import os
import subprocess
from dataclasses import replace

import pytest

from core.archive_scope import (
    SCOPE_RECEIPT_SCHEMA_VERSION, ScopeEnumerator, ScopeExclusionReason, ScopeIssueCode,
    ScopePolicy, ScopeReceipt, ScopeRequest, ScopeSelectionKind, ScopeState, ScopeSubBatch,
    consume_scope_pages, enumerate_scope,
)
from core.task_contracts import ContractError

NFC_NAME = "\u00e9.png"
NFD_NAME = "e\u0301.png"
ROUTES = ("recursive", "files", "prefix")


def write(root, relative, data=b"synthetic"):
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def paths(page):
    return [entry.path.relative_path for entry in page.entries]


def reasons(page):
    return {(item.relative_path, item.reason) for item in page.exclusions}


def covers(exclusion, target):
    selected = exclusion.relative_path.rstrip("/")
    return exclusion.relative_path == target or target.startswith(selected + "/")


def drain(source):
    page = source.page()
    pages = [page]
    while page.cursor:
        page = source.page(page.cursor)
        pages.append(page)
    return pages


def build_mixed_root(tmp_path):
    root = tmp_path / "mixed-root"
    (root / "archive_store" / "nested").mkdir(parents=True)
    (root / "gallery" / "outputs").mkdir(parents=True)
    write(root, "pixiv_config.json", b"config")
    write(root, "my-cookie.txt", b"cookie")
    write(root, ".env", b"environment")
    write(root, "nested/.env", b"nested environment")
    write(root, "archive_store/nested/package.7z", b"archive")
    write(root, "gallery/a.jpg", b"a")
    write(root, "gallery/output.jpg", b"ordinary output-like name")
    write(root, "gallery/outputs/keep.jpg", b"managed")
    return root


def run_route(root, route, target, policy=None):
    if route == "recursive":
        return enumerate_scope(root, ScopeRequest((".",)), policy)
    if route == "files":
        return enumerate_scope(root, ScopeRequest((), (target,)), policy)
    parent = target.rsplit("/", 1)[0] if "/" in target else "."
    return enumerate_scope(root, ScopeRequest((parent,)), policy)


def build_three(tmp_path, name="root"):
    root = tmp_path / name
    (root / "gallery").mkdir(parents=True)
    for index, letter in enumerate(("a", "b", "c")):
        write(root, "gallery/%s.jpg" % letter, letter.encode() * (index + 1))
    return root


# --- A1-02: one eligibility policy for every entry point --------------------


@pytest.mark.parametrize("target", [
    "pixiv_config.json",
    "my-cookie.txt",
    ".env",
    "nested/.env",
    "archive_store/nested/package.7z",
    "gallery/outputs/keep.jpg",
])
@pytest.mark.parametrize("route", ROUTES)
def test_every_entry_point_applies_the_same_exclusions(tmp_path, route, target):
    root = build_mixed_root(tmp_path)
    page = run_route(root, route, target)
    assert target not in paths(page)
    assert any(covers(item, target) for item in page.exclusions)
    assert all(item.reason for item in page.exclusions)
    assert page.completion.full


def test_recursive_route_excludes_secrets_and_managed_outputs(tmp_path):
    root = build_mixed_root(tmp_path)
    page = enumerate_scope(root, ScopeRequest((".",)))
    assert paths(page) == ["gallery/a.jpg", "gallery/output.jpg"]
    assert ("pixiv_config.json", ScopeExclusionReason.EXCLUDED_FILE) in reasons(page)
    assert ("my-cookie.txt", ScopeExclusionReason.EXCLUDED_FILE) in reasons(page)
    assert (".env", ScopeExclusionReason.EXCLUDED_FILE) in reasons(page)
    assert ("archive_store", ScopeExclusionReason.EXCLUDED_DIRECTORY) in reasons(page)
    assert ("gallery/outputs", ScopeExclusionReason.EXCLUDED_DIRECTORY) in reasons(page)
    assert page.completion.full


def test_explicit_files_cannot_bypass_secret_or_output_protection(tmp_path):
    """A1-02 regression: the audited probe selected these five paths directly."""
    root = build_mixed_root(tmp_path)
    request = ScopeRequest((), ("pixiv_config.json", "my-cookie.txt",
                                "archive_store/nested/package.7z", "gallery/a.jpg"))
    page = enumerate_scope(root, request)
    assert paths(page) == ["gallery/a.jpg"]
    assert ("pixiv_config.json", ScopeExclusionReason.EXCLUDED_FILE) in reasons(page)
    assert ("my-cookie.txt", ScopeExclusionReason.EXCLUDED_FILE) in reasons(page)
    nested = [item for item in page.exclusions
              if item.relative_path == "archive_store/nested/package.7z"]
    assert len(nested) == 1
    assert nested[0].reason == ScopeExclusionReason.EXCLUDED_ANCESTOR_DIRECTORY
    assert nested[0].rule == "archive_store"
    assert nested[0].related_path == "archive_store"
    assert page.completion.full


def test_explicit_prefix_inside_a_managed_output_is_excluded(tmp_path):
    """A1-02 regression: the audited probe selected archive_store/nested."""
    root = build_mixed_root(tmp_path)
    nested = enumerate_scope(root, ScopeRequest(("archive_store/nested",)))
    assert not nested.entries
    assert ("archive_store/nested",
            ScopeExclusionReason.EXCLUDED_ANCESTOR_DIRECTORY) in reasons(nested)
    direct = enumerate_scope(root, ScopeRequest(("archive_store",)))
    assert not direct.entries
    assert ("archive_store", ScopeExclusionReason.EXCLUDED_DIRECTORY) in reasons(direct)
    assert nested.completion.full and direct.completion.full


def test_ordinary_files_are_not_excluded_by_output_or_secret_rules(tmp_path):
    root = tmp_path / "ordinary-root"
    (root / "gallery").mkdir(parents=True)
    for relative in ("gallery/output.jpg", "gallery/outputs.jpg", "gallery/output-note.txt",
                     "gallery/readme", "gallery/a.keynote", "gallery/config.json"):
        write(root, relative)
    page = enumerate_scope(root, ScopeRequest((".",)))
    assert set(paths(page)) == {
        "gallery/output.jpg", "gallery/outputs.jpg", "gallery/output-note.txt",
        "gallery/readme", "gallery/a.keynote", "gallery/config.json",
    }
    assert not page.exclusions
    assert page.completion.full


@pytest.mark.parametrize("route", ROUTES)
def test_configured_output_root_is_protected_for_every_route(tmp_path, route):
    root = tmp_path / "configured-root"
    (root / "gallery").mkdir(parents=True)
    write(root, "custom_out/member.jpg", b"writer output")
    write(root, "gallery/a.jpg", b"a")
    policy = ScopePolicy(output_roots=("custom_out",))
    target = "custom_out/member.jpg"
    page = run_route(root, route, target, policy)
    assert target not in paths(page)
    assert page.completion.full
    assert any(covers(item, target) for item in page.exclusions)


def test_custom_output_root_is_not_guessed_without_configuration(tmp_path):
    root = tmp_path / "unconfigured-root"
    (root / "gallery").mkdir(parents=True)
    write(root, "custom_out/member.jpg", b"writer output")
    page = enumerate_scope(root, ScopeRequest((".",)))
    assert "custom_out/member.jpg" in paths(page)
    assert page.completion.full


def test_nested_configured_output_root_is_protected(tmp_path):
    root = tmp_path / "nested-output-root"
    (root / "gallery" / "writer").mkdir(parents=True)
    write(root, "gallery/writer/member.jpg", b"writer output")
    write(root, "gallery/other/keep.jpg", b"kept")
    policy = ScopePolicy(output_roots=("gallery/writer",))
    page = enumerate_scope(root, ScopeRequest((".",)), policy)
    assert paths(page) == ["gallery/other/keep.jpg"]
    assert ("gallery/writer", ScopeExclusionReason.EXCLUDED_DIRECTORY) in reasons(page)
    direct = enumerate_scope(root, ScopeRequest((), ("gallery/writer/member.jpg",)), policy)
    assert not direct.entries
    assert ("gallery/writer/member.jpg",
            ScopeExclusionReason.EXCLUDED_ANCESTOR_DIRECTORY) in reasons(direct)


def test_configured_output_root_only_excludes_directories(tmp_path):
    root = tmp_path / "output-file-name"
    (root / "gallery").mkdir(parents=True)
    write(root, "gallery/custom_out", b"a file that only shares the configured name")
    policy = ScopePolicy(output_roots=("gallery/custom_out",))
    page = enumerate_scope(root, ScopeRequest((".",)), policy)
    assert paths(page) == ["gallery/custom_out"]
    assert page.completion.full


@pytest.mark.parametrize("bad", [
    ".", "../escape", "/absolute", "a//b", "a/./b", "", "a/b/", "with\x00nul",
])
def test_output_root_configuration_is_validated(bad):
    with pytest.raises(ContractError):
        ScopePolicy(output_roots=(bad,))
    with pytest.raises(ContractError):
        ScopePolicy(output_roots="not-a-sequence")


def test_output_root_configuration_is_bound_into_rules_and_cursors(tmp_path):
    root = build_three(tmp_path)
    plain = ScopeEnumerator(root, ScopeRequest(("gallery",), page_size=1))
    configured = ScopeEnumerator(root, ScopeRequest(("gallery",), page_size=1),
                                 ScopePolicy(output_roots=("custom_out",)))
    page = plain.page()
    assert plain._rules_digest != configured._rules_digest
    configured._key = plain._key
    with pytest.raises(ContractError):
        configured.page(page.cursor)


# --- A1-03: physical identity, not display normalisation --------------------


def test_nfc_and_nfd_files_are_both_kept(tmp_path):
    root = tmp_path / "unicode-root"
    (root / "gallery").mkdir(parents=True)
    write(root, "gallery/" + NFC_NAME, b"nfc payload")
    write(root, "gallery/" + NFD_NAME, b"nfd payload!!")
    if len(list((root / "gallery").iterdir())) != 2:
        pytest.skip("This filesystem folds NFC and NFD names into one entry")
    page = enumerate_scope(root, ScopeRequest(("gallery",)))
    assert set(paths(page)) == {"gallery/" + NFC_NAME, "gallery/" + NFD_NAME}
    assert len({entry.content.asset_id for entry in page.entries}) == 2
    sizes = {entry.path.relative_path: entry.content.size_bytes for entry in page.entries}
    assert sizes["gallery/" + NFC_NAME] == len(b"nfc payload")
    assert sizes["gallery/" + NFD_NAME] == len(b"nfd payload!!")
    assert not page.exclusions
    assert page.completion.full


def test_explicit_nfc_and_nfd_selection_keeps_both_files(tmp_path):
    root = tmp_path / "unicode-explicit-root"
    (root / "gallery").mkdir(parents=True)
    write(root, "gallery/" + NFC_NAME, b"nfc payload")
    write(root, "gallery/" + NFD_NAME, b"nfd payload!!")
    if len(list((root / "gallery").iterdir())) != 2:
        pytest.skip("This filesystem folds NFC and NFD names into one entry")
    request = ScopeRequest((), ("gallery/" + NFC_NAME, "gallery/" + NFD_NAME))
    page = enumerate_scope(root, request)
    assert set(paths(page)) == {"gallery/" + NFC_NAME, "gallery/" + NFD_NAME}
    assert not page.exclusions
    assert page.completion.full


def test_nfc_nfd_hardlink_keeps_both_paths(tmp_path):
    """A hardlink whose two raw names normalise alike is still two paths.

    This is the case where "same physical identity + same normalised name"
    must not be read as "one directory entry": NTFS lists both names, so the
    second path is a real alias that restore needs.
    """
    root = tmp_path / "unicode-hardlink-root"
    (root / "gallery").mkdir(parents=True)
    first = write(root, "gallery/" + NFC_NAME, b"shared payload")
    try:
        os.link(first, root / "gallery" / NFD_NAME)
    except OSError as exc:
        pytest.skip("NFC/NFD hardlink pair unavailable: " + type(exc).__name__)
    if len(list((root / "gallery").iterdir())) != 2:
        pytest.skip("This filesystem folds NFC and NFD names into one entry")
    page = enumerate_scope(root, ScopeRequest(("gallery",)))
    assert set(paths(page)) == {"gallery/" + NFC_NAME, "gallery/" + NFD_NAME}
    assert len({entry.content.asset_id for entry in page.entries}) == 1
    assert len(page.aliases) == 1
    alias = page.aliases[0]
    assert {alias.relative_path, alias.alias_of} == {"gallery/" + NFC_NAME,
                                                     "gallery/" + NFD_NAME}
    assert page.completion.full
    assert not [item for item in page.exclusions
                if item.relative_path in {"gallery/" + NFC_NAME, "gallery/" + NFD_NAME}]


def test_case_alias_of_one_parent_path_is_one_entry(tmp_path):
    """Two spellings of one parent directory still name one directory entry."""
    root = tmp_path / "parent-alias-root"
    (root / "in_scope").mkdir(parents=True)
    write(root, "in_scope/a.jpg", b"a")
    if not (root / "IN_SCOPE").exists():
        pytest.skip("This filesystem is case sensitive; the alias cannot exist")
    request = ScopeRequest((), ("in_scope/a.jpg", "IN_SCOPE/A.JPG"))
    page = enumerate_scope(root, request)
    assert len(page.entries) == 1
    assert not page.aliases
    assert any(item.reason == ScopeExclusionReason.DUPLICATE_SELECTION
               for item in page.exclusions)
    assert page.completion.full


def test_casefold_identical_names_are_not_merged(tmp_path):
    """Names that casefold alike are still separate physical files.

    PathRef.target_key casefolds, so a downstream inventory sees one path key
    for both and must reject it as a collision.  The scope layer keeps both
    entries instead of silently dropping one and claiming a complete range.
    """
    root = tmp_path / "casefold-root"
    (root / "gallery").mkdir(parents=True)
    write(root, "gallery/" + "stra\u00dfe.png", b"one")
    write(root, "gallery/strasse.png", b"two")
    page = enumerate_scope(root, ScopeRequest(("gallery",)))
    assert len(page.entries) == 2
    assert len({entry.content.asset_id for entry in page.entries}) == 2
    assert page.completion.full


def test_explicit_hardlink_selection_keeps_both_aliases(tmp_path):
    root = tmp_path / "hardlink-explicit-root"
    (root / "gallery").mkdir(parents=True)
    write(root, "gallery/a.jpg", b"payload")
    try:
        os.link(root / "gallery" / "a.jpg", root / "gallery" / "b.jpg")
    except OSError as exc:
        pytest.skip("Synthetic hardlink unavailable: " + type(exc).__name__)
    request = ScopeRequest((), ("gallery/a.jpg", "gallery/b.jpg"))
    page = enumerate_scope(root, request)
    assert set(paths(page)) == {"gallery/a.jpg", "gallery/b.jpg"}
    assert len({entry.content.asset_id for entry in page.entries}) == 1
    assert len(page.aliases) == 1
    assert {(item.relative_path, item.alias_of) for item in page.aliases} == {
        ("gallery/b.jpg", "gallery/a.jpg")}


# --- A1-04: a real consumption receipt -------------------------------------


def test_complete_delivery_produces_a_verifiable_receipt(tmp_path):
    root = build_three(tmp_path)
    source = ScopeEnumerator(root, ScopeRequest(("gallery",), root_revision=5, page_size=1))
    pages = drain(source)
    assert len(pages) == 3
    receipt = consume_scope_pages(pages, source=source)
    assert receipt.schema_version == SCOPE_RECEIPT_SCHEMA_VERSION
    assert receipt.enumeration_complete and receipt.consumption_complete
    assert receipt.scope_complete
    assert not receipt.content_verified
    assert receipt.snapshot_verified
    assert receipt.page_count == 3 and receipt.expected_page_count == 3
    assert receipt.consumed_entries == 3 and receipt.remaining_entries == 0
    assert receipt.root_revision == 5
    entries = [entry for page in pages for entry in page.entries]
    assert receipt.matches_entries(entries)
    assert not receipt.matches_entries(list(reversed(entries)))
    receipt.verify()
    assert ScopeReceipt.from_json(receipt.to_json()) == receipt
    assert receipt.receipt_id.startswith("scope-")
    assert receipt.to_json() == receipt.to_json()


def test_first_page_only_is_not_a_complete_receipt(tmp_path):
    root = build_three(tmp_path)
    source = ScopeEnumerator(root, ScopeRequest(("gallery",), page_size=1))
    first_page = source.page()
    receipt = consume_scope_pages([first_page], source=source)
    assert receipt.enumeration_complete
    assert receipt.consumed_entries == 1 and receipt.remaining_entries == 2
    assert receipt.expected_page_count == 3 and receipt.page_count == 1
    assert not receipt.consumption_complete
    assert not receipt.scope_complete


def test_last_page_with_a_remaining_cursor_is_not_complete(tmp_path):
    """Consumption needs every page and no remaining cursor, not just counts."""
    root = build_three(tmp_path)
    source = ScopeEnumerator(root, ScopeRequest(("gallery",), page_size=1))
    pages = drain(source)
    forged = [replace(page, cursor="remaining-page", has_next_page=True) for page in pages]
    receipt = consume_scope_pages(forged, source=source)
    assert receipt.consumed_entries == 3
    assert receipt.last_page_has_next
    assert not receipt.consumption_complete
    assert not receipt.scope_complete


def test_missing_middle_page_is_rejected(tmp_path):
    root = build_three(tmp_path)
    source = ScopeEnumerator(root, ScopeRequest(("gallery",), page_size=1))
    pages = drain(source)
    with pytest.raises(ContractError, match="missing, duplicated, or out of order"):
        consume_scope_pages([pages[0], pages[2]], source=source)


def test_duplicate_page_is_rejected(tmp_path):
    root = build_three(tmp_path)
    source = ScopeEnumerator(root, ScopeRequest(("gallery",), page_size=1))
    pages = drain(source)
    with pytest.raises(ContractError, match="missing, duplicated, or out of order"):
        consume_scope_pages([pages[0], pages[0], pages[1], pages[2]], source=source)


def test_pages_from_a_different_snapshot_are_rejected(tmp_path):
    root = build_three(tmp_path)
    first = drain(ScopeEnumerator(root, ScopeRequest(("gallery",), page_size=1)))
    second = drain(ScopeEnumerator(root, ScopeRequest(("gallery",), page_size=1)))
    assert first[0].completion.snapshot_id != second[0].completion.snapshot_id
    with pytest.raises(ContractError, match="one snapshot"):
        consume_scope_pages([first[0], second[1]])


def test_receipt_rejects_tampered_counts_and_digests(tmp_path):
    root = build_three(tmp_path)
    source = ScopeEnumerator(root, ScopeRequest(("gallery",), page_size=1))
    receipt = consume_scope_pages(drain(source), source=source)

    payload = json.loads(receipt.to_json())
    payload["consumed_entries"] = 2
    payload["remaining_entries"] = 1
    with pytest.raises(ContractError):
        ScopeReceipt.from_json(json.dumps(payload))

    payload = json.loads(receipt.to_json())
    payload["scope_complete"] = False
    with pytest.raises(ContractError):
        ScopeReceipt.from_json(json.dumps(payload))

    payload = json.loads(receipt.to_json())
    payload["entries_digest"] = "0" * 64
    with pytest.raises(ContractError):
        ScopeReceipt.from_json(json.dumps(payload))

    with pytest.raises(ContractError):
        replace(receipt, consumed_entries=1)


def test_sub_batch_has_its_own_scope_and_no_root_completion(tmp_path):
    root = build_three(tmp_path)
    source = ScopeEnumerator(root, ScopeRequest(("gallery",), page_size=1))
    pages = drain(source)
    described = "first two entries of the gallery snapshot"
    sub = consume_scope_pages(pages[:2], source=source,
                              sub_batch=ScopeSubBatch(2, described))
    assert sub.selection_kind is ScopeSelectionKind.SUB_BATCH
    assert sub.sub_batch_limit == 2
    assert sub.sub_batch_description == described
    assert sub.consumption_complete
    assert not sub.scope_complete
    assert sub.remaining_entries == 1
    assert sub.selection_digest != pages[0].selection_digest
    whole = consume_scope_pages(pages, source=source,
                                sub_batch=ScopeSubBatch(3, "whole gallery"))
    assert whole.consumption_complete and not whole.scope_complete
    with pytest.raises(ContractError, match="page boundary"):
        consume_scope_pages(pages, source=source, sub_batch=ScopeSubBatch(2, described))


def test_signed_receipt_requires_the_same_key(tmp_path):
    root = build_three(tmp_path)
    source = ScopeEnumerator(root, ScopeRequest(("gallery",), page_size=1))
    receipt = consume_scope_pages(drain(source), source=source)
    key = b"scope-receipt-key-000"
    signed = receipt.signed(key)
    signed.verify_mac(key)
    ScopeReceipt.from_json(signed.to_json()).verify_mac(key)
    with pytest.raises(ContractError, match="signature"):
        signed.verify_mac(b"other-receipt-key-000")
    with pytest.raises(ContractError, match="not signed"):
        receipt.verify_mac(key)
    assert consume_scope_pages(drain(ScopeEnumerator(
        root, ScopeRequest(("gallery",), page_size=1))), signing_key=key).mac is not None


def test_cancelled_delivery_never_becomes_a_complete_receipt(tmp_path):
    root = build_three(tmp_path)
    source = ScopeEnumerator(root, ScopeRequest(("gallery",), page_size=1))
    first_page = source.page()

    class Cancel:
        def is_set(self):
            return True

    aborted = source.page(first_page.cursor, cancellation=Cancel())
    assert aborted.completion.state is ScopeState.CANCELLED
    receipt = consume_scope_pages([first_page, aborted], source=source)
    assert receipt.aborted
    assert not receipt.consumption_complete and not receipt.scope_complete
    assert receipt.remaining_entries == 2


def test_empty_scope_receipt_is_complete_and_empty(tmp_path):
    root = tmp_path / "empty-root"
    (root / "gallery").mkdir(parents=True)
    source = ScopeEnumerator(root, ScopeRequest(("gallery",)))
    receipt = consume_scope_pages(drain(source), source=source)
    assert receipt.entries_in_snapshot == 0
    assert receipt.exclusions_in_snapshot == 0
    assert receipt.consumption_complete and receipt.scope_complete


def test_fully_excluded_scope_is_complete_but_reports_exclusions(tmp_path):
    root = tmp_path / "excluded-root"
    (root / "gallery").mkdir(parents=True)
    write(root, "gallery/pixiv_config.json", b"config")
    source = ScopeEnumerator(root, ScopeRequest(("gallery",)))
    receipt = consume_scope_pages(drain(source), source=source)
    assert receipt.entries_in_snapshot == 0
    assert receipt.exclusions_in_snapshot == 1
    assert receipt.consumption_complete and receipt.scope_complete


# --- A1-10: linear identity index -----------------------------------------


@pytest.mark.parametrize("count", [200, 400, 800])
def test_identity_comparisons_match_the_candidate_count(tmp_path, count):
    root = tmp_path / ("linear-recursive-%d" % count)
    (root / "gallery").mkdir(parents=True)
    for index in range(count):
        (root / "gallery" / ("file%04d.jpg" % index)).write_bytes(b"x")
    source = ScopeEnumerator(root, ScopeRequest(("gallery",), page_size=1000))
    page = source.page()
    assert len(page.entries) == count
    assert source.identity_comparisons == count
    assert source.identity_comparisons < count * (count - 1) // 2


@pytest.mark.parametrize("count", [200, 400, 800])
def test_explicit_selection_identity_comparisons_stay_linear(tmp_path, count):
    root = tmp_path / ("linear-explicit-%d" % count)
    (root / "gallery").mkdir(parents=True)
    names = []
    for index in range(count):
        relative = "gallery/file%04d.jpg" % index
        write(root, relative, b"x")
        names.append(relative)
    source = ScopeEnumerator(root, ScopeRequest((), tuple(names), page_size=1000))
    page = source.page()
    assert len(page.entries) == count
    assert source.identity_comparisons == count


def test_identity_comparisons_grow_linearly_not_quadratically(tmp_path):
    measured = {}
    for count in (200, 400, 800):
        root = tmp_path / ("growth-%d" % count)
        (root / "gallery").mkdir(parents=True)
        for index in range(count):
            (root / "gallery" / ("file%04d.jpg" % index)).write_bytes(b"x")
        source = ScopeEnumerator(root, ScopeRequest(("gallery",), page_size=1000))
        source.page()
        measured[count] = source.identity_comparisons
    assert measured == {200: 200, 400: 400, 800: 800}
    assert measured[800] == 4 * measured[200]
    assert measured[800] < 800 * 799 // 2


# --- A1-11: ancestor safety versus selected membership ---------------------


def test_sibling_of_the_root_does_not_break_paging(tmp_path):
    """A1-11 regression: unrelated activity in the parent must not abort."""
    root = tmp_path / "gallery"
    (root / "in_scope").mkdir(parents=True)
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        write(root, "in_scope/" + name)
    source = ScopeEnumerator(root, ScopeRequest((".",), page_size=2))
    first_page = source.page()
    assert paths(first_page) == ["in_scope/a.jpg", "in_scope/b.jpg"]
    (tmp_path / "maintenance-report.txt").write_bytes(b"other project output")
    (tmp_path / "other-project").mkdir()
    second = source.page(first_page.cursor)
    assert not second.issues
    assert second.completion.state is ScopeState.COMPLETE
    assert paths(second) == ["in_scope/c.jpg"]
    receipt = consume_scope_pages([first_page, second], source=source)
    assert receipt.scope_complete


def test_root_membership_change_stops_paging(tmp_path):
    root = tmp_path / "gallery"
    root.mkdir()
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        write(root, name)
    source = ScopeEnumerator(root, ScopeRequest((".",), page_size=2))
    first_page = source.page()
    write(root, "d.jpg")
    second = source.page(first_page.cursor)
    assert second.completion.state is ScopeState.PARTIAL
    assert {issue.code for issue in second.issues} == {ScopeIssueCode.ROOT_CHANGED}
    assert not second.entries


def test_selected_subdirectory_membership_change_stops_paging(tmp_path):
    root = tmp_path / "gallery"
    (root / "in_scope").mkdir(parents=True)
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        write(root, "in_scope/" + name)
    source = ScopeEnumerator(root, ScopeRequest(("in_scope",), page_size=2))
    first_page = source.page()
    write(root, "in_scope/d.jpg")
    second = source.page(first_page.cursor)
    assert second.completion.state is ScopeState.PARTIAL
    assert {issue.code for issue in second.issues} == {ScopeIssueCode.DIRECTORY_CHANGED}
    assert not second.entries


def test_replaced_root_identity_stops_paging(tmp_path):
    root = tmp_path / "gallery"
    root.mkdir()
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        write(root, name)
    source = ScopeEnumerator(root, ScopeRequest((".",), page_size=2))
    first_page = source.page()
    root.rename(tmp_path / "gallery-old")
    root.mkdir()
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        write(root, name)
    second = source.page(first_page.cursor)
    assert second.completion.state is ScopeState.PARTIAL
    assert {issue.code for issue in second.issues} == {ScopeIssueCode.ROOT_CHANGED}
    assert not second.entries


def test_file_content_change_is_not_a_membership_change(tmp_path):
    root = tmp_path / "gallery"
    (root / "in_scope").mkdir(parents=True)
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        write(root, "in_scope/" + name)
    source = ScopeEnumerator(root, ScopeRequest(("in_scope",), page_size=2))
    first_page = source.page()
    write(root, "in_scope/a.jpg", b"rewritten payload")
    second = source.page(first_page.cursor)
    assert not second.issues
    assert second.completion.state is ScopeState.COMPLETE
    assert paths(second) == ["in_scope/c.jpg"]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction replacement check")
def test_root_replaced_by_a_junction_stops_paging(tmp_path):
    root = tmp_path / "gallery"
    (root / "in_scope").mkdir(parents=True)
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        write(root, "in_scope/" + name)
    source = ScopeEnumerator(root, ScopeRequest((".",), page_size=2))
    first_page = source.page()
    moved = tmp_path / "gallery-moved"
    root.rename(moved)
    created = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(root), str(moved)],
                             capture_output=True)
    if created.returncode != 0:
        pytest.skip("mklink /J unavailable: "
                    + created.stderr.decode("utf-8", "replace")[:200])
    second = source.page(first_page.cursor)
    assert second.completion.state is ScopeState.PARTIAL
    assert {issue.code for issue in second.issues} == {ScopeIssueCode.ROOT_REPARSE_POINT}
    assert not second.entries
