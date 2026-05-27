from __future__ import annotations

import pytest

from web_reader import cli
from web_reader.models import ReadResult


@pytest.mark.asyncio
async def test_run_url_forwards_youtube_prompt(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "_print_results", lambda results, output_format: None)
    monkeypatch.setattr("web_reader._detect.detect_source_type", lambda url: "youtube")

    async def fake_read_youtube(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return ReadResult(
            url=url,
            text="ok",
            title="",
            source_type="youtube",
        )

    monkeypatch.setattr("web_reader.readers.youtube.read_youtube", fake_read_youtube)

    await cli._run_url(
        "https://www.youtube.com/watch?v=abc123",
        no_cache=True,
        output_format="md",
        youtube_prompt="中文投資影片逐字稿，保留原文。",
    )

    assert captured == {
        "url": "https://www.youtube.com/watch?v=abc123",
        "kwargs": {"transcription_prompt": "中文投資影片逐字稿，保留原文。"},
    }
