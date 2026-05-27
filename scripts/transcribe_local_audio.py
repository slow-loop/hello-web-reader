#!/usr/bin/env python3
"""Batch-transcribe local audio files via the web_reader two-stage pipeline.

Stage 1: local SenseVoice ASR (FunASR).
Stage 2: OpenRouter LLM polish (default `deepseek/deepseek-v4-flash`).

Output: one `<stem>.md` per audio file, containing YAML frontmatter, the
polished transcript, and the raw ASR in a collapsible `<details>` block.

Usage:
    .venv/bin/python scripts/transcribe_local_audio.py output/stockchatu/audio \
        --out output/stockchatu/transcripts

Fails fast on the first error — the user can fix and rerun (existing outputs
will be overwritten by default; pass --skip-existing to resume).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Make `src/` importable without requiring an editable install.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from web_reader.transcribe import asr, refine  # noqa: E402

DEFAULT_CONTEXT = (
    "Mandarin financial podcast about US stocks (channel: stockchatu). "
    "Speakers frequently mix English ticker symbols and macro terms — "
    "NVIDIA, TSLA, AAPL, Fed, FOMC, ETF, options, taper, tapering — "
    "into otherwise Mandarin sentences."
)

DEFAULT_EXTS = "mp3,m4a,wav,opus"


def _frontmatter(
    audio_path: Path,
    refine_model: str,
    context: str,
    asr_s: float,
    refine_s: float,
    raw_chars: int,
    polished_chars: int,
) -> str:
    # Escape colons / quotes in source filename for YAML safety.
    safe_name = audio_path.name.replace('"', '\\"')
    return (
        "---\n"
        f'source: "{safe_name}"\n'
        "asr_model: iic/SenseVoiceSmall\n"
        f"refine_model: {refine_model}\n"
        f"asr_seconds: {asr_s:.1f}\n"
        f"refine_seconds: {refine_s:.1f}\n"
        f"raw_chars: {raw_chars}\n"
        f"polished_chars: {polished_chars}\n"
        f'context: "{context}"\n'
        "---\n"
    )


def _build_markdown(
    audio_path: Path,
    raw: str,
    polished: str,
    refine_model: str,
    context: str,
    asr_s: float,
    refine_s: float,
) -> str:
    fm = _frontmatter(
        audio_path=audio_path,
        refine_model=refine_model,
        context=context,
        asr_s=asr_s,
        refine_s=refine_s,
        raw_chars=len(raw),
        polished_chars=len(polished),
    )
    return (
        f"{fm}\n"
        "# Transcript\n\n"
        f"{polished}\n\n"
        "<details>\n"
        "<summary>Raw ASR output (pre-LLM)</summary>\n\n"
        "```\n"
        f"{raw}\n"
        "```\n\n"
        "</details>\n"
    )


def _looks_like_real_transcript(out_path: Path) -> bool:
    """A previous run's error stub starts with `## ERROR:` — treat anything
    else with frontmatter or a non-trivial body as real output."""
    if not out_path.exists():
        return False
    head = out_path.read_text(encoding="utf-8", errors="ignore")[:400]
    if head.startswith("## ERROR:"):
        return False
    return head.lstrip().startswith("---") or len(head) > 200


def transcribe_file(
    audio_path: Path,
    out_path: Path,
    context: str,
    refine_model: str,
) -> tuple[float, float, int, int]:
    t0 = time.time()
    raw = asr.transcribe(audio_path)
    asr_s = time.time() - t0

    t0 = time.time()
    polished = refine.refine(raw, context=context)
    refine_s = time.time() - t0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _build_markdown(
            audio_path=audio_path,
            raw=raw,
            polished=polished,
            refine_model=refine_model,
            context=context,
            asr_s=asr_s,
            refine_s=refine_s,
        ),
        encoding="utf-8",
    )
    return asr_s, refine_s, len(raw), len(polished)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio_dir", type=Path, help="Directory containing audio files")
    parser.add_argument("--out", type=Path, required=True, help="Output directory for .md transcripts")
    parser.add_argument("--context", default=DEFAULT_CONTEXT, help="Domain hint passed to the refine LLM")
    parser.add_argument(
        "--ext",
        default=DEFAULT_EXTS,
        help=f"Comma-separated extensions to include (default: {DEFAULT_EXTS})",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip files whose output already exists with non-error content (resume mode)",
    )
    args = parser.parse_args()

    if not args.audio_dir.is_dir():
        print(f"audio_dir does not exist or is not a directory: {args.audio_dir}", file=sys.stderr)
        return 2

    exts = {f".{e.strip().lower()}" for e in args.ext.split(",") if e.strip()}
    audio_files = sorted(p for p in args.audio_dir.iterdir() if p.suffix.lower() in exts)
    if not audio_files:
        print(f"No audio files with extensions {sorted(exts)} found in {args.audio_dir}", file=sys.stderr)
        return 1

    refine_model = os.environ.get("OPENROUTER_MODEL", refine.DEFAULT_MODEL)
    print(f"Found {len(audio_files)} audio file(s) in {args.audio_dir}")
    print(f"Out: {args.out}")
    print(f"Refine model: {refine_model}")
    print(f"Context: {args.context[:80]}{'...' if len(args.context) > 80 else ''}")
    print()

    total_audio_s = 0.0
    total_refine_s = 0.0
    written = 0
    skipped = 0
    overall_t0 = time.time()

    for idx, audio_path in enumerate(audio_files, start=1):
        out_path = args.out / f"{audio_path.stem}.md"
        prefix = f"[{idx:3d}/{len(audio_files)}] {audio_path.name}"

        if args.skip_existing and _looks_like_real_transcript(out_path):
            print(f"{prefix}  -> skip (exists)")
            skipped += 1
            continue

        print(prefix, flush=True)
        # Fail-fast: any exception propagates (per user's debug preference).
        asr_s, refine_s, raw_chars, polished_chars = transcribe_file(
            audio_path=audio_path,
            out_path=out_path,
            context=args.context,
            refine_model=refine_model,
        )
        total_audio_s += asr_s
        total_refine_s += refine_s
        written += 1
        elapsed = time.time() - overall_t0
        print(
            f"           asr {asr_s:5.1f}s  refine {refine_s:5.1f}s  "
            f"raw {raw_chars:5d}->polished {polished_chars:5d}  "
            f"(elapsed {elapsed / 60:.1f} min)",
            flush=True,
        )

    elapsed = time.time() - overall_t0
    print()
    print(
        f"=== Done. wrote {written}, skipped {skipped} in {elapsed / 60:.1f} min "
        f"(asr {total_audio_s / 60:.1f} min + refine {total_refine_s / 60:.1f} min) ==="
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
