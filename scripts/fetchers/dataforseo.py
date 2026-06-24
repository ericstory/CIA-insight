"""DataForSEO fetchers: App Store SERP, Keywords-for-App, App Reviews.

Cost reference (US prices, 2026):
  - App Searches:        $0.0012 per 100 items   (keyword -> ranked apps)
  - Keywords-for-App:    $0.01 per task + $0.0001 per item  (app -> ranking keywords + SV)
  - App Reviews:         $0.00075 per 50 reviews
  - App Info:            $0.0006 per result
"""
from __future__ import annotations

import time
from typing import Iterable, Optional

import requests

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from config import dataforseo_auth

BASE = "https://api.dataforseo.com/v3"
DEFAULT_TIMEOUT = 90


def _headers() -> dict:
    return {
        "Authorization": f"Basic {dataforseo_auth()}",
        "Content-Type": "application/json",
    }


def _post(path: str, payload: list[dict]) -> dict:
    r = requests.post(BASE + path, headers=_headers(), json=payload, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("status_code") != 20000:
        raise RuntimeError(f"DataForSEO error {data.get('status_code')}: {data.get('status_message')}")
    return data


# NOTE: App Store SERP (keyword -> ranked apps) is handled by iTunes Search API
# (free + live + reliable for US). See fetchers/itunes.py :: keyword_to_apps.
# DataForSEO's app_searches endpoint is task-based with polling and not worth
# the latency + cost for our SERP needs.

# ---------- Keywords-for-App (app -> ranking keywords + SV) ----------

def fetch_keywords_for_app(
    app_id: str,
    *,
    location_code: int = 2840,
    language_code: str = "en",
    limit: int = 200,
    order_by: str = "keyword_data.keyword_info.search_volume,desc",
) -> tuple[list[dict], float]:
    """Returns ranking keywords for an app, sorted by search volume desc."""
    payload = [{
        "app_id": app_id,
        "location_code": location_code,
        "language_code": language_code,
        "limit": limit,
        "order_by": [order_by],
    }]
    resp = _post("/dataforseo_labs/apple/keywords_for_app/live", payload)
    task = resp["tasks"][0]
    cost = float(resp.get("cost", 0))
    if not task.get("result"):
        return [], cost
    items = task["result"][0].get("items") or []
    rows = []
    for it in items:
        kd = it.get("keyword_data") or {}
        ki = kd.get("keyword_info") or {}
        ranked = it.get("ranked_serp_element") or {}
        se_el = ranked.get("serp_item") or {}
        rows.append({
            "app_id": app_id,
            "keyword": kd.get("keyword"),
            "rank": se_el.get("rank_absolute"),
            "search_volume": ki.get("search_volume"),
            "country": _country_code(location_code),
        })
    return rows, cost


def fetch_keywords_for_apps(
    app_ids: Iterable[str],
    *,
    location_code: int = 2840,
    language_code: str = "en",
    limit_per_app: int = 200,
) -> tuple[list[dict], float]:
    out: list[dict] = []
    total_cost = 0.0
    for aid in app_ids:
        rows, cost = fetch_keywords_for_app(
            aid, location_code=location_code, language_code=language_code, limit=limit_per_app
        )
        out.extend(rows)
        total_cost += cost
    return out, total_cost


# ---------- App Reviews ----------

def fetch_app_reviews(
    app_id: str,
    *,
    location_code: int = 2840,
    depth: int = 200,
    sort_by: str = "most_helpful",
) -> tuple[list[dict], float]:
    """Returns recent app reviews."""
    payload = [{
        "app_id": app_id,
        "location_code": location_code,
        "language_name": "English",
        "depth": depth,
        "sort_by": sort_by,
    }]
    resp = _post("/app_data/apple/app_reviews/task_post", payload)
    task_id = resp["tasks"][0]["id"]
    # Poll task_get — DataForSEO reviews tasks usually take 60-180s.
    # Try for up to 6 minutes (90 × 4s).
    data = None
    for _ in range(90):
        time.sleep(4)
        get_resp = requests.get(
            f"{BASE}/app_data/apple/app_reviews/task_get/advanced/{task_id}",
            headers=_headers(), timeout=DEFAULT_TIMEOUT,
        )
        get_resp.raise_for_status()
        data = get_resp.json()
        t = (data.get("tasks") or [{}])[0]
        # status_code 40602 = "Task In Queue", 40601 = "Task Handed", 20000 = ready
        if t.get("status_code") == 20000 and t.get("result"):
            break
    else:
        return [], float(resp.get("cost", 0))

    cost = float(resp.get("cost", 0)) + float(data.get("cost", 0))
    items = (data["tasks"][0]["result"][0] or {}).get("items") or []
    rows = []
    for it in items:
        up = it.get("user_profile") or {}
        rows.append({
            "id": f"{app_id}::{it.get('id')}",
            "app_id": app_id,
            "rating": it.get("rating", {}).get("value") if isinstance(it.get("rating"), dict) else it.get("rating"),
            "title": it.get("title"),
            "body": it.get("review_text"),
            "author": up.get("profile_name") if isinstance(up, dict) else None,
            "posted_at": it.get("timestamp"),
        })
    return rows, cost


# ---------- App Info (cheap metadata fallback) ----------

def fetch_app_info(app_id: str, *, location_code: int = 2840) -> tuple[Optional[dict], float]:
    payload = [{"app_id": app_id, "location_code": location_code}]
    resp = _post("/app_data/apple/app_info/live", payload)
    task = resp["tasks"][0]
    cost = float(resp.get("cost", 0))
    if not task.get("result"):
        return None, cost
    item = (task["result"][0] or {}).get("items") or []
    if not item:
        return None, cost
    a = item[0]
    return {
        "app_id": app_id,
        "name": a.get("title"),
        "subtitle": a.get("subtitle"),
        "description": a.get("description"),
        "developer": (a.get("developer") or {}).get("name") if isinstance(a.get("developer"), dict) else a.get("developer"),
        "rating": (a.get("rating") or {}).get("value") if isinstance(a.get("rating"), dict) else None,
        "review_count": (a.get("rating") or {}).get("votes_count") if isinstance(a.get("rating"), dict) else None,
        "price": a.get("price"),
        "category": (a.get("primary_category") or {}).get("title") if isinstance(a.get("primary_category"), dict) else None,
        "bundle_id": a.get("bundle_id"),
        "icon_url": a.get("icon"),
        "url": a.get("url"),
    }, cost


# ---------- helpers ----------

LOCATION_TO_CC = {2840: "us", 2392: "jp", 2826: "uk", 2124: "ca", 2036: "au", 2276: "de"}

def _country_code(location_code: int) -> str:
    return LOCATION_TO_CC.get(location_code, str(location_code))
