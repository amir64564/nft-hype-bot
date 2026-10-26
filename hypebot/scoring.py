"""Hype scoring engine, algorithm v2 (deterministic).

Each category function returns ``(score 0..1, quality 0..1)`` or ``None`` when its evidence was
not collected. Missing categories get neither full nor zero points: they are excluded from the
denominator and the result is marked provisional with its coverage disclosed. There is no
randomness, no wall-clock access and no AI-generated numbers anywhere in this module.
See docs/SCORING.md for the rules.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from datetime import datetime
from .config import DEFAULT_WEIGHTS, DEFAULT_TIERS, COMPONENT_LABELS, UNCOLLECTED_COMPONENTS

ALGORITHM_VERSION = "v2.1"   # v2.1: confidence label derived from the (capped) number only
LEGACY_VERSION = "v1-legacy"

# --- evidence thresholds (explicit, tested) ---
MIN_POSTS_ENGAGEMENT = 1      # below -> engagement category missing
MIN_POSTS_CONSISTENCY = 3     # below -> consistency category missing
FULL_POSTS = 10               # posts needed before engagement/consistency can reach full score
MIN_MENTIONS = 10             # mention sample needed for breadth/quality signals
STALE_HOURS = 72


HIGH_MIN, MEDIUM_MIN = 0.75, 0.5
LOW_CAP, MEDIUM_CAP = 0.49, 0.74


def confidence_label(value: float | None) -> str:
    """THE single mapping from confidence (0..1) to label, used everywhere."""
    if value is None or value != value:
        return "Low"
    v = max(0.0, min(1.0, float(value)))
    return "High" if v >= HIGH_MIN else "Medium" if v >= MEDIUM_MIN else "Low"


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def logscale(v, full):
    if v is None or v <= 0:
        return 0.0
    return clamp(math.log10(1 + v) / math.log10(1 + full))


def logrange(v, lo, hi):
    """Log curve: <=lo -> 0, >=hi -> 1."""
    if v is None or v <= lo:
        return 0.0
    return clamp(math.log10(v / lo) / math.log10(hi / lo))


def sample_cap(n):
    """Max category score allowed for n posts: 1 post -> 0.325, 5 -> 0.625, >=10 -> 1.0."""
    n = n or 0
    return 1.0 if n >= FULL_POSTS else clamp(0.25 + 0.75 * n / FULL_POSTS)


def _has(m, *keys):
    return all(m.get(k) is not None for k in keys)


def _wavg(parts):
    parts = [(w, v) for w, v in parts if v is not None]
    return sum(w * v for w, v in parts) / sum(w for w, _ in parts) if parts else None


def risk_indicators(m: dict) -> list[str]:
    """Human-readable risk indicators (NOT bot labels). Used for flags and confidence."""
    r = []
    if (m.get("duplicate_text_ratio") or 0) > 0.25:
        r.append(f"{m['duplicate_text_ratio']:.0%} of substantive mentions are duplicate (copy-pasted) text")
    if (m.get("wallet_address_ratio") or 0) > 0.2:
        r.append(f"{m['wallet_address_ratio']:.0%} of mentions are pasted wallet addresses (allowlist farming)")
    if (m.get("promo_or_low_effort_ratio") or 0) > 0.7 and (m.get("mentions_sample") or 0) >= MIN_MENTIONS:
        r.append(f"{m['promo_or_low_effort_ratio']:.0%} of mentions are low-effort/promo/wallet replies")
    if (m.get("repeat_author_ratio") or 0) > 0.6:
        r.append("Mentions concentrated in a few repeat accounts")
    if (m.get("top_thread_share") or 0) > 0.5:
        r.append(f"{m['top_thread_share']:.0%} of mentions are replies under one account's thread")
    if (m.get("new_account_ratio") or 0) > 0.5:
        r.append(f"{m['new_account_ratio']:.0%} of mentioning accounts are <30 days old")
    if (m.get("top_post_share") or 0) > 0.5 and (m.get("sample_tweets") or 0) >= 2:
        r.append(f"{m['top_post_share']:.0%} of official engagement comes from a single post")
    er = m.get("engagement_rate")
    if er is not None and er < 0.0005 and (m.get("followers") or 0) > 20_000:
        r.append(f"Very low engagement rate ({er:.3%}) for follower count")
    if m.get("unit_anomaly_posts"):
        r.append(f"{m['unit_anomaly_posts']} post(s) report more interactions than views — check metric units")
    return r


def community_risk_factor(m: dict) -> float:
    """0.7..1.0 multiplier applied ONLY to Community Discussion (where repetitive promo inflates volume)."""
    pen = 0.0
    dup = m.get("duplicate_text_ratio")
    if dup is not None and dup > 0.25:
        pen += min(0.15, (dup - 0.25) * 0.4)
    if (m.get("repeat_author_ratio") or 0) > 0.6:
        pen += 0.05
    if (m.get("top_thread_share") or 0) > 0.5:
        pen += min(0.1, (m["top_thread_share"] - 0.5) * 0.25)
    if (m.get("new_account_ratio") or 0) > 0.5:
        pen += 0.05
    return clamp(1 - pen, 0.7, 1.0)


def c_engagement(m):
    """Official-account post engagement only. Robust level = median and trimmed mean (top post dropped)."""
    n = m.get("sample_tweets") or 0
    if n < MIN_POSTS_ENGAGEMENT or not _has(m, "median_engagement"):
        return None
    level = math.sqrt(max(m["median_engagement"], 0) * max(m.get("trimmed_mean_engagement", m["median_engagement"]), 0))
    parts = [(0.45, logrange(level, 50, 5_000))]
    if m.get("median_views") is not None:
        parts.append((0.25, logrange(m["median_views"], 1_000, 40_000)))
    if m.get("engagement_rate") is not None:
        parts.append((0.2, clamp(m["engagement_rate"] / 0.10)))
    if m.get("conversation_ratio") is not None:
        parts.append((0.1, clamp(m["conversation_ratio"] / 0.25)))
    s = min(_wavg(parts), sample_cap(n))
    return clamp(s), clamp(n / FULL_POSTS)


def c_community(m):
    """Independent discussion: 7-day volume, breadth (unique/independent authors), substance."""
    vol = m.get("mentions_7d_total")
    ns = m.get("mentions_sample") or 0
    if vol is None and ns == 0:
        return None
    parts = []
    if vol is not None:
        parts.append((0.55, logrange(vol, 600, 25_000)))
        if m.get("mentions_peak_per_day") is not None:
            parts.append((0.1, logrange(m["mentions_peak_per_day"], 100, 5_000)))
    if ns >= MIN_MENTIONS:
        parts.append((0.15, clamp(m.get("mention_unique_authors", 0) / ns / 0.8)))
        parts.append((0.2, clamp((m.get("independent_authors") or 0) / ns / 0.5)))
    if not parts:
        return None
    s = _wavg(parts) * community_risk_factor(m)
    q = (0.6 if vol is not None else 0.0) + (0.4 if ns >= MIN_MENTIONS else 0.0)
    if vol is None:   # sample-only breadth cannot show volume: cap
        s = min(s, 0.4)
    return clamp(s), q


def c_growth(m, prev: dict | None = None, prev_days: float | None = None):
    """Momentum from mention velocity (counts endpoint) and, only when a comparable earlier
    observation exists, real follower growth. Never estimated from account age."""
    rate = m.get("mentions_recent_per_day")
    parts, q = [], 0.0
    vf = logrange(rate, 200, 5_000) if rate is not None else None
    if vf is not None:
        parts.append((0.5, vf)); q += 0.5
        if m.get("mention_velocity_change") is not None:
            parts.append((0.25, clamp(0.5 + m["mention_velocity_change"] / 2) * vf)); q += 0.2
    if prev and prev_days and prev_days >= 0.5 and _has(m, "followers") and prev.get("followers"):
        g = (m["followers"] - prev["followers"]) / prev["followers"] / prev_days * 7
        parts.append((0.25, clamp(0.5 + g / 0.2))); q += 0.3
    if m.get("intra_sample_engagement_change") is not None:
        # within-sample momentum (newer half vs older half of official posts), damped by absolute level
        damp = vf if vf is not None else logrange(m.get("median_engagement"), 50, 5_000)
        parts.append((0.15, clamp(0.5 + m["intra_sample_engagement_change"] / 2) * damp)); q += 0.1
    if not parts:
        return None
    return clamp(_wavg(parts)), clamp(q)


def c_influential(m):
    """Relevant influential accounts (>=10k followers or org-verified, with a substantive,
    non-promo mention). Counted per distinct conversation so one promoter's thread with many
    big repliers can't max the category: effective = min(relevant, 3 x distinct threads)."""
    if m.get("influential_relevant") is None:
        return None
    eff = min(m["influential_relevant"], 3 * (m.get("influential_threads") or 0))
    ns = m.get("mentions_sample") or 0
    return logscale(eff, 15), clamp(ns / 50) * clamp(m.get("author_followers_coverage") or 0)


def c_consistency(m):
    n = m.get("sample_tweets") or 0
    if n < MIN_POSTS_CONSISTENCY or not _has(m, "posts_per_week"):
        return None
    ppw = m["posts_per_week"]
    freq = clamp(ppw / 5) if ppw <= 21 else clamp(1 - (ppw - 21) / 60, 0.6)
    reg = 1 - clamp(m["post_gap_cv"] / 3) if m.get("post_gap_cv") is not None else None
    rec = 1 - clamp(((m.get("days_since_last_post") or 0) - 2) / 12)
    s = min(_wavg([(0.4, freq), (0.4, rec), (0.2, reg)]), sample_cap(n))
    return s, clamp(n / FULL_POSTS)


def c_credibility(m):
    """Verified status, bio/links, real-looking mention authors. Not a hype signal on its own."""
    parts = []
    if m.get("verified") is not None:
        parts.append((0.4, 1.0 if m["verified"] or m.get("verified_type") in ("business", "blue", "government") else 0.2))
    if m.get("has_bio") is not None:
        parts.append((0.3, 0.5 * bool(m["has_bio"]) + 0.5 * bool(m.get("has_links"))))
    if m.get("real_author_ratio") is not None:
        r = m["real_author_ratio"]
        if m.get("new_account_ratio") is not None:
            r = 0.7 * r + 0.3 * (1 - m["new_account_ratio"])
        parts.append((0.3, clamp(r)))
    if not parts:
        return None
    return _wavg(parts), clamp(sum(w for w, _ in parts))


def tier_for(score: float, tiers=DEFAULT_TIERS) -> int:
    for lo, t in sorted(tiers, reverse=True):
        if score >= lo:
            return t
    return max(t for _, t in tiers)   # lowest tier (robust to unsorted input)


# --------------------------- confidence ---------------------------
def _step(v, steps):
    """steps: [(threshold, value)] descending; first threshold v>= wins; else 0."""
    for th, val in steps:
        if v is not None and v >= th:
            return val
    return 0.0


def confidence_model(m: dict, coverage: float, *, source: str | None, identity_ok: bool | None,
                     data_age_hours: float | None, has_history: bool,
                     components: dict) -> tuple[float, str, dict, list]:
    """Evidence-quality confidence (independent of the hype score). Returns (value, label, parts, reasons)."""
    n = m.get("sample_tweets") or 0
    ua = m.get("mention_unique_authors") or 0
    parts = {
        "posts": (0.25, _step(n, [(15, 1.0), (8, 0.7), (3, 0.4), (1, 0.15)])),
        "authors": (0.15, _step(ua, [(50, 1.0), (25, 0.7), (10, 0.4), (1, 0.1)])),
        "coverage": (0.15, clamp(coverage)),
        "source": (0.10, {"X API v2": 1.0, "grokbot-x-read": 0.8}.get(source or "", 0.3)),
        "identity": (0.10, 1.0 if identity_ok else 0.6 if identity_ok is None else 0.0),
        "freshness": (0.10, 0.0 if data_age_hours is None else _step(-data_age_hours, [(-6, 1.0), (-24, 0.7), (-STALE_HOURS, 0.4)])),
        "history": (0.05, 1.0 if has_history else 0.0),
    }
    e, c = (components.get("engagement_quality") or {}).get("score"), (components.get("community_discussion") or {}).get("score")
    risks = risk_indicators(m)
    agree = 1.0 if e is None or c is None else 1.0 - clamp(abs(e - c) - 0.3) * 1.5
    parts["consistency"] = (0.10, clamp(agree - 0.15 * len(risks)))
    val = round(sum(w * v for w, v in parts.values()), 3)
    reasons = []
    if n < 3:
        reasons.append(f"only {n} official post(s) sampled")
    elif n < 8:
        reasons.append(f"small post sample ({n})")
    if ua < 25:
        reasons.append(f"only {ua} unique discussion authors")
    if (m.get("top_post_share") or 0) > 0.5 and n >= 2:
        reasons.append(f"one post carries {m['top_post_share']:.0%} of official engagement")
    if len(risks) > 1:
        reasons.append(f"{len(risks)} risk indicators present")
    if coverage < 0.8:
        reasons.append(f"category coverage {coverage:.0%}")
    if data_age_hours is not None and data_age_hours > STALE_HOURS:
        reasons.append(f"data is stale ({data_age_hours:.0f}h old)")
    if identity_ok is False:
        reasons.append("project identity not verified")
    # hard gates are applied to the NUMBER (caps), so the label is always confidence_label(value)
    if n < 3 or coverage < 0.6 or identity_ok is False or (data_age_hours or 0) > STALE_HOURS:
        val = min(val, LOW_CAP)
    elif not (n >= 8 and ua >= 25 and coverage >= 0.8 and len(risks) <= 1
              and (m.get("top_post_share") or 0) <= 0.5):   # one post dominating = effectively one data point
        val = min(val, MEDIUM_CAP)
    val = round(clamp(val), 3)
    label = confidence_label(val)
    return val, label, {k: round(v, 3) for k, (_, v) in parts.items()}, reasons


@dataclass
class ScoreResult:
    hype_score: float | None
    tier: int | None
    confidence: float
    confidence_label: str
    provisional: bool
    components: dict = field(default_factory=dict)  # key -> {score, points, max, quality}
    missing: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    weights: dict = field(default_factory=dict)
    coverage: float = 0.0
    ranked: bool = False
    confidence_parts: dict = field(default_factory=dict)
    confidence_reasons: list = field(default_factory=list)
    algorithm_version: str = ALGORITHM_VERSION


def score(m: dict, weights: dict | None = None, prev_metrics: dict | None = None,
          prev_days: float | None = None, provisional_threshold: float = 0.6,
          tiers=DEFAULT_TIERS, *, source: str | None = None, identity_ok: bool | None = None,
          data_age_hours: float | None = None) -> ScoreResult:
    weights = dict(weights or DEFAULT_WEIGHTS)
    raw = {
        "engagement_quality": c_engagement(m),
        "community_discussion": c_community(m),
        "growth_momentum": c_growth(m, prev_metrics, prev_days),
        "influential_engagement": c_influential(m),
        "posting_consistency": c_consistency(m),
        "project_credibility": c_credibility(m),
    }
    comps, missing, avail_w = {}, [], 0.0
    collectable_w = sum(w for k, w in weights.items() if k not in UNCOLLECTED_COMPONENTS)
    for k, w in weights.items():
        v = raw.get(k)
        if k in UNCOLLECTED_COMPONENTS:
            comps[k] = {"score": None, "points": None, "max": w, "quality": 0, "status": "not_collected"}
            continue
        if v is None:
            missing.append(k)
            comps[k] = {"score": None, "points": None, "max": w, "quality": 0, "status": "missing"}
            continue
        s, q = clamp(v[0]), clamp(v[1])
        comps[k] = {"score": round(s, 4), "points": round(s * w, 2), "max": w, "quality": round(q, 2), "status": "ok"}
        avail_w += w
    flags = risk_indicators(m)
    if m.get("followers") is not None and (m.get("sample_tweets") or 0) == 0:
        flags.append("No official posts sampled — official engagement not scored (community evidence only)")
    if avail_w == 0:
        return ScoreResult(None, None, 0.0, "Low", True, comps, missing, flags, weights)
    hs = round(clamp(sum(c["points"] for c in comps.values() if c["points"] is not None) / avail_w, 0, 1) * 100, 1)
    coverage = round(avail_w / collectable_w, 3) if collectable_w else 0.0
    conf, label, cparts, reasons = confidence_model(
        m, coverage, source=source or m.get("data_source"), identity_ok=identity_ok,
        data_age_hours=data_age_hours, has_history=bool(prev_metrics and prev_days and prev_days >= 0.5),
        components=comps)
    # Ranking policy: Low confidence or missing official engagement -> unranked (no tier forced).
    ranked = label != "Low" and "engagement_quality" not in missing
    provisional = (not ranked) or label != "High" or coverage < 1.0 or conf < provisional_threshold
    tier = tier_for(hs, tiers) if ranked else None
    return ScoreResult(hs, tier, conf, label, provisional, comps, missing, flags, weights,
                       coverage, ranked, cparts, reasons)


def trend(current: float | None, previous: float | None, threshold: float = 3.0,
          current_version: str | None = None, previous_version: str | None = None) -> tuple[str, float | None]:
    """Score trend. Scores from different algorithm versions are NOT comparable."""
    if current is None or previous is None:
        return "new", None
    if current_version and previous_version and current_version != previous_version:
        return "new", None
    d = round(current - previous, 1)
    if d > threshold:
        return "increasing", d
    if d < -threshold:
        return "decreasing", d
    return "stable", d


__all__ = ["score", "trend", "tier_for", "ScoreResult", "COMPONENT_LABELS", "ALGORITHM_VERSION"]
