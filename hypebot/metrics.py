"""Derive metrics from raw collected data (algorithm v2).

Rules:
* Missing inputs -> None (never 0).
* Deterministic: every time-relative metric is measured against the data's own
  ``collected_at`` (``as_of``), never the wall clock, and nothing depends on input order.
* Official-account metrics use only original posts authored by the analysed account.
* Mentions are de-duplicated by id; the project's own posts are not "community".
"""
from __future__ import annotations
import re, statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

WALLET_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
LEAD_HANDLE_RE = re.compile(r"^\s*@(\w+)")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _norm_text(t: str) -> str:
    t = WALLET_RE.sub(" ", t or "")
    t = re.sub(r"https?://\S+|@\w+|#\w+", "", t.lower())
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", t)).strip()


def _eng(pm: dict) -> int:
    return sum(int(pm.get(k) or 0) for k in ("like_count", "retweet_count", "reply_count", "quote_count"))


def resolve_as_of(x: dict | None, now: datetime | None = None) -> datetime:
    if now is not None:
        return now
    ca = (x or {}).get("collected_at")
    if ca:
        return _dt(ca)
    return datetime.now(timezone.utc)  # only for data without a collection time (legacy)


def official_posts(x: dict, as_of: datetime) -> tuple[list, dict]:
    """Original posts by the analysed account, de-duplicated by id. Returns (posts, audit counts)."""
    user = x.get("user") or {}
    me = (user.get("username") or x.get("handle") or "").lower()
    seen, out = set(), []
    audit = {"posts_supplied": len(x.get("tweets") or []), "posts_excluded_foreign": 0,
             "posts_excluded_replies": 0, "posts_excluded_duplicates": 0, "posts_excluded_future": 0}
    for t in sorted(x.get("tweets") or [], key=lambda t: (t.get("created_at", ""), str(t.get("id")))):
        ah = (t.get("author_handle") or "").lower()
        if ah and me and ah != me:
            audit["posts_excluded_foreign"] += 1; continue
        refs = {r.get("type") for r in (t.get("referenced_tweets") or [])}
        if refs & {"replied_to", "retweeted"}:
            audit["posts_excluded_replies"] += 1; continue
        if t.get("id") in seen:
            audit["posts_excluded_duplicates"] += 1; continue
        if _dt(t["created_at"]) > as_of + timedelta(minutes=5):
            audit["posts_excluded_future"] += 1; continue
        seen.add(t.get("id")); out.append(t)
    return out, audit


def derive_metrics(x: dict | None, public: dict | None = None, influencer_min: int = 10_000,
                   now: datetime | None = None) -> dict:
    as_of = resolve_as_of(x, now)
    m: dict = {"as_of": as_of.isoformat(), "data_source": (x or {}).get("source") or ("X API v2" if x else None)}
    user = (x or {}).get("user")
    me = ((user or {}).get("username") or "").lower()
    if user:
        pm = user.get("public_metrics", {})
        m.update(followers=pm.get("followers_count"), following=pm.get("following_count"),
                 tweet_count=pm.get("tweet_count"), listed=pm.get("listed_count"),
                 verified=bool(user.get("verified")), verified_type=user.get("verified_type"),
                 account_age_days=(as_of - _dt(user["created_at"])).days if user.get("created_at") else None,
                 has_bio=bool(user.get("description")), protected=bool(user.get("protected")),
                 has_links=bool(((user.get("entities") or {}).get("url") or {}).get("urls")) or "http" in (user.get("description") or ""))
    if (x or {}).get("tweets") is not None and user:
        own, audit = official_posts(x, as_of)
        m.update(audit)
        n = len(own)
        m["sample_tweets"] = n          # == number of records used in every per-post average below
        if n:
            pms = [t.get("public_metrics", {}) for t in own]
            eng = [_eng(p) for p in pms]
            m.update(avg_likes=statistics.mean(int(p.get("like_count") or 0) for p in pms),
                     avg_reposts=statistics.mean(int(p.get("retweet_count") or 0) for p in pms),
                     avg_replies=statistics.mean(int(p.get("reply_count") or 0) for p in pms),
                     avg_quotes=statistics.mean(int(p.get("quote_count") or 0) for p in pms),
                     avg_engagement=statistics.mean(eng), median_engagement=statistics.median(eng))
            # trimmed level: drop the single best post when n>=3 so one viral post can't set the level
            trimmed = sorted(eng)[:-1] if n >= 3 else eng
            m["trimmed_mean_engagement"] = statistics.mean(trimmed)
            vpairs = [(e, p["impression_count"]) for e, p in zip(eng, pms) if p.get("impression_count") is not None]
            m["posts_with_views"] = len(vpairs)
            m["avg_views"] = statistics.mean(v for _, v in vpairs) if vpairs else None
            m["median_views"] = statistics.median(v for _, v in vpairs) if vpairs else None
            sv = sum(v for _, v in vpairs)
            m["view_engagement_rate"] = (sum(e for e, _ in vpairs) / sv) if vpairs and sv else None
            # unit sanity: interactions should never exceed views on most posts
            m["unit_anomaly_posts"] = sum(1 for e, v in vpairs if v and e > v)
            f = m.get("followers")
            m["engagement_rate"] = (m["median_engagement"] / f) if f else None   # median per-post / followers
            tot = sum(eng)
            m["conversation_ratio"] = (sum(int(p.get("reply_count") or 0) + int(p.get("quote_count") or 0) for p in pms) / tot) if tot else None
            m["top_post_share"] = (max(eng) / tot) if tot else None
            if n >= 6:
                h = n // 2
                old, new = statistics.mean(eng[:h]), statistics.mean(eng[h:])   # own is time-sorted
                m["intra_sample_engagement_change"] = ((new - old) / old) if old else None
            dts = [_dt(t["created_at"]) for t in own]
            m["posts_last_30d"] = sum(1 for d in dts if (as_of - d).days < 30)
            m["days_since_last_post"] = max(0.0, (as_of - dts[-1]).total_seconds() / 86400)
            if n >= 3:
                span_days = max(1.0, (as_of - dts[0]).total_seconds() / 86400)
                m["posts_per_week"] = n / span_days * 7
                gaps = [(b - a).total_seconds() / 3600 for a, b in zip(dts, dts[1:])]
                mg = statistics.mean(gaps)
                m["post_gap_cv"] = (statistics.pstdev(gaps) / mg) if mg else None
    mentions = (x or {}).get("mentions")
    if mentions is not None:
        users = {u["id"]: u for u in mentions.get("users", [])}
        seen, mt = set(), []
        for t in sorted(mentions.get("tweets", []), key=lambda t: (t.get("created_at", ""), str(t.get("id")))):
            if t.get("id") in seen or (me and str(t.get("author_id", "")).lower() == me):
                continue
            seen.add(t.get("id")); mt.append(t)
        m["mentions_supplied"] = len(mentions.get("tweets", []))
        m["mentions_sample"] = len(mt)
        authors = [t.get("author_id") for t in mt]
        m["mention_unique_authors"] = len(set(authors))
        if mt:
            raw = [t.get("text", "") for t in mt]
            texts = [_norm_text(r) for r in raw]
            wallet = [bool(WALLET_RE.search(r)) for r in raw]
            c = Counter(t for t in texts if len(t.split()) >= 4)
            dup = [len(t.split()) >= 4 and c[t] > 1 for t in texts]
            low = [len(t.split()) <= 3 for t in texts]
            promo = [w or d or lo for w, d, lo in zip(wallet, dup, low)]
            n_m = len(mt)
            subst_n = sum(1 for t in texts if len(t.split()) >= 4)
            m["duplicate_text_ratio"] = (sum(dup) / subst_n) if subst_n >= 10 else None
            m["wallet_address_ratio"] = sum(wallet) / n_m
            m["low_effort_ratio"] = sum(low) / n_m
            m["promo_or_low_effort_ratio"] = sum(promo) / n_m
            ac = Counter(authors)
            m["repeat_author_ratio"] = sum(v for v in ac.values() if v > 1) / n_m
            m["top_author_share"] = max(ac.values()) / n_m
            # independent discussion: distinct authors with >=1 substantive, non-duplicate, non-wallet message
            ind = {a for a, p in zip(authors, promo) if not p}
            m["independent_authors"] = len(ind)
            m["distinct_substantive_texts"] = len({t for t, p in zip(texts, promo) if not p})
            # conversation concentration: share of mentions that are replies under the same lead account
            leads = [(LEAD_HANDLE_RE.match(r).group(1).lower() if LEAD_HANDLE_RE.match(r) else None) for r in raw]
            lc = Counter(l for l in leads if l and l != me)
            m["top_thread_share"] = (max(lc.values()) / n_m) if lc else 0.0
            dts = sorted(_dt(t["created_at"]) for t in mt)
            m["mentions_sample_span_hours"] = (dts[-1] - dts[0]).total_seconds() / 3600
            ages, fols = [], []
            infl_any, infl_rel = set(), {}
            by_author = defaultdict(list)
            for a, p, l in zip(authors, promo, leads):
                by_author[a].append((p, l))
            for aid in sorted(set(authors)):
                u = users.get(aid) or {}
                if u.get("created_at"):
                    ages.append((as_of - _dt(u["created_at"])).days)
                fc = (u.get("public_metrics") or {}).get("followers_count")
                if fc is not None:
                    fols.append(fc)
                if (fc or 0) >= influencer_min or u.get("verified_type") in ("business", "government"):
                    infl_any.add(aid)
                    subst = [l for p, l in by_author[aid] if not p]
                    if subst:   # relevant = at least one substantive, non-promo mention
                        infl_rel[aid] = sorted(l or "_top" for l in subst)[0]
            m["new_account_ratio"] = (sum(1 for a in ages if a < 30) / len(ages)) if ages else None
            m["real_author_ratio"] = (sum(1 for f in fols if f >= 50) / len(fols)) if fols else None
            m["author_followers_coverage"] = len(fols) / len(set(authors))
            has_fol = bool(fols) or any(u.get("verified_type") for u in users.values())
            m["influential_mentioners"] = len(infl_any) if has_fol else None
            m["influential_relevant"] = len(infl_rel) if has_fol else None
            m["influential_threads"] = len(set(infl_rel.values())) if has_fol else None
            m["influential_names"] = sorted(infl_rel)[:10]
    counts = (x or {}).get("mention_counts")
    if counts:
        lo = as_of - timedelta(days=7, hours=1)
        buckets = []
        for c in counts:
            if c.get("start") and c.get("end"):
                st, en = _dt(c["start"]), _dt(c["end"])
                if st < lo or en > as_of + timedelta(hours=1) or en <= st:
                    continue   # outside the 7-day window ending at collection time
                buckets.append((en, (en - st).total_seconds() / 3600, int(c.get("tweet_count") or 0)))
        if buckets:
            m["mentions_7d_total"] = sum(n for *_, n in buckets)
            m["mention_counts_hours"] = round(sum(h for _, h, _ in buckets), 2)
            full = [n / h * 24 for _, h, n in buckets if h >= 20]  # complete days only (no extrapolation)
            m["mentions_peak_per_day"] = max(full) if full else None
            end = max(b[0] for b in buckets)
            rec = [(h, n) for e, h, n in buckets if (end - e).total_seconds() / 3600 < 48]
            old = [(h, n) for e, h, n in buckets if (end - e).total_seconds() / 3600 >= 48]
            if rec:
                m["mentions_recent_per_day"] = sum(n for _, n in rec) / sum(h for h, _ in rec) * 24
            if rec and old:
                o = sum(n for _, n in old) / sum(h for h, _ in old) * 24
                m["mention_velocity_change"] = (m["mentions_recent_per_day"] - o) / o if o else None
    return m
