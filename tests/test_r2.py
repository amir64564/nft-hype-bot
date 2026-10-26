"""r2.0 tests: checklist, state machine, retries, visual review, benchmark, confidence, regression (real scan_3 data)."""
import json, pathlib, sys
import httpx, pytest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_research import run_job, fixture_payload, fixture_visual_review, setup_db, mock_client, complete
from hypebot.scoring import confidence_label, ALGORITHM_VERSION
from hypebot.research.engine import evaluate_research, benchmark, RESEARCH_VERSION
from hypebot.research.checklist import build_checklist, CHECKS
from hypebot.research.evidence import validate_research_payload, ResearchPayloadError
from hypebot.research.models import ResearchJob, ResearchResult, ResearchAttempt
from hypebot.research import jobs, attempts as att
from hypebot.research.audit import run_audit

FIX = json.loads((pathlib.Path(__file__).parent / "fixtures" / "scan3_regression.json").read_text())
SNAP = {"hype_score": 70, "confidence": 0.85, "confidence_label": "High", "missing": [], "flags": [], "tier": 2, "id": 1,
        "metrics": {"mentions_sample": 50, "independent_authors": 25, "promo_or_low_effort_ratio": 0.3}}


def test_versions_bumped():
    assert ALGORITHM_VERSION == "v2.1" and RESEARCH_VERSION == "r2.0"


@pytest.mark.parametrize("v,l", [(0, "Low"), (0.49, "Low"), (0.5, "Medium"), (0.74, "Medium"), (0.75, "High"), (1, "High")])
def test_single_confidence_mapping(v, l):
    assert confidence_label(v) == l


def test_checklist_has_all_checks_with_fields():
    r = evaluate_research(snap=SNAP, evidence=[], category={"primary": "pfp", "stage": "pre_mint"}, handle="p")
    assert set(r["checklist"]) == set(CHECKS)
    for c in r["checklist"].values():
        assert c["status"] in ("VERIFIED", "PARTIAL", "FAILED", "NOT_APPLICABLE")
        assert {"reason", "evidence", "sources", "checked_at", "mandatory"} <= set(c)


def test_mandatory_depends_on_category_and_stage():
    s, e = complete(SNAP, [])
    pfp = evaluate_research(snap=s, evidence=e, category={"primary": "pfp", "stage": "pre_mint"}, handle="p")["checklist"]
    game = evaluate_research(snap=s, evidence=e, category={"primary": "gaming", "stage": "post_mint"}, handle="p")["checklist"]
    assert pfp["artwork"]["mandatory"] and pfp["mint_details"]["mandatory"] and not pfp["marketplace"]["mandatory"]
    assert game["team"]["mandatory"] and game["marketplace"]["mandatory"] and not game["mint_details"]["mandatory"]
    assert pfp["marketplace"]["status"] == "NOT_APPLICABLE"


def test_incomplete_never_gets_final_tier_and_has_reason():
    r = evaluate_research(snap=SNAP, evidence=[], category={"primary": "pfp", "stage": "pre_mint"}, handle="p")
    assert r["status"] == "INCOMPLETE" and r["tier"] is None and r["not_finalizable_reason"]
    assert r["confidence"] <= 0.49 and r["confidence_label"] == "Low"     # ≥2 mandatory missing → Low


def test_hype_cannot_force_quality_or_tier1():
    s = dict(SNAP, hype_score=99, tier=1)
    s, e = complete(s, [])
    r = evaluate_research(snap=s, evidence=e, category={"primary": "unknown", "stage": "pre_mint"}, handle="p")
    assert r["suggested_tier"] != 1 and r["hype_tier"] == 1


def test_team_wording_never_no_experience():
    r = evaluate_research(snap=SNAP, evidence=[], category={"primary": "gaming", "stage": "pre_mint"}, handle="p")
    txt = (r["checklist"]["team"]["reason"] + r["checklist"]["track_record"]["reason"]).lower()
    assert "research did not find" in txt and "no experience" not in txt.replace("not evidence of no experience", "")


def test_visual_review_requires_verified_local_files(tmp_path):
    vr = fixture_visual_review(tmp_path)
    vr["images"][0]["sha256"] = "0" * 64           # wrong hash → not counted as viewed
    p = validate_research_payload(fixture_payload(1, visual_review=vr), 1)
    assert [i["file_verified"] for i in p["visual_review"]["images"]] == [False, True, True]
    chk = build_checklist(snap=SNAP, evidence=[], category="pfp", handle="p",
                          hints={"visual_review": p["visual_review"]}, attempts=[], stage="pre_mint", peers_n=0, conflicts=[])
    assert chk["artwork"]["status"] == "PARTIAL"   # only 2 verified < 3
    with pytest.raises(ResearchPayloadError):
        validate_research_payload(fixture_payload(1, visual_review=dict(vr, reviewer="someone")), 1)


def test_hashing_alone_is_only_partial():
    chk = build_checklist(snap=SNAP, evidence=[], category="pfp", handle="p",
                          hints={"image_hashes": [{"url": "u", "phash": "ff"}]}, attempts=[], stage="pre_mint", peers_n=0, conflicts=[])
    assert chk["artwork"]["status"] == "PARTIAL" and "NOT" in chk["artwork"]["reason"]


def test_benchmark_min_peers_and_excludes_self(tmp_path):
    assert benchmark(SNAP, [{"name": "a", "hype_score": 1, "metrics": {}}])["status"] == "insufficient benchmark data"
    peers = [{"name": n, "hype_score": h, "metrics": {}} for n, h in (("a", 50), ("b", 60), ("c", 80))]
    assert benchmark(SNAP, peers)["percentiles"]["hype_score"] == 67


def test_attempt_classification_and_backoff():
    assert att.classify(200) == "success" and att.classify(404) == "not_found" and att.classify(503) == "temporary_error"
    assert att.classify(401) == "blocked" and att.classify(None) == "temporary_error"


async def test_temporary_errors_retry_then_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(att, "BACKOFF_S", 0)
    calls = []
    def h(req):
        calls.append(str(req.url))
        return httpx.Response(503) if "fixture-slug" in str(req.url) else httpx.Response(404)
    db, jid = await run_job(tmp_path, client=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    with db.Session() as s:
        n1 = len(s.scalars(__import__("sqlalchemy").select(ResearchAttempt).where(ResearchAttempt.job_id == jid)).all())
        st = s.get(ResearchJob, jid).status
    assert st in ("RETRYING", "FINALIZED", "INCOMPLETE") and n1 >= 1
    for _ in range(att.MAX_RETRIES + 2):
        await jobs.advance_all(db, httpx.AsyncClient(transport=httpx.MockTransport(h)))
    with db.Session() as s:
        rows = s.scalars(__import__("sqlalchemy").select(ResearchAttempt).where(ResearchAttempt.job_id == jid)).all()
        st = s.get(ResearchJob, jid).status
    by_target = {}
    for r in rows:
        by_target.setdefault(r.target, []).append(r.outcome)
    assert all(len(v) <= att.MAX_RETRIES + 1 for v in by_target.values())          # first try + MAX_RETRIES retries
    assert all(v.count("not_found") <= 1 for v in by_target.values())          # not_found never repeated
    assert st in ("FINALIZED", "INCOMPLETE")


async def test_rescore_appends_new_result(tmp_path):
    db, jid = await run_job(tmp_path)
    with db.Session() as s:
        r1 = s.get(ResearchJob, jid).result_id
    r2 = jobs.rescore_job(db, jid)
    assert r2 != r1
    with db.Session() as s:
        assert s.get(ResearchResult, r1) is not None
        assert jobs.latest_result(s, s.get(ResearchJob, jid).project_id).id == r2
    assert run_audit(db)["ok"]


# ---------- regression on REAL exported data (tests/fixtures/scan3_regression.json) ----------
@pytest.mark.parametrize("name", ["GigaLegendsLTD", "Conso", "Minehood"])
def test_regression_scan3(name):
    f = FIX[name]
    cat = f["job"]["hints"].get("category") or {"primary": "unknown", "stage": "unknown"}
    r = evaluate_research(snap=f["snapshot"], evidence=f["evidence"], category=cat, handle=f["project"]["handle"],
                          hints=f["job"]["hints"], unavailable=f["job"]["unavailable"], attempts=[])
    assert 0 <= r["confidence"] <= 1 and confidence_label(r["confidence"]) == r["confidence_label"]
    assert r["status"] == "INCOMPLETE" and r["tier"] is None            # no visual review exists for any of them
    assert "artwork" in r["missing_mandatory"] or not r["checklist"]["artwork"]["mandatory"]
    assert r["verdict"] in ("Needs More Research", "Manual Verification Required")
    if name == "Minehood":   # the old inconsistency: hype T1 must not leak into the research tier
        assert f["snapshot"]["tier"] == 1 and r["suggested_tier"] != 1
        assert r["hype_tier"] == 1 and r["hype_snapshot_id"] == f["snapshot"]["id"]


async def test_supplement_completes_incomplete_job(tmp_path):
    db, jid = await run_job(tmp_path, {"visual_review": None})
    with db.Session() as s:
        j = s.get(ResearchJob, jid)
        assert j.status == "INCOMPLETE"
        j_posted_before = j.posted
    sup = {"visual_review": fixture_visual_review(tmp_path), "claims": [], "identity": {}, "category": {"primary": "unknown"}}
    jobs.submit_feeder(db, jid, fixture_payload(jid, **sup))
    await jobs.advance_all(db, mock_client())
    with db.Session() as s:
        j = s.get(ResearchJob, jid)
        r = s.get(ResearchResult, j.result_id).report
        assert j.status == "FINALIZED" and r["tier"] and j.posted is False
        assert r["profile"] == "pfp"                       # category kept from first submission
        assert len(s.scalars(__import__("sqlalchemy").select(ResearchResult).where(ResearchResult.job_id == jid)).all()) == 2
