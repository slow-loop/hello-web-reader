"""
YouTube reader — fetch video transcripts without a browser.

Tries yt-dlp subtitles first, then optional audio transcription fallback.
"""

import html
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx

import yt_dlp

from ..models import ReadResult

logger = logging.getLogger(__name__)


def _extract_video_id(url: str) -> Optional[str]:
    """Extract video ID from various YouTube URL formats."""
    parsed = urlparse(url)

    # youtu.be/VIDEO_ID
    if "youtu.be" in parsed.netloc:
        return parsed.path.lstrip("/").split("/")[0]

    # youtube.com/watch?v=VIDEO_ID
    if "youtube.com" in parsed.netloc:
        if "v" in parse_qs(parsed.query):
            return parse_qs(parsed.query)["v"][0]
        # youtube.com/shorts/VIDEO_ID
        if "/shorts/" in parsed.path:
            return parsed.path.split("/shorts/")[1].split("/")[0]

    return None


def _cookiefile_path() -> str | None:
    # Only use explicit path from environment variable
    env_path = os.environ.get("YOUTUBE_COOKIES")
    if env_path and os.path.exists(env_path):
        return env_path

    return None


def _build_markdown(video_id: str, text: str, language: str) -> str:
    return (
        f"VIDEO_ID: {video_id}\n"
        f"VIDEO_URL: https://www.youtube.com/watch?v={video_id}\n"
        f"LANG: {language}\n"
        f"\nTRANSCRIPT:\n{text}"
    )


def _build_result(
    video_id: str,
    text: str,
    language: str,
    method: str,
    snippet_count: int | None = None,
) -> ReadResult:
    raw: dict[str, object] = {
        "video_id": video_id,
        "language": language,
        "method": method,
    }
    if snippet_count is not None:
        raw["snippet_count"] = snippet_count

    return ReadResult(
        url=f"https://www.youtube.com/watch?v={video_id}",
        text=_build_markdown(video_id, text, language),
        title=f"YouTube Video {video_id}",
        source_type="youtube",
        language=language,
        raw=raw,
    )


def _vtt_to_text(content: str) -> str:
    lines: list[str] = []

    for raw_line in content.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line:
            continue
        if line == "WEBVTT":
            continue
        if line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        if "-->" in line:
            continue
        if line.isdigit():
            continue

        line = re.sub(r"<[^>]+>", "", line)
        line = html.unescape(line).strip()
        if not line:
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)

    return "\n".join(lines)


def _subtitle_language_rank(path: Path, languages: list[str]) -> tuple[int, str]:
    filename = path.name.lower()
    for index, language in enumerate(languages):
        needle = f".{language.lower()}."
        if needle in filename or filename.endswith(f".{language.lower()}.vtt"):
            return index, filename
    return len(languages), filename


def _download_ytdlp_subtitles(url: str, video_id: str, languages: list[str]) -> tuple[str, str] | None:
    with tempfile.TemporaryDirectory() as temp_dir:
        output_template = os.path.join(temp_dir, "%(id)s.%(ext)s")
        ydl_opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": languages,
            "subtitlesformat": "vtt",
            "outtmpl": output_template,
            "cookiefile": _cookiefile_path(),
            "quiet": True,
            "no_warnings": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        subtitle_files = sorted(
            Path(temp_dir).glob(f"{video_id}*.vtt"),
            key=lambda item: _subtitle_language_rank(item, languages),
        )

        for subtitle_file in subtitle_files:
            text = _vtt_to_text(subtitle_file.read_text(encoding="utf-8", errors="ignore"))
            if not text.strip():
                continue

            language = "unknown"
            stem_parts = subtitle_file.stem.split(".")
            if len(stem_parts) >= 2:
                language = stem_parts[-1]

            return text, language

    return None


async def read_youtube(
    url: str,
    languages: Optional[list[str]] = None,
    use_audio_fallback: bool = True,
) -> ReadResult:
    """
    Fetch YouTube video transcript.

    Args:
        url: YouTube video URL.
        languages: Preferred languages, e.g. ["en", "zh"]. Defaults to ["en", "zh"].
        use_audio_fallback: Whether to fallback to AI transcription if captions fail. Defaults to True.

    Requires: pip install web-reader[youtube]
    """
    if languages is None:
        languages = ["en", "zh"]

    video_id = _extract_video_id(url)
    if not video_id:
        return ReadResult.fail(url, "Could not extract video ID from URL", source_type="youtube")

    subtitle_error: Exception | None = None

    # Strategy 1: yt-dlp subtitle tracks (manual or auto-generated)
    try:
        subtitle_result = _download_ytdlp_subtitles(url, video_id, languages)
        if subtitle_result:
            text, language = subtitle_result
            return _build_result(
                video_id,
                text,
                language,
                method="yt-dlp-subs",
                snippet_count=len(text.splitlines()),
            )
    except Exception as e:
        subtitle_error = e
        logger.info(f"yt-dlp subtitles fetch failed for {video_id}: {e}")

    if not use_audio_fallback:
        error = str(subtitle_error) if subtitle_error else "No subtitles available via yt-dlp"
        return ReadResult.fail(url, f"Transcript unavailable: {error}", source_type="youtube")

    # Strategy 2: AI transcription fallback
    try:
        logger.info(f"Attempting AI transcription fallback for {video_id}...")
        transcript_text = await transcribe_youtube(url)
        return _build_result(
            video_id,
            transcript_text,
            "ai-transcribed",
            method="ai-whisper",
        )
    except Exception as e:
        return ReadResult.fail(url, f"All strategies failed: {e}", source_type="youtube")


async def resolve_channel_id(channel_id_or_handle: str) -> str:
    """
    Resolve a YouTube channel ID from a channel ID, handle, or URL via YouTube Data API v3.
    Returns the channel ID (starting with "UC").
    """
    if channel_id_or_handle.startswith("UC") and len(channel_id_or_handle) == 24:
        return channel_id_or_handle

    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise ValueError("YOUTUBE_API_KEY environment variable is required to resolve channel IDs.")

    # Handle URLs
    handle = channel_id_or_handle
    if "youtube.com/" in handle:
        parsed = urlparse(handle)
        path = parsed.path.strip("/")
        if path.startswith("@"):
            handle = path
        elif path.startswith("channel/"):
            return path.split("/")[1]

    if not handle.startswith("@"):
        handle = f"@{handle}"

    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {
        "part": "id",
        "forHandle": handle,
        "key": api_key
    }
    
    # Best practice: use gzip
    headers = {"Accept-Encoding": "gzip", "User-Agent": "web-reader (gzip)"}

    logger.info(f"Resolving channel ID for {handle} via YouTube API...")
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        if data.get("items") and len(data["items"]) > 0:
            return data["items"][0]["id"]
            
    raise ValueError(f"Could not resolve channel ID for: {channel_id_or_handle}")


async def list_channel_videos(channel_id_or_handle: str, limit: int = 5) -> list[dict]:
    """
    List recent videos for a YouTube channel via YouTube Data API v3.
    Provides gzip optimization and field filtering to minimize payload size.
    
    Args:
        channel_id_or_handle: YouTube channel ID (UC...) or handle (@name).
        limit: Max number of videos to return.
    """
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise ValueError("YOUTUBE_API_KEY environment variable is required to list channel videos.")

    channel_id = await resolve_channel_id(channel_id_or_handle)
    
    # Convert Channel ID (UC...) to Uploads Playlist ID (UU...)
    if not channel_id.startswith("UC"):
        raise ValueError(f"Invalid Channel ID format: {channel_id}")
    uploads_playlist_id = "UU" + channel_id[2:]

    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    params = {
        "part": "snippet",
        "playlistId": uploads_playlist_id,
        "maxResults": limit,
        "key": api_key,
        # Best practice: Use fields to optimize payload size
        "fields": "items(snippet(title,publishedAt,resourceId/videoId))"
    }
    
    # Best practice: use gzip
    headers = {"Accept-Encoding": "gzip", "User-Agent": "web-reader (gzip)"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("items", []):
        snippet = item.get("snippet", {})
        video_id = snippet.get("resourceId", {}).get("videoId")
        if not video_id:
            continue
            
        results.append({
            "id": video_id,
            "title": snippet.get("title", ""),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "published_at": snippet.get("publishedAt", ""),
        })
    return results


async def transcribe_youtube(url: str, context: str | None = None) -> str:
    """Download YouTube audio and transcribe via the local ASR + LLM pipeline.

    Stage 1: SenseVoice (local, via funasr).
    Stage 2: OpenRouter LLM polishing (default `deepseek/deepseek-chat`,
    override with `OPENROUTER_MODEL`; requires `OPENROUTER_API_KEY`).

    Args:
        url: YouTube video URL.
        context: Optional short domain hint forwarded to the refinement LLM.
    """
    from ..transcribe import transcribe_audio

    video_id = _extract_video_id(url)
    if not video_id:
        raise ValueError("Invalid YouTube URL")

    with tempfile.TemporaryDirectory() as temp_dir:
        audio_path_tmpl = os.path.join(temp_dir, "audio.%(ext)s")

        ydl_opts = {
            "format": "worstaudio/worst",
            "outtmpl": audio_path_tmpl,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "cookiefile": _cookiefile_path(),
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        downloaded_files = os.listdir(temp_dir)
        if not downloaded_files:
            raise FileNotFoundError("Audio file was not downloaded successfully.")

        audio_path = os.path.join(temp_dir, downloaded_files[0])
        return transcribe_audio(audio_path, context=context)
