# Hype scoring — algorithm v2.0

Code: `hypebot/metrics.py` (metrics), `hypebot/scoring.py` (categories, confidence, tiers),
`hypebot/pipeline.py::evaluate` (the single path used by /analyze, /refresh, ingest, rescore and recalculation).

## Determinism
* No randomness, no AI-generated numbers, no wall clock inside scoring. Every time-relative metric
  (account age, days since last post, posts/week, 7-day window) is measured against the data's own
  `collected_at`.
* Posts and mentions are sorted and de-duplicated by id (content hash if no id) before use, so input
  order never matters. Repeated authors are merged order-independently (max followers).
* The only history input is an explicit **baseline**: the latest earlier observation of the *same
  account* collected ≥ 12 h earlier. Re-ingesting the same collection is never treated as history.
  The baseline snapshot id is stored in `snapshots.evaluation.baseline_snapshot_id`.
* Freshness = analysis time − `collected_at` (for recalculations, the original snapshot time).

## Data validation (before scoring)
`source` must be `grokbot-x-read`; `account.username` must equal the requested handle; counts must be
raw non-negative integers (12.5 → rejected); timestamps ISO-8601; `mention_counts` end > start.
Official-account metrics use only original posts whose `author_handle` is the account (replies/reposts,
foreign authors, duplicates and future-dated posts are excluded and counted in `posts_excluded_*`).
`sample_tweets` is exactly the number of records used in every average. Posts where interactions > views
are flagged as a unit anomaly. The project's own posts are excluded from mentions.

## Categories (weights in `weights.json`)
| Category | Max | Evidence | Missing when |
|---|---|---|---|
| Engagement Quality | 25 | official posts only: robust level = √(median × trimmed mean without top post) (log 50→5000), median views (log 1k→40k), median engagement/followers (10% = max), replies+quotes share. Capped by sample size: 1 post 0.325, 5 posts 0.625, ≥10 posts 1.0 | 0 official posts |
| Community Discussion | 20 | 7-day mention volume (log 600→25k), complete-day peak, unique-author share, **independent authors** share (authors with ≥1 substantive, non-duplicate, non-wallet message). × risk factor 0.7–1.0 (duplicate text, repeat authors, one-thread concentration, new accounts). Sample-only (no counts) capped at 0.4 | no mentions and no counts |
| Growth & Momentum | 20 | last-48 h mention rate (log 200→5000/day), velocity vs older days (damped by volume), real follower growth vs baseline (only if baseline ≥ 12 h earlier), newer-vs-older half of posts (damped). **Never** estimated from account age | none of these available |
| Influential Engagement | 10 | accounts ≥10k followers or org-verified **with a substantive, non-promo mention**; effective = min(relevant, 3 × distinct conversations) (log, 15 = max) | no author follower data |
| Posting Consistency | 10 | posts/week, recency, gap regularity; capped by sample size | < 3 official posts |
| Project Credibility | 10 | verified, bio+links, real-looking mention authors. Not a hype signal on its own | no account data |
| Cross-Platform | 5 | **not collected in this build** → shown as "not collected", excluded from score and coverage | always |

No double counting: risk indicators reduce only Community Discussion (and confidence); engagement is not
also penalised. Concentrated official engagement is handled by the robust level, not a multiplier.

## Missing-data policy
Missing categories get neither full nor zero points: Hype Score = earned points ÷ max points of
available categories × 100. `coverage` = available weight ÷ collectable weight (cross-platform excluded)
and is shown in reports.

## Confidence (evidence quality, independent of score)
Weighted parts: posts sampled 25% (≥15:1, ≥8:0.7, ≥3:0.4, ≥1:0.15), unique discussion authors 15%
(≥50:1, ≥25:0.7, ≥10:0.4, ≥1:0.1), coverage 15%, source 10% (X API 1.0, grokbot-x-read 0.8, other 0.3),
identity 10% (name found in name/handle/bio 1.0, unverified 0.6, mismatch 0), freshness 10% (≤6 h 1,
≤24 h 0.7, ≤72 h 0.4, older 0), history 5%, signal agreement 10% (engagement vs community agreement,
−0.15 per risk indicator).

Labels (hard gates first):
* **Low** if < 3 official posts, coverage < 60%, identity mismatch, or data > 72 h old; or value < 0.5.
* **High** only if value ≥ 0.75 and ≥ 8 posts and ≥ 25 unique authors and coverage ≥ 80% and ≤ 1 risk
  indicator and no single post > 50% of official engagement.
* **Medium** otherwise (value ≥ 0.5).

## Ranking
* Low confidence or no official engagement → **Unranked** (indicative score, `tier = NULL`).
* Medium → tier assigned, marked provisional. High with full coverage → tier, not provisional.
* Tiers: T1 80–100, T2 60–79, T3 40–59, T4 0–39.

## Versions & history
* `snapshots.algorithm_version`: pre-v2 rows are `v1-legacy` (column default from the additive
  migration); originals are never modified (ORM blocks updates/deletes).
* `python scripts/ingest_x.py recalc [project]` re-scores old snapshots into `snapshot_recalcs`
  (`source_snapshot_id`, `original_hype_score/tier/algorithm_version`), evaluated as of the original
  snapshot time with only earlier observations. Idempotent.
* Trends and `/top` compare only results from the same algorithm version.

## Social signals (s1.0) — not part of the score
Smart Account Signal and Sorsa Social Credibility are reported separately (docs/SORSA.md). The hype weights
(25/20/20/10/10/10/5), the research profiles, tier bounds and confidence formula are **unchanged**. They will only be
re-weighted after back-testing against projects with known outcomes.
