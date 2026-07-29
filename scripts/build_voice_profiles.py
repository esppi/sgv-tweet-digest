#!/usr/bin/env python3
"""Fetch recent originals from the fund + personal X accounts, then ask Opus to
characterize each voice. Save to voice_profiles.json — used as few-shot context
in the daily digest's tweet-idea prompts.

One-time setup: re-run when voice drifts (probably never).

The two accounts (key/user_id/username/label) are read from the operator's
config.json (config.example.json shape), so nothing here is account-specific.

Output goes to the operator's WRITABLE voice_profiles.json under the state dir
(SGV_VOICE_PROFILES, default <state>/voice_profiles.json) — NOT the read-only
seed shipped in the skill repo.

Secrets come from the environment:
  - X_BEARER_TOKEN   (X API v2 app-only bearer)
  - ANTHROPIC_API_KEY (Opus call)

Cost:
  - 2 X API calls @ $0.01 = $0.02
  - 1 Opus call @ ~$0.04
  - Total ~$0.06 one-time
"""
import json, os, sys, urllib.request, urllib.error
from pathlib import Path

# ─────────────────────────────────────────
# PATHS / CONFIG (env-derived, no hardcoded home)
# ─────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))


def _skill_dir():
    """Root of the installed skill (where config.example.json + the JSON seeds live)."""
    env = os.environ.get("CLAUDE_SKILL_DIR")
    if env:
        return env
    # scripts/ lives directly under the skill root
    return os.path.dirname(HERE)


def _state_dir():
    """Per-user writable state dir: ${XDG_DATA_HOME:-$HOME/.local/share}/sgv-tweet-digest."""
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


def load_config():
    """Load the operator's config.json — the ONE canonical chain shared by every
    loader: $SGV_CONFIG -> $XDG_CONFIG_HOME/sgv-tweet-digest/config.json ->
    <skill_dir>/config.json (the README quickstart copy) -> the shipped example."""
    cfg_base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    candidates = [
        _env_path("SGV_CONFIG", None),
        os.path.join(cfg_base, "sgv-tweet-digest", "config.json"),
        os.path.join(_skill_dir(), "config.json"),
        os.path.join(_skill_dir(), "config.example.json"),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            try:
                with open(p) as f:
                    return json.load(f)
            except Exception:
                continue
    return {}


CONFIG = load_config()
MODELS = CONFIG.get("models", {}) if isinstance(CONFIG, dict) else {}
OPUS_MODEL = MODELS.get("opus", "claude-opus-4-7")

# Writable destination for the regenerated profiles (never the shipped seed).
VOICE_OUT = _env_path(
    "SGV_VOICE_PROFILES", os.path.join(_state_dir(), "voice_profiles.json")
)


def _accounts_from_config():
    """Build the [{key,user_id,username,label}] list from config.
    Expects personal_user_id/personal_username + sgv_user_id/sgv_username.
    """
    sgv_id = CONFIG.get("sgv_user_id")
    sgv_user = CONFIG.get("sgv_username")
    per_id = CONFIG.get("personal_user_id")
    per_user = CONFIG.get("personal_username")
    accounts = []
    if sgv_id and sgv_user:
        accounts.append({
            "key": "sgv", "user_id": str(sgv_id), "username": sgv_user,
            "label": f"@{sgv_user} — fund account",
        })
    if per_id and per_user:
        accounts.append({
            "key": "mist", "user_id": str(per_id), "username": per_user,
            "label": f"@{per_user} — personal account",
        })
    return accounts


def get_bearer():
    bearer = os.environ.get("X_BEARER_TOKEN")
    if not bearer:
        raise SystemExit("X_BEARER_TOKEN not set in environment")
    return bearer


def fetch_originals(user_id, bearer, count=30):
    """Pull recent originals (no replies, no RTs)."""
    url = (f"https://api.x.com/2/users/{user_id}/tweets"
           f"?max_results={count}"
           f"&tweet.fields=text,created_at,public_metrics,referenced_tweets"
           f"&exclude=retweets,replies")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return json.loads(r.read()).get("data", []) or []
    except urllib.error.HTTPError as e:
        print(f"  fetch error {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        return []


def filter_substantive(tweets):
    """Drop tweets that are just URLs or very short."""
    out = []
    for t in tweets:
        text = t.get("text", "").strip()
        if len(text) < 50:
            continue
        # Skip tweets that are mostly URLs
        if text.startswith("http") and len(text.split()) < 5:
            continue
        m = t.get("public_metrics", {})
        t["_engagement"] = m.get("like_count", 0) + m.get("retweet_count", 0) * 3
        out.append(t)
    return out


def characterize_voice(account_label, samples):
    """One LLM call to write a tight voice profile (provider/model per config
    'backends.voice_profiles')."""
    import llm_backend

    sample_block = "\n\n".join(f"[{i+1}] {s['text']}" for i, s in enumerate(samples[:15]))

    prompt = f"""You will read 15 recent tweets from {account_label} and produce a tight voice profile.

TWEETS:
{sample_block}

Output ONLY valid JSON, no preamble:
{{
  "voice_summary": "2-3 sentence description of tone, perspective, and stylistic tics. Be specific. e.g. 'Analytical fund-perspective with light contrarianism. Uses concrete numbers and named protocols. Avoids emojis. Tends to lead with a thesis claim, then back it with data.'",
  "do": ["bullet of stylistic moves to imitate", "another", "another", "another"],
  "dont": ["stylistic anti-patterns to avoid", "another", "another"],
  "best_examples": ["full text of the 3 most representative tweets, exact reproduction"]
}}

Be precise and useful — these will be used as few-shot context to write tweets in this voice."""

    raw, usage, cost = llm_backend.chat(
        stage="voice_profiles",
        system_text="You write precise voice profiles from tweet samples.",
        user_text=prompt, max_tokens=2400, default_model=OPUS_MODEL)
    if raw.startswith("```"):
        import re
        raw = re.sub(r"^```(?:json)?\n?|\n?```$", "", raw)
    start = raw.find("{")
    if start < 0:
        raise RuntimeError("voice_profiles: no JSON object in model output")
    parsed, _ = json.JSONDecoder().raw_decode(raw[start:])
    return parsed, cost


def main():
    accounts = _accounts_from_config()
    if not accounts:
        raise SystemExit(
            "no accounts in config — set sgv_user_id/sgv_username and "
            "personal_user_id/personal_username in config.json"
        )
    bearer = get_bearer()
    profiles = {}
    total_cost = 0.0

    for acc in accounts:
        print(f"=== {acc['label']} ===")
        raw = fetch_originals(acc["user_id"], bearer, count=30)
        substantive = filter_substantive(raw)
        substantive.sort(key=lambda t: -t.get("_engagement", 0))
        print(f"  fetched {len(raw)} originals → {len(substantive)} substantive")

        if len(substantive) < 5:
            print(f"  WARN: only {len(substantive)} substantive tweets — voice profile may be thin")

        # Cap at 15 for prompt size
        samples = substantive[:15]
        for s in samples[:5]:
            txt = s["text"].replace("\n", " ")[:120]
            print(f"  [{s.get('_engagement')}] {txt}")

        print(f"  characterizing voice via Opus...")
        try:
            profile, cost = characterize_voice(acc["label"], samples)
            total_cost += cost
            profiles[acc["key"]] = {
                "label": acc["label"],
                "username": acc["username"],
                "user_id": acc["user_id"],
                "sample_count": len(samples),
                "voice_summary": profile["voice_summary"],
                "do": profile["do"],
                "dont": profile["dont"],
                "best_examples": profile["best_examples"],
            }
            print(f"  ✓ characterized (${cost:.3f})")
            print(f"    voice: {profile['voice_summary']}")
        except Exception as e:
            print(f"  FAIL: {e}")
            profiles[acc["key"]] = {
                "label": acc["label"],
                "username": acc["username"],
                "user_id": acc["user_id"],
                "error": str(e),
            }

    # Never clobber a previously-good profile with an error stub — a failed
    # account keeps its existing entry so downstream drafting isn't degraded.
    existing = {}
    try:
        with open(VOICE_OUT) as f:
            existing = json.load(f)
    except Exception:
        pass
    for key, prof in list(profiles.items()):
        if prof.get("error") and existing.get(key, {}).get("voice_summary"):
            profiles[key] = existing[key]
            print(f"  [preserve] kept previous good '{key}' profile (this run failed: {prof['error'][:80]})")

    Path(VOICE_OUT).parent.mkdir(parents=True, exist_ok=True)
    with open(VOICE_OUT, "w") as f:
        json.dump(profiles, f, indent=2)
    print(f"\n=== wrote {VOICE_OUT} ===")
    print(f"Total Opus cost: ${total_cost:.4f}")


if __name__ == "__main__":
    main()
