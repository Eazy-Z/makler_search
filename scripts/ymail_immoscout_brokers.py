from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ymail_test import Ymail, parse_message


BROKER_RE = re.compile(
    r"(?:einen Immobilienberater|eine Immobilienberaterin)\s+von Firma\s+(.+?)\s+per E-Mail kontaktiert",
    re.I | re.S,
)
BROKER_PATTERNS = [
    BROKER_RE,
    re.compile(r"\bFirma\s+(.+?)\s+Anrede\b", re.I),
    re.compile(r"\bMakler\s*:\s*([^\r\n]+)", re.I),
]


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .,:;\t\r\n")


def date_only(value: str) -> str:
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return value[:10]


def extract_broker(body: str) -> str | None:
    for pattern in BROKER_PATTERNS:
        match = pattern.search(body)
        if match:
            return clean(match.group(1))
    return None


def context(body: str, term: str) -> str:
    position = body.casefold().find(term.casefold())
    if position < 0:
        position = 0
    return body[max(0, position - 180):position + 500].strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Listet Makler aus Yahoo-ImmoScout24-Mails.")
    parser.add_argument("--from-address", default="noreply@immobilienscout24.de")
    parser.add_argument("--term", default="Ihre Kontaktaufnahme zum Anbieter")
    parser.add_argument("--folder", default="INBOX")
    args = parser.parse_args()

    with Ymail(args.folder) as account:
        messages = account.search("FROM", args.from_address)
        brokers = Counter()
        rows = []
        term_matches = 0
        for message_id in messages:
            parsed = parse_message(account.fetch(message_id))
            body = re.sub(r"\s+", " ", parsed["body"])
            searchable = f"{parsed['subject']} {body}"
            has_term = args.term.casefold() in searchable.casefold()
            if not has_term and args.term.casefold().replace("anbieter", "anbierter") not in searchable.casefold():
                continue
            term_matches += 1
            broker = extract_broker(body)
            date = date_only(parsed["date"])
            if broker:
                brokers[broker] += 1
                rows.append((date, broker, parsed["subject"]))
            else:
                rows.append((date, "NICHT AUSGELESEN", context(body, args.term)))

    print(f"Nachrichten vom Absender: {len(messages)}")
    print(f"Treffer mit Suchbegriff: {term_matches}")
    print(f"Eindeutige genannte Anbieter: {len(brokers)}")
    for broker, count in sorted(brokers.items(), key=lambda item: (-item[1], item[0].lower())):
        dates = [date for date, name, _ in rows if name == broker]
        print(f"{broker} | {count} Nachricht(en) | {min(dates)} bis {max(dates)}")
    for date, broker, subject in sorted((row for row in rows if row[1] == "NICHT AUSGELESEN"), reverse=True):
        print(f"NICHT AUSGELESEN | {date} | {subject}")


if __name__ == "__main__":
    main()