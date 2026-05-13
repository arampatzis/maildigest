"""Command-line entry points for maildigest."""

import logging
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, time as dt_time
from pathlib import Path

import click
import yaml

from maildigest.config import (
    USER_CONFIG_DIR,
    _find_config_file,
    _try_get_secret,
    is_scheduled_today,
    load_config,
    read_last_run,
    store_anthropic_key,
    store_credentials,
    write_last_run,
)
from maildigest.fetcher import fetch_emails
from maildigest.notifier import (
    save_to_markdown,
    send_email_summary,
)
from maildigest.summarizer import summarize_with_claude

log = logging.getLogger(__name__)

_PLIST_LABEL = "com.user.maildigest"
_LAUNCHAGENTS = Path.home() / "Library" / "LaunchAgents"
_LAUNCHD_LOG_DIR = Path.home() / "Library" / "Logs" / "maildigest"
_LAUNCHD_DOMAIN = f"gui/{os.getuid()}"


def setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    fmt = (
        "%(asctime)s %(name)-28s %(levelname)-8s %(message)s"
        if debug
        else "%(asctime)s %(message)s"
    )
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")


def _build_plist(digest_bin: str, times: list[tuple[int, int]]) -> str:
    interval_dicts = "\n".join(
        f"        <dict>\n"
        f"            <key>Hour</key><integer>{h}</integer>\n"
        f"            <key>Minute</key><integer>{m}</integer>\n"
        f"        </dict>"
        for h, m in times
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_PLIST_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{digest_bin}</string>
        <string>run</string>
    </array>

    <key>StartCalendarInterval</key>
    <array>
{interval_dicts}
    </array>

    <key>StandardOutPath</key>
    <string>{_LAUNCHD_LOG_DIR}/output.log</string>
    <key>StandardErrorPath</key>
    <string>{_LAUNCHD_LOG_DIR}/error.log</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""


def _launchctl(verb: str, *args: str) -> None:
    """Run launchctl, capturing all output and surfacing it only on failure."""
    result = subprocess.run(
        ["launchctl", verb, *args],
        capture_output=True,
        text=True,
    )
    if result.stdout:
        log.debug("launchctl stdout: %s", result.stdout.strip())
    if result.stderr:
        log.debug("launchctl stderr: %s", result.stderr.strip())
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, stderr=result.stderr
        )


@click.group()
@click.option("--debug", is_flag=True, help="Enable debug logging.")
@click.pass_context
def main(ctx: click.Context, debug: bool) -> None:
    """Summarise your mailboxes with Claude AI."""
    ctx.ensure_object(dict)
    setup_logging(debug)


@main.command()
@click.option(
    "--from", "from_arg",
    type=click.DateTime(formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M"]),
    default=None,
    help="Start datetime (YYYY-MM-DD or YYYY-MM-DDTHH:MM). Defaults to last run time.",
)
@click.option(
    "--to", "to_arg",
    type=click.DateTime(formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M"]),
    default=None,
    help="End datetime (YYYY-MM-DD or YYYY-MM-DDTHH:MM). Defaults to now.",
)
@click.option(
    "--force", is_flag=True, default=False,
    help="Re-run from start of today even if already up to date.",
)
@click.option(
    "--dry-run", "dry_run", is_flag=True, default=False,
    help="Fetch and summarise but skip saving and emailing.",
)
@click.option(
    "--mailbox", "mailbox_filter", default=None,
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
        cfg = load_config()
    except Exception as exc:
        log.error("Fatal: %s", exc)
        log.debug("Traceback:", exc_info=True)
        sys.exit(1)

    today = date.today()

    mailboxes = [mb for mb in cfg.mailboxes if mb.enabled]
    if mailbox_filter:
        mailboxes = [mb for mb in mailboxes if mb.name == mailbox_filter]
        if not mailboxes:
            raise click.BadParameter(
                f"No enabled mailbox named '{mailbox_filter}'.",
                param_hint="--mailbox",
            )

    # Resolve explicit --from / --to once before the per-mailbox loop.
    explicit_mode = from_arg is not None or to_arg is not None
    if explicit_mode:
        fixed_to: datetime = to_arg if to_arg is not None else datetime.combine(today, dt_time(23, 59, 59))
        # date-only --to (midnight) → end of that day
        if to_arg is not None and to_arg.time() == dt_time.min:
            fixed_to = to_arg.replace(hour=23, minute=59, second=59)
        fixed_from: datetime = from_arg if from_arg is not None else datetime.combine(today, dt_time.min)
        if fixed_from > fixed_to:
            raise click.BadParameter(f"--from {fixed_from} is after --to {fixed_to}.")

    if dry_run:
        log.info("Dry-run mode — summaries printed but not saved or emailed.")

    failed: list[str] = []
    for mb in mailboxes:
        try:
            if explicit_mode:
                from_dt = fixed_from
                to_dt = fixed_to
            else:
                if not is_scheduled_today(mb) and not force:
                    log.info(
                        "[%s] Not scheduled for today (%s). Skipping.",
                        mb.label, today.strftime("%a"),
                    )
                    continue
                to_dt = datetime.now()
                last_run_dt = read_last_run(mb.name)
                if force or last_run_dt is None:
                    from_dt = datetime.combine(today, dt_time.min)
                else:
                    from_dt = last_run_dt

            log.info("[%s] Fetching %s → %s", mb.label, from_dt, to_dt)

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

            if not emails:
                log.info("[%s] No new emails — skipping summary.", mb.label)
                if not explicit_mode:
                    write_last_run(mb.name, to_dt)
                continue

            summary = summarize_with_claude(
                emails,
                mailbox=mb,
                api_key=cfg.anthropic_api_key,
                target_date=to_dt.date(),
            )

            click.echo(f"\n── [{mb.label}] Summary {to_dt.date()} " + "─" * 25)
            click.echo(summary)
            click.echo("─" * 55 + "\n")

            if dry_run:
                log.info("[%s] Dry run — skipped save and email.", mb.label)
                continue

            log.info("[%s] Saving summary to disk …", mb.label)
            path = save_to_markdown(summary, mb.summary_dir, mb.label, target_date=to_dt.date())
            log.info("[%s] Saved → %s", mb.label, path)

            log.info("[%s] Emailing summary to %s …", mb.label, mb.email)
            send_email_summary(
                summary=summary,
                smtp_server=mb.smtp_server,
                smtp_port=mb.smtp_port,
                email_address=mb.email,
                email_password=mb.smtp_password,
                label=mb.label,
                target_date=to_dt.date(),
            )
            log.info("[%s] Email delivered.", mb.label)

            write_last_run(mb.name, to_dt)

        except Exception as exc:
            log.error("[%s] Failed: %s", mb.label, exc)
            log.debug("Traceback:", exc_info=True)
            failed.append(mb.label)

    if failed:
        sys.exit(1)


@main.command("list")
def list_mailboxes() -> None:
    """Show all configured mailboxes and their status."""
    cfg_path = _find_config_file()
    if not cfg_path.exists():
        click.echo(f"No config file found at {cfg_path}")
        click.echo("Copy config.yaml.example there to get started.")
        return

    with cfg_path.open() as f:
        raw = yaml.safe_load(f)

    click.echo(f"Config: {cfg_path}\n")
    for mb in raw.get("mailboxes", []):
        name = mb["name"]
        label = mb.get("label", name)
        enabled = mb.get("enabled", True)
        imap = mb["imap"]
        smtp = mb["smtp"]
        sched = mb.get("schedule", {})
        days_raw = sched.get("days", "daily")
        days_str = "daily" if days_raw == "daily" else " ".join(
            str(d) for d in days_raw
        )
        last = read_last_run(name)
        last_str = last.isoformat() if last else "never"
        status = "●" if enabled else "○"
        disabled = " [disabled]" if not enabled else ""

        click.echo(f"  {status} {label} ({name}){disabled}")
        click.echo(
            f"    IMAP:     {imap['server']}:{imap.get('port', 993)}"
            f"  {imap['email']}  folder: {imap.get('folder', 'Newsletters')}"
        )
        click.echo(f"    SMTP:     {smtp['server']}:{smtp.get('port', 587)}")
        click.echo(f"    Schedule: {days_str}")
        click.echo(f"    Last run: {last_str}")
        click.echo()


@main.command("setup-credentials")
def setup_credentials() -> None:
    """Store secrets for all configured mailboxes in the system keychain."""
    cfg_path = _find_config_file()
    if not cfg_path.exists():
        raise click.ClickException(
            f"Config file not found at {cfg_path}. "
            "Create it first from config.yaml.example."
        )

    with cfg_path.open() as f:
        raw = yaml.safe_load(f)

    click.echo(
        "Credentials will be stored in the system keychain (Passwords app).\n"
        "They are never written to disk in plain text.\n"
    )
    click.echo(
        "Anthropic API key — get one at: https://console.anthropic.com/settings/keys"
    )
    has_anthropic = bool(_try_get_secret("anthropic_api_key"))
    if has_anthropic:
        click.echo("  [already stored — press Enter to keep]")
    api_key = click.prompt(
        "Anthropic API key", hide_input=True,
        default="" if has_anthropic else None,
        show_default=False,
    )
    if api_key:
        store_anthropic_key(api_key)

    # Collect unique email addresses (preserving first-seen order).
    # Multiple mailboxes on the same account share one set of credentials.
    seen: dict[str, list[str]] = {}  # email → list of mailbox labels
    for mb_raw in raw.get("mailboxes", []):
        email = mb_raw["imap"]["email"]
        label = mb_raw.get("label", mb_raw["name"])
        seen.setdefault(email, []).append(label)

    for email, labels in seen.items():
        mailboxes_str = ", ".join(labels)
        click.echo(f"\nAccount: {email}  (used by: {mailboxes_str})")

        has_imap = bool(_try_get_secret(f"imap:{email}"))
        if has_imap:
            click.echo("  IMAP: [already stored — press Enter to keep]")
        imap_pwd = click.prompt(
            "  IMAP password (or app password)", hide_input=True,
            default="" if has_imap else None,
            show_default=False,
        )

        has_smtp = bool(_try_get_secret(f"smtp:{email}"))
        if has_smtp:
            click.echo("  SMTP: [already stored — press Enter to keep]")
        smtp_pwd = click.prompt(
            "  SMTP password (leave blank to reuse IMAP password)",
            hide_input=True, default="", show_default=False,
        )

        if imap_pwd or smtp_pwd:
            store_credentials(email, imap_pwd or None, smtp_pwd or None)
        log.info("Stored credentials for %s.", email)

    click.echo("\nAll credentials stored successfully.")


@main.command()
@click.option(
    "--time", "run_times",
    multiple=True,
    default=("09:00",),
    show_default=True,
    help="Time to run in 24-hour HH:MM format. Repeat to add multiple daily fires.",
)
def install(run_times: tuple[str, ...]) -> None:
    """Schedule maildigest to run automatically via macOS launchd."""
    try:
        times: list[tuple[int, int]] = []
        for run_time in run_times:
            try:
                hour_str, minute_str = run_time.split(":")
                hour, minute = int(hour_str), int(minute_str)
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
            except ValueError:
                raise click.BadParameter(
                    "Expected HH:MM in 24-hour format (e.g. 09:00 or 13:40).",
                    param_hint="--time",
                )
            times.append((hour, minute))

        digest_bin = shutil.which("maildigest") or str(
            Path(sys.executable).parent / "maildigest"
        )
        _LAUNCHD_LOG_DIR.mkdir(parents=True, exist_ok=True)
        _LAUNCHAGENTS.mkdir(parents=True, exist_ok=True)

        plist_path = _LAUNCHAGENTS / f"{_PLIST_LABEL}.plist"
        if plist_path.exists():
            try:
                _launchctl("bootout", _LAUNCHD_DOMAIN, str(plist_path))
            except subprocess.CalledProcessError:
                pass  # job wasn't registered; safe to overwrite
        plist_path.write_text(_build_plist(digest_bin, times))
        _launchctl("bootstrap", _LAUNCHD_DOMAIN, str(plist_path))

        times_str = ", ".join(run_times)
        log.info("Scheduled: maildigest run daily at %s.", times_str)
        log.info("Plist:  %s", plist_path)
        log.info("Logs:   %s/output.log", _LAUNCHD_LOG_DIR)
    except Exception as exc:
        log.error("Fatal: %s", exc)
        log.debug("Traceback:", exc_info=True)
        sys.exit(1)


@main.command()
def uninstall() -> None:
    """Remove the launchd schedule."""
    plist_path = _LAUNCHAGENTS / f"{_PLIST_LABEL}.plist"
    if not plist_path.exists():
        log.info("Nothing to uninstall — plist not found.")
        return
    _launchctl("bootout", _LAUNCHD_DOMAIN, str(plist_path))
    plist_path.unlink()
    log.info("Uninstalled. maildigest will no longer run automatically.")
