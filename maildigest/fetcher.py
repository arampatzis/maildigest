"""IMAP email fetching."""

import email
import email.message
import imaplib
import logging
import re
from datetime import datetime, timedelta
from email.header import decode_header, make_header

log = logging.getLogger(__name__)


def fetch_emails(
    imap_server: str,
    imap_port: int,
    email_address: str,
    email_password: str,
    mail_folder: str,
    from_dt: datetime,
    to_dt: datetime,
    body_char_limit: int = 3000,
    sender_filter: list[str] | None = None,
) -> list[dict]:
    """
    Fetch emails with INTERNALDATE in [from_dt, to_dt] from an IMAP folder.

    Uses SINCE/BEFORE to select the relevant calendar days from the server,
    then filters each message by its INTERNALDATE for exact datetime boundaries.

    Parameters
    ----------
    imap_server : str
        Hostname of the IMAP server.
    imap_port : int
        Port for the IMAP SSL connection.
    email_address : str
        Full email address used for login.
    email_password : str
        Password or app-specific password for the account.
    mail_folder : str
        IMAP folder to read from.
    from_dt : datetime
        Start of the fetch window (inclusive).
    to_dt : datetime
        End of the fetch window (inclusive).
    body_char_limit : int, optional
        Maximum characters to keep per email body, by default 3000.
    sender_filter : list[str] | None, optional
        When provided, only emails whose From field contains at least one of
        these strings are returned (case-insensitive substring match).

    Returns
    -------
    list[dict]
        Each dict has keys ``'subject'``, ``'from'``, and ``'body'``.

    Raises
    ------
    ValueError
        If the requested IMAP folder cannot be opened.
    """
    log.info(
        "Fetching emails from %s folder '%s': %s → %s",
        imap_server, mail_folder, from_dt, to_dt,
    )
    log.debug("Connecting to %s:%d as %s", imap_server, imap_port, email_address)
    conn = imaplib.IMAP4_SSL(imap_server, imap_port)
    conn.login(email_address, email_password)
    try:
        log.debug("Selecting folder '%s'", mail_folder)
        status, _ = conn.select(f'"{mail_folder}"')
        if status != "OK":
            raise ValueError(
                f"Could not open folder '{mail_folder}'. "
                "Check the folder name in your config.yaml."
            )

        since_str = from_dt.date().strftime("%d-%b-%Y")
        before_str = (to_dt.date() + timedelta(days=1)).strftime("%d-%b-%Y")
        _, message_ids = conn.search(None, f"(SINCE {since_str} BEFORE {before_str})")

        ids = message_ids[0].split()
        log.info("Found %d candidate(s) in '%s'.", len(ids), mail_folder)

        emails = []
        for i, msg_id in enumerate(ids, 1):
            log.debug("Fetching message %d of %d (id=%s)", i, len(ids), msg_id)
            _, msg_data = conn.fetch(msg_id, "(INTERNALDATE RFC822)")
            header_bytes = msg_data[0][0]
            raw = msg_data[0][1]

            received = _parse_internaldate(header_bytes)
            if received is not None and not (from_dt <= received <= to_dt):
                log.debug(
                    "  Skipping — INTERNALDATE %s outside [%s, %s].",
                    received, from_dt, to_dt,
                )
                continue

            msg = email.message_from_bytes(raw)
            from_val = _decode_header_value(msg.get("From", "Unknown"))

            if sender_filter and not _matches_filter(from_val, sender_filter):
                log.debug("  Skipping — sender '%s' not in filter.", from_val)
                continue

            subject = _decode_header_value(msg.get("Subject", "(No subject)"))
            log.debug("  Subject: %s", subject)
            emails.append({
                "subject": subject,
                "from": from_val,
                "body": _extract_plain_text(msg)[:body_char_limit],
            })
    finally:
        conn.logout()
    return emails


def _parse_internaldate(data: bytes | None) -> datetime | None:
    """Parse INTERNALDATE from an IMAP fetch response header; returns local naive datetime."""
    if data is None:
        return None
    match = re.search(rb'INTERNALDATE "([^"]+)"', data, re.IGNORECASE)
    if not match:
        return None
    try:
        dt = datetime.strptime(match.group(1).decode(), "%d-%b-%Y %H:%M:%S %z")
        return dt.astimezone().replace(tzinfo=None)
    except Exception:
        return None


def _matches_filter(from_val: str, sender_filter: list[str]) -> bool:
    lower = from_val.lower()
    return any(f.lower() in lower for f in sender_filter)


def _decode_header_value(raw: str) -> str:
    return str(make_header(decode_header(raw)))


def _extract_plain_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            disposition = str(part.get("Content-Disposition", ""))
            if (
                part.get_content_type() == "text/plain"
                and "attachment" not in disposition
            ):
                return _decode_payload(part)
        return ""
    return _decode_payload(msg)


def _decode_payload(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")
