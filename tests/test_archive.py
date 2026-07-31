"""Tests for the output/ archive layout contract."""

from datetime import datetime, timezone

import pytest

from web_reader.archive import Archive, guid_slug, url_slug


@pytest.fixture
def archive(tmp_path):
    return Archive(tmp_path)


DT = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)


class TestYouTube:
    def test_save_and_find_roundtrip(self, archive):
        archive.save_youtube(
            "somechannel", "subtitles", "abcDEF12345", "測試 Title: 50%",
            DT, "hello world", language="zh", method="yt-dlp-subs",
        )
        item = archive.find_youtube("abcDEF12345")
        assert item is not None
        assert item.url == "https://www.youtube.com/watch?v=abcDEF12345"
        assert item.title == "測試 Title: 50%"
        assert item.published_at == DT
        assert item.text == "hello world"
        assert item.extra["method"] == "yt-dlp-subs"

    def test_subtitles_and_transcripts_are_separate_dirs(self, archive):
        p1 = archive.save_youtube("ch", "subtitles", "aaaaaaaaaaa", "t", DT, "x")
        p2 = archive.save_youtube("ch", "transcripts", "bbbbbbbbbbb", "t", DT, "x")
        assert p1.parent.name == "subtitles"
        assert p2.parent.name == "transcripts"

    def test_file_without_frontmatter_still_loads(self, archive):
        subs = archive.root / "youtube" / "ch" / "subtitles"
        subs.mkdir(parents=True)
        (subs / "2026-01-05_rawvideoid1_old style.md").write_text("VIDEO_ID: rawvideoid1\nTRANSCRIPT:\nhi")
        item = archive.find_youtube("rawvideoid1")
        assert item.text.startswith("VIDEO_ID:")
        assert item.published_at.date().isoformat() == "2026-01-05"

    def test_list_youtube_since_filters_by_filename_date(self, archive):
        archive.save_youtube("ch", "subtitles", "aaaaaaaaaaa", "old", datetime(2025, 1, 1, tzinfo=timezone.utc), "x")
        archive.save_youtube("ch", "transcripts", "bbbbbbbbbbb", "new", DT, "y")
        items = archive.list_youtube("ch", since_date="2026-01-01")
        assert [i.title for i in items] == ["new"]

    def test_missing_video_returns_none(self, archive):
        assert archive.find_youtube("nosuchvid00") is None
        assert not archive.has_youtube("nosuchvid00")


class TestPodcast:
    GUID = "https://firstory.me/ep/xyz?a=1"

    def test_save_and_dedup_by_guid(self, archive):
        archive.save_podcast("gooaye", self.GUID, "EP1", DT, "body", webpage_url="https://x")
        assert archive.has_podcast(self.GUID)
        assert not archive.has_podcast("other-guid")

    def test_list_carries_podcast_url_scheme(self, archive):
        archive.save_podcast("gooaye", self.GUID, "EP1", DT, "body")
        [item] = archive.list_podcast("gooaye")
        assert item.url == f"podcast://{self.GUID}"
        assert item.extra["guid"] == self.GUID

    def test_audio_dir_is_inside_source_dir(self, archive):
        assert archive.podcast_audio_dir("gooaye") == archive.podcast_dir("gooaye") / "audio"


class TestArticles:
    URL = "https://kp.substack.com/p/some-post"

    def test_save_and_dedup_by_url(self, archive):
        archive.save_article("kp", self.URL, "Post", DT, "body")
        assert archive.has_article("kp", self.URL)
        assert not archive.has_article("kp", "https://kp.substack.com/p/other")
        assert not archive.has_article("elsewhere", self.URL)

    def test_list_articles_roundtrip(self, archive):
        archive.save_article("kp", self.URL, "Post", DT, "body text")
        [item] = archive.list_articles("kp")
        assert (item.url, item.title, item.text) == (self.URL, "Post", "body text")


def test_slugs_are_filesystem_safe():
    assert "/" not in guid_slug("https://a/b?c=1")
    assert url_slug("https://kp.substack.com/p/hello-world/") == "hello-world"
