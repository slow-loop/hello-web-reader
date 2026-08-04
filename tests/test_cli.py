from __future__ import annotations

import pytest
from typer.testing import CliRunner

from web_reader import cli
from web_reader.models import ReadResult
from web_reader.readers import youtube
from web_reader.store import Store

VIDEO_URL = "https://www.youtube.com/watch?v=abc12345678"


class _NoopCache:
    def get_cached(self, *args, **kwargs):
        return None

    def save(self, result):
        pass


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the CLI's store at a temp root.

    Without this every `read` test would read and write the repo's real
    output/ archive — a store hit from someone's actual transcripts would make
    the fetch tests pass for the wrong reason.
    """
    monkeypatch.setattr(cli, "ReadCache", _NoopCache)
    monkeypatch.setattr(cli, "Store", lambda: Store(tmp_path))
    return Store(tmp_path)


def _fake_read(**raw):
    async def _read(*args, **kwargs):
        return ReadResult(
            url=VIDEO_URL,
            text="VIDEO_ID: abc12345678\n\nTRANSCRIPT:\nhello",
            title="Some Video",
            source_type="youtube",
            language=raw.get("language", "en"),
            raw={"video_id": "abc12345678", "method": "yt-dlp-subs", **raw},
        )

    return _read


def test_read_archives_youtube_into_the_store(store, monkeypatch):
    monkeypatch.setattr(
        youtube,
        "read_youtube",
        _fake_read(channel="somechannel", video_title="Some Video", upload_date="20260115"),
    )

    result = CliRunner().invoke(cli.app, ["read", VIDEO_URL])

    assert result.exit_code == 0
    assert "TRANSCRIPT:" in result.stdout
    assert "Stored:" in result.stderr
    # Same folder `channel` and `fetch` write, named by date + id + title.
    [path] = list((store.root / "youtube" / "somechannel" / "subtitles").glob("*.md"))
    assert path.name.startswith("2026-01-15_abc12345678_")
    assert store.has_youtube("abc12345678")


def test_read_serves_a_second_time_from_the_store_without_fetching(store, monkeypatch):
    store.save_youtube(
        "somechannel", "subtitles", "abc12345678", "Some Video", None,
        "VIDEO_ID: abc12345678\n\nTRANSCRIPT:\nhello", language="en", method="yt-dlp-subs",
    )

    async def _explode(*args, **kwargs):
        raise AssertionError("a store hit must not touch the network")

    monkeypatch.setattr(youtube, "read_youtube", _explode)

    result = CliRunner().invoke(cli.app, ["read", VIDEO_URL])

    assert result.exit_code == 0
    assert "TRANSCRIPT:" in result.stdout
    assert "Cached:" in result.stderr


def test_read_without_a_channel_handle_prints_but_does_not_guess_a_folder(store, monkeypatch):
    monkeypatch.setattr(youtube, "read_youtube", _fake_read(channel="", video_title="Some Video"))

    result = CliRunner().invoke(cli.app, ["read", VIDEO_URL])

    assert result.exit_code == 0
    assert "TRANSCRIPT:" in result.stdout
    assert "Not stored" in result.stderr
    assert not store.has_youtube("abc12345678")


def test_read_files_asr_output_under_transcripts(store, monkeypatch):
    monkeypatch.setattr(
        youtube,
        "read_youtube",
        _fake_read(channel="somechannel", method="sensevoice", language="unknown"),
    )

    result = CliRunner().invoke(cli.app, ["read", VIDEO_URL])

    assert result.exit_code == 0
    assert list((store.root / "youtube" / "somechannel" / "transcripts").glob("*.md"))
    assert not (store.root / "youtube" / "somechannel" / "subtitles").exists()


def test_read_failure_exits_nonzero_and_leaves_stdout_empty(store, monkeypatch):
    async def fake_read_youtube(*args, **kwargs):
        return ReadResult.fail(
            VIDEO_URL, "All strategies failed: SenseVoice unavailable", source_type="youtube"
        )

    monkeypatch.setattr(youtube, "read_youtube", fake_read_youtube)

    result = CliRunner().invoke(cli.app, ["read", VIDEO_URL])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "ERROR: All strategies failed: SenseVoice unavailable" in result.stderr
