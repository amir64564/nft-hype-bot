"""Regression + rule tests for scoring algorithm v2 (deterministic, evidence-based)."""
import copy, json, pathlib, random
from datetime import datetime, timedelta, timezone
import pytest
from hypebot.config import Settings, load_weights, DEFAULT_WEIGHTS
from hypebot.external_x import to_x_data, IngestError
from hypebot.metrics import derive_metrics, _dt
from hypebot.pipeline import evaluate, Analyzer
from hypebot.scoring import (score, trend, c_engagement, c_community, c_influential, c_growth,
                             c_consistency, confidence_model, ALGORITHM_VERSION)
from hypebot.db import Database, Snapshot, SnapshotRecalc

ROOT = pathlib.Path(__file__).resolve().parents[1]
from tests._paths import ingest_path
REAL = {"minehood": ("req1", "Minehood", "minehood_rh"), "brixes2": ("req2", "Brixes", "brixes_xyz"),
        "brixes": ("req2_full", "Brixes", "brixes_xyz"), "lildudes": ("req6", "LilDudes", "lildudes_"),
        "giga": ("req7", "GigaLegendsLTD", "GigaLegendsLTD"), "nuloraio": ("req8", "Nuloraio", "nuloraio"),
        "inkvire": ("inkvire", "Inkvire", "inkvire")}
S = Settings(weights=load_weights(str(ROOT / "weights.json")))


def payload(key):
    return json.loads(ingest_path(REAL[key][0]).read_text())


def run(key, p=None, age_h=0.0, earlier=()):
    p = p or payload(key)
    x = to_x_data(p, REAL[key][2])
    return evaluate(S, REAL[key][1], x, _dt(p["collected_at"]) + timedelta(hours=age_h), list(earlier))


def base_payload(n_posts=12, n_ment=60, likes=300, followers=8000, text=None, author=None, views=True):
    t0 = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"source": "grokbot-x-read", "collected_at": iso(t0),
            "account": {"username": "proj", "name": "Proj", "followers": followers, "verified": True,
                        "verified_type": "blue", "description": "Proj NFT", "links": ["https://p.x"]},
            "recent_posts": [{"id": f"p{i}", "created_at": iso(t0 - timedelta(hours=10 * i + 1)), "likes": likes,
                              "replies": likes // 5, "reposts": likes // 4, "quotes": 1, "author_handle": "proj",
                              **({"views": likes * 30} if views else {})} for i in range(n_posts)],
            "mentions": [{"id": f"m{i}", "author_handle": author or f"user{i}", "created_at": iso(t0 - timedelta(minutes=7 * i)),
                          "text": text or f"@proj I really like how the art {i} looks and the roadmap number {i}",
                          "author_followers": 900, "author_created_at": "2020-01-01T00:00:00Z"} for i in range(n_ment)],
            "mention_counts": [{"start": iso(t0 - timedelta(days=d + 1)), "end": iso(t0 - timedelta(days=d)),
                                "tweet_count": 1500} for d in range(7)]}


def ev_payload(p, earlier=(), age_h=0.0):
    x = to_x_data(p, "proj")
    return evaluate(S, "Proj", x, _dt(p["collected_at"]) + timedelta(hours=age_h), list(earlier))


# ---------- determinism ----------
@pytest.mark.parametrize("key", list(REAL))
def test_identical_input_identical_result(key):
    outs = {json.dumps([r.hype_score, r.tier, r.confidence, r.confidence_label, r.components], sort_keys=True)
            for _, r, _ in (run(key) for _ in range(5))}
    assert len(outs) == 1


@pytest.mark.parametrize("key", ["minehood", "inkvire", "giga"])
def test_order_independent(key):
    p = payload(key)
    a = run(key, p)[1]
    q = copy.deepcopy(p)
    rnd = random.Random(7)
    rnd.shuffle(q["recent_posts"]); rnd.shuffle(q["mentions"]); rnd.shuffle(q["mention_counts"])
    b = run(key, q)[1]
    assert (a.hype_score, a.tier, a.confidence, a.components) == (b.hype_score, b.tier, b.confidence, b.components)


def test_wall_clock_does_not_change_metrics():
    p = payload("minehood")
    x = to_x_data(p, "minehood_rh")
    assert derive_metrics(x) == derive_metrics(copy.deepcopy(x))
    assert derive_metrics(x)["as_of"].startswith("2026-10-21T14:28:30")


def test_reingest_same_collection_not_growth():
    """Minehood issue A: re-ingesting the same collection must not create 'history'."""
    class Fake:  # earlier snapshot holding the SAME collection
        id = 1; raw_data = {"x": to_x_data(payload("minehood"), "minehood_rh")}
    a = run("minehood")[1]; b = run("minehood", earlier=[Fake()])[1]
    assert a.hype_score == b.hype_score and a.components == b.components


# ---------- samples ----------
def test_zero_posts_no_official_points():
    m, r, _ = run("inkvire")
    assert m["sample_tweets"] == 0 and r.components["engagement_quality"]["points"] is None
    assert "engagement_quality" in r.missing and r.tier is None and r.confidence_label == "Low"


def test_one_post_capped_and_not_high():
    m, r, _ = run("giga")
    assert m["sample_tweets"] == 1
    assert r.components["engagement_quality"]["score"] <= 0.325 + 1e-9
    assert r.confidence_label == "Low" and r.tier is None and r.provisional
    assert "posting_consistency" in r.missing


def test_small_sample_not_ranked_brixes():
    m, r, _ = run("brixes2")
    assert m["sample_tweets"] == 2 and r.tier is None and r.confidence_label == "Low"
    full = run("brixes")[1]
    assert full.tier == 3


def test_engagement_concentrated_in_one_post():
    p = base_payload(n_posts=10, likes=100)
    q = copy.deepcopy(p); q["recent_posts"][0].update(likes=200_000, views=5_000_000)
    a, b = ev_payload(p)[1], ev_payload(q)[1]
    assert b.components["engagement_quality"]["score"] - a.components["engagement_quality"]["score"] < 0.25
    assert b.confidence_label != "High" and any("single post" in f for f in b.flags)
    assert b.tier != 1


def test_high_followers_weak_engagement():
    big = ev_payload(base_payload(followers=2_000_000, likes=20))[1]
    small = ev_payload(base_payload(followers=5_000, likes=600))[1]
    assert small.hype_score > big.hype_score and big.tier in (3, 4, None)


def test_missing_metrics_excluded_not_zero():
    p = base_payload(); del p["mention_counts"]; del p["mentions"]
    for post in p["recent_posts"]:
        post.pop("views")
    m, r, _ = ev_payload(p)
    assert m["avg_views"] is None and "influential_engagement" in r.missing
    assert r.components["influential_engagement"]["points"] is None and r.coverage < 1
    assert r.components["cross_platform"]["status"] == "not_collected"


def test_missing_history_ok():
    _, r, ev = ev_payload(base_payload())
    assert ev["baseline_snapshot_id"] is None and r.confidence_parts["history"] == 0.0


# ---------- community / risk ----------
def test_duplicate_text_and_repeated_authors():
    clean = ev_payload(base_payload())[1]
    dup = ev_payload(base_payload(text="@proj huge alpha join whitelist now before close"))[1]
    rep = ev_payload(base_payload(author="farmer1"))[1]
    assert dup.components["community_discussion"]["score"] < clean.components["community_discussion"]["score"]
    assert rep.components["community_discussion"]["score"] < clean.components["community_discussion"]["score"]
    assert any("duplicate" in f for f in dup.flags) and any("repeat" in f for f in rep.flags)


def test_wallet_address_farming_is_not_independent():
    m, r, _ = ev_payload(base_payload(text="@proj 0x3C85533A5789b7143fDaAACDcB0fdC4FFDa37274"))
    assert m["independent_authors"] == 0 and m["wallet_address_ratio"] == 1.0
    assert any("wallet" in f for f in r.flags)


def test_strong_independent_community_beats_promo():
    strong = c_community(derive_metrics(to_x_data(base_payload(n_ment=80), "proj")))[0]
    promo = c_community(derive_metrics(to_x_data(base_payload(n_ment=80, text="@proj gm"), "proj")))[0]
    assert strong > promo


def test_mentions_dedup_and_self_mentions_excluded():
    p = base_payload(n_ment=20)
    p["mentions"] += copy.deepcopy(p["mentions"][:5])            # duplicates by id
    p["mentions"].append({"id": "self", "author_handle": "proj", "created_at": p["collected_at"], "text": "@proj us"})
    m = derive_metrics(to_x_data(p, "proj"))
    assert m["mentions_supplied"] == 26 and m["mentions_sample"] == 20 and m["mention_unique_authors"] == 20


def test_influential_one_thread_not_maxed():
    """Inkvire issue D: many big accounts replying under one promoter thread can't max the category."""
    m, r, _ = run("inkvire")
    assert m["influential_mentioners"] == 17 and m["influential_threads"] <= 3
    assert r.components["influential_engagement"]["score"] < 1.0


def test_influential_low_effort_not_counted():
    p = base_payload(text="@proj gm")
    for x in p["mentions"]:
        x["author_followers"] = 50_000
    m = derive_metrics(to_x_data(p, "proj"))
    assert m["influential_mentioners"] == 60 and m["influential_relevant"] == 0
    assert c_influential(m)[0] == 0.0


# ---------- data validation ----------
def test_wrong_project_identity_rejected():
    p = base_payload(); p["account"]["username"] = "someoneelse"
    with pytest.raises(IngestError, match="identity"):
        to_x_data(p, "proj")


def test_foreign_and_reply_posts_excluded():
    p = base_payload(n_posts=10)
    p["recent_posts"][0]["author_handle"] = "otheracct"
    p["recent_posts"][1]["type"] = "reply"
    p["recent_posts"].append(copy.deepcopy(p["recent_posts"][2]))
    m = derive_metrics(to_x_data(p, "proj"))
    assert m["sample_tweets"] == 8 and m["posts_excluded_foreign"] == 1
    assert m["posts_excluded_replies"] == 1 and m["posts_excluded_duplicates"] == 1


def test_incorrect_metric_units_rejected_or_flagged():
    p = base_payload(); p["account"]["followers"] = 12.5   # "12.5K"
    with pytest.raises(IngestError, match="raw integer"):
        to_x_data(p, "proj")
    q = base_payload()
    for post in q["recent_posts"]:
        post["views"] = 3       # views in thousands?
    m, r, _ = ev_payload(q)
    assert m["unit_anomaly_posts"] == 12 and any("units" in f for f in r.flags)


def test_failed_collection_marks_request_failed(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    a = Analyzer(Settings(weights=dict(DEFAULT_WEIGHTS)), db)
    import asyncio
    o = asyncio.run(a.analyze("Proj", "proj"))
    with pytest.raises(IngestError):
        a.ingest_external(o.x_request_id, {"source": "grokbot-x-read"})
    from hypebot.db import XRequest
    with db.Session() as s:
        r = s.get(XRequest, o.x_request_id)
        assert r.status == "failed" and r.result_snapshot_id is None
        assert s.query(Snapshot).count() == 0


def test_stale_data_lowers_confidence():
    fresh = ev_payload(base_payload())[1]
    stale = ev_payload(base_payload(), age_h=200)[1]
    assert stale.confidence < fresh.confidence and stale.confidence_label == "Low" and stale.tier is None


# ---------- bounds ----------
@pytest.mark.parametrize("key", list(REAL))
def test_bounds(key):
    _, r, _ = run(key)
    assert 0 <= r.hype_score <= 100 and 0 <= r.confidence <= 1
    for c in r.components.values():
        if c["points"] is not None:
            assert 0 <= c["points"] <= c["max"] + 1e-9


def test_extreme_inputs_stay_in_range():
    m = {"sample_tweets": 10**6, "median_engagement": 10**12, "trimmed_mean_engagement": 10**12, "median_views": 10**15,
         "engagement_rate": 1e9, "conversation_ratio": 50, "mentions_7d_total": 10**12, "mentions_peak_per_day": 10**12,
         "mentions_sample": 100, "mention_unique_authors": 100, "independent_authors": 100,
         "mentions_recent_per_day": 10**12, "mention_velocity_change": 1e9, "influential_relevant": 10**6,
         "influential_threads": 10**6, "author_followers_coverage": 1, "posts_per_week": 5, "post_gap_cv": 0,
         "days_since_last_post": 0, "verified": True, "has_bio": True, "has_links": True, "real_author_ratio": 1}
    r = score(m, prev_metrics={"followers": 1}, prev_days=1)
    assert r.hype_score <= 100 and all(c["points"] is None or c["points"] <= c["max"] for c in r.components.values())


def test_confidence_not_high_with_thin_evidence():
    for key in ("giga", "inkvire", "brixes2", "nuloraio"):
        assert run(key)[1].confidence_label != "High", key


def test_confidence_independent_of_hype_level():
    """Same evidence structure, 10x engagement -> same evidence parts and label. Only the
    cross-signal agreement part may differ (it measures coherence, not hype level)."""
    a = ev_payload(base_payload(likes=100))[1]; b = ev_payload(base_payload(likes=1000))[1]
    assert a.hype_score != b.hype_score and a.confidence_label == b.confidence_label
    pa = {k: v for k, v in a.confidence_parts.items() if k != "consistency"}
    assert pa == {k: v for k, v in b.confidence_parts.items() if k != "consistency"}


# ---------- snapshots / versions ----------
def test_successive_snapshots_growth_and_trend(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    a = Analyzer(Settings(weights=dict(DEFAULT_WEIGHTS)), db)
    p1 = base_payload(followers=8000)
    p2 = base_payload(followers=9600); p2["collected_at"] = "2026-10-21T12:00:00Z"
    s1 = a.rescore_from_file if False else None
    with db.Session.begin() as s:
        db.upsert_project(s, "Proj", "proj")
    o1 = a.rescore_from_file("proj", p1); o2 = a.rescore_from_file("proj", p2)
    o3 = a.rescore_from_file("proj", p2)   # same collection again
    assert o2.snapshot.evaluation["baseline_snapshot_id"] == o1.snapshot.id
    assert o2.snapshot.algorithm_version == ALGORITHM_VERSION
    assert o2.snapshot.components["growth_momentum"]["score"] > o1.snapshot.components["growth_momentum"]["score"]
    assert "Re-used X data" in (o3.snapshot.notes or "")
    assert o3.snapshot.trend in ("stable", "increasing", "decreasing")


def test_trend_not_across_versions():
    assert trend(80, 40, current_version="v2.0", previous_version="v1-legacy") == ("new", None)
    assert trend(80, 40, current_version="v2.0", previous_version="v2.0")[0] == "increasing"


def test_recalculation_preserves_originals(tmp_path):
    import shutil, sqlite3
    src = ROOT / "hypebot.db"
    if not src.exists():
        pytest.skip("no production db")
    dst = tmp_path / "copy.db"; shutil.copy(src, dst)
    before = sqlite3.connect(dst).execute("select id, hype_score, tier, confidence from snapshots order by id").fetchall()
    db = Database(f"sqlite:///{dst}")
    a = Analyzer(S, db)
    rows = a.recalculate_history()
    rows2 = a.recalculate_history()          # idempotent
    assert [r.id for r in rows] == [r.id for r in rows2]
    con = sqlite3.connect(dst)
    assert con.execute("select id, hype_score, tier, confidence from snapshots order by id").fetchall() == before
    assert {v for (v,) in con.execute("select distinct algorithm_version from snapshots")} <= {"v1-legacy", "v2.0", ALGORITHM_VERSION}
    assert all(r.original_algorithm_version for r in rows) and all(r.algorithm_version == ALGORITHM_VERSION for r in rows)
    with db.Session() as s:
        with pytest.raises(RuntimeError):
            r = s.get(SnapshotRecalc, rows[0].id); r.hype_score = 1; s.flush()
