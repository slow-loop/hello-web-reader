"""
Fetch recent videos + transcripts from a YouTube channel.

Usage:
    uv run scripts/fetch_youtube_channel.py --handle @example
    uv run scripts/fetch_youtube_channel.py --handle @example --limit 3
    uv run scripts/fetch_youtube_channel.py --handle https://www.youtube.com/@example --limit 5 --out ./output

Requires:
    YOUTUBE_API_KEY   — list channel videos
    GROQ_API_KEY      — Whisper fallback when no subtitles available
"""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from web_reader.readers.youtube import list_channel_videos, read_youtube


async def fetch(handle: str, limit: int, languages: list[str], out_dir: Path | None) -> None:
    print(f"Listing {limit} recent videos from {handle}...")
    videos = await list_channel_videos(handle, limit=limit)

    if not videos:
        print("No videos found.")
        return

    for i, v in enumerate(videos, 1):
        print(f"\n[{i}/{len(videos)}] {v['title']}")
        print(f"  URL: {v['url']}")
        print(f"  Published: {v['published_at']}")
        print("  Fetching transcript...")

        result = await read_youtube(v["url"], languages=languages)

        if not result.success:
            print(f"  ERROR: {result.error}")
            continue

        method = result.raw.get("method", "?") if result.raw else "?"
        lang = result.language or "?"
        print(f"  OK ({method}, lang={lang}, {len(result.text)} chars)")

        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)
            date = v["published_at"][:10] if v["published_at"] else "unknown"
            safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in v["title"])[:60].strip()
            filename = f"{date}_{safe_title}.md"
            (out_dir / filename).write_text(result.text, encoding="utf-8")
            print(f"  Saved: {out_dir / filename}")
        else:
            print()
            print(result.text[:2000])
            if len(result.text) > 2000:
                print(f"... ({len(result.text) - 2000} more chars)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch YouTube channel transcripts")
    parser.add_argument("--handle", required=True, help="YouTube channel handle (e.g. @example) or full channel URL")
    parser.add_argument("--limit", type=int, default=5, help="Number of videos to fetch (default: 5)")
    parser.add_argument("--lang", nargs="+", default=["zh", "zh-Hant", "en"], help="Preferred languages")
    parser.add_argument("--out", type=Path, default=None, help="Output directory (default: print to stdout)")
    args = parser.parse_args()

    asyncio.run(fetch(args.handle, args.limit, args.lang, args.out))


if __name__ == "__main__":
    main()
