"""
Substack reader — fetch newsletter posts, content, and search.

Uses httpx to hit Substack's API endpoints directly.
No external substack_api dependency needed.
"""

import logging
from datetime import datetime
from typing import Any, List, Optional

import httpx
from markdownify import markdownify as md
from pydantic import BaseModel, Field

from ..models import ReadResult

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "DNT": "1",
}


# ---------------------------------------------------------------------------
# Pydantic models for Substack search / explore APIs
# ---------------------------------------------------------------------------


class Publication(BaseModel):
    id: int
    name: str
    subdomain: str
    custom_domain: Optional[str] = None


class AuthorByline(BaseModel):
    id: int
    name: str
    handle: Optional[str] = None
    primary_publication: Optional[Publication] = None


class PostTag(BaseModel):
    id: str
    name: str
    hidden: bool = False


class SearchPost(BaseModel):
    """Post result from Substack search API."""

    id: int
    title: str
    slug: str
    canonical_url: str
    post_date: str
    publication_id: int
    subtitle: Optional[str] = None
    description: Optional[str] = None
    truncated_body_text: Optional[str] = None
    reaction_count: Optional[int] = None
    comment_count: Optional[int] = None
    audience: Optional[str] = None
    publication: Optional[Publication] = None
    publishedBylines: Optional[List[AuthorByline]] = None
    postTags: Optional[List[PostTag]] = None

    class Config:
        extra = "ignore"


class SearchPostsResponse(BaseModel):
    results: List[SearchPost]
    focused: Optional[List[SearchPost]] = None


# --- Explore API models ---


class ExploreProfile(BaseModel):
    id: int
    name: str
    handle: Optional[str] = None
    bio: Optional[str] = None


class ExploreContext(BaseModel):
    type: Optional[str] = None
    timestamp: Optional[str] = None
    users: Optional[List[ExploreProfile]] = None


class ExploreAttachment(BaseModel):
    type: Optional[str] = None
    post: Optional[SearchPost] = None
    publication: Optional[Publication] = None

    class Config:
        extra = "ignore"


class ExploreComment(BaseModel):
    """A Substack Note, as returned by the explore and per-publication note APIs."""

    id: int
    name: Optional[str] = None
    handle: Optional[str] = None
    body: Optional[str] = None
    date: Optional[str] = None
    reaction_count: Optional[int] = None
    restacks: Optional[int] = None
    children_count: Optional[int] = None
    attachments: Optional[List[ExploreAttachment]] = None
    user_primary_publication: Optional[Publication] = None

    class Config:
        extra = "ignore"


class ExploreItem(BaseModel):
    type: Optional[str] = None
    profiles: Optional[List[ExploreProfile]] = None
    publication: Optional[Publication] = None
    user: Optional[ExploreProfile] = None
    items: Optional[List["ExploreItem"]] = None
    context: Optional[ExploreContext] = None
    comment: Optional[ExploreComment] = None

    class Config:
        extra = "ignore"


class ExploreResponse(BaseModel):
    items: List[ExploreItem]


class NotesResponse(BaseModel):
    """One page of a publication's own note feed."""

    items: List[ExploreItem] = Field(default_factory=list)
    nextCursor: Optional[str] = None

    class Config:
        extra = "ignore"


# Resolve forward refs
ExploreAttachment.model_rebuild()
ExploreItem.model_rebuild()


def _extract_subdomain(url: str) -> Optional[str]:
    """Extract subdomain from Substack URL. e.g. 'thegeneralist' from 'https://thegeneralist.substack.com'.
    For custom domains (e.g. newsletter.semianalysis.com), returns the full host so callers can
    use it as the base domain for API requests."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if ".substack.com" in host:
        return host.replace(".substack.com", "")

    # Custom domain — return the host directly so we can hit its Substack API endpoint
    return host if host else None


async def _fetch_api(url: str, timeout: float = 30.0, params: Optional[dict] = None) -> Any:
    """Fetch JSON from Substack API."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.json()


async def read_substack(
    url: Optional[str] = None,
    *,
    publication: Optional[str] = None,
    slug: Optional[str] = None,
    limit: int = 10,
) -> ReadResult:
    """
    Read Substack content.

    Usage:
        # List recent posts from a publication
        await read_substack(publication="thegeneralist", limit=5)
        await read_substack("https://thegeneralist.substack.com")

        # Read a specific post
        await read_substack("https://thegeneralist.substack.com/p/some-post-slug")
        await read_substack(publication="thegeneralist", slug="some-post-slug")
    """
    # Resolve publication name
    pub = publication
    if url and not pub:
        pub = _extract_subdomain(url)

        # Check if URL points to a specific post
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if "/p/" in parsed.path:
            slug = parsed.path.split("/p/")[1].strip("/")

    if not pub:
        # Try fetching the URL directly as a web page
        if url:
            return ReadResult.fail(
                url, "Could not determine Substack publication from URL", source_type="substack"
            )
        return ReadResult.fail("substack://", "Provide url or publication name", source_type="substack")

    try:
        if slug:
            return await _read_post(pub, slug)
        else:
            return await _read_posts_list(pub, limit)
    except Exception as e:
        logger.error(f"Substack fetch failed: {e}")
        return ReadResult.fail(
            url or f"substack://{pub}", str(e), source_type="substack"
        )


def _pub_base_url(publication: str) -> str:
    """Return the base URL for a publication. Handles both subdomain and custom domain forms."""
    # If it looks like a full domain (contains a dot, not a simple subdomain slug)
    if "." in publication:
        return f"https://{publication}"
    return f"https://{publication}.substack.com"


async def _read_posts_list(publication: str, limit: int = 10) -> ReadResult:
    """Fetch recent posts from a publication."""
    base = _pub_base_url(publication)
    api_url = f"{base}/api/v1/posts?limit={limit}"
    posts = await _fetch_api(api_url)

    if not isinstance(posts, list):
        return ReadResult.fail(api_url, "Unexpected API response format", source_type="substack")

    lines = []
    post_summaries = []

    for p in posts:
        title = p.get("title", "Untitled")
        subtitle = p.get("subtitle", "")
        date = p.get("post_date", "")[:10] if p.get("post_date") else ""
        slug = p.get("slug", "")
        base = _pub_base_url(publication)
        post_url = f"{base}/p/{slug}"
        audience = p.get("audience", "everyone")

        lines.append(f"### {title}")
        if subtitle:
            lines.append(f"*{subtitle}*")
        meta_parts = [f"date: {date}"] if date else []
        if audience != "everyone":
            meta_parts.append(f"audience: {audience}")
        if meta_parts:
            lines.append(" | ".join(meta_parts))
        lines.append(f"[read]({post_url})")
        lines.append("---")

        post_summaries.append({
            "title": title,
            "subtitle": subtitle,
            "date": date,
            "slug": slug,
            "url": post_url,
            "audience": audience,
        })

    pub_url = _pub_base_url(publication)
    return ReadResult(
        url=pub_url,
        text="\n".join(lines),
        title=f"{publication} (recent posts)",
        source_type="substack",
        raw=post_summaries,
    )


async def _read_post(publication: str, slug: str) -> ReadResult:
    """Fetch a specific post's content."""
    base = _pub_base_url(publication)
    api_url = f"{base}/api/v1/posts/{slug}"
    post = await _fetch_api(api_url)

    title = post.get("title", "Untitled")
    subtitle = post.get("subtitle", "")
    body_html = post.get("body_html", "")
    date = post.get("post_date", "")[:10] if post.get("post_date") else ""
    author_name = ""
    if post.get("publishedBylines"):
        author_name = post["publishedBylines"][0].get("name", "")

    # Convert HTML body to markdown
    body_md = md(body_html, strip=["img", "figure", "figcaption"]).strip() if body_html else ""

    text_parts = [f"# {title}"]
    if subtitle:
        text_parts.append(f"*{subtitle}*")
    meta = []
    if author_name:
        meta.append(f"author: {author_name}")
    if date:
        meta.append(f"date: {date}")
    if meta:
        text_parts.append(" | ".join(meta))
    text_parts.append("")
    text_parts.append(body_md)

    post_url = f"{_pub_base_url(publication)}/p/{slug}"

    published_at = None
    if post.get("post_date"):
        try:
            from datetime import datetime

            published_at = datetime.fromisoformat(post["post_date"].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    return ReadResult(
        url=post_url,
        text="\n".join(text_parts),
        title=title,
        source_type="substack",
        author=author_name or None,
        published_at=published_at,
        description=subtitle or None,
        raw={
            "title": title,
            "subtitle": subtitle,
            "slug": slug,
            "date": date,
            "author": author_name,
            "word_count": post.get("wordcount"),
        },
    )


# ---------------------------------------------------------------------------
# Search API
# ---------------------------------------------------------------------------


async def search_substack(
    query: str,
    *,
    page: int = 0,
    filter_type: str = "all",
) -> ReadResult:
    """
    Search Substack posts across all publications.

    Args:
        query: Search query string.
        page: Pagination (0-indexed).
        filter_type: "all", "top", or "recent".
    """
    try:
        data = await _fetch_api(
            "https://substack.com/api/v1/post/search",
            params={
                "query": query,
                "page": page,
                "includePlatformResults": True,
                "filter": filter_type,
            },
        )

        response = SearchPostsResponse.model_validate(data)
        posts = response.results

        if not posts:
            return ReadResult(
                url=f"substack://search?q={query}",
                text=f"No results for: {query}",
                title=f"Substack search: {query}",
                source_type="substack",
                raw=[],
            )

        lines: list[str] = []
        raw_items: list[dict] = []

        for p in posts:
            pub_name = p.publication.name if p.publication else "Unknown"
            authors = ", ".join(b.name for b in p.publishedBylines) if p.publishedBylines else ""
            date = p.post_date[:10] if p.post_date else ""

            lines.append(f"### {p.title}")
            if p.subtitle:
                lines.append(f"*{p.subtitle}*")
            meta = [f"date: {date}", f"source: {pub_name}"]
            if authors:
                meta.append(f"by: {authors}")
            if p.reaction_count:
                meta.append(f"reactions: {p.reaction_count}")
            lines.append(" | ".join(meta))
            snippet = p.truncated_body_text or p.description or ""
            if snippet:
                lines.append(snippet[:300])
            lines.append(f"[read]({p.canonical_url})")
            lines.append("---")

            raw_items.append(p.model_dump())

        return ReadResult(
            url=f"substack://search?q={query}&page={page}",
            text="\n".join(lines),
            title=f"Substack search: {query} ({len(posts)} results)",
            source_type="substack",
            raw=raw_items,
        )

    except Exception as e:
        logger.error(f"Substack search failed: {e}")
        return ReadResult.fail(
            f"substack://search?q={query}", str(e), source_type="substack"
        )


# ---------------------------------------------------------------------------
# Explore API
# ---------------------------------------------------------------------------

# Well-known Substack explore tab IDs
EXPLORE_TABS = {
    "finance": 153,
    "technology": 5,
    "politics": 15,
    "culture": 1,
    "business": 4,
    "science": 36,
    "health": 27,
}


def _normalize_pub_url(pub: Publication) -> str:
    if pub.custom_domain:
        domain = pub.custom_domain.strip()
        if domain:
            return domain if domain.startswith("http") else f"https://{domain}"
    return f"https://{pub.subdomain}.substack.com"


def _extract_explore_items(explore: ExploreResponse) -> dict:
    """Extract notes and attached posts from explore response."""
    notes: list[dict] = []
    posts: list[dict] = []
    seen_post_ids: set[int] = set()

    for item in explore.items:
        if item.type != "comment" or not item.comment:
            continue

        c = item.comment
        if not c.user_primary_publication and item.publication:
            c.user_primary_publication = item.publication

        notes.append({
            "id": c.id,
            "author": c.name or c.handle or "",
            "handle": c.handle,
            "body": c.body or "",
            "date": c.date,
            "reactions": c.reaction_count or 0,
            "restacks": c.restacks or 0,
            "publication": c.user_primary_publication.name if c.user_primary_publication else None,
        })

        if c.attachments:
            for att in c.attachments:
                if att.type == "post" and att.post and att.post.id not in seen_post_ids:
                    p = att.post
                    if not p.publication and p.publishedBylines:
                        for bl in p.publishedBylines:
                            if bl.primary_publication:
                                p.publication = bl.primary_publication
                                break
                    seen_post_ids.add(p.id)
                    posts.append(p.model_dump())

    return {"notes": notes, "posts": posts}


async def explore_substack(
    tab: str = "finance",
    *,
    tab_id: Optional[int] = None,
) -> ReadResult:
    """
    Browse curated content from Substack's explore feed.

    Args:
        tab: Category name (finance, technology, politics, culture, business, science, health).
        tab_id: Override with a specific tab ID.
    """
    tid = tab_id or EXPLORE_TABS.get(tab.lower(), 153)

    try:
        data = await _fetch_api(
            "https://substack.com/api/v1/search/explore/web",
            params={"tab": tid, "type": "category"},
        )

        explore = ExploreResponse.model_validate(data)
        extracted = _extract_explore_items(explore)

        lines: list[str] = []

        if extracted["notes"]:
            lines.append(f"## Notes ({len(extracted['notes'])})")
            for n in extracted["notes"]:
                author = n["author"]
                pub = f" ({n['publication']})" if n.get("publication") else ""
                lines.append(f"**{author}**{pub}")
                body = n["body"]
                if len(body) > 300:
                    body = body[:300] + "..."
                lines.append(body)
                meta = []
                if n["date"]:
                    meta.append(n["date"][:10])
                if n["reactions"]:
                    meta.append(f"reactions: {n['reactions']}")
                if meta:
                    lines.append(" | ".join(meta))
                lines.append("---")

        if extracted["posts"]:
            lines.append(f"## Posts ({len(extracted['posts'])})")
            for p in extracted["posts"]:
                lines.append(f"### {p['title']}")
                if p.get("subtitle"):
                    lines.append(f"*{p['subtitle']}*")
                pub_name = p["publication"]["name"] if p.get("publication") else "Unknown"
                date = p.get("post_date", "")[:10]
                lines.append(f"date: {date} | source: {pub_name}")
                lines.append(f"[read]({p['canonical_url']})")
                lines.append("---")

        return ReadResult(
            url=f"substack://explore?tab={tab}&tab_id={tid}",
            text="\n".join(lines),
            title=f"Substack explore: {tab} ({len(extracted['notes'])} notes, {len(extracted['posts'])} posts)",
            source_type="substack",
            raw=extracted,
        )

    except Exception as e:
        logger.error(f"Substack explore failed: {e}")
        return ReadResult.fail(
            f"substack://explore?tab={tab}", str(e), source_type="substack"
        )


# ---------------------------------------------------------------------------
# Notes API (per publication)
# ---------------------------------------------------------------------------

# Notes are a separate content stream from posts: they never appear in the RSS
# feed or in /api/v1/posts, so a publication watched by the `rss` reader is
# missing all of them. Public endpoint, no auth.
NOTES_PAGE_SIZE = 20
NOTES_MAX_PAGES = 25  # ~500 notes; the ceiling exists so a bad cursor can't loop


def _note_url(handle: str, note_id: int) -> str:
    return f"https://substack.com/@{handle}/note/c-{note_id}"


def _note_title(body: str) -> str:
    """First line of the note, as its display title.

    Notes have no title field. This author brackets one in 《》 on the first
    line, but that is a habit, not a rule — so take the first non-empty line
    whatever it looks like, and let the body carry the rest.
    """
    for line in (body or "").splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return ""


def _parse_note_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def list_notes(
    url: Optional[str] = None,
    *,
    publication: Optional[str] = None,
    published_after: Optional[datetime] = None,
    max_pages: int = NOTES_MAX_PAGES,
) -> List[ReadResult]:
    """One ReadResult per note the publication itself posted, newest first.

    Mirrors `read_rss_entries`: entry-level results the fetch layer can archive
    one by one. Restacks of other people's notes are dropped — the watchlist
    entry is about this author, and a restack's text is someone else's.

    `published_after` stops paging as soon as the feed goes older than the
    window, so a daily run costs one page.
    """
    pub = publication or (_extract_subdomain(url) if url else None)
    if not pub:
        return [ReadResult.fail(url or "substack://notes",
                                "Could not determine Substack publication",
                                source_type="substack")]

    handle = pub.split(".")[0]
    api_url = f"{_pub_base_url(pub)}/api/v1/notes"
    results: list[ReadResult] = []
    seen: set[int] = set()
    cursor: Optional[str] = None

    for _ in range(max_pages):
        params = {"limit": NOTES_PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        try:
            page = NotesResponse.model_validate(await _fetch_api(api_url, params=params))
        except Exception as e:
            logger.error(f"Substack notes fetch failed for {pub}: {e}")
            results.append(ReadResult.fail(api_url, str(e), source_type="substack"))
            break

        if not page.items:
            break

        reached_window_end = False
        for item in page.items:
            c = item.comment
            if c is None or c.id in seen:
                continue
            seen.add(c.id)
            if (c.handle or "").lower() != handle.lower():
                continue  # restack / reply from another author
            published_at = _parse_note_date(c.date)
            if published_after and published_at and published_at < published_after:
                reached_window_end = True
                continue
            body = c.body or ""
            if not body.strip():
                continue
            results.append(ReadResult(
                url=_note_url(handle, c.id),
                text=body,
                title=_note_title(body),
                source_type="substack",
                author=c.name or handle,
                published_at=published_at,
            ))

        cursor = page.nextCursor
        if reached_window_end or not cursor:
            break

    return results
