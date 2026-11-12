"""Exact metadata expansion and portable, collision-resistant output names."""
from dataclasses import replace
import pytest
from core.page_planner import OriginalMetadataError, plan_original_pages
from core.task_contracts import OutputSpec, PathRef, QualitySpec


def test_exact_irregular_original_urls_keep_each_page_extension():
    urls = ("https://i.pximg.net/img-original/2020/a/123_p0.jpg",
            "https://i-cf.pximg.net/img-original/2026/b/123_p1_unique.png",
            "https://i.pximg.net/img-original/c/123_p2_changed.webp")
    pages = plan_original_pages("123", 3, urls, OutputSpec(), artist_id="7")
    assert tuple(p.url for p in pages) == urls
    assert tuple(p.output_ref.relative_path for p in pages) == (
        "downl_Pixiv/7/123_p0.jpg", "downl_Pixiv/7/123_p1.png", "downl_Pixiv/7/123_p2.webp")
    assert [p.page_index for p in pages] == [0,1,2]


@pytest.mark.parametrize("urls,count", [((),1), (("https://i.pximg.net/img-master/123_p0.jpg",),1),
    (("https://i.pximg.net/img-original/124_p0.jpg",),1),
    (("https://i.pximg.net/img-original/123_p1.jpg",),1),
    (("https://i.pximg.net/img-original/123_p0.jpg",),2),
    (("https://attacker.invalid/img-original/123_p0.jpg",),1),
    (("https://i.pximg.net/img-original/123_p0.avif",),1)])
def test_missing_preview_foreign_and_partial_metadata_never_guess(urls, count):
    with pytest.raises(OriginalMetadataError):
        plan_original_pages("123", count, urls, OutputSpec())


def test_first_page_selection_can_later_expand_all_without_changing_p0_identity():
    urls = tuple(f"https://i.pximg.net/img-original/{i}/123_p{i}.png" for i in range(3))
    first = plan_original_pages("123", 3, urls[:1], OutputSpec(), quality=QualitySpec(all_pages=False))
    all_pages = plan_original_pages("123", 3, urls, OutputSpec())
    assert first == all_pages[:1]


def test_remote_title_cannot_escape_and_template_always_preserves_page_identity():
    output = OutputSpec(root_ref=PathRef(relative_path="图片 库"), folder_by_artist=False, name_template="{title}")
    pages = plan_original_pages("123", 2, ("https://i.pximg.net/img-original/123_p0.jpg",
         "https://i.pximg.net/img-original/123_p1.jpg"), output, title='../../CON <>:"/\\|?*')
    assert all(p.output_ref.relative_path.startswith("图片 库/") for p in pages)
    assert len({p.output_ref.target_key for p in pages}) == 2
    assert all(".." not in p.output_ref.relative_path.split("/") for p in pages)
    assert pages[0].output_ref.relative_path.endswith("_123_p0.jpg")


def test_external_root_reference_is_preserved_without_absolute_runtime_paths():
    output = OutputSpec(root_ref=PathRef(kind="external",root_id="archive",relative_path="pixiv"))
    page, = plan_original_pages("123",1,("https://i.pximg.net/img-original/123_p0.png",),output,artist_id="7")
    assert page.output_ref == replace(output.root_ref, relative_path="pixiv/7/123_p0.png")
