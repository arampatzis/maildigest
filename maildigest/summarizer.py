"""Claude-powered newsletter summarisation."""

import logging
from datetime import date

import anthropic

from maildigest.config import MailboxConfig

log = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-6"
_DEFAULT_MAX_TOKENS = 2048


def summarize_with_claude(
    emails: list[dict],
    mailbox: MailboxConfig,
    api_key: str,
    model: str = _DEFAULT_MODEL,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    target_date: date | None = None,
) -> str:
    """
    Summarise a list of emails using the Claude API.

    Parameters
    ----------
    emails : list[dict]
        Email dicts with ``'subject'``, ``'from'``, and ``'body'`` keys.
    mailbox : MailboxConfig
        Mailbox configuration (language, focus_areas, custom_prompt, etc.).
    api_key : str
        Anthropic API key.
    model : str, optional
        Claude model identifier.
    max_tokens : int, optional
        Maximum tokens in the response.
    target_date : date | None, optional
        Date the newsletters were received. Defaults to today when ``None``.

    Returns
    -------
    str
        Structured summary produced by Claude.
    """
    if not emails:
        d = target_date or date.today()
        return f"No newsletters received on {d.strftime('%B %d, %Y')}."

    prompt = _build_prompt(emails, mailbox, target_date)
    log.debug("Sending %d email(s) to Claude (%s) …", len(emails), model)
    log.debug("Prompt: %d chars, max_tokens: %d.", len(prompt), max_tokens)
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    result = message.content[0].text  # type: ignore[union-attr]
    log.debug("Claude responded with %d characters.", len(result))
    return result


def _build_prompt(
    emails: list[dict],
    mailbox: MailboxConfig,
    target_date: date | None = None,
) -> str:
    """
    Build the prompt string sent to Claude.

    When ``mailbox.custom_prompt`` is set it is used verbatim (with the raw
    email bodies appended). Otherwise a structured four-section prompt is
    assembled from ``mailbox.language``, ``mailbox.focus_areas``, and
    ``mailbox.extra_instructions``.
    """
    d = target_date or date.today()
    date_fmt = d.strftime("%A, %B %d, %Y")
    body = "\n".join(
        f"--- Email {i} ---\n"
        f"From: {em['from']}\n"
        f"Subject: {em['subject']}\n\n"
        f"{em['body']}"
        for i, em in enumerate(emails, 1)
    )

    if mailbox.custom_prompt:
        return f"{mailbox.custom_prompt}\n\n{body}"

    focus_section = ""
    if mailbox.focus_areas:
        items = "\n".join(f"- {area}" for area in mailbox.focus_areas)
        focus_section = f"\nPay special attention to:\n{items}\n"

    extra = (
        f"\nIMPORTANT: {mailbox.extra_instructions} "
        f"Do not mention excluded emails anywhere — not even to note their existence.\n"
        if mailbox.extra_instructions
        else ""
    )

    return (
        f"You are an assistant that summarises university newsletters.\n"
        f"Write your entire response in {mailbox.language}.\n"
        f"Use markdown formatting: ## for section headers, - for every list "
        f"item (one per line), **bold** for emphasis.\n"
        f"If an email is not relevant to university business (e.g. it is an "
        f"advertisement, spam, or promotion unrelated to the institution), "
        f"exclude it entirely — do not mention it in any section."
        f"{focus_section}"
        f"\nStructure your response with exactly these four sections:\n\n"
        f"## Grants & Funding\n"
        f"List every grant call, funding opportunity, scholarship, or financial "
        f"deadline mentioned. If none are present write 'None.'\n\n"
        f"## Overview\n"
        f"2-3 sentence summary of the day's newsletters.\n\n"
        f"## Announcements & Events\n"
        f"One bullet per announcement or event, including date/time when given.\n\n"
        f"## Action Items\n"
        f"One bullet item per action the reader should take, with any deadlines.\n\n"
        f"Keep the tone professional and concise.\n"
        f"{extra}\n"
        f"Below are {len(emails)} "
        f"{'newsletter' if len(emails) == 1 else 'newsletters'} "
        f"received on {date_fmt}:\n\n"
        f"{body}"
    )
