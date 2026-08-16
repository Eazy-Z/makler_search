from __future__ import annotations

import argparse
import os

from mcp.server.fastmcp import FastMCP

from ymail_imap import ImapMailbox


mcp = FastMCP("ymail-imap")


@mcp.tool()
def list_folders() -> list[str]:
    """List available Yahoo Mail folders."""
    with ImapMailbox("INBOX") as mailbox:
        return mailbox.folders()


@mcp.tool()
def search_emails(
    sender: str = "",
    subject: str = "",
    text: str = "",
    folder: str = "INBOX",
    limit: int = 50,
) -> list[dict[str, str]]:
    """Search Yahoo Mail and return message metadata with a short body preview."""
    if not 1 <= limit <= 200:
        raise ValueError("limit muss zwischen 1 und 200 liegen.")
    criteria = []
    if sender:
        criteria += ["FROM", sender]
    if subject:
        criteria += ["SUBJECT", subject]
    if text:
        criteria += ["BODY", text]
    with ImapMailbox(folder) as mailbox:
        message_ids = mailbox.search(criteria)
        results = []
        for message_id in reversed(message_ids):
            headers = mailbox.fetch_headers(message_id) if subject else None
            if headers and subject.casefold() not in headers["subject"].casefold():
                continue
            message = mailbox.fetch(message_id)
            results.append(
                {
                    "id": message_id,
                    "date": message["date"],
                    "from": message["from"],
                    "to": message["to"],
                    "subject": message["subject"],
                    "body_preview": message["body"][:1200],
                }
            )
            if len(results) >= limit:
                break
        return results


@mcp.tool()
def get_email(message_id: str, folder: str = "INBOX") -> dict[str, str]:
    """Read one complete Yahoo Mail message by IMAP UID."""
    with ImapMailbox(folder) as mailbox:
        return {"id": message_id, **mailbox.fetch(message_id)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read Yahoo Mail through MCP over stdio.")
    parser.parse_args()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()