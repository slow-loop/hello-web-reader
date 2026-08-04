#!/usr/bin/env python
"""One-off: file the loose `<video_id>_<lang>.md/.vtt` dumps into the store.

Until this commit, `web-reader read <youtube-url>` wrote a bare
`<video_id>_<lang>.vtt` + `.md` pair into the *current working directory* on
top of whatever else it did (cli.py, pre-`output/`-contract leftover). Since
the documented way to fetch one video's transcript is to cd into this repo
first, the pair landed in the repo root, and years of ad-hoc research piled up
there — outside the archive, invisible to `has_youtube()`, re-fetched every
time.

This moves them where `fetch`/`channel` would have put them:

    output/youtube/<handle>/subtitles/<YYYY-MM-DD>_<video_id>_<title>.md

Both file kinds are handled. A `.md` carries the extracted text already; a
`.vtt` with no `.md` beside it (the `.md` was moved away by hand at some
point) is converted with the same `_vtt_to_text` the reader uses, so those
transcripts are recovered rather than dropped.

Everything here came from the yt-dlp subtitle path — the loose write only ever
fired when `raw["vtt"]` was set, which no ASR result has — so all of it files
as `subtitles` / `method: yt-dlp-subs`.

Channel, title and publish date are not in the dumps; they come from the
YouTube Data API (`videos.list` for title/date/channel id, `channels.list` for
the `@handle` that names the store folder). A video whose channel cannot be
resolved is left alone rather than filed under a guessed name.

Originals are moved to tmp/, never deleted — verify, then remove by hand.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from web_reader.readers.youtube import (  # noqa: E402
    _build_markdown,
    _vtt_to_text,
    fetch_video_details,
)
from web_reader.store import Store, safe_name  # noqa: E402

# YouTube ids are exactly 11 chars and may start with `-` or `_`, so slice by
# width instead of splitting on the separator.
_ID_LEN = 11
_DUMP_NAME = re.compile(rf"^(.{{{_ID_LEN}}})_(.+)$")


def _parse_dump_name(path: Path) -> tuple[str, str] | None:
    """`<video_id>_<lang>.md` -> (video_id, lang)."""
    m = _DUMP_NAME.match(path.stem)
    return (m.group(1), m.group(2)) if m else None


def _body_from_md(text: str) -> str | None:
    """Strip the `format_results` header, keep the store's own body shape."""
    idx = text.find("VIDEO_ID: ")
    return text[idx:].strip() if idx != -1 else None


def _collect(root: Path) -> list[dict]:
    """One entry per video id, preferring the .md over a bare .vtt."""
    by_id: dict[str, dict] = {}

    for path in sorted(root.glob("*.md")):
        parsed = _parse_dump_name(path)
        if not parsed:
            continue
        video_id, lang = parsed
        body = _body_from_md(path.read_text(encoding="utf-8", errors="ignore"))
        if not body:
            continue
        by_id[video_id] = {"video_id": video_id, "lang": lang, "text": body, "sources": [path]}

    for path in sorted(root.glob("*.vtt")):
        parsed = _parse_dump_name(path)
        if not parsed:
            continue
        video_id, lang = parsed
        if video_id in by_id:
            by_id[video_id]["sources"].append(path)
            continue
        # No .md beside it — this vtt is the only copy of the transcript.
        text = _vtt_to_text(path.read_text(encoding="utf-8", errors="ignore"))
        if not text.strip():
            continue
        by_id[video_id] = {
            "video_id": video_id,
            "lang": lang,
            "text": _build_markdown(video_id, text, lang),
            "sources": [path],
            "from_vtt": True,
        }

    return list(by_id.values())


def _fetch_handles(channel_ids: list[str], api_key: str) -> dict[str, str]:
    """channel id -> store folder name, from the channel's `@handle`."""
    handles: dict[str, str] = {}
    with httpx.Client(timeout=30) as client:
        for i in range(0, len(channel_ids), 50):
            resp = client.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "snippet", "id": ",".join(channel_ids[i : i + 50]), "key": api_key},
            )
            resp.raise_for_status()
            for item in resp.json().get("items", []):
                custom = (item.get("snippet", {}).get("customUrl") or "").lstrip("@")
                if custom:
                    handles[item["id"]] = safe_name(custom)
    return handles


def main() -> None:
    import asyncio
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually write/move (default: dry run)")
    parser.add_argument("--root", default=str(_REPO_ROOT), help="directory holding the loose dumps")
    args = parser.parse_args()

    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        from dotenv import load_dotenv

        load_dotenv(_REPO_ROOT / ".env")
        api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise SystemExit("YOUTUBE_API_KEY not set (needed for title / date / channel)")

    store = Store()
    entries = _collect(Path(args.root))
    if not entries:
        print("No loose transcript dumps found.")
        return

    details = asyncio.run(fetch_video_details([e["video_id"] for e in entries]))
    channel_ids = sorted(
        {details[e["video_id"]]["snippet"]["channelId"] for e in entries if e["video_id"] in details}
    )
    handles = _fetch_handles(channel_ids, api_key)

    n_new = n_existing = n_unresolved = 0
    dump_dir = _REPO_ROOT / "tmp" / f"root-transcript-dump-{datetime.now():%Y%m%d-%H%M%S}"
    verb = "would file" if not args.apply else "filed"

    for e in entries:
        vid = e["video_id"]
        origin = ", ".join(p.name for p in e["sources"])

        existing = store.find_youtube(vid)
        if existing:
            print(f"  = {vid} already archived -> {existing.path.relative_to(store.root)}")
            n_existing += 1
            if args.apply:
                dump_dir.mkdir(parents=True, exist_ok=True)
                for p in e["sources"]:
                    shutil.move(str(p), dump_dir / p.name)
            continue

        item = details.get(vid)
        handle = handles.get(item["snippet"]["channelId"]) if item else None
        if not item or not handle:
            print(f"  ? {vid} channel unresolved ({origin}) — left in place")
            n_unresolved += 1
            continue

        snippet = item["snippet"]
        published = datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00"))
        note = " (from vtt)" if e.get("from_vtt") else ""

        if args.apply:
            path = store.save_youtube(
                handle,
                "subtitles",
                vid,
                snippet["title"],
                published,
                e["text"],
                language=e["lang"],
                method="yt-dlp-subs",
                extra={"migrated_from": origin},
            )
            dump_dir.mkdir(parents=True, exist_ok=True)
            for p in e["sources"]:
                shutil.move(str(p), dump_dir / p.name)
            print(f"  + {vid}{note} -> {path.relative_to(store.root)}")
        else:
            print(f"  + {vid}{note} -> youtube/{handle}/subtitles/"
                  f"{published:%Y-%m-%d}_{vid}_{safe_name(snippet['title'])}.md")
        n_new += 1

    print(f"\n{verb} {n_new}, already archived {n_existing}, unresolved {n_unresolved}")
    if args.apply and dump_dir.exists():
        print(f"Originals moved to {dump_dir.relative_to(_REPO_ROOT)} — delete once verified.")


if __name__ == "__main__":
    main()
