#!/usr/bin/env python3
"""One-off: repost final research reports (current embed format) for given job ids, via REST only."""
import asyncio, os, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{ROOT / 'hypebot.db'}")
import discord
from hypebot.config import load_settings
from hypebot.db import Database, Project
from hypebot.research.models import ResearchJob, ResearchResult
from hypebot.research.embeds import report_embed, headline


async def main(ids):
    st = load_settings(); db = Database(st.database_url)
    c = discord.Client(intents=discord.Intents.none())
    await c.login(st.discord_token)
    try:
        for jid in ids:
            with db.Session() as s:
                j = s.get(ResearchJob, jid)
                if not (j and j.status == "done" and j.posted and j.result_id and j.channel_id):
                    print(jid, "skip (no posted final report)"); continue
                res = s.get(ResearchResult, j.result_id); p = s.get(Project, j.project_id)
            ch = await c.fetch_channel(int(j.channel_id))
            m = await ch.send(content=f"🔁 Updated research report #{jid} (tier now shown at the top):", embed=report_embed(p, res))
            print(jid, p.name, "|", headline(res.report)[0], "| msg", m.id)
    finally:
        await c.close()

asyncio.run(main([int(x) for x in sys.argv[1:]]))
