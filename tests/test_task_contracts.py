"""Behavioral checks for portable, immutable collection requests."""
import dataclasses
import json

import pytest

from core.task_contracts import (
    BatchBudget, CandidateDecision, CollectionRequest, ContractError, FilterSpec,
    OutputSpec, PageSpec, PathRef, QualitySpec, QuerySpec, SourcePage, canonical_json,
)


def test_request_round_trip_and_independent_immutable_filters():
    conditions = {"artist_ids": ["42"]}
    request = CollectionRequest("request-1", "account-main", query=QuerySpec(
        parameters_json=canonical_json(conditions), filters=FilterSpec(100, True)))
    conditions["artist_ids"].append("43")
    assert json.loads(request.query.parameters_json) == {"artist_ids": ["42"]}
    assert CollectionRequest.from_json(request.to_json()) == request
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.query.mode = "new"
    assert request.quality.original and request.quality.all_pages
    assert CollectionRequest("r", "a").query.mode == "history"
    assert CollectionRequest("r", "a").query.filters.min_bookmarks is None
    assert FilterSpec(0, False).min_bookmarks == 0


@pytest.mark.parametrize("payload", [
    '{"schema_version":2}', '{"query":{},"query":{}}', '[]', '{"cookie":"private"}',
    '{"query":{"parameters_json":"{\\"refresh_token\\":\\"private\\"}"}}',
])
def test_bad_serialized_request_is_never_silently_defaulted(payload):
    with pytest.raises(ContractError):
        CollectionRequest.from_json(payload)


@pytest.mark.parametrize("factory", [
    lambda: FilterSpec(True), lambda: FilterSpec(-1), lambda: FilterSpec(exclude_ai=1),
    lambda: BatchBudget(max_works=0), lambda: BatchBudget(max_requests=True),
    lambda: QualitySpec(all_pages=1), lambda: QualitySpec(validation_policy_version=2),
    lambda: QuerySpec(source="unknown"), lambda: QuerySpec(mode="automatic"),
    lambda: QuerySpec(source="ranking"), lambda: QuerySpec(parameters_json='{"x":NaN}'),
    lambda: OutputSpec(name_template="{title.__class__}"),
    lambda: OutputSpec(name_template="../{illust_id}"),
    lambda: OutputSpec(name_template="{illust_id:>200}"),
    lambda: OutputSpec(name_template="{illust_id!r}"),
    lambda: CollectionRequest("r", "a", schema_version=True),
    lambda: CollectionRequest("r", "a", dedup_policy="ignore"),
])
def test_invalid_settings_fail_without_filesystem_access(factory):
    with pytest.raises(ContractError):
        factory()


@pytest.mark.parametrize("path", [
    "../escape.png", "a/../escape.png", "C:/escape.png", "C:escape.png", "/escape.png",
    "//server/share/file.png", "a//file.png", "a/./file.png", "a/file.png:stream",
    "a/NUL.png", "a/lpt1", "a/COM9.jpg", "a/trailing.", "a/trailing ", "a/file?.png", "a\x00b",
])
def test_windows_unsafe_paths_are_refused(path):
    with pytest.raises(ContractError):
        PathRef(relative_path=path)


def test_identity_is_portable_and_windows_case_insensitive():
    a = PathRef(relative_path="downl_Pixiv/Artist/42_p0.PNG")
    b = PathRef(relative_path="downl_Pixiv\\artist\\42_p0.png")
    assert a.target_key == b.target_key
    assert b.relative_path == "downl_Pixiv/artist/42_p0.png"
    assert a.target_key != PathRef(kind="external", root_id="archive", relative_path=a.relative_path).target_key


def test_page_and_source_snapshots_copy_mutable_sequences():
    pages = [PageSpec(0, PathRef(relative_path="downl_Pixiv/42_p0.png"), "https://i.pximg.net/42_p0.png")]
    candidate = CandidateDecision("42", "download", page_count=1, pages=pages)
    pages.clear()
    candidates = [candidate]
    source = SourcePage("follow", "history", "artist-7", "page-1", candidates, cursor_after_json='{"offset":1}')
    candidates.clear()
    assert len(source.candidates[0].pages) == 1
    with pytest.raises(ContractError):
        SourcePage("follow", "history", "a", "p", (candidate, candidate))


@pytest.mark.parametrize("url", ["http://i.pximg.net/a.png", "https://evil.test/a.png", "https://u:p@i.pximg.net/a.png", "https://i.pximg.net/a.png#fragment"])
def test_original_urls_do_not_accept_other_hosts_or_embedded_auth(url):
    with pytest.raises(ContractError):
        PageSpec(0, PathRef(relative_path="downl_Pixiv/a.png"), url)


@pytest.mark.parametrize("value", [{"headers": {"Authorization": "private"}}, {"Cookie": "private"}, {"x": float("inf")}, {1: "x"}])
def test_task_json_refuses_credentials_and_non_json_values(value):
    with pytest.raises(ContractError):
        canonical_json(value)
