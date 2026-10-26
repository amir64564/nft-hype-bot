import httpx, pytest, time
from hypebot.http import request_json, RateLimited


async def test_retry_then_ok(monkeypatch):
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 2 else httpx.Response(200, json={"ok": 1})
    import hypebot.http as h
    async def nosleep(_): pass
    monkeypatch.setattr(h.asyncio, "sleep", nosleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        st, d, _ = await request_json(c, "GET", "https://e/x")
    assert st == 200 and d == {"ok": 1} and calls["n"] == 2


async def test_rate_limit_long_reset_raises():
    reset = str(int(time.time()) + 900)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(429, headers={"x-rate-limit-reset": reset}))) as c:
        with pytest.raises(RateLimited) as e:
            await request_json(c, "GET", "https://e/x")
    assert e.value.reset_at == float(reset)
