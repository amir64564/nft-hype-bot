from unittest.mock import AsyncMock, MagicMock
import discord
from discord import app_commands
from hypebot.bot import Handlers, build_tree, HypeBot
from hypebot.config import Settings
from tests.test_db_pipeline import make
from tests.fixtures import fake_x


def inter():
    i = MagicMock()
    i.response.defer = AsyncMock(); i.response.send_message = AsyncMock()
    i.edit_original_response = AsyncMock()
    i.channel.send = AsyncMock(return_value=MagicMock(id=999))
    i.response.is_done = MagicMock(return_value=False)
    return i


async def test_analyze_and_views(tmp_path):
    db, a = make(tmp_path, [fake_x(), fake_x(likes=50), fake_x()])
    h = Handlers(a.s, db, a)
    i = inter()
    await h.analyze(i, "Test Project", "TestProj")
    i.response.defer.assert_awaited()
    final = i.edit_original_response.await_args_list[-1].kwargs
    assert "Research job #" in final["content"]          # /analyze now starts a background research job
    assert "Research job #" in i.channel.send.await_args.args[0]   # progress message posted
    await h.refresh(inter(), "TestProj")
    for fn, args in ((h.project, ("TestProj",)), (h.history, ("Test Project",)), (h.top, ()), (h.help, ())):
        j = inter(); await fn(j, *args)
        assert isinstance(j.response.send_message.await_args.kwargs["embed"], discord.Embed)
    await h.analyze(inter(), "Other", "other1")
    j = inter(); await h.compare(j, "TestProj, other1, unknownxyz")
    e = j.response.send_message.await_args.kwargs["embed"]
    assert len(e.fields) == 2 and "unknownxyz" in e.footer.text


async def test_errors_reported(tmp_path):
    db, a = make(tmp_path, [])
    h = Handlers(a.s, db, a)
    i = inter(); await h.analyze(i, "x", "bad handle!")
    assert "❌" in i.edit_original_response.await_args.kwargs["content"]
    j = inter(); await h.project(j, "nope")
    assert j.response.send_message.await_args.kwargs.get("ephemeral")
    k = inter(); await h.compare(k, "one")
    assert "at least 2" in k.response.send_message.await_args.args[0]


async def test_tree_builds(tmp_path):
    s = Settings(database_url=f"sqlite:///{tmp_path}/t.db")
    db, a = make(tmp_path, [])
    bot = HypeBot(s, Handlers(s, db, a))
    names = {c.name for c in bot.tree.get_commands()}
    assert names == {"analyze", "refresh", "project", "history", "compare", "top", "help", "verify", "review"}
    for c in bot.tree.get_commands():
        c.to_dict(bot.tree)  # validates payload
