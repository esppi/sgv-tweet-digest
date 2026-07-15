# Scoring rubric — 0-18, metrics-first

This is the heuristic pre-filter that ranks every gathered candidate tweet before
Opus picks the final top 3. `scripts/digest.py` computes it deterministically in
`heuristic_score()`; this file is the human-readable spec so you can audit or tune it.

The pipeline keeps the top `candidate_count` (config; the shipped example sets 30,
code default 50) by this score and hands only those to Opus. `score_threshold`
(config; example sets 8, 0 disables) drops candidates below that score first —
**softly**: if the gate would leave fewer than `top_n` tweets, it is skipped for
that run rather than starving the digest.

Displayed as `/20` for continuity with older digests, but **18 is the real max**
(the deprecated "mentions" provenance bucket was removed, dropping 2 points off the
old ceiling).

---

## Candidate pool

Source: `list_tweets[]` from owned-reads `latest.json` (tweets from the X Lists you
follow + your List memberships). De-duplicate by `tweet_id`, but track **`list_count`**
per tweet = the number of distinct `from_list_id` values it appeared in. That
cross-list count is the consensus signal and feeds Provenance below.

**Drop before scoring:**
- Own handles (`personal_username`, `sgv_username` from config).
- Spam: `author_followers < 100` AND `like_count < 10` — **unless** the author is in
  `following_snapshot` (tier-0).
- Content-free replies: `type == "reply"` AND `len(text) < 20` AND no URL.

**Always keep:** any tweet whose author is in `following_snapshot` (tier-0), regardless
of engagement.

---

## The five buckets

### 1. Engagement metrics (max 7)
```
engagement_rate = (likes + retweets + replies + quotes) / impression_count
  > 3%    +3
  > 1%    +2
  > 0.3%  +1
velocity = (likes + retweets + replies + quotes) / max(age_hours, 0.5)
  (fresh tweets only, age < 48h — catches risers before they peak)
  > 150/h  +2
  > 50/h   +1
like_count   > 1000  +2   | > 300  +1
retweet_count > 100  +1
reply_count   > 50   +1     (real discussion, not a broadcast)
cap at 7
```
If `impression_count` is missing, the rate buckets contribute 0; the absolute-count
rules still apply.

### 2. List provenance (max 5)
```
list_count >= 3  +3       (cross-list consensus — multiple curators surfaced it)
list_count == 2  +2
list_count == 1  +1
author in following_snapshot (tier-0)  +2
cap at 5
```

### 3. Thesis match (max 3) — first matching bucket wins, sets `thesis_label`
```
consumer crypto / onchain UX / app layer / consumer app   +3   consumer-crypto
fundraise / seed / series A|B / raised / closed $          +3   fundraise
defi / stablecoin / L2 / rollup                            +2   defi-infra
ai agent / fintech / creator economy / prediction market  +2   crypto-adjacent
(none of the above)                                        +1   general
```

### 4. Novelty (max 2)
```
len(text) > 200 chars (thread / long-form)        +1
entities present (urls OR mentions OR hashtags)   +1
```

### 5. Actionability (max 1)
```
reply_count < 500 AND like_count > 50   +1   (engageable, not buried)
```

---

## Top-3 selection (Opus)

After the heuristic ranking, Opus picks the final **3** top tweets (shared across both
voices) and assigns each ONE action. Variety constraints on the 3:
- at least 1 fundraise / deal signal,
- at least 1 product-launch signal (consumer-crypto, defi-infra, or crypto-adjacent),
- all 3 from **unique authors**.

### Action taxonomy (exactly one per pick)
| Action | When |
|---|---|
| **Reply** | Thesis-relevant, <500 replies, visible thread. Default for conversations. |
| **QT** | Viral or insightful; the fund has a thesis overlay to add. |
| **DM founder** | Launches, stealth reveals, deal signals — private outreach. |
| **Bookmark** | Long-form / research worth a later reference; no public action. |
| **Pass** | Rare — only if buried under 1000+ replies. |

Each pick's `why` is one factual sentence weaving metrics + provenance + thesis
(e.g. "5.2% engagement on a 412-like thread about consumer payments, surfaced by 4 of
your Lists").

---

## News context ranking (Shoal channel)

If `SHOAL_CHANNEL` is set and reachable, the digest pulls the last ~30 messages, keeps
those from the last 20h that contain a URL, and Opus ranks the best few **per voice**:
- **Fund (sgv) fit:** institutional moves, infra unlocks, market-structure shifts,
  fundraises — what partners and LPs want to discuss.
- **Personal (mist) fit:** builder / crypto-native angles, contrarian takes, AI x crypto,
  technical primitives, founder moves, edge thinking before consensus.

Each news pick's `why` must cover BOTH a fit reason AND a virality reason (crisp headline,
named entities, concrete numbers, timeliness, quote-tweet potential, contrarian framing).
The two voices may overlap but should prefer distinct picks when both are strong.
