# Research workflow (research r2.0 on top of hype v2.1; r1.0 notes below kept for history)

## r2.0 summary (2026-10-22)

**State machine** (`research_jobs.status`): `RESEARCHING` → (`RETRYING` while temporary source errors have retries left)
→ `READY_FOR_SCORING` → `FINALIZED` (every mandatory check VERIFIED or NOT_APPLICABLE) or `INCOMPLETE`
(verdict `Needs More Research` / `Manual Verification Required`, no final tier, `not_finalizable_reason` stored).
An INCOMPLETE job can receive a *supplementary* feeder payload (`research_feed.py list-jobs` shows `missing_checks`);
it is re-opened, re-scored into a NEW result row and its report is posted normally. Old statuses were migrated
(queued/researching/scoring → RESEARCHING, failed → INCOMPLETE, done+Tier → FINALIZED, other done → INCOMPLETE);
old result rows are kept (`finalization_status='LEGACY'`).

**Checklist** (`hypebot/research/checklist.py`), each item = status VERIFIED / PARTIAL / FAILED / NOT_APPLICABLE + reason,
evidence, sources, counts, checked_at: identity, official_x, team, track_record, artwork, community, organic,
mint_details, marketplace, benchmark, conflicts.

| Mandatory | When |
|---|---|
| identity, official_x, community, organic | always |
| artwork | artist_led, generative_art, pfp, derivative |
| team | gaming, membership_utility, artist_led |
| mint_details | stage pre_mint / minting |
| marketplace | stage post_mint (pre-mint marketplace = NOT_APPLICABLE) |

Thresholds: identity needs ≥2 independent verified cross-links; official_x ≥8 official posts examined; community
≥50 mentions, ≥25 unique authors and 7-day counts; organic ≥25 mentions; mint_details ≥2 sourced facts;
artwork ≥3 official images viewed by `grokbot-feeder-visual` whose local files match their sha256, plus quality &
consistency checks. Perceptual hashing alone is only PARTIAL (it is not an originality review). Team/track record
wording is always "research did not find…", never "no experience"; anonymity is not negative.

**Retries** (`hypebot/research/attempts.py`, table `research_attempts`, append-only): every fetch is logged with
outcome success / not_found (404/410) / temporary_error (timeouts, 429, 5xx) / blocked (401/402/403) / invalid.
Only temporary errors are retried: up to `RESEARCH_MAX_RETRIES` (3) retries, backoff `RESEARCH_BACKOFF_SECONDS`·2^n
(60 s base), timeout `RESEARCH_TIMEOUT_SECONDS` (15 s). not_found/blocked are not repeated. CoinGecko is tried as an
alternative when marketplace stats fail; image fetches try `alt_urls`. Feeder attempts are stored too (`attempts`).

**Scores**: Hype (X snapshot under the current hype algorithm, v2.1), Quality (research factors) and Confidence are
separate. Suggested tier is capped at one tier better than the quality tier (Tier 2 max without quality) and Tier 1
needs Tier-1 quality itself — hype can never force it. Confidence ∈ [0,1] = 0.35·snapshot confidence +
0.25·checklist coverage (PARTIAL = ½) + 0.15·evidence quality (sourced feeder claims) + 0.10·source reliability +
0.15·(no contradictions); capped ≤0.49 if ≥2 mandatory checks missing / no or Low snapshot, ≤0.74 if 1 missing or
conflicts. One function, `scoring.confidence_label`, maps the number: ≥0.75 High, ≥0.5 Medium, else Low.

**Benchmark**: only latest results of other projects (different project_id and handle) with the same category and
stage; fewer than 3 → "insufficient benchmark data" (no percentiles).

**Views**: `/analyze` report, `/project`, `/compare`, `/history`, `/verify` all read `jobs.latest_result` (the same
row); `/top` ranks only FINALIZED research tiers, then lists hype separately. Any tier from the X snapshot is
labelled "hype tier (not research tier)".

**Re-score** existing evidence without posting: `python scripts/research_feed.py rescore <job ids>`
(new result rows; nothing overwritten). Attempts: `research_feed.py attempts <job id>`.


`/analyze project x [opensea] [magiceden] [website]` no longer returns a tier immediately. It:
1. defers the interaction at once (if Discord already expired it — error 10062, caused by late gateway
   delivery/host stalls seen in `bot.log` — the bot falls back to a normal channel message and continues);
2. queues an `x_requests` row (real X data) and creates a `research_jobs` row (deadline 45 min);
3. posts a progress message that the bot edits every poll (60 s) through the 10 steps;
4. the **feeder routine** fills the X request (`scripts/ingest_x.py ingest`) and submits sourced research
   evidence (`scripts/research_feed.py submit`);
5. the bot itself then calls keyless market APIs + downloads/perceptual-hashes images, finalizes, and posts the report.
If X data or feeder evidence never arrives, at the deadline the job is finalized as **Needs More Research**
with every missing/unavailable source listed. Nothing is ever filled in with guesses.

## Data sources (checked from the box on 2026-10-22)
| Source | Status | Used for |
|---|---|---|
| X (via feeder, Grok Bot public read) | works, ~1000 reads/day | posts, mentions, counts, accounts |
| OpenSea API v2 `/collections/{slug}`, `/collections/{slug}/stats` | **works without key** | supply, owners, floor, sales/volume (1d/7d), linked X account |
| OpenSea events/listings | **needs API key** (401) | — |
| Magic Eden Solana v2 `/collections/{sym}/stats`, `/activities` | **works without key** (rate-limited, 429 seen) | floor, listed, recent sales, wash-pair check |
| Magic Eden EVM v3 (rtp) | **not usable** (400) | — |
| CoinGecko `/nfts/{id}` | works without key (rate-limited) | floor USD, holders, supply |
| Reservoir API | **unreachable** (DNS fails; service shut down) | — |
| Public RPC: publicnode ETH, base.org, Solana mainnet-beta | reachable | not integrated yet (no collection-level use without an indexer) |
| cloudflare-eth | returns error | — |

## Evidence rules (enforced in `hypebot/research/evidence.py` / `engine.py`)
* Every stored claim has `kind` = observation | assumption | judgment. Only **sourced observations** are
  `verified`. Unsourced claims are listed under *Unverified claims* and never enter supporting evidence,
  positives or risks. The audit (`research_feed.py audit`) flags any report claim without a link.
* Art ratings (`art_quality/originality/consistency`) are always stored as *judgment* with image URLs and
  shown in a separate "agent opinion, not fact" section.
* Image similarity (pHash distance ≤ 10/64 vs supplied reference images) is a **risk indicator that
  forces Manual Verification**, never a copying verdict.
* Identity: verified only through sourced cross-links (website ↔ X, marketplace page → X). The bot verifies
  marketplace → X itself from OpenSea's `twitter_username`. No name-similarity assumption. No verified
  link, or a link to a different account ⇒ **Manual Verification Required**.

## Factors and why they matter
| Factor | Why it matters | Evidence |
|---|---|---|
| hype | demand signal on the primary channel (X) | v2 hype score (docs/SCORING.md) |
| x_genuineness | mentions are not automatically positive; giveaway/allowlist farming inflates hype | independent-author share, promo/wallet share, sourced respected-account / giveaway / paid-promo claims |
| team | delivery record predicts follow-through; anonymous ≠ bad, followers ≠ reputation | sourced team signals only; none ⇒ missing (not negative) |
| art | for art-led collections the work is the product | labeled judgments with image URLs |
| originality | copies collapse once discovered | judgments + sourced copy claims; pHash ⇒ manual review |
| market | liquidity and holder dispersion show real demand post-mint; pre-mint judged on milestones only | OpenSea/ME/CoinGecko; floor price alone is not quality; unavailable ≠ zero |
| identity | scams impersonate projects | sourced cross-links |

## Category profiles (`hypebot/research/profiles.py`, weights sum to 100)
| Profile | hype | genuine | team | art | orig. | market | identity | required |
|---|---|---|---|---|---|---|---|---|
| artist_led | 20 | 15 | 20 | 20 | 10 | 10 | 5 | hype, genuine, art |
| generative_art | 20 | 15 | 15 | 20 | 15 | 10 | 5 | hype, genuine, art |
| pfp | 30 | 20 | 15 | 10 | 5 | 15 | 5 | hype, genuine |
| gaming | 25 | 15 | 25 | 5 | 5 | 20 | 5 | hype, genuine, team |
| brand_ip | 20 | 10 | 15 | 10 | 5 | 20 | 20 | hype, genuine |
| membership_utility | 20 | 15 | 25 | 5 | 5 | 25 | 5 | hype, genuine, team |
| derivative | 30 | 20 | 10 | 10 | 15 | 10 | 5 | hype, genuine |
| experimental / unknown | 25/30 | 20 | 15 | 15/10 | 10/5 | 10/15 | 5 | hype, genuine |
Art matters most where the art is the product; team and market for gaming/utility (delivery); identity for
brand/IP (impersonation risk); originality is weighted up for derivatives.

## Verdict rules
* Manual Verification Required: identity not verified, link to a different account, image similarity, or a sourced copying claim.
* Needs More Research: a required factor missing, coverage < 60%, hype snapshot confidence Low, or no X data.
* Otherwise a tier (T1 80+, T2 60–79, T3 40–59, T4 < 40) from the profile-weighted overall score over available factors.
* Precise overall/quality scores are shown only for a tier with Medium/High confidence; otherwise "withheld".
* Hysteresis: a one-tier move is suppressed while the score is within 3 pts of the boundary (raw tier kept in `raw_tier`).
* Changes vs the previous result list the top factor-point deltas and the new evidence ids.
* Peers: hype percentile among latest results in the same category + stage.
* Confidence = 0.5 × hype-snapshot confidence + 0.3 × factor coverage + 0.2 × sourced-claim share; Low if coverage < 60% or snapshot Low; High only if ≥ 0.75, snapshot High and coverage ≥ 85%.

## Manual review
`/verify project` shows evidence, links, unverified claims, conflicts, missing info and confidence reasons.
`/review project action:{confirm|change_tier|reject|note} [tier] [notes]` — only `REVIEWER_USER_IDS`
(default 922754935806894091) or `REVIEWER_ROLE_IDS`. Reviews go to `manual_reviews` and never modify bot
results; they feed `calibration_stats` (exact agreement, within-one-tier, mean offset).

## Tables (additive)
`research_jobs`, `evidence` (no deletes), `research_results` and `manual_reviews` (append-only).
Market evidence is cached 6 h (`expires_at`); a fresh v2 X snapshot (< 24 h) is reused if no new X data arrives.

## Social signals s1.0 (Sorsa + Smart Account Signal)
Research reports now carry `report.signals` with five **separate** signals: overall popularity (hype),
Smart Account Signal, Sorsa Social Credibility, project quality and research confidence. Only the existing
hype/quality/confidence feed the tier; **weights and tier rules are unchanged** pending validation on projects
with known outcomes (`signals.affects_tier = false`).

* Pipeline: job step `social` — `jobs.collect_sorsa` (≤3 cached calls, only with `SORSA_API_KEY`) then
  `jobs.social_signal_for` builds the signal from stored data only (`research/accounts.py`).
* Matched tracked accounts are classed `small_discoverer` | `large_influential` | `insufficient_evidence` |
  `low_quality_suspect`; only medium/high-reliability accounts count, deduped by handle across sources.
  A high follower count or Sorsa score alone never makes an account smart.
* `early_attention`: hype < 50 but ≥2 independent credible discovery accounts → "deserves further research", never a tier boost.
* Conflicts (Sorsa vs independent: credibility vs promo-heavy mentions, respected engagement vs ~0 Sorsa graph,
  spam-flagged account with high Sorsa score, identity FAILED) are recorded in `signals.social_conflicts` and the
  advisory checklist item, never auto-resolved.
* New advisory checklist items `sorsa_social_graph`, `smart_follower_overlap`: never mandatory, excluded from
  coverage/confidence. No Sorsa data → NOT_APPLICABLE (no penalty).
* Tables (additive): `tracked_accounts`, `account_project_links`, `sorsa_project_snapshots` (append-only),
  `sorsa_cache`, `sorsa_api_log` (append-only). See docs/SORSA.md.
