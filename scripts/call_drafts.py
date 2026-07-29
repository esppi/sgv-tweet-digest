#!/usr/bin/env python3
"""CALL DRAFTS — pull recent Fireflies call transcripts and draft up to 3
anonymized personal-voice tweets per call (insights, reframed quotes, takes
that could hit). Personal-channel only, like good-content.

Two-stage per call (cost control + safety):
  Stage 1 EXTRACT: Sonnet reads the transcript and returns 6-10 moments,
          ANONYMIZED AT EXTRACTION — stage 2 never sees a name, a metric
          worth bucketing, or a verbatim quote.
  Stage 2 DRAFT:   Sonnet turns the moments + voice profile + burned-topics
          memory into <=DRAFTS_PER_CALL tweets in the personal voice.

Cost: ~$0.05-0.08/call (Sonnet), capped at MAX_CALLS_PER_RUN calls/day.

OPTIONAL source — activates only when FIREFLIES_API_KEY is set. Safety gates:
refuses to run with only the example denylist (same rule as insights.py), and
every draft passes the denylist + postprocess scrub as a final net.

Processed-call tracking: transcript ids are remembered via feedback_db
poll-state so a call is never drafted twice across runs.
"""
import os, sys, json, re, datetime, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import feedback_db as db
import insights as _ins  # reuse: denylist machinery, voice blocks, postprocess

FIREFLIES_API_KEY = os.environ.get("FIREFLIES_API_KEY")
FIREFLIES_GQL = "https://api.fireflies.ai/graphql"

LOOKBACK_HOURS = 26          # digest at 13:00 UTC covers yesterday-afternoon + today
MIN_CALL_MINUTES = 15        # skip standups/no-shows
MAX_CALLS_PER_RUN = 3        # hard cost cap
DRAFTS_PER_CALL = 3
TRANSCRIPT_CAP_CHARS = 48_000  # ~1h call; beyond this the tail is truncated
SEEN_STATE_KEY = "call_drafts_seen_ids"
DEFAULT_SONNET_MODEL = "claude-sonnet-4-5-20250929"


def _sonnet_model():
    models = _ins._MODELS if isinstance(_ins._MODELS, dict) else {}
    return models.get("sonnet") or DEFAULT_SONNET_MODEL


# ─────────────────────────────────────────
# FIREFLIES FETCH
# ─────────────────────────────────────────
def _gql(query, variables, timeout=45):
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        FIREFLIES_GQL, data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + FIREFLIES_API_KEY})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read())
    if out.get("errors"):
        raise RuntimeError(f"fireflies gql: {str(out['errors'])[:200]}")
    return out.get("data") or {}


def fetch_recent_calls():
    """List transcripts in the lookback window, newest first, >= MIN_CALL_MINUTES."""
    now = datetime.datetime.now(datetime.timezone.utc)
    frm = (now - datetime.timedelta(hours=LOOKBACK_HOURS)).isoformat()
    data = _gql(
        """query Transcripts($fromDate: DateTime, $toDate: DateTime, $limit: Int) {
             transcripts(fromDate: $fromDate, toDate: $toDate, limit: $limit) {
               id title dateString duration } }""",
        {"fromDate": frm, "toDate": now.isoformat(), "limit": 20})
    calls = [t for t in (data.get("transcripts") or [])
             if (t.get("duration") or 0) >= MIN_CALL_MINUTES]
    calls.sort(key=lambda t: t.get("dateString") or "", reverse=True)
    return calls


def fetch_transcript_text(transcript_id):
    """Full transcript as 'Speaker: text' lines, capped at TRANSCRIPT_CAP_CHARS."""
    data = _gql(
        """query Transcript($transcriptId: String!) {
             transcript(id: $transcriptId) {
               id duration sentences { speaker_name text } } }""",
        {"transcriptId": transcript_id})
    sents = ((data.get("transcript") or {}).get("sentences")) or []
    lines = [f"{s.get('speaker_name') or '?'}: {s.get('text') or ''}" for s in sents]
    text = "\n".join(lines)
    if len(text) > TRANSCRIPT_CAP_CHARS:
        text = text[:TRANSCRIPT_CAP_CHARS] + "\n[...transcript truncated for length...]"
    return text


# ─────────────────────────────────────────
# SEEN-CALL TRACKING (never draft the same call twice)
# ─────────────────────────────────────────
def _seen_ids():
    try:
        raw = db.get_poll_state(SEEN_STATE_KEY)
        return set(json.loads(raw)) if raw else set()
    except Exception:
        return set()


def _mark_seen(ids):
    try:
        seen = list((_seen_ids() | set(ids)))[-50:]
        db.set_poll_state(SEEN_STATE_KEY, json.dumps(seen))
    except Exception:
        pass


# ─────────────────────────────────────────
# PROMPTS
# ─────────────────────────────────────────
EXTRACT_SYSTEM = """You extract tweet-worthy moments from a VC's PRIVATE call
transcript. Your output feeds a public-tweet drafter, so anonymization happens
HERE, at extraction — nothing identifying may survive into your output.

Extract the 6-10 strongest moments: crisp claims, surprising mechanics,
contrarian takes, market-structure observations, founder-psychology patterns.

ANONYMIZATION — ABSOLUTE:
- NEVER include any company, product, fund, platform, or person name from the
  call. The only allowed brand is SGV. Public protocols/platforms in GENERIC
  use (USDC, Polymarket, Solana...) are fine.
- NEVER preserve verbatim phrasing longer than a few words — restate every
  moment in neutral reporting language.
- Bucket identifying numbers ("120,000 accounts" -> "six figures of accounts";
  "$4.5M raise" -> "a mid-seven-figure raise"). Ecosystem-level public stats
  may stay precise.
- NEVER include: deal terms, valuations, pipeline status, anyone's fundraising
  strategy, compensation, roadmaps framed as a specific company's unlaunched
  plans, or opinions attributable to a named person.
- If a moment cannot be anonymized without losing its point, drop it.

Output JSON only:
{"moments": [{"moment": "1-2 sentence anonymized restatement",
              "why_interesting": "a few words",
              "hook": "concrete_number|contrarian|pattern|question"}]}"""


def _draft_system():
    mist_block = _ins._voice_block("mist", "PERSONAL")
    return f"""You draft tweets for a crypto-VC's PERSONAL X account from
anonymized call moments. The reader must never be able to identify the call,
the company, or the counterparty.

{mist_block}

RULES:
- <=280 chars each. Capitalize the first letter of the tweet AND after every
  period; everything else stays lowercase-casual. Standalone "i" stays lowercase.
- NEVER use the em dash character. No emojis. No hedging, no corporate speak.
- First-person where natural ("a founder told me this week", "my read", or
  just state the insight as his own take).
- Every draft needs a real hook: concrete (bucketed) number, contrarian
  position, named pattern, or sharp question.
- The DO-NOT-REPEAT list below is BURNED territory: different angles only.

Output JSON only:
{{"drafts": [{{"draft": "...", "angle": "insight|reframed-quote|hot-take",
              "source_moment": "short anonymized pointer",
              "viral_hooks": ["contrarian"]}}]}}
(UP TO {DRAFTS_PER_CALL} drafts; fewer strong ones beat filler.)"""


def _burned_lines(days=5, cap=25):
    try:
        rows = db.get_recent_ideas(days=days)
    except Exception:
        return []
    lines, seen = [], set()
    for r in rows:
        line = f"topic={r.get('topic') or '?'} | {(r.get('draft') or '')[:100]}"
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return lines[:cap]


# ─────────────────────────────────────────
# SONNET CALLS
# ─────────────────────────────────────────
def _sonnet(stage, system, user_msg, max_tokens):
    """One LLM call for `stage` ('call_extract' or 'call_draft' — provider/model
    per config 'backends')."""
    import llm_backend
    raw, usage, cost = llm_backend.chat(
        stage=stage, system_text=system, user_text=user_msg,
        max_tokens=max_tokens, default_model=_sonnet_model())
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?|\n?```$", "", raw)
    # Tolerant parse: models sometimes append prose after the JSON object
    # ("Extra data" on strict loads) — take the FIRST valid object.
    start = raw.find("{")
    if start < 0:
        raise RuntimeError("no JSON object in response")
    obj, _ = json.JSONDecoder().raw_decode(raw[start:])
    return obj, cost


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def run(dry_run=False, verbose=True):
    """Returns {"call_drafts": [per-call dicts], "cost_usd": float, "errors": []}.
    Optional source: silently empty when FIREFLIES_API_KEY is unset."""
    log = print if verbose else (lambda *a, **k: None)
    empty = {"call_drafts": [], "cost_usd": 0, "errors": []}

    if not FIREFLIES_API_KEY:
        log("  [calls] skipped: FIREFLIES_API_KEY not set")
        return empty
    # Same safety gate as insights: private-call material never runs on the
    # shipped fake-name example denylist.
    if _ins.DENYLIST_PATH.endswith("anonymize_denylist.example.txt"):
        log("  [calls] REFUSING: only the example denylist present (see insights.py gate)")
        return {**empty, "errors": ["call_drafts: example denylist only"]}

    try:
        calls = fetch_recent_calls()
    except Exception as e:
        log(f"  [calls] fireflies list failed: {e}")
        return {**empty, "errors": [f"fireflies list: {e}"]}

    seen = _seen_ids()
    todo = [c for c in calls if c.get("id") not in seen][:MAX_CALLS_PER_RUN]
    log(f"  [calls] {len(calls)} recent, {len(todo)} new (cap {MAX_CALLS_PER_RUN})")
    if not todo:
        return empty

    burned = _burned_lines()
    draft_sys = _draft_system()
    results, total_cost, errors, processed = [], 0.0, [], []

    for n, call in enumerate(todo, 1):
        cid = call["id"]
        dur = call.get("duration") or 0
        try:
            text = fetch_transcript_text(cid)
            if len(text) < 500:
                log(f"  [calls] call {n}: transcript too thin, skipping")
                processed.append(cid)
                continue
            # Stage 1 — anonymized extraction (5000: dense hour-long calls plus
            # reasoning-model thinking burn — truncation aborts the whole call)
            ex, c1 = _sonnet("call_extract", EXTRACT_SYSTEM,
                             f"CALL TRANSCRIPT ({dur:.0f} min):\n{text}\n\nReturn the JSON.",
                             max_tokens=5000)
            total_cost += c1
            moments = [m for m in (ex.get("moments") or []) if isinstance(m, dict)][:10]
            if not moments:
                processed.append(cid)
                continue
            # Belt-and-suspenders: denylist pass over the extracted moments
            for m in moments:
                m["moment"] = _ins.denylist_only(m.get("moment") or "")
            # Stage 2 — drafts
            user2 = (f"ANONYMIZED MOMENTS from one call ({dur:.0f} min):\n"
                     f"{json.dumps(moments, indent=1)}\n\n")
            if burned:
                user2 += ("DO-NOT-REPEAT (recent drafts):\n"
                          + "\n".join("  - " + b for b in burned) + "\n\n")
            user2 += "Return the JSON."
            dr, c2 = _sonnet("call_draft", draft_sys, user2, max_tokens=3000)
            total_cost += c2
            drafts = []
            for d in (dr.get("drafts") or [])[:DRAFTS_PER_CALL]:
                if not isinstance(d, dict) or not (d.get("draft") or "").strip():
                    continue
                clean = _ins.postprocess_draft(_ins.denylist_only(d["draft"].strip()))
                drafts.append({
                    "draft": clean,
                    "angle": d.get("angle") or "insight",
                    "source_moment": _ins.denylist_only((d.get("source_moment") or "")[:120]),
                    "viral_hooks": d.get("viral_hooks") or [],
                })
            if drafts:
                # Call label is POSITIONAL ONLY — titles often contain company names.
                results.append({"call_label": f"call {n} ({dur:.0f} min)", "drafts": drafts})
            processed.append(cid)
            log(f"  [calls] call {n} ({dur:.0f}m): {len(moments)} moments -> {len(drafts)} drafts "
                f"(${c1 + c2:.4f})")
        except Exception as e:
            errors.append(f"call {n}: {str(e)[:120]}")
            log(f"  [calls] call {n} FAILED (non-fatal): {str(e)[:120]}")

    if processed and not dry_run:
        _mark_seen(processed)

    return {"call_drafts": results, "cost_usd": round(total_cost, 4), "errors": errors}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()
    print(json.dumps(run(dry_run=args.dry_run, verbose=not args.quiet), indent=2))
