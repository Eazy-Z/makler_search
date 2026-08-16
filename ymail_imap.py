from __future__ import annotations

import email
import imaplib
import os
import re
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from typing import Any


def decode_header_value(value: str | None) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except (UnicodeDecodeError, ValueError):
        return value or ""


def ascii_search_prefix(value: str) -> str:
    prefix = re.split(r"[^\x00-\x7f]", value, maxsplit=1)[0].rstrip()
    return prefix.rsplit(" ", 1)[0] if " " in prefix else prefix


def parse_message(raw_message: bytes) -> dict[str, str]:
    message = email.message_from_bytes(raw_message)
    parts = []
    if message.is_multipart():
        candidates = message.walk()
    else:
        candidates = [message]
    for part in candidates:
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain":
            parts.insert(0, text)
        elif not parts:
            parts.append(text)
    sent_at = message.get("Date", "")
    try:
        sent_at = parsedate_to_datetime(sent_at).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    return {
        "subject": decode_header_value(message.get("Subject")),
        "from": decode_header_value(message.get("From")),
        "to": decode_header_value(message.get("To")),
        "date": sent_at,
        "body": parts[0].strip() if parts else "",
    }


class ImapMailbox:
    def __init__(self, folder: str = "INBOX") -> None:
        address = os.environ.get("YMAIL_ADDRESS")
        password = os.environ.get("YMAIL_APP_PASSWORD")
        host = os.environ.get("YMAIL_IMAP_HOST", "imap.mail.yahoo.com")
        if not address or not password:
            raise RuntimeError("Setze YMAIL_ADDRESS und YMAIL_APP_PASSWORD.")
        self.connection = imaplib.IMAP4_SSL(host, int(os.environ.get("YMAIL_IMAP_PORT", "993")))
        self.connection._encoding = "utf-8"
        self.connection.login(address, password)
        status, _ = self.connection.select(folder, readonly=True)
        if status != "OK":
            self.close()
            raise RuntimeError(f"Yahoo-Ordner konnte nicht geöffnet werden: {folder}")

    def folders(self) -> list[str]:
        status, data = self.connection.list()
        if status != "OK":
            raise RuntimeError("Yahoo-Ordner konnten nicht gelesen werden.")
        folders = []
        for item in data or []:
            if item:
                line = item.decode("utf-8", errors="replace")
                match = re.search(r'(?:(?:"([^"]+)")|(\S+))\s*$', line)
                if match:
                    folders.append(match.group(1) or match.group(2))
        return folders

    def search(self, criteria: list[str]) -> list[str]:
        search_criteria = criteria or ["ALL"]
        quoted_criteria = [
            value if index % 2 == 0 else '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
            for index, value in enumerate(search_criteria)
        ]
        charset = "UTF-8" if any(not value.isascii() for value in quoted_criteria) else None
        try:
            status, data = self.connection.uid("search", charset, *quoted_criteria)
        except imaplib.IMAP4.error:
            if charset != "UTF-8":
                raise
            ascii_criteria = [
                value if index % 2 == 0 else '"' + ascii_search_prefix(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
                for index, value in enumerate(search_criteria)
            ]
            status, data = self.connection.uid("search", None, *ascii_criteria)
        if status != "OK":
            raise RuntimeError("Yahoo-Mail-Suche fehlgeschlagen.")
        return [item.decode("ascii") for item in (data[0].split() if data and data[0] else [])]

    def fetch(self, message_id: str) -> dict[str, str]:
        status, data = self.connection.uid("fetch", message_id, "(RFC822)")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError(f"Yahoo-Mail konnte nicht gelesen werden: {message_id}")
        return parse_message(data[0][1])

    def fetch_headers(self, message_id: str) -> dict[str, str]:
        status, data = self.connection.uid(
            "fetch", message_id, "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT)])"
        )
        if status != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError(f"Yahoo-Mail-Header konnte nicht gelesen werden: {message_id}")
        return parse_message(data[0][1])

    def close(self) -> None:
        try:
            self.connection.close()
        except imaplib.IMAP4.error:
            pass
        self.connection.logout()

    def __enter__(self) -> "ImapMailbox":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()