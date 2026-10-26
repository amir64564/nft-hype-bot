"""Research layer tests. Feeder payloads and marketplace JSON here are TEST FIXTURES (example.test
URLs / httpx MockTransport), never presented as real sources."""
import asyncio, copy, json, pathlib
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
import httpx, pytest
from hypebot.config import Settings, load_weights
from hypebot.db import Database, XRequest, Snapshot
from hypebot.collectors.x_api import XClient
from hypebot.pipeline import Analyzer, evaluate
from hypebot.external_x import to_x_data
from hypebot.metrics import _dt
from hypebot.research import jobs, market, images
from hypebot.research.engine import evaluate_research, NMR, MVR, HYSTERESIS
from hypebot.research.evidence import validate_research_payload, ResearchPayloadError
from hypebot.research.models import Evidence, ResearchJob, ResearchResult, ManualReview
from hypebot.research.profiles import PROFILES
from hypebot.research.audit import run_audit

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXP = json.loads((ROOT / "tests" / "regression_expectations.json").read_text())["cases"]
W = load_weights(str(ROOT / "weights.json"))


def real(name):
    return json.loads((ROOT / "x_ingest" / f"{name}.json").read_text())


# ---------- regression on real datasets (expectations live in a fixture file, not in code) ----------
@pytest.mark.parametrize("case", EXP, ids=[c["file"] for c in EXP])
def test_regression_real_projects(case):
    p = real(case["file"])
    x = to_x_data(p, case["handle"])
    _, r, _ = evaluate(Settings(weights=W), case["name"], x, _dt(p["collected_at"]), [])
    assert r.ranked == case["ranked"] and r.tier == case["tier"] and r.confidence_label == case["confidence"], case["why"]
    assert case["score"][0] <= r.hype_score <= case["score"][1]


def test_no_project_names_in_scoring_logic():
    src = "".join((ROOT / "hypebot" / f).read_text().lower() for f in
                  ("scoring.py", "metrics.py", "research/engine.py", "research/profiles.py"))
    for c in EXP:
        assert c["name"].lower() not in src and c["handle"].lower() not in src


# ---------- fixtures ----------
def fixture_payload(job_id, **over):
    p = {"source": "grokbot-research", "job_id": job_id, "collected_at": "2026-10-21T14:30:00Z",
         "identity": {"official_x": "https://x.com/minehood_rh", "website": "https://example.test/site",
                      "marketplaces": [{"name": "opensea", "slug": "fixture-slug", "url": "https://example.test/os"}]},
         "category": {"primary": "pfp", "mint_type": "free", "stage": "pre_mint"},
         "claims": [
             {"signal": "official_link", "value": 1, "claim": "Website links to X", "source_url": "https://example.test/site",
              "data": {"from_kind": "website", "to_kind": "x"}},
             {"signal": "official_link", "value": 1, "claim": "X bio links website", "source_url": "https://x.com/minehood_rh",
              "data": {"from_kind": "x", "to_kind": "website"}},
             {"signal": "team_prior_collection", "value": 0.8, "claim": "Artist shipped a prior collection", "source_url": "https://example.test/prior"},
             {"signal": "milestone", "value": 1, "claim": "Allowlist closed on schedule", "source_url": "https://example.test/ann"},
             {"section": "team", "claim": "Founder is ex-Big Studio", "kind": "observation"},       # no URL -> unverified
             {"signal": "art_quality", "value": 0.7, "claim": "Clean consistent style", "source_url": "https://example.test/img1.png"},
         ],
         "unavailable": [{"source": "Discord", "reason": "invite-only"}]}
    p.update(over)
    return p


# FIXTURE helpers: complete counts / evidence so mandatory checks can be VERIFIED in unit tests
FULL_X = {"sample_tweets": 10, "mention_unique_authors": 30, "mentions_7d_total": 120, "mentions_sample": 50}
EXTRA_EV = [{"signal": "official_link", "value": 1, "source_url": "https://example.test/mk", "verified": True, "section": "identity",
             "kind": "observation", "claim": "fixture marketplace links X", "data": {"from_kind": "marketplace", "to_kind": "x"}},
            {"signal": "market_fact", "value": 1, "source_url": "https://example.test/mint", "verified": True, "section": "market",
             "kind": "observation", "claim": "fixture supply 3333"},
            {"signal": "milestone", "value": 1, "source_url": "https://example.test/mint2", "verified": True, "section": "market",
             "kind": "observation", "claim": "fixture mint date announced"}]


def complete(snap, ev):
    return dict(snap, metrics=dict(FULL_X, **snap["metrics"])), ev + EXTRA_EV


def fixture_visual_review(tmp_path, n=3):
    """FIXTURE visual review: writes n tiny local PNG files and records their sha256 as the feeder would."""
    import hashlib
    from PIL import Image
    imgs = []
    for i in range(n):
        f = tmp_path / f"fixture_img{i}.png"
        Image.new("RGB", (8, 8), (i * 40, 10, 10)).save(f)
        imgs.append({"url": f"https://example.test/img{i}.png", "local_path": str(f), "official": True, "viewed": True,
                     "sha256": hashlib.sha256(f.read_bytes()).hexdigest()})
    return {"reviewer": "grokbot-feeder-visual", "reviewed_at": "2026-10-21T14:20:00Z", "images": imgs,
            "checks": [{"check": "quality", "judgment": 0.7, "notes": "fixture"},
                       {"check": "consistency", "judgment": 0.8, "notes": "fixture"},
                       {"check": "originality_indicators", "judgment": 0.6, "notes": "fixture"}]}


def setup_db(tmp_path):
    s = Settings(x_bearer_token=None, database_url=f"sqlite:///{tmp_path}/t.db", weights=W)
    db = Database(s.database_url)
    return s, db, Analyzer(s, db, XClient(None))


def mock_client():
    """MockTransport serving FIXTURE marketplace JSON (shape of OpenSea v2 responses)."""
    def handler(req):
        u = str(req.url)
        if u.endswith("/collections/fixture-slug"):
            return httpx.Response(200, json={"name": "Fixture", "total_supply": 1000, "twitter_username": "minehood_rh"})
        if u.endswith("/collections/fixture-slug/stats"):
            return httpx.Response(200, json={"total": {"floor_price": 0.1, "floor_price_symbol": "ETH", "num_owners": 600, "sales": 900},
                                             "intervals": [{"interval": "seven_day", "sales": 40, "volume": 4}]})
        return httpx.Response(404, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def run_job(tmp_path, payload_over=None, with_x=True, client=None):
    s, db, a = setup_db(tmp_path)
    o = await a.analyze("Minehood", "minehood_rh", context={"channel_id": "1"})
    jid = jobs.create_job(db, o.project.id, o.x_request_id, {"channel_id": "1"})
    if with_x:
        # pin the analysis clock to 1h after collection: the fixture is a fixed historical dataset, and
        # wall-clock staleness (>72h) would otherwise make this test fail as the calendar moves on
        from unittest import mock
        from hypebot.metrics import _dt
        from datetime import timedelta
        x1 = real("req1")
        with mock.patch("hypebot.pipeline.utcnow", lambda: _dt(x1["collected_at"]) + timedelta(hours=1)):
            a.ingest_external(o.x_request_id, x1)
    over = dict(payload_over or {})
    if "visual_review" not in over:
        over["visual_review"] = fixture_visual_review(tmp_path)
    if "claims" not in over:
        over["claims"] = fixture_payload(jid)["claims"] + [
            {"signal": "market_fact", "value": 1, "claim": "Fixture supply 3333", "source_url": "https://example.test/mint"}]
    jobs.submit_feeder(db, jid, fixture_payload(jid, **over))
    await jobs.advance_all(db, client or mock_client())
    return db, jid


# ---------- payload validation ----------
def test_unsourced_claim_marked_unverified_and_judgment_labeled():
    p = validate_research_payload(fixture_payload(5), 5)
    by = {c["claim"]: c for c in p["claims"]}
    assert by["Founder is ex-Big Studio"]["verified"] is False
    assert by["Clean consistent style"]["kind"] == "judgment" and by["Clean consistent style"]["verified"] is False


@pytest.mark.parametrize("bad", [{"source": "x"}, {"job_id": 99}, {"category": {"primary": "meme"}},
                                 {"claims": [{"signal": "nope", "claim": "a"}]},
                                 {"claims": [{"signal": "team_abandonment", "value": 0.5, "claim": "a"}]}])
def test_invalid_payloads_rejected(bad):
    with pytest.raises(ResearchPayloadError):
        p = fixture_payload(5); p.update(bad)
        validate_research_payload(p, 5)


# ---------- full job pipeline ----------
async def test_full_research_job(tmp_path):
    db, jid = await run_job(tmp_path)
    with db.Session() as s:
        j = s.get(ResearchJob, jid)
        assert j.status == "FINALIZED" and j.result_id
        r = s.get(ResearchResult, j.result_id).report
    assert r["verdict"] == "Tier" and r["tier"] in (1, 2, 3, 4)
    assert r["factors"]["identity"]["score"] == 1.0 and r["factors"]["market"]["score"] is not None
    assert all(c["url"] for c in r["supporting_evidence"] + r["positives"])          # no claim without link
    assert any("ex-Big Studio" in c["claim"] for c in r["unverified_claims"])
    assert any("Discord" in m for m in r["missing_info"])
    assert r["hype_score"] is not None and "quality_score_internal" in r and r["why_tier"]
    assert run_audit(db)["ok"]


async def test_identity_unverified_requires_manual(tmp_path):
    p = fixture_payload(0)
    claims = [c for c in p["claims"] if c.get("signal") != "official_link"]
    db, jid = await run_job(tmp_path, {"claims": claims, "identity": {}}, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    with db.Session() as s:
        r = s.get(ResearchResult, s.get(ResearchJob, jid).result_id)
    assert r.verdict == MVR and r.tier is None and r.report["overall_score"] is None


async def test_marketplace_links_other_account_requires_manual(tmp_path):
    def h(req):
        if str(req.url).endswith("/fixture-slug"):
            return httpx.Response(200, json={"name": "F", "twitter_username": "impostor"})
        return httpx.Response(404, json={})
    db, jid = await run_job(tmp_path, client=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    with db.Session() as s:
        r = s.get(ResearchResult, s.get(ResearchJob, jid).result_id).report
    assert r["verdict"] == MVR and any("different" in m for m in r["manual_verification"])
    assert any("OpenSea stats" in m for m in r["missing_info"])        # failed source listed, not zero


async def test_no_x_data_needs_more_research(tmp_path):
    s, db, a = setup_db(tmp_path)
    o = await a.analyze("Minehood", "minehood_rh")
    jid = jobs.create_job(db, o.project.id, o.x_request_id)
    jobs.submit_feeder(db, jid, fixture_payload(jid))
    await jobs.advance_all(db, mock_client())
    with db.Session() as ss:
        assert ss.get(ResearchJob, jid).status == "RESEARCHING"     # waits for X data, no tier
    assert jobs.finalize(db, jid, force=True)
    with db.Session() as ss:
        r = ss.get(ResearchResult, ss.get(ResearchJob, jid).result_id)
    assert r.verdict == NMR and r.tier is None and r.report["overall_score"] is None


def test_pre_mint_not_penalized_and_unavailable_market_not_zero():
    snap = {"hype_score": 70, "confidence": 0.85, "confidence_label": "High", "metrics": {"mentions_sample": 50,
            "independent_authors": 30, "promo_or_low_effort_ratio": 0.3}, "missing": [], "flags": []}
    ev = [{"signal": "official_link", "value": 1, "source_url": "https://example.test/a", "verified": True, "section": "identity",
           "kind": "observation", "claim": "c", "data": {"from_kind": "x", "to_kind": "website"}}]
    r = evaluate_research(snap=snap, evidence=ev, category={"primary": "pfp", "stage": "pre_mint"}, handle="p")
    assert r["factors"]["market"]["score"] is None and r["factors"]["market"]["points"] is None
    assert "not penalized" in r["factors"]["market"]["why"]


def test_giveaway_signals_lower_genuineness():
    m = {"mentions_sample": 50, "independent_authors": 30, "promo_or_low_effort_ratio": 0.3}
    from hypebot.research.engine import f_genuineness
    base = f_genuineness(m, [])[0]
    g = [{"signal": "giveaway_driven", "value": -1, "verified": True, "source_url": "https://example.test/g"}]
    assert f_genuineness(m, g)[0] < base


def test_hysteresis_and_attribution():
    snap = {"hype_score": 59, "confidence": 0.85, "confidence_label": "High", "missing": [], "flags": [],
            "metrics": {"mentions_sample": 50, "independent_authors": 25, "promo_or_low_effort_ratio": 0.4}}
    ev = [{"signal": "official_link", "value": 1, "source_url": "https://example.test/a", "verified": True, "section": "identity",
           "kind": "observation", "claim": "c", "data": {"from_kind": "x", "to_kind": "website"}, "id": 1},
          {"signal": "official_link", "value": 1, "source_url": "https://example.test/b", "verified": True, "section": "identity",
           "kind": "observation", "claim": "d", "data": {"from_kind": "marketplace", "to_kind": "x"}, "id": 2},
          {"signal": "team_prior_collection", "value": 0.5, "source_url": "https://example.test/t", "verified": True,
           "section": "team", "kind": "observation", "claim": "t", "id": 3},
          {"signal": "milestone", "value": 0.5, "source_url": "https://example.test/m", "verified": True,
           "section": "market", "kind": "observation", "claim": "m", "id": 4}]
    snap, ev = complete(snap, ev)
    cat = {"primary": "unknown", "stage": "pre_mint"}
    first = evaluate_research(snap=snap, evidence=ev, category=cat, handle="p")
    assert first["verdict"] == "Tier"
    t0, sc = first["tier"], first["overall_score_internal"]
    prev = dict(first, tier=t0 - 1 if t0 > 1 else t0 + 1)     # pretend previous was adjacent tier
    second = evaluate_research(snap=snap, evidence=ev, category=cat, handle="p", previous=prev)
    boundary = {t: lo for lo, t in [(80, 1), (60, 2), (40, 3), (0, 4)]}[min(t0, prev["tier"])]
    if abs(sc - boundary) < HYSTERESIS:
        assert second["tier"] == prev["tier"] and second["hysteresis"]
    else:
        assert second["tier"] == t0 and second["change"]["drivers"] is not None


def test_profiles_weights_and_required():
    assert all(sum(v.values()) == 100 for v in PROFILES.values())
    snap = {"hype_score": 90, "confidence": 0.9, "confidence_label": "High", "missing": [], "flags": [],
            "metrics": {"mentions_sample": 50, "independent_authors": 40, "promo_or_low_effort_ratio": 0.1}}
    ev = [{"signal": "official_link", "value": 1, "source_url": "https://example.test/a", "verified": True, "section": "identity",
           "kind": "observation", "claim": "c", "data": {"from_kind": "x", "to_kind": "website"}}]
    r = evaluate_research(snap=snap, evidence=ev, category={"primary": "artist_led", "stage": "post_mint"}, handle="p")
    assert r["verdict"] == NMR and any("art" in x for x in r["confidence_reasons"])   # art required for artist-led


def test_one_influential_account_cannot_drive_tier():
    from hypebot.scoring import c_influential
    assert c_influential({"influential_relevant": 1, "influential_threads": 1, "mentions_sample": 50,
                          "author_followers_coverage": 1})[0] < 0.3


# ---------- market collectors (fixtures via MockTransport) ----------
async def test_market_failure_reported_not_zero():
    c = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"errors": ["key"]})))
    claims, un = await market.collect_market([{"name": "opensea", "slug": "x"}], None, c)
    assert claims == [] and len(un) == 2 and "401" in un[0]["reason"]


async def test_magiceden_wash_indicator():
    acts = [{"type": "buyNow", "buyer": "A", "seller": "B"}] * 4 + [{"type": "buyNow", "buyer": f"u{i}", "seller": "S"} for i in range(4)]
    def h(req):
        if "activities" in str(req.url):
            return httpx.Response(200, json=acts)
        return httpx.Response(200, json={"floorPrice": 2_000_000_000, "listedCount": 10})
    claims, un = await market.collect_market([{"name": "magiceden_sol", "slug": "fx"}], None,
                                             httpx.AsyncClient(transport=httpx.MockTransport(h)))
    act = [c for c in claims if "activities" in c["source_url"]][0]
    assert act["data"]["repeat_pair_sales"] == 4 and act["value"] == -0.5 and "not proof" in act["why"]


def test_phash_similarity_is_not_proof(tmp_path):
    from PIL import Image
    import imagehash
    a = Image.new("RGB", (64, 64), "white"); [a.putpixel((i, i), (0, 0, 0)) for i in range(64)]
    hs = [{"url": "https://example.test/p.png", "role": "project", "phash": str(imagehash.phash(a))},
          {"url": "https://example.test/r.png", "role": "reference", "label": "ref", "phash": str(imagehash.phash(a))}]
    cl = images.comparison_claims(images.compare(hs))
    assert cl and "not proof" in cl[0]["claim"] and cl[0]["signal"] is None


# ---------- reviews / calibration / discord ----------
async def test_review_authorization_and_calibration(tmp_path):
    db, jid = await run_job(tmp_path)
    from hypebot.bot import Handlers
    s = Settings(weights=W)
    h = Handlers(s, db, Analyzer(s, db, XClient(None)))
    i = MagicMock(); i.user.id = 1; i.user.roles = []; i.response.send_message = AsyncMock()
    await h.review(i, "minehood_rh", "confirm")
    assert "not an authorized" in i.response.send_message.await_args.args[0]
    i.user.id = 922754935806894091
    await h.review(i, "minehood_rh", "change_tier", 1, "my call")
    with db.Session() as ss:
        rv = ss.query(ManualReview).one(); res = ss.get(ResearchResult, rv.result_id)
        assert rv.reviewer_tier == 1 and res.tier == rv.bot_tier       # bot result untouched
    st = jobs.calibration_stats(db)
    assert st["reviews"] == 1 and st["exact_agreement"] in (0.0, 1.0)
    v = MagicMock(); v.response.send_message = AsyncMock()
    await h.verify(v, "minehood_rh")
    e = v.response.send_message.await_args.kwargs["embed"]
    assert "Verify" in e.author.name and e.title.startswith("🏆 Tier") and len(e) <= 6000 and any("Unverified" in f.name for f in e.fields)


async def test_update_research_posts_progress_and_report(tmp_path):
    db, jid = await run_job(tmp_path)
    from hypebot.bot import Handlers
    s = Settings(weights=W)
    h = Handlers(s, db, Analyzer(s, db, XClient(None)))
    with db.Session.begin() as ss:
        ss.get(ResearchJob, jid).progress_message_id = "55"
    ch = MagicMock(); ch.send = AsyncMock(return_value=MagicMock(id=99)); pm = MagicMock(); pm.edit = AsyncMock(); ch.get_partial_message.return_value = pm
    n = await h.update_research(AsyncMock(return_value=ch))
    assert n == 1 and "finished" in ch.send.await_args.kwargs["content"]
    with db.Session() as ss:
        j = ss.get(ResearchJob, jid)
        assert j.report_message_id == "99" and j.posted_fingerprint
    assert await h.update_research(AsyncMock(return_value=ch)) == 0



async def test_update_research_edits_existing_report_no_ping(tmp_path):
    """Supplementary re-finalize edits the same Discord message; does not send a second ping."""
    db, jid = await run_job(tmp_path)
    from hypebot.bot import Handlers
    s = Settings(weights=W)
    h = Handlers(s, db, Analyzer(s, db, XClient(None)))
    ch = MagicMock(); ch.send = AsyncMock(return_value=MagicMock(id=101))
    pm = MagicMock(); pm.edit = AsyncMock(); ch.get_partial_message.return_value = pm
    assert await h.update_research(AsyncMock(return_value=ch)) == 1
    ch.send.reset_mock(); pm.edit.reset_mock()
    with db.Session.begin() as ss:
        j = ss.get(ResearchJob, jid)
        assert j.report_message_id == "101"
        j.posted = False   # simulate feeder re-open after a meaningful change
        # bump fingerprint by tweaking stored report so edit path runs
        r = ss.get(ResearchResult, j.result_id)
        rep = dict(r.report); rep["verdict_text"] = (rep.get("verdict_text") or "") + " · refreshed"
        # ResearchResult is append-only — create a new result row instead
        from hypebot.research.models import ResearchResult as RR
        nr = RR(job_id=j.id, project_id=j.project_id, snapshot_id=r.snapshot_id,
                algorithm_version=r.algorithm_version, category=r.category, stage=r.stage,
                verdict=r.verdict, tier=r.tier, raw_tier=r.raw_tier, overall_score=r.overall_score,
                hype_score=r.hype_score, quality_score=r.quality_score, confidence=r.confidence,
                confidence_label=r.confidence_label, report=rep, finalization_status=r.finalization_status)
        ss.add(nr); ss.flush(); j.result_id = nr.id
        j.posted_fingerprint = "stale"   # force mismatch so we deliver the update
    n = await h.update_research(AsyncMock(return_value=ch))
    assert n == 1
    ch.send.assert_not_awaited()
    pm.edit.assert_awaited()
    assert "updated" in pm.edit.await_args.kwargs["content"]


async def test_update_research_skips_identical_fingerprint(tmp_path):
    db, jid = await run_job(tmp_path)
    from hypebot.bot import Handlers
    from hypebot.research import jobs as rjobs
    s = Settings(weights=W)
    h = Handlers(s, db, Analyzer(s, db, XClient(None)))
    ch = MagicMock(); ch.send = AsyncMock(return_value=MagicMock(id=55))
    pm = MagicMock(); pm.edit = AsyncMock(); ch.get_partial_message.return_value = pm
    assert await h.update_research(AsyncMock(return_value=ch)) == 1
    ch.send.reset_mock(); pm.edit.reset_mock()
    with db.Session.begin() as ss:
        j = ss.get(ResearchJob, jid)
        j.posted = False   # feeder flipped the flag, but content unchanged
        # fingerprint already matches posted_fingerprint from first delivery
    assert await h.update_research(AsyncMock(return_value=ch)) == 0
    ch.send.assert_not_awaited()
    # progress edit may still happen; report must not be re-sent
    assert all("finished" not in str(c) and "updated" not in str(c)
               for c in (pm.edit.await_args_list or []))


async def test_expired_interaction_falls_back_to_channel(tmp_path):
    from hypebot.bot import Handlers
    s, db, a = setup_db(tmp_path)
    h = Handlers(s, db, a)
    i = MagicMock(); i.response.is_done = MagicMock(return_value=False)
    resp = MagicMock(status=404, reason="Not Found")
    import discord
    i.response.defer = AsyncMock(side_effect=discord.NotFound(resp, {"code": 10062, "message": "Unknown interaction"}))
    i.channel.send = AsyncMock(return_value=MagicMock(id=7)); i.edit_original_response = AsyncMock()
    i.channel_id = 1; i.guild_id = 2; i.user.id = 3; i.id = 4
    await h.analyze(i, "Proj", "proj")
    texts = [str(c.args[0] if c.args else c.kwargs.get("content")) for c in i.channel.send.await_args_list]
    assert any("late" in t for t in texts) and any("Research job" in t for t in texts)
    i.edit_original_response.assert_not_awaited()


def test_hysteresis_keeps_tier_near_boundary():
    from hypebot.research import engine
    snap = {"hype_score": 60, "confidence": 0.85, "confidence_label": "High", "missing": [], "flags": [],
            "metrics": {"mentions_sample": 50, "independent_authors": 25, "promo_or_low_effort_ratio": 0.4}}
    ev = [{"signal": "official_link", "value": 1, "source_url": "https://example.test/a", "verified": True, "section": "identity",
           "kind": "observation", "claim": "c", "data": {"from_kind": "x", "to_kind": "website"}},
          {"signal": "team_prior_collection", "value": 0.0, "source_url": "https://example.test/t", "verified": True,
           "section": "team", "kind": "observation", "claim": "t"},
          {"signal": "milestone", "value": 0.0, "source_url": "https://example.test/m", "verified": True,
           "section": "market", "kind": "observation", "claim": "m"}]
    snap, ev = complete(snap, ev)
    for hs in range(30, 95):      # pick a hype input that lands near a tier boundary
        snap["hype_score"] = hs
        r = evaluate_research(snap=snap, evidence=ev, category={"primary": "unknown", "stage": "pre_mint"}, handle="p")
        sc = r["overall_score_internal"]
        if r["tier"] and min(abs(sc - b) for b in (80, 60, 40)) < HYSTERESIS - 0.5:
            break
    # find nearest boundary and fabricate a previous result on the other side of it
    lo = min((80, 60, 40), key=lambda b: abs(b - sc))
    assert abs(sc - lo) < HYSTERESIS, sc
    prev_tier = r["tier"] - 1 if sc < lo else r["tier"] + 1
    prev = dict(r, tier=prev_tier, verdict_text=f"Tier {prev_tier}")
    r2 = evaluate_research(snap=snap, evidence=ev, category={"primary": "unknown", "stage": "pre_mint"}, handle="p", previous=prev)
    assert r2["tier"] == prev_tier and r2["raw_tier"] == r["tier"] and "kept" in r2["hysteresis"]


def test_headline_shows_tier_or_labeled_estimate():
    from hypebot.research.embeds import headline
    final = {"tier": 2, "overall_score": 64.2, "hype_score": 64.4, "quality_score": 58.1, "verdict": "Tier", "confidence_label": "Medium"}
    assert headline(final)[0] == "🏆 Tier 2 — Hype 64 / Quality 58"
    nmr = {"tier": None, "overall_score": None, "overall_score_internal": 68.0, "hype_score": 67.1,
           "quality_score": None, "quality_score_internal": 68.5, "verdict": NMR, "confidence_label": "Low"}
    h, t = headline(nmr)
    assert t == 2 and h.startswith("🏆 Tier 2 (provisional — Needs More Research, confidence Low)") and "~65" in h and "67.1" not in h
    assert headline(dict(nmr, overall_score_internal=None))[1] is None


async def test_report_embed_title_has_tier(tmp_path):
    db, jid = await run_job(tmp_path)
    from hypebot.research.embeds import report_embed
    with db.Session() as s:
        res = s.get(ResearchResult, s.get(ResearchJob, jid).result_id)
        from hypebot.db import Project
        p = s.get(Project, res.project_id)
    for v in (False, True):
        e = report_embed(p, res, verify=v)
        assert e.title.startswith("🏆 Tier") and e.author.name and ("FINALIZED" in e.description or "INCOMPLETE" in e.description)
