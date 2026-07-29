#!/usr/bin/env python3
"""SGV INSIGHTS — distill last 7d of fund-internal data into 3 anonymized viral
tweet drafts in EACH of two voices (fund + personal).

Data sources — ALL OPTIONAL, each independently gated and skipped (with a logged
note, never a crash) when its prerequisites are absent:
  1. Fireflies transcripts cache  — active only if the transcript-cache dir exists
                                     (SGV_FIREFLIES_CACHE_DIR, default <state>/fireflies/)
  2. Notion deals pool             — active only if NOTION_TOKEN + NOTION_DEALS_DB_ID
                                     are both set (statuses: New Leads / Qualified Leads / Discussion)
  3. Weekly-update emails          — active only if GMAIL_ACCOUNT is set AND the gog
                                     CLI is on PATH (or GOG_BIN points at it); filtered
                                     to from:$SGV_ADMIN_EMAIL subject:Weekly, last 7d

Pipeline:
  fetch → anonymize → Sonnet distill → save snapshot → return for delivery

Secrets / config from the environment:
  - ANTHROPIC_API_KEY   (Sonnet distill)
  - NOTION_TOKEN, NOTION_DEALS_DB_ID  (optional Notion source — read by notion_client)
  - GMAIL_ACCOUNT, SGV_ADMIN_EMAIL    (optional Gmail source)

Per-run cost: ~$0.10 (Sonnet input ~17K tok + output ~1.5K tok)
"""
import os, sys, json, re, subprocess, datetime, shutil, glob as _glob
from pathlib import Path

# ─────────────────────────────────────────
# PATHS / CONFIG (env-derived, no hardcoded home)
# ─────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
# Make the vendored notion_client + feedback_db importable as siblings.
sys.path.insert(0, HERE)


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


def _env_path(name, default):
    """Env-sourced path with $VAR/~ expansion (systemd EnvironmentFile does not
    expand $HOME — a literal '$HOME/...' value must still resolve here)."""
    v = os.environ.get(name)
    if not v:
        return default
    return os.path.expanduser(os.path.expandvars(v))


def _voice_profiles_path():
    for p in (
        _env_path("SGV_VOICE_PROFILES", None),
        os.path.join(_state_dir(), "voice_profiles.json"),
        os.path.join(_skill_dir(), "voice_profiles.json"),
    ):
        if p and os.path.exists(p):
            return p
    return os.path.join(_skill_dir(), "voice_profiles.json")


def _denylist_path():
    """The operator's REAL anonymize_denylist.txt (gitignored) if present, else
    the shipped fake-name example. Resolution:
      $SGV_ANONYMIZE_DENYLIST -> <state>/anonymize_denylist.txt
      -> <skill>/anonymize_denylist.txt -> <skill>/anonymize_denylist.example.txt
    """
    for p in (
        _env_path("SGV_ANONYMIZE_DENYLIST", None),
        os.path.join(_state_dir(), "anonymize_denylist.txt"),
        os.path.join(_skill_dir(), "anonymize_denylist.txt"),
        os.path.join(_skill_dir(), "anonymize_denylist.example.txt"),
    ):
        if p and os.path.exists(p):
            return p
    return os.path.join(_skill_dir(), "anonymize_denylist.example.txt")


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


_CONFIG = _load_app_config()
_MODELS = _CONFIG.get("models", {}) if isinstance(_CONFIG, dict) else {}
SONNET_MODEL = _MODELS.get("sonnet", "claude-sonnet-4-5-20250929")

VOICE_PATH = _voice_profiles_path()
DENYLIST_PATH = _denylist_path()
SNAPSHOT_DIR = _state_dir()

# Fireflies cache dir — activation is purely on this dir/glob existing.
FIREFLIES_CACHE_DIR = _env_path(
    "SGV_FIREFLIES_CACHE_DIR", os.path.join(_state_dir(), "fireflies")
)
TRANSCRIPTS_CACHE_GLOB = os.path.join(FIREFLIES_CACHE_DIR, "transcripts_*.json")

# Gmail (optional). GOG resolved from PATH unless GOG_BIN overrides.
GOG = os.environ.get("GOG_BIN") or shutil.which("gog")
GMAIL_ACCOUNT = os.environ.get("GMAIL_ACCOUNT")
SGV_ADMIN_EMAIL = os.environ.get("SGV_ADMIN_EMAIL")

# Brand handles for the voice labels (non-secret; overridable).
SGV_HANDLE = _CONFIG.get("sgv_username", "socialgraphvc")
PERSONAL_HANDLE = _CONFIG.get("personal_username", "0x_Mist")

DAYS_WINDOW = 7
N_INSIGHTS = 3

# Anti-repetition + timeline-mix (set any to 0 to disable that mechanism):
MEMORY_RUNS = 7          # DO-NOT-REPEAT window, in RUNS not days (robust to run gaps)
DEAL_COOLDOWN_RUNS = 3   # Notion deals featured in the last N runs are hard-excluded
MIN_NOTION_POOL = 8      # never let the cooldown shrink the deal pool below this
TIMELINE_TOP_N = 20      # top-engagement public tweets injected as fresh context


# ─────────────────────────────────────────
# ANONYMIZATION
# ─────────────────────────────────────────
def load_denylist():
    """Load (term, replacement) pairs from denylist file. Longest term first for greedy match."""
    rules = []
    with open(DENYLIST_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if " -> " in line:
                term, repl = line.split(" -> ", 1)
                rules.append((term.strip(), repl.strip()))
            else:
                rules.append((line.strip(), "[redacted]"))
    # Sort by term length descending so multi-word terms match before their prefixes
    rules.sort(key=lambda r: -len(r[0]))
    return rules


_DENYLIST = None


def _denylist():
    global _DENYLIST
    if _DENYLIST is None:
        _DENYLIST = load_denylist()
    return _DENYLIST


# Regex patterns
_DOLLAR_RAISE_RE = re.compile(
    r"\$(\d+(?:\.\d+)?)(M|B|k|K)?\s*(?:raised|raising|round|valuation|allocation|seed|series\s+[A-D])",
    re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{1,15})\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _bucket_dollar(match):
    num = float(match.group(1))
    unit = (match.group(2) or "").lower()
    keyword = match.group(0).split()[-1] if " " in match.group(0) else ""
    if unit == "b":
        return f"[a >$1B context]"
    if unit == "m":
        if num >= 100:
            return f"[>$100M valuation]"
        elif num >= 25:
            return f"[a $25-100M round]"
        elif num >= 5:
            return f"[a $5-25M Series A]"
        else:
            return f"[a <$5M seed]"
    if unit == "k" or num < 5:
        return f"[a small allocation]"
    return "[a deal-stage amount]"


def anonymize(text):
    """Apply denylist + regex anonymization to a single string."""
    if not text:
        return text
    s = text
    # 1. Denylist (longest match first; case-insensitive substring)
    for term, repl in _denylist():
        # Word-boundary match to avoid partial-word replacement (e.g. a term inside a longer word)
        pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
        s = pattern.sub(repl, s)
    # 2. Dollar amounts in deal context
    s = _DOLLAR_RAISE_RE.sub(_bucket_dollar, s)
    # 3. @handles → [a handle]   (skip if already inside brackets)
    s = _HANDLE_RE.sub(lambda m: "[a handle]", s)
    # 4. Emails → [email redacted]
    s = _EMAIL_RE.sub("[email]", s)
    return s


def anonymize_dict(d):
    """Recursively anonymize all string values in a dict/list."""
    if isinstance(d, str):
        return anonymize(d)
    if isinstance(d, dict):
        return {k: anonymize_dict(v) for k, v in d.items()}
    if isinstance(d, list):
        return [anonymize_dict(x) for x in d]
    return d


# Guard set for the denylist auto-add: deal names that are also common English
# words must NOT be auto-denylisted (redacting the word "ground" everywhere would
# wreck every draft). Such names get a loud warning for manual handling instead.
_COMMON_WORD_GUARD = {
    "ground", "crush", "prized", "peso", "tomorrow", "foundry", "pulse", "seam",
    "berry", "pluto", "atlas", "nova", "prime", "spark", "wave", "echo", "drift",
    "forge", "quest", "scout", "amber", "basis", "bloom", "cedar", "delta",
    "ember", "flint", "haven", "index", "lever", "meter", "oasis", "orbit",
    "pilot", "quill", "ridge", "slate", "summit", "tide", "vault", "zephyr",
}


def auto_add_denylist_terms(deal_names, log):
    """Auto-append NEW Notion deal names to the ACTIVE operator denylist so a raw
    deal name can never reach a draft or inspo label (this leaked repeatedly when
    additions were manual). Never touches the shipped .example file. Returns the
    number of terms added; resets the in-memory rules so THIS run is covered."""
    global _DENYLIST
    if DENYLIST_PATH.endswith(".example.txt"):
        return 0  # the safety gate in run() refuses internal sources anyway
    existing = {t.lower() for t, _ in _denylist()}
    added, skipped = [], []
    for name in deal_names:
        name = (name or "").strip()
        low = name.lower()
        if not name or low in existing:
            continue
        if len(name) < 4 or low in _COMMON_WORD_GUARD:
            skipped.append(name)
            continue
        added.append(f"{name} -> [a startup]")
        existing.add(low)
    if skipped:
        log(f"  !! denylist auto-add SKIPPED (common-word/short names — handle manually): {skipped}")
    if not added:
        return 0
    with open(DENYLIST_PATH, "a") as f:
        f.write("\n# auto-added by insights.py (new Notion deals)\n" + "\n".join(added) + "\n")
    _DENYLIST = None  # reload so this run's anonymization already covers them
    log(f"  ++ denylist auto-add: {[a.split(' -> ')[0] for a in added]}")
    return len(added)


def denylist_only(text):
    """Denylist pass WITHOUT the handle/email/dollar regexes — for PUBLIC timeline
    tweets: a denylisted portfolio company trending publicly must not hand Sonnet
    the real name, but public @handles and figures stay as-is."""
    if not text:
        return text
    s = text
    for term, repl in _denylist():
        s = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE).sub(repl, s)
    return s


# ─────────────────────────────────────────
# OUTPUT POST-PROCESS (safety net for Sonnet's drafts)
# ─────────────────────────────────────────

# Detect "<Capitalized name> <role>"  → abstract the name
# Example matches: "<Co> cofounder", "<Co> exec", "<Co>'s engineer"
_NAMED_ROLE_RE = re.compile(
    r"\b([A-Z][a-zA-Z0-9]{2,})('s)?\s+"
    r"(co[\- ]?founder|founder|CEO|CTO|COO|CFO|exec(?:utive)?|engineer|lead|VP|"
    r"head\s+of\s+\w+|president|partner)s?\b"
)

# Words that can precede a role legitimately (not company names)
_NAMED_ROLE_ALLOWLIST = {
    "sgv", "social", "solo", "first", "second", "third", "former", "ex",
    "serial", "stealth", "anonymous", "crypto", "defi", "web3", "consumer",
    "infra", "ai", "the", "this", "that", "our", "their", "his", "her",
    "young", "veteran", "new", "old", "top", "early", "late",
}


def _abstract_named_roles(text):
    """Replace '<Co> cofounder' / '<Co> exec' → 'a top crypto-industry cofounder/exec'.
    Cleans up double-articles ('an a top ...') afterward.
    """
    def repl(m):
        word = m.group(1)
        role = m.group(3).lower().replace(" ", "-")
        # Skip benign descriptors and our own brand
        if word.lower() in _NAMED_ROLE_ALLOWLIST:
            return m.group(0)
        # Normalize the role
        if role.startswith("co"):
            role_clean = "cofounder"
        elif role in ("ceo", "cto", "coo", "cfo", "vp"):
            role_clean = role.upper()
        elif role.startswith("head-of"):
            role_clean = role.replace("-", " ")
        elif role == "executive":
            role_clean = "exec"
        else:
            role_clean = role
        return f"a top crypto-industry {role_clean}"

    text = _NAMED_ROLE_RE.sub(repl, text)
    # Clean up "an a top" / "the a top" / "a a top" double-articles
    text = re.sub(
        r"\b(an?|the)\s+a\s+top\s+crypto-industry\b",
        "a top crypto-industry",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _strip_brackets(text):
    """Drop [...] bracket placeholders, keep the prose inside.
    Handles nested brackets via repeated passes (innermost first).
    '[a yield-infra co]' → 'a yield-infra co'.
    '[an [the fund] partner]' → 'an the fund partner'.
    '[redacted]' → '' (drop entirely so we don't ship literal 'redacted' in a tweet).
    """
    def repl(m):
        inner = m.group(1).strip()
        if inner.lower() == "redacted":
            return ""
        return inner
    prev = text
    for _ in range(6):
        cur = re.sub(r"\[([^\[\]]+)\]", repl, prev)
        if cur == prev:
            break
        prev = cur
    return prev


def postprocess_draft(text):
    """Sanitize a Sonnet-produced tweet draft. Strip brackets and abstract any leaked
    named-company-role descriptors."""
    if not text:
        return text
    text = _strip_brackets(text)
    text = _abstract_named_roles(text)
    # Collapse any double-spaces introduced
    text = re.sub(r"  +", " ", text).strip()
    return text


# ─────────────────────────────────────────
# SOURCE ATTRIBUTION
# ─────────────────────────────────────────
_REF_RE = re.compile(r"(fireflies|notion|emails?|timeline)\[(\d+)\]")
_KIND_TO_KEY = {"fireflies": "fireflies", "notion": "notion", "email": "emails",
                "emails": "emails", "timeline": "timeline"}


def _resolve_source_refs(refs, clean_payload):
    """Given source_refs like ['fireflies[2]','notion[5]','email[0]'], look up
    the anonymized label + snippet from the cleaned payload. Returns
    (label, snippet). Labels have brackets stripped + duplicates collapsed."""
    if not refs:
        return ("fund-internal data", "")
    parts = []
    seen_parts = set()
    snippets = []
    for ref in refs[:4]:
        m = _REF_RE.match(ref.strip())
        if not m:
            continue
        kind = _KIND_TO_KEY.get(m.group(1), m.group(1))
        idx_s = int(m.group(2))
        bucket = clean_payload.get(kind, [])
        if idx_s >= len(bucket):
            continue
        item = bucket[idx_s] or {}
        if kind == "fireflies":
            title = _strip_brackets((item.get("title") or "a call").strip())[:60]
            label = f"{title} call"
            snippet = _strip_brackets(item.get("short_summary") or "")[:200]
        elif kind == "notion":
            name = _strip_brackets((item.get("name") or "a deal").strip())[:40]
            status = (item.get("status") or "").lower()
            label = f"{name} ({status})" if status else name
            snippet = _strip_brackets(item.get("page_body") or "")[:200]
        elif kind == "emails":
            subj = _strip_brackets(item.get("subject") or "a weekly memo")[:50]
            label = subj
            snippet = _strip_brackets(item.get("body") or "")[:200]
        elif kind == "timeline":
            author = (item.get("author") or "?")
            label = f"{author} on X"
            snippet = _strip_brackets(item.get("text") or "")[:200]
        else:
            continue
        # Collapse whitespace and dedupe
        label = re.sub(r"\s+", " ", label).strip()
        key = label.lower()
        if key in seen_parts:
            continue
        seen_parts.add(key)
        parts.append(label)
        if snippet:
            snippets.append(snippet)
    if not parts:
        return ("fund-internal data", "")
    return (" + ".join(parts), " | ".join(snippets)[:300])


# ─────────────────────────────────────────
# FETCH: Fireflies (cache, last 7d) — OPTIONAL, gated on cache dir existing
# ─────────────────────────────────────────
def fetch_fireflies_last_7d():
    """Read latest Fireflies cache, filter to last 7 days. Returns slim list of summaries.

    Skipped (returns []) unless the transcript-cache dir exists and holds a
    transcripts_*.json file.
    """
    if not os.path.isdir(FIREFLIES_CACHE_DIR):
        return []
    files = sorted(_glob.glob(TRANSCRIPTS_CACHE_GLOB))
    if not files:
        return []
    latest = files[-1]
    with open(latest) as f:
        data = json.load(f)
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=DAYS_WINDOW)
    out = []
    for t in data:
        # Date is epoch ms
        dt_v = t.get("date")
        if not isinstance(dt_v, (int, float)):
            continue
        dt = datetime.datetime.fromtimestamp(dt_v / 1000, tz=datetime.timezone.utc)
        if dt < cutoff:
            continue
        summary = t.get("summary") or {}
        out.append({
            "date": dt.strftime("%Y-%m-%d"),
            "title": t.get("title", ""),
            "duration_min": t.get("duration", 0),
            "organizer": t.get("organizer_email", ""),
            "short_summary": (summary.get("short_summary") or "")[:1500],
            "keywords": summary.get("keywords") or [],
            "action_items": (summary.get("action_items") or "")[:600],
        })
    return out


# ─────────────────────────────────────────
# FETCH: Notion (deals pool, statuses) — OPTIONAL, gated on NOTION_TOKEN + NOTION_DEALS_DB_ID
# ─────────────────────────────────────────
def fetch_notion_deals(top_n=15, body_for_top=5, exclude_ids=None, exclude_names=None):
    """Query the deals pool, filter to active statuses, sort by last_edited desc.

    exclude_ids / exclude_names: Notion deals featured in recent insight runs
    (cooldown). Excluding them BETWEEN the sort and the top_n slice is the point —
    deals beyond the usual top-15 rotate IN instead of the pool shrinking. The
    cooldown is skipped entirely if it would leave fewer than MIN_NOTION_POOL deals.

    Skipped (returns ([], note)) unless both NOTION_TOKEN and NOTION_DEALS_DB_ID
    are set.
    """
    if not (os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_DEALS_DB_ID")):
        return [], "notion skipped: NOTION_TOKEN / NOTION_DEALS_DB_ID not set"
    try:
        from notion_client import query_deals_pool, extract_deal_summary, fetch_page_body
    except Exception as e:
        return [], f"notion import failed: {e}"
    # Honor the same NOTION_KEY_COLUMNS override the query layer uses, so a
    # custom funnel isn't fetched by notion_client only to be dropped here.
    key_cols = os.environ.get("NOTION_KEY_COLUMNS")
    if key_cols:
        active_statuses = {c.strip() for c in key_cols.split(",") if c.strip()}
    else:
        active_statuses = {"New Leads", "Qualified Leads", "Discussion"}
    try:
        pages = query_deals_pool()
    except Exception as e:
        return [], f"notion query failed: {e}"
    deals = []
    for p in pages:
        try:
            summary = extract_deal_summary(p)
        except Exception:
            continue
        if summary.get("status") not in active_statuses:
            continue
        deals.append(summary)
    # Sort by last_edited desc
    deals.sort(key=lambda d: d.get("last_edited") or "", reverse=True)
    # Cooldown rotation (between sort and slice — see docstring).
    if exclude_ids or exclude_names:
        ex_ids = set(exclude_ids or ())
        ex_names = {n.lower() for n in (exclude_names or ())}
        kept = [d for d in deals
                if d.get("notion_page_id") not in ex_ids
                and (d.get("name") or "").strip().lower() not in ex_names]
        if len(kept) >= MIN_NOTION_POOL:
            deals = kept
        # else: pool too small — cooldown skipped this run (starvation guard)
    deals = deals[:top_n]
    # Fetch body for top N
    for d in deals[:body_for_top]:
        try:
            body = fetch_page_body(d["notion_page_id"])
            d["page_body"] = body[:3000] if body else ""
        except Exception as e:
            d["page_body"] = ""
            d["_body_err"] = str(e)
    return deals, None


# ─────────────────────────────────────────
# FETCH: weekly-update emails (gog CLI) — OPTIONAL, gated on GMAIL_ACCOUNT + gog
# ─────────────────────────────────────────
def _gog(*args, timeout=30):
    """Run a gog subcommand. Returns (stdout, stderr, returncode)."""
    cmd = [GOG, *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.stderr, r.returncode


def fetch_weekly_emails(days=DAYS_WINDOW):
    """Search Gmail for 'from:$SGV_ADMIN_EMAIL subject:Weekly' in the last `days`,
    fetch full body for each via `gog gmail get <id>`.

    Skipped (returns ([], note)) unless GMAIL_ACCOUNT is set and the gog CLI is
    available. SGV_ADMIN_EMAIL is the sender filter (required when GMAIL_ACCOUNT set).
    """
    if not GMAIL_ACCOUNT:
        return [], "gmail skipped: GMAIL_ACCOUNT not set"
    if not GOG:
        return [], "gmail skipped: gog CLI not found on PATH (set GOG_BIN to override)"
    if not SGV_ADMIN_EMAIL:
        return [], "gmail skipped: SGV_ADMIN_EMAIL (sender filter) not set"

    query = f"from:{SGV_ADMIN_EMAIL} subject:Weekly newer_than:{days}d"

    out, err1, rc1 = _gog("gmail", "search", query, "-a", GMAIL_ACCOUNT, "--limit", "10", "-j", "--results-only", timeout=45)
    if rc1 != 0:
        # Try fallback without -a (use default account)
        out, err2, rc2 = _gog("gmail", "search", query, "--limit", "10", "-j", "--results-only", timeout=45)
        if rc2 != 0:
            # Keep BOTH errors — the fallback's stderr is misleading on its own
            return [], (
                f"gmail search failed: "
                f"first(-a={GMAIL_ACCOUNT}) rc={rc1} stderr={(err1 or '').strip()[:160]} | "
                f"fallback rc={rc2} stderr={(err2 or '').strip()[:160]}"
            )
        err, rc = err2, rc2
    else:
        err, rc = err1, rc1
    try:
        result = json.loads(out)
    except Exception as e:
        return [], f"gmail search json parse failed: {e}"
    msgs = result if isinstance(result, list) else (result.get("messages") or result.get("data") or [])
    if not msgs:
        return [], None

    # Filter for true weekly updates (skip calendar invites)
    filtered = []
    for m in msgs:
        subj = (m.get("subject") or "").lower()
        if "weekly update" in subj or "weekly" in subj.split("(")[0]:
            if "invitation" in subj or "invite" in subj:
                continue
            filtered.append(m)

    # Fetch body for each via gog gmail get
    emails = []
    for m in filtered[:5]:  # cap to 5 emails
        mid = m.get("id") or m.get("messageId")
        if not mid:
            continue
        body_text, _, brc = _gog("gmail", "get", mid, "-a", GMAIL_ACCOUNT, timeout=20)
        if brc != 0:
            body_text, _, brc = _gog("gmail", "get", mid, timeout=20)
        if brc != 0:
            continue
        # gog get prints headers as TSV then a blank line then the body
        body = body_text
        # Extract body after the blank line that separates headers from content
        if "\n\n" in body_text:
            parts = body_text.split("\n\n", 1)
            if len(parts) == 2:
                body = parts[1]
        emails.append({
            "id": mid,
            "subject": m.get("subject", ""),
            "date": m.get("date", ""),
            "body": body[:8000],   # cap to 8K chars per email
        })
    return emails, None


# ─────────────────────────────────────────
# SONNET DISTILL
# ─────────────────────────────────────────
def _load_voice_profile(key="sgv"):
    try:
        with open(VOICE_PATH) as f:
            return json.load(f).get(key, {})
    except Exception:
        return {}


def _voice_block(key, label):
    voice = _load_voice_profile(key)
    voice_summary = voice.get("voice_summary", "")
    do_list = "\n".join(f"  • {x}" for x in (voice.get("do") or [])[:6])
    dont_list = "\n".join(f"  • {x}" for x in (voice.get("dont") or [])[:6])
    examples = "\n".join(f"  • {x}" for x in (voice.get("best_examples") or [])[:4])
    return f"""=== {label} VOICE ({voice.get('handle','')}) ===
{voice_summary}

DO:
{do_list}

DON'T:
{dont_list}

REPRESENTATIVE PAST TWEETS:
{examples}"""


def _feedback_block():
    """Inject 'WHAT'S WORKED RECENTLY' lines if a recent feedback_profile exists."""
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
    if not (do_more or do_less):
        return ""
    parts = ["RECENT PERFORMANCE SIGNAL (lean toward these):"]
    for r in do_more[:5]:
        parts.append(f"  • DO MORE: {r}")
    for r in do_less[:5]:
        parts.append(f"  • DO LESS: {r}")
    return "\n" + "\n".join(parts) + "\n"


def _build_system_prompt():
    sgv_block = _voice_block("sgv", f"@{SGV_HANDLE} (fund)")
    mist_block = _voice_block("mist", f"@{PERSONAL_HANDLE} (personal)")
    feedback = _feedback_block()

    return f"""You are the senior analyst at Social Graph Ventures, a crypto VC fund.
Your job: distill the last 7 days of fund-internal data (anonymized call transcripts,
deal-funnel updates, weekly memos) plus today's PUBLIC TIMELINE into UP TO {N_INSIGHTS}
tweet drafts in EACH of two voices, engineered to go viral on crypto Twitter.

THE PUBLIC TIMELINE section is public context, not internal data: use it to find
FRESH angles and cross-source connections (an internal pattern colliding with what
the timeline is discussing is the best kind of insight). Cite it as timeline[i].
Public figures/companies in timeline tweets follow the same output anonymization
rules below — only SGV may be named in your drafts.
{feedback}

The input has ALREADY been anonymized by a preprocessor (names, companies, dollar
amounts are bucketed). You may notice generic placeholders like [a startup],
[an SGV partner], [a $5-25M Series A]. Treat those as the level of specificity you
can preserve. NEVER reintroduce specific names or numbers that aren't in the input.

ABSOLUTE WRITING RULES:
- Never use the em dash character (the long dash that auto-inserts when typing two hyphens).
  Use periods, commas, parentheses, or 'and' instead. This is absolute.
- Each draft is ≤280 characters
- Copy-paste ready (no surrounding quotes, no commentary, just the tweet text)

ANONYMIZATION OUTPUT RULES (CRITICAL — your tweets are public):
- NEVER include square brackets [ or ] in your output. The bracket tokens in the
  input ([a yield-infra co], [a tier-1 crypto fund], etc.) are anonymization stubs
  meant for you only. Rewrite them as natural prose for the final tweet:
    [a yield-infra co]               → "a yield infrastructure startup"
    [a tier-1 crypto fund]           → "a top-tier crypto fund"
    [a privacy-focused crypto co]    → "a privacy crypto project"
    [a $5-25M Series A]              → "a Series A round" or "a mid-stage round"
    [a stablecoins-adjacent co]      → "a stablecoin-adjacent startup"
    [an SGV partner]                 → "an SGV partner" (SGV is allowed, no brackets)
- NEVER name a person by their role at a specific company. This is identifying:
    WRONG: "an Infura cofounder", "a Coinbase exec", "an Anchorage engineer",
           "a Solana cofounder", "Ethereum's CTO"
    RIGHT: "a top crypto-infrastructure cofounder", "a crypto exchange exec",
           "a custody engineer", "a top L1 cofounder"
- The ONLY brand names allowed in your output are "SGV" / "Social Graph Ventures"
  (our own fund). EVERY other company, protocol, or person must be either anonymized
  to a category or removed entirely.
- If you find yourself about to write "<CapitalizedCompanyName> <role>", stop and
  abstract it to a generic descriptor.

YOU ARE WRITING IN TWO VOICES — produce UP TO {N_INSIGHTS} insights for EACH.
Returning 1-2 genuinely FRESH insights is BETTER than {N_INSIGHTS} that repeat a
burned company+stat from the DO-NOT-REPEAT list (when present). Never pad.

{sgv_block}

{mist_block}

THE SAME UNDERLYING INSIGHTS, REPHRASED PER VOICE. The fund voice is measured +
fund-level; the personal voice is sharper + first-person. The fund and personal
account should NOT post identical tweets, but they can riff on the same underlying
observation from the data.

EACH DRAFT MUST satisfy AT LEAST 3 of these viral hooks:
  • concrete_number — a specific bucket-level metric ("3 of 4 founders this week pitched X")
  • contrarian — flip a popular consensus on its head
  • pattern — a named pattern observed across multiple deals or calls
  • specific_qa — a sharp question or answer that surfaces a real tension founders are facing
  • internal_credibility — implicit "we saw this from inside the fund" tone without
    naming anyone or anything specific

Diversity rule: the {N_INSIGHTS} drafts in each voice must NOT all be about the same topic.
Span: one pattern across deals, one observation from calls, one synthesis from the weekly memo.

SOURCE ATTRIBUTION (required): for each insight, list the INDICES of the source
records that inspired it. Refer to entries in the input by their `idx` field
(which I've added). Example: if a draft was inspired by Fireflies call #2 and
Notion deal #5, source_refs = ["fireflies[2]", "notion[5]"]; a timeline-sparked
insight cites ["timeline[4]"] (plus any internal ref it connects to). Always
include at least one ref.

OUTPUT FORMAT (strict JSON, nothing else — your entire response must be valid JSON):

`viral_hooks` is REQUIRED on every draft — a JSON array of 1-3 tags ordered by
dominance (first = primary_hook). Canonical vocabulary (use ONLY these values):
  - "concrete_number"  → has a specific $, %, Nx, count, or hard number
  - "contrarian"       → takes a position against consensus
  - "pattern"          → identifies a trend
  - "question"         → opens with or contains a real reader-facing question
  - "no_hook"          → none of the above (use sparingly)

{{
  "sgv": [
    {{
      "rank": 1,
      "topic": "1-2 word tag (e.g. 'agent payments' or 'stablecoin distribution')",
      "draft": "<=280 char tweet text in @{SGV_HANDLE} voice",
      "viral_hooks": ["concrete_number","pattern"],
      "source_refs": ["fireflies[2]","notion[5]"]
    }}
    // ...UP TO {N_INSIGHTS} items (fewer beats repeating a burned company+stat)
  ],
  "mist": [
    {{
      "rank": 1,
      "topic": "1-2 word tag",
      "draft": "<=280 char tweet text in @{PERSONAL_HANDLE} voice",
      "viral_hooks": ["contrarian","concrete_number"],
      "source_refs": ["fireflies[2]","notion[5]"]
    }}
    // ...UP TO {N_INSIGHTS} items (fewer beats repeating a burned company+stat)
  ]
}}"""


def sonnet_distill(payload, memory_lines=None):
    """The insights distill call (provider/model per config 'backends.insights').

    memory_lines: recent-run DO-NOT-REPEAT lines — injected into the USER message
    (not the system prompt: that is cache_control'd and must stay static)."""
    import llm_backend

    system_prompt = _build_system_prompt()
    # Annotate each source with an idx field so Sonnet can attribute insights back
    fireflies_indexed = [{"idx": i, **t} for i, t in enumerate(payload.get("fireflies", []))]
    notion_indexed = [{"idx": i, **t} for i, t in enumerate(payload.get("notion", []))]
    emails_indexed = [{"idx": i, **t} for i, t in enumerate(payload.get("emails", []))]
    timeline_indexed = [{"idx": i, **t} for i, t in enumerate(payload.get("timeline", []))]
    user_msg = (
        "Last 7 days of anonymized fund-internal data, plus today's public timeline. "
        "Use the `idx` field on each entry to cite sources in your source_refs output.\n\n"
        f"FIREFLIES CALL SUMMARIES ({len(fireflies_indexed)} meetings):\n"
        f"{json.dumps(fireflies_indexed, indent=2)}\n\n"
        f"NOTION ACTIVE DEALS ({len(notion_indexed)} deals):\n"
        f"{json.dumps(notion_indexed, indent=2)}\n\n"
        f"WEEKLY UPDATE EMAILS ({len(emails_indexed)} emails):\n"
        f"{json.dumps(emails_indexed, indent=2)}\n\n"
        f"PUBLIC TIMELINE (top {len(timeline_indexed)} by engagement today — public "
        f"context for fresh angles; cite as timeline[i]):\n"
        f"{json.dumps(timeline_indexed, indent=2)}\n\n"
    )
    if memory_lines:
        user_msg += (
            f"ALREADY TWEETED IN THE LAST {MEMORY_RUNS} RUNS — DO NOT REPEAT:\n"
            + "\n".join("  - " + l for l in memory_lines)
            + "\nHARD RULES: (1) a company+stat combination appearing above is BURNED — "
            "do not reuse it even reworded. (2) if a payload company matches one above, "
            "find a genuinely NEW angle or SKIP it and build from the PUBLIC TIMELINE "
            "or a cross-source connection instead.\n\n"
        )
    user_msg += "Return the JSON, nothing else."

    raw, usage, cost = llm_backend.chat(
        stage="insights",
        system_text=system_prompt,
        user_text=user_msg,
        max_tokens=6000,  # GLM's thinking burn truncated at 2600 on real runs
        default_model=SONNET_MODEL,
    )
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?|\n?```$", "", raw)
    start = raw.find("{")
    if start < 0:
        raise RuntimeError("insights: no JSON object in model output")
    parsed, _ = json.JSONDecoder().raw_decode(raw[start:])
    return parsed, usage, cost


# ─────────────────────────────────────────
# MEMORY (anti-repetition) + TIMELINE CONTEXT
# ─────────────────────────────────────────
def load_recent_memory(n_runs=MEMORY_RUNS, cooldown_runs=DEAL_COOLDOWN_RUNS):
    """Read the last n_runs insight snapshots.

    Returns (memory_lines, cooldown_ids, cooldown_names):
      memory_lines   — what shipped recently, fed to the prompt as DO-NOT-REPEAT
      cooldown_ids   — notion_page_ids featured in the newest cooldown_runs snapshots
      cooldown_names — anonymized deal names (fallback key when the id is absent)

    Snapshots are written AFTER sanitization (save_snapshot) — that ordering is
    LOAD-BEARING: these lines feed future prompts, so a refactor that snapshots
    pre-anonymization text would leak real names into prompts.

    No snapshots (fresh install) -> empty memory -> today's behavior.
    """
    if n_runs <= 0:
        return [], set(), set()
    files = sorted(_glob.glob(os.path.join(SNAPSHOT_DIR, "insights_snapshot_*.json")))[-n_runs:]
    memory_lines, cooldown_ids, cooldown_names = [], set(), set()
    newest_for_cooldown = set(files[-cooldown_runs:]) if cooldown_runs > 0 else set()
    for path in files:
        try:
            with open(path) as f:
                snap = json.load(f)
        except Exception:
            continue
        date = os.path.basename(path).replace("insights_snapshot_", "").replace(".json", "")
        ins = snap.get("insights") or {}
        items = []
        if isinstance(ins, dict):
            for voice in ("sgv", "mist"):
                for it in ins.get(voice) or []:
                    if isinstance(it, dict):
                        items.append((voice, it))
        elif isinstance(ins, list):  # legacy single-list shape
            items = [("sgv", it) for it in ins if isinstance(it, dict)]
        payload = snap.get("anonymized_payload") or {}
        for voice, it in items:
            memory_lines.append(
                f"[{date} {voice}] topic={it.get('topic') or '?'} | "
                f"inspo={it.get('inspo_label') or '?'} | draft: {(it.get('draft') or '')[:120]}"
            )
            if path in newest_for_cooldown:
                for ref in it.get("source_refs") or []:
                    m = re.match(r"notion\[(\d+)\]", str(ref).strip())
                    if not m:
                        continue
                    idx = int(m.group(1))
                    bucket = payload.get("notion") or []
                    if idx < len(bucket) and isinstance(bucket[idx], dict):
                        pid = bucket[idx].get("notion_page_id")
                        if pid:
                            cooldown_ids.add(pid)
                        nm = (bucket[idx].get("name") or "").strip()
                        if nm:
                            cooldown_names.add(nm.lower())
    seen, deduped = set(), []
    for line in memory_lines:
        if line not in seen:
            seen.add(line)
            deduped.append(line)
    return deduped[-42:], cooldown_ids, cooldown_names


def fetch_timeline_context(top_n=TIMELINE_TOP_N):
    """Top-engagement PUBLIC tweets from today's gather output (latest.json) —
    fresh context so insights can connect internal patterns with what the
    timeline is discussing. $0: the file is already on disk. Missing/empty ->
    [] (today's behavior). Denylist scrubbing happens in run(), not here."""
    if top_n <= 0:
        return []
    path = os.path.expanduser(os.path.expandvars(
        os.environ.get("SGV_OWNED_READS")
        or os.path.join(_state_dir(), "latest.json")))
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return []
    tweets = data.get("list_tweets") or []
    tweets.sort(key=lambda t: -(((t.get("metrics") or {}).get("like_count", 0))
                                + ((t.get("metrics") or {}).get("retweet_count", 0))))
    out, seen_ids = [], set()
    for t in tweets:
        tid = t.get("tweet_id")
        if tid in seen_ids:
            continue
        seen_ids.add(tid)
        m = t.get("metrics") or {}
        out.append({
            "author": t.get("account", ""),
            "text": (t.get("text") or "")[:280],
            "likes": m.get("like_count", 0),
            "rts": m.get("retweet_count", 0),
        })
        if len(out) >= top_n:
            break
    return out


# ─────────────────────────────────────────
# SNAPSHOT (for debug + future trend analysis)
# ─────────────────────────────────────────
def save_snapshot(payload_clean, payload_raw, insights, usage, cost):
    Path(SNAPSHOT_DIR).mkdir(parents=True, exist_ok=True)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    path = f"{SNAPSHOT_DIR}/insights_snapshot_{today}.json"
    snap = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "insights": insights,
        "anonymized_payload": payload_clean,
        # Don't save raw payload to disk for safety; keep counts only
        "raw_counts": {
            "fireflies": len(payload_raw.get("fireflies", [])),
            "notion": len(payload_raw.get("notion", [])),
            "emails": len(payload_raw.get("emails", [])),
        },
        "usage": usage,
        "cost_usd": round(cost, 4),
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f, indent=2)
    os.rename(tmp, path)
    return path


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def run(dry_run=False, verbose=True):
    """Run the SGV INSIGHTS pipeline. Returns dict with insights + metadata.

    Every source is optional: missing prerequisites log a note and contribute no
    data, but never raise. If all three sources are empty, the run returns empty
    insight lists (the digest still ships its other sections)."""
    log = print if verbose else (lambda *a, **k: None)
    t0 = datetime.datetime.now()

    log(f"== SGV INSIGHTS {t0.isoformat()} ==")
    log(f"   dry-run: {dry_run}")

    errors = []

    # SAFETY GATE: never draft from real internal sources with only the shipped
    # FAKE-NAME example denylist — real portfolio/founder names would pass the
    # deterministic anonymizer untouched. Drop in your real anonymize_denylist.txt
    # (see SGV_ANONYMIZE_DENYLIST / the state dir) to enable these sources.
    any_source_configured = (
        os.path.isdir(FIREFLIES_CACHE_DIR)
        or (os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_DEALS_DB_ID"))
        or os.environ.get("GMAIL_ACCOUNT")
    )
    if any_source_configured and DENYLIST_PATH.endswith("anonymize_denylist.example.txt"):
        msg = ("REFUSING to run internal sources: only the shipped EXAMPLE denylist was "
               f"found ({DENYLIST_PATH}). Create your real anonymize_denylist.txt "
               "(state dir or SGV_ANONYMIZE_DENYLIST) so portfolio/founder names get "
               "scrubbed before any draft is written.")
        log(f"  !! {msg}")
        return {"insights_sgv": [], "insights_mist": [], "insights": [],
                "errors": errors + [msg], "cost_usd": 0,
                "source_counts": {"fireflies": 0, "notion": 0, "emails": 0}}

    # Fetch — Fireflies (optional, cache-presence gated)
    log("  [F] Fireflies (last 7d from cache)...")
    fireflies = fetch_fireflies_last_7d()
    if not fireflies and not os.path.isdir(FIREFLIES_CACHE_DIR):
        log(f"    -> skipped: no transcript cache dir at {FIREFLIES_CACHE_DIR}")
    else:
        log(f"    -> {len(fireflies)} transcripts")

    # Anti-repetition memory from recent snapshots (empty on fresh installs)
    memory_lines, cooldown_ids, cooldown_names = load_recent_memory()
    log(f"  [M] memory: {len(memory_lines)} recent-insight lines, "
        f"{max(len(cooldown_ids), len(cooldown_names))} deals on cooldown")

    # Fetch — Notion (optional, env-gated; recently-featured deals rotate out)
    log("  [N] Notion deals (active statuses)...")
    notion_deals, n_err = fetch_notion_deals(
        exclude_ids=cooldown_ids, exclude_names=cooldown_names)
    if n_err:
        errors.append(n_err)
        log(f"    -> {n_err}")
    else:
        log(f"    -> {len(notion_deals)} deals")

    # Fetch — weekly emails (optional, env + CLI gated)
    log("  [E] Weekly update emails (last 7d)...")
    emails, e_err = fetch_weekly_emails()
    if e_err:
        errors.append(e_err)
        log(f"    -> {e_err}")
    else:
        log(f"    -> {len(emails)} emails")

    # Auto-denylist any NEW deal names BEFORE anonymization runs, so raw names
    # can never reach drafts or inspo labels even on a deal's first appearance.
    if notion_deals:
        auto_add_denylist_terms([d.get("name") for d in notion_deals], log)

    raw_payload = {
        "fireflies": fireflies,
        "notion": notion_deals,
        "emails": emails,
    }

    # Anonymize (internal sources: full pass — denylist + handles + dollars + emails)
    log("  [A] Anonymizing (denylist + regex)...")
    clean_payload = anonymize_dict(raw_payload)
    log(f"    -> denylist: {DENYLIST_PATH} ({len(_denylist())} rules)")

    # Timeline context: PUBLIC tweets get the denylist pass ONLY (a portfolio
    # company trending publicly must not hand Sonnet the real name), but public
    # @handles/figures stay — they are public content, not internal data.
    timeline = fetch_timeline_context()
    clean_payload["timeline"] = [
        {**t, "author": denylist_only(t.get("author", "")),
         "text": denylist_only(t.get("text", ""))}
        for t in timeline
    ]
    log(f"  [T] timeline: {len(timeline)} top-engagement public tweets injected")

    if not any([fireflies, notion_deals, emails]):
        log("  no data from any configured source — returning empty insights")
        return {"insights_sgv": [], "insights_mist": [], "insights": [],
                "errors": errors + ["no data from any source"], "cost_usd": 0,
                "source_counts": {"fireflies": 0, "notion": 0, "emails": 0}}

    # Distill
    log(f"  [S] Sonnet distill → up to {N_INSIGHTS} drafts × 2 voices...")
    try:
        parsed, usage, cost = sonnet_distill(clean_payload, memory_lines=memory_lines)
    except Exception as e:
        log(f"    -> Sonnet failed: {e}")
        return {"insights_sgv": [], "insights_mist": [], "errors": errors + [f"sonnet: {e}"], "cost_usd": 0}

    # Accept legacy single-list shape OR new dual-voice shape
    if "sgv" in parsed or "mist" in parsed:
        insights_sgv = parsed.get("sgv", []) or []
        insights_mist = parsed.get("mist", []) or []
    else:
        # Legacy: treat the whole list as fund-voice
        insights_sgv = parsed.get("insights", []) or []
        insights_mist = []
    log(f"    -> sgv={len(insights_sgv)} mist={len(insights_mist)} | cost: ${cost:.4f} | input: {usage['input_tokens']}t output: {usage['output_tokens']}t")

    # Post-process each draft as a safety net: strip residual [brackets] and
    # abstract any leaked "<NamedCo> cofounder/CEO/exec" descriptors.
    # Also resolve source_refs into human-readable inspo labels.
    sanitized_count = 0
    for voice_label, lst in (("sgv", insights_sgv), ("mist", insights_mist)):
        for ins in lst:
            if not isinstance(ins, dict):
                continue
            original = ins.get("draft", "")
            clean = postprocess_draft(original)
            if clean != original:
                sanitized_count += 1
                ins["draft"] = clean
                ins["_sanitized"] = True
            # Resolve source_refs → inspo_label + inspo_snippet (anonymized)
            ins["voice"] = voice_label
            inspo_label, inspo_snippet = _resolve_source_refs(
                ins.get("source_refs") or [], clean_payload
            )
            ins["inspo_label"] = inspo_label
            ins["inspo_snippet"] = inspo_snippet
    if sanitized_count:
        log(f"    -> postprocess: {sanitized_count}/{len(insights_sgv)+len(insights_mist)} drafts sanitized")

    # Snapshot
    if not dry_run:
        combined = {"sgv": insights_sgv, "mist": insights_mist}
        path = save_snapshot(clean_payload, raw_payload, combined, usage, cost)
        log(f"  [snap] wrote {path}")

    duration = (datetime.datetime.now() - t0).total_seconds()
    log(f"== DONE in {duration:.1f}s | cost ${cost:.4f} ==")

    return {
        "insights_sgv": insights_sgv,
        "insights_mist": insights_mist,
        # Legacy alias kept so any older code that reads .insights still works
        "insights": insights_sgv,
        "errors": errors,
        "usage": usage,
        "cost_usd": cost,
        "duration_s": duration,
        "source_counts": {
            "fireflies": len(fireflies),
            "notion": len(notion_deals),
            "emails": len(emails),
            "timeline": len(timeline),
        },
        "memory_lines": len(memory_lines),
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    result = run(dry_run=args.dry_run, verbose=not args.quiet)
    print(json.dumps(result, indent=2, default=str))
