import pytest
from hypebot.scoring import score, tier_for, trend
from hypebot.metrics import derive_metrics
from hypebot.config import DEFAULT_WEIGHTS, load_weights
from tests.fixtures import fake_x


@pytest.mark.parametrize("s,t", [(100, 1), (80, 1), (79.9, 2), (60, 2), (59.9, 3), (40, 3), (39.9, 4), (0, 4)])
def test_tiers(s, t):
    assert tier_for(s) == t


def test_weights_sum_100():
    from hypebot.config import UNCOLLECTED_COMPONENTS
    assert sum(DEFAULT_WEIGHTS.values()) == 100 and UNCOLLECTED_COMPONENTS == {"cross_platform"}


def test_full_score_in_range():
    m = derive_metrics(fake_x(), {})
    r = score(m)
    assert 0 <= r.hype_score <= 100 and r.tier in (1, 2, 3, 4, None)
    assert "growth_momentum" not in r.missing  # intra-sample momentum available
    assert all(c["points"] <= c["max"] + 1e-9 for c in r.components.values() if c["points"] is not None)


def test_missing_data_not_zero():
    m = {"verified": True, "has_bio": True, "has_links": True, "followers": 10000,
         "posts_per_week": 6, "days_since_last_post": 1, "sample_tweets": 20}
    r = score(m)
    assert r.hype_score is not None and r.hype_score > 50  # renormalized over available, not penalised
    assert "engagement_quality" in r.missing and r.components["engagement_quality"]["points"] is None
    assert r.provisional and r.confidence < 0.6
    assert r.tier is None  # coverage too low to assign a tier


def test_no_data_returns_none():
    r = score({})
    assert r.hype_score is None and r.tier is None and r.provisional and len(r.missing) == 6


def test_followers_alone_not_ranked_high():
    big = score(derive_metrics(fake_x(followers=2_000_000, likes=50, mentions=0, influencers=0), {}))
    small = score(derive_metrics(fake_x(followers=20_000, likes=600), {}))
    assert small.hype_score > big.hype_score


def test_bot_signals_penalise():
    clean = score(derive_metrics(fake_x(), {}))
    botty = score(derive_metrics(fake_x(dup=True), {}))
    assert botty.hype_score < clean.hype_score
    assert any("duplicate" in f for f in botty.flags)


def test_growth_from_history():
    m = derive_metrics(fake_x(followers=60_000), {})
    up = score(m, prev_metrics={"followers": 50_000, "avg_engagement": 500}, prev_days=7)
    down = score(m, prev_metrics={"followers": 70_000, "avg_engagement": 3000}, prev_days=7)
    assert up.components["growth_momentum"]["score"] > down.components["growth_momentum"]["score"]


def test_custom_weights(tmp_path):
    p = tmp_path / "w.json"
    p.write_text('{"weights": {"project_credibility": 0}}')
    w = load_weights(str(p))
    assert w["project_credibility"] == 0 and w["engagement_quality"] == 25
    p.write_text('{"weights": {"bogus": 1}}')
    with pytest.raises(ValueError):
        load_weights(str(p))


def test_trend():
    assert trend(60, 50) == ("increasing", 10)
    assert trend(40, 50) == ("decreasing", -10)
    assert trend(51, 50)[0] == "stable"
    assert trend(50, None) == ("new", None)


def test_new_account_not_penalised_for_age():
    from hypebot.scoring import c_credibility
    young = c_credibility({"verified": True, "has_bio": True, "has_links": True, "account_age_days": 5})
    old = c_credibility({"verified": True, "has_bio": True, "has_links": True, "account_age_days": 2000})
    assert young == old


def test_gm_replies_not_flagged_and_high_er_is_hype():
    m = derive_metrics(fake_x(followers=5000, likes=3000), {})
    r = score(m)
    assert not any("low-effort" in f or "farming" in f for f in r.flags)
    assert r.components["engagement_quality"]["score"] > 0.8


def test_penalty_is_moderate():
    from hypebot.scoring import community_risk_factor, risk_indicators
    m = {"duplicate_text_ratio": 0.9, "new_account_ratio": 0.9, "repeat_author_ratio": 0.9, "top_thread_share": 0.9}
    assert 0.7 <= community_risk_factor(m) < 1 and len(risk_indicators(m)) == 4


def test_four_tiers_configurable():
    from hypebot.config import parse_tiers, tier_ranges_text, DEFAULT_TIERS
    assert len(DEFAULT_TIERS) == 4 and parse_tiers("80,60,40") == DEFAULT_TIERS
    assert tier_ranges_text(DEFAULT_TIERS) == "T1 80–100 · T2 60–79 · T3 40–59 · T4 0–39"
    t3 = parse_tiers("70,35")
    assert tier_for(50, t3) == 2 and tier_for(10, t3) == 3
    with pytest.raises(ValueError):
        parse_tiers("40,60")


def test_community_curve_calibration():
    from hypebot.scoring import c_community
    mid = c_community({"mentions_7d_total": 3621})[0]
    top = c_community({"mentions_7d_total": 21541})[0]
    assert 0.40 <= mid <= 0.52 and top >= 0.93


def test_growth_velocity_damped_by_volume():
    from hypebot.scoring import c_growth
    small = c_growth({"mentions_recent_per_day": 840, "mention_velocity_change": 1.6}, None, None)[0]
    big = c_growth({"mentions_recent_per_day": 2600, "mention_velocity_change": -0.23}, None, None)[0]
    assert small < 0.5 and big > small


def test_small_post_sample_capped():
    from hypebot.scoring import c_engagement, c_consistency
    base = {"avg_engagement": 5000, "median_engagement": 5000, "avg_views": 50000, "engagement_rate": 0.3,
            "conversation_ratio": 0.3, "posts_per_week": 7, "days_since_last_post": 0.5, "post_gap_cv": 0.2}
    few, many = dict(base, sample_tweets=2), dict(base, sample_tweets=20)
    assert c_engagement(few)[0] <= 0.55 < c_engagement(many)[0]
    assert c_engagement(few)[1] < c_engagement(many)[1]
    assert c_consistency(few) is None and c_consistency(many)[0] > 0.55  # <3 posts: missing


def test_low_absolute_engagement_scores_low():
    from hypebot.scoring import c_engagement
    s = c_engagement({"avg_engagement": 135, "median_engagement": 120, "avg_views": 2500, "median_views": 2400,
                      "engagement_rate": 0.03, "conversation_ratio": 0.22, "sample_tweets": 25})[0]
    assert s < 0.4
