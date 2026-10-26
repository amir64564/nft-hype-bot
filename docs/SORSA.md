# Sorsa integration (social signals s1.0)

Sorsa is an **additional verification source, never the authority**. All Sorsa endpoints together count as
**one source family** (never as independent confirmations of each other).

## Endpoints used (base `https://api.sorsa.io/v3`, header `ApiKey: <SORSA_API_KEY>`)
| Endpoint | Used for | Default pack? |
|---|---|---|
| `GET /score?username=` | Sorsa Score (crypto social-graph influence) | yes |
| `GET /followers-stats?username=` | Sorsa-tracked crypto followers: influencers / projects / VCs (**not** total X followers) | yes |
| `GET /top-followers?username=` | top **20** followers by score — an incomplete list | yes |
| `GET /score-changes?username=` | week/month delta (account must already be indexed) | only if cached or remaining > SORSA_OPTIONAL_MIN_REMAINING (default 20) |
| `GET /new-followers-7d?username=` | crypto-tracked follows in the last 7 days (with follow date) | only if cached or remaining > SORSA_OPTIONAL_MIN_REMAINING (default 20) |
| `GET /key-usage-info` | remaining quota | manual (`scripts/sorsa_quota.py --live`) |

The paginated `/followers` endpoint is never used (too expensive).

## Quota policy (free plan ≈ 100 requests)
* Cache in `sorsa_cache` keyed by (endpoint, handle), TTL 24h. 404 / not-indexed answers are cached too.
* Identical requests in one process are deduplicated.
* Every decision (live / cached / no key / soft-stop) is logged in `sorsa_api_log` with remaining quota.
* Soft-stop when the last known remaining < `SORSA_MIN_REMAINING` (10) unless `--force`.
* One research job = at most 3 live calls, once per job (`hints.sorsa_done`). ≈ 30 projects per 100 requests.
* Tests never call Sorsa (`tests/conftest.py` removes the key; HTTP is mocked).

## Scripts
```
python scripts/sorsa_quota.py [--live] [--log 20]
python scripts/sorsa_fetch.py <handle> [--dry-run] [--force]
python scripts/seed_accounts.py import seed_accounts.json   # or .csv with a `handle` column
python scripts/seed_accounts.py mine                        # discovery stats from stored X snapshots
python scripts/seed_accounts.py list
```

## Adding Rudra's ~50 discovery accounts
1. Put handles in `seed_accounts.json` → `"accounts": [{"handle": "abc", "notes": "...", "category_hints": ["alpha_caller"]}]`
   (or a CSV `handle,notes,category_hints` with hints separated by `;`).
2. `python scripts/seed_accounts.py import seed_accounts.json && python scripts/seed_accounts.py mine`
3. Seeded accounts start as **provisional / insufficient_evidence**. They become `medium`/`high` only after ≥3
   known project outcomes (finalized research tier or manual `/review`), with a Laplace-shrunk hit rate
   (≥5 outcomes needed for `high`). Spam-promoter / coordinated flags cap reliability at `low`.

## Limitations
* top-followers = top-20 by score only; absence from it says nothing.
* followers-stats / new-followers-7d cover only accounts in Sorsa's crypto DB.
* score-changes / new-followers-7d need the account to be indexed already.
* Top-follower follow dates are often absent → follow recency is reported as unverified.
* History mining sees only mentions the feeder sampled; "early" = early within our sample, not proven first discovery.
* Few projects have known outcomes yet (only FINALIZED or reviewed ones), so all mined accounts are currently provisional/low.

## Optional endpoints & unknown quota (r2.2)
* If the remaining quota is unknown and an optional endpoint is not cached, the pack makes ONE live
  `/key-usage-info` call (once per client) to learn the real remaining count. Quota is never assumed to reset.
* Optional endpoints run only when cached or remaining > `SORSA_OPTIONAL_MIN_REMAINING` (default 20, was 40).
  Core soft-stop `SORSA_MIN_REMAINING` (10) is unchanged. Every skip is logged as `skipped_optional`.

## Seed matching (r2.2)
Smart Account Signal now also observes tracked/seed accounts that authored a stored mention of the project
(links recorded as `mentioned`/`independent_research`). Matched seeds are listed (`seed_matches`, `is_seed`) but
provisional accounts without known outcomes add 0 to the score. The signal never changes any tier.
