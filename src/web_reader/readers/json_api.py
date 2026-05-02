"""
JSON API reader — simple httpx GET, returns pretty-printed JSON as text.
"""

import json
import logging
from typing import Optional

import httpx

from ..models import ReadResult

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)


async def read_json(
    url: str,
    timeout: float = 30.0,
    headers: Optional[dict] = None,
) -> ReadResult:
    """
    Fetch a JSON API endpoint.

    Returns:
        ReadResult with:
        - text: pretty-printed JSON wrapped in markdown code block
        - raw: parsed JSON object (dict or list)
    """
    default_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "DNT": "1",
    }
    if headers:
        default_headers.update(headers)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers=default_headers,
                timeout=timeout,
                follow_redirects=True,
            )
            response.raise_for_status()

        data = response.json()
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
        text = f"```json\n{pretty}\n```"

        return ReadResult(
            url=str(response.url),
            text=text,
            source_type="json",
            raw=data,
        )

    except httpx.HTTPError as e:
        return ReadResult.fail(url, f"HTTP error: {e}", source_type="json")
    except json.JSONDecodeError as e:
        return ReadResult.fail(url, f"JSON decode error: {e}", source_type="json")
