"""YouTube Data API v3 fetcher. 10K units/day free quota.

Quota costs:
  - search.list: 100 units/req
  - videos.list: 1 unit/req (per ID)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional
import time

import requests

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from config import youtube_api_key

BASE = "https://www.googleapis.com/youtube/v3"


def _get_with_retry(url: str, params: dict, *, max_retries: int = 4, timeout: int = 30):
    """requests.get with retry on transient ConnectTimeout/ConnectionError
    (defends against local proxy/TUN flakiness for daemon processes)."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return requests.get(url, params=params, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt < max_retries - 1:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise
    raise last_exc  # unreachable


def search_videos(
    query: str,
    *,
    max_results: int = 25,
    order: str = "viewCount",
    published_after_days: Optional[int] = 365,
    region: str = "US",
    language: str = "en",
) -> list[str]:
    """Returns list of video IDs. Cheap-ish; consume quota carefully."""
    params = {
        "part": "id",
        "q": query,
        "type": "video",
        "maxResults": min(max_results, 50),
        "order": order,
        "regionCode": region,
        "relevanceLanguage": language,
        "key": youtube_api_key(),
    }
    if published_after_days:
        params["publishedAfter"] = (
            datetime.utcnow() - timedelta(days=published_after_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = _get_with_retry(f"{BASE}/search", params, timeout=30)
    r.raise_for_status()
    return [item["id"]["videoId"] for item in r.json().get("items", []) if item["id"].get("videoId")]


def get_video_details(video_ids: Iterable[str]) -> list[dict]:
    """Hydrate video IDs with stats + metadata. Batch up to 50 IDs/req."""
    ids = list(video_ids)
    out: list[dict] = []
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        params = {
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(batch),
            "key": youtube_api_key(),
        }
        r = _get_with_retry(f"{BASE}/videos", params, timeout=30)
        r.raise_for_status()
        for v in r.json().get("items", []):
            sn = v["snippet"]
            st = v.get("statistics", {})
            out.append({
                "id": v["id"],
                "query": None,
                "title": sn.get("title"),
                "description": sn.get("description"),
                "channel": sn.get("channelTitle"),
                "views": int(st.get("viewCount", 0) or 0),
                "likes": int(st.get("likeCount", 0) or 0),
                "comments": int(st.get("commentCount", 0) or 0),
                "duration_seconds": _parse_duration(v.get("contentDetails", {}).get("duration")),
                "url": f"https://www.youtube.com/watch?v={v['id']}",
                "posted_at": sn.get("publishedAt"),
            })
    return out


def fetch_videos_for_queries(
    queries: Iterable[str],
    *,
    per_query: int = 15,
    published_after_days: int = 365,
) -> list[dict]:
    """Search + hydrate for multiple queries. Tags rows with their source query."""
    rows: list[dict] = []
    for q in queries:
        try:
            ids = search_videos(q, max_results=per_query, published_after_days=published_after_days)
            details = get_video_details(ids)
            for d in details:
                d["query"] = q
            rows.extend(details)
        except requests.HTTPError as e:
            # Quota exhaustion or query error — skip this query, keep going
            print(f"[youtube] search failed for {q!r}: {e}", flush=True)
    return rows


def _parse_duration(iso: Optional[str]) -> Optional[int]:
    """Parse ISO 8601 duration (PT#H#M#S) to seconds."""
    if not iso or not iso.startswith("PT"):
        return None
    iso = iso[2:]
    sec = 0
    cur = ""
    for ch in iso:
        if ch.isdigit():
            cur += ch
        elif ch == "H":
            sec += int(cur or 0) * 3600; cur = ""
        elif ch == "M":
            sec += int(cur or 0) * 60; cur = ""
        elif ch == "S":
            sec += int(cur or 0); cur = ""
    return sec or None
