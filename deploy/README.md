# Deploy — systemd user templates

These are **systemd user-unit templates** for running `sgv-tweet-digest` on a schedule on a Linux
host (a VPS, a home server, anything with `systemd --user`). They are templates: you fill in two
path variables and an `EnvironmentFile`, then enable them. Nothing here hardcodes a user or an
absolute path.

> macOS note: `systemd --user` is Linux-only. On macOS, schedule the same scripts with `launchd`
> or `cron`; the schedule table and the run commands below still apply.

## What each unit does

| Unit | Stage | What it runs |
| --- | --- | --- |
| `sgv-our-tweets-fetcher.{service,timer}` | B | `scripts/our_tweets_fetcher.py --budget 0.20` — snapshot our own tweets' metrics into `feedback.db` |
| `sgv-idea-matcher.{service,timer}` | feedback 1 | `scripts/idea_matcher.py` — link posted tweets to drafted ideas (writes `tweet_idea_matches`) |
| `sgv-owned-reads-gather.{service,timer}` | A | `scripts/gather_x.py` — gather X Lists tweets + following snapshot under the daily cost cap |
| `sgv-tweet-digest.{service,timer}` | C | `scripts/digest.py` — score, draft the dual-voice digest, deliver to Telegram |
| `sgv-feedback-profile.{service,timer}` | feedback 2 | `scripts/feedback_profile.py` then `scripts/steering_analyzer.py` — tune tomorrow's drafts |
| `sgv-good-content-poll.{service,timer}` | poller | `scripts/good_content_poll.py` — ingest `gc: <url>` links you flag in Telegram |

## Schedule (all times UTC)

| Time (UTC) | Unit | Why this order |
| --- | --- | --- |
| 12:30 | `sgv-our-tweets-fetcher.timer` | Stage B first: snapshot own-tweet metrics before drafting |
| 12:40 | `sgv-idea-matcher.timer` | Feedback stage 1: match freshly-snapshotted tweets to past ideas |
| 12:45 | `sgv-owned-reads-gather.timer` | Stage A: gather the follow-graph / Lists, 15 min before the digest |
| 13:00 | `sgv-tweet-digest.timer` | Stage C: the main digest (9am ET) — reads what A and B produced |
| 13:30 | `sgv-feedback-profile.timer` | Feedback stage 2: after the digest, reads the matches from 12:40 |
| every ~5 min | `sgv-good-content-poll` | Single-pass poller re-run by `Restart=always`; the timer is an opt-in alternative |

The two gather stages run *before* the digest on purpose, so `digest.py` reads fresh `latest.json`
(Stage A) and fresh own-tweet metrics in `feedback.db` (Stage B).

## Two Python interpreters — by design

This is intentional, not a mistake:

- **`gather_x.py` (Stage A) is stdlib-only** and can run on your **system Python**. The
  `sgv-owned-reads-gather.service` template uses `${SYSTEM_PYTHON}` (point it at `/usr/bin/python3`,
  or at `${VENV_PYTHON}` if you'd rather — both work).
- **Everything that touches Telegram or Anthropic** (`digest.py`, `our_tweets_fetcher.py`,
  `idea_matcher.py`, `feedback_profile.py`, `steering_analyzer.py`, `good_content_poll.py`) runs on
  the **venv Python** that has `telethon` + `anthropic` installed. Those templates use `$VENV_PYTHON`.

So you will set both `SYSTEM_PYTHON` and `VENV_PYTHON` in your env file.

## One-time setup

### 1. Install the skill and its venv

Follow the repo `README.md` (clone into `~/.claude/skills/sgv-tweet-digest`, create a venv,
`pip install -r requirements.txt`). Note the absolute paths to the skill dir and the venv python.

### 2. Create the EnvironmentFile

Every unit reads `EnvironmentFile=%h/.config/sgv-tweet-digest/env` (where `%h` is your home dir).
Create it from the repo's `.env.example` and add the two path variables the templates need:

```bash
mkdir -p ~/.config/sgv-tweet-digest ~/.local/state/sgv-tweet-digest
cp ~/.claude/skills/sgv-tweet-digest/.env.example ~/.config/sgv-tweet-digest/env
$EDITOR ~/.config/sgv-tweet-digest/env
```

In that file, alongside the canonical secret/config vars from `.env.example`, set:

```ini
# Absolute path to the installed skill (no ~ — systemd does not expand it; use the full path)
SKILL_DIR=/home/youruser/.claude/skills/sgv-tweet-digest
# Telethon-capable venv interpreter (telethon + anthropic installed)
VENV_PYTHON=/home/youruser/.local/share/sgv-tweet-digest/venv/bin/python3
# System interpreter for the stdlib-only gather stage (or set it to VENV_PYTHON)
SYSTEM_PYTHON=/usr/bin/python3
```

> `EnvironmentFile` values are literal — systemd does **not** expand `$HOME`, `~`, or any
> `$VARIABLE` inside them. Use absolute paths, unquoted. Inside the unit files, `%h` expands to
> your home directory (used for the EnvironmentFile and log paths). The `ExecStart` lines invoke
> `/bin/bash -c 'exec "$VENV_PYTHON" "$SKILL_DIR/..."'` because systemd refuses an environment
> variable as the executable itself — bash expands `$SKILL_DIR` / `$VENV_PYTHON` /
> `$SYSTEM_PYTHON` from the EnvironmentFile at runtime.

### 3. Create the log + state directory

The templates append logs to `%h/.local/state/sgv-tweet-digest/`. The `mkdir -p` above created it.
(`gather_x.py`, `feedback_db.py`, and the digest also keep their runtime state under
`${XDG_DATA_HOME:-$HOME/.local/share}/sgv-tweet-digest/` per the canonical path env vars.)

### 4. Install and enable the units

```bash
cp ~/.claude/skills/sgv-tweet-digest/deploy/*.service ~/.config/systemd/user/
cp ~/.claude/skills/sgv-tweet-digest/deploy/*.timer   ~/.config/systemd/user/
systemctl --user daemon-reload

# Enable the five scheduled timers (NOT the .service units — the timers pull them in):
systemctl --user enable --now sgv-our-tweets-fetcher.timer
systemctl --user enable --now sgv-idea-matcher.timer
systemctl --user enable --now sgv-owned-reads-gather.timer
systemctl --user enable --now sgv-tweet-digest.timer
systemctl --user enable --now sgv-feedback-profile.timer

# Enable the good-content poller as a service (single pass, re-run every ~5 min
# by Restart=always — see modes below):
systemctl --user enable --now sgv-good-content-poll.service
```

If you want timers to fire while you're logged out, enable lingering once:
`sudo loginctl enable-linger "$USER"`.

### Good-content poller: two modes

- **Restart-loop (default):** enable `sgv-good-content-poll.service`. The poller does a **single
  pass and exits**; `Restart=always` + `RestartSec=300` re-runs it every ~5 minutes, ingesting
  `gc: <url>` messages from your Saved Messages and bot DM.
- **Timer (alternative):** if you'd rather drive the cadence with a timer, edit
  `sgv-good-content-poll.service` to set `Type=oneshot` and remove the `Restart=` lines, do **not**
  enable that service directly, and instead enable `sgv-good-content-poll.timer` (every ~10 min).
  Pick one mode, not both.

## Verify

```bash
systemctl --user list-timers            # confirm the next run times line up with the table above
systemctl --user status sgv-tweet-digest.service
journalctl --user -u sgv-tweet-digest.service -n 50
tail -f ~/.local/state/sgv-tweet-digest/digest.log

# Run a stage on demand (does not wait for the timer):
systemctl --user start sgv-tweet-digest.service
```

## Notes

- These units send real Telegram messages and spend Anthropic + X API budget on each digest run.
  Test by running `scripts/digest.py` manually first (see the repo `README.md`).
- The digest depends on Stage A's output; the unit declares `After=sgv-owned-reads-gather.service`
  so a same-boot catch-up run orders correctly, but day-to-day ordering comes from the timers.
- All `*.timer` units use `OnCalendar=... UTC` and the services set `Environment=TZ=UTC`, so the
  schedule is stable regardless of the host's local timezone.
