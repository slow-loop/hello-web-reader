"""Substack Notes reader — the parts that don't need the network."""

from datetime import datetime, timezone

import pytest

from web_reader.readers.substack import (
    NotesResponse,
    _note_title,
    _note_url,
    _parse_note_date,
    list_notes,
)


def _page(*comments, cursor=None):
    return {
        "items": [{"type": "comment", "comment": c} for c in comments],
        "nextCursor": cursor,
    }


def _note(nid, handle="acme", body="hello", date="2026-09-03T09:00:00.000Z"):
    return {"id": nid, "handle": handle, "name": "Acme", "body": body, "date": date}


def test_note_url_is_the_canonical_permalink():
    assert _note_url("acme", 123) == "https://substack.com/@acme/note/c-123"


def test_note_title_takes_the_first_non_empty_line():
    assert _note_title("\n\n《標題》\n內文") == "《標題》"
    assert _note_title("no brackets here\nrest") == "no brackets here"
    assert _note_title("   ") == ""


def test_parse_note_date_handles_the_z_suffix():
    assert _parse_note_date("2026-09-03T09:00:00.000Z") == datetime(
        2026, 9, 3, 9, 0, tzinfo=timezone.utc
    )
    assert _parse_note_date(None) is None
    assert _parse_note_date("not a date") is None


def test_notes_response_tolerates_unknown_fields():
    r = NotesResponse.model_validate(_page(_note(1), cursor="abc"))
    assert r.nextCursor == "abc"
    assert r.items[0].comment.id == 1


@pytest.mark.asyncio
async def test_lists_own_notes_and_drops_other_authors(monkeypatch):
    async def fake(url, timeout=30.0, params=None):
        return _page(_note(1), _note(2, handle="someone-else"))

    monkeypatch.setattr("web_reader.readers.substack._fetch_api", fake)
    out = await list_notes("https://acme.substack.com")
    assert [r.url for r in out] == ["https://substack.com/@acme/note/c-1"]


@pytest.mark.asyncio
async def test_stops_paging_once_the_feed_predates_the_window(monkeypatch):
    pages = [
        _page(_note(1, date="2026-09-03T09:00:00.000Z"), cursor="p2"),
        _page(_note(2, date="2026-01-01T09:00:00.000Z"), cursor="p3"),
        _page(_note(3, date="2025-12-01T09:00:00.000Z")),
    ]
    calls = []

    async def fake(url, timeout=30.0, params=None):
        calls.append((params or {}).get("cursor"))
        return pages[len(calls) - 1]

    monkeypatch.setattr("web_reader.readers.substack._fetch_api", fake)
    since = datetime(2026, 8, 1, tzinfo=timezone.utc)
    out = await list_notes("https://acme.substack.com", published_after=since)

    assert [r.url for r in out] == ["https://substack.com/@acme/note/c-1"]
    assert calls == [None, "p2"], "第三頁不該被抓——第二頁已經越過窗口下緣"


@pytest.mark.asyncio
async def test_skips_empty_bodies_and_deduplicates_ids(monkeypatch):
    async def fake(url, timeout=30.0, params=None):
        return _page(_note(1), _note(1), _note(2, body="   "))

    monkeypatch.setattr("web_reader.readers.substack._fetch_api", fake)
    out = await list_notes("https://acme.substack.com")
    assert len(out) == 1


@pytest.mark.asyncio
async def test_unresolvable_publication_fails_without_raising():
    out = await list_notes("not-a-url")
    assert len(out) == 1 and not out[0].ok


@pytest.mark.asyncio
async def test_api_error_surfaces_as_a_failed_result(monkeypatch):
    async def boom(url, timeout=30.0, params=None):
        raise RuntimeError("503")

    monkeypatch.setattr("web_reader.readers.substack._fetch_api", boom)
    out = await list_notes("https://acme.substack.com")
    assert len(out) == 1 and not out[0].ok and "503" in out[0].error
