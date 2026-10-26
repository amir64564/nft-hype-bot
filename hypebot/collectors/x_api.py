"""Official X API v2 collector (app-only bearer token)."""
from __future__ import annotations
import logging, re
from datetime import datetime, timezone
import httpx
from ..http import request_json, RateLimited

log = logging.getLogger("hypebot.x")
BASE = "https://api.twitter.com/2"
USER_FIELDS = "created_at,description,entities,public_metrics,verified,verified_type,url,location,protected"
TWEET_FIELDS = "created_at,public_metrics,conversation_id,author_id,text,in_reply_to_user_id,referenced_tweets"
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


class XError(Exception):
    pass


class XCredentialsMissing(XError):
    pass


def normalize_handle(h: str) -> str:
    h = (h or "").strip()
    h = re.sub(r"^https?://(www\.)?(x|twitter)\.com/", "", h, flags=re.I).split("/")[0].split("?")[0]
    h = h.lstrip("@")
    if not HANDLE_RE.match(h):
        raise ValueError(f"Invalid X username: {h!r}")
    return h


class XClient:
    def __init__(self, bearer_token: str | None, client: httpx.AsyncClient | None = None):
        self.token = bearer_token
        self._client = client

    @property
    def available(self) -> bool:
        return bool(self.token)

    async def _get(self, path: str, params: dict) -> dict:
        if not self.token:
            raise XCredentialsMissing("X_BEARER_TOKEN is not configured")
        client = self._client or httpx.AsyncClient(timeout=20)
        try:
            status, data, _ = await request_json(
                client, "GET", BASE + path, params=params,
                headers={"Authorization": f"Bearer {self.token}"})
        finally:
            if self._client is None:
                await client.aclose()
        if status == 401:
            raise XError("X API rejected the bearer token (401)")
        if status == 403:
            raise XError("X API access forbidden (403) — your X API tier may not include this endpoint")
        if status >= 400:
            raise XError(f"X API error {status}: {data}")
        if data and "errors" in data and "data" not in data:
            raise XError(f"X API error: {data['errors'][0].get('detail') or data['errors'][0]}")
        return data or {}

    async def get_user(self, handle: str) -> dict | None:
        d = await self._get(f"/users/by/username/{handle}", {"user.fields": USER_FIELDS})
        return d.get("data")

    async def get_user_tweets(self, user_id: str, max_results: int = 100) -> list[dict]:
        d = await self._get(f"/users/{user_id}/tweets", {
            "max_results": max(5, min(max_results, 100)),
            "exclude": "retweets",
            "tweet.fields": TWEET_FIELDS,
        })
        return d.get("data", [])

    async def search_mentions(self, handle: str, name: str | None, max_results: int = 100) -> dict:
        terms = [f"@{handle}"]
        if name and name.lower() != handle.lower():
            terms.append(f'"{name}"')
        q = f"({' OR '.join(terms)}) -from:{handle} -is:retweet"
        d = await self._get("/tweets/search/recent", {
            "query": q, "max_results": max(10, min(max_results, 100)),
            "tweet.fields": TWEET_FIELDS, "expansions": "author_id",
            "user.fields": "public_metrics,verified,verified_type,created_at",
        })
        return {"tweets": d.get("data", []), "users": d.get("includes", {}).get("users", []),
                "query": q}

    async def mention_counts(self, handle: str) -> list[dict] | None:
        """7-day hourly/daily counts (requires Basic+ tier). Returns None if not permitted."""
        try:
            d = await self._get("/tweets/counts/recent", {
                "query": f"@{handle} -from:{handle} -is:retweet", "granularity": "day"})
            return d.get("data", [])
        except XError as e:
            log.info("counts endpoint unavailable: %s", e)
            return None


async def collect_x(client: XClient, handle: str, project_name: str | None) -> dict:
    """Collect raw X data. Partial failures are recorded in 'errors', never filled with fakes."""
    out: dict = {"handle": handle, "errors": [], "collected_at": datetime.now(timezone.utc).isoformat()}
    user = await client.get_user(handle)
    if not user:
        raise XError(f"X account @{handle} not found")
    out["user"] = user
    for key, coro in (("tweets", client.get_user_tweets(user["id"])),
                      ("mentions", client.search_mentions(handle, project_name)),
                      ("mention_counts", client.mention_counts(handle))):
        try:
            out[key] = await coro
        except RateLimited as e:
            out["errors"].append(f"{key}: {e}")
        except XError as e:
            out["errors"].append(f"{key}: {e}")
    return out
