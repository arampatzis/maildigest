"""Configuration loading for maildigest."""

import getpass
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger(__name__)

KEYRING_SERVICE = "maildigest"

_XDG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
USER_CONFIG_DIR = _XDG_HOME / "maildigest"
_USER_CONFIG_FILE = USER_CONFIG_DIR / "config.yaml"

VALID_DAYS = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun", "daily"})

_keyring = None  # CryptFileKeyring instance, set by _init_keyring()


def _imap_key(email: str) -> str:
    return f"imap:{email}"


def _smtp_key(email: str) -> str:
    return f"smtp:{email}"


def _email_env_key(email: str, prefix: str) -> str:
    safe = email.upper().replace("@", "_").replace(".", "_").replace("-", "_")
    return f"{prefix}_{safe}"


def _init_keyring() -> None:
    """Initialise the CryptFileKeyring singleton.

    Reads the master password from KEYRING_CRYPTFILE_PASSWORD env var if set
    (useful for CI/testing), otherwise prompts interactively via getpass.
    Must be called before any _get_secret / store_* calls.
    """
    global _keyring
    from keyrings.cryptfile.cryptfile import CryptFileKeyring

    kr = CryptFileKeyring()
    password = os.environ.pop("KEYRING_CRYPTFILE_PASSWORD", None)
    if not password:
        # Check for password written by `service start` to the user's tmpfs runtime dir.
        runtime_dir = Path(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        )
        passwd_file = runtime_dir / "maildigest.passwd"
        if passwd_file.exists():
            try:
                password = passwd_file.read_text().strip()
            finally:
                passwd_file.unlink(missing_ok=True)
    if not password:
        is_new = not Path(kr.file_path).exists()
        password = getpass.getpass("Keyring master password: ")
        if is_new:
            confirm = getpass.getpass("Confirm master password: ")
            if password != confirm:
                raise RuntimeError("Passwords do not match.")
    try:
        kr.keyring_key = password
    except ValueError as exc:
        raise RuntimeError(
            "Wrong master password (or the keyring file is corrupted)."
        ) from exc
    _keyring = kr


# ---------------------------------------------------------------------------
# Runtime data model (dataclasses — holds resolved secrets too)
# ---------------------------------------------------------------------------


@dataclass
class MailboxConfig:
    name: str  # machine name — keychain key prefix, last_run filename
    label: str  # human-readable display name
    enabled: bool
    imap_server: str
    imap_port: int
    email: str  # IMAP login + SMTP From/To (send-to-self)
    imap_folder: str
    smtp_server: str
    smtp_port: int
    schedule_days: frozenset  # {"daily"} or {"mon", "tue", …}
    schedule_times: list  # [(hour, minute), …]
    language: str
    focus_areas: list
    extra_instructions: str
    custom_prompt: str | None
    body_char_limit: int
    sender_filter: list  # only fetch from these senders (empty = no filter)
    summary_dir: Path
    imap_password: str = field(repr=False)
    smtp_password: str = field(repr=False)


@dataclass
class AppConfig:
    anthropic_api_key: str = field(repr=False)
    mailboxes: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# YAML schema (pydantic — validates raw config before any logic runs)
# ---------------------------------------------------------------------------


class _ImapSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server: str
    port: int = 993
    email: str
    folder: str  # required — no default


class _SmtpSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server: str
    port: int = 587


class _ScheduleSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: str | list[str]  # required — no default
    times: list[str]  # required — no default

    @field_validator("days")
    @classmethod
    def _check_days(cls, v: str | list[str]) -> str | list[str]:
        _parse_schedule_days(v)  # raises ValueError on bad input
        return v

    @field_validator("times")
    @classmethod
    def _check_times(cls, v: list[str]) -> list[str]:
        _parse_schedule_times(v)  # raises ValueError on bad input
        return v


class _SummarizerSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str = "English"
    focus_areas: list[str] = Field(default_factory=list)
    extra_instructions: str = ""
    custom_prompt: str | None = None


class _MailboxSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    label: str | None = None
    enabled: bool = True
    imap: _ImapSchema
    smtp: _SmtpSchema
    schedule: _ScheduleSchema
    summarizer: _SummarizerSchema = Field(default_factory=_SummarizerSchema)
    body_char_limit: int = 3000
    sender_filter: list[str] = Field(default_factory=list)
    summary_dir: str | None = None


class _AppSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary_dir: str = "~/Documents/NewsletterSummaries"
    mailboxes: list[_MailboxSchema]


# ---------------------------------------------------------------------------
# Config file helpers
# ---------------------------------------------------------------------------


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


def _parse_schedule_days(raw: str | list[str]) -> frozenset[str]:
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


def _parse_schedule_times(raw: str | list[str] | None) -> list[tuple[int, int]]:
    if raw is None:
        return [(9, 0)]
    if isinstance(raw, str):
        raw = [raw]
    result = []
    for item in raw:
        try:
            h_str, m_str = str(item).split(":")
            hour, minute = int(h_str), int(m_str)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except (ValueError, AttributeError):
            raise ValueError(
                f"Invalid schedule time '{item}'. Expected HH:MM (e.g. '09:00')."
            ) from None
        result.append((hour, minute))
    if not result:
        raise ValueError("schedule.times cannot be empty.")
    return result


# ---------------------------------------------------------------------------
# Main config loader
# ---------------------------------------------------------------------------


def load_config(config_path: str | None = None) -> AppConfig:
    path = Path(config_path).expanduser() if config_path else _find_config_file()
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found at {path}. "
            "Copy config.yaml.example there and fill in your settings."
        )

    with path.open() as f:
        raw = yaml.safe_load(f)

    try:
        schema = _AppSchema.model_validate(raw)
    except ValidationError as exc:
        lines = ["Config file is invalid:"]
        for error in exc.errors():
            loc = ".".join(str(part) for part in error["loc"])
            lines.append(f"  {loc}: {error['msg']}")
        raise ValueError("\n".join(lines)) from None

    anthropic_key = _get_secret("ANTHROPIC_API_KEY", "anthropic_api_key")
    global_summary_dir = Path(schema.summary_dir).expanduser()

    mailboxes: list[MailboxConfig] = []
    for mb in schema.mailboxes:
        email = mb.imap.email
        imap_password = _get_secret(_email_env_key(email, "IMAP"), _imap_key(email))
        smtp_password = _try_get_secret(_smtp_key(email)) or imap_password

        summary_dir = (
            Path(mb.summary_dir).expanduser()
            if mb.summary_dir
            else global_summary_dir / mb.name
        )

        mailboxes.append(
            MailboxConfig(
                name=mb.name,
                label=mb.label or mb.name,
                enabled=mb.enabled,
                imap_server=mb.imap.server,
                imap_port=mb.imap.port,
                email=email,
                imap_folder=mb.imap.folder,
                smtp_server=mb.smtp.server,
                smtp_port=mb.smtp.port,
                schedule_days=_parse_schedule_days(mb.schedule.days),
                schedule_times=_parse_schedule_times(mb.schedule.times),
                language=mb.summarizer.language,
                focus_areas=list(mb.summarizer.focus_areas),
                extra_instructions=mb.summarizer.extra_instructions,
                custom_prompt=mb.summarizer.custom_prompt,
                body_char_limit=mb.body_char_limit,
                sender_filter=list(mb.sender_filter),
                summary_dir=summary_dir,
                imap_password=imap_password,
                smtp_password=smtp_password,
            )
        )

    return AppConfig(anthropic_api_key=anthropic_key, mailboxes=mailboxes)


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def _get_secret(env_key: str, keyring_name: str) -> str:
    if _keyring is not None:
        value = _keyring.get_password(KEYRING_SERVICE, keyring_name)
        if value:
            log.debug("Secret '%s': loaded from keyring.", keyring_name)
            return value
    value = os.environ.get(env_key)
    if not value:
        raise ValueError(
            f"Missing required secret '{env_key}'. "
            "Run `maildigest config setup` or set the variable in your environment."
        )
    log.debug("Secret '%s': loaded from environment.", keyring_name)
    return value


def _try_get_secret(keyring_name: str) -> str | None:
    if _keyring is None:
        return None
    try:
        return _keyring.get_password(KEYRING_SERVICE, keyring_name) or None
    except Exception:
        return None


def is_scheduled_today(mailbox: MailboxConfig) -> bool:
    """Return True if this mailbox should run on today's weekday."""
    if "daily" in mailbox.schedule_days:
        return True
    today = date.today().strftime("%a").lower()
    return today in mailbox.schedule_days


def last_run_path(name: str) -> Path:
    return USER_CONFIG_DIR / f"last_run_{name}"


def read_last_run(name: str) -> datetime | None:
    path = last_run_path(name)
    if not path.exists():
        return None
    try:
        text = path.read_text().strip()
        if "T" in text:
            return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
        return datetime.combine(date.fromisoformat(text), datetime.min.time())
    except ValueError:
        log.warning("Could not parse last_run_%s; treating as first run.", name)
        return None


def write_last_run(name: str, dt: datetime) -> None:
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    last_run_path(name).write_text(dt.strftime("%Y-%m-%dT%H:%M:%S"))


def store_credentials(
    email: str,
    imap_password: str | None,
    smtp_password: str | None = None,
) -> None:
    """Store IMAP (and optionally SMTP) password for an email account."""
    if _keyring is None:
        raise RuntimeError("Call _init_keyring() before storing credentials.")
    if imap_password:
        _keyring.set_password(KEYRING_SERVICE, _imap_key(email), imap_password)
    if smtp_password:
        _keyring.set_password(KEYRING_SERVICE, _smtp_key(email), smtp_password)


def store_anthropic_key(api_key: str) -> None:
    """Store the Anthropic API key in the keyring."""
    if _keyring is None:
        raise RuntimeError("Call _init_keyring() before storing credentials.")
    _keyring.set_password(KEYRING_SERVICE, "anthropic_api_key", api_key)
