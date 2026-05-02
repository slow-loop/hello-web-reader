"""
Reddit reader — fetch subreddit listings and threads via .json API.

No API key needed. Uses Reddit's public JSON endpoints.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from html import unescape
from typing import Any, Optional
from urllib.parse import urlencode, urlparse, urlunparse

import httpx

from ..models import ReadResult

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "DNT": "1",
}

# One request at a time, with a minimum gap between requests
_semaphore = asyncio.Semaphore(1)
REQUEST_DELAY = 2.0  # seconds between requests


def _ensure_json_url(url: str) -> str:
    """Ensure Reddit URL ends with .json."""
    parsed = urlparse(url)
    if not parsed.path.endswith(".json"):
        path = parsed.path.rstrip("/") + ".json"
        url = urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment))
    return url


def _build_subreddit_url(
    subreddit: str,
    sort: str = "hot",
    time: Optional[str] = None,
    limit: int = 25,
) -> str:
    """Build a subreddit listing URL."""
    params = {"limit": str(limit)}
    if time:
        params["t"] = time
    return f"https://www.reddit.com/r/{subreddit}/{sort}.json?{urlencode(params)}"


def _build_search_url(
    query: str,
    subreddit: Optional[str] = None,
    sort: Optional[str] = None,
    time: Optional[str] = None,
    limit: int = 25,
) -> str:
    """Build a Reddit search URL."""
    params: dict[str, str] = {"q": query, "limit": str(limit), "type": "link"}
    if sort:
        params["sort"] = sort
    if time:
        params["t"] = time
    if subreddit:
        params["restrict_sr"] = "1"
        return f"https://www.reddit.com/r/{subreddit}/search.json?{urlencode(params)}"
    return f"https://www.reddit.com/search.json?{urlencode(params)}"


def _ts_to_date(ts: Optional[float]) -> Optional[str]:
    if ts:
        try:
            return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            pass
    return None


def _format_listing(data: dict[str, Any], url: str) -> tuple[str, list[dict]]:
    """Format a subreddit/search listing as markdown."""
    children = data.get("data", {}).get("children", [])
    posts = []
    lines = []

    for child in children:
        p = child.get("data", {})
        title = unescape(p.get("title", "No Title"))
        author = p.get("author", "unknown")
        score = p.get("score", 0)
        comments = p.get("num_comments", 0)
        date = _ts_to_date(p.get("created_utc"))
        permalink = p.get("permalink", "")
        selftext = unescape(p.get("selftext", ""))

        meta = f"score: {score} | comments: {comments} | author: {author}"
        if date:
            meta += f" | date: {date}"

        lines.append(f"### {title}")
        lines.append(meta)
        if selftext:
            # Truncate long posts in listing view
            preview = selftext[:500] + ("..." if len(selftext) > 500 else "")
            lines.append(f"\n{preview}")
        lines.append(f"[link](https://www.reddit.com{permalink})")
        lines.append("---")

        posts.append({
            "title": title,
            "author": author,
            "score": score,
            "num_comments": comments,
            "date": date,
            "permalink": permalink,
            "id": p.get("id"),
            "subreddit": p.get("subreddit"),
        })

    text = "\n".join(lines)
    return text, posts


def _format_thread(data: list[dict[str, Any]]) -> tuple[str, Optional[dict]]:
    """Format a thread (post + comments) as markdown."""
    if not isinstance(data, list) or len(data) < 2:
        return "Error: Invalid thread JSON", None

    # Post
    post_data = data[0].get("data", {}).get("children", [{}])[0].get("data", {})
    title = unescape(post_data.get("title", "No Title"))
    author = post_data.get("author", "unknown")
    score = post_data.get("score", 0)
    comments_count = post_data.get("num_comments", 0)
    selftext = unescape(post_data.get("selftext", ""))
    subreddit = post_data.get("subreddit", "")
    date = _ts_to_date(post_data.get("created_utc"))

    lines = [
        f"# {title}",
        f"r/{subreddit} | score: {score} | comments: {comments_count} | author: {author}" + (f" | date: {date}" if date else ""),
    ]
    if selftext:
        lines.append(f"\n{selftext}")
    lines.append("\n---\n## Comments\n")

    # Comments
    comment_listing = data[1].get("data", {}).get("children", [])
    lines.append(_format_comments(comment_listing))

    post_meta = {
        "title": title,
        "author": author,
        "score": score,
        "num_comments": comments_count,
        "subreddit": subreddit,
        "id": post_data.get("id"),
    }

    return "\n".join(lines), post_meta


def _format_comments(comments: list[dict], depth: int = 0) -> str:
    """Recursively format comments."""
    parts = []
    for child in comments:
        if child.get("kind") == "more":
            continue
        c = child.get("data", {})
        author = c.get("author", "unknown")
        score = c.get("score", "?")
        body = c.get("body", "")
        indent = "  " * depth

        parts.append(f"{indent}- **{author}** (score: {score})")
        if body:
            for line in body.split("\n"):
                parts.append(f"{indent}  {line}")
            parts.append("")

        replies = c.get("replies", "")
        if isinstance(replies, dict) and "data" in replies:
            parts.append(_format_comments(replies["data"].get("children", []), depth + 1))

    return "\n".join(parts)


async def _fetch_json(url: str, max_retries: int = 3) -> Any:
    """Fetch JSON with retry on 429/5xx."""
    url = _ensure_json_url(url)
    last_error = None

    async with _semaphore:
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, headers=HEADERS, timeout=30.0, follow_redirects=True)
                    resp.raise_for_status()
                    data = resp.json()
                await asyncio.sleep(REQUEST_DELAY)
                return data
            except httpx.HTTPStatusError as e:
                last_error = e
                if e.response.status_code in (429,) or e.response.status_code >= 500:
                    await asyncio.sleep(2**attempt)
                else:
                    raise
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                await asyncio.sleep(2**attempt)

    raise last_error  # type: ignore


async def read_reddit(
    url: Optional[str] = None,
    *,
    subreddit: Optional[str] = None,
    sort: str = "hot",
    time: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 25,
) -> ReadResult:
    """
    Read Reddit content.

    Usage:
        # Direct URL
        await read_reddit("https://reddit.com/r/investing/hot")

        # By subreddit
        await read_reddit(subreddit="investing", sort="top", time="week")

        # Search
        await read_reddit(subreddit="stocks", query="NVDA earnings")
    """
    # Build URL from params if not provided
    if url is None:
        if query:
            url = _build_search_url(query, subreddit=subreddit, sort=sort, time=time, limit=limit)
        elif subreddit:
            url = _build_subreddit_url(subreddit, sort=sort, time=time, limit=limit)
        else:
            return ReadResult.fail("reddit://", "Provide url, subreddit, or query", source_type="reddit")

    try:
        data = await _fetch_json(url)

        if isinstance(data, list):
            # Thread (post + comments)
            text, post_meta = _format_thread(data)
            return ReadResult(
                url=url,
                text=text,
                title=post_meta.get("title", "") if post_meta else "",
                source_type="reddit",
                author=post_meta.get("author") if post_meta else None,
                raw=post_meta,
            )
        elif isinstance(data, dict) and data.get("kind") == "Listing":
            # Subreddit or search listing
            text, posts = _format_listing(data, url)
            title = f"r/{posts[0]['subreddit']}" if posts and posts[0].get("subreddit") else url
            return ReadResult(
                url=url,
                text=text,
                title=title,
                source_type="reddit",
                raw=posts,
            )
        else:
            return ReadResult.fail(url, f"Unexpected Reddit JSON format", source_type="reddit")

    except Exception as e:
        logger.error(f"Reddit fetch failed: {e}")
        return ReadResult.fail(url, str(e), source_type="reddit")
