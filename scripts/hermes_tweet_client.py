"""Optional Hermes Tweet / Xquik read client for SGV Tweet Digest.

The digest keeps the X API path as the default. This module is used only when
SGV_X_READ_BACKEND=hermes/xquik, or when no X credentials are configured and a
Hermes Tweet / Xquik API key is available.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_BASE_URL = "https://xquik.com"
BACKEND_ENV = "SGV_X_READ_BACKEND"


def _clean_base_url(value):
    return (value or DEFAULT_BASE_URL).rstrip("/")


def _api_key():
    return os.environ.get("HERMES_TWEET_API_KEY") or os.environ.get("XQUIK_API_KEY")


def _base_url():
    return _clean_base_url(
        os.environ.get("HERMES_TWEET_BASE_URL") or os.environ.get("XQUIK_BASE_URL")
    )


def has_hermes_credentials():
    return bool(_api_key())


def hermes_read_backend_enabled(has_x_credentials):
    backend = os.environ.get(BACKEND_ENV, "auto").strip().lower()
    if backend in ("x", "twitter", "x-api", "off", "false", "0"):
        return False
    if backend in ("hermes", "xquik"):
        return has_hermes_credentials()
    return (not has_x_credentials) and has_hermes_credentials()


def _headers():
    key = _api_key()
    if not key:
        raise RuntimeError("Set HERMES_TWEET_API_KEY or XQUIK_API_KEY to use Hermes Tweet reads.")
    if key.startswith("xq_"):
        return {"x-api-key": key, "Accept": "application/json"}
    return {"Authorization": "Bearer " + key, "Accept": "application/json"}


def _request(path, params=None):
    query = urllib.parse.urlencode(params or {}, doseq=True)
    url = _base_url() + path + (("?" + query) if query else "")
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("Hermes Tweet read failed: HTTP " + str(exc.code) + ": " + body[:300])


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _first_present(data, keys):
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _unwrap_record(record, preferred):
    current = record
    for key in preferred:
        nested = _as_dict(current).get(key)
        if isinstance(nested, dict):
            current = nested
    return current


def _find_list(value, keys):
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    for key in keys:
        item = value.get(key)
        if isinstance(item, list):
            return item
        if isinstance(item, dict):
            nested = _find_list(item, keys)
            if nested:
                return nested
    for item in value.values():
        if isinstance(item, dict):
            nested = _find_list(item, keys)
            if nested:
                return nested
    return []


def _meta(value):
    if not isinstance(value, dict):
        return {}
    for key in ("meta", "pagination", "pageInfo"):
        item = value.get(key)
        if isinstance(item, dict):
            return item
    data = value.get("data")
    if isinstance(data, dict):
        return _meta(data)
    return {}


def _next_cursor(payload):
    meta = _meta(payload)
    value = _first_present(
        meta,
        ("next_token", "nextToken", "next_cursor", "nextCursor", "cursor", "after"),
    )
    return str(value) if value not in (None, "") else None


def _metric(data, names):
    value = _first_present(data, names)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_user(record):
    data = _as_dict(_unwrap_record(record, ("user", "author", "profile")))
    user_id = _first_present(data, ("id", "id_str", "rest_id", "user_id", "userId"))
    username = _first_present(data, ("username", "userName", "screen_name", "handle"))
    metrics = _as_dict(data.get("public_metrics") or data.get("metrics"))
    followers = _metric(
        metrics or data,
        ("followers_count", "followersCount", "followers", "follower_count"),
    )
    if user_id is None and username is None:
        return None
    return {
        "id": str(user_id or username),
        "username": str(username or user_id),
        "followers": followers,
    }


def _tweet_payload(record):
    data = _as_dict(record)
    if _first_present(data, ("id", "id_str", "tweet_id", "tweetId", "rest_id")) is not None:
        return data
    for key in ("tweet", "post", "result", "data"):
        nested = data.get(key)
        if isinstance(nested, dict):
            candidate = _tweet_payload(nested)
            if candidate:
                return candidate
    return data


def normalize_tweet(record, owned_signal):
    data = _tweet_payload(record)
    tweet_id = _first_present(data, ("id", "id_str", "tweet_id", "tweetId", "rest_id"))
    if tweet_id is None:
        return None

    author = _as_dict(data.get("author") or data.get("user"))
    author_id = _first_present(
        data,
        ("author_id", "authorId", "user_id", "userId"),
    ) or _first_present(author, ("id", "id_str", "rest_id", "user_id", "userId"))
    username = _first_present(
        data,
        ("username", "author_username", "screen_name"),
    ) or _first_present(author, ("username", "userName", "screen_name", "handle"))
    metrics = _as_dict(data.get("public_metrics") or data.get("metrics"))
    text = _first_present(data, ("text", "full_text", "fullText", "content")) or ""
    created_at = _first_present(data, ("created_at", "createdAt", "date", "time")) or ""
    tweet_type = "original"
    refs = data.get("referenced_tweets") or []
    if refs and isinstance(refs, list) and isinstance(refs[0], dict):
        tweet_type = str(refs[0].get("type") or "unknown")
    elif data.get("quoted_tweet") or data.get("quotedTweet"):
        tweet_type = "quote"
    elif data.get("in_reply_to_status_id") or data.get("in_reply_to_user_id"):
        tweet_type = "reply"

    normalized = {
        "id": str(tweet_id),
        "author_id": str(author_id or ""),
        "account": "@" + str(username or "unknown"),
        "account_id": str(author_id or ""),
        "author_followers": _metric(
            _as_dict(author.get("public_metrics") or author.get("metrics")) or author,
            ("followers_count", "followersCount", "followers", "follower_count"),
        ),
        "author_verified": _first_present(author, ("verified_type", "verifiedType", "verified")),
        "tweet_id": str(tweet_id),
        "type": tweet_type,
        "text": str(text),
        "created_at": str(created_at),
        "metrics": {
            "like_count": _metric(metrics or data, ("like_count", "favorite_count", "likes")),
            "retweet_count": _metric(metrics or data, ("retweet_count", "retweets")),
            "reply_count": _metric(metrics or data, ("reply_count", "replies")),
            "quote_count": _metric(metrics or data, ("quote_count", "quotes")),
            "bookmark_count": _metric(metrics or data, ("bookmark_count", "bookmarks")),
            "impression_count": _metric(metrics or data, ("impression_count", "views", "view_count")),
        },
        "entities": data.get("entities") or {},
        "source": "hermes_tweet_" + owned_signal,
        "owned_signal": owned_signal,
        "tweet_url": str(
            _first_present(data, ("tweet_url", "tweetUrl", "url"))
            or ("https://x.com/" + str(username or "i") + "/status/" + str(tweet_id))
        ),
    }
    normalized["public_metrics"] = dict(normalized["metrics"])
    if refs:
        normalized["referenced_tweets"] = refs
    if data.get("conversation_id") or data.get("conversationId"):
        normalized["conversation_id"] = str(data.get("conversation_id") or data.get("conversationId"))
    if data.get("lang"):
        normalized["lang"] = data.get("lang")
    if data.get("in_reply_to_user_id") or data.get("inReplyToUserId"):
        normalized["in_reply_to_user_id"] = str(
            data.get("in_reply_to_user_id") or data.get("inReplyToUserId")
        )
    return normalized


def fetch_hermes_list_members(list_id, page_size=100):
    payload = _request(
        "/api/v1/x/lists/" + urllib.parse.quote(str(list_id)) + "/members",
        {"pageSize": str(page_size)},
    )
    return [
        user
        for user in (normalize_user(item) for item in _find_list(payload, ("users", "members", "data", "items")))
        if user is not None
    ]


def fetch_hermes_list_tweets(list_id, max_results=100):
    payload = _request(
        "/api/v1/x/lists/" + urllib.parse.quote(str(list_id)) + "/tweets",
        {"includeReplies": "true"},
    )
    tweets = [
        tweet
        for tweet in (
            normalize_tweet(item, "list_tweets")
            for item in _find_list(payload, ("tweets", "posts", "data", "items"))
        )
        if tweet is not None
    ]
    return tweets[:max_results]


def fetch_hermes_following(user_id, max_results=200):
    payload = _request(
        "/api/v1/x/users/" + urllib.parse.quote(str(user_id)) + "/following",
        {"pageSize": str(max_results)},
    )
    return [
        user
        for user in (normalize_user(item) for item in _find_list(payload, ("users", "following", "data", "items")))
        if user is not None
    ][:max_results]


def fetch_hermes_user_tweets(user_id, max_results=10, pagination_token=None):
    params = {"includeReplies": "true", "includeParentTweet": "true"}
    if pagination_token:
        params["cursor"] = pagination_token
    payload = _request(
        "/api/v1/x/users/" + urllib.parse.quote(str(user_id)) + "/tweets",
        params,
    )
    tweets = [
        tweet
        for tweet in (
            normalize_tweet(item, "user_tweets")
            for item in _find_list(payload, ("tweets", "posts", "data", "items"))
        )
        if tweet is not None
    ][:max_results]
    meta = {}
    cursor = _next_cursor(payload)
    if cursor:
        meta["next_token"] = cursor
    return tweets, meta
