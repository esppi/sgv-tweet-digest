# Voice guide — drafting tweet ideas in BOTH voices

The digest drafts tweet ideas in two distinct voices every run. The full, hand-tuned
profiles (summary + do / don't + representative tweets) live in `voice_profiles.json`
and are the source of truth — load that file and follow it. This guide is the operating
manual for *how the two voices differ* and the rules that apply to every draft.

The two voices are keyed `sgv` and `mist` in `voice_profiles.json`:
- **`sgv`** → the fund account (`sgv_username` in your config). Analytical fund voice.
- **`mist`** → the personal account (`personal_username` in your config). Builder voice.

---

## Hard rules (every draft, both voices)

- **≤ 280 characters** per draft. QT commentary ≤ 260 (the source URL is appended after).
- **Never use the em dash character** (the long dash from two hyphens). Use periods,
  commas, parentheses, or "and". This applies to every field: drafts, setups, `why`
  rationales, headlines.
- **Anonymize internal info.** Never name portfolio companies, founders, or deal terms.
  When the insights pass surfaces a real entity, refer to it by category, not by name.
- **The two voices must address DIFFERENT angles.** Do not write a mist version of an
  sgv draft. Different sources, different framings, distinct content.
- **No "really excited / thrilled to announce / pleased to share."** A summary is not a
  tweet — take a position.

---

## Fund voice (`sgv`)

Analytical fund voice. Leads with a thesis claim, then proves it with a concrete number,
a named protocol, or a market-structure observation. Surfaces what other VCs are missing
rather than amplifying consensus.

- Open with a short declarative thesis claim, prove it in the next sentence.
- Back it with concrete numbers; reference category-defining protocols by name.
- Fund-perspective framing is fine ("what we're seeing...").
- End on a forward-looking question or a structural observation, not a punchline.
- **No emojis. Zero.** Don't parrot headlines — add the thesis lens or skip it.

Good: `Tokenized stocks just crossed $5B in daily volume. The infrastructure is ready. Now it's a distribution problem.`

---

## Personal voice (`mist`)

Direct, opinionated, builder-perspective. More conversational and less polished than the
fund account. First-person quick reactions and sharp takes, comfortable with contrarianism.
Sounds like a friend texting you what they actually think.

- **Capitalization:** capitalize the first letter of the tweet AND the first letter after
  every period (basic grammar). Everything else stays lowercase as part of the voice — do
  not capitalize a mid-sentence "i", do not title-case headlines, do not capitalize after
  commas. (`digest.py` enforces this with `_capitalize_mist()`; match it when drafting.)
- First-person framing ("I think", "my read", "been watching"). Never the fund "we".
- Short and conversational; reply or QT framing works best.
- Engage handles directly when relevant; take a position before consensus settles.
- Reference what builders are saying, not what funds are saying. Emojis sparingly if at all.

Good: `Agents are not the future of consumer crypto. They're the present of it. You're either building for them or you're losing distribution.`

---

## What gets drafted per voice (3 sections)

Each voice receives up to three sections of ideas. Section 3 is **personal-voice only** by
design (the fund channel omits it).

1. **Tweet ideas (Opus)** — 2 standalone tweets + 1 quote-tweet, each with an `inspo_label`
   and (for QTs and source-linked tweets) an `inspo_url`. Vary the sources across:
   Shoal news overlay, cross-list consensus pick (highest `list_count`), tier-0 follow-graph
   highlight, and direct builder/founder engagement (especially the personal voice).
   For the quote-tweet, the source URL is appended to the draft body so X auto-embeds the
   quote when posted.

2. **Insight ideas (Sonnet, optional)** — up to 3 anonymized drafts distilled from the last
   7 days of fund-internal data (Notion deal funnel + weekly Gmail memos + Fireflies call
   transcripts), produced by `scripts/insights.py`. These never link out. The whole section
   is skipped when no optional source is configured.

3. **Good-content ideas (Sonnet, personal voice only)** — drafts built from URLs you flagged
   via the `gc: <url>` Telegram poller, drawn from `feedback.db`.

### Viral-hook tagging
Every Opus-drafted idea (tweets and the QT) carries a `viral_hooks` array of 1-3 tags,
most-dominant first, from this vocabulary. The first tag is the `primary_hook` the feedback
loop tracks:
- `concrete_number` — a specific $, %, Nx, count, or hard number.
- `contrarian` — a position against consensus ("hot take", "actually", "myth").
- `pattern` — identifies a trend ("we're seeing", "every X", "the new pattern").
- `question` — opens with or contains a real reader-facing question.
- `no_hook` — none of the above (use only if nothing else fits).

---

## Feedback signal

If a recent feedback profile exists (built by `scripts/feedback_profile.py` once at least
3 drafted ideas have been matched to posted tweets), the digest injects a short
"recent performance signal" block of do-more / do-less rules. Bias new drafts toward the
do-more archetypes and away from the do-less ones, but never at the cost of the hard rules
above.
