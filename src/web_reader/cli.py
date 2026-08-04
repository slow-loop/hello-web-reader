"""
CLI for web-reader.

Usage:
    uv run web-reader read <url> [OPTIONS]
    uv run web-reader config <file> [OPTIONS]

Examples:
    uv run web-reader config feeds.yaml
    uv run web-reader config feeds.yaml --tags finance,tech
    uv run web-reader read "https://hnrss.org/frontpage"
    uv run web-reader read "https://www.youtube.com/watch?v=i8OI8CNdZgU" --lang en
"""

import asyncio
import logging
from typing import Optional, List
from urllib.parse import parse_qs

import typer

from .store import Store, safe_name as _safe_name
from .formatting import flatten_results_map, format_results
from .cache import ReadCache

# Two different things, kept visibly apart:
#   Store     — the output/ archive, the permanent home of fetched content.
#   ReadCache — a short-TTL SQLite scratch cache for repeat reads.
# Never name a ReadCache variable `store`; that confusion is what let `read`
# drift away from the archive contract for months without anyone noticing.

app = typer.Typer(
    help="Lightweight web reader — fetch structured content from URLs and feeds.",
    add_completion=False,
)

# Per-video pause between yt-dlp calls in `channel --subtitles`/`--transcribe`.
# Firing one request after another with no gap reads as abuse to YouTube and
# gets the whole session rate-limited for up to an hour.
YTDLP_REQUEST_DELAY_SECONDS = 7

def _print_results(results, output_format: str) -> None:
    print(format_results(results, output_format))

def _setup_logging(verbose: bool):
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s - %(levelname)s - %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

def _stored_as_result(item):
    """A store hit, shaped like a fresh read so stdout is byte-identical."""
    from .models import ReadResult

    return ReadResult(
        url=item.url,
        text=item.text,
        title=item.title or item.url,
        source_type="youtube",
        language=item.extra.get("language"),
        raw=dict(item.extra),
        cached=True,
    )


async def _run_youtube(url: str, lang: Optional[str], output_format: str, use_asr: bool) -> None:
    """One video: serve it from the store, or fetch it into the store.

    The store is the cache here, not ReadCache. A published transcript never
    changes, so "is this video id anywhere in output/" is the only freshness
    question worth asking — and it is the same question `fetch` and `channel`
    ask before spending a yt-dlp round trip. Sharing it means a video pulled by
    any of the three is never pulled again by the others.
    """
    from datetime import datetime

    from .readers.youtube import _extract_video_id, read_youtube

    store = Store()
    video_id = _extract_video_id(url)
    if not video_id:
        typer.echo(f"ERROR: could not extract a video id from {url}", err=True)
        raise typer.Exit(1)

    hit = store.find_youtube(video_id)
    if hit:
        typer.echo(f"Cached: {hit.path}", err=True)
        _print_results([_stored_as_result(hit)], output_format)
        return

    result = await read_youtube(
        url, languages=[lang] if lang else None, use_audio_fallback=use_asr
    )
    if not result.success:
        # `read` is commonly redirected into a transcript file. Keep failures
        # off stdout so a failed acquisition cannot masquerade as a transcript.
        typer.echo(f"ERROR: {result.error or 'Read failed'}", err=True)
        raise typer.Exit(1)

    raw = result.raw or {}
    method = raw.get("method", "unknown")
    channel = raw.get("channel") or ""
    if channel:
        upload_date = raw.get("upload_date") or ""
        published = datetime.strptime(upload_date, "%Y%m%d") if len(upload_date) == 8 else None
        path = store.save_youtube(
            channel,
            # File by what actually happened: a video the API calls caption-less
            # can still resolve via an auto-generated track.
            "subtitles" if method == "yt-dlp-subs" else "transcripts",
            video_id,
            raw.get("video_title") or f"YouTube Video {video_id}",
            published,
            result.text,
            language=result.language,
            method=method,
        )
        typer.echo(f"Stored: {path}  (method={method}, language={result.language})", err=True)
    else:
        # No @handle, no honest folder name: the channel's display name would
        # spawn a second folder for a channel that already has one. Hand the
        # transcript over and say why it was not filed.
        typer.echo(
            f"Not stored: yt-dlp reported no channel handle for {video_id} "
            f"(method={method}) — transcript printed only",
            err=True,
        )

    _print_results([result], output_format)


async def _run_url(
    url: str,
    no_cache: bool,
    output_format: str,
    lang: Optional[str] = None,
    use_asr: bool = True,
) -> None:
    """Fetch a single URL."""
    from ._detect import detect_source_type

    source_type = detect_source_type(url)

    # YouTube is the one source type with a home in the store, so it gets the
    # store instead of the scratch cache. Everything else here is a page we do
    # not archive — per the pipeline's own rule, non-YouTube sources go through
    # OpenCLI, not this tool — so ReadCache stays their only memory.
    if source_type == "youtube":
        await _run_youtube(url, lang, output_format, use_asr)
        return

    cache = ReadCache() if not no_cache else None

    if cache and source_type != "rss":
        cached = cache.get_cached(url, ttl_seconds=3600, source_type=source_type)
        if cached:
            _print_results([cached], output_format)
            return

    if source_type == "gnews":
        from .readers.gnews import read_gnews
        # Parse: gnews://NVDA stock?period=7d&max=5
        raw = url[len("gnews://"):]
        parts = raw.split("?", 1)
        query = parts[0]
        qs = parse_qs(parts[1]) if len(parts) > 1 else {}
        period = qs.get("period", [None])[0]
        max_results = int(qs.get("max", [10])[0])
        results = await read_gnews(query, period=period, max_results=max_results)
        if cache:
            for r in results:
                if r.success:
                    cache.save(r)
        _print_results(results, output_format)
        return
    elif source_type == "ptt":
        from .readers.ptt import read_ptt
        result = await read_ptt(url)
    elif source_type == "reddit":
        from .readers.reddit import read_reddit
        result = await read_reddit(url)
    elif source_type == "stocktwits":
        from .readers.stocktwits import read_stocktwits
        result = await read_stocktwits(url)
    elif source_type == "substack" and url.startswith("substack://search"):
        from .readers.substack import search_substack
        raw = url[len("substack://search"):]
        qs = parse_qs(raw.lstrip("?"))
        query = qs.get("q", [""])[0]
        page = int(qs.get("page", [0])[0])
        result = await search_substack(query, page=page)
    elif source_type == "substack" and url.startswith("substack://explore"):
        from .readers.substack import explore_substack
        raw = url[len("substack://explore"):]
        qs = parse_qs(raw.lstrip("?"))
        tab = qs.get("tab", ["finance"])[0]
        result = await explore_substack(tab=tab)
    elif source_type == "substack":
        from .readers.substack import read_substack
        result = await read_substack(url)
    elif source_type == "rss":
        from .readers.rss import read_rss
        result = await read_rss(url)
    elif source_type == "json":
        from .readers.json_api import read_json
        result = await read_json(url)
    else:
        from .readers.web import read_web
        result = await read_web(url)

    if not result.success:
        # `read` is commonly redirected into a transcript file. Keep failures
        # off stdout so a failed acquisition cannot masquerade as a transcript.
        typer.echo(f"ERROR: {result.error or 'Read failed'}", err=True)
        raise typer.Exit(1)

    if cache and source_type != "rss":
        cache.save(result)

    _print_results([result], output_format)


def _parse_iso_duration(iso: str) -> str:
    """ISO 8601 duration (PT1H2M3S) -> h:mm:ss / m:ss."""
    import re

    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not match:
        return ""
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _is_ytdlp_rate_limited(error: str | None) -> bool:
    return "rate-limited" in (error or "").lower()


def _duration_seconds(duration: str) -> int:
    """Inverse of _parse_iso_duration's output: h:mm:ss / m:ss -> seconds."""
    if not duration:
        return 0
    parts = [int(p) for p in duration.split(":")]
    return parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1]


async def _run_channel(
    handle: str,
    limit: int,
    since: str | None,
    until: str | None,
    thumbnails: bool,
    subtitles: bool,
    transcribe: bool,
    languages: list[str] | None,
    out_dir,
    channel_key: str,
) -> None:
    """List a channel's videos with full API metadata, and optionally download
    thumbnails and subtitle transcripts."""
    import csv
    import json
    import time
    from datetime import datetime

    import httpx

    from .readers.youtube import (
        download_thumbnail,
        fetch_channel_info,
        fetch_video_details,
        list_channel_videos,
        read_youtube,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    # Timestamp aggregate files so re-runs never overwrite a previous snapshot,
    # and name any active filter into them so a windowed snapshot can never be
    # mistaken on disk for a complete one.
    scope = "".join(
        f"_{k}-{v}" for k, v in (("since", since), ("until", until), ("limit", limit or None)) if v
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + scope

    # Report the channel's true size first — a capped run must never look complete.
    info = await fetch_channel_info(handle)
    print(f"Channel: {info['title']} — {info['video_count']} videos total")

    videos = await list_channel_videos(info["id"], limit=limit, since=since, until=until)
    if not videos:
        print("No videos matched.")
        return

    window = f", window {since or 'start'}..{until or 'now'}" if (since or until) else ""
    capped = " (capped by --limit; use --limit 0 for all)" if limit and len(videos) == limit else ""
    print(f"Selected {len(videos)} videos{window}{capped}. Fetching full metadata...")

    details = await fetch_video_details([v["id"] for v in videos])
    print(f"Got metadata for {len(details)}/{len(videos)} videos.")

    # Keep the complete API response — the CSV below is only a readable subset.
    raw_path = out_dir / f"videos_{stamp}.json"
    raw_path.write_text(
        json.dumps([details[v["id"]] for v in videos if v["id"] in details], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Saved raw metadata: {raw_path}")

    rows = []
    for i, v in enumerate(videos, 1):
        item = details.get(v["id"], {})
        snippet = item.get("snippet", {})
        content = item.get("contentDetails", {})
        stats = item.get("statistics", {})
        rows.append({
            "index": i,
            "video_id": v["id"],
            "published_at": v["published_at"],
            "duration": _parse_iso_duration(content.get("duration", "")),
            "title": v["title"],
            "views": stats.get("viewCount", ""),
            "likes": stats.get("likeCount", ""),
            "comments": stats.get("commentCount", ""),
            "caption": content.get("caption", ""),
            "definition": content.get("definition", ""),
            "language": snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage") or "",
            "tags": "|".join(snippet.get("tags", [])),
            "url": v["url"],
            "thumbnail_url": v.get("thumbnail") or "",
        })

    manifest = out_dir / f"manifest_{stamp}.csv"
    with manifest.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved manifest: {manifest}")

    if thumbnails:
        thumb_dir = out_dir / "thumbnails"
        thumb_dir.mkdir(exist_ok=True)
        print(f"\nDownloading {len(rows)} thumbnails to {thumb_dir}...")
        async with httpx.AsyncClient(timeout=30) as client:
            for i, r in enumerate(rows, 1):
                dest = thumb_dir / f"{r['video_id']}.jpg"
                if dest.exists():
                    continue
                used = await download_thumbnail(r["video_id"], dest, client, r["thumbnail_url"] or None)
                status = "ok" if used else "FAILED"
                print(f"  [{i}/{len(rows)}] {status} {r['video_id']}")

    # Transcript storage goes through the store contract (frontmatter,
    # canonical paths, global dedupe by video id) — the same layout `fetch`
    # writes and downstream consumers read. --out only moves the snapshot
    # files above, never the transcripts: the store folder comes from
    # `channel_key` (the parsed handle), never from the snapshot path, so
    # `--out /tmp/scratch` cannot file a channel's videos under "scratch".
    store = Store()

    async def _save_transcripts(targets: list[dict], use_audio: bool) -> None:
        for i, r in enumerate(targets, 1):
            if store.has_youtube(r["video_id"]):
                print(f"  [{i}/{len(targets)}] exists, skipping {r['video_id']}")
                continue
            started = time.monotonic()
            result = await read_youtube(r["url"], languages=languages, use_audio_fallback=use_audio)
            await asyncio.sleep(YTDLP_REQUEST_DELAY_SECONDS)
            if not result.success:
                print(f"  [{i}/{len(targets)}] FAILED {r['video_id']}: {result.error}")
                if _is_ytdlp_rate_limited(result.error):
                    print(f"  Stopping: YouTube rate-limited this session. {len(targets) - i} videos not attempted — rerun later.")
                    break
                continue
            published = None
            if r["published_at"]:
                published = datetime.fromisoformat(r["published_at"].replace("Z", "+00:00"))
            method = (result.raw or {}).get("method", "")
            # A caption-less video may still resolve via captions after all
            # (auto-generated tracks) — file it by what actually happened.
            actual_kind = "subtitles" if method == "yt-dlp-subs" else "transcripts"
            path = store.save_youtube(
                channel_key, actual_kind, r["video_id"], r["title"], published,
                result.text, language=result.language, method=method,
            )
            took = f"{r['duration']} audio in {time.monotonic() - started:.0f}s" if use_audio else (result.language or "?")
            print(f"  [{i}/{len(targets)}] ok ({took}, {len(result.text)} chars) -> {actual_kind}/{path.name}")

    if subtitles:
        # The API already told us which videos have captions — skip the rest
        # without paying for a yt-dlp round trip.
        targets = [r for r in rows if r["caption"] == "true"]
        print(
            f"\nSubtitles: {len(targets)}/{len(rows)} videos have captions "
            f"(est. ~{max(1, round(len(targets) * 3 / 60))} min) -> {store.youtube_dir(channel_key) / 'subtitles'}"
        )
        await _save_transcripts(targets, use_audio=False)

    if transcribe:
        # Complement of --subtitles: only videos with no caption track, which is
        # the only case worth paying for audio ASR. Kept in its own folder
        # because machine transcripts are not interchangeable with real captions.
        targets = [r for r in rows if r["caption"] != "true"]
        audio_min = sum(_duration_seconds(r["duration"]) for r in targets) / 60
        print(
            f"\nTranscripts: {len(targets)}/{len(rows)} videos have no captions, "
            f"{audio_min / 60:.1f}h of audio -> {store.youtube_dir(channel_key) / 'transcripts'}"
        )
        await _save_transcripts(targets, use_audio=True)

    print("\nDone.")


async def _run_config(config_path: str, tags: list[str] | None, no_cache: bool, output_format: str) -> None:
    """Run a YAML config file."""
    from .runner import run_config

    cache = ReadCache() if not no_cache else None
    results_map = await run_config(config_path, tags=tags, no_cache=no_cache, cache=cache)
    _print_results(flatten_results_map(results_map), output_format)


@app.command()
def read(
    url: str = typer.Argument(..., help="The URL to read."),
    format: str = typer.Option("md", help="Output format (json or md)"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Skip cache, always re-fetch"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Enable verbose logging"),
    lang: Optional[str] = typer.Option(None, help="Specific subtitle language for YouTube (e.g. 'en')"),
    no_asr: bool = typer.Option(False, "--no-asr", help="YouTube: fail instead of falling back to local audio ASR (minutes of compute)."),
):
    """
    Read a single URL and output structured text.

    A YouTube video is archived into the output/ store on the way through —
    same folder `channel` and `fetch` write — and served straight from there on
    a repeat read, without touching the network.
    """
    _setup_logging(verbose)
    asyncio.run(_run_url(url, no_cache, format, lang=lang, use_asr=not no_asr))


@app.command()
def config(
    file_path: str = typer.Argument(..., help="Path to the YAML config file."),
    tags: Optional[str] = typer.Option(None, help="Filter config sources by tags (comma-separated)"),
    format: str = typer.Option("md", help="Output format (json or md)"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Skip cache, always re-fetch"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Enable verbose logging"),
):
    """
    Read multiple feeds from a YAML config file.
    """
    _setup_logging(verbose)
    tags_list = [t.strip() for t in tags.split(",")] if tags else None
    asyncio.run(_run_config(file_path, tags_list, no_cache, format))


@app.command()
def channel(
    handle: str = typer.Argument(..., help="YouTube channel handle (@name) or channel/URL."),
    since: Optional[str] = typer.Option(None, help="Only videos published on/after this date (YYYY-MM-DD)."),
    until: Optional[str] = typer.Option(None, help="Only videos published on/before this date (YYYY-MM-DD)."),
    limit: int = typer.Option(0, help="Cap at N newest videos after date filtering. 0 = no cap."),
    thumbnails: bool = typer.Option(False, "--thumbnails", help="Also download cover images (no video)."),
    subtitles: bool = typer.Option(False, "--subtitles", help="Also fetch subtitle transcripts where available."),
    transcribe: bool = typer.Option(False, "--transcribe", help="Audio-transcribe the videos that have NO captions (slow, local ASR)."),
    lang: Optional[str] = typer.Option(None, help="Comma-separated preferred subtitle languages (e.g. 'zh-Hant,en')."),
    out: Optional[str] = typer.Option(None, help="Directory for snapshot files (manifest/videos/thumbnails); default <repo>/output/youtube/<handle>. Subtitle/ASR transcripts always land in the output/ store, under the handle — --out never moves them."),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Enable verbose logging"),
):
    """
    Snapshot a YouTube channel's videos via the YouTube Data API: every run
    writes a fresh timestamped manifest_<stamp>.csv and videos_<stamp>.json and
    never overwrites an earlier one, so the newest file is always the newest
    observation. Add --thumbnails / --subtitles to also download cover images
    and subtitle transcripts; those are named by video ID and skipped when
    already present, so an interrupted run resumes for free.

    The whole channel is listed by default. --since/--until pick a date window;
    --limit then caps that window at N newest videos.
    """
    from pathlib import Path

    from .readers.youtube import _channel_lookup_params

    _setup_logging(verbose)
    languages = [s.strip() for s in lang.split(",")] if lang else None
    # Derive the folder from the parsed handle so a full channel URL and a bare
    # @handle land in the same place.
    lookup = _channel_lookup_params(handle)
    handle_name = _safe_name(lookup.get("forHandle", lookup.get("id", "")).lstrip("@")) or "channel"
    # Default the snapshot dir off the store's own root, not a cwd-relative
    # "output/". They are the same folder only when you happen to run from the
    # repo root; anywhere else a bare relative path splits the snapshot files
    # away from the transcripts they describe.
    out_dir = Path(out) if out else Store().youtube_dir(handle_name)
    asyncio.run(
        _run_channel(
            handle, limit, since, until, thumbnails, subtitles, transcribe,
            languages, out_dir, handle_name,
        )
    )


@app.command()
def fetch(
    watchlist: str = typer.Argument(..., help="Path to the watchlist config (feeds.yaml format)."),
    since: str = typer.Option("-24h", help="Window start: -24h / -7d / YYYY-MM-DD."),
    source: Optional[str] = typer.Option(None, help="Only fetch this source id."),
    limit: Optional[int] = typer.Option(None, help="Override per-source cap on new expensive fetches (ASR/yt-dlp)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List what would be fetched; write nothing."),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Enable verbose logging"),
):
    """
    Incrementally fetch watchlist sources into the output/ store.

    The recurring acquisition step: rss articles into output/substack/,
    podcast ASR transcripts into output/podcast/, YouTube captions (with ASR
    fallback for caption-less videos) into output/youtube/. Already-archived
    items are never re-fetched, so overlapping windows are free.
    """
    from .fetch import fetch_watchlist

    _setup_logging(verbose)
    results = asyncio.run(fetch_watchlist(watchlist, since, source, limit, dry_run))
    if not results:
        return
    # Exit 3 for a partial failure. Callers pull from the archive after this
    # returns, so "8 of 35 sources failed" must not look like a clean run —
    # otherwise the stale items still in the archive get served as fresh.
    # Not 2: click already spends that on usage errors, and a caller that sees
    # 2 could not tell a mistyped flag from sources that genuinely failed.
    if all(r.error for r in results):
        raise typer.Exit(1)
    if any(r.error for r in results):
        raise typer.Exit(3)


def main():
    app()


if __name__ == "__main__":
    main()
