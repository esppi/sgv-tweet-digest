#!/usr/bin/env python3
"""SQLite layer for SGV digest v3 — feedback loop + good content.

Single source of truth for the schema. Idempotent init_schema() runs on every import.

Tables:
  our_tweets               — every tweet from the personal + fund accounts
  tweet_metrics_snapshots  — many-per-tweet metric rows over time
  tweet_ideas              — every idea our pipeline generates
  tweet_idea_matches       — links our_tweets <-> tweet_ideas
  good_content             — URLs the operator flagged
  good_content_drafts      — voice-specific drafts per good_content item
  feedback_profiles        — daily snapshot of "what's working" recommendations

Stdlib-only. The DB path is overridable via SGV_FEEDBACK_DB; by default it lives
OUTSIDE the skill dir under the XDG data home so per-user runtime state is never
committed alongside the code.
"""
import os, sqlite3, json, datetime, threading
from pathlib import Path

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
def _default_db_path():
    """feedback.db under ${XDG_DATA_HOME:-$HOME/.local/share}/sgv-tweet-digest/."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(base, "sgv-tweet-digest", "feedback.db")


DB_PATH = os.environ.get("SGV_FEEDBACK_DB", _default_db_path())

_LOCAL = threading.local()


def _conn():
    """Per-thread sqlite3 connection. Auto-creates the parent dir + schema."""
    c = getattr(_LOCAL, "conn", None)
    if c is not None:
        return c
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA synchronous = NORMAL")
    _LOCAL.conn = c
    return c


def init_schema():
    """Create all tables + indexes. Idempotent."""
    c = _conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS our_tweets (
      tweet_id            TEXT PRIMARY KEY,
      account             TEXT NOT NULL,
      user_id             TEXT NOT NULL,
      text                TEXT NOT NULL,
      created_at          TEXT NOT NULL,
      fetched_at          TEXT NOT NULL,
      is_reply            INTEGER DEFAULT 0,
      is_quote            INTEGER DEFAULT 0,
      is_retweet          INTEGER DEFAULT 0,
      conversation_id     TEXT,
      in_reply_to_user_id TEXT,
      referenced_tweet_id TEXT,
      url                 TEXT,
      lang                TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_our_tweets_account_created
      ON our_tweets(account, created_at DESC);

    CREATE TABLE IF NOT EXISTS tweet_metrics_snapshots (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      tweet_id     TEXT NOT NULL,
      snapshot_at  TEXT NOT NULL,
      age_hours    INTEGER,
      likes        INTEGER DEFAULT 0,
      retweets     INTEGER DEFAULT 0,
      replies      INTEGER DEFAULT 0,
      quotes       INTEGER DEFAULT 0,
      bookmarks    INTEGER DEFAULT 0,
      impressions  INTEGER DEFAULT 0,
      raw_json     TEXT,
      FOREIGN KEY (tweet_id) REFERENCES our_tweets(tweet_id)
    );
    CREATE INDEX IF NOT EXISTS idx_metrics_tweet_age
      ON tweet_metrics_snapshots(tweet_id, age_hours);
    CREATE INDEX IF NOT EXISTS idx_metrics_snapshot_at
      ON tweet_metrics_snapshots(snapshot_at DESC);

    CREATE TABLE IF NOT EXISTS tweet_ideas (
      id               INTEGER PRIMARY KEY AUTOINCREMENT,
      generated_at     TEXT NOT NULL,
      digest_run_id    TEXT,
      source_module    TEXT NOT NULL,
      voice            TEXT NOT NULL,
      topic            TEXT,
      draft            TEXT NOT NULL,
      inspo_kind       TEXT,
      inspo_id         TEXT,
      inspo_label      TEXT,
      inspo_snippet    TEXT,
      inspo_url        TEXT,
      viral_hooks_json TEXT,
      raw_idea_json    TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_ideas_generated  ON tweet_ideas(generated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_ideas_voice_mod  ON tweet_ideas(voice, source_module);
    CREATE INDEX IF NOT EXISTS idx_ideas_digest_run ON tweet_ideas(digest_run_id);

    CREATE TABLE IF NOT EXISTS tweet_idea_matches (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      our_tweet_id  TEXT NOT NULL,
      idea_id       INTEGER NOT NULL,
      similarity    REAL,
      confidence    TEXT,
      method        TEXT,
      detected_at   TEXT NOT NULL,
      reason        TEXT,
      FOREIGN KEY (our_tweet_id) REFERENCES our_tweets(tweet_id),
      FOREIGN KEY (idea_id)      REFERENCES tweet_ideas(id),
      UNIQUE(our_tweet_id, idea_id)
    );
    CREATE INDEX IF NOT EXISTS idx_matches_tweet ON tweet_idea_matches(our_tweet_id);
    CREATE INDEX IF NOT EXISTS idx_matches_idea  ON tweet_idea_matches(idea_id);

    CREATE TABLE IF NOT EXISTS good_content (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      url             TEXT UNIQUE NOT NULL,
      url_canonical   TEXT,
      submitted_at    TEXT NOT NULL,
      submitted_via   TEXT,
      source_kind     TEXT,
      title           TEXT,
      author          TEXT,
      description     TEXT,
      full_text       TEXT,
      full_text_chars INTEGER,
      ingested_at     TEXT,
      status          TEXT DEFAULT 'awaiting_confirmation',
      ingest_error    TEXT,
      david_note      TEXT,
      tg_message_id   INTEGER,
      tg_chat_id      INTEGER,
      reminded_at     TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_good_content_status ON good_content(status);
    CREATE INDEX IF NOT EXISTS idx_good_content_sub_at ON good_content(submitted_at DESC);

    CREATE TABLE IF NOT EXISTS good_content_drafts (
      id               INTEGER PRIMARY KEY AUTOINCREMENT,
      content_id       INTEGER NOT NULL,
      voice            TEXT NOT NULL,
      why_david_liked  TEXT,
      draft            TEXT NOT NULL,
      generated_at     TEXT NOT NULL,
      FOREIGN KEY (content_id) REFERENCES good_content(id)
    );
    CREATE INDEX IF NOT EXISTS idx_gc_drafts_content ON good_content_drafts(content_id);

    CREATE TABLE IF NOT EXISTS feedback_profiles (
      id                   INTEGER PRIMARY KEY AUTOINCREMENT,
      generated_at         TEXT NOT NULL,
      lookback_days        INTEGER,
      matched_tweets       INTEGER,
      total_tweets         INTEGER,
      archetype_stats_json TEXT,
      recommendations_json TEXT,
      full_profile_json    TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_fp_generated ON feedback_profiles(generated_at DESC);

    CREATE TABLE IF NOT EXISTS inbox_poll_state (
      key        TEXT PRIMARY KEY,
      value      TEXT,
      updated_at TEXT
    );

    -- Phase 1+2: per-day measurement of whether the model shifted toward the feedback
    -- profile's do_more recommendations, and whether that shift produced engagement.
    CREATE TABLE IF NOT EXISTS steering_metrics (
      id                     INTEGER PRIMARY KEY AUTOINCREMENT,
      computed_at            TEXT NOT NULL,
      feedback_profile_id    INTEGER NOT NULL,
      prior_profile_id       INTEGER,
      ideas_under_profile    INTEGER,
      archetype_dist_json    TEXT,
      do_more_compliance     REAL,
      do_less_compliance     REAL,
      -- Phase 2 outcome columns (populated lazily once matches accumulate):
      matched_on_rec_count   INTEGER,
      matched_off_rec_count  INTEGER,
      on_rec_avg_engagement  REAL,
      off_rec_avg_engagement REAL,
      engagement_lift        REAL,
      notes                  TEXT,
      FOREIGN KEY (feedback_profile_id) REFERENCES feedback_profiles(id)
    );
    CREATE INDEX IF NOT EXISTS idx_sm_profile ON steering_metrics(feedback_profile_id);
    CREATE INDEX IF NOT EXISTS idx_sm_computed ON steering_metrics(computed_at DESC);

    -- Phase 4: per-archetype reliability so feedback_profile.py can auto-soften
    -- recommendations that consistently fail to deliver promised lift.
    -- archetype_key format: "<voice>::<source_module>::<primary_hook>"
    CREATE TABLE IF NOT EXISTS recommendation_reliability (
      id                       INTEGER PRIMARY KEY AUTOINCREMENT,
      archetype_key            TEXT UNIQUE NOT NULL,
      n_recommended            INTEGER DEFAULT 0,
      n_matched_on_rec         INTEGER DEFAULT 0,
      on_rec_total_engagement  REAL DEFAULT 0,
      off_rec_total_engagement REAL DEFAULT 0,
      reliability_score        REAL,
      suppressed_until         TEXT,
      last_updated             TEXT,
      notes                    TEXT
    );
    """)

    # Phase 0: additive columns on existing tables. SQLite doesn't support ALTER
    # TABLE ADD COLUMN IF NOT EXISTS, so probe and add lazily — idempotent.
    cols = {row[1] for row in c.execute("PRAGMA table_info(tweet_ideas)").fetchall()}
    if "feedback_profile_id" not in cols:
        c.execute("ALTER TABLE tweet_ideas ADD COLUMN feedback_profile_id INTEGER")
    if "primary_hook" not in cols:
        c.execute("ALTER TABLE tweet_ideas ADD COLUMN primary_hook TEXT")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ideas_fp ON tweet_ideas(feedback_profile_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ideas_hook ON tweet_ideas(primary_hook)")

    gc_draft_cols = {row[1] for row in c.execute("PRAGMA table_info(good_content_drafts)").fetchall()}
    if "viral_hooks_json" not in gc_draft_cols:
        c.execute("ALTER TABLE good_content_drafts ADD COLUMN viral_hooks_json TEXT")


# ─────────────────────────────────────────
# HELPERS — TWEETS
# ─────────────────────────────────────────
def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def upsert_our_tweet(t):
    """Insert or update an our_tweet row. `t` is a dict matching the schema."""
    c = _conn()
    c.execute("""
        INSERT INTO our_tweets
          (tweet_id, account, user_id, text, created_at, fetched_at,
           is_reply, is_quote, is_retweet, conversation_id, in_reply_to_user_id,
           referenced_tweet_id, url, lang)
        VALUES (:tweet_id, :account, :user_id, :text, :created_at, :fetched_at,
                :is_reply, :is_quote, :is_retweet, :conversation_id, :in_reply_to_user_id,
                :referenced_tweet_id, :url, :lang)
        ON CONFLICT(tweet_id) DO UPDATE SET
          fetched_at = excluded.fetched_at,
          text = excluded.text
    """, t)


def record_metric_snapshot(tweet_id, snapshot_at, created_at, metrics, raw_json=None):
    """Append a metric snapshot. metrics is a dict with int values for known fields."""
    c = _conn()
    age_hours = None
    try:
        dt_created = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        dt_snap = datetime.datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
        age_hours = int((dt_snap - dt_created).total_seconds() / 3600)
    except Exception:
        pass
    c.execute("""
        INSERT INTO tweet_metrics_snapshots
          (tweet_id, snapshot_at, age_hours, likes, retweets, replies, quotes,
           bookmarks, impressions, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        tweet_id, snapshot_at, age_hours,
        int(metrics.get("likes", 0) or 0),
        int(metrics.get("retweets", 0) or 0),
        int(metrics.get("replies", 0) or 0),
        int(metrics.get("quotes", 0) or 0),
        int(metrics.get("bookmarks", 0) or 0),
        int(metrics.get("impressions", 0) or 0),
        json.dumps(raw_json) if raw_json is not None else None,
    ))


def get_our_tweets_since(account=None, since_iso=None):
    """Return our_tweets rows newer than since_iso, optionally filtered by account."""
    c = _conn()
    sql = "SELECT * FROM our_tweets WHERE 1=1"
    args = []
    if account:
        sql += " AND account = ?"
        args.append(account)
    if since_iso:
        sql += " AND created_at >= ?"
        args.append(since_iso)
    sql += " ORDER BY created_at DESC"
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def latest_metrics_for_tweet(tweet_id):
    """Return the most recent metric snapshot dict for a tweet, or None."""
    c = _conn()
    r = c.execute("""
        SELECT * FROM tweet_metrics_snapshots
        WHERE tweet_id = ?
        ORDER BY snapshot_at DESC
        LIMIT 1
    """, (tweet_id,)).fetchone()
    return dict(r) if r else None


# ─────────────────────────────────────────
# HELPERS — IDEAS
# ─────────────────────────────────────────
def save_idea(idea):
    """Insert one tweet_ideas row. `idea` is a dict matching the schema. Returns id.

    Phase-0 additions: optional `feedback_profile_id` and `primary_hook`.
    If `primary_hook` is omitted, it's derived as `viral_hooks[0]` (or "no_hook").
    """
    c = _conn()
    viral_hooks = idea.get("viral_hooks") or []
    if not isinstance(viral_hooks, list):
        viral_hooks = []
    primary_hook = idea.get("primary_hook")
    if not primary_hook:
        primary_hook = viral_hooks[0] if viral_hooks else "no_hook"
    cur = c.execute("""
        INSERT INTO tweet_ideas
          (generated_at, digest_run_id, source_module, voice, topic, draft,
           inspo_kind, inspo_id, inspo_label, inspo_snippet, inspo_url,
           viral_hooks_json, raw_idea_json,
           feedback_profile_id, primary_hook)
        VALUES (:generated_at, :digest_run_id, :source_module, :voice, :topic, :draft,
                :inspo_kind, :inspo_id, :inspo_label, :inspo_snippet, :inspo_url,
                :viral_hooks_json, :raw_idea_json,
                :feedback_profile_id, :primary_hook)
    """, {
        "generated_at":     idea.get("generated_at", now_iso()),
        "digest_run_id":    idea.get("digest_run_id"),
        "source_module":    idea["source_module"],
        "voice":            idea["voice"],
        "topic":            idea.get("topic"),
        "draft":            idea["draft"],
        "inspo_kind":       idea.get("inspo_kind"),
        "inspo_id":         idea.get("inspo_id"),
        "inspo_label":      idea.get("inspo_label"),
        "inspo_snippet":    idea.get("inspo_snippet"),
        "inspo_url":        idea.get("inspo_url"),
        "viral_hooks_json": json.dumps(viral_hooks) if not isinstance(idea.get("viral_hooks_json"), str) else idea["viral_hooks_json"],
        "raw_idea_json":    json.dumps(idea.get("raw")) if idea.get("raw") else None,
        "feedback_profile_id": idea.get("feedback_profile_id"),
        "primary_hook":     primary_hook,
    })
    return cur.lastrowid


def get_recent_ideas(days=14, voice=None, source_module=None):
    """Return tweet_ideas generated in the last N days, optionally filtered."""
    c = _conn()
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
    sql = "SELECT * FROM tweet_ideas WHERE generated_at >= ?"
    args = [cutoff]
    if voice:
        sql += " AND voice = ?"
        args.append(voice)
    if source_module:
        sql += " AND source_module = ?"
        args.append(source_module)
    sql += " ORDER BY generated_at DESC"
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def save_match(our_tweet_id, idea_id, similarity, confidence, method, reason):
    """Insert a tweet_idea_match. Ignores duplicates via UNIQUE constraint."""
    c = _conn()
    try:
        c.execute("""
            INSERT INTO tweet_idea_matches
              (our_tweet_id, idea_id, similarity, confidence, method, detected_at, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (our_tweet_id, idea_id, similarity, confidence, method, now_iso(), reason))
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]
    except sqlite3.IntegrityError:
        return None  # duplicate match


# ─────────────────────────────────────────
# HELPERS — GOOD CONTENT
# ─────────────────────────────────────────
def upsert_good_content(url, **kwargs):
    """Insert a good_content row in status=awaiting_confirmation. Returns the row id.
    If the URL already exists, returns the existing id without changing state.
    """
    c = _conn()
    r = c.execute("SELECT id, status FROM good_content WHERE url = ?", (url,)).fetchone()
    if r:
        return r["id"]
    cur = c.execute("""
        INSERT INTO good_content
          (url, url_canonical, submitted_at, submitted_via, source_kind,
           title, author, description, david_note, tg_message_id, tg_chat_id, status)
        VALUES (:url, :url_canonical, :submitted_at, :submitted_via, :source_kind,
                :title, :author, :description, :david_note, :tg_message_id, :tg_chat_id,
                :status)
    """, {
        "url": url,
        "url_canonical":  kwargs.get("url_canonical"),
        "submitted_at":   kwargs.get("submitted_at", now_iso()),
        "submitted_via":  kwargs.get("submitted_via", "telegram_dm"),
        "source_kind":    kwargs.get("source_kind"),
        "title":          kwargs.get("title"),
        "author":         kwargs.get("author"),
        "description":    kwargs.get("description"),
        "david_note":     kwargs.get("david_note"),
        "tg_message_id":  kwargs.get("tg_message_id"),
        "tg_chat_id":     kwargs.get("tg_chat_id"),
        "status":         kwargs.get("status", "awaiting_confirmation"),
    })
    return cur.lastrowid


def set_good_content_status(content_id, status, **kwargs):
    """Update status (+ optional fields like ingested_at, full_text, ingest_error)."""
    c = _conn()
    fields = ["status = ?"]
    args = [status]
    for k in ("ingested_at", "full_text", "full_text_chars", "ingest_error",
              "title", "author", "description", "source_kind", "url_canonical",
              "reminded_at"):
        if k in kwargs:
            fields.append(f"{k} = ?")
            args.append(kwargs[k])
    args.append(content_id)
    c.execute(f"UPDATE good_content SET {', '.join(fields)} WHERE id = ?", args)


def get_pending_good_content_for_digest(limit=3):
    """Return up to `limit` good_content rows ready for digest inclusion.
    Status='ingested' AND no draft yet, oldest first."""
    c = _conn()
    return [dict(r) for r in c.execute("""
        SELECT gc.* FROM good_content gc
        WHERE gc.status = 'ingested'
          AND NOT EXISTS (
            SELECT 1 FROM good_content_drafts d WHERE d.content_id = gc.id
          )
        ORDER BY gc.submitted_at ASC
        LIMIT ?
    """, (limit,)).fetchall()]


def get_pending_good_content_with_drafts(limit=3):
    """Return up to `limit` good_content rows that have drafts but haven't been
    used in a digest yet (reminded_at IS NULL)."""
    c = _conn()
    rows = c.execute("""
        SELECT gc.* FROM good_content gc
        WHERE gc.status IN ('ingested', 'drafted')
          AND gc.reminded_at IS NULL
          AND EXISTS (
            SELECT 1 FROM good_content_drafts d WHERE d.content_id = gc.id
          )
        ORDER BY gc.submitted_at ASC
        LIMIT ?
    """, (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["drafts"] = [dict(x) for x in c.execute(
            "SELECT * FROM good_content_drafts WHERE content_id = ? ORDER BY voice",
            (d["id"],)
        ).fetchall()]
        out.append(d)
    return out


def save_good_content_draft(content_id, voice, why_david_liked, draft, viral_hooks=None):
    """Save a voice-specific draft. Idempotent: replaces existing draft for same (content_id, voice).

    `viral_hooks` is the per-voice array Sonnet emitted (first element is primary_hook).
    """
    c = _conn()
    c.execute("DELETE FROM good_content_drafts WHERE content_id = ? AND voice = ?",
              (content_id, voice))
    c.execute("""
        INSERT INTO good_content_drafts
          (content_id, voice, why_david_liked, draft, generated_at, viral_hooks_json)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (content_id, voice, why_david_liked, draft, now_iso(),
          json.dumps(viral_hooks) if viral_hooks else None))


# ─────────────────────────────────────────
# HELPERS — FEEDBACK PROFILE
# ─────────────────────────────────────────
def save_feedback_profile(profile):
    """Insert a feedback_profiles row. Returns id."""
    c = _conn()
    cur = c.execute("""
        INSERT INTO feedback_profiles
          (generated_at, lookback_days, matched_tweets, total_tweets,
           archetype_stats_json, recommendations_json, full_profile_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        profile.get("generated_at", now_iso()),
        profile.get("lookback_days"),
        profile.get("matched_tweets"),
        profile.get("total_tweets"),
        json.dumps(profile.get("archetype_stats", {})),
        json.dumps(profile.get("recommendations", {})),
        json.dumps(profile),
    ))
    return cur.lastrowid


def get_latest_feedback_profile():
    """Return the most recent feedback_profile row, or None."""
    c = _conn()
    r = c.execute("""
        SELECT * FROM feedback_profiles
        ORDER BY generated_at DESC
        LIMIT 1
    """).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("archetype_stats_json", "recommendations_json", "full_profile_json"):
        if d.get(k):
            try:
                d[k.replace("_json", "")] = json.loads(d[k])
            except Exception:
                d[k.replace("_json", "")] = None
    return d


# ─────────────────────────────────────────
# HELPERS — INBOX POLL STATE
# ─────────────────────────────────────────
def get_poll_state(key):
    c = _conn()
    r = c.execute("SELECT value FROM inbox_poll_state WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def set_poll_state(key, value):
    c = _conn()
    c.execute("""
        INSERT INTO inbox_poll_state (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
    """, (key, value, now_iso()))


# ─────────────────────────────────────────
# AUTO-INIT
# ─────────────────────────────────────────
init_schema()


if __name__ == "__main__":
    import sys
    init_schema()
    c = _conn()
    print(f"feedback.db at: {DB_PATH}")
    print(f"schema:")
    for row in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        n = row["name"]
        cols = c.execute(f"PRAGMA table_info({n})").fetchall()
        print(f"  {n}: {len(cols)} cols")
    print(f"counts:")
    for tbl in ["our_tweets", "tweet_metrics_snapshots", "tweet_ideas",
                "tweet_idea_matches", "good_content", "good_content_drafts",
                "feedback_profiles"]:
        n = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl}: {n} rows")
