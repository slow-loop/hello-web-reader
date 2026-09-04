"""YAML config loader for web-reader."""

import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class CacheConfig(BaseModel):
    """Minimal cache strategy for a source."""

    ttl: Optional[int] = None  # seconds


class SourceConfig(BaseModel):
    """A single source in the watchlist config."""

    name: str
    reader: str  # web, rss, reddit, youtube, email, json, substack
    source_id: str  # stable source id; names the archive folder. Required:
    # deriving it from `name` would silently move the archive on every rename.
    url: Optional[str] = None
    params: dict[str, Any] = Field(default_factory=dict)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    tags: list[str] = Field(default_factory=list)


class DefaultsConfig(BaseModel):
    """Default params and cache applied to all sources."""

    params: dict[str, Any] = Field(default_factory=dict)
    cache: Optional[CacheConfig] = None


class WatchlistConfig(BaseModel):
    """Top-level watchlist.yaml structure."""

    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    sources: list[SourceConfig] = Field(default_factory=list)


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand ${VAR} placeholders in string values."""
    if isinstance(obj, str):
        return re.sub(r"\$\{([^}]+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), obj)
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars(i) for i in obj]
    return obj


def load_config(path: str | Path) -> WatchlistConfig:
    """Load a watchlist.yaml file, expanding ${ENV_VAR} placeholders."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config not found: {p}")

    # Load .env from the same directory as the config file (or cwd)
    load_dotenv(p.parent / ".env", override=False)
    load_dotenv(override=False)

    with open(p) as f:
        raw = yaml.safe_load(f)

    raw = _expand_env_vars(raw)

    if raw is None:
        return WatchlistConfig()

    config = WatchlistConfig.model_validate(raw)

    # Merge defaults into each source
    if config.defaults.params or config.defaults.cache:
        for source in config.sources:
            for key, value in config.defaults.params.items():
                if key not in source.params:
                    source.params[key] = value
            if config.defaults.cache and source.cache.ttl is None:
                source.cache.ttl = config.defaults.cache.ttl

    return config
