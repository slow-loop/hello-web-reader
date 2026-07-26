"""
Podcast RSS reader — fetch a podcast feed, download the episode audio, and
transcribe it with the local ASR pipeline.

Unlike the `rss` reader (which only reads the text body and ignores the audio
`<enclosure>`), this reader is audio-first: it parses the feed for enclosures,
downloads the mp3, and runs raw SenseVoice ASR. It deliberately skips the
OpenCC + LLM refinement stages of `transcribe.transcribe_audio` — this reader's
job is to fetch the raw transcript; script normalization and polishing are left
to whatever consumes the text downstream.

Dedup identity is the feed's `<guid>` (feedparser `entry.id`), wrapped as
`podcast://<guid>`. The guid is published by the podcast host and is stable,
whereas the enclosure mp3 URL often carries a rotating signed token.

Host-agnostic: works with any standards-compliant podcast feed (SoundOn,
Firstory, SoundCloud, …) since feedparser normalizes enclosures and guids.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import feedparser
import httpx
from platformdirs import user_cache_dir
from pydantic import BaseModel

from ..models import ReadResult

logger = logging.getLogger(__name__)

DEFAULT_AUDIO_DIR_ENV = "WEB_READER_AUDIO_DIR"

# Sentinel: default keeps audio in the resolved cache dir; pass audio_dir=None
# to discard instead, or an explicit path to keep it elsewhere.
_KEEP_DEFAULT = object()


def resolve_default_audio_dir() -> Path:
    """Where episode audio is kept when the caller doesn't say.

      macOS:   ~/Library/Caches/web-reader/audio
      Linux:   ~/.cache/web-reader/audio   (XDG)

    Override with the WEB_READER_AUDIO_DIR env var, or the read_episode
    audio_dir argument. Consistent with ReadStore's WEB_READER_DB_PATH.
    """
    configured = os.environ.get(DEFAULT_AUDIO_DIR_ENV)
    if configured:
        return Path(configured).resolve()
    return (Path(user_cache_dir("web-reader")) / "audio").resolve()

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

DOWNLOAD_ATTEMPTS = 3


class Episode(BaseModel):
    guid: str
    title: str
    podcast_title: str
    audio_url: str
    published_at: Optional[datetime] = None
    webpage_url: Optional[str] = None
    author: Optional[str] = None
    duration_seconds: Optional[float] = None


def _published_at(raw_entry) -> datetime | None:
    """feedparser gives a UTC struct_time; return tz-aware UTC to match
    apple_podcast (so downstream date comparisons line up)."""
    parsed = raw_entry.get("published_parsed") or raw_entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def _duration_seconds(raw_entry) -> float | None:
    """`<itunes:duration>` in seconds.

    The tag is either a plain seconds count ("3902") or an [[HH:]MM:]SS clock
    string ("1:05:02"), so accept both. Lets a caller price a batch of episodes
    before downloading any of them — episode *count* is a poor proxy, since a
    feed of 4-minute clips and a feed of 90-minute interviews look identical.
    """
    raw = raw_entry.get("itunes_duration")
    if not raw:
        return None
    try:
        parts = [float(p) for p in str(raw).strip().split(":")]
    except ValueError:
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds or None


def _enclosure_url(raw_entry) -> str | None:
    """The first audio enclosure href, if any."""
    for enc in raw_entry.get("enclosures", []):
        href = enc.get("href")
        if href:
            return href
    return None


async def _fetch_feed(feed_url: str, timeout: float) -> feedparser.FeedParserDict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml,*/*;q=0.1",
    }
    async with httpx.AsyncClient() as client:
        response = await client.get(feed_url, headers=headers, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
    return feedparser.parse(response.content)


async def list_episodes(feed_url: str, timeout: float = 15.0) -> list[Episode]:
    """List episodes that have a downloadable audio enclosure.

    Cheap: one HTTP call, no audio download. Returns episodes sorted by publish
    date descending. Entries without a stable guid or an audio enclosure are
    skipped.
    """
    feed = await _fetch_feed(feed_url, timeout)
    podcast_title = feed.feed.get("title", "") if hasattr(feed, "feed") else ""
    podcast_author = feed.feed.get("author", "") if hasattr(feed, "feed") else ""

    episodes: list[Episode] = []
    for raw_entry in feed.entries:
        guid = raw_entry.get("id") or raw_entry.get("guid")
        audio_url = _enclosure_url(raw_entry)
        if not guid or not audio_url:
            continue
        episodes.append(Episode(
            guid=guid,
            title=raw_entry.get("title", ""),
            podcast_title=podcast_title,
            audio_url=audio_url,
            published_at=_published_at(raw_entry),
            webpage_url=raw_entry.get("link"),
            author=raw_entry.get("author") or podcast_author or None,
            duration_seconds=_duration_seconds(raw_entry),
        ))

    episodes.sort(key=lambda e: e.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return episodes


_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9_.-]")


def audio_filename(guid: str, audio_url: str) -> str:
    """Stable on-disk name for an episode's audio, derived from its guid."""
    suffix = Path(httpx.URL(audio_url).path).suffix or ".mp3"
    return f"{_UNSAFE_FILENAME.sub('_', guid)[:120]}{suffix}"


@contextlib.contextmanager
def _audio_target(episode: "Episode", audio_dir: Path | str | None) -> Iterator[Path]:
    """Where to put the downloaded audio.

    With `audio_dir` the file is kept after transcription (archive it, re-run
    ASR later, spot-check a bad transcript); without it the audio lives in a
    temp dir and is discarded once transcribed.
    """
    name = audio_filename(episode.guid, episode.audio_url)
    if audio_dir is not None:
        directory = Path(audio_dir)
        directory.mkdir(parents=True, exist_ok=True)
        yield directory / name
    else:
        with tempfile.TemporaryDirectory() as temp_dir:
            yield Path(temp_dir) / name


async def _download_audio(url: str, dest: Path, timeout: float) -> None:
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        async with client.stream("GET", url, headers=headers, timeout=timeout) as response:
            response.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in response.aiter_bytes():
                    f.write(chunk)


async def read_episode(
    episode: Episode,
    download_timeout: float = 120.0,
    audio_dir: Path | str | None = _KEEP_DEFAULT,
) -> ReadResult:
    """Download an episode's audio and transcribe it (raw ASR only).

    No OpenCC, no LLM refinement — `asr.transcribe` output verbatim. Returns a
    failed ReadResult on download error so a single bad episode doesn't abort a
    batch.

    Args:
        audio_dir: Where to keep the downloaded audio. Defaults to
            `resolve_default_audio_dir()` (the audio is kept, not discarded).
            Pass an explicit path to keep it elsewhere, or None to discard it
            after transcription.
    """
    from ..transcribe import asr

    if audio_dir is _KEEP_DEFAULT:
        audio_dir = resolve_default_audio_dir()

    url = f"podcast://{episode.guid}"

    with _audio_target(episode, audio_dir) as audio_path:
        # Transient blips (timeouts, resets) are common on podcast CDNs and a
        # lost download costs a whole episode, so retry before giving up.
        for attempt in range(DOWNLOAD_ATTEMPTS):
            try:
                await _download_audio(episode.audio_url, audio_path, download_timeout)
                break
            except httpx.HTTPError as exc:
                if attempt == DOWNLOAD_ATTEMPTS - 1:
                    return ReadResult.fail(
                        url,
                        f"audio download failed after {DOWNLOAD_ATTEMPTS} attempts: "
                        f"{type(exc).__name__}: {exc}",
                        source_type="rss_podcast",
                    )
                logger.warning(
                    "download attempt %d/%d failed for %s (%s: %s); retrying",
                    attempt + 1, DOWNLOAD_ATTEMPTS, episode.title, type(exc).__name__, exc,
                )
                await asyncio.sleep(2 ** attempt)

        text = asr.transcribe(audio_path).strip()

    return ReadResult(
        url=url,
        text=text,
        title=f"{episode.podcast_title} — {episode.title}" if episode.title else episode.podcast_title,
        source_type="rss_podcast",
        author=episode.author or episode.podcast_title,
        published_at=episode.published_at,
        raw={
            "guid": episode.guid,
            "podcast_title": episode.podcast_title,
            "episode_title": episode.title,
            "webpage_url": episode.webpage_url,
            "audio_url": episode.audio_url,
            "audio_file": str(audio_path) if audio_dir is not None else None,
            "method": "sensevoice",
        },
    )
