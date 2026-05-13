"""Tests for maildigest.config."""

from datetime import date, datetime
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

from maildigest.config import (
    MailboxConfig,
    _find_config_file,
    _get_secret,
    _parse_schedule_days,
    is_scheduled_today,
    last_run_path,
    load_config,
    read_last_run,
    write_last_run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mailbox(**overrides) -> MailboxConfig:
    defaults = dict(
        name="mb",
        label="MB",
        enabled=True,
        imap_server="imap.test",
        imap_port=993,
        email="u@test.edu",
        imap_folder="Newsletters",
        smtp_server="smtp.test",
        smtp_port=587,
        schedule_days=frozenset({"daily"}),
        language="English",
        focus_areas=[],
        extra_instructions="",
        custom_prompt=None,
        body_char_limit=3000,
        sender_filter=[],
        summary_dir=Path("/tmp/s"),
        imap_password="pw",
        smtp_password="pw",
    )
    defaults.update(overrides)
    return MailboxConfig(**defaults)


_YAML = dedent("""\
    summary_dir: /tmp/summaries

    mailboxes:
      - name: uoc
        label: UOC Newsletters
        imap:
          server: imap.uoc.gr
          port: 993
          email: user@uoc.gr
          folder: Newsletters
        smtp:
          server: smtp.uoc.gr
          port: 587
        schedule:
          days: [mon, tue, wed, thu, fri]
        summarizer:
          language: Greek
          focus_areas:
            - grant deadlines
          extra_instructions: Ignore promotions.
""")


# ---------------------------------------------------------------------------
# _parse_schedule_days
# ---------------------------------------------------------------------------

class TestParseScheduleDays:
    def test_daily_string(self):
        result = _parse_schedule_days("daily")
        assert result == frozenset({"daily"})

    def test_list_of_days(self):
        result = _parse_schedule_days(["mon", "wed", "fri"])
        assert result == frozenset({"mon", "wed", "fri"})

    def test_single_day_string(self):
        result = _parse_schedule_days("mon")
        assert result == frozenset({"mon"})

    def test_invalid_day_raises(self):
        with pytest.raises(ValueError, match="Invalid schedule day"):
            _parse_schedule_days(["mon", "funday"])

    def test_case_normalised(self):
        result = _parse_schedule_days(["MON", "TUE"])
        assert "mon" in result and "tue" in result

    def test_empty_list_raises(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            _parse_schedule_days([])


# ---------------------------------------------------------------------------
# is_scheduled_today
# ---------------------------------------------------------------------------

class TestIsScheduledToday:
    def test_daily_always_true(self):
        mb = _make_mailbox(schedule_days=frozenset({"daily"}))
        assert is_scheduled_today(mb) is True

    def test_matching_weekday_true(self):
        today_abbr = date.today().strftime("%a").lower()
        mb = _make_mailbox(schedule_days=frozenset({today_abbr}))
        assert is_scheduled_today(mb) is True

    def test_non_matching_weekday_false(self):
        all_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        today_abbr = date.today().strftime("%a").lower()
        other_days = frozenset(all_days - {today_abbr})
        mb = _make_mailbox(schedule_days=other_days)
        assert is_scheduled_today(mb) is False


# ---------------------------------------------------------------------------
# read_last_run / write_last_run
# ---------------------------------------------------------------------------

class TestLastRun:
    def test_returns_none_when_file_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("maildigest.config.USER_CONFIG_DIR", tmp_path)
        assert read_last_run("mybox") is None

    def test_write_then_read_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setattr("maildigest.config.USER_CONFIG_DIR", tmp_path)
        dt = datetime(2026, 5, 9, 17, 0, 34)
        write_last_run("mybox", dt)
        assert read_last_run("mybox") == dt

    def test_write_creates_directory(self, tmp_path, monkeypatch):
        nested = tmp_path / "a" / "b"
        monkeypatch.setattr("maildigest.config.USER_CONFIG_DIR", nested)
        write_last_run("mybox", datetime(2026, 5, 9, 9, 0, 0))
        assert (nested / "last_run_mybox").exists()

    def test_corrupt_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("maildigest.config.USER_CONFIG_DIR", tmp_path)
        (tmp_path / "last_run_mybox").write_text("not-a-date")
        assert read_last_run("mybox") is None

    def test_separate_names_independent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("maildigest.config.USER_CONFIG_DIR", tmp_path)
        write_last_run("box_a", datetime(2026, 5, 1, 10, 0, 0))
        write_last_run("box_b", datetime(2026, 5, 9, 17, 0, 0))
        assert read_last_run("box_a") == datetime(2026, 5, 1, 10, 0, 0)
        assert read_last_run("box_b") == datetime(2026, 5, 9, 17, 0, 0)

    def test_last_run_path_uses_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr("maildigest.config.USER_CONFIG_DIR", tmp_path)
        assert last_run_path("mybox") == tmp_path / "last_run_mybox"

    def test_old_date_only_format_parsed_as_midnight(self, tmp_path, monkeypatch):
        monkeypatch.setattr("maildigest.config.USER_CONFIG_DIR", tmp_path)
        (tmp_path / "last_run_mybox").write_text("2026-05-12")
        result = read_last_run("mybox")
        assert result == datetime(2026, 5, 12, 0, 0, 0)

    def test_stored_format_has_seconds_no_microseconds(self, tmp_path, monkeypatch):
        monkeypatch.setattr("maildigest.config.USER_CONFIG_DIR", tmp_path)
        write_last_run("mybox", datetime(2026, 5, 9, 17, 0, 34, 123456))
        text = (tmp_path / "last_run_mybox").read_text()
        assert text == "2026-05-09T17:00:34"


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    @patch("maildigest.config._try_get_secret", return_value=None)
    @patch("maildigest.config._get_secret")
    def test_parses_single_mailbox(self, mock_get, mock_try, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(_YAML)
        monkeypatch.setenv("MAILDIGEST_CONFIG", str(cfg_file))

        mock_get.side_effect = lambda env_key, kr_name: (
            "sk-anthropic" if kr_name == "anthropic_api_key" else "imap_pwd"
        )

        cfg = load_config()

        assert cfg.anthropic_api_key == "sk-anthropic"
        assert len(cfg.mailboxes) == 1
        mb = cfg.mailboxes[0]
        assert mb.name == "uoc"
        assert mb.label == "UOC Newsletters"
        assert mb.imap_server == "imap.uoc.gr"
        assert mb.email == "user@uoc.gr"
        assert mb.smtp_server == "smtp.uoc.gr"
        assert mb.schedule_days == frozenset({"mon", "tue", "wed", "thu", "fri"})
        assert mb.language == "Greek"
        assert mb.focus_areas == ["grant deadlines"]
        assert mb.extra_instructions == "Ignore promotions."
        assert mb.imap_password == "imap_pwd"

    @patch("maildigest.config._try_get_secret", return_value=None)
    @patch("maildigest.config._get_secret")
    def test_summary_dir_defaults_to_global_subdir(
        self, mock_get, mock_try, tmp_path, monkeypatch
    ):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(_YAML)
        monkeypatch.setenv("MAILDIGEST_CONFIG", str(cfg_file))
        mock_get.return_value = "any"

        cfg = load_config()

        assert cfg.mailboxes[0].summary_dir == Path("/tmp/summaries/uoc")

    @patch("maildigest.config._try_get_secret", return_value=None)
    @patch("maildigest.config._get_secret")
    def test_missing_config_file_raises(
        self, mock_get, mock_try, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("MAILDIGEST_CONFIG", str(tmp_path / "nonexistent.yaml"))

        with pytest.raises(FileNotFoundError):
            load_config()

    @patch("maildigest.config._try_get_secret", return_value="smtp_pwd")
    @patch("maildigest.config._get_secret")
    def test_smtp_password_from_keychain_overrides_imap(
        self, mock_get, mock_try, tmp_path, monkeypatch
    ):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(_YAML)
        monkeypatch.setenv("MAILDIGEST_CONFIG", str(cfg_file))
        mock_get.return_value = "imap_pw"

        cfg = load_config()
        assert cfg.mailboxes[0].smtp_password == "smtp_pwd"


# ---------------------------------------------------------------------------
# _find_config_file — lookup order
# ---------------------------------------------------------------------------

class TestFindConfigFile:
    def test_env_var_takes_highest_priority(self, tmp_path, monkeypatch):
        cfg = tmp_path / "custom.yaml"
        cfg.write_text("x: 1")
        monkeypatch.setenv("MAILDIGEST_CONFIG", str(cfg))
        monkeypatch.setattr("maildigest.config._USER_CONFIG_FILE", tmp_path / "user.yaml")
        assert _find_config_file() == cfg

    def test_local_config_takes_priority_over_user_config(self, tmp_path, monkeypatch):
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (local_dir / "config.yaml").write_text("x: 1")

        user_cfg = tmp_path / "user" / "config.yaml"
        user_cfg.parent.mkdir()
        user_cfg.write_text("x: 2")

        monkeypatch.delenv("MAILDIGEST_CONFIG", raising=False)
        monkeypatch.setattr("maildigest.config._USER_CONFIG_FILE", user_cfg)
        monkeypatch.chdir(local_dir)

        assert _find_config_file() == local_dir / "config.yaml"

    def test_falls_back_to_user_config_when_no_local(self, tmp_path, monkeypatch):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        user_cfg = tmp_path / "user.yaml"
        user_cfg.write_text("x: 1")

        monkeypatch.delenv("MAILDIGEST_CONFIG", raising=False)
        monkeypatch.setattr("maildigest.config._USER_CONFIG_FILE", user_cfg)
        monkeypatch.chdir(empty_dir)

        assert _find_config_file() == user_cfg


# ---------------------------------------------------------------------------
# _get_secret — environment variable fallback
# ---------------------------------------------------------------------------

class TestGetSecret:
    @patch("keyring.get_password", return_value=None)
    def test_falls_back_to_env_var(self, mock_kp, monkeypatch):
        monkeypatch.setenv("MY_TEST_SECRET", "env_value")
        result = _get_secret("MY_TEST_SECRET", "some_kr_name")
        assert result == "env_value"

    @patch("keyring.get_password", return_value=None)
    def test_raises_when_neither_keychain_nor_env(self, mock_kp, monkeypatch):
        monkeypatch.delenv("MY_TEST_SECRET", raising=False)
        with pytest.raises(ValueError, match="Missing required secret"):
            _get_secret("MY_TEST_SECRET", "some_kr_name")

    @patch("keyring.get_password", return_value="kc_value")
    def test_prefers_keychain_over_env(self, mock_kp, monkeypatch):
        monkeypatch.setenv("MY_TEST_SECRET", "env_value")
        result = _get_secret("MY_TEST_SECRET", "some_kr_name")
        assert result == "kc_value"
