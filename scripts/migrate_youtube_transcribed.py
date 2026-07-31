#!/usr/bin/env python3
"""One-off: migrate legacy `output/youtube/*/transcribed/` into the archive contract.

Two legacy shapes exist on disk, both ASR output and both moved to
`transcripts/<date>_<video_id>_<title>.md` with frontmatter
(see web_reader.archive):

  transcribed/<video_id>.md          — YAML frontmatter + "# Transcript" body
  transcribed/<date>_<title>.md      — read_youtube text; VIDEO_ID: in the body

Title / publish date are backfilled from the YouTube Data API (needs
YOUTUBE_API_KEY); videos the API no longer knows fall back to whatever the old
filename carried. Originals are deleted only after the new file is verified
non-empty; empty transcribed/ dirs are removed.

Usage:
    uv run python scripts/migrate_youtube_transcribed.py           # dry run
    uv run python scripts/migrate_youtube_transcribed.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from web_reader.archive import Archive, _split_frontmatter  # noqa: E402

_VIDEO_ID_STEM = re.compile(r"^[A-Za-z0-9_-]{11}$")
_VIDEO_ID_IN_BODY = re.compile(r"^VIDEO_ID:\s*(\S+)", re.MULTILINE)
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.*)$")


def _scan(archive: Archive) -> list[dict]:
    """All legacy files with what we can learn without the API."""
    entries = []
    for path in sorted((archive.root / "youtube").glob("*/transcribed/*.md")):
        raw = path.read_text(encoding="utf-8", errors="ignore")
        meta, body = _split_frontmatter(raw)

        if _VIDEO_ID_STEM.match(path.stem):
            video_id = path.stem
        else:
            m = _VIDEO_ID_IN_BODY.search(body)
            if not m:
                print(f"  SKIP (no video id found): {path}")
                continue
            video_id = m.group(1)

        fallback_date, fallback_title = None, path.stem
        dm = _DATE_PREFIX.match(path.stem)
        if dm:
            fallback_date, fallback_title = dm.group(1), dm.group(2)

        entries.append({
            "path": path,
            "channel": path.parent.parent.name,
            "video_id": video_id,
            "body": body.strip(),
            "old_meta": meta,
            "fallback_date": fallback_date,
            "fallback_title": fallback_title,
        })
    return entries


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="actually move files (default: dry run)")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env", override=False)
    archive = Archive()
    entries = _scan(archive)
    if not entries:
        print("Nothing to migrate.")
        return 0
    print(f"{len(entries)} file(s) in legacy transcribed/ layout.")

    from web_reader.readers.youtube import fetch_video_details
    details = await fetch_video_details(sorted({e["video_id"] for e in entries}))
    print(f"YouTube API resolved {len(details)}/{len(entries)} video(s).\n")

    n_moved = 0
    for e in entries:
        snippet = details.get(e["video_id"], {}).get("snippet", {})
        title = snippet.get("title") or e["fallback_title"]
        published_raw = snippet.get("publishedAt") or (
            f"{e['fallback_date']}T00:00:00Z" if e["fallback_date"] else None
        )
        published = (
            datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
            if published_raw else None
        )

        if not args.apply:
            date = published.date().isoformat() if published else "unknown"
            print(f"  {e['channel']}/transcribed/{e['path'].name}")
            print(f"    → transcripts/{date}_{e['video_id']}_{title[:40]}…")
            continue

        new_path = archive.save_youtube(
            e["channel"], "transcripts", e["video_id"], title, published,
            e["body"], method="sensevoice",
            extra={"migrated_from": f"transcribed/{e['path'].name}", **e["old_meta"]},
        )
        # Only delete the original once the replacement is verifiably readable.
        check = archive.find_youtube(e["video_id"])
        if check is None or not check.text:
            print(f"  ✗ verification failed, keeping original: {e['path']}")
            continue
        e["path"].unlink()
        n_moved += 1
        print(f"  ✓ {e['channel']}: {new_path.name}")

    if args.apply:
        for d in (archive.root / "youtube").glob("*/transcribed"):
            if not any(d.iterdir()):
                d.rmdir()
                print(f"  removed empty {d.parent.name}/transcribed/")
        print(f"\nMoved {n_moved}/{len(entries)}.")
    else:
        print("\nDry run — rerun with --apply to move.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
