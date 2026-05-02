"""
Email reader — IMAP fetch with clean text extraction.

Uses trafilatura + markitdown for HTML-to-markdown conversion.
Runs blocking IMAP I/O in a thread via asyncio.to_thread.
"""

import asyncio
import email as email_lib
import email.message
import hashlib
import imaplib
import io
import json
import logging
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel

from ..models import EmailRequest, ReadResult

logger = logging.getLogger(__name__)


class _EmailMessage(BaseModel):
    """Internal model for parsed email."""

    id: str
    subject: str
    sender: str
    date: str
    body_text: str


# --- Header / Body parsing ---


def _decode_header_str(text: Optional[str]) -> str:
    if not text:
        return ""
    decoded_list = decode_header(text)
    parts = []
    for decoded, charset in decoded_list:
        if isinstance(decoded, bytes):
            try:
                parts.append(decoded.decode(charset or "utf-8"))
            except Exception:
                parts.append(decoded.decode("utf-8", errors="ignore"))
        else:
            parts.append(str(decoded))
    return "".join(parts)


def _parse_email_body(msg: email_lib.message.Message) -> str:
    """
    Extract email body. Priority: trafilatura(HTML) > markitdown(HTML) > plain text.
    """
    raw_text = ""
    raw_html = None

    if msg.is_multipart():
        for part in msg.walk():
            if "attachment" in str(part.get("Content-Disposition")):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                decoded = payload.decode("utf-8", errors="replace")

            ct = part.get_content_type()
            if ct == "text/plain" and not raw_text:
                raw_text = decoded
            elif ct == "text/html" and not raw_html:
                raw_html = decoded
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except Exception:
                text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                raw_html = text
            else:
                raw_text = text

    # Strategy 1: trafilatura (best for noise removal)
    if raw_html:
        try:
            import trafilatura

            extracted = trafilatura.extract(
                raw_html, include_links=True, include_images=False, output_format="markdown"
            )
            if extracted:
                return extracted
        except Exception as e:
            logger.warning(f"Trafilatura failed: {e}")

    # Strategy 2: markitdown (fallback)
    if raw_html:
        try:
            from markitdown import MarkItDown, StreamInfo

            md = MarkItDown()
            stream = io.BytesIO(raw_html.encode("utf-8"))
            result = md.convert_stream(
                stream, stream_info=StreamInfo(extension=".html", mimetype="text/html")
            )
            if result.markdown.strip():
                return result.markdown.strip()
        except Exception as e:
            logger.warning(f"MarkItDown failed: {e}")

    # Strategy 3: plain text
    return raw_text or ""


def _parse_raw_messages(messages: list) -> List[_EmailMessage]:
    """Parse IMAP message tuples into EmailMessage models."""
    results = []
    for i in range(0, len(messages), 2):
        try:
            msg_part = messages[i]
            if isinstance(msg_part, tuple) and len(msg_part) >= 2:
                raw_email = msg_part[1]
            else:
                continue

            msg = email_lib.message_from_bytes(raw_email)
            sender = _decode_header_str(msg.get("From"))
            subject = _decode_header_str(msg.get("Subject"))
            date_str = msg.get("Date") or formatdate(localtime=True)
            body = _parse_email_body(msg)

            results.append(
                _EmailMessage(
                    id=f"msg_{i // 2}",
                    sender=sender,
                    subject=subject,
                    date=date_str,
                    body_text=body,
                )
            )
        except Exception as e:
            logger.warning(f"Failed to parse email index {i}: {e}")
    return results


def _filter_by_sender(
    emails: List[_EmailMessage], senders: Optional[List[str]]
) -> List[_EmailMessage]:
    if not senders:
        return emails
    allowed = [s.lower() for s in senders]
    return [e for e in emails if any(a in e.sender.lower() for a in allowed)]


# --- IMAP Fetch (blocking) ---


def _fetch_emails_sync(
    username: str,
    password: str,
    imap_server: str,
    start_date: datetime,
    end_date: Optional[datetime] = None,
    folder: str = "INBOX",
) -> List[_EmailMessage]:
    """Blocking IMAP fetch."""
    effective_end = end_date or datetime.now()

    mail = imaplib.IMAP4_SSL(imap_server)
    try:
        mail.login(username, password)
        mail.select(folder, readonly=True)

        fmt_start = start_date.strftime("%d-%b-%Y")
        fmt_end = (effective_end.date() + timedelta(days=1)).strftime("%d-%b-%Y")
        criteria = f'(SINCE "{fmt_start}" BEFORE "{fmt_end}")'

        status, data = mail.search(None, criteria)
        if status != "OK" or not data[0]:
            return []

        ids = data[0].split()
        all_emails = []
        batch_size = 25

        for i in range(0, len(ids), batch_size):
            batch = ids[i : i + batch_size]
            id_list = b",".join(batch).decode("utf-8")
            status, messages = mail.fetch(id_list, "(RFC822)")
            if status == "OK":
                all_emails.extend(_parse_raw_messages(messages))

        # Precision date filter (IMAP only does date, not datetime)
        filtered = []
        for em in all_emails:
            try:
                msg_dt = parsedate_to_datetime(em.date)
                # Normalize timezone awareness
                cmp_start = start_date
                cmp_end = end_date
                if msg_dt.tzinfo is not None:
                    if cmp_start and cmp_start.tzinfo is None:
                        cmp_start = cmp_start.astimezone()
                    if cmp_end and cmp_end.tzinfo is None:
                        cmp_end = cmp_end.astimezone()
                else:
                    if cmp_start and cmp_start.tzinfo is not None:
                        cmp_start = cmp_start.replace(tzinfo=None)
                    if cmp_end and cmp_end.tzinfo is not None:
                        cmp_end = cmp_end.replace(tzinfo=None)

                if cmp_start and msg_dt < cmp_start:
                    continue
                if cmp_end and msg_dt > cmp_end:
                    continue
                filtered.append(em)
            except Exception:
                continue

        return filtered
    finally:
        try:
            mail.logout()
        except Exception:
            pass


# --- Public API ---


async def read_email(request: EmailRequest) -> list[ReadResult]:
    """
    Read emails via IMAP. Returns one ReadResult per email.

    Args:
        request: EmailRequest with credentials and filters.
    """
    if not request.start_date:
        return [ReadResult.fail("email://", "start_date is required", source_type="email")]

    try:
        emails = await asyncio.to_thread(
            _fetch_emails_sync,
            username=request.username,
            password=request.password,
            imap_server=request.imap_server,
            start_date=request.start_date,
            end_date=request.end_date,
            folder=request.folder,
        )

        emails = _filter_by_sender(emails, request.senders)

        if request.limit and len(emails) > request.limit:
            emails = emails[: request.limit]

        results = []
        for em in emails:
            # Render as markdown
            text = f"# {em.subject}\n**From:** {em.sender}\n**Date:** {em.date}\n\n{em.body_text or '(No Content)'}"

            email_url = f"email://{request.username}@{request.imap_server}/{request.folder}#{em.id}"

            results.append(
                ReadResult(
                    url=email_url,
                    text=text,
                    title=em.subject,
                    source_type="email",
                    author=em.sender,
                    raw=em.model_dump(),
                )
            )

        return results

    except Exception as e:
        logger.error(f"Email read failed: {e}", exc_info=True)
        return [ReadResult.fail("email://", str(e), source_type="email")]
