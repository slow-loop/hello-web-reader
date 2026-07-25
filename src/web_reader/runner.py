"""
Feed runner — executes a FeedConfig, dispatching to the right readers.
"""

import asyncio
import logging
from typing import Optional

from .config import FeedConfig, SourceConfig, load_config
from .models import ReadResult
from .store import ReadStore

logger = logging.getLogger(__name__)

RSS_CACHE_TTL_SECONDS = 900


def _filter_rss_results(
    results: list[ReadResult],
    *,
    published_after: str | None = None,
    limit: int | None = None,
) -> list[ReadResult]:
    from .readers.rss import _parse_datetime

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


async def _run_rss_source(
    source: SourceConfig,
    store: Optional[ReadStore],
    no_cache: bool,
) -> list[ReadResult]:
    from .readers.rss import fetch_rss_entries

    url = source.url
    params = source.params
    timeout = params.get("timeout", 15.0)
    published_after = params.get("published_after")
    limit = params.get("limit")
    delay_sec = float(params.get("delay_sec", 0.0))
    cache_ttl = source.cache.ttl if source.cache.ttl is not None else RSS_CACHE_TTL_SECONDS

    if store and not no_cache and url and store.rss_feed_is_fresh(url, cache_ttl):
        cached = store.list_rss_entries(
            url,
            published_after=published_after,
            limit=limit,
        )
        if cached:
            logger.info(f"[{source.name}] cache hit")
            return cached

    results = await fetch_rss_entries(url, timeout=timeout)
    if len(results) == 1 and not results[0].success:
        return results

    if delay_sec > 0:
        await asyncio.sleep(delay_sec)

    if store and url:
        store.upsert_rss_entries(url, results)
        store.touch_rss_feed(url)
        return store.list_rss_entries(
            url,
            published_after=published_after,
            limit=limit,
        )

    return _filter_rss_results(
        results,
        published_after=published_after,
        limit=limit,
    )


async def _run_source(source: SourceConfig, store: Optional[ReadStore], no_cache: bool) -> list[ReadResult]:
    """Run a single source config and return results."""
    reader = source.reader.lower()
    url = source.url
    params = source.params

    if reader == "rss":
        return await _run_rss_source(source, store, no_cache)

    # Check cache (unless no_cache)
    if store and not no_cache and url:
        cache_ttl = source.cache.ttl if source.cache.ttl is not None else 3600
        cached = store.get_cached(url, ttl_seconds=cache_ttl, source_type=reader)
        if cached:
            logger.info(f"[{source.name}] cache hit")
            return [cached]

    try:
        results = await _dispatch(reader, url, params)
    except Exception as e:
        logger.error(f"[{source.name}] failed: {e}")
        results = [ReadResult.fail(url or f"{reader}://", str(e), source_type=reader)]

    # Save to cache
    if store:
        for r in results:
            if r.success:
                store.save(r)

    return results


async def _dispatch(reader: str, url: Optional[str], params: dict) -> list[ReadResult]:
    """Dispatch to the appropriate reader."""
    if reader == "web":
        from .readers.web import read_web
        result = await read_web(url, **params)
        return [result]

    elif reader == "rss":
        from .readers.rss import read_rss
        result = await read_rss(url, **params)
        return [result]

    elif reader == "reddit":
        from .readers.reddit import read_reddit
        result = await read_reddit(url, **params)
        return [result]

    elif reader == "stocktwits":
        from .readers.stocktwits import read_stocktwits
        result = await read_stocktwits(url, **params)
        return [result]

    elif reader == "substack":
        from .readers.substack import read_substack
        result = await read_substack(url, **params)
        return [result]

    elif reader == "substack_search":
        from .readers.substack import search_substack
        result = await search_substack(**params)
        return [result]

    elif reader == "substack_explore":
        from .readers.substack import explore_substack
        result = await explore_substack(**params)
        return [result]

    elif reader == "youtube":
        from .readers.youtube import read_youtube
        result = await read_youtube(url, **params)
        return [result]

    elif reader == "youtube_channel":
        from .readers.youtube import list_channel_videos
        channel = params.get("handle") or params.get("channel_id") or url
        if not channel:
            raise ValueError("youtube_channel requires 'handle', 'channel_id', or 'url' param")
        videos = await list_channel_videos(channel_id_or_handle=channel, limit=params.get("limit", 5))
        results = []
        for v in videos:
            results.append(
                ReadResult(
                    url=v["url"],
                    title=v["title"],
                    text=f"Title: {v['title']}\nPublished: {v['published_at']}\nURL: {v['url']}",
                    source_type="youtube_channel",
                    language="zh",
                    published_at=v["published_at"],
                    raw=v
                )
            )
        return results

    elif reader in ("json", "json_api"):
        from .readers.json_api import read_json
        result = await read_json(url, **params)
        return [result]

    elif reader == "email":
        from .readers.email import read_email
        from .models import EmailRequest
        req = EmailRequest(**params)
        return await read_email(req)

    elif reader == "gnews":
        from .readers.gnews import read_gnews
        results = await read_gnews(**params)
        return results

    elif reader == "ptt":
        from .readers.ptt import read_ptt
        result = await read_ptt(url, **params)
        return [result]

    elif reader == "cnyes":
        from .readers.cnyes import read_cnyes
        result = await read_cnyes(**params)
        return [result]

    elif reader == "apple_podcast":
        from .readers.apple_podcast import list_episodes, read_podcast
        podcast_title = params.get("podcast_title")
        limit = params.get("limit")
        episode_uuids = params.get("episode_uuids")
        vocabulary_terms = params.get("vocabulary_terms") or None

        if episode_uuids:
            uuids = episode_uuids
        else:
            episodes = list_episodes(podcast_title=podcast_title)
            if limit:
                episodes = episodes[:int(limit)]
            uuids = [ep.uuid for ep in episodes]

        results = []
        for uuid in uuids:
            results.append(await read_podcast(uuid, vocabulary_terms=vocabulary_terms))
        return results

    elif reader == "rss_podcast":
        from .readers.rss_podcast import list_episodes, read_episode
        limit = params.get("limit")
        episodes = await list_episodes(url)
        if limit:
            episodes = episodes[:int(limit)]
        return [await read_episode(ep) for ep in episodes]

    else:
        return [ReadResult.fail(url or f"{reader}://", f"Unknown reader: {reader}", source_type="unknown")]


async def run_config(
    config_path: str,
    *,
    tags: Optional[list[str]] = None,
    no_cache: bool = False,
    store: Optional[ReadStore] = None,
) -> dict[str, list[ReadResult]]:
    """
    Run a feeds.yaml config and return results grouped by source name.

    Args:
        config_path: Path to the YAML config file.
        tags: If set, only run sources that have at least one matching tag.
        no_cache: Skip cache for all sources.
        store: ReadStore for caching. Created automatically if None.
    """
    config = load_config(config_path)

    if store is None and not no_cache:
        store = ReadStore()

    sources = config.sources
    if tags:
        sources = [s for s in sources if any(t in s.tags for t in tags)]
        if not sources:
            raise ValueError(f"No sources matched tags: {tags}")

    results: dict[str, list[ReadResult]] = {}
    for source in sources:
        logger.info(f"[{source.name}] fetching ({source.reader})...")
        results[source.name] = await _run_source(source, store, no_cache)

    return results
