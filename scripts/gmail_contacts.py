from __future__ import annotations

import argparse
import re
from collections import defaultdict
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

BASE_DIR = Path(__file__).resolve().parents[1]
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
BROKER_URLS = BASE_DIR / "broker-urls.txt"
TOKEN_FILE = BASE_DIR / "token.json"
KEYWORDS = re.compile(r"immobilien|makler|expos|besichtig|wohnung|haus|kauf", re.I)


def header(headers: list[dict], name: str) -> str:
    return next(
        (item.get("value", "") for item in headers if item.get("name", "").lower() == name.lower()),
        "",
    )


def domain(value: str) -> str:
    return value.rsplit("@", 1)[-1].lower().removeprefix("www.")


def known_domains() -> set[str]:
    result = set()
    for line in BROKER_URLS.read_text(errors="replace").splitlines():
        if not line.strip().startswith("http"):
            continue
        host = urlparse(line.strip()).hostname
        if host:
            result.add(host.lower().removeprefix("www."))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Findet historische Maklerkontakte aus Gmail-Metadaten.")
    parser.add_argument("--query", default="in:anywhere {immobilien makler exposé besichtigung wohnung haus kauf}")
    parser.add_argument("--token-file", type=Path, default=TOKEN_FILE)
    args = parser.parse_args()

    credentials = Credentials.from_authorized_user_file(args.token_file, SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        args.token_file.write_text(credentials.to_json())

    gmail = build("gmail", "v1", credentials=credentials)
    broker_domains = known_domains()
    message_ids = []
    page_token = None
    while True:
        result = gmail.users().messages().list(
            userId="me", q=args.query, maxResults=500, pageToken=page_token
        ).execute()
        message_ids.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    contacts = defaultdict(lambda: {"count": 0, "first": None, "last": None, "subjects": set(), "domains": set()})
    for message in message_ids:
        metadata = gmail.users().messages().get(
            userId="me",
            id=message["id"],
            format="metadata",
            metadataHeaders=["From", "To", "Cc", "Subject", "Date"],
        ).execute()
        headers = metadata.get("payload", {}).get("headers", [])
        sender = header(headers, "From")
        recipients = " ".join(filter(None, [header(headers, "To"), header(headers, "Cc")]))
        subject = header(headers, "Subject")
        addresses = getaddresses([sender, recipients])
        relevant = any(domain(address) in broker_domains for _, address in addresses if "@" in address)
        relevant = relevant or bool(KEYWORDS.search(subject))
        if not relevant:
            continue
        date_value = header(headers, "Date")
        try:
            date = parsedate_to_datetime(date_value).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            date = date_value[:16]
        for name, address in addresses:
            if "@" not in address:
                continue
            address = address.lower()
            item = contacts[address]
            item["count"] += 1
            item["first"] = min(filter(None, [item["first"], date]), default=date)
            item["last"] = max(filter(None, [item["last"], date]), default=date)
            item["domains"].add(domain(address))
            if subject:
                item["subjects"].add(subject)
            if name:
                item.setdefault("names", set()).add(name)

    print(f"Durchsuchte Nachrichten: {len(message_ids)}")
    print(f"Kontaktkandidaten: {len(contacts)}")
    for address, item in sorted(contacts.items(), key=lambda pair: (-pair[1]["count"], pair[0])):
        names = ", ".join(sorted(item.get("names", set())))
        subjects = " | ".join(sorted(item["subjects"])[:3])
        print(f"{names or '-'} <{address}> | {item['count']} Nachrichten | {item['first']} bis {item['last']} | {subjects}")


if __name__ == "__main__":
    main()
