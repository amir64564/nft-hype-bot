"""Keyless public market collectors that were verified reachable from the box (2026-10-22):
  * OpenSea API v2  GET /api/v2/collections/{slug} and /collections/{slug}/stats  (no key needed)
  * Magic Eden Solana v2 GET /v2/collections/{symbol}/stats and /activities (rate-limited ~1 req/s)
  * CoinGecko GET /api/v3/nfts/{id} (public, rate-limited)
Verified NOT usable without keys: OpenSea events (401), Magic Eden EVM v3 rtp (400), Reservoir (DNS fails).
Every number returned carries its source URL. Failures return an 'unavailable' entry, never zeros."""
from __future__ import annotations
import logging
from collections import Counter
from datetime import datetime, timezone
import httpx
from ..http import request_json, RateLimited

log = logging.getLogger("hypebot.market")
UA = {"User-Agent": "Mozilla/5.0 (hypebot research)", "Accept": "application/json"}
OS = "https://api.opensea.io/api/v2"
ME = "https://api-mainnet.magiceden.dev/v2"
CG = "https://api.coingecko.com/api/v3"


def _claim(claim, url, name, signal="market_fact", data=None, value=None, why=None):
    return {"section": "market", "kind": "observation", "claim": claim, "source_url": url, "source_name": name,
            "signal": signal, "data": data or {}, "value": value, "why": why, "verified": True}


class Hooks:
    """Attempt bookkeeping injected by the job runner. Default: no persistence (tests/CLI)."""
    def allow(self, target):
        return True, "no attempt history"

    def record(self, source, target, outcome, http_status=None, error=None, duration_ms=None, alternative_for=None):
        pass


_HOOKS = {"h": Hooks()}


async def _get(client, url, source="market"):
    from .attempts import fetch
    h = _HOOKS["h"]
    ok, why = h.allow(url)
    if not ok:
        return None, f"skipped ({why})"
    outcome, r, err, ms = await fetch(client, url, headers=UA)
    js = None
    if outcome == "success":
        try:
            js = r.json()
        except ValueError:
            outcome, err = "invalid", "response is not JSON"
    h.record(source, url, outcome, getattr(r, "status_code", None), err, ms)
    if outcome != "success":
        return None, f"{outcome}: {err}"
    return js, None


async def opensea(client, slug):
    claims, unavailable = [], []
    u1, u2 = f"{OS}/collections/{slug}", f"{OS}/collections/{slug}/stats"
    c, err = await _get(client, u1, "OpenSea collection")
    if err:
        unavailable.append({"source": f"OpenSea collection {slug}", "reason": err})
    else:
        sup = c.get("total_supply")
        links = {k: c.get(k) for k in ("project_url", "twitter_username", "discord_url", "opensea_url") if c.get(k)}
        claims.append(_claim(f"OpenSea lists '{c.get('name')}'" + (f", supply {sup}" if sup is not None else ""),
                             u1, "OpenSea", data={"supply": sup, "links": links, "category": c.get("category"),
                                                  "safelist": c.get("safelist_status"), "created_date": c.get("created_date"),
                                                  "contracts": c.get("contracts")}))
    s, err = await _get(client, u2, "OpenSea stats")
    if err:
        unavailable.append({"source": f"OpenSea stats {slug}", "reason": err})
    else:
        t = s.get("total") or {}
        iv = {i.get("interval"): i for i in s.get("intervals") or []}
        d1, d7 = iv.get("one_day") or {}, iv.get("seven_day") or {}
        data = {"floor": t.get("floor_price"), "floor_symbol": t.get("floor_price_symbol"), "owners": t.get("num_owners"),
                "sales_total": t.get("sales"), "volume_total": t.get("volume"), "volume_symbol": t.get("volume_symbol"),
                "volume_1d": d1.get("volume"), "sales_1d": d1.get("sales"), "volume_7d": d7.get("volume"),
                "sales_7d": d7.get("sales"), "volume_change_7d": d7.get("volume_change")}
        claims.append(_claim(f"OpenSea stats: floor {data['floor']} {data['floor_symbol'] or ''}, owners {data['owners']}, "
                             f"7d sales {data['sales_7d']}, 7d volume {data['volume_7d']}", u2, "OpenSea", data=data))
    return claims, unavailable


async def magiceden_sol(client, symbol):
    claims, unavailable = [], []
    u1, u2 = f"{ME}/collections/{symbol}/stats", f"{ME}/collections/{symbol}/activities?offset=0&limit=100"
    s, err = await _get(client, u1, "Magic Eden stats")
    if err:
        unavailable.append({"source": f"Magic Eden stats {symbol}", "reason": err})
    else:
        lam = 1e9
        data = {"floor_sol": (s.get("floorPrice") or 0) / lam if s.get("floorPrice") is not None else None,
                "listed": s.get("listedCount"), "volume_all_sol": (s.get("volumeAll") or 0) / lam if s.get("volumeAll") is not None else None,
                "avg_price_24h_sol": (s.get("avgPrice24hr") / lam) if s.get("avgPrice24hr") is not None else None}
        claims.append(_claim(f"Magic Eden stats: floor {data['floor_sol']} SOL, listed {data['listed']}", u1, "Magic Eden", data=data))
    a, err = await _get(client, u2, "Magic Eden activities")
    if err:
        unavailable.append({"source": f"Magic Eden activities {symbol}", "reason": err})
    elif isinstance(a, list):
        sales = [x for x in a if x.get("type") == "buyNow"]
        pairs = Counter(tuple(sorted([x.get("buyer") or "", x.get("seller") or ""])) for x in sales)
        rep = sum(v for v in pairs.values() if v > 1)
        buyers = Counter(x.get("buyer") for x in sales if x.get("buyer"))
        data = {"recent_sales": len(sales), "repeat_pair_sales": rep, "unique_buyers": len(buyers),
                "top_buyer_share": (max(buyers.values()) / len(sales)) if sales and buyers else None}
        claims.append(_claim(f"Magic Eden last {len(a)} activities: {len(sales)} sales, {len(buyers)} unique buyers, "
                             f"{rep} sales between repeating wallet pairs", u2, "Magic Eden", data=data,
                             signal="market_fact", value=-0.5 if sales and rep / len(sales) > 0.3 else None,
                             why="Repeated buyer/seller pairs are a wash-trading risk indicator (not proof)"))
    return claims, unavailable


async def coingecko(client, cg_id):
    u = f"{CG}/nfts/{cg_id}"
    c, err = await _get(client, u, "CoinGecko")
    if err:
        return [], [{"source": f"CoinGecko {cg_id}", "reason": err}]
    fp = (c.get("floor_price") or {}).get("usd")
    data = {"floor_usd": fp, "holders": c.get("number_of_unique_addresses"), "supply": c.get("total_supply"),
            "volume_24h_usd": (c.get("volume_24h") or {}).get("usd"),
            "floor_change_24h_pct": c.get("floor_price_in_usd_24h_percentage_change"),
            "links": c.get("links")}
    return [_claim(f"CoinGecko: floor ${fp}, holders {data['holders']}, supply {data['supply']}", u, "CoinGecko", data=data)], []


async def collect_market(marketplaces: list, cg_id: str | None = None, client: httpx.AsyncClient | None = None,
                         hooks: Hooks | None = None):
    """marketplaces: [{name: opensea|magiceden_sol, slug}] from the feeder or the user."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    claims, unavailable = [], []
    _HOOKS["h"] = hooks or Hooks()
    try:
        for m in marketplaces:
            slug = (m or {}).get("slug")
            if not slug:
                continue
            if m.get("name") == "opensea":
                c, u = await opensea(client, slug)
            elif m.get("name") in ("magiceden_sol", "magiceden") and (m.get("chain") in (None, "solana")):
                c, u = await magiceden_sol(client, slug)
            else:
                c, u = [], [{"source": f"{m.get('name')} {slug}", "reason": "no keyless API for this marketplace/chain"}]
            claims += c; unavailable += u
        stats_ok = any("stats" in c["source_url"] for c in claims)
        if cg_id:   # also the alternative source when marketplace stats failed
            c, u = await coingecko(client, cg_id); claims += c; unavailable += u
            if c and not stats_ok:
                for x in c:
                    x["why"] = "alternative source: marketplace stats unavailable"
    finally:
        _HOOKS["h"] = Hooks()
        if own:
            await client.aclose()
    for c in claims:
        c["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return claims, unavailable
