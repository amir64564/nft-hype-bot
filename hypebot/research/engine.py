"""Research verdict engine (deterministic). Inputs: v2 hype snapshot, stored evidence, category,
previous result and peers. Output: report dict + verdict/tier. No claim enters the report's
'supporting evidence', 'positives' or 'risks' without a source URL."""
from __future__ import annotations
from ..scoring import clamp, tier_for, confidence_label, LOW_CAP, MEDIUM_CAP, ALGORITHM_VERSION
from ..config import DEFAULT_TIERS
from .profiles import PROFILES, FACTORS
from .checklist import build_checklist, missing_mandatory, coverage as cl_coverage, CHECKS, MIN_PEERS

RESEARCH_VERSION = "r2.0"   # r2.0: checklist + state machine + visual art review + capped confidence
HYSTERESIS = 3.0          # points inside a boundary needed before the tier changes
NMR = "Needs More Research"
MVR = "Manual Verification Required"


def _ev(e):
    """Evidence row or dict -> dict."""
    if isinstance(e, dict):
        return e
    return {k: getattr(e, k) for k in ("id", "section", "kind", "claim", "source_url", "source_name", "account",
                                         "posted_at", "engagement", "why", "signal", "value", "data", "verified",
                                         "collected_by")}


def _sig(evs, name, verified_only=True):
    return [e for e in evs if e.get("signal") == name and (e.get("verified") or not verified_only)]


def f_hype(snap):
    if not snap or snap.get("hype_score") is None or "engagement_quality" in (snap.get("missing") or []):
        return None, "no scoreable official X engagement"
    return snap["hype_score"] / 100, f"v2 hype score {snap['hype_score']:.1f} ({snap.get('confidence_label')} confidence)"


def f_genuineness(m, evs):
    ns = (m or {}).get("mentions_sample") or 0
    if ns < 10:
        return None, "fewer than 10 sampled mentions"
    ind = clamp(((m.get("independent_authors") or 0) / ns) / 0.5)
    promo = m.get("promo_or_low_effort_ratio") or 0
    s = 0.6 * ind + 0.4 * (1 - promo)
    resp = _sig(evs, "respected_account_engaged")
    neg = _sig(evs, "giveaway_driven") + _sig(evs, "paid_promotion")
    s += min(0.3, 0.1 * len(resp)) - min(0.4, 0.2 * len(neg))
    return clamp(s), (f"{m.get('independent_authors')}/{ns} independent authors, {promo:.0%} promo/low-effort; "
                      f"{len(resp)} sourced respected-account engagements, {len(neg)} sourced giveaway/paid-promo signals")


def f_team(evs):
    t = [e for e in evs if e.get("section") == "team" and e.get("verified") and e.get("signal")]
    if not t:
        return None, "no sourced team/artist evidence (unverified, not negative)"
    s = 0.5
    for e in t:
        v = e.get("value") or 0
        s += {"team_prior_collection": 0.3, "team_delivery_record": 0.2, "team_known_identity": 0.1,
              "team_misleading_claim": 0.4, "team_abandonment": 0.5}.get(e["signal"], 0) * v
    return clamp(s), f"{len(t)} sourced team/artist observations"


def f_art(vr):
    """Only a visual review record (images actually downloaded + viewed) can score art."""
    ok = [i for i in (vr or {}).get("images") or [] if i.get("viewed") and i.get("file_verified") and i.get("official")]
    js = [c["judgment"] for c in (vr or {}).get("checks") or [] if c["check"] in ("quality", "consistency", "variation")]
    if not ok or not js:
        return None, "no visual review record (perceptual hashes alone are not an art review)"
    return sum(js) / len(js), f"visual review of {len(ok)} image(s): {len(js)} quality/consistency/variation judgments (opinion, not fact)"


def f_originality(vr, evs):
    js = [c["judgment"] for c in (vr or {}).get("checks") or [] if c["check"] in ("originality_indicators", "suspicious_similarity")]
    copy = _sig(evs, "copy_evidence")
    if not js and not copy:
        return None, "no visual originality review (hash similarity alone is not an originality verdict)"
    s = (sum(js) / len(js)) if js else 0.6
    s += sum(0.5 * (e.get("value") or -1) for e in copy)
    return clamp(s), f"{len(js)} visual originality judgments, {len(copy)} sourced copying claims"


def f_market(evs, stage):
    mf = [e for e in evs if e.get("section") == "market" and e.get("verified")]
    if stage in ("pre_mint", "minting", "unknown"):
        ms = [e for e in mf if e.get("signal") == "milestone" and e.get("value") is not None]
        if not ms:
            return None, f"stage {stage}: no sourced milestones (not penalized)"
        return clamp(0.5 + sum(e["value"] for e in ms) * 0.25), f"stage {stage}: {len(ms)} sourced milestones"
    data = {}
    for e in mf:
        for k, v in (e.get("data") or {}).items():
            if v is not None and k not in data:
                data[k] = v
    parts = []
    sup, own = data.get("supply"), data.get("owners") or data.get("holders")
    if sup and own:
        parts.append(clamp((own / sup) / 0.5))                     # holder dispersion; 50%+ unique = max
    if data.get("sales_7d") is not None:
        parts.append(clamp((data["sales_7d"] or 0) / max(1, (sup or 1000) * 0.02)))   # ~2% of supply/week = liquid
    if data.get("repeat_pair_sales") is not None and data.get("recent_sales"):
        parts.append(1 - clamp(data["repeat_pair_sales"] / data["recent_sales"] / 0.3))
    if not parts:
        return None, "post-mint but no usable holder/sales data"
    ms = sum(e.get("value") or 0 for e in mf if e.get("signal") in ("milestone", "utility_delivered"))
    return clamp(sum(parts) / len(parts) + 0.1 * ms), "holders/supply, 7d sales, wash-trade check (floor price alone is not quality)"


def f_identity(evs, handle):
    links = [e for e in _sig(evs, "official_link") if (e.get("value") or 0) >= 1]
    pairs = {tuple(sorted([(e.get("data") or {}).get("from_kind", "?"), (e.get("data") or {}).get("to_kind", "?")])) for e in links}
    if not links:
        return 0.0, "no verified cross-links between official X, website and marketplace"
    return clamp(len(pairs) / 2), f"{len(links)} verified cross-links ({', '.join('↔'.join(p) for p in sorted(pairs))})"


SOURCE_RELIABILITY = {"bot:x": 0.8, "bot:market": 0.9, "bot:images": 0.9, "feeder": 0.7}


def _fmt(e):
    return {"claim": e["claim"], "url": e["source_url"], "source": e.get("source_name"), "account": e.get("account"),
            "posted_at": e.get("posted_at"), "engagement": e.get("engagement"), "why": e.get("why"), "kind": e["kind"]}


def benchmark(snap, peers):
    """Percentiles vs comparable peers (same category & stage, other projects). None if < MIN_PEERS."""
    if not snap or len(peers) < MIN_PEERS:
        return {"status": "insufficient benchmark data", "n": len(peers), "min_required": MIN_PEERS,
                "peers": [p["name"] for p in peers],
                "summary": f"insufficient benchmark data: {len(peers)} comparable peer(s), need {MIN_PEERS} (same category & stage, other projects)"}
    m = snap.get("metrics") or {}
    def ind(mm):
        return (mm.get("independent_authors") or 0) / mm["mentions_sample"] if mm.get("mentions_sample") else None
    metrics = {"hype_score": lambda x: x.get("hype_score"), "median_engagement": lambda x: (x.get("metrics") or {}).get("median_engagement"),
               "mentions_7d_total": lambda x: (x.get("metrics") or {}).get("mentions_7d_total"),
               "independent_author_share": lambda x: ind(x.get("metrics") or {})}
    me = {"hype_score": snap.get("hype_score"), "median_engagement": m.get("median_engagement"),
          "mentions_7d_total": m.get("mentions_7d_total"), "independent_author_share": ind(m)}
    pct = {}
    for k, f in metrics.items():
        vals = [f(p) for p in peers if f(p) is not None]
        if me[k] is not None and len(vals) >= MIN_PEERS:
            pct[k] = round(100 * sum(1 for v in vals if v < me[k]) / len(vals))
    return {"status": "ok", "n": len(peers), "percentiles": pct, "peers": [p["name"] for p in peers][:10],
            "summary": f"vs {len(peers)} same-category/stage peers: " + (", ".join(f"{k} p{v}" for k, v in pct.items()) or "no comparable metrics")}


def evaluate_research(*, snap: dict | None, evidence: list, category: dict, handle: str,
                      previous: dict | None = None, peers: list | None = None, unavailable: list | None = None,
                      hints: dict | None = None, attempts: list | None = None, tiers=DEFAULT_TIERS,
                      social: dict | None = None) -> dict:
    evs = [_ev(e) for e in evidence]
    hints = hints or {}
    attempts = attempts or []
    peers = peers or []
    m = (snap or {}).get("metrics") or {}
    prim = category.get("primary") or "unknown"
    stage = category.get("stage") or "unknown"
    w = PROFILES.get(prim, PROFILES["unknown"])
    vr = hints.get("visual_review")
    raw = {"hype": f_hype(snap), "x_genuineness": f_genuineness(m, evs), "team": f_team(evs), "art": f_art(vr),
           "originality": f_originality(vr, evs), "market": f_market(evs, stage), "identity": f_identity(evs, handle)}
    factors, avail = {}, 0
    for k, (v, why) in raw.items():
        factors[k] = {"score": None if v is None else round(v, 4), "points": None if v is None else round(v * w[k], 2),
                      "max": w[k], "why": why, "label": FACTORS[k]}
        if v is not None:
            avail += w[k]
    overall = round(sum(f["points"] for f in factors.values() if f["points"] is not None) / avail * 100, 1) if avail else None
    qk = [k for k in factors if k != "hype" and factors[k]["score"] is not None]
    quality = round(sum(factors[k]["points"] for k in qk) / sum(w[k] for k in qk) * 100, 1) if qk else None
    # conflicts (same signal, opposite sourced values)
    conflicts, by_sig = [], {}
    for e in evs:
        if e.get("signal") and e.get("value") is not None and e.get("source_url"):
            by_sig.setdefault(e["signal"], []).append(e)
    for sg, es in sorted(by_sig.items()):
        if any(e["value"] > 0 for e in es) and any(e["value"] < 0 for e in es):
            conflicts.append({"signal": sg, "claims": [_fmt(e) for e in es][:4]})
    bench = benchmark(snap, peers)
    cl = build_checklist(snap=snap, evidence=evs, hints=hints, attempts=attempts, category=prim, stage=stage,
                         peers_n=len(peers), conflicts=conflicts, handle=handle, social=social)
    if social and social.get("has_sorsa_pack") and cl["identity"]["status"] == "FAILED" \
            and not any(c.get("type") == "identity" for c in social.get("conflicts") or []):
        from .checklist import social_checks
        social = dict(social, conflicts=list(social.get("conflicts") or []) + [
            {"type": "identity", "detail": "Sorsa returned data for this handle, but the independent identity check FAILED"}],
            unverified=list(social.get("unverified") or []) + [
            "project identity not independently verified — Sorsa data may describe the wrong account"])
        for k, v in social_checks(social).items():
            cl[k] = dict(v, label=CHECKS[k], mandatory=False)
    miss = missing_mandatory(cl)
    clc = cl_coverage(cl)
    # ---- confidence (0..1, label from the number only) ----
    sc = clamp((snap or {}).get("confidence") or 0.0)
    feeder = [e for e in evs if e.get("collected_by") == "feeder"]
    evq = (sum(1 for e in feeder if e.get("verified")) / len(feeder)) if feeder else 0.0
    rel = [SOURCE_RELIABILITY.get(e.get("collected_by"), 0.5) for e in evs if e.get("source_url")]
    srel = sum(rel) / len(rel) if rel else 0.0
    parts = {"hype_snapshot": round(sc, 3), "checklist_coverage": clc, "evidence_quality": round(evq, 3),
             "source_reliability": round(srel, 3), "contradictions": round(1 - clamp(0.5 * len(conflicts)), 3)}
    conf = 0.35 * sc + 0.25 * clc + 0.15 * evq + 0.10 * srel + 0.15 * parts["contradictions"]
    reasons = list(((snap or {}).get("evaluation") or {}).get("confidence_reasons") or [])
    if len(miss) >= 2 or (snap or {}).get("confidence_label") == "Low" or not snap:
        conf = min(conf, LOW_CAP)
    elif miss or conflicts:
        conf = min(conf, MEDIUM_CAP)
    conf = round(clamp(conf), 3)
    label = confidence_label(conf)
    if miss:
        reasons.append("mandatory checks not verified: " + ", ".join(CHECKS[k] for k in miss))
    if conflicts:
        reasons.append(f"{len(conflicts)} unresolved contradiction(s)")
    if bench["status"] != "ok":
        reasons.append(f"insufficient benchmark data ({bench['n']} peer(s))")
    # ---- verdict / state ----
    manual = []
    if cl["identity"]["status"] == "FAILED":
        manual.append("identity not verified: " + cl["identity"]["reason"])
    sim = [e for e in evs if (e.get("data") or {}).get("similar")]
    if sim:
        manual.append(f"{len(sim)} image(s) visually similar to reference works — needs side-by-side human review")
    if _sig(evs, "copy_evidence"):
        manual.append("sourced claim of copying present")
    est = tier_for(overall, tiers) if overall is not None else None
    if est is not None:   # hype cannot force quality: at most one tier better than the quality tier; T2 max w/o quality
        qcap = (tier_for(quality, tiers) - 1) if quality is not None else 2
        est = max(est, qcap, 1)
        if est == 1 and (quality is None or tier_for(quality, tiers) != 1):
            est = 2   # Tier 1 needs Tier-1 quality on its own, never hype alone
    if manual:
        verdict, status = MVR, "INCOMPLETE"
    elif miss or overall is None or label == "Low":
        verdict, status = NMR, "INCOMPLETE"
    else:
        verdict, status = "Tier", "FINALIZED"
    tier = est if status == "FINALIZED" else None
    raw_tier, hyst = tier, None
    if tier and previous and previous.get("tier") and previous["tier"] != tier and previous.get("version") == RESEARCH_VERSION:
        lo_new = {t: lo for lo, t in tiers}[min(tier, previous["tier"])]
        if abs(overall - lo_new) < HYSTERESIS and abs(tier - previous["tier"]) == 1:
            hyst = f"kept Tier {previous['tier']}: score {overall} is within {HYSTERESIS} of the {lo_new:g} boundary"
            tier = previous["tier"]
    # ---- change explanation ----
    change = None
    if previous:
        same_version = previous.get("version") == RESEARCH_VERSION and previous.get("hype_version") == ALGORITHM_VERSION
        new_ids = sorted({e["id"] for e in evs if e.get("id") and e["id"] > (previous.get("max_evidence_id") or 0)})
        deltas = sorted(((k, (factors[k]["points"] or 0) - (((previous.get("factors") or {}).get(k) or {}).get("points") or 0))
                         for k in factors), key=lambda kv: -abs(kv[1]))
        po = previous.get("overall_score_internal")
        change = {"from": previous.get("verdict_text"), "to": None,
                  "score_delta": round(overall - po, 1) if (overall is not None and po is not None) else None,
                  "comparable": same_version,
                  "cause": ("same algorithm; " + (f"{len(new_ids)} new evidence item(s)" if new_ids else "NO new evidence"))
                           if same_version else f"algorithm changed ({previous.get('version')} → {RESEARCH_VERSION}); scores not directly comparable",
                  "drivers": [f"{FACTORS[k]}: {d:+.1f} pts" for k, d in deltas[:3] if abs(d) >= 0.5],
                  "new_evidence_ids": new_ids[:20]}
    supporting = [_fmt(e) for e in evs if e.get("verified") and e.get("source_url")]
    positives = [_fmt(e) for e in evs if e.get("source_url") and (e.get("value") or 0) > 0 and e["kind"] != "assumption"]
    risks = [_fmt(e) for e in evs if e.get("source_url") and (e.get("value") or 0) < 0]
    risks += [{"claim": f, "url": None, "source": "derived from sampled X data", "kind": "indicator"} for f in (snap or {}).get("flags") or []]
    judgments = [_fmt(e) for e in evs if e["kind"] == "judgment" and e.get("source_url")]
    if vr:
        judgments += [{"claim": f"{c['check']}: {c['judgment']:.2f} — {c['notes'][:120]}", "url": (vr['images'][0]['url'] if vr.get('images') else None),
                       "source": vr["reviewer"], "kind": "judgment"} for c in vr.get("checks") or []]
    unverified = [{"claim": e["claim"], "reason": "no source URL" if not e.get("source_url") else e["kind"]}
                  for e in evs if not e.get("source_url") or e["kind"] == "assumption"]
    missing = [f"{CHECKS[k]}: {cl[k]['status']} — {cl[k]['reason']}" for k in cl if cl[k]["status"] in ("FAILED", "PARTIAL")]
    missing += [f"{FACTORS[k]}: {factors[k]['why']}" for k in factors if factors[k]["score"] is None]
    missing += [f"{u['source']}: {u['reason']}" for u in (unavailable or [])]
    why_tier = None
    shown = tier or est
    if shown and overall is not None:
        bounds = sorted(tiers, reverse=True)
        upper = next((lo for lo, t in bounds if t == shown - 1), None)
        lower = next((lo for lo, t in bounds if t == shown), 0)
        avail_f = [k for k in factors if factors[k]["score"] is not None]
        weakest = min(avail_f, key=lambda k: factors[k]["score"]); strongest = max(avail_f, key=lambda k: factors[k]["score"])
        why_tier = {"not_higher": (f"{upper - overall:.1f} pts below Tier {shown-1} ({upper:g}); weakest factor: {FACTORS[weakest]}"
                                   if upper else "already top tier"),
                    "not_lower": f"{overall - lower:.1f} pts above the Tier {shown} floor ({lower:g}); strongest factor: {FACTORS[strongest]}",
                    "quality_cap": (f"capped by quality ({quality}) — hype cannot force a higher tier" if est and tier_for(overall, tiers) < est else None)}
    not_final = None if status == "FINALIZED" else (
        "; ".join(manual) if manual else
        ("mandatory checks incomplete: " + ", ".join(f"{CHECKS[k]} ({cl[k]['status']}: {cl[k]['reason']})" for k in miss)) if miss
        else "confidence Low" if label == "Low" else "no scoreable evidence")
    sources_checked = sorted({e["source_url"] for e in evs if e.get("source_url")})
    verdict_text = f"Tier {tier}" if tier else verdict
    if change:
        change["to"] = verdict_text
    show_precise = status == "FINALIZED"
    return {"version": RESEARCH_VERSION, "hype_version": ALGORITHM_VERSION, "algorithm": f"{ALGORITHM_VERSION}+{RESEARCH_VERSION}",
            "status": status, "verdict": verdict, "verdict_text": verdict_text, "tier": tier, "raw_tier": raw_tier,
            "suggested_tier": est, "hysteresis": hyst, "overall_score": overall if show_precise else None,
            "overall_score_internal": overall, "hype_score": (snap or {}).get("hype_score"),
            "hype_tier": (snap or {}).get("tier"), "hype_snapshot_id": (snap or {}).get("id"),
            "quality_score": quality if show_precise else None, "quality_score_internal": quality,
            "coverage": clc, "profile": prim, "stage": stage, "mint_type": category.get("mint_type"),
            "secondary_categories": category.get("secondary") or [], "confidence": conf, "confidence_label": label,
            "confidence_parts": parts, "confidence_reasons": reasons, "factors": factors, "checklist": cl,
            "missing_mandatory": miss, "not_finalizable_reason": not_final, "sources_checked": sources_checked[:40],
            "n_sources_checked": len(sources_checked), "attempts": attempts[-40:],
            "supporting_evidence": supporting[:25], "positives": positives[:15], "risks": risks[:15],
            "judgments": judgments[:10], "unverified_claims": unverified[:20], "missing_info": missing,
            "manual_verification": manual, "conflicts": conflicts, "why_tier": why_tier, "change": change,
            "benchmark": bench, "peers": bench if bench["status"] == "ok" else None,
            "max_evidence_id": max([e.get("id") or 0 for e in evs] or [0]),
            "signals": separate_signals(snap, quality, conf, label, status, not_final, social)}


def separate_signals(snap, quality, conf, label, status, not_final, social) -> dict:
    """r2.1: the five signals reported SEPARATELY. Only popularity/quality/confidence feed the tier (unchanged);
    Smart Account Signal and Sorsa credibility are informational until validated on historical outcomes."""
    sg = social or {}
    return {"overall_popularity": {"hype_score": (snap or {}).get("hype_score"), "hype_tier": (snap or {}).get("tier")},
            "smart_account_signal": sg.get("smart_account_signal") or {"label": "unavailable", "score": None},
            "sorsa_social_credibility": sg.get("sorsa_social_credibility") or {"label": "unavailable", "score": None},
            "project_quality": quality, "research_confidence": {"value": conf, "label": label},
            "matched_accounts": sg.get("matched_accounts") or [], "notable_unhistoried_followers": sg.get("notable_unhistoried_followers") or [],
            "social_conflicts": sg.get("conflicts") or [], "unverified": sg.get("unverified") or [],
            "unavailable_reasons": sg.get("unavailable_reasons") or [],
            "early_attention": bool(sg.get("early_attention")), "early_attention_reason": sg.get("early_attention_reason"),
            "ready_for_final_tier": status == "FINALIZED", "not_final_reason": not_final,
            "version": "s1.0", "affects_tier": False, "weights_changed": False}
