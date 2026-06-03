#!/usr/bin/env python3
"""Sonnet draft generator for good_content items.

Each call surfaces up to 2 items for the digest:
  - 1 newly-ingested item that hasn't been drafted yet (status='ingested')
  - 1 randomly-picked item from the library that has been surfaced before
    (status='drafted' or 'sent', uniform random)

For each picked item, Sonnet is called fresh — the existing draft (if any)
is replaced via save_good_content_draft's idempotent INSERT. After drafting,
reminded_at is cleared so digest.py's section3 query picks the item up.

Both voices (fund + personal) are drafted for every item.

Cost: ~$0.007 per item, so ~$0.014 per digest run.
"""
import os, sys, json, re, datetime, random

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import feedback_db as db


# ─────────────────────────────────────────
# PATHS / CONFIG (env-derived, no hardcoded home)
# ─────────────────────────────────────────
def _skill_dir():
    env = os.environ.get("CLAUDE_SKILL_DIR")
    if env:
        return env
    return os.path.dirname(HERE)  # scripts/ lives under the skill root


def _state_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(base, "sgv-tweet-digest")


def _voice_profiles_path():
    """Resolve voice_profiles.json: $SGV_VOICE_PROFILES -> <state>/ -> shipped seed."""
    for p in (
        os.environ.get("SGV_VOICE_PROFILES"),
        os.path.join(_state_dir(), "voice_profiles.json"),
        os.path.join(_skill_dir(), "voice_profiles.json"),
    ):
        if p and os.path.exists(p):
            return p
    return os.path.join(_skill_dir(), "voice_profiles.json")


def _load_app_config():
    for p in (
        os.environ.get("SGV_CONFIG"),
        os.path.join(_state_dir(), "config.json"),
        os.path.join(_skill_dir(), "config.example.json"),
    ):
        if p and os.path.exists(p):
            try:
                with open(p) as f:
                    return json.load(f)
            except Exception:
                continue
    return {}


_CONFIG = _load_app_config()
_MODELS = _CONFIG.get("models", {}) if isinstance(_CONFIG, dict) else {}
SONNET_MODEL = _MODELS.get("sonnet", "claude-sonnet-4-5-20250929")
VOICE_PATH = _voice_profiles_path()

MAX_TEXT_CHARS = 12_000  # ~3K tokens of input per item
DEFAULT_LIMIT = 3  # legacy; new logic always surfaces 1 new + 1 random old

# Handles used to label the two voices in the drafting prompt. Non-secret brand
# identity; overridable via config so the skill isn't hardcoded to one fund.
SGV_HANDLE = _CONFIG.get("sgv_username", "socialgraphvc")
PERSONAL_HANDLE = _CONFIG.get("personal_username", "0x_Mist")


def _capitalize_mist(text):
    """Personal-voice grammar pass: capitalize first letter of the tweet and first
    letter after each '. '. Standalone 'i' stays lowercase. URLs skipped."""
    if not text:
        return text
    text = re.sub(
        r'^(\W*)([a-z])(?!ttps?://)',
        lambda m: m.group(1) + m.group(2).upper(),
        text, count=1,
    )
    text = re.sub(
        r'(\.\s+)([a-z])(?!ttps?://)',
        lambda m: m.group(1) + m.group(2).upper(),
        text,
    )
    return text


def _load_voice(key):
    try:
        with open(VOICE_PATH) as f:
            return json.load(f).get(key, {})
    except Exception:
        return {}


def _voice_block(key, handle):
    v = _load_voice(key)
    summary = v.get("voice_summary", "")
    do_list = "\n".join(f"  • {x}" for x in (v.get("do") or [])[:5])
    dont_list = "\n".join(f"  • {x}" for x in (v.get("dont") or [])[:5])
    examples = "\n".join(f"  • {x}" for x in (v.get("best_examples") or [])[:3])
    return f"""=== {handle} VOICE ===
{summary}

DO:
{do_list}

DON'T:
{dont_list}

REPRESENTATIVE PAST TWEETS:
{examples}"""


def _build_system_prompt():
    sgv_b = _voice_block("sgv", f"@{SGV_HANDLE} (fund)")
    mist_b = _voice_block("mist", f"@{PERSONAL_HANDLE} (personal)")
    return f"""You are helping a founding partner at Social Graph Ventures (SGV)
distill an external piece of content (article, YouTube transcript, or tweet) that
they flagged as worth sharing. The thesis: consumer crypto, tokenization, agent
infrastructure, stablecoin distribution.

Your job for EACH piece of content:
  1. Infer in ONE sentence why this was likely flagged. Match it to the thesis.
  2. Write ONE tweet draft in @{SGV_HANDLE} voice (fund, measured, pattern-claim)
  3. Write ONE tweet draft in @{PERSONAL_HANDLE} voice (personal, sharper, contrarian-friendly)

Each draft:
  - ≤280 chars TOTAL including the URL
  - End with the URL so readers can click through
  - Add a POV (a take, question, or framing) — not a passive share
  - Drive engagement: question, contrarian claim, or non-obvious takeaway
  - NEVER use the em dash character. Use periods, commas, or 'and'.
  - No surrounding quotes. Copy-paste ready.

ANONYMIZATION: the only brand allowed in your output is "SGV" / "Social Graph Ventures".
If the content names specific founders / companies, you may reference them
naturally (they're already public), but don't fabricate.

{sgv_b}

{mist_b}

OUTPUT FORMAT (strict JSON, nothing else):
{{
  "why_david_liked": "1 sentence",
  "draft_sgv": "tweet text ending with URL",
  "draft_mist": "tweet text ending with URL",
  "viral_hooks_sgv": ["concrete_number"],
  "viral_hooks_mist": ["contrarian","concrete_number"]
}}

`viral_hooks_sgv` and `viral_hooks_mist` are REQUIRED — each a JSON array of
1-3 tags ordered by dominance (first = primary_hook). Canonical vocabulary
(use ONLY these values):
  - "concrete_number"  → has a specific $, %, Nx, count, or hard number
  - "contrarian"       → takes a position against consensus
  - "pattern"          → identifies a trend
  - "question"         → opens with or contains a real reader-facing question
  - "no_hook"          → none of the above (use sparingly)"""


def _sonnet_draft(client, system_prompt, content_row):
    """One Sonnet call for one good_content item."""
    text = (content_row.get("full_text") or "")[:MAX_TEXT_CHARS]
    title = content_row.get("title") or "(no title)"
    author = content_row.get("author") or "(unknown)"
    url = content_row.get("url") or ""
    kind = content_row.get("source_kind") or "article"
    note = content_row.get("david_note") or ""

    user_msg = (
        f"CONTENT KIND: {kind}\n"
        f"URL: {url}\n"
        f"TITLE: {title}\n"
        f"AUTHOR: {author}\n"
        + (f"FLAG NOTE: {note}\n" if note else "")
        + f"\nBODY:\n{text}\n\nReturn the JSON, nothing else."
    )

    resp = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=1200,
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?|\n?```$", "", raw)
    parsed = json.loads(raw)

    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    cost = (
        usage["input_tokens"] * 3 / 1_000_000
        + usage["output_tokens"] * 15 / 1_000_000
        + usage["cache_creation_input_tokens"] * 3.75 / 1_000_000
        + usage["cache_read_input_tokens"] * 0.30 / 1_000_000
    )
    return parsed, usage, cost


def _pick_items(c):
    """Return up to 2 items for today's digest: 1 newly-ingested + 1 random old.

    Each item is dict-ified with an extra 'is_new' key. Order: [new, old].
    """
    picks = []

    new_row = c.execute("""
        SELECT * FROM good_content gc
        WHERE gc.status = 'ingested'
          AND NOT EXISTS (SELECT 1 FROM good_content_drafts d WHERE d.content_id = gc.id)
        ORDER BY gc.submitted_at ASC
        LIMIT 1
    """).fetchone()
    if new_row:
        d = dict(new_row); d["is_new"] = True
        picks.append(d)

    # Uniformly random pick from the library — anything previously surfaced
    # OR drafted-but-not-yet-surfaced (excluding the one we just picked above).
    exclude_id = picks[0]["id"] if picks else -1
    old_row = c.execute("""
        SELECT * FROM good_content gc
        WHERE gc.status IN ('drafted', 'sent', 'ingested')
          AND gc.full_text IS NOT NULL AND gc.full_text <> ''
          AND gc.id <> ?
        ORDER BY RANDOM()
        LIMIT 1
    """, (exclude_id,)).fetchone()
    if old_row:
        d = dict(old_row); d["is_new"] = False
        picks.append(d)

    return picks


def run(limit=DEFAULT_LIMIT, verbose=True):
    """Surface 1 newly-ingested + 1 random old good_content item for the digest.

    `limit` is kept for backwards compat but ignored — count is fixed at 1+1.
    """
    log = print if verbose else (lambda *a, **k: None)
    t0 = datetime.datetime.now()
    log(f"== good-content drafter {t0.isoformat()} ==")

    db.init_schema()
    c = db._conn()

    picks = _pick_items(c)
    if not picks:
        log("  no good_content rows in library (need at least 1 ingested URL)")
        return {"drafted": 0, "resurfaced": 0, "failed": 0, "cost_usd": 0, "duration_s": 0}

    # Anthropic client
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY not set in environment")
    client = anthropic.Anthropic(api_key=api_key)

    system_prompt = _build_system_prompt()

    drafted = 0       # net-new items
    resurfaced = 0    # library items re-drafted
    failed = 0
    total_cost = 0.0

    for row in picks:
        kind_tag = "NEW" if row["is_new"] else "RESURFACE"
        log(f"  gc#{row['id']} ({kind_tag}) ({row.get('source_kind', '?')}) {(row.get('title') or row['url'])[:60]}")
        try:
            parsed, usage, cost = _sonnet_draft(client, system_prompt, row)
        except Exception as e:
            log(f"    -> FAILED: {e}")
            if row["is_new"]:
                # only mark net-new items as failed; resurfaced items keep their prior status
                db.set_good_content_status(row["id"], "failed", ingest_error=f"draft failed: {e}")
            failed += 1
            continue

        why = (parsed.get("why_david_liked") or "").strip()
        draft_sgv = (parsed.get("draft_sgv") or "").strip()
        draft_mist = (parsed.get("draft_mist") or "").strip()
        hooks_sgv = parsed.get("viral_hooks_sgv") or []
        hooks_mist = parsed.get("viral_hooks_mist") or []
        # Personal voice: fix sentence-start capitalization before saving
        if draft_mist:
            draft_mist = _capitalize_mist(draft_mist)

        if draft_sgv:
            db.save_good_content_draft(row["id"], "sgv", why, draft_sgv, viral_hooks=hooks_sgv)
        if draft_mist:
            db.save_good_content_draft(row["id"], "mist", why, draft_mist, viral_hooks=hooks_mist)

        # Move to 'drafted' and clear reminded_at so section3 picks this up today
        c.execute(
            "UPDATE good_content SET status='drafted', reminded_at=NULL WHERE id=?",
            (row["id"],),
        )

        total_cost += cost
        if row["is_new"]:
            drafted += 1
        else:
            resurfaced += 1
        log(f"    -> {kind_tag} drafted (sgv={len(draft_sgv)}c, mist={len(draft_mist)}c, cost=${cost:.4f})")

    duration = (datetime.datetime.now() - t0).total_seconds()
    log(f"== DONE in {duration:.1f}s | new={drafted} resurfaced={resurfaced} failed={failed} cost=${total_cost:.4f} ==")
    return {
        "drafted": drafted,
        "resurfaced": resurfaced,
        "failed": failed,
        "cost_usd": round(total_cost, 4),
        "duration_s": round(duration, 1),
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()
    result = run(limit=args.limit, verbose=not args.quiet)
    print(json.dumps(result, indent=2))
