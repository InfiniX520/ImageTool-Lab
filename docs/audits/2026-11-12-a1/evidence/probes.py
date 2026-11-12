"""Independent offline audit cases. Synthetic files and in-memory DBs only.

This records actual behavior, including defects; it is not a passing acceptance
suite. Never imports real configuration, credentials or a production database.
"""
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import replace
from pathlib import Path

BATCH = Path(__file__).resolve().parent
ROOT = BATCH.parents[1]
sys.path.insert(0, str(ROOT / "JpgPng"))
from core import archive_scope as scope_module
from core.archive_scope import ScopeEnumerator, ScopePolicy, ScopeRequest
from core.archive_inventory import ArchiveEntry, ArtworkGroup, CapacityPolicy, build_archive_inventory, plan_archive_capacity
from core.archive_plan_store import save_archive_plan, _encode_scope
from core.archive_schema import ARCHIVE_DDL, _content_key, ensure_archive_asset, record_archive_observation, record_local_observation, validate_archive_facts
from core.library_repository import LibraryRepository, _DDL
from core.local_observation import LocalObserver, ObservationRequest
from core.preservation_policy import ArchiveEvidence, ArchiveState, ContentVersion, LocalEvidence, LocalState
from core.task_contracts import CollectionRequest, PathRef

DATA = BATCH / "probe_data"
DATA.mkdir(exist_ok=False)
TIME = "2026-11-11T16:00:00+00:00"
CONTENT = ContentVersion("asset-1", 1, 4, "a" * 64)
PATH = PathRef(relative_path="source/page.png")
RESULTS = []


def case(name, fn):
    try:
        result = fn()
        RESULTS.append({"case": name, "completed": True, "result": result})
    except Exception as exc:
        RESULTS.append({"case": name, "completed": False, "error": explain(exc)})
    print(name + ": " + json.dumps(RESULTS[-1], ensure_ascii=True), flush=True)


def explain(exc):
    chain = []
    while exc is not None:
        chain.append({"type": type(exc).__name__, "message": str(exc)})
        exc = exc.__cause__
    return chain


def db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    for sql in _DDL + ARCHIVE_DDL:
        conn.execute(sql)
    conn.executemany("INSERT INTO schema_meta VALUES (?,?)", [("format", "jpgpng-library"), ("schema_version", "2")])
    return conn


def plan(batch="batch-1", entries=None):
    if entries is None:
        entries = (ArchiveEntry(CONTENT, PATH, 0),)
    inventory = build_archive_inventory((ArtworkGroup("work-1", tuple(entries)),), batch_id=batch)
    capacity = plan_archive_capacity(inventory, available_additional_bytes=100 * 1024**3)
    return inventory, capacity


def snapshot(page):
    return {"entries": [e.path.relative_path for e in page.entries],
            "exclusions": [{"path": e.relative_path, "reason": e.reason} for e in page.exclusions],
            "issues": [i.code.value for i in page.issues], "full": page.completion.full,
            "snapshot_count": page.completion.entries_in_snapshot, "has_next": page.has_next_page}


def sensitive_explicit():
    root = DATA / "sensitive"; (root / "archive_store" / "nested").mkdir(parents=True)
    for rel in ("pixiv_config.json", "my-cookie.txt", "archive_store/nested/package.7z"):
        (root / rel).write_bytes(b"SYNTHETIC TEST ONLY")
    results = {}
    results["recursive_root"] = snapshot(ScopeEnumerator(root, ScopeRequest((".",))).page())
    results["explicit_files"] = snapshot(ScopeEnumerator(root, ScopeRequest((), (
        "pixiv_config.json", "my-cookie.txt", "archive_store/nested/package.7z"))).page())
    results["nested_output_prefix"] = snapshot(ScopeEnumerator(root, ScopeRequest(("archive_store/nested",))).page())
    return results


def unicode_distinct():
    root = DATA / "unicode"; root.mkdir()
    names = ["\u00e9.png", "e\u0301.png"]
    for index, name in enumerate(names):
        (root / name).write_bytes(bytes([index]))
    actual = [{"name": p.name, "inode": p.stat().st_ino, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in root.iterdir()]
    return {"actual_files": actual, "enumerated": snapshot(ScopeEnumerator(root, ScopeRequest((".",))).page())}


def unrelated_parent_change():
    parent = DATA / "parent-change"; root = parent / "library"; root.mkdir(parents=True)
    for i in range(3): (root / f"{i}.png").write_bytes(b"safe")
    enumerator = ScopeEnumerator(root, ScopeRequest((".",), page_size=1))
    first = enumerator.page(); before = root.stat()
    (parent / "unrelated-report.txt").write_text("ordinary sibling output", encoding="utf-8")
    after = root.stat(); second = enumerator.page(first.cursor)
    return {"root_unchanged": (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns),
            "first": snapshot(first), "second": snapshot(second)}


def partial_page_plan():
    root = DATA / "pages"; root.mkdir()
    for i in range(3): (root / f"{i}.png").write_bytes(b"safe")
    page = ScopeEnumerator(root, ScopeRequest((".",), page_size=1)).page()
    observer = LocalObserver(root, root_id="project", root_revision=0)
    entries = []
    for e in page.entries:
        observation = observer.observe(ObservationRequest(e.content, e.path, "run-1", "check-1"))
        entries.append(replace(e, content=replace(e.content, sha256=observation.evidence.observed_sha256)))
    inv, capacity = plan("partial-page", entries)
    conn = db()
    try:
        saved = save_archive_plan(conn, inv, capacity, scope={"snapshot_count": 3, "consumed_count": 1, "has_next_page": True})
        return {"page": snapshot(page), "persisted_plan_complete": saved.plan.inventory.scope_complete,
                "persisted_plan_count": saved.plan.inventory.path_count}
    finally: conn.close()


def same_content_second_batch():
    conn = db()
    try:
        save_archive_plan(conn, *plan("one"))
        try:
            save_archive_plan(conn, *plan("two"))
            result = {"second_saved": True}
        except Exception as exc:
            result = {"second_saved": False, "error": explain(exc)}
        result["preserved_batch_count"] = conn.execute("SELECT count(*) FROM archive_batches").fetchone()[0]
        return result
    finally: conn.close()


def row_factory_alias():
    conn = db(); conn.row_factory = sqlite3.Row
    try:
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="user_selected")
        try:
            ensure_archive_asset(conn, CONTENT, PathRef(relative_path="other/page.png"), source_kind="user_selected")
            return {"alias_saved": True}
        except Exception as exc: return {"alias_saved": False, "error": explain(exc)}
    finally: conn.close()


def content_revision():
    conn = db()
    try:
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="observed_existing")
        try:
            ensure_archive_asset(conn, replace(CONTENT, revision=2, sha256="c" * 64), PATH, source_kind="observed_existing")
            return {"revision_saved": True}
        except Exception as exc: return {"revision_saved": False, "error": explain(exc)}
    finally: conn.close()


def large_plan():
    entries = [ArchiveEntry(ContentVersion(f"a{i}", 1, 1, "a" * 64), PathRef(relative_path=f"images/{i:05}.png"), i) for i in range(2000)]
    inv, cap = plan("large", entries)
    size = len(_encode_scope(inv, {"kind": "explicit_inventory"}).encode("utf-8"))
    conn = db(); started = time.perf_counter()
    try:
        try:
            save_archive_plan(conn, inv, cap)
            result = {"saved": True}
        except Exception as exc: result = {"saved": False, "error": explain(exc)}
        return {**result, "input_files": 2000, "scope_json_bytes": size, "elapsed_seconds": time.perf_counter() - started,
                "remaining_batches": conn.execute("SELECT count(*) FROM archive_batches").fetchone()[0]}
    finally: conn.close()


def sealed_fixture(conn, content):
    conn.execute("INSERT INTO archive_batches VALUES ('batch',1,'{}','{}','planned',NULL,?)", (TIME,))
    conn.execute("INSERT INTO archive_packages VALUES ('pkg','batch',1,'7z','synthetic','test-key-ref',8,?,'sealed')", ("b" * 64,))
    conn.execute("INSERT INTO archive_members VALUES ('pkg',?,?,?,?,?,4,?)",
                 (content.asset_id, content.revision, PATH.target_key, _content_key(content), "page.png", "a" * 64))
    conn.execute("INSERT INTO archive_objects VALUES ('pkg',1,'package',8,?,'{}')", ("b" * 64,))
    conn.execute("INSERT INTO archive_locations VALUES ('loc','pkg',1,1,'synthetic','{}',?)", (TIME,))


def null_hash_fact_validation():
    conn = db(); legacy = replace(CONTENT, sha256=None)
    try:
        ensure_archive_asset(conn, legacy, PATH, source_kind="user_selected")
        sealed_fixture(conn, legacy)
        claimed = ArchiveEvidence(CONTENT, ArchiveState.USER_RETURNED_VERIFIED, "archive-check", TIME, "loc")
        record_archive_observation(conn, claimed, observed_size=4, observed_sha256="a" * 64)
        try:
            validate_archive_facts(conn)
            valid = True; error = None
        except Exception as exc: valid = False; error = explain(exc)
        return {"unknown_content_digest": conn.execute("SELECT sha256 FROM archive_contents").fetchone()[0],
                "accepted_verified_state": conn.execute("SELECT state FROM archive_checks").fetchone()[0],
                "validator_accepts": valid, "error": error}
    finally: conn.close()


def release_events():
    conn = db()
    try:
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="user_selected"); sealed_fixture(conn, CONTENT)
        evidence = LocalEvidence(CONTENT, PATH, 0, "run", "local", TIME, LocalState.HASH_MATCH, 4, "a" * 64)
        record_local_observation(conn, evidence)
        record_archive_observation(conn, ArchiveEvidence(CONTENT, ArchiveState.USER_RETURNED_VERIFIED, "remote", TIME, "loc"), observed_size=4, observed_sha256="a" * 64)
        conn.execute("INSERT INTO archive_releases VALUES ('operation',?,'local','remote','prepared',?,'{}')", (_content_key(CONTENT), TIME))
        try:
            conn.execute("INSERT INTO archive_releases VALUES ('operation',?,'local','remote','released',?,'{}')", (_content_key(CONTENT), TIME))
            return {"same_operation_append": True}
        except Exception as exc: return {"same_operation_append": False, "error": explain(exc)}
    finally: conn.close()


def receipt_provenance():
    conn = db()
    try:
        ensure_archive_asset(conn, CONTENT, PATH, source_kind="download_receipt", artifact_id=None)
        validate_archive_facts(conn)
        return {"accepted": True, "source_and_receipt": list(conn.execute("SELECT source_kind,artifact_id FROM archive_assets").fetchone())}
    finally: conn.close()


def scope_comparisons():
    observations = []
    for n in (200, 400, 800):
        root = DATA / f"scaling-{n}"; root.mkdir()
        for i in range(n): (root / f"{i:05}.png").write_bytes(b"a")
        original = scope_module.FileIdentity.__eq__; count = [0]
        def counted(self, other):
            count[0] += 1
            return original(self, other)
        scope_module.FileIdentity.__eq__ = counted
        started = time.perf_counter()
        try: page = ScopeEnumerator(root, ScopeRequest((".",))).page()
        finally: scope_module.FileIdentity.__eq__ = original
        observations.append({"files": n, "identity_comparisons": count[0], "elapsed_seconds": time.perf_counter()-started, "complete": page.completion.full})
    return observations


def original_v1_stop_fixture():
    directory = DATA / "v1-stop"; directory.mkdir()
    with LibraryRepository(directory / "library.sqlite3") as repo:
        first = repo.submit(CollectionRequest("one", "synthetic"), "submit-1")
        second = repo.submit(CollectionRequest("two", "synthetic"), "submit-2")
        active = repo.activate_next()
        stop = repo.request_stop(second["job_id"], "stop-2")
        settled = repo.settle_stop(second["batch_id"])
        return {"settle_stop_second": settled, "stop_return": stop, "first_and_second_distinct": first["job_id"] != second["job_id"]}


for name, fn in [
    ("sensitive_explicit_bypass", sensitive_explicit),
    ("unicode_distinct_files", unicode_distinct),
    ("unrelated_ancestor_change", unrelated_parent_change),
    ("partial_page_saved_as_complete", partial_page_plan),
    ("same_content_second_batch", same_content_second_batch),
    ("sqlite_row_factory_alias", row_factory_alias),
    ("new_revision_same_path", content_revision),
    ("two_thousand_file_plan", large_plan),
    ("null_hash_verified_fact", null_hash_fact_validation),
    ("release_event_append", release_events),
    ("receipt_without_artifact", receipt_provenance),
    ("scope_identity_comparison_growth", scope_comparisons),
    ("original_v1_stop_fixture", original_v1_stop_fixture),
]: case(name, fn)

with (BATCH / "probe_results.json").open("x", encoding="utf-8", newline="\n") as f:
    json.dump({"scope": "Synthetic audit evidence, NOT acceptance", "cases": RESULTS}, f, ensure_ascii=False, indent=2)
    f.write("\n")
