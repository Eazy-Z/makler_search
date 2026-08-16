from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ymail_test import Ymail, parse_message


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
    parser = argparse.ArgumentParser(description="Listet Makler aus Yahoo-Immowelt-Mails.")
    parser.add_argument("--from-address", default="maklerbewertung@immowelt.de")
    parser.add_argument("--folder", default="INBOX")
    args = parser.parse_args()

    with Ymail(args.folder) as account:
        messages = account.search("FROM", args.from_address)
        brokers = Counter()
        unmatched = []
        for message_id in messages:
            parsed = parse_message(account.fetch(message_id))
            body = re.sub(r"\s+", " ", parsed["body"])
            broker = next((clean(match.group(1)) for pattern in PATTERNS if (match := pattern.search(body))), None)
            if broker:
                brokers[broker] += 1
            else:
                context = []
                for keyword in ("Makler", "Anbieter", "kontaktiert", "bewerten"):
                    position = body.lower().find(keyword.lower())
                    if position >= 0:
                        context.append(body[max(0, position - 80):position + 220])
                unmatched.append((parsed["date"][:10], " | ".join(context)[:500]))

    print(f"Nachrichten vom Absender: {len(messages)}")
    print(f"Eindeutige erkannte Anbieter: {len(brokers)}")
    for broker, count in sorted(brokers.items(), key=lambda item: (-item[1], item[0].lower())):
        print(f"{broker} | {count} Nachricht(en)")
    for date, context in sorted(unmatched, reverse=True):
        print(f"NICHT AUSGELESEN | {date} | {context}")


if __name__ == "__main__":
    main()