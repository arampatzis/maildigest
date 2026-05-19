# 📬 maildigest

Multi-mailbox newsletter summariser powered by Claude AI.

Fetches emails from one or more configured IMAP folders, summarises them with
Claude, then delivers each digest to your inbox and saves a local markdown file.
Multiple mailboxes run on independent schedules (daily, weekdays, weekly, …) via
a long-lived daemon managed by systemd.

---

## ✅ Requirements

- Ubuntu Server (22.04+)
- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/settings/keys)

---

## 📦 Installation

### Option A — pipx (recommended for end users)

```bash
sudo apt install pipx
pipx install .          # from this directory
```

or use a specific `Python` version (e.g. `pyenv`):

```bash
pipx install . --python $(pyenv which python)
```

After this, `maildigest` is available as a regular command everywhere.

### Option B — Poetry (recommended for development)

```bash
poetry install
poetry run maildigest --help
```

### Shell completion

Add this to `~/.bashrc` to enable tab completion for all commands and flags:

```bash
eval "$(_MAILDIGEST_COMPLETE=bash_source maildigest)"
```

---

## ⚙️ Configuration

### 1. Create your config file

```bash
mkdir -p ~/.config/maildigest
cp config.example.yaml ~/.config/maildigest/config.yaml
```

Edit `~/.config/maildigest/config.yaml`. The following fields are **required** for
each mailbox — there are no defaults for them:


| Field            | Why required                                     |
| ---------------- | ------------------------------------------------ |
| `imap.folder`    | Folder names differ per account and server       |
| `schedule.days`  | Being explicit prevents silent misconfiguration  |
| `schedule.times` | The fire time is a deliberate operational choice |


A minimal mailbox looks like this:

```yaml
summary_dir: ~/Documents/NewsletterSummaries   # base dir for markdown files

mailboxes:
  - name: uoc_newsletters
    label: "UOC Newsletters"
    enabled: true

    imap:
      server: imap.uoc.gr
      email: user@uoc.gr
      folder: UOC-filtered.News        # required

    smtp:
      server: smtp.uoc.gr
      # digest is sent from/to imap.email (send-to-self)

    schedule:
      days: [mon, tue, wed, thu, fri]  # required — or: daily
      times: ["09:00", "17:00"]        # required — one or more HH:MM times

    summarizer:
      language: Greek
      focus_areas:
        - grant calls and funding deadlines
        - events and seminars
      extra_instructions: "Ignore promotions."
```

Optional fields and their defaults:


| Field                 | Default                     | Notes                                   |
| --------------------- | --------------------------- | --------------------------------------- |
| `imap.port`           | `993`                       | Standard IMAP SSL port                  |
| `smtp.port`           | `587`                       | Standard SMTP STARTTLS port             |
| `enabled`             | `true`                      | Set to `false` to skip without removing |
| `label`               | mailbox `name`              | Display name in logs and email subjects |
| `summarizer.language` | `"English"`                 | Language for Claude's output            |
| `body_char_limit`     | `3000`                      | Max characters read per email body      |
| `sender_filter`       | *(none)*                    | List of From substrings to filter on    |
| `summary_dir`         | global `summary_dir/<name>` | Per-mailbox override                    |


The config is validated on startup. A misconfigured file fails immediately with a
clear error:

```
Config file is invalid:
  mailboxes[0].imap.folder: Field required
  mailboxes[0].schedule.times: Field required
```

**Config file lookup order** (first match wins):

1. `$MAILDIGEST_CONFIG` environment variable
2. `./config.yaml` in the current directory
3. `~/.config/maildigest/config.yaml`

### 2. 🔐 Store secrets

Do **not** put passwords or API keys in the config file. Run this once after
filling in `config.yaml`:

```bash
maildigest config setup
```

You will be prompted for a **master password** first. On the first run you will
type it twice to confirm. Secrets are stored AES-encrypted on disk
(`~/.local/share/python_keyring/cryptfile_pass.cfg`) using
[keyrings.cryptfile](https://github.com/frispete/keyrings.cryptfile). The master
password is never written to disk — you type it once when starting the daemon, and
it lives in memory for the lifetime of the process.

You will then be prompted for:

- Your **Anthropic API key** (once, global)
- For each unique email account: **IMAP password** (and optionally a separate SMTP
password if your outgoing server uses different credentials)

> **Gmail accounts** require an [app password](https://myaccount.google.com/apppasswords),
> not your regular account password. `config setup` will remind you of this.

### 3. ✅ Verify credentials

After storing secrets, confirm that every server is reachable and every folder
exists:

```bash
maildigest config check
```

This tests, for each enabled mailbox:

1. **IMAP login** — connects to the IMAP server and authenticates (deduplicated
  per server so shared accounts are checked only once)
2. **SMTP login** — connects to the SMTP server and authenticates (deduplicated
  the same way)
3. **IMAP folder** — selects the configured folder to confirm it exists; skipped
  automatically if IMAP login failed for that account

Each check is shown in green (`OK`) or red (`FAILED: <reason>`).

---

## 🚀 Usage

### One-shot run

```bash
maildigest run
```

For each enabled mailbox scheduled for today:

1. Fetches emails from the configured IMAP folder since the last run
2. Summarises them with Claude
3. Saves a markdown file to `<summary_dir>/<name>/summary-YYYY-MM-DD.md`
4. Emails the digest to the mailbox's own address (subject includes date and time)

### List configured mailboxes

```bash
maildigest list
```

Shows each mailbox with its IMAP/SMTP settings, schedule, and last-run timestamp.

### Useful flags

```bash
# Preview without saving or emailing
maildigest run --dry-run

# Process only one mailbox by name
maildigest run --mailbox uoc_newsletters

# Catch up on a missed date range
maildigest run --from 2026-05-01 --to 2026-05-07

# Re-run today from midnight even if already up to date
maildigest run --force

# Combine flags
maildigest run --dry-run --mailbox uoc_newsletters --from 2026-05-06 --to 2026-05-06
```

### Debug mode

```bash
maildigest --debug run
```

Shows the config file in use, IMAP connection details, each email subject as it is
fetched, Claude API request size, and SMTP steps.

---

## 🕐 Scheduling

### How it works

The daemon is a long-lived process that reads `schedule.days` and `schedule.times`
from every enabled mailbox and fires each one at the configured times using
[APScheduler](https://apscheduler.readthedocs.io/). All mailboxes are handled by a
single process. When a job fires, the daemon fetches emails since the last run,
summarises them, and delivers the digest — exactly like `maildigest run`, but
triggered automatically on schedule.

Sending `SIGHUP` to the daemon causes it to re-read `config.yaml` and reschedule
all jobs, so changes to days or times take effect without a restart.

### First-time setup

```bash
# 1. Install the systemd user service (one-time)
maildigest service install

# 2. Start the daemon — you will be prompted for the keyring master password
maildigest service start

# 3. Verify it is running
maildigest service status
```

`service install` does three things in one step:

1. Writes `~/.config/systemd/user/maildigest.service`
2. Enables the service so it starts automatically (`systemctl --user enable`)
3. Enables **linger** (`loginctl enable-linger`) so the daemon survives logout and
  starts at every boot — even when no user is logged in

### Day-to-day management

```bash
maildigest service start     # start the daemon (prompts for master password)
maildigest service stop      # stop the daemon
maildigest service reload    # apply config changes without restarting (SIGHUP)
maildigest service status    # show running state, last log lines
maildigest service log       # show last 50 log lines
maildigest service log -f    # follow log output live
maildigest service log -n 100  # show last N lines
maildigest service install   # reinstall unit file after upgrading maildigest
maildigest service uninstall # stop, disable, and remove the unit file
```

### After a reboot

Because `service install` enables both the service and linger, the daemon's
systemd unit is armed to start at boot. However, the daemon needs the keyring
master password on startup and cannot proceed without it.

**What happens after a reboot:**

1. The system boots; the user's systemd instance starts (linger ensures this).
2. systemd attempts to start `maildigest.service`.
3. The service waits at the password prompt (`StandardInput=tty`).
4. You SSH into the server and run `maildigest service start` — this is enough to
  supply the password and let the already-waiting unit proceed.

In practice this means one manual step after each reboot: SSH in and type the
master password. This is an intentional trade-off — the password never touches
disk, so a rebooted machine without intervention cannot decrypt your secrets.

---

## 🧪 Running tests

```bash
poetry run pytest
```

