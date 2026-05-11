"""iTunes Search API — free app metadata. No auth required."""
from __future__ import annotations

from typing import Iterable, Optional

import requests

BASE = "https://itunes.apple.com"


def search_apps(term: str, *, country: str = "us", limit: int = 50) -> list[dict]:
    """Search the App Store. Returns list of app metadata."""
    r = requests.get(
        f"{BASE}/search",
        params={"term": term, "country": country, "media": "software", "entity": "software", "limit": limit},
        timeout=30,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return [_normalize(a) for a in results]


def keyword_to_apps(keyword: str, *, country: str = "us", limit: int = 20) -> list[dict]:
    """Returns App Store SERP for a keyword. Each row = one ranked app.

    iTunes Search ordering reflects the live App Store SERP for unbranded queries.
    """
    raw = requests.get(
        f"{BASE}/search",
        params={"term": keyword, "country": country, "media": "software",
                "entity": "software", "limit": limit},
        timeout=30,
    )
    raw.raise_for_status()
    rows = []
    for rank, a in enumerate(raw.json().get("results", []), start=1):
        rows.append({
            "keyword": keyword,
            "rank": rank,
            "app_id": str(a.get("trackId") or ""),
            "app_name": a.get("trackName"),
            "developer": a.get("artistName"),
            "rating": a.get("averageUserRating"),
            "review_count": a.get("userRatingCount"),
            "icon_url": a.get("artworkUrl512") or a.get("artworkUrl100"),
            "country": country,
        })
    return rows


def keyword_to_apps_bulk(keywords: Iterable[str], *, country: str = "us", limit: int = 20) -> list[dict]:
    out: list[dict] = []
    for kw in keywords:
        try:
            out.extend(keyword_to_apps(kw, country=country, limit=limit))
        except Exception as e:
            print(f"[itunes] search failed for {kw!r}: {e}", flush=True)
    return out


def lookup_apps(app_ids: Iterable[str], *, country: str = "us") -> list[dict]:
    """Lookup specific apps by ID. Returns list of normalized app metadata."""
    ids = ",".join(str(a) for a in app_ids)
    if not ids:
        return []
    r = requests.get(
        f"{BASE}/lookup",
        params={"id": ids, "country": country},
        timeout=30,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return [_normalize(a) for a in results if a.get("kind") == "software"]


def _normalize(a: dict) -> dict:
    review_count = a.get("userRatingCount") or 0
    # Heuristic download estimate from reviews (industry rule of thumb: 50-100x reviews)
    return {
        "app_id": str(a.get("trackId") or ""),
        "name": a.get("trackName"),
        "subtitle": a.get("description", "")[:120] if a.get("description") else None,
        "description": a.get("description"),
        "developer": a.get("artistName"),
        "rating": a.get("averageUserRating"),
        "review_count": review_count,
        "est_downloads_low": review_count * 50,
        "est_downloads_high": review_count * 100,
        "price": "Free" if a.get("price") == 0 else f"${a.get('price')}",
        "category": a.get("primaryGenreName"),
        "bundle_id": a.get("bundleId"),
        "icon_url": a.get("artworkUrl512") or a.get("artworkUrl100"),
        "url": a.get("trackViewUrl"),
    }
