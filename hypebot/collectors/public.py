"""Keyless public sources: website check, Discord invite counts, CoinGecko."""
from __future__ import annotations
import logging, re
import httpx
from ..http import request_json, RateLimited

log = logging.getLogger("hypebot.public")
INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]+)", re.I)


def extract_links(user: dict | None) -> list[str]:
    if not user:
        return []
    links = []
    ent = user.get("entities") or {}
    for part in ("url", "description"):
        for u in (ent.get(part) or {}).get("urls", []) or []:
            if u.get("expanded_url"):
                links.append(u["expanded_url"])
    links += re.findall(r"https?://\S+", user.get("description") or "")
    seen, res = set(), []
    for l in links:
        if l not in seen:
            seen.add(l); res.append(l)
    return res


def find_discord_invite(links: list[str], text: str = "") -> str | None:
    for s in links + [text]:
        m = INVITE_RE.search(s or "")
        if m:
            return m.group(1)
    return None


async def check_website(client: httpx.AsyncClient, url: str) -> dict:
    if not url.startswith("http"):
        url = "https://" + url
    try:
        r = await client.get(url, follow_redirects=True, timeout=15)
        return {"url": url, "final_url": str(r.url), "status": r.status_code,
                "ok": r.status_code < 400 or r.status_code in (401, 403, 429), "bot_protected": r.status_code in (401, 403, 429), "https": str(r.url).startswith("https://")}
    except httpx.HTTPError as e:
        return {"url": url, "ok": False, "error": type(e).__name__}


async def discord_invite(client: httpx.AsyncClient, code: str) -> dict | None:
    status, data, _ = await request_json(
        client, "GET", f"https://discord.com/api/v10/invites/{code}",
        params={"with_counts": "true"})
    if status != 200 or not data:
        return {"code": code, "valid": False, "status": status}
    return {"code": code, "valid": True,
            "guild_name": (data.get("guild") or {}).get("name"),
            "members": data.get("approximate_member_count"),
            "online": data.get("approximate_presence_count")}


async def coingecko(client: httpx.AsyncClient, query: str, handle: str, api_key: str | None) -> dict | None:
    headers = {"x-cg-demo-api-key": api_key} if api_key else {}
    base = "https://api.coingecko.com/api/v3"
    status, data, _ = await request_json(client, "GET", f"{base}/search", params={"query": query},
                                         headers=headers, max_wait=10)
    if status != 200 or not data:
        return None
    q = query.lower().strip()
    for kind in ("nfts", "coins"):
        for item in data.get(kind, [])[:5]:
            if item.get("name", "").lower() == q or item.get("symbol", "").lower() == q:
                st, det, _ = await request_json(client, "GET", f"{base}/{kind}/{item['id']}",
                                                headers=headers, max_wait=10)
                if st != 200 or not det:
                    return {"type": kind[:-1], "id": item["id"], "name": item.get("name"), "verified_handle": False}
                tw = ((det.get("links") or {}).get("twitter_screen_name") or det.get("links", {}).get("twitter") or "")
                tw = str(tw).rstrip("/").split("/")[-1].lstrip("@").lower()
                res = {"type": kind[:-1], "id": item["id"], "name": det.get("name"),
                       "verified_handle": bool(tw) and tw == handle.lower()}
                if kind == "nfts":
                    res.update(floor_usd=(det.get("floor_price") or {}).get("usd"),
                               volume_24h_usd=(det.get("volume_24h") or {}).get("usd"),
                               owners=det.get("number_of_unique_addresses"),
                               supply=det.get("total_supply"))
                else:
                    md = det.get("market_data") or {}
                    res.update(market_cap_usd=(md.get("market_cap") or {}).get("usd"),
                               volume_24h_usd=(md.get("total_volume") or {}).get("usd"),
                               cg_twitter_followers=(det.get("community_data") or {}).get("twitter_followers"))
                return res
    return {"found": False}


async def collect_public(project_name: str, handle: str, user: dict | None, *,
                         website: str | None = None, discord_code: str | None = None,
                         coingecko_key: str | None = None,
                         client: httpx.AsyncClient | None = None) -> dict:
    out: dict = {"errors": []}
    links = extract_links(user)
    own = client is None
    client = client or httpx.AsyncClient(timeout=20, headers={"User-Agent": "nft-hype-bot/1.0"})
    try:
        site = website or next((l for l in links if not re.search(r"(x|twitter|discord|t\.me|linktr)\.", l)), None)
        if site:
            out["website"] = await check_website(client, site)
        code = discord_code or find_discord_invite(links, (user or {}).get("description", ""))
        if code:
            code = INVITE_RE.search(code).group(1) if INVITE_RE.search(code) else code
            try:
                out["discord"] = await discord_invite(client, code)
            except (RateLimited, httpx.HTTPError) as e:
                out["errors"].append(f"discord: {e}")
        try:
            cg = await coingecko(client, project_name, handle, coingecko_key)
            if cg is not None:
                out["coingecko"] = cg
        except (RateLimited, httpx.HTTPError) as e:
            out["errors"].append(f"coingecko: {e}")
        out["links"] = links
    finally:
        if own:
            await client.aclose()
    return out
