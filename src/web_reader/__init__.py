"""
web-reader — Lightweight async web reader.

URL in, clean text out.

Usage:
    # Python API
    from web_reader import read_url, read_urls
    result = await read_url("https://example.com/article")
    if result.ok:
        print(result.text)

    # With caching (default: user-level shared cache, e.g. ~/Library/Caches/web-reader/)
    from web_reader import read_url, ReadCache
    store = ReadCache()
    result = await read_url("https://example.com", store=store)

    # CLI
    uv run web-reader fetch "https://example.com"
    uv run web-reader fetch feeds.yaml --only=reddit --format=md
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from .models import ReadResult, EmailRequest, RssEntry, SourceType
from .cache import ReadCache, ReadRecord
from ._detect import detect_source_type
from .config import FeedConfig, SourceConfig, CacheConfig, load_config

logger = logging.getLogger(__name__)


def _load_workspace_env() -> None:
    cwd = Path.cwd()
    candidates = [
        cwd / ".env",
        cwd.parent / ".env",
    ]

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.exists():
            continue
        load_dotenv(resolved, override=False)
        seen.add(resolved)


_load_workspace_env()

__all__ = [
    # Core API
    "read_url",
    "read_urls",
    # Models
    "ReadResult",
    "ReadCache",
    "ReadRecord",
    "EmailRequest",
    "RssEntry",
    "SourceType",
    # Config
    "FeedConfig",
    "SourceConfig",
    "CacheConfig",
    "load_config",
    # Detection
    "detect_source_type",
    # Individual readers
    "read_web",
    "read_rss",
    "read_rss_entries",
    "read_json",
    "read_youtube",
    "read_email",
    "read_reddit",
    "read_substack",
    "read_gnews",
    "read_ptt",
    "read_cnyes",
    # Integrations
    "WebReaderKnowledge",
]

# --- Lazy imports for individual readers ---


def read_web(*args, **kwargs):
    from .readers.web import read_web as _read_web
    return _read_web(*args, **kwargs)


def read_rss(*args, **kwargs):
    from .readers.rss import read_rss as _read_rss
    return _read_rss(*args, **kwargs)


def read_rss_entries(*args, **kwargs):
    from .readers.rss import read_rss_entries as _read_rss_entries
    return _read_rss_entries(*args, **kwargs)


def read_json(*args, **kwargs):
    from .readers.json_api import read_json as _read_json
    return _read_json(*args, **kwargs)


def read_youtube(*args, **kwargs):
    from .readers.youtube import read_youtube as _read_youtube
    return _read_youtube(*args, **kwargs)


def read_email(*args, **kwargs):
    from .readers.email import read_email as _read_email
    return _read_email(*args, **kwargs)


def read_reddit(*args, **kwargs):
    from .readers.reddit import read_reddit as _read_reddit
    return _read_reddit(*args, **kwargs)


def read_substack(*args, **kwargs):
    from .readers.substack import read_substack as _read_substack
    return _read_substack(*args, **kwargs)


def read_gnews(*args, **kwargs):
    from .readers.gnews import read_gnews as _read_gnews
    return _read_gnews(*args, **kwargs)


def read_ptt(*args, **kwargs):
    from .readers.ptt import read_ptt as _read_ptt
    return _read_ptt(*args, **kwargs)


def read_cnyes(*args, **kwargs):
    from .readers.cnyes import read_cnyes as _read_cnyes
    return _read_cnyes(*args, **kwargs)


def WebReaderKnowledge(*args, **kwargs):
    from .agno_reader import WebReaderKnowledge as _WebReaderKnowledge
    return _WebReaderKnowledge(*args, **kwargs)


# --- Main API ---


async def read_url(
    url: str,
    store: Optional[ReadCache] = None,
    cache_ttl: Optional[int] = 3600,
    **kwargs,
) -> ReadResult:
    """
    Read a URL, auto-detecting the appropriate reader.

    Args:
        url: The URL to read.
        store: Optional ReadCache for caching. Pass None to skip caching.
        cache_ttl: Cache TTL in seconds. Default 1 hour. None = no TTL check.
        **kwargs: Passed through to the underlying reader.

    Returns:
        ReadResult with extracted text.
    """
    source_type = detect_source_type(url)

    # Check cache
    if store and source_type != "rss":
        cached = store.get_cached(url, ttl_seconds=cache_ttl, source_type=source_type)
        if cached:
            return cached
        # Fallback: URL may have been pulled as part of an RSS feed
        rss_entry = store.get_rss_entry(url)
        if rss_entry:
            return rss_entry

    # Dispatch to reader
    if source_type == "youtube":
        from .readers.youtube import read_youtube as _yt
        result = await _yt(url, **kwargs)
    elif source_type == "reddit":
        from .readers.reddit import read_reddit as _reddit
        result = await _reddit(url, **kwargs)
    elif source_type == "substack":
        from .readers.substack import read_substack as _substack
        result = await _substack(url, **kwargs)
    elif source_type == "rss":
        from .readers.rss import read_rss as _rss
        result = await _rss(url, **kwargs)
    elif source_type == "json":
        from .readers.json_api import read_json as _json
        result = await _json(url, **kwargs)
    elif source_type == "email":
        return ReadResult.fail(url, "Use read_email() with EmailRequest for email reading", source_type="email")
    elif source_type == "gnews":
        from .readers.gnews import read_gnews as _gnews
        from urllib.parse import urlparse, parse_qs
        
        parsed = urlparse(url)
        query = parsed.netloc or parsed.path.lstrip('/')
        params = parse_qs(parsed.query)
        
        search_args = {
            "query": query,
            "period": params.get('period', ['7d'])[0],
            "max_results": int(params.get('max', [10])[0])
        }
        search_args.update(kwargs)
        search_results = await _gnews(**search_args)
        if not search_results:
             return ReadResult.fail(url, f"No news found for {query}", source_type="gnews")
        
        # Merge individual news items into one summary report
        combined_text = "\n\n---\n\n".join([r.text + f"\nLink: {r.url}" for r in search_results])
        result = ReadResult(
            url=url,
            text=combined_text,
            title=f"Google News: {query}",
            source_type="gnews",
            success=True,
            raw={"count": len(search_results), "items": [r.model_dump() for r in search_results]}
        )
    else:
        from .readers.web import read_web as _web
        result = await _web(url, **kwargs)

    # Save to cache
    if store and result.success and source_type != "rss":
        store.save(result)

    return result


async def read_urls(
    urls: list[str],
    store: Optional[ReadCache] = None,
    cache_ttl: Optional[int] = 3600,
    concurrency: int = 5,
    **kwargs,
) -> list[ReadResult]:
    """
    Read multiple URLs concurrently.

    Args:
        urls: List of URLs to read.
        store: Optional ReadCache for caching.
        cache_ttl: Cache TTL in seconds.
        concurrency: Max concurrent requests. Default 5.
        **kwargs: Passed through to the underlying readers.

    Returns:
        List of ReadResults in the same order as input URLs.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def _read_one(url: str) -> ReadResult:
        async with semaphore:
            return await read_url(url, store=store, cache_ttl=cache_ttl, **kwargs)

    return await asyncio.gather(*[_read_one(url) for url in urls])
