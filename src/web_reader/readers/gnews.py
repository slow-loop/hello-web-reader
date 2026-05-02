"""
Google News reader — gnews + googlenewsdecoder.

Search Google News by keyword, decode URLs, return ReadResults.
"""

import asyncio
import logging
from typing import Optional

from gnews import GNews
from googlenewsdecoder import gnewsdecoder

from ..models import ReadResult

logger = logging.getLogger(__name__)


def _decode_url(url: str) -> Optional[str]:
    """Decode a Google News proxy URL to the real article URL."""
    try:
        res = gnewsdecoder(url, interval=1)
        if res.get("status"):
            return res.get("decoded_url")
    except Exception as e:
        logger.warning(f"Failed to decode URL {url}: {e}")
    return None


async def read_gnews(
    query: str,
    max_results: int = 10,
    period: str = "7d",
    language: str = "en",
    country: str = "US",
) -> list[ReadResult]:
    """
    Search Google News and return one ReadResult per article.

    Args:
        query: Search keywords (e.g., "NVDA stock").
        max_results: Max items to return.
        period: Time range — "1h", "1d", "7d", "1y", etc.
        language: Language code.
        country: Country code.
    """
    logger.info(f"GNews search: '{query}' (period={period}, max={max_results})")

    google_news = GNews(
        language=language,
        country=country,
        period=period,
        max_results=max_results,
    )

    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, google_news.get_news, query)

    if not results:
        logger.warning(f"No GNews results for: {query}")
        return []

    items: list[ReadResult] = []
    for item in results:
        gnews_url = item.get("url", "")
        title = item.get("title", "")
        source_name = item.get("publisher", {}).get("title", "")
        published_date = item.get("published_date")
        description = item.get("description", "")

        # Decode to real URL
        final_url = await loop.run_in_executor(None, _decode_url, gnews_url) or gnews_url

        # Build text
        lines = []
        if title:
            lines.append(f"# {title}")
        if source_name:
            lines.append(f"Source: {source_name}")
        if published_date:
            lines.append(f"Date: {published_date}")
        if description:
            lines.append("")
            lines.append(description)
        text = "\n".join(lines)

        items.append(
            ReadResult(
                url=final_url,
                text=text,
                title=title,
                source_type="gnews",
                author=source_name or None,
                description=description or None,
                raw={
                    "gnews_url": gnews_url,
                    "publisher": item.get("publisher"),
                    "published_date": published_date,
                },
            )
        )

    logger.info(f"GNews: got {len(items)} results for '{query}'")
    return items
