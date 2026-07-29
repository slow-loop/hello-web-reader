"""
Live integration tests — requires network.

Run all:     RUN_WEB_READER_LIVE=1 uv run pytest -m live
Run one:     RUN_WEB_READER_LIVE=1 uv run pytest -m live tests/test_live.py::test_ptt_listing
"""

import os

import pytest

RUN_LIVE = os.getenv("RUN_WEB_READER_LIVE") == "1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not RUN_LIVE, reason="set RUN_WEB_READER_LIVE=1 to run"),
]


# --- PTT ---

@pytest.mark.asyncio
async def test_ptt_listing():
    from web_reader.readers.ptt import read_ptt
    result = await read_ptt("https://www.ptt.cc/bbs/Stock/index.html")
    assert result.ok
    assert result.source_type == "ptt"
    assert result.title
    assert len(result.text) > 100


@pytest.mark.asyncio
async def test_ptt_search():
    from web_reader.readers.ptt import read_ptt
    result = await read_ptt("https://www.ptt.cc/bbs/Stock/search?q=台積電")
    assert result.ok
    assert result.source_type == "ptt"
    assert len(result.text) > 50


@pytest.mark.asyncio
async def test_ptt_thread():
    from web_reader.readers.ptt import read_ptt
    result = await read_ptt("https://www.ptt.cc/bbs/Stock/M.1774581562.A.27A.html")
    assert result.ok
    assert result.source_type == "ptt"
    assert result.author
    assert len(result.text) > 100


# --- GNews ---

@pytest.mark.asyncio
async def test_gnews_en():
    from web_reader.readers.gnews import read_gnews
    results = await read_gnews("NVDA stock", period="3d", max_results=3)
    assert len(results) > 0
    for r in results:
        assert r.ok
        assert r.source_type == "gnews"
        assert r.title
        assert r.url.startswith("http")


@pytest.mark.asyncio
async def test_gnews_zh():
    from web_reader.readers.gnews import read_gnews
    results = await read_gnews("台積電", period="3d", max_results=3)
    assert len(results) > 0
    assert results[0].ok


# --- Substack ---

@pytest.mark.asyncio
async def test_substack_listing():
    from web_reader.readers.substack import read_substack
    result = await read_substack("https://thegeneralist.substack.com")
    assert result.ok
    assert result.source_type == "substack"
    assert "recent posts" in result.title.lower()
    assert len(result.text) > 100


@pytest.mark.asyncio
async def test_substack_post():
    from web_reader.readers.substack import read_substack
    # Get the latest post slug first
    listing = await read_substack("https://thegeneralist.substack.com", limit=1)
    assert listing.ok
    assert len(listing.raw) > 0
    slug = listing.raw[0]["slug"]

    # Now read the specific post
    result = await read_substack(publication="thegeneralist", slug=slug)
    assert result.ok
    assert result.source_type == "substack"
    assert result.title
    assert len(result.text) > 200


@pytest.mark.asyncio
async def test_substack_search():
    from web_reader.readers.substack import search_substack
    result = await search_substack("AI agents")
    assert result.ok
    assert result.source_type == "substack"
    assert "search" in result.title.lower()
    assert len(result.text) > 100


@pytest.mark.asyncio
async def test_substack_explore():
    from web_reader.readers.substack import explore_substack
    result = await explore_substack("technology")
    assert result.ok
    assert result.source_type == "substack"
    assert "explore" in result.title.lower()
    assert len(result.text) > 100
