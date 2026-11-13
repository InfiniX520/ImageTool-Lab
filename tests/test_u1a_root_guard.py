"""UA-03 (executor side): a bound root that disappeared is never recreated.

The library root is an identity, not a cache directory.  When the volume is
unplugged, the library moved, or the project directory is gone, a worker must
report a typed storage block instead of `mkdir -p`-ing the old path and
downloading into it.  A live root still gains its first output subdirectory.
"""
from core.app_paths import AppPaths
from core.library_paths import PathResolver, RootState
from core.library_repository import LibraryRepository
from core.task_contracts import PathRef
from delivery_fixtures import Response, Transport, executor, make_job, run
from image_fixtures import PNG_BYTES

REL = "downl_Pixiv/123_p0.png"


def test_root_state_separates_available_missing_and_unmapped(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    resolver = PathResolver(AppPaths(project), {"archive": external})
    project_ref = PathRef(relative_path=REL)
    external_ref = PathRef(kind="external", root_id="archive", relative_path="a.png")
    assert resolver.root_state(project_ref) is RootState.AVAILABLE
    assert resolver.root_state(external_ref) is RootState.AVAILABLE
    assert str(resolver.root_state(project_ref)) == "available"
    unmapped = PathRef(kind="external", root_id="unmapped", relative_path="a.png")
    assert resolver.root_state(unmapped) is RootState.UNBOUND
    external.rename(tmp_path / "external-offline")
    assert resolver.root_state(external_ref) is RootState.MISSING_BOUND


def test_root_state_treats_a_file_at_the_root_as_missing(tmp_path):
    project = tmp_path / "project"
    project.write_bytes(b"not a directory")
    resolver = PathResolver(AppPaths(project))
    assert resolver.root_state(PathRef(relative_path=REL)) is RootState.MISSING_BOUND


def test_executor_does_not_recreate_a_bound_root_that_disappeared(tmp_path):
    project = tmp_path / "library"
    (project / "downl_Pixiv").mkdir(parents=True)
    (project / "downl_Pixiv" / ".stale-delivery.part").write_bytes(b"partial bytes")
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, ids, _ = make_job(repo)
        offline = tmp_path / "library-offline"
        project.rename(offline)
        transport = Transport(Response())
        outcome = run(repo, job, ids[0], executor(repo, project, transport))
        assert (outcome.state, outcome.error_code) == ("suspended", "root_unavailable")
        assert not project.exists(), "The executor recreated an offline bound root"
        assert transport.calls == [], "An offline root must not start a download"
        batch = repo.get_batch(job["batch_id"])
        assert batch["phase"] == "pausing"
        assert batch["stop_reason"] == "root_unavailable"
        row = repo.get_delivery(ids[0])
        assert (row["state"], row["error_code"]) == ("suspended", "root_unavailable")
        moved = offline / "downl_Pixiv"
        assert sorted(item.name for item in moved.iterdir()) == [".stale-delivery.part"]
        assert (moved / ".stale-delivery.part").read_bytes() == b"partial bytes"


def test_first_output_subdirectory_is_still_created_under_a_live_root(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as repo:
        job, ids, _ = make_job(repo)
        assert not (tmp_path / "downl_Pixiv").exists()
        outcome = run(repo, job, ids[0], executor(repo, tmp_path, Transport(Response())))
        assert outcome.state == "completed"
        assert (tmp_path / "downl_Pixiv" / "123_p0.png").read_bytes() == PNG_BYTES

