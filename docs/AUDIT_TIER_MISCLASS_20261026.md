# Tier misclassification audit — 2026-10-26 (r2.5)

Scope: main @ 94c95f0. Previous audit (docs/AUDIT_20261026.md) items were re-verified, not redone:
`configured_tiers()`, committed `tests/fixtures/x_ingest`, identity_state verified/unverified/mismatch,
exact duplicate-evidence drop, why-tier, `tests/test_audit_r24.py` — all present; suite 220/220 passed before changes.

## Key finding: there is no independent ground truth
* `manual_reviews` table in hypebot.db: **0 rows**. No reviewer tier exists for any project.
* `tests/regression_expectations.json` and `tests/fixtures/legacy_tier_corrections.json` are **algorithm echo**
  (v2 output on fixed inputs). Passing them proves stability, not correctness.
* Therefore no statement "the bot is more/less accurate" can be made. `benchmark/tier_benchmark.json`
  now labels every row `ground_truth_source` (all 10 are `algorithm_echo`; 0 `manual_review`) and
  `scripts/benchmark_tiers.py` reports the two groups separately.

## Observed root causes of unstable / questionable tiers (evidence from real fixtures)
| Cause | Evidence | Layer |
|---|---|---|
| Scores sit on tier boundaries | Minehood hype 79.6 (T1 at 80), Freedznft 59.7 (T2 at 60), LootPlanetsRH 38.9 (T3 at 40) | tier boundaries; small sample noise flips tier |
| Single-post concentration still ranks T2 | LilDudes: 56% of engagement in one post → hype T2, Medium | metrics/quality caps (hype path caps confidence only for very few posts; research `official_x` → PARTIAL only when >50%) |
| Small X samples | Minehood: 122 mention authors examined out of ~21.5k 7d mentions; Inkvire 0 official posts | data collection (sample, not population) |
| Hype identity = name substring | `pipeline.identity_verified` returns True if project name appears in name/handle/bio; never False | identity; differs from research (needs ≥2 sourced cross-links) |
| Research finalization rarely reached | 31 research_results; only 3 `Tier` verdicts (2 legacy r1.0); rest Needs More Research / Manual Verification | mandatory checks working as designed; most jobs lack identity/team evidence |
| Seed/collector relevance not observed | DB seed links: 5 total; Sorsa follow check not run (no X_BEARER_TOKEN) | data collection |

None of these were "fixed" by moving weights/thresholds: with no manual benchmark such a change cannot be validated.

## Bugs fixed in this change
1. `hypebot/research/engine.py`: Sorsa identity contradiction was injected whenever the identity check was
   FAILED — which includes *unverified* (0 cross-links). Missing verification was reported as a conflict.
   Now only `identity_state == mismatch` creates a conflict; unverified adds an "unverified" note only.
2. `hypebot/research/engine.py` `f_identity`: no cross-links scored 0.0 (same as a sourced mismatch).
   Now unverified → None (unknown, factor excluded); mismatch → 0.0. Finalization unchanged: unverified still
   FAILS the mandatory identity check. Benchmark impact: none (hype-only benchmark; no finalized tier changed in tests).
3. `dedupe_evidence`: added safe near-duplicate removal (same signal+value+same normalized source URL, or same
   account when no URL, claim-token Jaccard ≥ 0.8). Different source URLs are never merged. Report field
   `duplicate_evidence_audit` = {exact, near, removed:[{id, reason, kept_id}]}.

## Remaining (not fixed)
* Hype-side identity (`pipeline.identity_verified`) is name-substring based and cannot express mismatch;
  research uses sourced cross-links. Unifying needs research evidence available at hype time.
* `accounts.smart_signal(identity_status=...)` parameter is never passed by callers (dead path).
* X accounting tracks posts supplied/excluded(foreign/replies/duplicates/future) and mentions supplied/sampled,
  but not *requested* vs *received* per endpoint, rate-limit/permission outcome per request, or partial pages.
* Growth: follower growth needs a ≥12h baseline; without it the term is omitted (unknown, not 0) but the
  component can still be "ok" from mention velocity alone — no explicit "growth history unknown" label.
* Near-dup detection does not catch paraphrases with low token overlap, or the same claim copied to a new URL.
* URL ≠ support: no automated check that a source page actually contains the claim.

## How to create real ground truth
For each project: a human opens X/site/marketplace, decides the tier, records evidence URLs, and fills
`manual_review` in `benchmark/tier_benchmark.json` (set `ground_truth_source: manual_review`). Then run
`python scripts/benchmark_tiers.py`; only that group may be quoted as accuracy.
