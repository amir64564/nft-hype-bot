from unittest.mock import MagicMock
import json, subprocess, sys, pathlib
from datetime import timedelta
from unittest.mock import AsyncMock
import pytest
from hypebot.config import Settings
from hypebot.db import Database, XRequest, Snapshot
from hypebot.pipeline import Analyzer
from hypebot.collectors.x_api import XClient
from hypebot.bot import Handlers
from hypebot.external_x import IngestError
from tests.fixtures import NOW, iso

ROOT = pathlib.Path(__file__).resolve().parents[1]


def payload(likes=500, mentions=True):
    p = {"source": "grokbot-x-read", "collected_at": iso(NOW),
         "account": {"followers": 40000, "following": 200, "created_at": iso(NOW - timedelta(days=700)),
                     "verified": True, "description": "Test Project NFT", "tweet_count": 3000},
         "recent_posts": [{"id": str(i), "text": f"post {i}", "created_at": iso(NOW - timedelta(hours=20 * i)),
                           "likes": likes, "replies": likes // 10, "reposts": likes // 5, "quotes": 2,
                           "views": likes * 40, "author_handle": "testproj"} for i in range(30)]}
    if mentions:
        p["mentions"] = [{"text": f"really like where testproj roadmap goes {i}", "author_handle": f"fan{i}",
                          "author_followers": 20000 if i < 3 else 500,
                          "author_created_at": iso(NOW - timedelta(days=400)), "likes": 4,
                          "created_at": iso(NOW - timedelta(hours=i))} for i in range(60)]
    return p


def setup(tmp_path):
    s = Settings(x_bearer_token=None, database_url=f"sqlite:///{tmp_path}/t.db")
    db = Database(s.database_url)
    return s, db, Analyzer(s, db, XClient(None))


def inter():
    i = MagicMock(); i.channel_id = 111; i.guild_id = 222; i.user.id = 333; i.id = 444
    i.response.defer = AsyncMock(); i.edit_original_response = AsyncMock()
    i.response.is_done = MagicMock(return_value=False)
    i.channel.send = AsyncMock(return_value=MagicMock(id=999))
    return i


async def test_request_queued_and_ingested(tmp_path):
    s, db, a = setup(tmp_path)
    h = Handlers(s, db, a)
    await h.analyze(inter(), "Test Project", "testproj")
    with db.Session() as ss:
        r = ss.query(XRequest).one()
        assert (r.status, r.channel_id, r.requested_by, r.interaction_id) == ("pending", "111", "333", "444")
        assert ss.query(Snapshot).count() == 0  # no score without X data
    out = a.ingest_external(r.id, payload())
    assert out.snapshot.sources[0]["source"] == "grokbot-x-read"
    assert "engagement_quality" not in out.snapshot.missing and out.snapshot.tier is not None
    assert out.snapshot.trend == "new"
    with db.Session() as ss:
        assert ss.query(Snapshot).count() == 1
        r2 = ss.get(XRequest, r.id); assert r2.status == "done"
        assert r2.posted  # superseded by the research job report (no duplicate one-off post)
    ch = AsyncMock()
    assert await h.post_completed_requests(AsyncMock(return_value=ch)) == 0
    with pytest.raises(Exception):
        a.ingest_external(r.id, payload())  # already done


async def test_invalid_payload_marks_failed(tmp_path):
    s, db, a = setup(tmp_path)
    o = await a.analyze("Test Project", "testproj", context={"channel_id": "1"})
    bad = payload(); bad["source"] = "made-up"
    with pytest.raises(IngestError):
        a.ingest_external(o.x_request_id, bad)
    with db.Session() as ss:
        assert ss.get(XRequest, o.x_request_id).status == "failed"
        assert ss.query(Snapshot).count() == 0


async def test_missing_mentions_is_missing_not_zero(tmp_path):
    s, db, a = setup(tmp_path)
    o = await a.analyze("Test Project", "testproj")
    out = a.ingest_external(o.x_request_id, payload(mentions=False))
    assert "community_discussion" in out.snapshot.missing and "influential_engagement" in out.snapshot.missing


async def test_cli(tmp_path):
    s, db, a = setup(tmp_path)
    o = await a.analyze("Test Project", "testproj")
    env = {"DATABASE_URL": s.database_url, "PATH": "/usr/bin:/bin"}
    run = lambda *args: subprocess.run([sys.executable, str(ROOT / "scripts/ingest_x.py"), *args],
                                       capture_output=True, text=True, env=env, cwd=tmp_path)
    lp = run("list-pending"); assert lp.returncode == 0, lp.stderr
    assert json.loads(lp.stdout)[0]["id"] == o.x_request_id
    f = tmp_path / "x.json"; f.write_text(json.dumps(payload()))
    ig = run("ingest", str(o.x_request_id), str(f)); assert ig.returncode == 0, ig.stderr
    assert json.loads(ig.stdout)["status"] == "done"
    assert json.loads(run("list-pending").stdout) == []


def test_clean_project_name():
    from hypebot.pipeline import clean_project_name as c
    assert c("https://x.com/minehood_rh", "minehood_rh") == "Minehood"
    assert c("@PudgyPenguins", "pudgypenguins") == "PudgyPenguins"
    assert c("https://minehood.io/", "x") == "Minehood"
    assert c("Pudgy Penguins", "x") == "Pudgy Penguins"


async def test_mention_counts_ingested(tmp_path):
    s, db, a = setup(tmp_path)
    o = await a.analyze("Test Project", "testproj")
    p = payload()
    p["mention_counts"] = [{"start": iso(NOW - timedelta(days=7 - d)), "end": iso(NOW - timedelta(days=6 - d)),
                            "tweet_count": 100 * (d + 1)} for d in range(7)]
    out = a.ingest_external(o.x_request_id, p)
    assert out.snapshot.metrics["mentions_7d_total"] == 2800
    assert out.snapshot.metrics["mention_velocity_change"] > 0


async def test_rescore_appends_without_post(tmp_path):
    s, db, a = setup(tmp_path)
    o = await a.analyze("Test Project", "testproj", context={"channel_id": "9"})
    first = a.ingest_external(o.x_request_id, payload())
    second = a.rescore_from_file("testproj", payload(likes=900))
    assert second.snapshot.id != first.snapshot.id and second.snapshot.trend in ("increasing", "stable")
    h = Handlers(s, db, a)
    ch = AsyncMock()
    assert await h.post_completed_requests(AsyncMock(return_value=ch)) == 1  # only the original request
    assert await h.post_completed_requests(AsyncMock(return_value=ch)) == 0
