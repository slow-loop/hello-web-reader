"""
Apple Podcasts reader — local cache only.

Reads metadata from Apple Podcasts SQLite library and transcribes the
locally-cached mp3 via the SenseVoice + OpenRouter pipeline (see
`web_reader.transcribe`).

Mac-only: relies on ~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from pydantic import BaseModel

from ..models import ReadResult

logger = logging.getLogger(__name__)


APPLE_PODCASTS_DIR = Path.home() / "Library/Group Containers/243LU875E5.groups.com.apple.podcasts"
SQLITE_PATH = APPLE_PODCASTS_DIR / "Documents/MTLibrary.sqlite"

# Apple Core Data timestamps are seconds since 2001-01-01 UTC.
_COREDATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


class Episode(BaseModel):
    uuid: str
    title: str
    podcast_title: str
    podcast_uuid: str
    published_at: Optional[datetime] = None
    mp3_path: Path
    webpage_url: Optional[str] = None
    author: Optional[str] = None


def _coredata_to_datetime(ts: float | None) -> datetime | None:
    if ts is None:
        return None
    return _COREDATA_EPOCH + timedelta(seconds=float(ts))


def _file_url_to_path(file_url: str | None) -> Path | None:
    """Convert a file:// URL from ZASSETURL to a local Path."""
    if not file_url or not file_url.startswith("file://"):
        return None
    parsed = urlparse(file_url)
    return Path(unquote(parsed.path))


def list_episodes(podcast_title: str | None = None) -> list[Episode]:
    """List episodes that have a locally-cached mp3.

    Args:
        podcast_title: Exact match against ZMTPODCAST.ZTITLE. None = all podcasts.

    Returns episodes sorted by pubdate descending. Only episodes whose mp3
    actually exists on disk are returned.
    """
    if not SQLITE_PATH.exists():
        raise FileNotFoundError(f"Apple Podcasts library not found at {SQLITE_PATH}")

    query = """
        SELECT
            e.ZUUID, e.ZTITLE, e.ZASSETURL, e.ZPUBDATE,
            e.ZWEBPAGEURL, e.ZAUTHOR,
            p.ZTITLE, p.ZUUID
        FROM ZMTEPISODE e
        LEFT JOIN ZMTPODCAST p ON e.ZPODCASTUUID = p.ZUUID
        WHERE e.ZASSETURL IS NOT NULL
    """
    params: tuple = ()
    if podcast_title is not None:
        query += " AND p.ZTITLE = ?"
        params = (podcast_title,)
    query += " ORDER BY e.ZPUBDATE DESC"

    # read-only URI to avoid lock contention with Podcasts.app
    uri = f"file:{SQLITE_PATH}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    episodes: list[Episode] = []
    for uuid, title, asset_url, pubdate, webpage_url, author, p_title, p_uuid in rows:
        mp3_path = _file_url_to_path(asset_url)
        if mp3_path is None or not mp3_path.exists():
            continue
        episodes.append(Episode(
            uuid=uuid,
            title=title or "",
            podcast_title=p_title or "",
            podcast_uuid=p_uuid or "",
            published_at=_coredata_to_datetime(pubdate),
            mp3_path=mp3_path,
            webpage_url=webpage_url,
            author=author,
        ))
    return episodes


def get_episode(episode_uuid: str) -> Episode:
    """Look up a single episode by UUID. Raises if not found or no local mp3."""
    for ep in list_episodes():
        if ep.uuid == episode_uuid:
            return ep
    raise LookupError(f"Episode {episode_uuid} not found in local cache")


async def read_podcast(episode_uuid: str, vocabulary_terms: list[str] | None = None) -> ReadResult:
    """Transcribe a locally-cached Apple Podcasts episode.

    Stage 1: local SenseVoice ASR (no upload, no size limit).
    Stage 2: OpenRouter LLM polishing (requires OPENROUTER_API_KEY).

    Args:
        episode_uuid: Apple Podcasts episode UUID.
        vocabulary_terms: Optional list of proper nouns / terms ASR often mishears.
    """
    from ..transcribe import transcribe_audio

    url = f"apple-podcast://{episode_uuid}"
    try:
        ep = get_episode(episode_uuid)
    except LookupError as exc:
        return ReadResult.fail(url, str(exc), source_type="apple_podcast")

    text = transcribe_audio(ep.mp3_path, vocabulary_terms=vocabulary_terms).strip()

    return ReadResult(
        url=url,
        text=text,
        title=f"{ep.podcast_title} — {ep.title}" if ep.title else ep.podcast_title,
        source_type="apple_podcast",
        author=ep.author or ep.podcast_title,
        published_at=ep.published_at,
        raw={
            "episode_uuid": ep.uuid,
            "podcast_uuid": ep.podcast_uuid,
            "podcast_title": ep.podcast_title,
            "episode_title": ep.title,
            "webpage_url": ep.webpage_url,
            "mp3_path": str(ep.mp3_path),
            "method": "sensevoice+openrouter",
        },
    )
