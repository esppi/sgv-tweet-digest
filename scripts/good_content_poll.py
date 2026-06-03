#!/usr/bin/env python3
"""Poll Telegram Saved Messages for 'good content:' / 'gc:' submissions and ingest immediately.

Single-stage flow — no YES/NO confirmation:

  operator sends "gc: https://..." in Saved Messages
        →  fetch content (article/YT/X tweet) right away
        →  save to feedback.db with status='ingested' (or 'failed')
        →  reply to the Saved Message with status + title + char count

Idempotent: same URL re-submitted returns "already saved" without re-fetching.
Safe to re-run frequently (every 10 min).

Config / secrets are read from the environment (no hardcoded home, host, account,
chat, or bot). Telegram credentials come from telegram/auth.py (TELEGRAM_API_ID /
TELEGRAM_API_HASH / TELEGRAM_SESSION). The feedback.db path comes from feedback_db
(SGV_FEEDBACK_DB). The yt-dlp binary is resolved from YTDLP_BIN (or PATH).
"""
import os, sys, json, re, subprocess, asyncio, datetime, urllib.request, urllib.parse, tempfile, shutil
from pathlib import Path

# Allow sibling imports (feedback_db.py lives next to this file)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import feedback_db as db

# Shared TG auth helper (API_ID, API_HASH, canonical session path, Telethon import)
# lives under scripts/telegram/.
sys.path.insert(0, os.path.join(HERE, "telegram"))
from auth import make_client

try:
    import trafilatura
except ImportError:
    trafilatura = None

# ─── CONFIG ───
# yt-dlp binary path, parametrized so no host-specific path is baked in.
YTDLP = os.environ.get("YTDLP_BIN") or shutil.which("yt-dlp") or "/usr/bin/yt-dlp"
MAX_FULLTEXT_CHARS = 30_000

# Chats to poll. (chat, cursor_key, only_outgoing).
# - "me" is Saved Messages (chat with yourself); all messages are by you.
# - bot DMs: filter to msg.out=True so we don't process the bot's own replies.
#   The bot handle (if any) is read from TELEGRAM_GC_BOT to avoid hardcoding it;
#   unset → only Saved Messages is polled.
POLL_SOURCES = [("me", "saved_messages_last_seen_id", False)]
_GC_BOT = os.environ.get("TELEGRAM_GC_BOT", "").strip()
if _GC_BOT:
    POLL_SOURCES.append((_GC_BOT, f"{_GC_BOT}_last_seen_id", True))

KEY_LAST_SEEN_ID = "saved_messages_last_seen_id"  # legacy compat

# Where status replies (✅/❌/ℹ️/⚠️) get sent. Always Saved Messages so the user has
# one consistent place to see capture confirmations, regardless of which chat they
# submitted from.
REPLY_CHAT = "me"

URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)
# Match "good content" or "gc" as the leading prefix; the URL can appear
# anywhere after that. Colon/dash separator is optional (so "Good content <url>"
# works, matching how people naturally type).
GC_PREFIX_RE = re.compile(r"^(good content|gc)\b\s*[:\-]?\s*", re.IGNORECASE)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _extract_first_url(text):
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(1).rstrip(".,;)>]") if m else None


def _parse_submission(text):
    """If `text` matches `gc:`/`good content:` prefix, return (url_or_None, note).
    Returns (None, None) if not a submission at all."""
    if not text:
        return None, None
    t = text.strip()
    m = GC_PREFIX_RE.match(t)
    if not m:
        return None, None
    rest = t[m.end():].strip()
    url = _extract_first_url(rest)
    note = (rest.replace(url, "").strip() if url else rest) or None
    return url, note


# ─── KIND DETECTION ───
def detect_kind(url):
    u = url.lower()
    if "youtube.com/watch" in u or "youtu.be/" in u or "youtube.com/shorts" in u:
        return "youtube"
    if re.search(r"\b(x\.com|twitter\.com)/[^/]+/status/\d+", u):
        return "x_tweet"
    return "article"


# ─── FETCH HELPERS ───
def _http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 SGV-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_article(url):
    """Returns (title, description, body, author, error)."""
    if trafilatura is None:
        return None, None, None, None, "trafilatura not installed"
    try:
        raw_bytes = _http_get(url, timeout=20)
    except Exception as e:
        return None, None, None, None, f"http fetch failed: {e}"

    raw_html = raw_bytes.decode("utf-8", errors="ignore") if isinstance(raw_bytes, bytes) else raw_bytes
    if not raw_html or len(raw_html) < 200:
        return None, None, None, None, f"empty or tiny html ({len(raw_html)} chars)"

    try:
        meta = trafilatura.extract_metadata(raw_html)
        title = (meta.title if meta else None) or None
        desc = (meta.description if meta else None) or None
        author = (meta.author if meta else None) or None
    except Exception:
        title = desc = author = None

    text = None
    try:
        text = trafilatura.extract(raw_html, favor_recall=True,
                                   include_comments=False, include_tables=False)
    except Exception as e:
        return title, desc, None, author, f"trafilatura.extract failed: {e}"

    if not text:
        m = re.search(r"<title>(.*?)</title>", raw_html, re.IGNORECASE | re.DOTALL)
        if m and not title:
            title = re.sub(r"\s+", " ", m.group(1).strip())[:200]
        return title, desc, (desc or title or "")[:MAX_FULLTEXT_CHARS], author, "no body extracted, using metadata"

    return title, desc, text[:MAX_FULLTEXT_CHARS], author, None


def _youtube_oembed(url):
    """Fetch YouTube metadata via the public oEmbed endpoint. No auth, not
    blocked from cloud IPs (unlike yt-dlp + youtube-transcript-api which both
    get IP-blocked from some hosts). Returns (title, author) or (None, None)."""
    try:
        oembed_url = "https://www.youtube.com/oembed?url=" + urllib.parse.quote(url, safe='') + "&format=json"
        raw = _http_get(oembed_url, timeout=10)
        d = json.loads(raw)
        return d.get("title"), d.get("author_name")
    except Exception:
        return None, None


def fetch_youtube(url):
    """Try yt-dlp for full metadata + transcript first. If yt-dlp is blocked
    (YouTube blacklists cloud-provider IPs), fall back to oEmbed for at least
    title + author. Returns (title, desc, body, author, error)."""
    title = desc = body = author = None

    # Step 1: try yt-dlp metadata
    yt_dlp_blocked = False
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            r = subprocess.run(
                [YTDLP, "--dump-single-json", "--no-warnings", "--skip-download", url],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                meta = json.loads(r.stdout)
                title = meta.get("title")
                desc = (meta.get("description") or "")[:1000]
                author = meta.get("uploader") or meta.get("channel")
            else:
                yt_dlp_blocked = "Sign in to confirm" in (r.stderr or "") or "bot" in (r.stderr or "").lower()
        except Exception:
            yt_dlp_blocked = True

        # Step 2: if yt-dlp failed, fall back to oEmbed for title/author only
        if not title:
            oem_title, oem_author = _youtube_oembed(url)
            if oem_title:
                title = oem_title
                author = oem_author
                # No transcript path possible if yt-dlp is blocked. Return
                # a metadata-only success so the URL still gets captured.
                body = f"[YouTube video — transcript unavailable from this server's IP]\nTitle: {title}\nChannel: {author or '(unknown)'}\nURL: {url}"
                err = "youtube_ip_blocked: metadata captured via oEmbed, no transcript"
                return title, desc, body[:MAX_FULLTEXT_CHARS], author, err
            return None, None, None, None, "yt-dlp metadata failed AND oEmbed fallback failed"

        try:
            r = subprocess.run([
                YTDLP, "--no-warnings", "--skip-download",
                "--write-auto-subs", "--write-subs",
                "--sub-langs", "en.*,en",
                "--sub-format", "vtt",
                "-o", os.path.join(tmpdir, "%(id)s.%(ext)s"),
                url,
            ], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return title, desc, desc[:MAX_FULLTEXT_CHARS], author, "yt-dlp subs timeout"
        except Exception as e:
            return title, desc, desc[:MAX_FULLTEXT_CHARS], author, f"yt-dlp subs error: {e}"

        vtt_files = list(Path(tmpdir).glob("*.vtt"))
        if not vtt_files:
            return title, desc, (desc or "")[:MAX_FULLTEXT_CHARS], author, "no_subs_available"

        with open(vtt_files[0]) as f:
            vtt = f.read()

        body_lines, seen = [], set()
        for line in vtt.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line == "WEBVTT" or "-->" in line or line.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
                continue
            clean = re.sub(r"<[^>]+>", "", line).strip()
            if clean and clean not in seen:
                seen.add(clean)
                body_lines.append(clean)
        body = " ".join(body_lines)[:MAX_FULLTEXT_CHARS]
        return title, desc, body, author, None


def fetch_x_tweet(url):
    """Use the X API to fetch a single tweet by ID. Cost: ~$0.005.

    The OAuth2 user-context token (falling back to the app-only Bearer) is
    sourced from gather_x, the sibling X-API helper used by the rest of the
    pipeline — so credentials come from the same X_OAUTH2_* / X_BEARER_TOKEN
    env vars and nothing is hardcoded.
    """
    m = re.search(r"/status/(\d+)", url)
    if not m:
        return None, None, None, None, "could not parse tweet_id from URL"
    tweet_id = m.group(1)

    try:
        from gather_x import _oauth2_access_token, bearer_token
        try:
            app_only = bearer_token()
        except Exception:
            app_only = None
        token = _oauth2_access_token() or app_only
    except Exception as e:
        return None, None, None, None, f"could not load X credentials: {e}"
    if not token:
        return None, None, None, None, "no X credentials available (set X_OAUTH2_* or X_BEARER_TOKEN)"

    api_url = (
        f"https://api.x.com/2/tweets/{tweet_id}?"
        f"tweet.fields=created_at,public_metrics,author_id,text&"
        f"expansions=author_id&"
        f"user.fields=username,name"
    )
    try:
        req = urllib.request.Request(api_url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return None, None, None, None, f"X API error: {e}"

    t = data.get("data") or {}
    users = {u["id"]: u for u in (data.get("includes", {}).get("users") or [])}
    author = users.get(t.get("author_id"), {})
    author_handle = author.get("username")
    title = f"@{author_handle} tweet" if author_handle else "X tweet"
    desc = t.get("text", "")[:500]
    body = t.get("text", "")[:MAX_FULLTEXT_CHARS]
    return title, desc, body, author_handle, None


# ─── DB HELPERS ───
def _existing_row_by_url(url):
    """Return the existing good_content row for `url`, or None."""
    c = db._conn()
    r = c.execute("SELECT * FROM good_content WHERE url = ?", (url,)).fetchone()
    return dict(r) if r else None


def fetch_and_save(row_id, url):
    """Fetch URL based on detected kind, persist to DB. Returns (status, title, body_len, err)."""
    kind = detect_kind(url)
    try:
        if kind == "youtube":
            title, desc, body, author, err = fetch_youtube(url)
        elif kind == "x_tweet":
            title, desc, body, author, err = fetch_x_tweet(url)
        else:
            title, desc, body, author, err = fetch_article(url)
    except Exception as e:
        title = desc = body = author = None
        err = f"unhandled exception: {e}"

    if not body or (err and not (body or "").strip()):
        db.set_good_content_status(
            row_id, "failed",
            ingest_error=err or "no body extracted",
            source_kind=kind,
        )
        return "failed", title, 0, err

    db.set_good_content_status(
        row_id, "ingested",
        title=title,
        author=author,
        description=desc,
        full_text=body,
        full_text_chars=len(body),
        source_kind=kind,
        ingested_at=db.now_iso(),
    )
    return "ingested", title, len(body), None


# ─── TELEGRAM ───
async def _send(client, chat, text, reply_to=None):
    try:
        await client.send_message(chat, text, reply_to=reply_to)
        return True
    except Exception as e:
        print(f"  send failed: {e}")
        return False


def _format_reply(status, row_id, url, kind, title, body_len, err):
    if status == "already":
        title_str = f": {title}" if title else ""
        return f"ℹ️ already saved gc#{row_id}{title_str}\n{url}"
    if status == "ingested":
        title_str = title[:120] if title else "(no title)"
        return f"✅ saved gc#{row_id} ({kind}, {body_len:,} chars)\n{title_str}\n{url}"
    return f"❌ failed gc#{row_id} ({kind}): {err or 'unknown error'}\n{url}"


async def _process_message(client, msg, source_label, counters, log):
    """Apply gc-submission logic to one message. Status replies go to REPLY_CHAT
    so the user has one consistent place to see capture confirmations.
    `source_label` is shown in cross-chat replies (e.g., "from bot DM")."""
    text = msg.text or msg.message or ""
    url, note = _parse_submission(text)
    if url is None and note is None:
        return  # not a gc submission

    same_chat = (source_label == "saved")
    reply_to = msg.id if same_chat else None
    src_tag = "" if same_chat else f" (from {source_label})"

    if url is None:
        log(f"  msg#{msg.id} 'gc:' without URL — replying")
        await _send(client, REPLY_CHAT,
                    f"⚠️ I saw `gc:` but no URL{src_tag}. Send `gc: https://...` to ingest.",
                    reply_to=reply_to)
        counters["no_url"] += 1
        return

    counters["new"] += 1
    existing = _existing_row_by_url(url)

    if existing and existing.get("status") == "ingested":
        reply = _format_reply("already", existing["id"], url,
                              existing.get("source_kind"), existing.get("title"),
                              existing.get("full_text_chars") or 0, None)
        await _send(client, REPLY_CHAT, reply + src_tag, reply_to=reply_to)
        counters["already"] += 1
        log(f"  msg#{msg.id} ({source_label}) already-saved gc#{existing['id']}")
        return

    submitted_at = (msg.date.astimezone(datetime.timezone.utc).isoformat()
                    if msg.date else _now().isoformat())
    row_id = db.upsert_good_content(
        url=url,
        submitted_at=submitted_at,
        submitted_via=f"telegram_{source_label}",
        david_note=note,
        tg_message_id=msg.id,
        tg_chat_id=msg.chat_id,
        status="pending",
    )

    kind = detect_kind(url)
    log(f"  msg#{msg.id} ({source_label}) gc#{row_id} ({kind}) {url[:80]} — fetching")
    status, title, body_len, err = fetch_and_save(row_id, url)
    log(f"    -> {status}" + (f" ({body_len:,} chars)" if status == "ingested" else f" ({err})"))

    await _send(client, REPLY_CHAT,
                _format_reply(status, row_id, url, kind, title, body_len, err) + src_tag,
                reply_to=reply_to)
    counters["ingested" if status == "ingested" else "failed"] += 1


async def poll(verbose=True):
    log = print if verbose else (lambda *a, **k: None)
    t0 = datetime.datetime.now()
    log(f"== good-content poll {_now().isoformat()} ==")

    db.init_schema()

    counters = {"new": 0, "ingested": 0, "failed": 0, "already": 0, "no_url": 0}

    async with make_client() as client:
        for chat_target, cursor_key, only_outgoing in POLL_SOURCES:
            source_label = "saved" if chat_target == "me" else chat_target

            try:
                entity = await client.get_entity(chat_target) if chat_target != "me" else "me"
            except Exception as e:
                log(f"  [skip] could not resolve {chat_target}: {e}")
                continue

            last_seen_raw = db.get_poll_state(cursor_key)
            last_seen_id = int(last_seen_raw) if last_seen_raw and last_seen_raw.isdigit() else 0

            if last_seen_id == 0:
                # First run for this source — set cursor to current max so we don't
                # backfill arbitrarily-old history. Use a manual backfill script if needed.
                latest = await client.get_messages(entity, limit=1)
                if latest:
                    db.set_poll_state(cursor_key, str(latest[0].id))
                    log(f"  [{source_label}] first run — cursor set to {latest[0].id}")
                continue

            new_max_id = last_seen_id
            seen_in_source = 0
            async for msg in client.iter_messages(entity, min_id=last_seen_id, reverse=True):
                if only_outgoing and not getattr(msg, "out", False):
                    new_max_id = max(new_max_id, msg.id)
                    continue
                new_max_id = max(new_max_id, msg.id)
                seen_in_source += 1
                await _process_message(client, msg, source_label, counters, log)

            if new_max_id > last_seen_id:
                db.set_poll_state(cursor_key, str(new_max_id))
                log(f"  [{source_label}] scanned {seen_in_source} new messages, cursor → {new_max_id}")

    duration = (datetime.datetime.now() - t0).total_seconds()
    log(f"== DONE in {duration:.1f}s | "
        f"new={counters['new']} ingested={counters['ingested']} "
        f"failed={counters['failed']} already={counters['already']} no_url={counters['no_url']} ==")
    return {**counters, "duration_s": round(duration, 1)}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()
    result = asyncio.run(poll(verbose=not args.quiet))
    print(json.dumps(result, indent=2))
