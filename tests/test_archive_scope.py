"""Synthetic, offline tests for explicit archive-scope enumeration."""
import hashlib
import os
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import core.archive_scope as scope
from core.archive_scope import (
    SCOPE_SCHEMA_VERSION, ScopeEnumerator, ScopeIssueCode, ScopePolicy, ScopeRequest,
    ScopeState, enumerate_scope,
)
from core.local_observation import LocalObserver, ObservationRequest
from core.task_contracts import ContractError, PathRef


@pytest.fixture
def root(tmp_path):
    result = tmp_path / "synthetic-root"
    (result / "in_scope" / "nested").mkdir(parents=True)
    (result / "outside").mkdir()
    (result / "outside" / "never-read.txt").write_text("outside", encoding="utf-8")
    return result


def file(root, relative, data=b"synthetic"):
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def paths(page):
    return [entry.path.relative_path for entry in page.entries]


def codes(page):
    return {issue.code for issue in page.issues}


def first(root, request=None, policy=None, **kwargs):
    return enumerate_scope(root, request or ScopeRequest(("in_scope",), root_revision=9),
                           policy, **kwargs)


class Cancel:
    def __init__(self, value=True):
        self.value = value
        self.calls = 0

    def is_set(self):
        self.calls += 1
        return self.value if isinstance(self.value, bool) else self.value()


@dataclass(frozen=True)
class FakeStat:
    st_mode: int = stat.S_IFREG
    st_size: int = 9
    st_mtime_ns: int = 123456
    st_dev: int = 7
    st_ino: int = 9
    st_file_attributes: int = 0


def test_bounded_prefix_deny_rules_and_compatible_entry_types(root, monkeypatch):
    target = file(root, "in_scope/nested/image without extension", b"not-an-image")
    file(root, "in_scope/readme.txt", b"text")
    file(root, "in_scope/output/must-not-read.bin")
    file(root, "in_scope/archive_store/must-not-read.bin")
    file(root, "in_scope/diagnostic.LOG")
    file(root, "in_scope/library.sqlite3")
    file(root, "in_scope/library.sqlite3-wal")
    file(root, "in_scope/my-cookie-backup")
    file(root, "in_scope/private.pem")
    file(root, "in_scope/work.part")
    file(root, "in_scope/custom-skip.dat")
    visited = []
    real_scandir = os.scandir

    def scoped_scandir(path):
        visited.append(Path(path))
        assert Path(path) != root / "outside"
        return real_scandir(path)

    def no_content_hash(*args, **kwargs):
        raise AssertionError("enumeration must not hash contents")

    monkeypatch.setattr(os, "scandir", scoped_scandir)
    monkeypatch.setattr(scope, 'sha256', scope.hashlib.sha256, raising=False)
    monkeypatch.setattr(LocalObserver, "observe", lambda *args: (_ for _ in ()).throw(
        AssertionError("observer must not be called")))
    result = first(root, policy=ScopePolicy(additional_file_excludes=("*.dat",)))
    monkeypatch.undo()
    assert paths(result) == ["in_scope/nested/image without extension", "in_scope/readme.txt"]
    assert not result.issues
    assert result.completion.state is ScopeState.COMPLETE
    assert result.completion.full
    assert result.cursor is None and not result.has_next_page
    assert result.completion.root_identity.device >= 0
    assert result.completion.root_identity.file_index > 0
    assert result.completion.snapshot_digest
    assert len(result.rules_digest) == 64
    entry = result.entries[0]
    assert entry.content.size_bytes == 12
    assert entry.content.revision == 0 and entry.content.sha256 is None
    assert entry.path == PathRef(relative_path="in_scope/nested/image without extension")
    assert all(item.reason in {"excluded_directory", "excluded_file"}
               for item in result.exclusions)
    assert not any(item.relative_path.startswith("outside") for item in result.exclusions)
    # No automatic observation occurs above; the caller can explicitly use the pair.
    observation = LocalObserver(root, root_id="project", root_revision=9).observe(
        ObservationRequest(entry.content, entry.path, "scope-test", "scope-test-check"))
    assert observation.digest_complete
    assert observation.evidence.observed_sha256 == hashlib.sha256(target.read_bytes()).hexdigest()
    assert set(visited).isdisjoint({root / "in_scope" / "output", root / "in_scope" / "archive_store"})


def test_explicit_file_sets_do_not_recursively_expand(root):
    file(root, "in_scope/a.bin", b"a")
    file(root, "in_scope/nested/b.bin", b"bb")
    result = first(root, ScopeRequest((), ("in_scope/a.bin", "in_scope/nested/b.bin")))
    assert paths(result) == ["in_scope/a.bin", "in_scope/nested/b.bin"]
    assert [entry.content.size_bytes for entry in result.entries] == [1, 2]


def test_invalid_ads_traversal_and_wrong_root_are_issues_not_filesystem_privileges(root):
    request = ScopeRequest(
        (), ("in:stream", "../escape", "C:/absolute", "in_scope/", "CON.txt", "missing.bin",
              PathRef(kind="external", root_id="other", relative_path="secret.txt")),
    )
    result = first(root, request)
    assert not result.entries
    assert not result.cursor
    assert result.completion.state is ScopeState.COMPLETE_WITH_ISSUES
    assert codes(result) == {ScopeIssueCode.ADS_PATH, ScopeIssueCode.INVALID_PATH,
                             ScopeIssueCode.FILE_UNAVAILABLE, ScopeIssueCode.ROOT_BINDING_MISMATCH}
    assert result.completion.root_identity is not None


def test_missing_file_explicit_prefix_and_directory_as_file(root):
    file(root, "in_scope/a.bin")
    result = first(root, ScopeRequest(("missing-directory", "in_scope/a.bin"), ("in_scope",)))
    assert not result.entries
    assert codes(result) == {ScopeIssueCode.FILE_UNAVAILABLE,
                             ScopeIssueCode.PREFIX_NOT_DIRECTORY,
                             ScopeIssueCode.DIRECTORY_AS_FILE}


def test_unicode_case_alias_and_overlapping_selections_are_reported(root):
    file(root, "in_scope/é.bin")
    file(root, "in_scope/z.bin")
    result = first(root, ScopeRequest(("in_scope", "IN_SCOPE"),
                                      ("in_scope/z.bin", "in_scope/z.bin")))
    assert paths(result) == ["in_scope/z.bin", "in_scope/é.bin"]
    assert any(item.reason == "duplicate_selection" for item in result.exclusions)
    # A file already covered by a selected prefix is one redundant selection,
    # not a second traversal.  No exclusion may point at itself.
    assert not any(item.reason == "duplicate_candidate" for item in result.exclusions)
    assert all(item.related_path != item.relative_path for item in result.exclusions)


def test_windows_case_aliases_never_emit_two_paths(root):
    file(root, "in_scope/A.bin")
    result = first(root, ScopeRequest((), ("in_scope/a.bin", "in_scope/A.bin")))
    assert len(result.entries) == 1
    assert any(item.reason == "duplicate_selection" for item in result.exclusions)


def test_hardlink_alias_keeps_every_path_and_counts_content_once(root):
    """A1-03: a hardlink is not a second copy, but neither path may vanish.

    The earlier assertion here (one entry, one duplicate_file_identity
    exclusion) discarded the alias path while still reporting a complete
    scope.  Restore needs every path, so the stronger rule keeps both paths,
    shares one content identity, and records the alias relation explicitly.
    """
    payload = b"hardlink payload"
    file(root, "in_scope/a.bin", payload)
    try:
        os.link(root / "in_scope" / "a.bin", root / "in_scope" / "b.bin")
    except OSError as exc:
        pytest.skip("Synthetic hardlink unavailable: " + type(exc).__name__)
    result = first(root)
    assert paths(result) == ["in_scope/a.bin", "in_scope/b.bin"]
    assert result.completion.full
    # Shared content: one asset revision, so capacity/release counts it once.
    identities = {(entry.content.asset_id, entry.content.revision) for entry in result.entries}
    assert len(identities) == 1
    assert {entry.content.size_bytes for entry in result.entries} == {len(payload)}
    assert len(result.aliases) == 1
    alias = result.aliases[0]
    assert (alias.relative_path, alias.alias_of) == ("in_scope/b.bin", "in_scope/a.bin")
    assert alias.asset_id == next(iter(identities))[0]
    assert alias.identity.file_index > 0
    assert not [item for item in result.exclusions if item.relative_path == "in_scope/b.bin"]


def test_stable_paging_snapshot_boundary_and_immutability(root):
    for name in ("z.bin", "A.bin", "m.bin", "e.bin", "k.bin"):
        file(root, "in_scope/" + name, name.encode())
    request = ScopeRequest(("in_scope",), root_revision=17, page_size=2)
    source = ScopeEnumerator(root, request)
    collected = []
    pages = []
    page = source.page()
    pages.append(page)
    assert paths(page) == ["in_scope/A.bin", "in_scope/e.bin"]
    while page.cursor is not None:
        page = source.page(page.cursor)
        pages.append(page)
    for page in pages:
        collected.extend(paths(page))
        assert page.root_revision == 17
        assert page.schema_version == SCOPE_SCHEMA_VERSION
        assert page.completion.snapshot_id == pages[0].completion.snapshot_id
        assert page.completion.entries_in_snapshot == 5
        assert page.completion.full
    assert collected == ["in_scope/A.bin", "in_scope/e.bin", "in_scope/k.bin",
                         "in_scope/m.bin", "in_scope/z.bin"]
    assert pages[-1].cursor is None
    with pytest.raises(FrozenInstanceError):
        pages[0].entries = ()
    with pytest.raises(FrozenInstanceError):
        pages[0].completion.state = ScopeState.PARTIAL


def test_cursor_tampering_cross_enumerator_and_replay_are_rejected(root):
    for name in ("a.bin", "b.bin", "c.bin"):
        file(root, "in_scope/" + name)
    request = ScopeRequest(("in_scope",), page_size=1)
    source = ScopeEnumerator(root, request)
    page = source.page()
    changed = ("A" if page.cursor[-1] != "A" else "B")
    with pytest.raises(ContractError, match="altered"):
        source.page(page.cursor[:-1] + changed)
    with pytest.raises(ContractError, match="foreign"):
        ScopeEnumerator(root, request).page(page.cursor)
    second = source.page(page.cursor)
    assert paths(second) == ["in_scope/b.bin"]
    with pytest.raises(ContractError, match="altered"):
        source.page(page.cursor)
    snapshot = source._snapshot
    source._snapshot = replace(snapshot, digest="0" * 64)
    with pytest.raises(ContractError, match="altered"):
        source.page(second.cursor)


def test_cursor_is_bound_to_root_mapping_policy_and_snapshot(root):
    for name in ("a.bin", "b.bin", "c.bin"):
        file(root, "in_scope/" + name)
    source = ScopeEnumerator(root, ScopeRequest(("in_scope",), root_revision=1, page_size=1))
    page = source.page()
    for changed_request, changed_policy in (
        (ScopeRequest(("in_scope",), root_revision=2, page_size=1), None),
        (ScopeRequest(("in_scope",), page_size=1), ScopePolicy(additional_file_excludes=("*.dat",))),
        (ScopeRequest(("in_scope/nested",), page_size=1), None),
    ):
        other = ScopeEnumerator(root, changed_request, changed_policy)
        other._key = source._key
        with pytest.raises(ContractError):
            other.page(page.cursor)
    # An authenticated token still cannot cross a different entry snapshot.
    other = ScopeEnumerator(root, ScopeRequest(("in_scope",), root_revision=1, page_size=1))
    other._key = source._key
    other.page()
    with pytest.raises(ContractError):
        other.page(page.cursor)


def test_missing_root_does_not_fabricate_identity_snapshot_or_completion(tmp_path):
    missing = tmp_path / "not-created"
    request = ScopeRequest(("in_scope",), root_revision=41)
    result = first(missing, request)
    assert result.root_revision == 41 and result.root_id == "project"
    assert result.completion.root_identity is None
    assert result.completion.snapshot_id is None
    assert result.completion.snapshot_digest is None
    assert result.completion.state is ScopeState.PARTIAL
    assert not result.entries and not result.cursor
    assert codes(result) == {ScopeIssueCode.ROOT_UNAVAILABLE}
    assert not missing.exists()


def test_root_file_and_real_junction_root_are_rejected(root):
    ordinary = root / "ordinary-root-file"
    ordinary.write_bytes(b"not a directory")
    result = first(ordinary)
    assert codes(result) == {ScopeIssueCode.ROOT_NOT_DIRECTORY}
    assert result.completion.root_identity is None
    if os.name != "nt":
        pytest.skip("Windows junction root check")
    link = root / "junction-root"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link),
                    str(root / "in_scope")], check=True, capture_output=True)
    result = first(link)
    assert codes(result) == {ScopeIssueCode.ROOT_REPARSE_POINT}
    assert result.completion.root_identity is None


def test_real_descendant_junction_is_not_followed(root):
    if os.name != "nt":
        pytest.skip("Windows junction descendant check")
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J",
                    str(root / "in_scope" / "junction"), str(root / "outside")],
                   check=True, capture_output=True)
    result = first(root)
    assert not result.entries
    assert codes(result) == {ScopeIssueCode.REPARSE_POINT}
    assert result.completion.state is ScopeState.COMPLETE_WITH_ISSUES
    assert not any("never-read" in item.relative_path for item in result.exclusions)


def test_fake_reparse_attribute_is_rejected_without_real_privilege(root):
    file(root, "in_scope/a.bin")
    default = scope._DEFAULT_IO
    original = (root / "in_scope" / "a.bin").lstat()

    def reparse(path):
        if path == root / "in_scope" / "a.bin":
            return FakeStat(st_mode=original.st_mode, st_size=original.st_size,
                            st_mtime_ns=original.st_mtime_ns, st_dev=original.st_dev,
                            st_ino=original.st_ino,
                            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return path.lstat()

    result = first(root, _io=None) if False else ScopeEnumerator(
        root, ScopeRequest(("in_scope",)), _io=scope._FileOperations(reparse, os.scandir)).page()
    assert not result.entries
    assert codes(result) == {ScopeIssueCode.REPARSE_POINT}
    assert default is scope._DEFAULT_IO


def test_fake_unreadable_metadata_and_directory_are_reported(root):
    file(root, "in_scope/a.bin")
    target = root / "in_scope" / "a.bin"

    def unreadable(path):
        if path == target:
            raise PermissionError(13, "synthetic metadata denial")
        return path.lstat()

    page = ScopeEnumerator(root, ScopeRequest(("in_scope",)),
                           _io=scope._FileOperations(unreadable, os.scandir)).page()
    assert codes(page) == {ScopeIssueCode.UNREADABLE}

    def denied(path):
        raise PermissionError(13, "synthetic directory denial")

    page = ScopeEnumerator(root, ScopeRequest(("in_scope",)),
                           _io=scope._FileOperations(lambda p: p.lstat(), denied)).page()
    assert codes(page) == {ScopeIssueCode.DIRECTORY_UNREADABLE}
    assert page.completion.state is ScopeState.PARTIAL
    assert not page.entries and not page.cursor


def test_fake_non_regular_and_missing_identity_are_reported(root):
    file(root, "in_scope/a.bin")
    target = root / "in_scope" / "a.bin"
    original = target.lstat()
    fifo = FakeStat(st_mode=stat.S_IFIFO, st_size=original.st_size,
                    st_mtime_ns=original.st_mtime_ns, st_dev=original.st_dev, st_ino=original.st_ino)
    unknown = replace(fifo, st_ino=0, st_mode=original.st_mode)
    for info, expected in ((fifo, ScopeIssueCode.NOT_REGULAR_FILE),
                           (unknown, ScopeIssueCode.FILE_IDENTITY_UNAVAILABLE)):
        io = scope._FileOperations(lambda p: info if p == target else p.lstat(), os.scandir)
        page = ScopeEnumerator(root, ScopeRequest(("in_scope",)), _io=io).page()
        assert codes(page) == {expected}


def test_fake_file_metadata_race_is_not_a_complete_snapshot(root):
    file(root, "in_scope/a.bin")
    target = root / "in_scope" / "a.bin"
    changed = False

    def race(path):
        nonlocal changed
        info = path.lstat()
        if path == target:
            if changed:
                return replace(FakeStat(st_mode=info.st_mode, st_size=info.st_size,
                                st_mtime_ns=info.st_mtime_ns, st_dev=info.st_dev,
                                st_ino=info.st_ino, st_file_attributes=getattr(info, 'st_file_attributes', 0)),
                       st_size=info.st_size + 1)
            changed = True
        return info

    page = ScopeEnumerator(root, ScopeRequest(("in_scope",)),
                           _io=scope._FileOperations(race, os.scandir)).page()
    assert codes(page) == {ScopeIssueCode.FILE_CHANGED}
    assert page.completion.state is ScopeState.PARTIAL
    assert not page.entries and not page.cursor


def test_root_metadata_change_during_enumeration_is_not_complete(root):
    file(root, "in_scope/a.bin")
    changed = False

    def race(path):
        nonlocal changed
        info = path.lstat()
        if path == root and changed:
            return replace(FakeStat(st_mode=info.st_mode, st_size=info.st_size,
                                st_mtime_ns=info.st_mtime_ns, st_dev=info.st_dev,
                                st_ino=info.st_ino, st_file_attributes=getattr(info, 'st_file_attributes', 0)),
                       st_mtime_ns=info.st_mtime_ns + 1)
        if path == root:
            changed = True
        return info

    page = ScopeEnumerator(root, ScopeRequest(("in_scope",)),
                           _io=scope._FileOperations(race, os.scandir)).page()
    assert codes(page) == {ScopeIssueCode.ROOT_CHANGED}
    assert page.completion.state is ScopeState.PARTIAL
    assert not page.entries and not page.cursor


def test_root_disappearing_between_pages_stops_with_no_further_entries(root):
    for name in ("a.bin", "b.bin", "c.bin"):
        file(root, "in_scope/" + name)
    source = ScopeEnumerator(root, ScopeRequest(("in_scope",), page_size=1))
    page = source.page()
    assert len(page.entries) == 1 and page.cursor

    def offline(path):
        if path == root:
            raise OSError(1236, "synthetic network root offline")
        return path.lstat()

    source._io = scope._FileOperations(offline, os.scandir)
    failed = source.page(page.cursor)
    assert not failed.entries and not failed.cursor
    assert failed.completion.state is ScopeState.PARTIAL
    assert ScopeIssueCode.ROOT_UNAVAILABLE in codes(failed)


def test_entry_directory_and_exclusion_budgets_are_bounded(root):
    for index in range(4):
        file(root, "in_scope/nested/%d.bin" % index)
    page = first(root, policy=ScopePolicy(max_entries=3))
    assert codes(page) == {ScopeIssueCode.ENTRY_BUDGET_EXHAUSTED}
    assert page.completion.state is ScopeState.BUDGET_EXHAUSTED
    assert not page.cursor
    page = first(root, policy=ScopePolicy(max_directories=1))
    assert codes(page) == {ScopeIssueCode.DIRECTORY_BUDGET_EXHAUSTED}
    page = first(root, policy=ScopePolicy(max_exclusions=1),
                 request=ScopeRequest(("in_scope", "IN_SCOPE", "in_SCOPE")))
    assert codes(page) == {ScopeIssueCode.EXCLUSION_BUDGET_EXHAUSTED}


def test_issue_budget_stops_and_keeps_terminal_reason(root):
    result = first(root, ScopeRequest((), ("../bad", "also:ads")),
                   ScopePolicy(max_issues=1))
    assert codes(result) == {ScopeIssueCode.ISSUE_BUDGET_EXHAUSTED}
    assert result.completion.state is ScopeState.BUDGET_EXHAUSTED


def test_fake_monotonic_clock_and_cancellation_are_not_full(root):
    file(root, "in_scope/a.bin")
    class Clock:
        def __init__(self):
            self.now = 0.0
        def __call__(self):
            self.now += 0.6
            return self.now
    page = ScopeEnumerator(root, ScopeRequest(("in_scope",)),
                           ScopePolicy(max_seconds=1), _monotonic=Clock()).page()
    assert codes(page) == {ScopeIssueCode.TIME_BUDGET_EXHAUSTED}
    assert page.completion.state is ScopeState.BUDGET_EXHAUSTED
    cancel = Cancel()
    page = first(root, cancellation=cancel)
    assert cancel.calls > 0
    assert codes(page) == {ScopeIssueCode.CANCELLED}
    assert page.completion.state is ScopeState.CANCELLED
    assert not page.entries and not page.cursor


def test_cancellation_after_first_page_invalidates_delivery(root):
    for name in ("a.bin", "b.bin"):
        file(root, "in_scope/" + name)
    source = ScopeEnumerator(root, ScopeRequest(("in_scope",), page_size=1))
    page = source.page()
    result = source.page(page.cursor, cancellation=Cancel())
    assert not result.entries and not result.cursor
    assert result.completion.state is ScopeState.CANCELLED
    assert ScopeIssueCode.CANCELLED in codes(result)


def test_selection_sequence_is_copied_and_request_is_validated(root):
    prefixes = ["in_scope"]
    request = ScopeRequest(prefixes)
    prefixes.append("outside")
    assert request.prefixes == ("in_scope",)
    for kwargs in ({"prefixes": ()}, {"prefixes": "in_scope"},
                   {"prefixes": ("in_scope",), "root_revision": -1},
                   {"prefixes": ("in_scope",), "page_size": 0},
                   {"prefixes": ("in_scope",), "kind": "project", "root_id": "other"},
                   {"prefixes": ("in_scope",), "kind": "external", "root_id": "project",
                    "root_revision": True}):
        with pytest.raises(ContractError):
            ScopeRequest(**kwargs)
    for kwargs in ({"max_entries": 0}, {"max_directories": 0}, {"max_seconds": 0},
                   {"max_seconds": float("nan")}, {"max_entries": True},
                   {"additional_file_excludes": ("nested/*.jpg",)},
                   {"additional_file_excludes": ("nested/*.jpg",)}):
        with pytest.raises(ContractError):
            ScopePolicy(**kwargs)
    with pytest.raises(ContractError):
        first(root, ScopeRequest(("in_scope",)), cancellation=object())
    with pytest.raises(ContractError):
        ScopeEnumerator("relative", ScopeRequest(("in_scope",)))


def test_single_use_page_and_malformed_cursor_fail_closed(root):
    file(root, "in_scope/a.bin")
    source = ScopeEnumerator(root, ScopeRequest(("in_scope",)))
    page = source.page()
    with pytest.raises(ContractError):
        source.page()
    with pytest.raises(ContractError):
        source.page("!!!!")
    assert len(paths(page)) == 1


def test_only_synthetic_fixture_files_are_created(tmp_path, root):
    assert str(root).startswith(str(tmp_path))
    assert not (root.parent / "escape").exists()
    before = {path.relative_to(root).as_posix() for path in root.rglob("*")}
    file(root, "in_scope/a.bin")
    result = first(root)
    after = {path.relative_to(root).as_posix() for path in root.rglob("*")}
    assert before | {"in_scope/a.bin"} == after
    assert result.completion.full
