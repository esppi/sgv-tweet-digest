#!/usr/bin/env python3
"""Match real tweets we posted to tweet_ideas we previously generated.

This is the script that makes the feedback loop live: it writes
`tweet_idea_matches`, which feedback_profile.py joins against to learn which
idea archetypes actually perform. Without it the loop stays cold forever.

Two-phase matching:
  Phase 1: TF-IDF cosine prefilter (cheap, in-process, stdlib-only)
  Phase 2: Sonnet judge on top survivors (~$0.003/call)

For each our_tweet posted in the last `lookback_hours`:
  - Find candidate ideas: same voice, generated 0-14 days BEFORE the tweet
  - TF-IDF rank -> top survivors above threshold
  - Send (idea, tweet) pair to Sonnet for binary match + confidence verdict
  - Save matches via feedback_db.save_match (UNIQUE constraint dedupes)

Run daily (deploy/sgv-idea-matcher.timer, 12:40 UTC): after our_tweets_fetcher
has snapshotted the latest posted tweets, before feedback_profile at 13:30.

Credentials/config from the environment + operator config:
  ANTHROPIC_API_KEY   (Sonnet judge)
  models.sonnet       in config.json (same key as the other modules)
"""
import os, sys, json, re, math, datetime, argparse
from collections import Counter

# Sibling imports (feedback_db.py + gather_x.py live next to this file)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import feedback_db as db

DEFAULT_SONNET_MODEL = "claude-sonnet-4-5-20250929"

LOOKBACK_TWEET_HOURS = 36         # match tweets from last 36h
LOOKBACK_IDEA_DAYS = 14           # consider ideas from last 14d
PREFILTER_THRESHOLD = 0.06        # min TF-IDF cosine to survive to the Sonnet judge
                                  # (deliberately low: paraphrases share few tokens)
MAX_JUDGE_PAIRS_PER_TWEET = 8     # max Sonnet calls per real tweet
MAX_JUDGE_PAIRS_PER_RUN = 50      # global cap for cost control (~$0.15/run worst case)

# Confidence levels that qualify a verdict for saving
SAVE_CONFIDENCE = {"exact", "high", "medium"}


def _config():
    try:
        from gather_x import load_config
        return load_config() or {}
    except Exception:
        return {}


def _sonnet_model():
    cfg = _config()
    models = cfg.get("models", {}) if isinstance(cfg, dict) else {}
    return models.get("sonnet") or DEFAULT_SONNET_MODEL


# ─────────────────────────────────────────
# TF-IDF (manual, no sklearn dep)
# ─────────────────────────────────────────
STOPWORDS = set("""
a an the and or but if then is are was were be been being have has had do does did
i you he she it we they me him her us them my your his its our their this that
these those of at to in on for from by with as via about over under through into
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def _tokenize(text):
    """Lowercase + alpha-num tokens, stopwords removed."""
    if not text:
        return []
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in STOPWORDS and len(t) > 2]


def _tfidf_cosine(tokens_a, tokens_b, df):
    """Compute cosine similarity of two token lists, with IDF weighting."""
    if not tokens_a or not tokens_b:
        return 0.0
    counter_a = Counter(tokens_a)
    counter_b = Counter(tokens_b)
    n_docs = sum(df.values()) or 1

    def vec(counter):
        v = {}
        for tok, freq in counter.items():
            df_tok = df.get(tok, 1)
            idf = math.log(1 + n_docs / df_tok)
            v[tok] = freq * idf
        return v

    va = vec(counter_a)
    vb = vec(counter_b)
    dot = sum(va[t] * vb.get(t, 0) for t in va)
    na = math.sqrt(sum(x * x for x in va.values())) or 1
    nb = math.sqrt(sum(x * x for x in vb.values())) or 1
    return dot / (na * nb)


# ─────────────────────────────────────────
# DATA FETCH
# ─────────────────────────────────────────
def get_unmatched_recent_tweets(hours):
    """Return our_tweets from last `hours` that have no entry in tweet_idea_matches."""
    c = db._conn()
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)).isoformat()
    rows = c.execute("""
        SELECT t.* FROM our_tweets t
        WHERE t.created_at >= ?
          AND NOT EXISTS (
            SELECT 1 FROM tweet_idea_matches m WHERE m.our_tweet_id = t.tweet_id
          )
        ORDER BY t.created_at DESC
    """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def get_candidate_ideas(tweet_created_at, voice, max_days=14):
    """Ideas generated up to N days before a tweet, in the same voice."""
    c = db._conn()
    tweet_dt = datetime.datetime.fromisoformat(tweet_created_at.replace("Z", "+00:00"))
    upper = tweet_dt.isoformat()
    lower = (tweet_dt - datetime.timedelta(days=max_days)).isoformat()
    rows = c.execute("""
        SELECT * FROM tweet_ideas
        WHERE voice = ?
          AND generated_at <= ?
          AND generated_at >= ?
        ORDER BY generated_at DESC
    """, (voice, upper, lower)).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────
# SONNET JUDGE
# ─────────────────────────────────────────
JUDGE_SYSTEM = """You evaluate whether a real tweet was inspired by an earlier
tweet idea draft. Both come from the same author. The idea was offered to the
author 0-14 days BEFORE the tweet was posted.

Heavy editing is expected. Look for:
  - Semantic similarity of the CORE CLAIM (not surface wording)
  - Shared key phrases or named concepts
  - Shared hook structure (concrete number / contrarian / pattern / Q&A)
  - Shared topic / framing / target audience

A real tweet that's clearly about a DIFFERENT topic should be `match: false`
even if it shares some words.

Bias toward MATCH when:
  - The CORE CLAIM is the same even if the surface text shares few words.
  - The hook structure or framing is preserved (e.g., both use a specific
    number, both call out a pattern, both ask a question about the same thing).
  - The tweet is plausibly a rewrite/spin-up of the idea's underlying point.

Bias toward NO-MATCH when:
  - The topics genuinely differ even if the words rhyme.
  - The tweet is a generic platitude that could've been written without
    seeing the idea.

Output JSON only, no other text:
{"match": true|false, "confidence": "exact|high|medium|low", "similarity": 0.0-1.0,
 "reason": "<one short sentence>"}

Confidence meanings:
  exact   — almost copy-paste from the idea draft
  high    — same claim/hook, paraphrased
  medium  — same topic + similar angle, but materially different framing
  low     — tangential overlap only (return match: false in this case)"""


def sonnet_judge(idea, our_tweet, days_diff):
    """One judge verdict (provider/model per config 'backends.idea_matcher')."""
    import llm_backend
    user_msg = (
        f"IDEA (offered {days_diff} days ago, voice={idea['voice']}, topic={idea.get('topic') or 'n/a'}):\n"
        f"{idea['draft']}\n\n"
        f"REAL TWEET (posted at {our_tweet['created_at']}):\n"
        f"{our_tweet['text']}\n\n"
        "Return the JSON, nothing else."
    )
    raw, usage, cost = llm_backend.chat(
        stage="idea_matcher", system_text=JUDGE_SYSTEM, user_text=user_msg,
        max_tokens=700, default_model=_sonnet_model())
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?|\n?```$", "", raw)
    start = raw.find("{")
    if start < 0:
        raise RuntimeError("judge: no JSON object in model output")
    parsed, _ = json.JSONDecoder().raw_decode(raw[start:])
    return parsed, cost


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def voice_for_tweet(account):
    """Map our X account -> voice key, via the operator's config handles
    (nothing hardcoded to a specific fund)."""
    cfg = _config()
    personal = (cfg.get("personal_username") or "").lstrip("@").lower()
    acc = (account or "").lstrip("@").lower()
    return "mist" if acc == personal else "sgv"


def run(lookback_hours=LOOKBACK_TWEET_HOURS, max_pairs=MAX_JUDGE_PAIRS_PER_RUN, verbose=True):
    log = print if verbose else (lambda *a, **k: None)
    t0 = datetime.datetime.now()
    log(f"== idea-matcher {t0.isoformat()} (lookback {lookback_hours}h) ==")

    db.init_schema()

    tweets = get_unmatched_recent_tweets(lookback_hours)
    log(f"  {len(tweets)} unmatched tweets in last {lookback_hours}h")

    if not tweets:
        return {"tweets_examined": 0, "judge_calls": 0, "matches_saved": 0,
                "cost_usd": 0.0, "duration_s": 0}

    judge_calls = 0
    matches_saved = 0
    total_cost = 0.0

    for tweet in tweets:
        # Skip retweets — those are by definition copies of others, not our ideas
        if tweet.get("is_retweet"):
            continue
        voice = voice_for_tweet(tweet["account"])
        ideas = get_candidate_ideas(tweet["created_at"], voice, max_days=LOOKBACK_IDEA_DAYS)
        if not ideas:
            log(f"  tweet {tweet['tweet_id'][:10]} ({voice}) — no candidate ideas")
            continue

        # TF-IDF prefilter
        tweet_tokens = _tokenize(tweet["text"])
        # Build document-frequency map across all ideas + this tweet
        all_docs = [_tokenize(i["draft"]) for i in ideas] + [tweet_tokens]
        df = Counter()
        for doc in all_docs:
            df.update(set(doc))

        scored = []
        for idea in ideas:
            idea_tokens = _tokenize(idea["draft"])
            sim = _tfidf_cosine(tweet_tokens, idea_tokens, df)
            scored.append((sim, idea))
        scored.sort(key=lambda x: -x[0])
        survivors = [(s, i) for s, i in scored if s >= PREFILTER_THRESHOLD][:MAX_JUDGE_PAIRS_PER_TWEET]

        if not survivors:
            top = f"{scored[0][0]:.2f}" if scored else "n/a"
            log(f"  tweet {tweet['tweet_id'][:10]} ({voice}) — 0 survivors after prefilter (top={top})")
            continue

        log(f"  tweet {tweet['tweet_id'][:10]} ({voice}) — {len(survivors)} survivors, calling Sonnet judge...")

        for tfidf_sim, idea in survivors:
            if judge_calls >= max_pairs:
                log(f"  reached max judge pairs ({max_pairs}), stopping")
                break

            try:
                tweet_dt = datetime.datetime.fromisoformat(tweet["created_at"].replace("Z", "+00:00"))
                idea_dt = datetime.datetime.fromisoformat(idea["generated_at"])
                days_diff = max(0, (tweet_dt - idea_dt).days)
                verdict, call_cost = sonnet_judge(idea, tweet, days_diff)
            except Exception as e:
                log(f"    sonnet judge error: {e}")
                continue
            judge_calls += 1
            total_cost += call_cost

            is_match = bool(verdict.get("match"))
            confidence = (verdict.get("confidence") or "").lower()
            similarity = float(verdict.get("similarity") or tfidf_sim)
            reason = verdict.get("reason") or ""

            if is_match and confidence in SAVE_CONFIDENCE:
                row_id = db.save_match(
                    our_tweet_id=tweet["tweet_id"],
                    idea_id=idea["id"],
                    similarity=similarity,
                    confidence=confidence,
                    method="sonnet_judge",
                    reason=reason,
                )
                if row_id:
                    matches_saved += 1
                    log(f"    ✓ MATCH idea#{idea['id']} confidence={confidence} sim={similarity:.2f} | {reason[:80]}")
            else:
                log(f"    ✗ no match (confidence={confidence}, sim={similarity:.2f}) | {reason[:80]}")

        if judge_calls >= max_pairs:
            break

    duration = (datetime.datetime.now() - t0).total_seconds()
    log(f"== DONE in {duration:.1f}s | tweets={len(tweets)} judge_calls={judge_calls} matches={matches_saved} cost=${total_cost:.4f} ==")
    return {
        "tweets_examined": len(tweets),
        "judge_calls": judge_calls,
        "matches_saved": matches_saved,
        "cost_usd": round(total_cost, 4),
        "duration_s": round(duration, 1),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--lookback-hours", type=int, default=LOOKBACK_TWEET_HOURS)
    p.add_argument("--max-pairs", type=int, default=MAX_JUDGE_PAIRS_PER_RUN)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()
    result = run(lookback_hours=args.lookback_hours, max_pairs=args.max_pairs,
                 verbose=not args.quiet)
    print(json.dumps(result, indent=2))
