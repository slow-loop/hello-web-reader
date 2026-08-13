"""`web-reader fetch` — incremental watchlist fetch into the output/ store.

Reads a watchlist (feeds.yaml-format config), fetches whatever is new since
the window start, and archives it through `web_reader.store`. This is the
recurring acquisition step of the pipeline: downstream consumers never fetch
from the network themselves — they read the store this command maintains.

Per reader:
    rss           → article full text  → output/substack/<id>/
    rss_podcast   → audio + local ASR  → output/podcast/<id>/  (audio kept in audio/)
    youtube_channel → captions, ASR fallback for caption-less videos
                                       → output/youtube/<handle>/{subtitles,transcripts}/

Already-archived items (by url / guid / video id) are never re-fetched — ASR
is the only irreversible cost here, and a published item's content never goes
stale. `limit` caps the number of *new* expensive fetches per source, so a
missed window widens cost linearly, not silently.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import Store, safe_name
from .config import SourceConfig, load_config

# Measured: SenseVoice runs at ~7.4x realtime. Used for --dry-run estimates.
ASR_REALTIME_FACTOR = 7.4

# Per-video pause between yt-dlp calls; back-to-back requests get the whole
# session rate-limited by YouTube for up to an hour.
YTDLP_REQUEST_DELAY_SECONDS = 7

DEFAULT_PODCAST_LIMIT = 5
DEFAULT_YOUTUBE_LIMIT = 10

_RELATIVE_SINCE_RE = re.compile(r"^-(\d+)([hdm])$")
_ISO8601_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def resolve_since(s: str) -> datetime:
    """-24h / -7d / -30m / YYYY-MM-DD → aware-UTC datetime."""
    m = _RELATIVE_SINCE_RE.match(s)
    if m:
        amount, unit = int(m.group(1)), m.group(2)
        delta = {
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "m": timedelta(minutes=amount),
        }[unit]
        return datetime.now(timezone.utc) - delta
    return datetime.fromisoformat(f"{s}T00:00:00+00:00")


def _iso_duration_seconds(s: str) -> int:
    m = _ISO8601_DURATION_RE.match(s or "")
    if not m:
        return 0
    h, mi, se = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + se


def _source_id(source: SourceConfig) -> str:
    return source.id or safe_name(source.name) or "unnamed"


class SourceResult:
    def __init__(self, source_id: str, reader: str):
        self.source_id = source_id
        self.reader = reader
        self.new = 0        # newly archived this run
        self.reused = 0     # in window but already archived
        self.error: str | None = None


async def _fetch_rss(source: SourceConfig, store: Store, since: datetime, dry_run: bool) -> SourceResult:
    from .readers.rss import read_rss_entries

    res = SourceResult(_source_id(source), source.reader)
    # web-reader's RSS filter compares against feedparser's naive-UTC datetimes.
    since_naive = since.replace(tzinfo=None).isoformat()
    results = await read_rss_entries(source.url, published_after=since_naive)
    for r in results:
        if not r.ok:
            print(f"    ✗ {(r.title or r.url)[:50]}: {r.error}")
            continue
        if store.has_article(res.source_id, r.url):
            res.reused += 1
            continue
        if dry_run:
            print(f"    would fetch: {(r.title or r.url)[:70]}")
        else:
            store.save_article(
                res.source_id, r.url, r.title, r.published_at, r.text, author=r.author,
            )
        res.new += 1
    return res


async def _fetch_podcast(
    source: SourceConfig, store: Store, since: datetime, dry_run: bool, limit_override: int | None,
) -> SourceResult:
    from .readers.rss_podcast import list_episodes, read_episode

    res = SourceResult(_source_id(source), source.reader)
    limit = limit_override if limit_override is not None else int(source.params.get("limit", DEFAULT_PODCAST_LIMIT))

    todo = []
    for ep in await list_episodes(source.url):
        if not (ep.published_at and ep.published_at >= since):
            continue
        if store.has_podcast(ep.guid):
            res.reused += 1
            continue
        todo.append(ep)

    # Episodes are newest-first; the cap drops the oldest — and says so, rather
    # than silently losing them the way a bare newest-N slice would.
    if len(todo) > limit:
        print(
            f"    ⚠ {len(todo)} episodes to transcribe, doing newest {limit} "
            f"(raise --limit to widen)"
        )
        todo = todo[:limit]

    audio_h = sum(ep.duration_seconds or 0 for ep in todo) / 3600
    if todo:
        print(
            f"    {len(todo)} episode(s) to transcribe, {audio_h:.1f}h audio "
            f"(~{audio_h / ASR_REALTIME_FACTOR * 60:.0f} min ASR)"
        )
    for ep in todo:
        if dry_run:
            date = ep.published_at.date().isoformat() if ep.published_at else "??????????"
            print(f"    would transcribe: {date}  {ep.title[:60]}")
            res.new += 1
            continue
        r = await read_episode(ep, audio_dir=store.podcast_audio_dir(res.source_id))
        if not r.ok:
            print(f"    ✗ {ep.title[:50]}: {r.error}")
            continue
        path = store.save_podcast(
            res.source_id, ep.guid, ep.title, ep.published_at, r.text,
            webpage_url=ep.webpage_url, author=ep.author, method="sensevoice",
        )
        print(f"    ✓ {path.name}")
        res.new += 1
    return res


async def _fetch_youtube(
    source: SourceConfig, store: Store, since: datetime, dry_run: bool, limit_override: int | None,
) -> SourceResult:
    from .readers.youtube import (
        fetch_video_details, list_channel_videos, probe_captions, read_youtube,
    )

    res = SourceResult(_source_id(source), source.reader)
    channel_id = source.params.get("channel_id") or source.params.get("handle")
    if not channel_id:
        raise ValueError("youtube source missing params.channel_id")
    # Store folder: explicit handle param, falling back to the source id.
    channel_dir = (source.params.get("handle") or res.source_id).lstrip("@")
    limit = limit_override if limit_override is not None else int(source.params.get("limit", DEFAULT_YOUTUBE_LIMIT))

    videos = await list_channel_videos(channel_id, since=since.date().isoformat())
    fresh = []
    for v in videos:
        if store.has_youtube(v["id"]):
            res.reused += 1
        else:
            fresh.append(v)

    details = await fetch_video_details([v["id"] for v in fresh]) if fresh else {}
    todo = []
    skipped = Counter()
    for v in fresh:
        d = details.get(v["id"], {})
        snippet, content = d.get("snippet", {}), d.get("contentDetails", {})
        # Live *replays* are ordinary videos here: for some channels the daily
        # livestream is the whole show. Only a stream still running (or merely
        # scheduled) is skipped — it has no complete transcript yet, so it gets
        # picked up by a later run instead.
        if snippet.get("liveBroadcastContent", "none") != "none":
            skipped["still live / upcoming"] += 1
            continue
        duration = _iso_duration_seconds(content.get("duration", ""))
        v["duration_seconds"] = duration
        todo.append(v)

    # Never drop a video without saying so. A filter that quietly eats a whole
    # channel's output looks exactly like a channel that published nothing.
    if skipped:
        print("    skipped " + ", ".join(f"{n} {reason}" for reason, n in skipped.items()))

    if len(todo) > limit:
        print(f"    ⚠ {len(todo)} videos to fetch, doing newest {limit} (raise --limit to widen)")
        todo = todo[:limit]

    # The only distinction that matters for cost is captions vs no captions —
    # manual and auto are both just a subtitle download. The Data API's
    # `contentDetails.caption` sees manual tracks only, so it cannot make that
    # split: a dry-run built on it labels a free auto-captioned video the same
    # as one facing ASR. That ambiguity nearly cost us a top source (ILTB read
    # as "68 min ASR"; the real fetch took 13s and no ASR), so dry-run pays
    # ~1.5s/video to ask for real. A live fetch does not probe — it finds out
    # by doing, and reports the same number below as a fact.
    languages = source.params.get("languages")
    asr_seconds = 0
    for v in todo:
        date = (v.get("published_at") or "??????????")[:10]
        if dry_run:
            track = probe_captions(v["url"])
            if track:
                label = f"{track} captions"
            else:
                label = "NO CAPTIONS → ASR"
                asr_seconds += v["duration_seconds"]
            print(f"    would fetch [{label}]: {date}  {v['title'][:60]}")
            res.new += 1
            continue
        r = await read_youtube(v["url"], languages=languages, use_audio_fallback=True)
        await asyncio.sleep(YTDLP_REQUEST_DELAY_SECONDS)
        if not r.ok:
            print(f"    ✗ {v['title'][:50]}: {r.error}")
            if "rate-limited" in (r.error or "").lower():
                res.error = "YouTube rate-limited this session; rerun later"
                print(f"    Stopping: rate-limited. Videos not attempted: "
                      f"{len(todo) - todo.index(v) - 1}")
                break
            continue
        method = (r.raw or {}).get("method", "")
        kind = "subtitles" if method == "yt-dlp-subs" else "transcripts"
        published = datetime.fromisoformat(v["published_at"].replace("Z", "+00:00")) if v.get("published_at") else None
        path = store.save_youtube(
            channel_dir, kind, v["id"], v["title"], published, r.text,
            language=r.language, method=method,
        )
        if kind == "transcripts":
            asr_seconds += v["duration_seconds"]
        print(f"    ✓ {kind}/{path.name}")
        res.new += 1

    # Only surfaced for videos with neither manual nor auto captions — the one
    # case that costs anything. "No manual captions" on its own does not.
    hours = asr_seconds / 3600
    if hours:
        verb = "would need" if dry_run else "transcribed by"
        print(f"    ⚠ {hours:.1f}h of video has no captions at all → {verb} ASR "
              f"(~{hours / ASR_REALTIME_FACTOR * 60:.0f} min)")
    return res


_FETCHERS = {
    "rss": _fetch_rss,
    "rss_podcast": _fetch_podcast,
    "youtube": _fetch_youtube,
    "youtube_channel": _fetch_youtube,
}


async def fetch_watchlist(
    config_path: str | Path,
    since_str: str = "-24h",
    only_source: str | None = None,
    limit_override: int | None = None,
    dry_run: bool = False,
    store: Store | None = None,
) -> list[SourceResult]:
    store = store or Store()
    since = resolve_since(since_str)
    config = load_config(config_path)

    sources = config.sources
    if only_source:
        sources = [s for s in sources if _source_id(s) == only_source]
        if not sources:
            raise ValueError(f"source id not found in watchlist: {only_source}")

    results: list[SourceResult] = []
    for source in sources:
        sid = _source_id(source)
        fetcher = _FETCHERS.get(source.reader)
        if fetcher is None:
            print(f"  - {sid}: reader {source.reader!r} not supported by fetch, skipping")
            continue
        print(f"  {sid} ({source.reader})")
        try:
            if source.reader == "rss":
                res = await fetcher(source, store, since, dry_run)
            else:
                res = await fetcher(source, store, since, dry_run, limit_override)
        except Exception as e:
            res = SourceResult(sid, source.reader)
            res.error = f"{type(e).__name__}: {e}"
            print(f"    ✗ {res.error}")
        # Say when a source was already up to date. Without this, "we have all
        # of it already" and "this source published nothing" look identical —
        # a bare header line either way.
        if res.reused:
            print(f"    {res.reused} already archived")
        results.append(res)

    n_new = sum(r.new for r in results)
    n_reused = sum(r.reused for r in results)
    n_errors = sum(1 for r in results if r.error)
    verb = "would store" if dry_run else "archived"
    print(
        f"\n{len(results)} source(s), {verb} {n_new} new item(s), "
        f"{n_reused} already archived, {n_errors} error(s), "
        f"since {since.isoformat(timespec='seconds')}"
    )
    return results
