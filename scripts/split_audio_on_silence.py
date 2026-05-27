#!/usr/bin/env python3
"""Split an audio file into Groq-friendly chunks near silence boundaries."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class SilenceSpan:
    start: float
    end: float
    duration: float

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2


@dataclass(frozen=True)
class ChunkPlan:
    index: int
    start: float
    end: float
    extract_start: float
    extract_end: float
    hard_cut: bool
    reason: str

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def extract_duration(self) -> float:
        return self.extract_end - self.extract_start


def parse_duration(value: str) -> float:
    """Parse seconds or HH:MM:SS(.ms) duration strings."""
    value = value.strip()
    if ":" not in value:
        return float(value)

    parts = [float(part) for part in value.split(":")]
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    raise ValueError(f"Unsupported duration format: {value}")


def format_hms(seconds: float) -> str:
    rounded = int(round(seconds))
    hours = rounded // 3600
    minutes = (rounded % 3600) // 60
    secs = rounded % 60
    return f"{hours:02d}{minutes:02d}{secs:02d}"


def parse_silencedetect_output(stderr: str) -> list[SilenceSpan]:
    starts: list[float] = []
    spans: list[SilenceSpan] = []

    for line in stderr.splitlines():
        start_match = re.search(r"silence_start:\s*([0-9.]+)", line)
        if start_match:
            starts.append(float(start_match.group(1)))
            continue

        end_match = re.search(
            r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)",
            line,
        )
        if end_match and starts:
            start = starts.pop(0)
            end = float(end_match.group(1))
            duration = float(end_match.group(2))
            spans.append(SilenceSpan(start=start, end=end, duration=duration))

    return spans


def plan_chunks(
    duration: float,
    silences: list[SilenceSpan],
    target_sec: float,
    min_sec: float,
    max_sec: float,
    overlap_sec: float,
) -> list[ChunkPlan]:
    if duration <= 0:
        raise ValueError("duration must be positive")
    if min_sec <= 0 or target_sec <= 0 or max_sec <= 0:
        raise ValueError("min, target, and max durations must be positive")
    if not min_sec <= target_sec <= max_sec:
        raise ValueError("durations must satisfy min <= target <= max")

    cutpoints = sorted(span.midpoint for span in silences)
    chunks: list[ChunkPlan] = []
    start = 0.0
    index = 1

    while duration - start > max_sec:
        window_min = start + min_sec
        window_max = min(start + max_sec, duration)
        target = start + target_sec
        candidates = [cut for cut in cutpoints if window_min <= cut <= window_max]

        if candidates:
            cut = min(candidates, key=lambda value: abs(value - target))
            hard_cut = False
            reason = "silence"
        else:
            cut = window_max
            hard_cut = True
            reason = "max_duration"

        if duration - cut < min_sec:
            cut = duration

        chunks.append(
            _build_chunk(index, start, cut, duration, overlap_sec, hard_cut, reason)
        )
        start = cut
        index += 1

    if start < duration:
        chunks.append(
            _build_chunk(index, start, duration, duration, overlap_sec, False, "final")
        )

    return chunks


def _build_chunk(
    index: int,
    start: float,
    end: float,
    total_duration: float,
    overlap_sec: float,
    hard_cut: bool,
    reason: str,
) -> ChunkPlan:
    extract_start = max(0.0, start - overlap_sec)
    extract_end = min(total_duration, end + overlap_sec)
    return ChunkPlan(
        index=index,
        start=start,
        end=end,
        extract_start=extract_start,
        extract_end=extract_end,
        hard_cut=hard_cut,
        reason=reason,
    )


def probe_duration(audio_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return float(result.stdout.strip())


def detect_silences(audio_path: Path, noise: str, silence_duration: float) -> list[SilenceSpan]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(audio_path),
            "-af",
            f"silencedetect=n={noise}:d={silence_duration}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return parse_silencedetect_output(result.stderr)


def split_chunks(
    audio_path: Path,
    output_dir: Path,
    chunks: list[ChunkPlan],
    bitrate: str,
    dry_run: bool,
) -> list[dict]:
    chunks_dir = output_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    manifest_entries = []
    for chunk in chunks:
        filename = (
            f"chunk_{chunk.index:03d}_"
            f"{format_hms(chunk.start)}-{format_hms(chunk.end)}.m4a"
        )
        output_path = chunks_dir / filename
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{chunk.extract_start:.3f}",
            "-i",
            str(audio_path),
            "-t",
            f"{chunk.extract_duration:.3f}",
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            bitrate,
            str(output_path),
        ]
        if not dry_run:
            subprocess.run(command, check=True)

        entry = asdict(chunk)
        entry["file"] = str(output_path)
        entry["duration"] = chunk.duration
        entry["extract_duration"] = chunk.extract_duration
        entry["command"] = command
        manifest_entries.append(entry)

    return manifest_entries


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split audio into chunks using ffmpeg silence detection."
    )
    parser.add_argument("audio", type=Path, help="Input audio file")
    parser.add_argument("-o", "--output-dir", type=Path, required=True)
    parser.add_argument("--target", default="10:00", help="Target chunk length")
    parser.add_argument("--min", dest="minimum", default="7:00", help="Minimum chunk length")
    parser.add_argument("--max", dest="maximum", default="15:00", help="Maximum chunk length")
    parser.add_argument("--overlap", default="1.0", help="Seconds of boundary overlap")
    parser.add_argument("--noise", default="-32dB", help="silencedetect noise threshold")
    parser.add_argument("--silence-duration", type=float, default=0.35)
    parser.add_argument("--bitrate", default="48k")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    audio_path = args.audio.expanduser().resolve()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    duration = probe_duration(audio_path)
    silences = detect_silences(audio_path, args.noise, args.silence_duration)
    chunks = plan_chunks(
        duration=duration,
        silences=silences,
        target_sec=parse_duration(args.target),
        min_sec=parse_duration(args.minimum),
        max_sec=parse_duration(args.maximum),
        overlap_sec=parse_duration(args.overlap),
    )
    manifest_entries = split_chunks(
        audio_path=audio_path,
        output_dir=output_dir,
        chunks=chunks,
        bitrate=args.bitrate,
        dry_run=args.dry_run,
    )

    manifest = {
        "source": str(audio_path),
        "duration": duration,
        "silence_count": len(silences),
        "silence": {
            "noise": args.noise,
            "duration": args.silence_duration,
        },
        "chunks": manifest_entries,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"source: {audio_path}")
    print(f"duration: {duration:.1f}s")
    print(f"silences: {len(silences)}")
    print(f"chunks: {len(chunks)}")
    print(f"manifest: {manifest_path}")
    for entry in manifest_entries:
        marker = "hard" if entry["hard_cut"] else entry["reason"]
        print(
            f"{entry['index']:03d} {entry['start']:.1f}-{entry['end']:.1f}s "
            f"({entry['duration']:.1f}s, {marker}) {entry['file']}"
        )


if __name__ == "__main__":
    main()
