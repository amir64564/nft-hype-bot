"""Account tracking + Smart Account Signal (r2.1).

* Seed accounts (Rudra's discovery-oriented follows) are imported as *candidates*: being seeded never makes an
  account 'smart'. Reliability comes only from recorded history (history mining + manual notes).
* History mining uses ONLY data already stored in this DB (mention authors in X snapshots, research outcomes).
  "Early" means early *within our sampled mention window* — it is not proof of first discovery.
* The Smart Account Signal is a SEPARATE signal. It never changes Hype/Quality/Confidence/Tier weights.
  Following ≠ endorsement. No Sorsa data → 'unavailable', never a penalty."""
from __future__ import annotations
import json, re
from datetime import timezone
from sqlalchemy import select
from ..db import Project, Snapshot, utcnow
from ..metrics import WALLET_RE, _dt
from .models import TrackedAccount, AccountProjectLink, ResearchResult, ManualReview, Evidence

LARGE_FOLLOWERS = 10_000          # >= this = large account (KOL-sized), regardless of history
MIN_OUTCOMES_FOR_RELIABILITY = 3  # fewer known outcomes -> provisional
EARLY_PCT = 0.2                   # first 20% of sampled mention authors (chronological) = "early in sample"
PROMO_RE = re.compile(r"\b(wl|whitelist|allowlist|gtd|giveaway|gib|spots?|drop your|follow\s*&|rt\s*&|tag \d|airdrop)\b", re.I)


def norm(h: str) -> str:
    return (h or "").strip().lstrip("@").lower()


def _aware(d):
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------------------------
# seed import
# ---------------------------------------------------------------------------------------------
def load_seed_file(path: str) -> list[dict]:
    """JSON: {"accounts": [{"handle": "...", "notes": "...", "category_hints": [...]}, ...]} or a list of handles;
    CSV: header with at least `handle` (optional notes, category_hints separated by ';')."""
    import csv, os
    if not os.path.exists(path):
        return []
    if path.endswith(".csv"):
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        return [{"handle": r.get("handle") or r.get("x_handle") or "", "notes": r.get("notes") or None,
                 "category_hints": [x.strip() for x in (r.get("category_hints") or "").split(";") if x.strip()]} for r in rows]
    data = json.load(open(path))
    items = data.get("accounts", []) if isinstance(data, dict) else data
    return [x if isinstance(x, dict) else {"handle": x} for x in items]


HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def import_seeds(db, items: list[dict], source: str = "seed_list") -> dict:
    added, updated, rejected = 0, 0, []
    with db.Session.begin() as s:
        for it in items:
            h = norm(it.get("handle") or "")
            if not HANDLE_RE.match(h):
                rejected.append(it.get("handle")); continue
            a = s.scalar(select(TrackedAccount).where(TrackedAccount.x_handle == h))
            if not a:
                a = TrackedAccount(x_handle=h, sources=[source], flags={}, history={}, confidence=0.0,
                                   reliability_label="provisional")
                s.add(a); added += 1
            else:
                updated += 1
                a.sources = sorted(set((a.sources or []) + [source]))
            if it.get("notes"):
                a.notes = it["notes"]
            if it.get("category_hints"):
                a.category_hints = sorted(set((a.category_hints or []) + list(it["category_hints"])))
            if it.get("display_name"):
                a.display_name = it["display_name"]
    return {"added": added, "updated": updated, "rejected": rejected}


# ---------------------------------------------------------------------------------------------
# history mining (existing DB data only)
# ---------------------------------------------------------------------------------------------
def project_outcome(s, project_id: int) -> dict:
    """Known outcome = manual review tier, else FINALIZED research tier. Anything else is 'unknown'."""
    mr = s.scalar(select(ManualReview).where(ManualReview.project_id == project_id, ManualReview.reviewer_tier.is_not(None))
                  .order_by(ManualReview.created_at.desc(), ManualReview.id.desc()).limit(1))
    if mr:
        return {"known": True, "tier": mr.reviewer_tier, "source": f"manual review #{mr.id}"}
    rr = s.scalar(select(ResearchResult).where(ResearchResult.project_id == project_id,
                                               ResearchResult.finalization_status == "FINALIZED")
                  .order_by(ResearchResult.created_at.desc(), ResearchResult.id.desc()).limit(1))
    if rr and rr.tier:
        return {"known": True, "tier": rr.tier, "source": f"finalized research #{rr.id}"}
    prov = s.scalar(select(ResearchResult).where(ResearchResult.project_id == project_id)
                    .order_by(ResearchResult.created_at.desc(), ResearchResult.id.desc()).limit(1))
    return {"known": False, "tier": None,
            "provisional_tier": (prov.report or {}).get("suggested_tier") if prov else None,
            "source": "no finalized outcome"}


def _mentions(sn) -> list[dict]:
    x = (sn.raw_data or {}).get("x") or {}
    m = x.get("mentions") or {}
    tweets = m.get("tweets") if isinstance(m, dict) else (m if isinstance(m, list) else [])
    users = {}
    for u in (m.get("users") if isinstance(m, dict) else None) or []:
        users[str(u.get("id"))] = u
    out = []
    for t in tweets or []:
        u = users.get(str(t.get("author_id"))) or {}
        h = norm(u.get("username") or t.get("author_id") or "")
        if not h or not t.get("created_at"):
            continue
        out.append({"author": h, "at": t["created_at"], "text": t.get("text") or "", "id": t.get("id"),
                    "followers": (u.get("public_metrics") or {}).get("followers_count")})
    return out


def _is_promo(text: str) -> bool:
    body = re.sub(r"@\w+|https?://\S+", "", text).strip()
    return bool(WALLET_RE.search(text) or PROMO_RE.search(text) or len(body) < 12)


def reliability(n_known: int, hits: int, promo_ratio: float | None, flags: dict) -> tuple[str, float]:
    """Shrunk hit rate; < MIN_OUTCOMES_FOR_RELIABILITY known outcomes -> provisional. Spam/coordination caps at low."""
    conf = min(1.0, n_known / 10)
    if promo_ratio is not None:
        conf *= (1 - 0.5 * promo_ratio)
    if flags.get("spam_promoter") or flags.get("coordinated_suspect"):
        return "low", round(conf, 3)
    if n_known < MIN_OUTCOMES_FOR_RELIABILITY:
        return "provisional", round(conf, 3)
    rate = (hits + 1) / (n_known + 2)            # Laplace shrink: 2/2 hits -> 0.75, not 1.0
    lab = "high" if rate >= 0.65 and n_known >= 5 else "medium" if rate >= 0.5 else "low"
    return lab, round(conf, 3)


def mine_history(db, min_projects: int = 2, create_new: bool = True) -> dict:
    """Compute discovery stats per mention author from stored snapshots. Creates tracked rows only for
    authors seen on >= min_projects distinct projects (plus updates every already-tracked account)."""
    per_author: dict[str, dict] = {}
    texts: dict[str, set] = {}
    with db.Session() as s:
        projects = s.scalars(select(Project)).all()
        outcomes = {p.id: project_outcome(s, p.id) for p in projects}
        for p in projects:
            ms = []
            for sn in s.scalars(select(Snapshot).where(Snapshot.project_id == p.id)).all():
                ms += _mentions(sn)
            seen, first = {}, []
            for m in sorted(ms, key=lambda m: m["at"]):
                if m["author"] in seen or m["author"] == p.x_username:
                    continue
                seen[m["author"]] = m; first.append(m["author"])
            n = len(first)
            for i, h in enumerate(first):
                m = seen[h]
                a = per_author.setdefault(h, {"projects": {}, "mentions": 0, "promo": 0, "followers": None})
                a["projects"][p.id] = {"project": p.x_display_handle, "first_mention_at": m["at"],
                                       "mention_url": f"https://x.com/{h}/status/{m['id']}" if m.get("id") else None,
                                       "rank_in_sample": i + 1, "authors_in_sample": n,
                                       "early_in_sample": n >= 10 and (i + 1) <= max(1, int(n * EARLY_PCT)),
                                       "outcome": outcomes[p.id]}
                if m.get("followers") is not None:
                    a["followers"] = m["followers"]
            for m in ms:
                a = per_author.get(m["author"])
                if a is None:
                    continue
                a["mentions"] += 1
                a["promo"] += int(_is_promo(m["text"]))
                texts.setdefault(re.sub(r"@\w+|https?://\S+|\s+", " ", m["text"]).strip().lower(), set()).add(m["author"])
        # coordinated text: same normalized non-trivial text by >= 3 different authors
        coord = set()
        for t, authors in texts.items():
            if len(t) >= 20 and len(authors) >= 3:
                coord |= authors
        tracked = {a.x_handle for a in s.scalars(select(TrackedAccount)).all()}
    stats = {"authors_seen": len(per_author), "created": 0, "updated": 0}
    with db.Session.begin() as s:
        for h, a in per_author.items():
            if len(a["projects"]) < min_projects and h not in tracked:
                continue
            row = s.scalar(select(TrackedAccount).where(TrackedAccount.x_handle == h))
            if not row:
                if not create_new:
                    continue
                row = TrackedAccount(x_handle=h, sources=["history_mining"], flags={}, history={}, confidence=0.0,
                                     reliability_label="provisional")
                s.add(row); s.flush(); stats["created"] += 1
            else:
                stats["updated"] += 1
                row.sources = sorted(set((row.sources or []) + ["history_mining"]))
            known = [v for v in a["projects"].values() if v["outcome"]["known"]]
            hits = sum(1 for v in known if v["outcome"]["tier"] <= 2)
            promo_ratio = round(a["promo"] / a["mentions"], 3) if a["mentions"] else None
            flags = dict(row.flags or {})
            flags["spam_promoter"] = bool(promo_ratio is not None and a["mentions"] >= 3 and promo_ratio >= 0.6)
            flags["coordinated_suspect"] = h in coord
            flags["insufficient_evidence"] = len(known) < MIN_OUTCOMES_FOR_RELIABILITY
            if a["followers"] is not None:
                row.followers_count = a["followers"]
            lab, conf = reliability(len(known), hits, promo_ratio, flags)
            fc = row.followers_count
            flags["large_kol"] = bool(fc is not None and fc >= LARGE_FOLLOWERS)
            flags["small_discoverer"] = bool(lab in ("medium", "high") and (fc is None or fc < LARGE_FOLLOWERS))
            row.flags = flags
            row.reliability_label, row.confidence = lab, conf
            row.history = {"computed_at": utcnow().isoformat(), "projects_mentioned": len(a["projects"]),
                           "early_in_sample": sum(1 for v in a["projects"].values() if v["early_in_sample"]),
                           "known_outcomes": len(known), "hits_tier_1_2": hits, "misses_tier_3_4": len(known) - hits,
                           "mentions_examined": a["mentions"], "promo_or_low_effort_ratio": promo_ratio,
                           "projects": list(a["projects"].values())[:30],
                           "caveat": "early = early within our sampled mention window, not verified first discovery"}
            for pid, v in a["projects"].items():
                if not s.scalar(select(AccountProjectLink).where(AccountProjectLink.account_id == row.id,
                                                                 AccountProjectLink.project_id == pid,
                                                                 AccountProjectLink.relationship == "mentioned")):
                    s.add(AccountProjectLink(account_id=row.id, project_id=pid, relationship="mentioned",
                                             source="independent_research", first_seen_at=_dt(v["first_mention_at"]),
                                             evidence={"url": v["mention_url"], "rank_in_sample": v["rank_in_sample"],
                                                       "authors_in_sample": v["authors_in_sample"]}))
    return stats


# ---------------------------------------------------------------------------------------------
# Smart Account Signal
# ---------------------------------------------------------------------------------------------
def classify(acc: dict) -> str:
    f = acc.get("flags") or {}
    if f.get("spam_promoter") or f.get("coordinated_suspect"):
        return "low_quality_suspect"
    rel = acc.get("reliability_label") or "provisional"
    fc = acc.get("followers_count")
    if rel in ("medium", "high") and (fc is None or fc < LARGE_FOLLOWERS):
        return "small_discoverer"
    if fc is not None and fc >= LARGE_FOLLOWERS:
        return "large_influential"
    return "insufficient_evidence"


def credibility(pack: dict | None) -> dict:
    """Sorsa Social Credibility summary. Bands are heuristic display labels, not a score input."""
    if not pack or (pack.get("score") is None and not pack.get("followers_stats")):
        reasons = [f"{u['endpoint']}: {u['reason']}" for u in (pack or {}).get("unavailable") or []] or ["no Sorsa data fetched"]
        return {"label": "unavailable", "score": None, "reasons": reasons}
    sc, st = pack.get("score"), pack.get("followers_stats") or {}
    lab = ("unavailable" if sc is None else "very low" if sc < 10 else "low" if sc < 100 else "moderate" if sc < 500 else "high")
    return {"label": lab, "score": sc, "week_delta": pack.get("week_delta"), "month_delta": pack.get("month_delta"),
            "crypto_followers": st.get("followers_count"), "influencers": st.get("influencers_count"),
            "projects": st.get("projects_count"), "vcs": st.get("venture_capitals_count"),
            "fetched_at": pack.get("fetched_at"), "source": "Sorsa (one source family)",
            "note": "counts are Sorsa-tracked crypto followers, not total X followers"}


def _acc_dict(a: TrackedAccount) -> dict:
    return {"handle": a.x_handle, "followers_count": a.followers_count, "sorsa_score": a.sorsa_score,
            "reliability_label": a.reliability_label, "confidence": a.confidence, "flags": a.flags or {},
            "history": a.history or {}, "sources": a.sources or []}


def smart_signal(*, pack: dict | None, tracked: dict, independent: list | None = None,
                 hype_score: float | None = None, independent_flags: dict | None = None,
                 identity_status: str | None = None) -> dict:
    """pack: Sorsa pack dict (or None). tracked: handle -> account dict. independent: [{handle, relationship,
    url}] observed by the independent pipeline (mentions / respected-engagement evidence)."""
    independent = independent or []
    independent_flags = independent_flags or {}
    unavailable = []
    if not pack:
        unavailable.append("Sorsa: no data fetched (key missing, quota soft-stop, or not yet run) — not a penalty")
    else:
        unavailable += [f"Sorsa {u['endpoint']}: {u['reason']}" for u in pack.get("unavailable") or []]
    obs: dict[str, dict] = {}

    def add(h, source, family, follow_date=None, url=None, sorsa_score=None, followers=None):
        h = norm(h)
        o = obs.setdefault(h, {"handle": h, "sources": set(), "families": set(), "follow_verified_at": None,
                               "urls": [], "sorsa_score": None, "followers_count": None})
        o["sources"].add(source); o["families"].add(family)
        if follow_date:
            o["follow_verified_at"] = follow_date
        if url:
            o["urls"].append(url)
        if sorsa_score is not None:
            o["sorsa_score"] = sorsa_score
        if followers is not None:
            o["followers_count"] = followers

    top = (pack or {}).get("top_followers")
    for u in top or []:
        add(u["username"], "sorsa_top_followers", "sorsa", u.get("follower_date"), sorsa_score=u.get("score"),
            followers=u.get("followers_count"))
    for u in (pack or {}).get("new_followers_7d") or []:
        add(u["username"], "sorsa_new_followers_7d", "sorsa", u.get("follower_date") or "within last 7d (Sorsa)",
            sorsa_score=u.get("score"), followers=u.get("followers_count"))
    for x in independent:
        add(x["handle"], f"independent_{x.get('relationship', 'mentioned')}", "independent", url=x.get("url"))
    matched, notable, conflicts = [], [], []
    for h, o in obs.items():
        acc = tracked.get(h)
        if acc is None:
            if "sorsa" in o["families"] and (o.get("sorsa_score") or 0) > 0:
                notable.append({"handle": h, "sorsa_score": o["sorsa_score"], "followers_count": o["followers_count"],
                                "class": "large_influential" if (o["followers_count"] or 0) >= LARGE_FOLLOWERS else "insufficient_evidence",
                                "why": "high-score follower without tracked discovery history (popularity context only)"})
            continue
        acc = dict(acc)
        if acc.get("followers_count") is None and o["followers_count"] is not None:
            acc["followers_count"] = o["followers_count"]
        cls = classify(acc)
        hist = acc.get("history") or {}
        why = (f"{acc['reliability_label']} reliability: {hist.get('known_outcomes', 0)} known outcome(s), "
               f"{hist.get('hits_tier_1_2', 0)} hit(s) T1–T2, {hist.get('early_in_sample', 0)} early-in-sample mention(s)")
        if "seed_list" in (acc.get("sources") or []) and not hist.get("known_outcomes"):
            why = "seed account (Rudra's list) — no recorded discovery history yet"
        rel = "followed" if "sorsa" in o["families"] else "mentioned"
        matched.append({"handle": h, "class": cls, "why": why, "relationship": rel,
                        "source": sorted(o["sources"]), "source_families": sorted(o["families"]),
                        "follow_verified_at": o["follow_verified_at"], "reliability": acc["reliability_label"],
                        "sorsa_score": o["sorsa_score"] if o["sorsa_score"] is not None else acc.get("sorsa_score"),
                        "followers_count": acc.get("followers_count"), "urls": o["urls"][:2]})
        if cls == "low_quality_suspect" and (o.get("sorsa_score") or 0) >= 100:
            conflicts.append({"type": "account_quality", "handle": h,
                              "detail": f"Sorsa score {o['sorsa_score']} looks credible, but our history flags "
                                        + ", ".join(k for k, v in (acc.get("flags") or {}).items() if v and k in ("spam_promoter", "coordinated_suspect"))})
    # coordinated follow burst: >= 3 Sorsa new-followers with the same follow minute
    burst = {}
    for u in (pack or {}).get("new_followers_7d") or []:
        d = (u.get("follower_date") or "")[:16]
        if d:
            burst.setdefault(d, []).append(u["username"])
    coordinated_groups = [v for v in burst.values() if len(v) >= 3]
    for g in coordinated_groups:
        for m in matched:
            if m["handle"] in g and m["class"] != "low_quality_suspect":
                m["class"] = "low_quality_suspect"; m["why"] += "; part of a same-minute follow burst (possible coordination)"
    weight = {"small_discoverer": 1.0, "large_influential": 0.5}
    credible = [m for m in matched if m["class"] in weight and m["reliability"] in ("medium", "high")]
    counted = {m["handle"]: m for m in credible}           # independent accounts = distinct handles
    w = sum(weight[m["class"]] * (1.0 if m["reliability"] == "high" else 0.6) for m in counted.values())
    has_history = any((a.get("history") or {}).get("known_outcomes") for a in tracked.values())
    if not obs and not pack:
        label, score = "unavailable", None
    elif not has_history:
        label, score = "insufficient history", None
    else:
        score = round(min(100.0, 25 * w), 1)
        label = "none observed" if score == 0 else "weak" if score < 25 else "moderate" if score < 60 else "strong"
    cred = credibility(pack)
    # Sorsa vs independent research conflicts (recorded, never auto-resolved)
    promo = independent_flags.get("promo_or_low_effort_ratio")
    if cred["label"] in ("moderate", "high") and promo is not None and promo >= 0.6:
        conflicts.append({"type": "credibility_vs_engagement",
                          "detail": f"Sorsa credibility {cred['label']} (score {cred['score']}) but independent sample is "
                                    f"{promo:.0%} low-effort/promo mentions"})
    resp = independent_flags.get("respected_engagements") or 0
    if pack and resp >= 2 and cred.get("score") is not None and cred["score"] < 10 and not (cred.get("influencers") or 0):
        conflicts.append({"type": "engagement_vs_graph",
                          "detail": f"independent research found {resp} respected-account engagements, but Sorsa shows "
                                    f"score {cred['score']} and 0 tracked influencer followers"})
    unverified = []
    if top is not None:
        unverified.append("Sorsa top-followers is the top-20 by score only — not a complete follower list")
        if any(not u.get("follower_date") for u in top):
            unverified.append("follow dates of top followers not provided — recency of those follows is unverified")
    if pack and identity_status == "FAILED":
        unverified.append("project identity not independently verified — Sorsa data may describe the wrong account")
        conflicts.append({"type": "identity", "detail": "Sorsa returned data for the handle, but independent identity check FAILED"})
    sorsa_only = [m["handle"] for m in counted.values() if m["source_families"] == ["sorsa"]]
    if sorsa_only:
        unverified.append(f"{len(sorsa_only)} credible match(es) seen only via Sorsa (single source family): "
                          + ", ".join("@" + h for h in sorsa_only[:5]))
    early_attention = bool(len(counted) >= 2 and (hype_score is None or hype_score < 50))
    seed_m = [m for m in matched if "seed_list" in ((tracked.get(m["handle"]) or {}).get("sources") or [])]
    for m in matched:
        m["is_seed"] = m in seed_m
    matched.sort(key=lambda m: (not m["is_seed"], m["handle"]))
    return {"smart_account_signal": {"label": label, "score": score, "independent_credible_accounts": len(counted),
                                     "matched": len(matched), "seed_matches": len(seed_m),
                                     "seed_matches_followed": sum(1 for m in seed_m if m["relationship"] == "followed"),
                                     "seed_matches_mentioned": sum(1 for m in seed_m if "independent" in m["source_families"]),
                                     "seeds_tracked": sum(1 for a in tracked.values() if "seed_list" in (a.get("sources") or [])),
                                     "score_basis": "only medium/high-reliability accounts with known outcomes count; "
                                                    "provisional seed matches are listed but add 0"},
            "sorsa_social_credibility": cred, "matched_accounts": matched[:25], "notable_unhistoried_followers": notable[:10],
            "coordinated_groups": coordinated_groups, "conflicts": conflicts, "unavailable_reasons": unavailable,
            "unverified": unverified, "early_attention": early_attention,
            "early_attention_reason": (f"low overall popularity (hype {hype_score}) but {len(counted)} independent credible "
                                       f"discovery account(s) follow/mention it — deserves further research (not a tier boost)")
                                      if early_attention else None,
            "has_sorsa_pack": bool(pack and (pack.get("score") is not None or pack.get("followers_stats") or top is not None)),
            "note": "Following ≠ endorsement, investment or partnership. Separate signal: does not change the tier."}


def tracked_map(s) -> dict:
    return {a.x_handle: _acc_dict(a) for a in s.scalars(select(TrackedAccount)).all()}


def project_mention_authors(s, project_id: int) -> dict:
    """author handle -> earliest stored mention {at, url, followers} across this project's X snapshots."""
    proj = s.get(Project, project_id)
    own = norm(proj.x_username) if proj else None
    out: dict[str, dict] = {}
    for sn in s.scalars(select(Snapshot).where(Snapshot.project_id == project_id)).all():
        for m in _mentions(sn):
            if m["author"] == own:
                continue
            cur = out.get(m["author"])
            if cur is None or m["at"] < cur["at"]:
                out[m["author"]] = {"at": m["at"], "followers": m.get("followers"),
                                    "url": f"https://x.com/{m['author']}/status/{m['id']}" if m.get("id") else None}
    return out


def link_tracked_mention_authors(s, project_id: int) -> list:
    """Tracked accounts (seeds included) that authored a stored mention of this project. Creates a deduped
    'mentioned' AccountProjectLink (source independent_research) when the session is writable. Observed facts only."""
    authors = project_mention_authors(s, project_id)
    if not authors:
        return []
    out = []
    for a in s.scalars(select(TrackedAccount).where(TrackedAccount.x_handle.in_(list(authors)))).all():
        m = authors[a.x_handle]
        out.append({"handle": a.x_handle, "relationship": "mentioned", "url": m["url"]})
        try:
            if s.in_transaction() and not s.scalar(select(AccountProjectLink).where(
                    AccountProjectLink.account_id == a.id, AccountProjectLink.project_id == project_id,
                    AccountProjectLink.relationship == "mentioned")):
                s.add(AccountProjectLink(account_id=a.id, project_id=project_id, relationship="mentioned",
                                         source="independent_research", first_seen_at=_dt(m["at"]),
                                         evidence={"url": m["url"], "via": "project mention scan"}))
        except Exception:   # read-only session: observation still counts, link is recorded next write pass
            pass
    return out


def independent_observations(s, project_id: int, evs: list) -> list:
    """Accounts seen by the independent pipeline: stored mention links, tracked accounts found among this
    project's stored mention authors, and respected-engagement evidence."""
    out = link_tracked_mention_authors(s, project_id)
    rows = s.execute(select(AccountProjectLink, TrackedAccount).join(TrackedAccount, TrackedAccount.id == AccountProjectLink.account_id)
                     .where(AccountProjectLink.project_id == project_id,
                            AccountProjectLink.source.in_(("independent_research", "history_mining")))).all()
    for l, a in rows:
        out.append({"handle": a.x_handle, "relationship": l.relationship, "url": (l.evidence or {}).get("url")})
    for e in evs:
        if e.get("signal") == "respected_account_engaged" and e.get("account") and e.get("verified"):
            out.append({"handle": e["account"], "relationship": "engaged", "url": e.get("source_url")})
    return out


def record_sorsa_links(db, project_id: int, pack: dict):
    """Link tracked accounts seen in a Sorsa pack to the project (append-only, deduped) and store their scores."""
    users = [(u, "sorsa_top_followers") for u in pack.get("top_followers") or []] + \
            [(u, "sorsa_new_followers") for u in pack.get("new_followers_7d") or []]
    with db.Session.begin() as s:
        for u, src in users:
            a = s.scalar(select(TrackedAccount).where(TrackedAccount.x_handle == norm(u["username"])))
            if not a:
                continue
            if u.get("score") is not None:
                a.sorsa_score, a.sorsa_fetched_at = float(u["score"]), utcnow()
            if not s.scalar(select(AccountProjectLink).where(AccountProjectLink.account_id == a.id, AccountProjectLink.project_id == project_id,
                                                             AccountProjectLink.relationship == "followed", AccountProjectLink.source == src)):
                s.add(AccountProjectLink(account_id=a.id, project_id=project_id, relationship="followed", source=src,
                                         follow_date=u.get("follower_date"),
                                         evidence={"sorsa_snapshot_id": pack.get("snapshot_id"), "score": u.get("score")}))
