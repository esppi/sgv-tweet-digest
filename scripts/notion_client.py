#!/usr/bin/env python3
"""Minimal Notion REST client (direct HTTP via urllib — no MCP, no pip deps).

Vendored for the optional Notion deal-funnel source in insights.py, which does:

    from notion_client import query_deals_pool, extract_deal_summary, fetch_page_body

Config comes entirely from the environment (this repo is public — nothing is
hardcoded). The canonical env-var names match .env.example:

    NOTION_TOKEN          (secret)     internal-integration token. Required to make
                                       any request; read lazily so importing this
                                       module never fails when the source is off.
    NOTION_DEALS_DB_ID    (non-secret) database id query_deals_pool() targets when
                                       no database_id argument is passed.
    NOTION_KEY_COLUMNS    (optional)   comma-separated Status values treated as the
                                       "always include" set in the pool query.
                                       Defaults to the active funnel statuses.

insights.py gates this source on NOTION_TOKEN + NOTION_DEALS_DB_ID both being set,
so the bare query_deals_pool() call relies on the env fallback for the db id.

Stdlib only (urllib). No third-party dependency is introduced.
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# Make sibling modules under scripts/ importable, matching the repo convention
# (see our_tweets_fetcher.py / digest.py). This client imports no siblings today,
# but keeps the same import-path setup so it behaves identically wherever it runs.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Default "always include" Status values for the deals-pool query. These are
# generic deal-funnel stage labels (no company/person/deal identifiers), safe to
# ship. Override via the NOTION_KEY_COLUMNS env var (comma-separated) if your
# Notion uses different stage names.
_DEFAULT_KEY_COLUMNS = ("New Leads", "Qualified Leads", "Discussion")

# Statuses excluded from the "recently edited" branch of the pool query.
_EXCLUDED_STATUSES = (
    "Pass",
    "Missed",
    "Pass (send rejection)",
    "Revisit later",
    "Funded",
)


def _notion_token():
    """Read NOTION_TOKEN from the environment at call time (never at import).

    Raises a clear RuntimeError if unset so a misconfigured run fails loudly at
    the first request rather than crashing on import.
    """
    tok = os.environ.get("NOTION_TOKEN")
    if not tok:
        raise RuntimeError(
            "NOTION_TOKEN is not set. Create an internal integration at "
            "https://notion.so/my-integrations and export NOTION_TOKEN."
        )
    return tok.strip()


def _key_columns():
    """Status values for the 'always include' branch of the pool query.

    NOTION_KEY_COLUMNS env (comma-separated) overrides the default funnel stages.
    """
    raw = os.environ.get("NOTION_KEY_COLUMNS")
    if raw:
        cols = [c.strip() for c in raw.split(",") if c.strip()]
        if cols:
            return cols
    return list(_DEFAULT_KEY_COLUMNS)


def _deals_database_id(database_id=None):
    """Resolve the deals database id: explicit arg first, else NOTION_DEALS_DB_ID.

    Notion's query endpoint accepts the hyphenated UUID; a bare 32-char hex id is
    normalized to the 8-4-4-4-12 form. Never hardcoded.
    """
    db_id = database_id or os.environ.get("NOTION_DEALS_DB_ID")
    if not db_id:
        raise RuntimeError(
            "No Notion database id: pass database_id= or set NOTION_DEALS_DB_ID."
        )
    db_id = db_id.strip().replace("-", "")
    if len(db_id) == 32:
        return f"{db_id[0:8]}-{db_id[8:12]}-{db_id[12:16]}-{db_id[16:20]}-{db_id[20:]}"
    return db_id


def _headers():
    return {
        "Authorization": f"Bearer {_notion_token()}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def _request(method, path, body=None):
    """Issue one Notion API request. Returns parsed JSON, or {"error": ...} on failure."""
    url = f"{NOTION_API_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        print(f"Notion API {e.code}: {err_body[:200]}")
        return {"error": f"HTTP {e.code}", "body": err_body[:500]}
    except Exception as e:
        return {"error": str(e)}


def query_deals_pool(database_id=None):
    """Fetch deals from Notion: key columns OR last_edited in 14d (minus terminal statuses).

    Returns a deduplicated list of raw Notion page objects (pass each to
    extract_deal_summary). `database_id` defaults to NOTION_DEALS_DB_ID.
    """
    cutoff_14d = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    db_id = _deals_database_id(database_id)

    query = {
        "filter": {
            "or": [
                # Always-include key columns
                *[
                    {"property": "Status", "select": {"equals": col}}
                    for col in _key_columns()
                ],
                # Recently edited, excluding terminal/parked statuses
                {
                    "and": [
                        {"timestamp": "last_edited_time", "last_edited_time": {"after": cutoff_14d}},
                        *[
                            {"property": "Status", "select": {"does_not_equal": s}}
                            for s in _EXCLUDED_STATUSES
                        ],
                    ]
                },
            ]
        },
        "page_size": 100,
    }

    results = []
    cursor = None
    while True:
        if cursor:
            query["start_cursor"] = cursor
        data = _request("POST", f"/databases/{db_id}/query", query)
        if "error" in data:
            print(f"Query error: {data}")
            break
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(0.3)

    # Deduplicate by page id
    seen = set()
    unique = []
    for r in results:
        pid = r.get("id")
        if pid and pid not in seen:
            seen.add(pid)
            unique.append(r)
    return unique


def extract_deal_summary(page):
    """From a Notion page object, pull key properties into a summary dict.

    Always returns a `notion_page_id` (used by callers to fetch the page body).
    """
    props = page.get("properties", {})

    def _prop_text(name):
        p = props.get(name, {})
        t = p.get("type")
        if t == "title":
            arr = p.get("title", [])
            return "".join(x.get("plain_text", "") for x in arr)
        if t == "rich_text":
            arr = p.get("rich_text", [])
            return "".join(x.get("plain_text", "") for x in arr)
        if t == "select":
            s = p.get("select")
            return (s or {}).get("name", "") if s else ""
        if t == "status":
            s = p.get("status")
            return (s or {}).get("name", "") if s else ""
        if t == "url":
            return p.get("url") or ""
        if t == "number":
            return p.get("number")
        return ""

    return {
        "notion_page_id": page.get("id"),
        "name": _prop_text("Name") or _prop_text("Deal") or "Untitled",
        "status": _prop_text("Status"),
        "tldr": _prop_text("tl;dr") or _prop_text("Summary") or "",
        "priority": _prop_text("Priority"),
        "lead_source": _prop_text("Lead Source"),
        "valuation": _prop_text("Valuation") or _prop_text("Current val"),
        "investment": _prop_text("Investment"),
        "deck_url": _prop_text("Deck"),
        "memo_url": _prop_text("Memo"),
        "website": _prop_text("Website"),
        "last_edited": page.get("last_edited_time", ""),
        "url": page.get("url", ""),
    }


def _fetch_blocks(page_id):
    """Fetch all child blocks of a page, following pagination. Returns a list."""
    blocks = []
    cursor = None
    while True:
        path = f"/blocks/{page_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        data = _request("GET", path)
        if "error" in data:
            break
        blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(0.2)
    return blocks


def fetch_page_body(page_id):
    """Fetch all blocks on a Notion page and concatenate to markdown-ish text."""
    blocks = _fetch_blocks(page_id)

    lines = []
    for b in blocks:
        btype = b.get("type")
        block_data = b.get(btype, {}) or {}
        rich = block_data.get("rich_text", []) or []
        text = "".join(x.get("plain_text", "") for x in rich)
        if text.strip():
            if btype == "heading_1":
                lines.append(f"\n# {text}\n")
            elif btype == "heading_2":
                lines.append(f"\n## {text}\n")
            elif btype == "heading_3":
                lines.append(f"\n### {text}\n")
            elif btype == "bulleted_list_item":
                lines.append(f"- {text}")
            elif btype == "numbered_list_item":
                lines.append(f"1. {text}")
            elif btype == "to_do":
                checked = block_data.get("checked", False)
                lines.append(f"[{'x' if checked else ' '}] {text}")
            elif btype == "toggle":
                lines.append(f"▶ {text}")
            elif btype == "quote":
                lines.append(f"> {text}")
            elif btype == "code":
                lang = block_data.get("language", "")
                lines.append(f"```{lang}\n{text}\n```")
            else:
                lines.append(text)

        # Embedded URL blocks
        if btype in ("bookmark", "link_preview", "embed", "file", "video"):
            url = block_data.get("url", "")
            if url:
                lines.append(f"[link]({url})")

    return "\n".join(lines)


def fetch_page_urls(page_id):
    """Fetch all URLs in a Notion page preserving hrefs — rich_text inline links,
    bookmarks, embeds, child_page links, page mentions.

    fetch_page_body() flattens to plain_text and loses hrefs (e.g. a
    `[Website](https://...)` link becomes just `Website`); this preserves the
    actual hrefs for any URL-extraction pipeline.

    Returns: list of {url, label, context}
    """
    blocks = _fetch_blocks(page_id)

    urls = []
    for b in blocks:
        btype = b.get("type")
        block_data = b.get(btype, {}) or {}

        # 1. Inline links inside rich_text
        rich = block_data.get("rich_text", []) or []
        full_text = "".join(x.get("plain_text", "") for x in rich)
        for span in rich:
            href = span.get("href")
            if not href:
                # Sometimes href is nested under text.link
                text_obj = span.get("text", {}) or {}
                link_obj = (text_obj.get("link") or {}) if isinstance(text_obj, dict) else {}
                if isinstance(link_obj, dict):
                    href = link_obj.get("url")
            if href:
                label = span.get("plain_text", "") or ""
                urls.append({"url": href, "label": label.strip(), "context": full_text[:240]})

        # 2. Embedded URL blocks (bookmarks, link previews, files, videos, pdfs)
        if btype in ("bookmark", "link_preview", "embed", "file", "video", "pdf"):
            url = block_data.get("url")
            if not url and isinstance(block_data.get("external"), dict):
                url = block_data["external"].get("url")
            if url:
                caption_rich = block_data.get("caption") or []
                caption = "".join(x.get("plain_text", "") for x in caption_rich)
                urls.append({"url": url, "label": (caption or btype).strip(), "context": caption or btype})

        # 3. Child_page links (a memo nested as a subpage under the deal page)
        if btype == "child_page":
            child_title = block_data.get("title", "")
            child_page_id = b.get("id", "")
            if child_page_id:
                notion_url = f"https://www.notion.so/{child_page_id.replace('-', '')}"
                urls.append({
                    "url": notion_url,
                    "label": (child_title or "Notion subpage").strip(),
                    "context": f"Child page: {child_title}",
                })

        # 4. Page mentions inside rich_text can link to other Notion pages
        for span in rich:
            if span.get("type") == "mention":
                mention = span.get("mention", {}) or {}
                if mention.get("type") == "page":
                    mpid = (mention.get("page") or {}).get("id", "")
                    if mpid:
                        urls.append({
                            "url": f"https://www.notion.so/{mpid.replace('-', '')}",
                            "label": span.get("plain_text", "") or "Notion mention",
                            "context": full_text[:240],
                        })

    # Dedupe by URL (keep first label+context)
    seen = set()
    out = []
    for u in urls:
        url = (u.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(u)
    return out
