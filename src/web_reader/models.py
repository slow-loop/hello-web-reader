"""
Core data models for web-reader.

Design: One flat ReadResult instead of 3-layer nesting.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


SourceType = Literal["web", "rss", "json", "youtube", "youtube_channel", "apple_podcast", "rss_podcast", "email", "reddit", "stocktwits", "substack", "gnews", "ptt", "cnyes", "unknown"]


class ReadResult(BaseModel):
    """
    The single output type for all readers.

    Flat structure — no nesting. Easy to serialize, easy to consume.
    """

    model_config = ConfigDict(extra="allow")

    # --- Core ---
    url: str
    text: str = ""
    title: str = ""
    source_type: SourceType = "unknown"
    success: bool = True
    error: Optional[str] = None

    # --- Metadata ---
    author: Optional[str] = None
    published_at: Optional[datetime] = None
    language: Optional[str] = None
    description: Optional[str] = None

    # --- Raw data (for downstream processing) ---
    raw: Optional[Any] = None

    # --- Cache info ---
    cached: bool = False

    @property
    def ok(self) -> bool:
        return self.success and bool(self.text)

    @staticmethod
    def fail(url: str, error: str, source_type: SourceType = "unknown") -> "ReadResult":
        """Convenience constructor for error results."""
        return ReadResult(
            url=url,
            success=False,
            error=error,
            source_type=source_type,
        )


class RssEntry(BaseModel):
    """Individual RSS feed entry."""

    entry_id: str = ""
    title: str = ""
    link: str = ""
    author: str = ""
    published: Optional[str] = None
    summary: str = ""
    content: str = ""
    tags: list[str] = Field(default_factory=list)


class EmailRequest(BaseModel):
    """Configuration for email reading."""

    username: str
    password: str
    imap_server: str
    folder: str = "INBOX"
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    hours: Optional[int] = None  # shorthand: fetch last N hours; sets start_date if not provided
    limit: Optional[int] = None
    senders: Optional[list[str]] = None

    @model_validator(mode="after")
    def _apply_hours_window(self) -> "EmailRequest":
        if self.hours is not None and self.start_date is None:
            self.start_date = datetime.now(tz=timezone.utc) - timedelta(hours=self.hours)
        return self
