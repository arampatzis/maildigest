"""Command-line entry points for maildigest."""

import imaplib
import logging
import os
import shutil
import smtplib
import subprocess
import sys
from datetime import date, datetime
from datetime import time as dt_time
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.markup import escape

from maildigest.config import (
    MailboxConfig,
    _find_config_file,
    _init_keyring,
    _parse_schedule_days,
    _parse_schedule_times,
    _try_get_secret,
    is_scheduled_today,
    load_config,
    read_last_run,
    store_anthropic_key,
    store_credentials,
    validate_config_file,
    write_last_run,
)
from maildigest.fetcher import fetch_emails
from maildigest.notifier import (
    save_to_markdown,
    send_email_summary,
)
from maildigest.summarizer import summarize_with_claude

log = logging.getLogger(__name__)
_console = Console(highlight=False)


def setup_logging(debug: bool) -> None:
    handler: logging.Handler
    if sys.stderr.isatty():
        handler = RichHandler(
            show_path=debug,
            rich_tracebacks=debug,
            markup=False,
            log_time_format="[%H:%M:%S]",
        )
        fmt = "%(message)s"
    else:
        handler = logging.StreamHandler()
        fmt = "%(levelname)-8s %(message)s"
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format=fmt,
        handlers=[handler],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def _build_unit(digest_bin: str) -> str:
    return f"""[Unit]
Description=maildigest daemon
After=network.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
ExecStart={digest_bin} daemon
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""


def _run_mailbox_digest(
    mb: MailboxConfig,
    api_key: str,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
    dry_run: bool = False,
    update_last_run: bool = True,
    print_summary: bool = True,
) -> None:
    """Fetch, summarise, save and email for one mailbox over [from_dt, to_dt].

    When from_dt/to_dt are None, the window is computed from read_last_run → now.
    """
    if to_dt is None:
        to_dt = datetime.now()
    if from_dt is None:
        last_run_dt = read_last_run(mb.name)
        from_dt = (
            last_run_dt
            if last_run_dt is not None
            else datetime.combine(date.today(), dt_time.min)
        )

    log.info(
        "[%s] Fetching %s → %s",
        mb.label,
        from_dt.strftime("%Y-%m-%d %H:%M:%S"),
        to_dt.strftime("%Y-%m-%d %H:%M:%S"),
    )

    emails = fetch_emails(
        imap_server=mb.imap_server,
        imap_port=mb.imap_port,
        email_address=mb.email,
        email_password=mb.imap_password,
        mail_folder=mb.imap_folder,
        from_dt=from_dt,
        to_dt=to_dt,
        body_char_limit=mb.body_char_limit,
        sender_filter=mb.sender_filter or None,
    )

    log.info("[%s] Found %d email(s) in '%s'.", mb.label, len(emails), mb.imap_folder)

    if not emails:
        log.info("[%s] No new emails — skipping summary.", mb.label)
        if update_last_run:
            write_last_run(mb.name, to_dt)
        return

    log.info("[%s] Summarising %d email(s) with Claude …", mb.label, len(emails))
    summary = summarize_with_claude(
        emails,
        mailbox=mb,
        api_key=api_key,
        target_date=to_dt.date(),
    )

    if print_summary:
        _console.rule(f"[bold]{mb.label}[/bold]  {to_dt.date()}")
        _console.print(Markdown(summary))
        _console.rule()
    else:
        log.info("[%s] Summary generated (%d chars).", mb.label, len(summary))

    if dry_run:
        log.info("[%s] Dry run — skipped save and email.", mb.label)
        return

    path = save_to_markdown(summary, mb.summary_dir, mb.label, target_date=to_dt.date())
    log.info("[%s] Saved → %s", mb.label, path)

    send_email_summary(
        summary=summary,
        smtp_server=mb.smtp_server,
        smtp_port=mb.smtp_port,
        email_address=mb.email,
        email_password=mb.smtp_password,
        label=mb.label,
        target_dt=to_dt,
    )
    log.info("[%s] Digest emailed to %s.", mb.label, mb.email)

    if update_last_run:
        write_last_run(mb.name, to_dt)


@click.group()
@click.option("--debug", is_flag=True, help="Enable debug logging.")
@click.version_option(package_name="maildigest")
@click.pass_context
def main(ctx: click.Context, debug: bool) -> None:
    """Summarise your mailboxes with Claude AI."""
    ctx.ensure_object(dict)
    setup_logging(debug)


@main.command()
@click.option(
    "--from",
    "from_arg",
    type=click.DateTime(formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M"]),
    default=None,
    help="Start datetime (YYYY-MM-DD or YYYY-MM-DDTHH:MM). Defaults to last run time.",
)
@click.option(
    "--to",
    "to_arg",
    type=click.DateTime(formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M"]),
    default=None,
    help="End datetime (YYYY-MM-DD or YYYY-MM-DDTHH:MM). Defaults to now.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Re-run from start of today even if already up to date.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Fetch and summarise but skip saving and emailing.",
)
@click.option(
    "--mailbox",
    "mailbox_filter",
    default=None,
    help="Process only this mailbox (by name).",
)
def run(
    from_arg: datetime | None,
    to_arg: datetime | None,
    force: bool,
    dry_run: bool,
    mailbox_filter: str | None,
) -> None:
    """Fetch and summarise emails for configured mailboxes."""
    try:
        _init_keyring()
    except RuntimeError as exc:
        log.exception("Fatal: %s", exc)
        sys.exit(1)
    try:
        cfg = load_config()
    except Exception as exc:
        log.exception("Fatal: %s", exc)
        log.debug("Traceback:", exc_info=True)
        sys.exit(1)

    today = date.today()

    mailboxes = [mb for mb in cfg.mailboxes if mb.enabled]
    if not mailboxes:
        log.info("No enabled mailboxes configured.")
        return
    if mailbox_filter:
        mailboxes = [mb for mb in mailboxes if mb.name == mailbox_filter]
        if not mailboxes:
            raise click.BadParameter(
                f"No enabled mailbox named '{mailbox_filter}'.",
                param_hint="--mailbox",
            )

    explicit_mode = from_arg is not None or to_arg is not None
    if explicit_mode:
        fixed_to: datetime = (
            to_arg
            if to_arg is not None
            else datetime.combine(today, dt_time(23, 59, 59))
        )
        if to_arg is not None and to_arg.time() == dt_time.min:
            fixed_to = to_arg.replace(hour=23, minute=59, second=59)
        fixed_from: datetime = (
            from_arg if from_arg is not None else datetime.combine(today, dt_time.min)
        )
        if fixed_from > fixed_to:
            raise click.BadParameter(f"--from {fixed_from} is after --to {fixed_to}.")

    if dry_run:
        log.info("Dry-run mode — summaries printed but not saved or emailed.")

    failed: list[str] = []
    for mb in mailboxes:
        try:
            if explicit_mode:
                _run_mailbox_digest(
                    mb,
                    cfg.anthropic_api_key,
                    fixed_from,
                    fixed_to,
                    dry_run,
                    update_last_run=False,
                )
            else:
                if not is_scheduled_today(mb) and not force:
                    log.info(
                        "[%s] Not scheduled for today (%s). Skipping.",
                        mb.label,
                        today.strftime("%a"),
                    )
                    continue
                to_dt = datetime.now()
                last_run_dt = read_last_run(mb.name)
                if force or last_run_dt is None:
                    from_dt = datetime.combine(today, dt_time.min)
                else:
                    from_dt = last_run_dt
                _run_mailbox_digest(mb, cfg.anthropic_api_key, from_dt, to_dt, dry_run)
        except Exception as exc:
            log.exception("[%s] Failed: %s", mb.label, exc)
            log.debug("Traceback:", exc_info=True)
            failed.append(mb.label)

    if failed:
        sys.exit(1)


@main.command("list")
def list_mailboxes() -> None:
    """Show all configured mailboxes and their status."""
    cfg_path = _find_config_file()
    if not cfg_path.exists():
        _console.print(
            f"[yellow]No config file found at {escape(str(cfg_path))}[/yellow]"
        )
        _console.print(
            "[italic]Copy config.yaml.example there to get started.[/italic]"
        )
        return

    with cfg_path.open() as f:
        raw = yaml.safe_load(f) or {}

    _console.print(f"[italic]Config: {cfg_path}[/italic]\n")
    mailboxes = raw.get("mailboxes", [])
    if not mailboxes:
        _console.print("[yellow]No mailboxes configured.[/yellow]")
        return
    for mb in mailboxes:
        name = mb["name"]
        label = mb.get("label", name)
        enabled = mb.get("enabled", True)
        imap = mb["imap"]
        smtp = mb["smtp"]
        sched = mb.get("schedule", {})
        days_raw = sched.get("days", "daily")
        days_str = (
            "daily" if days_raw == "daily" else " ".join(str(d) for d in days_raw)
        )
        times_raw = sched.get("times", [])
        times_str = "  ".join(
            str(t) for t in (times_raw if isinstance(times_raw, list) else [times_raw])
        )
        last = read_last_run(name)
        last_str = last.strftime("%Y-%m-%dT%H:%M:%S") if last else "never"

        if enabled:
            bullet = "[green]●[/green]"
            label_fmt = f"[bold]{escape(label)}[/bold]"
        else:
            bullet = "[red]○[/red]"
            label_fmt = f"[italic]{escape(label)}[/italic]"

        header = f"  {bullet} {label_fmt} [italic]({escape(name)})[/italic]"
        if not enabled:
            header += " [red italic]disabled[/red italic]"
        _console.print(header)

        key = "[italic]{:<9}[/italic]".format
        imap_port = imap.get("port", 993)
        smtp_port = smtp.get("port", 587)
        _console.print(
            f"    {key('IMAP:')} "
            f"{escape(imap['server'])}:[blue]{imap_port}[/blue]  "
            f"[cyan]{escape(imap['email'])}[/cyan]  "
            f"[italic]folder:[/italic] {escape(imap.get('folder', ''))}"
        )
        _console.print(
            f"    {key('SMTP:')} {escape(smtp['server'])}:[blue]{smtp_port}[/blue]"
        )
        schedule_fmt = f"[yellow]{escape(days_str)}[/yellow]"
        if times_str:
            schedule_fmt += f"  [cyan]{escape(times_str)}[/cyan]"
        _console.print(f"    {key('Schedule:')} {schedule_fmt}")
        last_fmt = (
            f"[italic]{escape(last_str)}[/italic]"
            if last_str == "never"
            else f"[green]{escape(last_str)}[/green]"
        )
        _console.print(f"    {key('Last run:')} {last_fmt}")
        _console.print()


@main.group()
def config() -> None:
    """Manage configuration and credentials."""


@config.command("setup")
def config_setup() -> None:
    """Store secrets for all configured mailboxes in the encrypted keyring."""
    cfg_path = _find_config_file()
    if not cfg_path.exists():
        raise click.ClickException(
            f"Config file not found at {cfg_path}. "
            "Create it first from config.yaml.example."
        )

    _console.print(
        "Using [bold]keyrings.cryptfile[/bold] backend. "
        "Secrets will be AES-encrypted on disk.\n"
        "You will be asked for this master password each time the daemon starts.\n"
    )
    try:
        _init_keyring()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    with cfg_path.open() as f:
        raw = yaml.safe_load(f) or {}

    _console.rule("[bold]Anthropic API key[/bold]")
    _console.print(
        "Get one at: [link]https://console.anthropic.com/settings/keys[/link]",
        highlight=False,
    )
    has_anthropic = bool(_try_get_secret("anthropic_api_key"))
    if has_anthropic:
        _console.print("  [italic]already stored — press Enter to keep[/italic]")
    api_key = click.prompt(
        "  Anthropic API key",
        hide_input=True,
        default="" if has_anthropic else None,
        show_default=False,
    )
    if api_key:
        store_anthropic_key(api_key)

    seen: dict[str, list[str]] = {}
    for mb_raw in raw.get("mailboxes", []):
        email = mb_raw["imap"]["email"]
        label = mb_raw.get("label", mb_raw["name"])
        seen.setdefault(email, []).append(label)

    for email, labels in seen.items():
        mailboxes_str = ", ".join(labels)
        _console.rule()
        _console.print(
            f"[bold]{escape(email)}[/bold]"
            f"  [italic]used by: {escape(mailboxes_str)}[/italic]"
        )

        if email.endswith("@gmail.com"):
            _console.print(
                "  [yellow]Gmail requires an app password"
                " (not your account password).[/yellow]\n"
                "  1. Go to: [link]https://myaccount.google.com/apppasswords[/link]\n"
                "  2. Create a new app password"
                " (name it e.g. [italic]maildigest[/italic])\n"
                "  3. Enter the 16-character password below.",
                highlight=False,
            )

        has_imap = bool(_try_get_secret(f"imap:{email}"))
        if has_imap:
            _console.print(
                "  [italic]IMAP: already stored — press Enter to keep[/italic]"
            )
        imap_pwd = click.prompt(
            "  IMAP password (or app password)",
            hide_input=True,
            default="" if has_imap else None,
            show_default=False,
        )

        has_smtp = bool(_try_get_secret(f"smtp:{email}"))
        if has_smtp:
            _console.print(
                "  [italic]SMTP: already stored — press Enter to keep[/italic]"
            )
        smtp_pwd = click.prompt(
            "  SMTP password (leave blank to reuse IMAP password)",
            hide_input=True,
            default="",
            show_default=False,
        )

        if imap_pwd or smtp_pwd:
            store_credentials(email, imap_pwd or None, smtp_pwd or None)

    _console.print("\n[green]All credentials stored successfully.[/green]")
    log.info("config setup: credentials stored.")


def _check_anthropic_key(api_key: str) -> str | None:
    """Returns None on success, error string on failure."""
    import anthropic

    try:
        anthropic.Anthropic(api_key=api_key).models.list()
        return None
    except anthropic.AuthenticationError:
        return "invalid API key"
    except Exception as exc:
        return str(exc)


def _check_imap_login(server: str, port: int, email: str, password: str) -> str | None:
    """Returns None on success, error string on failure."""
    try:
        conn = imaplib.IMAP4_SSL(server, port)
        conn.login(email, password)
        conn.logout()
        return None
    except Exception as exc:
        return str(exc)


def _check_imap_folder(
    server: str, port: int, email: str, password: str, folder: str
) -> str | None:
    """Returns None on success, error string on failure."""
    try:
        conn = imaplib.IMAP4_SSL(server, port)
        conn.login(email, password)
        status, _ = conn.select(f'"{folder}"')
        conn.logout()
        if status != "OK":
            return f"folder '{folder}' not found"
        return None
    except Exception as exc:
        return str(exc)


def _check_smtp(server: str, port: int, email: str, password: str) -> str | None:
    """Returns None on success, error string on failure."""
    try:
        with smtplib.SMTP(server, port) as s:
            s.starttls()
            s.login(email, password)
        return None
    except Exception as exc:
        return str(exc)


@config.command("validate")
def config_validate() -> None:
    """Validate config file structure without loading secrets or network."""
    try:
        path, schema = validate_config_file()
    except FileNotFoundError as exc:
        _console.print(f"[red]Error:[/red] {escape(str(exc))}")
        log.error("config validate: %s", exc)
        raise SystemExit(1) from exc
    except ValueError as exc:
        lines = str(exc).splitlines()
        _console.print(f"[red]{escape(lines[0])}[/red]")
        for line in lines[1:]:
            field, _, msg = line.strip().partition(": ")
            _console.print(f"  [yellow]{escape(field)}[/yellow]: {escape(msg)}")
        log.error("config validate: %s", exc)
        raise SystemExit(1) from exc

    _console.print(f"[italic]{escape(str(path))}[/italic]")
    _console.rule()
    for mb in schema.mailboxes:
        status = "[green]enabled[/green]" if mb.enabled else "[italic]disabled[/italic]"
        label = escape(mb.label or mb.name)
        days = _parse_schedule_days(mb.schedule.days)
        times = _parse_schedule_times(mb.schedule.times)
        days_str = (
            "daily"
            if "daily" in days
            else ",".join(
                sorted(
                    days, key=["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index
                )
            )
        )
        times_str = ", ".join(f"{h:02d}:{m:02d}" for h, m in times)
        _console.print(f"  [bold]{label}[/bold]  {status}")
        _console.print(
            f"    [italic]schedule[/italic]  "
            f"[yellow]{days_str}[/yellow] at [cyan]{times_str}[/cyan]"
        )
        _console.print(
            f"    [italic]imap    [/italic]  "
            f"[blue]{escape(mb.imap.server)}:{mb.imap.port}[/blue]"
            f"  [italic]{escape(mb.imap.folder)}[/italic]"
        )
        _console.print(
            f"    [italic]smtp    [/italic]  "
            f"[blue]{escape(mb.smtp.server)}:{mb.smtp.port}[/blue]"
        )
    _console.rule()
    enabled = sum(1 for mb in schema.mailboxes if mb.enabled)
    disabled = len(schema.mailboxes) - enabled
    _console.print(
        f"[green]Valid.[/green]  "
        f"{len(schema.mailboxes)} mailbox(es) — {enabled} enabled, {disabled} disabled."
    )
    log.info(
        "config validate: valid — %d mailbox(es), %d enabled.",
        len(schema.mailboxes),
        enabled,
    )


@config.command("check")
def config_check() -> None:
    """Verify IMAP/SMTP login and folder accessibility for all configured mailboxes."""
    try:
        _init_keyring()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        cfg = load_config()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    mailboxes = [mb for mb in cfg.mailboxes if mb.enabled]
    if not mailboxes:
        _console.print("[yellow]No enabled mailboxes to check.[/yellow]")
        return

    all_ok = True

    # Anthropic API key
    _console.rule("[bold]Anthropic API key[/bold]")
    err = _check_anthropic_key(cfg.anthropic_api_key)
    if err:
        _console.print(f"  [red]FAILED[/red]: {escape(err)}")
        all_ok = False
    else:
        _console.print("  [green]OK[/green]")

    # IMAP login — one check per unique (server, port, email)
    _console.rule("[bold]IMAP login[/bold]")
    imap_ok: set[tuple] = set()
    seen_imap: set[tuple] = set()
    for mb in mailboxes:
        key = (mb.imap_server, mb.imap_port, mb.email)
        if key in seen_imap:
            continue
        seen_imap.add(key)
        err = _check_imap_login(
            mb.imap_server, mb.imap_port, mb.email, mb.imap_password
        )
        if err:
            _console.print(
                f"  [blue]{mb.imap_server}:{mb.imap_port}[/blue]"
                f"  [cyan]{escape(mb.email)}[/cyan]"
                f"  [red]FAILED[/red]: {escape(err)}"
            )
            all_ok = False
        else:
            _console.print(
                f"  [blue]{mb.imap_server}:{mb.imap_port}[/blue]"
                f"  [cyan]{escape(mb.email)}[/cyan]  [green]OK[/green]"
            )
            imap_ok.add(key)

    # SMTP login — one check per unique (server, port, email)
    _console.rule("[bold]SMTP login[/bold]")
    seen_smtp: set[tuple] = set()
    for mb in mailboxes:
        key = (mb.smtp_server, mb.smtp_port, mb.email)
        if key in seen_smtp:
            continue
        seen_smtp.add(key)
        err = _check_smtp(mb.smtp_server, mb.smtp_port, mb.email, mb.smtp_password)
        if err:
            _console.print(
                f"  [blue]{mb.smtp_server}:{mb.smtp_port}[/blue]"
                f"  [cyan]{escape(mb.email)}[/cyan]"
                f"  [red]FAILED[/red]: {escape(err)}"
            )
            all_ok = False
        else:
            _console.print(
                f"  [blue]{mb.smtp_server}:{mb.smtp_port}[/blue]"
                f"  [cyan]{escape(mb.email)}[/cyan]  [green]OK[/green]"
            )

    # IMAP folders — one check per mailbox; skip if login failed for that account
    _console.rule("[bold]IMAP folders[/bold]")
    for mb in mailboxes:
        key = (mb.imap_server, mb.imap_port, mb.email)
        if key not in imap_ok:
            _console.print(
                f"  [bold]{escape(mb.label)}[/bold]"
                f"  [italic]{escape(mb.imap_folder)}[/italic]"
                f"  [italic]skipped (login failed)[/italic]"
            )
            continue
        err = _check_imap_folder(
            mb.imap_server, mb.imap_port, mb.email, mb.imap_password, mb.imap_folder
        )
        if err:
            _console.print(
                f"  [bold]{escape(mb.label)}[/bold]"
                f"  [italic]{escape(mb.imap_folder)}[/italic]"
                f"  [red]FAILED[/red]: {escape(err)}"
            )
            all_ok = False
        else:
            _console.print(
                f"  [bold]{escape(mb.label)}[/bold]"
                f"  [italic]{escape(mb.imap_folder)}[/italic]  [green]OK[/green]"
            )

    _console.rule()
    if all_ok:
        _console.print("[green]All checks passed.[/green]")
        log.info("config check: all checks passed.")
    else:
        _console.print("[red]Some checks failed.[/red]")
        log.warning("config check: some checks failed.")
        sys.exit(1)


@main.command(hidden=True)
@click.option("--config", "cfg_path", default=None, help="Path to config.yaml.")
def daemon(cfg_path: str | None) -> None:
    """Run the long-lived scheduler daemon."""
    from maildigest.daemon import run_daemon

    run_daemon(cfg_path)


@main.group()
def service() -> None:
    """Manage the maildigest systemd user service."""


@service.command("install")
def service_install() -> None:
    """Write unit file, enable service, and enable linger for boot persistence."""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    # Use the direct venv script, not a pyenv shim that requires pyenv's environment.
    direct_bin = Path(sys.executable).parent / "maildigest"
    digest_bin = (
        str(direct_bin)
        if direct_bin.exists()
        else (shutil.which("maildigest") or str(direct_bin))
    )
    unit = unit_dir / "maildigest.service"
    unit.write_text(_build_unit(digest_bin))
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "maildigest"], check=True)
        subprocess.run(["loginctl", "enable-linger", os.getenv("USER", "")], check=True)
    except subprocess.CalledProcessError as exc:
        log.exception("Command failed: %s", exc)
        sys.exit(1)
    log.info("Service installed and enabled.")
    _console.print("  [italic]Start:[/italic]   [bold]maildigest service start[/bold]")
    _console.print("  [italic]Status:[/italic]  [bold]maildigest service status[/bold]")
    _console.print("  [italic]Logs:[/italic]    [bold]maildigest service log[/bold]")


@service.command("uninstall")
def service_uninstall() -> None:
    """Stop, disable, and remove the systemd user unit."""
    subprocess.run(["systemctl", "--user", "stop", "maildigest"], check=False)
    subprocess.run(["systemctl", "--user", "disable", "maildigest"], check=False)
    unit = Path.home() / ".config" / "systemd" / "user" / "maildigest.service"
    if unit.exists():
        unit.unlink()
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    except subprocess.CalledProcessError as exc:
        log.exception("systemctl failed: %s", exc)
        sys.exit(1)
    log.info("Service uninstalled.")


@service.command("start")
def service_start() -> None:
    """Start the daemon (prompts for keyring master password)."""
    state = subprocess.run(
        ["systemctl", "--user", "is-active", "maildigest"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if state in ("active", "activating"):
        log.info("Service is already running.")
        subprocess.run(
            ["systemctl", "--user", "status", "maildigest", "--no-pager", "-l"],
            check=False,
        )
        sys.stdout.write("\033[0m")
        sys.stdout.flush()
        return

    import getpass
    import stat

    password = getpass.getpass("Keyring master password: ")

    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    passwd_file = runtime_dir / "maildigest.passwd"
    passwd_file.write_text(password)
    passwd_file.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600 — owner only
    # The daemon reads and deletes this file on startup; we do not delete it here
    # because systemctl start returns before the daemon process has read it.
    try:
        subprocess.run(["systemctl", "--user", "start", "maildigest"], check=True)
    except subprocess.CalledProcessError as exc:
        passwd_file.unlink(missing_ok=True)
        log.exception("systemctl failed: %s", exc)
        sys.exit(1)
    subprocess.run(
        ["systemctl", "--user", "status", "maildigest", "--no-pager", "-l"], check=False
    )
    sys.stdout.write("\033[0m")
    sys.stdout.flush()


@service.command("stop")
def service_stop() -> None:
    """Stop the daemon."""
    try:
        subprocess.run(["systemctl", "--user", "stop", "maildigest"], check=True)
    except subprocess.CalledProcessError as exc:
        log.exception("systemctl failed: %s", exc)
        sys.exit(1)
    log.info("Service stopped.")


@service.command("reload")
def service_reload() -> None:
    """Reload config without restarting (sends SIGHUP to the daemon)."""
    try:
        subprocess.run(["systemctl", "--user", "reload", "maildigest"], check=True)
        log.info("Config reloaded.")
    except subprocess.CalledProcessError as exc:
        log.exception("systemctl failed: %s", exc)
        sys.exit(1)


@service.command("status")
def service_status() -> None:
    """Show current daemon status."""
    subprocess.run(["systemctl", "--user", "status", "maildigest", "-l"], check=False)
    sys.stdout.write("\033[0m")
    sys.stdout.flush()


@service.command("log")
@click.option("--follow", "-f", is_flag=True, help="Follow log output.")
@click.option(
    "--lines", "-n", default=50, show_default=True, help="Number of lines to show."
)
def service_log(follow: bool, lines: int) -> None:
    """Show daemon logs."""
    cmd = ["journalctl", "--user", "-u", "maildigest", f"-n{lines}"]
    if follow:
        cmd.append("-f")
    subprocess.run(cmd, check=False)
    sys.stdout.write("\033[0m")
    sys.stdout.flush()
