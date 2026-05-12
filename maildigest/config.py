"""Configuration loading for maildigest."""

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

KEYRING_SERVICE = "maildigest"

_XDG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
USER_CONFIG_DIR = _XDG_HOME / "maildigest"
_USER_CONFIG_FILE = USER_CONFIG_DIR / "config.yaml"

VALID_DAYS = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun", "daily"})


def _imap_key(email: str) -> str:
    """Keychain key for an IMAP account password, shared across all mailboxes on that account."""
    return f"imap:{email}"


def _smtp_key(email: str) -> str:
    """Keychain key for an SMTP account password."""
    return f"smtp:{email}"


def _email_env_key(email: str, prefix: str) -> str:
    """Environment variable name derived from an email address, e.g. IMAP_USER_UOC_GR."""
    safe = email.upper().replace("@", "_").replace(".", "_").replace("-", "_")
    return f"{prefix}_{safe}"


@dataclass
class MailboxConfig:
    name: str               # machine name — keychain key prefix, last_run filename
    label: str              # human-readable display name
    enabled: bool
    imap_server: str
    imap_port: int
    email: str              # IMAP login + SMTP From/To (send-to-self)
    imap_folder: str
    smtp_server: str
    smtp_port: int
    schedule_days: frozenset  # {"daily"} or {"mon", "tue", …}
    language: str
    focus_areas: list
    extra_instructions: str
    custom_prompt: str | None
    body_char_limit: int
    sender_filter: list       # only fetch from these senders (empty = no filter)
    summary_dir: Path
    imap_password: str = field(repr=False)
    smtp_password: str = field(repr=False)


@dataclass
class AppConfig:
    anthropic_api_key: str = field(repr=False)
    mailboxes: list = field(default_factory=list)


def _find_config_file() -> Path:
    if custom := os.environ.get("MAILDIGEST_CONFIG"):
        path = Path(custom).expanduser()
        log.debug("Config: using MAILDIGEST_CONFIG override → %s", path)
        return path

    local = Path.cwd() / "config.yaml"
    if local.exists():
        log.debug("Config: using local config → %s", local)
        return local

    if _USER_CONFIG_FILE.exists():
        log.debug("Config: using user config → %s", _USER_CONFIG_FILE)
        return _USER_CONFIG_FILE

    log.debug("Config: no file found; will look at %s", _USER_CONFIG_FILE)
    return _USER_CONFIG_FILE


def _parse_schedule_days(raw) -> frozenset:
    if isinstance(raw, str):
        val = raw.strip().lower()
        days = {"daily"} if val == "daily" else {val}
    else:
        days = {str(d).strip().lower() for d in raw}

    if not days:
        raise ValueError("Schedule days cannot be empty.")

    invalid = days - VALID_DAYS
    if invalid:
        raise ValueError(
            f"Invalid schedule day(s): {sorted(invalid)}. "
            f"Valid values: {sorted(VALID_DAYS)}"
        )
    return frozenset(days)


def load_config() -> AppConfig:
    """
    Load configuration from the YAML file and system keychain.

    Returns
    -------
    AppConfig
        Fully resolved configuration including per-mailbox secrets.

    Raises
    ------
    FileNotFoundError
        When the config YAML is absent.
    ValueError
        When a required secret is missing from both keychain and environment.
    """
    config_path = _find_config_file()
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found at {config_path}. "
            "Copy config.yaml.example there and fill in your settings."
        )

    with config_path.open() as f:
        raw = yaml.safe_load(f)

    anthropic_key = _get_secret("ANTHROPIC_API_KEY", "anthropic_api_key")

    global_summary_dir = Path(
        raw.get("summary_dir", "~/Documents/NewsletterSummaries")
    ).expanduser()

    mailboxes: list[MailboxConfig] = []
    for mb in raw.get("mailboxes", []):
        name = mb["name"]
        imap_cfg = mb["imap"]
        smtp_cfg = mb["smtp"]
        sched = mb.get("schedule", {})
        summ = mb.get("summarizer", {})

        email = imap_cfg["email"]
        imap_password = _get_secret(_email_env_key(email, "IMAP"), _imap_key(email))
        smtp_password = _try_get_secret(_smtp_key(email)) or imap_password

        per_mailbox_summary_dir = mb.get("summary_dir")
        summary_dir = (
            Path(per_mailbox_summary_dir).expanduser()
            if per_mailbox_summary_dir
            else global_summary_dir / name
        )

        mailboxes.append(MailboxConfig(
            name=name,
            label=mb.get("label", name),
            enabled=bool(mb.get("enabled", True)),
            imap_server=imap_cfg["server"],
            imap_port=int(imap_cfg.get("port", 993)),
            email=email,
            imap_folder=imap_cfg.get("folder", "Newsletters"),
            smtp_server=smtp_cfg["server"],
            smtp_port=int(smtp_cfg.get("port", 587)),
            schedule_days=_parse_schedule_days(sched.get("days", "daily")),
            language=summ.get("language", "English"),
            focus_areas=list(summ.get("focus_areas") or []),
            extra_instructions=summ.get("extra_instructions", ""),
            custom_prompt=summ.get("custom_prompt"),
            body_char_limit=int(mb.get("body_char_limit", 3000)),
            sender_filter=list(mb.get("sender_filter") or []),
            summary_dir=summary_dir,
            imap_password=imap_password,
            smtp_password=smtp_password,
        ))

    return AppConfig(anthropic_api_key=anthropic_key, mailboxes=mailboxes)


def _get_secret(env_key: str, keyring_name: str) -> str:
    import keyring
    value = keyring.get_password(KEYRING_SERVICE, keyring_name)
    if value:
        log.debug("Secret '%s': loaded from keychain.", keyring_name)
        return value
    value = os.environ.get(env_key)
    if not value:
        raise ValueError(
            f"Missing required secret '{env_key}'. "
            "Run `maildigest setup-credentials` or set the variable in your environment."
        )
    log.debug("Secret '%s': loaded from environment.", keyring_name)
    return value


def _try_get_secret(keyring_name: str) -> str | None:
    import keyring
    return keyring.get_password(KEYRING_SERVICE, keyring_name) or None


def is_scheduled_today(mailbox: MailboxConfig) -> bool:
    """Return True if this mailbox should run on today's weekday."""
    if "daily" in mailbox.schedule_days:
        return True
    today = date.today().strftime("%a").lower()
    return today in mailbox.schedule_days


def last_run_path(name: str) -> Path:
    return USER_CONFIG_DIR / f"last_run_{name}"


def read_last_run(name: str) -> date | None:
    path = last_run_path(name)
    if not path.exists():
        return None
    try:
        return date.fromisoformat(path.read_text().strip())
    except ValueError:
        log.warning("Could not parse last_run_%s; treating as first run.", name)
        return None


def write_last_run(name: str, run_date: date) -> None:
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    last_run_path(name).write_text(run_date.isoformat())


def store_credentials(
    email: str,
    imap_password: str | None,
    smtp_password: str | None = None,
) -> None:
    """Store IMAP (and optionally SMTP) password for an email account.

    Keyed by email address so all mailboxes on the same account share one entry.
    Passing ``None`` for either value leaves the existing keychain entry untouched.
    """
    import keyring
    if imap_password:
        keyring.set_password(KEYRING_SERVICE, _imap_key(email), imap_password)
    if smtp_password:
        keyring.set_password(KEYRING_SERVICE, _smtp_key(email), smtp_password)


def store_anthropic_key(api_key: str) -> None:
    """Store the Anthropic API key in the keychain."""
    import keyring
    keyring.set_password(KEYRING_SERVICE, "anthropic_api_key", api_key)
