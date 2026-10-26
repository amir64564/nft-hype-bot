#!/usr/bin/env python3
"""Feeder interface for research jobs (used by the 10-minute routine).

  python scripts/research_feed.py list-jobs                 # open jobs + what each still needs (JSON)
  python scripts/research_feed.py show <job_id>             # full job state
  python scripts/research_feed.py submit <job_id> <file>    # research evidence (docs/research_payload_schema.json)
  python scripts/research_feed.py unavailable <job_id> <source> "<reason>"
  python scripts/research_feed.py finalize <job_id> [--force]
  python scripts/research_feed.py advance                   # run keyless market/image collection + finalize ready jobs
  python scripts/research_feed.py audit                     # reliability audit over stored results
X data for a job is still delivered with: python scripts/ingest_x.py ingest <x_request_id> <file>
"""
import argparse, asyncio, json, os, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{ROOT / 'hypebot.db'}")
from sqlalchemy import select
from hypebot.config import load_settings
from hypebot.db import Database, Project, XRequest
from hypebot.research.models import ResearchJob, ResearchResult
from hypebot.research import jobs
from hypebot.research.evidence import ResearchPayloadError
from hypebot.research.audit import run_audit


def job_json(s, j):
    p = s.get(Project, j.project_id)
    xr = s.get(XRequest, j.x_request_id) if j.x_request_id else None
    needs = []
    if xr and xr.status == "pending":
        needs.append(f"X data: python scripts/ingest_x.py ingest {xr.id} <file>")
    if not j.feeder_submitted:
        needs.append(f"research evidence: python scripts/research_feed.py submit {j.id} <file>")
    return {"job_id": j.id, "project": p.name, "x_handle": p.x_display_handle, "status": j.status,
            "x_request_id": j.x_request_id, "x_request_status": xr.status if xr else None,
            "feeder_submitted": j.feeder_submitted, "hints": {k: v for k, v in (j.hints or {}).items() if k != "image_hashes"},
            "deadline_at": j.deadline_at.isoformat() if j.deadline_at else None, "needs": needs,
            "steps": j.steps, "unavailable_sources": j.unavailable_sources, "result_id": j.result_id}


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-jobs")
    x = sub.add_parser("show"); x.add_argument("job_id", type=int)
    x = sub.add_parser("submit"); x.add_argument("job_id", type=int); x.add_argument("file")
    x = sub.add_parser("unavailable"); x.add_argument("job_id", type=int); x.add_argument("source"); x.add_argument("reason")
    x = sub.add_parser("finalize"); x.add_argument("job_id", type=int); x.add_argument("--force", action="store_true")
    x = sub.add_parser("rescore"); x.add_argument("job_ids", type=int, nargs="+")
    x = sub.add_parser("attempts"); x.add_argument("job_id", type=int)
    sub.add_parser("advance"); sub.add_parser("audit")
    a = ap.parse_args(argv)
    db = Database(load_settings().database_url)
    try:
        if a.cmd == "list-jobs":
            with db.Session() as s:
                rows = s.scalars(select(ResearchJob).where(ResearchJob.status.in_(jobs.ACTIVE + ("INCOMPLETE",)))
                                 .order_by(ResearchJob.id)).all()
                out = []
                for j in rows:
                    d = job_json(s, j)
                    if j.status == "INCOMPLETE" and j.result_id:
                        rep = s.get(ResearchResult, j.result_id).report or {}
                        ck = rep.get("checklist") or {}
                        d["needs"] = ["supplementary research for missing checks"]
                        d["missing_checks"] = {k: f"{c['status']}: {c['reason']}" for k, c in ck.items()
                                               if c["status"] in ("FAILED", "PARTIAL") and (c.get("mandatory") or k == "artwork")}
                    out.append(d)
                print(json.dumps(out, indent=1, default=str))
        elif a.cmd == "show":
            with db.Session() as s:
                j = s.get(ResearchJob, a.job_id)
                out = job_json(s, j)
                if j.result_id:
                    out["result"] = s.get(ResearchResult, j.result_id).report
                print(json.dumps(out, indent=1, default=str, ensure_ascii=False))
        elif a.cmd == "submit":
            print(json.dumps(jobs.submit_feeder(db, a.job_id, json.load(open(a.file)))))
        elif a.cmd == "unavailable":
            jobs.mark_unavailable(db, a.job_id, a.source, a.reason); print(json.dumps({"ok": True}))
        elif a.cmd == "finalize":
            print(json.dumps({"result_id": jobs.finalize(db, a.job_id, force=a.force)}))
        elif a.cmd == "rescore":
            out = []
            for jid in a.job_ids:
                rid = jobs.rescore_job(db, jid)
                with db.Session() as s:
                    r = s.get(ResearchResult, rid).report
                out.append({"job_id": jid, "result_id": rid, "status": r["status"], "verdict": r["verdict_text"],
                            "suggested_tier": r["suggested_tier"], "hype": r["hype_score"], "quality": r["quality_score_internal"],
                            "confidence": r["confidence"], "label": r["confidence_label"], "missing_mandatory": r["missing_mandatory"],
                            "change": r.get("change")})
            print(json.dumps(out, indent=1, default=str, ensure_ascii=False))
        elif a.cmd == "attempts":
            from hypebot.research import attempts as att
            with db.Session() as s:
                print(json.dumps(att.summary(s, a.job_id), indent=1, default=str))
        elif a.cmd == "advance":
            print(json.dumps({"advanced": asyncio.run(jobs.advance_all(db))}))
        elif a.cmd == "audit":
            print(json.dumps(run_audit(db), indent=1, default=str, ensure_ascii=False))
    except (ResearchPayloadError, ValueError) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
