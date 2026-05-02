"""Tests for URL auto-detection."""

from web_reader._detect import detect_source_type


def test_youtube():
    assert detect_source_type("https://www.youtube.com/watch?v=abc123") == "youtube"
    assert detect_source_type("https://youtu.be/abc123") == "youtube"


def test_reddit():
    assert detect_source_type("https://www.reddit.com/r/investing/hot") == "reddit"
    assert detect_source_type("https://old.reddit.com/r/stocks") == "reddit"


def test_substack():
    assert detect_source_type("https://thegeneralist.substack.com") == "substack"
    assert detect_source_type("https://foo.substack.com/p/some-post") == "substack"


def test_rss():
    assert detect_source_type("https://example.com/feed") == "rss"
    assert detect_source_type("https://example.com/rss") == "rss"
    assert detect_source_type("https://example.com/feed.xml") == "rss"


def test_json():
    assert detect_source_type("https://example.com/api/data.json") == "json"
    assert detect_source_type("https://example.com/api/v1/posts") == "json"


def test_email():
    assert detect_source_type("email://user@imap.example.com") == "email"


def test_gnews():
    assert detect_source_type("gnews://NVDA stock") == "gnews"
    assert detect_source_type("gnews://台積電?period=3d&max=5") == "gnews"


def test_ptt():
    assert detect_source_type("https://www.ptt.cc/bbs/Stock/index.html") == "ptt"
    assert detect_source_type("https://www.ptt.cc/bbs/Stock/search?q=台積電") == "ptt"
    assert detect_source_type("https://www.ptt.cc/bbs/Stock/M.1774581562.A.27A.html") == "ptt"


def test_web_default():
    assert detect_source_type("https://example.com/article") == "web"
    assert detect_source_type("https://news.ycombinator.com") == "web"
