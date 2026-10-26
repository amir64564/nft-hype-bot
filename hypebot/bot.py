"""Discord bot: slash commands and handlers."""
from __future__ import annotations
import asyncio, logging, time
from sqlalchemy import select
import discord
from .scoring import ALGORITHM_VERSION
from discord import app_commands

from .config import Settings, load_settings
from .db import Database
from .pipeline import Analyzer, AnalysisError
from . import embeds
from .research import jobs as rjobs
from .research.models import ResearchJob, ResearchResult, ManualReview
from .research.embeds import report_embed

log = logging.getLogger("hypebot.bot")


class Handlers:
    """Command logic, separated from discord.py registration for testability."""

    def __init__(self, settings: Settings, db: Database, analyzer: Analyzer):
        self.s, self.db, self.analyzer = settings, db, analyzer
        self._locks: dict[str, asyncio.Lock] = {}

    def _progress(self, interaction):
        last = [0.0]
        async def cb(msg: str):
            if time.monotonic() - last[0] < 1.0 and not msg.startswith("✅"):
                return
            last[0] = time.monotonic()
            try:
                await interaction.edit_original_response(content=msg)
            except discord.HTTPException as e:
                log.warning("progress edit failed: %s", e)
        return cb

    @staticmethod
    async def safe_defer(interaction, ephemeral=False) -> bool:
        """Acknowledge within Discord's 3 s window. Returns False if the interaction already expired
        (10062: the gateway/host delivered it late) so callers can fall back to a channel message."""
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=True, ephemeral=ephemeral)
            return True
        except discord.NotFound as e:
            log.error("interaction %s expired before defer (%s)", getattr(interaction, "id", "?"), e)
            return False
        except discord.HTTPException as e:
            log.error("defer failed: %s", e)
            return False

    async def _say(self, interaction, ok, content=None, embed=None):
        try:
            if ok:
                return await interaction.edit_original_response(content=content, embed=embed)
            ch = interaction.channel
            if ch is not None:
                return await ch.send(content=content, embed=embed)
        except discord.HTTPException as e:
            log.warning("reply failed: %s", e)

    async def _run(self, interaction, key, coro_fn):
        await interaction.response.defer(thinking=True)
        lock = self._locks.setdefault(key.lower(), asyncio.Lock())
        if lock.locked():
            await interaction.edit_original_response(content=f"⏳ `{key}` is already being analyzed; queued…")
        async with lock:
            try:
                out = await coro_fn(self._progress(interaction))
            except (AnalysisError, ValueError) as e:
                await interaction.edit_original_response(content=f"❌ {e}")
                return
            except Exception as e:
                log.exception("analysis failed")
                await interaction.edit_original_response(content=f"❌ Analysis failed: {type(e).__name__}: {e}")
                return
        e = (embeds.analysis_embed(out.project, out.snapshot, out.warnings) if out.snapshot
             else embeds.queued_embed(out.project, out.warnings))
        await interaction.edit_original_response(content=None, embed=e)

    @staticmethod
    def _ctx(i):
        def sid(v):
            return str(v) if isinstance(v, (int, str)) else None
        return {"channel_id": sid(getattr(i, "channel_id", None)), "guild_id": sid(getattr(i, "guild_id", None)),
                "user_id": sid(getattr(getattr(i, "user", None), "id", None)), "interaction_id": sid(getattr(i, "id", None))}

    async def analyze(self, interaction, project: str, x: str, opensea: str | None = None,
                      magiceden: str | None = None, website: str | None = None):
        ok = await self.safe_defer(interaction)
        if not ok:
            await self._say(interaction, False, content=f"⚠️ Discord delivered the command late, so it expired; "
                                                         f"starting research for **{project}** anyway.")
        hints = {"user_marketplaces": [m for m in ([{"name": "opensea", "slug": opensea}] if opensea else [])
                                       + ([{"name": "magiceden_sol", "slug": magiceden}] if magiceden else [])],
                 "user_website": website}
        await self._start_job(interaction, ok, lambda: self.analyzer.analyze(project, x, context=self._ctx(interaction)), hints)

    async def refresh(self, interaction, project: str):
        ok = await self.safe_defer(interaction)
        await self._start_job(interaction, ok, lambda: self.analyzer.refresh(project, context=self._ctx(interaction)), {})

    async def _start_job(self, interaction, ok, analyze_fn, hints):
        try:
            out = await analyze_fn()
        except (AnalysisError, ValueError) as e:
            await self._say(interaction, ok, content=f"❌ {e}"); return None
        except Exception as e:
            log.exception("analysis failed")
            await self._say(interaction, ok, content=f"❌ Analysis failed: {type(e).__name__}: {e}"); return None
        ctx = self._ctx(interaction)
        if hints.get("user_marketplaces"):
            hints["marketplaces"] = hints["user_marketplaces"]
        jid = await asyncio.to_thread(rjobs.create_job, self.db, out.project.id, out.x_request_id, ctx, hints)
        with self.db.Session() as s:
            job = s.get(ResearchJob, jid)
            text = rjobs.render_progress(job, out.project.name)
        msg = None
        try:
            ch = interaction.channel
            msg = await ch.send(text) if ch is not None else None
        except discord.HTTPException as e:
            log.warning("could not post progress message: %s", e)
        await self._say(interaction, ok, content=f"🔬 Research job #{jid} started for **{out.project.name}** — "
                                                 "progress is tracked in the message below; the final report is posted when done.")
        if msg is not None:
            with self.db.Session.begin() as s:
                j = s.get(ResearchJob, jid); j.progress_message_id = str(msg.id); j.progress_rendered = text
        return jid

    async def update_research(self, get_channel) -> int:
        """Advance jobs, edit progress messages, post or edit final reports. Returns reports delivered."""
        await rjobs.advance_all(self.db)
        from sqlalchemy import select
        from .db import Project
        with self.db.Session() as s:
            rows = s.scalars(select(ResearchJob).where(ResearchJob.posted == False)).all()  # noqa
            todo = [(j.id, j.channel_id, j.progress_message_id, j.progress_rendered,
                     rjobs.render_progress(j, s.get(Project, j.project_id).name),
                     j.status, j.result_id, s.get(Project, j.project_id), j.requested_by,
                     j.report_message_id, j.posted_fingerprint) for j in rows]
        n = 0
        for jid, ch_id, mid, old, new, status, rid, proj, uid, report_mid, last_fp in todo:
            if not ch_id:
                continue
            try:
                ch = await get_channel(int(ch_id))
                if mid and new != old:
                    await ch.get_partial_message(int(mid)).edit(content=new)
                if status in ("FINALIZED", "INCOMPLETE") and rid:
                    with self.db.Session() as s:
                        res = s.get(ResearchResult, rid)
                    fp = rjobs.report_fingerprint(res.report if res else None)
                    if last_fp and last_fp == fp:
                        # Identical to what Discord already has — mark posted, send nothing.
                        with self.db.Session.begin() as s:
                            j = s.get(ResearchJob, jid)
                            j.progress_rendered = new
                            j.posted = True
                            j.posted_fingerprint = fp
                        continue
                    emb = report_embed(proj, res)
                    if report_mid:
                        # Update in place — no new message, no user ping.
                        try:
                            await ch.get_partial_message(int(report_mid)).edit(
                                content=f"📑 Research job #{jid} updated:", embed=emb)
                            delivered_mid = report_mid
                        except (discord.NotFound, discord.Forbidden):
                            msg = await ch.send(content=f"📑 Research job #{jid} updated:", embed=emb,
                                                allowed_mentions=discord.AllowedMentions.none())
                            delivered_mid = str(msg.id)
                    else:
                        # First delivery only — ping the requester once.
                        msg = await ch.send(
                            content=(f"<@{uid}> " if uid else "") + f"📑 Research job #{jid} finished:",
                            embed=emb, allowed_mentions=discord.AllowedMentions(users=True))
                        delivered_mid = str(msg.id)
                    n += 1
                    with self.db.Session.begin() as s:
                        j = s.get(ResearchJob, jid)
                        j.progress_rendered = new
                        j.posted = True
                        j.report_message_id = delivered_mid
                        j.posted_fingerprint = fp
                else:
                    with self.db.Session.begin() as s:
                        j = s.get(ResearchJob, jid); j.progress_rendered = new
            except discord.HTTPException as e:
                log.warning("research job %s update failed: %s", jid, e)
                if isinstance(e, (discord.NotFound, discord.Forbidden)):
                    with self.db.Session.begin() as s:
                        j = s.get(ResearchJob, jid); j.posted = True
        return n

    def is_reviewer(self, interaction) -> bool:
        uid = str(getattr(getattr(interaction, "user", None), "id", ""))
        roles = {str(r.id) for r in getattr(getattr(interaction, "user", None), "roles", []) or []}
        return uid in self.s.reviewer_user_ids or bool(roles & self.s.reviewer_role_ids)

    def _latest_result(self, s, key):
        from sqlalchemy import select
        p = self.db.get_project(s, key)
        if not p:
            return None, None, []
        r = s.scalar(select(ResearchResult).where(ResearchResult.project_id == p.id)
                     .order_by(ResearchResult.created_at.desc(), ResearchResult.id.desc()).limit(1))
        revs = s.scalars(select(ManualReview).where(ManualReview.project_id == p.id).order_by(ManualReview.id)).all() if r else []
        return p, r, list(revs)

    async def verify(self, interaction, project: str):
        with self.db.Session() as s:
            p, r, revs = self._latest_result(s, project)
        if not r:
            await interaction.response.send_message(f"No research result for `{project}` yet. Run /analyze first.", ephemeral=True)
            return
        await interaction.response.send_message(embed=report_embed(p, r, revs, verify=True))

    async def review(self, interaction, project: str, action: str, tier: int | None = None, notes: str | None = None):
        if not self.is_reviewer(interaction):
            await interaction.response.send_message("⛔ You are not an authorized reviewer.", ephemeral=True)
            return
        with self.db.Session() as s:
            p = self.db.get_project(s, project)
        if not p:
            await interaction.response.send_message(f"Unknown project `{project}`.", ephemeral=True); return
        try:
            rid = rjobs.add_review(self.db, p.id, str(interaction.user.id), action, tier, notes, tiers=self.s.tiers)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True); return
        st = rjobs.calibration_stats(self.db)
        agr = f"{st['exact_agreement']:.0%}" if st["exact_agreement"] is not None else "n/a"
        await interaction.response.send_message(
            f"✅ Review #{rid} stored for **{p.name}** ({action}{f' → Tier {tier}' if action == 'change_tier' else ''}). "
            f"Bot result is kept unchanged; reviews are used for calibration (agreement so far: {agr} over {st['reviews']}).")

    async def post_completed_requests(self, get_channel) -> int:
        """Post follow-up embeds for done-but-unposted x_requests. get_channel(id) -> awaitable channel."""
        from sqlalchemy import select
        from .db import XRequest, Snapshot, Project, utcnow
        with self.db.Session() as s:
            rows = s.scalars(select(XRequest).where(XRequest.status == "done", XRequest.posted == False)).all()  # noqa
            jobs = [(r.id, r.channel_id, s.get(Project, r.project_id), s.get(Snapshot, r.result_snapshot_id),
                     r.requested_by) for r in rows]
        n = 0
        for rid, ch_id, proj, snap, uid in jobs:
            ok = True
            if ch_id and snap:
                try:
                    ch = await get_channel(int(ch_id))
                    await ch.send(content=(f"<@{uid}> " if uid else "") + f"📬 Full X-data report for request #{rid}:",
                                  embed=embeds.analysis_embed(proj, snap),
                                  allowed_mentions=discord.AllowedMentions(users=True))
                    n += 1
                except Exception as e:
                    log.warning("follow-up post for request %s failed: %s", rid, e)
                    ok = isinstance(e, (discord.NotFound, discord.Forbidden))  # give up on dead channels
            if ok:
                with self.db.Session.begin() as s:
                    r = s.get(XRequest, rid); r.posted = True; r.posted_at = utcnow()
        return n

    async def project(self, interaction, project: str):
        """Research result first (same row as the report); hype snapshot otherwise, labeled as hype tier."""
        from .research import jobs as rjobs
        with self.db.Session() as s:
            p = self.db.get_project(s, project)
            snap = (self.db.latest_current(s, p.id, ALGORITHM_VERSION) or self.db.latest_snapshot(s, p.id)) if p else None
            res = rjobs.latest_result(s, p.id) if p else None
            revs = list(s.scalars(select(ManualReview).where(ManualReview.result_id == res.id)).all()) if res else []
        if res:
            await interaction.response.send_message(
                content=(f"X hype snapshot: **{snap.hype_score:.1f}** · {'Tier %s ' % snap.tier if snap.tier else ''}"
                         f"hype tier (not research tier) · {snap.confidence_label}" if snap and snap.hype_score is not None else None),
                embed=report_embed(p, res, revs))
            return
        if not snap:
            await interaction.response.send_message(f"No saved analysis for `{project}`. Use /analyze.", ephemeral=True)
            return
        await interaction.response.send_message(embed=embeds.analysis_embed(p, snap))

    async def history(self, interaction, project: str, limit: int = 15):
        from .research import jobs as rjobs
        with self.db.Session() as s:
            p = self.db.get_project(s, project)
            snaps = self.db.history(s, p.id, limit) if p else []
            results = [(r, rjobs.view_line(r)) for r in s.scalars(select(ResearchResult).where(
                ResearchResult.project_id == p.id).order_by(ResearchResult.id)).all()] if p else []
        if not snaps and not results:
            await interaction.response.send_message(f"No history for `{project}`.", ephemeral=True)
            return
        await interaction.response.send_message(embed=embeds.history_embed(p, snaps, results))

    async def compare(self, interaction, projects: str):
        from .research import jobs as rjobs
        keys = [k.strip() for k in projects.replace(";", ",").split(",") if k.strip()]
        if len(keys) < 2:
            await interaction.response.send_message("Give at least 2 projects, comma-separated.", ephemeral=True)
            return
        rows, missing = [], []
        with self.db.Session() as s:
            for k in keys[:10]:
                p = self.db.get_project(s, k)
                snap = (self.db.latest_current(s, p.id, ALGORITHM_VERSION) or self.db.latest_snapshot(s, p.id)) if p else None
                res = rjobs.latest_result(s, p.id) if p else None
                (rows.append((p, snap, rjobs.view_line(res) if res else None)) if snap else missing.append(k))
        rows.sort(key=lambda r: (r[1].algorithm_version == ALGORITHM_VERSION, r[1].hype_score or 0), reverse=True)
        e = embeds.compare_embed(rows)
        if missing:
            e.set_footer(text="Not analyzed yet: " + ", ".join(missing))
        await interaction.response.send_message(embed=e)

    async def top(self, interaction, limit: int = 10, include_provisional: bool = True):
        from .research import jobs as rjobs
        from .db import Project as P
        with self.db.Session() as s:
            rows = self.db.top(s, max(1, min(limit, 25)), include_provisional, version=ALGORITHM_VERSION)
            fin = []
            for p in s.scalars(select(P)).all():
                r = rjobs.latest_result(s, p.id)
                if r and r.finalization_status == "FINALIZED":
                    fin.append((r.tier or 9, -(r.overall_score or 0), p, rjobs.view_line(r)))
            fin = [(p, line) for _, _, p, line in sorted(fin, key=lambda x: x[:2])][:limit]
        await interaction.response.send_message(embed=embeds.top_embed(rows, fin))

    async def help(self, interaction):
        await interaction.response.send_message(embed=embeds.help_embed(bool(self.s.x_bearer_token), self.s.tiers), ephemeral=True)


class HypeBot(discord.Client):
    def __init__(self, settings: Settings, handlers: Handlers):
        super().__init__(intents=discord.Intents.default())
        self.settings, self.h = settings, handlers
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = self._on_tree_error
        build_tree(self.tree, handlers)

    async def on_interaction(self, interaction: discord.Interaction):
        age = (discord.utils.utcnow() - interaction.created_at).total_seconds()
        name = (interaction.data or {}).get("name") if interaction.data else None
        log.info("interaction %s /%s from %s in #%s — received %.2fs after creation",
                 interaction.id, name, getattr(interaction.user, "id", None), interaction.channel_id, age)
        if age > 2.5:
            log.warning("interaction %s arrived %.1fs late (Discord's limit is 3s) — likely gateway stall/"
                        "host pause; Discord will show 'application did not respond'", interaction.id, age)

    async def _on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        orig = getattr(error, "original", error)
        age = (discord.utils.utcnow() - interaction.created_at).total_seconds()
        if isinstance(orig, discord.NotFound) and getattr(orig, "code", None) == 10062:
            log.error("interaction %s expired before the bot could respond (age %.1fs): %s",
                      interaction.id, age, orig)
            return  # cannot reply to an expired interaction
        log.error("command error in %s", getattr(interaction.command, "name", "?"), exc_info=orig)
        msg = f"❌ Error: {type(orig).__name__}: {orig}"[:1900]
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException as e:
            log.error("could not report error to user: %s", e)

    async def on_resumed(self):
        log.info("gateway session resumed")

    async def on_disconnect(self):
        log.warning("gateway disconnected")

    async def setup_hook(self):
        if self.settings.sync_commands:
            if self.settings.discord_guild_id:
                g = discord.Object(id=self.settings.discord_guild_id)
                self.tree.copy_global_to(guild=g)
                synced = await self.tree.sync(guild=g)
            else:
                synced = await self.tree.sync()
            log.info("synced %d commands", len(synced))
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def _get_channel(self, cid: int):
        return self.get_channel(cid) or await self.fetch_channel(cid)

    async def _poll_loop(self):
        await self.wait_until_ready()
        self.loop.create_task(self._watchdog())
        log.info("x_request poller started (every %ss)", self.settings.poll_seconds)
        while not self.is_closed():
            try:
                n = await self.h.post_completed_requests(self._get_channel)
                if n:
                    log.info("posted %d x_request follow-ups", n)
                r = await self.h.update_research(self._get_channel)
                if r:
                    log.info("posted %d research reports", r)
            except Exception:
                log.exception("x_request poll failed")
            await asyncio.sleep(self.settings.poll_seconds)

    async def _watchdog(self):
        """Log gateway latency every 5 min; detect event-loop stalls (host pause) by wall-clock drift."""
        import time as _t
        last = _t.time()
        while not self.is_closed():
            await asyncio.sleep(30)
            now = _t.time(); drift = now - last - 30; last = now
            if drift > 10:
                log.warning("event loop/host stalled for ~%.0fs (latency now %.2fs)", drift, self.latency)
            elif int(now) % 300 < 30:
                log.info("gateway latency %.3fs", self.latency)

    async def on_ready(self):
        log.info("logged in as %s (%s) in %d guilds", self.user, self.user.id, len(self.guilds))


def build_tree(tree: app_commands.CommandTree, h: Handlers):
    @tree.command(name="analyze", description="Start background research (X hype, team, art, market) for a project")
    @app_commands.describe(project="Project name", x="X username (without @)", opensea="OpenSea collection slug (optional)",
                           magiceden="Magic Eden Solana collection symbol (optional)", website="Official website (optional)")
    async def analyze(i: discord.Interaction, project: str, x: str, opensea: str | None = None,
                      magiceden: str | None = None, website: str | None = None):
        await h.analyze(i, project, x, opensea, magiceden, website)

    @tree.command(name="verify", description="Evidence, links, unverified claims and confidence behind a project's tier")
    async def verify(i: discord.Interaction, project: str):
        await h.verify(i, project)

    @tree.command(name="review", description="(Reviewers) confirm, change tier, reject or annotate the latest result")
    @app_commands.choices(action=[app_commands.Choice(name=n, value=n) for n in ("confirm", "change_tier", "reject", "note")])
    async def review(i: discord.Interaction, project: str, action: app_commands.Choice[str],
                     tier: app_commands.Range[int, 1, 9] | None = None, notes: str | None = None):
        await h.review(i, project, action.value, tier, notes)

    @tree.command(name="refresh", description="Collect fresh data and recalculate a saved project's score")
    async def refresh(i: discord.Interaction, project: str):
        await h.refresh(i, project)

    @tree.command(name="project", description="Show the latest saved analysis")
    async def project(i: discord.Interaction, project: str):
        await h.project(i, project)

    @tree.command(name="history", description="Show historical scores and tier changes")
    async def history(i: discord.Interaction, project: str, limit: app_commands.Range[int, 1, 50] = 15):
        await h.history(i, project, limit)

    @tree.command(name="compare", description="Compare projects (comma-separated names or @handles)")
    async def compare(i: discord.Interaction, projects: str):
        await h.compare(i, projects)

    @tree.command(name="top", description="Highest-ranked projects")
    async def top(i: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 10, include_provisional: bool = True):
        await h.top(i, limit, include_provisional)

    @tree.command(name="help", description="Show available commands")
    async def help_(i: discord.Interaction):
        await h.help(i)
    return tree


def create_bot(settings: Settings | None = None) -> HypeBot:
    settings = settings or load_settings()
    db = Database(settings.database_url)
    return HypeBot(settings, Handlers(settings, db, Analyzer(settings, db)))


def main():
    settings = load_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not settings.discord_token:
        raise SystemExit("DISCORD_TOKEN is required")
    if not settings.x_bearer_token:
        log.warning("X_BEARER_TOKEN not set — analyses will be provisional and exclude X metrics")
    create_bot(settings).run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
