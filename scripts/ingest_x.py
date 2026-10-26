#!/usr/bin/env python3
"""Manage externally collected X data requests.

  python scripts/ingest_x.py list-pending
  python scripts/ingest_x.py ingest <request_id> <json_file>   (schema: docs/x_ingest_schema.json)
  python scripts/ingest_x.py fail <request_id> "<reason>"
  python scripts/ingest_x.py rescore <project|@handle> <json_file>   (new snapshot, no Discord post)
  python scripts/ingest_x.py recalc [project|@handle]   (re-score history with the current algorithm into
                                                         snapshot_recalcs; original snapshots untouched)
Uses DATABASE_URL (default sqlite:///hypebot.db relative to the bot dir).
"""
import argparse, json, os, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{ROOT / 'hypebot.db'}")
from sqlalchemy import select
from hypebot.config import load_settings
from hypebot.db import Database, XRequest, utcnow
from hypebot.pipeline import Analyzer, AnalysisError
from hypebot.external_x import IngestError


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-pending")
    ig = sub.add_parser("ingest"); ig.add_argument("request_id", type=int); ig.add_argument("json_file")
    fl = sub.add_parser("fail"); fl.add_argument("request_id", type=int); fl.add_argument("reason")
    rs = sub.add_parser("rescore"); rs.add_argument("project"); rs.add_argument("json_file")
    rc = sub.add_parser("recalc"); rc.add_argument("project", nargs="?")
    a = ap.parse_args(argv)
    settings = load_settings()
    db = Database(settings.database_url)
    if a.cmd == "list-pending":
        with db.Session() as s:
            rows = s.scalars(select(XRequest).where(XRequest.status == "pending").order_by(XRequest.id)).all()
            print(json.dumps([{"id": r.id, "project_id": r.project_id, "project_name": r.project_name,
                               "x_username": r.x_username, "channel_id": r.channel_id, "guild_id": r.guild_id,
                               "requested_by": r.requested_by, "interaction_id": r.interaction_id,
                               "provisional_snapshot_id": r.provisional_snapshot_id, "status": r.status,
                               "created_at": r.created_at.isoformat()} for r in rows], indent=2))
        return 0
    if a.cmd == "fail":
        with db.Session.begin() as s:
            r = s.get(XRequest, a.request_id)
            if not r:
                print("not found", file=sys.stderr); return 1
            r.status, r.error, r.completed_at = "failed", a.reason, utcnow()
        print(json.dumps({"id": a.request_id, "status": "failed"})); return 0
    if a.cmd == "recalc":
        try:
            rows = Analyzer(settings, db).recalculate_history(a.project)
        except AnalysisError as e:
            print(json.dumps({"error": str(e)}), file=sys.stderr); return 2
        print(json.dumps([{"recalc_id": r.id, "source_snapshot_id": r.source_snapshot_id, "project_id": r.project_id,
                           "original": [r.original_hype_score, r.original_tier, r.original_algorithm_version],
                           "new": [r.hype_score, r.tier, r.confidence_label, r.confidence, r.algorithm_version],
                           "provisional": r.provisional, "coverage": (r.evaluation or {}).get("coverage"),
                           "notes": r.notes} for r in rows], indent=1))
        return 0
    with open(a.json_file) as f:
        payload = json.load(f)
    try:
        an = Analyzer(settings, db)
        out = an.rescore_from_file(a.project, payload) if a.cmd == "rescore" else an.ingest_external(a.request_id, payload)
    except (IngestError, AnalysisError) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr); return 2
    sn = out.snapshot
    print(json.dumps({"id": getattr(a, "request_id", None), "status": "done", "snapshot_id": sn.id, "hype_score": sn.hype_score,
                      "tier": sn.tier, "confidence": sn.confidence, "confidence_label": sn.confidence_label,
                      "algorithm_version": sn.algorithm_version, "evaluation": sn.evaluation, "provisional": sn.provisional,
                      "trend": sn.trend, "score_delta": sn.score_delta, "missing": sn.missing,
                      "components": {k: [c["points"], c["max"]] for k, c in sn.components.items()},
                      "flags": sn.flags}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
