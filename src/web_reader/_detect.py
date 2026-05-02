"""
URL auto-detection — figure out which reader to use from the URL.
"""

from urllib.parse import urlparse

from .models import SourceType


def detect_source_type(url: str) -> SourceType:
    """Auto-detect the appropriate reader from a URL."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    # Email protocol
    if url.startswith("email://"):
        return "email"

    # GNews pseudo-protocol
    if url.startswith("gnews://"):
        return "gnews"

    # Substack pseudo-protocols
    if url.startswith("substack://"):
        return "substack"

    # YouTube
    if any(h in host for h in ("youtube.com", "youtu.be")):
        return "youtube"

    # PTT
    if "ptt.cc" in host:
        return "ptt"

    # Reddit
    if "reddit.com" in host or "redd.it" in host:
        return "reddit"

    # Stocktwits
    if "stocktwits.com" in host:
        return "stocktwits"

    # Substack
    if ".substack.com" in host:
        return "substack"

    # Known RSS domains
    if any(h in host for h in ("hnrss.org", "rsshub.app", "rss.nytimes.com")):
        return "rss"

    # RSS feed patterns
    if any(
        pattern in path
        for pattern in ("/feed", "/rss", "/atom", ".xml", ".rss")
    ):
        return "rss"

    # JSON API patterns
    if path.endswith(".json") or "/api/" in path:
        return "json"

    # Default: web page
    return "web"
