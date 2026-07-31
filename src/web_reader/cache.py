"""
ReadCache — SQLModel-based cache for read results.

Uses SQLite via SQLModel/SQLAlchemy for:
- Fast "have I read this URL before?" lookups
- TTL-based cache expiry
- Text search across all cached content
- Listing/filtering by source type, date, etc.
"""

import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

from platformdirs import user_cache_dir
from sqlalchemy import JSON, Column, event
from sqlmodel import Field, Session, SQLModel, create_engine, select

from .models import ReadResult

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH_ENV = "WEB_READER_DB_PATH"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def resolve_default_db_file() -> Path:
    """
    Default cache location is user-level and shared across projects:
      macOS:   ~/Library/Caches/web-reader/cache.db
      Linux:   ~/.cache/web-reader/cache.db   (XDG)
      Windows: %LOCALAPPDATA%\\web-reader\\Cache\\cache.db

    Override with the WEB_READER_DB_PATH env var for project-isolated caches.
    """
    configured_path = os.environ.get(DEFAULT_DB_PATH_ENV)
    if configured_path:
        return Path(configured_path).resolve()
    return (Path(user_cache_dir("web-reader")) / "cache.db").resolve()


class ReadRecord(SQLModel, table=True):
    """
    Cached read result. Combines searchable columns with full JSON payload.
    """

    __tablename__ = "read_records"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)

    # --- Searchable columns ---
    url: str = Field(index=True)
    title: Optional[str] = Field(default=None)
    text: str = Field(default="")
    source_type: str = Field(default="unknown", index=True)
    author: Optional[str] = Field(default=None)
    published_at: Optional[datetime] = Field(default=None, index=True)

    # --- Status ---
    success: bool = Field(default=True, index=True)
    error: Optional[str] = None

    # --- Full payload as JSON ---
    payload: Optional[dict] = Field(
        default=None,
        sa_column=Column(JSON),
        description="Complete ReadResult serialized as JSON",
    )

    created_at: datetime = Field(default_factory=_utcnow)

    def to_read_result(self) -> ReadResult:
        """Reconstruct ReadResult from stored payload."""
        if self.payload:
            result = ReadResult.model_validate(self.payload)
            result.cached = True
            return result
        # Fallback: reconstruct from columns
        return ReadResult(
            url=self.url,
            text=self.text,
            title=self.title or "",
            source_type=self.source_type,
            success=self.success,
            error=self.error,
            author=self.author,
            published_at=self.published_at,
            cached=True,
        )


class RssFeedState(SQLModel, table=True):
    """Tracks when an RSS feed was last fetched."""

    __tablename__ = "rss_feed_state"

    feed_url: str = Field(primary_key=True)
    last_fetched_at: datetime = Field(default_factory=_utcnow, index=True)


class RssEntryRecord(SQLModel, table=True):
    """Stored RSS entry keyed by feed URL and stable entry id."""

    __tablename__ = "rss_entries"

    feed_url: str = Field(primary_key=True)
    entry_id: str = Field(primary_key=True)
    url: str = Field(index=True)
    title: str = Field(default="")
    text: str = Field(default="")
    source_type: str = Field(default="rss", index=True)
    author: Optional[str] = Field(default=None)
    published_at: Optional[datetime] = Field(default=None, index=True)
    payload: Optional[dict] = Field(
        default=None,
        sa_column=Column(JSON),
        description="Complete RSS entry ReadResult serialized as JSON",
    )
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def to_read_result(self) -> ReadResult:
        if self.payload:
            result = ReadResult.model_validate(self.payload)
            result.cached = True
            return result
        return ReadResult(
            url=self.url,
            text=self.text,
            title=self.title,
            source_type="rss",
            author=self.author,
            published_at=self.published_at,
            cached=True,
        )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    # Relative dates: -24h, -1d, -7d, -30m
    import re
    match = re.match(r"^-(\d+)([hdm])$", normalized)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        delta = {"h": timedelta(hours=amount), "d": timedelta(days=amount), "m": timedelta(minutes=amount)}[unit]
        return _utcnow() - delta
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


class ReadCache:
    """Manages SQLite cache for read results."""

    def __init__(self, db_path: str | Path | None = None):
        db_file = Path(db_path).resolve() if db_path else resolve_default_db_file()
        db_file.parent.mkdir(parents=True, exist_ok=True)

        self.db_path = db_file
        self.engine = create_engine(f"sqlite:///{db_file}", echo=False)

        # WAL + busy_timeout: safe concurrent writes from multiple projects
        # sharing the user-level cache.
        @event.listens_for(self.engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        SQLModel.metadata.create_all(self.engine)

    def save(self, result: ReadResult) -> str:
        """Save a ReadResult to the store. Returns record ID."""
        record = ReadRecord(
            url=result.url,
            title=result.title,
            text=result.text,
            source_type=result.source_type,
            author=result.author,
            published_at=result.published_at,
            success=result.success,
            error=result.error,
            payload=result.model_dump(mode="json"),
        )
        with Session(self.engine) as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            logger.debug(f"Saved: {record.id} -> {record.url}")
            return record.id

    def rss_feed_is_fresh(self, feed_url: str, ttl_seconds: int) -> bool:
        """Return True when the RSS feed fetch is still within TTL."""
        with Session(self.engine) as session:
            state = session.get(RssFeedState, feed_url)
            if state is None:
                return False
            cutoff = _utcnow() - timedelta(seconds=ttl_seconds)
            return state.last_fetched_at >= cutoff

    def touch_rss_feed(self, feed_url: str) -> None:
        """Record that an RSS feed was fetched now."""
        now = _utcnow()
        with Session(self.engine) as session:
            state = session.get(RssFeedState, feed_url)
            if state is None:
                state = RssFeedState(feed_url=feed_url, last_fetched_at=now)
            else:
                state.last_fetched_at = now
            session.add(state)
            session.commit()

    def upsert_rss_entries(self, feed_url: str, results: list[ReadResult]) -> None:
        """Insert or update RSS entries for a feed."""
        now = _utcnow()
        with Session(self.engine) as session:
            for result in results:
                if not result.success or not isinstance(result.raw, dict):
                    continue
                entry_id = result.raw.get("entry_id")
                if not entry_id:
                    continue
                record = session.get(RssEntryRecord, (feed_url, entry_id))
                if record is None:
                    record = RssEntryRecord(
                        feed_url=feed_url,
                        entry_id=entry_id,
                        created_at=now,
                    )
                record.url = result.url
                record.title = result.title
                record.text = result.text
                record.source_type = result.source_type
                record.author = result.author
                record.published_at = result.published_at
                record.payload = result.model_dump(mode="json")
                record.updated_at = now
                session.add(record)
            session.commit()

    def list_rss_entries(
        self,
        feed_url: str,
        *,
        published_after: str | None = None,
        limit: int | None = None,
    ) -> list[ReadResult]:
        """List stored RSS entries for a feed."""
        with Session(self.engine) as session:
            stmt = select(RssEntryRecord).where(RssEntryRecord.feed_url == feed_url)
            if published_after:
                cutoff = _parse_datetime(published_after)
                if cutoff is None:
                    raise ValueError(
                        f"Invalid published_after value: {published_after}. "
                        "Use YYYY-MM-DD or ISO datetime."
                    )
                stmt = stmt.where(
                    RssEntryRecord.published_at.is_not(None),
                    RssEntryRecord.published_at >= cutoff,
                )
            stmt = stmt.order_by(
                RssEntryRecord.published_at.desc(),
                RssEntryRecord.updated_at.desc(),
            )
            if limit is not None:
                if limit < 0:
                    raise ValueError(f"Invalid limit value: {limit}. Must be >= 0.")
                stmt = stmt.limit(limit)
            return [record.to_read_result() for record in session.exec(stmt).all()]

    def get_cached(
        self,
        url: str,
        ttl_seconds: Optional[int] = None,
        source_type: Optional[str] = None,
    ) -> Optional[ReadResult]:
        """
        Retrieve cached result for a URL if it exists and is within TTL.
        Returns None on cache miss.
        """
        with Session(self.engine) as session:
            stmt = select(ReadRecord).where(
                ReadRecord.url == url,
                ReadRecord.success == True,  # noqa: E712
            )
            if source_type:
                stmt = stmt.where(ReadRecord.source_type == source_type)
            if ttl_seconds is not None:
                cutoff = _utcnow() - timedelta(seconds=ttl_seconds)
                stmt = stmt.where(ReadRecord.created_at >= cutoff)

            stmt = stmt.order_by(ReadRecord.created_at.desc())
            record = session.exec(stmt).first()

            if record:
                logger.debug(f"Cache hit: {url}")
                return record.to_read_result()
            return None

    def get_rss_entry(self, url: str) -> Optional[ReadResult]:
        """
        Look up a cached RSS entry by its post URL (across all feeds).
        No TTL — published article bodies are immutable.
        """
        with Session(self.engine) as session:
            stmt = (
                select(RssEntryRecord)
                .where(RssEntryRecord.url == url)
                .order_by(RssEntryRecord.updated_at.desc())
            )
            record = session.exec(stmt).first()
            if record:
                logger.debug(f"RSS entry cache hit: {url}")
                return record.to_read_result()
            return None

    def search_by_url(self, url: str) -> list[ReadRecord]:
        """Find all records matching a URL."""
        with Session(self.engine) as session:
            stmt = select(ReadRecord).where(ReadRecord.url == url)
            return list(session.exec(stmt).all())

    def list_records(
        self,
        source_type: Optional[str] = None,
        success: Optional[bool] = None,
        limit: int = 50,
        offset: int = 0,
        hours_ago: Optional[int] = None,
    ) -> list[ReadRecord]:
        """List records with filtering and pagination."""
        with Session(self.engine) as session:
            stmt = select(ReadRecord)
            if source_type:
                stmt = stmt.where(ReadRecord.source_type == source_type)
            if success is not None:
                stmt = stmt.where(ReadRecord.success == success)
            if hours_ago:
                cutoff = _utcnow() - timedelta(hours=hours_ago)
                stmt = stmt.where(ReadRecord.created_at >= cutoff)

            stmt = stmt.order_by(ReadRecord.created_at.desc())
            stmt = stmt.limit(limit).offset(offset)
            return list(session.exec(stmt).all())

    def search_text(
        self,
        query: str,
        source_type: Optional[str] = None,
        limit: int = 50,
    ) -> list[ReadRecord]:
        """Simple text search using LIKE."""
        with Session(self.engine) as session:
            stmt = select(ReadRecord)
            if source_type:
                stmt = stmt.where(ReadRecord.source_type == source_type)
            if query:
                stmt = stmt.where(ReadRecord.text.like(f"%{query}%"))
            stmt = stmt.order_by(ReadRecord.created_at.desc())
            stmt = stmt.limit(limit)
            return list(session.exec(stmt).all())
