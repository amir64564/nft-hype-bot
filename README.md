# NFT / Web3 Hype Bot (Discord)

Analyzes NFT/Web3 projects from X (Twitter) and public sources, computes a **Hype Score /100**, assigns
**Tier 1–4**, stores append-only snapshots, and tracks trend over time.

## Commands
| Command | What it does |
|---|---|
| `/analyze project x` | Verify identity, collect data, score, save a snapshot (deferred, with progress edits) |
| `/refresh project` | Re-collect and re-score a saved project; reports increasing/decreasing/stable vs previous |
| `/project project` | Latest saved analysis |
| `/history project [limit]` | Score/tier history |
| `/compare projects` | Comma-separated names or handles |
| `/top [limit] [include_provisional]` | Highest ranked |
| `/help` | Help |

## Data sources
| Source | Auth | Used for |
|---|---|---|
| X API v2 `users/by/username` | `X_BEARER_TOKEN` | identity, verified, followers, account age, bio links |
| X API v2 `users/:id/tweets` | `X_BEARER_TOKEN` | likes/reposts/replies/quotes/views, cadence, momentum |
| X API v2 `tweets/search/recent` | `X_BEARER_TOKEN` (Basic+) | community discussion, influential mentioners, bot signals |
| X API v2 `tweets/counts/recent` | `X_BEARER_TOKEN` (Basic+; optional) | 7-day mention volume & change |
| Discord invite API `invites/{code}?with_counts=true` | none | member / online counts |
| CoinGecko `/search`, `/nfts/{id}`, `/coins/{id}` | none (optional key) | listing, handle match, floor, volume |
| Website HTTP check | none | reachability, HTTPS |

Missing sources are **excluded and renormalized**, never treated as zero or fabricated. Each result has a
confidence (coverage × data quality); results are **provisional** if confidence < `PROVISIONAL_CONFIDENCE`
or X engagement data is missing. If less than 50% of the scoring weight has data, a partial score is shown but **no tier is assigned** (and the project is excluded from `/top`).

## Scoring — tuned for short-cycle NFT hype (configurable in `weights.json` or `WEIGHT_<KEY>` env)
Engagement 25 · Community Discussion (mention volume) 25 · Growth & Momentum 25 · Influential Engagement 10 ·
Posting Consistency 10 · Credibility 5. Tiers (4, configurable via `TIERS=80,60,40`): T1 80–100, T2 60–79, T3 40–59, T4 0–39. Only X data is scored.

Curves use `logrange(v, lo, hi)` = 0 at ≤lo, 1 at hi, log-shaped. Per-post levels use √(mean×median) to damp single viral/giveaway posts.
* **Engagement:** interactions/post logrange(50, 5k) ×0.4, views/post logrange(1k, 40k) ×0.3,
  interactions/follower (≥10% = max) ×0.2, conversation share ×0.1.
* **Community:** 7-day mentions logrange(600, 25k) ×0.7 (3.6k ≈ 0.48, 20k ≈ 0.94), peak full day
  logrange(100, 5k) ×0.2, author diversity ×0.1.
* **Growth:** last-48h mentions/day logrange(200, 5k) ×0.4 = volume factor *vf*; velocity
  (0.6 + Δ/2)×*vf* ×0.15 (damped by absolute volume); follower velocity logrange(20, 1k)/day ×0.3 (young
  accounts) or history growth; engagement trend × volume ×0.15.
* **Small samples:** with <8 own posts, engagement and consistency are capped at 0.4 + 0.6·n/8 and their
  data quality (confidence) is reduced.
* **Credibility:** verified, bio/links, share of mention authors that look real. Account age is NOT used.
* **Bot signals** (moderate, multiplier floor 0.75): copy-paste duplicates among ≥4-word mentions,
  >50% mentioning accounts <30 days old, repeat-author concentration. Short gm/LFG/wen replies are normal
  and not flagged. Extreme engagement/follower is only flagged when corroborated by those signals.
* Website/Discord/CoinGecko are no longer collected or scored (`collectors/public.py` kept, unused).

## Setup
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill DISCORD_TOKEN, X_BEARER_TOKEN
SYNC_COMMANDS=true .venv/bin/python -m hypebot
.venv/bin/python -m pytest -q
```
### Discord application
1. https://discord.com/developers/applications → New Application → Bot → copy token → `DISCORD_TOKEN`.
2. No privileged intents required.
3. Invite: OAuth2 URL Generator, scopes `bot` + `applications.commands`, permissions *Send Messages*,
   *Embed Links*: `https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot+applications.commands&permissions=18432`
4. Run once with `SYNC_COMMANDS=true` (set `DISCORD_GUILD_ID` for instant guild sync).
   ⚠️ Use a **dedicated application**: a global sync replaces that application's existing global commands.

### X API
Create a project/app at https://developer.x.com, copy the app **Bearer Token**. Free tier cannot use
search; **Basic or higher** is needed for community-discussion/influencer metrics. 429s honour
`x-rate-limit-reset` (waits ≤60s, otherwise reports the partial failure).

### PostgreSQL
`DATABASE_URL=postgresql://user:pass@host:5432/hypebot` — tables are created automatically.
Snapshots are append-only (ORM blocks updates/deletes).

## Deployment
* **Docker:** `docker build -t hypebot . && docker run -d --env-file .env -v hypebot-data:/data hypebot`
* **systemd:** copy to `/opt/nft-hype-bot`, create venv, `cp deploy/hypebot.service /etc/systemd/system/`,
  `systemctl enable --now hypebot`.

## Layout
`hypebot/collectors/` (X API, public sources) · `metrics.py` · `scoring.py` · `db.py` · `pipeline.py` ·
`embeds.py` · `bot.py` · `tests/` (mocks only in tests) · `scripts/smoke_public_sources.py` (live keyless check).

## External X data path (no X API token)
When `X_BEARER_TOKEN` is absent, `/analyze` and `/refresh` post a provisional report (public sources only)
**and** queue a row in `x_requests` (no score is produced without X data) (project, handle, channel/guild/user/interaction ids, status
`pending|done|failed`). Real X data collected elsewhere can then be ingested:

```bash
.venv/bin/python scripts/ingest_x.py list-pending                 # JSON list of pending requests
.venv/bin/python scripts/ingest_x.py ingest <request_id> data.json # validate + score as NEW snapshot
.venv/bin/python scripts/ingest_x.py rescore <project|@handle> data.json  # re-score saved real data, NEW snapshot, no Discord post
.venv/bin/python scripts/ingest_x.py fail <request_id> "reason"
```
Ingest validates the payload, creates a new append-only snapshot (sources tagged `grokbot-x-read`,
computes the trend vs. the previous one and marks
the request `done`. The running bot polls every `XREQUEST_POLL_SECONDS` (default 60) and posts the full
report in the original channel, mentioning the requester. Invalid payloads mark the request `failed`.

Schema: [`docs/x_ingest_schema.json`](docs/x_ingest_schema.json). Example:
```json
{
  "source": "grokbot-x-read",
  "collected_at": "2026-10-21T14:00:00Z",
  "account": {"followers": 40000, "following": 200, "created_at": "2024-11-01T00:00:00Z",
              "verified": true, "description": "…", "tweet_count": 3000},
  "recent_posts": [{"id": "1", "text": "…", "created_at": "2026-10-20T10:00:00Z", "likes": 500,
                    "replies": 50, "reposts": 100, "quotes": 2, "views": 20000, "author_handle": "proj"}],
  "mentions": [{"text": "…", "author_handle": "fan1", "author_followers": 20000,
                "author_created_at": "2025-01-01T00:00:00Z", "likes": 4, "created_at": "2026-10-21T09:00:00Z"}]
}
```
Required: `source`, `collected_at`, `account.followers`, `recent_posts` (each: `created_at`, `likes`,
`replies`, `reposts`). Optional `mention_counts` [{start,end,tweet_count}] (daily counts) feeds 7-day volume/change. Omitted `mentions`/`views` are treated as **missing**, never zero. Only submit data
actually read from X.


## Scoring algorithm v2.0
Scoring and confidence rules are documented in [docs/SCORING.md](docs/SCORING.md). Summary: deterministic
(no randomness, times measured from `collected_at`), official engagement only from the account's own
original posts, missing categories excluded (coverage shown), confidence based on evidence quality,
Low-confidence results are **Unranked**. Re-score history without touching originals:
`python scripts/ingest_x.py recalc [project]`.


## Deep research (r1.0)
`/analyze` starts a background research job (15–30 min) instead of an instant tier; `/verify` shows the evidence
behind a result; `/review` lets authorized reviewers (REVIEWER_USER_IDS / REVIEWER_ROLE_IDS) record decisions
for calibration. See [docs/RESEARCH.md](docs/RESEARCH.md) (workflow, sources, profiles, verdict rules) and
[docs/FEEDER_ROUTINE.md](docs/FEEDER_ROUTINE.md) (what the external feeder must do).
Feeder CLI: `scripts/research_feed.py list-jobs|show|submit|unavailable|finalize|advance|audit`.

## Sorsa & smart-follower signals
Optional `SORSA_API_KEY` adds Sorsa Score, crypto follower breakdown and top-20 followers to research reports, plus a
Smart Account Signal from tracked discovery accounts (`seed_accounts.json`). Separate signals only — tiers/weights
unchanged. Quota-safe (24h cache, ≤3 calls/project, soft-stop at 10 remaining). See docs/SORSA.md.

### Hunter seeds (r2.3)
`python scripts/seed_accounts.py enrich` → `python scripts/seed_accounts.py follows` (needs `X_BEARER_TOKEN`, optional
`SEED_FOLLOW_CHECK=true`) and `python scripts/prune_accounts.py` to drop junk non-seed accounts. Details: docs/RESEARCH.md.
