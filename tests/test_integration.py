"""Real integration tests for the web-reader package.

These tests use a local HTTP server so the package exercises real network I/O,
parsing, caching, config dispatch, and CLI formatting without relying on mocks.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from web_reader import ReadCache, read_url, read_urls
from web_reader.cli import main as cli_main
from web_reader.readers.json_api import read_json
from web_reader.readers.reddit import read_reddit
from web_reader.readers.rss import read_rss, read_rss_entries
from web_reader.readers.web import read_web
from web_reader.runner import run_config


ARTICLE_HTML = """\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Local Article</title>
    <meta name="author" content="Local Author">
    <meta name="description" content="Local article description">
  </head>
  <body>
    <main>
      <article>
        <h1>Local Article</h1>
        <p>This is a local article used for integration testing.</p>
        <p>It contains enough text for extraction to succeed.</p>
      </article>
    </main>
  </body>
</html>
"""

RSS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Local Feed</title>
    <link>http://localhost/feed</link>
    <description>Integration test RSS feed</description>
    <item>
      <title>First Item</title>
      <link>https://example.com/posts/1</link>
      <description><![CDATA[<p>First item body.</p>]]></description>
      <pubDate>Sat, 01 Mar 2025 12:00:00 GMT</pubDate>
      <author>alice@example.com (Alice)</author>
      <category>tech</category>
    </item>
    <item>
      <title>Second Item</title>
      <link>https://example.com/posts/2</link>
      <description><![CDATA[<p>Second item body.</p>]]></description>
      <pubDate>Sun, 02 Mar 2025 12:00:00 GMT</pubDate>
      <author>bob@example.com (Bob)</author>
      <category>finance</category>
    </item>
  </channel>
</rss>
"""


def _reddit_listing() -> dict:
    return {
        "kind": "Listing",
        "data": {
            "children": [
                {
                    "data": {
                        "id": "abc123",
                        "title": "Markets are moving",
                        "author": "alice",
                        "score": 42,
                        "num_comments": 7,
                        "created_utc": 1740830400,
                        "permalink": "/r/investing/comments/abc123/markets_are_moving/",
                        "selftext": "A quick summary of the market action.",
                        "subreddit": "investing",
                    }
                },
                {
                    "data": {
                        "id": "def456",
                        "title": "Second post",
                        "author": "bob",
                        "score": 5,
                        "num_comments": 2,
                        "created_utc": 1740916800,
                        "permalink": "/r/investing/comments/def456/second_post/",
                        "selftext": "",
                        "subreddit": "investing",
                    }
                },
            ]
        }
    }


def _reddit_thread() -> list[dict]:
    return [
        {
            "data": {
                "children": [
                    {
                        "data": {
                            "id": "abc123",
                            "title": "Markets are moving",
                            "author": "alice",
                            "score": 42,
                            "num_comments": 2,
                            "created_utc": 1740830400,
                            "selftext": "A longer thread body.",
                            "subreddit": "investing",
                        }
                    }
                ]
            }
        },
        {
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {
                            "author": "charlie",
                            "score": 10,
                            "body": "First comment",
                            "replies": {
                                "data": {
                                    "children": [
                                        {
                                            "kind": "t1",
                                            "data": {
                                                "author": "delta",
                                                "score": 3,
                                                "body": "Nested reply",
                                                "replies": "",
                                            },
                                        }
                                    ]
                                }
                            },
                        },
                    }
                ]
            }
        },
    ]


class _Handler(BaseHTTPRequestHandler):
    feed_hits = 0

    def do_GET(self) -> None:
        if self.path == "/article":
            self._send(200, ARTICLE_HTML, "text/html; charset=utf-8")
            return

        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/article")
            self.end_headers()
            return

        if self.path == "/feed":
            type(self).feed_hits += 1
            self._send(200, RSS_XML, "application/rss+xml; charset=utf-8")
            return

        if self.path == "/api/data.json":
            self._send_json(200, {"message": "hello", "items": [1, 2, 3]})
            return

        if self.path == "/r/investing/hot.json":
            self._send_json(200, _reddit_listing())
            return

        if self.path == "/r/investing/comments/abc123/markets_are_moving.json":
            self._send_json(200, _reddit_thread())
            return

        self._send(404, "not found", "text/plain; charset=utf-8")

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _send(self, status: int, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, payload: object) -> None:
        self._send(status, json.dumps(payload), "application/json; charset=utf-8")


@pytest.fixture()
def local_server():
    _Handler.feed_hits = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_read_web_local_server(local_server: str):
    result = await read_web(f"{local_server}/redirect")

    assert result.success is True
    assert result.source_type == "web"
    assert result.title == "Local Article"
    assert "local article used for integration testing" in result.text.lower()
    assert result.author == "Local Author"
    assert result.url == f"{local_server}/article"


@pytest.mark.asyncio
async def test_read_rss_local_server(local_server: str):
    result = await read_rss(f"{local_server}/feed")

    assert result.success is True
    assert result.source_type == "rss"
    assert result.title == "Local Feed"
    assert "Title: First Item" in result.text
    assert "Title: Second Item" in result.text
    assert isinstance(result.raw, list)
    assert len(result.raw) == 2
    assert result.raw[0]["entry_id"] == "https://example.com/posts/1"


@pytest.mark.asyncio
async def test_read_rss_local_server_filters_by_date_and_limit(local_server: str):
    result = await read_rss(
        f"{local_server}/feed",
        published_after="2025-03-02",
        limit=1,
    )

    assert result.success is True
    assert result.source_type == "rss"
    assert "Title: First Item" not in result.text
    assert "Title: Second Item" in result.text
    assert isinstance(result.raw, list)
    assert len(result.raw) == 1
    assert result.raw[0]["title"] == "Second Item"


@pytest.mark.asyncio
async def test_read_rss_entries_local_server(local_server: str):
    results = await read_rss_entries(f"{local_server}/feed")

    assert len(results) == 2
    assert results[0].success is True
    assert results[0].title == "First Item"
    assert "First item body." in results[0].text
    assert results[0].source_type == "rss"


@pytest.mark.asyncio
async def test_read_rss_entries_local_server_filters_by_date_and_limit(local_server: str):
    results = await read_rss_entries(
        f"{local_server}/feed",
        published_after="2025-03-02",
        limit=1,
    )

    assert len(results) == 1
    assert results[0].success is True
    assert results[0].title == "Second Item"
    assert "Second item body." in results[0].text
    assert results[0].source_type == "rss"


@pytest.mark.asyncio
async def test_read_json_local_server(local_server: str):
    result = await read_json(f"{local_server}/api/data.json")

    assert result.success is True
    assert result.source_type == "json"
    assert '"message": "hello"' in result.text
    assert result.raw["items"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_read_reddit_listing_local_server(local_server: str):
    result = await read_reddit(f"{local_server}/r/investing/hot")

    assert result.success is True
    assert result.source_type == "reddit"
    assert result.title == "r/investing"
    assert "Markets are moving" in result.text
    assert "score: 42" in result.text
    assert isinstance(result.raw, list)
    assert result.raw[0]["id"] == "abc123"


@pytest.mark.asyncio
async def test_read_reddit_thread_local_server(local_server: str):
    result = await read_reddit(f"{local_server}/r/investing/comments/abc123/markets_are_moving")

    assert result.success is True
    assert result.source_type == "reddit"
    assert result.title == "Markets are moving"
    assert "## Comments" in result.text
    assert "First comment" in result.text
    assert result.author == "alice"


@pytest.mark.asyncio
async def test_read_url_uses_cache_with_real_cache(local_server: str, tmp_path: Path):
    cache = ReadCache(tmp_path / "cache.db")
    url = f"{local_server}/article"

    first = await read_url(url, cache=cache)
    second = await read_url(url, cache=cache)

    assert first.success is True
    assert first.cached is False
    assert second.success is True
    assert second.cached is True
    assert second.text == first.text


@pytest.mark.asyncio
async def test_read_urls_preserves_order_and_detects_types(local_server: str):
    urls = [
        f"{local_server}/article",
        f"{local_server}/feed",
        f"{local_server}/api/data.json",
    ]

    results = await read_urls(urls, concurrency=2)

    assert [r.source_type for r in results] == ["web", "rss", "json"]
    assert [r.url for r in results] == urls
    assert all(r.success for r in results)


@pytest.mark.asyncio
async def test_run_config_with_tags_and_cache(local_server: str, tmp_path: Path):
    config_path = tmp_path / "feeds.yaml"
    config_path.write_text(
        f"""\
sources:
  - name: local-web
    reader: web
    url: "{local_server}/article"
    tags: [web, smoke]
    cache:
      ttl: 3600
  - name: local-rss
    reader: rss
    url: "{local_server}/feed"
    tags: [rss, smoke]
    cache:
      ttl: 3600
  - name: local-json
    reader: json
    url: "{local_server}/api/data.json"
    tags: [json]
    cache:
      ttl: 3600
  - name: local-reddit
    reader: reddit
    url: "{local_server}/r/investing/hot"
    tags: [reddit]
    cache:
      ttl: 3600
""",
        encoding="utf-8",
    )

    cache = ReadCache(tmp_path / "runner.db")

    first = await run_config(str(config_path), tags=["smoke"], cache=cache)
    second = await run_config(str(config_path), tags=["smoke"], cache=cache)

    assert list(first) == ["local-web", "local-rss"]
    assert all(first[name][0].success for name in first)
    assert len(first["local-rss"]) == 2
    assert {result.title for result in first["local-rss"]} == {"First Item", "Second Item"}
    assert second["local-web"][0].cached is True
    assert len(second["local-rss"]) == 2
    assert all(result.cached is True for result in second["local-rss"])
    assert _Handler.feed_hits == 1


@pytest.mark.asyncio
async def test_run_config_rss_no_cache_stays_uncached_and_respects_delay(
    local_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config_path = tmp_path / "rss-only.yaml"
    config_path.write_text(
        f"""\
sources:
  - name: local-rss
    reader: rss
    url: "{local_server}/feed"
    params:
      delay_sec: 0.25
""",
        encoding="utf-8",
    )

    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr("web_reader.runner.asyncio.sleep", _fake_sleep)

    results = await run_config(str(config_path), no_cache=True, cache=None)

    assert len(results["local-rss"]) == 2
    assert all(result.cached is False for result in results["local-rss"])
    assert sleep_calls == [0.25]


def test_cli_reads_single_url_as_json(local_server: str, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        ["web-reader", "read", f"{local_server}/api/data.json", "--format=json"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_main()
    assert exc_info.value.code == 0

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data[0]["source_type"] == "json"
    assert data[0]["raw"]["message"] == "hello"


def test_cli_reads_config_as_markdown(local_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    config_path = tmp_path / "cli-feeds.yaml"
    config_path.write_text(
        f"""\
sources:
  - name: cli-web
    reader: web
    url: "{local_server}/article"
    tags: [web]
    cache:
      ttl: 3600
  - name: cli-rss
    reader: rss
    url: "{local_server}/feed"
    tags: [rss]
    cache:
      ttl: 3600
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "sys.argv",
        ["web-reader", "config", str(config_path), "--tags=web,rss", "--format=md", "--no-cache"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_main()
    assert exc_info.value.code == 0

    output = capsys.readouterr().out
    assert "## Local Article" in output
    assert "## First Item" in output
    assert "## Second Item" in output
