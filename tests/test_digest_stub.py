#!/usr/bin/env python3
"""Stubbed end-to-end regression harness (no network, no keys needed beyond a
dummy). Run with a python that has the `anthropic` package installed:

    python3 tests/test_digest_stub.py

Covers: velocity scoring, Opus DO-NOT-REPEAT memory injection, dual-channel
delivery incl. hot QTs + call-drafts section, mist capitalization, and the
truly-dry guarantee (--dry-run writes zero DB rows, no stats.jsonl)."""
import os, sys, json, types, glob, sqlite3, datetime, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp(prefix="sgv-digest-test-")
STATE = os.path.join(WORK, "state")

os.environ["XDG_DATA_HOME"] = STATE
os.environ["SGV_OWNED_READS"] = os.path.join(REPO, "tests", "fixture_latest.json")
os.environ["CLAUDE_SKILL_DIR"] = REPO
os.environ.pop("SHOAL_CHANNEL", None)
os.environ.pop("FIREFLIES_API_KEY", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-stubbed")
os.environ["TELEGRAM_DELIVERY_CHAT_SGV"] = "-100111"
os.environ["TELEGRAM_DELIVERY_CHAT_MIST"] = "222"
os.environ["TELEGRAM_BOT_TOKEN"] = "123:testtoken"
sys.path.insert(0, os.path.join(REPO, "scripts"))

# Stub the lazily-imported Sonnet modules
ins = types.ModuleType("insights")
ins.run = lambda dry_run=False, verbose=True: {"insights_sgv": [], "insights_mist": [], "errors": [],
    "cost_usd": 0, "source_counts": {"fireflies": 0, "notion": 0, "emails": 0}}
sys.modules["insights"] = ins
gc_mod = types.ModuleType("good_content")
gc_mod.run = lambda **k: (_ for _ in ()).throw(AssertionError("gc must not run on dry-run"))
sys.modules["good_content"] = gc_mod
cd_mod = types.ModuleType("call_drafts")
cd_mod.run = lambda dry_run=False, verbose=True: {"call_drafts": [
    {"call_label": "call 1 (37 min)", "drafts": [
        {"draft": "everyone builds cards. the off ramp is the quiet wedge.",
         "angle": "insight", "source_moment": "a neobank pattern", "viral_hooks": ["pattern"]}]}],
    "cost_usd": 0, "errors": []}
sys.modules["call_drafts"] = cd_mod

import digest

# ── velocity: identical metrics, fresh must outscore stale ──
now = datetime.datetime.now(datetime.timezone.utc)
mk = lambda age_h: {"metrics": {"like_count": 120, "retweet_count": 30, "reply_count": 10, "quote_count": 5},
                    "text": "stablecoin rails", "created_at": (now - datetime.timedelta(hours=age_h)).isoformat(),
                    "list_count": 1, "in_following": False, "category": "crypto", "entities": {}}
sf, ss = digest.heuristic_score(mk(0.75)), digest.heuristic_score(mk(30))
assert sf > ss, f"velocity not rewarded: fresh={sf} stale={ss}"

# ── memory: empty DB -> [], and lines reach the Opus user message ──
assert digest._load_idea_memory() == []
captured = {}
class FakeMsg:
    def __init__(self):
        self.content = [types.SimpleNamespace(type="text", text=json.dumps({
            "picks": [], "sgv_shoal_picks": [], "mist_shoal_picks": [],
            "sgv_ideas": [], "mist_ideas": [], "hot_qt_picks": []}))]
        self.usage = types.SimpleNamespace(input_tokens=1, output_tokens=1,
            cache_creation_input_tokens=0, cache_read_input_tokens=0)
        self.stop_reason = "end_turn"
class FakeClient:
    class messages:
        @staticmethod
        def create(**kw):
            captured["user_msg"] = kw["messages"][0]["content"]
            return FakeMsg()
digest.anthropic = types.SimpleNamespace(Anthropic=lambda api_key: FakeClient())
_real_mem = digest._load_idea_memory
digest._load_idea_memory = lambda days=7, cap=40: ["[2026-07-14 mist] topic=jito | draft: test take"]
digest.opus_pick([{"tweet_id": "1", "account": "@a", "text": "t", "metrics": {}, "_heuristic_score": 5,
                   "_thesis_label": "general", "category": "crypto"}], [])
assert "DO NOT REPEAT" in captured["user_msg"] and "jito" in captured["user_msg"]
digest._load_idea_memory = _real_mem

# ── stubbed end-to-end dry-run ──
IDEA = lambda v: {"kind": "tweet", "source": "News", "setup": "s", "draft": f"test {v} draft.",
                  "inspo_label": "@x", "inspo_url": "", "topic": "t", "viral_hooks": ["pattern"]}
FIX = {"picks": [{"rank": 1, "tweet_id": "9001", "why": "w", "action": "Reply"}],
       "sgv_shoal_picks": [], "mist_shoal_picks": [],
       "sgv_ideas": [IDEA("sgv")], "mist_ideas": [IDEA("mist")],
       "hot_qt_picks": [{"category": "ai", "tweet_id": "9101", "setup": "s",
                         "sgv_draft": "Hot take sgv.", "mist_draft": "hot take mist.",
                         "inspo_label": "@ai_builder", "inspo_url": "https://x.com/ai_builder/status/9101",
                         "topic": "ai", "viral_hooks": ["pattern"]}]}
USAGE = {"input_tokens": 10, "output_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
digest.opus_pick = lambda c, s: (FIX, USAGE, 0.0)
sys.argv = ["digest.py", "--dry-run"]
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    digest.main()
out = buf.getvalue()
assert out.count("HOT QTs — AI / tech / VC") == 2, "hot-QT block must appear in both channels"
assert "Hot take mist." in out, "mist capitalization pass failed"
assert out.count("CALL DRAFTS") == 1, "call-drafts section must appear once (personal only)"
assert "Everyone builds cards. The off ramp is the quiet wedge." in out, "call draft missing/uncapitalized"
dbp = os.path.join(STATE, "sgv-tweet-digest", "feedback.db")
n = sqlite3.connect(dbp).execute("SELECT COUNT(*) FROM tweet_ideas").fetchone()[0] if os.path.exists(dbp) else 0
assert n == 0, f"dry-run wrote {n} rows"
assert not glob.glob(os.path.join(STATE, "sgv-tweet-digest", "stats.jsonl")), "dry-run wrote stats"
print("HARNESS OK: velocity (fresh", sf, "> stale", ss, "), memory injection, dual-channel + hot-QT + call-drafts delivery, 0 DB writes on dry-run")
