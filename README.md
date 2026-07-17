# sgv-tweet-digest

A self-hosted [Claude Code](https://claude.com/claude-code) skill that drafts a daily **dual-voice**
(fund + personal) crypto-Twitter digest from your X follow-graph and curated Lists, with an
engagement feedback loop that tunes tomorrow's drafts.

It is the open-source version of the internal tool [Social Graph Ventures](https://socialgraph.vc)
runs for [@socialgraphvc](https://x.com/socialgraphvc) (fund voice) and
[@0x_Mist](https://x.com/0x_Mist) (David Espinel's personal "Morning Mist" voice). You point it at
**your own** accounts, X Lists, and credentials.

## What you get

Each run produces, **per voice** (a fund voice and a personal voice):

- **3 top tweets to engage with** — the highest-signal posts from your follow-graph / Lists today,
  scored 0–18 on a transparent heuristic, with a one-line "why" and a suggested angle.
- **2 original tweet ideas** in that voice.
- **1 quote-tweet idea** off one of the surfaced posts.
- **Up to 3 hot non-crypto QTs** (AI / tech / VC) — if you category-tag extra Lists in config,
  the hottest non-crypto tweets get their own 🔥 block with a QT draft per voice.

Delivery is split by voice:

- **Fund voice → a Telegram group** (e.g. your team channel).
- **Personal voice → a Telegram bot DM** to you.

A SQLite **feedback loop** snapshots how your *actually-posted* tweets perform, learns which idea
archetypes land, and feeds do-more / do-less guidance into the next morning's drafting.

> Honest expectation-setting: output quality is only as good as your inputs. The signal comes from
> **curating tight X Lists** of accounts worth listening to (the shipped `vc-watchlist.json` and your
> `config.json` Lists are the steering wheel). A noisy follow-graph produces a noisy digest.

### Sample output (shape)

```
☀️ SGV Tweet Digest — 2026-06-03

— FUND VOICE (@socialgraphvc) —
Top to engage:
 1. [score 15] @someprotocol shipped X … → reply angle: …
 2. [score 13] @somefounder on Y … → …
 3. [score 12] …
Original ideas:
 • …
 • …
Quote-tweet:
 • QT @someprotocol: …

— PERSONAL VOICE (@0x_Mist) —
Top to engage: …
Original ideas: …
Quote-tweet: …
```

## How it works

A three-stage daily pipeline plus a feedback pass:

```
Stage A  gather_x.py            X Lists + following snapshot  ──▶ latest.json, state.json
Stage B  our_tweets_fetcher.py  your own tweets' live metrics ──▶ feedback.db
Stage C  digest.py              score 0–18  ▶ Opus picks top-3 + dual-voice ideas
                                ▶ Sonnet insights + good-content  ▶ deliver to Telegram
                                ▶ log stats.jsonl + feedback.db
feedback idea_matcher.py        links posted tweets ⇆ past drafted ideas (tweet_idea_matches)
         feedback_profile.py + steering_analyzer.py  ──▶ tunes the next run
```

- **Scoring** is a deterministic 0–18 heuristic over each candidate tweet (recency, author signal,
  engagement velocity, list membership, etc.) — no model spend to rank.
- **Drafting** uses Claude: Opus to pick the top tweets and write the per-voice ideas, Sonnet for the
  optional fund-internal "insights" pass and for turning links you've flagged into drafts.
- **Delivery** uses a Telethon **user session** to post the fund voice into a Telegram group, and a
  **bot** to DM you the personal voice.

## Prerequisites

- **[Claude Code](https://claude.com/claude-code)** installed.
- **Python 3.10+** and the ability to create a virtual environment.
- **Your own credentials** (you create these yourself — see the table below and
  [`SECRETS-TO-SHARE.md`](SECRETS-TO-SHARE.md) if you received a handoff doc):
  - An **Anthropic API key** (your own spend).
  - An **X / Twitter developer app**: an app **Bearer token** plus an **OAuth2** client authorized
    against **your own** `@handle` (app-only auth 403s on reading Lists / following, so a user token
    is required).
  - **Telegram**: an `api_id` / `api_hash` from [my.telegram.org](https://my.telegram.org), a one-time
    **Telethon user session** login, and a **bot** from [@BotFather](https://t.me/BotFather).
- **Optional** (the fund-internal insights pass; each is skipped if unconfigured): a **Notion**
  integration + deals database, a **Gmail** account reachable via the `gog` CLI, and a **Fireflies**
  transcript cache. The skill runs fine with all three off.

## Install

Install it as a Claude Code skill by cloning into your skills directory:

```bash
git clone <REPO_URL> ~/.claude/skills/sgv-tweet-digest

# Create + activate a virtual environment for the Python deps:
python3 -m venv ~/.local/share/sgv-tweet-digest/venv
source ~/.local/share/sgv-tweet-digest/venv/bin/activate
pip install -r ~/.claude/skills/sgv-tweet-digest/requirements.txt

# Copy the templates to your own (gitignored) copies:
cd ~/.claude/skills/sgv-tweet-digest
cp .env.example          .env            # then fill in the canonical env vars (table below)
cp config.example.json   config.json     # then set your X user ids, Lists, model ids, cost cap
```

Restart Claude Code only if `~/.claude/skills/` is brand new (so it picks up the new skills dir).

`requirements.txt` installs into your **venv / `--user`**, never globally:
`anthropic`, `telethon==1.43.1`, `trafilatura`. A couple of externals are **not** pip-installed and
are noted there as comments: the `yt-dlp` binary (for YouTube `gc:` links) and the optional `gog`
Gmail CLI. The minimal Notion client is **vendored** in `scripts/notion_client.py` — no extra install.

### One-time logins

- **Telethon session:** run the bootstrap once to log in with your phone number + code; it writes the
  session file at `TELEGRAM_SESSION`. (See `scripts/telegram/auth.py` and the repo's setup notes.)
- **X OAuth2 token:** run the OAuth bootstrap once to authorize your app against your `@handle` and
  mint the refresh token:
  ```bash
  X_OAUTH2_CLIENT_ID=... X_OAUTH2_CLIENT_SECRET=... python3 scripts/oauth_bootstrap.py
  ```
  (`X_OAUTH2_CLIENT_SECRET` only for confidential clients.) It opens a browser, captures the code on
  a localhost callback — `X_OAUTH2_REDIRECT`, default `http://localhost:8723/callback`, which must be
  registered on the X app — and writes the token blob to `X_OAUTH2_TOKEN_FILE` (default
  `${XDG_DATA_HOME:-$HOME/.local/share}/sgv-tweet-digest/twitter_oauth2.json`). `gather_x.py` then
  auto-refreshes that token on each run.

## Configure

Two files, both gitignored, both copied from the shipped templates:

1. **`.env`** (from `.env.example`) — your secrets and paths. Use the **canonical env-var names**
   below and only those.
2. **`config.json`** (from `config.example.json`) — non-secret operational knobs: your personal +
   fund X **user ids**, the **followed List ids** you want monitored, a membership allowlist, the
   Opus/Sonnet **model ids**, the **daily X cost cap**, and scoring thresholds. The shipped example
   has placeholder ids and 1–2 example Lists — replace them with yours.

Two reference files also ship: `voice_profiles.json` (the two tuned voice definitions — read at
runtime, rebuild yours with `scripts/build_voice_profiles.py`) and `vc-watchlist.json` (~300+ public
crypto-VC / KOL X handles with tier labels — **reference data only**: no script currently reads it;
it's a curation aid for building your own X Lists).

## Use

In Claude Code, run the skill explicitly with **`/sgv-tweet-digest`**. Because it has real side
effects (it sends Telegram messages and spends Anthropic + X budget), it is marked
`disable-model-invocation: true` — Claude will **not** auto-fire it from a passing mention. Phrases
like these are recognized as cues to *suggest* the skill, but you still confirm the run:

- "run the tweet digest"
- "draft my morning tweets"
- "sgv digest"

## Optional sources (fund-internal insights pass)

`scripts/insights.py` can enrich the drafts with a fund-internal context pass. Each source activates
**only** when its inputs are present, and is otherwise skipped with a logged note — the skill never
hard-fails on a missing source:

| Source | Enable by setting | Notes |
| --- | --- | --- |
| **Notion** deal funnel | `NOTION_TOKEN` **and** `NOTION_DEALS_DB_ID` | Queried via the vendored `scripts/notion_client.py`. |
| **Gmail** weekly memos | `GMAIL_ACCOUNT` **and** `SGV_ADMIN_EMAIL` (plus the `gog` CLI on PATH) | Searches a sender's weekly memos. |
| **Fireflies** transcripts | *(no env var)* — drop a transcript cache where the script looks | Activates purely on cache presence. |

Whatever these sources surface is run through an **anonymization denylist** before drafting. The repo
ships only `anonymize_denylist.example.txt` (invented placeholder names). If you operate a real fund,
you **must** create your real `anonymize_denylist.txt` (state dir or `SGV_ANONYMIZE_DENYLIST`) — it is
**gitignored** and loaded in preference to the example. As a safety gate, `insights.py` **refuses to
run the internal sources** while only the example denylist is present, so real portfolio/founder names
can never reach a draft unscrubbed. If you are not using the internal sources, leave them off and
ignore this section.

## Scheduling

To run the pipeline daily, use the systemd **user-unit templates** in
[`deploy/`](deploy/README.md). That directory has a full install guide and the UTC schedule:

| Time (UTC) | Unit | Stage |
| --- | --- | --- |
| 12:30 | `sgv-our-tweets-fetcher.timer` | B — own-tweet metrics |
| 12:40 | `sgv-idea-matcher.timer` | feedback stage 1 — match posted tweets to past ideas |
| 12:45 | `sgv-owned-reads-gather.timer` | A — Lists + following |
| 13:00 | `sgv-tweet-digest.timer` | C — score, draft, deliver |
| 13:30 | `sgv-feedback-profile.timer` | feedback stage 2 — profile + steering rollup |
| every ~5 min | `sgv-good-content-poll` | `gc:` link ingester (single pass, restart-looped) |

By design, two Python interpreters are used: **system Python** for the stdlib-only Stage A gather,
and the **venv Python** (Telethon + Anthropic) for the digest and all Telegram I/O. The deploy README
explains the two path variables (`VENV_PYTHON`, `SYSTEM_PYTHON`) you set.

## Environment variable reference

The canonical names. Every script, the `.env.example`, and the deploy templates use exactly these.

| Var | Required? | Secret? | What it's for |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | yes | secret | Opus + Sonnet calls (scoring picks, drafting, insights, feedback). |
| `X_BEARER_TOKEN` | yes | secret | X API v2 app-only auth: list members/tweets, following, single-tweet fetch. |
| `X_OAUTH2_CLIENT_ID` | yes | secret | OAuth2 user-token refresh client id (your own X app). |
| `X_OAUTH2_CLIENT_SECRET` | conditional | secret | OAuth2 Basic-auth secret (confidential clients; optional for public clients). |
| `X_OAUTH2_REFRESH_TOKEN` | yes | secret | Mints user tokens to read your Lists / following / own timeline. |
| `X_OAUTH2_TOKEN_FILE` | no | not secret | Where the refreshed OAuth2 token blob is stored (seeded from the vars above). |
| `TELEGRAM_API_ID` | yes | secret | Telethon application id (my.telegram.org). |
| `TELEGRAM_API_HASH` | yes | secret | Telethon application hash (my.telegram.org). |
| `TELEGRAM_SESSION` | yes | secret | Path to your Telethon user session file. **Never shared.** |
| `TELEGRAM_BOT_TOKEN` | yes | secret | BotFather token for personal-voice DM delivery; the bot handle is derived from it. |
| `TELEGRAM_DELIVERY_CHAT_SGV` | yes | not secret | Telegram group id for fund-voice delivery. |
| `TELEGRAM_DELIVERY_CHAT_MIST` | yes | not secret | Telegram user/DM id for personal-voice delivery. |
| `SHOAL_CHANNEL` | no | not secret | Telegram news channel/username the digest reads for context. |
| `SGV_FEEDBACK_DB` | no | not secret | Path to the SQLite `feedback.db` (defaults under XDG state). |
| `VENV_PYTHON` | yes (for scheduling) | not secret | Telethon-capable Python interpreter for the digest + Telegram I/O. **Absolute path** — systemd does not expand `$HOME`. |
| `SKILL_DIR` | yes (for scheduling) | not secret | Absolute path to the installed skill repo (used by the `deploy/` units' `ExecStart`). |
| `NOTION_TOKEN` | optional | secret | Enables the Notion deal-funnel insights source. |
| `NOTION_DEALS_DB_ID` | optional | not secret | Which Notion database insights queries (needed only if `NOTION_TOKEN` is set). |
| `FIREFLIES_API_KEY` | optional | secret | Enables the call-drafts lane: anonymized personal-voice tweets from your recent Fireflies calls. |
| `GMAIL_ACCOUNT` | optional | not secret | Account the `gog` CLI reads for weekly memos (insights). |
| `SGV_ADMIN_EMAIL` | optional | not secret | Sender filter for the weekly-memo Gmail search (needed only if `GMAIL_ACCOUNT` is set). |

State and runtime files (`feedback.db`, `latest.json`, `state.json`, `stats.jsonl`, the refreshed
OAuth token, your `config.json` and `.env`) live **outside** the skill directory under
`${XDG_DATA_HOME:-$HOME/.local/share}/sgv-tweet-digest/` and are all gitignored.

## Updating

```bash
git -C ~/.claude/skills/sgv-tweet-digest pull
# Re-install deps if requirements.txt changed:
~/.local/share/sgv-tweet-digest/venv/bin/pip install -r ~/.claude/skills/sgv-tweet-digest/requirements.txt
```

Your `.env`, `config.json`, and `anonymize_denylist.txt` are gitignored, so a pull never clobbers
them. The skill follows semantic-ish versioning in `SKILL.md` frontmatter; check the diff on bumps.

## Security

- **This skill runs code on your machine, sends Telegram messages, and spends API budget.** Read
  `SKILL.md` and everything under `scripts/` before you trust it.
- **Secrets live in environment variables only** (via your gitignored `.env`). No token, session, or
  key is hardcoded anywhere in this repo; `.gitignore` blocks every secret and per-user state file
  (`.env`, `*.session`, `*.token`, `.api_key`, `feedback.db`, your real `config.json`, your real
  `anonymize_denylist.txt`, and more) from ever being committed.
- It talks to **your own** accounts and Lists — nothing is shared with the project authors.
- Licensed **MIT** (see [`LICENSE`](LICENSE)).
