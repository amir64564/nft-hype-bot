"""Reliability audit over stored snapshots and research results (read-only)."""
from __future__ import annotations
from sqlalchemy import select
from ..db import Database, Project, Snapshot
from .models import ResearchResult, Evidence


def run_audit(db: Database) -> dict:
    issues = []
    with db.Session() as s:
        for p in s.scalars(select(Project)).all():
            snaps = [x for x in db.history(s, p.id, 1000) if x.algorithm_version != "v1-legacy"]
            for a, b in zip(snaps, snaps[1:]):
                same_data = ((a.raw_data or {}).get("x") or {}).get("collected_at") == ((b.raw_data or {}).get("x") or {}).get("collected_at")
                if same_data and (a.hype_score != b.hype_score):
                    issues.append({"project": p.name, "check": "score changed without new evidence", "snapshots": [a.id, b.id]})
                if a.tier and b.tier and abs(a.tier - b.tier) >= 2:
                    issues.append({"project": p.name, "check": "drastic tier change", "snapshots": [a.id, b.id]})
            for x in snaps:
                m = x.metrics or {}
                if x.confidence_label == "High" and (m.get("sample_tweets") or 0) < 8:
                    issues.append({"project": p.name, "check": "High confidence on few posts", "snapshot": x.id})
                if x.components:
                    for k, c in x.components.items():
                        if c.get("status") == "missing" and c.get("points") not in (None,):
                            issues.append({"project": p.name, "check": "missing scored as number", "snapshot": x.id, "component": k})
                        if c.get("points") is not None and c["points"] > c["max"] + 1e-9:
                            issues.append({"project": p.name, "check": "category above max", "snapshot": x.id, "component": k})
                infl = (x.components or {}).get("influential_engagement", {})
                if x.tier in (1, 2) and (m.get("influential_relevant") or 0) <= 1 and (infl.get("points") or 0) >= 0.8 * (infl.get("max") or 10):
                    issues.append({"project": p.name, "check": "one influential account drives high tier", "snapshot": x.id})
                if x.tier in (1, 2) and (m.get("wallet_address_ratio") or 0) > 0.5:
                    issues.append({"project": p.name, "check": "possible giveaway/allowlist farming counted as demand", "snapshot": x.id})
        for r in s.scalars(select(ResearchResult)).all():
            rep = r.report or {}
            for sec in ("supporting_evidence", "positives", "risks"):
                for c in rep.get(sec) or []:
                    if not c.get("url") and c.get("kind") != "indicator":
                        issues.append({"result": r.id, "check": f"claim without link in {sec}", "claim": c.get("claim")})
            if r.verdict == "Tier" and r.confidence_label == "Low":
                issues.append({"result": r.id, "check": "tier given at Low confidence"})
            if "checklist" in rep:   # r2+ invariants
                from ..scoring import confidence_label
                if not 0 <= (r.confidence or 0) <= 1 or confidence_label(r.confidence) != r.confidence_label:
                    issues.append({"result": r.id, "check": "confidence number/label mismatch"})
                if rep["status"] == "FINALIZED" and rep.get("missing_mandatory"):
                    issues.append({"result": r.id, "check": "finalized with missing mandatory checks"})
                if rep["status"] != "FINALIZED" and r.tier:
                    issues.append({"result": r.id, "check": "final tier on non-finalized result"})
        n_unsourced = len(s.scalars(select(Evidence).where(Evidence.source_url.is_(None))).all())
    return {"issues": issues, "unsourced_evidence_rows": n_unsourced, "ok": not issues}
