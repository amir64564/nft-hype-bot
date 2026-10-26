"""Research job orchestration. The bot creates jobs; the external feeder (routine) fills the X request
(scripts/ingest_x.py) and submits research evidence (scripts/research_feed.py); the bot collects
keyless market data + image hashes itself, then finalizes. Sources that fail are listed, never faked."""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from ..db import Database, Project, Snapshot, XRequest, utcnow
from ..scoring import ALGORITHM_VERSION
from .models import ResearchJob, Evidence, ResearchResult, ManualReview, ResearchAttempt, JOB_STEPS
from . import attempts as att
from .evidence import validate_research_payload, ResearchPayloadError
from .engine import evaluate_research, RESEARCH_VERSION
from . import market as market_mod, images as images_mod

log = logging.getLogger("hypebot.research")
JOB_DEADLINE_MIN = 45
MARKET_TTL_H = 6        # market evidence is refreshed after this
X_TTL_H = 24


def _aware(d):
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def _set(job, step, status, note=None):
    st = dict(job.steps or {})
    st[step] = {"status": status, "note": note, "at": utcnow().isoformat()}
    job.steps = st


def report_fingerprint(rep: dict | None) -> str:
    """Stable short hash of the Discord-visible report. Used to skip duplicate posts."""
    import hashlib, json
    r = rep or {}
    ck = {k: ((v or {}).get("status"), (v or {}).get("reason")) for k, v in (r.get("checklist") or {}).items()}
    sig = r.get("signals") or {}
    payload = {
        "status": r.get("status"), "verdict": r.get("verdict_text") or r.get("verdict"),
        "tier": r.get("tier"), "suggested": r.get("suggested_tier"),
        "hype": round(float(r.get("hype_score") or 0), 1),
        "quality": round(float(r.get("quality_score_internal") or r.get("quality_score") or 0), 1),
        "overall": round(float(r.get("overall_score_internal") or r.get("overall_score") or 0), 1),
        "confidence": round(float(r.get("confidence") or 0), 3),
        "checklist": ck,
        "smart": (sig.get("smart_account_signal") or {}).get("label"),
        "sorsa": (sig.get("sorsa_social_credibility") or {}).get("label"),
        "not_final": r.get("not_finalizable_reason"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def render_progress(job, project_name) -> str:
    icon = {"done": "✅", "running": "⏳", "waiting": "🕓", "failed": "⚠️", "skipped": "➖"}
    lines = [f"🔬 **Research job #{job.id} — {project_name}** · status: **{job.status}**"
             + (" (not fully researched — see report)" if job.status == "INCOMPLETE" else "")]
    for i, (k, label) in enumerate(JOB_STEPS, 1):
        s = (job.steps or {}).get(k) or {"status": "waiting"}
        lines.append(f"{icon.get(s['status'], '🕓')} {i}. {label}" + (f" — {s['note']}" if s.get("note") else ""))
    if job.unavailable_sources:
        lines.append(f"⚠️ {len(job.unavailable_sources)} source(s) unavailable so far (listed in the final report).")
    if job.status in ACTIVE:
        lines.append("_Real research runs in the background (typically 15–30 min: X data is collected by the "
                     "feeder every 10 min, 9 AM–11:50 PM IST). No tier is given until it finishes._")
    return "\n".join(lines)[:1990]


def create_job(db: Database, project_id: int, x_request_id: int | None, ctx: dict | None = None,
               hints: dict | None = None) -> int:
    ctx = ctx or {}
    with db.Session.begin() as s:
        j = ResearchJob(project_id=project_id, x_request_id=x_request_id, status="RESEARCHING",
                        channel_id=ctx.get("channel_id"), guild_id=ctx.get("guild_id"), requested_by=ctx.get("user_id"),
                        hints=hints or {}, unavailable_sources=[], steps={},
                        deadline_at=utcnow() + timedelta(minutes=JOB_DEADLINE_MIN))
        _set(j, "identity", "waiting", "feeder")
        _set(j, "x_data", "waiting", f"x_request #{x_request_id}" if x_request_id else "no request")
        s.add(j); s.flush()
        if x_request_id:   # the research report replaces the old one-off X follow-up post
            r = s.get(XRequest, x_request_id)
            if r:
                r.posted = True
        return j.id


def store_evidence(s, job, claims, collected_by, ttl_h=None):
    exp = utcnow() + timedelta(hours=ttl_h) if ttl_h else None
    seen = set()
    if collected_by.startswith("bot:"):   # retries must not duplicate bot observations
        seen = {(e.source_url, e.claim) for e in s.scalars(select(Evidence).where(Evidence.job_id == job.id,
                                                                                 Evidence.collected_by == collected_by)).all()}
    for c in claims:
        if (c.get("source_url"), c["claim"]) in seen:
            continue
        s.add(Evidence(job_id=job.id, project_id=job.project_id, section=c["section"], kind=c["kind"], claim=c["claim"],
                       source_url=c.get("source_url"), source_name=c.get("source_name"), account=c.get("account"),
                       posted_at=c.get("posted_at"), engagement=c.get("engagement"), why=c.get("why"),
                       signal=c.get("signal"), value=c.get("value"), data=c.get("data"),
                       verified=bool(c.get("verified")), collected_by=collected_by, expires_at=exp))


def submit_feeder(db: Database, job_id: int, payload: dict) -> dict:
    with db.Session.begin() as s:
        job = s.get(ResearchJob, job_id)
        if not job:
            raise ResearchPayloadError(f"research job #{job_id} not found")
        if job.status == "FINALIZED":
            raise ResearchPayloadError(f"research job #{job_id} is already FINALIZED")
        p = validate_research_payload(payload, job_id)
        for a in p["attempts"]:
            att.record(s, job.id, a["check"], a["source"], a["target"], a["outcome"], error=a["error"], by="feeder")
        supplement = job.status == "INCOMPLETE"
        if supplement:   # supplementary research for missing checks: store only claims not already stored
            have = {(e.source_url, e.claim) for e in s.scalars(select(Evidence).where(Evidence.job_id == job.id)).all()}
            p["claims"] = [c for c in p["claims"] if (c.get("source_url"), c["claim"]) not in have]
        store_evidence(s, job, p["claims"], "feeder")
        h = dict(job.hints or {})
        new = {"category": p["category"], "links": p["links"], "marketplaces": p["marketplaces"],
               "people": p["people"], "images": p["images"], "feeder_notes": p["notes"],
               "coingecko_id": payload.get("coingecko_id"), "visual_review": p["visual_review"]}
        if supplement:   # keep earlier values the supplement doesn't repeat
            new = {k: v for k, v in new.items() if v not in (None, [], {}, "") and not (k == "category" and v.get("primary") == "unknown")}
            if p["marketplaces"] or p["images"]:
                h["bot_sources_done"] = False
            job.posted = False   # allow delivery; finalize/update_research skip or edit if unchanged
        h.update(new)
        job.hints = h
        job.unavailable_sources = list(job.unavailable_sources or []) + p["unavailable"]
        job.feeder_submitted = True
        job.status = "RESEARCHING"
        by = {}
        for c in p["claims"]:
            by[c["section"]] = by.get(c["section"], 0) + 1
        _set(job, "identity", "done", f"{by.get('identity', 0)} claims")
        _set(job, "team", "done" if by.get("team") else "skipped", f"{by.get('team', 0)} claims")
        _set(job, "art", "running" if p["images"] else ("done" if by.get("art") else "skipped"),
             f"{len(p['images'])} images, {by.get('art', 0)} claims")
        _set(job, "category", "done", f"{p['category']['primary']} · {p['category']['mint_type']} mint · {p['category']['stage']}")
        _set(job, "market", "running" if p["marketplaces"] else "skipped", f"{len(p['marketplaces'])} marketplace(s)")
        return {"job_id": job_id, "claims": len(p["claims"]),
                "unverified": sum(1 for c in p["claims"] if not c["verified"]), "images": len(p["images"])}


def mark_unavailable(db, job_id, source, reason):
    with db.Session.begin() as s:
        job = s.get(ResearchJob, job_id)
        job.unavailable_sources = list(job.unavailable_sources or []) + [{"source": source, "reason": reason}]


class DBHooks(market_mod.Hooks):
    def __init__(self, db, job_id, check):
        self.db, self.job_id, self.check = db, job_id, check

    def allow(self, target):
        with self.db.Session() as s:
            return att.should_try(s, self.job_id, target)

    def record(self, source, target, outcome, http_status=None, error=None, duration_ms=None, alternative_for=None):
        with self.db.Session.begin() as s:
            att.record(s, self.job_id, self.check, source, target, outcome, http_status, error,
                       alternative_for=alternative_for, duration_ms=duration_ms)


async def collect_bot_sources(db: Database, job_id: int, http_client=None):
    """Keyless market APIs + image hashes. Re-uses fresh cached market evidence for the project."""
    with db.Session() as s:
        job = s.get(ResearchJob, job_id)
        h = dict(job.hints or {})
        fresh = s.scalars(select(Evidence).where(Evidence.project_id == job.project_id, Evidence.collected_by == "bot:market",
                                                 Evidence.expires_at > utcnow())).all()
    mk_claims, unav = [], []
    if h.get("marketplaces") or h.get("coingecko_id"):
        if fresh:
            mk_claims = []   # cached evidence will be included via project query
            note = f"re-used {len(fresh)} cached market observations (<{MARKET_TTL_H}h old)"
        else:
            mk_claims, unav = await market_mod.collect_market(h.get("marketplaces") or [], h.get("coingecko_id"), http_client,
                                                              hooks=DBHooks(db, job_id, "marketplace"))
            note = f"{len(mk_claims)} observations, {len(unav)} unavailable"
    else:
        note = "no marketplace given"
    # identity cross-links the bot can verify itself from marketplace metadata (never by name similarity)
    with db.Session() as s:
        handle = s.get(Project, s.get(ResearchJob, job_id).project_id).x_display_handle.lower()
    for c in list(mk_claims):
        links = (c.get("data") or {}).get("links") or {}
        tw = (links.get("twitter_username") or "").lstrip("@").lower()
        if tw:
            mk_claims.append({"section": "identity", "kind": "observation", "signal": "official_link",
                              "value": 1.0 if tw == handle else 0.0, "source_url": c["source_url"], "source_name": c["source_name"],
                              "claim": f"{c['source_name']} collection lists X account @{tw}" + ("" if tw == handle else f" (≠ @{handle})"),
                              "data": {"from_kind": "marketplace", "to_kind": "x"}, "verified": True,
                              "why": "Marketplace collection page links to the official X account"})
    img_claims = []
    if h.get("images"):
        hashed, u2 = await images_mod.fetch_hashes(h["images"], http_client, hooks=DBHooks(db, job_id, "artwork"))
        unav += u2
        pairs = images_mod.compare(hashed) if hashed else []
        img_claims = images_mod.comparison_claims(pairs)
        h["image_hashes"] = hashed
    with db.Session.begin() as s:
        job = s.get(ResearchJob, job_id)
        store_evidence(s, job, mk_claims, "bot:market", MARKET_TTL_H)
        store_evidence(s, job, img_claims, "bot:images")
        known = {(u["source"], u["reason"]) for u in job.unavailable_sources or []}
        unav = [u for u in unav if not str(u.get("reason", "")).startswith("skipped (")]   # already handled earlier
        job.unavailable_sources = list(job.unavailable_sources or []) + [u for u in unav if (u["source"], u["reason"]) not in known]
        pend = att.pending_retries(s, job.id)
        hh = dict(job.hints or {}); hh["bot_sources_done"] = not pend
        job.status = "RETRYING" if pend else "RESEARCHING"
        if pend:
            _set(job, "market", "running", f"retrying {len(pend)} source(s) with backoff")
        if "image_hashes" in h:
            hh["image_hashes"] = h["image_hashes"]
        job.hints = hh
        _set(job, "market", "done" if (h.get("marketplaces") or h.get("coingecko_id")) else "skipped", note)
        if h.get("images"):
            _set(job, "art", "done", f"{len(h.get('image_hashes') or [])} images hashed, {len(img_claims)} comparison notes")


def _snap_dict(sn):
    if not sn:
        return None
    return {"id": sn.id, "hype_score": sn.hype_score, "tier": sn.tier, "confidence": sn.confidence,
            "confidence_label": sn.confidence_label, "metrics": sn.metrics, "missing": sn.missing,
            "flags": sn.flags, "evaluation": sn.evaluation}


def x_evidence_claims(snap, handle) -> list:
    """Sourced X observations derived from the stored raw X data of the snapshot."""
    out = []
    x = (snap.raw_data or {}).get("x") or {}
    for t in sorted(x.get("tweets") or [], key=lambda t: -sum((t.get("public_metrics") or {}).values()))[:5]:
        if t.get("id") and not str(t["id"]).startswith("h"):
            pm = t.get("public_metrics") or {}
            out.append({"section": "x_interest", "kind": "observation", "claim": f"Official post: {t.get('text', '')[:140]}",
                        "source_url": f"https://x.com/{handle}/status/{t['id']}", "source_name": "X", "account": handle,
                        "posted_at": t.get("created_at"), "engagement": pm, "why": "Top official post by engagement",
                        "signal": None, "value": None, "data": None, "verified": True})
    return out


ACTIVE = ("RESEARCHING", "RETRYING", "READY_FOR_SCORING", "queued", "researching", "scoring")


def current_hype(s, snap):
    """Hype record under the CURRENT hype algorithm for a snapshot: the snapshot itself, or its
    recalculation. Report and /project then always show the same hype numbers."""
    if snap is None:
        return None
    if snap.algorithm_version == ALGORITHM_VERSION:
        return _snap_dict(snap)
    from ..db import SnapshotRecalc
    rc = s.scalar(select(SnapshotRecalc).where(SnapshotRecalc.source_snapshot_id == snap.id,
                                               SnapshotRecalc.algorithm_version == ALGORITHM_VERSION))
    if rc is None:
        return dict(_snap_dict(snap), stale_version=snap.algorithm_version)
    d = {"id": snap.id, "recalc_id": rc.id, "hype_score": rc.hype_score, "tier": rc.tier, "confidence": rc.confidence,
         "confidence_label": rc.confidence_label, "metrics": rc.metrics, "missing": rc.missing, "flags": rc.flags,
         "evaluation": rc.evaluation, "version": ALGORITHM_VERSION}
    return d


def find_peers(s, proj, cat):
    """Latest result of OTHER projects (by id and handle) in the same category & stage."""
    out = {}
    for pr in s.scalars(select(ResearchResult).where(ResearchResult.category == cat["primary"],
                                                     ResearchResult.stage == cat.get("stage", "unknown"),
                                                     ResearchResult.project_id != proj.id)).all():
        pp = s.get(Project, pr.project_id)
        if pp.x_username == proj.x_username:
            continue
        if pr.project_id not in out or pr.id > out[pr.project_id]["id"]:
            sn = s.get(Snapshot, pr.snapshot_id) if pr.snapshot_id else None
            out[pr.project_id] = {"name": pp.name, "hype_score": pr.hype_score, "pid": pr.project_id, "id": pr.id,
                                  "metrics": (sn.metrics if sn else {})}
    return list(out.values())


def finalize(db: Database, job_id: int, force: bool = False, rescore: bool = False) -> int | None:
    """Score and store a ResearchResult when inputs are ready, the deadline passed, or force/rescore.
    Status becomes FINALIZED only if every mandatory check is verified; otherwise INCOMPLETE."""
    with db.Session.begin() as s:
        job = s.get(ResearchJob, job_id)
        if job.status not in ACTIVE and not rescore:
            return job.result_id
        proj = s.get(Project, job.project_id)
        xr = s.get(XRequest, job.x_request_id) if job.x_request_id else None
        x_done = xr is not None and xr.status == "done"
        if xr is None:
            sn0 = db.latest_snapshot(s, proj.id, ALGORITHM_VERSION)
            x_done = bool(sn0 and _aware(sn0.created_at) > utcnow() - timedelta(hours=X_TTL_H))
        if xr is not None and xr.status == "failed":
            _set(job, "x_data", "failed", xr.error or "failed")
        late = _aware(job.deadline_at) and utcnow() > _aware(job.deadline_at)
        ready = x_done and job.feeder_submitted and (job.hints or {}).get("bot_sources_done")
        if not (ready or force or late or rescore):
            if x_done:
                _set(job, "x_data", "done", f"snapshot #{xr.result_snapshot_id}")
            return None
        job.status = "READY_FOR_SCORING"
        snap = s.get(Snapshot, xr.result_snapshot_id) if (xr is not None and xr.result_snapshot_id) else None
        if snap is None:
            sn2 = db.latest_snapshot(s, proj.id)
            if sn2 and _aware(sn2.created_at) > utcnow() - timedelta(hours=X_TTL_H):
                snap = sn2
        if snap:
            _set(job, "x_data", "done", f"snapshot #{snap.id}")
            if not s.scalar(select(Evidence).where(Evidence.job_id == job.id, Evidence.collected_by == "bot:x")):
                store_evidence(s, job, x_evidence_claims(snap, proj.x_display_handle), "bot:x")
                s.flush()
        else:
            _set(job, "x_data", "failed", "no X data arrived before the deadline")
            if not any(u["source"] == "X data" for u in job.unavailable_sources or []):
                job.unavailable_sources = list(job.unavailable_sources or []) + [{"source": "X data", "reason": "not collected before deadline"}]
        _set(job, "x_interest", "done" if snap else "failed")
        evs = list(s.scalars(select(Evidence).where(Evidence.job_id == job.id)).all())
        evs += list(s.scalars(select(Evidence).where(Evidence.project_id == proj.id, Evidence.job_id != job.id,
                                                     Evidence.collected_by == "bot:market", Evidence.expires_at > utcnow())).all())
        cat = (job.hints or {}).get("category") or {"primary": "unknown", "stage": "unknown", "mint_type": "unknown"}
        prev = s.scalar(select(ResearchResult).where(ResearchResult.project_id == proj.id)
                        .order_by(ResearchResult.created_at.desc(), ResearchResult.id.desc()).limit(1))
        peers = find_peers(s, proj, cat)
        _set(job, "history", "done", f"{len(peers)} peer(s) in {cat['primary']}/{cat.get('stage')}; "
                                     + ("previous result #%d" % prev.id if prev else "no previous research"))
        hype_now = current_hype(s, snap)
        social = social_signal_for(s, proj, [_evd(e) for e in evs], hype_now)
        _set(job, "social", "done" if social["has_sorsa_pack"] else "skipped",
             f"smart signal {social['smart_account_signal']['label']}, Sorsa {social['sorsa_social_credibility']['label']}")
        rep = evaluate_research(snap=hype_now, evidence=evs, category=cat, handle=proj.x_display_handle,
                                previous=(prev.report if prev else None), peers=peers,
                                unavailable=job.unavailable_sources, hints=job.hints, attempts=att.summary(s, job.id),
                                social=social)
        _set(job, "score", "done", f"profile {rep['profile']}, checklist coverage {rep['coverage']:.0%}")
        prev_for_job = s.scalar(select(ResearchResult).where(ResearchResult.job_id == job.id)
                                .order_by(ResearchResult.id.desc()).limit(1))
        res = ResearchResult(job_id=job.id, project_id=proj.id, snapshot_id=snap.id if snap else None,
                             algorithm_version=rep["algorithm"], category=rep["profile"],
                             stage=rep["stage"], verdict=rep["verdict"], tier=rep["tier"], raw_tier=rep["raw_tier"],
                             overall_score=rep["overall_score_internal"], hype_score=rep["hype_score"],
                             quality_score=rep["quality_score_internal"], confidence=rep["confidence"],
                             confidence_label=rep["confidence_label"], report=rep, finalization_status=rep["status"])
        s.add(res); s.flush()
        job.result_id = res.id
        job.status = rep["status"]
        fp = report_fingerprint(rep)
        # Same Discord-visible report as last time → keep posted=True so the bot does not re-send.
        # Meaningful change → posted=False so update_research can edit the existing message (or post once).
        if prev_for_job and report_fingerprint(prev_for_job.report) == fp:
            job.posted = True
            log.info("research job %s -> %s (unchanged fingerprint %s; skip Discord re-post)",
                     job.id, rep["status"], fp)
        else:
            job.posted = False
        _set(job, "report", "done" if rep["status"] == "FINALIZED" else "failed",
             rep["verdict_text"] + ("" if rep["status"] == "FINALIZED" else f" — {rep['not_finalizable_reason'][:120]}"))
        log.info("research job %s -> %s: %s", job.id, rep["status"], rep["verdict_text"])
        return res.id


def _evd(e):
    return {"signal": e.signal, "account": e.account, "verified": e.verified, "source_url": e.source_url}


def social_signal_for(s, proj, evs, hype) -> dict:
    """Smart Account Signal from DATA ALREADY STORED (cached Sorsa pack + tracked accounts). No network here."""
    from . import accounts as acc_mod, sorsa as sorsa_mod
    pack = sorsa_mod.pack_dict(sorsa_mod.latest_pack(s, proj.x_username, proj.id))
    m = (hype or {}).get("metrics") or {}
    return acc_mod.smart_signal(
        pack=pack, tracked=acc_mod.tracked_map(s), independent=acc_mod.independent_observations(s, proj.id, evs),
        hype_score=(hype or {}).get("hype_score"),
        independent_flags={"promo_or_low_effort_ratio": m.get("promo_or_low_effort_ratio") if (m.get("mentions_sample") or 0) >= 25 else None,
                           "respected_engagements": sum(1 for e in evs if e.get("signal") == "respected_account_engaged" and e.get("verified")),
                           "unavailable": seed_follow_unavailable(s)})


def seed_follow_unavailable(s) -> list:
    from .seed_position import last_follow_check_status
    st = last_follow_check_status(s)
    return [f"seed_follow_check: {st}"] if st else []


def collect_sorsa(db: Database, job_id: int, http=None) -> dict | None:
    """Fetch the (cached) Sorsa pack for the job's project when SORSA_API_KEY is set and auto-fetch is on.
    Uses at most 3 live calls; respects the quota soft-stop. Failures are recorded as unavailable, never scored."""
    from ..config import sorsa_settings
    from . import sorsa as sorsa_mod, accounts as acc_mod
    st = sorsa_settings()
    with db.Session() as s:
        job = s.get(ResearchJob, job_id)
        proj = s.get(Project, job.project_id)
        if (job.hints or {}).get("sorsa_done"):
            return None
        handle, pid = proj.x_username, proj.id
    if not st["sorsa_api_key"] or not st["sorsa_auto_fetch"]:
        pack = None
        reason = "SORSA_API_KEY not configured" if not st["sorsa_api_key"] else "SORSA_AUTO_FETCH=false"
    else:
        client = sorsa_mod.SorsaClient(db, st["sorsa_api_key"], st["sorsa_cache_ttl_h"], st["sorsa_min_remaining"], http=http,
                                       quota_total=st.get("sorsa_quota_total"))
        pack = sorsa_mod.fetch_project_pack(db, client, handle, pid, optional_min_remaining=st["sorsa_optional_min_remaining"])
        acc_mod.record_sorsa_links(db, pid, pack)
        with db.Session.begin() as s:
            for a in pack["unavailable"]:   # permanent outcomes only; temporary ones stay in sorsa_api_log (no retry loop)
                if a.get("status") in (401, 403, 404) or "quota" in a["reason"]:
                    att.record(s, job_id, "sorsa_social_graph", "Sorsa " + a["endpoint"], f"sorsa:{a['endpoint']}:{handle}",
                               "not_found" if a.get("status") == 404 else "blocked", a.get("status"), a["reason"])
        reason = None
    with db.Session.begin() as s:
        job = s.get(ResearchJob, job_id)
        h = dict(job.hints or {}); h["sorsa_done"] = True; job.hints = h
        if pack is None:
            _set(job, "social", "skipped", f"Sorsa not fetched: {reason} (not a penalty)")
        else:
            _set(job, "social", "running", f"Sorsa: {pack['api_calls_used']} API call(s), {pack['cache_hits']} cache hit(s), "
                                           f"remaining {pack['remaining_quota'] if pack['remaining_quota'] is not None else 'unknown'}")
    return pack


def rescore_job(db: Database, job_id: int) -> int:
    """Re-run finalization with existing evidence under the current algorithm (new result row; old kept)."""
    return finalize(db, job_id, rescore=True)


async def advance_all(db: Database, http_client=None) -> list[int]:
    """Called by the bot poller. Returns ids of jobs whose progress changed."""
    with db.Session() as s:
        ids = [j.id for j in s.scalars(select(ResearchJob).where(ResearchJob.status.in_(ACTIVE))).all()]
    changed = []
    for jid in ids:
        try:
            with db.Session() as s:
                j = s.get(ResearchJob, jid)
                need_bot = j.feeder_submitted and not (j.hints or {}).get("bot_sources_done")
                if j.status == "RETRYING":
                    need_bot = True
            if need_bot:
                await collect_bot_sources(db, jid, http_client)
                try:   # optional verification source; never blocks or fails the job
                    import asyncio
                    await asyncio.to_thread(collect_sorsa, db, jid)
                except Exception as e:
                    log.warning("Sorsa collection for job %s failed: %s", jid, e)
            finalize(db, jid)
            changed.append(jid)
        except Exception as e:
            log.exception("advancing research job %s failed", jid)
            with db.Session.begin() as s:
                j = s.get(ResearchJob, jid)
                j.unavailable_sources = list(j.unavailable_sources or []) + [{"source": "internal", "reason": f"{type(e).__name__}: {e}"[:200]}]
    return changed


def add_review(db: Database, project_id: int, reviewer_id: str, action: str, tier: int | None, notes: str | None) -> int:
    if action not in ("confirm", "change_tier", "reject", "note"):
        raise ValueError("action must be confirm|change_tier|reject|note")
    if action == "change_tier" and tier not in (1, 2, 3, 4):
        raise ValueError("change_tier needs tier 1-4")
    with db.Session.begin() as s:
        res = s.scalar(select(ResearchResult).where(ResearchResult.project_id == project_id)
                       .order_by(ResearchResult.created_at.desc(), ResearchResult.id.desc()).limit(1))
        if not res:
            raise ValueError("no research result to review yet")
        r = ManualReview(result_id=res.id, project_id=project_id, reviewer_id=str(reviewer_id), action=action,
                         bot_tier=res.tier, bot_verdict=res.verdict,
                         reviewer_tier=tier if action == "change_tier" else (res.tier if action == "confirm" else None),
                         notes=notes)
        s.add(r); s.flush()
        return r.id


def calibration_stats(db: Database) -> dict:
    """Agreement between bot verdicts and manual reviews (calibration signal, not ground truth)."""
    with db.Session() as s:
        rows = s.scalars(select(ManualReview).where(ManualReview.action.in_(("confirm", "change_tier", "reject")))).all()
    n = len(rows)
    agree = sum(1 for r in rows if r.action == "confirm" or (r.action == "change_tier" and r.reviewer_tier == r.bot_tier))
    within1 = sum(1 for r in rows if r.bot_tier and r.reviewer_tier and abs(r.bot_tier - r.reviewer_tier) <= 1)
    bias = [r.reviewer_tier - r.bot_tier for r in rows if r.bot_tier and r.reviewer_tier]
    return {"reviews": n, "exact_agreement": (agree / n) if n else None, "within_one_tier": (within1 / n) if n else None,
            "rejected": sum(1 for r in rows if r.action == "reject"),
            "mean_tier_offset": (sum(bias) / len(bias)) if bias else None}


def latest_result(s, project_id, finalized_only=False):
    """THE single research result every Discord view uses for a project (newest row)."""
    q = select(ResearchResult).where(ResearchResult.project_id == project_id)
    if finalized_only:
        q = q.where(ResearchResult.finalization_status == "FINALIZED")
    return s.scalar(q.order_by(ResearchResult.created_at.desc(), ResearchResult.id.desc()).limit(1))


def view_line(r) -> str:
    """One-line summary of a research result, consistent with the report headline."""
    from .embeds import headline, conf_text
    rep = r.report
    head, _ = headline(rep)
    st = rep.get("status") or r.finalization_status
    return f"{head.replace('🏆 ', '')} · {st} · confidence {conf_text(rep)}"
