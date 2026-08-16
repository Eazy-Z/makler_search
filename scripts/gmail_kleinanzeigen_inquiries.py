from __future__ import annotations

import argparse
import base64
import re
import sys
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
BROKER_RE = re.compile(r"Du hast\s+(.*?)\s+zu folgender Immobilie kontaktiert", re.I | re.S)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Listet Makler aus Kleinanzeigen-Immobilienanfragen.")
    parser.add_argument("--query", default='subject:"Deine Immobilienanfrage bei Kleinanzeigen"')
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

    print(f"Treffer mit Betreff: {len(messages)}")
    for item in messages:
        raw = gmail.users().messages().get(userId="me", id=item["id"], format="raw").execute()
        parsed = parse_message(base64.urlsafe_b64decode(raw["raw"]))
        date = parsed["date"]
        try:
            date = parsedate_to_datetime(date).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            date = date[:10]
        body = clean(parsed["body"])
        match = BROKER_RE.search(body)
        if match:
            broker = clean(match.group(1))
            after = clean(body[match.end():])
            object_text = after.split("Der Anbieter", 1)[0].strip()
            print(f"{date} | {broker} | {object_text[:180]}")
        else:
            print(f"{date} | NICHT AUSGELESEN | {body[:180]}")


if __name__ == "__main__":
    main()
