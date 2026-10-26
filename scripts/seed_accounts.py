#!/usr/bin/env python3
"""Tracked accounts (seed watchlist + history).

  python scripts/seed_accounts.py import seed_accounts.json      # or a .csv with a `handle` column
  python scripts/seed_accounts.py mine [--min-projects 2]        # compute discovery stats from stored X snapshots
  python scripts/seed_accounts.py list [--all]                   # tracked accounts with reliability

Importing a handle does NOT label it smart; reliability is computed from recorded outcomes only."""
import argparse, json, os, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{ROOT / 'hypebot.db'}")
from sqlalchemy import select
from hypebot.db import Database
from hypebot.research import accounts
from hypebot.research.models import TrackedAccount

ap = argparse.ArgumentParser()
sub = ap.add_subparsers(dest="cmd", required=True)
i = sub.add_parser("import"); i.add_argument("file")
m = sub.add_parser("mine"); m.add_argument("--min-projects", type=int, default=2)
l = sub.add_parser("list"); l.add_argument("--all", action="store_true")
a = ap.parse_args()
db = Database(os.environ["DATABASE_URL"])
if a.cmd == "import":
    print(json.dumps(accounts.import_seeds(db, accounts.load_seed_file(a.file)), indent=2))
elif a.cmd == "mine":
    print(json.dumps(accounts.mine_history(db, a.min_projects), indent=2))
else:
    with db.Session() as s:
        rows = s.scalars(select(TrackedAccount).order_by(TrackedAccount.reliability_label, TrackedAccount.x_handle)).all()
        for r in rows:
            h = r.history or {}
            if not a.all and not h and "seed_list" not in (r.sources or []):
                continue
            print(json.dumps({"handle": r.x_handle, "reliability": r.reliability_label, "confidence": r.confidence,
                              "class": accounts.classify(accounts._acc_dict(r)), "followers": r.followers_count,
                              "sorsa_score": r.sorsa_score, "sources": r.sources,
                              "projects_mentioned": h.get("projects_mentioned"), "known_outcomes": h.get("known_outcomes"),
                              "hits": h.get("hits_tier_1_2"), "promo_ratio": h.get("promo_or_low_effort_ratio"),
                              "flags": {k: v for k, v in (r.flags or {}).items() if v}}))
