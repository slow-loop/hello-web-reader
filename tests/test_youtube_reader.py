from __future__ import annotations

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


@pytest.mark.asyncio
async def test_read_youtube_prefers_ytdlp_subtitles(monkeypatch):
    monkeypatch.setattr(
        youtube,
        "_download_ytdlp_subtitles",
        lambda url, video_id, languages: ("Hello\nWorld", "en", "WEBVTT\n..."),
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

    async def fake_audio(url, vocabulary_terms=None):
        return "AI transcript text"

    monkeypatch.setattr(youtube, "transcribe_youtube", fake_audio)

    result = await youtube.read_youtube("https://www.youtube.com/watch?v=abc123")

    assert result.success is True
    assert result.language == "ai-transcribed"
    assert result.raw["method"] == "ai-whisper"
    assert "AI transcript text" in result.text


@pytest.mark.asyncio
async def test_transcribe_youtube_uses_pipeline(monkeypatch):
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

    class DummyTempDir:
        def __enter__(self):
            return "/tmp/web-reader-test"

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_transcribe_audio(audio_path, vocabulary_terms=None):
        captured["audio_path"] = audio_path
        captured["vocabulary_terms"] = vocabulary_terms
        return "polished transcript"

    monkeypatch.setattr(youtube.tempfile, "TemporaryDirectory", lambda: DummyTempDir())
    monkeypatch.setattr(youtube.yt_dlp, "YoutubeDL", DummyYoutubeDL)
    monkeypatch.setattr(youtube.os, "listdir", lambda path: ["audio.m4a"])

    import web_reader.transcribe as transcribe_module
    monkeypatch.setattr(transcribe_module, "transcribe_audio", fake_transcribe_audio)

    text = await youtube.transcribe_youtube("https://www.youtube.com/watch?v=abc123")

    assert text == "polished transcript"
    assert captured["urls"] == ["https://www.youtube.com/watch?v=abc123"]
    assert captured["opts"]["noprogress"] is True
    assert captured["audio_path"] == "/tmp/web-reader-test/audio.m4a"
    assert captured["vocabulary_terms"] is None


@pytest.mark.asyncio
async def test_transcribe_youtube_forwards_vocabulary_terms(monkeypatch):
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

    class DummyTempDir:
        def __enter__(self):
            return "/tmp/web-reader-test"

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_transcribe_audio(audio_path, vocabulary_terms=None):
        captured["vocabulary_terms"] = vocabulary_terms
        return "ok"

    monkeypatch.setattr(youtube.tempfile, "TemporaryDirectory", lambda: DummyTempDir())
    monkeypatch.setattr(youtube.yt_dlp, "YoutubeDL", DummyYoutubeDL)
    monkeypatch.setattr(youtube.os, "listdir", lambda path: ["audio.m4a"])

    import web_reader.transcribe as transcribe_module
    monkeypatch.setattr(transcribe_module, "transcribe_audio", fake_transcribe_audio)

    await youtube.transcribe_youtube(
        "https://www.youtube.com/watch?v=abc123",
        vocabulary_terms=["TSMC"],
    )

    assert captured["vocabulary_terms"] == ["TSMC"]
