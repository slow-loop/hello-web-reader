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


def _video_meta(info: dict | None) -> dict:
    """Channel / title / publish date out of a yt-dlp info dict.

    Everything here is already in the dict yt-dlp hands back while fetching
    subtitles, so a caller that wants to archive the transcript pays no extra
    round trip for the fields the store needs.

    `uploader_id` is the `@handle`, which is exactly what the store names
    channel folders after — so a transcript pulled by `read` lands in the same
    folder as one pulled by `channel` or `fetch`. When it is missing there is
    no honest folder name (the display name would spawn a second folder for a
    channel that already has one), so the caller is told rather than guessed at.
    """
    if not info:
        return {}
    return {
        "channel": (info.get("uploader_id") or "").lstrip("@"),
        "video_title": info.get("title") or "",
        "upload_date": info.get("upload_date") or "",  # YYYYMMDD
    }


def _build_result(
    video_id: str,
    text: str,
    language: str,
    method: str,
    snippet_count: int | None = None,
    meta: dict | None = None,
) -> ReadResult:
    raw: dict[str, object] = {
        "video_id": video_id,
        "language": language,
        "method": method,
        **(meta or {}),
    }
    if snippet_count is not None:
        raw["snippet_count"] = snippet_count

    return ReadResult(
        url=f"https://www.youtube.com/watch?v={video_id}",
        text=_build_markdown(video_id, text, language),
        title=(meta or {}).get("video_title") or f"YouTube Video {video_id}",
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


def _fetch_video_meta(url: str) -> dict:
    """`_video_meta` for callers outside the subtitle path (i.e. audio ASR).

    Its own info request, because the subtitle path may not have run — but ASR
    is minutes of local compute, so one metadata round trip is noise.
    """
    try:
        with yt_dlp.YoutubeDL(
            {"quiet": True, "no_warnings": True, "cookiefile": _cookiefile_path()}
        ) as ydl:
            return _video_meta(ydl.extract_info(url, download=False))
    except Exception as e:
        logger.info(f"Failed to fetch video metadata for {url}: {e}")
        return {}


def probe_captions(url: str) -> Optional[str]:
    """Which track a fetch would land on: "manual", "auto", or None for neither.

    None is the only expensive case — no captions of any kind means the fetch
    falls back to ASR. Manual and auto are both just a subtitle download, so
    callers reporting cost should treat them as one category ("has captions").

    The YouTube Data API cannot answer this: its `contentDetails.caption` flag
    covers manual tracks only, and reporting *that* as the cost signal reads as
    "no captions" for the very common channel that has auto-captions and costs
    nothing. Hence a real request here — ~1.5s, and worth it to stop guessing.

    Returns None on failure too; callers should treat that as "unknown" rather
    than assume the expensive path.
    """
    try:
        with yt_dlp.YoutubeDL(
            {"skip_download": True, "quiet": True, "no_warnings": True,
             "noprogress": True, "cookiefile": _cookiefile_path()}
        ) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception as e:
        logger.info(f"Caption probe failed for {url}: {e}")
        return None
    if info.get("subtitles"):
        return "manual"
    if info.get("automatic_captions"):
        return "auto"
    return None


def _download_ytdlp_subtitles(
    url: str, video_id: str, languages: list[str]
) -> tuple[str, str, str, dict] | None:
    """Best subtitle track as (text, language, raw_vtt, meta).

    `meta` is the archive-facing metadata from step 1's info dict; see
    `_video_meta`.
    """
    meta: dict = {}
    with tempfile.TemporaryDirectory() as temp_dir:
        # Step 1: Fast extract info to detect original language
        info_opts = {
            "quiet": True,
            "no_warnings": True,
            "cookiefile": _cookiefile_path(),
        }
        try:
            with yt_dlp.YoutubeDL(info_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                meta = _video_meta(info)
                if info and info.get("language"):
                    # e.g. "en-US" -> "en"
                    base_lang = info.get("language").split("-")[0]
                    # Make it the highest priority
                    if base_lang in languages:
                        languages.remove(base_lang)
                    languages.insert(0, base_lang)
                # A manual (non-auto) track means an official transcript exists —
                # request it even under a region code we didn't guess (YouTube
                # labels the same "Traditional Chinese" video zh-Hant but its
                # subtitle track zh-TW), so it isn't silently dropped downstream.
                for lang in (info or {}).get("subtitles", {}):
                    if lang not in languages:
                        languages.append(lang)
        except Exception as e:
            logger.info(f"Failed to detect original language for {video_id}: {e}")

        # Step 2: Download subtitles with the updated priority list
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
            "noprogress": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        subtitle_files = sorted(
            Path(temp_dir).glob(f"{video_id}*.vtt"),
            key=lambda item: _subtitle_language_rank(item, languages),
        )

        for subtitle_file in subtitle_files:
            raw_vtt = subtitle_file.read_text(encoding="utf-8", errors="ignore")
            text = _vtt_to_text(raw_vtt)
            if not text.strip():
                continue

            language = "unknown"
            stem_parts = subtitle_file.stem.split(".")
            if len(stem_parts) >= 2:
                language = stem_parts[-1]

            return text, language, raw_vtt, meta

    return None


async def read_youtube(
    url: str,
    languages: Optional[list[str]] = None,
    use_audio_fallback: bool = True,
    vocabulary_terms: list[str] | None = None,
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
        languages = ["zh", "zh-Hant", "zh-Hans", "en"]

    video_id = _extract_video_id(url)
    if not video_id:
        return ReadResult.fail(url, "Could not extract video ID from URL", source_type="youtube")

    subtitle_error: Exception | None = None

    # Strategy 1: yt-dlp subtitle tracks (manual or auto-generated)
    try:
        subtitle_result = _download_ytdlp_subtitles(url, video_id, languages)
        # An empty track is not a transcript — fall through to audio rather than
        # return a "successful" empty result that callers would cache or write.
        if subtitle_result and subtitle_result[0].strip():
            text, language, raw_vtt, meta = subtitle_result
            res = _build_result(
                video_id,
                text,
                language,
                method="yt-dlp-subs",
                snippet_count=len(text.splitlines()),
                meta=meta,
            )
            res.raw["vtt"] = raw_vtt
            return res
    except Exception as e:
        subtitle_error = e
        logger.info(f"yt-dlp subtitles fetch failed for {video_id}: {e}")

    if not use_audio_fallback:
        error = str(subtitle_error) if subtitle_error else "No subtitles available via yt-dlp"
        return ReadResult.fail(url, f"Transcript unavailable: {error}", source_type="youtube")

    # Strategy 2: AI transcription fallback
    try:
        logger.info(f"Attempting AI transcription fallback for {video_id}...")
        transcript_text = await transcribe_youtube(url, vocabulary_terms=vocabulary_terms)
        if not transcript_text.strip():
            # Silent-video ASR yields nothing. Failing here keeps the caller from
            # writing a header-only file that a resume would then skip forever.
            return ReadResult.fail(url, "Transcription produced no text", source_type="youtube")
        # `language` carries a language, never a provenance marker — how the
        # text was produced is `method`'s job. This path has none to report:
        # transcribe_youtube returns bare text, so SenseVoice's own language
        # detection never reaches us.
        return _build_result(
            video_id,
            transcript_text,
            "unknown",
            # `sensevoice`, not `ai-sensevoice`: that is what the 900+ archived
            # files already say, and the caption method is a bare `yt-dlp-subs`
            # with no `ai-` prefix either.
            method="sensevoice",
            meta=_fetch_video_meta(url),
        )
    except Exception as e:
        return ReadResult.fail(url, f"All strategies failed: {e}", source_type="youtube")


def _channel_lookup_params(channel_id_or_handle: str) -> dict[str, str]:
    """Build channels.list lookup params from a channel ID, handle, or URL."""
    value = channel_id_or_handle
    if "youtube.com/" in value:
        path = urlparse(value).path.strip("/")
        if path.startswith("channel/"):
            return {"id": path.split("/")[1]}
        if path.startswith("@"):
            # Drop trailing segments like /videos or /streams.
            value = path.split("/")[0]

    if value.startswith("UC") and len(value) == 24:
        return {"id": value}
    return {"forHandle": value if value.startswith("@") else f"@{value}"}


def _raise_api_error(resp: httpx.Response, api_key: str) -> None:
    """Raise on a Data API error, keeping Google's machine-readable reason.

    `resp.raise_for_status()` reports only the status code, but the reason that
    actually tells you what to do — quotaExceeded vs rateLimitExceeded vs
    keyInvalid, all of them 403 — lives in the body. The request URL carries the
    API key, so build the message from the body alone and redact the key.
    """
    if not resp.is_error:
        return
    detail = resp.text.replace(api_key, "<redacted>").strip()[:500]
    raise RuntimeError(f"YouTube Data API {resp.status_code}: {detail}")


async def fetch_channel_info(channel_id_or_handle: str) -> dict:
    """
    Resolve a channel ID, handle, or URL to {id, title, video_count} via
    YouTube Data API v3.

    The video_count is what lets callers report a channel's true size before
    deciding how much of it to fetch, so a partial run can never look complete.
    """
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise ValueError("YOUTUBE_API_KEY environment variable is required to resolve channels.")

    params = {
        "part": "snippet,statistics",
        "key": api_key,
        **_channel_lookup_params(channel_id_or_handle),
    }
    # Best practice: use gzip
    headers = {"Accept-Encoding": "gzip", "User-Agent": "web-reader (gzip)"}

    logger.info(f"Resolving channel {channel_id_or_handle} via YouTube API...")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/youtube/v3/channels", params=params, headers=headers
        )
        _raise_api_error(resp, api_key)
        items = resp.json().get("items") or []

    if not items:
        raise ValueError(f"Could not resolve channel: {channel_id_or_handle}")

    item = items[0]
    return {
        "id": item["id"],
        "title": item.get("snippet", {}).get("title", ""),
        "video_count": int(item.get("statistics", {}).get("videoCount", 0)),
    }


async def list_channel_videos(
    channel_id_or_handle: str,
    limit: int = 0,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """
    List a channel's videos, newest first, via YouTube Data API v3.
    Provides gzip optimization and field filtering to minimize payload size.

    Args:
        channel_id_or_handle: YouTube channel ID (UC...), handle (@name), or URL.
        since: Only include videos published on/after this date (YYYY-MM-DD).
        until: Only include videos published on/before this date (YYYY-MM-DD).
        limit: Cap at N newest videos after date filtering. 0 means no cap.
    """
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise ValueError("YOUTUBE_API_KEY environment variable is required to list channel videos.")

    channel_id = channel_id_or_handle
    if not (channel_id.startswith("UC") and len(channel_id) == 24):
        channel_id = (await fetch_channel_info(channel_id_or_handle))["id"]

    # Convert Channel ID (UC...) to Uploads Playlist ID (UU...)
    uploads_playlist_id = "UU" + channel_id[2:]

    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    # Best practice: use gzip
    headers = {"Accept-Encoding": "gzip", "User-Agent": "web-reader (gzip)"}

    results = []
    next_page_token = None

    async with httpx.AsyncClient() as client:
        while True:
            params = {
                "part": "snippet",
                "playlistId": uploads_playlist_id,
                "maxResults": 50,
                "key": api_key,
                # Best practice: Use fields to optimize payload size
                "fields": "nextPageToken,items(snippet(title,publishedAt,thumbnails,resourceId/videoId))"
            }
            if next_page_token:
                params["pageToken"] = next_page_token

            resp = await client.get(url, params=params, headers=headers)
            _raise_api_error(resp, api_key)
            data = resp.json()

            reached_older_than_since = False
            for item in data.get("items", []):
                snippet = item.get("snippet", {})
                video_id = snippet.get("resourceId", {}).get("videoId")
                if not video_id:
                    continue

                published_at = snippet.get("publishedAt", "")
                day = published_at[:10]
                if since and day < since:
                    # Uploads arrive newest-first, so the first *dated* item
                    # older than `since` means every later item is older too —
                    # stop paging instead of walking the whole channel history.
                    # Undated items sort as "" and would fake that signal, so
                    # they only get skipped.
                    if day:
                        reached_older_than_since = True
                        break
                    continue
                if until and day > until:
                    continue

                results.append({
                    "id": video_id,
                    "title": snippet.get("title", ""),
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "published_at": published_at,
                    "thumbnail": _best_thumbnail(snippet.get("thumbnails", {})),
                })

            if reached_older_than_since:
                break

            # Items arrive newest-first, so the first `limit` matches are the
            # newest `limit` matches — stop paging once we have them.
            if limit and len(results) >= limit:
                return results[:limit]

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

    # Truncate here too: breaking out on the `since` boundary (or on the last
    # page) skips the in-loop `limit` return, so a page holding more than
    # `limit` matches would otherwise hand back more than the caller asked for.
    return results[:limit] if limit else results


def _best_thumbnail(thumbnails: dict) -> str | None:
    """Pick the highest-resolution thumbnail URL the API returned."""
    for key in ("maxres", "standard", "high", "medium", "default"):
        entry = thumbnails.get(key)
        if entry and entry.get("url"):
            return entry["url"]
    return None


async def fetch_video_details(video_ids: list[str]) -> dict[str, dict]:
    """Fetch full video metadata via YouTube Data API v3, batched 50 IDs per call.

    Returns {video_id: raw_api_item} with snippet (title, description, tags,
    languages), contentDetails (duration, caption availability), statistics
    (views, likes, comments) and status. Costs 1 quota unit per 50 videos.
    """
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise ValueError("YOUTUBE_API_KEY environment variable is required to fetch video details.")

    url = "https://www.googleapis.com/youtube/v3/videos"
    headers = {"Accept-Encoding": "gzip", "User-Agent": "web-reader (gzip)"}

    details: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for start in range(0, len(video_ids), 50):
            batch = video_ids[start:start + 50]
            params = {
                "part": "snippet,contentDetails,statistics,status",
                "id": ",".join(batch),
                "key": api_key,
            }
            resp = await client.get(url, params=params, headers=headers)
            _raise_api_error(resp, api_key)
            for item in resp.json().get("items", []):
                details[item["id"]] = item

    return details


async def download_thumbnail(
    video_id: str,
    dest: Path,
    client: httpx.AsyncClient,
    fallback_url: str | None = None,
) -> str | None:
    """Download a video's cover image to `dest`. Uses standard quality
    (hqdefault, 480x360), which exists for every video; falls back to the
    API-provided URL. Returns the URL used, or None.

    Works standalone for a single video_id too, no channel run or API key
    needed — the hqdefault URL is a fixed i.ytimg.com path."""
    candidates = [f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"]
    if fallback_url:
        candidates.append(fallback_url)

    for candidate in candidates:
        try:
            resp = await client.get(candidate)
        except httpx.HTTPError:
            continue
        # YouTube returns a 120x90 placeholder (a few KB) for missing maxres.
        if resp.status_code == 200 and len(resp.content) > 2000:
            dest.write_bytes(resp.content)
            return candidate
    return None


async def transcribe_youtube(url: str, vocabulary_terms: list[str] | None = None) -> str:
    """Download YouTube audio and transcribe via the local ASR + LLM pipeline.

    Stage 1: SenseVoice (local, via funasr).
    Stage 2: OpenRouter LLM polishing (default `deepseek/deepseek-v4-flash`,
    override with `OPENROUTER_MODEL`; requires `OPENROUTER_API_KEY`).

    Args:
        url: YouTube video URL.
        vocabulary_terms: Optional list of proper nouns / terms ASR often mishears.
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
        return transcribe_audio(audio_path, vocabulary_terms=vocabulary_terms)
