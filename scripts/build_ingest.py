#!/usr/bin/env python3
"""Build an ingest JSON from saved x-tool outputs (real data only).
usage: build_ingest.py OUT PROFILE POSTS COUNTS MENTIONS... [--collected-at ISO]"""
import json, sys
def L(p):
    t = open(p).read(); return json.JSONDecoder().raw_decode(t[t.index('{'):])[0]
args = sys.argv[1:]; ca = None
if "--collected-at" in args:
    i = args.index("--collected-at"); ca = args[i + 1]; del args[i:i + 2]
out, prof, postsf, countsf, *mfs = args
u = L(prof)["data"]; pm = u["public_metrics"]
acc = {"username": u["username"], "name": u["name"], "followers": pm["followers_count"],
       "following": pm["following_count"], "tweet_count": pm["tweet_count"], "listed": pm["listed_count"],
       "created_at": u["created_at"], "verified": u["verified"], "verified_type": u.get("verified_type"),
       "description": u.get("description", ""),
       "links": [x["expanded_url"] for x in ((u.get("entities") or {}).get("url") or {}).get("urls", [])]}
posts = []
for t in L(postsf).get("data", []):
    if any(r["type"] == "replied_to" for r in t.get("referenced_tweets", [])):
        continue
    m = t["public_metrics"]
    x = {"id": t["id"], "text": t.get("text", ""), "created_at": t["created_at"], "likes": m["like_count"],
         "replies": m["reply_count"], "reposts": m["retweet_count"], "quotes": m["quote_count"], "author_handle": u["username"]}
    if "impression_count" in m: x["views"] = m["impression_count"]
    posts.append(x)
users, tw = {}, {}
for f in mfs:
    p = L(f)
    for x in p.get("includes", {}).get("users", []): users[x["id"]] = x
    for t in p.get("data", []): tw[t["id"]] = t
ments = []
for t in tw.values():
    a = users.get(t["author_id"])
    if not a: continue
    m = t.get("public_metrics", {})
    d = {"text": t.get("text", ""), "author_handle": a["username"], "created_at": t["created_at"],
         "likes": m.get("like_count"), "replies": m.get("reply_count"), "reposts": m.get("retweet_count")}
    if a.get("public_metrics"): d["author_followers"] = a["public_metrics"]["followers_count"]
    if a.get("created_at"): d["author_created_at"] = a["created_at"]
    if a.get("verified_type") and a["verified_type"] != "none": d["author_verified_type"] = a["verified_type"]
    ments.append({k: v for k, v in d.items() if v is not None})
C = L(countsf)
o = {"source": "grokbot-x-read", "collected_at": ca or C["data"][-1]["end"], "account": acc, "recent_posts": posts,
     "mentions": ments, "mention_counts": [{k: c[k] for k in ("start", "end", "tweet_count")} for c in C["data"]]}
json.dump(o, open(out, "w"), indent=1, ensure_ascii=False)
print(json.dumps({"posts": len(posts), "mentions": len(ments), "counts_7d": sum(c["tweet_count"] for c in C["data"])}))
