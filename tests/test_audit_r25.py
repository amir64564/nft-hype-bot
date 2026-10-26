"""Audit r2.5 (docs/AUDIT_TIER_MISCLASS_20261026.md). Synthetic inputs (example.test URLs) prove that a
mitigation EXISTS; they say nothing about real-world accuracy."""
import json, pathlib
from test_audit_r24 import base_case, link, CAT, W
from hypebot.research.engine import evaluate_research, dedupe_evidence, f_identity
from hypebot.pipeline import identity_verified
from hypebot.scoring import score

ROOT = pathlib.Path(__file__).resolve().parents[1]


def ev(claim, url, i, signal="team_prior_collection", value=0.5):
    return {"signal": signal, "value": value, "source_url": url, "verified": True, "section": "team",
            "kind": "observation", "claim": claim, "id": i}


def test_reworded_copy_same_source_is_dropped_and_audited():
    a = ev("Founder previously launched the Alpha Cats collection in 2023", "https://example.test/t", 1)
    b = ev("founder previously launched Alpha Cats collection in 2023!", "https://example.test/t/", 2)
    audit = {}
    out, n = dedupe_evidence([a, b], audit)
    assert n == 1 and [e["id"] for e in out] == [1]
    assert audit["near"] == 1 and audit["removed"][0] == {"id": 2, "reason": "near duplicate (reworded claim, same source)", "kept_id": 1}


def test_same_claim_from_independent_sources_is_kept():
    a = ev("Founder previously launched the Alpha Cats collection in 2023", "https://example.test/t", 1)
    b = ev("Founder previously launched the Alpha Cats collection in 2023", "https://other.example.test/x", 2)
    out, n = dedupe_evidence([a, b])
    assert n == 0 and len(out) == 2


def test_different_claims_same_source_are_kept():
    a = ev("Founder launched Alpha Cats in 2023", "https://example.test/t", 1)
    b = ev("Team delivered the promised game demo on schedule", "https://example.test/t", 2, signal="team_delivery_record")
    assert dedupe_evidence([a, b])[1] == 0


def test_report_carries_dedupe_audit():
    snap, evs = base_case()
    evs = evs + [dict(evs[1], id=9, claim=evs[1]["claim"] + " ")]
    r = evaluate_research(snap=snap, evidence=evs, category=CAT, handle="p")
    assert r["duplicate_evidence_dropped"] == 1 and r["duplicate_evidence_audit"]["removed"]


def test_unverified_identity_is_unknown_not_zero_and_not_mismatch():
    assert f_identity([], "p")[0] is None
    assert f_identity([link("x", "website", "https://example.test/a", value=0)], "p")[0] == 0.0


def test_unverified_identity_with_sorsa_is_not_a_conflict():
    snap, evs = base_case()
    evs = [e for e in evs if e["signal"] != "official_link"]
    social = {"has_sorsa_pack": True, "conflicts": [], "unverified": []}
    r = evaluate_research(snap=snap, evidence=evs, category=CAT, handle="p", social=social)
    assert r["identity_state"] == "unverified" and r["status"] == "INCOMPLETE" and r["tier"] is None


def test_hype_name_mismatch_is_unverified_not_fraud():
    # legitimate brand whose X display name differs from the project name
    assert identity_verified("Minehood", {"name": "MH Studio", "username": "mh_studio", "description": ""}) is None


def test_high_followers_weak_engagement_is_not_high_tier():
    """synthetic: 500k followers but tiny engagement / discussion."""
    m = {"followers": 500_000, "sample_tweets": 15, "median_engagement": 8, "engagement_rate": 0.00002,
         "posts_per_week": 5, "post_gap_cv": 0.5, "days_since_last_post": 1, "mentions_sample": 40,
         "mention_unique_authors": 30, "independent_authors": 5, "promo_or_low_effort_ratio": 0.8,
         "mentions_recent_per_day": 20, "influential_relevant": 0, "influential_threads": 0}
    r = score(m, W)
    assert r.hype_score is None or r.hype_score < 60


def test_benchmark_file_labels_ground_truth():
    d = json.loads((ROOT / "benchmark" / "tier_benchmark.json").read_text())
    for c in d["cases"]:
        assert c["ground_truth_source"] in ("manual_review", "algorithm_echo", "unknown")
        if c["ground_truth_source"] == "manual_review":   # manual rows need a reviewer and real evidence links
            mr = c["manual_review"]
            assert mr["reviewer"] and mr["reviewed_at"] and mr["evidence_urls"] and mr["manual_tier"] is not None
