from __future__ import annotations

import argparse
import base64
import re
import sys
from collections import Counter
from email.utils import parsedate_to_datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gmail_test import parse_message

BASE_DIR = Path(__file__).resolve().parents[1]
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = BASE_DIR / "token.json"
PATTERNS = [
    re.compile(r"abgibst\.\s*(.+?)\s+Standort der Immobilie:", re.I | re.S),
    re.compile(r"abgeben\.\s*(.+?)\s+Standort der Immobilie:", re.I | re.S),
    re.compile(r"(?:Makler|Anbieter)\s*(?:namens|:)?\s*(.+?)(?:\s+(?:bewerten|kontaktiert|zu bewerten))", re.I),
    re.compile(r"(?:von|bei)\s+(.+?)\s+(?:kontaktiert|geschrieben)", re.I),
]


def clean(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" .,:;\t\r\n")
    return re.sub(r"\s+Makler$", "", value, flags=re.I)


def main() -> None:
    parser = argparse.ArgumentParser(description="Listet Makler aus Immowelt-Bewertungsmails.")
    parser.add_argument("--query", default="from:maklerbewertung@immowelt.de")
    args = parser.parse_args()

    credentials = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        TOKEN_FILE.write_text(credentials.to_json())

    gmail = build("gmail", "v1", credentials=credentials)
    messages = []
    page_token = None
    while True:
        result = gmail.users().messages().list(
            userId="me", q=args.query, maxResults=500, pageToken=page_token
        ).execute()
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    brokers = Counter()
    unmatched = []
    for item in messages:
        raw = gmail.users().messages().get(userId="me", id=item["id"], format="raw").execute()
        parsed = parse_message(base64.urlsafe_b64decode(raw["raw"]))
        body = re.sub(r"\s+", " ", parsed["body"])
        broker = None
        for pattern in PATTERNS:
            match = pattern.search(body)
            if match:
                broker = clean(match.group(1))
                break
        date = parsed["date"]
        try:
            date = parsedate_to_datetime(date).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            date = date[:10]
        if broker:
            brokers[broker] += 1
        else:
            context = []
            for keyword in ("Makler", "Anbieter", "kontaktiert", "bewerten"):
                position = body.lower().find(keyword.lower())
                if position >= 0:
                    context.append(body[max(0, position - 80):position + 220])
            unmatched.append((date, " | ".join(context)[:500]))

    print(f"Nachrichten vom Absender: {len(messages)}")
    print(f"Eindeutige erkannte Anbieter: {len(brokers)}")
    for broker, count in sorted(brokers.items(), key=lambda item: (-item[1], item[0].lower())):
        print(f"{broker} | {count} Nachricht(en)")
    for date, context in sorted(unmatched, reverse=True):
        print(f"NICHT AUSGELESEN | {date} | {context}")


if __name__ == "__main__":
    main()
