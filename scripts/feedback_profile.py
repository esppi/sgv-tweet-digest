#!/usr/bin/env python3
"""Aggregate idea <-> real-tweet matches + metrics over the last N days,
ask Sonnet to write 3-5 do/don't recommendations for the next digest.

Output:
  feedback_profiles row in feedback.db
  recommendations_json = {do_more: [...], do_less: [...], evidence: [...]}

Credentials: the Anthropic key comes from the ANTHROPIC_API_KEY env var.
The Sonnet model id is read from the operator config ("sonnet_model"), falling
back to a sensible default if unset.
"""
import os, sys, json, datetime, statistics, argparse
from collections import defaultdict

# Sibling imports (feedback_db.py + gather_x.py live next to this file)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import feedback_db as db

DEFAULT_SONNET_MODEL = "claude-sonnet-4-5-20250929"

LOOKBACK_DAYS = 14
MIN_SAMPLE_TO_RECOMMEND = 3   # need at least this many matched tweets before generating recs


def _config():
    try:
        from gather_x import load_config
        return load_config() or {}
    except Exception:
        return {}


def _sonnet_model():
    """Sonnet model id from operator config (models.sonnet — same key as the
    other modules; the old top-level 'sonnet_model' is kept as a fallback)."""
    cfg = _config()
    models = cfg.get("models", {}) if isinstance(cfg, dict) else {}
    return models.get("sonnet") or cfg.get("sonnet_model") or DEFAULT_SONNET_MODEL


def _anthropic_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    return key


def _engagement_score(metrics):
    """Combine like + 5*reply + 3*RT + 0.1*impressions into one heuristic engagement number."""
    if not metrics:
        return 0
    return (
        (metrics.get("likes") or 0)
        + 5 * (metrics.get("replies") or 0)
        + 3 * (metrics.get("retweets") or 0)
        + 0.1 * (metrics.get("impressions") or 0)
    )


def collect_matched_data(lookback_days=LOOKBACK_DAYS):
    """Return list of dicts: {tweet, idea, latest_metrics, engagement_score, days_idea_to_tweet}."""
    c = db._conn()
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=lookback_days)).isoformat()
    rows = c.execute("""
        SELECT
          t.tweet_id, t.account, t.text, t.created_at, t.url AS tweet_url,
          i.id          AS idea_id,
          i.source_module,
          i.voice,
          i.topic       AS idea_topic,
          i.draft       AS idea_draft,
          i.viral_hooks_json,
          i.generated_at AS idea_generated_at,
          m.confidence,
          m.similarity
        FROM tweet_idea_matches m
        JOIN our_tweets t  ON t.tweet_id = m.our_tweet_id
        JOIN tweet_ideas i ON i.id       = m.idea_id
        WHERE t.created_at >= ?
        ORDER BY t.created_at DESC
    """, (cutoff,)).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["latest_metrics"] = db.latest_metrics_for_tweet(d["tweet_id"]) or {}
        d["engagement_score"] = _engagement_score(d["latest_metrics"])
        try:
            d["viral_hooks"] = json.loads(d.get("viral_hooks_json") or "[]")
        except Exception:
            d["viral_hooks"] = []
        try:
            t_dt = datetime.datetime.fromisoformat(d["created_at"].replace("Z", "+00:00"))
            i_dt = datetime.datetime.fromisoformat(d["idea_generated_at"])
            d["days_idea_to_tweet"] = max(0, (t_dt - i_dt).days)
        except Exception:
            d["days_idea_to_tweet"] = None
        out.append(d)
    return out


def collect_all_recent_tweets(lookback_days=LOOKBACK_DAYS):
    """All our tweets in lookback window (matched + unmatched). Used for baseline."""
    c = db._conn()
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=lookback_days)).isoformat()
    rows = c.execute("""
        SELECT * FROM our_tweets
        WHERE created_at >= ?
          AND is_retweet = 0
        ORDER BY created_at DESC
    """, (cutoff,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["latest_metrics"] = db.latest_metrics_for_tweet(d["tweet_id"]) or {}
        d["engagement_score"] = _engagement_score(d["latest_metrics"])
        out.append(d)
    return out


def compute_archetype_stats(matched, all_tweets):
    """Group matched tweets by (voice, source_module, primary_hook) and compute engagement."""
    by_archetype = defaultdict(list)
    for m in matched:
        primary_hook = (m["viral_hooks"] + ["no_hook"])[0]
        key = f"{m['voice']}::{m['source_module']}::{primary_hook}"
        by_archetype[key].append(m["engagement_score"])

    # Baseline: average engagement across ALL our tweets in window
    all_scores = [t["engagement_score"] for t in all_tweets]
    baseline = statistics.mean(all_scores) if all_scores else 0

    stats = []
    for key, scores in sorted(by_archetype.items(), key=lambda kv: -statistics.mean(kv[1])):
        voice, mod, hook = key.split("::")
        avg = statistics.mean(scores)
        stats.append({
            "voice":          voice,
            "source_module":  mod,
            "primary_hook":   hook,
            "n_matched":      len(scores),
            "avg_engagement": round(avg, 2),
            "baseline":       round(baseline, 2),
            "delta_vs_baseline": round(avg - baseline, 2),
            "lift_x":         round(avg / max(baseline, 0.001), 2),
        })
    return stats, round(baseline, 2)


def _sonnet_system():
    """System prompt with the operator's OWN handles (nothing hardcoded).
    Uses .replace(), not .format() — the template body contains JSON braces."""
    cfg = _config()
    personal = cfg.get("personal_username", "personal-account")
    fund = cfg.get("sgv_username", "fund-account")
    return (SONNET_SYSTEM_TEMPLATE
            .replace("@{personal}", "@" + str(personal))
            .replace("@{fund}", "@" + str(fund)))


SONNET_SYSTEM_TEMPLATE = """You are analyzing the SGV tweet-feedback loop.

Goal: tell the digest pipeline what TYPES of ideas drove the BEST engagement
on @{personal} and @{fund} in the recent window. The pipeline will inject
your recommendations into tomorrow's Opus + Sonnet system prompts so the next
batch of ideas leans toward what worked.

You'll see:
  - Per-archetype engagement stats (voice x source_module x primary_hook)
  - The matched (idea -> tweet) pairs with their engagement scores
  - The baseline engagement across all our tweets in the window

Write 3-5 SHORT actionable rules. Be specific. Reference numbers.

Output JSON only:
{
  "do_more": ["short rule 1", "short rule 2", ...],
  "do_less": ["short rule 1", ...],
  "evidence": ["concrete evidence statement 1", ...]
}

Constraints:
  - 'do_more' / 'do_less' items <= 80 chars each
  - 'evidence' items <= 120 chars each
  - All items reference real archetypes / numbers from the data
  - No em dashes anywhere in your output (use commas/periods/'and')"""


def sonnet_recommend(client, stats, matched, baseline):
    """One Sonnet call to produce recommendations."""
    user_msg = (
        f"BASELINE engagement (avg across all our tweets, last {LOOKBACK_DAYS}d): {baseline}\n\n"
        f"PER-ARCHETYPE STATS ({len(stats)} archetypes):\n"
        f"{json.dumps(stats, indent=2)}\n\n"
        f"MATCHED PAIRS (idea -> tweet, with engagement scores):\n"
    )
    # Truncate match details to keep prompt manageable
    user_msg += json.dumps([{
        "voice": m["voice"],
        "source_module": m["source_module"],
        "hooks": m["viral_hooks"],
        "topic": m.get("idea_topic"),
        "idea_draft": (m.get("idea_draft") or "")[:200],
        "tweet_text": (m.get("text") or "")[:200],
        "engagement_score": m["engagement_score"],
        "confidence": m["confidence"],
        "days_idea_to_tweet": m["days_idea_to_tweet"],
    } for m in matched[:30]], indent=2)
    user_msg += "\n\nReturn the JSON, nothing else."

    resp = client.messages.create(
        model=_sonnet_model(),
        max_tokens=1200,
        system=[{"type": "text", "text": _sonnet_system(), "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_msg}],
    )
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise RuntimeError("Sonnet output truncated at max_tokens — JSON would be invalid")
    # First TEXT block (newer models may emit a thinking block at content[0])
    raw = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "").strip()
    if raw.startswith("```"):
        import re
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
    return parsed, cost


def _apply_reliability_softening(stats, log):
    """Discount each archetype's lift_x by its observed reliability
    (from recommendation_reliability). Suppressed archetypes get their lift
    capped at 1.0 so they don't appear in do_more.

    First-time archetypes (no reliability row yet) pass through unchanged.
    """
    c = db._conn()
    rows = c.execute(
        "SELECT archetype_key, reliability_score, suppressed_until FROM recommendation_reliability"
    ).fetchall()
    if not rows:
        return stats
    by_key = {r["archetype_key"]: dict(r) for r in rows}
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    softened = []
    for s in stats:
        key = f"{s['voice']}::{s['source_module']}::{s['primary_hook']}"
        rel = by_key.get(key)
        s2 = dict(s)
        if rel:
            score = rel.get("reliability_score")
            suppressed = (rel.get("suppressed_until") or "") > now_iso
            original_lift = s2.get("lift_x") or 1.0
            if suppressed:
                s2["lift_x_raw"] = original_lift
                s2["lift_x"] = min(original_lift, 1.0)
                s2["reliability_note"] = f"suppressed (rel={score:.2f})" if score is not None else "suppressed"
                log(f"  soften: {key} SUPPRESSED (lift {original_lift:.2f} -> {s2['lift_x']:.2f}; rel={score})")
            elif score is not None and score < 1.0:
                s2["lift_x_raw"] = original_lift
                s2["lift_x"] = round(original_lift * score, 2)
                s2["reliability_note"] = f"softened x{score:.2f}"
                log(f"  soften: {key} discounted (lift {original_lift:.2f} -> {s2['lift_x']:.2f}; rel={score:.2f})")
        softened.append(s2)
    # Re-sort by adjusted lift_x descending
    softened.sort(key=lambda x: -(x.get("lift_x") or 0))
    return softened


def run(lookback_days=LOOKBACK_DAYS, verbose=True):
    log = print if verbose else (lambda *a, **k: None)
    t0 = datetime.datetime.now()
    log(f"== feedback-profile {t0.isoformat()} (lookback {lookback_days}d) ==")

    db.init_schema()

    matched = collect_matched_data(lookback_days)
    all_tweets = collect_all_recent_tweets(lookback_days)
    log(f"  {len(matched)} matched (idea<->tweet) pairs - {len(all_tweets)} total tweets in window")

    stats, baseline = compute_archetype_stats(matched, all_tweets)
    stats = _apply_reliability_softening(stats, log)

    if len(matched) < MIN_SAMPLE_TO_RECOMMEND:
        # Save an empty profile so digest.py knows the loop is alive but not yet useful
        empty = {
            "generated_at":   db.now_iso(),
            "lookback_days":  lookback_days,
            "matched_tweets": len(matched),
            "total_tweets":   len(all_tweets),
            "archetype_stats": stats,
            "recommendations": {
                "do_more": [],
                "do_less": [],
                "evidence": [f"Only {len(matched)} matched tweets so far; need >={MIN_SAMPLE_TO_RECOMMEND} before issuing recommendations."],
            },
            "baseline": baseline,
            "note": "cold_start_insufficient_sample",
        }
        rid = db.save_feedback_profile(empty)
        duration = (datetime.datetime.now() - t0).total_seconds()
        log(f"  cold start — saved empty profile id={rid}, no Sonnet call. duration={duration:.1f}s")
        return {"matched_tweets": len(matched), "cost_usd": 0, "profile_id": rid,
                "duration_s": round(duration, 1)}

    # Sonnet call
    import anthropic
    client = anthropic.Anthropic(api_key=_anthropic_api_key())

    log(f"  calling Sonnet for recommendations...")
    try:
        recs, cost = sonnet_recommend(client, stats, matched, baseline)
    except Exception as e:
        log(f"  Sonnet failed: {e}")
        return {"matched_tweets": len(matched), "cost_usd": 0, "error": str(e),
                "duration_s": (datetime.datetime.now() - t0).total_seconds()}

    profile = {
        "generated_at":   db.now_iso(),
        "lookback_days":  lookback_days,
        "matched_tweets": len(matched),
        "total_tweets":   len(all_tweets),
        "archetype_stats": stats,
        "recommendations": recs,
        "baseline": baseline,
    }
    rid = db.save_feedback_profile(profile)
    duration = (datetime.datetime.now() - t0).total_seconds()
    log(f"  saved profile id={rid} | do_more={len(recs.get('do_more', []))} do_less={len(recs.get('do_less', []))} cost=${cost:.4f}")
    log(f"== DONE in {duration:.1f}s ==")
    return {
        "matched_tweets": len(matched),
        "cost_usd":       round(cost, 4),
        "profile_id":     rid,
        "duration_s":     round(duration, 1),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()
    result = run(lookback_days=args.lookback_days, verbose=not args.quiet)
    print(json.dumps(result, indent=2))
