#!/usr/bin/env python3
"""OCR a Rednote note's image cards via the on-device Apple Vision framework.

Some Rednote notes carry their whole article as a stack of image cards
(a title/hook card + N body cards) instead of real post text — `note.desc`
comes back empty even though the note clearly has content. This reads those
images back into text so the rest of the pipeline (clippings, scripting)
has something to work with.

Images are ordered by the numeric suffix in the filename
(`<note-id>_<n>.<ext>`), not by directory listing order — `opencli_rednote_liked.js`'s
fallback download path names files off an alphabetical sort, so `_10` sorts
before `_2` there. The numeric suffix is always the true reading order
(assigned during `note.imageList` iteration at fetch time).

Usage:
    uv run scripts/rednote_ocr.py 6a92e21a000000000302bedc
    uv run scripts/rednote_ocr.py 6a92e21a000000000302bedc --print
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from ocrmac import ocrmac

BASE_DIR = Path(__file__).resolve().parent.parent / "output" / "rednote" / "liked"
IMAGE_RE = re.compile(r"^(?P<id>.+)_(?P<n>\d+)\.(?:jpe?g|png|webp)$", re.IGNORECASE)
LANGUAGES = ["zh-Hans", "zh-Hant", "en-US"]


def ordered_images(note_dir: Path, note_id: str) -> list[Path]:
    matches = []
    for path in note_dir.iterdir():
        m = IMAGE_RE.match(path.name)
        if m and m.group("id") == note_id:
            matches.append((int(m.group("n")), path))
    matches.sort(key=lambda pair: pair[0])
    return [path for _, path in matches]


def ocr_image(path: Path) -> str:
    results = ocrmac.OCR(str(path), language_preference=LANGUAGES).recognize()
    return "\n".join(text for text, _confidence, _box in results)


def run(note_id: str) -> Path:
    note_dir = BASE_DIR / note_id
    if not note_dir.is_dir():
        raise SystemExit(f"no such note dir: {note_dir}")

    images = ordered_images(note_dir, note_id)
    if not images:
        raise SystemExit(f"no images matching {note_id}_<n>.<ext> in {note_dir}")

    sections = []
    for path in images:
        text = ocr_image(path)
        if text:
            sections.append(text)

    out_path = note_dir / "ocr.md"
    out_path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("note_id", help="Rednote note id (the folder name under output/rednote/liked/)")
    parser.add_argument("--print", dest="print_output", action="store_true", help="also print the result to stdout")
    args = parser.parse_args()

    out_path = run(args.note_id)
    text = out_path.read_text(encoding="utf-8")
    print(f"{out_path}  ({len(text)} chars)")
    if args.print_output:
        print(text)


if __name__ == "__main__":
    main()
