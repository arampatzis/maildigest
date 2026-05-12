# 📬 maildigest

[![CI](https://github.com/YOUR_USERNAME/maildigest/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR_USERNAME/maildigest/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/YOUR_USERNAME/maildigest/branch/main/graph/badge.svg)](https://codecov.io/gh/YOUR_USERNAME/maildigest)

Multi-mailbox newsletter summariser powered by Claude AI.

Fetches emails from one or more configured IMAP folders, summarises them with
Claude, then delivers each digest to your inbox and saves a local markdown file.
Multiple mailboxes can run on independent schedules (daily, weekdays, weekly, …).

---

## ✅ Requirements

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/settings/keys)
- macOS (scheduling relies on launchd; IMAP/SMTP works on any OS)

---

## 📦 Installation

### Option A — pipx (recommended for end users)

[pipx](https://pipx.pypa.io) installs CLI tools in isolated environments so
they never conflict with your other Python projects.

```bash
brew install pipx
pipx install .          # from this directory
```

After this, `maildigest` is available as a regular command everywhere.

### Option B — Poetry (recommended for development)

```bash
brew install poetry
poetry install
poetry run maildigest run   # or just `maildigest run` after activating the venv
```

---

## ⚙️ Configuration

### 1. Create your config file

```bash
mkdir -p ~/.config/maildigest
cp config.yaml.example ~/.config/maildigest/config.yaml
```

Edit `~/.config/maildigest/config.yaml` with your settings. The file has two
top-level sections:

**Global settings**

```yaml
summary_dir: ~/Documents/NewsletterSummaries   # base directory for markdown files
```

Each mailbox gets its own subdirectory: `<summary_dir>/<mailbox_name>/`.

**Per-mailbox settings**

```yaml
mailboxes:
  - name: uoc_newsletters          # machine name — used in keychain and filenames
    label: "UOC Newsletters"       # display name in logs and email subjects
    enabled: true

    imap:
      server: imap.uoc.gr
      port: 993                    # default: 993
      email: user@uoc.gr
      folder: Newsletters          # IMAP folder to read from

    smtp:
      server: smtp.uoc.gr
      port: 587                    # default: 587
      # The digest email is sent from/to imap.email (send-to-self).

    schedule:
      days: [mon, tue, wed, thu, fri]   # or: daily

    summarizer:
      language: Greek              # language for the Claude output
      focus_areas:                 # Claude pays special attention to these
        - grant calls and funding deadlines
        - events and seminars
      extra_instructions: "Ignore promotions."
      # custom_prompt: |           # full prompt override for power users
      #   Summarise in three bullet points only.

    # Optional: only process emails from these senders (substring match)
    # sender_filter:
    #   - "@uoc.gr"

    # Optional: per-mailbox character limit for email bodies (default 3000)
    # body_char_limit: 3000
```

See [config.yaml](config.yaml) for a complete annotated template.

**Config file lookup order** (first match wins):

1. `$MAILDIGEST_CONFIG` environment variable
2. `./config.yaml` in the current directory (useful with `poetry run`)
3. `~/.config/maildigest/config.yaml`

### 2. 🔐 Store secrets in the system keychain

Do **not** put passwords or API keys in the config file. Run this once after
creating your `config.yaml`:

```bash
maildigest setup-credentials
```

You will be prompted for:

- Your **Anthropic API key** (once, global)
- For **each mailbox**: the IMAP password (and optionally a separate SMTP
password if your outgoing server uses different credentials)

All secrets are stored in the **macOS Passwords app** (formerly Keychain Access) —
encrypted on disk, protected by your login password, never visible in plain text.

---

## 🚀 Usage

### Run a digest

```bash
maildigest run
```

For each enabled mailbox that is scheduled for today:

1. Fetches emails from the configured IMAP folder
2. Sends them to Claude for summarisation
3. Saves a markdown file to `<summary_dir>/<name>/newsletter-summary-YYYY-MM-DD.md`
4. Emails the summary to the mailbox's own address

### List configured mailboxes

```bash
maildigest list
```

Shows each mailbox with its IMAP/SMTP settings, schedule, and last-run date.

### Useful flags

```bash
# Preview without saving or emailing
maildigest run --dry-run

# Process only one mailbox by name
maildigest run --mailbox uoc_newsletters

# Catch up on a missed date range
maildigest run --from 2026-05-01 --to 2026-05-07

# Re-run today even if already up to date
maildigest run --force

# Combine: dry-run a single mailbox for a past date
maildigest run --dry-run --mailbox uoc_newsletters --from 2026-05-06 --to 2026-05-06
```

### Debug mode

Add `--debug` before the subcommand to see detailed logging:

```bash
maildigest --debug run
```

Debug mode shows the config file in use, IMAP connection details, each email
subject as it is fetched, the Claude API request size, and SMTP steps.

---

## 🕐 Scheduling (automatic daily runs)

macOS uses **launchd** to run scheduled tasks. `maildigest install` generates a
`.plist` file, places it in `~/Library/LaunchAgents/`, and registers it with
launchd. From that point on, macOS runs `maildigest run` every day at the time
you choose.

Per-mailbox schedules (e.g. `days: [mon]` for a weekly digest) are enforced by
the app itself at runtime — you only need one launchd entry regardless of how
many mailboxes you have.

```bash
# Install with the default schedule (09:00 every day)
maildigest install

# Or choose a different time (24-hour format)
maildigest install --time 13:40
```

To stop automatic runs:

```bash
maildigest uninstall
```

### 📋 Logs

When running under launchd, all output is captured in two files:

| File | Contents |
|------|----------|
| `~/Library/Logs/maildigest/error.log` | Log messages (fetch progress, Claude API calls, email delivery, errors) |
| `~/Library/Logs/maildigest/output.log` | Printed summaries |

Check the last run:

```bash
cat ~/Library/Logs/maildigest/error.log
```

Watch it live while testing a manual run:

```bash
tail -f ~/Library/Logs/maildigest/error.log
```

If something went wrong, `error.log` is the first place to look — it will show which step failed and why.

---

## 🧪 Running tests

```bash
poetry run pytest
```

---

## 🗂️ Project layout

```
maildigest/               Python package
├── config.py             YAML loader, MailboxConfig dataclass, keychain helpers
├── fetcher.py            IMAP email retrieval with optional sender filter
├── summarizer.py         Claude API integration, structured prompt builder
├── notifier.py           Markdown file and email delivery
└── cli.py                CLI entry point (run, list, setup-credentials,
                          install, uninstall)

tests/
├── test_cli.py           install / uninstall / plist generation
├── test_config.py        YAML parsing, schedule logic, last_run helpers
├── test_fetcher.py       IMAP fetching, header decoding, sender filter
├── test_summarizer.py    Prompt construction, Claude API call
└── test_notifier.py      Markdown file creation, email delivery

config.yaml.example       Annotated config template (no secrets)
pyproject.toml            Poetry / pipx project config
```
