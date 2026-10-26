import pytest
from hypebot.db import Database

@pytest.fixture
def db(tmp_path):
    return Database(f"sqlite:///{tmp_path}/s.db")
from hypebot.research import accounts as A, sorsa as S

def seed(h, **k):
    return {"handle": h, "followers_count": k.get("fc", 500), "sorsa_score": None, "reliability_label": k.get("rel", "provisional"),
            "confidence": 0, "flags": {}, "history": k.get("hist", {}), "sources": ["seed_list"]}

def test_seed_in_top_followers_matched_not_scored():
    r = A.smart_signal(pack={"score": 50, "followers_stats": {}, "top_followers": [{"username": "Alpha", "score": 300}]},
                       tracked={"alpha": seed("alpha")})
    sig = r["smart_account_signal"]
    assert sig["matched"] == 1 and sig["seed_matches"] == 1 and sig["seed_matches_followed"] == 1
    assert r["matched_accounts"][0]["relationship"] == "followed" and r["matched_accounts"][0]["is_seed"]
    assert sig["score"] in (None, 0) and sig["independent_credible_accounts"] == 0

def test_seed_only_mention_author_matched():
    r = A.smart_signal(pack=None, tracked={"beta": seed("beta")}, independent=[{"handle": "beta", "relationship": "mentioned", "url": "u"}])
    m = r["matched_accounts"][0]
    assert m["relationship"] == "mentioned" and m["source_families"] == ["independent"]
    assert r["smart_account_signal"]["seed_matches_mentioned"] == 1

def test_credible_seed_counts():
    h = {"known_outcomes": 5, "hits_tier_1_2": 4}
    r = A.smart_signal(pack=None, tracked={"g": seed("g", rel="high", hist=h)}, independent=[{"handle": "g"}])
    assert r["smart_account_signal"]["score"] > 0 and r["smart_account_signal"]["independent_credible_accounts"] == 1

class FakeClient:
    def __init__(self, rem, usage_rem=None):
        self.api_key, self.live_calls, self.cache_hits, self._usage_checked = "k", 0, 0, False
        self.rem, self.usage_rem, self.got, self.usage_calls = rem, usage_rem, [], 0
    def remaining(self): return self.rem
    def cached(self, ep, u): return None
    def key_usage(self): self.usage_calls += 1; self.rem = self.usage_rem
    def _log(self, *a, **k): pass
    def get(self, ep, u, force=False): self.got.append(ep); return {} if ep != "top-followers" else []

def run(db, c):
    return S.fetch_project_pack(db, c, "proj", None, optional_min_remaining=20)

def test_optional_when_rem_ok(db):
    c = FakeClient(50); run(db, c)
    assert "new-followers-7d" in c.got and "score-changes" in c.got and c.usage_calls == 0

def test_optional_skipped_when_low(db):
    c = FakeClient(15); out = run(db, c)
    assert "new-followers-7d" not in c.got
    assert any(u["endpoint"] == "new-followers-7d" and "remaining 15" in u["reason"] for u in out["unavailable"])

def test_rem_unknown_refreshed(db):
    c = FakeClient(None, usage_rem=60); run(db, c)
    assert c.usage_calls == 1 and "new-followers-7d" in c.got
