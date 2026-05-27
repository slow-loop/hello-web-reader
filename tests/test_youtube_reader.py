from __future__ import annotations

import os
from unittest.mock import mock_open

import pytest

from web_reader.readers import youtube


def test_vtt_to_text_strips_metadata_and_tags():
    content = """WEBVTT

Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000
<c.colorE5E5E5>Hello</c>

00:00:02.000 --> 00:00:04.000
Hello

00:00:04.000 --> 00:00:06.000
World
"""

    assert youtube._vtt_to_text(content) == "Hello\nWorld"


def test_format_verbose_transcription_uses_segment_lines():
    class DummyTranscription:
        text = "This should not be returned as one blob"
        segments = [
            {"start": 0.0, "end": 0.76, "text": "美股實盤挑戰"},
            {"start": 0.76, "end": 2.52, "text": "你覺得我可以從4000美金"},
        ]

    text = youtube._format_verbose_transcription(DummyTranscription())

    assert text == (
        "[00:00:00.000 --> 00:00:00.760] 美股實盤挑戰\n"
        "[00:00:00.760 --> 00:00:02.520] 你覺得我可以從4000美金"
    )


@pytest.mark.asyncio
async def test_read_youtube_prefers_ytdlp_subtitles(monkeypatch):
    monkeypatch.setattr(
        youtube,
        "_download_ytdlp_subtitles",
        lambda url, video_id, languages: ("Hello\nWorld", "en"),
    )

    async def fail_audio(url):
        raise AssertionError("audio fallback should not be called when yt-dlp subtitles exist")

    monkeypatch.setattr(youtube, "transcribe_youtube", fail_audio)

    result = await youtube.read_youtube("https://www.youtube.com/watch?v=abc123", use_audio_fallback=False)

    assert result.success is True
    assert result.language == "en"
    assert result.raw["method"] == "yt-dlp-subs"
    assert "TRANSCRIPT:\nHello\nWorld" in result.text


@pytest.mark.asyncio
async def test_read_youtube_fails_without_audio_fallback_when_ytdlp_has_no_subtitles(monkeypatch):
    monkeypatch.setattr(youtube, "_download_ytdlp_subtitles", lambda url, video_id, languages: None)

    result = await youtube.read_youtube("https://www.youtube.com/watch?v=abc123", use_audio_fallback=False)

    assert result.success is False
    assert result.source_type == "youtube"
    assert "Transcript unavailable" in (result.error or "")


@pytest.mark.asyncio
async def test_read_youtube_falls_back_to_audio(monkeypatch):
    monkeypatch.setattr(youtube, "_download_ytdlp_subtitles", lambda url, video_id, languages: None)

    async def fake_audio(url, prompt=None):
        return "AI transcript text"

    monkeypatch.setattr(youtube, "transcribe_youtube", fake_audio)

    result = await youtube.read_youtube("https://www.youtube.com/watch?v=abc123")

    assert result.success is True
    assert result.language == "ai-transcribed"
    assert result.raw["method"] == "ai-whisper"
    assert "AI transcript text" in result.text


@pytest.mark.asyncio
async def test_read_youtube_passes_transcription_prompt(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(youtube, "_download_ytdlp_subtitles", lambda url, video_id, languages: None)

    async def fake_audio(url, prompt=None):
        captured["url"] = url
        captured["prompt"] = prompt
        return "AI transcript text"

    monkeypatch.setattr(youtube, "transcribe_youtube", fake_audio)

    result = await youtube.read_youtube(
        "https://www.youtube.com/watch?v=abc123",
        transcription_prompt="中文投資影片逐字稿，保留原文。",
    )

    assert result.success is True
    assert captured == {
        "url": "https://www.youtube.com/watch?v=abc123",
        "prompt": "中文投資影片逐字稿，保留原文。",
    }


def test_build_transcription_client_requires_groq(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-should-not-be-used")

    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        youtube._build_transcription_client()


def test_build_transcription_client_uses_groq(monkeypatch):
    captured: dict[str, str] = {}

    class DummyOpenAI:
        def __init__(self, *, base_url=None, api_key=None):
            captured["base_url"] = base_url
            captured["api_key"] = api_key

    monkeypatch.setenv("GROQ_API_KEY", "groq-test-key")
    monkeypatch.setattr(youtube, "OpenAI", DummyOpenAI)

    client, model = youtube._build_transcription_client()

    assert isinstance(client, DummyOpenAI)
    assert model == "whisper-large-v3-turbo"
    assert captured == {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "groq-test-key",
    }


@pytest.mark.asyncio
async def test_transcribe_youtube_disables_ytdlp_progress(monkeypatch):
    captured: dict[str, object] = {}

    class DummyYoutubeDL:
        def __init__(self, opts):
            captured["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, urls):
            captured["urls"] = urls

    class DummyTranscriptions:
        def create(self, **kwargs):
            captured["transcription_kwargs"] = kwargs
            return "AI transcript text"

    class DummyAudio:
        transcriptions = DummyTranscriptions()

    class DummyClient:
        audio = DummyAudio()

    class DummyTempDir:
        def __enter__(self):
            return "/tmp/web-reader-test"

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(youtube.tempfile, "TemporaryDirectory", lambda: DummyTempDir())
    monkeypatch.setattr(youtube.yt_dlp, "YoutubeDL", DummyYoutubeDL)
    monkeypatch.setattr(youtube.os, "listdir", lambda path: ["audio.m4a"])
    monkeypatch.setattr(youtube, "_build_transcription_client", lambda: (DummyClient(), "whisper-large-v3"))
    monkeypatch.setattr("builtins.open", mock_open(read_data=b"audio-bytes"))

    text = await youtube.transcribe_youtube("https://www.youtube.com/watch?v=abc123")

    assert text == "AI transcript text"
    assert captured["urls"] == ["https://www.youtube.com/watch?v=abc123"]
    assert captured["opts"]["noprogress"] is True
    assert captured["transcription_kwargs"]["response_format"] == "verbose_json"
    assert "prompt" not in captured["transcription_kwargs"]


@pytest.mark.asyncio
async def test_transcribe_youtube_accepts_optional_prompt(monkeypatch):
    captured: dict[str, object] = {}

    class DummyYoutubeDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, urls):
            pass

    class DummyTranscriptions:
        def create(self, **kwargs):
            captured["transcription_kwargs"] = kwargs
            return "AI transcript text"

    class DummyAudio:
        transcriptions = DummyTranscriptions()

    class DummyClient:
        audio = DummyAudio()

    class DummyTempDir:
        def __enter__(self):
            return "/tmp/web-reader-test"

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(youtube.tempfile, "TemporaryDirectory", lambda: DummyTempDir())
    monkeypatch.setattr(youtube.yt_dlp, "YoutubeDL", DummyYoutubeDL)
    monkeypatch.setattr(youtube.os, "listdir", lambda path: ["audio.m4a"])
    monkeypatch.setattr(youtube, "_build_transcription_client", lambda: (DummyClient(), "whisper-large-v3"))
    monkeypatch.setattr("builtins.open", mock_open(read_data=b"audio-bytes"))

    text = await youtube.transcribe_youtube(
        "https://www.youtube.com/watch?v=abc123",
        prompt="中文投資影片逐字稿，保留原文。",
    )

    assert text == "AI transcript text"
    assert captured["transcription_kwargs"]["prompt"] == "中文投資影片逐字稿，保留原文。"
