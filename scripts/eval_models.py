#!/usr/bin/env python3
"""Model trial battery — measures candidate models on THIS pipeline's real
workloads, not benchmarks. Four suites:

  voice        — dual-voice tweet drafting from real candidates + real voice
                 profiles; outputs side-by-side drafts (+ recent real Opus
                 drafts as anchors) for human blind-ranking
  judge        — replay stored matcher decisions (positives from
                 tweet_idea_matches, constructed negatives) and score accuracy
  extract      — call_drafts stage-1 extraction on a SYNTHETIC transcript with
                 PLANTED names/metrics; scores JSON validity + anonymization
                 leaks (a planted token appearing in output = leak)
  reliability  — N small strict-JSON calls; parse-failure rate + latency

Usage (on the VPS, env sourced):
  "$VENV_PYTHON" scripts/eval_models.py --provider aster \
      --models kimi-k3,glm-5.2,gpt-oss-120b [--suites voice,judge,extract,reliability]

Writes <state>/model_eval_<provider>.json and prints a summary table.
Costs are measured from returned usage via llm_backend's price map.
"""
import os, sys, json, time, argparse, random, sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import llm_backend as lb
import feedback_db as db
from call_drafts import EXTRACT_SYSTEM
from idea_matcher import JUDGE_SYSTEM


def _state_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "sgv-tweet-digest")


def _call(provider, model, system, user, max_tokens, json_mode=False):
    t0 = time.time()
    if provider == "aster":
        key = os.environ.get("ASTER_API_KEY")
        text, usage = lb._openai_chat(lb.ASTER_BASE, key, model, system, user,
                                      max_tokens, json_mode=json_mode)
    elif provider == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY")
        text, usage = lb._openai_chat(lb.OPENROUTER_OPENAI_BASE, key, model,
                                      system, user, max_tokens, json_mode=json_mode)
    elif provider == "anthropic":
        text, usage = lb._anthropic_chat(model, system, user, max_tokens)
    else:
        raise RuntimeError("unknown provider " + provider)
    dt = time.time() - t0
    cost = lb._cost(provider, model, usage)
    return text, usage, cost, dt


def _parse_json(text):
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    return json.JSONDecoder().raw_decode(text[start:])[0]


# ─────────────────────────────────────────
# SUITE: voice
# ─────────────────────────────────────────
def suite_voice(provider, models, report, log):
    import insights as _ins
    profiles = json.load(open(_ins._voice_profiles_path()))
    owned_path = os.path.expanduser(os.path.expandvars(
        os.environ.get("SGV_OWNED_READS")
        or os.path.join(_state_dir(), "latest.json")))
    owned = json.load(open(owned_path))
    tweets = sorted(owned.get("list_tweets") or [],
                    key=lambda t: -(t.get("metrics") or {}).get("like_count", 0))[:8]
    cands = [{"author": t.get("account"), "text": (t.get("text") or "")[:300],
              "likes": (t.get("metrics") or {}).get("like_count", 0)} for t in tweets]

    def vb(key):
        p = profiles.get(key) or {}
        return (f"VOICE: {p.get('voice_summary','')}\nDO: " + " | ".join((p.get('do') or [])[:5])
                + "\nDON'T: " + " | ".join((p.get('dont') or [])[:5])
                + "\nEXAMPLES: " + " | ".join((p.get('best_examples') or [])[:3]))

    system = (
        "You draft tweets for a crypto VC. Two voices:\n=== FUND (sgv) ===\n" + vb("sgv")
        + "\n=== PERSONAL (mist) ===\n" + vb("mist")
        + "\nNEVER use an em dash. <=280 chars per draft. JSON only:\n"
        '{"mist": ["draft1", "draft2"], "sgv": ["draft1"]}')
    user = ("Today's top candidate tweets:\n" + json.dumps(cands, indent=1)
            + "\nWrite 2 personal-voice drafts + 1 fund-voice draft inspired by "
              "(not copying) the strongest signals. JSON only.")

    # Real recent Opus drafts as anchors
    try:
        c = db._conn()
        anchors = [r["draft"] for r in c.execute(
            "SELECT draft FROM tweet_ideas WHERE voice='mist' AND "
            "source_module='opus_picks' ORDER BY id DESC LIMIT 3").fetchall()]
    except Exception:
        anchors = []
    report["voice"] = {"anchors_recent_opus": anchors, "models": {}}

    for m in models:
        try:
            text, usage, cost, dt = _call(provider, m, system, user, 1200)
            parsed = _parse_json(text)
            drafts = {"mist": parsed.get("mist") or [], "sgv": parsed.get("sgv") or []}
            over = [d for v in drafts.values() for d in v if len(d) > 280]
            emd = [d for v in drafts.values() for d in v if "—" in d]
            report["voice"]["models"][m] = {
                "drafts": drafts, "cost": round(cost, 5), "latency_s": round(dt, 1),
                "over_280": len(over), "em_dashes": len(emd)}
            log(f"  voice {m}: ok ${cost:.4f} {dt:.0f}s over280={len(over)} emdash={len(emd)}")
        except Exception as e:
            report["voice"]["models"][m] = {"error": str(e)[:200]}
            log(f"  voice {m}: FAIL {str(e)[:120]}")


# ─────────────────────────────────────────
# SUITE: judge (replay)
# ─────────────────────────────────────────
def suite_judge(provider, models, report, log, n_pos=12, n_neg=13):
    c = db._conn()
    pos = c.execute("""
        SELECT t.text AS tweet_text, t.created_at, i.draft, i.topic, i.voice,
               i.generated_at
        FROM tweet_idea_matches m
        JOIN our_tweets t ON t.tweet_id = m.our_tweet_id
        JOIN tweet_ideas i ON i.id = m.idea_id
        ORDER BY m.id DESC LIMIT ?""", (n_pos,)).fetchall()
    ideas = c.execute("SELECT id, draft, topic, voice, generated_at FROM tweet_ideas "
                      "ORDER BY RANDOM() LIMIT 60").fetchall()
    tweets = c.execute("SELECT tweet_id, text, created_at FROM our_tweets "
                       "WHERE is_retweet=0 ORDER BY RANDOM() LIMIT 60").fetchall()
    matched_pairs = {(r["draft"][:60], r["tweet_text"][:60]) for r in pos}
    rng = random.Random(42)
    neg = []
    for i in ideas:
        if len(neg) >= n_neg:
            break
        t = rng.choice(tweets)
        if (i["draft"][:60], t["text"][:60]) in matched_pairs:
            continue
        if (i["topic"] or "")[:12].lower() in (t["text"] or "").lower():
            continue  # avoid accidental true matches in negatives
        neg.append((i, t))
    cases = ([{"idea": r["draft"], "tweet": r["tweet_text"], "label": True} for r in pos]
             + [{"idea": i["draft"], "tweet": t["text"], "label": False} for i, t in neg])
    report["judge"] = {"n_cases": len(cases), "models": {}}

    for m in models:
        correct = fails = 0
        cost_sum = 0.0
        t0 = time.time()
        for case in cases:
            user = (f"IDEA (offered earlier):\n{case['idea']}\n\nREAL TWEET:\n{case['tweet']}"
                    "\n\nReturn the JSON, nothing else.")
            try:
                text, usage, cost, dt = _call(provider, m, JUDGE_SYSTEM, user, 700,
                                              json_mode=True)
                verdict = _parse_json(text)
                cost_sum += cost
                if bool(verdict.get("match")) == case["label"]:
                    correct += 1
            except Exception:
                fails += 1
        report["judge"]["models"][m] = {
            "accuracy": round(correct / max(len(cases) - fails, 1), 3),
            "parse_or_call_failures": fails,
            "total_cost": round(cost_sum, 4),
            "wall_s": round(time.time() - t0, 1)}
        log(f"  judge {m}: acc={report['judge']['models'][m]['accuracy']} "
            f"fails={fails} ${cost_sum:.4f}")


# ─────────────────────────────────────────
# SUITE: extract (synthetic transcript, planted names)
# ─────────────────────────────────────────
_PLANTED = ["Quorline", "quorline.io", "Besnik Halla", "Marrow Capital",
            "$7.3M", "7.3M ARR", "Tessa Brightwell"]

_SYNTHETIC = """David: Thanks for making time. Walk me through it.
Besnik Halla: Sure. Quorline is a settlement layer for cross-border payroll.
We're at $7.3M ARR, growing 22% month over month, 41 people. quorline.io if
you want to poke at the product.
David: Who else is in the round?
Besnik Halla: Marrow Capital led the seed. Tessa Brightwell from their team
is on our board. The thing nobody gets is that payroll is a trust product,
not a payments product. Compliance IS the moat. Every competitor treats
compliance as a cost center and that's why they lose enterprise deals.
David: How do you think about stablecoin rails here?
Besnik Halla: Honestly the rail is boring, and that's the point. Our
customers never see a token. We settle in stables under the hood because
correspondent banking eats 3 days and 80 basis points. The CFO just sees
payroll that lands same-day. If you market the crypto, you lose the CFO.
David: What breaks first if you 10x?
Besnik Halla: Local licensing. Money transmission licenses don't scale like
software. We think the endgame is everyone rents licensing the way they rent
cloud. Licensing-as-a-service is the actual platform play hiding in here.
"""


def suite_extract(provider, models, report, log):
    user = f"CALL TRANSCRIPT (28 min):\n{_SYNTHETIC}\n\nReturn the JSON."
    report["extract"] = {"models": {}}
    for m in models:
        try:
            text, usage, cost, dt = _call(provider, m, EXTRACT_SYSTEM, user, 1600)
            parsed = _parse_json(text)
            moments = parsed.get("moments") or []
            blob = json.dumps(parsed).lower()
            leaks = [p for p in _PLANTED if p.lower() in blob]
            report["extract"]["models"][m] = {
                "n_moments": len(moments),
                "leaks": leaks,
                "sample_moments": [mm.get("moment", "")[:140] for mm in moments[:3]
                                   if isinstance(mm, dict)],
                "cost": round(cost, 5), "latency_s": round(dt, 1)}
            log(f"  extract {m}: {len(moments)} moments, leaks={leaks or 'NONE'} ${cost:.4f}")
        except Exception as e:
            report["extract"]["models"][m] = {"error": str(e)[:200]}
            log(f"  extract {m}: FAIL {str(e)[:120]}")


# ─────────────────────────────────────────
# SUITE: reliability
# ─────────────────────────────────────────
def suite_reliability(provider, models, report, log, n=5):
    system = ('Return ONLY this JSON, values filled: {"ok": true, "sum": <int>, '
              '"words": [<3 lowercase words>]}')
    report["reliability"] = {"models": {}}
    for m in models:
        ok = 0
        lat = []
        cost_sum = 0.0
        for i in range(n):
            try:
                text, usage, cost, dt = _call(
                    provider, m, system,
                    f"sum = {i}+{i * 3}; words = any three words about markets", 700,
                    json_mode=True)
                parsed = _parse_json(text)
                if parsed.get("ok") is True and isinstance(parsed.get("sum"), int):
                    ok += 1
                lat.append(dt)
                cost_sum += cost
            except Exception:
                lat.append(-1)
        good_lat = [x for x in lat if x >= 0]
        report["reliability"]["models"][m] = {
            "json_ok_rate": round(ok / n, 2),
            "median_latency_s": round(sorted(good_lat)[len(good_lat) // 2], 1) if good_lat else None,
            "cost": round(cost_sum, 5)}
        log(f"  reliability {m}: ok={ok}/{n} lat~{report['reliability']['models'][m]['median_latency_s']}s")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--provider", default="aster")
    p.add_argument("--models", default="kimi-k3,glm-5.2,gpt-oss-120b")
    p.add_argument("--suites", default="voice,judge,extract,reliability")
    args = p.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    suites = [s.strip() for s in args.suites.split(",")]
    log = print
    report = {"provider": args.provider, "models": models}
    log(f"== model eval | provider={args.provider} models={models} ==")
    if "voice" in suites:
        log("[voice]")
        suite_voice(args.provider, models, report, log)
    if "judge" in suites:
        log("[judge]")
        suite_judge(args.provider, models, report, log)
    if "extract" in suites:
        log("[extract]")
        suite_extract(args.provider, models, report, log)
    if "reliability" in suites:
        log("[reliability]")
        suite_reliability(args.provider, models, report, log)
    out = os.path.join(_state_dir(), f"model_eval_{args.provider}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(report, open(out, "w"), indent=1)
    log(f"== report written: {out} ==")


if __name__ == "__main__":
    main()
