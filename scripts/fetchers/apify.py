"""Apify fetchers — TikTok and Reddit content via run-sync."""
from __future__ import annotations

from typing import Iterable

import requests

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from config import apify_token

BASE = "https://api.apify.com/v2"
DEFAULT_TIMEOUT = 600  # actor runs can be slow


def _run_actor(actor: str, payload: dict, *, memory: int = 512, run_timeout: int = 180) -> list[dict]:
    """Synchronous run + dataset items fetch."""
    actor_path = actor.replace("/", "~")
    url = (
        f"{BASE}/acts/{actor_path}/run-sync-get-dataset-items"
        f"?token={apify_token()}&timeout={run_timeout}&memory={memory}"
    )
    r = requests.post(url, json=payload, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ---------- TikTok ----------

def fetch_tiktok_search(queries: Iterable[str], *, max_items: int = 80) -> list[dict]:
    """Search TikTok for videos matching the queries. Returns normalized rows.

    Actor field reference (clockworks/free-tiktok-scraper):
      - searchQueries: list of keyword strings
      - searchSection: "top" | "video" | "user"
      - resultsPerPage: int (this is the per-query result count — NOT maxItems)
    """
    queries = [q for q in queries if q]
    if not queries:
        return []
    payload = {
        "searchQueries": queries,
        "resultsPerPage": max_items,
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": False,
        "shouldDownloadSubtitles": False,
        "shouldDownloadSlideshowImages": False,
    }
    raw = _run_actor("clockworks/free-tiktok-scraper", payload, memory=1024, run_timeout=300)
    rows = []
    for it in raw:
        rows.append({
            "id": str(it.get("id") or it.get("webVideoUrl", "")),
            "query": it.get("searchQuery") or (queries[0] if queries else None),
            "author": (it.get("authorMeta") or {}).get("name") or it.get("authorMeta", {}).get("nickName"),
            "plays": it.get("playCount"),
            "likes": it.get("diggCount"),
            "shares": it.get("shareCount"),
            "comments": it.get("commentCount"),
            "text": (it.get("text") or "").strip(),
            "url": it.get("webVideoUrl"),
            "posted_at": it.get("createTimeISO"),
        })
    return rows


def fetch_tiktok_hashtag(hashtags: Iterable[str], *, max_items: int = 50) -> list[dict]:
    """Pull recent videos under given hashtags."""
    tags = [h.lstrip("#") for h in hashtags if h]
    if not tags:
        return []
    payload = {"hashtags": tags, "resultsPerPage": max_items, "shouldDownloadVideos": False}
    raw = _run_actor("clockworks/tiktok-hashtag-scraper", payload, memory=1024, run_timeout=180)
    rows = []
    for it in raw:
        rows.append({
            "id": str(it.get("id") or it.get("webVideoUrl", "")),
            "query": "#" + (it.get("hashtag") or ""),
            "author": (it.get("authorMeta") or {}).get("name"),
            "plays": it.get("playCount"),
            "likes": it.get("diggCount"),
            "shares": it.get("shareCount"),
            "comments": it.get("commentCount"),
            "text": (it.get("text") or "").strip(),
            "url": it.get("webVideoUrl"),
            "posted_at": it.get("createTimeISO"),
        })
    return rows


# ---------- Reddit ----------

def fetch_reddit_search(
    queries: Iterable[str],
    *,
    subreddits: list[str] | None = None,
    max_items: int = 60,
    sort: str = "top",
    time_filter: str = "year",
) -> list[dict]:
    """Search Reddit. Optionally restrict to subreddits."""
    queries = [q for q in queries if q]
    if not queries:
        return []
    payload = {
        "searches": queries,
        "searchPosts": True,
        "searchComments": False,
        "searchCommunities": False,
        "searchUsers": False,
        "sort": sort,
        "time": time_filter,
        "maxItems": max_items,
        "maxPostCount": max_items,
    }
    if subreddits:
        payload["restrictToSubreddits"] = subreddits
    raw = _run_actor("trudax/reddit-scraper-lite", payload, memory=1024, run_timeout=300)
    rows = []
    for it in raw:
        if it.get("dataType") and it["dataType"] != "post":
            continue
        rows.append({
            "id": str(it.get("id") or it.get("url", "")),
            "subreddit": it.get("parsedCommunityName") or it.get("communityName"),
            "query": it.get("searchQuery"),
            "title": it.get("title"),
            "body": (it.get("body") or it.get("selftext") or "").strip(),
            "score": it.get("upVotes") or it.get("score"),
            "num_comments": it.get("numberOfComments") or it.get("num_comments"),
            "author": it.get("username") or it.get("author"),
            "url": it.get("url"),
            "posted_at": it.get("createdAt") or it.get("created_utc"),
        })
    return rows
