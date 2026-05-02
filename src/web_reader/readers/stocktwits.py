"""
Stocktwits reader — fetch symbol streams and trending messages.

Uses the public Stocktwits API (no auth needed for basic reads). Each message
may carry a user-tagged sentiment ("Bullish" / "Bearish"), which we aggregate
into a per-stream bull/bear count — the main value for sentiment analysis.

    https://api.stocktwits.com/api/2/streams/symbol/{SYMBOL}.json
    https://api.stocktwits.com/api/2/streams/trending.json
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from html import unescape
from typing import Any, Optional
from urllib.parse import urlparse

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
}

_semaphore = asyncio.Semaphore(1)
REQUEST_DELAY = 1.5


def _build_symbol_url(symbol: str, limit: int = 30) -> str:
    symbol = symbol.upper().lstrip("$")
    return f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json?limit={limit}"


def _build_trending_url(limit: int = 30) -> str:
    return f"https://api.stocktwits.com/api/2/streams/trending.json?limit={limit}"


def _extract_symbol_from_url(url: str) -> Optional[str]:
    """Parse stocktwits.com/symbol/XXX or api.stocktwits.com/.../symbol/XXX.json."""
    parsed = urlparse(url)
    m = re.search(r"/symbol/([A-Za-z0-9._-]+?)(?:\.json)?/?$", parsed.path)
    if m:
        return m.group(1).upper()
    return None


def _parse_iso(ts: Optional[str]) -> Optional[str]:
    if not ts:
        return None
    try:
        # Stocktwits uses ISO 8601 with trailing Z or offset
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return ts


def _format_stream(data: dict[str, Any], label: str) -> tuple[str, list[dict], dict]:
    """Format a Stocktwits stream as markdown, return (text, messages, summary)."""
    messages = data.get("messages", []) or []
    bull = 0
    bear = 0
    parsed: list[dict] = []
    lines = [f"# {label}", ""]

    for m in messages:
        body = unescape(m.get("body", "") or "")
        user = m.get("user", {}) or {}
        username = user.get("username", "unknown")
        followers = user.get("followers", 0)
        created = _parse_iso(m.get("created_at"))
        entities = m.get("entities", {}) or {}
        sent = (entities.get("sentiment") or {})
        sentiment = sent.get("basic") if isinstance(sent, dict) else None
        if sentiment == "Bullish":
            bull += 1
        elif sentiment == "Bearish":
            bear += 1

        symbols_tagged = [s.get("symbol") for s in (m.get("symbols", []) or []) if s.get("symbol")]

        tag = f" [{sentiment}]" if sentiment else ""
        meta = f"@{username} | followers: {followers}"
        if created:
            meta += f" | {created}"
        if symbols_tagged:
            meta += f" | ${', $'.join(symbols_tagged)}"

        lines.append(f"### {username}{tag}")
        lines.append(meta)
        if body:
            lines.append(f"\n{body}")
        lines.append("---")

        parsed.append({
            "id": m.get("id"),
            "username": username,
            "followers": followers,
            "created_at": m.get("created_at"),
            "body": body,
            "sentiment": sentiment,
            "symbols": symbols_tagged,
        })

    total = bull + bear
    bull_pct = round(100 * bull / total, 1) if total else None
    summary = {
        "message_count": len(messages),
        "bullish": bull,
        "bearish": bear,
        "bullish_pct": bull_pct,
    }

    header = [
        f"**Messages:** {len(messages)}",
        f"**Bullish:** {bull} | **Bearish:** {bear}"
        + (f" | **Bullish %:** {bull_pct}" if bull_pct is not None else ""),
        "",
    ]
    text = "\n".join(header + lines[2:])
    return text, parsed, summary


async def _fetch_json(url: str, max_retries: int = 3) -> Any:
    last_error: Optional[Exception] = None
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
                if e.response.status_code == 429 or e.response.status_code >= 500:
                    await asyncio.sleep(2**attempt)
                else:
                    raise
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                await asyncio.sleep(2**attempt)

    assert last_error is not None
    raise last_error


async def read_stocktwits(
    url: Optional[str] = None,
    *,
    symbol: Optional[str] = None,
    trending: bool = False,
    limit: int = 30,
) -> ReadResult:
    """
    Read a Stocktwits stream.

    Usage:
        # By symbol
        await read_stocktwits(symbol="NVDA")
        await read_stocktwits("https://stocktwits.com/symbol/NVDA")

        # Trending
        await read_stocktwits(trending=True)
    """
    target_symbol: Optional[str] = None

    if url and not symbol and not trending:
        sym_from_url = _extract_symbol_from_url(url)
        if sym_from_url:
            target_symbol = sym_from_url
        elif "trending" in url:
            trending = True
        else:
            return ReadResult.fail(url, "Could not parse symbol from URL", source_type="stocktwits")

    if symbol:
        target_symbol = symbol.upper().lstrip("$")

    if target_symbol:
        fetch_url = _build_symbol_url(target_symbol, limit=limit)
        label = f"Stocktwits — ${target_symbol}"
    elif trending:
        fetch_url = _build_trending_url(limit=limit)
        label = "Stocktwits — Trending"
    else:
        return ReadResult.fail(
            url or "stocktwits://",
            "Provide url, symbol, or trending=True",
            source_type="stocktwits",
        )

    try:
        data = await _fetch_json(fetch_url)
        text, messages, summary = _format_stream(data, label)
        return ReadResult(
            url=fetch_url,
            text=text,
            title=label,
            source_type="stocktwits",
            raw={"summary": summary, "messages": messages},
        )
    except Exception as e:
        logger.error(f"Stocktwits fetch failed: {e}")
        return ReadResult.fail(fetch_url, str(e), source_type="stocktwits")
