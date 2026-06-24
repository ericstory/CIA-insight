"""Generic web data — topic-agnostic.

Two primitives any market analysis needs:
  - google_serp(query): Google organic results for a query (DataForSEO live).
  - fetch_url(url): readable text + title from any page (stdlib only, no bs4).
"""
from __future__ import annotations

import html as _html
import re

import requests

from config import dataforseo_auth

_DFS_SERP = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"
_UA = "Mozilla/5.0 (compatible; cia-research-bot/1.0; +https://cia.ericstory.me)"


def google_serp(
    query: str,
    *,
    location_code: int = 2840,
    language_code: str = "en",
    depth: int = 10,
) -> list[dict]:
    """Google organic SERP for `query`. Returns ranked organic rows."""
    payload = [{
        "keyword": query,
        "location_code": location_code,
        "language_code": language_code,
        "depth": depth,
    }]
    r = requests.post(
        _DFS_SERP,
        headers={"Authorization": f"Basic {dataforseo_auth()}",
                 "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status_code") != 20000:
        raise RuntimeError(f"DataForSEO error {data.get('status_code')}: {data.get('status_message')}")
    task = (data.get("tasks") or [{}])[0]
    result = (task.get("result") or [{}])[0]
    items = result.get("items") or []
    rows = []
    for it in items:
        if it.get("type") != "organic":
            continue
        rows.append({
            "rank": it.get("rank_absolute"),
            "title": it.get("title"),
            "url": it.get("url"),
            "domain": it.get("domain"),
            "snippet": it.get("description"),
        })
    return rows


_DROP = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_BLANK = re.compile(r"\n\s*\n\s*\n+")


def fetch_url(url: str, *, max_chars: int = 20000) -> dict:
    """Fetch a page and return {url, title, text, truncated}. stdlib-only extraction."""
    r = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
    r.raise_for_status()
    raw = r.text

    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    if m:
        title = _html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()

    body = _DROP.sub(" ", raw)
    text = _TAGS.sub(" ", body)
    text = _html.unescape(text)
    text = re.sub(r"[ \t\r\f]+", " ", text)
    text = _BLANK.sub("\n\n", text).strip()

    return {
        "url": url,
        "title": title,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    }
