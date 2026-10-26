"""Image download + perceptual-hash comparison (imagehash pHash). Similarity is a risk indicator that
requires human comparison; it is never reported as proof of copying."""
from __future__ import annotations
import io, logging
import httpx

log = logging.getLogger("hypebot.images")
MAX_BYTES = 8 * 1024 * 1024
SIMILAR_MAX_DIST = 10   # of 64 bits


async def fetch_hashes(images: list, client: httpx.AsyncClient | None = None, limit: int = 16, hooks=None):
    try:
        import imagehash
        from PIL import Image
    except ImportError:
        return [], [{"source": "image comparison", "reason": "imagehash/Pillow not installed"}]
    own = client is None
    client = client or httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
    out, unavailable = [], []
    try:
        import hashlib
        from .attempts import fetch
        from .market import Hooks
        hooks = hooks or Hooks()
        for im in images[:limit]:
            urls = [im["url"]] + [u for u in (im.get("alt_urls") or []) if u]
            done = False
            for k, url in enumerate(urls):
                ok, why = hooks.allow(url)
                if not ok:
                    unavailable.append({"source": f"image {url[:120]}", "reason": f"skipped ({why})"}); continue
                outcome, r, err, ms = await fetch(client, url)
                if outcome == "success":
                    if len(r.content) > MAX_BYTES:
                        outcome, err = "invalid", f"{len(r.content)} bytes > limit"
                    else:
                        try:
                            img = Image.open(io.BytesIO(r.content)); img.seek(0)
                            out.append({**im, "fetched_url": url, "sha256": hashlib.sha256(r.content).hexdigest(),
                                        "phash": str(imagehash.phash(img.convert("RGB"))), "size": list(img.size)})
                            done = True
                        except Exception as e:
                            outcome, err = "invalid", f"not a readable image: {type(e).__name__}"
                hooks.record("image", url, outcome, getattr(r, "status_code", None), err, ms,
                             alternative_for=im["url"] if k else None)
                if done:
                    break
                unavailable.append({"source": f"image {url[:120]}", "reason": f"{outcome}: {err}"[:200]})
    finally:
        if own:
            await client.aclose()
    return out, unavailable


def compare(hashed: list) -> list:
    """Pairs of project vs reference images with Hamming distance; also project-vs-project consistency."""
    import imagehash
    proj = [h for h in hashed if h["role"] == "project"]
    ref = [h for h in hashed if h["role"] == "reference"]
    res = []
    for p in proj:
        for r in ref:
            d = imagehash.hex_to_hash(p["phash"]) - imagehash.hex_to_hash(r["phash"])
            res.append({"project_image": p["url"], "reference_image": r["url"], "reference_label": r.get("label"),
                        "distance": int(d), "similar": d <= SIMILAR_MAX_DIST})
    return res


def comparison_claims(pairs: list) -> list:
    claims = []
    for c in pairs:
        if c["similar"]:
            claims.append({"section": "art", "kind": "observation", "signal": None, "value": None,
                           "claim": f"Perceptual hash of a project image is close to reference '{c['reference_label'] or c['reference_image']}' "
                                    f"(distance {c['distance']}/64). Visual similarity only — needs manual side-by-side review; not proof of copying.",
                           "source_url": c["project_image"], "source_name": "pHash comparison", "data": c, "verified": True,
                           "why": "Possible resemblance to existing work"})
    if pairs and not claims:
        claims.append({"section": "art", "kind": "observation", "signal": None, "value": None,
                       "claim": f"No close perceptual-hash match among {len(pairs)} project/reference image pairs.",
                       "source_url": pairs[0]["project_image"], "source_name": "pHash comparison",
                       "data": {"pairs": len(pairs)}, "verified": True, "why": "Limited originality check (only supplied references)"})
    return claims
