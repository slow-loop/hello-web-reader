#!/usr/bin/env python3
"""One-off: migrate legacy YouTube layouts into the store contract.

Three legacy shapes exist(ed) on disk, all moved to
`{subtitles,transcripts}/<date>_<video_id>_<title>.md` with frontmatter
(see web_reader.store):

  transcribed/<video_id>.md          — YAML frontmatter + "# Transcript" body → transcripts/
  transcribed/<date>_<title>.md      — read_youtube text; VIDEO_ID: in body   → transcripts/
  <channel>/<date>_<title>.md        — root-level read_youtube text; kind
                                       decided by its LANG: header
                                       (ai-transcribed → transcripts/,
                                       anything else → subtitles/)

Root-level .md files without a VIDEO_ID header (hand-written notes, drafts)
are not video transcripts and are left in place. Videos already archived in
the canonical layout are skipped so no duplicates are created.

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

from web_reader.store import Store, _split_frontmatter  # noqa: E402

_VIDEO_ID_STEM = re.compile(r"^[A-Za-z0-9_-]{11}$")
_VIDEO_ID_IN_BODY = re.compile(r"^VIDEO_ID:\s*(\S+)", re.MULTILINE)
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.*)$")


_ASR_LANG = re.compile(r"^LANG:\s*ai-transcribed\s*$", re.MULTILINE)


def _scan(store: Store) -> list[dict]:
    """All legacy files with what we can learn without the API."""
    yt = store.root / "youtube"
    legacy = [(p, "transcripts") for p in sorted(yt.glob("*/transcribed/*.md"))]
    # Root-level files: ASR output goes to transcripts/, caption fetches to
    # subtitles/ — the LANG: header read_youtube wrote tells them apart.
    legacy += [(p, None) for p in sorted(yt.glob("*/*.md"))]

    entries = []
    for path, kind in legacy:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        meta, body = _split_frontmatter(raw)

        if _VIDEO_ID_STEM.match(path.stem):
            video_id = path.stem
        else:
            m = _VIDEO_ID_IN_BODY.search(body)
            if not m:
                print(f"  SKIP (no video id — not a transcript?): {path}")
                continue
            video_id = m.group(1)

        if store.has_youtube(video_id):
            print(f"  SKIP (already in canonical layout): {path}")
            continue

        fallback_date, fallback_title = None, path.stem
        dm = _DATE_PREFIX.match(path.stem)
        if dm:
            fallback_date, fallback_title = dm.group(1), dm.group(2)

        entries.append({
            "path": path,
            "channel": path.parent.parent.name if kind else path.parent.name,
            "kind": kind or ("transcripts" if _ASR_LANG.search(body) else "subtitles"),
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
    store = Store()
    entries = _scan(store)
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

        origin = str(e["path"].relative_to(store.root / "youtube" / e["channel"]))
        if not args.apply:
            date = published.date().isoformat() if published else "unknown"
            print(f"  {e['channel']}/{origin}")
            print(f"    → {e['kind']}/{date}_{e['video_id']}_{title[:40]}…")
            continue

        new_path = store.save_youtube(
            e["channel"], e["kind"], e["video_id"], title, published,
            e["body"],
            method="sensevoice" if e["kind"] == "transcripts" else "yt-dlp-subs",
            extra={"migrated_from": origin, **e["old_meta"]},
        )
        # Only delete the original once the replacement is verifiably readable.
        check = store.find_youtube(e["video_id"])
        if check is None or not check.text:
            print(f"  ✗ verification failed, keeping original: {e['path']}")
            continue
        e["path"].unlink()
        n_moved += 1
        print(f"  ✓ {e['channel']}: {new_path.name}")

    if args.apply:
        for d in (store.root / "youtube").glob("*/transcribed"):
            if not any(d.iterdir()):
                d.rmdir()
                print(f"  removed empty {d.parent.name}/transcribed/")
        print(f"\nMoved {n_moved}/{len(entries)}.")
    else:
        print("\nDry run — rerun with --apply to move.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
