from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from email.utils import getaddresses
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ymail_test import Ymail, parse_message


BASE_DIR = Path(__file__).resolve().parents[1]
BROKER_URLS = BASE_DIR / "broker-urls.txt"
KEYWORDS = re.compile(r"immobilien|makler|expos|besichtig|wohnung|haus|kauf", re.I)


def domain(value: str) -> str:
    return value.rsplit("@", 1)[-1].lower().removeprefix("www.")


def known_domains() -> set[str]:
    result = set()
    for line in BROKER_URLS.read_text(errors="replace").splitlines():
        if line.strip().startswith("http"):
            host = urlparse(line.strip()).hostname
            if host:
                result.add(host.lower().removeprefix("www."))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Findet historische Maklerkontakte aus Yahoo-Metadaten.")
    parser.add_argument("--folder", default="ALL")
    args = parser.parse_args()

    folders = ["INBOX", "Sent"] if args.folder == "ALL" else [args.folder]
    contacts = defaultdict(lambda: {"count": 0, "first": None, "last": None, "subjects": set(), "domains": set()})
    message_count = 0
    broker_domains = known_domains()
    for folder in folders:
        with Ymail(folder) as account:
            message_ids = account.search("ALL")
            message_count += len(message_ids)
            for message_id in message_ids:
                parsed = parse_message(account.fetch(message_id))
                addresses = getaddresses([parsed["from"], parsed["to"]])
                relevant = any(domain(address) in broker_domains for _, address in addresses if "@" in address)
                relevant = relevant or bool(KEYWORDS.search(parsed["subject"]))
                if not relevant:
                    continue
                date = parsed["date"][:10]
                for name, address in addresses:
                    if "@" not in address:
                        continue
                    address = address.lower()
                    item = contacts[address]
                    item["count"] += 1
                    item["first"] = min(filter(None, [item["first"], date]), default=date)
                    item["last"] = max(filter(None, [item["last"], date]), default=date)
                    item["domains"].add(domain(address))
                    if parsed["subject"]:
                        item["subjects"].add(parsed["subject"])
                    if name:
                        item.setdefault("names", set()).add(name)

    print(f"Durchsuchte Nachrichten: {message_count}")
    print(f"Kontaktkandidaten: {len(contacts)}")
    for address, item in sorted(contacts.items(), key=lambda pair: (-pair[1]["count"], pair[0])):
        names = ", ".join(sorted(item.get("names", set())))
        subjects = " | ".join(sorted(item["subjects"])[:3])
        print(f"{names or '-'} <{address}> | {item['count']} Nachrichten | {item['first']} bis {item['last']} | {subjects}")


if __name__ == "__main__":
    main()