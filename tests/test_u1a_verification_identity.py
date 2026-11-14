#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Contract v6 sec.4.3: the recovery command acts on the row the user saw.

The list names a delivery; the command used to read whatever delivery the page
was linked to at that moment.  These tests pin the difference:

* a row whose delivery is no longer the linked one is refused with
  'state_changed' **before any file is observed**;
* the matching row still walks the ordinary observation path and the existing
  compare-and-set;
* the desktop port refuses to act without the identity, so no caller can
  quietly fall back to the unchecked behaviour.

Everything here is synthetic: one temporary ledger, one temporary output root,
PNG bytes from the shared image fixture.  No network, account, real library or
real gallery is touched, and no test deletes or rewrites a historical row.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.app_paths import AppPaths
from core.collection_desktop import DesktopSession
from core.library_paths import PathResolver
from core.library_repository import LibraryError, LibraryRepository

import test_u1a_recovery as h


@pytest.fixture
def repo(tmp_path):
    with LibraryRepository(tmp_path / "ledger.sqlite3") as value:
        yield value


@pytest.fixture
def library_root(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    return root


@pytest.fixture
def held_page(repo, library_root):
    """A second task parked on a page whose recorded bytes were replaced."""
    job, hold_id, first = h.parked_hold(repo, library_root)
    repo.bind_path_resolver(PathResolver(AppPaths(library_root)))
    return job, hold_id, first


def test_a_stale_delivery_id_is_refused_before_any_file_is_observed(
    repo, held_page, monkeypatch
):
    job, hold_id, _first = held_page
    observed = []

    def refuse_to_observe(self, artifact, **kwargs):
        observed.append(artifact)
        raise AssertionError("a stale row must not observe a file")

    monkeypatch.setattr(LibraryRepository, "_observe_artifact", refuse_to_observe)
    result = repo.recover_verification_page(
        job["batch_id"], h.ARTWORK, 0, expected_delivery_id="d-somebody-else"
    )
    assert observed == [], "the observation path was entered"
    assert result["outcome"] == "refused"
    assert result["reason"] == "state_changed"
    assert result["health"] is None
    assert result["repair_delivery_id"] is None
    # The refusal names the row that really is linked, not the stale one.
    assert result["previous_delivery_id"] == hold_id
    assert result["state"] == "needs_verification"
    assert repo.get_delivery(hold_id)["state"] == "needs_verification"


def test_the_matching_delivery_id_still_walks_the_observation_path(
    repo, held_page, monkeypatch
):
    job, hold_id, first = held_page
    root = repo.database_path.parent / "library"
    h.target(root).write_bytes(h.PNG_BYTES)
    seen = []
    original = LibraryRepository._observe_artifact

    def spy(self, artifact, **kwargs):
        seen.append(artifact["artifact_id"])
        return original(self, artifact, **kwargs)

    monkeypatch.setattr(LibraryRepository, "_observe_artifact", spy)
    result = repo.recover_verification_page(
        job["batch_id"], h.ARTWORK, 0, expected_delivery_id=hold_id
    )
    assert len(seen) == 1, "exactly one candidate was observed"
    assert result["outcome"] == "reused", result
    assert result["reason"] == "hash_match"
    assert result["previous_delivery_id"] == hold_id
    assert result["observed_sha256"] == h.PNG_SHA256
    # The existing compare-and-set still ran: the hold is retired, not removed.
    assert repo.get_delivery(hold_id)["state"] == "cancelled"
    assert repo.get_artifact(first)["artifact_id"] == result["artifact_id"]


def test_the_recovery_snapshot_names_the_link_it_compares(repo, held_page):
    job, hold_id, _first = held_page
    page = repo._read_recovery_page(job["batch_id"], h.ARTWORK, 0)
    assert page.linked_delivery_id == hold_id
    assert page.delivery_id == hold_id


def test_an_invalid_expected_delivery_id_is_a_library_error(repo, held_page):
    job, _hold_id, _first = held_page
    for bad in ("", 0, None.__class__, b"d-1", "x" * 513):
        with pytest.raises(LibraryError):
            repo.recover_verification_page(
                job["batch_id"], h.ARTWORK, 0, expected_delivery_id=bad
            )


def test_a_replayed_command_keeps_its_first_verdict(repo, held_page, monkeypatch):
    """A command id owns its result: the identity check cannot rewrite history."""
    job, hold_id, _first = held_page
    observed = []
    monkeypatch.setattr(
        LibraryRepository, "_observe_artifact",
        lambda self, artifact, **kwargs: observed.append(artifact),
    )
    first = repo.recover_verification_page(
        job["batch_id"], h.ARTWORK, 0, expected_delivery_id="d-stale",
        command_id="recover-identity-1",
    )
    again = repo.recover_verification_page(
        job["batch_id"], h.ARTWORK, 0, expected_delivery_id=hold_id,
        command_id="recover-identity-1",
    )
    assert first["reason"] == "state_changed"
    assert again == first
    assert observed == []


def test_the_port_refuses_to_act_without_the_expected_delivery_id():
    session = object.__new__(DesktopSession)
    session._command = lambda method, *args: (method, args)
    with pytest.raises(ValueError):
        session.recover_verification_page("batch-1", "100", 0, command_id="c-1")
    method, args = session.recover_verification_page(
        "batch-1", "100", 0, expected_delivery_id="d-1", command_id="c-1"
    )
    assert method == session._recover_verification_page
    assert args == ("batch-1", "100", 0, "d-1", "c-1")


def test_the_port_passes_the_identity_to_the_repository():
    seen = []

    def recover(*args, **kwargs):
        seen.append((args, kwargs))
        return {"outcome": "refused", "reason": "state_changed"}

    session = object.__new__(DesktopSession)
    session.runtime = SimpleNamespace(
        repository=SimpleNamespace(recover_verification_page=recover)
    )
    result = session._recover_verification_page("batch-1", "100", 0, "d-1", "c-1")
    assert result["reason"] == "state_changed"
    assert seen == [
        (("batch-1", "100", 0), {"expected_delivery_id": "d-1", "command_id": "c-1"})
    ]
