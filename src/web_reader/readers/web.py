"""
Web reader — httpx + trafilatura.

No browser needed. Trafilatura handles article extraction,
boilerplate removal, and markdown conversion.
"""

import logging
from typing import Optional

import httpx
import trafilatura

from ..models import ReadResult

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7",
    "DNT": "1",
}


async def _read_jina(
    url: str,
    timeout: float = 30.0,
    include_links: bool = False,
    include_images: bool = False,
    headers: Optional[dict] = None,
) -> Optional[ReadResult]:
    import os

    async with httpx.AsyncClient() as client:
        jina_headers = {
            "Accept": "application/json",
            "X-Return-Format": "markdown",
        }
        if include_links:
            jina_headers["X-With-Links-Summary"] = "true"
        if include_images:
            jina_headers["X-With-Images-Summary"] = "true"

        jina_api_key = os.getenv("JINA_API_KEY")
        if jina_api_key:
            jina_headers["Authorization"] = f"Bearer {jina_api_key}"

        if headers:
            jina_headers.update(headers)
            
        jina_response = await client.get(
            f"https://r.jina.ai/{url}",
            headers=jina_headers,
            timeout=timeout,
            follow_redirects=True,
        )
        jina_response.raise_for_status()
        jina_data = jina_response.json()
        
        jina_data = jina_data.get("data", {})
        text = jina_data.get("content", "")
        title = jina_data.get("title", "")
        
        if text:
            return ReadResult(
                url=jina_data.get("url", url),
                text=text,
                title=title,
                source_type="web",
                raw={"source": "jina", "html_length": len(text)}
            )
    
    return None


async def _read_trafilatura(
    url: str,
    timeout: float = 30.0,
    include_links: bool = False,
    include_images: bool = False,
    output_format: str = "markdown",
    headers: Optional[dict] = None,
) -> ReadResult:
    """Fallback: Fetch web page and extract clean text via trafilatura."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers=headers or DEFAULT_HEADERS,
                timeout=timeout,
                follow_redirects=True,
            )
            response.raise_for_status()
            html = response.text
            final_url = str(response.url)

    except httpx.HTTPError as e:
        return ReadResult.fail(url, f"HTTP error: {e}", source_type="web")

    try:
        text = trafilatura.extract(
            html,
            url=url,
            include_links=include_links,
            include_images=include_images,
            output_format=output_format,
        )
    except Exception as e:
        return ReadResult.fail(url, f"Extraction error: {e}", source_type="web")

    if not text:
        return ReadResult.fail(url, "No content extracted", source_type="web")

    metadata = trafilatura.extract_metadata(html, default_url=url)
    title = ""
    author = None
    description = None
    published_at = None

    if metadata:
        title = metadata.title or ""
        author = metadata.author
        description = metadata.description
        if metadata.date:
            try:
                from datetime import datetime

                published_at = datetime.fromisoformat(metadata.date)
            except (ValueError, TypeError):
                pass

    return ReadResult(
        url=final_url,
        text=text,
        title=title,
        source_type="web",
        author=author,
        description=description,
        published_at=published_at,
        raw={"html_length": len(html), "final_url": final_url},
    )


async def read_web(
    url: str,
    timeout: float = 30.0,
    include_links: bool = False,
    include_images: bool = False,
    output_format: str = "markdown",
    headers: Optional[dict] = None,
) -> ReadResult:
    """
    Fetch a web page and extract clean text. Attempts Jina API first, then falls back to trafilatura.

    Args:
        url: The URL to read.
        timeout: HTTP timeout in seconds.
        include_links: Whether to preserve links in output.
        include_images: Whether to preserve image refs in output.
        output_format: "markdown", "text", or "xml".
        headers: Optional custom HTTP headers.
    """
    # Step 1: Attempt Jina first
    try:
        jina_result = await _read_jina(
            url,
            timeout,
            include_links=include_links,
            include_images=include_images,
            headers=headers,
        )
        if jina_result:
            return jina_result
    except Exception as e:
        logger.debug(f"Jina fallback triggered for {url}. Reason: {e}")

    # Step 2: Fallback to local httpx + Trafilatura extraction
    return await _read_trafilatura(
        url=url,
        timeout=timeout,
        include_links=include_links,
        include_images=include_images,
        output_format=output_format,
        headers=headers,
    )
