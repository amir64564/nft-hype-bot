"""Category evaluation profiles: same factors and tier system, different weights per category.
Why each factor matters is documented in docs/RESEARCH.md."""
from __future__ import annotations

FACTORS = {
    "hype": "X hype (v2 hype score: engagement, discussion, momentum)",
    "x_genuineness": "Genuine vs farmed interest (independent authors, giveaway/promo/copy-paste share, respected accounts)",
    "team": "Artist/team track record and verifiable reputation",
    "art": "Artwork quality & consistency (labeled judgment)",
    "originality": "Originality / absence of credible copying evidence",
    "market": "Market health or mint-stage progress",
    "identity": "Identity verification (official links cross-reference each other)",
}

# weights sum to 100 per profile
PROFILES = {
    "artist_led":       {"hype": 20, "x_genuineness": 15, "team": 20, "art": 20, "originality": 10, "market": 10, "identity": 5},
    "generative_art":   {"hype": 20, "x_genuineness": 15, "team": 15, "art": 20, "originality": 15, "market": 10, "identity": 5},
    "pfp":              {"hype": 30, "x_genuineness": 20, "team": 15, "art": 10, "originality": 5, "market": 15, "identity": 5},
    "gaming":           {"hype": 25, "x_genuineness": 15, "team": 25, "art": 5, "originality": 5, "market": 20, "identity": 5},
    "brand_ip":         {"hype": 20, "x_genuineness": 10, "team": 15, "art": 10, "originality": 5, "market": 20, "identity": 20},
    "membership_utility": {"hype": 20, "x_genuineness": 15, "team": 25, "art": 5, "originality": 5, "market": 25, "identity": 5},
    "derivative":       {"hype": 30, "x_genuineness": 20, "team": 10, "art": 10, "originality": 15, "market": 10, "identity": 5},
    "experimental":     {"hype": 25, "x_genuineness": 20, "team": 15, "art": 15, "originality": 10, "market": 10, "identity": 5},
    "unknown":          {"hype": 30, "x_genuineness": 20, "team": 15, "art": 10, "originality": 5, "market": 15, "identity": 5},
}
# factors that MUST be present for a final tier in each profile (else Needs More Research)
REQUIRED = {k: {"hype", "x_genuineness"} for k in PROFILES}
for k in ("artist_led", "generative_art"):
    REQUIRED[k] |= {"art"}
for k in ("gaming", "membership_utility"):
    REQUIRED[k] |= {"team"}

assert all(sum(v.values()) == 100 for v in PROFILES.values())
