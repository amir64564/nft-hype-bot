"""TEST-ONLY synthetic data. Never used by the bot at runtime."""
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fake_x(followers=50_000, n_tweets=40, likes=800, mentions=100, dup=False, influencers=5):
    user = {"id": "123", "username": "TestProj", "name": "Test Project", "verified": True,
            "verified_type": "business", "description": "Test Project NFT https://t.co/x",
            "created_at": iso(NOW - timedelta(days=800)),
            "entities": {"url": {"urls": [{"expanded_url": "https://testproj.example"}]}},
            "public_metrics": {"followers_count": followers, "following_count": 300,
                               "tweet_count": 5000, "listed_count": 200}}
    tweets = [{"id": str(i), "text": f"update {i}", "created_at": iso(NOW - timedelta(hours=18 * i)),
               "public_metrics": {"like_count": likes, "retweet_count": likes // 5, "reply_count": likes // 10,
                                  "quote_count": likes // 40, "impression_count": likes * 30}}
              for i in range(n_tweets)]
    users = [{"id": f"u{i}", "username": f"user{i}", "created_at": iso(NOW - timedelta(days=500)),
              "public_metrics": {"followers_count": 50_000 if i < influencers else 300}} for i in range(mentions)]
    mt = [{"id": f"m{i}", "author_id": f"u{i}", "created_at": iso(NOW - timedelta(hours=i)),
           "text": "huge alpha join the whitelist now before it closes" if dup else f"I think the {i} roadmap for project is genuinely interesting number {i}",
           "public_metrics": {"like_count": 5, "retweet_count": 1, "reply_count": 1}} for i in range(mentions)]
    return {"handle": "TestProj", "errors": [], "collected_at": iso(NOW), "user": user, "tweets": tweets,
            "mentions": {"tweets": mt, "users": users}, "mention_counts": None}
