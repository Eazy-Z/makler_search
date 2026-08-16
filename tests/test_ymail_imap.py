import imaplib

from ymail_imap import ImapMailbox, parse_message


def test_parse_message_decodes_headers_and_body():
    raw = b"\r\n".join([
        b"From: noreply@immobilienscout24.de",
        b"To: melinamarrek@ymail.com",
        b"Subject: Ihre Kontaktaufnahme zum Anbieter",
        b"Date: Mon, 10 Aug 2026 12:00:00 +0200",
        b"Content-Type: text/plain; charset=utf-8",
        b"",
        "Firma FM Immobilien - Felix Mühlbauer Anrede Herr".encode(),
    ])

    message = parse_message(raw)

    assert message["subject"] == "Ihre Kontaktaufnahme zum Anbieter"
    assert message["from"] == "noreply@immobilienscout24.de"
    assert "FM Immobilien" in message["body"]


def test_decode_header_tolerates_invalid_encoded_value():
    raw = b"Subject: =?utf-8?Q?broken=FF?=\r\n\r\nBody"

    message = parse_message(raw)

    assert message["subject"]


def test_folders_extract_names_from_imap_list_lines(monkeypatch):
    class FakeConnection:
        def list(self):
            return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" Sent']

    mailbox = object.__new__(ImapMailbox)
    mailbox.connection = FakeConnection()

    assert mailbox.folders() == ["INBOX", "Sent"]


def test_search_quotes_multiword_and_unicode_terms():
    class FakeConnection:
        def uid(self, command, charset, *criteria):
            assert command == "search"
            assert charset == "UTF-8"
            assert criteria == ("SUBJECT", '"Vielen Dank für deine Anfrage"')
            return "OK", [b"123"]

    mailbox = object.__new__(ImapMailbox)
    mailbox.connection = FakeConnection()

    assert mailbox.search(["SUBJECT", "Vielen Dank für deine Anfrage"]) == ["123"]


def test_mailbox_uses_utf8_for_imap_commands(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self._encoding = "ascii"

        def login(self, address, password):
            assert (address, password) == ("user@example.com", "app-password")

        def select(self, folder, readonly):
            assert (folder, readonly) == ("INBOX", True)
            return "OK", []

    monkeypatch.setenv("YMAIL_ADDRESS", "user@example.com")
    monkeypatch.setenv("YMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setattr("ymail_imap.imaplib.IMAP4_SSL", lambda host, port: FakeConnection())

    mailbox = ImapMailbox()

    assert mailbox.connection._encoding == "utf-8"


def test_search_falls_back_to_ascii_prefix_for_yahoo_unicode_limitations():
    class FakeConnection:
        def __init__(self):
            self.calls = []

        def uid(self, command, charset, *criteria):
            self.calls.append((command, charset, criteria))
            if charset == "UTF-8":
                raise imaplib.IMAP4.error("BAD")
            return "OK", [b"456"]

    mailbox = object.__new__(ImapMailbox)
    mailbox.connection = FakeConnection()

    assert mailbox.search(["SUBJECT", "Vielen Dank für deine Anfrage"]) == ["456"]
    assert mailbox.connection.calls[-1] == ("search", None, ("SUBJECT", '"Vielen Dank"'))


def test_fetch_headers_uses_header_only_fetch():
    class FakeConnection:
        def uid(self, command, message_id, query):
            assert (command, message_id) == ("fetch", "789")
            assert query == "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT)])"
            return "OK", [(b"header", b"Subject: Vielen Dank\r\n\r\n")]

    mailbox = object.__new__(ImapMailbox)
    mailbox.connection = FakeConnection()

    assert mailbox.fetch_headers("789")["subject"] == "Vielen Dank"