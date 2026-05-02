"""
Cnyes (anue.com.tw) reader — fetch news from Cnyes JSON API.

API: https://news.cnyes.com/api/v3/news/category/{cat}?limit={limit}
Categories: tw_stock, wd_stock, headline
"""

import logging
from datetime import datetime, timezone
from html import unescape
from typing import Optional

import httpx
from markdownify import markdownify as md

from ..models import ReadResult

logger = logging.getLogger(__name__)

CNYES_API = "https://news.cnyes.com/api/v3/news/category/{cat}?limit={limit}"
CNYES_URL_BASE = "https://news.cnyes.com/news/id/{news_id}"

CATEGORY_ALIASES = {
    "tw_stock": "tw_stock",
    "tw-stock": "tw_stock",
    "tw": "tw_stock",
    "headline": "headline",
    "headlines": "headline",
    "wd_stock": "wd_stock",
    "global": "wd_stock",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://news.cnyes.com/",
}


def _parse_item(item: dict) -> dict:
    """Extract fields from a Cnyes news item."""
    news_id = item.get("newsId", "")
    published_at = None
    if ts := item.get("publishAt"):
        try:
            published_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            pass

    return {
        "news_id": news_id,
        "title": unescape(item.get("title") or ""),
        "summary": unescape(item.get("summary") or ""),
        "url": CNYES_URL_BASE.format(news_id=news_id) if news_id else "",
        "published": published_at,
        "keywords": item.get("keyword") or [],
        "source": item.get("source") or "",
    }


def _items_to_markdown(items: list[dict]) -> str:
    """Format parsed items as readable markdown."""
    lines = []
    for item in items:
        lines.append(f"**{item['title']}**")
        if item["published"]:
            try:
                dt = datetime.fromisoformat(item["published"]).strftime("%Y-%m-%d %H:%M")
                lines.append(f"Date: {dt}")
            except ValueError:
                lines.append(f"Date: {item['published']}")
        if item["url"]:
            lines.append(f"Link: {item['url']}")
        if item["summary"]:
            lines.append(item["summary"])
        if item["keywords"]:
            lines.append(f"Keywords: {', '.join(item['keywords'])}")
        lines.append("=" * 40)
    return "\n".join(lines)


async def read_cnyes(
    category: str = "tw_stock",
    limit: int = 20,
    timeout: float = 15.0,
) -> ReadResult:
    """
    Fetch Cnyes news for a given category.

    Args:
        category: News category. One of tw_stock, headline, wd_stock.
        limit: Number of articles to fetch.
        timeout: HTTP timeout in seconds.

    Returns:
        ReadResult with markdown-formatted news.
    """
    cat = CATEGORY_ALIASES.get(category, category)
    url = CNYES_API.format(cat=cat, limit=limit)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
            response.raise_for_status()
    except httpx.HTTPError as e:
        return ReadResult.fail(url, f"HTTP error: {e}", source_type="cnyes")

    try:
        data = response.json()
    except Exception as e:
        return ReadResult.fail(url, f"JSON parse error: {e}", source_type="cnyes")

    raw_items = (data.get("items") or {}).get("data") or []
    if not raw_items:
        return ReadResult.fail(url, "No items in response", source_type="cnyes")

    items = [_parse_item(i) for i in raw_items]
    text = _items_to_markdown(items)

    return ReadResult(
        url=url,
        text=text,
        title=f"Cnyes/{category}",
        source_type="cnyes",  # type: ignore
        raw=[i for i in items],
    )
