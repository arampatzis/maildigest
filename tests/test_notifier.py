"""Tests for maildigest.notifier."""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from maildigest.notifier import (
    save_to_markdown,
    send_email_summary,
)

_SUMMARY = "Key announcements:\n- Event on Friday\n- Deadline Monday"


_LABEL = "UOC Newsletters"


class TestSaveToMarkdown:
    def test_creates_file(self, tmp_path):
        path = save_to_markdown(_SUMMARY, tmp_path, _LABEL)
        assert path.exists()

    def test_filename_contains_todays_date(self, tmp_path):
        path = save_to_markdown(_SUMMARY, tmp_path, _LABEL)
        assert date.today().isoformat() in path.name

    def test_file_contains_summary_text(self, tmp_path):
        path = save_to_markdown(_SUMMARY, tmp_path, _LABEL)
        assert _SUMMARY in path.read_text()

    def test_creates_missing_directory(self, tmp_path):
        nested = tmp_path / "a" / "b"
        path = save_to_markdown(_SUMMARY, nested, _LABEL)
        assert path.exists()

    def test_file_header_contains_label(self, tmp_path):
        path = save_to_markdown(_SUMMARY, tmp_path, _LABEL)
        assert path.read_text().startswith(f"# {_LABEL}")

    def test_target_date_used_in_filename(self, tmp_path):
        target = date.today() - timedelta(days=2)
        path = save_to_markdown(_SUMMARY, tmp_path, _LABEL, target_date=target)
        assert target.isoformat() in path.name

    def test_target_date_used_in_header(self, tmp_path):
        target = date(2026, 5, 6)
        path = save_to_markdown(_SUMMARY, tmp_path, _LABEL, target_date=target)
        assert "May 06, 2026" in path.read_text()


class TestSendEmailSummary:
    @patch("maildigest.notifier.smtplib.SMTP")
    def test_authenticates_with_starttls(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__.return_value = mock_server

        send_email_summary(_SUMMARY, "smtp.test", 587, "me@test.com", "secret", _LABEL)

        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("me@test.com", "secret")

    @patch("maildigest.notifier.smtplib.SMTP")
    def test_sends_to_self(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__.return_value = mock_server

        send_email_summary(_SUMMARY, "smtp.test", 587, "me@test.com", "secret", _LABEL)

        args = mock_server.sendmail.call_args[0]
        assert args[0] == "me@test.com"
        assert args[1] == "me@test.com"

    @patch("maildigest.notifier.smtplib.SMTP")
    def test_connects_to_correct_server_and_port(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__.return_value = mock_server

        send_email_summary(_SUMMARY, "smtp.myuni.edu", 465, "u@uni.edu", "pwd", _LABEL)

        mock_smtp_class.assert_called_once_with("smtp.myuni.edu", 465)


class TestSendEmailSummaryLabel:
    @patch("maildigest.notifier.smtplib.SMTP")
    def test_label_in_subject(self, mock_smtp_class):
        import email as email_lib
        from email.header import decode_header, make_header

        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__.return_value = mock_server

        send_email_summary(
            _SUMMARY, "smtp.test", 587, "me@test.com", "secret", "UOC Faculty"
        )

        raw = mock_server.sendmail.call_args[0][2]
        parsed = email_lib.message_from_string(raw)
        subject = str(make_header(decode_header(parsed["Subject"])))
        assert "UOC Faculty" in subject


class TestSendEmailSummaryTargetDate:
    @patch("maildigest.notifier.smtplib.SMTP")
    def test_target_date_in_subject(self, mock_smtp_class):
        import email as email_lib
        from email.header import decode_header, make_header

        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__.return_value = mock_server
        from datetime import datetime

        target = datetime(2026, 5, 6, 9, 0)

        send_email_summary(
            _SUMMARY,
            "smtp.test",
            587,
            "me@test.com",
            "secret",
            _LABEL,
            target_dt=target,
        )

        raw = mock_server.sendmail.call_args[0][2]
        parsed = email_lib.message_from_string(raw)
        subject = str(make_header(decode_header(parsed["Subject"])))
        assert "May 06, 2026" in subject
        assert "09:00" in subject


class TestSendEmailSummaryCharset:
    @patch("maildigest.notifier.smtplib.SMTP")
    def test_non_ascii_summary_does_not_raise(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__.return_value = mock_server
        greek_summary = "Ανακοινώσεις:\n- Εκδήλωση Παρασκευή"
        send_email_summary(
            greek_summary, "smtp.test", 587, "me@test.com", "secret", _LABEL
        )
        mock_server.sendmail.assert_called_once()
