#!/usr/bin/env python3
"""One-off: export kol_mvp's podcast transcript cache into the output/ archive.

Reads rss_podcast rows from hello-note's `.cache/transcripts.db` (a ReadStore
SQLite file) and writes each as `output/podcast/<source_id>/<date>_<guid>.md`
via web_reader.archive. The source id is recovered from the cached audio path
(`podcast_audio/<source_id>/…`). The db itself is left untouched — retire it
once nothing reads it anymore.

Usage:
    uv run python scripts/migrate_kol_transcripts_db.py           # dry run
    uv run python scripts/migrate_kol_transcripts_db.py --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from web_reader.archive import Archive  # noqa: E402

DEFAULT_DB = REPO_ROOT.parent / "hello-note" / "scripts" / "kol_mvp" / ".cache" / "transcripts.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--apply", action="store_true", help="actually write files (default: dry run)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"db not found: {db_path}")
        return 2

    rows = sqlite3.connect(db_path).execute(
        "select url, title, published_at, payload, created_at from read_records "
        "where source_type='rss_podcast' and success=1 order by created_at"
    ).fetchall()

    # Latest write per url wins (the cache may hold retries).
    by_url = {r[0]: r for r in rows}
    print(f"{len(rows)} row(s), {len(by_url)} unique episode(s).")

    archive = Archive()
    n_new = n_existing = n_skipped = 0
    per_source: dict[str, int] = {}

    for url, title, published_raw, payload_raw, _ in by_url.values():
        payload = json.loads(payload_raw) if payload_raw else {}
        raw = payload.get("raw") or {}
        guid = raw.get("guid") or url.removeprefix("podcast://")
        text = (payload.get("text") or "").strip()
        if not text:
            n_skipped += 1
            continue

        audio_file = raw.get("audio_file")
        if audio_file:
            source_id = Path(audio_file).parent.name
        else:
            # One row predates raw/audio_file being recorded; its feed is
            # unambiguous from the "<podcast> — <episode>" title prefix.
            fallback = {"MacroMicro 財經M平方": "podcast-macromicro"}
            podcast = raw.get("podcast_title") or (title or "").split(" — ")[0]
            source_id = fallback.get(podcast)
            if not source_id:
                print(f"  SKIP (no audio path → unknown source): {title[:60]}")
                n_skipped += 1
                continue

        if archive.has_podcast(guid):
            n_existing += 1
            continue

        published = None
        if published_raw:
            published = datetime.fromisoformat(published_raw).replace(tzinfo=timezone.utc)

        per_source[source_id] = per_source.get(source_id, 0) + 1
        if args.apply:
            archive.save_podcast(
                source_id, guid, raw.get("episode_title") or title, published, text,
                webpage_url=raw.get("webpage_url"),
                author=raw.get("podcast_title"),
                method="sensevoice",
            )
        n_new += 1

    verb = "exported" if args.apply else "would export"
    print(f"\n{verb} {n_new}, already archived {n_existing}, skipped {n_skipped}")
    for sid, n in sorted(per_source.items()):
        print(f"  {sid}: {n}")
    if not args.apply:
        print("\nDry run — rerun with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
