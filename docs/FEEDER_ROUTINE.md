# Feeder routine instructions (paste into the "Hype bot X data feeder" routine prompt)

Work in /workspace/nft-hype-bot with `.venv/bin/python`. Never start/stop the bot. Never invent data: every
claim you submit must come from a page or post you actually read, with its URL. If a source fails, report it
as unavailable — do not guess. Budget: ≤ ~1000 X reads/day across all jobs; spend at most ~60 per job.

Each run:
1. `.venv/bin/python scripts/ingest_x.py list-pending` — for each pending X request, collect real X data
   exactly as before (docs/x_ingest_schema.json: account, 20–100 own recent posts with ids and author_handle,
   up to 100 mentions with ids/author_followers/author_created_at, 7-day daily mention_counts), save to
   x_ingest/req<id>.json and run `scripts/ingest_x.py ingest <id> x_ingest/req<id>.json`
   (or `scripts/ingest_x.py fail <id> "<reason>"` if the account can't be read).
2. `.venv/bin/python scripts/research_feed.py list-jobs` — for each job with "research evidence" in `needs`
   (full payload) or "supplementary research for missing checks" (INCOMPLETE job: research ONLY the items in
   `missing_checks`, submit a payload with just the new claims / visual_review / attempts):
   a. Identity: read the official X profile (bio links), the website (web fetch) and find the marketplace
      collection (OpenSea slug and/or Magic Eden symbol). For every link you SEE, add a claim
      `{"signal":"official_link","value":1,"claim":"<A> links to <B>","source_url":"<page where the link appears>",
      "data":{"from_kind":"x|website|marketplace|discord|artist","to_kind":"..."}}`. If a page links to a
      different account, use value 0. Don't infer links from similar names.
   b. Category: choose primary from artist_led, pfp, generative_art, gaming, brand_ip, membership_utility,
      derivative, experimental, unknown; mint_type free|paid|unknown; stage pre_mint|minting|post_mint|unknown;
      add the source_url that shows it.
   c. Artist/team: X search for founders/artist; prior collections (marketplace pages), delivery, reputation,
      credible copying/abandonment/misleading reports. Signals: team_prior_collection (-1..1),
      team_delivery_record (-1..1), team_known_identity (0..1), team_misleading_claim (-1..0),
      team_abandonment (-1..0), copy_evidence (-1..0, only with a credible source). Anonymous is not negative.
   d. X interest: note respected collectors/artists/alpha callers with NFT history who engaged
      (`respected_account_engaged`, value 0..1, with post URL, account, posted_at, engagement); giveaway-driven
      interest (`giveaway_driven` -1..0) and paid promotion (`paid_promotion` -1..0), each with the post URL.
   e. Art (VISUAL REVIEW, required for pfp / artist_led / generative_art / derivative): download ≥3 (up to 10)
      OFFICIAL images (from the official site, X media or marketplace page) into x_ingest/art/job<id>/ with curl,
      compute `sha256sum`, LOOK at each file (open/read the image), then add `visual_review`:
      `{"reviewer":"grokbot-feeder-visual","reviewed_at":"<UTC ISO>","images":[{"url":..,"source_url":..,
      "local_path":"/workspace/nft-hype-bot/x_ingest/art/job<id>/1.png","sha256":..,"official":true,"viewed":true}],
      "checks":[{"check":"quality|consistency|variation|originality_indicators|suspicious_similarity",
      "judgment":0..1,"notes":"what you saw"}],"download_failures":[{"url":..,"reason":..}]}`.
      quality and consistency checks are required. The bot re-hashes the files: a wrong/missing file is not counted.
      Also list the image URLs in `art.images` (role "project"; role "reference" for suspected source works).
   f. Market/milestones: supply, mint price/date, sold-out/postponed (`milestone` -1..1) or facts
      (`market_fact`), each with source URL. The bot fetches OpenSea/Magic Eden/CoinGecko itself from the slugs.
   g. List every source you could not access in `unavailable` (e.g. Discord invite-only, site down, X limit), and
      log every source you tried in `attempts`: `{"check":..,"source":..,"target":"<url>","outcome":
      "success|not_found|temporary_error|blocked|invalid","error":".."}`. Retry temporary errors next run
      (max 3); don't repeat not_found/blocked; try an alternative source instead.
   h. Write the payload (docs/research_payload_schema.json, `"source":"grokbot-research"`, `"job_id":<id>`)
      to x_ingest/research_job<id>.json and run
      `.venv/bin/python scripts/research_feed.py submit <id> x_ingest/research_job<id>.json`.
      If it prints an error, fix the payload and resubmit.
   i. X counts: identity needs ≥2 independent cross-links (e.g. X↔website AND marketplace→X); official_x needs
      ≥8 own posts; community ≥50 mentions by ≥25 authors + 7-day counts; mint_details ≥2 sourced facts.
3. Optionally `.venv/bin/python scripts/research_feed.py advance` (the bot also does this every 60 s).
4. Report briefly what was ingested/submitted and any failures. Do not post to Discord yourself.


## Routine prompt (r2.0, paste as the routine's prompt)

```
Hype bot feeder (every 10 min, 9:00–23:50 IST). Work in /workspace/nft-hype-bot with .venv/bin/python. Follow docs/FEEDER_ROUTINE.md and docs/research_payload_schema.json. Never start/stop the bot, never post to Discord, never delete anything. Never invent data: every claim needs the URL you actually read; failures go in "unavailable". Budget ≤1000 X reads/day, ≤60 per job.

1. scripts/ingest_x.py list-pending → for each request collect real X data (account, 20–100 own posts, up to 100 mentions, 7-day daily mention counts), save x_ingest/req<id>.json, run `ingest <id> <file>` (or `fail <id> "<reason>"`).
2. scripts/research_feed.py list-jobs → for "research evidence" jobs submit a full payload; for "supplementary research for missing checks" (INCOMPLETE) research ONLY missing_checks and submit only new items.
 a. Identity: ≥2 independent cross-links (X↔website, marketplace→X), each an official_link claim with the page URL.
 b. Category, mint type, stage with source.
 c. Team/prior collections: sourced claims; anonymous is not negative; write "research did not find…", never "no experience".
 d. X interest: respected accounts, giveaway/paid promo with post URLs.
 e. Visual review (pfp/artist_led/generative/derivative): download ≥3 official images to x_ingest/art/job<id>/, sha256sum them, actually look at each, add visual_review (reviewer "grokbot-feeder-visual", images with url/source_url/local_path/sha256/official/viewed, checks quality+consistency(+variation, originality_indicators, suspicious_similarity) judgment 0..1 with notes).
 f. Mint/market: ≥2 sourced facts (supply, price, date, stage); give OpenSea slug / Magic Eden symbol / coingecko_id.
 g. Log every source tried in "attempts" (outcome success|not_found|temporary_error|blocked|invalid). Retry temporary errors next run (max 3); don't repeat not_found/blocked; try an alternative.
 h. Save x_ingest/research_job<id>.json, run `submit <id> <file>`; fix and resubmit on error.
3. Report briefly: requests ingested, jobs submitted, failures.
```
