"""Hunter-seed position signal, cook-position keepers, and worthless-account pruning (r2.3).

Thresholds (documented in docs/RESEARCH.md):
* WORTHLESS (non-seed): reliability 'low' OR spam/coordinated flag, AND followers < PRUNE_MAX_FOLLOWERS (500) or
  unknown, AND 0 known outcomes, AND not early-in-sample on >= 2 projects, AND not a cook-position keeper.
  Pruned non-seeds are hard-deleted together with their account_project_links.
* Seeds (source seed_list) are NEVER deleted. A seed with enriched followers < SEED_TINY_FOLLOWERS (100), or
  flagged spam/coordinated, gets flags.worthless_candidate=true + a note for Rudra to review.
* KEEPER (flags.cook_position_keeper): early-in-sample on >= 2 projects, OR early on a project that later
  FINALIZED Tier 1-2, OR mentioned on >= 2 projects whose cook is ongoing (no final outcome, hype rising).
  Keepers are listed first in the Smart Account block. Keeper status never invents outcomes and never
  changes Hype/Quality/Confidence/Tier weights; numeric smart score still needs reliability medium/high.
* Seed follow check uses X API following lookup (app-only bearer). Cached >= 24h per seed. If X refuses
  (403/unsupported tier) the reason is recorded and shown as unavailable; Sorsa + mention-sample fallbacks stay."""
from __future__ import annotations
import logging, os
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, delete
from ..db import Project, Snapshot, utcnow
from .models import TrackedAccount, AccountProjectLink, ResearchJob

log = logging.getLogger("hypebot.seed_position")
PRUNE_MAX_FOLLOWERS = 500
SEED_TINY_FOLLOWERS = 100
FOLLOW_CACHE_H = 24


def _is_seed(a) -> bool:
    return "seed_list" in (a.sources or [])


def _rising(s, project_id) -> bool:
    rows = s.scalars(select(Snapshot).where(Snapshot.project_id == project_id).order_by(Snapshot.created_at.desc()).limit(2)).all()
    return len(rows) == 2 and rows[0].hype_score is not None and rows[1].hype_score is not None \
        and rows[0].hype_score > rows[1].hype_score


def keeper_reason(s, a) -> str | None:
    projs = (a.history or {}).get("projects") or []
    early = [p for p in projs if p.get("early_in_sample")]
    if len(early) >= 2:
        return f"early-in-sample on {len(early)} projects"
    for p in early:
        o = p.get("outcome") or {}
        if o.get("known") and (o.get("tier") or 9) <= 2:
            return f"early on {p.get('project')} which later finalized Tier {o['tier']}"
    pids = {l.project_id for l in s.scalars(select(AccountProjectLink).where(AccountProjectLink.account_id == a.id))}
    from .accounts import project_outcome
    cooking = [pid for pid in pids if not project_outcome(s, pid)["known"] and _rising(s, pid)]
    if len(cooking) >= 2:
        return f"present on {len(cooking)} projects while cook ongoing (rising hype, not finalized)"
    return None


def mark_keepers(db) -> int:
    n = 0
    with db.Session.begin() as s:
        for a in s.scalars(select(TrackedAccount)).all():
            r = keeper_reason(s, a)
            f = dict(a.flags or {})
            f["cook_position_keeper"] = bool(r)
            if r:
                f["cook_position_reason"] = r; n += 1
            else:
                f.pop("cook_position_reason", None)
            a.flags = f
    return n


def is_worthless(a) -> bool:
    f, h = a.flags or {}, a.history or {}
    bad = a.reliability_label == "low" or f.get("spam_promoter") or f.get("coordinated_suspect")
    small = a.followers_count is None or a.followers_count < PRUNE_MAX_FOLLOWERS
    return bool(bad and small and not h.get("known_outcomes") and (h.get("early_in_sample") or 0) < 2
                and not f.get("cook_position_keeper"))


def prune_worthless_accounts(db, dry_run: bool = False) -> dict:
    out = {"pruned": [], "seed_candidates": [], "kept_keepers": 0, "dry_run": dry_run}
    with db.Session.begin() as s:
        for a in s.scalars(select(TrackedAccount)).all():
            f = dict(a.flags or {})
            if f.get("cook_position_keeper"):
                out["kept_keepers"] += 1; continue
            if _is_seed(a):
                tiny = a.followers_count is not None and a.followers_count < SEED_TINY_FOLLOWERS
                if tiny or f.get("spam_promoter") or f.get("coordinated_suspect"):
                    out["seed_candidates"].append(a.x_handle)
                    if not dry_run:
                        f["worthless_candidate"] = True
                        a.notes = ((a.notes or "") + f" [auto {utcnow().date()}: worthless candidate — "
                                   f"followers {a.followers_count}; review, not deleted]").strip()
                        a.flags = f
                continue
            if is_worthless(a):
                out["pruned"].append(a.x_handle)
                if not dry_run:
                    s.execute(delete(AccountProjectLink).where(AccountProjectLink.account_id == a.id))
                    s.delete(a)
    return out


# ------------------------------------------------------------------ X enrichment / follow checks
async def enrich_seed_profiles(db, xclient) -> dict:
    if not xclient or not xclient.available:
        return {"enriched": 0, "unavailable": "X_BEARER_TOKEN not configured"}
    with db.Session() as s:
        handles = [a.x_handle for a in s.scalars(select(TrackedAccount)).all() if _is_seed(a)]
    got, errors = {}, []
    for i in range(0, len(handles), 100):
        try:
            d = await xclient._get("/users/by", {"usernames": ",".join(handles[i:i + 100]),
                                                 "user.fields": "public_metrics,name"})
            for u in d.get("data") or []:
                got[u["username"].lower()] = u
        except Exception as e:
            errors.append(str(e)[:160])
    with db.Session.begin() as s:
        for a in s.scalars(select(TrackedAccount).where(TrackedAccount.x_handle.in_(list(got)))).all():
            u = got[a.x_handle]
            a.x_user_id, a.display_name = u["id"], u.get("name") or a.display_name
            a.followers_count = (u.get("public_metrics") or {}).get("followers_count", a.followers_count)
    return {"enriched": len(got), "seeds": len(handles), "errors": errors}


def _set_status(a, status, checked_at=None):
    h = dict(a.history or {})
    h["follow_check"] = {"status": status, "checked_at": checked_at or utcnow().isoformat()}
    a.history = h


async def check_seed_follows(db, xclient, project_ids: list[int] | None = None, max_seeds: int = 15,
                             force: bool = False) -> dict:
    """One `GET /users/:id/following` page (1000) per seed, intersected with active-research project handles.
    Cached FOLLOW_CACHE_H hours per seed. Stops at the first 401/403 (tier) and records the reason."""
    if not xclient or not xclient.available:
        return {"checked": 0, "status": "X_BEARER_TOKEN not configured"}
    now = datetime.now(timezone.utc)
    with db.Session() as s:
        if project_ids is None:
            project_ids = sorted({j.project_id for j in s.scalars(select(ResearchJob)).all()
                                  if j.status not in ("FINALIZED", "CANCELLED")})
        projs = {p.x_username.lower(): p.id for p in s.scalars(select(Project).where(Project.id.in_(project_ids))).all()
                 if p.x_username}
        seeds = []
        for a in s.scalars(select(TrackedAccount)).all():
            if not _is_seed(a) or not a.x_user_id:
                continue
            fc = (a.history or {}).get("follow_check") or {}
            ts = fc.get("checked_at")
            if not force and fc.get("status") == "ok" and ts and now - datetime.fromisoformat(ts) < timedelta(hours=FOLLOW_CACHE_H):
                continue
            seeds.append((a.id, a.x_user_id))
    res = {"checked": 0, "follows_found": 0, "status": "ok", "projects": len(projs)}
    if not projs:
        res["status"] = "no active research projects"; return res
    for aid, uid in seeds[:max_seeds]:
        try:
            d = await xclient._get(f"/users/{uid}/following", {"max_results": 1000, "user.fields": "username"})
        except Exception as e:
            msg = str(e)
            res["status"] = ("X following endpoint not available on this tier" if "403" in msg or "401" in msg
                             else f"X following check failed: {msg[:120]}")
            with db.Session.begin() as s:
                for a in s.scalars(select(TrackedAccount).where(TrackedAccount.id.in_([x[0] for x in seeds]))).all():
                    _set_status(a, res["status"])
            return res
        follows = {u["username"].lower() for u in d.get("data") or []}
        partial = bool((d.get("meta") or {}).get("next_token"))
        ts = utcnow().isoformat()
        with db.Session.begin() as s:
            a = s.get(TrackedAccount, aid)
            _set_status(a, "ok", ts)
            for h, pid in projs.items():
                l = s.scalar(select(AccountProjectLink).where(AccountProjectLink.account_id == aid,
                             AccountProjectLink.project_id == pid, AccountProjectLink.source == "x_following_check"))
                ev = {"follows": h in follows, "checked_at": ts, "first_page_only": partial}
                if l:
                    l.evidence = ev
                elif h in follows:
                    s.add(AccountProjectLink(account_id=aid, project_id=pid, relationship="followed",
                                             source="x_following_check", follow_date=None, evidence=ev))
                res["follows_found"] += int(h in follows)
        res["checked"] += 1
    return res


def last_follow_check_status(s) -> str | None:
    """Reason string for unavailable_reasons, or None when checks ran fine."""
    sts = [((a.history or {}).get("follow_check") or {}).get("status") for a in s.scalars(select(TrackedAccount)).all()
           if _is_seed(a)]
    bad = [x for x in sts if x and x != "ok"]
    if bad:
        return bad[0]
    if not any(sts):
        return ("not run yet" if os.getenv("SEED_FOLLOW_CHECK", "").lower() == "true"
                else "disabled (set SEED_FOLLOW_CHECK=true + X_BEARER_TOKEN); using Sorsa + mention sample fallback")
    return None
