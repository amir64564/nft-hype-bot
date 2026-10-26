#!/usr/bin/env python3
"""Sorsa quota.

  python scripts/sorsa_quota.py            # last KNOWN remaining quota from the local log (0 API calls)
  python scripts/sorsa_quota.py --live     # GET /key-usage-info (refreshes the local record)
  python scripts/sorsa_quota.py --log 20   # last N Sorsa request decisions (live/cached/skipped)
"""
import argparse, json, os, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{ROOT / 'hypebot.db'}")
from sqlalchemy import select, func
from hypebot.db import Database
from hypebot.research import sorsa
from hypebot.research.models import SorsaApiLog

ap = argparse.ArgumentParser()
ap.add_argument("--live", action="store_true")
ap.add_argument("--log", type=int, default=0)
a = ap.parse_args()
db = Database(os.environ["DATABASE_URL"])
c = sorsa.client_from_env(db)
out = {"key_configured": bool(c.api_key), "last_known_remaining": c.remaining()}
with db.Session() as s:
    out["live_calls_logged"] = s.scalar(select(func.count()).select_from(SorsaApiLog).where(SorsaApiLog.cached.is_(False),
                                         SorsaApiLog.status.notin_(("no_key", "skipped_quota"))))
    out["cache_hits_logged"] = s.scalar(select(func.count()).select_from(SorsaApiLog).where(SorsaApiLog.cached.is_(True)))
    if a.log:
        out["log"] = [{"ts": r.ts.isoformat(), "endpoint": r.endpoint, "username": r.username, "status": r.status,
                       "cached": r.cached, "remaining_after": r.remaining_after}
                      for r in s.scalars(select(SorsaApiLog).order_by(SorsaApiLog.id.desc()).limit(a.log)).all()]
if a.live:
    try:
        out["key_usage_info"] = c.key_usage()
        out["last_known_remaining"] = c.remaining()
    except sorsa.SorsaUnavailable as e:
        out["error"] = str(e)
print(json.dumps(out, indent=2, default=str))
