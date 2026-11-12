"""Pure-data archive scope, package affinity, and capacity arithmetic."""
from dataclasses import FrozenInstanceError
import json
import random

import pytest

from core.archive_inventory import (
    ARCHIVE_INVENTORY_SCHEMA_VERSION, ArchiveEntry, ArchiveInventory, ArtworkGroup,
    CAPACITY_PLAN_SCHEMA_VERSION, CapacityPlan, CapacityPolicy, CapacityStatus,
    InventoryIssueCode as Issue, InventoryPolicy, InventoryStatus,
    build_archive_inventory, plan_archive_capacity,
)
from core.preservation_policy import ContentVersion
from core.task_contracts import ContractError, PathRef


GIB = 1024**3


def content(asset_id="asset-1", size=100, marker="a", revision=1):
    return ContentVersion(asset_id, revision, size, None if marker is None else marker * 64)


def entry(path="library/1_p0.png", size=100, asset_id="asset-1", page=None, marker="a"):
    return ArchiveEntry(content(asset_id, size, marker), PathRef(relative_path=path), page)


def group(work_id, *entries):
    return ArtworkGroup(work_id, tuple(entries))


def inventory(*groups, target=100, budget=20 * GIB, batch_id="batch-1"):
    return build_archive_inventory(
        groups, batch_id=batch_id,
        policy=InventoryPolicy(target_package_bytes=target, batch_budget_bytes=budget),
    )


def test_input_and_work_order_do_not_change_identity_packages_or_json():
    first = group("work-2", entry("library/2.png", 30, "asset-2", marker="b"))
    second = group(
        "work-1", entry("library/1_p0.png", 40, "asset-0", page=0),
        entry("library/1_p1.png", 60, "asset-1", page=1, marker="b"),
    )
    groups = [first, second]
    expected = inventory(*groups)
    for seed in range(20):
        shuffled = [
            ArtworkGroup(item.work_id, tuple(random.Random(seed + index).sample(
                item.entries, len(item.entries))))
            for index, item in enumerate(random.Random(seed).sample(groups, len(groups)))
        ]
        actual = inventory(*shuffled)
        assert actual == expected
        assert actual.to_json() == expected.to_json()
    assert expected.to_json() == inventory(second, first).to_json()
    assert [package.sequence for package in expected.packages] == [1, 2]
    assert expected.split_work_ids == ()


def test_exact_duplicate_and_windows_aliases_collapse_to_one_path_identity():
    original = entry("library/Artist/1.PNG", page=0)
    aliases = [
        original,
        ArchiveEntry(original.content, PathRef(relative_path="library\\artist\\1.png"), 0),
        entry("LIBRARY/ARTIST/1.png", page=0),
    ]
    result = inventory(group("work-1", *aliases), group("work-1", original))
    assert result.scope_complete
    assert result.input_entry_count == 4
    assert result.duplicate_entry_count == 3
    assert result.path_count == result.asset_count == 1
    assert result.member_payload_bytes == 100
    assert len(result.packages[0].members) == 1


def test_identical_bytes_at_two_paths_keep_both_members_but_report_unique_content():
    original = content()
    result = inventory(group(
        "work-1",
        ArchiveEntry(original, PathRef(relative_path="library/a.png"), 0),
        ArchiveEntry(original, PathRef(relative_path="library/copy.png"), 0),
    ))
    assert result.path_count == 2
    assert result.unique_content_count == 1
    assert result.member_payload_bytes == 200
    assert result.unique_content_bytes == 100
    assert sum(package.payload_bytes for package in result.packages) == 200
    # No hardlink allocation inference is made: both source members still count.
    plan = plan_archive_capacity(
        result, available_additional_bytes=None,
        policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
    )
    assert plan.source_member_bytes_excluded == 200


@pytest.mark.parametrize("path", [
    "", ".", "../outside.png", "a/../../outside.png", "a//b.png", "a/./b.png",
    "/absolute.png", "C:/absolute.png", "C:relative.png", "//server/share/a.png",
    "a/file.jpg:stream", "a/NUL.png", "a/lpt1.jpg", "a/trailing.", "a/trailing ",
    "a/bad?.png", "a/control\x00.png",
])
def test_invalid_relative_paths_ads_and_empty_names_are_explicitly_rejected(path):
    result = inventory(group("work-1", ArchiveEntry(content(), path)))
    assert not result.scope_complete
    assert result.status is InventoryStatus.NEEDS_ADJUSTMENT
    assert not result.assets and not result.packages
    assert result.member_payload_bytes == 0
    assert [item.code for item in result.issues] == [Issue.INVALID_PATH]


def test_external_directory_root_cannot_be_silently_treated_as_a_file():
    directory = PathRef(kind="external", root_id="library", relative_path=".")
    result = inventory(group("work-1", ArchiveEntry(content(), directory)))
    assert [item.code for item in result.issues] == [Issue.INVALID_PATH]
    assert not result.packages


def test_unknown_content_or_historical_missing_digest_is_not_archivable():
    result = inventory(
        group("work-1", ArchiveEntry(None, "library/unknown.png")),
        group("work-2", entry("library/no-digest.png", marker=None)),
        group("work-3", entry("library/known.png", 999)),
    )
    assert {item.code for item in result.issues} == {
        Issue.UNKNOWN_CONTENT_VERSION, Issue.MISSING_CONTENT_DIGEST,
    }
    assert not result.scope_complete and not result.packages
    assert result.status is InventoryStatus.NEEDS_ADJUSTMENT
    assert result.member_payload_bytes == 0
    plan = plan_archive_capacity(result, available_additional_bytes=10**12)
    assert plan.status is CapacityStatus.NEEDS_ADJUSTMENT
    assert plan.peak_additional_bytes is None and plan.shortfall_bytes is None
    assert not plan.phases


def test_windows_case_or_slash_collision_rejects_all_colliding_records():
    result = inventory(group(
        "work-1",
        entry("library/a.png", asset_id="asset-a"),
        entry("LIBRARY\\A.PNG", asset_id="asset-b", marker="b"),
    ))
    assert not result.assets and not result.packages
    assert len(result.issues) == 2
    assert {item.code for item in result.issues} == {Issue.PATH_COLLISION}


def test_one_asset_revision_cannot_have_conflicting_byte_identity():
    result = inventory(
        group("work-1", entry("library/a.png", asset_id="same", marker="a")),
        group("work-2", entry("library/b.png", asset_id="same", marker="b")),
    )
    assert not result.assets
    assert {item.code for item in result.issues} == {Issue.ASSET_CONFLICT}


def test_artwork_page_cannot_point_to_two_content_versions():
    result = inventory(group(
        "work-1", entry("library/a.png", asset_id="a", page=0),
        entry("library/b.png", asset_id="b", page=0, marker="b"),
    ))
    assert not result.assets
    assert {item.code for item in result.issues} == {Issue.PAGE_CONFLICT}


def test_exact_target_boundary_is_allowed_without_oversize_marker():
    result = inventory(group(
        "work-1", entry("library/a.png", 40, "a"), entry("library/b.png", 60, "b", marker="b"),
    ))
    assert len(result.packages) == 1
    assert result.packages[0].payload_bytes == 100
    assert not result.packages[0].oversized_single_file
    assert not result.packages[0].exception_codes


def test_one_byte_over_target_creates_a_new_package_not_an_inflated_one():
    result = inventory(
        group("work-1", entry("library/a.png", 100, "a")),
        group("work-2", entry("library/b.png", 1, "b")),
    )
    assert [package.payload_bytes for package in result.packages] == [100, 1]
    assert all(not package.oversized_single_file for package in result.packages)


def test_oversized_single_file_is_alone_and_clearly_marked():
    result = inventory(
        group("work-1", entry("library/huge.png", 101, "huge")),
        group("work-2", entry("library/small.png", 100, "small")),
    )
    assert [package.payload_bytes for package in result.packages] == [101, 100]
    assert result.packages[0].oversized_single_file
    assert result.packages[0].exception_codes == ("single_file_over_target",)
    assert len(result.packages[0].members) == 1
    assert result.packages[0].work_ids == ("work-1",)


def test_artwork_affinity_does_not_unintentionally_split_a_fitting_work():
    result = inventory(
        group("work-2", entry("library/2.png", 60, "c", marker="c")),
        group(
            "work-1", entry("library/1_p0.png", 60, "a"),
            entry("library/1_p1.png", 40, "b", page=1, marker="b"),
        ),
    )
    assert [package.work_ids for package in result.packages] == [("work-1",), ("work-2",)]
    assert len(result.packages[0].members) == 2
    assert not result.split_work_ids


def test_large_artwork_is_split_by_whole_file_with_explicit_exception():
    result = inventory(group(
        "work-1", entry("library/a.png", 60, "a"),
        entry("library/b.png", 60, "b", page=1, marker="b"),
    ))
    assert result.split_work_ids == ("work-1",)
    assert [package.payload_bytes for package in result.packages] == [60, 60]
    assert all(package.exception_codes == ("work_split_for_target",) for package in result.packages)
    assert sum(len(package.members) for package in result.packages) == 2


def test_zero_byte_file_is_a_whole_member_and_does_not_create_empty_packages():
    result = inventory(
        group("work-1", entry("library/empty.png", 0, "empty", marker="0")),
        group("work-2", entry("library/full.png", 100, "full")),
    )
    assert len(result.packages) == 1
    assert result.packages[0].payload_bytes == 100
    assert result.member_payload_bytes == 100
    assert result.path_count == 2
    assert result.status is InventoryStatus.READY


def test_empty_scope_and_explicit_empty_work_are_distinguished():
    empty = inventory()
    assert empty.status is InventoryStatus.READY and empty.scope_complete
    assert empty.assets == empty.packages == empty.issues == ()
    plan = plan_archive_capacity(
        empty, available_additional_bytes=0,
        policy=CapacityPolicy(control_bytes=0, safety_margin_bytes=0),
    )
    assert plan.peak_additional_bytes == 0
    assert plan.status is CapacityStatus.READY
    selected_empty = inventory(group("work-without-files"))
    assert not selected_empty.scope_complete
    assert [item.code for item in selected_empty.issues] == [Issue.EMPTY_GROUP]
    assert not selected_empty.packages


def test_batch_budget_overrun_is_explicit_without_changing_package_math():
    result = inventory(group("work-1", entry(size=101)), budget=100)
    assert result.status is InventoryStatus.NEEDS_ADJUSTMENT
    assert result.adjustment_codes == ("batch_budget_exceeded",)
    assert result.packages[0].payload_bytes == 101
    plan = plan_archive_capacity(result, available_additional_bytes=10**12)
    assert plan.status is CapacityStatus.NEEDS_ADJUSTMENT
    assert plan.adjustment_codes == ("batch_budget_exceeded",)


def test_document_57_gib_example_components_and_conservative_peak():
    size = 20 * GIB
    result = inventory(group("work-large", entry("library/large.bin", size, "large")))
    plan = plan_archive_capacity(
        result, available_additional_bytes=plan_archive_capacity(
            result, available_additional_bytes=None,
            policy=CapacityPolicy(control_bytes=0),
        ).peak_additional_bytes,
        policy=CapacityPolicy(control_bytes=0),
    )
    assert result.packages[0].oversized_single_file
    assert plan.status is CapacityStatus.READY
    assert plan.source_member_bytes_excluded == size
    values = {item.name: item.estimated_bytes for item in plan.peak_components}
    assert values == {
        "frozen_staging": 20 * GIB,
        "packages": 20 * GIB,
        "recovery_estimate": 3 * GIB,
        "current_readback": 20 * GIB,
        "current_unpack": 20 * GIB,
        "control": 0,
        "safety_margin": 10 * GIB,
    }
    assert "source" not in values
    assert plan.phases[0].additional_bytes == 53 * GIB
    assert plan.phases[1].additional_bytes == 93 * GIB
    assert plan.phases[2].additional_bytes == 33 * GIB
    assert plan.peak_additional_bytes == 93 * GIB
    # 20+20+3+2+2+10=57 GiB is the two-GiB current-package envelope;
    # this implementation separately preserves the whole oversized member.
    assert 20 * GIB + 20 * GIB + 3 * GIB + 2 * GIB + 2 * GIB + 10 * GIB == 57 * GIB


def test_two_gib_packages_match_document_readback_example_without_large_io():
    groups = [
        group("work-" + str(index), entry(
            "library/" + str(index) + ".bin", GIB, "asset-" + str(index), marker="a",
        ))
        for index in range(20)
    ]
    result = inventory(*groups, target=2 * GIB)
    plan = plan_archive_capacity(
        result, available_additional_bytes=57 * GIB,
        policy=CapacityPolicy(staging_mode="full_batch", control_bytes=0),
    )
    assert len(result.packages) == 10
    assert all(package.payload_bytes == 2 * GIB for package in result.packages)
    assert plan.peak_additional_bytes == 57 * GIB
    assert plan.status is CapacityStatus.READY
    assert plan.source_member_bytes_excluded == 20 * GIB


def test_capacity_exact_free_boundary_unknown_and_insufficient_states():
    result = inventory(group("work-1", entry(size=100)))
    policy = CapacityPolicy(
        staging_mode="single_package", recovery_estimate_basis_points=1500,
        control_bytes=5, safety_margin_bytes=10,
    )
    # staging 100 + package 100 + recovery 15 + readback 100 + unpack 100 + 5 + 10
    exact = plan_archive_capacity(result, available_additional_bytes=430, policy=policy)
    assert exact.peak_additional_bytes == 430
    assert exact.status is CapacityStatus.READY and exact.shortfall_bytes == 0
    insufficient = plan_archive_capacity(result, available_additional_bytes=429, policy=policy)
    assert insufficient.status is CapacityStatus.INSUFFICIENT_SPACE
    assert insufficient.shortfall_bytes == 1
    unknown = plan_archive_capacity(result, available_additional_bytes=None, policy=policy)
    assert unknown.status is CapacityStatus.NEEDS_ADJUSTMENT
    assert unknown.shortfall_bytes is None
    assert unknown.adjustment_codes == ("available_space_unknown",)


def test_recovery_estimate_rounds_up_and_stage_phases_expose_coexistence():
    result = inventory(group("work-1", entry(size=101)), target=100)
    plan = plan_archive_capacity(
        result, available_additional_bytes=1000,
        policy=CapacityPolicy(recovery_estimate_basis_points=1, control_bytes=0, safety_margin_bytes=0),
    )
    values = {item.name: item.estimated_bytes for item in plan.peak_components}
    assert values["recovery_estimate"] == 1
    assert values["frozen_staging"] == values["current_readback"] == values["current_unpack"] == 101
    assert plan.phases[0].additional_bytes == 203
    assert plan.phases[1].additional_bytes == 405
    assert plan.phases[2].additional_bytes == 102


def test_serializable_immutable_versioned_contracts_copy_input_sequences():
    entries = [entry()]
    work = ArtworkGroup("work-1", entries)
    entries.clear()
    result = inventory(work)
    assert len(result.assets) == 1
    assert result.schema_version == ARCHIVE_INVENTORY_SCHEMA_VERSION == 1
    assert json.loads(result.to_json()) == json.loads(json.dumps(result.to_dict()))
    plan = plan_archive_capacity(result, available_additional_bytes=10**6)
    assert plan.schema_version == CAPACITY_PLAN_SCHEMA_VERSION == 1
    assert plan.inventory_schema_version == result.schema_version
    assert json.loads(plan.to_json()) == json.loads(json.dumps(plan.to_dict()))
    with pytest.raises(FrozenInstanceError):
        result.batch_id = "changed"
    with pytest.raises(FrozenInstanceError):
        plan.status = CapacityStatus.READY


@pytest.mark.parametrize("factory", [
    lambda: ArchiveEntry(content=None, path=None),
    lambda: ArchiveEntry(content=None, path="a.png", page_index=True),
    lambda: ArchiveEntry(content=None, path="a.png", page_index=10000),
    lambda: ArtworkGroup("", ()),
    lambda: InventoryPolicy(target_package_bytes=0),
    lambda: InventoryPolicy(batch_budget_bytes=-1),
    lambda: InventoryPolicy(schema_version=True),
    lambda: InventoryPolicy(schema_version=2),
    lambda: CapacityPolicy(staging_mode="unknown"),
    lambda: CapacityPolicy(recovery_estimate_basis_points=-1),
    lambda: CapacityPolicy(schema_version=2),
    lambda: build_archive_inventory([], batch_id=""),
    lambda: build_archive_inventory([], batch_id="bad:batch"),
    lambda: build_archive_inventory([], batch_id=True),
    lambda: build_archive_inventory(["not-a-group"], batch_id="batch"),
    lambda: plan_archive_capacity("not-an-inventory", available_additional_bytes=1),
    lambda: plan_archive_capacity(inventory(), available_additional_bytes=-1),
])
def test_malformed_configuration_or_contract_fails_closed(factory):
    with pytest.raises(ContractError):
        factory()


def test_problematic_scope_and_issue_order_are_stable_across_permutations():
    groups = [
        group("work-2", ArchiveEntry(content("unknown-size"), "../outside.png")),
        group("work-1", entry("library/no-hash.png", marker=None)),
        group("work-1", ArchiveEntry(None, "library/unknown.png")),
        group("work-3"),
        group("work-4", entry("library/a.png", asset_id="a")),
        group("work-4", entry("LIBRARY\\A.PNG", asset_id="b", marker="b")),
    ]
    expected = inventory(*groups)
    for seed in range(20):
        shuffled = random.Random(seed).sample(groups, len(groups))
        actual = inventory(*shuffled)
        assert actual == expected
        assert actual.to_json() == expected.to_json()
    assert not expected.scope_complete
    assert expected.member_payload_bytes == 0 and not expected.packages


def test_same_relative_path_on_distinct_explicit_roots_is_not_a_collision():
    first = content("project-copy")
    second = content("external-copy", marker="b")
    result = inventory(group(
        "work-1",
        ArchiveEntry(first, PathRef(relative_path="library/a.png")),
        ArchiveEntry(second, PathRef(kind="external", root_id="other", relative_path="LIBRARY\\A.PNG")),
    ))
    assert result.scope_complete
    assert result.path_count == 2
    assert len({asset.path_key for asset in result.assets}) == 2
    assert result.member_payload_bytes == 200
