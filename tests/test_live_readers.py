"""Opt-in live smoke tests for external readers.

Run with:
    RUN_WEB_READER_LIVE=1 uv run pytest tests/test_live_readers.py
"""

from __future__ import annotations

import os

import pytest

from web_reader import read_url
from web_reader.readers.substack import read_substack
from web_reader.readers.youtube import read_youtube


RUN_LIVE = os.getenv("RUN_WEB_READER_LIVE") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_LIVE,
    reason="set RUN_WEB_READER_LIVE=1 to run external smoke tests",
)

LIVE_SUBSTACK_PUBLICATION = "thegeneralist"
LIVE_YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.mark.asyncio
async def test_substack_live_listing_and_post():
    listing = await read_substack(publication=LIVE_SUBSTACK_PUBLICATION, limit=1)

    assert listing.success is True
    assert listing.source_type == "substack"
    assert isinstance(listing.raw, list)
    assert len(listing.raw) == 1

    first_post = listing.raw[0]
    assert first_post["slug"]

    post = await read_substack(
        publication=LIVE_SUBSTACK_PUBLICATION,
        slug=first_post["slug"],
    )

    assert post.success is True
    assert post.source_type == "substack"
    assert post.title
    assert post.url.endswith(f"/p/{first_post['slug']}")
    assert len(post.text.strip()) > 100


@pytest.mark.asyncio
async def test_youtube_live_transcript():
    result = await read_youtube(LIVE_YOUTUBE_URL)

    assert result.success is True
    assert result.source_type == "youtube"
    assert result.raw["video_id"] == "dQw4w9WgXcQ"
    assert result.language
    assert "TRANSCRIPT:" in result.text
    assert len(result.text) > 500


@pytest.mark.asyncio
async def test_read_url_auto_detects_live_substack_and_youtube():
    listing = await read_url(f"https://{LIVE_SUBSTACK_PUBLICATION}.substack.com")
    video = await read_url(LIVE_YOUTUBE_URL)

    assert listing.success is True
    assert listing.source_type == "substack"
    assert video.success is True
    assert video.source_type == "youtube"
