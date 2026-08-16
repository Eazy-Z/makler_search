from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ymail_test import Ymail, parse_message


BROKER_RE = re.compile(r"Du hast\s+(.*?)\s+zu folgender Immobilie kontaktiert", re.I | re.S)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Listet Makler aus Yahoo-Kleinanzeigen-Anfragen.")
    parser.add_argument("--subject", default="Deine Immobilienanfrage bei Kleinanzeigen")
    parser.add_argument("--folder", default="INBOX")
    args = parser.parse_args()

    with Ymail(args.folder) as account:
        messages = account.search("SUBJECT", args.subject)
        print(f"Treffer mit Betreff: {len(messages)}")
        for message_id in messages:
            parsed = parse_message(account.fetch(message_id))
            body = clean(parsed["body"])
            match = BROKER_RE.search(body)
            if match:
                broker = clean(match.group(1))
                after = clean(body[match.end():])
                object_text = after.split("Der Anbieter", 1)[0].strip()
                print(f"{parsed['date'][:10]} | {broker} | {object_text[:180]}")
            else:
                print(f"{parsed['date'][:10]} | NICHT AUSGELESEN | {body[:180]}")


if __name__ == "__main__":
    main()