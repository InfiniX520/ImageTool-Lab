"""AR2-06 regressions: real directory I/O, page cost, and mid-page stops.

The earlier scope "linearity" tests counted only physical-identity comparisons,
which is one sub-step: they stayed green while explicit file selections re-read
the same parent directory once per file (3,240 / 12,880 / 51,360 directory
entries for 80 / 160 / 320 selections) and while every page re-lstat'ed every
recorded directory (1,872 / 6,952 / 26,712 extra lstat calls for 40 / 80 / 160
directories at page_size=1).

Every case here therefore measures the real adapter boundary: how often
scandir is opened, how many entries it yields, how many lstat calls happen, how
many pages a delivery has, and where a cancellation or an exhausted budget
stops it.  No assertion depends on wall-clock time, machine load, or a fixed
number of seconds; growth is always compared as n versus 2n.

All fixtures are synthetic temporary directories.  Nothing here opens a real
library, database, account, or network resource.
"""
import os
import time
from contextlib import contextmanager

import pytest

import core.archive_scope as scope
from core.archive_scope import (
    ScopeEnumerator, ScopeIssueCode, ScopePolicy, ScopeRequest, ScopeState,
    consume_scope_pages,
)


class _Recorder:
    """Counts every call the scope makes on the filesystem adapter.

    The optional `on_lstat` hook runs after each metadata call, so a test can
    change the cancellation flag or the clock exactly between two real calls
    instead of guessing when the scope will look at them.
    """

    def __init__(self):
        self.lstat_paths = []
        self.scandir_calls = []
        self.on_lstat = None

    def lstat(self, path):
        info = path.lstat()
        self.lstat_paths.append(path)
        if self.on_lstat is not None:
            self.on_lstat(path)
        return info

    @contextmanager
    def scandir(self, path):
        record = {"path": path, "items": 0}
        self.scandir_calls.append(record)
        with os.scandir(path) as children:
            def iterate():
                for child in children:
                    record["items"] += 1
                    yield child
            yield iterate()

    def adapter(self):
        return scope._FileOperations(self.lstat, self.scandir)

    @property
    def scan_count(self):
        return len(self.scandir_calls)

    @property
    def scanned_items(self):
        return sum(record["items"] for record in self.scandir_calls)


class _Cancel:
    """A cancellation flag that also counts how often it was consulted."""

    def __init__(self, value=False):
        self.value = value
        self.calls = 0

    def is_set(self):
        self.calls += 1
        return self.value

    def trip(self, _path=None):
        self.value = True


class _Clock:
    """Manual monotonic clock: tests decide what work "costs"."""

    def __init__(self, start=0.0):
        self.now = float(start)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.now

    def advance(self, seconds):
        self.now += seconds


def write(root, relative, data=b"synthetic"):
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def paths(page):
    return [entry.path.relative_path for entry in page.entries]


def page_lstat_paths(recorder, before):
    return recorder.lstat_paths[before:]


def drain(source):
    page = source.page()
    pages = [page]
    while page.cursor:
        page = source.page(page.cursor)
        pages.append(page)
    return pages


# --- explicit selections: one bounded directory pass per parent -------------


def test_explicit_files_never_rescan_their_parent_per_file(tmp_path):
    """n distinct files in one directory need one listing, never n listings.

    Since AR4-05 every explicit selection is resolved to its real directory entry,
    so the parent is listed once per enumeration and reused for all selections in
    it.  Before the earlier fix this shape visited n * (n/2) directory entries:
    3,240 for 80 files and 51,360 for 320.
    """
    for count in (80, 160):
        root = tmp_path / ("flat-%d" % count)
        root.mkdir()
        names = []
        for index in range(count):
            relative = "file%04d.jpg" % index
            write(root, relative)
            names.append(relative)
        recorder = _Recorder()
        source = ScopeEnumerator(root, ScopeRequest((), tuple(names), page_size=1000),
                                 ScopePolicy(max_seconds=60), _io=recorder.adapter())
        page = source.page()
        assert paths(page) == names
        assert page.completion.full
        # Exactly one listing of the one parent for every selection in it.
        assert recorder.scan_count == 1
        assert recorder.scanned_items == count
        # One lstat per selection to capture it, one for the closing check.
        assert 2 * count <= len(recorder.lstat_paths) <= 2 * count + 40
        assert source.identity_comparisons == count


def test_explicit_file_io_grows_with_n_and_not_with_n_squared(tmp_path):
    """n versus 2n: metadata doubles, directory reads stay constant."""
    measured = {}
    for count in (160, 320):
        root = tmp_path / ("growth-%d" % count)
        root.mkdir()
        names = ["f%04d.png" % index for index in range(count)]
        for name in names:
            write(root, name)
        recorder = _Recorder()
        source = ScopeEnumerator(root, ScopeRequest((), tuple(names), page_size=1000),
                                 ScopePolicy(max_seconds=60), _io=recorder.adapter())
        source.page()
        measured[count] = (recorder.scan_count, len(recorder.lstat_paths))
    assert measured[160][0] == measured[320][0] == 1
    assert measured[320][1] <= 2.2 * measured[160][1]
    assert measured[320][1] < 320 * 10


def test_case_alias_selections_read_each_parent_once(tmp_path):
    """n real names plus n alternate spellings read the parent twice, not 2n."""
    for count in (80, 160):
        root = tmp_path / ("alias-%d" % count)
        (root / "gallery").mkdir(parents=True)
        real = []
        for index in range(count):
            relative = "gallery/file%04d.jpg" % index
            write(root, relative)
            real.append(relative)
        if not (root / "GALLERY").exists():
            pytest.skip("This filesystem is case sensitive; the alias cannot exist")
        aliases = ["GALLERY/FILE%04d.JPG" % index for index in range(count)]
        selections = tuple(real) + tuple(aliases)
        recorder = _Recorder()
        source = ScopeEnumerator(root, ScopeRequest((), selections, page_size=1000),
                                 ScopePolicy(max_seconds=60), _io=recorder.adapter())
        page = source.page()
        assert page.completion.full
        assert paths(page) == real
        # One pass over the root and one over the shared parent, whatever n is.
        assert recorder.scan_count == 2
        assert recorder.scanned_items <= 2 * count + 4
        # Either spelling may be the one that was listed; it is still one pass over
        # each physical directory, resolved by identity rather than by path string.
        listed = {os.path.normcase(record["path"].name) for record in recorder.scandir_calls}
        assert listed == {os.path.normcase(root.name), "gallery"}
        duplicate = [item for item in page.exclusions
                     if item.reason == "duplicate_selection"]
        assert len(duplicate) == len(aliases)


def test_hardlink_aliases_read_each_parent_once(tmp_path):
    """Hardlink pairs need the raw name of each path, still one pass per parent."""
    measured = []
    for count in (40, 80):
        root = tmp_path / ("hardlink-%d" % count)
        (root / "gallery").mkdir(parents=True)
        for index in range(count):
            relative = "gallery/a%04d.jpg" % index
            first = write(root, relative, b"payload")
            alias = root / "gallery" / ("b%04d.jpg" % index)
            try:
                os.link(first, alias)
            except OSError as exc:
                pytest.skip("Synthetic hardlink unavailable: " + type(exc).__name__)
        recorder = _Recorder()
        source = ScopeEnumerator(
            root, ScopeRequest(("gallery",), page_size=1000),
            ScopePolicy(max_seconds=60), _io=recorder.adapter())
        page = source.page()
        assert page.completion.full
        assert len(page.aliases) == count
        # One traversal read, one root read and one parent read for the spelling
        # question of all aliases together: never one read per alias.
        assert recorder.scan_count == 3
        assert recorder.scanned_items <= 4 * count + 4
        measured.append(recorder.scanned_items)
    # 40 -> 80 aliases: the single directory pass doubles with n, never with n².
    assert measured[1] <= 2.2 * measured[0]


def test_repeated_selection_of_one_file_does_not_rescan(tmp_path):
    """The same path selected many times is resolved once, then excluded."""
    root = tmp_path / "repeated"
    (root / "gallery").mkdir(parents=True)
    names = ["gallery/f%02d.jpg" % index for index in range(20)]
    for name in names:
        write(root, name)
    recorder = _Recorder()
    source = ScopeEnumerator(root, ScopeRequest((), tuple(names) * 4, page_size=1000),
                             ScopePolicy(max_seconds=60), _io=recorder.adapter())
    page = source.page()
    assert paths(page) == names
    # One listing of the root and one of "gallery" serve all repeated selections.
    assert recorder.scan_count == 2
    assert len([item for item in page.exclusions
                if item.reason == "duplicate_selection"]) == 3 * len(names)


def test_deep_directory_selections_stay_linear_in_files_and_depth(tmp_path):
    """Depth adds one ancestor check and one parent listing, never a scan per file."""
    measured = {}
    deep = "a/b/c/d/e/f"
    for count in (60, 120):
        root = tmp_path / ("deep-%d" % count)
        (root / deep.replace("/", os.sep)).parent.mkdir(parents=True)
        names = []
        for index in range(count):
            relative = deep + "/d%04d.jpg" % index
            write(root, relative)
            names.append(relative)
        recorder = _Recorder()
        source = ScopeEnumerator(root, ScopeRequest((), tuple(names), page_size=1000),
                                 ScopePolicy(max_seconds=60), _io=recorder.adapter())
        page = source.page()
        assert page.completion.full
        # a, a/b, ... a/b/c/d/e/f: one listing per distinct parent, not per file.
        assert recorder.scan_count == 7
        measured[count] = len(recorder.lstat_paths)
    # 60 -> 120 files: 6 ancestor checks plus 2 file checks each.
    assert measured[120] <= 2.2 * measured[60]
    assert measured[120] <= 120 * (6 + 2) + 60


def test_raw_name_index_invalidates_on_state_and_identity_change(tmp_path):
    """The index is bound to one directory, not to a path string."""
    root = tmp_path / "indexed"
    (root / "gallery").mkdir(parents=True)
    write(root, "gallery/a.jpg")
    recorder = _Recorder()
    guard = scope._WorkGuard(time.monotonic, None, 60.0)
    index = scope._RawNameIndex(recorder.adapter(), root, guard,
                                lambda relative: True)
    first = index.index("gallery")
    assert first == frozenset({"a.jpg"})
    assert recorder.scan_count == 1
    # Unchanged directory: reused, no second read.
    assert index.index("gallery") == frozenset({"a.jpg"})
    assert recorder.scan_count == 1
    # Membership change: state moved, so the names are read again.
    write(root, "gallery/b.jpg")
    os.utime(root / "gallery", ns=(0, 10 ** 18))
    second = index.index("gallery")
    assert second == frozenset({"a.jpg", "b.jpg"})
    assert recorder.scan_count == 2
    # A different directory at the same path must not inherit the old names.
    gallery = root / "gallery"
    gallery.rename(root / "gallery-old")
    gallery.mkdir()
    write(root, "gallery/c.jpg")
    third = index.index("gallery")
    assert third == frozenset({"c.jpg"})
    assert recorder.scan_count == 3


def test_raw_name_index_never_opens_an_excluded_output_directory(tmp_path):
    """Resolving a spelling must not read a managed output or credential folder."""
    root = tmp_path / "protected"
    (root / "archive_store" / "nested").mkdir(parents=True)
    write(root, "gallery/a.jpg")
    write(root, "archive_store/nested/package.7z", b"archive")
    selections = ("archive_store/nested/package.7z",
                  "ARCHIVE_STORE/NESTED/PACKAGE.7Z",
                  "gallery/a.jpg")
    recorder = _Recorder()
    page = ScopeEnumerator(root, ScopeRequest((), selections, page_size=100),
                           ScopePolicy(max_seconds=60), _io=recorder.adapter()).page()
    assert paths(page) == ["gallery/a.jpg"]
    assert page.completion.full
    opened = [record["path"] for record in recorder.scandir_calls]
    # Resolving spellings may list an ordinary parent such as "gallery", but never
    # a managed output or credential directory, and never outside the root.
    assert all(path == root or root in path.parents for path in opened)
    assert not [path for path in opened if "archive_store" in path.parts]
    assert {path.name for path in opened} <= {root.name, "gallery"}


def test_raw_name_index_refuses_a_directory_that_keeps_changing(tmp_path):
    """Repeated instability is refused instead of trusted or retried forever."""
    root = tmp_path / "unstable"
    (root / "gallery").mkdir(parents=True)
    write(root, "gallery/a.jpg")
    recorder = _Recorder()
    guard = scope._WorkGuard(time.monotonic, None, 60.0)
    index = scope._RawNameIndex(recorder.adapter(), root, guard, lambda relative: True)
    original = index.index("gallery")
    gallery = root / "gallery"
    # Every lookup sees a new modification time, so every lookup is a rescan.
    for step in range(scope._MAX_NAME_INDEX_RESCANS):
        os.utime(gallery, ns=(0, 10 ** 18 + step * 10 ** 9))
        assert index.index("gallery") == original
    os.utime(gallery, ns=(0, 10 ** 18 + 10 ** 12))
    with pytest.raises(Exception) as failure:
        index.index("gallery")
    assert getattr(failure.value, "issue", None) is not None
    assert failure.value.issue.code is ScopeIssueCode.DIRECTORY_CHANGED


# --- paging: bounded re-verification, no full sweep per page ----------------


def _tree(root, directories, files_per_directory=1):
    for index in range(directories):
        sub = "d%04d" % index
        for offset in range(files_per_directory):
            write(root, "%s/f%02d.png" % (sub, offset))


def test_paging_metadata_grows_with_directories_not_with_pages(tmp_path):
    """40 -> 80 -> 160 one-file directories: lstat grows linearly, not D*pages.

    The re-audit measured 1,872 / 6,952 / 26,712 paging-only lstat calls for
    this shape at page_size=1; the bound below fails by an order of magnitude
    for that behaviour and holds for a bounded rotating verification.
    """
    measured = {}
    for count in (40, 80):
        root = tmp_path / ("paged-%d" % count)
        root.mkdir()
        _tree(root, count)
        recorder = _Recorder()
        source = ScopeEnumerator(root, ScopeRequest((".",), page_size=1),
                                 ScopePolicy(max_seconds=60), _io=recorder.adapter())
        first = source.page()
        before = len(recorder.lstat_paths)
        pages = 1
        page = first
        while page.cursor:
            page = source.page(page.cursor)
            pages += 1
        assert page.completion.full
        paging = len(recorder.lstat_paths) - before
        measured[count] = (pages, paging)
        depth = len(source._snapshot.ancestors)
        # Rotation + the closing sweep, plus one root/ancestor check per page.
        assert paging <= 2 * count + pages * (depth + 1)
        assert paging < 15 * count
    assert measured[40][0] == 40 and measured[80][0] == 80
    assert measured[80][1] <= 2.5 * measured[40][1]


@pytest.mark.parametrize("page_size", [1, 100, 1000])
def test_pages_do_not_repeat_the_whole_directory_sweep(tmp_path, page_size):
    """60 directories, 120 entries: paging cost stays O(directories + pages)."""
    root = tmp_path / ("sized-%d" % page_size)
    root.mkdir()
    _tree(root, 60, files_per_directory=2)
    recorder = _Recorder()
    source = ScopeEnumerator(root, ScopeRequest((".",), page_size=page_size),
                             ScopePolicy(max_seconds=60), _io=recorder.adapter())
    first = source.page()
    before = len(recorder.lstat_paths)
    pages = 1
    page = first
    while page.cursor:
        page = source.page(page.cursor)
        pages += 1
    assert page.completion.full
    assert pages == -(-120 // page_size)
    depth = len(source._snapshot.ancestors)
    paging = len(recorder.lstat_paths) - before
    assert paging <= 2 * 60 + pages * (depth + 1)
    # Directory reads after the enumeration are only the real membership
    # re-reads, never a stub scan added per page.
    assert len(recorder.scandir_calls) == 61


def test_final_page_verifies_every_directory_before_a_receipt(tmp_path):
    """The page that can complete a receipt checks all recorded directories."""
    root = tmp_path / "closing"
    root.mkdir()
    _tree(root, 8)
    recorder = _Recorder()
    source = ScopeEnumerator(root, ScopeRequest((".",), page_size=1),
                             ScopePolicy(max_seconds=60), _io=recorder.adapter())
    pages = []
    before = len(recorder.lstat_paths)
    page = source.page()
    pages.append((page, before))
    while page.cursor:
        before = len(recorder.lstat_paths)
        page = source.page(page.cursor)
        pages.append((page, before))
    assert len(pages) == 8
    last_page, last_before = pages[-1]
    last_lstat = set(page_lstat_paths(recorder, last_before))
    # Every recorded directory was re-checked on the closing page.
    expected = {root} | {root / ("d%04d" % index) for index in range(8)}
    assert expected <= last_lstat
    receipt = consume_scope_pages([item for item, _ in pages], source=source)
    assert receipt.scope_complete and receipt.snapshot_verified


def test_membership_change_outside_the_rotation_window_is_caught(tmp_path):
    """A late change is still refused before the delivery can be complete."""
    root = tmp_path / "late-change"
    root.mkdir()
    _tree(root, 8)
    recorder = _Recorder()
    source = ScopeEnumerator(root, ScopeRequest((".",), page_size=1),
                             ScopePolicy(max_seconds=60), _io=recorder.adapter())
    pages = [source.page()]
    # The change lands in the last directory of the snapshot, which the first
    # page's rotation slice does not cover.
    write(root, "d0007/late.png")
    while pages[-1].cursor:
        pages.append(source.page(pages[-1].cursor))
    failed = [page for page in pages
              if page.completion.state is not ScopeState.COMPLETE]
    assert failed, "the late membership change was never observed"
    assert {issue.code for issue in failed[0].issues} == {ScopeIssueCode.DIRECTORY_CHANGED}
    assert not failed[0].entries
    receipt = consume_scope_pages(pages, source=source)
    assert receipt.aborted and not receipt.scope_complete
    assert not receipt.consumption_complete


def test_nested_directory_change_is_caught_before_the_receipt(tmp_path):
    """Deep nesting keeps the membership guard: the leaf directory is re-read."""
    root = tmp_path / "deep-change"
    (root / "a" / "b" / "c").mkdir(parents=True)
    for index in range(4):
        write(root, "a/b/c/f%02d.png" % index)
    writer = _Recorder()
    source = ScopeEnumerator(root, ScopeRequest(("a/b/c",), page_size=1),
                             ScopePolicy(max_seconds=60), _io=writer.adapter())
    pages = [source.page()]
    write(root, "a/b/c/appended.png")
    while pages[-1].cursor:
        pages.append(source.page(pages[-1].cursor))
    failed = [page for page in pages
              if page.completion.state is not ScopeState.COMPLETE]
    assert failed
    assert {issue.code for issue in failed[0].issues} == {ScopeIssueCode.DIRECTORY_CHANGED}
    assert not consume_scope_pages(pages, source=source).scope_complete


def test_unchanged_directories_are_not_re_read_even_after_a_touch(tmp_path):
    """A moved modification time with identical members is not re-read forever."""
    root = tmp_path / "touched"
    (root / "gallery").mkdir(parents=True)
    for index in range(6):
        write(root, "gallery/f%d.png" % index)
    recorder = _Recorder()
    source = ScopeEnumerator(root, ScopeRequest(("gallery",), page_size=1),
                             ScopePolicy(max_seconds=60), _io=recorder.adapter())
    pages = [source.page()]
    gallery = root / "gallery"
    # One unrelated modification of the directory, then no further change: the
    # confirmed state is remembered instead of re-reading the names per page.
    os.utime(gallery, ns=(0, 10 ** 18))
    while pages[-1].cursor:
        pages.append(source.page(pages[-1].cursor))
    assert all(page.completion.full for page in pages)
    assert paths(pages[-1]) == ["gallery/f5.png"]
    # Initial enumeration read + the first touch's confirmation; later pages
    # compare against the confirmed state instead of re-reading every time.
    assert len([record for record in recorder.scandir_calls
                if record["path"].name == "gallery"]) <= 2


def test_root_membership_is_still_checked_on_every_page(tmp_path):
    """The bounded slice never drops the root or the ancestor identity guard."""
    root = tmp_path / "root-guard"
    root.mkdir()
    for name in ("a.png", "b.png", "c.png", "d.png"):
        write(root, name)
    recorder = _Recorder()
    source = ScopeEnumerator(root, ScopeRequest((".",), page_size=1),
                             ScopePolicy(max_seconds=60), _io=recorder.adapter())
    first = source.page()
    before = len(recorder.lstat_paths)
    second = source.page(first.cursor)
    seen = {str(path) for path in page_lstat_paths(recorder, before)}
    assert str(root) in seen
    assert second.completion.state is ScopeState.COMPLETE


# --- cancellation and work budget inside one page --------------------------


def test_cancellation_during_page_io_returns_no_entries(tmp_path):
    """The audited probe: the flag is set by the page's first metadata call."""
    root = tmp_path / "cancel-io"
    root.mkdir()
    for name in ("a.png", "b.png", "c.png"):
        write(root, name)
    recorder = _Recorder()
    cancel = _Cancel()
    source = ScopeEnumerator(root, ScopeRequest((".",), page_size=1),
                             ScopePolicy(max_seconds=60), _io=recorder.adapter())
    first = source.page(cancellation=cancel)
    assert paths(first) == ["a.png"]
    before = len(recorder.lstat_paths)
    armed = {"value": False}

    def trip_once(_path):
        if armed["value"]:
            return
        armed["value"] = True
        cancel.trip()

    recorder.on_lstat = trip_once
    calls_before = cancel.calls
    aborted = source.page(first.cursor, cancellation=cancel)
    assert aborted.completion.state is ScopeState.CANCELLED
    assert not aborted.entries
    assert aborted.cursor is None
    assert {issue.code for issue in aborted.issues} == {ScopeIssueCode.CANCELLED}
    # The search stopped between two calls: no further metadata read happened.
    assert len(recorder.lstat_paths) - before == 1
    # Cancellation was consulted before and after that call.
    assert cancel.calls - calls_before >= 2


def test_page_work_budget_stops_inside_the_page(tmp_path):
    """Simulated work inside one page stops it; idle time between pages does not."""
    root = tmp_path / "budget"
    root.mkdir()
    for name in ("a.png", "b.png", "c.png", "d.png"):
        write(root, name)
    recorder = _Recorder()
    clock = _Clock()
    source = ScopeEnumerator(root, ScopeRequest((".",), page_size=1),
                             ScopePolicy(max_seconds=1), _io=recorder.adapter(),
                             _monotonic=clock)
    first = source.page()
    assert paths(first) == ["a.png"]
    # The caller walks away for a long time: an idle gap is not execution time.
    clock.advance(10_000.0)
    second = source.page(first.cursor)
    assert second.completion.state is ScopeState.COMPLETE
    # Now the page itself performs 16 simulated seconds of work, as the audited
    # probe did, with a one second budget.
    recorder.on_lstat = lambda _path: clock.advance(16.0)
    aborted = source.page(second.cursor)
    assert aborted.completion.state is ScopeState.PARTIAL
    assert {issue.code for issue in aborted.issues} == {
        ScopeIssueCode.TIME_BUDGET_EXHAUSTED}
    assert not aborted.entries and aborted.cursor is None


def test_cancellation_is_checked_between_directory_entries(tmp_path):
    """A directory scan stops at the first entry after the flag is set."""
    root = tmp_path / "cancel-scan"
    root.mkdir()
    for index in range(50):
        write(root, "f%03d.png" % index)
    recorder = _Recorder()
    cancel = _Cancel()
    guard = scope._WorkGuard(time.monotonic, cancel.is_set, 60.0)
    calls = {"seen": 0}

    original_scandir = recorder.scandir

    @contextmanager
    def tripping_scandir(path):
        with original_scandir(path) as children:
            def iterate():
                for child in children:
                    calls["seen"] += 1
                    if calls["seen"] == 3:
                        cancel.trip()
                    yield child
            yield iterate()

    # The adapter must be built after the patch: it captures the callables here.
    recorder.scandir = tripping_scandir  # type: ignore[assignment]
    index = scope._RawNameIndex(recorder.adapter(), root, guard, lambda relative: True)
    with pytest.raises(Exception) as failure:
        index.index(".")
    assert failure.value.issue.code is ScopeIssueCode.CANCELLED
    assert calls["seen"] == 3
    assert failure.value.state is ScopeState.CANCELLED


def test_complete_delivery_of_a_flat_scope_still_produces_a_receipt(tmp_path):
    """The bounded verification must not weaken an ordinary receipt."""
    root = tmp_path / "receipt"
    root.mkdir()
    names = ["f%03d.png" % index for index in range(25)]
    for name in names:
        write(root, name)
    recorder = _Recorder()
    source = ScopeEnumerator(root, ScopeRequest((".",), page_size=10),
                             ScopePolicy(max_seconds=60), _io=recorder.adapter())
    pages = drain(source)
    assert len(pages) == 3
    receipt = consume_scope_pages(pages, source=source)
    assert receipt.scope_complete and receipt.snapshot_verified
    assert receipt.consumed_entries == 25
    assert [entry for page in pages for entry in page.entries]
    assert receipt.matches_entries([entry for page in pages for entry in page.entries])
