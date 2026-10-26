import pytest
from hypebot.config import Settings
from hypebot.db import Database, Snapshot
from hypebot.pipeline import Analyzer, AnalysisError
from hypebot.collectors.x_api import XClient, normalize_handle
from tests.fixtures import fake_x


def make(tmp_path, x_data_seq, token="t"):
    s = Settings(x_bearer_token=token, database_url=f"sqlite:///{tmp_path}/t.db")
    db = Database(s.database_url)
    seq = iter(x_data_seq)
    async def fx(client, handle, name):
        d = next(seq); d['user']['username'] = handle; return d
    return db, Analyzer(s, db, XClient(token), x_collector=fx)


async def test_snapshots_append_only_and_trend(tmp_path):
    db, a = make(tmp_path, [fake_x(likes=200), fake_x(likes=3000)])
    o1 = await a.analyze("Test Project", "@TestProj")
    o2 = await a.refresh("testproj")
    assert o1.snapshot.id != o2.snapshot.id and o1.snapshot.trend == "new"
    assert o2.snapshot.trend == "increasing" and o2.snapshot.score_delta > 0
    with db.Session() as s:
        p = db.get_project(s, "Test Project")
        hist = db.history(s, p.id)
        assert [h.id for h in hist] == [o1.snapshot.id, o2.snapshot.id]
        assert hist[0].hype_score == o1.snapshot.hype_score  # not overwritten
        hist[0].hype_score = 1
        with pytest.raises(RuntimeError):
            s.commit()


async def test_no_x_token_queues_without_score(tmp_path):
    db, a = make(tmp_path, [], token=None)
    o = await a.analyze("Test Project", "TestProj")
    assert o.snapshot is None and o.x_request_id and any("X_BEARER_TOKEN" in w for w in o.warnings)
    with db.Session() as s:
        assert s.query(Snapshot).count() == 0


async def test_x_failure_raises(tmp_path):
    s = Settings(x_bearer_token="t", database_url=f"sqlite:///{tmp_path}/t.db")
    db = Database(s.database_url)
    from hypebot.collectors.x_api import XError
    async def fx(*a): raise XError("X API error 503")
    a = Analyzer(s, db, XClient("t"), x_collector=fx)
    with pytest.raises(AnalysisError):
        await a.analyze("Nothing", "nothing")
    with db.Session() as ss:
        assert ss.query(Snapshot).count() == 0


async def test_top(tmp_path):
    db, a = make(tmp_path, [fake_x(likes=100), fake_x(likes=3000)])
    await a.analyze("Low", "low1")
    await a.analyze("High", "high1")
    with db.Session() as s:
        rows = db.top(s, 5)
        assert [p.name for p, _ in rows] == ["High", "Low"]


def test_normalize_handle():
    assert normalize_handle("https://x.com/Foo_Bar?s=1") == "Foo_Bar"
    assert normalize_handle("@abc") == "abc"
    with pytest.raises(ValueError):
        normalize_handle("bad handle!")
