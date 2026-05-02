"""Integration-style tests for the email reader.

These tests run through the public read_email() API with a fake IMAP backend so
the parsing, filtering, and rendering paths are exercised end to end without
requiring real mailbox credentials.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from web_reader.models import EmailRequest
from web_reader.readers import email as email_reader


def _build_message(
    *,
    subject: str,
    sender: str,
    sent_at: datetime,
    plain: str | None = None,
    html: str | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "reader@example.com"
    msg["Date"] = sent_at.strftime("%a, %d %b %Y %H:%M:%S +0000")

    if html and plain:
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
    elif html:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(plain or "")

    return msg.as_bytes()


class _FakeIMAP4SSL:
    def __init__(self, server: str):
        self.server = server
        self.messages = {
            b"1": _build_message(
                subject="Daily Market Note",
                sender="alerts@example.com",
                sent_at=datetime(2025, 3, 1, 8, 0, tzinfo=UTC),
                plain="Plain fallback",
                html=(
                    "<html><body><article><h1>Daily Market Note</h1>"
                    "<p>Stocks moved higher today.</p></article></body></html>"
                ),
            ),
            b"2": _build_message(
                subject="Team Update",
                sender="team@example.com",
                sent_at=datetime(2025, 3, 1, 9, 0, tzinfo=UTC),
                plain="Second message body",
            ),
        }

    def login(self, username: str, password: str):
        return "OK", [b"logged in"]

    def select(self, folder: str, readonly: bool = True):
        return "OK", [b"2"]

    def search(self, charset, criteria: str):
        return "OK", [b"1 2"]

    def fetch(self, id_list: str, parts: str):
        ids = id_list.split(",")
        payload = []
        for raw_id in ids:
            msg_id = raw_id.encode("utf-8")
            payload.append((msg_id, self.messages[msg_id]))
            payload.append(b")")
        return "OK", payload

    def logout(self):
        return "BYE", [b"logged out"]


@pytest.mark.asyncio
async def test_read_email_parses_filters_and_limits(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(email_reader.imaplib, "IMAP4_SSL", _FakeIMAP4SSL)

    request = EmailRequest(
        username="reader@example.com",
        password="secret",
        imap_server="imap.example.com",
        folder="INBOX",
        start_date=datetime(2025, 3, 1, 0, 0, tzinfo=UTC),
        end_date=datetime(2025, 3, 1, 23, 59, tzinfo=UTC),
        senders=["alerts@"],
        limit=1,
    )

    results = await email_reader.read_email(request)

    assert len(results) == 1
    result = results[0]
    assert result.success is True
    assert result.source_type == "email"
    assert result.title == "Daily Market Note"
    assert result.author == "alerts@example.com"
    assert "Stocks moved higher today." in result.text
    assert result.url == "email://reader@example.com@imap.example.com/INBOX#msg_0"
    assert result.raw["subject"] == "Daily Market Note"


@pytest.mark.asyncio
async def test_read_email_returns_error_when_backend_fails(monkeypatch: pytest.MonkeyPatch):
    class _BrokenIMAP4SSL:
        def __init__(self, server: str):
            raise RuntimeError("imap unavailable")

    monkeypatch.setattr(email_reader.imaplib, "IMAP4_SSL", _BrokenIMAP4SSL)

    request = EmailRequest(
        username="reader@example.com",
        password="secret",
        imap_server="imap.example.com",
        start_date=datetime(2025, 3, 1, 0, 0, tzinfo=UTC),
    )

    results = await email_reader.read_email(request)

    assert len(results) == 1
    assert results[0].success is False
    assert "imap unavailable" in (results[0].error or "")
