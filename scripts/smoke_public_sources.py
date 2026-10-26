"""Live smoke test of keyless public sources (real network)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import asyncio, json, sys
from hypebot.collectors.public import collect_public
async def main():
    r = await collect_public(sys.argv[1], sys.argv[2], None, website=sys.argv[3], discord_code=sys.argv[4])
    print(json.dumps({k: v for k, v in r.items()}, indent=1, default=str))
asyncio.run(main())
