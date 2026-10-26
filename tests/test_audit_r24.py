"""Audit 2026-10-26 (docs/AUDIT_20261026.md): tier config, identity states, data quality, manipulation,
mandatory research checks, legacy-tier regression and negative cases. Synthetic inputs use example.test URLs."""
import copy, json, pathlib, re
import pytest
from test_research import complete, setup_db, run_job, fixture_payload
from tests._paths import ingest_path
from hypebot.config import Settings, load_weights, parse_tiers, configured_tiers, tier_ranges_text, DEFAULT_TIERS
from hypebot.external_x import to_x_data
from hypebot.metrics import _dt
from hypebot.pipeline import evaluate
from hypebot.scoring import score, tier_for, c_community, confidence_label, risk_indicators
from hypebot.research.engine import evaluate_research, f_genuineness, f_team, f_identity
from hypebot.research.checklist import mandatory_for
from hypebot.research import jobs
from hypebot.research.models import ResearchJob, ResearchResult

ROOT = pathlib.Path(__file__).resolve().parents[1]
W = load_weights(str(ROOT / "weights.json"))
CAT = {"primary": "unknown", "stage": "pre_mint"}


def link(fk, tk, url, value=1, i=None):
    return {"signal": "official_link", "value": value, "source_url": url, "verified": True, "section": "identity",
            "kind": "observation", "claim": f"{fk}->{tk}", "data": {"from_kind": fk, "to_kind": tk}, "id": i}


def base_case():
    """Synthetic complete case that FINALIZES (same shape as test_hysteresis_and_attribution)."""
    snap = {"hype_score": 59, "confidence": 0.85, "confidence_label": "High", "missing": [], "flags": [], "id": 1,
            "metrics": {"mentions_sample": 50, "independent_authors": 25, "promo_or_low_effort_ratio": 0.4}}
    ev = [link("x", "website", "https://example.test/a", i=1),
          {"signal": "team_prior_collection", "value": 0.5, "source_url": "https://example.test/t", "verified": True,
           "section": "team", "kind": "observation", "claim": "t", "id": 3},
          {"signal": "milestone", "value": 0.5, "source_url": "https://example.test/m", "verified": True,
           "section": "market", "kind": "observation", "claim": "m", "id": 4}]
    return complete(snap, ev)


def strip_ts(o):
    if isinstance(o, dict):
        return {k: strip_ts(v) for k, v in o.items() if k != "checked_at"}
    if isinstance(o, list):
        return [strip_ts(v) for v in o]
    return o


def test_base_case_finalizes():
    snap, ev = base_case()
    r = evaluate_research(snap=snap, evidence=ev, category=CAT, handle="p")
    assert r["status"] == "FINALIZED" and r["tier"] and r["identity_state"] == "verified"


# ---------------- tier configuration ----------------
def test_default_tiers_match_readme_and_docs():
    txt = tier_ranges_text(DEFAULT_TIERS)            # "T1 80–100 · T2 60–79 · T3 40–59 · T4 0–39"
    want = re.findall(r"T\d \d+–\d+", txt)
    for doc in ("README.md", "docs/SCORING.md"):
        body = (ROOT / doc).read_text()
        assert all(w in body for w in want), (doc, want)
    assert "Tier 1–4**" not in (ROOT / "README.md").read_text()


def test_configured_tiers_env(monkeypatch):
    monkeypatch.setenv("TIERS", "85,70,55,40")
    t = configured_tiers()
    assert len(t) == 5 and tier_for(39.9, t) == 5 and tier_for(85, t) == 1
    assert tier_for(-1, list(reversed(t))) == 5       # robust to unsorted input
    monkeypatch.delenv("TIERS")
    assert configured_tiers() == DEFAULT_TIERS


async def test_review_tier_range_follows_config(tmp_path, monkeypatch):
    db, jid = await run_job(tmp_path)
    with db.Session() as s:
        pid = s.get(ResearchJob, jid).project_id
    five = parse_tiers("85,70,55,40")
    assert jobs.add_review(db, pid, "u", "change_tier", 5, None, tiers=five)
    with pytest.raises(ValueError, match="1-5"):
        jobs.add_review(db, pid, "u", "change_tier", 6, None, tiers=five)
    with pytest.raises(ValueError, match="1-4"):
        jobs.add_review(db, pid, "u", "change_tier", 5, None)      # default config: 4 tiers
    monkeypatch.setenv("TIERS", "70,40")
    with pytest.raises(ValueError, match="1-3"):
        jobs.add_review(db, pid, "u", "change_tier", 4, None)


def test_research_uses_configured_tier_count():
    snap, ev = base_case()
    three = parse_tiers("70,40")
    r = evaluate_research(snap=snap, evidence=ev, category=CAT, handle="p", tiers=three)
    assert r["tier"] in (1, 2, 3)


def test_hysteresis_survives_tier_config_change():
    snap, ev = base_case()
    r = evaluate_research(snap=snap, evidence=ev, category=CAT, handle="p")
    prev = dict(r, tier=7)          # previous result from a config with more tiers: must not KeyError
    r2 = evaluate_research(snap=snap, evidence=ev, category=CAT, handle="p", previous=prev)
    assert r2["tier"] == r["tier"]


# ---------------- identity: verified / unverified / mismatch ----------------
def test_identity_three_states_are_distinct():
    snap, ev = base_case()
    r_ok = evaluate_research(snap=snap, evidence=ev, category=CAT, handle="p")
    no_links = [e for e in ev if e.get("signal") != "official_link"]
    r_un = evaluate_research(snap=snap, evidence=no_links, category=CAT, handle="p")
    one = no_links + [link("x", "website", "https://example.test/a")]
    r_one = evaluate_research(snap=snap, evidence=one, category=CAT, handle="p")
    r_mm = evaluate_research(snap=snap, evidence=ev + [link("marketplace", "x", "https://example.test/other", value=0)],
                             category=CAT, handle="p")
    assert (r_ok["identity_state"], r_un["identity_state"], r_one["identity_state"], r_mm["identity_state"]) == \
           ("verified", "unverified", "unverified", "mismatch")
    for r in (r_un, r_one, r_mm):
        assert r["status"] != "FINALIZED" and r["tier"] is None
    assert any("mismatch" in m for m in r_mm["manual_verification"])
    assert not any("mismatch" in m for m in r_un["manual_verification"])
    assert r_mm["factors"]["identity"]["score"] == 0.0 < r_ok["factors"]["identity"]["score"]


def test_hype_identity_false_caps_confidence():
    p = json.loads(ingest_path("req1").read_text())
    metrics, ok, _ = evaluate(Settings(weights=W), "Minehood", to_x_data(p, "minehood_rh"), _dt(p["collected_at"]), [])
    bad = score(metrics, W, identity_ok=False, data_age_hours=0)
    assert ok.ranked and bad.confidence <= 0.49 and bad.tier is None
    assert "project identity not verified" in bad.confidence_reasons


# ---------------- data quality ----------------
def test_stale_data_cannot_rank():
    p = json.loads(ingest_path("req1").read_text())
    x = to_x_data(p, "minehood_rh")
    from datetime import timedelta
    _, fresh, _ = evaluate(Settings(weights=W), "Minehood", x, _dt(p["collected_at"]), [])
    _, stale, _ = evaluate(Settings(weights=W), "Minehood", x, _dt(p["collected_at"]) + timedelta(hours=100), [])
    assert fresh.ranked and not stale.ranked and stale.confidence_label == "Low"
    assert any("stale" in r for r in stale.confidence_reasons)


def test_low_confidence_snapshot_blocks_research_finalization():
    snap, ev = base_case()
    snap = dict(snap, confidence=0.4, confidence_label="Low")     # e.g. stale or thin X data
    r = evaluate_research(snap=snap, evidence=ev, category=CAT, handle="p")
    assert r["status"] == "INCOMPLETE" and r["tier"] is None and r["confidence"] <= 0.49


def test_insufficient_samples_fail_checks():
    snap, ev = base_case()
    thin = dict(snap, metrics={"sample_tweets": 2, "mentions_sample": 5, "mention_unique_authors": 4})
    r = evaluate_research(snap=thin, evidence=ev, category=CAT, handle="p")
    assert r["checklist"]["official_x"]["status"] == "PARTIAL"
    assert r["checklist"]["community"]["status"] == "FAILED" and r["checklist"]["organic"]["status"] == "FAILED"
    assert r["status"] == "INCOMPLETE" and r["tier"] is None and r["not_finalizable_reason"]


def test_no_x_source_is_reported_not_scored_zero():
    snap, ev = base_case()
    r = evaluate_research(snap=None, evidence=ev, category=CAT, handle="p",
                          unavailable=[{"source": "X data", "reason": "not collected before deadline"}])
    assert r["status"] == "INCOMPLETE" and r["tier"] is None and r["factors"]["hype"]["score"] is None
    assert any(m.startswith("X data") for m in r["missing_info"])


def test_duplicate_evidence_does_not_inflate():
    snap, ev = base_case()
    base = evaluate_research(snap=snap, evidence=ev, category=CAT, handle="p")
    dup = ev + [copy.deepcopy(e) for e in ev if e.get("signal") == "team_prior_collection"] * 3
    resp = {"signal": "respected_account_engaged", "value": 1, "source_url": "https://example.test/r", "verified": True,
            "section": "x_interest", "kind": "observation", "claim": "respected account replied", "account": "someone"}
    one = evaluate_research(snap=snap, evidence=ev + [resp], category=CAT, handle="p")
    many = evaluate_research(snap=snap, evidence=dup + [dict(resp) for _ in range(4)], category=CAT, handle="p")
    assert many["factors"]["team"]["score"] == base["factors"]["team"]["score"]
    assert many["factors"]["x_genuineness"]["score"] == one["factors"]["x_genuineness"]["score"]
    assert many["overall_score_internal"] == one["overall_score_internal"]
    assert many["duplicate_evidence_dropped"] == 6   # 3 team copies + 3 extra respected-account copies


def test_duplicate_identity_link_is_not_two_independent_links():
    snap, ev = base_case()
    only_x = [e for e in ev if e.get("signal") != "official_link"]
    two_same = only_x + [link("x", "website", "https://example.test/a"), link("x", "website", "https://example.test/a2")]
    r = evaluate_research(snap=snap, evidence=two_same, category=CAT, handle="p")
    assert r["checklist"]["identity"]["status"] == "PARTIAL" and r["status"] != "FINALIZED"


def test_conflicting_sources_cap_confidence_and_are_reported():
    snap, ev = base_case()
    conf = [{"signal": "team_delivery_record", "value": 1, "source_url": "https://example.test/c1", "verified": True,
             "section": "team", "kind": "observation", "claim": "delivered roadmap"},
            {"signal": "team_delivery_record", "value": -1, "source_url": "https://example.test/c2", "verified": True,
             "section": "team", "kind": "observation", "claim": "missed roadmap"}]
    r = evaluate_research(snap=snap, evidence=ev + conf, category=CAT, handle="p")
    assert r["conflicts"] and r["confidence"] <= 0.74 and r["confidence_label"] != "High"
    assert any("contradiction" in x for x in r["confidence_reasons"])


def test_repeated_scoring_is_idempotent():
    snap, ev = base_case()
    a = evaluate_research(snap=snap, evidence=ev, category=CAT, handle="p")
    b = evaluate_research(snap=copy.deepcopy(snap), evidence=copy.deepcopy(ev), category=CAT, handle="p")
    assert strip_ts(a) == strip_ts(b)
    p = json.loads(ingest_path("req8").read_text())
    rs = [evaluate(Settings(weights=W), "Nuloraio", to_x_data(p, "nuloraio"), _dt(p["collected_at"]), [])[1] for _ in range(3)]
    assert len({(r.hype_score, r.tier, r.confidence) for r in rs}) == 1


async def test_market_api_failure_is_reported(tmp_path):
    import httpx
    from test_research import mock_client
    c = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503, json={})))
    db, jid = await run_job(tmp_path, {"category": {"primary": "pfp", "mint_type": "free", "stage": "post_mint"}}, client=c)
    with db.Session() as s:
        j = s.get(ResearchJob, jid)
        res = s.get(ResearchResult, j.result_id) if j.result_id else None
        status = j.status
    assert status != "FINALIZED" and (res is None or res.tier is None)
    if res is not None:
        assert res.report["checklist"]["marketplace"]["status"] == "FAILED"


# ---------------- manipulation resistance ----------------
def test_viral_single_post_cannot_give_high_confidence():
    for f, h in (("req7", "GigaLegendsLTD"), ("req8", "nuloraio")):
        p = json.loads(ingest_path(f).read_text())
        _, r, _ = evaluate(Settings(weights=W), h, to_x_data(p, h), _dt(p["collected_at"]), [])
        assert r.confidence_label != "High"


def test_repetitive_and_concentrated_replies_reduce_community():
    m = {"mentions_7d_total": 5000, "mentions_sample": 50, "mention_unique_authors": 40, "independent_authors": 30}
    clean = c_community(m)[0]
    spam = c_community(dict(m, duplicate_text_ratio=0.6, repeat_author_ratio=0.7, top_thread_share=0.9))[0]
    assert spam < clean
    assert len(risk_indicators(dict(m, duplicate_text_ratio=0.6, repeat_author_ratio=0.7, top_thread_share=0.9))) >= 3


def test_giveaway_and_paid_promo_lower_genuineness():
    m = {"mentions_sample": 50, "independent_authors": 30, "promo_or_low_effort_ratio": 0.3}
    base = f_genuineness(m, [])[0]
    g = [{"signal": "giveaway_driven", "value": -1, "verified": True, "source_url": "https://example.test/g", "claim": "g"}]
    pp = [{"signal": "paid_promotion", "value": -1, "verified": True, "source_url": "https://example.test/p", "claim": "p"}]
    assert f_genuineness(m, g)[0] < base and f_genuineness(m, pp)[0] < base
    assert f_genuineness(dict(m, promo_or_low_effort_ratio=0.9), [])[0] < base


def test_hype_alone_cannot_force_top_tier():
    snap, ev = base_case()
    r = evaluate_research(snap=dict(snap, hype_score=100), evidence=ev, category=CAT, handle="p")
    assert r["tier"] != 1 or tier_for(r["quality_score_internal"]) == 1


# ---------------- mandatory research checks ----------------
@pytest.mark.parametrize("cat,stage", [("pfp", "pre_mint"), ("artist_led", "post_mint"), ("gaming", "minting"),
                                       ("unknown", "pre_mint"), ("brand_ip", "post_mint")])
def test_every_mandatory_check_blocks_finalization(cat, stage):
    snap, ev = base_case()
    r = evaluate_research(snap=snap, evidence=ev, category={"primary": cat, "stage": stage}, handle="p")
    for k in mandatory_for(cat, stage):
        st = r["checklist"][k]["status"]
        if st not in ("VERIFIED", "NOT_APPLICABLE"):
            assert k in r["missing_mandatory"] and r["status"] != "FINALIZED" and r["tier"] is None
    if r["missing_mandatory"]:
        assert r["status"] == "INCOMPLETE" and r["tier"] is None


def test_artwork_never_verified_without_visual_review():
    snap, ev = base_case()
    r = evaluate_research(snap=snap, evidence=ev, category={"primary": "pfp", "stage": "pre_mint"}, handle="p",
                          hints={"image_hashes": [{"url": "https://example.test/i.png", "phash": "0"}]})
    assert r["checklist"]["artwork"]["status"] == "PARTIAL" and r["factors"]["art"]["score"] is None
    assert r["status"] != "FINALIZED"


def test_founder_and_track_record_absence_is_not_negative():
    snap, ev = base_case()
    no_team = [e for e in ev if e.get("section") != "team"]
    r = evaluate_research(snap=snap, evidence=no_team, category=CAT, handle="p")
    assert r["factors"]["team"]["score"] is None
    assert "not evidence of no experience" in r["checklist"]["track_record"]["reason"]


# ---------------- legacy tier corrections (real DB snapshots) ----------------
LEG = json.loads((ROOT / "tests" / "fixtures" / "legacy_tier_corrections.json").read_text())["cases"]


@pytest.mark.parametrize("case", LEG, ids=[c["name"] for c in LEG])
def test_legacy_tier_corrections(case):
    p = json.loads(ingest_path(case["input_file"]).read_text())
    _, r, _ = evaluate(Settings(weights=W), case["name"], to_x_data(p, case["handle"]), _dt(p["collected_at"]), [])
    assert (r.ranked, r.tier, r.confidence_label) == (case["expected_ranked"], case["expected_tier"],
                                                      case["expected_confidence"]), case["reason"]
    assert (r.tier, r.confidence_label) != (case["legacy_tier"], case["legacy_confidence"])


# ---------------- report explains the tier ----------------
def test_report_explains_tier():
    from hypebot.research.embeds import why_tier_text
    snap, ev = base_case()
    r = evaluate_research(snap=snap, evidence=ev, category=CAT, handle="p")
    for k in ("tier", "factors", "supporting_evidence", "risks", "confidence", "confidence_label", "missing_info",
              "why_tier", "identity_state", "checklist"):
        assert k in r
    t = why_tier_text(r)
    assert "Not higher" in t and "Not lower" in t and "identity" in t
    prev = dict(r, tier=(r["tier"] % 4) + 1, verdict_text="Tier x", factors={})
    r2 = evaluate_research(snap=snap, evidence=ev, category=CAT, handle="p", previous=prev)
    assert r2["change"]["cause"] and r2["change"]["from"] == "Tier x"
