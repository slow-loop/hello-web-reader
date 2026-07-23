#!/usr/bin/env python3
"""Extract shot-change keyframes from a video and tile them into a grid image.

Keyframes here means shot/scene-boundary frames (ffmpeg scene-change
detection), not codec I-frames or animation keyframes. The grid lets an
agent glance at one (or a few) images to see how a video's shots progress,
without watching the whole thing.

Usage:
    uv run scripts/extract_keyframes.py --url "https://youtube.com/watch?v=xxx" --out ./output/example/keyframes/xxx
    uv run scripts/extract_keyframes.py --video ./local.mp4 --out ./output/example/keyframes/xxx --threshold 0.2
"""

import argparse
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import yt_dlp

DEFAULT_THRESHOLD = 0.08
MAX_PER_GRID = 25


def download_video(url: str, dest_dir: Path) -> Path:
    outtmpl = str(dest_dir / "source.%(ext)s")
    ydl_opts = {
        "format": "best[height<=480]/bestvideo[height<=480]/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "cookiefile": os.environ.get("YOUTUBE_COOKIES"),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    files = list(dest_dir.glob("source.*"))
    if not files:
        raise FileNotFoundError("Video download failed.")
    return files[0]


def extract_frames(video_path: Path, frames_dir: Path, threshold: float) -> list[float]:
    """Run ffmpeg scene-change selection. Returns per-frame timestamps (seconds)."""
    for f in frames_dir.glob("*.jpg"):
        f.unlink()

    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vf", f"select='eq(n\\,0)+gt(scene\\,{threshold})',showinfo",
        "-vsync", "vfr", "-qscale:v", "3",
        str(frames_dir / "frame_%04d.jpg"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    timestamps = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", result.stderr)]
    return timestamps


def build_grids(frames_dir: Path, out_dir: Path, frame_count: int, max_per_grid: int) -> list[Path]:
    pages = math.ceil(frame_count / max_per_grid)
    grid_paths = []

    for page in range(pages):
        start = page * max_per_grid + 1
        count = min(max_per_grid, frame_count - page * max_per_grid)
        cols = math.ceil(math.sqrt(count))
        rows = math.ceil(count / cols)

        suffix = f"_{page + 1}" if pages > 1 else ""
        grid_path = out_dir / f"keyframes_grid{suffix}.jpg"

        cmd = [
            "ffmpeg", "-y",
            "-start_number", str(start),
            "-i", str(frames_dir / "frame_%04d.jpg"),
            "-frames:v", str(count),
            "-vf", f"tile={cols}x{rows}",
            str(grid_path),
        ]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        grid_paths.append(grid_path)

    return grid_paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract shot-change keyframes and tile into a grid")
    parser.add_argument("--url", help="Video URL (downloaded via yt-dlp)")
    parser.add_argument("--video", type=Path, help="Local video file (alternative to --url)")
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="Scene-change threshold (0-1, lower = more frames)")
    parser.add_argument("--max-per-grid", type=int, default=MAX_PER_GRID, help="Max frames per grid image before splitting into pages")
    args = parser.parse_args()

    if not args.url and not args.video:
        parser.error("one of --url or --video is required")

    args.out.mkdir(parents=True, exist_ok=True)
    frames_dir = args.out / "frames"
    frames_dir.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        video_path = args.video or download_video(args.url, Path(tmp))
        timestamps = extract_frames(video_path, frames_dir, args.threshold)

    frame_count = len(list(frames_dir.glob("*.jpg")))
    if frame_count == 0:
        print("No keyframes extracted.", file=sys.stderr)
        sys.exit(1)

    grid_paths = build_grids(frames_dir, args.out, frame_count, args.max_per_grid)

    print(f"threshold: {args.threshold}")
    print(f"frames: {frame_count}  ({', '.join(f'{t:.1f}s' for t in timestamps)})")
    for p in grid_paths:
        print(f"grid: {p}")


if __name__ == "__main__":
    main()
