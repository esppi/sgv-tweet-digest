#!/usr/bin/env python3
"""Fetch our own tweets (the two own accounts) + their metrics, store to feedback.db.

Modes:
  default     : fetch last 10 tweets per account, snapshot metrics
  --backfill  : initial backfill, paginate to fetch last N days (default 14)

Cost model: per X API per-resource pricing.
  user_timeline: $0.005/tweet returned
  Daily run cost: ~$0.10 (20 tweets total across both accounts)
  Backfill (14d, ~50 tweets/account): ~$0.50 one-time

Credentials are inherited from gather_x (X_BEARER_TOKEN + X_OAUTH2_* env vars).
The two own-account user IDs are read from the operator config (config.example.json
shape) — see gather_x.load_config / SGV_CONFIG.

Output: feedback.db.our_tweets + tweet_metrics_snapshots
"""
import os, sys, json, time, urllib.parse, urllib.request, urllib.error, argparse, datetime

# Allow sibling imports (feedback_db.py and gather_x.py live next to this file)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import feedback_db as db

# Bearer (app-only) + OAuth2 user-context token fn come from the gather script.
from gather_x import bearer_token, _oauth2_access_token, load_config

# User IDs from the operator config
_CFG = load_config()
USER_IDS = {
    _CFG["personal_username"]: _CFG["personal_user_id"],
    _CFG["sgv_username"]:      _CFG["sgv_user_id"],
}

# Pricing
PRICE_PER_TWEET = 0.005

# X API
API_BASE = "https://api.x.com/2"
TWEET_FIELDS = "created_at,public_metrics,referenced_tweets,conversation_id,lang,in_reply_to_user_id"


# ─────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────
_OAUTH2_TOKEN_CACHED = None


def _bearer():
    """Get OAuth2 user-context bearer (refreshes if needed). Falls back to app-only.

    user-timeline reads require the user-context token; the app-only Bearer is
    only a last-resort fallback.
    """
    global _OAUTH2_TOKEN_CACHED
    if _OAUTH2_TOKEN_CACHED is None:
        try:
            app_only = bearer_token()
        except RuntimeError:
            app_only = None
        _OAUTH2_TOKEN_CACHED = _oauth2_access_token() or app_only
    if not _OAUTH2_TOKEN_CACHED:
        raise RuntimeError(
            "No X credentials available: set X_OAUTH2_* (preferred for user-timeline) "
            "or at least X_BEARER_TOKEN."
        )
    return _OAUTH2_TOKEN_CACHED


def _get(url, retries=3):
    """GET with OAuth2 user-context auth. Returns parsed JSON or raises."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + _bearer()})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 401:
                # Token may be stale; force one refresh attempt
                if attempt == 0:
                    global _OAUTH2_TOKEN_CACHED
                    _OAUTH2_TOKEN_CACHED = None
                    continue
                raise RuntimeError(f"HTTP 401 after token refresh: {body[:300]}")
            if e.code == 429:
                reset = e.headers.get("x-rate-limit-reset")
                wait = max(int(reset) - int(time.time()), 5) if reset and reset.isdigit() else 30
                print(f"  rate limited, sleeping {wait}s...")
                time.sleep(min(wait, 60))
                continue
            if 500 <= e.code < 600 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body[:300]}")
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("retries exhausted")


# ─────────────────────────────────────────
# FETCH
# ─────────────────────────────────────────
def fetch_user_tweets(account, user_id, max_results=10, since_id=None,
                     start_time=None, pagination_token=None):
    """Fetch one page of tweets from /2/users/<id>/tweets. Returns (data_list, meta_dict)."""
    params = {
        "max_results": str(max(5, min(100, max_results))),  # X API min=5, max=100
        "tweet.fields": TWEET_FIELDS,
    }
    if since_id:
        params["since_id"] = since_id
    if start_time:
        params["start_time"] = start_time
    if pagination_token:
        params["pagination_token"] = pagination_token
    url = f"{API_BASE}/users/{user_id}/tweets?{urllib.parse.urlencode(params)}"
    data = _get(url)
    return data.get("data", []) or [], data.get("meta", {}) or {}


def _to_iso_z(dt):
    if isinstance(dt, datetime.datetime):
        return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt


def fetch_account(account, user_id, mode="daily", days_back=14, budget_remaining=None):
    """Fetch tweets for one account. Returns (tweets_seen, tweets_new, cost_usd)."""
    tweets_seen = 0
    tweets_new = 0
    cost_usd = 0.0

    # Determine start_time / since_id
    if mode == "backfill":
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_back)
        start_time = _to_iso_z(cutoff)
        since_id = None
        max_results = 100
    else:
        # Daily: max_results=10, no since_id (we want metric refresh on existing too)
        start_time = None
        since_id = None
        max_results = 10

    snapshot_at = db.now_iso()
    pagination_token = None
    page = 0
    while True:
        page += 1
        if budget_remaining is not None and budget_remaining <= 0:
            print(f"  [{account}] budget exhausted, stopping pagination")
            break
        try:
            data, meta = fetch_user_tweets(
                account, user_id,
                max_results=max_results,
                since_id=since_id,
                start_time=start_time,
                pagination_token=pagination_token,
            )
        except Exception as e:
            print(f"  [{account}] fetch error: {e}")
            break

        if not data:
            break

        for t in data:
            tweet_id = t["id"]
            tweets_seen += 1
            cost_usd += PRICE_PER_TWEET
            if budget_remaining is not None:
                budget_remaining -= PRICE_PER_TWEET

            metrics = t.get("public_metrics", {}) or {}

            # Detect reply / quote / retweet from referenced_tweets
            is_reply = is_quote = is_retweet = 0
            ref_tweet_id = None
            for ref in (t.get("referenced_tweets") or []):
                if ref.get("type") == "replied_to":
                    is_reply = 1
                    ref_tweet_id = ref.get("id")
                elif ref.get("type") == "quoted":
                    is_quote = 1
                    ref_tweet_id = ref.get("id")
                elif ref.get("type") == "retweeted":
                    is_retweet = 1
                    ref_tweet_id = ref.get("id")

            url = f"https://x.com/{account}/status/{tweet_id}"

            row = {
                "tweet_id": tweet_id,
                "account": account,
                "user_id": user_id,
                "text": t.get("text", ""),
                "created_at": t.get("created_at", ""),
                "fetched_at": snapshot_at,
                "is_reply": is_reply,
                "is_quote": is_quote,
                "is_retweet": is_retweet,
                "conversation_id": t.get("conversation_id"),
                "in_reply_to_user_id": t.get("in_reply_to_user_id"),
                "referenced_tweet_id": ref_tweet_id,
                "url": url,
                "lang": t.get("lang"),
            }

            # Detect whether this is a NEW row (for counting only)
            existing = db._conn().execute(
                "SELECT 1 FROM our_tweets WHERE tweet_id = ?", (tweet_id,)
            ).fetchone()
            if not existing:
                tweets_new += 1

            db.upsert_our_tweet(row)
            db.record_metric_snapshot(
                tweet_id=tweet_id,
                snapshot_at=snapshot_at,
                created_at=row["created_at"],
                metrics={
                    "likes":       metrics.get("like_count", 0),
                    "retweets":    metrics.get("retweet_count", 0),
                    "replies":     metrics.get("reply_count", 0),
                    "quotes":      metrics.get("quote_count", 0),
                    "bookmarks":   metrics.get("bookmark_count", 0),
                    "impressions": metrics.get("impression_count", 0),
                },
                raw_json=metrics,
            )

        pagination_token = meta.get("next_token")
        if mode != "backfill" or not pagination_token:
            break
        # In backfill mode, keep paginating until we hit start_time

    return tweets_seen, tweets_new, cost_usd


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def run(mode="daily", days_back=14, budget_usd=0.50, verbose=True):
    log = print if verbose else (lambda *a, **k: None)
    t0 = datetime.datetime.now()
    log(f"== our-tweets-fetcher {t0.isoformat()} (mode={mode}, budget=${budget_usd:.2f}) ==")

    db.init_schema()

    total_seen = 0
    total_new = 0
    total_cost = 0.0
    budget_remaining = budget_usd

    for account, user_id in USER_IDS.items():
        log(f"  [{account}] user_id={user_id}")
        seen, new, cost = fetch_account(
            account, user_id, mode=mode, days_back=days_back,
            budget_remaining=budget_remaining,
        )
        total_seen += seen
        total_new += new
        total_cost += cost
        budget_remaining -= cost
        log(f"    -> {seen} tweets seen, {new} new, cost ${cost:.4f}")

    duration = (datetime.datetime.now() - t0).total_seconds()
    log(f"== DONE in {duration:.1f}s | total_seen={total_seen} new={total_new} cost=${total_cost:.4f} ==")

    return {
        "mode": mode,
        "total_seen": total_seen,
        "total_new": total_new,
        "cost_usd": round(total_cost, 4),
        "duration_s": round(duration, 1),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true",
                        help="Backfill mode: fetch last N days of tweets")
    parser.add_argument("--days-back", type=int, default=14)
    parser.add_argument("--budget", type=float, default=0.50,
                        help="Max USD to spend this run")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    mode = "backfill" if args.backfill else "daily"
    result = run(mode=mode, days_back=args.days_back, budget_usd=args.budget,
                 verbose=not args.quiet)
    print(json.dumps(result, indent=2))
