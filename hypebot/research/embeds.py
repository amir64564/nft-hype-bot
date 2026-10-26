"""Discord embeds for research reports and /verify."""
from __future__ import annotations
import discord

COL = {1: 0x2ECC71, 2: 0xF1C40F, 3: 0xE67E22, 4: 0xE74C3C}



IDENTITY_TEXT = {"verified": "✅ identity verified", "unverified": "⚠️ identity unverified", "mismatch": "❌ identity MISMATCH"}


def why_tier_text(r: dict) -> str:
    """Why this tier: not higher / not lower, plus quality cap, hysteresis and identity state when relevant."""
    w = r.get("why_tier") or {}
    out = [f"Not higher: {w.get('not_higher')}", f"Not lower: {w.get('not_lower')}"]
    if w.get("quality_cap"):
        out.append(f"Cap: {w['quality_cap']}")
    if r.get("hysteresis"):
        out.append(f"Hysteresis: {r['hysteresis']}")
    if r.get("identity_state"):
        out.append(IDENTITY_TEXT.get(r["identity_state"], r["identity_state"]))
    return "\n".join(out)

def _lines(items, n=6, w=1000):
    out = []
    for c in items[:n]:
        u = c.get("url")
        out.append(f"• {c['claim'][:160]}" + (f" — [source]({u})" if u else f" ({c.get('source', '')})"))
    return "\n".join(out)[:w] or "—"


def _num(v, precise):
    if v is None:
        return "n/a"
    return f"{v:.0f}" if precise else f"~{5 * round(v / 5):.0f}"   # no invented precision when withheld


ICON = {"VERIFIED": "✅", "PARTIAL": "🟡", "FAILED": "❌", "NOT_APPLICABLE": "➖"}


def conf_text(r) -> str:
    c = r.get("confidence")
    return f"{r['confidence_label']} ({c:.0%})" if isinstance(c, (int, float)) else str(r.get("confidence_label"))


def headline(r) -> tuple[str, int | None]:
    """Top line: final tier, or the suggested tier clearly labeled provisional."""
    from ..scoring import tier_for
    precise = r.get("overall_score") is not None
    hype = _num(r.get("hype_score"), precise)
    qual = _num(r.get("quality_score") if precise else r.get("quality_score_internal"), precise)
    if r.get("tier") and r.get("status", "FINALIZED") == "FINALIZED":
        return f"🏆 Tier {r['tier']} — Hype {hype} / Quality {qual}", r["tier"]
    t = r.get("suggested_tier")
    if t is None and r.get("overall_score_internal") is not None and "suggested_tier" not in r:
        t = tier_for(r["overall_score_internal"])
    if t is None:
        return f"🏆 Tier — unknown (provisional — {r['verdict']}, confidence {r['confidence_label']})", None
    return (f"🏆 Tier {t} (provisional — {r['verdict']}, confidence {r['confidence_label']}) — "
            f"Hype {hype} / Quality {qual}"), t


def checklist_lines(r) -> str:
    out = []
    for k, c in (r.get("checklist") or {}).items():
        out.append(f"{ICON.get(c['status'], '?')} {'**' if c.get('mandatory') else ''}{c['label'][:38]}{'**' if c.get('mandatory') else ''}"
                   f" — {c['reason'][:70]}")
    return "\n".join(out)


def signals_text(r) -> str:
    """Compact r2.1 signal block: popularity · smart accounts · Sorsa · quality · confidence + who/why/unverified."""
    sg = r["signals"]
    pop = sg.get("overall_popularity") or {}
    ss = sg.get("smart_account_signal") or {}
    cr = sg.get("sorsa_social_credibility") or {}
    q = sg.get("project_quality")
    rc = sg.get("research_confidence") or {}
    lines = [f"Popularity (hype) **{pop.get('hype_score') if pop.get('hype_score') is not None else 'n/a'}** · "
             f"Smart accounts **{ss.get('label', 'unavailable')}**" + (f" ({ss['score']:.0f}, {ss.get('independent_credible_accounts', 0)} independent)" if ss.get("score") is not None else "")
             + f" · Sorsa **{cr.get('label', 'unavailable')}**" + (f" (score {cr['score']:.0f}; {cr.get('influencers')} infl / {cr.get('projects')} proj / {cr.get('vcs')} VC)" if cr.get("score") is not None else "")
             + f" · Quality **{'~%.0f' % q if q is not None else 'n/a'}** · Confidence **{rc.get('label')}**"]
    ms = sg.get("matched_accounts") or []
    if ss.get("seed_matches") or ss.get("keeper_matches"):
        fh = ss.get("seed_follow_handles") or []
        lines.append(f"🎯 Hunter seeds: **{ss.get('seed_matches', 0)}** matched · **{ss.get('seed_matches_followed', 0)}** follow"
                     + (" (" + ", ".join("@" + h for h in fh[:8]) + ")" if fh else "")
                     + f" · {ss.get('seed_matches_mentioned', 0)} mentioned · {ss.get('keeper_matches', 0)} cook-keepers")
    for m in ms[:10 if (ss.get("seed_matches") or ss.get("keeper_matches")) else 4]:
        src = "+".join(m["source_families"])
        fv = f", follow seen {str(m['follow_verified_at'])[:10]}" if m.get("follow_verified_at") else ", follow date unverified" if m["relationship"] == "followed" else ""
        lines.append(f"• @{m['handle']} — {m['class'].replace('_', ' ')} [{src}{fv}]: {m['why'][:90]}")
    if sg.get("early_attention"):
        lines.append(f"🔎 {sg['early_attention_reason'][:200]}")
    for c in (sg.get("social_conflicts") or [])[:2]:
        lines.append(f"⚔️ {c['detail'][:150]}")
    unv = (sg.get("unverified") or []) + (sg.get("unavailable_reasons") or [])
    if unv:
        lines.append("❔ " + "; ".join(unv[:2])[:220])
    lines.append("Final tier: " + ("✅ research complete" if sg.get("ready_for_final_tier") else "not yet — " + (sg.get("not_final_reason") or "incomplete")[:120]))
    return "\n".join(lines)


def report_embed(project, result, reviews=None, verify=False) -> discord.Embed:
    r = result.report
    if "checklist" not in r:
        return _legacy_report_embed(project, result, reviews, verify)
    head, est_tier = headline(r)
    final = r["status"] == "FINALIZED"
    e = discord.Embed(title=head[:256], url=f"https://x.com/{project.x_display_handle}",
                      color=COL.get(r.get("tier") or est_tier, 0x95A5A6) if final else 0x95A5A6)
    e.set_author(name=f"{'🔎 Verify — ' if verify else '🔬 Research report — '}{project.name} (@{project.x_display_handle})"[:256])
    hype = f"{r['hype_score']:.1f}" if r.get("hype_score") is not None else "n/a"
    q = r.get("quality_score") if final else r.get("quality_score_internal")
    qual = (f"{q:.1f}" if final else f"~{5 * round(q / 5):.0f} (provisional)") if q is not None else "n/a"
    status = ("✅ **FINALIZED** — all mandatory checks verified" if final else
              f"⚠️ **{r['status']}** · {r['verdict']} — tier is a suggestion, not final\nWhy: {r.get('not_finalizable_reason') or '—'}")
    ht = f" (hype tier {r['hype_tier']}, not research tier)" if r.get("hype_tier") else ""
    e.description = (f"{status}\nHype **{hype}**{ht} · Quality **{qual}** · Confidence **{conf_text(r)}**\n"
                     f"Category **{r['profile']}** · stage {r['stage']} · {r.get('mint_type') or 'unknown'} mint · "
                     f"checklist coverage {r['coverage']:.0%}" + (f"\n↔️ {r['hysteresis']}" if r.get("hysteresis") else ""))
    e.add_field(name="Checklist (bold = mandatory)", value=checklist_lines(r)[:1024] or "—", inline=False)
    if r.get("missing_mandatory"):
        e.add_field(name="Missing mandatory checks", value=", ".join(r["missing_mandatory"])[:1024], inline=False)
    fl = [f"`{f['label'][:30]}` " + (f"**{f['points']:.1f}**/{f['max']}" if f["points"] is not None else f"— /{f['max']}")
          for f in r["factors"].values()]
    e.add_field(name="Factor breakdown", value="\n".join(fl)[:1024], inline=False)
    if r.get("signals"):
        e.add_field(name="🧭 Signals (reported separately — Sorsa/smart followers do NOT change the tier)",
                    value=signals_text(r)[:1024], inline=False)
    cp = r.get("confidence_parts") or {}
    e.add_field(name="Confidence", value=(" · ".join(f"{k} {v:.2f}" for k, v in cp.items() if v is not None) + "\n"
                                          + "\n".join(f"• {x}" for x in r.get("confidence_reasons", [])[:4]))[:1024] or "—", inline=False)
    e.add_field(name="Supporting evidence (sourced)", value=_lines(r.get("supporting_evidence", []), 5, 900), inline=False)
    if r.get("positives"):
        e.add_field(name="➕ Positives", value=_lines(r["positives"], 4, 800), inline=False)
    if r.get("risks"):
        e.add_field(name="⚠️ Risks", value=_lines(r["risks"], 5, 800), inline=False)
    if r.get("judgments"):
        e.add_field(name="🎨 Visual/qualitative judgments (opinion, not fact)", value=_lines(r["judgments"], 4, 800), inline=False)
    bm = r.get("benchmark") or {}
    e.add_field(name="Benchmark", value=(bm.get("summary") or bm.get("status") or "—")[:1024], inline=False)
    srcs = r.get("sources_checked") or []
    last = {}
    for a in r.get("attempts") or []:
        last[(a["source"], a["target"])] = a
    fails = [a for a in last.values() if a["outcome"] != "success"]
    e.add_field(name=f"Sources checked ({r.get('n_sources_checked', len(srcs))})",
                value=("\n".join(f"• {x}" for x in srcs[:6]) + ("\n" + "\n".join(
                    f"• ✗ {a['source']}: {a['outcome']}" + (f" (HTTP {a['http_status']})" if a.get("http_status") else "")
                    + f" after {a['attempt']} attempt(s)" for a in fails[:4]) if fails else ""))[:1024] or "—",
                inline=False)
    if verify or r.get("unverified_claims"):
        e.add_field(name="❔ Unverified claims (excluded)", value="\n".join(
            f"• {c['claim'][:120]} ({c['reason']})" for c in r.get("unverified_claims", [])[:5])[:1024] or "—", inline=False)
    if r.get("conflicts"):
        e.add_field(name="⚔️ Conflicting evidence", value="\n".join(
            f"• {c['signal']}: " + " vs ".join(f"[{x['claim'][:50]}]({x['url']})" for x in c["claims"][:2]) for c in r["conflicts"])[:1024], inline=False)
    if r.get("why_tier"):
        e.add_field(name="Why this tier", value=why_tier_text(r)[:1024], inline=False)
    ch = r.get("change") or {}
    if ch.get("from") is not None or ch.get("cause"):
        e.add_field(name=f"Change: {ch.get('from')} → {ch.get('to')}",
                    value=(f"{ch.get('cause') or ''}\n" + "\n".join(ch.get("drivers") or []))[:1024] or "—", inline=False)
    if reviews:
        e.add_field(name="Manual reviews", value="\n".join(f"• {x.action}" + (f" → T{x.reviewer_tier}" if x.reviewer_tier else "")
                                                             + (f": {x.notes[:80]}" if x.notes else "") for x in reviews[-4:])[:1024], inline=False)
    while len(e) > 5800 and len(e.fields) > 4:
        e.remove_field(len(e.fields) - 1)
    e.set_footer(text=f"Result #{result.id} · {result.algorithm_version} · {r['status']} · {result.created_at:%Y-%m-%d %H:%M} UTC")
    return e


def _legacy_report_embed(project, result, reviews=None, verify=False) -> discord.Embed:
    r = result.report
    vt = r["verdict_text"]
    sc = (f" · Score **{r['overall_score']:.1f}**/100" if r.get("overall_score") is not None else " · score withheld (evidence insufficient)")
    head, est_tier = headline(r)
    e = discord.Embed(title=head[:256], url=f"https://x.com/{project.x_display_handle}",
                      color=COL.get(r.get("tier") or est_tier, 0x95A5A6))
    e.set_author(name=f"{'🔎 Verify — ' if verify else '🔬 Research report — '}{project.name} (@{project.x_display_handle})"[:256])
    hype = f"{r['hype_score']:.1f}" if r.get("hype_score") is not None else "n/a"
    qual = f"{r['quality_score']:.1f}" if r.get("quality_score") is not None else ("withheld" if r.get("quality_score_internal") is not None else "n/a")
    status = "✅ Final tier" if r.get("tier") else f"⚠️ Status: **{r['verdict']}** — tier above is a best estimate, not final"
    e.description = (f"{status}\n**{vt}**{sc}\nConfidence: **{r['confidence_label']}** · Coverage {r['coverage']:.0%} · "
                     f"Profile **{r['profile']}** · {r.get('mint_type')} mint · stage {r['stage']}\n"
                     f"Hype: **{hype}** · Quality: **{qual}** (separate dimensions)"
                     + (f"\n↔️ {r['hysteresis']}" if r.get("hysteresis") else ""))
    fl = []
    for k, f in r["factors"].items():
        fl.append(f"`{f['label'][:34]}` " + (f"**{f['points']:.1f}**/{f['max']}" if f["points"] is not None else f"— /{f['max']}")
                  + f" · {f['why'][:70]}")
    e.add_field(name="Factor breakdown", value="\n".join(fl)[:1024], inline=False)
    if r.get("manual_verification"):
        e.add_field(name="🛑 Manual verification required", value="\n".join(f"• {m}" for m in r["manual_verification"])[:1024], inline=False)
    e.add_field(name="Supporting evidence (sourced)", value=_lines(r.get("supporting_evidence", [])), inline=False)
    if r.get("positives"):
        e.add_field(name="➕ Positives", value=_lines(r["positives"], 5), inline=False)
    if r.get("risks"):
        e.add_field(name="⚠️ Risks / negatives", value=_lines(r["risks"], 6), inline=False)
    if r.get("judgments"):
        e.add_field(name="🎨 Qualitative judgments (agent opinion, not fact)", value=_lines(r["judgments"], 4), inline=False)
    if verify or r.get("unverified_claims"):
        e.add_field(name="❔ Unverified claims (excluded from scoring facts)",
                    value="\n".join(f"• {c['claim'][:150]} ({c['reason']})" for c in r.get("unverified_claims", [])[:6])[:1024] or "—", inline=False)
    if verify and r.get("conflicts"):
        e.add_field(name="⚔️ Conflicting evidence", value="\n".join(
            f"• {c['signal']}: " + " vs ".join(f"[{x['claim'][:50]}]({x['url']})" for x in c["claims"][:2]) for c in r["conflicts"])[:1024], inline=False)
    e.add_field(name="Missing info", value="\n".join(f"• {m}" for m in r.get("missing_info", [])[:8])[:1024] or "—", inline=False)
    if r.get("why_tier"):
        e.add_field(name="Why this tier", value=why_tier_text(r)[:1024], inline=False)
    if r.get("change") and (r["change"].get("drivers") or r["change"].get("from") != r["change"].get("to")):
        e.add_field(name=f"Change: {r['change']['from']} → {r['change']['to']}",
                    value="\n".join([r["change"].get("cause") or ""] + list(r["change"].get("drivers") or []))[:1024] or "—", inline=False)
    if r.get("confidence_reasons"):
        e.add_field(name="Confidence reasons", value="\n".join(f"• {x}" for x in r["confidence_reasons"][:6])[:1024], inline=False)
    if r.get("peers"):
        pr = r["peers"]
        e.add_field(name="Peers", value=f"Hype percentile {pr['hype_percentile']}% among {pr['n']} {pr['category']}/{pr['stage']} projects", inline=False)
    if reviews:
        e.add_field(name="Manual reviews", value="\n".join(f"• {x.action}" + (f" → T{x.reviewer_tier}" if x.reviewer_tier else "")
                                                             + (f": {x.notes[:80]}" if x.notes else "") for x in reviews[-4:])[:1024], inline=False)
    while len(e) > 5800 and len(e.fields) > 3:
        e.remove_field(len(e.fields) - 1)
    e.set_footer(text=f"Result #{result.id} · {result.algorithm_version} · {result.created_at:%Y-%m-%d %H:%M} UTC")
    return e
