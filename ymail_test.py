from __future__ import annotations

import argparse
import base64
import html
import imaplib
import json
import os
import re
import secrets
import time
import urllib.parse
import urllib.request
import webbrowser
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

IMAP_HOST = os.environ.get("YMAIL_IMAP_HOST", "imap.mail.yahoo.com")
IMAP_PORT = 993
OAUTH_AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
OAUTH_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
OAUTH_SCOPE = "mail-r"
BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "ymail_token.json"


def decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


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


class Ymail:
    def __init__(self, folder: str = "INBOX") -> None:
        address = os.environ.get("YMAIL_ADDRESS")
        if not address:
            raise RuntimeError("Setze YMAIL_ADDRESS, zum Beispiel melinamarrek@ymail.com.")
        self.connection = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        app_password = os.environ.get("YMAIL_APP_PASSWORD")
        if app_password:
            self.connection.login(address, app_password)
        else:
            access_token = get_access_token()
            auth = f"user={address}\x01auth=Bearer {access_token}\x01\x01"
            self.connection.authenticate("XOAUTH2", lambda _: auth.encode("utf-8"))
        status, _ = self.connection.select(folder, readonly=True)
        if status != "OK":
            self.close()
            raise RuntimeError(f"Yahoo-Ordner konnte nicht geöffnet werden: {folder}")

    def search(self, *criteria: str) -> list[bytes]:
        status, data = self.connection.uid("search", None, *(criteria or ("ALL",)))
        if status != "OK":
            raise RuntimeError("Yahoo-Mail-Suche fehlgeschlagen.")
        return data[0].split() if data and data[0] else []

    def fetch(self, message_id: bytes) -> bytes:
        status, data = self.connection.uid("fetch", message_id, "(RFC822)")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError(f"Yahoo-Mail konnte nicht gelesen werden: {message_id!r}")
        return data[0][1]

    def close(self) -> None:
        try:
            self.connection.close()
        except imaplib.IMAP4.error:
            pass
        self.connection.logout()

    def __enter__(self) -> "Ymail":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def oauth_config() -> tuple[str, str, str]:
    client_id = os.environ.get("YMAIL_CLIENT_ID")
    client_secret = os.environ.get("YMAIL_CLIENT_SECRET")
    redirect_uri = os.environ.get("YMAIL_REDIRECT_URI", "http://127.0.0.1:8765/")
    if not client_id or not client_secret:
        raise RuntimeError(
            "Setze YMAIL_CLIENT_ID und YMAIL_CLIENT_SECRET aus der Yahoo-App."
        )
    return client_id, client_secret, redirect_uri


def request_token(data: dict[str, str], client_id: str, client_secret: str) -> dict:
    encoded = urllib.parse.urlencode(data).encode("ascii")
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=encoded,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def save_token(token: dict) -> None:
    TOKEN_FILE.write_text(json.dumps(token, indent=2) + "\n")
    TOKEN_FILE.chmod(0o600)


def authorize(client_id: str, client_secret: str, redirect_uri: str) -> dict:
    redirect = urllib.parse.urlparse(redirect_uri)
    if redirect.hostname not in {"127.0.0.1", "localhost"} or not redirect.port:
        raise RuntimeError("YMAIL_REDIRECT_URI muss auf einen lokalen HTTP-Port zeigen.")

    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": OAUTH_SCOPE,
            "state": state,
        }
    )
    authorization_url = f"{OAUTH_AUTH_URL}?{query}"
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            values = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({key: values[key][0] for key in ("code", "state", "error") if key in values})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Yahoo-Anmeldung erhalten. Dieses Fenster kann geschlossen werden.")

        def log_message(self, *_: object) -> None:
            return

    server = HTTPServer((redirect.hostname, redirect.port), CallbackHandler)
    print("Yahoo-Anmeldung wird im Browser geoeffnet ...")
    webbrowser.open(authorization_url)
    server.handle_request()
    server.server_close()
    if result.get("state") != state:
        raise RuntimeError("Ungueltiger OAuth-State von Yahoo.")
    if result.get("error") or not result.get("code"):
        raise RuntimeError(f"Yahoo-OAuth-Anmeldung fehlgeschlagen: {result.get('error', 'kein Code')}")
    return request_token(
        {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri,
        },
        client_id,
        client_secret,
    )


def get_access_token() -> str:
    token = {}
    if TOKEN_FILE.exists():
        token = json.loads(TOKEN_FILE.read_text())
    if token.get("access_token") and token.get("expires_at", 0) > time.time() + 60:
        return token["access_token"]
    client_id, client_secret, redirect_uri = oauth_config()
    if token.get("refresh_token"):
        token = request_token(
            {"grant_type": "refresh_token", "refresh_token": token["refresh_token"]},
            client_id,
            client_secret,
        ) | {"refresh_token": token["refresh_token"]}
    else:
        token = authorize(client_id, client_secret, redirect_uri)
    token["expires_at"] = int(time.time()) + int(token.get("expires_in", 3600))
    save_token(token)
    return token["access_token"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Analysiert gesendete Yahoo-Mails.")
    parser.add_argument("--max-results", type=int, default=20)
    parser.add_argument("--folder", default="Sent")
    args = parser.parse_args()

    with Ymail(args.folder) as account:
        message_ids = account.search("ALL")[-args.max_results :]
        print(f"Yahoo-Ordner: {args.folder}")
        print(f"Nachrichten gefunden: {len(message_ids)}")
        for message_id in reversed(message_ids):
            details = parse_message(account.fetch(message_id))
            print(f"\n--- {details['date']} | {details['subject']} ---")
            print(f"Von: {details['from']}")
            print(f"An: {details['to']}")
            print(details["body"][:4000])


if __name__ == "__main__":
    main()