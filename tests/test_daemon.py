"""Tests for maildigest.daemon."""

import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maildigest.config import AppConfig, MailboxConfig
from maildigest.daemon import _schedule_all


def _make_mailbox(**overrides) -> MailboxConfig:
    defaults = {
        "name": "mb",
        "label": "MB",
        "enabled": True,
        "imap_server": "imap.test",
        "imap_port": 993,
        "email": "u@test.edu",
        "imap_folder": "Newsletters",
        "smtp_server": "smtp.test",
        "smtp_port": 587,
        "schedule_days": frozenset({"mon", "tue", "wed", "thu", "fri"}),
        "schedule_times": [(9, 0)],
        "language": "English",
        "focus_areas": [],
        "extra_instructions": "",
        "custom_prompt": None,
        "body_char_limit": 3000,
        "sender_filter": [],
        "summary_dir": Path("/tmp/s"),
        "imap_password": "pw",
        "smtp_password": "pw",
    }
    defaults.update(overrides)
    return MailboxConfig(**defaults)


class TestScheduleAll:
    def test_adds_job_for_enabled_mailbox(self):
        mb = _make_mailbox(schedule_times=[(9, 0)])
        cfg = AppConfig(anthropic_api_key="key", mailboxes=[mb])
        scheduler = MagicMock()

        _schedule_all(scheduler, cfg)

        scheduler.add_job.assert_called_once()
        call_kwargs = scheduler.add_job.call_args
        assert call_kwargs.kwargs["id"] == "mb_0900"

    def test_skips_disabled_mailbox(self):
        mb = _make_mailbox(enabled=False)
        cfg = AppConfig(anthropic_api_key="key", mailboxes=[mb])
        scheduler = MagicMock()

        _schedule_all(scheduler, cfg)

        scheduler.add_job.assert_not_called()

    def test_adds_one_job_per_time(self):
        mb = _make_mailbox(schedule_times=[(9, 0), (17, 0)])
        cfg = AppConfig(anthropic_api_key="key", mailboxes=[mb])
        scheduler = MagicMock()

        _schedule_all(scheduler, cfg)

        assert scheduler.add_job.call_count == 2

    def test_daily_schedule_uses_wildcard(self):
        mb = _make_mailbox(schedule_days=frozenset({"daily"}), schedule_times=[(9, 0)])
        cfg = AppConfig(anthropic_api_key="key", mailboxes=[mb])
        scheduler = MagicMock()

        _schedule_all(scheduler, cfg)

        trigger_arg = scheduler.add_job.call_args.args[1]
        day_of_week_field = next(
            f for f in trigger_arg.fields if f.name == "day_of_week"
        )
        assert str(day_of_week_field) == "*"

    def test_removes_existing_jobs_before_rescheduling(self):
        cfg = AppConfig(anthropic_api_key="key", mailboxes=[])
        scheduler = MagicMock()

        _schedule_all(scheduler, cfg)

        scheduler.remove_all_jobs.assert_called_once()

    def test_mixed_enabled_disabled(self):
        mb_on = _make_mailbox(name="on", schedule_times=[(9, 0)])
        mb_off = _make_mailbox(name="off", enabled=False, schedule_times=[(9, 0)])
        cfg = AppConfig(anthropic_api_key="key", mailboxes=[mb_on, mb_off])
        scheduler = MagicMock()

        _schedule_all(scheduler, cfg)

        assert scheduler.add_job.call_count == 1

    def test_weekday_schedule_days_sorted_in_week_order(self):
        mb = _make_mailbox(
            schedule_days=frozenset({"fri", "mon", "wed"}),
            schedule_times=[(9, 0)],
        )
        cfg = AppConfig(anthropic_api_key="key", mailboxes=[mb])
        scheduler = MagicMock()

        _schedule_all(scheduler, cfg)

        trigger_arg = scheduler.add_job.call_args.args[1]
        day_of_week_field = next(
            f for f in trigger_arg.fields if f.name == "day_of_week"
        )
        assert str(day_of_week_field) == "mon,wed,fri"

    def test_print_summary_false_passed_to_job(self):
        mb = _make_mailbox(schedule_times=[(9, 0)])
        cfg = AppConfig(anthropic_api_key="key", mailboxes=[mb])
        scheduler = MagicMock()

        _schedule_all(scheduler, cfg)

        call_kwargs = scheduler.add_job.call_args.kwargs
        assert call_kwargs["kwargs"] == {"print_summary": False}


class TestRunDaemon:
    def test_exits_on_init_keyring_failure(self):
        from maildigest.daemon import run_daemon

        with patch(
            "maildigest.daemon._init_keyring", side_effect=RuntimeError("bad pw")
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_daemon()
            assert exc_info.value.code == 1

    def test_starts_scheduler_with_loaded_config(self):
        from maildigest.daemon import run_daemon

        mock_cfg = MagicMock()
        mock_cfg.mailboxes = []
        with (
            patch("maildigest.daemon._init_keyring"),
            patch("maildigest.daemon.load_config", return_value=mock_cfg),
            patch("maildigest.daemon.BlockingScheduler") as mock_sched_cls,
        ):
            mock_scheduler = MagicMock()
            mock_sched_cls.return_value = mock_scheduler
            mock_scheduler.start.side_effect = KeyboardInterrupt
            with contextlib.suppress(KeyboardInterrupt):
                run_daemon()
            mock_scheduler.start.assert_called_once()

    def test_uses_cfg_path_when_provided(self):
        from maildigest.daemon import run_daemon

        mock_cfg = MagicMock()
        mock_cfg.mailboxes = []
        with (
            patch("maildigest.daemon._init_keyring"),
            patch("maildigest.daemon.load_config", return_value=mock_cfg) as mock_load,
            patch("maildigest.daemon.BlockingScheduler") as mock_sched_cls,
        ):
            mock_scheduler = MagicMock()
            mock_sched_cls.return_value = mock_scheduler
            mock_scheduler.start.side_effect = KeyboardInterrupt
            with contextlib.suppress(KeyboardInterrupt):
                run_daemon("/custom/config.yaml")
            mock_load.assert_called_once_with("/custom/config.yaml")
