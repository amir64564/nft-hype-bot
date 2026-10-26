"""Convert/validate externally collected REAL X data (source='grokbot-x-read') to collector format."""
from __future__ import annotations
from datetime import datetime

SOURCE = "grokbot-x-read"


class IngestError(ValueError):
    pass


def _ts(v, field):
    if not isinstance(v, str):
        raise IngestError(f"{field}: ISO-8601 timestamp string required")
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise IngestError(f"{field}: invalid timestamp {v!r}")
    return v


def _int(d, k, field, required=True):
    v = d.get(k)
    if v is None:
        if required:
            raise IngestError(f"{field}.{k} is required")
        return None
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
        raise IngestError(f"{field}.{k} must be a non-negative number")
    if isinstance(v, float) and not v.is_integer():
        # e.g. 12.5 meaning "12.5K": counts must be raw integers, never abbreviated units
        raise IngestError(f"{field}.{k} must be a raw integer count (got {v}); abbreviated units like 12.5K are not allowed")
    return int(v)


def _hid(*parts) -> str:
    import hashlib
    return "h" + hashlib.sha1("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:16]


def to_x_data(payload: dict, handle: str) -> dict:
    if payload.get("source") != SOURCE:
        raise IngestError(f"source must be '{SOURCE}'")
    _ts(payload.get("collected_at"), "collected_at")
    acc = payload.get("account")
    if not isinstance(acc, dict):
        raise IngestError("account object is required")
    if acc.get("username") and acc["username"].lstrip("@").lower() != handle.lstrip("@").lower():
        raise IngestError(f"identity mismatch: payload account @{acc['username']} is not the requested @{handle}")
    user = {"id": f"handle:{handle.lower()}", "username": acc.get("username", handle),
            "name": acc.get("name", handle), "description": acc.get("description") or "",
            "verified": bool(acc.get("verified")), "verified_type": acc.get("verified_type"),
            "created_at": _ts(acc["created_at"], "account.created_at") if acc.get("created_at") else None,
            "public_metrics": {"followers_count": _int(acc, "followers", "account"),
                               "following_count": _int(acc, "following", "account", False),
                               "tweet_count": _int(acc, "tweet_count", "account", False),
                               "listed_count": _int(acc, "listed", "account", False)},
            "entities": {"url": {"urls": [{"expanded_url": u} for u in acc.get("links", []) or []]}}}
    posts = payload.get("recent_posts")
    if not isinstance(posts, list):
        raise IngestError("recent_posts array is required (may be empty)")
    tweets = []
    for i, p in enumerate(posts):
        f = f"recent_posts[{i}]"
        pm = {"like_count": _int(p, "likes", f), "reply_count": _int(p, "replies", f),
              "retweet_count": _int(p, "reposts", f), "quote_count": _int(p, "quotes", f, False) or 0}
        v = _int(p, "views", f, False)
        if v is not None:
            pm["impression_count"] = v
        ca = _ts(p.get("created_at"), f + ".created_at")
        t = {"id": str(p["id"]) if p.get("id") is not None else _hid(ca, p.get("text", "")),
             "text": p.get("text", ""), "created_at": ca, "public_metrics": pm}
        if p.get("author_handle"):
            t["author_handle"] = p["author_handle"].lstrip("@")
        if p.get("type") in ("reply", "repost", "quote"):
            t["referenced_tweets"] = [{"type": {"reply": "replied_to", "repost": "retweeted", "quote": "quoted"}[p["type"]]}]
        tweets.append(t)
    mc = payload.get("mention_counts")
    if mc is not None:
        if not isinstance(mc, list):
            raise IngestError("mention_counts must be an array")
        out_counts = []
        for i, c in enumerate(mc):
            f = f"mention_counts[{i}]"
            st, en = _ts(c.get("start"), f + ".start"), _ts(c.get("end"), f + ".end")
            if datetime.fromisoformat(en.replace("Z", "+00:00")) <= datetime.fromisoformat(st.replace("Z", "+00:00")):
                raise IngestError(f"{f}: end must be after start")
            out_counts.append({"start": st, "end": en,
                               "tweet_count": _int(c, "tweet_count", f)})
    ments = payload.get("mentions")
    out = {"handle": handle, "errors": [], "collected_at": payload["collected_at"], "user": user,
           "tweets": tweets, "mention_counts": out_counts if mc is not None else None, "source": SOURCE}
    if ments is not None:
        if not isinstance(ments, list):
            raise IngestError("mentions must be an array")
        mt, users = [], {}
        for i, m in enumerate(ments):
            f = f"mentions[{i}]"
            a = (m.get("author_handle") or m.get("author") or "").lstrip("@").lower()
            if not a:
                raise IngestError(f"{f}.author_handle is required")
            ca = _ts(m.get("created_at"), f + ".created_at")
            mt.append({"id": str(m["id"]) if m.get("id") is not None else _hid(a, ca, m.get("text", "")), "author_id": a, "text": m.get("text", ""),
                       "created_at": ca,
                       "public_metrics": {"like_count": _int(m, "likes", f, False) or 0,
                                          "retweet_count": _int(m, "reposts", f, False) or 0,
                                          "reply_count": _int(m, "replies", f, False) or 0}})
            u = {"id": a, "username": a}
            fol = _int(m, "author_followers", f, False)
            if fol is not None:
                u["public_metrics"] = {"followers_count": fol}
            if m.get("author_created_at"):
                u["created_at"] = _ts(m["author_created_at"], f + ".author_created_at")
            if m.get("author_verified_type"):
                u["verified_type"] = m["author_verified_type"]
            old = users.get(a)
            if old:  # same author seen twice: merge deterministically (order-independent)
                of = (old.get("public_metrics") or {}).get("followers_count")
                nf = (u.get("public_metrics") or {}).get("followers_count")
                if of is not None and (nf is None or of > nf):
                    u["public_metrics"] = old["public_metrics"]
                for k in ("created_at", "verified_type"):
                    if old.get(k) and (not u.get(k) or str(old[k]) < str(u[k])):
                        u[k] = old[k]
            users[a] = u
        out["mentions"] = {"tweets": mt, "users": list(users.values())}
    return out
