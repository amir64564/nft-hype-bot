"""Dynamic research checklist (r2.0). Each check: VERIFIED | PARTIAL | FAILED | NOT_APPLICABLE with
evidence, sources, timestamp and reason. Mandatory checks depend on category + stage (docs/RESEARCH.md).
A job can only be FINALIZED when every mandatory check is VERIFIED or NOT_APPLICABLE."""
from __future__ import annotations
from datetime import datetime, timezone

VERIFIED, PARTIAL, FAILED, NA = "VERIFIED", "PARTIAL", "FAILED", "NOT_APPLICABLE"
MIN_VIEWED_IMAGES = 3
MIN_PEERS = 3

CHECKS = {
    "identity": "Identity & official X verification",
    "official_x": "Official X posts & history",
    "team": "Founder / artist / team verification",
    "track_record": "Previous collections / track record",
    "artwork": "Artwork quality & originality (visual review)",
    "community": "Community activity & engagement quality",
    "organic": "Organic vs task-driven / promotional activity",
    "mint_details": "Mint details, supply, price, stage",
    "marketplace": "Marketplace & trading activity",
    "benchmark": "Historical comparison & peer benchmark",
    "conflicts": "Conflicting claims & missing evidence",
    "sorsa_social_graph": "Sorsa social graph (crypto followers)",
    "smart_follower_overlap": "Smart-follower overlap (tracked discoverers)",
}
# r2.1 advisory checks: shown in the report, never mandatory, excluded from coverage/confidence so that
# adding Sorsa does not move existing confidence or tiers (weights unchanged pending historical validation).
ADVISORY = {"sorsa_social_graph", "smart_follower_overlap"}
BASE_MANDATORY = {"identity", "official_x", "community", "organic"}
CATEGORY_MANDATORY = {
    "artist_led": {"artwork", "team"}, "generative_art": {"artwork"}, "pfp": {"artwork"}, "derivative": {"artwork"},
    "gaming": {"team"}, "membership_utility": {"team"}, "brand_ip": {"identity"}, "experimental": set(), "unknown": set(),
}
STAGE_MANDATORY = {"pre_mint": {"mint_details"}, "minting": {"mint_details"}, "post_mint": {"marketplace"}, "unknown": set()}


def mandatory_for(category: str, stage: str) -> set:
    return BASE_MANDATORY | CATEGORY_MANDATORY.get(category, set()) | STAGE_MANDATORY.get(stage, set())


def _now():
    return datetime.now(timezone.utc).isoformat()


def _c(status, reason, evidence=None, sources=None, counts=None):
    return {"status": status, "reason": reason, "evidence": (evidence or [])[:6], "sources": sorted(set(sources or []))[:8],
            "counts": counts or {}, "checked_at": _now()}


def _sig(evs, *names, verified=True):
    return [e for e in evs if e.get("signal") in names and (e.get("verified") or not verified)]


def build_checklist(*, snap, evidence, hints, attempts, category, stage, peers_n, conflicts, handle, social=None) -> dict:
    evs = list(evidence)
    m = (snap or {}).get("metrics") or {}
    out = {}
    # identity
    links = _sig(evs, "official_link")
    good = [e for e in links if (e.get("value") or 0) >= 1]
    bad = [e for e in links if (e.get("value") or 0) < 1]
    pairs = {tuple(sorted([(e.get("data") or {}).get("from_kind", "?"), (e.get("data") or {}).get("to_kind", "?")])) for e in good}
    src = [e["source_url"] for e in links if e.get("source_url")]
    if bad:
        out["identity"] = _c(FAILED, f"{len(bad)} sourced link(s) point to a different/unlinked account", [e["claim"] for e in bad], src)
    elif len(pairs) >= 2:
        out["identity"] = _c(VERIFIED, f"{len(pairs)} independent verified cross-links", [e["claim"] for e in good], src)
    elif good:
        out["identity"] = _c(PARTIAL, "only one verified cross-link (need ≥2 independent, e.g. X↔website and marketplace→X)",
                             [e["claim"] for e in good], src)
    else:
        out["identity"] = _c(FAILED, "no verified cross-link between official X, website, marketplace")
    # official X
    n = m.get("sample_tweets")
    counts = {"official_posts_examined": n or 0, "posts_supplied": m.get("posts_supplied"),
              "mentions_examined": m.get("mentions_sample") or 0, "unique_mention_authors": m.get("mention_unique_authors") or 0,
              "mention_count_buckets_hours": m.get("mention_counts_hours"), "top_post_share": m.get("top_post_share")}
    xsrc = [f"https://x.com/{handle}"] if snap else []
    if not snap:
        out["official_x"] = _c(FAILED, "no X snapshot (X data not collected)", counts=counts)
    elif (n or 0) >= 8:
        conc = (m.get("top_post_share") or 0) > 0.5
        out["official_x"] = _c(PARTIAL if conc else VERIFIED,
                               f"{n} official posts examined" + (f"; {m['top_post_share']:.0%} of engagement in one post" if conc else ""),
                               sources=xsrc, counts=counts)
    elif (n or 0) >= 1:
        out["official_x"] = _c(PARTIAL, f"only {n} official post(s) examined (need ≥8)", sources=xsrc, counts=counts)
    else:
        out["official_x"] = _c(FAILED, "0 official posts examined", sources=xsrc, counts=counts)
    # team / track record
    team_v = [e for e in evs if e.get("section") == "team" and e.get("verified")]
    team_u = [e for e in evs if e.get("section") == "team" and not e.get("verified")]
    known = _sig(evs, "team_known_identity")
    prior = _sig(evs, "team_prior_collection", "team_delivery_record")
    tsrc = [e["source_url"] for e in team_v if e.get("source_url")]
    if known or (team_v and any((e.get("value") or 0) > 0 for e in team_v)):
        out["team"] = _c(VERIFIED, f"{len(team_v)} sourced team observation(s)", [e["claim"] for e in team_v], tsrc)
    elif team_u or team_v:
        out["team"] = _c(PARTIAL, "team claims found but not verified by a source", [e["claim"] for e in team_u + team_v])
    else:
        out["team"] = _c(FAILED, "research did not find verifiable founder/artist information (anonymous is not negative)")
    if prior:
        out["track_record"] = _c(VERIFIED, f"{len(prior)} sourced track-record observation(s)", [e["claim"] for e in prior],
                                 [e["source_url"] for e in prior])
    elif known:
        out["track_record"] = _c(PARTIAL, "team identified, but research did not find a prior collection (this is NOT 'no experience')")
    else:
        out["track_record"] = _c(FAILED, "research did not find prior collections or delivery history (not evidence of no experience)")
    # artwork: visual review record required
    vr = (hints or {}).get("visual_review") or {}
    viewed = [i for i in vr.get("images") or [] if i.get("official") and i.get("viewed") and i.get("file_verified")]
    claimed = [i for i in vr.get("images") or [] if i.get("viewed") and not i.get("file_verified")]
    vchecks = {c["check"] for c in vr.get("checks") or []}
    img_att = [a for a in attempts if a.get("check") == "artwork" or a.get("source") == "image"]
    img_fail = [a for a in img_att if a["outcome"] != "success"]
    hashed = (hints or {}).get("image_hashes") or []
    acounts = {"images_viewed_verified": len(viewed), "images_claimed_unverified_file": len(claimed),
               "visual_checks": sorted(vchecks), "images_hashed": len(hashed), "image_fetch_failures": len(img_fail)}
    if len(viewed) >= MIN_VIEWED_IMAGES and {"quality", "consistency"} <= vchecks:
        out["artwork"] = _c(VERIFIED, f"{len(viewed)} official images downloaded and visually reviewed by {vr.get('reviewer')}",
                            [c["check"] + ": " + c["notes"][:80] for c in vr.get("checks", [])], [i["url"] for i in viewed], acounts)
    elif viewed:
        out["artwork"] = _c(PARTIAL, f"only {len(viewed)} image(s) visually reviewed (need ≥{MIN_VIEWED_IMAGES} + quality & consistency checks)",
                            sources=[i["url"] for i in viewed], counts=acounts)
    elif img_fail and not hashed:
        out["artwork"] = _c(FAILED, f"{len(img_fail)} image download(s) failed; no image reviewed", counts=acounts)
    elif hashed:
        out["artwork"] = _c(PARTIAL, "images only perceptual-hashed — hashing is NOT an originality/quality review; no visual review record",
                            counts=acounts)
    else:
        out["artwork"] = _c(FAILED, "no visual review record and no images", counts=acounts)
    # community / organic
    ns, ua = m.get("mentions_sample") or 0, m.get("mention_unique_authors") or 0
    ccounts = {"mentions_examined": ns, "unique_authors": ua, "independent_authors": m.get("independent_authors"),
               "mentions_7d_total": m.get("mentions_7d_total")}
    if ns >= 50 and ua >= 25 and m.get("mentions_7d_total") is not None:
        out["community"] = _c(VERIFIED, f"{ns} mentions by {ua} unique authors examined + 7-day counts", sources=xsrc, counts=ccounts)
    elif ns >= 10:
        out["community"] = _c(PARTIAL, f"small discussion sample ({ns} mentions, {ua} authors)", sources=xsrc, counts=ccounts)
    else:
        out["community"] = _c(FAILED, "fewer than 10 mentions examined", counts=ccounts)
    promo = m.get("promo_or_low_effort_ratio")
    og = {"promo_or_low_effort_ratio": promo, "wallet_address_ratio": m.get("wallet_address_ratio"),
          "duplicate_text_ratio": m.get("duplicate_text_ratio"), "top_thread_share": m.get("top_thread_share"),
          "giveaway_signals": len(_sig(evs, "giveaway_driven")), "paid_promo_signals": len(_sig(evs, "paid_promotion"))}
    if ns >= 25 and promo is not None:
        out["organic"] = _c(VERIFIED, f"{promo:.0%} of {ns} mentions low-effort/promo/wallet; "
                                      f"{og['giveaway_signals']} giveaway and {og['paid_promo_signals']} paid-promo sourced signals", counts=og)
    elif ns >= 10:
        out["organic"] = _c(PARTIAL, "mention sample too small for a reliable organic/promo split", counts=og)
    else:
        out["organic"] = _c(FAILED, "no usable mention sample", counts=og)
    # mint details
    mint = [e for e in evs if e.get("verified") and e.get("signal") in ("market_fact", "milestone") and e.get("collected_by") != "bot:market"]
    if len(mint) >= 2:
        out["mint_details"] = _c(VERIFIED, f"{len(mint)} sourced mint/stage facts", [e["claim"] for e in mint], [e["source_url"] for e in mint])
    elif mint:
        out["mint_details"] = _c(PARTIAL, "only one sourced mint/stage fact", [e["claim"] for e in mint], [e["source_url"] for e in mint])
    else:
        out["mint_details"] = _c(FAILED, "no sourced supply/price/date/stage facts")
    # marketplace
    mk = [e for e in evs if e.get("collected_by") == "bot:market" and e.get("section") == "market"]
    mk_fail = [a for a in attempts if a.get("check") == "marketplace" and a["outcome"] != "success"]
    if stage in ("pre_mint",) and not mk:
        out["marketplace"] = _c(NA, "pre-mint: no trading expected (not a failure)")
    elif mk:
        out["marketplace"] = _c(VERIFIED, f"{len(mk)} marketplace observations", [e["claim"] for e in mk], [e["source_url"] for e in mk])
    elif mk_fail:
        out["marketplace"] = _c(FAILED, f"{len(mk_fail)} marketplace fetch attempt(s) failed",
                                [f"{a['source']}: {a['outcome']}" for a in mk_fail])
    else:
        out["marketplace"] = _c(FAILED if stage == "post_mint" else PARTIAL, "no marketplace collection identified")
    # benchmark
    if peers_n >= MIN_PEERS:
        out["benchmark"] = _c(VERIFIED, f"{peers_n} comparable peers (same category & stage)", counts={"peers": peers_n})
    else:
        out["benchmark"] = _c(PARTIAL, f"insufficient benchmark data ({peers_n} peer(s); need ≥{MIN_PEERS})", counts={"peers": peers_n})
    # conflicts
    if conflicts:
        out["conflicts"] = _c(PARTIAL, f"{len(conflicts)} unresolved contradiction(s)", [c["signal"] for c in conflicts])
    else:
        out["conflicts"] = _c(VERIFIED, "no contradictory sourced claims found")
    out.update(social_checks(social))
    mand = mandatory_for(category, stage)
    for k, v in out.items():
        v["label"] = CHECKS[k]
        v["mandatory"] = k in mand
    return out


def missing_mandatory(cl: dict) -> list:
    return [k for k, v in cl.items() if v["mandatory"] and v["status"] not in (VERIFIED, NA)]


def coverage(cl: dict) -> float:
    """Share of checks VERIFIED (PARTIAL = half, N/A excluded)."""
    vals = [1.0 if v["status"] == VERIFIED else 0.5 if v["status"] == PARTIAL else 0.0
            for k, v in cl.items() if v["status"] != NA and k not in ADVISORY]
    return round(sum(vals) / len(vals), 3) if vals else 0.0


def social_checks(social: dict | None) -> dict:
    """Advisory checks from the Smart Account Signal (accounts.smart_signal). No data = NOT_APPLICABLE (no penalty)."""
    sg = social or {}
    cred = sg.get("sorsa_social_credibility") or {"label": "unavailable"}
    conf = sg.get("conflicts") or []
    out = {}
    if cred.get("label") == "unavailable":
        out["sorsa_social_graph"] = _c(NA, "Sorsa data unavailable (" + "; ".join((cred.get("reasons") or [])[:2])[:120]
                                       + ") — not a penalty")
    else:
        got = [k for k in ("score", "crypto_followers") if cred.get(k) is not None]
        reason = (f"Sorsa score {cred.get('score')}, {cred.get('crypto_followers')} crypto followers "
                  f"({cred.get('influencers')} influencers / {cred.get('projects')} projects / {cred.get('vcs')} VCs)")
        if conf:
            out["sorsa_social_graph"] = _c(PARTIAL, f"{len(conf)} Sorsa-vs-independent conflict(s) recorded (unresolved): "
                                           + "; ".join(c["detail"][:80] for c in conf[:2]), counts={"conflicts": len(conf)})
        elif len(got) == 2:
            out["sorsa_social_graph"] = _c(VERIFIED, reason + " — one source family", sources=["https://api.sorsa.io/v3"])
        else:
            out["sorsa_social_graph"] = _c(PARTIAL, reason + " (partial pack)")
    ss = sg.get("smart_account_signal") or {"label": "unavailable"}
    matched = sg.get("matched_accounts") or []
    cnt = {"matched": len(matched), "independent_credible": ss.get("independent_credible_accounts", 0)}
    if ss["label"] == "unavailable":
        out["smart_follower_overlap"] = _c(NA, "no follower/mention data to compare with tracked accounts")
    elif ss["label"] == "insufficient history":
        out["smart_follower_overlap"] = _c(NA, f"{len(matched)} tracked account(s) matched, but no account has recorded "
                                               "discovery outcomes yet (reliability provisional)", counts=cnt)
    elif any(len(m["source_families"]) >= 2 or m["source_families"] == ["independent"]
             for m in matched if m["class"] in ("small_discoverer", "large_influential") and m["reliability"] in ("medium", "high")):
        out["smart_follower_overlap"] = _c(VERIFIED, f"{cnt['independent_credible']} credible tracked account(s); at least one "
                                                     "confirmed by independent research", [m["handle"] for m in matched][:8], counts=cnt)
    else:
        out["smart_follower_overlap"] = _c(PARTIAL, f"{cnt['independent_credible']} credible match(es) in visible data "
                                                    "(Sorsa top-20 is incomplete; Sorsa-only matches are single-source)", counts=cnt)
    return out
