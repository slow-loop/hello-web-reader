
import pytest
import os
from web_reader.readers.youtube import (
    _channel_lookup_params,
    fetch_channel_info,
    list_channel_videos,
)

RUN_LIVE = os.getenv("RUN_WEB_READER_LIVE") == "1"

LIVE_HANDLE = "@TED"

LIVE_CHANNEL_ID = "UCAuUUnT6oDeKwE6v1NGQxug"


def test_channel_lookup_params():
    assert _channel_lookup_params(LIVE_CHANNEL_ID) == {"id": LIVE_CHANNEL_ID}
    assert _channel_lookup_params("@TED") == {"forHandle": "@TED"}
    assert _channel_lookup_params("TED") == {"forHandle": "@TED"}
    assert _channel_lookup_params("https://www.youtube.com/@TED/videos") == {"forHandle": "@TED"}
    assert _channel_lookup_params(
        f"https://www.youtube.com/channel/{LIVE_CHANNEL_ID}"
    ) == {"id": LIVE_CHANNEL_ID}


@pytest.mark.skipif(not RUN_LIVE, reason="set RUN_WEB_READER_LIVE=1 to run")
@pytest.mark.asyncio
async def test_youtube_channel_info():
    info = await fetch_channel_info(LIVE_HANDLE)
    assert info["id"].startswith("UC")
    assert len(info["id"]) == 24
    assert info["title"]
    assert info["video_count"] > 0


@pytest.mark.skipif(not RUN_LIVE, reason="set RUN_WEB_READER_LIVE=1 to run")
@pytest.mark.asyncio
async def test_youtube_list_videos_via_handle():
    videos = await list_channel_videos(LIVE_HANDLE, limit=2)
    assert len(videos) == 2
    assert videos[0]["title"]
    assert "youtube.com/watch?v=" in videos[0]["url"]


@pytest.mark.skipif(not RUN_LIVE, reason="set RUN_WEB_READER_LIVE=1 to run")
@pytest.mark.asyncio
async def test_youtube_list_videos_date_window():
    videos = await list_channel_videos(LIVE_HANDLE, since="2024-01-01", until="2024-06-30", limit=5)
    assert videos
    assert all("2024-01-01" <= v["published_at"][:10] <= "2024-06-30" for v in videos)
