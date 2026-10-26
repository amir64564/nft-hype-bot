"""Analysis pipeline: identify -> collect -> derive -> score -> persist -> compare."""
from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .collectors.x_api import XClient, XError, XCredentialsMissing, collect_x, normalize_handle
from .config import Settings
from .db import Database, Snapshot, SnapshotRecalc, Project, XRequest, utcnow
from .external_x import to_x_data, IngestError, SOURCE as EXT_SOURCE
from .metrics import derive_metrics, _dt
from .scoring import score, trend, ALGORITHM_VERSION
from sqlalchemy import select

BASELINE_MIN_DAYS = 0.5


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def identity_verified(project_name: str, user: dict | None) -> bool | None:
    """True if the project name appears in the account's name/handle/bio; None = unverified."""
    if not user:
        return None
    nm = "".join(ch for ch in (project_name or "").lower() if ch.isalnum())
    hay = "".join(ch for ch in (user.get("name", "") + user.get("username", "") + (user.get("description") or "")).lower() if ch.isalnum())
    return True if nm and nm in hay else None


def find_baseline(snaps: list, x_data: dict, username: str | None):
    """Latest earlier observation of the SAME account collected >= 12h before this one.
    Returns (metrics, days, snapshot_id) or (None, None, None). Never uses re-ingests of
    the same collection (identical collected_at) or snapshots without X account data."""
    ca = x_data.get("collected_at")
    if not ca:
        return None, None, None
    cur = _dt(ca)
    best = None
    for sn in snaps:
        x = (sn.raw_data or {}).get("x") or {}
        u = x.get("user") or {}
        if not x.get("collected_at") or (u.get("username") or "").lower() != (username or "").lower():
            continue
        if ((u.get("public_metrics") or {}).get("followers_count")) is None:
            continue
        d = (cur - _dt(x["collected_at"])).total_seconds() / 86400
        if d >= BASELINE_MIN_DAYS and (best is None or d < best[1]):
            best = ({"followers": u["public_metrics"]["followers_count"]}, d, sn.id)
    return best or (None, None, None)


def evaluate(settings: Settings, project_name: str, x_data: dict, analysis_time: datetime,
             earlier_snaps: list):
    """The ONE scoring path used by /analyze, /refresh, ingest, rescore and recalculation."""
    metrics = derive_metrics(x_data, None, settings.influencer_min_followers)
    user = x_data.get("user") or {}
    base_m, base_days, base_id = find_baseline(earlier_snaps, x_data, user.get("username"))
    age_h = None
    if x_data.get("collected_at"):
        age_h = max(0.0, (_aware(analysis_time) - _dt(x_data["collected_at"])).total_seconds() / 3600)
    ident = identity_verified(project_name, user)
    res = score(metrics, settings.weights, base_m, base_days, settings.provisional_confidence, settings.tiers,
                source=x_data.get("source") or "X API v2", identity_ok=ident, data_age_hours=age_h)
    evaluation = {"coverage": res.coverage, "ranked": res.ranked, "confidence_parts": res.confidence_parts,
                  "confidence_reasons": res.confidence_reasons, "baseline_snapshot_id": base_id,
                  "baseline_days": round(base_days, 3) if base_days else None,
                  "data_age_hours": round(age_h, 2) if age_h is not None else None,
                  "identity_verified": ident, "algorithm_version": ALGORITHM_VERSION}
    return metrics, res, evaluation

log = logging.getLogger("hypebot.pipeline")
Progress = Callable[[str], Awaitable[None]]


def clean_project_name(name: str, handle: str) -> str:
    """Turn a URL/@handle typed as the project name into a readable name."""
    import re
    n = (name or "").strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?(?:x|twitter)\.com/@?([A-Za-z0-9_]{1,15})", n, re.I)
    if m or n.startswith("@") or not n:
        n = (m.group(1) if m else n.lstrip("@")) or handle
        n = re.sub(r"_(rh|nft|nfts|xyz|io|official|hq)$", "", n, flags=re.I).replace("_", " ").strip() or handle
        return n[:1].upper() + n[1:]
    m = re.match(r"^(?:https?://)?(?:www\.)?([A-Za-z0-9-]+)\.[a-z.]{2,}(?:/.*)?$", n, re.I)
    if m and ("://" in n or "/" in n or n.count(".") >= 1 and " " not in n):
        return m.group(1).replace("-", " ").title()
    return n


class AnalysisError(Exception):
    pass


@dataclass
class AnalysisOutcome:
    project: Project
    snapshot: Snapshot
    previous: Snapshot | None
    warnings: list
    x_request_id: int | None = None


async def _noop(_: str):
    return None


class Analyzer:
    def __init__(self, settings: Settings, db: Database, x_client: XClient | None = None,
                 x_collector=collect_x):
        self.s, self.db = settings, db
        self.x = x_client or XClient(settings.x_bearer_token)
        self.collect_x = x_collector

    async def analyze(self, project_name: str, x_username: str, *, progress: Progress = _noop,
                      context: dict | None = None) -> AnalysisOutcome:
        handle = normalize_handle(x_username)
        project_name = clean_project_name(project_name, handle)
        warnings, sources, x_data = [], [], None
        await progress(f"🔎 Identifying **@{handle}** on X…")
        if not self.x.available:
            warnings.append("X_BEARER_TOKEN is not configured — X metrics (engagement, discussion, "
                            "influencers, cadence) were NOT collected. Set X_BEARER_TOKEN for a full analysis.")
        else:
            try:
                x_data = await self.collect_x(self.x, handle, project_name)
                sources.append({"source": "X API v2", "endpoints": ["users/by/username", "users/:id/tweets",
                                "tweets/search/recent", "tweets/counts/recent"],
                                "collected_at": x_data["collected_at"]})
                warnings += [f"X partial: {e}" for e in x_data.get("errors", [])]
            except XCredentialsMissing as e:
                warnings.append(str(e))
            except XError as e:
                if "not found" in str(e):
                    raise AnalysisError(str(e))
                warnings.append(f"X API: {e}")
        user = (x_data or {}).get("user")
        identity = None
        if user:
            identity = {"x_id": user["id"], "name": user.get("name"), "username": user.get("username"),
                        "verified": user.get("verified"), "verified_type": user.get("verified_type"),
                        "description": user.get("description"), "created_at": user.get("created_at")}
            nm = project_name.lower().replace(" ", "")
            if nm not in (user.get("name", "") + user.get("username", "") + (user.get("description") or "")).lower().replace(" ", ""):
                warnings.append(f"Identity check: project name '{project_name}' does not appear in @{handle}'s "
                                "name/bio — verify this is the official account.")
            handle = user.get("username", handle)
        pub: dict = {}  # website/Discord/CoinGecko are no longer part of the score
        if x_data is None:
            if self.x.available:
                raise AnalysisError("X data could not be collected: " + " ".join(warnings))
            # No X token: queue a request for externally collected real X data; no score yet.
            with self.db.Session.begin() as s:
                proj = self.db.upsert_project(s, project_name, handle)
                ctx = context or {}
                r = XRequest(project_id=proj.id, x_username=handle, project_name=project_name,
                             channel_id=ctx.get("channel_id"), guild_id=ctx.get("guild_id"),
                             requested_by=ctx.get("user_id"), interaction_id=ctx.get("interaction_id"),
                             provisional_snapshot_id=None, status="pending")
                s.add(r); s.flush(); req_id = r.id
                prev = self.db.latest_snapshot(s, proj.id)
            warnings.append(f"X data request #{req_id} queued — the full report will be posted here once "
                            "real X data is ingested.")
            await progress("✅ Queued.")
            return AnalysisOutcome(proj, None, prev, warnings, req_id)
        await progress("🧮 Computing metrics and Hype Score…")
        snap, proj, prev = self._persist(project_name, handle, user, identity, x_data, pub, sources, warnings)
        req_id = None
        await progress("✅ Done.")
        return AnalysisOutcome(proj, snap, prev, warnings, req_id)

    def _persist(self, project_name, handle, user, identity, x_data, pub, sources, warnings):
        with self.db.Session.begin() as s:
            proj = self.db.upsert_project(s, project_name, handle,
                                          x_user_id=(user or {}).get("id") if not str((user or {}).get("id", "")).startswith("handle:") else None,
                                          identity=identity)
            earlier = list(self.db.history(s, proj.id, limit=1000))
            now = utcnow()   # patchable clock (tests pin it for fixed historical datasets)
            metrics, res, ev = evaluate(self.s, project_name, x_data, now, earlier)
            if res.hype_score is None:
                raise AnalysisError("Insufficient data: no scoring component could be computed. " +
                                    " ".join(warnings))
            if (ev.get("data_age_hours") or 0) > 72:
                warnings.append(f"⚠️ Stale X data: collected {ev['data_age_hours']:.0f}h before this analysis.")
            if any(sn.raw_data and (sn.raw_data.get("x") or {}).get("collected_at") == x_data.get("collected_at")
                   for sn in earlier):
                warnings.append("Re-used X data: this exact collection was already scored before (no new observation).")
            prev = self.db.latest_snapshot(s, proj.id)
            prev_same = self.db.latest_snapshot(s, proj.id, ALGORITHM_VERSION)
            tr, delta = trend(res.hype_score, prev_same.hype_score if prev_same else None)
            snap = Snapshot(project_id=proj.id, raw_data={"x": x_data, "public": pub}, metrics=metrics,
                            sources=sources, components=res.components, missing=res.missing,
                            flags=res.flags, weights=res.weights, hype_score=res.hype_score, tier=res.tier,
                            confidence=res.confidence, confidence_label=res.confidence_label,
                            provisional=res.provisional, trend=tr, score_delta=delta,
                            previous_tier=prev_same.tier if prev_same else None,
                            notes="\n".join(warnings) or None,
                            algorithm_version=ALGORITHM_VERSION, evaluation=ev, created_at=now)
            s.add(snap)
            s.flush()
        return snap, proj, prev

    def recalculate_history(self, key: str | None = None) -> list:
        """Re-score stored snapshots with the current algorithm into snapshot_recalcs.
        Originals are untouched; each recalc is evaluated as of its original snapshot time and
        may only use observations that existed before it. Idempotent per (snapshot, version)."""
        out = []
        with self.db.Session.begin() as s:
            projs = [self.db.get_project(s, key)] if key else s.scalars(select(Project).order_by(Project.id)).all()
            for p in projs:
                if p is None:
                    raise AnalysisError(f"No saved project matches '{key}'")
                snaps = self.db.history(s, p.id, limit=10_000)
                for i, sn in enumerate(snaps):
                    if sn.algorithm_version == ALGORITHM_VERSION:
                        continue
                    done = s.scalar(select(SnapshotRecalc).where(SnapshotRecalc.source_snapshot_id == sn.id,
                                                                 SnapshotRecalc.algorithm_version == ALGORITHM_VERSION))
                    if done:
                        out.append(done); continue
                    x = (sn.raw_data or {}).get("x")
                    base = dict(source_snapshot_id=sn.id, project_id=p.id, algorithm_version=ALGORITHM_VERSION,
                                created_at=sn.created_at, original_hype_score=sn.hype_score,
                                original_tier=sn.tier, original_algorithm_version=sn.algorithm_version,
                                sources=sn.sources, weights=dict(self.s.weights))
                    if not x or not x.get("user"):
                        rc = SnapshotRecalc(**base, metrics={}, components={}, missing=[], flags=[],
                                            hype_score=None, tier=None, confidence=0.0, confidence_label="Low",
                                            provisional=True, evaluation={"ranked": False},
                                            notes="No X data in original snapshot — not scoreable (legacy non-X score).")
                    else:
                        metrics, res, ev = evaluate(self.s, p.name, x, sn.created_at, snaps[:i])
                        rc = SnapshotRecalc(**base, metrics=metrics, components=res.components, missing=res.missing,
                                            flags=res.flags, hype_score=res.hype_score, tier=res.tier,
                                            confidence=res.confidence, confidence_label=res.confidence_label,
                                            provisional=res.provisional, evaluation=ev)
                    s.add(rc); s.flush(); out.append(rc)
        return out

    def ingest_external(self, request_id: int, payload: dict) -> AnalysisOutcome:
        """Store externally collected real X data as a NEW snapshot and mark the request done."""
        with self.db.Session() as s:
            r = s.get(XRequest, request_id)
            if not r:
                raise AnalysisError(f"x_request #{request_id} not found")
            if r.status != "pending":
                raise AnalysisError(f"x_request #{request_id} is already {r.status}")
            proj = s.get(Project, r.project_id)
            last = self.db.latest_snapshot(s, proj.id)
            pub, pub_sources = {}, []
            name, handle = proj.name, proj.x_display_handle
        try:
            x_data = to_x_data(payload, handle)
        except IngestError as e:
            with self.db.Session.begin() as s:
                r = s.get(XRequest, request_id); r.status = "failed"; r.error = str(e); r.completed_at = utcnow()
            raise
        user = x_data["user"]
        identity = {"x_id": None, "name": user.get("name"), "username": user.get("username"),
                    "verified": user.get("verified"), "verified_type": user.get("verified_type"),
                    "description": user.get("description"), "created_at": user.get("created_at")}
        warnings = [f"X data supplied externally (source: {EXT_SOURCE}, collected {payload['collected_at']})."]
        if "mentions" not in x_data:
            warnings.append("No mentions supplied — community/influencer components treated as missing.")
        sources = [{"source": EXT_SOURCE, "collected_at": payload["collected_at"],
                    "request_id": request_id}] + pub_sources
        snap, proj, prev = self._persist(name, handle, user, identity, x_data, pub, sources, warnings)
        with self.db.Session.begin() as s:
            r = s.get(XRequest, request_id)
            r.status = "done"; r.result_snapshot_id = snap.id; r.completed_at = utcnow()
        return AnalysisOutcome(proj, snap, prev, warnings)

    async def refresh(self, key: str, progress: Progress = _noop, context: dict | None = None) -> AnalysisOutcome:
        with self.db.Session() as s:
            p = self.db.get_project(s, key)
            if not p:
                raise AnalysisError(f"No saved project matches '{key}'. Use /analyze first.")
            name, handle = p.name, p.x_display_handle
        return await self.analyze(name, handle, progress=progress, context=context)

    def rescore_from_file(self, key: str, payload: dict) -> AnalysisOutcome:
        """Re-score saved real external X data as a NEW snapshot (no Discord follow-up)."""
        with self.db.Session.begin() as s:
            p = self.db.get_project(s, key)
            if not p:
                raise AnalysisError(f"No saved project matches '{key}'")
            r = XRequest(project_id=p.id, x_username=p.x_display_handle, project_name=p.name,
                         status="pending", posted=True)  # posted=True: never announce
            s.add(r); s.flush(); rid = r.id
        return self.ingest_external(rid, payload)
