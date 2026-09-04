"""Tests for YAML config loading."""

import tempfile

from web_reader.config import CacheConfig, load_config


def test_load_basic_config():
    yaml_content = """
sources:
  - source_id: test-rss
    name: test-rss
    reader: rss
    url: "https://example.com/feed"
    tags: [tech]
    cache:
      ttl: 3600
  - source_id: test-reddit
    name: test-reddit
    reader: reddit
    tags: [finance, reddit]
    params:
      subreddit: investing
      sort: hot
    cache:
      ttl: 1800
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        config = load_config(f.name)

    assert len(config.sources) == 2
    assert config.sources[0].name == "test-rss"
    assert config.sources[0].reader == "rss"
    assert config.sources[0].cache.ttl == 3600
    assert config.sources[0].tags == ["tech"]
    assert config.sources[1].params["subreddit"] == "investing"
    assert "finance" in config.sources[1].tags
    assert "reddit" in config.sources[1].tags


def test_source_tags_default_empty():
    yaml_content = """
sources:
  - source_id: no-tags
    name: no-tags
    reader: web
    url: "https://example.com"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        config = load_config(f.name)

    assert config.sources[0].tags == []


def test_cache_config_default():
    c = CacheConfig()
    assert c.ttl is None


def test_load_config_with_cache_ttl():
    yaml_content = """
sources:
  - source_id: morning-email
    name: morning-email
    reader: email
    tags: [email]
    params:
      username: "test@example.com"
      password: "secret"
      imap_server: "imap.gmail.com"
    cache:
      ttl: 900
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        config = load_config(f.name)

    assert config.sources[0].cache.ttl == 900
    assert config.sources[0].params["username"] == "test@example.com"
    assert "email" in config.sources[0].tags
