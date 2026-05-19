"""Long-lived APScheduler daemon for maildigest."""

import logging
import signal

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from maildigest.cli import _run_mailbox_digest
from maildigest.config import AppConfig, _init_keyring, load_config

log = logging.getLogger(__name__)

_DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def run_daemon(cfg_path: str | None = None) -> None:
    """Prompt for keyring master password, then block on the APScheduler loop."""
    try:
        _init_keyring()
    except RuntimeError as exc:
        log.exception("%s", exc)
        raise SystemExit(1) from exc
    cfg = load_config(cfg_path) if cfg_path else load_config()
    scheduler = BlockingScheduler()
    _schedule_all(scheduler, cfg)

    def _reload(*_: object) -> None:
        new_cfg = load_config(cfg_path) if cfg_path else load_config()
        _schedule_all(scheduler, new_cfg)
        log.info("Config reloaded via SIGHUP.")

    signal.signal(signal.SIGHUP, _reload)
    log.info(
        "Service started. "
        "Use 'maildigest service reload' to apply config changes, "
        "'maildigest service stop' to stop."
    )
    scheduler.start()


def _schedule_all(scheduler: BlockingScheduler, cfg: AppConfig) -> None:
    scheduler.remove_all_jobs()
    for mb in cfg.mailboxes:
        if not mb.enabled:
            continue
        days_cron = (
            "*"
            if "daily" in mb.schedule_days
            else ",".join(sorted(mb.schedule_days, key=_DAY_ORDER.index))
        )
        for hour, minute in mb.schedule_times:
            scheduler.add_job(
                _run_mailbox_digest,
                CronTrigger(day_of_week=days_cron, hour=hour, minute=minute),
                args=[mb, cfg.anthropic_api_key],
                kwargs={"print_summary": False},
                id=f"{mb.name}_{hour:02d}{minute:02d}",
                replace_existing=True,
            )
            log.info(
                "Scheduled [%s] at %02d:%02d on %s",
                mb.label,
                hour,
                minute,
                days_cron,
            )
