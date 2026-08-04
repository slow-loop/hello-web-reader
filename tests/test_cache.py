"""Tests for ReadCache (SQLite cache)."""

import tempfile
from pathlib import Path

from web_reader.models import ReadResult
from web_reader.cache import ReadCache


def test_save_and_retrieve():
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = ReadCache(db_path=Path(tmpdir) / "test.db")

        result = ReadResult(
            url="https://example.com/test",
            text="Hello world",
            title="Test Page",
            source_type="web",
        )

        record_id = cache.save(result)
        assert record_id

        cached = cache.get_cached("https://example.com/test", source_type="web")
        assert cached is not None
        assert cached.text == "Hello world"
        assert cached.cached is True


def test_cache_miss():
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = ReadCache(db_path=Path(tmpdir) / "test.db")
        cached = cache.get_cached("https://nonexistent.com")
        assert cached is None


def test_ttl_expiry():
    """Test that TTL=0 means everything is expired."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = ReadCache(db_path=Path(tmpdir) / "test.db")

        result = ReadResult(
            url="https://example.com/ttl-test",
            text="Old content",
            source_type="web",
        )
        cache.save(result)

        # TTL=0 should not return cached result
        cached = cache.get_cached("https://example.com/ttl-test", ttl_seconds=0)
        assert cached is None


def test_upsert_rss_entries_and_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = ReadCache(db_path=Path(tmpdir) / "test.db")
        feed_url = "https://example.com/feed"

        first = ReadResult(
            url="https://example.com/posts/1",
            title="First",
            text="First body",
            source_type="rss",
            raw={"entry_id": "https://example.com/posts/1"},
        )
        second = ReadResult(
            url="https://example.com/posts/2",
            title="Second",
            text="Second body",
            source_type="rss",
            raw={"entry_id": "https://example.com/posts/2"},
        )

        cache.upsert_rss_entries(feed_url, [first, second])
        cache.upsert_rss_entries(feed_url, [first])

        results = cache.list_rss_entries(feed_url)
        assert len(results) == 2
        assert all(result.cached is True for result in results)


def test_rss_feed_is_fresh_after_touch():
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = ReadCache(db_path=Path(tmpdir) / "test.db")
        feed_url = "https://example.com/feed"

        assert cache.rss_feed_is_fresh(feed_url, ttl_seconds=900) is False
        cache.touch_rss_feed(feed_url)
        assert cache.rss_feed_is_fresh(feed_url, ttl_seconds=900) is True
