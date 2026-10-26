#!/usr/bin/env python3
"""Fetch + cache the Sorsa pack for ONE project handle (cache-first; ≤3 live calls by default).

  python scripts/sorsa_fetch.py <handle> [--force] [--dry-run]

--dry-run   show what would be called (cache vs live) without calling
--force     ignore the quota soft-stop and also fetch score-changes/new-followers-7d
The pack is stored in sorsa_project_snapshots (append-only) and tracked accounts seen in it are linked."""
import argparse, json, os, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{ROOT / 'hypebot.db'}")
from hypebot.config import sorsa_settings
from hypebot.db import Database
from hypebot.research import sorsa, accounts

ap = argparse.ArgumentParser()
ap.add_argument("handle")
ap.add_argument("--force", action="store_true")
ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()
db = Database(os.environ["DATABASE_URL"])
st = sorsa_settings()
c = sorsa.client_from_env(db)
h = sorsa.norm(a.handle)
if a.dry_run:
    rem = c.remaining()
    plan = {ep: ("cache" if c.cached(ep, h) else "live" if c.api_key else "unavailable (no key)") for ep in sorsa.PACK_CORE}
    for ep in sorsa.PACK_OPTIONAL:
        plan[ep] = "cache" if c.cached(ep, h) else ("live" if (a.force or (rem is not None and rem > st["sorsa_optional_min_remaining"])) and c.api_key else "skip (quota policy)")
    print(json.dumps({"handle": h, "remaining": rem, "plan": plan}, indent=2)); sys.exit(0)
with db.Session() as s:
    p = db.get_project(s, h)
    pid = p.id if p else None
pack = sorsa.fetch_project_pack(db, c, h, pid, force=a.force, optional_min_remaining=st["sorsa_optional_min_remaining"])
if pid:
    accounts.record_sorsa_links(db, pid, pack)
pack.pop("raw", None)
print(json.dumps(pack, indent=2, default=str))
