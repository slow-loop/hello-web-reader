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

from .formatting import flatten_results_map, format_results
from .store import ReadStore

app = typer.Typer(
    help="Lightweight web reader — fetch structured content from URLs and feeds.",
    add_completion=False,
)

def _print_results(results, output_format: str) -> None:
    print(format_results(results, output_format))

def _setup_logging(verbose: bool):
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s - %(levelname)s - %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

async def _run_url(
    url: str,
    no_cache: bool,
    output_format: str,
    lang: Optional[str] = None,
) -> None:
    """Fetch a single URL."""
    from ._detect import detect_source_type

    source_type = detect_source_type(url)
    store = ReadStore() if not no_cache else None

    if store and source_type != "rss":
        cached = store.get_cached(url, ttl_seconds=3600, source_type=source_type)
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
        if store:
            for r in results:
                if r.success:
                    store.save(r)
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
    elif source_type == "youtube":
        from .readers.youtube import read_youtube
        languages = [lang] if lang else None
        result = await read_youtube(url, languages=languages)
    elif source_type == "rss":
        from .readers.rss import read_rss
        result = await read_rss(url)
    elif source_type == "json":
        from .readers.json_api import read_json
        result = await read_json(url)
    else:
        from .readers.web import read_web
        result = await read_web(url)

    if store and result.success and source_type != "rss":
        store.save(result)

    if result.raw and "vtt" in result.raw:
        video_id = result.raw.get("video_id", "video")
        lang = result.raw.get("language", "unknown")
        
        # Save VTT
        vtt_path = f"{video_id}_{lang}.vtt"
        try:
            with open(vtt_path, "w", encoding="utf-8") as f:
                f.write(str(result.raw["vtt"]))
            logging.getLogger(__name__).info(f"Saved VTT to {vtt_path}")
        except Exception as e:
            logging.getLogger(__name__).error(f"Failed to save VTT: {e}")
            
        # Save MD
        md_path = f"{video_id}_{lang}.md"
        try:
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(format_results([result], output_format))
            logging.getLogger(__name__).info(f"Saved MD to {md_path}")
        except Exception as e:
            logging.getLogger(__name__).error(f"Failed to save MD: {e}")

    _print_results([result], output_format)


def _safe_name(text: str, limit: int = 60) -> str:
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in text)[:limit].strip()


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


async def _run_channel(
    handle: str,
    limit: int,
    thumbnails: bool,
    subtitles: bool,
    languages: list[str] | None,
    out_dir,
) -> None:
    """List a channel's recent videos with full API metadata, and optionally
    download thumbnails and subtitle transcripts."""
    import csv
    import json
    from datetime import datetime

    import httpx

    from .readers.youtube import (
        download_thumbnail,
        fetch_video_details,
        list_channel_videos,
        read_youtube,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    # Timestamp aggregate files so re-runs never overwrite a previous snapshot.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    print(f"Listing {limit} recent videos from {handle}...")
    videos = await list_channel_videos(handle, limit=limit)
    if not videos:
        print("No videos found.")
        return
    print(f"Found {len(videos)} videos. Fetching full metadata...")

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

    if subtitles:
        subs_dir = out_dir / "subtitles"
        subs_dir.mkdir(exist_ok=True)
        # The API already told us which videos have captions — skip the rest
        # without paying for a yt-dlp round trip.
        targets = [r for r in rows if r["caption"] == "true"]
        print(f"\nFetching subtitles for {len(targets)}/{len(rows)} videos with captions to {subs_dir}...")
        for i, r in enumerate(targets, 1):
            date = (r["published_at"] or "unknown")[:10]
            out_file = subs_dir / f"{date}_{_safe_name(r['title'])}.md"
            if out_file.exists():
                print(f"  [{i}/{len(targets)}] exists, skipping {r['video_id']}")
                continue
            result = await read_youtube(r["url"], languages=languages, use_audio_fallback=False)
            if not result.success:
                print(f"  [{i}/{len(targets)}] FAILED {r['video_id']}: {result.error}")
                continue
            out_file.write_text(result.text, encoding="utf-8")
            lang = result.language or "?"
            print(f"  [{i}/{len(targets)}] ok ({lang}, {len(result.text)} chars) -> {out_file.name}")

    print("\nDone.")


async def _run_config(config_path: str, tags: list[str] | None, no_cache: bool, output_format: str) -> None:
    """Run a YAML config file."""
    from .runner import run_config

    store = ReadStore() if not no_cache else None
    results_map = await run_config(config_path, tags=tags, no_cache=no_cache, store=store)
    _print_results(flatten_results_map(results_map), output_format)


@app.command()
def read(
    url: str = typer.Argument(..., help="The URL to read."),
    format: str = typer.Option("md", help="Output format (json or md)"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Skip cache, always re-fetch"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Enable verbose logging"),
    lang: Optional[str] = typer.Option(None, help="Specific subtitle language for YouTube (e.g. 'en')"),
):
    """
    Read a single URL and output structured text.
    """
    _setup_logging(verbose)
    asyncio.run(_run_url(url, no_cache, format, lang=lang))


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
    limit: int = typer.Option(30, help="Number of most-recent videos to include."),
    thumbnails: bool = typer.Option(False, "--thumbnails", help="Also download cover images (no video)."),
    subtitles: bool = typer.Option(False, "--subtitles", help="Also fetch subtitle transcripts where available."),
    lang: Optional[str] = typer.Option(None, help="Comma-separated preferred subtitle languages (e.g. 'zh-Hant,en')."),
    out: Optional[str] = typer.Option(None, help="Output directory (default: ./output/<handle>)."),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Enable verbose logging"),
):
    """
    List a YouTube channel's recent videos into a manifest.csv (title, date,
    url, thumbnail URL) via the YouTube Data API. Add --thumbnails / --subtitles
    to also download cover images and subtitle transcripts.
    """
    from pathlib import Path

    _setup_logging(verbose)
    languages = [s.strip() for s in lang.split(",")] if lang else None
    handle_name = _safe_name(handle.rstrip("/").split("/")[-1].lstrip("@")) or "channel"
    out_dir = Path(out) if out else Path("output") / handle_name
    asyncio.run(_run_channel(handle, limit, thumbnails, subtitles, languages, out_dir))


def main():
    app()


if __name__ == "__main__":
    main()
