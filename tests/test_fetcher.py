"""Tests for maildigest.fetcher."""

import email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch

import pytest

from maildigest.fetcher import (
    _decode_header_value,
    _extract_plain_text,
    _matches_filter,
    fetch_todays_emails,
)


def _plain_bytes(subject: str, sender: str, body: str) -> bytes:
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = sender
    return msg.as_bytes()


def _multipart_bytes(subject: str, sender: str, body: str) -> bytes:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg.attach(MIMEText(body, "plain"))
    msg.attach(MIMEText(f"<p>{body}</p>", "html"))
    return msg.as_bytes()


def _encode_utf8_b64(text: str) -> str:
    """Produce a valid =?UTF-8?B?...?= encoded-word for testing."""
    import base64
    b64 = base64.b64encode(text.encode("utf-8")).decode()
    return f"=?UTF-8?B?{b64}?="


class TestDecodeHeaderValue:
    def test_plain_ascii_unchanged(self):
        assert _decode_header_value("Hello") == "Hello"

    def test_utf8_base64_encoded(self):
        encoded = _encode_utf8_b64("Καλημέρα")
        assert _decode_header_value(encoded) == "Καλημέρα"

    def test_latin1_quoted_printable(self):
        encoded = "=?iso-8859-1?q?R=E9?="
        assert _decode_header_value(encoded) == "Ré"

    def test_mixed_encoded_and_plain(self):
        encoded = _encode_utf8_b64("Καλημέρα") + " world"
        result = _decode_header_value(encoded)
        assert "Καλημέρα" in result


class TestExtractPlainText:
    def test_plain_message_returns_body(self):
        msg = email.message_from_bytes(_plain_bytes("s", "f", "hello"))
        assert _extract_plain_text(msg) == "hello"

    def test_multipart_returns_plain_part(self):
        msg = email.message_from_bytes(_multipart_bytes("s", "f", "hi there"))
        assert _extract_plain_text(msg) == "hi there"

    def test_empty_payload_returns_empty_string(self):
        msg = email.message_from_string("Content-Type: text/plain\n\n")
        assert _extract_plain_text(msg) == ""

    def test_latin1_body_decoded_correctly(self):
        raw_bytes = "Héllo".encode("latin-1")
        msg = email.message_from_bytes(
            b"Content-Type: text/plain; charset=iso-8859-1\n"
            b"Content-Transfer-Encoding: base64\n\n"
            + __import__("base64").b64encode(raw_bytes)
        )
        assert _extract_plain_text(msg) == "Héllo"

    def test_multipart_no_plain_part_returns_empty_string(self):
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText("<b>only html</b>", "html"))
        parsed = email.message_from_bytes(msg.as_bytes())
        result = _extract_plain_text(parsed)
        assert result == ""


class TestMatchesFilter:
    def test_exact_email_matches(self):
        assert _matches_filter("admin@uoc.gr", ["admin@uoc.gr"])

    def test_domain_substring_matches(self):
        assert _matches_filter("newsletter@uoc.gr", ["@uoc.gr"])

    def test_case_insensitive(self):
        assert _matches_filter("News@UOC.GR", ["@uoc.gr"])

    def test_no_match_returns_false(self):
        assert not _matches_filter("other@gmail.com", ["@uoc.gr"])

    def test_any_filter_entry_suffices(self):
        assert _matches_filter("news@uoc.gr", ["@gmail.com", "@uoc.gr"])


class TestFetchTodaysEmails:
    @patch("maildigest.fetcher.imaplib.IMAP4_SSL")
    def test_returns_parsed_emails(self, mock_ssl_class):
        raw = _plain_bytes("Subj", "from@uni.edu", "body text")
        conn = MagicMock()
        mock_ssl_class.return_value = conn
        conn.select.return_value = ("OK", [None])
        conn.search.return_value = (None, [b"1"])
        conn.fetch.return_value = (None, [(None, raw)])

        result = fetch_todays_emails(
            "imap.test", 993, "u@uni.edu", "pwd", "Inbox"
        )

        assert len(result) == 1
        assert result[0]["subject"] == "Subj"
        assert result[0]["from"] == "from@uni.edu"
        assert result[0]["body"] == "body text"

    @patch("maildigest.fetcher.imaplib.IMAP4_SSL")
    def test_invalid_folder_raises_value_error(self, mock_ssl_class):
        conn = MagicMock()
        mock_ssl_class.return_value = conn
        conn.select.return_value = ("NO", [None])

        with pytest.raises(ValueError, match="Could not open folder"):
            fetch_todays_emails(
                "imap.test", 993, "u@uni.edu", "pwd", "BadFolder"
            )

    @patch("maildigest.fetcher.imaplib.IMAP4_SSL")
    def test_no_emails_returns_empty_list(self, mock_ssl_class):
        conn = MagicMock()
        mock_ssl_class.return_value = conn
        conn.select.return_value = ("OK", [None])
        conn.search.return_value = (None, [b""])

        result = fetch_todays_emails(
            "imap.test", 993, "u@uni.edu", "pwd", "Inbox"
        )
        assert result == []

    @patch("maildigest.fetcher.imaplib.IMAP4_SSL")
    def test_body_truncated_to_limit(self, mock_ssl_class):
        raw = _plain_bytes("S", "f@uni.edu", "A" * 5000)
        conn = MagicMock()
        mock_ssl_class.return_value = conn
        conn.select.return_value = ("OK", [None])
        conn.search.return_value = (None, [b"1"])
        conn.fetch.return_value = (None, [(None, raw)])

        result = fetch_todays_emails(
            "imap.test", 993, "u@uni.edu", "pwd", "Inbox",
            body_char_limit=100,
        )
        assert len(result[0]["body"]) == 100

    @patch("maildigest.fetcher.imaplib.IMAP4_SSL")
    def test_logout_always_called(self, mock_ssl_class):
        conn = MagicMock()
        mock_ssl_class.return_value = conn
        conn.select.return_value = ("OK", [None])
        conn.search.return_value = (None, [b""])

        fetch_todays_emails("imap.test", 993, "u@uni.edu", "pwd", "Inbox")
        conn.logout.assert_called_once()

    @patch("maildigest.fetcher.imaplib.IMAP4_SSL")
    def test_logout_called_even_on_exception(self, mock_ssl_class):
        conn = MagicMock()
        mock_ssl_class.return_value = conn
        conn.select.return_value = ("NO", [None])

        with pytest.raises(ValueError):
            fetch_todays_emails("imap.test", 993, "u@uni.edu", "pwd", "BadFolder")

        conn.logout.assert_called_once()

    @patch("maildigest.fetcher.imaplib.IMAP4_SSL")
    def test_target_date_used_in_imap_search(self, mock_ssl_class):
        from datetime import date as dt
        conn = MagicMock()
        mock_ssl_class.return_value = conn
        conn.select.return_value = ("OK", [None])
        conn.search.return_value = (None, [b""])

        target = dt(2026, 5, 6)
        fetch_todays_emails(
            "imap.test", 993, "u@uni.edu", "pwd", "Inbox",
            target_date=target,
        )

        search_arg = conn.search.call_args[0][1]
        assert "06-May-2026" in search_arg

    @patch("maildigest.fetcher.imaplib.IMAP4_SSL")
    def test_sender_filter_excludes_non_matching(self, mock_ssl_class):
        raw_matching = _plain_bytes("Uni News", "news@uoc.gr", "body")
        raw_other = _plain_bytes("Spam", "spam@other.com", "junk")
        conn = MagicMock()
        mock_ssl_class.return_value = conn
        conn.select.return_value = ("OK", [None])
        conn.search.return_value = (None, [b"1 2"])
        conn.fetch.side_effect = [
            (None, [(None, raw_matching)]),
            (None, [(None, raw_other)]),
        ]

        result = fetch_todays_emails(
            "imap.test", 993, "u@uni.edu", "pwd", "Inbox",
            sender_filter=["@uoc.gr"],
        )

        assert len(result) == 1
        assert result[0]["subject"] == "Uni News"

    @patch("maildigest.fetcher.imaplib.IMAP4_SSL")
    def test_no_sender_filter_returns_all(self, mock_ssl_class):
        raw1 = _plain_bytes("A", "a@uoc.gr", "body1")
        raw2 = _plain_bytes("B", "b@other.com", "body2")
        conn = MagicMock()
        mock_ssl_class.return_value = conn
        conn.select.return_value = ("OK", [None])
        conn.search.return_value = (None, [b"1 2"])
        conn.fetch.side_effect = [
            (None, [(None, raw1)]),
            (None, [(None, raw2)]),
        ]

        result = fetch_todays_emails(
            "imap.test", 993, "u@uni.edu", "pwd", "Inbox",
        )
        assert len(result) == 2
