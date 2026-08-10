import argparse
import base64
import html
import re
from pathlib import Path
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"


def decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def decode_message_body(payload: dict) -> str:
    data = payload.get("body", {}).get("data")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        body = decode_message_body(part)
        if body:
            return body
    return ""


def html_to_text(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</p\s*>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(value)).strip()


def parse_message(raw_message: bytes) -> dict[str, str]:
    message = message_from_bytes(raw_message)
    body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            content = part.get_payload(decode=True)
            if not content:
                continue
            text = content.decode(part.get_content_charset() or "utf-8", errors="replace")
            if part.get_content_type() == "text/plain":
                body = text
                break
            if part.get_content_type() == "text/html" and not body:
                body = html_to_text(text)
    else:
        content = message.get_payload(decode=True) or b""
        body = content.decode(message.get_content_charset() or "utf-8", errors="replace")
        if message.get_content_type() == "text/html":
            body = html_to_text(body)

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
        "body": body.strip(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Analysiert gesendete Makler-Search-Mails.")
    parser.add_argument("--max-results", type=int, default=20)
    parser.add_argument("--query", default="in:sent")
    args = parser.parse_args()

    credentials = None

    if TOKEN_FILE.exists():
        credentials = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if credentials is None or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE,
                SCOPES,
            )
            credentials = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(credentials.to_json())

    gmail = build("gmail", "v1", credentials=credentials)
    profile = gmail.users().getProfile(userId="me").execute()
    messages = gmail.users().messages().list(
        userId="me",
        q=args.query,
        maxResults=args.max_results,
    ).execute()

    print(f"Verbunden mit: {profile['emailAddress']}")
    message_ids = messages.get("messages", [])
    print(f"Gesendete Nachrichten gefunden: {len(message_ids)}")
    for message in message_ids:
        raw = gmail.users().messages().get(
            userId="me",
            id=message["id"],
            format="raw",
        ).execute()
        details = parse_message(base64.urlsafe_b64decode(raw["raw"]))
        print(f"\n--- {details['date']} | {details['subject']} ---")
        print(f"Von: {details['from']}")
        print(f"An: {details['to']}")
        print(details["body"][:4000])


if __name__ == "__main__":
    main()