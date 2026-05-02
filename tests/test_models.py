"""Tests for core models."""

from web_reader.models import ReadResult, RssEntry, EmailRequest


def test_read_result_ok():
    r = ReadResult(url="https://example.com", text="Hello", source_type="web")
    assert r.ok is True
    assert r.success is True


def test_read_result_fail():
    r = ReadResult.fail("https://example.com", "Something broke", source_type="web")
    assert r.ok is False
    assert r.success is False
    assert r.error == "Something broke"


def test_read_result_empty_text_not_ok():
    r = ReadResult(url="https://example.com", text="", source_type="web")
    assert r.ok is False
    assert r.success is True


def test_read_result_serialization():
    r = ReadResult(url="https://example.com", text="Hello", source_type="web", title="Test")
    data = r.model_dump(mode="json")
    assert data["url"] == "https://example.com"
    assert data["text"] == "Hello"
    assert data["source_type"] == "web"

    # Round-trip
    r2 = ReadResult.model_validate(data)
    assert r2.url == r.url
    assert r2.text == r.text


def test_rss_entry():
    entry = RssEntry(title="Post", link="https://example.com/post", tags=["tech", "ai"])
    assert entry.title == "Post"
    assert len(entry.tags) == 2


def test_email_request():
    req = EmailRequest(
        username="test@example.com",
        password="secret",
        imap_server="imap.example.com",
    )
    assert req.folder == "INBOX"
    assert req.limit is None
