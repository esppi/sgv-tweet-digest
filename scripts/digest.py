#!/usr/bin/env python3
"""SGV Tweet Digest — Claude Opus direct (dual-voice).

Runs the full daily pipeline:
  1. Load owned-reads latest.json + Shoal-style news (via Telethon $VENV_PYTHON)
  2. Heuristic pre-filter + scoring (0-18) → top 50 candidate tweets
  3. Claude Opus picks final top 3 + writes dual-voice tweet ideas
     (fund voice + personal voice, each: 2 originals + 1 quote-tweet)
  4. Sonnet SGV INSIGHTS pass (insights.py) + good-content drafts (good_content.py)
  5. Delivery: fund-voice content → Telegram group; personal-voice content → bot DM
  6. Append stats.jsonl + persist ideas to feedback.db

Invocation:
  python3 digest.py              # full run, delivers to Telegram
  python3 digest.py --dry-run    # echo messages to stdout, no send, no stats write

Dependencies:
  - anthropic SDK (install into the Telethon-capable venv; see requirements.txt)
  - ANTHROPIC_API_KEY in the environment
  - send_message.sh + telegram/read.py (bundled under scripts/)
  - $VENV_PYTHON pointing at the Telethon-capable interpreter (for Telegram I/O)

Config / secrets are read from the environment and the operator's config.json;
nothing is hardcoded to a specific home, host, account, chat, or bot.
"""

import argparse, json, os, re, subprocess, sys, time, datetime, tempfile
from collections import Counter, defaultdict

# Make sibling modules (feedback_db.py, insights.py, good_content.py) importable
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import anthropic
except ImportError:
    print("FATAL: anthropic SDK not available. Run with the Telethon-capable venv:", file=sys.stderr)
    print('  "$VENV_PYTHON" digest.py    (after: pip install -r requirements.txt)', file=sys.stderr)
    sys.exit(1)


# ─────────────────────────────────────────
# PATHS / CONFIG (env-derived, no hardcoded home/host/account)
# ─────────────────────────────────────────
def _skill_dir():
    """Root of the installed skill (where config.example.json + JSON seeds live)."""
    env = os.environ.get("CLAUDE_SKILL_DIR")
    if env:
        return env
    return os.path.dirname(HERE)  # scripts/ lives under the skill root


def _state_dir():
    """Per-user writable state dir: ${XDG_DATA_HOME:-$HOME/.local/share}/sgv-tweet-digest."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(base, "sgv-tweet-digest")


def _voice_profiles_path():
    """Resolve voice_profiles.json: $SGV_VOICE_PROFILES -> <state>/ -> shipped seed."""
    for p in (
        _env_path("SGV_VOICE_PROFILES", None),
        os.path.join(_state_dir(), "voice_profiles.json"),
        os.path.join(_skill_dir(), "voice_profiles.json"),
    ):
        if p and os.path.exists(p):
            return p
    return os.path.join(_skill_dir(), "voice_profiles.json")


def _env_path(name, default):
    """Env-sourced path with $VAR/~ expansion (systemd EnvironmentFile does not
    expand $HOME — a literal '$HOME/...' value must still resolve here)."""
    v = os.environ.get(name)
    if not v:
        return default
    return os.path.expanduser(os.path.expandvars(v))


def _load_app_config():
    """Load the operator's config.json — the ONE canonical chain shared by every
    loader: $SGV_CONFIG -> $XDG_CONFIG_HOME/sgv-tweet-digest/config.json ->
    <skill_dir>/config.json (the README quickstart copy) -> the shipped example."""
    cfg_base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    for p in (
        _env_path("SGV_CONFIG", None),
        os.path.join(cfg_base, "sgv-tweet-digest", "config.json"),
        os.path.join(_skill_dir(), "config.json"),
        os.path.join(_skill_dir(), "config.example.json"),
    ):
        if p and os.path.exists(p):
            try:
                with open(p) as f:
                    return json.load(f)
            except Exception:
                continue
    return {}


CONFIG = _load_app_config()
_MODELS = CONFIG.get("models", {}) if isinstance(CONFIG, dict) else {}
_THRESHOLDS = CONFIG.get("thresholds", {}) if isinstance(CONFIG, dict) else {}

STATE_DIR = _state_dir()
SKILL_DIR = _skill_dir()
STATS_PATH = _env_path("SGV_STATS_PATH", os.path.join(STATE_DIR, "stats.jsonl"))

# Bundled helpers — under the skill's scripts/ dir (this file's dir).
SEND_SCRIPT = _env_path("SGV_SEND_SCRIPT", os.path.join(HERE, "send_message.sh"))
TELEGRAM_READ_PY = _env_path(
    "SGV_TELEGRAM_READ", os.path.join(HERE, "telegram", "read.py")
)

# Telethon-capable interpreter (Telegram I/O). Defaults to python3 on PATH.
VENV_PY = _env_path("VENV_PYTHON", "python3")

# owned-reads gatherer output (written by gather_x.py into the state dir).
OWNED_READS_PATH = _env_path(
    "SGV_OWNED_READS", os.path.join(STATE_DIR, "latest.json")
)

# Delivery targets — non-secret ids, parametrized (no hardcoded chat/user ids).
DELIVERY_CHAT_SGV = os.environ.get("TELEGRAM_DELIVERY_CHAT_SGV", "")   # fund voice → group
DELIVERY_CHAT_MIST = os.environ.get("TELEGRAM_DELIVERY_CHAT_MIST", "")  # personal voice → bot DM
DELIVERY_CHAT = DELIVERY_CHAT_SGV  # alias for error/credit-depleted alerts (group only)

# Telegram news channel/username read for context. Set SHOAL_CHANNEL to a public
# channel username (e.g. a crypto-research feed); unset → news context is skipped.
SHOAL_CHANNEL = os.environ.get("SHOAL_CHANNEL", "")

# Generic placeholder for the host the operator would SSH into to read gather logs.
VPS_HOST = os.environ.get("SGV_VPS_HOST", "<vps-host>")

TOP_N = int(_THRESHOLDS.get("top_n", 3))                 # top tweets (shared by both voices)
SHOAL_N = int(_THRESHOLDS.get("shoal_n", 3))             # Shoal headlines per voice
IDEAS_N_PER_VOICE = int(_THRESHOLDS.get("ideas_n_per_voice", 2))  # ideas per voice
SHOAL_LIMIT = int(_THRESHOLDS.get("shoal_limit", 30))
# Stage 1 keeps top N for Opus — config `candidate_count` (documented knob);
# `thresholds.pre_filter_keep` kept as a legacy fallback name.
PRE_FILTER_KEEP = int(CONFIG.get("candidate_count", _THRESHOLDS.get("pre_filter_keep", 50)))
# Non-crypto (ai/tech/vc) candidates kept for the hot-QT lane — a constant, not a
# knob: the post-dedupe non-crypto pool is small and Opus ranks on heat anyway.
NONCRYPTO_KEEP = 15
# Optional min heuristic score for the candidate pool (config `score_threshold`;
# 0 disables). Softly enforced — see main(): never allowed to empty the pool.
SCORE_THRESHOLD = int(CONFIG.get("score_threshold", 0))
OPUS_MODEL = _MODELS.get("opus", "claude-opus-4-7")
SEND_DELAY = float(_THRESHOLDS.get("send_delay_s", 1.0))
BUDGET_SECONDS = int(_THRESHOLDS.get("budget_seconds", 550))

VOICE_PROFILES_PATH = _voice_profiles_path()


# ─────────────────────────────────────────
# STAGE 1 — heuristic pre-filter
# ─────────────────────────────────────────
def load_inputs():
    with open(OWNED_READS_PATH) as f:
        o = json.load(f)
    return o


def fetch_shoal_news():
    """Read the configured news channel via the bundled Telethon reader.
    No-op (returns []) if SHOAL_CHANNEL is unset."""
    if not SHOAL_CHANNEL:
        print("[shoal] SHOAL_CHANNEL unset — skipping news context", file=sys.stderr)
        return []
    shoal_out = os.path.join(tempfile.gettempdir(), "sgv_shoal_news.json")
    try:
        subprocess.run(
            [VENV_PY, TELEGRAM_READ_PY, "--channel", SHOAL_CHANNEL,
             "--limit", str(SHOAL_LIMIT), "--json"],
            stdout=open(shoal_out, "w"), stderr=subprocess.DEVNULL,
            timeout=60, check=False)
        with open(shoal_out) as f:
            msgs = json.load(f)
    except Exception as e:
        print(f"[shoal] fetch error: {e}", file=sys.stderr)
        return []

    # Filter to last 20h + URLs
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=20)
    filtered = []
    for m in msgs:
        if not m.get("date"):
            continue
        try:
            d = datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00"))
        except Exception:
            continue
        if d <= cutoff:
            continue
        text = m.get("full_text", "") or ""
        if "http" not in text:
            continue
        filtered.append({
            "date": m["date"],
            "text": text[:1000],
        })
    return filtered


def dedupe_and_filter(owned_reads):
    # Pool = owned_reads.list_tweets (mentions path is deprecated/removed)
    list_tweets = owned_reads.get("list_tweets", []) or []

    pool = {}
    list_count_by_id = defaultdict(set)
    list_names_by_id = defaultdict(set)
    cats_by_id = defaultdict(set)
    for t in list_tweets:
        tid = t.get("tweet_id")
        if not tid:
            continue
        list_count_by_id[tid].add(t.get("from_list_id"))
        if t.get("from_list_name"):
            list_names_by_id[tid].add(t.get("from_list_name"))
        # None-safe: a pre-upgrade latest.json (no category field) resolves to
        # crypto, keeping the core pipeline byte-identical to today.
        cats_by_id[tid].add(t.get("from_list_category") or "crypto")
        if tid not in pool:
            pool[tid] = t

    # Own accounts are excluded from the candidate pool (read from config).
    own = set()
    for k in ("sgv_username", "personal_username"):
        v = CONFIG.get(k)
        if v:
            own.add(v.lstrip("@").lower())

    raw_following = owned_reads.get("following_snapshot", []) or []
    if raw_following and isinstance(raw_following[0], dict):
        following = {u["id"] for u in raw_following}
    else:
        following = set(raw_following)

    filtered = []
    for tid, t in pool.items():
        acc = t["account"].lstrip("@").lower()
        if acc in own:
            continue
        m = t.get("metrics", {})
        af = t.get("author_followers", 0)
        author_in_following = t.get("account_id") in following
        if af < 100 and m.get("like_count", 0) < 10 and not author_in_following:
            continue
        # non-content replies filter
        text = t.get("text", "") or ""
        if t.get("type") == "reply" and len(text) < 20 and "http" not in text:
            continue
        t["list_count"] = len(list_count_by_id[tid])
        t["from_list_names"] = sorted(list_names_by_id[tid])
        t["in_following"] = author_in_following
        # Rule: a tweet appearing in ANY crypto list stays crypto — the crypto
        # pipeline sees exactly what it sees today.
        cats = cats_by_id[tid]
        t["category"] = "crypto" if ("crypto" in cats or not cats) else sorted(cats)[0]
        filtered.append(t)

    return filtered


def heuristic_score(t):
    m = t.get("metrics", {})
    text = (t.get("text") or "").lower()
    sc = 0

    # ENGAGEMENT (max 7)
    eng_sum = m.get("like_count", 0) + m.get("retweet_count", 0) + m.get("reply_count", 0) + m.get("quote_count", 0)
    impr = m.get("impression_count", 0)
    rate = (eng_sum / impr) if impr > 0 else 0
    e = 0
    if rate > 0.03:
        e += 3
    elif rate > 0.01:
        e += 2
    elif rate > 0.003:
        e += 1
    # VELOCITY: engagement per hour since posting — a 30-min-old tweet with 150
    # engagements is hotter than a 6-hour-old one with 400. Catches risers before
    # they peak (the whole point of engaging early). Missing created_at -> 0.
    age_days = _days_since_iso_str(t.get("created_at"))
    if age_days < 2:  # only meaningful for fresh tweets; guards the 1e9 sentinel
        age_h = max(age_days * 24, 0.5)
        velocity = eng_sum / age_h
        if velocity > 150:
            e += 2
        elif velocity > 50:
            e += 1
    if m.get("like_count", 0) > 1000:
        e += 2
    elif m.get("like_count", 0) > 300:
        e += 1
    if m.get("retweet_count", 0) > 100:
        e += 1
    if m.get("reply_count", 0) > 50:
        e += 1
    sc += min(e, 7)

    # LIST PROVENANCE (max 5)
    p = 0
    lc = t.get("list_count", 0)
    if lc >= 3:
        p += 3
    elif lc == 2:
        p += 2
    elif lc == 1:
        p += 1
    if t.get("in_following"):
        p += 2
    sc += min(p, 5)

    # THESIS (max 3)
    th = 0
    label = "general"
    cat = t.get("category") or "crypto"
    if cat != "crypto":
        # Non-crypto lane: flat provenance-as-thesis — the operator curated the
        # list, that IS the fit signal; the crypto keywords below would zero
        # these out. Engagement/provenance/novelty still score normally.
        th += 2
        label = cat
    elif any(k in text for k in ["consumer crypto", "onchain ux", "app layer", "consumer app"]):
        th += 3
        label = "consumer-crypto"
    elif any(k in text for k in ["raised ", "seed round", "series a", "series b", "fundraise", "$m round", "closed $"]):
        th += 3
        label = "fundraise"
    elif any(k in text for k in ["defi ", "stablecoin", "l2", "rollup"]):
        th += 2
        label = "defi-infra"
    elif any(k in text for k in ["ai agent", "fintech", "creator economy", "prediction market", "polymarket", "kalshi"]):
        th += 2
        label = "crypto-adjacent"
    else:
        th += 1
    sc += min(th, 3)

    # NOVELTY (max 2)
    n = 0
    if len(t.get("text", "")) > 200:
        n += 1
    ents = t.get("entities", {}) or {}
    if ents.get("urls") or ents.get("mentions") or ents.get("hashtags"):
        n += 1
    sc += min(n, 2)

    # ACTIONABILITY (max 1)
    if m.get("reply_count", 0) < 500 and m.get("like_count", 0) > 50:
        sc += 1

    t["_heuristic_score"] = sc
    t["_thesis_label"] = label
    return sc


# ─────────────────────────────────────────
# STAGE 2 — Opus picks + drafts (dual voice)
# ─────────────────────────────────────────


def _days_since_iso_str(iso_str):
    """Days between now and ISO date string. None / invalid → infinity."""
    if not iso_str:
        return 1e9
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - dt).total_seconds() / 86400
    except Exception:
        return 1e9


def _load_voice_profiles():
    """Load voice profiles for both voices from voice_profiles.json."""
    try:
        with open(VOICE_PROFILES_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _render_voice_block(profile):
    """Compact voice block for the system prompt."""
    if not profile or "voice_summary" not in profile:
        return "(no voice profile available. use generic crypto-VC voice)"
    lines = [f"Voice: {profile['voice_summary']}"]
    do = profile.get("do") or []
    if do:
        lines.append("DO:")
        lines.extend(f"  • {x}" for x in do[:6])
    dont = profile.get("dont") or []
    if dont:
        lines.append("DON'T:")
        lines.extend(f"  • {x}" for x in dont[:5])
    examples = profile.get("best_examples") or []
    if examples:
        lines.append("REPRESENTATIVE TWEETS:")
        lines.extend(f"  • {x}" for x in examples[:5])
    return "\n".join(lines)


def _build_system_prompt():
    profiles = _load_voice_profiles()
    sgv_voice = _render_voice_block(profiles.get("sgv", {}))
    mist_voice = _render_voice_block(profiles.get("mist", {}))

    sgv_handle = CONFIG.get("sgv_username", "socialgraphvc")
    personal_handle = CONFIG.get("personal_username", "0x_Mist")

    return f"""You are the analyst for Social Graph Ventures (SGV), a crypto VC fund.

Thesis (what SGV invests in):
- Consumer crypto / crypto apps / onchain UX
- DeFi infrastructure, stablecoins, payment rails
- Crypto-adjacent: AI agents, fintech, creator economy, prediction markets

Your job today: from a pre-filtered list of top tweets (crypto core + a separate
non-crypto lane) and crypto-news headlines, produce ONE structured digest with
content for TWO different audiences:
  1) The SGV team group chat (@{sgv_handle} fund voice)
  2) The partner's personal account (@{personal_handle} personal voice)

Every candidate carries a `category` field. CATEGORY FENCING (strict):
- `picks`, `sgv_ideas`, and `mist_ideas` must use ONLY category="crypto" candidates.
- `hot_qt_picks` (below) uses ONLY category!="crypto" candidates.
- No tweet_id may appear in more than one place anywhere in your output.

Both channels share the same {TOP_N} top tweets and {SHOAL_N} news headlines. They DIFFER on tweet drafts:
  - {IDEAS_N_PER_VOICE} tweet ideas in @{sgv_handle} voice (for the fund account)
  - {IDEAS_N_PER_VOICE} tweet ideas in @{personal_handle} personal voice (for the personal account)
The two sets MUST be DIFFERENT. Different angles, different framings, distinct content.

CRITICAL WRITING RULE: Never use the em dash character (the long dash that auto-inserts
when typing two hyphens). Use periods, commas, parentheses, or 'and' instead. This applies
to every field you output: 'why' rationales, tweet drafts, setups, headlines, all of it.
The voice profiles below repeat this rule, and the picks should follow it too.

========================================
TOP TWEETS: pick {TOP_N} (shared between both channels)
========================================

ACTION TAXONOMY (pick exactly one per top tweet):
- **Reply**: thesis-relevant, <500 replies. Default for conversations.
- **QT**: viral or insightful; SGV has a thesis overlay to add.
- **DM founder**: launches, stealth reveals, deal signals.
- **Bookmark**: long-form research, no public action.
- **Pass**: rare; only if buried under 1000+ replies.

VARIETY CONSTRAINTS on the {TOP_N} picks:
- At least 1 fundraise or deal signal
- At least 1 product-launch signal (consumer-crypto, defi-infra, or crypto-adjacent)
- All {TOP_N} must be unique authors

========================================
NEWS HEADLINES: pick {SHOAL_N} per voice (can be DIFFERENT picks per channel)
========================================

From the news items below, pick {SHOAL_N} for the SGV channel AND {SHOAL_N} for the personal channel.
The two sets MAY overlap if the same item is genuinely best for both, but PREFER different picks when both are strong fits for different reasons.

Ranking criteria (apply to each voice separately):
1. **Fit with the voice's audience**
   - SGV fit: thesis relevance for the fund (consumer crypto, DeFi infra, stablecoins, payment rails,
     AI agents, fintech, creator economy, prediction markets). Items SGV partners and LPs want to discuss.
     Favor: institutional moves, infra unlocks, market-structure shifts, fundraises.
   - Personal fit: builder and crypto-native angles. Things the partner would personally riff on. Favor: contrarian
     takes, AI x crypto intersection, technical primitives, founder moves, edge thinking before consensus.

2. **Virality potential** (applies to both voices equally)
   - Crispness of the headline (does it grab attention in one line?)
   - Named entities and concrete numbers (a fund name, a dollar figure, a YoY %)
   - Timeliness (very recent breaking news weighs heavier)
   - Implicit quote-tweet potential (will people want to add their take?)
   - Controversy or contrarian framing

For each pick, the `why` field must mention BOTH a fit reason AND a virality reason.

========================================
HOT NON-CRYPTO QTs: pick UP TO 3 (shared picks, per-voice drafts)
========================================

From the candidates with category != "crypto" (ai / tech / vc), pick UP TO 3 of
the HOTTEST tweets worth quote-tweeting from a crypto-VC seat. If there are no
non-crypto candidates (or none worth it), return "hot_qt_picks": [] — NEVER fill
these slots with crypto tweets or mislabeled picks.

Rank purely on heat + QT-ability: engagement velocity, crispness, named entities
and concrete numbers, implicit quote-tweet potential, controversy or contrarian
framing. Unique authors across the picks; prefer >=2 distinct categories when
available. For each pick write TWO QT drafts of the SAME tweet — one in the
@{sgv_handle} fund voice, one in the @{personal_handle} personal voice — each
adding a crypto/agents/market-structure angle the original doesn't have. Do not
write a personal rephrase of the fund draft; different entry points.

========================================
TWEET IDEAS: voice-specific
========================================

=== @{sgv_handle} voice ===
{sgv_voice}

=== @{personal_handle} personal voice ===
{mist_voice}

CONSTRAINTS for ALL {IDEAS_N_PER_VOICE * 2} drafts (combined across both voices):
- 280 characters or fewer per draft
- The {IDEAS_N_PER_VOICE} SGV drafts and the {IDEAS_N_PER_VOICE} personal drafts must address DIFFERENT angles or topics.
  Do not write a personal version of an SGV draft. Different sources, different framings.
- Anonymize internal info (no portfolio companies, founder names, deal terms)
- For QT-style drafts: include the source URL
- Reminder: never use the em dash character anywhere in the drafts

IDEA SOURCING: try to vary across these source types:
- News thesis overlay
- Cross-list consensus (tweet with highest list_count)
- Tier-0 follow-graph highlight
- Builder or founder direct engagement (personal voice particularly)

OUTPUT FORMAT (strict JSON, nothing else. Your entire response must be valid JSON):

ADDITIONAL REQUIRED FIELD: every object inside `sgv_ideas` and `mist_ideas`
(both the regular "tweet" items AND the "quote_tweet" item) MUST also include
a `viral_hooks` field — a JSON array of 1-3 tags, MOST-DOMINANT FIRST, drawn
from this vocabulary:
  - "concrete_number"  → has a specific $, %, Nx, count, or hard number
  - "contrarian"       → takes a position against consensus ("hot take", "actually", "myth")
  - "pattern"          → identifies a trend ("we're seeing", "every X", "the new pattern")
  - "question"         → opens with or contains a real reader-facing question
  - "no_hook"          → none of the above (use ONLY if nothing else fits)
Pick the ONE that best characterises the draft, plus up to 2 secondary tags.
The first element is treated as the primary_hook by the feedback loop.

{{
  "picks": [
    {{
      "rank": 1,
      "tweet_id": "...",
      "why": "1-sentence rationale weaving metrics + provenance + thesis",
      "action": "Reply|QT|DM founder|Bookmark|Pass"
    }}
    // ...{TOP_N} items
  ],
  "sgv_shoal_picks": [
    {{
      "headline": "short summary of the news item",
      "url": "first URL in the message",
      "why": "1 sentence covering BOTH (a) SGV fit (thesis lens) AND (b) virality (why it grabs attention or invites quote-tweets)"
    }}
    // ...{SHOAL_N} items ranked by SGV fit + virality
  ],
  "mist_shoal_picks": [
    {{
      "headline": "short summary of the news item",
      "url": "first URL in the message",
      "why": "1 sentence covering BOTH (a) personal fit (builder/contrarian angle) AND (b) virality"
    }}
    // ...{SHOAL_N} items ranked by personal fit + virality. MAY overlap with SGV picks if genuinely best for both, but prefer distinct picks when possible
  ],
  "sgv_ideas": [
    {{
      "kind": "tweet",
      "source": "News|Cross-list consensus|Tier-0 follow|Founder engagement",
      "setup": "1-sentence setup",
      "draft": "<=280 char @{sgv_handle}-voice draft (standalone tweet, no URL appended)",
      "inspo_label": "short text describing the inspo (e.g. '@someone tweet on MEV' or 'News: a stablecoin issuer launches yield')",
      "inspo_url": "https://x.com/.../status/...  OR  the news URL  OR  empty string if neither applies",
      "topic": "1-2 word topic tag"
    }},
    // ...{IDEAS_N_PER_VOICE} 'tweet' items, then 1 'quote_tweet':
    {{
      "kind": "quote_tweet",
      "source": "Cross-list consensus|Tier-0 follow|News",
      "setup": "1-sentence rationale for quoting THIS specific tweet",
      "draft": "<=260 char QT commentary in @{sgv_handle} voice (no URL — system appends it)",
      "inspo_label": "@author — first line of the original tweet",
      "inspo_url": "https://x.com/<author>/status/<id>  (REQUIRED for kind=quote_tweet — the tweet being quoted)",
      "topic": "1-2 word topic tag"
    }}
  ],
  "mist_ideas": [
    {{
      "kind": "tweet",
      "source": "News|Cross-list consensus|Tier-0 follow|Founder engagement",
      "setup": "1-sentence setup",
      "draft": "<=280 char @{personal_handle}-voice draft (DIFFERENT topic/angle from sgv_ideas)",
      "inspo_label": "short text describing the inspo",
      "inspo_url": "URL of the original tweet or news item, or empty",
      "topic": "1-2 word topic tag"
    }},
    // ...{IDEAS_N_PER_VOICE} 'tweet' items, then 1 'quote_tweet':
    {{
      "kind": "quote_tweet",
      "source": "Cross-list consensus|Tier-0 follow|News",
      "setup": "1-sentence rationale for quoting THIS specific tweet",
      "draft": "<=260 char QT commentary in @{personal_handle} voice (no URL — system appends it)",
      "inspo_label": "@author — first line of the original tweet",
      "inspo_url": "https://x.com/<author>/status/<id>  (REQUIRED for kind=quote_tweet)",
      "topic": "1-2 word topic tag"
    }}
  ],
  "hot_qt_picks": [
    {{
      "category": "ai|tech|vc  (the candidate's category field)",
      "tweet_id": "...",
      "setup": "1-sentence why THIS tweet is hot + what the SGV angle adds",
      "sgv_draft": "<=260 char QT commentary in @{sgv_handle} voice (no URL — system appends it)",
      "mist_draft": "<=260 char QT commentary in @{personal_handle} voice (different entry point)",
      "inspo_label": "@author — first line of the original tweet",
      "inspo_url": "https://x.com/<author>/status/<id>  (REQUIRED)",
      "topic": "1-2 word topic tag",
      "viral_hooks": ["concrete_number"]
    }}
    // ...UP TO 3 items; [] when no non-crypto candidate is worth it
  ]
}}

EACH voice array contains {IDEAS_N_PER_VOICE} regular tweets + 1 quote_tweet = {IDEAS_N_PER_VOICE + 1} items total.
hot_qt_picks is SHARED (not per-voice): each item carries both drafts."""


def _feedback_block():
    """Build a short 'WHAT'S WORKED RECENTLY' block from the latest feedback_profile.
    Returns empty string if none available or sample size is too small."""
    try:
        import feedback_db as _fdb
        fp = _fdb.get_latest_feedback_profile()
    except Exception:
        fp = None
    if not fp or fp.get("matched_tweets", 0) < 3:
        return ""
    recs = (fp.get("recommendations") or {}) if isinstance(fp.get("recommendations"), dict) else {}
    do_more = recs.get("do_more") or []
    do_less = recs.get("do_less") or []
    evidence = recs.get("evidence") or []
    if not (do_more or do_less):
        return ""
    parts = ["RECENT PERFORMANCE SIGNAL (from past tweet engagement — bias toward these):"]
    for r in do_more[:5]:
        parts.append(f"  • DO MORE: {r}")
    for r in do_less[:5]:
        parts.append(f"  • DO LESS: {r}")
    if evidence:
        parts.append("  Evidence:")
        for e in evidence[:3]:
            parts.append(f"    - {e}")
    return "\n".join(parts)


def _build_system_prompt_with_feedback():
    base = _build_system_prompt()
    fb = _feedback_block()
    if not fb:
        return base
    return base + "\n\n" + fb


SYSTEM_PROMPT = _build_system_prompt_with_feedback()


def _load_active_feedback_profile_id():
    """Return id of the feedback_profile that's actually being injected into the
    Opus prompt right now, or None if the gate (matched_tweets >= 3) failed."""
    try:
        import feedback_db as _fdb
        fp = _fdb.get_latest_feedback_profile()
        if not fp or fp.get("matched_tweets", 0) < 3:
            return None
        return fp.get("id")
    except Exception:
        return None


# Captured at import time, same moment SYSTEM_PROMPT is built, so every idea
# saved during this digest run is conditioned on the same profile id.
ACTIVE_FEEDBACK_PROFILE_ID = _load_active_feedback_profile_id()


def _print_steering_status_if_enabled():
    """Phase 3: when SGV_SHOW_STEERING is truthy, log a one-line summary of the
    LAST measured loop (yesterday's profile + its observed compliance/lift).
    Goes to stdout only (no Telegram). Today's loop won't be measured until the
    feedback job runs, so what we show is always "yesterday's"."""
    if not os.environ.get("SGV_SHOW_STEERING"):
        return
    try:
        import feedback_db as _fdb
        c = _fdb._conn()
        row = c.execute("""
            SELECT feedback_profile_id, ideas_under_profile,
                   do_more_compliance, do_less_compliance,
                   matched_on_rec_count, matched_off_rec_count, engagement_lift
            FROM steering_metrics
            ORDER BY id DESC LIMIT 1
        """).fetchone()
    except Exception as e:
        print(f"  [steering] read failed: {e}")
        return
    if not row:
        print("  📊 Steering: no measurements yet")
        return
    fp = row["feedback_profile_id"]
    n = row["ideas_under_profile"] or 0
    dm = row["do_more_compliance"]
    dl = row["do_less_compliance"]
    on_n = row["matched_on_rec_count"] or 0
    off_n = row["matched_off_rec_count"] or 0
    lift = row["engagement_lift"]

    def pct(v):
        return f"{v*100:+.0f}%" if v is not None else "—"

    lift_str = f"{lift:.2f}x" if lift is not None else "— (waiting on matches)"
    print(f"  📊 Steering (profile #{fp}, n={n} ideas): "
          f"do_more compliance {pct(dm)}, do_less compliance {pct(dl)}; "
          f"engagement on_rec/off_rec = {on_n}/{off_n}, lift {lift_str}")


def _load_idea_memory(days=7, cap=40):
    """Recent drafted ideas as DO-NOT-REPEAT lines for the Opus USER message —
    the same anti-repetition pattern insights.py uses (never in the cached
    system prompt). Empty list on any failure -> today's behavior."""
    try:
        import feedback_db as _fdb
        rows = _fdb.get_recent_ideas(days=days)
    except Exception:
        return []
    lines, seen = [], set()
    for r in rows:
        line = (f"[{(r.get('generated_at') or '')[:10]} {r.get('voice') or '?'}] "
                f"topic={r.get('topic') or '?'} | draft: {(r.get('draft') or '')[:120]}")
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return lines[:cap]


def opus_pick(candidates, shoal_news):
    """The main pick+draft call (provider/model per config 'backends.opus_pick';
    defaults to direct Anthropic + OPUS_MODEL). Returns parsed JSON."""
    import llm_backend

    # Compact the candidates (strip noisy fields)
    compact = []
    for t in candidates:
        m = t.get("metrics", {})
        compact.append({
            "tweet_id": t["tweet_id"],
            "account": t["account"],
            "author_followers": t.get("author_followers"),
            "text": (t.get("text") or "")[:400],
            "like": m.get("like_count"),
            "rt": m.get("retweet_count"),
            "reply": m.get("reply_count"),
            "quote": m.get("quote_count"),
            "impr": m.get("impression_count"),
            "type": t.get("type"),
            "list_count": t.get("list_count", 0),
            "from_lists": t.get("from_list_names", []),
            "in_following": t.get("in_following", False),
            "heuristic_score": t["_heuristic_score"],
            "thesis_label": t["_thesis_label"],
            "category": t.get("category") or "crypto",
            "url": t.get("tweet_url"),
        })

    compact_shoal = [
        {"date": s["date"][:16], "text": s["text"][:500]}
        for s in shoal_news[:10]
    ]

    user_msg = (
        "Today's data:\n\n"
        f"CANDIDATES ({len(compact)} tweets, pre-filtered + heuristically ranked):\n"
        f"{json.dumps(compact, indent=2)}\n\n"
        f"NEWS ({len(compact_shoal)} items from last 20h):\n"
        f"{json.dumps(compact_shoal, indent=2)}\n\n"
    )
    memory_lines = _load_idea_memory()
    if memory_lines:
        user_msg += (
            "ALREADY DRAFTED IN THE LAST 7 DAYS — DO NOT REPEAT:\n"
            + "\n".join("  - " + l for l in memory_lines)
            + "\nHARD RULE: a topic+angle above is BURNED even reworded. Take a "
            "genuinely different angle or different source material for every "
            "idea and QT draft in this output.\n\n"
        )
    user_msg += "Return the structured JSON as specified. No preamble, no explanation, JSON only."

    # 20000 max_tokens: the JSON alone is ~3.6k and reasoning models (K3/GLM)
    # burn thinking tokens from the same budget — GLM truncated at 10000 on
    # this prompt. Only generated tokens are billed; Opus is unaffected.
    raw, usage, cost = llm_backend.chat(
        stage="opus_pick",
        system_text=SYSTEM_PROMPT,
        user_text=user_msg,
        max_tokens=20000,
        default_model=OPUS_MODEL,
    )
    # Parse JSON (tolerate code fences + trailing prose)
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?|\n?```$", "", raw)
    start = raw.find("{")
    if start < 0:
        raise RuntimeError("opus_pick: no JSON object in model output")
    parsed, _ = json.JSONDecoder().raw_decode(raw[start:])
    return parsed, usage, cost


# ─────────────────────────────────────────
# STAGE 3 — delivery
# ─────────────────────────────────────────
_BOT_TOKEN_CACHE = None


def _bot_token():
    """Telegram Bot API token from the environment (no token file on disk)."""
    global _BOT_TOKEN_CACHE
    if _BOT_TOKEN_CACHE is None:
        tok = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not tok:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment")
        _BOT_TOKEN_CACHE = tok.strip()
    return _BOT_TOKEN_CACHE


def _bot_label():
    """Human-readable bot label derived from the token's leading numeric id
    (no hardcoded @handle). Falls back to 'bot' if the token is unset/odd."""
    try:
        return f"bot#{_bot_token().split(':', 1)[0]}"
    except Exception:
        return "bot"


def _send_via_bot(chat_id, text, timeout=20):
    """Send a message via the Telegram Bot HTTP API. Raises on failure."""
    import urllib.request as _ur, urllib.error as _ue, json as _j
    url = f"https://api.telegram.org/bot{_bot_token()}/sendMessage"
    payload = _j.dumps({
        "chat_id": int(chat_id),
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = _ur.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with _ur.urlopen(req, timeout=timeout) as resp:
        result = _j.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"bot send failed: {result}")
    return result


def _alert_operator(msg, dry_run):
    """Fatal-path alert to BOTH the fund group and the personal DM. All three
    credit outages in week 1 DID alert the group — and got drowned; the
    operator's DM is what actually gets seen."""
    if dry_run:
        return
    try:
        subprocess.run(["bash", SEND_SCRIPT, DELIVERY_CHAT, msg], check=False, timeout=20)
    except Exception:
        pass
    try:
        if DELIVERY_CHAT_MIST:
            _send_via_bot(DELIVERY_CHAT_MIST, msg)
    except Exception:
        pass


def make_sender(dry_run, chat_id, label="", via="user_session"):
    """Returns a (send, stats) pair scoped to a single chat_id.

    via='user_session' → the operator's Telethon user account (used for the group)
    via='bot'          → the configured bot via Bot HTTP API (DM delivery)
    """
    errors = []
    sent = 0

    def send(msg):
        nonlocal sent
        if dry_run:
            print(f"[DRY → {label} via {via}] {msg}")
            print("---")
            sent += 1
            return
        try:
            if via == "bot":
                _send_via_bot(chat_id, msg)
            else:
                subprocess.run(
                    ["bash", SEND_SCRIPT, str(chat_id), msg],
                    check=True, timeout=30, capture_output=True,
                )
            sent += 1
        except Exception as e:
            err = f"SEND_FAIL[{label}/{via}]: {msg[:60]}... ({e})"
            errors.append(err)
            print(err, file=sys.stderr)
        time.sleep(SEND_DELAY)

    def stats():
        return {"sent": sent, "send_failures": len(errors)}

    return send, stats


# ─────────────────────────────────────────
# SLIM 3-SECTION DELIVERY
# ─────────────────────────────────────────
def _idea_messages(emoji, num, kind, topic, draft, inspo_label, inspo_url=""):
    """Return (context_msg, draft_msg) — context goes first, draft is the standalone
    copy-paste-ready tweet text.

    kind='quote_tweet' → context uses 🔁 QT label, draft has the inspo_url appended
    at the end so when the user posts on X the original tweet auto-embeds as a quote.
    """
    if kind == "quote_tweet":
        header_label = "🔁 QT"
        if topic:
            header_label += f" ({topic})"
        ctx_parts = [header_label]
        if inspo_label:
            ctx_parts.append(f"↳ quoting: {inspo_label}")
        if inspo_url:
            ctx_parts.append(f"  {inspo_url}")
        context = "\n".join(ctx_parts)
        # Append the URL to the draft body so X auto-embeds the quote when posted
        draft_body = draft.strip()
        if inspo_url and inspo_url not in draft_body:
            draft_body = f"{draft_body}\n\n{inspo_url}"
        return context, draft_body

    # Regular tweet (kind='tweet' or any other / unspecified)
    header = f"{emoji} #{num}" + (f" ({topic})" if topic else "")
    ctx_parts = [header]
    if inspo_label:
        ctx_parts.append(f"↳ inspo: {inspo_label}")
    if inspo_url:
        ctx_parts.append(f"  {inspo_url}")
    context = "\n".join(ctx_parts)
    return context, draft.strip()


def _capitalize_mist(text):
    """Personal-voice grammar pass: capitalize the first letter of the tweet and
    the first letter after each '. '. Leaves the rest unchanged (standalone
    'i' stays lowercase — the personal voice is deliberately lowercase otherwise).

    Skips capitalization for URLs (`https://`, `http://`) — they shouldn't
    get sentence-cased even when they follow a period.
    """
    if not text:
        return text
    # Capitalize the first alphabetic char in the string, unless it starts a URL
    text = re.sub(
        r'^(\W*)([a-z])(?!ttps?://)',
        lambda m: m.group(1) + m.group(2).upper(),
        text, count=1,
    )
    # Capitalize the first alphabetic char after a sentence-ending '. '
    text = re.sub(
        r'(\.\s+)([a-z])(?!ttps?://)',
        lambda m: m.group(1) + m.group(2).upper(),
        text,
    )
    return text


def _emit_idea(send, emoji, num, kind, topic, draft, inspo_label, inspo_url=""):
    """Send context + standalone draft as TWO separate messages for clean copy-paste."""
    ctx, body = _idea_messages(emoji, num, kind, topic, draft, inspo_label, inspo_url)
    send(ctx)
    send(body)


# Set by main() — when True, delivery never touches feedback.db (a dry run must
# not seed the feedback loop with ideas that were never actually delivered).
DRY_RUN = False


def _save_idea_to_db(idea_dict):
    """Persist one idea row. Silent-pass on DB import failure (db is optional infra).
    No-op on --dry-run."""
    if DRY_RUN:
        return None
    try:
        import feedback_db as _db
        return _db.save_idea(idea_dict)
    except Exception as e:
        print(f"  [warn] could not save idea to feedback.db: {e}")
        return None


def _deliver_section1_tweet_ideas(opus_out, send, voice_key, digest_run_id, voice):
    """Section 1 — Opus-generated tweet ideas (2 regular + 1 QT) with inspo.

    Each idea = 2 messages: a small context message + a standalone draft message
    that's clean for copy-paste.

    For quote_tweet items: the standalone draft has the original tweet URL appended
    at the end so X auto-embeds the quoted tweet when posted.
    """
    ideas = (opus_out.get(voice_key) or [])[:IDEAS_N_PER_VOICE + 1]  # up to 3 (2 tweet + 1 QT)
    if not ideas:
        send("━━ 1️⃣ TWEET IDEAS ━━\n(no ideas this run)")
        return 0
    send("━━ 1️⃣ TWEET IDEAS ━━")
    sent_count = 0
    # Number ONLY the 'tweet' kind items; QT gets its own label
    tweet_num = 0
    for idea in ideas:
        draft = (idea.get("draft") or "").strip()
        if not draft:
            continue
        if voice == "mist":
            draft = _capitalize_mist(draft)
        kind = (idea.get("kind") or "tweet").lower()
        if kind == "tweet":
            tweet_num += 1
        _emit_idea(
            send,
            emoji="💡",
            num=tweet_num,
            kind=kind,
            topic=idea.get("topic") or idea.get("source") or "",
            draft=draft,
            inspo_label=idea.get("inspo_label") or idea.get("setup") or idea.get("source") or "",
            inspo_url=idea.get("inspo_url") or "",
        )
        sent_count += 1
        _save_idea_to_db({
            "digest_run_id":    digest_run_id,
            "source_module":    "opus_picks" if kind == "tweet" else "opus_quote_tweet",
            "voice":            voice,
            "topic":            idea.get("topic") or idea.get("source"),
            "draft":            draft,
            "inspo_kind":       "quote_tweet" if kind == "quote_tweet" else
                                ("x_tweet" if "x.com" in (idea.get("inspo_url") or "") else "news"),
            "inspo_id":         None,
            "inspo_label":      idea.get("inspo_label") or idea.get("setup"),
            "inspo_snippet":    idea.get("setup"),
            "inspo_url":        idea.get("inspo_url"),
            "viral_hooks":      idea.get("viral_hooks") or [],
            "feedback_profile_id": ACTIVE_FEEDBACK_PROFILE_ID,
            "raw":              idea,
        })
    return sent_count


def _deliver_section1b_hot_qts(opus_out, send, voice, digest_run_id):
    """Section 1b — hot NON-CRYPTO QTs (ai/tech/vc): shared Opus picks, per-voice
    drafts. Each pick = context message + standalone QT draft (inspo URL appended
    so X auto-embeds the quote on paste). Saved with source_module
    'opus_noncrypto_qt' so the feedback loop learns whether the lane earns
    engagement — zero extra learning code."""
    picks = (opus_out.get("hot_qt_picks") or [])[:3]
    if not picks:
        send("━━ 🔥 HOT QTs — AI / tech / VC ━━\n(none this run)")
        return 0
    send("━━ 🔥 HOT QTs — AI / tech / VC ━━")
    delivered = 0
    for i, p in enumerate(picks, 1):
        draft = (p.get("sgv_draft" if voice == "sgv" else "mist_draft") or "").strip()
        if not draft:
            continue
        if voice == "mist":
            draft = _capitalize_mist(draft)
        _emit_idea(
            send,
            emoji="🔥",
            num=i,
            kind="quote_tweet",
            topic=p.get("topic") or p.get("category") or "",
            draft=draft,
            inspo_label=p.get("inspo_label") or p.get("setup") or "",
            inspo_url=p.get("inspo_url") or "",
        )
        delivered += 1
        _save_idea_to_db({
            "digest_run_id":    digest_run_id,
            "source_module":    "opus_noncrypto_qt",
            "voice":            voice,
            "topic":            p.get("topic") or p.get("category"),
            "draft":            draft,
            "inspo_kind":       "noncrypto_qt",
            "inspo_id":         p.get("tweet_id"),
            "inspo_label":      p.get("inspo_label"),
            "inspo_snippet":    p.get("setup"),
            "inspo_url":        p.get("inspo_url"),
            "viral_hooks":      p.get("viral_hooks") or [],
            "feedback_profile_id": ACTIVE_FEEDBACK_PROFILE_ID,
            "raw":              p,
        })
    return delivered


def _deliver_section2_insights(insights_result, send, voice, digest_run_id):
    """Section 2 — Sonnet-generated SGV INSIGHTS with anonymized inspo."""
    key = "insights_sgv" if voice == "sgv" else "insights_mist"
    insights = ((insights_result or {}).get(key) or [])[:3]
    src_counts = (insights_result or {}).get("source_counts", {}) or {}
    src_summary = (
        f"{src_counts.get('fireflies',0)} calls · "
        f"{src_counts.get('notion',0)} deals · "
        f"{src_counts.get('emails',0)} weekly memos"
    )
    if not insights:
        send(f"━━ 2️⃣ SGV INSIGHT TWEET IDEAS — 7d fund-internal ({src_summary}) ━━\n(no insights this run)")
        return 0
    send(f"━━ 2️⃣ SGV INSIGHT TWEET IDEAS — 7d fund-internal ({src_summary}) ━━")
    for i, ins in enumerate(insights, 1):
        draft = (ins.get("draft") or "").strip()
        if not draft:
            continue
        if voice == "mist":
            draft = _capitalize_mist(draft)
        _emit_idea(
            send,
            emoji="🧠",
            num=i,
            kind="tweet",
            topic=ins.get("topic", ""),
            draft=draft,
            inspo_label=ins.get("inspo_label", "fund-internal data"),
            inspo_url="",  # SGV INSIGHTS never link out
        )
        _save_idea_to_db({
            "digest_run_id":    digest_run_id,
            "source_module":    "sgv_insights",
            "voice":            voice,
            "topic":            ins.get("topic"),
            "draft":            draft,
            "inspo_kind":       "fund_internal_mix",
            "inspo_id":         json.dumps(ins.get("source_refs") or []),
            "inspo_label":      ins.get("inspo_label"),
            "inspo_snippet":    ins.get("inspo_snippet"),
            "inspo_url":        None,
            "viral_hooks":      ins.get("viral_hooks") or [],
            "feedback_profile_id": ACTIVE_FEEDBACK_PROFILE_ID,
            "raw":              ins,
        })
    return len(insights)


def _deliver_section3_good_content(send, voice, digest_run_id):
    """Section 3 — good-content drafts pulled from feedback.db.
    Returns count of items delivered. Skips silently if no pending content."""
    try:
        import feedback_db as _db
        pending = _db.get_pending_good_content_with_drafts(limit=3)
    except Exception as e:
        print(f"  [warn] good_content lookup failed: {e}")
        pending = []
    if not pending:
        send("━━ 3️⃣ GOOD CONTENT TWEET IDEAS ━━\n(nothing flagged. send 'gc: <url>' in DM to add)")
        return 0
    send("━━ 3️⃣ GOOD CONTENT TWEET IDEAS ━━")
    delivered = 0
    for i, gc in enumerate(pending, 1):
        draft = None
        why = None
        hooks = []
        for d in (gc.get("drafts") or []):
            if d.get("voice") == voice:
                draft = d.get("draft")
                why = d.get("why_david_liked")
                try:
                    hooks = json.loads(d.get("viral_hooks_json") or "[]")
                except Exception:
                    hooks = []
                break
        if not draft:
            continue
        if voice == "mist":
            draft = _capitalize_mist(draft)
        title = (gc.get("title") or gc.get("url") or "")[:80]
        _emit_idea(
            send,
            emoji="📚",
            num=i,
            kind="tweet",
            topic=f"gc#{gc.get('id')}: {title}",
            draft=draft,
            inspo_label=why or title,
            inspo_url=gc.get("url") or "",
        )
        delivered += 1
        _save_idea_to_db({
            "digest_run_id":    digest_run_id,
            "source_module":    "good_content",
            "voice":            voice,
            "topic":            title,
            "draft":            draft,
            "inspo_kind":       gc.get("source_kind") or "article",
            "inspo_id":         str(gc.get("id")),
            "inspo_label":      why or title,
            "inspo_snippet":    (gc.get("description") or "")[:200],
            "inspo_url":        gc.get("url"),
            "viral_hooks":      hooks,
            "feedback_profile_id": ACTIVE_FEEDBACK_PROFILE_ID,
            "raw":              gc,
        })
        # Mark this content as 'reminded' so it doesn't recur.
        # Never on --dry-run: a preview must not consume the pending item.
        if not DRY_RUN:
            try:
                _db.set_good_content_status(
                    gc["id"],
                    "sent" if gc.get("status") != "sent" else "sent",
                    reminded_at=_db.now_iso(),
                )
            except Exception:
                pass
    # Staleness nudge: resurfacing keeps this section non-empty, which used to
    # hide the "send gc:" hint exactly when the library had gone stale.
    try:
        row = _db._conn().execute(
            "SELECT MAX(submitted_at) AS last FROM good_content").fetchone()
        last = row["last"] if row else None
        if last:
            last_dt = datetime.datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=datetime.timezone.utc)
            age_d = (datetime.datetime.now(datetime.timezone.utc) - last_dt).days
            if age_d >= 14:
                send(f"(all resurfaced — no new gc: links in {age_d}d. "
                     f"send 'gc: <url>' in DM to add fresh material)")
    except Exception:
        pass
    return delivered


def _deliver_section4_call_drafts(call_result, send, digest_run_id):
    """Section 4 — personal-only: anonymized tweet drafts from recent calls
    (call_drafts.py two-stage lane). Skips silently when the lane is off."""
    calls = (call_result or {}).get("call_drafts") or []
    if not calls:
        return 0
    send("━━ 4️⃣ CALL DRAFTS — from your recent calls ━━")
    delivered = 0
    for ci, call in enumerate(calls, 1):
        for di, d in enumerate(call.get("drafts") or [], 1):
            draft = _capitalize_mist((d.get("draft") or "").strip())
            if not draft:
                continue
            _emit_idea(
                send,
                emoji="🎙",
                num=delivered + 1,
                kind="tweet",
                topic=f"{call.get('call_label', f'call {ci}')} · {d.get('angle', '')}",
                draft=draft,
                inspo_label=d.get("source_moment") or "call moment",
                inspo_url="",
            )
            delivered += 1
            _save_idea_to_db({
                "digest_run_id":    digest_run_id,
                "source_module":    "call_drafts",
                "voice":            "mist",
                "topic":            d.get("angle"),
                "draft":            draft,
                "inspo_kind":       "call",
                "inspo_id":         call.get("call_label"),
                "inspo_label":      d.get("source_moment"),
                "inspo_snippet":    None,
                "inspo_url":        None,
                "viral_hooks":      d.get("viral_hooks") or [],
                "feedback_profile_id": ACTIVE_FEEDBACK_PROFILE_ID,
                "raw":              d,
            })
    return delivered


def deliver_digest_dual(picks_by_id, opus_out, shoal_news, insights_result, dry_run, call_result=None):
    """Deliver the slim 3-section digest:
      Section 1 — Tweet ideas (Opus, with inspo)
      Section 2 — SGV insight tweet ideas (Sonnet, both voices)
      Section 3 — Good content tweet ideas (Sonnet-drafted from flagged URLs)

    Fund-voice content  → the fund Telegram group (TELEGRAM_DELIVERY_CHAT_SGV)
    Personal-voice content → the personal bot DM (TELEGRAM_DELIVERY_CHAT_MIST)
    """
    today = datetime.datetime.now().strftime("%a %b %-d")
    digest_run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    # Fund group — fund-voice only, sent via the operator's user session.
    # GOOD CONTENT (section 3) intentionally omitted here — that's a personal-only feature.
    print(f"[deliver] → fund group ({DELIVERY_CHAT_SGV}) via user-session")
    sgv_send, sgv_stats = make_sender(dry_run, DELIVERY_CHAT_SGV, "SGV", via="user_session")
    sgv_send(f"☀️ SGV Morning Brief — {today}")
    s1 = _deliver_section1_tweet_ideas(opus_out, sgv_send, "sgv_ideas", digest_run_id, "sgv")
    s1b = _deliver_section1b_hot_qts(opus_out, sgv_send, "sgv", digest_run_id)
    s2 = _deliver_section2_insights(insights_result, sgv_send, "sgv", digest_run_id)
    s3 = 0   # good_content is personal-only by design
    print(f"  SGV sections: tweet_ideas={s1} hot_qts={s1b} insights={s2} good_content=skipped(personal-only)")

    # Personal DM — personal-voice only, sent as the configured bot.
    print(f"[deliver] → personal DM ({DELIVERY_CHAT_MIST}) via {_bot_label()}")
    mist_send, mist_stats = make_sender(dry_run, DELIVERY_CHAT_MIST, "Mist", via="bot")
    mist_send(f"☀️ Personal Morning Brief — {today}")
    m1 = _deliver_section1_tweet_ideas(opus_out, mist_send, "mist_ideas", digest_run_id, "mist")
    m1b = _deliver_section1b_hot_qts(opus_out, mist_send, "mist", digest_run_id)
    m2 = _deliver_section2_insights(insights_result, mist_send, "mist", digest_run_id)
    m3 = _deliver_section3_good_content(mist_send, "mist", digest_run_id)
    m4 = _deliver_section4_call_drafts(call_result, mist_send, digest_run_id)
    print(f"  Mist sections: tweet_ideas={m1} hot_qts={m1b} insights={m2} good_content={m3} call_drafts={m4}")

    return {
        "sgv":  sgv_stats(),
        "mist": mist_stats(),
        "sgv_section_counts":  {"tweet_ideas": s1, "hot_qts": s1b, "insights": s2, "good_content": s3},
        "mist_section_counts": {"tweet_ideas": m1, "hot_qts": m1b, "insights": m2, "good_content": m3,
                                "call_drafts": m4},
    }


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def main():
    global DRY_RUN
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    t0 = time.time()
    print(f"== SGV Tweet Digest {datetime.datetime.now().isoformat()} ==")
    _print_steering_status_if_enabled()

    # Hard-require owned-reads
    if not os.path.exists(OWNED_READS_PATH):
        print(f"FATAL: {OWNED_READS_PATH} missing", file=sys.stderr)
        _alert_operator("⚠️ SGV digest ABORTED: owned-reads latest.json missing.", args.dry_run)
        sys.exit(2)

    owned = load_inputs()
    list_tweets_count = len(owned.get('list_tweets', []) or [])
    following_count = len(owned.get('following_snapshot', []) or [])
    print(f"  loaded owned-reads: {list_tweets_count} list_tweets, "
          f"{following_count} following")

    # Pre-flight: detect upstream gather failures with specific, actionable alerts.
    gather_stats = owned.get("stats", {}) or {}
    gather_errors = gather_stats.get("errors", []) or []
    cap_hit = bool(gather_stats.get("budget_cap_hit"))
    skipped = gather_stats.get("priorities_skipped", []) or []
    cost_today = gather_stats.get("estimated_cost_usd", 0)
    calls_today = gather_stats.get("api_calls", 0)
    scan_age_days = _days_since_iso_str(owned.get("scan_end_utc") or owned.get("scan_start_utc"))

    # Path 1: explicit credits-depleted (Bearer-side 402)
    if gather_stats.get("credits_depleted") or any("credits-depleted" in str(e) for e in gather_errors):
        print("FATAL: gather reported credits_depleted upstream", file=sys.stderr)
        msg = (
            "🛑 SGV digest cannot run today.\n\n"
            "X API credits depleted at the developer-account level. "
            "Top up at https://console.x.com → your project → Pricing/Usage. "
            "The next scheduled gather resumes automatically once credits are restored."
        )
        _alert_operator(msg, args.dry_run)
        sys.exit(2)

    # Path 2: gather data is empty/stale — diagnose and alert with specifics
    if list_tweets_count == 0:
        # Build a specific reason
        if scan_age_days > 1.5:
            reason = f"gather hasn't run successfully in {scan_age_days:.1f} days (cron/timer issue?)"
        elif cap_hit and calls_today < 3:
            reason = f"daily cost cap reached BEFORE first list could be fetched (cost_today=${cost_today}, only {calls_today} calls completed). Likely yesterday's spend hasn't rolled over yet, or the gather already ran today and used the budget."
        elif cap_hit:
            reason = f"daily cost cap hit early (cost_today=${cost_today}, {calls_today} calls). All list fetches were skipped: {', '.join(skipped[:5])}."
        elif gather_errors:
            reason = f"upstream errors: {gather_errors[:3]}"
        else:
            reason = (f"unknown — gather logged {calls_today} calls / ${cost_today} but list_tweets=0. "
                      f"Likely a 401/403 on the /lists/{{id}}/tweets endpoint. "
                      f"Check the gather cron log for [api error] lines.")
        msg = (
            f"⚠️ SGV digest: gather returned 0 tweets.\n\n"
            f"Reason: {reason}\n\n"
            f"State: {len(owned.get('followed_lists',[]))} followed list slots, "
            f"{len(owned.get('list_memberships',[]))} membership slots, "
            f"{following_count} follows cached, scan_age={scan_age_days:.1f}d.\n\n"
            f"Diagnose: `ssh {VPS_HOST} \"tail -30 <gather cron log>\"`"
        )
        print(f"FATAL: list_tweets=0 — {reason}", file=sys.stderr)
        _alert_operator(msg, args.dry_run)
        sys.exit(2)

    if cap_hit:
        print(f"  note: gather hit the daily cost cap; running on partial data "
              f"(${cost_today} spent, {calls_today} calls, {len(skipped)} skipped)")

    shoal = fetch_shoal_news()
    print(f"  news: {len(shoal)} thesis-relevant items from last 20h")

    filtered = dedupe_and_filter(owned)
    for t in filtered:
        heuristic_score(t)
    crypto_pool = [t for t in filtered if (t.get("category") or "crypto") == "crypto"]
    noncrypto_pool = [t for t in filtered if (t.get("category") or "crypto") != "crypto"]
    # Optional min-score gate (config score_threshold) — crypto lane only (the
    # non-crypto lane rides its list curation), and soft: never empties the pool.
    if SCORE_THRESHOLD > 0:
        gated = [t for t in crypto_pool if t["_heuristic_score"] >= SCORE_THRESHOLD]
        if len(gated) >= TOP_N:
            crypto_pool = gated
        else:
            print(f"  score_threshold={SCORE_THRESHOLD} would leave only {len(gated)} tweets — gate skipped this run")
    crypto_pool.sort(key=lambda t: -t["_heuristic_score"])
    noncrypto_pool.sort(key=lambda t: -t["_heuristic_score"])
    candidates = crypto_pool[:PRE_FILTER_KEEP] + noncrypto_pool[:NONCRYPTO_KEEP]
    print(f"  heuristic stage: {len(filtered)} filtered → "
          f"{min(len(crypto_pool), PRE_FILTER_KEEP)} crypto + "
          f"{min(len(noncrypto_pool), NONCRYPTO_KEEP)} non-crypto for Opus")

    if not candidates:
        print("FATAL: no candidates after filter", file=sys.stderr)
        if not args.dry_run:
            cost_today = gather_stats.get("estimated_cost_usd", 0)
            calls = gather_stats.get("api_calls", 0)
            msg = (
                f"⚠️ SGV digest: 0 candidate tweets after filter.\n"
                f"Upstream gather: {calls} calls / ${cost_today}. "
                f"Possibly all lists returned empty or were skipped. "
                f"Check the gather cron log for diagnostics."
            )
            _alert_operator(msg, False)
        sys.exit(2)

    print(f"  calling Opus...")
    try:
        opus_out, usage, cost = opus_pick(candidates, shoal)
    except Exception as e:
        # A cron box would otherwise fail silently — one alert, then non-zero exit.
        print(f"FATAL: Opus pick failed: {e}", file=sys.stderr)
        _alert_operator(f"⚠️ SGV digest ABORTED: Opus call failed ({str(e)[:200]}). "
                        f"No digest today — check the digest log.", args.dry_run)
        sys.exit(3)
    print(f"  Opus returned: {len(opus_out.get('picks',[]))} picks, "
          f"sgv_shoal={len(opus_out.get('sgv_shoal_picks',[]))} mist_shoal={len(opus_out.get('mist_shoal_picks',[]))}, "
          f"sgv_ideas={len(opus_out.get('sgv_ideas',[]))} mist_ideas={len(opus_out.get('mist_ideas',[]))}, "
          f"hot_qt_picks={len(opus_out.get('hot_qt_picks',[]))}")
    print(f"  usage: {usage}")
    print(f"  cost: ${cost:.4f}")

    # SGV INSIGHTS: distill 7d of fund-internal data (Fireflies + Notion + admin emails)
    # into anonymized viral tweet drafts. Separate Sonnet call, ~$0.06/run.
    print("  calling Sonnet for SGV INSIGHTS...")
    insights_result = None
    insights_cost = 0
    try:
        import insights as _ins_mod
        insights_result = _ins_mod.run(dry_run=args.dry_run, verbose=True)
        insights_cost = (insights_result or {}).get("cost_usd", 0) or 0
        sgv_n = len(insights_result.get("insights_sgv", []) or [])
        mist_n = len(insights_result.get("insights_mist", []) or [])
        print(f"  insights returned: sgv={sgv_n} mist={mist_n} | cost: ${insights_cost:.4f}")
    except Exception as e:
        print(f"  insights FAILED (non-fatal): {e}")
        insights_result = {"insights_sgv": [], "insights_mist": [],
                           "errors": [f"insights module crashed: {e}"], "cost_usd": 0}

    # Generate Sonnet drafts for any ingested good_content rows that don't have drafts yet.
    # Skipped entirely on --dry-run: it spends Sonnet AND writes drafts to feedback.db.
    gc_cost = 0.0
    if args.dry_run:
        print("  good_content drafting: skipped (--dry-run)")
    else:
        print("  generating good_content drafts...")
        try:
            import good_content as _gc_mod
            gc_result = _gc_mod.run(limit=3, verbose=True)
            gc_cost = (gc_result or {}).get("cost_usd", 0) or 0
            print(f"  good_content: drafted={gc_result.get('drafted',0)} cost=${gc_cost:.4f}")
        except Exception as e:
            print(f"  good_content drafter FAILED (non-fatal): {e}")

    # Call-drafts lane (optional; FIREFLIES_API_KEY-gated; personal-only)
    call_result = None
    cd_cost = 0.0
    try:
        import call_drafts as _cd_mod
        call_result = _cd_mod.run(dry_run=args.dry_run, verbose=True)
        cd_cost = (call_result or {}).get("cost_usd", 0) or 0
        n_calls = len((call_result or {}).get("call_drafts") or [])
        print(f"  call_drafts: {n_calls} calls drafted | cost ${cd_cost:.4f}")
    except Exception as e:
        print(f"  call_drafts FAILED (non-fatal): {e}")

    picks_by_id = {t["tweet_id"]: t for t in candidates}
    delivery_stats = deliver_digest_dual(picks_by_id, opus_out, shoal, insights_result, args.dry_run,
                                         call_result=call_result)

    duration_s = time.time() - t0
    sgv_s = delivery_stats["sgv"]
    mist_s = delivery_stats["mist"]
    total_sent = sgv_s["sent"] + mist_s["sent"]
    total_fail = sgv_s["send_failures"] + mist_s["send_failures"]

    # Footer goes to BOTH channels
    sgv_section = delivery_stats.get("sgv_section_counts", {})
    mist_section = delivery_stats.get("mist_section_counts", {})
    insights_count_sgv = sgv_section.get("insights", 0)
    insights_count_mist = mist_section.get("insights", 0)
    total_cost = cost + (insights_cost or 0)
    footer = (
        f"📊 sgv sections: ideas={sgv_section.get('tweet_ideas',0)}/"
        f"hotqt={sgv_section.get('hot_qts',0)}/"
        f"insights={insights_count_sgv}/gc={sgv_section.get('good_content',0)} · "
        f"mist sections: ideas={mist_section.get('tweet_ideas',0)}/"
        f"hotqt={mist_section.get('hot_qts',0)}/"
        f"insights={insights_count_mist}/gc={mist_section.get('good_content',0)}/"
        f"calls={mist_section.get('call_drafts',0)} · "
        f"sent sgv={sgv_s['sent']} mist={mist_s['sent']} fail={total_fail} · "
        f"Opus ${cost:.3f} + Sonnet ${insights_cost + cd_cost:.3f} · {duration_s:.0f}s"
    )
    if not args.dry_run:
        # Fund group footer via user session (group send)
        try:
            subprocess.run(["bash", SEND_SCRIPT, DELIVERY_CHAT_SGV, footer], check=False, timeout=20)
        except Exception as e:
            print(f"  footer SGV send failed: {e}")
        # Personal DM footer via the bot
        try:
            _send_via_bot(DELIVERY_CHAT_MIST, footer)
        except Exception as e:
            print(f"  footer Mist send failed: {e}")
    else:
        print(f"[DRY → footer] {footer}")

    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tweets_scored": len(filtered),
        "candidates_to_opus": len(candidates),
        "shoal_msgs": len(shoal),
        "picks": len(opus_out.get("picks", [])),
        "hot_qt_picks": len(opus_out.get("hot_qt_picks", [])),
        "sgv_ideas": len(opus_out.get("sgv_ideas", [])),
        "mist_ideas": len(opus_out.get("mist_ideas", [])),
        "sgv_shoal_picks": len(opus_out.get("sgv_shoal_picks", [])),
        "mist_shoal_picks": len(opus_out.get("mist_shoal_picks", [])),
        "sent_sgv": sgv_s["sent"],
        "sent_mist": mist_s["sent"],
        "send_failures": total_fail,
        "duration_s": round(duration_s, 1),
        "dry_run": args.dry_run,
        "opus_usage": usage,
        "opus_cost_usd": round(cost, 4),
        "insights_sgv_count":  insights_count_sgv,
        "insights_mist_count": insights_count_mist,
        "insights_cost_usd":   round(insights_cost or 0, 4),
        "insights_source_counts": (insights_result or {}).get("source_counts", {}),
        "call_drafts": len((call_result or {}).get("call_drafts") or []),
        "call_drafts_cost_usd": round(cd_cost or 0, 4),
        "sgv_section_counts":  sgv_section,
        "mist_section_counts": mist_section,
    }

    if not args.dry_run:
        os.makedirs(os.path.dirname(STATS_PATH), exist_ok=True)
        with open(STATS_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")

    print(f"== DONE in {duration_s:.1f}s | sgv={sgv_s['sent']} mist={mist_s['sent']} failed={total_fail} | Opus ${cost:.3f} + Sonnet ${insights_cost:.3f} (total ${total_cost:.3f}) ==")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
