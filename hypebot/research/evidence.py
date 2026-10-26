"""Validation of research payloads submitted by the external feeder (routine).

Rules enforced in code:
* every claim needs a source URL to count as a *verified observation*; claims without one are stored
  with verified=False and are never shown as facts (report lists them under "Unverified claims").
* kind=judgment (e.g. agent art review) is stored as labeled qualitative judgment, never as fact.
* signal keys and value ranges are whitelisted; unknown signals are rejected.
"""
from __future__ import annotations
import re
from datetime import datetime

SOURCE = "grokbot-research"
SECTIONS = {"identity", "x_interest", "team", "art", "market", "category", "community"}
KINDS = {"observation", "assumption", "judgment"}
CATEGORIES = ["artist_led", "pfp", "generative_art", "gaming", "brand_ip", "membership_utility",
              "derivative", "experimental", "unknown"]
MINT_TYPES = {"free", "paid", "unknown"}
STAGES = {"pre_mint", "minting", "post_mint", "unknown"}
# signal -> (section, allowed value range). Positive = supports quality, negative = risk.
SIGNALS = {
    "official_link": ("identity", (0, 1)),            # data: {from_url, to_url}; value 1 = link present
    "team_prior_collection": ("team", (-1, 1)),       # +: delivered prior collection with collector interest
    "team_delivery_record": ("team", (-1, 1)),
    "team_known_identity": ("team", (0, 1)),          # doxxed/known artist (anonymous is NOT negative)
    "team_misleading_claim": ("team", (-1, 0)),
    "team_abandonment": ("team", (-1, 0)),
    "copy_evidence": ("art", (-1, 0)),                # credible, sourced evidence of copying
    "art_quality": ("art", (0, 1)),                   # judgment
    "art_originality": ("art", (0, 1)),               # judgment
    "art_consistency": ("art", (0, 1)),               # judgment
    "respected_account_engaged": ("x_interest", (0, 1)),  # collector/artist/alpha caller with NFT history
    "giveaway_driven": ("x_interest", (-1, 0)),
    "paid_promotion": ("x_interest", (-1, 0)),
    "utility_delivered": ("market", (0, 1)),
    "milestone": ("market", (-1, 1)),                 # mint sold out (+) / mint postponed (-) etc.
    "market_fact": ("market", (-1, 1)),               # supply, mint price ... in data; value optional
    "community_quality": ("community", (-1, 1)),
}
URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.I)


VISUAL_REVIEWER = "grokbot-feeder-visual"
VISUAL_CHECKS = {"quality", "consistency", "variation", "originality_indicators", "suspicious_similarity"}
ATTEMPT_OUTCOMES = {"success", "temporary_error", "not_found", "blocked", "invalid"}
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _file_sha(path):
    import hashlib, os
    try:
        if not path or not os.path.isfile(path):
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1 << 16), b""):
                h.update(b)
        return h.hexdigest()
    except OSError:
        return None


def validate_visual_review(v) -> dict | None:
    """Visual review record by the feeder agent. Each image counts as 'viewed' only if its local file
    exists on the box and its sha256 matches (proves it was actually downloaded)."""
    if v is None:
        return None
    if not isinstance(v, dict) or v.get("reviewer") != VISUAL_REVIEWER:
        raise ResearchPayloadError(f"visual_review.reviewer must be '{VISUAL_REVIEWER}'")
    imgs = []
    for i, im in enumerate(v.get("images") or []):
        f = f"visual_review.images[{i}]"
        u = _url((im or {}).get("url"))
        sha = str((im or {}).get("sha256", "")).lower()
        if not u or not SHA_RE.match(sha):
            raise ResearchPayloadError(f"{f}: url and 64-hex sha256 required")
        actual = _file_sha(im.get("local_path"))
        imgs.append({"url": u, "local_path": im.get("local_path"), "sha256": sha, "official": bool(im.get("official", True)),
                     "viewed": bool(im.get("viewed", False)), "source_url": _url(im.get("source_url")),
                     "file_verified": actual == sha, "file_status": "ok" if actual == sha else ("sha mismatch" if actual else "file missing")})
    checks = []
    for i, c in enumerate(v.get("checks") or []):
        f = f"visual_review.checks[{i}]"
        if (c or {}).get("check") not in VISUAL_CHECKS:
            raise ResearchPayloadError(f"{f}.check must be one of {sorted(VISUAL_CHECKS)}")
        j = c.get("judgment")
        if not isinstance(j, (int, float)) or isinstance(j, bool) or not 0 <= j <= 1:
            raise ResearchPayloadError(f"{f}.judgment must be a number 0..1 (1 = good / no concern)")
        checks.append({"check": c["check"], "judgment": float(j), "notes": str(c.get("notes") or "")[:600]})
    return {"reviewer": VISUAL_REVIEWER, "reviewed_at": _ts(v.get("reviewed_at"), "visual_review.reviewed_at"),
            "images": imgs, "checks": checks, "download_failures": [
                {"url": str(x.get("url"))[:500], "reason": str(x.get("reason", ""))[:200]} for x in v.get("download_failures") or [] if isinstance(x, dict)]}


def validate_attempts(items) -> list:
    out = []
    for i, a in enumerate(items or []):
        if not isinstance(a, dict) or a.get("outcome") not in ATTEMPT_OUTCOMES or not a.get("source"):
            raise ResearchPayloadError(f"attempts[{i}] needs source, target and outcome in {sorted(ATTEMPT_OUTCOMES)}")
        out.append({"check": str(a.get("check") or "other")[:30], "source": str(a["source"])[:80],
                    "target": str(a.get("target") or a["source"])[:1000], "outcome": a["outcome"],
                    "error": str(a.get("error") or "")[:500] or None})
    return out


class ResearchPayloadError(ValueError):
    pass


def _url(v):
    return v if isinstance(v, str) and URL_RE.match(v) else None


def _ts(v, f):
    if v is None:
        return None
    try:
        datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise ResearchPayloadError(f"{f}: invalid timestamp {v!r}")
    return str(v)


def normalize_claim(c: dict, i: int) -> dict:
    f = f"claims[{i}]"
    if not isinstance(c, dict) or not str(c.get("claim", "")).strip():
        raise ResearchPayloadError(f"{f}.claim text is required")
    sec = c.get("section")
    sig = c.get("signal")
    if sig is not None:
        if sig not in SIGNALS:
            raise ResearchPayloadError(f"{f}.signal {sig!r} unknown; allowed: {sorted(SIGNALS)}")
        sec = sec or SIGNALS[sig][0]
    if sec not in SECTIONS:
        raise ResearchPayloadError(f"{f}.section must be one of {sorted(SECTIONS)}")
    kind = c.get("kind", "observation")
    if kind not in KINDS:
        raise ResearchPayloadError(f"{f}.kind must be one of {sorted(KINDS)}")
    val = c.get("value")
    if val is not None:
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise ResearchPayloadError(f"{f}.value must be a number")
        lo, hi = SIGNALS[sig][1] if sig else (-1, 1)
        if not lo <= val <= hi:
            raise ResearchPayloadError(f"{f}.value {val} outside [{lo},{hi}] for {sig}")
    if sig in ("art_quality", "art_originality", "art_consistency") and kind != "judgment":
        kind = "judgment"   # art assessments are never facts
    src = _url(c.get("source_url"))
    eng = c.get("engagement")
    if eng is not None and not isinstance(eng, dict):
        raise ResearchPayloadError(f"{f}.engagement must be an object")
    return {"section": sec, "kind": kind, "claim": str(c["claim"])[:2000], "source_url": src,
            "source_name": (c.get("source_name") or None), "account": (c.get("account") or "").lstrip("@") or None,
            "posted_at": _ts(c.get("posted_at"), f + ".posted_at"), "engagement": eng,
            "why": c.get("why"), "signal": sig, "value": float(val) if val is not None else None,
            "data": c.get("data") if isinstance(c.get("data"), dict) else None,
            # verified = sourced observation; judgments/assumptions are never "verified facts"
            "verified": bool(src) and kind == "observation"}


def validate_research_payload(p: dict, job_id: int) -> dict:
    if not isinstance(p, dict) or p.get("source") != SOURCE:
        raise ResearchPayloadError(f"source must be '{SOURCE}'")
    if p.get("job_id") != job_id:
        raise ResearchPayloadError(f"job_id mismatch: payload {p.get('job_id')} vs {job_id}")
    if not p.get("collected_at"):
        raise ResearchPayloadError("collected_at required")
    _ts(p["collected_at"], "collected_at")
    ident = p.get("identity") or {}
    if not isinstance(ident, dict):
        raise ResearchPayloadError("identity must be an object")
    cat = p.get("category") or {}
    prim = cat.get("primary", "unknown")
    if prim not in CATEGORIES:
        raise ResearchPayloadError(f"category.primary must be one of {CATEGORIES}")
    secondary = [c for c in cat.get("secondary", []) if c in CATEGORIES]
    mt = cat.get("mint_type", "unknown"); st = cat.get("stage", "unknown")
    if mt not in MINT_TYPES or st not in STAGES:
        raise ResearchPayloadError(f"category.mint_type in {sorted(MINT_TYPES)}, stage in {sorted(STAGES)}")
    claims = [normalize_claim(c, i) for i, c in enumerate(p.get("claims") or [])]
    imgs = []
    for i, im in enumerate((p.get("art") or {}).get("images") or []):
        u = _url((im or {}).get("url"))
        if not u:
            raise ResearchPayloadError(f"art.images[{i}].url must be an http(s) URL")
        role = im.get("role", "project")
        if role not in ("project", "reference"):
            raise ResearchPayloadError(f"art.images[{i}].role must be project|reference")
        imgs.append({"url": u, "role": role, "label": im.get("label"), "source_url": _url(im.get("source_url"))})
    unavailable = [{"source": str(u.get("source")), "reason": str(u.get("reason", ""))[:300]}
                   for u in p.get("unavailable") or [] if isinstance(u, dict) and u.get("source")]
    links = {k: _url(ident.get(k)) for k in ("official_x", "website", "discord")}
    mkts = []
    for m in ident.get("marketplaces") or []:
        if isinstance(m, dict) and m.get("name") in ("opensea", "magiceden", "magiceden_sol", "other"):
            mkts.append({"name": m["name"], "url": _url(m.get("url")), "slug": m.get("slug"),
                         "chain": m.get("chain"), "contract": m.get("contract")})
    people = [{"role": x.get("role", "team"), "handle": (x.get("handle") or "").lstrip("@") or None,
               "url": _url(x.get("url"))} for x in (ident.get("people") or []) if isinstance(x, dict)]
    return {"collected_at": p["collected_at"], "links": links, "marketplaces": mkts, "people": people,
            "category": {"primary": prim, "secondary": secondary, "mint_type": mt, "stage": st,
                         "source_url": _url(cat.get("source_url"))},
            "claims": claims, "images": imgs, "unavailable": unavailable,
            "visual_review": validate_visual_review(p.get("visual_review")),
            "attempts": validate_attempts(p.get("attempts")),
            "notes": str(p.get("notes") or "")[:2000]}
