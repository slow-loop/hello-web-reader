"""
PTT reader — fetch board listings, search results, and threads.

Uses ptt.cc public HTML pages. No login required for public boards.
"""

import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from ..models import ReadResult

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ptt.cc"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Cookie": "over18=1",
}

# One request at a time, with a minimum gap between requests
_semaphore = asyncio.Semaphore(1)
REQUEST_DELAY = 2.0  # seconds between requests


def _parse_listing(html: str) -> tuple[str, list[dict]]:
    """Parse board index/search HTML → plain text + list of post dicts."""
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    lines = []

    for entry in soup.select("div.r-ent"):
        title_tag = entry.select_one("div.title a")
        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)
        href = title_tag["href"]
        url = BASE_URL + href
        push = entry.select_one("div.nrec").get_text(strip=True)
        author = entry.select_one("div.author").get_text(strip=True)
        date = entry.select_one("div.date").get_text(strip=True)

        lines.append(f"[{title}]({url})")
        lines.append(f"push:{push} | {author} | {date}")
        lines.append("---")
        posts.append({"title": title, "url": url, "author": author, "date": date, "push": push})

    return "\n".join(lines), posts


def _parse_thread(html: str) -> tuple[str, dict]:
    """Parse a PTT article thread → plain text + meta."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("div", id="main-content")
    if not main:
        return "", {}

    # Extract header meta
    meta = {}
    for row in main.select("div.article-metaline, div.article-metaline-right"):
        key = row.select_one("span.article-meta-tag")
        val = row.select_one("span.article-meta-value")
        if key and val:
            meta[key.get_text(strip=True)] = val.get_text(strip=True)
        row.decompose()

    # Extract pushes
    pushes = []
    for push in main.select("div.push"):
        tag = push.select_one("span.push-tag")
        userid = push.select_one("span.push-userid")
        content = push.select_one("span.push-content")
        dt = push.select_one("span.push-ipdatetime")
        if tag and userid and content:
            text = content.get_text(strip=True).lstrip(":")
            pushes.append(
                f"{tag.get_text(strip=True)} {userid.get_text(strip=True)}: "
                f"{text.strip()} [{dt.get_text(strip=True) if dt else ''}]"
            )
        push.decompose()

    body_text = re.sub(r"\n{2,}", "\n", main.get_text("\n", strip=True))

    # Compose output
    lines = []
    if meta:
        if "標題" in meta:
            lines.append(f"# {meta['標題']}")
        for k, v in meta.items():
            if k != "標題":
                lines.append(f"{k}: {v}")
        lines.append("")

    lines.append(body_text)

    if pushes:
        lines.append("\n---\n## 推文\n")
        lines.extend(pushes)

    return "\n".join(lines), {"title": meta.get("標題", ""), "author": meta.get("作者", ""), "meta": meta}


async def _fetch(url: str, max_retries: int = 3) -> str:
    async with _semaphore:
        last_err = None
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, headers=HEADERS, timeout=15.0, follow_redirects=True)
                    resp.raise_for_status()
                    text = resp.text
                await asyncio.sleep(REQUEST_DELAY)
                return text
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_err = e
                await asyncio.sleep(2 ** attempt)
        raise last_err  # type: ignore


async def read_ptt(
    url: Optional[str] = None,
    *,
    board: Optional[str] = None,
    query: Optional[str] = None,
    page: int = 1,
) -> ReadResult:
    """
    Read PTT content.

    Usage:
        # Board listing (latest)
        await read_ptt(board="Stock")

        # Search within board
        await read_ptt(board="Stock", query="台積電")

        # Single thread
        await read_ptt("https://www.ptt.cc/bbs/Stock/M.xxx.html")
    """
    if url is None:
        if board is None:
            return ReadResult.fail("ptt://", "Provide url or board", source_type="ptt")  # type: ignore
        if query:
            params = urlencode({"q": query})
            url = f"{BASE_URL}/bbs/{board}/search?{params}"
        else:
            suffix = f"index{page}" if page > 1 else "index"
            url = f"{BASE_URL}/bbs/{board}/{suffix}.html"

    try:
        html = await _fetch(url)
        logger.info(f"PTT fetch: {url}")

        if re.search(r"/M\.\d+\.A\.", url):
            text, post_meta = _parse_thread(html)
            return ReadResult(
                url=url,
                text=text,
                title=post_meta.get("title", ""),
                author=post_meta.get("author", ""),
                source_type="ptt",  # type: ignore
                raw=post_meta,
            )
        else:
            text, posts = _parse_listing(html)
            board_name = board or url.split("/bbs/")[-1].split("/")[0]
            return ReadResult(
                url=url,
                text=text,
                title=f"PTT/{board_name}",
                source_type="ptt",  # type: ignore
                raw=posts,
            )

    except Exception as e:
        logger.error(f"PTT fetch failed: {e}")
        return ReadResult.fail(url, str(e), source_type="ptt")  # type: ignore
