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
BROKER_RE = re.compile(
    r"(?:einen Immobilienberater|eine Immobilienberaterin)\s+von Firma\s+(.+?)\s+per E-Mail kontaktiert",
    re.I | re.S,
)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .,:;\t\r\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Listet Makler aus ImmoScout24-Bewertungsmails.")
    parser.add_argument("--query", default="from:anbieterbewertung@immobilienscout24.de")
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
    rows = []
    for item in messages:
        raw = gmail.users().messages().get(userId="me", id=item["id"], format="raw").execute()
        parsed = parse_message(base64.urlsafe_b64decode(raw["raw"]))
        body = re.sub(r"\s+", " ", parsed["body"])
        match = BROKER_RE.search(body)
        date = parsed["date"]
        try:
            date = parsedate_to_datetime(date).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            date = date[:10]
        if match:
            broker = clean(match.group(1))
            brokers[broker] += 1
            rows.append((date, broker, parsed["subject"]))
        else:
            rows.append((date, "NICHT AUSGELESEN", parsed["subject"]))

    print(f"Nachrichten vom Absender: {len(messages)}")
    print(f"Eindeutige genannte Anbieter: {len(brokers)}")
    for broker, count in sorted(brokers.items(), key=lambda item: (-item[1], item[0].lower())):
        dates = [date for date, name, _ in rows if name == broker]
        print(f"{broker} | {count} Nachricht(en) | {min(dates)} bis {max(dates)}")
    for date, broker, subject in sorted((row for row in rows if row[1] == "NICHT AUSGELESEN"), reverse=True):
        print(f"NICHT AUSGELESEN | {date} | {subject}")


if __name__ == "__main__":
    main()
