"""Configuration: env vars + optional JSON weights file."""
from __future__ import annotations
import json, os
from dataclasses import dataclass, field

DEFAULT_WEIGHTS = {
    "engagement_quality": 25,
    "community_discussion": 20,
    "growth_momentum": 20,
    "influential_engagement": 10,
    "posting_consistency": 10,
    "project_credibility": 10,
    "cross_platform": 5,
}
# Categories with no collector in this build: reported as "not collected", excluded from the
# score denominator and from coverage (never scored as zero, never given points).
UNCOLLECTED_COMPONENTS = {"cross_platform"}
COMPONENT_LABELS = {
    "engagement_quality": "X Engagement Quality",
    "community_discussion": "Community Discussion",
    "growth_momentum": "Growth & Momentum",
    "influential_engagement": "Influential Account Engagement",
    "posting_consistency": "Posting Consistency",
    "project_credibility": "Project Credibility",
    "cross_platform": "Cross-Platform Presence",
}
# (min score, tier)
DEFAULT_TIERS = [(80, 1), (60, 2), (40, 3), (0, 4)]


def parse_tiers(spec: str | None) -> list:
    """TIERS env: descending lower bounds for Tier 1..N-1, e.g. "80,60,40" -> 4 tiers."""
    if not spec:
        return list(DEFAULT_TIERS)
    bounds = [float(x) for x in spec.split(",") if x.strip()]
    if not bounds or bounds != sorted(bounds, reverse=True) or bounds[-1] <= 0 or bounds[0] > 100:
        raise ValueError("TIERS must be descending bounds in (0,100], e.g. 80,60,40")
    return [(b, i + 1) for i, b in enumerate(bounds)] + [(0, len(bounds) + 1)]


def tier_ranges_text(tiers) -> str:
    t = sorted(tiers, reverse=True)
    out, hi = [], 100
    for lo, n in t:
        out.append(f"T{n} {lo:g}–{hi:g}")
        hi = lo - 1 if float(lo).is_integer() else lo
    return " · ".join(out)


@dataclass
class Settings:
    discord_token: str | None = None
    discord_guild_id: int | None = None
    x_bearer_token: str | None = None
    database_url: str = "sqlite:///hypebot.db"
    coingecko_api_key: str | None = None
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    tiers: list = field(default_factory=lambda: list(DEFAULT_TIERS))
    provisional_confidence: float = 0.6
    influencer_min_followers: int = 10_000
    sync_commands: bool = False
    log_level: str = "INFO"
    poll_seconds: int = 60
    reviewer_user_ids: set = field(default_factory=lambda: {"922754935806894091"})
    reviewer_role_ids: set = field(default_factory=set)
    # Sorsa (crypto social graph) — optional verification source, never the final authority
    sorsa_api_key: str | None = None
    sorsa_cache_ttl_h: float = 24.0
    sorsa_min_remaining: int = 10          # soft-stop: no live calls below this unless forced
    sorsa_quota_total: int | None = 100      # lifetime request pack (free plan); used only if key-usage-info fails
    sorsa_optional_min_remaining: int = 20  # score-changes / new-followers-7d only above this
    sorsa_auto_fetch: bool = True          # fetch the 3-call pack during research jobs when a key is set
    seed_accounts_file: str = "seed_accounts.json"


def load_weights(path: str | None) -> dict:
    w = dict(DEFAULT_WEIGHTS)
    if path and os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        for k, v in data.get("weights", data).items():
            if k not in DEFAULT_WEIGHTS:
                raise ValueError(f"Unknown weight key: {k}")
            if float(v) < 0:
                raise ValueError(f"Weight {k} must be >= 0")
            w[k] = float(v)
    for k in DEFAULT_WEIGHTS:  # env overrides e.g. WEIGHT_ENGAGEMENT_QUALITY=30
        ev = os.getenv(f"WEIGHT_{k.upper()}")
        if ev:
            w[k] = float(ev)
    if sum(w.values()) <= 0:
        raise ValueError("Weights must sum to > 0")
    return w


def load_settings() -> Settings:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    gid = os.getenv("DISCORD_GUILD_ID")
    return Settings(
        discord_token=os.getenv("DISCORD_TOKEN") or None,
        discord_guild_id=int(gid) if gid else None,
        x_bearer_token=os.getenv("X_BEARER_TOKEN") or os.getenv("TWITTER_BEARER_TOKEN") or None,
        database_url=os.getenv("DATABASE_URL", "sqlite:///hypebot.db"),
        coingecko_api_key=os.getenv("COINGECKO_API_KEY") or None,
        weights=load_weights(os.getenv("WEIGHTS_FILE", "weights.json")),
        tiers=parse_tiers(os.getenv("TIERS")),
        provisional_confidence=float(os.getenv("PROVISIONAL_CONFIDENCE", "0.6")),
        influencer_min_followers=int(os.getenv("INFLUENCER_MIN_FOLLOWERS", "10000")),
        sync_commands=os.getenv("SYNC_COMMANDS", "false").lower() in ("1", "true", "yes"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        poll_seconds=int(os.getenv("XREQUEST_POLL_SECONDS", "60")),
        reviewer_user_ids={x.strip() for x in os.getenv("REVIEWER_USER_IDS", "922754935806894091").split(",") if x.strip()},
        reviewer_role_ids={x.strip() for x in os.getenv("REVIEWER_ROLE_IDS", "").split(",") if x.strip()},
        **sorsa_settings(),
    )


def sorsa_settings() -> dict:
    """Sorsa env vars (also used by jobs/scripts that do not build full Settings)."""
    return dict(
        sorsa_api_key=os.getenv("SORSA_API_KEY") or None,
        sorsa_cache_ttl_h=float(os.getenv("SORSA_CACHE_TTL_H", "24")),
        sorsa_min_remaining=int(os.getenv("SORSA_MIN_REMAINING", "10")),
        sorsa_optional_min_remaining=int(os.getenv("SORSA_OPTIONAL_MIN_REMAINING", "20")),
        sorsa_quota_total=int(os.getenv("SORSA_QUOTA_TOTAL", "100")) or None,
        sorsa_auto_fetch=os.getenv("SORSA_AUTO_FETCH", "true").lower() in ("1", "true", "yes"),
        seed_accounts_file=os.getenv("SEED_ACCOUNTS_FILE", "seed_accounts.json"),
    )
