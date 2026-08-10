import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gmail_test import parse_message


def test_parse_message_decodes_subject_and_plaintext_body():
    raw = b"\r\n".join([
        b"From: Makler Search <app@example.com>",
        b"To: buyer@example.com",
        b"Subject: =?utf-8?b?U3VjaGU6IE11ZW5jaGVu?=",
        b"Date: Mon, 10 Aug 2026 12:00:00 +0200",
        b"Content-Type: text/plain; charset=utf-8",
        b"",
        "Neue Immobilie in M\u00fcnchen".encode(),
    ])

    parsed = parse_message(raw)

    assert parsed["subject"] == "Suche: Muenchen"
    assert parsed["from"] == "Makler Search <app@example.com>"
    assert parsed["body"] == "Neue Immobilie in M\u00fcnchen"


def test_parse_message_prefers_plaintext_over_html():
    raw = b"\r\n".join([
        b"Subject: Test",
        b"Content-Type: multipart/alternative; boundary=mail",
        b"",
        b"--mail",
        b"Content-Type: text/html; charset=utf-8",
        b"",
        b"<p>HTML</p>",
        b"--mail",
        b"Content-Type: text/plain; charset=utf-8",
        b"",
        b"Plaintext",
        b"--mail--",
    ])

    assert parse_message(raw)["body"] == "Plaintext"