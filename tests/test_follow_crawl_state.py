"""Durability contracts for the isolated followed-artist crawl state."""

from __future__ import annotations

from pathlib import Path

from core.follow_crawl_state import (
    FollowArtistSnapshot,
    FollowCrawlStateRepository,
)


def test_run_and_artist_checkpoint_round_trip(tmp_path: Path) -> None:
    repository = FollowCrawlStateRepository(tmp_path / "follow_crawl_state.sqlite3")
    run = repository.create_run(
        account_id="42",
        config_key="cfg",
        artists=[
            FollowArtistSnapshot("100", "A"),
            FollowArtistSnapshot("200", "B"),
        ],
        start_mode="index",
        start_value="2",
        start_ordinal=2,
    )

    repository.mark_artist_running(run.run_id, "200")
    repository.checkpoint_artist(
        run.run_id,
        "200",
        cursor={"user_id": "200", "offset": 30},
        item_index=4,
        last_illust_id="999",
        works_seen=34,
        queued_items=3,
    )
    repository.mark_run(run.run_id, "paused", "request budget")

    resumed = repository.find_resumable_run("42", "cfg")
    assert resumed is not None
    assert resumed.run_id == run.run_id
    assert resumed.start_ordinal == 2
    artist = repository.list_artists(run.run_id, start_ordinal=2)[0]
    assert artist.cursor == {"user_id": "200", "offset": 30}
    assert artist.item_index == 4
    assert artist.last_illust_id == "999"
    assert artist.works_seen == 34
    assert artist.queued_items == 3
    assert repository.integrity_check() == "ok"
    repository.close()


def test_completed_run_is_not_selected_for_resume(tmp_path: Path) -> None:
    repository = FollowCrawlStateRepository(tmp_path / "state.sqlite3")
    run = repository.create_run(
        account_id="42",
        config_key="cfg",
        artists=[FollowArtistSnapshot("100", "A")],
        start_mode="restart",
    )
    repository.mark_artist_completed(run.run_id, "100", works_seen=1, queued_items=1)
    repository.mark_run(run.run_id, "completed")

    assert repository.find_resumable_run("42", "cfg") is None
    repository.close()

