"""Discord embed builders."""
from __future__ import annotations
import discord
from .config import COMPONENT_LABELS

TIER_COLORS = {1: 0x2ECC71, 2: 0xF1C40F, 3: 0xE67E22, 4: 0xE74C3C}
from .config import DEFAULT_TIERS, tier_ranges_text
TREND_ICON = {"increasing": "📈 Increasing", "decreasing": "📉 Decreasing", "stable": "➖ Stable", "new": "🆕 First analysis"}


def _fmt(v, pct=False):
    if v is None:
        return "n/a"
    if pct:
        return f"{v:.2%}"
    if isinstance(v, float):
        return f"{v:,.1f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def queued_embed(project, warnings) -> discord.Embed:
    e = discord.Embed(title=f"{project.name} (@{project.x_display_handle})", color=0x95A5A6,
                      url=f"https://x.com/{project.x_display_handle}",
                      description="⏳ **Queued for X data** — no score yet (the score is based only on X data).")
    e.add_field(name="Notes", value="\n".join(f"• {n}" for n in warnings)[:1024], inline=False)
    return e


HT = "hype tier (not research tier)"


def analysis_embed(project, snap, warnings=None, research_line=None) -> discord.Embed:
    title = f"{project.name} (@{project.x_display_handle}) — X hype snapshot"
    ev = getattr(snap, "evaluation", None) or {}
    ver = getattr(snap, "algorithm_version", "v1-legacy")
    tier = f"Tier {snap.tier} {HT}" if snap.tier else "Unranked — evidence too thin for a tier (indicative score only)"
    prov = " — ⚠️ PROVISIONAL" if snap.provisional and snap.tier else ""
    if snap.hype_score is None:
        e = discord.Embed(title=title, color=0x95A5A6, description="No X data — not scoreable.")
        return e
    e = discord.Embed(title=title, url=f"https://x.com/{project.x_display_handle}",
                      color=TIER_COLORS.get(snap.tier, 0x95A5A6),
                      description=f"**Hype Score: {snap.hype_score:.1f}/100 · {tier}**{prov}\n"
                                  f"Confidence: **{snap.confidence_label}** ({snap.confidence:.0%}) · "
                                  f"Trend: {TREND_ICON.get(snap.trend, snap.trend)}"
                                  + (f" ({snap.score_delta:+.1f})" if snap.score_delta is not None else "")
                                  + (f" · prev Tier {snap.previous_tier}" if snap.previous_tier else "")
                                  + (f"\nCoverage: {ev['coverage']:.0%} of collectable categories" if ev.get("coverage") is not None else "")
                                  + f" · Algorithm {ver}"
                                  + (" (recalculated from snapshot #%s)" % snap.source_snapshot_id if getattr(snap, "is_recalculation", False) else ""))
    e.add_field(name="🔬 Research result", value=(research_line or "No research job yet — this is X hype only, not a research tier.")[:1024],
                inline=False)
    lines = []
    for k, label in COMPONENT_LABELS.items():
        c = snap.components.get(k, {})
        if c.get("status") == "not_collected":
            lines.append(f"`{label}`: — not collected in this build (excluded, /{c.get('max', 0):g})")
        elif c.get("points") is None:
            lines.append(f"`{label}`: — missing data (/{c.get('max', 0):g})")
        else:
            lines.append(f"`{label}`: **{c['points']:.1f}**/{c['max']:g}")
    e.add_field(name="Score breakdown", value="\n".join(lines)[:1024], inline=False)
    m = snap.metrics
    xm = (f"Followers: {_fmt(m.get('followers'))} · Verified: {_fmt(m.get('verified'))}\n"
          f"Avg likes/RTs/replies: {_fmt(m.get('avg_likes'))}/{_fmt(m.get('avg_reposts'))}/{_fmt(m.get('avg_replies'))}\n"
          f"Avg views: {_fmt(m.get('avg_views'))} · Eng. rate: {_fmt(m.get('engagement_rate'), True)}\n"
          f"Mentions 7d: {_fmt(m.get('mentions_7d_total'))} · last 48h/day: {_fmt(m.get('mentions_recent_per_day'))} "
          f"· velocity: {_fmt(m.get('mention_velocity_change'), True)}\n"
          f"Posts/week: {_fmt(m.get('posts_per_week'))} · Mentions (sample): {_fmt(m.get('mentions_sample'))} "
          f"by {_fmt(m.get('mention_unique_authors'))} accounts\n"
          f"Influential mentioners: {_fmt(m.get('influential_mentioners'))}")
    e.add_field(name="X metrics", value=xm[:1024], inline=False)
    if snap.flags:
        e.add_field(name="⚠️ Engagement quality signals", value="\n".join(f"• {f}" for f in snap.flags)[:1024], inline=False)
    if snap.missing:
        e.add_field(name="Missing data (excluded, not counted as zero)",
                    value=", ".join(COMPONENT_LABELS[k] for k in snap.missing)[:1024], inline=False)
    if ev.get("confidence_reasons"):
        e.add_field(name=f"Confidence: {snap.confidence_label} — why", value="\n".join(f"• {r}" for r in ev["confidence_reasons"])[:1024], inline=False)
    notes = list(warnings or []) or ((snap.notes or "").splitlines())
    if notes:
        e.add_field(name="Notes", value="\n".join(f"• {n}" for n in notes)[:1024], inline=False)
    e.set_footer(text=f"Snapshot #{snap.id} · {snap.created_at:%Y-%m-%d %H:%M} UTC · Sources: "
                      + (", ".join(s["source"] for s in snap.sources) or "none"))
    return e


def history_embed(project, snaps, results=()) -> discord.Embed:
    e = discord.Embed(title=f"History — {project.name} (@{project.x_display_handle})", color=0x3498DB)
    rows = []
    for s in snaps:
        rows.append(f"`{s.created_at:%Y-%m-%d %H:%M}` `{getattr(s, 'algorithm_version', 'v1-legacy')}` **{(s.hype_score or 0):.1f}** · {f"hype T{s.tier}" if s.tier else "unranked"} · "
                    f"{s.confidence_label}{' · prov.' if s.provisional else ''}"
                    + (f" · {s.score_delta:+.1f}" if s.score_delta is not None else "")
                    + (f" · T{s.previous_tier}→T{s.tier}" if s.previous_tier and s.previous_tier != s.tier else ""))
    e.description = ("**X hype snapshots** (hype tier, not research tier)\n" + "\n".join(rows))[:3000] or "No snapshots."
    if results:
        e.add_field(name="🔬 Research results", value="\n".join(
            f"`{r.created_at:%Y-%m-%d %H:%M}` `{r.algorithm_version}` {line}" for r, line in results[-6:])[:1024], inline=False)
    same = [s for s in snaps if getattr(s, "algorithm_version", "v1-legacy") == getattr(snaps[-1], "algorithm_version", "v1-legacy")]
    e.set_footer(text="Scores from different algorithm versions are not directly comparable.")
    if len(same) >= 2:
        snaps = same
        d = snaps[-1].hype_score - snaps[0].hype_score
        e.add_field(name="Overall", value=f"{'📈' if d > 3 else '📉' if d < -3 else '➖'} {d:+.1f} over {len(snaps)} snapshots")
    return e


def compare_embed(rows) -> discord.Embed:
    e = discord.Embed(title="Project comparison", color=0x9B59B6)
    for row in rows:
        p, s, rl = (row + (None,))[:3]
        comps = " · ".join(f"{COMPONENT_LABELS[k].split()[0]} {c['points']:.0f}/{c['max']:g}"
                           for k, c in s.components.items() if c.get("points") is not None and k in COMPONENT_LABELS)
        e.add_field(name=f"{p.name} (@{p.x_display_handle})",
                    value=(f"**{(s.hype_score or 0):.1f}** · `{getattr(s, 'algorithm_version', 'v1-legacy')}` · {f"Tier {s.tier} {HT}" if s.tier else "Unranked"} · {s.confidence_label} confidence"
                           f"{' · ⚠️ provisional' if s.provisional else ''} · {TREND_ICON.get(s.trend)}\n"
                           f"🔬 {rl or 'no research result'}\n{comps}")[:1024],
                    inline=False)
    return e


def top_embed(rows, research=()) -> discord.Embed:
    e = discord.Embed(title="🏆 Top projects", color=0xF1C40F)
    e.description = ("**Research tiers (FINALIZED only)**\n" + ("\n".join(
        f"**{i}.** {p.name} (@{p.x_display_handle}) — {line}" for i, (p, line) in enumerate(research, 1))
        or "No finalized research yet — tiers below are X hype only.")
        + f"\n\n**By X Hype Score — {HT}**\n" + ("\n".join(
        f"**{i}.** {p.name} (@{p.x_display_handle}) — **{s.hype_score:.1f}** · hype T{s.tier} · "
        f"{s.confidence_label}{' · prov.' if s.provisional else ''}" for i, (p, s) in enumerate(rows, 1)
    ) or "No analyzed projects yet. Use `/analyze`."))[:4000]
    return e


def help_embed(x_ready: bool, tiers=DEFAULT_TIERS) -> discord.Embed:
    e = discord.Embed(title="NFT / Web3 Hype Bot — commands", color=0x5865F2)
    for n, d in [("/analyze project x [opensea] [magiceden] [website]", "Starts a background research job (15–30 min): identity, X interest, team, art, market, category, history → evidence report. No tier until research finishes."),
                 ("/refresh project", "New research job for a saved project (cached evidence re-used when fresh)."),
                 ("/verify project", "Evidence + links, unverified claims, conflicts, missing info, confidence reasons."),
                 ("/review project action [tier] [notes]", "Authorized reviewers only: confirm / change_tier / reject / note (stored separately)."),
                 ("/project project", "Show the latest saved analysis."),
                 ("/history project", "Historical scores and tier changes."),
                 ("/compare projects", "Compare saved projects (comma-separated names or @handles)."),
                 ("/top [limit] [include_provisional]", "Highest-ranked projects in the database."),
                 ("/help", "This message.")]:
        e.add_field(name=n, value=d, inline=False)
    e.add_field(name="Tiers", value=tier_ranges_text(tiers), inline=False)
    e.add_field(name="Confidence & ranking", value=(
        "Confidence measures evidence quality (posts sampled, unique authors, coverage, source, identity, "
        "freshness, history), not hype. **Low** confidence or no official posts → **Unranked** (indicative "
        "score, no tier). **Medium** → tier marked provisional. Missing categories are excluded, never zero. "
        "/top only ranks results from the current algorithm."), inline=False)
    e.set_footer(text="X API: " + ("configured ✅" if x_ready else "NOT configured — set X_BEARER_TOKEN"))
    return e
