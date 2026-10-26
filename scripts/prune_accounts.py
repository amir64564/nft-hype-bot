#!/usr/bin/env python3
"""Mark cook-position keepers, then prune worthless non-seed tracked accounts (seeds are only soft-flagged).

  python scripts/prune_accounts.py [--dry-run]"""
import argparse, json, os, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{ROOT / 'hypebot.db'}")
from hypebot.db import Database
from hypebot.research import seed_position as sp

ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true"); a = ap.parse_args()
db = Database(os.environ["DATABASE_URL"])
k = sp.mark_keepers(db)
r = sp.prune_worthless_accounts(db, dry_run=a.dry_run)
print(json.dumps({"keepers": k, **{x: (len(v) if isinstance(v, list) else v) for x, v in r.items()},
                  "pruned_handles": r["pruned"][:50], "seed_candidates": r["seed_candidates"]}, indent=2))
