import asyncio, pytest
from hypebot.db import Database, Project
from hypebot.research import accounts as A, seed_position as SP
from hypebot.research.models import TrackedAccount, AccountProjectLink


@pytest.fixture
def db(tmp_path):
    return Database(f"sqlite:///{tmp_path}/p.db")


def _acc(s, h, **k):
    a = TrackedAccount(x_handle=h, sources=k.get("src", ["history_mining"]), flags=k.get("flags", {}),
                       history=k.get("hist", {}), confidence=0, reliability_label=k.get("rel", "provisional"),
                       followers_count=k.get("fc"), x_user_id=k.get("uid"))
    s.add(a); s.flush(); return a


def test_prune_seed_never_deleted_junk_deleted_keeper_kept(db):
    with db.Session.begin() as s:
        p = Project(name="P", x_username="proj", x_display_handle="@proj"); s.add(p); s.flush()
        j = _acc(s, "junkkid", rel="low", fc=40, flags={"spam_promoter": True})
        s.add(AccountProjectLink(account_id=j.id, project_id=p.id, relationship="mentioned", source="independent_research"))
        _acc(s, "tinyseed", src=["seed_list"], rel="low", fc=20)
        _acc(s, "keeper", rel="low", fc=50, flags={"cook_position_keeper": True})
        _acc(s, "bigspam", rel="low", fc=50_000)
    r = SP.prune_worthless_accounts(db)
    assert r["pruned"] == ["junkkid"] and r["seed_candidates"] == ["tinyseed"]
    with db.Session() as s:
        hs = {a.x_handle: a for a in s.query(TrackedAccount).all()}
        assert "junkkid" not in hs and {"tinyseed", "keeper", "bigspam"} <= set(hs)
        assert hs["tinyseed"].flags["worthless_candidate"] is True
        assert s.query(AccountProjectLink).count() == 0


def test_keeper_early_on_two_projects(db):
    with db.Session.begin() as s:
        _acc(s, "early", hist={"projects": [{"project": "a", "early_in_sample": True}, {"project": "b", "early_in_sample": True}]})
    assert SP.mark_keepers(db) == 1


def test_seed_follow_listed_even_provisional():
    seed = {"handle": "hunter", "followers_count": None, "sorsa_score": None, "reliability_label": "provisional",
            "confidence": 0, "flags": {}, "history": {}, "sources": ["seed_list"]}
    r = A.smart_signal(pack=None, tracked={"hunter": seed},
                       independent=[{"handle": "hunter", "relationship": "followed", "family": "seed_follow",
                                     "source": "x_following_check"}],
                       independent_flags={"unavailable": ["seed_follow_check: X following endpoint not available on this tier"]})
    sig, m = r["smart_account_signal"], r["matched_accounts"][0]
    assert m["is_seed"] and m["relationship"] == "followed" and "hunter seed follows" in m["why"]
    assert sig["seed_matches_followed"] == 1 and sig["seed_follow_handles"] == ["hunter"] and not sig["score"]
    assert any("seed_follow_check" in u for u in r["unavailable_reasons"])


class X403:
    available = True
    async def _get(self, path, params):
        raise Exception("X API access forbidden (403) — tier")


def test_follow_check_degrades_on_403(db):
    with db.Session.begin() as s:
        p = Project(name="P", x_username="proj", x_display_handle="@proj"); s.add(p); s.flush(); pid = p.id
        _acc(s, "hunter", src=["seed_list"], uid="1")
    r = asyncio.run(SP.check_seed_follows(db, X403(), project_ids=[pid]))
    assert r["status"] == "X following endpoint not available on this tier"
    with db.Session() as s:
        assert SP.last_follow_check_status(s) == r["status"]
