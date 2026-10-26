"""Sorsa integration + Smart Account Signal (s1.0). All HTTP is mocked — no live Sorsa quota is ever used."""
import json, pathlib, sys
from datetime import timedelta
import httpx, pytest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_research import run_job, setup_db, fixture_payload
from test_r2 import SNAP, FIX
from hypebot.db import Database, utcnow, Project
from hypebot.research import sorsa, accounts, jobs
from hypebot.research.sorsa import SorsaClient, SorsaUnavailable, fetch_project_pack
from hypebot.research.engine import evaluate_research
from hypebot.research.checklist import CHECKS, ADVISORY, coverage
from hypebot.research.models import (SorsaApiLog, SorsaCache, TrackedAccount, AccountProjectLink, ResearchJob,
                                     ResearchResult, SorsaProjectSnapshot)

TOP = {"users": [
    {"username": "SmallHunter", "score": 140, "followers_count": 2100},
    {"username": "BigKol", "score": 1900, "followers_count": 450000},
    {"username": "RandomWhale", "score": 900, "followers_count": 80000},
    {"username": "spammy", "score": 300, "followers_count": 900},
]}


def mock_http(calls, overrides=None):
    data = {"score": (200, {"score": 212.5}), "followers-stats": (200, {"followers_count": 40, "influencers_count": 25,
            "projects_count": 12, "venture_capitals_count": 3, "user_protected": False}),
            "top-followers": (200, TOP), "score-changes": (200, {"week_delta": 12, "month_delta": 30}),
            "new-followers-7d": (200, {"users": []}),
            "key-usage-info": (200, {"key_requests": 5, "remaining_requests": 95, "total_requests": 100, "valid_until": "2027-01-01"})}
    data.update(overrides or {})

    def h(req):
        ep = req.url.path.rsplit("/", 1)[-1]
        calls.append((ep, dict(req.url.params), req.headers.get("ApiKey")))
        st, js = data[ep]
        return httpx.Response(st, json=js)
    return httpx.Client(transport=httpx.MockTransport(h))


@pytest.fixture
def db(tmp_path):
    return Database(f"sqlite:///{tmp_path}/s.db")


def client(db, calls, key="k", overrides=None, min_remaining=10):
    return SorsaClient(db, key, 24, min_remaining, http=mock_http(calls, overrides))


# ---------------- client ----------------
def test_score_stats_top_followers_parsed_and_auth_header(db):
    calls = []
    p = fetch_project_pack(db, client(db, calls), "@Proj")
    assert p["score"] == 212.5 and p["followers_stats"]["venture_capitals_count"] == 3
    assert [u["username"] for u in p["top_followers"]][:2] == ["smallhunter", "bigkol"]
    # unknown remaining -> one key-usage-info refresh (95 left) -> optional endpoints allowed (r2.2)
    assert [c[0] for c in calls] == ["score", "followers-stats", "top-followers", "key-usage-info",
                                     "score-changes", "new-followers-7d"]
    assert all(c[1] == {"username": "proj"} and c[2] == "k" for c in calls if c[0] != "key-usage-info")
    assert p["api_calls_used"] == 6 and p["unavailable"] == [] and p["week_delta"] == 12


def test_cache_hit_uses_zero_api_calls(db):
    calls = []
    fetch_project_pack(db, client(db, calls), "proj")
    calls2 = []
    p = fetch_project_pack(db, client(db, calls2), "PROJ")
    assert calls2 == [] and p["api_calls_used"] == 0 and p["cache_hits"] == 5 and p["score"] == 212.5
    with db.Session() as s:
        assert s.query(SorsaApiLog).filter(SorsaApiLog.cached.is_(True)).count() == 5


def test_in_process_dedupe(db):
    calls = []
    c = client(db, calls)
    c.get("score", "x"); c.get("score", "@X")
    assert len(calls) == 1


def test_missing_key_is_unavailable_not_zero(db):
    calls = []
    p = fetch_project_pack(db, client(db, calls, key=None), "proj")
    assert calls == [] and p["score"] is None and p["followers_stats"] is None and p["top_followers"] is None
    assert any("SORSA_API_KEY" in u["reason"] for u in p["unavailable"])


def test_404_cached_as_permanent_miss(db):
    calls = []
    c = client(db, calls, overrides={"score": (404, {"error": "not found"})})
    with pytest.raises(SorsaUnavailable) as e:
        c.get("score", "ghost")
    assert e.value.status == 404 and e.value.permanent
    calls2 = []
    with pytest.raises(SorsaUnavailable):
        client(db, calls2).get("score", "ghost")
    assert calls2 == []                     # the miss is not paid for twice


def test_quota_soft_stop_and_force(db):
    calls = []
    c = client(db, calls, overrides={"key-usage-info": (200, {"remaining_requests": 5})})
    assert c.key_usage()["remaining_requests"] == 5 and c.remaining() == 5
    with pytest.raises(SorsaUnavailable) as e:
        c.get("score", "a")
    assert "soft-stop" in e.value.reason and [x[0] for x in calls] == ["key-usage-info"]
    assert c.get("score", "a", force=True)["score"] == 212.5
    assert c.remaining() == 4               # decremented locally after a live call


def test_optional_endpoints_only_with_quota_headroom(db):
    calls = []
    c = client(db, calls)
    c.key_usage()                           # remaining 95 > 40
    p = fetch_project_pack(db, c, "proj")
    assert p["week_delta"] == 12 and p["new_followers_7d"] == []
    assert {x[0] for x in calls} == {"key-usage-info", "score", "followers-stats", "top-followers", "score-changes", "new-followers-7d"}


# ---------------- accounts / smart signal ----------------
def acc(handle, rel="provisional", followers=None, flags=None, known=0, hits=0, sources=("history_mining",)):
    return {"handle": handle, "followers_count": followers, "sorsa_score": None, "reliability_label": rel, "confidence": 0.5,
            "flags": flags or {}, "sources": list(sources),
            "history": {"known_outcomes": known, "hits_tier_1_2": hits, "early_in_sample": hits} if known else {}}


TRACKED = {"smallhunter": acc("smallhunter", "high", 2100, known=6, hits=5),
           "bigkol": acc("bigkol", "provisional", 450000),
           "spammy": acc("spammy", "low", 900, {"spam_promoter": True}, known=3, hits=0),
           "seedonly": acc("seedonly", sources=("seed_list",))}


def pack(**over):
    p = {"score": 212.5, "followers_stats": {"followers_count": 40, "influencers_count": 25, "projects_count": 12,
                                             "venture_capitals_count": 3}, "unavailable": [],
         "top_followers": [sorsa.compact_user(u) for u in TOP["users"]], "new_followers_7d": None}
    p.update(over)
    return p


def test_reliability_needs_history_and_shrinks_small_samples():
    assert accounts.reliability(2, 2, 0.1, {})[0] == "provisional"          # 2/2 wins is not enough
    assert accounts.reliability(3, 3, 0.1, {})[0] == "medium"               # shrunk 0.8 but n<5 -> not high
    assert accounts.reliability(6, 5, 0.1, {})[0] == "high"
    assert accounts.reliability(10, 10, 0.1, {"spam_promoter": True})[0] == "low"


def test_smart_signal_matches_discoverers_ignores_big_accounts_without_history():
    sg = accounts.smart_signal(pack=pack(), tracked=TRACKED, hype_score=35)
    by = {m["handle"]: m for m in sg["matched_accounts"]}
    assert by["smallhunter"]["class"] == "small_discoverer"
    assert by["bigkol"]["class"] == "large_influential" and by["bigkol"]["reliability"] == "provisional"
    assert by["spammy"]["class"] == "low_quality_suspect"
    assert sg["smart_account_signal"]["independent_credible_accounts"] == 1     # only smallhunter counts
    notable = {n["handle"] for n in sg["notable_unhistoried_followers"]}
    assert "randomwhale" in notable and "randomwhale" not in by                   # high score alone != smart
    assert any("top-20" in u for u in sg["unverified"])                           # incomplete list stated
    assert any("follow dates" in u for u in sg["unverified"])                     # no follow date -> recency unverified
    assert by["smallhunter"]["follow_verified_at"] is None
    assert sg["early_attention"] is False                                         # needs >= 2 independent credible


def test_early_attention_for_low_popularity_with_two_discoverers():
    t = dict(TRACKED, hunter2=acc("hunter2", "medium", 700, known=4, hits=3))
    p = pack(top_followers=pack()["top_followers"] + [{"username": "hunter2", "score": 50, "followers_count": 700, "follower_date": None}])
    sg = accounts.smart_signal(pack=p, tracked=t, hype_score=30)
    assert sg["early_attention"] and "not a tier boost" in sg["early_attention_reason"]
    assert accounts.smart_signal(pack=p, tracked=t, hype_score=80)["early_attention"] is False


def test_duplicate_sources_count_as_one_independent_account():
    sg = accounts.smart_signal(pack=pack(), tracked=TRACKED, hype_score=30,
                               independent=[{"handle": "SmallHunter", "relationship": "mentioned", "url": "https://x.com/a/status/1"}])
    m = [x for x in sg["matched_accounts"] if x["handle"] == "smallhunter"]
    assert len(m) == 1 and m[0]["source_families"] == ["independent", "sorsa"]
    assert sg["smart_account_signal"]["independent_credible_accounts"] == 1


def test_coordinated_follow_burst_flagged():
    nf = [{"username": f"bot{i}", "score": 5, "followers_count": 50, "follower_date": "2026-10-20T10:15:31Z"} for i in range(3)]
    t = dict(TRACKED, bot0=acc("bot0", "medium", 50, known=3, hits=2))
    sg = accounts.smart_signal(pack=pack(new_followers_7d=nf), tracked=t, hype_score=30)
    assert len(sg["coordinated_groups"]) == 1
    assert {m["handle"]: m["class"] for m in sg["matched_accounts"]}["bot0"] == "low_quality_suspect"


def test_seed_without_history_is_not_smart():
    p = pack(top_followers=[{"username": "seedonly", "score": 800, "followers_count": 3000, "follower_date": None}])
    sg = accounts.smart_signal(pack=p, tracked={"seedonly": TRACKED["seedonly"]}, hype_score=30)
    assert sg["smart_account_signal"]["label"] == "insufficient history"
    assert sg["matched_accounts"][0]["class"] == "insufficient_evidence"
    assert "no recorded discovery history" in sg["matched_accounts"][0]["why"]


def test_conflicts_recorded_not_resolved():
    sg = accounts.smart_signal(pack=pack(), tracked=TRACKED, hype_score=30, independent_flags={"promo_or_low_effort_ratio": 0.8})
    types = {c["type"] for c in sg["conflicts"]}
    assert "credibility_vs_engagement" in types and "account_quality" in types   # spammy has Sorsa 300 but spam history
    low = pack(score=3.0, followers_stats={"followers_count": 1, "influencers_count": 0, "projects_count": 0, "venture_capitals_count": 0})
    sg2 = accounts.smart_signal(pack=low, tracked={}, independent_flags={"respected_engagements": 3})
    assert any(c["type"] == "engagement_vs_graph" for c in sg2["conflicts"])


def test_no_sorsa_is_unavailable_and_not_penalized():
    sg = accounts.smart_signal(pack=None, tracked=TRACKED)
    assert sg["sorsa_social_credibility"]["label"] == "unavailable" and sg["smart_account_signal"]["label"] == "unavailable"
    base = evaluate_research(snap=SNAP, evidence=[], category={"primary": "pfp", "stage": "pre_mint"}, handle="p")
    with_none = evaluate_research(snap=SNAP, evidence=[], category={"primary": "pfp", "stage": "pre_mint"}, handle="p", social=sg)
    with_good = evaluate_research(snap=SNAP, evidence=[], category={"primary": "pfp", "stage": "pre_mint"}, handle="p",
                                  social=accounts.smart_signal(pack=pack(), tracked=TRACKED, hype_score=70))
    for r in (with_none, with_good):   # separate signals never move score, confidence, coverage or tier
        for k in ("overall_score_internal", "quality_score_internal", "confidence", "coverage", "suggested_tier", "tier", "status"):
            assert r[k] == base[k], k
    assert with_none["checklist"]["sorsa_social_graph"]["status"] == "NOT_APPLICABLE"
    assert with_good["checklist"]["sorsa_social_graph"]["status"] == "PARTIAL"     # conflict (spammy) recorded
    assert set(with_good["checklist"]) == set(CHECKS)
    assert not any(with_good["checklist"][k]["mandatory"] for k in ADVISORY)
    s = with_good["signals"]
    assert s["affects_tier"] is False and s["weights_changed"] is False
    assert set(s) >= {"overall_popularity", "smart_account_signal", "sorsa_social_credibility", "project_quality", "research_confidence"}


def test_wrong_identity_records_conflict():
    ev = [{"section": "identity", "kind": "observation", "signal": "official_link", "value": 0.0, "claim": "OpenSea lists @other",
           "source_url": "https://opensea.io/x", "verified": True}]
    r = evaluate_research(snap=SNAP, evidence=ev, category={"primary": "pfp", "stage": "pre_mint"}, handle="p",
                          social=accounts.smart_signal(pack=pack(), tracked={}, hype_score=70))
    assert r["checklist"]["identity"]["status"] == "FAILED"
    assert any(c["type"] == "identity" for c in r["signals"]["social_conflicts"])
    assert r["checklist"]["sorsa_social_graph"]["status"] == "PARTIAL"


def test_coverage_excludes_advisory_checks():
    cl = {"a": {"status": "VERIFIED"}, "sorsa_social_graph": {"status": "FAILED"}, "smart_follower_overlap": {"status": "FAILED"}}
    assert coverage(cl) == 1.0


# ---------------- history mining (existing data only) ----------------
def _mk_snap(db, handle, mentions, collected="2026-10-20T12:00:00Z"):
    from hypebot.db import Snapshot
    with db.Session.begin() as s:
        p = db.upsert_project(s, handle, handle)
        users = [{"id": a, "username": a, "public_metrics": {"followers_count": 500}} for a, _, _ in mentions]
        tw = [{"id": str(i), "author_id": a, "text": t, "created_at": at} for i, (a, at, t) in enumerate(mentions)]
        s.add(Snapshot(project_id=p.id, raw_data={"x": {"collected_at": collected, "mentions": {"tweets": tw, "users": users}}},
                       metrics={}, sources=[], components={}, missing=[], flags=[], weights={}, confidence=0.5,
                       confidence_label="Low", provisional=True, trend="new"))
        return p.id


def test_mine_history_only_from_stored_data_and_provisional(db):
    _mk_snap(db, "proja", [("alice", "2026-10-01T00:00:00Z", "This art direction is genuinely fresh, watching closely"),
                           ("bob", "2026-10-02T00:00:00Z", "gib wl spots pls")])
    _mk_snap(db, "projb", [("alice", "2026-10-03T00:00:00Z", "Strong team, prior collection sold out"),
                           ("carol", "2026-10-04T00:00:00Z", "nice")])
    st = accounts.mine_history(db)
    assert st["created"] == 1                              # only alice spans >= 2 projects
    with db.Session() as s:
        a = s.query(TrackedAccount).filter_by(x_handle="alice").one()
        assert a.reliability_label == "provisional"        # no known outcomes -> never inferred
        assert a.history["known_outcomes"] == 0 and a.history["projects_mentioned"] == 2
        assert "not verified first discovery" in a.history["caveat"]
        links = s.query(AccountProjectLink).filter_by(account_id=a.id).all()
        assert len(links) == 2 and all(l.follow_date is None and l.relationship == "mentioned" for l in links)
    accounts.mine_history(db)                              # idempotent: no duplicate links
    with db.Session() as s:
        assert s.query(AccountProjectLink).count() == 2


def test_seed_import_validates_and_does_not_label_smart(db, tmp_path):
    f = tmp_path / "seeds.csv"
    f.write_text("handle,notes,category_hints\n@GoodHunter,found X early,alpha_caller;collector\nbad handle!,,\n")
    r = accounts.import_seeds(db, accounts.load_seed_file(str(f)))
    assert r["added"] == 1 and r["rejected"] == ["bad handle!"]
    with db.Session() as s:
        a = s.query(TrackedAccount).one()
        assert a.x_handle == "goodhunter" and a.reliability_label == "provisional" and a.category_hints == ["alpha_caller", "collector"]
        assert accounts.classify(accounts._acc_dict(a)) == "insufficient_evidence"
    repo_seed = json.loads((pathlib.Path(__file__).resolve().parents[1] / "seed_accounts.json").read_text())
    # Seed list is real alpha-hunter handles (provisional only — never auto-labeled smart).
    assert len(repo_seed["accounts"]) >= 1
    assert all(isinstance(a.get("handle"), str) and a["handle"] for a in repo_seed["accounts"])


# ---------------- pipeline / embeds / regression ----------------
async def test_job_pipeline_without_key_unchanged_and_reports_signals(tmp_path):
    db, jid = await run_job(tmp_path)
    with db.Session() as s:
        j = s.get(ResearchJob, jid)
        r = s.get(ResearchResult, j.result_id).report
    assert j.status == "FINALIZED"
    assert r["signals"]["sorsa_social_credibility"]["label"] == "unavailable"
    assert r["checklist"]["sorsa_social_graph"]["status"] == "NOT_APPLICABLE"
    assert j.steps["social"]["status"] in ("skipped", "done")
    with db.Session() as s:
        assert s.query(SorsaApiLog).filter(SorsaApiLog.cached.is_(False), SorsaApiLog.status == "200").count() == 0


async def test_job_pipeline_with_mocked_sorsa(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("SORSA_API_KEY", "test-key"); monkeypatch.setenv("SORSA_AUTO_FETCH", "true")
    real_init = SorsaClient.__init__
    def init(self, db, key, ttl=24, mr=10, http=None, base_url=sorsa.BASE_URL, **kw):
        real_init(self, db, key, ttl, mr, http=mock_http(calls), base_url=base_url, **kw)
    monkeypatch.setattr(SorsaClient, "__init__", init)
    s0, dbx, _ = setup_db(tmp_path)
    accounts.import_seeds(dbx, [{"handle": "SmallHunter"}])
    from test_research import run_job as rj
    import test_research
    monkeypatch.setattr(test_research, "setup_db", lambda tp: (s0, dbx, _))
    db, jid = await rj(tmp_path)
    assert len(calls) == 6          # 3 core + key-usage-info + 2 optional (remaining 95 > 20)
    with db.Session() as s:
        j = s.get(ResearchJob, jid)
        rep = s.get(ResearchResult, j.result_id).report
        link = s.query(AccountProjectLink).filter_by(relationship="followed").one()
        assert link.source == "sorsa_top_followers" and link.follow_date is None
    assert j.status == "FINALIZED"                                       # tier pipeline unaffected
    assert rep["signals"]["sorsa_social_credibility"]["score"] == 212.5
    assert rep["signals"]["smart_account_signal"]["label"] == "insufficient history"   # seed only, no outcomes
    from hypebot.research.embeds import report_embed
    with db.Session() as s:
        proj = s.get(Project, j.project_id); res = s.get(ResearchResult, j.result_id)
        e = report_embed(proj, res)
    names = [f.name for f in e.fields]
    assert any(n.startswith("🧭 Signals") for n in names) and names[0].startswith("Checklist")
    val = next(f.value for f in e.fields if f.name.startswith("🧭"))
    assert "@smallhunter" in val and "Sorsa **moderate**" in val and len(e) <= 6000


@pytest.mark.parametrize("name", ["GigaLegendsLTD", "Conso", "Minehood"])
@pytest.mark.parametrize("social_kind", ["none", "rich", "conflicting"])
def test_scan3_real_data_tiers_unchanged_with_signals(name, social_kind):
    f = FIX[name]
    cat = f["job"]["hints"].get("category") or {"primary": "unknown", "stage": "unknown"}
    kw = dict(snap=f["snapshot"], evidence=f["evidence"], category=cat, handle=f["project"]["handle"],
              hints=f["job"]["hints"], unavailable=f["job"]["unavailable"], attempts=[])
    a = evaluate_research(**kw)
    social = {"none": accounts.smart_signal(pack=None, tracked={}),
              "rich": accounts.smart_signal(pack=pack(), tracked=TRACKED, hype_score=a["hype_score"]),
              "conflicting": accounts.smart_signal(pack=pack(), tracked=TRACKED, hype_score=a["hype_score"],
                                                   independent_flags={"promo_or_low_effort_ratio": 0.9})}[social_kind]
    b = evaluate_research(**kw, social=social)
    for k in ("suggested_tier", "tier", "confidence", "confidence_label", "status", "overall_score_internal",
              "quality_score_internal", "coverage", "missing_mandatory", "verdict"):
        assert a[k] == b[k], (name, social_kind, k)


def test_key_usage_500_falls_back_to_local_estimate(db):
    calls = []
    c = SorsaClient(db, "k", 24, 10, http=mock_http(calls, {"key-usage-info": (500, {"message": "x"})}), quota_total=100)
    p = fetch_project_pack(db, c, "proj", optional_min_remaining=20)
    # 3 core + failed usage call = 4 logged live -> estimate 96 -> optional allowed
    assert [x[0] for x in calls][-2:] == ["score-changes", "new-followers-7d"] and p["remaining_quota"] == 94


def test_no_quota_total_keeps_skip_when_unknown(db):
    calls = []
    c = SorsaClient(db, "k", 24, 10, http=mock_http(calls, {"key-usage-info": (500, {"message": "x"})}))
    p = fetch_project_pack(db, c, "proj", optional_min_remaining=20)
    assert "new-followers-7d" not in [x[0] for x in calls]
    assert any("unknown" in u["reason"] for u in p["unavailable"])
