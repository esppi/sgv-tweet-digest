#!/usr/bin/env python3
"""Owned-reads gather — list-based and follow-graph signals ONLY.

Scope: only Lists + following. (Other sources — bookmarks, liked tweets,
mentions, followers — are intentionally not fetched.)

Fetched (all via the operator's OAuth2 user token + app-only Bearer fallback):
  - following (the operator's follow graph — Bearer)
  - owned_lists + per-list members + per-list tweets (OAuth)
  - followed_lists (config-declared IDs bypassing the /followed_lists 403) + members + tweets
  - list_memberships + members + tweets (lists that include the operator)

Credentials (all from the environment — nothing hardcoded):
  X_BEARER_TOKEN          X API v2 app-only bearer
  X_OAUTH2_CLIENT_ID      OAuth2 client id (operator's own X app)
  X_OAUTH2_CLIENT_SECRET  OAuth2 client secret (confidential clients only; optional)
  X_OAUTH2_REFRESH_TOKEN  OAuth2 refresh token (mints user access tokens)
  X_OAUTH2_TOKEN_FILE     where the refreshed OAuth2 token blob is persisted
                          (default under XDG state; seeded from the env vars above
                           on first run)

Operational config (non-secret) is read from a JSON file:
  SGV_CONFIG              path to the operator's config copy (canonical name, shared
                          with digest.py; SGV_DIGEST_CONFIG is a legacy fallback alias)
                          (default: $XDG_CONFIG_HOME/sgv-tweet-digest/config.json;
                           falls back to the shipped config.example.json)

Output: $XDG_DATA_HOME/sgv-tweet-digest/owned-reads/latest.json
State:  $XDG_DATA_HOME/sgv-tweet-digest/owned-reads/state.json

Stdlib-only — runs fine on system python.
"""

import argparse, json, os, sys, time, datetime, urllib.parse, urllib.request, urllib.error


def _state_base():
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(base, "sgv-tweet-digest")


def _config_path():
    """Resolve the operator's config: $SGV_CONFIG (canonical, shared with digest.py),
    then $SGV_DIGEST_CONFIG (legacy alias), else XDG config, else the shipped
    config.example.json at the repo root."""
    explicit = os.environ.get("SGV_CONFIG") or os.environ.get("SGV_DIGEST_CONFIG")
    if explicit:
        return explicit
    cfg_base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    candidate = os.path.join(cfg_base, "sgv-tweet-digest", "config.json")
    if os.path.exists(candidate):
        return candidate
    # Fall back to the shipped example (repo root, two levels up from scripts/)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "config.example.json")


OWNED_DIR    = os.path.join(_state_base(), "owned-reads")
OUTPUT_PATH  = os.path.join(OWNED_DIR, "latest.json")
STATE_PATH   = os.path.join(OWNED_DIR, "state.json")
CONFIG_PATH  = _config_path()
OAUTH2_PATH  = os.environ.get(
    "X_OAUTH2_TOKEN_FILE", os.path.join(_state_base(), "twitter_oauth2.json")
)
BUDGET_USD_PER_DAY = 1.00      # hard cap; gather skips calls that would push past this
LIST_TWEETS_MAX_RESULTS = 10   # 50 was burning $0.25/list at $0.005/tweet -> drop to 10 = $0.05/list
# Members fetch is now SKIPPED by default (digest doesn't consume members data,
# and 100 users x $0.001 x 15 lists = $1.50/refresh-day blows the budget).
# Set FETCH_LIST_MEMBERS=True to re-enable. Members from prior runs stay in state.json.
FETCH_LIST_MEMBERS = False
MEMBERS_REFRESH_DAYS = 30      # only relevant if FETCH_LIST_MEMBERS=True
# Following: refreshed DAILY as P2, capped at 200 most recent follows.
# 200 x $0.001 = $0.20/day. Tier-0 boost surface tracks current attention tightly.
FOLLOWING_MAX_RESULTS = 200
FOLLOWING_REFRESH_DAYS = 1     # daily refresh; X returns reverse-chronological so we get the 200 most recent

# Per-endpoint cost estimates (User tier = $0.01/call per dashboard observation).
# Conservative: assume per-CALL billing. If real billing turns out per-tweet,
# the budget cap still protects us — gather degrades gracefully.
# Per-RESOURCE pricing: X bills timeline / member-list endpoints PER ITEM RETURNED.
_RESOURCE_COSTS = {
    "lists_tweets":  0.005,   # per tweet returned
    "lists_members": 0.001,   # per user returned
    "following":     0.001,   # per user returned
    "followers":     0.001,   # per user returned
    "owned_reads":   0.001,   # per tweet returned (bookmarks/liked/mentions)
}
# Flat per-call costs for metadata endpoints (small fixed payload)
_FLAT_COSTS = {
    "owned_lists":      0.0001,
    "list_memberships": 0.0001,
    "followed_lists":   0.0001,
    "users_me":         0.0001,
}
_DEFAULT_FLAT_COST = 0.01  # conservative for unknown buckets


def _estimate_cost(url, n_resources=None):
    """Pre-flight cost estimate.
    For resource-returning endpoints: multiply by `n_resources` (the max_results upper bound).
    For metadata endpoints: flat cost. Falls back to $0.01 conservative default.
    """
    if "/users/me" in url:
        return _FLAT_COSTS["users_me"]
    bucket = _url_bucket(url)
    if bucket in _FLAT_COSTS:
        return _FLAT_COSTS[bucket]
    if bucket in _RESOURCE_COSTS:
        upper_bound = n_resources if n_resources else 100
        return _RESOURCE_COSTS[bucket] * upper_bound
    return _DEFAULT_FLAT_COST


def _actual_cost(url, n_resources_returned):
    """After-call cost using actual count of resources returned."""
    if "/users/me" in url:
        return _FLAT_COSTS["users_me"]
    bucket = _url_bucket(url)
    if bucket in _FLAT_COSTS:
        return _FLAT_COSTS[bucket]
    if bucket in _RESOURCE_COSTS:
        return _RESOURCE_COSTS[bucket] * max(1, n_resources_returned)
    return _DEFAULT_FLAT_COST


_BEARER_CACHED = None


def bearer_token():
    """X API v2 app-only Bearer, read from the X_BEARER_TOKEN env var.

    Cached after first read. Raises if unset — callers that can fall back to
    OAuth2 (our_tweets_fetcher) should catch this and use the user token.
    """
    global _BEARER_CACHED
    if _BEARER_CACHED is None:
        tok = os.environ.get("X_BEARER_TOKEN")
        if not tok:
            raise RuntimeError(
                "X_BEARER_TOKEN is not set. Create an X API app and export the "
                "app-only Bearer token as X_BEARER_TOKEN."
            )
        _BEARER_CACHED = tok
    return _BEARER_CACHED


def _delivery_chat_sgv():
    """Telegram group id for fund-voice alerts (TELEGRAM_DELIVERY_CHAT_SGV)."""
    return os.environ.get("TELEGRAM_DELIVERY_CHAT_SGV", "")


def _send_message_script():
    """Path to send_message.sh inside the skill. Honors CLAUDE_SKILL_DIR, else
    resolves relative to this file (scripts/ -> scripts/send_message.sh)."""
    skill_dir = os.environ.get("CLAUDE_SKILL_DIR")
    if skill_dir:
        return os.path.join(skill_dir, "scripts", "send_message.sh")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "send_message.sh")


_RATE_LIMIT_STATE = {}  # url-prefix -> (remaining, reset_epoch)


def _url_bucket(url):
    """Group URLs into rate-limit buckets (X caps by endpoint pattern)."""
    if "/lists/" in url and url.endswith("/tweets"):
        return "lists_tweets"
    if "/lists/" in url and url.endswith("/members"):
        return "lists_members"
    if "/followed_lists" in url:
        return "followed_lists"
    if "/owned_lists" in url:
        return "owned_lists"
    if "/list_memberships" in url:
        return "list_memberships"
    if "/following" in url:
        return "following"
    if "/followers" in url:
        return "followers"
    if "/bookmarks" in url or "/liked_tweets" in url:
        return "owned_reads"
    return "default"


def _preflight_wait(url):
    """If we know we're near the reset for this bucket, pause."""
    bucket = _url_bucket(url)
    state = _RATE_LIMIT_STATE.get(bucket)
    if not state:
        return
    remaining, reset_epoch = state
    now = int(time.time())
    if remaining <= 1 and reset_epoch and now < reset_epoch:
        wait = min(reset_epoch - now + 2, 120)
        if wait > 0:
            print("    [rate-limit] " + bucket + " remaining=" + str(remaining) +
                  ", sleeping " + str(wait) + "s until reset")
            time.sleep(wait)


def _record_rate_headers(url, headers):
    """Record rate-limit state per bucket."""
    try:
        rem = int(headers.get("x-rate-limit-remaining", 0))
        reset = int(headers.get("x-rate-limit-reset", 0))
        bucket = _url_bucket(url)
        _RATE_LIMIT_STATE[bucket] = (rem, reset)
    except (TypeError, ValueError):
        pass


def xapi(url, params=None):
    """Call X API v2 with app-only Bearer.
    Rate-limit aware: preflight pause when a bucket is near reset,
    record remaining/reset headers, exponential retry on 429.
    """
    _preflight_wait(url)
    if params:
        url = url + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + bearer_token()})
    backoff = 30
    for attempt in range(3):
        try:
            resp = urllib.request.urlopen(req)
            _record_rate_headers(url, resp.headers)
            return json.loads(resp.read()), int(resp.headers.get("x-rate-limit-remaining", 0))
        except urllib.error.HTTPError as e:
            _record_rate_headers(url, e.headers)
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", str(backoff)))
                print("    [429] sleeping " + str(retry_after) + "s (attempt " + str(attempt+1) + "/3)")
                time.sleep(retry_after)
                backoff = min(backoff * 2, 300)
                continue
            if e.code == 402:
                print("    [402] budget exhausted")
                return {"_budget_exhausted": True}, 0
            print("    [api error " + str(e.code) + "] " + str(e.reason))
            return {}, 0
        except Exception as e:
            print("    [api error] " + str(e))
            return {}, 0
    return {}, 0


def load_state():
    if not os.path.exists(STATE_PATH):
        return {
            "following_snapshot": [],
            "following_last_fetched": None,        # ISO date of last /following call
            "list_member_snapshot": {},
            "members_last_fetched": {},            # list_id -> ISO date
            "api_calls_today": 0,
            "api_calls_day": "",
            "cost_today_usd": 0.0,                 # $ tracker for budget cap
            "cost_today_day": "",                  # YYYY-MM-DD for cost reset
        }
    with open(STATE_PATH) as f:
        s = json.load(f)
    # Backfill missing fields for upgraded state
    s.setdefault("members_last_fetched", {})
    s.setdefault("following_last_fetched", None)
    s.setdefault("cost_today_usd", 0.0)
    s.setdefault("cost_today_day", "")
    return s


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.rename(tmp, STATE_PATH)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _seed_oauth2_token_file():
    """If the OAuth2 token file does not exist yet, seed it from the X_OAUTH2_* env vars.

    The refresh flow (_oauth2_access_token) reads/writes this file. On first run
    there is no access_token yet, so we set expires_at_utc in the past to force an
    immediate refresh using the refresh_token.
    """
    if os.path.exists(OAUTH2_PATH):
        return
    client_id = os.environ.get("X_OAUTH2_CLIENT_ID")
    refresh_token = os.environ.get("X_OAUTH2_REFRESH_TOKEN")
    if not (client_id and refresh_token):
        return  # nothing to seed; OAuth simply unavailable
    blob = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "access_token": "",
        # Force a refresh on first use.
        "expires_at_utc": "1970-01-01T00:00:00Z",
    }
    secret = os.environ.get("X_OAUTH2_CLIENT_SECRET")
    if secret:
        blob["client_secret"] = secret
    os.makedirs(os.path.dirname(OAUTH2_PATH), exist_ok=True)
    tmp = OAUTH2_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(blob, f, indent=2)
    os.chmod(tmp, 0o600)
    os.rename(tmp, OAUTH2_PATH)


def _can_afford(state, url, n_resources=None):
    """True if `url` (with at most n_resources items) won't push past the daily cap."""
    cost = _estimate_cost(url, n_resources=n_resources)
    return state.get("cost_today_usd", 0.0) + cost <= BUDGET_USD_PER_DAY


def _record_actual(state, url, n_resources_returned):
    """Increment cost tracker using ACTUAL resources returned. Call after a successful call."""
    cost = _actual_cost(url, n_resources_returned)
    state["cost_today_usd"] = round(state.get("cost_today_usd", 0.0) + cost, 5)
    return cost


def _days_since_iso(iso_str):
    """Days between now and the ISO date string. None / invalid -> infinity."""
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


def _oauth2_access_token():
    """Read user token, refresh if near expiry, return access_token or None.
    Supports both public (Native) and confidential clients — confidential clients
    authenticate the refresh request via HTTP Basic with client_id:client_secret.

    The token blob is persisted at X_OAUTH2_TOKEN_FILE and is seeded from the
    X_OAUTH2_* env vars on first run.
    """
    import base64 as _b64
    _seed_oauth2_token_file()
    if not os.path.exists(OAUTH2_PATH):
        return None
    try:
        with open(OAUTH2_PATH) as f:
            tok = json.load(f)
        exp = datetime.datetime.fromisoformat(tok["expires_at_utc"].rstrip("Z"))
        if exp - datetime.datetime.utcnow() < datetime.timedelta(minutes=5):
            body = urllib.parse.urlencode({
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
                "client_id": tok["client_id"],
            }).encode()
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            secret = tok.get("client_secret")
            if secret:
                basic = _b64.b64encode((tok["client_id"] + ":" + secret).encode()).decode()
                headers["Authorization"] = "Basic " + basic
            req = urllib.request.Request(
                "https://api.x.com/2/oauth2/token", data=body, headers=headers,
            )
            r = json.loads(urllib.request.urlopen(req).read())
            tok["access_token"] = r["access_token"]
            tok["refresh_token"] = r.get("refresh_token", tok["refresh_token"])
            tok["expires_at_utc"] = (
                datetime.datetime.utcnow()
                + datetime.timedelta(seconds=r["expires_in"])
            ).isoformat() + "Z"
            tmp = OAUTH2_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(tok, f, indent=2)
            os.chmod(tmp, 0o600)
            os.rename(tmp, OAUTH2_PATH)
            print("  refreshed OAuth2 token (expires " + tok["expires_at_utc"] + ")")
        return tok["access_token"]
    except Exception as e:
        print("  OAuth2 token error: " + str(e))
        return None


def _xapi_user(url, token, params=None):
    """Like xapi but with user-context OAuth2 token.
    Same rate-limit tracking + 429/402 handling.
    """
    _preflight_wait(url)
    if params:
        url = url + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    backoff = 30
    for attempt in range(3):
        try:
            resp = urllib.request.urlopen(req)
            _record_rate_headers(url, resp.headers)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            _record_rate_headers(url, e.headers)
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", str(backoff)))
                print("    [429 user-ctx] sleeping " + str(retry_after) + "s (attempt " + str(attempt+1) + "/3)")
                time.sleep(retry_after)
                backoff = min(backoff * 2, 300)
                continue
            if e.code == 402:
                print("    [402 user-ctx] budget exhausted")
                return {}
            print("    [user-api " + str(e.code) + "] " + str(e.reason) + " on " + url[:60])
            return {}
        except Exception as e:
            print(f"    [user-api error] {e} on {url[:60]}")
            return {}
    return {}


def _normalize_tweets(api_response, owned_signal):
    """Convert X API v2 response to the pipeline's tweet shape."""
    tweets = []
    data = api_response.get("data", []) or []
    includes = api_response.get("includes", {}) or {}
    users = {u["id"]: u for u in includes.get("users", [])}
    ref_tweets = {t["id"]: t for t in includes.get("tweets", [])}

    for t in data:
        author_id = t.get("author_id", "")
        author_info = users.get(author_id, {})
        username = author_info.get("username", "unknown")
        metrics = author_info.get("public_metrics", {})

        refs = t.get("referenced_tweets", []) or []
        if not refs:
            tweet_type = "original"
        elif refs[0]["type"] == "retweeted":
            tweet_type = "retweet"
        elif refs[0]["type"] == "quoted":
            tweet_type = "quote"
        elif refs[0]["type"] == "replied_to":
            tweet_type = "reply"
        else:
            tweet_type = "unknown"

        entry = {
            "account": "@" + username,
            "account_id": author_id,
            "author_followers": metrics.get("followers_count", 0),
            "author_verified": author_info.get("verified_type"),
            "tweet_id": t["id"],
            "type": tweet_type,
            "text": t.get("text", ""),
            "created_at": t.get("created_at", ""),
            "metrics": t.get("public_metrics", {}),
            "entities": t.get("entities", {}),
            "source": "owned_reads_" + owned_signal,
            "owned_signal": owned_signal,
            "tweet_url": "https://x.com/" + username + "/status/" + t["id"],
        }

        if refs and tweet_type in ("retweet", "quote", "reply"):
            ref_id = refs[0]["id"]
            ref = ref_tweets.get(ref_id, {})
            ref_author_id = ref.get("author_id", "")
            ref_author = users.get(ref_author_id, {}).get("username", "")
            ref_metrics = users.get(ref_author_id, {}).get("public_metrics", {})
            entry["referenced_tweet"] = {
                "author": "@" + ref_author,
                "author_followers": ref_metrics.get("followers_count", 0),
                "text": ref.get("text", "")[:300],
                "id": ref_id,
            }

        tweets.append(entry)
    return tweets


_TWEET_PARAMS = {
    "max_results": "100",
    "tweet.fields": "created_at,public_metrics,entities,referenced_tweets,author_id,conversation_id",
    "expansions": "author_id,referenced_tweets.id",
    "user.fields": "username,public_metrics,verified_type",
}


def fetch_owned_lists(config):
    """Lists the user created. Tries OAuth first (sees private lists too), falls back to app-only."""
    token = _oauth2_access_token()
    url = "https://api.x.com/2/users/" + config["personal_user_id"] + "/owned_lists"
    params = {
        "max_results": "100",
        "list.fields": "name,description,member_count,private,follower_count,created_at",
    }
    if token:
        resp = _xapi_user(url, token, params)
    else:
        resp, _ = xapi(url, params)
    return resp.get("data", []) or [], 1


def fetch_followed_lists(config):
    """Lists the user is subscribed to (created by others). Requires OAuth (app-only gets 403)."""
    token = _oauth2_access_token()
    if not token:
        return [], 0, "oauth2 token required for followed_lists"
    url = "https://api.x.com/2/users/" + config["personal_user_id"] + "/followed_lists"
    params = {
        "max_results": "100",
        "list.fields": "name,description,member_count,private,follower_count,created_at,owner_id",
    }
    resp = _xapi_user(url, token, params)
    return resp.get("data", []) or [], 1, None


_SPAM_PATTERNS = [
    "airdrop", "giveaway", "giveway", "$ blur", "eth giveaway", "10.000$", "500.000$",
    "claim", "follow to get", "tier", "$1m",
]

def _looks_spammy(list_name):
    n = (list_name or "").lower()
    return any(p in n for p in _SPAM_PATTERNS)


def fetch_list_memberships(config):
    """Lists that include the user as a member (created by others, they added you).
    Often higher signal than followed_lists — if someone curated 'Venture Capital'
    and put you in it, the other members are probably also VCs.
    Works with OAuth or app-only Bearer; prefer OAuth for full visibility.
    """
    token = _oauth2_access_token()
    url = "https://api.x.com/2/users/" + config["personal_user_id"] + "/list_memberships"
    params = {
        "max_results": "100",
        "list.fields": "name,description,member_count,private,follower_count,created_at,owner_id",
    }
    if token:
        resp = _xapi_user(url, token, params)
    else:
        resp, _ = xapi(url, params)
    lists = resp.get("data", []) or []
    # Tag each with spam heuristic so downstream can filter
    for lst in lists:
        lst["is_suspicious"] = _looks_spammy(lst.get("name"))
    return lists, 1


def fetch_list_members(list_id):
    """Prefer app-only Bearer (500/15min cap) over OAuth user-context (75/15min)."""
    url = "https://api.x.com/2/lists/" + list_id + "/members"
    params = {
        "max_results": "100",
        "user.fields": "username,public_metrics,description,verified_type",
    }
    resp, _ = xapi(url, params)
    if resp.get("_budget_exhausted") or (not resp.get("data") and not resp):
        token = _oauth2_access_token()
        if token:
            resp = _xapi_user(url, token, params)
    members = [
        {"username": u["username"], "id": u["id"],
         "followers": u.get("public_metrics", {}).get("followers_count", 0)}
        for u in (resp.get("data", []) or [])
    ]
    return members, 1


def fetch_list_tweets(list_id, max_results=100, since_id=None):
    """Tweets from members of a list. Note: X API v2 /lists/{id}/tweets does NOT support
    since_id (returns 400). We ignore the arg for forward compat and dedupe downstream
    by tweet_id across successive runs.
    Prefers app-only Bearer; falls back to OAuth on Bearer 401/403.
    """
    url = "https://api.x.com/2/lists/" + list_id + "/tweets"
    params = {
        "max_results": str(max_results),
        "tweet.fields": "created_at,public_metrics,entities,referenced_tweets,author_id,conversation_id",
        "expansions": "author_id,referenced_tweets.id",
        "user.fields": "username,public_metrics,verified_type",
    }
    # Intentionally NOT sending since_id — endpoint rejects it with 400.
    resp, _ = xapi(url, params)
    if resp.get("_budget_exhausted") or (not resp.get("data") and not resp):
        token = _oauth2_access_token()
        if token:
            resp = _xapi_user(url, token, params)
    return _normalize_tweets(resp, "list_tweets"), 1


def fetch_following(config):
    """The operator's follow graph. Bearer first, OAuth fallback."""
    url = "https://api.x.com/2/users/" + config["personal_user_id"] + "/following"
    params = {
        "max_results": str(FOLLOWING_MAX_RESULTS),
        "user.fields": "username,public_metrics,verified_type",
    }
    resp, _ = xapi(url, params)
    if not resp.get("data"):
        token = _oauth2_access_token()
        if token:
            resp = _xapi_user(url, token, params)
    users = [
        {"username": u["username"], "id": u["id"],
         "followers": u.get("public_metrics", {}).get("followers_count", 0)}
        for u in (resp.get("data", []) or [])
    ]
    return users, 1


def _alert(msg, dry_run=False):
    """Best-effort Telegram heads-up to the fund delivery group. Non-blocking."""
    chat = _delivery_chat_sgv()
    if dry_run or not chat:
        return
    try:
        import subprocess as _sp
        _sp.run(["bash", _send_message_script(), chat, msg], check=False, timeout=20)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="No writes; print counts only")
    args = parser.parse_args()

    os.makedirs(OWNED_DIR, exist_ok=True)
    config = load_config()
    state = load_state()

    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    if state.get("api_calls_day") != today:
        state["api_calls_today"] = 0
        state["api_calls_day"] = today
    if state.get("cost_today_day") != today:
        state["cost_today_usd"] = 0.0
        state["cost_today_day"] = today

    print("== Owned-reads gather (" + today + ") ==")
    print("   personal:    @" + config["personal_username"] + " (" + config["personal_user_id"] + ")")
    print("   sgv:         @" + config["sgv_username"] + " (" + config["sgv_user_id"] + ")")
    print("   dry-run:     " + str(args.dry_run))
    print("   budget cap:  $" + str(BUDGET_USD_PER_DAY) + "/day  (used so far: $" + str(round(state["cost_today_usd"], 4)) + ")")

    output = {
        "scan_date": today,
        "scan_start_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "personal_user_id": config["personal_user_id"],
        "sgv_user_id": config["sgv_user_id"],
        "lists": [],
        "followed_lists": [],
        "list_memberships": [],
        "list_tweets": [],
        "following_snapshot": [],
        "stats": {
            "api_calls": 0,
            "estimated_cost_usd": 0.0,
            "errors": [],
            "budget_cap_hit": False,
            "priorities_skipped": [],
            "credits_depleted": False,
        },
    }

    def _afford(url, n_resources=None):
        """Return (ok, reason). Uses upper-bound estimate (n_resources = max_results)."""
        cost = _estimate_cost(url, n_resources=n_resources)
        new_total = state.get("cost_today_usd", 0.0) + cost
        if new_total > BUDGET_USD_PER_DAY:
            output["stats"]["budget_cap_hit"] = True
            return False, "would push to $" + str(round(new_total, 4)) + " (cap $" + str(BUDGET_USD_PER_DAY) + ", est=$" + str(round(cost, 4)) + ")"
        return True, None

    def _spent(url, n_resources_returned):
        """After a successful call, charge the ACTUAL resources returned."""
        actual = _record_actual(state, url, n_resources_returned)
        output["stats"]["estimated_cost_usd"] = state["cost_today_usd"]
        return actual

    def _skip(priority_label, reason):
        print("    [budget-cap] skipping " + priority_label + " — " + reason)
        if priority_label not in output["stats"]["priorities_skipped"]:
            output["stats"]["priorities_skipped"].append(priority_label)

    # ─────────────────────────────────────────────
    # PRIORITY 1: pre-flight credits probe (~$0.0001)
    # ─────────────────────────────────────────────
    probe_url = "https://api.x.com/2/users/me"
    print("  [P1] credits probe...")
    if _afford(probe_url)[0]:
        import urllib.request as _ur, urllib.error as _ue
        req = _ur.Request(probe_url, headers={"Authorization": "Bearer " + bearer_token()})
        try:
            r = _ur.urlopen(req)
            r.read()
            _spent(probe_url, 1)  # /me returns single user, flat cost
            print("    -> ok (cost so far: $" + str(round(state["cost_today_usd"], 4)) + ")")
        except _ue.HTTPError as e:
            if e.code == 402:
                output["stats"]["credits_depleted"] = True
                output["stats"]["errors"].append("x-api-credits-depleted")
                print("    -> 402 CREDITS DEPLETED. Aborting gather, will alert.")
                # Still write minimal output + state so digest knows the failure mode
                output["scan_end_utc"] = datetime.datetime.utcnow().isoformat() + "Z"
                tmp = OUTPUT_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(output, f, indent=2)
                os.rename(tmp, OUTPUT_PATH)
                save_state(state)
                _alert("X API CREDITS DEPLETED. Top up at console.x.com or today's digest will be empty.",
                       dry_run=args.dry_run)
                sys.exit(2)
            print("    -> probe error " + str(e.code) + ": " + str(e.reason))
        except Exception as e:
            print("    -> probe error: " + str(e))

    # Data sources kept: following (daily P2), owned lists, followed lists (config),
    # list memberships, list tweets. (mentions, followers, bookmarks, liked_tweets not fetched.)

    # ─────────────────────────────────────────────
    # PRIORITY 2: following snapshot (daily, 200 most recent)
    # ─────────────────────────────────────────────
    following_age = _days_since_iso(state.get("following_last_fetched"))
    if following_age >= FOLLOWING_REFRESH_DAYS:
        following_url = "https://api.x.com/2/users/" + config["personal_user_id"] + "/following"
        if _afford(following_url, n_resources=FOLLOWING_MAX_RESULTS)[0]:
            print(f"  [P2] following refresh (cache age {following_age:.1f}d, max {FOLLOWING_MAX_RESULTS})...")
            try:
                following, _ = fetch_following(config)
                _spent(following_url, len(following))
                output["stats"]["api_calls"] += 1
                if following:
                    output["following_snapshot"] = following
                    state["following_snapshot"] = [u["id"] for u in following]
                    state["following_last_fetched"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    print(f"    -> {len(following)} follows (running: ${state['cost_today_usd']:.4f}/${BUDGET_USD_PER_DAY})")
                else:
                    prev = state.get("following_snapshot", [])
                    output["following_snapshot"] = [{"id": uid} for uid in prev]
                    print(f"    -> 0 fetched (transient); preserved {len(prev)} from state")
            except Exception as e:
                output["stats"]["errors"].append("following: " + str(e))
        else:
            _skip("following", "budget")
            prev = state.get("following_snapshot", [])
            output["following_snapshot"] = [{"id": uid} for uid in prev]
    else:
        print(f"  [P2] following: cached (age {following_age:.2f}d < {FOLLOWING_REFRESH_DAYS}d), skipped")
        prev = state.get("following_snapshot", [])
        output["following_snapshot"] = [{"id": uid} for uid in prev]

    # ─────────────────────────────────────────────
    # Heuristic: rank memberships by inferred quality.
    # Lower number = higher priority.
    # ─────────────────────────────────────────────
    _PRIORITY_KEYWORDS = [
        # name patterns -> priority bonus (lower is better)
        ("smart", 1), ("vc", 2), ("invest", 3), ("crypto", 4), ("ai", 4),
        ("defi", 4), ("angel", 5), ("partner", 5), ("found", 6),
    ]
    _DEPRIORITIZE = ["follow", "cool", "beep"]

    def _membership_priority(lst):
        n = (lst.get("name") or "").lower()
        # explicit deprioritized
        for k in _DEPRIORITIZE:
            if k in n:
                return 100
        # numeric-only or single char
        if n.strip() in ("3", "1", "2"):
            return 200
        # keyword-matched
        for kw, pri in _PRIORITY_KEYWORDS:
            if kw in n:
                return pri
        return 50  # default mid-priority

    def _process_list_block(lst, kind, output_field):
        """Fetch members (cached weekly) + tweets for one list, respecting budget cap."""
        lid = lst["id"]
        lname = lst.get("name") or "?"

        # Members: only refetch if cache is stale AND FETCH_LIST_MEMBERS=True
        members_url = "https://api.x.com/2/lists/" + lid + "/members"
        members = state.get("list_member_snapshot", {}).get(lid, [])
        members_age = _days_since_iso(state.get("members_last_fetched", {}).get(lid))
        if FETCH_LIST_MEMBERS and members_age > MEMBERS_REFRESH_DAYS:
            # Members upper bound: 100 per call (X's max_results)
            ok, reason = _afford(members_url, n_resources=100)
            if ok:
                fetched, _ = fetch_list_members(lid)
                if fetched:
                    members = fetched
                    state.setdefault("list_member_snapshot", {})[lid] = fetched
                    state.setdefault("members_last_fetched", {})[lid] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                actual = _spent(members_url, len(fetched) if fetched else 0)
                output["stats"]["api_calls"] += 1
                print(f"      members: fetched {len(fetched)} (age was {members_age:.0f}d, ${actual:.4f})")
            else:
                _skip(f"members[{lname}]", reason)
        else:
            print(f"      members: cached ({len(members)}, age {members_age:.1f}d)")

        # Tweets: always try to fetch (the high-value signal). max_results=10 is the new default.
        tweets_url = "https://api.x.com/2/lists/" + lid + "/tweets"
        max_tweets = LIST_TWEETS_MAX_RESULTS
        ok, reason = _afford(tweets_url, n_resources=max_tweets)
        if not ok:
            _skip(f"tweets[{lname}]", reason)
            return False  # signal: cap hit
        tweets, _ = fetch_list_tweets(lid, max_results=max_tweets)
        actual = _spent(tweets_url, len(tweets))
        output["stats"]["api_calls"] += 1
        for t in tweets:
            t["from_list_id"] = lid
            t["from_list_name"] = lst.get("name")
            t["from_list_kind"] = kind
        output[output_field].append({
            "list_id": lid,
            "name": lst.get("name"),
            "description": lst.get("description"),
            "private": lst.get("private"),
            "member_count": lst.get("member_count"),
            "owner_id": lst.get("owner_id") or lst.get("_owner"),
            "kind": kind,
            "members": members,
            "recent_tweet_count": len(tweets),
        })
        output["list_tweets"].extend(tweets)
        print(f"      -> {len(tweets)} tweets (${actual:.4f}), members={len(members)}  (running: ${state['cost_today_usd']:.4f}/${BUDGET_USD_PER_DAY})")
        return True

    # ─────────────────────────────────────────────
    # PRIORITY 3: followed lists (the operator's curated set, config-declared)
    # ─────────────────────────────────────────────
    # NOTE: owned_lists metadata and list_memberships fetching are not wired into
    # the gather flow by default (cost). Their fetchers + the membership_allowlist
    # config knob are kept available in case you want to re-enable them.
    configured = config.get("followed_list_ids", []) or []
    print(f"  [P3] followed_lists from config ({len(configured)})...")
    flists = [{
        "id": e["id"], "name": e.get("name"), "description": "",
        "private": False, "member_count": None, "owner_id": e.get("owner"),
        "_source": "config",
    } for e in configured]
    for idx, lst in enumerate(flists):
        print(f"    [P3.{idx+1}/{len(flists)}] {lst.get('name','?')}...")
        keep_going = _process_list_block(lst, "followed", "followed_lists")
        if not keep_going:
            print("    [budget-cap] stopping followed_lists loop")
            break
        if idx < len(flists) - 1:
            time.sleep(2)

    output["scan_end_utc"] = datetime.datetime.utcnow().isoformat() + "Z"
    # Use real cost tracker, not the legacy estimate
    output["stats"]["estimated_cost_usd"] = round(state["cost_today_usd"], 4)

    # If we hit the cap, fire a Telegram heads-up (informational, non-blocking)
    if output["stats"]["budget_cap_hit"] and not args.dry_run:
        n_lists = len(output["followed_lists"]) + len([l for l in output["list_memberships"] if not l.get("is_suspicious")])
        n_tweets = len(output["list_tweets"])
        skipped = ", ".join(output["stats"]["priorities_skipped"][:5])
        msg = (f"X API daily cap reached at ${state['cost_today_usd']:.2f}/{BUDGET_USD_PER_DAY:.2f} "
               f"— digest will run on partial data ({n_lists} lists, {n_tweets} tweets fetched). "
               f"Skipped: {skipped}")
        _alert(msg, dry_run=args.dry_run)

    if args.dry_run:
        print("\n== DRY RUN -- no writes ==")
        print("  api_calls: " + str(output["stats"]["api_calls"]))
        print("  cost:      $" + str(output["stats"]["estimated_cost_usd"]))
        print("  errors:    " + str(output["stats"]["errors"]))
        return

    tmp = OUTPUT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(output, f, indent=2)
    os.rename(tmp, OUTPUT_PATH)
    save_state(state)

    print("\n== Done ==")
    print("  wrote: " + OUTPUT_PATH)
    print("  api_calls: " + str(output["stats"]["api_calls"]) +
          " ($" + str(output["stats"]["estimated_cost_usd"]) + ")")
    print("  errors: " + str(output["stats"]["errors"]))


if __name__ == "__main__":
    main()
