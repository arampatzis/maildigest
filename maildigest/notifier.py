"""Delivery mechanisms: markdown file and email."""

import logging
import smtplib
import traceback as _traceback
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import markdown as _md

log = logging.getLogger(__name__)


def save_to_markdown(
    summary: str,
    summary_dir: Path,
    label: str,
    target_date: date | None = None,
) -> Path:
    d = target_date or date.today()
    summary_dir.mkdir(parents=True, exist_ok=True)
    date_iso = d.isoformat()
    date_fmt = d.strftime("%B %d, %Y")
    path = summary_dir / f"summary-{date_iso}.md"
    log.debug("Writing markdown to %s", path)
    path.write_text(f"# {label} — {date_fmt}\n\n{summary}\n")
    return path


def send_email_summary(
    summary: str,
    smtp_server: str,
    smtp_port: int,
    email_address: str,
    email_password: str,
    label: str,
    target_dt: datetime | None = None,
) -> None:
    dt = target_dt or datetime.now()
    date_fmt = dt.strftime("%B %d, %Y %H:%M")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{label} — {date_fmt}"
    msg["From"] = email_address
    msg["To"] = email_address

    html = (
        "<html><body style='font-family:sans-serif;max-width:700px'>"
        f"<h2>{label} — {date_fmt}</h2>" + _md.markdown(summary) + "</body></html>"
    )
    msg.attach(MIMEText(summary, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    log.debug("Connecting to SMTP %s:%d", smtp_server, smtp_port)
    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(email_address, email_password)
        server.sendmail(email_address, email_address, msg.as_string())
    log.debug("Email delivered to %s.", email_address)


def send_error_notification(
    exc: Exception,
    smtp_server: str,
    smtp_port: int,
    email_address: str,
    email_password: str,
    label: str,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
) -> None:
    dt = datetime.now()
    date_fmt = dt.strftime("%B %d, %Y %H:%M")
    subject = f"[maildigest ERROR] {label} — {date_fmt}"

    window = ""
    if from_dt is not None and to_dt is not None:
        window = (
            f"Fetch window : {from_dt.strftime('%Y-%m-%d %H:%M:%S')}"
            f" → {to_dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )

    tb = _traceback.format_exc()
    body = (
        f"maildigest failed for mailbox: {label}\n"
        f"Time         : {date_fmt}\n"
        f"{window}"
        f"\nError: {type(exc).__name__}: {exc}\n"
        f"\nTraceback:\n{tb}"
    )

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = email_address
    msg["To"] = email_address
    msg.attach(MIMEText(body, "plain", "utf-8"))

    log.debug("Sending error notification to %s.", email_address)
    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(email_address, email_password)
        server.sendmail(email_address, email_address, msg.as_string())
    log.debug("Error notification delivered to %s.", email_address)
