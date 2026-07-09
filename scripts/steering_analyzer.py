#!/usr/bin/env python3
"""SGV steering analyzer — measures whether Opus's daily output actually shifts
toward the feedback profile's high-lift archetypes vs the prior day's baseline.

Runs daily (after the digest finishes generating ideas).

Inputs (from feedback.db):
  - Latest feedback_profile (FP_today) and the one before it (FP_prior)
  - tweet_ideas tagged with feedback_profile_id = FP_today.id ("today's batch")
  - tweet_ideas tagged with feedback_profile_id = FP_prior.id ("yesterday's batch", baseline)

Output (one row in steering_metrics):
  - feedback_profile_id, prior_profile_id
  - ideas_under_profile (count of today's batch)
  - archetype_dist_json (today's batch grouped by voice::source::primary_hook)
  - do_more_compliance: mean (today_share - prior_share) / max(prior_share, eps)
    across archetypes that today's profile classifies as 'do_more' (lift_x > 1.0)
  - do_less_compliance: same but inverted — mean (prior_share - today_share) / ...
    across archetypes today's profile classifies as 'do_less' (lift_x < 1.0)

Compliance > 0 means Opus shifted in the recommended direction.
Compliance ~ 0 means no shift (Opus ignored).
Compliance < 0 means Opus shifted the WRONG way (bug or sampling noise).

Phase 2 (later): we'll add outcome columns (matched_on_rec_count, engagement_lift)
once enough days of tagged ideas have been actually-tweeted and metric-snapshotted.

No LLM; stdlib + feedback_db only.
"""
import os
import sys
import json
import datetime
from collections import Counter

# Sibling import (feedback_db.py lives next to this file)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import feedback_db as db

# Lift threshold for classifying archetypes
DO_MORE_THRESHOLD = 1.0   # lift_x > 1.0 -> do_more
DO_LESS_THRESHOLD = 1.0   # lift_x < 1.0 -> do_less
MIN_N_FOR_RECOMMENDATION = 2  # archetype needs n_matched >= this to be a valid recommendation

EPS = 0.01  # avoid divide-by-zero on prior_share


def _archetype_key(voice, source_module, primary_hook):
    return f"{voice}::{source_module}::{primary_hook}"


def _classify_archetypes(profile):
    """From a feedback_profile, return (do_more_keys, do_less_keys) sets.

    profile["archetype_stats"] is a list of dicts each with voice, source_module,
    primary_hook, lift_x, n_matched.
    """
    stats = profile.get("archetype_stats") or []
    do_more = set()
    do_less = set()
    for s in stats:
        if (s.get("n_matched") or 0) < MIN_N_FOR_RECOMMENDATION:
            continue
        key = _archetype_key(s.get("voice"), s.get("source_module"), s.get("primary_hook"))
        lift = s.get("lift_x") or 1.0
        if lift > DO_MORE_THRESHOLD:
            do_more.add(key)
        elif lift < DO_LESS_THRESHOLD:
            do_less.add(key)
    return do_more, do_less


def _idea_archetype_dist(c, feedback_profile_id):
    """Return (counter, total) — Counter of archetype_key -> count for all
    tweet_ideas with this feedback_profile_id."""
    rows = c.execute("""
        SELECT voice, source_module, primary_hook
        FROM tweet_ideas
        WHERE feedback_profile_id = ?
    """, (feedback_profile_id,)).fetchall()
    counter = Counter()
    for r in rows:
        key = _archetype_key(r["voice"], r["source_module"], r["primary_hook"] or "no_hook")
        counter[key] += 1
    return counter, sum(counter.values())


def _share(counter, total, key):
    if total == 0:
        return 0.0
    return counter.get(key, 0) / total


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def compute_steering(today_profile, prior_profile, c):
    """Compute compliance metrics. Returns dict suitable for INSERT into steering_metrics."""
    today_id = today_profile["id"]
    prior_id = prior_profile["id"] if prior_profile else None

    today_dist, today_total = _idea_archetype_dist(c, today_id)
    prior_dist, prior_total = (
        _idea_archetype_dist(c, prior_id) if prior_id else (Counter(), 0)
    )

    do_more_keys, do_less_keys = _classify_archetypes(today_profile)

    def directional_compliance(keys, invert):
        """For each archetype key: signed delta (today_share - prior_share) / prior_share.
        If invert=True (do_less), flip sign so "drop in share" -> positive compliance."""
        deltas = []
        for k in keys:
            t = _share(today_dist, today_total, k)
            p = _share(prior_dist, prior_total, k)
            denom = max(p, EPS)
            delta = (t - p) / denom
            if invert:
                delta = -delta
            deltas.append(delta)
        return _mean(deltas)

    do_more_compliance = directional_compliance(do_more_keys, invert=False)
    do_less_compliance = directional_compliance(do_less_keys, invert=True)

    notes_parts = []
    if today_total == 0:
        notes_parts.append("no ideas tagged with this profile yet (Phase 0 just deployed?)")
    if not do_more_keys and not do_less_keys:
        notes_parts.append(f"profile had no archetypes with n_matched >= {MIN_N_FOR_RECOMMENDATION}")
    if prior_total == 0 and prior_id:
        notes_parts.append(f"prior profile #{prior_id} has no tagged ideas (insufficient baseline)")

    return {
        "feedback_profile_id": today_id,
        "prior_profile_id": prior_id,
        "ideas_under_profile": today_total,
        "archetype_dist_json": json.dumps(dict(today_dist)),
        "do_more_compliance": do_more_compliance,
        "do_less_compliance": do_less_compliance,
        "notes": "; ".join(notes_parts) or None,
    }


def _engagement_score(metrics):
    """Same formula as feedback_profile.py:_engagement_score. Kept consistent so
    on/off recipes use the same scale as the do_more thresholds."""
    if not metrics:
        return 0
    return (
        (metrics.get("likes") or 0)
        + 5 * (metrics.get("replies") or 0)
        + 3 * (metrics.get("retweets") or 0)
        + 0.1 * (metrics.get("impressions") or 0)
    )


def _latest_metrics_for_tweet(c, tweet_id):
    """Return the most recent snapshot dict for `tweet_id`, or None."""
    r = c.execute("""
        SELECT * FROM tweet_metrics_snapshots
        WHERE tweet_id = ?
        ORDER BY snapshot_at DESC
        LIMIT 1
    """, (tweet_id,)).fetchone()
    return dict(r) if r else None


def update_outcomes(c, log):
    """Phase 2: backfill outcome columns on prior steering_metrics rows that don't
    yet have engagement data. Matches accumulate over weeks; this fills lazily.

    For each row whose engagement_lift is NULL:
      - Load its feedback_profile's do_more archetypes
      - Find all tweet_ideas with that feedback_profile_id that have been matched
        to real tweets (via tweet_idea_matches)
      - Compute engagement_score on each matched tweet (latest snapshot)
      - Bucket into on_rec / off_rec based on the idea's archetype
      - UPDATE the steering_metrics row with counts + averages + lift
    """
    rows = c.execute("""
        SELECT id, feedback_profile_id
        FROM steering_metrics
        WHERE engagement_lift IS NULL
          AND computed_at > datetime('now', '-60 days')
        ORDER BY id DESC
    """).fetchall()

    updated = 0
    for r in rows:
        sm_id = r["id"]
        fp_id = r["feedback_profile_id"]

        fp = c.execute(
            "SELECT archetype_stats_json FROM feedback_profiles WHERE id = ?",
            (fp_id,),
        ).fetchone()
        if not fp:
            continue
        try:
            stats = json.loads(fp["archetype_stats_json"] or "[]")
        except Exception:
            continue

        do_more_keys = {
            _archetype_key(s.get("voice"), s.get("source_module"), s.get("primary_hook"))
            for s in stats
            if (s.get("n_matched") or 0) >= MIN_N_FOR_RECOMMENDATION
            and (s.get("lift_x") or 1.0) > DO_MORE_THRESHOLD
        }

        matches = c.execute("""
            SELECT i.voice, i.source_module, i.primary_hook, t.tweet_id
            FROM tweet_ideas i
            JOIN tweet_idea_matches m ON m.idea_id = i.id
            JOIN our_tweets t ON t.tweet_id = m.our_tweet_id
            WHERE i.feedback_profile_id = ?
        """, (fp_id,)).fetchall()

        on_rec, off_rec = [], []
        for m in matches:
            metrics = _latest_metrics_for_tweet(c, m["tweet_id"])
            if not metrics:
                continue
            score = _engagement_score(metrics)
            key = _archetype_key(m["voice"], m["source_module"], m["primary_hook"] or "no_hook")
            (on_rec if key in do_more_keys else off_rec).append(score)

        if not on_rec and not off_rec:
            continue  # no matches yet for this profile

        on_avg = sum(on_rec) / len(on_rec) if on_rec else None
        off_avg = sum(off_rec) / len(off_rec) if off_rec else None
        # lift only meaningful if both sides have samples
        lift = (on_avg / off_avg) if (on_avg is not None and off_avg) else None

        c.execute("""
            UPDATE steering_metrics SET
              matched_on_rec_count = ?,
              matched_off_rec_count = ?,
              on_rec_avg_engagement = ?,
              off_rec_avg_engagement = ?,
              engagement_lift = ?
            WHERE id = ?
        """, (len(on_rec), len(off_rec), on_avg, off_avg, lift, sm_id))
        updated += 1
        log(f"  outcome filled for steering_metrics#{sm_id} (profile #{fp_id}): "
            f"on_rec={len(on_rec)} off_rec={len(off_rec)} lift={lift!r}")

    if updated:
        log(f"  total outcome rows backfilled: {updated}")
    else:
        log("  no outcome backfill candidates (either no rows pending, or no matches accumulated yet)")


def update_reliability(c, log):
    """Phase 4: per-archetype reliability score.

    For each archetype that has EVER been recommended (lift_x > 1.0 in any
    feedback_profile's archetype_stats), measure whether ideas of that
    archetype, generated under profiles that recommended it, actually
    engaged above baseline when matched to real tweets.

    Writes one row per archetype into recommendation_reliability with:
      n_recommended            — # of profiles where this archetype was do_more
      n_matched_on_rec         — # of matched (idea->tweet) pairs counted
      on_rec_total_engagement  — sum of engagement_scores for those pairs
      off_rec_total_engagement — sum for matches NOT under a recommending profile
      reliability_score        — on_rec_avg / overall_baseline  (1.0 = neutral)
      suppressed_until         — 14 days out if reliability < 0.85 AND n>=3

    Run after update_outcomes so the upstream tables are consistent.
    """
    # 1) Collect all profiles + their do_more archetype sets
    profiles = c.execute(
        "SELECT id, archetype_stats_json FROM feedback_profiles ORDER BY id"
    ).fetchall()
    archetype_to_recommending_profiles = {}   # key -> [profile_id, ...]
    all_keys = set()
    for p in profiles:
        try:
            stats = json.loads(p["archetype_stats_json"] or "[]")
        except Exception:
            continue
        for s in stats:
            key = _archetype_key(s.get("voice"), s.get("source_module"), s.get("primary_hook"))
            all_keys.add(key)
            if (s.get("n_matched") or 0) >= MIN_N_FOR_RECOMMENDATION and (s.get("lift_x") or 1.0) > DO_MORE_THRESHOLD:
                archetype_to_recommending_profiles.setdefault(key, []).append(p["id"])

    # 2) Baseline engagement: avg engagement_score across ALL recently-matched tweets
    matched_rows = c.execute("""
        SELECT t.tweet_id
        FROM tweet_idea_matches m
        JOIN our_tweets t ON t.tweet_id = m.our_tweet_id
    """).fetchall()
    all_scores = []
    for mr in matched_rows:
        metrics = _latest_metrics_for_tweet(c, mr["tweet_id"])
        if metrics:
            all_scores.append(_engagement_score(metrics))
    overall_baseline = sum(all_scores) / len(all_scores) if all_scores else None
    if overall_baseline is None or overall_baseline == 0:
        log("  reliability: no matched tweets with metrics yet — skipping")
        return

    log(f"  reliability baseline (avg engagement across {len(all_scores)} matched tweets): {overall_baseline:.2f}")

    updated_count = 0

    for key in all_keys:
        voice, source_module, primary_hook = key.split("::")
        recommending = archetype_to_recommending_profiles.get(key, [])
        n_recommended = len(recommending)

        # Engagement scores split by whether the idea's profile recommended this archetype
        on_rec_scores = []
        off_rec_scores = []

        ideas = c.execute("""
            SELECT i.id AS idea_id, i.feedback_profile_id, m.our_tweet_id
            FROM tweet_ideas i
            JOIN tweet_idea_matches m ON m.idea_id = i.id
            WHERE i.voice = ? AND i.source_module = ?
              AND (i.primary_hook = ? OR (? = 'no_hook' AND i.primary_hook IS NULL))
        """, (voice, source_module, primary_hook, primary_hook)).fetchall()

        for row in ideas:
            metrics = _latest_metrics_for_tweet(c, row["our_tweet_id"])
            if not metrics:
                continue
            score = _engagement_score(metrics)
            if row["feedback_profile_id"] in recommending:
                on_rec_scores.append(score)
            else:
                off_rec_scores.append(score)

        on_total = sum(on_rec_scores)
        off_total = sum(off_rec_scores)
        on_avg = (on_total / len(on_rec_scores)) if on_rec_scores else None
        reliability = (on_avg / overall_baseline) if on_avg is not None else None

        # Suppress only if we have enough samples AND archetype underperforms baseline
        suppressed_until = None
        if reliability is not None and reliability < 0.85 and len(on_rec_scores) >= 3:
            suppressed_until = (datetime.datetime.now(datetime.timezone.utc)
                                + datetime.timedelta(days=14)).isoformat()

        # UPSERT
        existing = c.execute(
            "SELECT id FROM recommendation_reliability WHERE archetype_key = ?", (key,)
        ).fetchone()
        if existing:
            c.execute("""
                UPDATE recommendation_reliability SET
                  n_recommended = ?, n_matched_on_rec = ?,
                  on_rec_total_engagement = ?, off_rec_total_engagement = ?,
                  reliability_score = ?, suppressed_until = ?, last_updated = ?
                WHERE archetype_key = ?
            """, (n_recommended, len(on_rec_scores), on_total, off_total,
                  reliability, suppressed_until, db.now_iso(), key))
        else:
            c.execute("""
                INSERT INTO recommendation_reliability
                  (archetype_key, n_recommended, n_matched_on_rec,
                   on_rec_total_engagement, off_rec_total_engagement,
                   reliability_score, suppressed_until, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (key, n_recommended, len(on_rec_scores), on_total, off_total,
                  reliability, suppressed_until, db.now_iso()))
        updated_count += 1
        if reliability is not None and suppressed_until:
            log(f"  SUPPRESSED {key}: reliability {reliability:.2f} over {len(on_rec_scores)} matched ideas")

    log(f"  reliability: updated {updated_count} archetypes")


def run(verbose=True):
    log = print if verbose else (lambda *a, **k: None)
    t0 = datetime.datetime.now()
    log(f"== steering-analyzer {t0.isoformat()} ==")

    db.init_schema()
    c = db._conn()

    profiles = c.execute("""
        SELECT * FROM feedback_profiles
        ORDER BY id DESC LIMIT 3
    """).fetchall()
    if not profiles:
        log("  no feedback_profiles yet — nothing to analyze")
        return None

    # profiles[0] was created by feedback_profile.py MOMENTS ago (same systemd
    # unit) — no ideas can be tagged with it until tomorrow's digest, so measuring
    # it yields ideas_under_profile=0 and ±1.0 compliance noise. Measure the
    # PREVIOUS profile — the one that actually governed today's drafted ideas.
    if len(profiles) < 2:
        log("  only the just-created profile exists — nothing measurable yet (come back tomorrow)")
        return None
    today = dict(profiles[1])
    prior = dict(profiles[2]) if len(profiles) >= 3 else None
    for p in (today, prior):
        if not p:
            continue
        for k in ("archetype_stats_json", "recommendations_json"):
            if p.get(k):
                try:
                    p[k.replace("_json", "")] = json.loads(p[k])
                except Exception:
                    p[k.replace("_json", "")] = None

    log(f"  measured profile: id={today['id']} generated={today['generated_at'][:19]} (the one today's ideas ran under)")
    if prior:
        log(f"  prior profile:    id={prior['id']} generated={prior['generated_at'][:19]}")
    else:
        log("  prior profile:    (none — this is the first measurable profile)")

    metrics = compute_steering(today, prior, c)

    log(f"  ideas under today's profile: {metrics['ideas_under_profile']}")
    log(f"  archetypes seen: {len(json.loads(metrics['archetype_dist_json']))}")
    log(f"  do_more_compliance: {metrics['do_more_compliance']}")
    log(f"  do_less_compliance: {metrics['do_less_compliance']}")
    if metrics.get("notes"):
        log(f"  notes: {metrics['notes']}")

    # Skip insert if we have zero today_ideas AND zero prior baseline — pure noise row
    skip_insert = (
        metrics["ideas_under_profile"] == 0
        and (metrics["prior_profile_id"] is None
             or _idea_archetype_dist(c, metrics["prior_profile_id"])[1] == 0)
    )
    if skip_insert:
        log("  not writing today's row (no ideas tagged yet; come back tomorrow)")
    else:
        c.execute("""
            INSERT INTO steering_metrics
              (computed_at, feedback_profile_id, prior_profile_id, ideas_under_profile,
               archetype_dist_json, do_more_compliance, do_less_compliance, notes)
            VALUES (:computed_at, :feedback_profile_id, :prior_profile_id, :ideas_under_profile,
                    :archetype_dist_json, :do_more_compliance, :do_less_compliance, :notes)
        """, {
            "computed_at": db.now_iso(),
            **{k: metrics[k] for k in (
                "feedback_profile_id", "prior_profile_id", "ideas_under_profile",
                "archetype_dist_json", "do_more_compliance", "do_less_compliance", "notes",
            )},
        })

    # Phase 2: backfill outcome data on prior rows — runs regardless of today's
    # insert decision because it's about historical rows, not the current one.
    log("  -- backfilling outcome columns on prior rows --")
    update_outcomes(c, log)

    # Phase 4: roll up per-archetype reliability so feedback_profile.py can auto-soften
    log("  -- updating per-archetype reliability --")
    update_reliability(c, log)

    duration = (datetime.datetime.now() - t0).total_seconds()
    log(f"== DONE in {duration:.1f}s ==")
    return metrics


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()
    result = run(verbose=not args.quiet)
    if result:
        print(json.dumps({k: v for k, v in result.items() if k != "archetype_dist_json"}, indent=2, default=str))
