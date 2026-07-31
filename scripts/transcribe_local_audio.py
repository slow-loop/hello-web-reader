#!/usr/bin/env python3
"""Batch-transcribe local audio files via the web_reader two-stage pipeline.

Stage 1: local SenseVoice ASR (FunASR). Optionally pre-split with
`split_audio_on_silence` when a file exceeds the duration/size threshold,
to keep peak memory bounded (funasr/FunASR Issue #2116 — memory scales
badly with single-file length; macOS jetsam can silently kill the process).

Stage 2: OpenRouter LLM polish (default `deepseek/deepseek-v4-flash`).

Output: one `<stem>.md` per audio file with YAML frontmatter, the polished
transcript, and the raw ASR (pre-LLM) in a collapsible `<details>` block.

Usage:
    .venv/bin/python scripts/transcribe_local_audio.py output/stockchatu/audio \
        --out output/stockchatu/transcripts \
        --skip-existing

Fails fast on the first error.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

# Make `src/` importable without requiring an editable install, and let us
# pull helpers from sibling scripts in this directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import split_audio_on_silence as splitter  # noqa: E402
from web_reader.transcribe import asr, refine  # noqa: E402


# Default vocabulary terms for the stockchatu channel. Override with
# --vocabulary "TERM1,TERM2,..." on the command line.
DEFAULT_VOCABULARY = [
    "NVIDIA", "TSLA", "AAPL", "MSFT", "AMZN", "GOOGL", "META",
    "Fed", "FOMC", "ETF", "GPU", "AI",
    "taper", "tapering", "tantrum",
    "stockchatu",
]

DEFAULT_EXTS = "mp3,m4a,wav,opus"

# Files exceeding either threshold get split with split_audio_on_silence
# before ASR. Defaults chosen so a ~22 min mp3 still runs in one shot
# (already validated end-to-end), while the 2h compilation gets chunked.
DEFAULT_MAX_DURATION_SEC = 25 * 60   # 25 minutes
DEFAULT_MAX_SIZE_MB = 50

# Chunking parameters when we *do* split — match split_audio_on_silence's CLI defaults.
_CHUNK_TARGET_SEC = 10 * 60   # 10 min
_CHUNK_MIN_SEC = 7 * 60       # 7 min
_CHUNK_MAX_SEC = 15 * 60      # 15 min
_CHUNK_OVERLAP_SEC = 1.0
_CHUNK_NOISE = "-32dB"
_CHUNK_SILENCE_DURATION = 0.35
_CHUNK_BITRATE = "48k"


def _frontmatter(
    audio_path: Path,
    refine_model: str,
    vocabulary: list[str],
    asr_s: float,
    refine_s: float,
    raw_chars: int,
    polished_chars: int,
    chunk_count: int | None,
    duration_sec: float | None,
) -> str:
    safe_name = audio_path.name.replace('"', '\\"')
    lines = [
        "---",
        f'source: "{safe_name}"',
        "asr_model: iic/SenseVoiceSmall",
        f"refine_model: {refine_model}",
        f"asr_seconds: {asr_s:.1f}",
        f"refine_seconds: {refine_s:.1f}",
        f"raw_chars: {raw_chars}",
        f"polished_chars: {polished_chars}",
    ]
    if duration_sec is not None:
        lines.append(f"audio_duration_seconds: {duration_sec:.1f}")
    if chunk_count is not None:
        lines.append(f"asr_chunks: {chunk_count}")
    if vocabulary:
        joined = ", ".join(vocabulary)
        lines.append(f'vocabulary: "{joined}"')
    lines.append("---")
    return "\n".join(lines) + "\n"


def _build_markdown(
    audio_path: Path,
    raw: str,
    polished: str,
    refine_model: str,
    vocabulary: list[str],
    asr_s: float,
    refine_s: float,
    chunk_count: int | None,
    duration_sec: float | None,
) -> str:
    fm = _frontmatter(
        audio_path=audio_path,
        refine_model=refine_model,
        vocabulary=vocabulary,
        asr_s=asr_s,
        refine_s=refine_s,
        raw_chars=len(raw),
        polished_chars=len(polished),
        chunk_count=chunk_count,
        duration_sec=duration_sec,
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
    """A previous run's error stub starts with `## ERROR:`."""
    if not out_path.exists():
        return False
    head = out_path.read_text(encoding="utf-8", errors="ignore")[:400]
    if head.startswith("## ERROR:"):
        return False
    return head.lstrip().startswith("---") or len(head) > 200


def _needs_chunking(audio_path: Path, max_duration_sec: float, max_size_mb: float) -> tuple[bool, float]:
    """Return (needs_chunking, duration_seconds). Always probes duration."""
    duration = splitter.probe_duration(audio_path)
    size_mb = audio_path.stat().st_size / 1024 / 1024
    return (duration > max_duration_sec or size_mb > max_size_mb), duration


def _asr_chunked(audio_path: Path, duration: float) -> tuple[str, int]:
    """Split into silence-aware chunks in a temp dir, ASR each, return concatenated raw text."""
    silences = splitter.detect_silences(audio_path, _CHUNK_NOISE, _CHUNK_SILENCE_DURATION)
    plan = splitter.plan_chunks(
        duration=duration,
        silences=silences,
        target_sec=_CHUNK_TARGET_SEC,
        min_sec=_CHUNK_MIN_SEC,
        max_sec=_CHUNK_MAX_SEC,
        overlap_sec=_CHUNK_OVERLAP_SEC,
    )

    raw_parts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="transcribe-chunks-") as tmp:
        manifest_entries = splitter.split_chunks(
            audio_path=audio_path,
            output_dir=Path(tmp),
            chunks=plan,
            bitrate=_CHUNK_BITRATE,
            dry_run=False,
        )
        for i, entry in enumerate(manifest_entries, start=1):
            chunk_file = Path(entry["file"])
            print(
                f"           chunk {i:2d}/{len(manifest_entries)}  "
                f"{entry['start']:.0f}-{entry['end']:.0f}s  ({entry['duration']:.0f}s)",
                flush=True,
            )
            raw_parts.append(asr.transcribe(chunk_file))

    return "\n".join(part.strip() for part in raw_parts if part.strip()), len(manifest_entries)


def transcribe_file(
    audio_path: Path,
    out_path: Path,
    vocabulary: list[str],
    refine_model: str,
    max_duration_sec: float,
    max_size_mb: float,
) -> tuple[float, float, int, int, int | None, float]:
    chunked, duration = _needs_chunking(audio_path, max_duration_sec, max_size_mb)

    t0 = time.time()
    if chunked:
        size_mb = audio_path.stat().st_size / 1024 / 1024
        print(
            f"           chunking ({duration:.0f}s / {size_mb:.0f} MB > "
            f"{max_duration_sec / 60:.0f} min / {max_size_mb:.0f} MB)",
            flush=True,
        )
        raw, chunk_count = _asr_chunked(audio_path, duration)
    else:
        raw = asr.transcribe(audio_path)
        chunk_count = None
    asr_s = time.time() - t0

    t0 = time.time()
    polished = refine.refine(raw, vocabulary_terms=vocabulary)
    refine_s = time.time() - t0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _build_markdown(
            audio_path=audio_path,
            raw=raw,
            polished=polished,
            refine_model=refine_model,
            vocabulary=vocabulary,
            asr_s=asr_s,
            refine_s=refine_s,
            chunk_count=chunk_count,
            duration_sec=duration,
        ),
        encoding="utf-8",
    )
    return asr_s, refine_s, len(raw), len(polished), chunk_count, duration


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio_dir", type=Path, help="Directory containing audio files")
    parser.add_argument("--out", type=Path, required=True, help="Output directory for .md transcripts")
    parser.add_argument(
        "--vocabulary",
        default=",".join(DEFAULT_VOCABULARY),
        help="Comma-separated list of proper nouns / terms ASR often mishears",
    )
    parser.add_argument(
        "--ext",
        default=DEFAULT_EXTS,
        help=f"Comma-separated extensions to include (default: {DEFAULT_EXTS})",
    )
    parser.add_argument(
        "--max-duration-min",
        type=float,
        default=DEFAULT_MAX_DURATION_SEC / 60,
        help=f"Pre-split files longer than this many minutes (default: {DEFAULT_MAX_DURATION_SEC / 60:.0f})",
    )
    parser.add_argument(
        "--max-size-mb",
        type=float,
        default=DEFAULT_MAX_SIZE_MB,
        help=f"Pre-split files larger than this many MB (default: {DEFAULT_MAX_SIZE_MB:.0f})",
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

    vocabulary = [t.strip() for t in args.vocabulary.split(",") if t.strip()]
    refine_model = os.environ.get("OPENROUTER_MODEL", refine.DEFAULT_MODEL)
    max_duration_sec = args.max_duration_min * 60.0

    print(f"Found {len(audio_files)} audio file(s) in {args.audio_dir}")
    print(f"Out: {args.out}")
    print(f"Refine model: {refine_model}")
    print(f"Vocabulary ({len(vocabulary)} terms): {', '.join(vocabulary[:8])}{'...' if len(vocabulary) > 8 else ''}")
    print(f"Chunk threshold: {args.max_duration_min:.0f} min  /  {args.max_size_mb:.0f} MB")
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
        asr_s, refine_s, raw_chars, polished_chars, chunk_count, _duration = transcribe_file(
            audio_path=audio_path,
            out_path=out_path,
            vocabulary=vocabulary,
            refine_model=refine_model,
            max_duration_sec=max_duration_sec,
            max_size_mb=args.max_size_mb,
        )
        total_audio_s += asr_s
        total_refine_s += refine_s
        written += 1
        elapsed = time.time() - overall_t0
        chunk_note = f"  ({chunk_count} chunks)" if chunk_count else ""
        print(
            f"           asr {asr_s:5.1f}s  refine {refine_s:5.1f}s  "
            f"raw {raw_chars:5d}->polished {polished_chars:5d}{chunk_note}  "
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
