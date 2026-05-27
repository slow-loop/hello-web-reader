from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from web_reader.readers import apple_podcast


_HAS_LIBRARY = apple_podcast.SQLITE_PATH.exists()


def test_coredata_to_datetime_round_trip():
    # 0 seconds since the Core Data epoch (2001-01-01 UTC)
    assert apple_podcast._coredata_to_datetime(0) == datetime(2001, 1, 1, tzinfo=timezone.utc)
    # 86400 seconds = exactly one day later
    assert apple_podcast._coredata_to_datetime(86400) == datetime(2001, 1, 2, tzinfo=timezone.utc)
    assert apple_podcast._coredata_to_datetime(None) is None


def test_file_url_to_path_decodes_spaces():
    url = "file:///Users/test/Library/Group%20Containers/foo/bar.mp3"
    path = apple_podcast._file_url_to_path(url)
    assert path is not None
    assert str(path) == "/Users/test/Library/Group Containers/foo/bar.mp3"
    assert apple_podcast._file_url_to_path(None) is None
    assert apple_podcast._file_url_to_path("https://x.com/a.mp3") is None


@pytest.mark.skipif(not _HAS_LIBRARY, reason="No local Apple Podcasts library on this machine")
def test_list_episodes_returns_episodes_with_local_mp3():
    episodes = apple_podcast.list_episodes()
    assert isinstance(episodes, list)
    for ep in episodes:
        assert ep.mp3_path.exists(), f"{ep.uuid} mp3 path does not exist"
        assert ep.uuid


def _has_funasr() -> bool:
    try:
        import funasr  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.asyncio
async def test_read_podcast_calls_pipeline_and_forwards_context(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    mp3 = tmp_path / "episode.mp3"
    mp3.write_bytes(b"\x00")

    fake_episode = apple_podcast.Episode(
        uuid="ep-123",
        title="Sample Title",
        podcast_title="Sample Podcast",
        podcast_uuid="pod-xyz",
        published_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        mp3_path=mp3,
        webpage_url="https://example.com/episode",
        author="Host Name",
    )
    monkeypatch.setattr(apple_podcast, "get_episode", lambda uuid: fake_episode)

    def fake_transcribe_audio(audio_path, context=None):
        captured["audio_path"] = audio_path
        captured["context"] = context
        return "  polished transcript  "

    import web_reader.transcribe as transcribe_module
    monkeypatch.setattr(transcribe_module, "transcribe_audio", fake_transcribe_audio)

    result = await apple_podcast.read_podcast("ep-123", context="Mandarin finance")

    assert result.success is True
    assert result.text == "polished transcript"
    assert result.source_type == "apple_podcast"
    assert result.title == "Sample Podcast — Sample Title"
    assert result.raw["method"] == "sensevoice+openrouter"
    assert result.raw["episode_uuid"] == "ep-123"
    assert captured["audio_path"] == mp3
    assert captured["context"] == "Mandarin finance"


@pytest.mark.asyncio
async def test_read_podcast_returns_fail_when_episode_missing(monkeypatch):
    def raise_lookup(uuid):
        raise LookupError("not found")

    monkeypatch.setattr(apple_podcast, "get_episode", raise_lookup)

    result = await apple_podcast.read_podcast("missing-uuid")
    assert result.success is False
    assert result.source_type == "apple_podcast"
    assert "not found" in (result.error or "")


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_LIBRARY, reason="No local Apple Podcasts library on this machine")
@pytest.mark.skipif(not os.environ.get("OPENROUTER_API_KEY"), reason="OPENROUTER_API_KEY not set")
@pytest.mark.skipif(not _has_funasr(), reason="funasr not installed (pip install web-reader[transcribe])")
async def test_read_podcast_round_trip_with_readstore(tmp_path):
    """Transcribe once, persist to ReadStore, re-read from cache without re-running the pipeline."""
    from web_reader.store import ReadStore

    episodes = apple_podcast.list_episodes()
    if not episodes:
        pytest.skip("No locally cached podcast episodes")

    # pick the smallest mp3 so the live call is fast
    target = min(episodes, key=lambda e: e.mp3_path.stat().st_size)
    store = ReadStore(db_path=tmp_path / "cache.sqlite")
    url = f"apple-podcast://{target.uuid}"

    # 1st pass: cache miss, real transcription
    assert store.get_cached(url) is None
    fresh = await apple_podcast.read_podcast(target.uuid)
    assert fresh.success and fresh.text.strip()
    assert fresh.url == url
    store.save(fresh)

    # 2nd pass: cache hit, no model call
    cached = store.get_cached(url)
    assert cached is not None
    assert cached.text == fresh.text
    assert cached.source_type == "apple_podcast"
    assert cached.raw["episode_uuid"] == target.uuid
    assert cached.raw["method"] == "sensevoice+openrouter"
