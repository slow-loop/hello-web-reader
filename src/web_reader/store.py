"""Store — the file-layout contract for `output/`.

`output/` is the permanent home of fetched raw content: everything web-reader
fetches lands here as human-readable markdown, and downstream consumers (e.g.
the hello-note KOL pipeline) read it back exclusively through this module.
No other code should hard-code the layout — writer and reader sharing this
file is what keeps them from drifting apart.

Layout, one directory per source:

    output/youtube/<channel>/subtitles/<YYYY-MM-DD>_<video_id>_<title>.md
    output/youtube/<channel>/transcripts/<YYYY-MM-DD>_<video_id>_<title>.md
    output/podcast/<source_id>/<YYYY-MM-DD>_<guid_slug>.md
    output/podcast/<source_id>/audio/<audio files>
    output/substack/<source_id>/<YYYY-MM-DD>_<url_slug>.md

`subtitles/` holds real caption tracks; `transcripts/` holds local ASR output
for videos that have none — machine transcripts are not interchangeable with
captions, so they never share a folder.

Files written by this module carry YAML frontmatter (url, title,
published_at, ...). Files that predate the contract may not; read functions
tolerate both. (All legacy layouts were migrated into the canonical one via
scripts/migrate_youtube_transcribed.py on 2026-07-30/31.)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal, Optional

import yaml
from pydantic import BaseModel

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = _REPO_ROOT / "output"

_UNSAFE_SLUG = re.compile(r"[^A-Za-z0-9_.-]")


def safe_name(text: str, limit: int = 60) -> str:
    """Filesystem-safe fragment of a human title (same rule as `channel`)."""
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in text)[:limit].strip()


def guid_slug(guid: str) -> str:
    """Filesystem-safe fragment of a podcast guid (often a URL)."""
    return _UNSAFE_SLUG.sub("_", guid)[:80]


def url_slug(url: str) -> str:
    """Filesystem-safe fragment of an article URL (its last path segment)."""
    last = url.rstrip("/").rsplit("/", 1)[-1] or "index"
    return _UNSAFE_SLUG.sub("_", last)[:80]


def _date_prefix(published_at: Optional[datetime]) -> str:
    return published_at.date().isoformat() if published_at else "unknown"


class StoredItem(BaseModel):
    """One archived document, frontmatter merged with filename knowledge."""

    url: str
    title: str = ""
    published_at: Optional[datetime] = None
    text: str = ""
    source_type: str = "unknown"
    path: Path
    extra: dict = {}


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    """Return (frontmatter, body). Files without frontmatter yield ({}, raw)."""
    if raw.startswith("---\n"):
        head, sep, body = raw[4:].partition("\n---\n")
        if sep:
            try:
                meta = yaml.safe_load(head) or {}
            except yaml.YAMLError:
                return {}, raw
            if isinstance(meta, dict):
                return meta, body.lstrip("\n")
    return {}, raw


def _render_frontmatter(meta: dict) -> str:
    clean = {k: v for k, v in meta.items() if v is not None}
    return "---\n" + yaml.safe_dump(clean, allow_unicode=True, sort_keys=False) + "---\n\n"


def _parse_dt(value) -> Optional[datetime]:
    parsed = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed is None:
        return None
    # Frontmatter timestamps come from feeds that publish naive UTC.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Store:
    """Read/write access to the output/ tree. Root defaults to this repo's."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else DEFAULT_ROOT

    # ── YouTube ──────────────────────────────────────────────────────────────

    def youtube_dir(self, channel: str) -> Path:
        return self.root / "youtube" / channel

    def _youtube_paths(self, video_id: str) -> Iterator[Path]:
        yt = self.root / "youtube"
        if not yt.is_dir():
            return
        yield from yt.glob(f"*/subtitles/*_{video_id}_*.md")
        yield from yt.glob(f"*/transcripts/*_{video_id}_*.md")

    def has_youtube(self, video_id: str) -> bool:
        return next(self._youtube_paths(video_id), None) is not None

    def find_youtube(self, video_id: str) -> Optional[StoredItem]:
        path = next(self._youtube_paths(video_id), None)
        if path is None:
            return None
        return self._load(path, source_type="youtube", url=_youtube_url(video_id))

    def save_youtube(
        self,
        channel: str,
        kind: Literal["subtitles", "transcripts"],
        video_id: str,
        title: str,
        published_at: Optional[datetime],
        text: str,
        language: Optional[str] = None,
        method: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> Path:
        directory = self.youtube_dir(channel) / kind
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_date_prefix(published_at)}_{video_id}_{safe_name(title)}.md"
        meta = {
            "url": _youtube_url(video_id),
            "title": title,
            "published_at": published_at.isoformat() if published_at else None,
            "source_type": "youtube",
            "video_id": video_id,
            "language": language,
            "method": method,
            **(extra or {}),
        }
        path.write_text(_render_frontmatter(meta) + text, encoding="utf-8")
        return path

    def list_youtube(self, channel: str, since_date: str | None = None) -> list[StoredItem]:
        items = []
        for kind in ("subtitles", "transcripts"):
            for path in _md_files(self.youtube_dir(channel) / kind, since_date):
                vid = _video_id_from_name(path.name)
                items.append(self._load(path, source_type="youtube", url=_youtube_url(vid) if vid else ""))
        items.sort(key=lambda i: i.published_at or datetime.min.replace(tzinfo=timezone.utc))
        return items

    # ── Podcast ──────────────────────────────────────────────────────────────

    def podcast_dir(self, source_id: str) -> Path:
        return self.root / "podcast" / source_id

    def podcast_audio_dir(self, source_id: str) -> Path:
        return self.podcast_dir(source_id) / "audio"

    def _podcast_paths(self, guid: str) -> Iterator[Path]:
        pod = self.root / "podcast"
        if not pod.is_dir():
            return
        yield from pod.glob(f"*/*_{guid_slug(guid)}.md")

    def has_podcast(self, guid: str) -> bool:
        return next(self._podcast_paths(guid), None) is not None

    def save_podcast(
        self,
        source_id: str,
        guid: str,
        title: str,
        published_at: Optional[datetime],
        text: str,
        webpage_url: Optional[str] = None,
        author: Optional[str] = None,
        method: Optional[str] = None,
    ) -> Path:
        directory = self.podcast_dir(source_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_date_prefix(published_at)}_{guid_slug(guid)}.md"
        meta = {
            "url": f"podcast://{guid}",
            "title": title,
            "published_at": published_at.isoformat() if published_at else None,
            "source_type": "rss_podcast",
            "guid": guid,
            "webpage_url": webpage_url,
            "author": author,
            "method": method,
        }
        path.write_text(_render_frontmatter(meta) + text, encoding="utf-8")
        return path

    def list_podcast(self, source_id: str, since_date: str | None = None) -> list[StoredItem]:
        items = [
            self._load(path, source_type="rss_podcast")
            for path in _md_files(self.podcast_dir(source_id), since_date)
        ]
        items.sort(key=lambda i: i.published_at or datetime.min.replace(tzinfo=timezone.utc))
        return items

    # ── Articles (substack / rss) ────────────────────────────────────────────

    def article_dir(self, source_id: str) -> Path:
        return self.root / "substack" / source_id

    def has_article(self, source_id: str, url: str) -> bool:
        directory = self.article_dir(source_id)
        return directory.is_dir() and next(directory.glob(f"*_{url_slug(url)}.md"), None) is not None

    def save_article(
        self,
        source_id: str,
        url: str,
        title: str,
        published_at: Optional[datetime],
        text: str,
        author: Optional[str] = None,
    ) -> Path:
        directory = self.article_dir(source_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_date_prefix(published_at)}_{url_slug(url)}.md"
        meta = {
            "url": url,
            "title": title,
            "published_at": published_at.isoformat() if published_at else None,
            "source_type": "substack",
            "author": author,
        }
        path.write_text(_render_frontmatter(meta) + text, encoding="utf-8")
        return path

    def list_articles(self, source_id: str, since_date: str | None = None) -> list[StoredItem]:
        items = [
            self._load(path, source_type="substack")
            for path in _md_files(self.article_dir(source_id), since_date)
        ]
        items.sort(key=lambda i: i.published_at or datetime.min.replace(tzinfo=timezone.utc))
        return items

    # ── Shared ───────────────────────────────────────────────────────────────

    def _load(self, path: Path, source_type: str, url: str = "") -> StoredItem:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        meta, body = _split_frontmatter(raw)
        known = {"url", "title", "published_at", "source_type"}
        return StoredItem(
            url=meta.get("url") or url,
            title=meta.get("title") or "",
            published_at=_parse_dt(meta.get("published_at")) or _parse_dt(_file_date(path.name)),
            text=body.strip(),
            source_type=meta.get("source_type") or source_type,
            path=path,
            extra={k: v for k, v in meta.items() if k not in known},
        )


def _md_files(directory: Path, since_date: str | None = None) -> list[Path]:
    """Markdown files in `directory`, optionally date-filtered by filename prefix.

    Files without a parseable date prefix are always included — losing them
    silently would be worse than over-including.
    """
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.md")):
        day = _file_date(path.name)
        if since_date and day and day < since_date:
            continue
        out.append(path)
    return out


_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})_")
_VIDEO_ID_IN_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_([A-Za-z0-9_-]{11})_")


def _file_date(name: str) -> Optional[str]:
    m = _DATE_PREFIX.match(name)
    return m.group(1) if m else None


def _video_id_from_name(name: str) -> Optional[str]:
    m = _VIDEO_ID_IN_NAME.match(name)
    return m.group(1) if m else None


def _youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"
