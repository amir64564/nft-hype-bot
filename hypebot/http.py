"""HTTP helper with retries, exponential backoff and rate-limit handling."""
from __future__ import annotations
import asyncio, logging, random, time
import httpx

log = logging.getLogger("hypebot.http")


class RateLimited(Exception):
    def __init__(self, reset_at: float | None, msg: str = "rate limited"):
        super().__init__(msg)
        self.reset_at = reset_at


async def request_json(client: httpx.AsyncClient, method: str, url: str, *, retries: int = 3,
                       max_wait: float = 60.0, **kw):
    """Returns (status, json|None, headers). Retries 5xx/network errors; on 429 waits until
    x-rate-limit-reset / Retry-After if within max_wait, else raises RateLimited."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            r = await client.request(method, url, **kw)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_exc = e
            wait = min(2 ** attempt + random.random(), 20)
            log.warning("network error %s on %s; retry in %.1fs", e, url, wait)
            await asyncio.sleep(wait)
            continue
        if r.status_code == 429:
            reset = r.headers.get("x-rate-limit-reset")
            retry_after = r.headers.get("retry-after")
            if reset:
                wait = max(0.0, float(reset) - time.time()) + 1
                reset_at = float(reset)
            elif retry_after:
                wait = float(retry_after); reset_at = time.time() + wait
            else:
                wait = 2 ** attempt * 5; reset_at = None
            if wait > max_wait or attempt == retries:
                raise RateLimited(reset_at, f"rate limited on {url}; reset in {wait:.0f}s")
            log.warning("429 on %s; sleeping %.1fs", url, wait)
            await asyncio.sleep(wait)
            continue
        if r.status_code >= 500 and attempt < retries:
            wait = min(2 ** attempt + random.random(), 20)
            log.warning("HTTP %s on %s; retry in %.1fs", r.status_code, url, wait)
            await asyncio.sleep(wait)
            continue
        try:
            data = r.json()
        except ValueError:
            data = None
        return r.status_code, data, r.headers
    raise last_exc or RuntimeError(f"request failed: {url}")
