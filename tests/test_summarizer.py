"""Tests for maildigest.summarizer."""

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from maildigest.config import MailboxConfig
from maildigest.summarizer import _build_prompt, summarize_with_claude


def _make_mailbox(**overrides) -> MailboxConfig:
    defaults = {
        "name": "test_mb",
        "label": "Test Mailbox",
        "enabled": True,
        "imap_server": "imap.test",
        "imap_port": 993,
        "email": "u@test.edu",
        "imap_folder": "Newsletters",
        "smtp_server": "smtp.test",
        "smtp_port": 587,
        "schedule_days": frozenset({"daily"}),
        "schedule_times": [(9, 0)],
        "language": "English",
        "focus_areas": [],
        "extra_instructions": "",
        "custom_prompt": None,
        "body_char_limit": 3000,
        "sender_filter": [],
        "summary_dir": Path("/tmp/summaries"),
        "imap_password": "imap_pwd",
        "smtp_password": "smtp_pwd",
    }
    defaults.update(overrides)
    return MailboxConfig(**defaults)


_EMAILS = [
    {"subject": "Campus Event", "from": "a@uni.edu", "body": "Join us Tue."},
    {"subject": "Deadline", "from": "b@uni.edu", "body": "Forms due Friday."},
]

_MB = _make_mailbox()


class TestBuildPrompt:
    def test_contains_each_subject(self):
        prompt = _build_prompt(_EMAILS, _MB)
        assert "Campus Event" in prompt
        assert "Deadline" in prompt

    def test_contains_each_sender(self):
        prompt = _build_prompt(_EMAILS, _MB)
        assert "a@uni.edu" in prompt
        assert "b@uni.edu" in prompt

    def test_email_count_appears_in_prompt(self):
        prompt = _build_prompt(_EMAILS, _MB)
        assert "2 newsletters" in prompt

    def test_single_email_count(self):
        prompt = _build_prompt(_EMAILS[:1], _MB)
        assert "1 newsletter " in prompt
        assert "1 newsletters" not in prompt

    def test_body_text_included(self):
        prompt = _build_prompt(_EMAILS, _MB)
        assert "Join us Tue." in prompt
        assert "Forms due Friday." in prompt

    def test_default_language_is_english(self):
        prompt = _build_prompt(_EMAILS, _MB)
        assert "English" in prompt

    def test_custom_language_appears_in_prompt(self):
        mb = _make_mailbox(language="Greek")
        prompt = _build_prompt(_EMAILS, mb)
        assert "Greek" in prompt

    def test_focus_areas_included_in_prompt(self):
        mb = _make_mailbox(focus_areas=["grant deadlines", "seminars"])
        prompt = _build_prompt(_EMAILS, mb)
        assert "grant deadlines" in prompt
        assert "seminars" in prompt

    def test_extra_instructions_included(self):
        mb = _make_mailbox(extra_instructions="Ignore promotions.")
        prompt = _build_prompt(_EMAILS, mb)
        assert "Ignore promotions." in prompt

    def test_custom_prompt_overrides_default_structure(self):
        mb = _make_mailbox(custom_prompt="Just summarise briefly.")
        prompt = _build_prompt(_EMAILS, mb)
        assert "Just summarise briefly." in prompt
        assert "## Grants & Funding" not in prompt

    def test_custom_prompt_includes_email_bodies(self):
        mb = _make_mailbox(custom_prompt="Summarize:")
        prompt = _build_prompt(_EMAILS, mb)
        assert "Join us Tue." in prompt

    def test_date_range_appears_when_from_differs(self):
        from_d = date(2026, 6, 12)
        to_d = date(2026, 6, 26)
        prompt = _build_prompt(_EMAILS, _MB, from_date=from_d, target_date=to_d)
        assert "June 12, 2026" in prompt
        assert "June 26, 2026" in prompt

    def test_single_date_when_from_equals_to(self):
        d = date(2026, 6, 26)
        prompt = _build_prompt(_EMAILS, _MB, from_date=d, target_date=d)
        assert "June 26, 2026" in prompt
        assert "between" not in prompt

    def test_custom_prompt_includes_label_and_date_range(self):
        mb = _make_mailbox(custom_prompt="Summarize:", label="SPOK")
        from_d = date(2026, 6, 12)
        to_d = date(2026, 6, 26)
        prompt = _build_prompt(_EMAILS, mb, from_date=from_d, target_date=to_d)
        assert "SPOK" in prompt
        assert "June 12, 2026" in prompt
        assert "June 26, 2026" in prompt


class TestSummarizeWithClaude:
    def test_empty_list_returns_no_newsletters_message(self):
        result = summarize_with_claude([], mailbox=_MB, api_key="test-key")
        today = date.today().strftime("%B %d, %Y")
        assert result == f"No newsletters received on {today}."

    def test_empty_list_uses_target_date(self):
        target = date(2026, 5, 6)
        result = summarize_with_claude(
            [], mailbox=_MB, api_key="test-key", target_date=target
        )
        assert "May 06, 2026" in result

    @patch("maildigest.summarizer.anthropic.Anthropic")
    def test_returns_claude_response_text(self, mock_class):
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        mock_client.messages.create.return_value.content = [
            MagicMock(text="Great summary.")
        ]

        result = summarize_with_claude(_EMAILS, mailbox=_MB, api_key="sk-test")

        assert result == "Great summary."

    @patch("maildigest.summarizer.anthropic.Anthropic")
    def test_forwards_model_and_max_tokens(self, mock_class):
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        mock_client.messages.create.return_value.content = [MagicMock(text="ok")]

        summarize_with_claude(
            _EMAILS, mailbox=_MB, api_key="sk", model="my-model", max_tokens=42
        )

        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs["model"] == "my-model"
        assert kwargs["max_tokens"] == 42

    @patch("maildigest.summarizer.anthropic.Anthropic")
    def test_api_key_passed_to_client(self, mock_class):
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        mock_client.messages.create.return_value.content = [MagicMock(text="ok")]

        summarize_with_claude(_EMAILS, mailbox=_MB, api_key="my-secret-key")

        mock_class.assert_called_once_with(api_key="my-secret-key")
