"""RSS reader built around entry-level results."""

import logging
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import unquote

import feedparser
import httpx
from markdownify import markdownify as md

from ..models import ReadResult, RssEntry

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    # Relative dates: -24h, -1d, -7d, -30m
    match = re.match(r"^-(\d+)([hdm])$", normalized)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        delta = {"h": timedelta(hours=amount), "d": timedelta(days=amount), "m": timedelta(minutes=amount)}[unit]
        return datetime.now() - delta
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _parse_entry(raw_entry: Any) -> RssEntry:
    """Convert a feedparser entry to a normalized RSS entry."""
    content_html = (raw_entry.get("content") or [{}])[0].get("value", "")
    summary_html = raw_entry.get("summary", "")

    published = None
    if raw_entry.get("published_parsed"):
        published = datetime(*raw_entry["published_parsed"][:6]).isoformat()
    elif raw_entry.get("updated_parsed"):
        published = datetime(*raw_entry["updated_parsed"][:6]).isoformat()
    elif raw_entry.get("published"):
        published = raw_entry["published"]

    entry_id = raw_entry.get("id") or raw_entry.get("guid") or raw_entry.get("link") or ""

    return RssEntry(
        entry_id=entry_id,
        title=raw_entry.get("title", ""),
        link=raw_entry.get("link", ""),
        author=raw_entry.get("author", ""),
        published=published,
        summary=summary_html,
        content=content_html,
        tags=[tag.get("term", "") for tag in raw_entry.get("tags", [])],
    )


def _entry_to_markdown(entry: RssEntry) -> str:
    body_html = entry.content or entry.summary
    return md(body_html, strip=["img", "figure", "figcaption"]).strip()


def _filter_results(
    results: list[ReadResult],
    *,
    published_after: str | None = None,
    limit: int | None = None,
) -> list[ReadResult]:
    filtered = results

    if published_after:
        cutoff = _parse_datetime(published_after)
        if cutoff is None:
            raise ValueError(
                f"Invalid published_after value: {published_after}. "
                "Use YYYY-MM-DD or ISO datetime."
            )
        filtered = [
            result
            for result in filtered
            if result.published_at is not None and result.published_at >= cutoff
        ]

    if limit is not None:
        if limit < 0:
            raise ValueError(f"Invalid limit value: {limit}. Must be >= 0.")
        filtered = filtered[:limit]

    return filtered


def _results_to_feed_text(results: list[ReadResult]) -> str:
    lines: list[str] = []
    for result in results:
        lines.append(f"Title: {result.title}")
        lines.append(f"Date: {result.published_at.isoformat() if result.published_at else 'N/A'}")
        lines.append(f"Link: {unquote(result.url)}")
        lines.append("-" * 20)
        lines.append(result.text)
        lines.append("=" * 40)
    return "\n".join(lines)


async def _fetch_feed(url: str, timeout: float) -> feedparser.FeedParserDict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml,*/*;q=0.1",
    }
    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
    return feedparser.parse(response.content)


async def fetch_rss_entries(url: str, timeout: float = 15.0) -> list[ReadResult]:
    """Fetch RSS feed and return one result per entry."""
    try:
        feed = await _fetch_feed(url, timeout)
    except httpx.HTTPError as exc:
        return [ReadResult.fail(url, f"HTTP error: {exc}", source_type="rss")]

    if feed.bozo and not feed.entries:
        error_msg = str(feed.bozo_exception) if feed.bozo_exception else "Feed parse error"
        return [ReadResult.fail(url, error_msg, source_type="rss")]

    results: list[ReadResult] = []
    for raw_entry in feed.entries:
        entry = _parse_entry(raw_entry)
        if not entry.entry_id:
            logger.debug("Skipping RSS entry without stable id: %s", entry.title or url)
            continue
        results.append(
            ReadResult(
                url=entry.link,
                text=_entry_to_markdown(entry),
                title=entry.title,
                source_type="rss",
                author=entry.author,
                published_at=_parse_datetime(entry.published),
                raw=entry.model_dump(),
            )
        )
    return results


async def read_rss_entries(
    url: str,
    timeout: float = 15.0,
    published_after: str | None = None,
    limit: int | None = None,
) -> list[ReadResult]:
    results = await fetch_rss_entries(url, timeout=timeout)
    if len(results) == 1 and not results[0].success:
        return results
    return _filter_results(results, published_after=published_after, limit=limit)


async def read_rss(
    url: str,
    timeout: float = 15.0,
    published_after: str | None = None,
    limit: int | None = None,
) -> ReadResult:
    """Read an RSS feed as a single presentation result."""
    try:
        feed = await _fetch_feed(url, timeout)
    except httpx.HTTPError as exc:
        return ReadResult.fail(url, f"HTTP error: {exc}", source_type="rss")

    if feed.bozo and not feed.entries:
        error_msg = str(feed.bozo_exception) if feed.bozo_exception else "Feed parse error"
        return ReadResult.fail(url, error_msg, source_type="rss")

    results: list[ReadResult] = []
    for raw_entry in feed.entries:
        entry = _parse_entry(raw_entry)
        if not entry.entry_id:
            continue
        results.append(
            ReadResult(
                url=entry.link,
                text=_entry_to_markdown(entry),
                title=entry.title,
                source_type="rss",
                author=entry.author,
                published_at=_parse_datetime(entry.published),
                raw=entry.model_dump(),
            )
        )

    results = _filter_results(results, published_after=published_after, limit=limit)
    feed_title = feed.feed.get("title", "") if hasattr(feed, "feed") else ""

    return ReadResult(
        url=url,
        text=_results_to_feed_text(results),
        title=feed_title,
        source_type="rss",
        raw=[result.raw for result in results],
    )
