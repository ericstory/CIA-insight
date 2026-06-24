"""Reddit JSON public API fetcher (no Apify required).

Uses reddit.com/r/X/search.json — rate limited but free. Writes directly to
social_reddit table.

Usage:
  python3 fetch_reddit_free.py --topic "amazon seller tools" \
    --queries "helium 10,jungle scout" \
    --subreddits "FulfillmentByAmazon,AmazonSeller" \
    [--global-queries "best amazon software"] [--limit 25]
"""
from __future__ import annotations

import argparse
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import db
import requests

UA = "Mozilla/5.0 (compatible; cia-research-bot/1.0; +https://cia.ericstory.me)"
BASE = "https://www.reddit.com"


def search_subreddit(subreddit: str, query: str, *, limit: int = 25,
                     sort: str = "top", t: str = "year") -> list[dict]:
    """Search within a subreddit. Returns normalized rows."""
    url = f"{BASE}/r/{subreddit}/search.json"
    params = {"q": query, "restrict_sr": "1", "sort": sort, "t": t, "limit": limit}
    r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=20)
    if r.status_code == 429:
        time.sleep(5)
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    return _normalize(r.json(), query=query)


def search_global(query: str, *, limit: int = 25,
                  sort: str = "top", t: str = "year") -> list[dict]:
    """Search across all of reddit."""
    url = f"{BASE}/search.json"
    params = {"q": query, "sort": sort, "t": t, "limit": limit, "type": "link"}
    r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=20)
    if r.status_code == 429:
        time.sleep(5)
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    return _normalize(r.json(), query=query)


def _normalize(payload: dict, *, query: str) -> list[dict]:
    rows = []
    for ch in payload.get("data", {}).get("children", []):
        d = ch.get("data") or {}
        rows.append({
            "id": d.get("id") or d.get("name") or d.get("permalink"),
            "subreddit": d.get("subreddit"),
            "query": query,
            "title": d.get("title"),
            "body": (d.get("selftext") or "").strip(),
            "score": d.get("score"),
            "num_comments": d.get("num_comments"),
            "author": d.get("author"),
            "url": f"{BASE}{d.get('permalink','')}" if d.get("permalink") else d.get("url"),
            "posted_at": d.get("created_utc"),
        })
    return [r for r in rows if r["id"] and r["title"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--queries", required=True, help="comma-separated")
    ap.add_argument("--subreddits", default="", help="comma-separated; empty = global search only")
    ap.add_argument("--global-queries", default="", help="extra queries for global search (not subreddit-restricted)")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--sleep", type=float, default=1.5, help="sleep between requests (seconds)")
    args = ap.parse_args()

    queries = [q.strip() for q in args.queries.split(",") if q.strip()]
    subreddits = [s.strip() for s in args.subreddits.split(",") if s.strip()]
    global_queries = [q.strip() for q in args.global_queries.split(",") if q.strip()]

    # Resolve DB path
    import cli
    p = cli.db_path_for(args.topic)
    print(f"DB: {p}", flush=True)

    all_rows: list[dict] = []
    seen_ids: set = set()

    # 1) Subreddit-restricted searches
    for sub in subreddits:
        for q in queries:
            try:
                rows = search_subreddit(sub, q, limit=args.limit)
                new = [r for r in rows if r["id"] not in seen_ids]
                for r in new:
                    seen_ids.add(r["id"])
                all_rows.extend(new)
                print(f"  r/{sub} q={q!r}: +{len(new)} (total {len(all_rows)})", flush=True)
                time.sleep(args.sleep)
            except Exception as e:
                print(f"  r/{sub} q={q!r}: FAIL {type(e).__name__}: {e}", flush=True)
                time.sleep(args.sleep * 2)

    # 2) Global search (no subreddit restriction)
    for q in global_queries:
        try:
            rows = search_global(q, limit=args.limit)
            new = [r for r in rows if r["id"] not in seen_ids]
            for r in new:
                seen_ids.add(r["id"])
            all_rows.extend(new)
            print(f"  global q={q!r}: +{len(new)} (total {len(all_rows)})", flush=True)
            time.sleep(args.sleep)
        except Exception as e:
            print(f"  global q={q!r}: FAIL {type(e).__name__}: {e}", flush=True)

    n = db.upsert_rows(p, "social_reddit", all_rows)
    print(f"\nWrote {n} new rows to social_reddit.")
    db.log_fetch(p, "reddit_free", "search", {"queries": queries, "subreddits": subreddits}, rows=n)


if __name__ == "__main__":
    main()
