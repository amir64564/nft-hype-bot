"""Sorsa API client (https://docs.sorsa.io/) — an ADDITIONAL verification source, never the authority.

Quota discipline (free plan ≈ 100 requests total):
* every response is cached in `sorsa_cache` keyed by (endpoint, username) for SORSA_CACHE_TTL_H (24h);
  permanent misses (404 / not indexed) are cached too, so they are never paid for twice;
* identical requests inside one process are de-duplicated;
* every decision (live call, cache hit, skip) is written to `sorsa_api_log` with the remaining quota;
* soft-stop: no live call when the last known remaining quota < SORSA_MIN_REMAINING unless force=True;
* a default project pack is at most 3 live calls: /score, /followers-stats, /top-followers.
  /score-changes and /new-followers-7d are only used when cached or remaining > SORSA_OPTIONAL_MIN_REMAINING (default 20);
  when remaining is unknown, ONE live /key-usage-info call refreshes it first (quota is never assumed to reset);
* the paginated /followers endpoint is never used.
Missing data raises SorsaUnavailable — callers record it as unavailable; it is never scored as zero."""
from __future__ import annotations
import logging
from datetime import timedelta, timezone
import httpx
from sqlalchemy import select
from ..db import utcnow
from .models import SorsaCache, SorsaApiLog, SorsaProjectSnapshot

log = logging.getLogger("hypebot.sorsa")
BASE_URL = "https://api.sorsa.io/v3"
PACK_CORE = ("score", "followers-stats", "top-followers")
PACK_OPTIONAL = ("score-changes", "new-followers-7d")
ENDPOINTS = set(PACK_CORE) | set(PACK_OPTIONAL)
SOURCE_FAMILY = "sorsa"   # all Sorsa endpoints together count as ONE source family


class SorsaUnavailable(Exception):
    def __init__(self, endpoint: str, reason: str, status: int | None = None, permanent: bool = False):
        super().__init__(f"Sorsa {endpoint}: {reason}")
        self.endpoint, self.reason, self.status, self.permanent = endpoint, reason, status, permanent


def _aware(d):
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def norm(handle: str) -> str:
    return (handle or "").strip().lstrip("@").lower()


class SorsaClient:
    def __init__(self, db, api_key: str | None, ttl_h: float = 24.0, min_remaining: int = 10,
                 http: httpx.Client | None = None, base_url: str = BASE_URL, quota_total: int | None = None):
        self.quota_total = quota_total
        self.db, self.api_key, self.ttl = db, api_key, timedelta(hours=ttl_h)
        self.min_remaining, self.base_url = min_remaining, base_url
        self._http = http
        self._memo: dict = {}
        self.live_calls = 0
        self.cache_hits = 0
        self._usage_checked = False

    # ---- bookkeeping ----
    def _log(self, endpoint, username, status, cached=False, remaining=None, note=None):
        with self.db.Session.begin() as s:
            s.add(SorsaApiLog(endpoint=endpoint, username=username, status=str(status), cached=cached,
                              remaining_after=remaining, note=note))

    def remaining(self) -> int | None:
        """Last known remaining quota (from key-usage-info or decremented after each live call)."""
        with self.db.Session() as s:
            row = s.scalar(select(SorsaApiLog).where(SorsaApiLog.remaining_after.is_not(None))
                           .order_by(SorsaApiLog.id.desc()).limit(1))
            return row.remaining_after if row else None

    def estimated_remaining(self) -> int | None:
        """Conservative local estimate when /key-usage-info is unavailable: SORSA_QUOTA_TOTAL (lifetime pack size,
        free plan = 100, NOT a monthly reset) minus every live request this DB has logged (errors included)."""
        if not self.quota_total:
            return None
        with self.db.Session() as s:
            used = s.query(SorsaApiLog).filter(SorsaApiLog.cached.is_(False),
                                               SorsaApiLog.status.not_in(("cached", "no_key", "skipped_quota",
                                                                          "skipped_optional", "estimate"))).count()
        return max(0, self.quota_total - used)

    def record_estimate(self):
        est = self.estimated_remaining()
        if est is not None:
            self._log("key-usage-info", None, "estimate", remaining=est,
                      note=f"key-usage-info unavailable; local estimate {self.quota_total} total - logged live calls")
        return est

    def cached(self, endpoint, username):
        with self.db.Session() as s:
            row = s.scalar(select(SorsaCache).where(SorsaCache.endpoint == endpoint, SorsaCache.username == username)
                           .order_by(SorsaCache.fetched_at.desc(), SorsaCache.id.desc()).limit(1))
            if row and utcnow() - _aware(row.fetched_at) < self.ttl:
                return row
        return None

    def _client(self):
        if self._http is None:
            self._http = httpx.Client(timeout=20)
        return self._http

    # ---- requests ----
    def get(self, endpoint: str, username: str, force: bool = False):
        if endpoint not in ENDPOINTS:
            raise ValueError(f"unsupported Sorsa endpoint {endpoint!r}")
        u = norm(username)
        key = (endpoint, u)
        if key in self._memo:
            return self._memo[key]
        row = self.cached(endpoint, u)
        if row is not None:
            self.cache_hits += 1
            self._log(endpoint, u, "cached", cached=True, note=f"cached HTTP {row.status}")
            if row.status != 200:
                raise SorsaUnavailable(endpoint, f"cached miss (HTTP {row.status})", row.status, permanent=True)
            self._memo[key] = row.payload
            return row.payload
        if not self.api_key:
            self._log(endpoint, u, "no_key")
            raise SorsaUnavailable(endpoint, "SORSA_API_KEY not configured")
        rem = self.remaining()
        if not force and rem is not None and rem < self.min_remaining:
            self._log(endpoint, u, "skipped_quota", remaining=rem)
            raise SorsaUnavailable(endpoint, f"quota soft-stop: {rem} request(s) left (< {self.min_remaining})")
        data, status = self._live(endpoint, {"username": u})
        rem_after = rem - 1 if rem is not None else None
        if status == 200 and data is not None:
            self._store(endpoint, u, 200, data)
            self._log(endpoint, u, 200, remaining=rem_after)
            self._memo[key] = data
            return data
        permanent = status in (400, 404, 422)
        if permanent:   # not found / not indexed: cache the miss so it is not paid for again
            self._store(endpoint, u, status, data)
        self._log(endpoint, u, status, remaining=rem_after, note=str(data)[:200] if data else None)
        reason = {401: "invalid API key", 403: "forbidden / plan limit", 404: "account not found or not indexed by Sorsa",
                  429: "rate limited"}.get(status, f"HTTP {status}")
        raise SorsaUnavailable(endpoint, reason, status, permanent)

    def _live(self, endpoint, params):
        self.live_calls += 1
        try:
            r = self._client().get(f"{self.base_url}/{endpoint}", params=params, headers={"ApiKey": self.api_key})
        except httpx.HTTPError as e:
            return None, 0 if not isinstance(e, httpx.TimeoutException) else 504
        try:
            return r.json(), r.status_code
        except ValueError:
            return None, r.status_code

    def _store(self, endpoint, username, status, payload):
        with self.db.Session.begin() as s:
            s.add(SorsaCache(endpoint=endpoint, username=username, status=status, payload=payload))

    def key_usage(self) -> dict:
        """Live GET /key-usage-info (refreshes the local remaining-quota record)."""
        if not self.api_key:
            raise SorsaUnavailable("key-usage-info", "SORSA_API_KEY not configured")
        self.live_calls += 1
        try:
            r = self._client().get(f"{self.base_url}/key-usage-info", headers={"ApiKey": self.api_key})
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            self._log("key-usage-info", None, "error", note=str(e)[:200])
            raise SorsaUnavailable("key-usage-info", str(e))
        if r.status_code != 200:
            self._log("key-usage-info", None, r.status_code, note=str(data)[:200])
            raise SorsaUnavailable("key-usage-info", f"HTTP {r.status_code}", r.status_code)
        rem = data.get("remaining_requests")
        self._log("key-usage-info", None, 200, remaining=int(rem) if rem is not None else None, note=str(data)[:300])
        return data


# ---------------------------------------------------------------------------------------------
def _users(payload) -> list:
    if isinstance(payload, dict):
        return list(payload.get("users") or payload.get("followers") or [])
    return list(payload or []) if isinstance(payload, list) else []


def compact_user(u: dict) -> dict:
    """Keep only what the signal needs; follow date only when Sorsa states one."""
    return {"username": norm(u.get("username") or u.get("screen_name") or ""), "user_id": u.get("id") or u.get("user_id"),
            "display_name": u.get("display_name") or u.get("name"), "score": u.get("score"),
            "followers_count": u.get("followers_count"),
            "follower_date": u.get("follower_date") or u.get("followerDate"),
            "description": (u.get("description") or "")[:160]}


def fetch_project_pack(db, client: SorsaClient, handle: str, project_id: int | None = None, force: bool = False,
                       optional_min_remaining: int = 20) -> dict:
    """Fetch (cache-first) the Sorsa pack for one project handle and store an append-only snapshot.
    Returns a dict with whatever is available plus `unavailable` reasons. Never invents values."""
    u = norm(handle)
    calls0, hits0 = client.live_calls, client.cache_hits
    out = {"username": u, "score": None, "week_delta": None, "month_delta": None, "followers_stats": None,
           "top_followers": None, "new_followers_7d": None, "unavailable": [], "raw": {}}

    def one(ep):
        try:
            d = client.get(ep, u, force=force)
            out["raw"][ep] = d
            return d
        except SorsaUnavailable as e:
            out["unavailable"].append({"endpoint": ep, "reason": e.reason, "status": e.status})
            return None

    d = one("score")
    if isinstance(d, dict) and d.get("score") is not None:
        out["score"] = float(d["score"])
    d = one("followers-stats")
    if isinstance(d, dict):
        out["followers_stats"] = {k: d.get(k) for k in ("followers_count", "influencers_count", "projects_count",
                                                        "venture_capitals_count", "user_protected")}
    d = one("top-followers")
    if d is not None:
        out["top_followers"] = [compact_user(x) for x in _users(d)][:20]
    rem = client.remaining()
    need_live = [ep for ep in PACK_OPTIONAL if client.cached(ep, u) is None]
    if rem is None and need_live and client.api_key and not force and not client._usage_checked:
        # unknown quota: one live key-usage-info check (once per client) instead of silently skipping forever
        client._usage_checked = True
        try:
            client.key_usage()
        except SorsaUnavailable as e:
            log.warning("Sorsa key-usage-info failed: %s", e.reason)
            client.record_estimate()
        rem = client.remaining()
    for ep in PACK_OPTIONAL:
        if client.cached(ep, u) is not None or (client.api_key and rem is not None and rem > optional_min_remaining) or force:
            d = one(ep)
            if ep == "score-changes" and isinstance(d, dict):
                out["week_delta"], out["month_delta"] = d.get("week_delta"), d.get("month_delta")
            elif ep == "new-followers-7d" and d is not None:
                out["new_followers_7d"] = [compact_user(x) for x in _users(d)]
        else:
            client._log(ep, u, "skipped_optional", remaining=None, note=f"remaining {rem}, needs > {optional_min_remaining}")
            out["unavailable"].append({"endpoint": ep, "reason": "skipped to save quota (remaining "
                                       + (str(rem) if rem is not None else "unknown") + f", needs > {optional_min_remaining})",
                                       "status": None})
    out["api_calls_used"] = client.live_calls - calls0
    out["cache_hits"] = client.cache_hits - hits0
    out["remaining_quota"] = client.remaining()
    with db.Session.begin() as s:
        snap = SorsaProjectSnapshot(project_id=project_id, username=u, score=out["score"], week_delta=out["week_delta"],
                                    month_delta=out["month_delta"], followers_stats=out["followers_stats"],
                                    top_followers=out["top_followers"], new_followers_7d=out["new_followers_7d"],
                                    unavailable=out["unavailable"], raw=out["raw"], cache_hits=out["cache_hits"],
                                    api_calls_used=out["api_calls_used"], remaining_quota=out["remaining_quota"])
        s.add(snap); s.flush()
        out["snapshot_id"] = snap.id
    return out


def latest_pack(s, username: str, project_id: int | None = None):
    q = select(SorsaProjectSnapshot).where(SorsaProjectSnapshot.username == norm(username))
    return s.scalar(q.order_by(SorsaProjectSnapshot.fetched_at.desc(), SorsaProjectSnapshot.id.desc()).limit(1))


def pack_dict(row) -> dict | None:
    if row is None:
        return None
    return {"snapshot_id": row.id, "username": row.username, "fetched_at": _aware(row.fetched_at).isoformat(),
            "score": row.score, "week_delta": row.week_delta, "month_delta": row.month_delta,
            "followers_stats": row.followers_stats, "top_followers": row.top_followers,
            "new_followers_7d": row.new_followers_7d, "unavailable": row.unavailable or []}


def client_from_env(db, http=None) -> SorsaClient:
    from ..config import sorsa_settings
    st = sorsa_settings()
    return SorsaClient(db, st["sorsa_api_key"], st["sorsa_cache_ttl_h"], st["sorsa_min_remaining"], http=http,
                       quota_total=st.get("sorsa_quota_total"))
