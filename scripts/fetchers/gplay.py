"""Google Play Store fetchers using google-play-scraper."""
from __future__ import annotations

from typing import Optional


def _import_gplay():
    try:
        from google_play_scraper import search as gplay_search, app as gplay_app
        return gplay_search, gplay_app
    except ImportError:
        raise RuntimeError(
            "google-play-scraper not installed. Run: pip3 install google-play-scraper --break-system-packages"
        )


# Country code → (lang, country) for google-play-scraper
_COUNTRY_LANG = {
    "us": ("en", "us"),
    "gb": ("en", "gb"),
    "au": ("en", "au"),
    "ca": ("en", "ca"),
    "in": ("en", "in"),
    "sg": ("en", "sg"),
    "br": ("pt", "br"),
    "jp": ("ja", "jp"),
    "de": ("de", "de"),
    "fr": ("fr", "fr"),
    "mx": ("es", "mx"),
    "id": ("id", "id"),
    "ph": ("en", "ph"),
    "my": ("en", "my"),
    "za": ("en", "za"),
}


def keyword_to_apps(keyword: str, country: str = "us", limit: int = 20) -> list[dict]:
    """Search Google Play by keyword, return ranked app list."""
    gplay_search, _ = _import_gplay()
    lang, cc = _COUNTRY_LANG.get(country.lower(), ("en", country.lower()))
    try:
        results = gplay_search(keyword, lang=lang, country=cc, n_hits=limit)
    except Exception as e:
        return []
    rows = []
    for rank, r in enumerate(results, 1):
        rows.append({
            "keyword": keyword,
            "rank": rank,
            "app_id": r.get("appId"),
            "app_name": r.get("title"),
            "developer": r.get("developer"),
            "rating": r.get("score"),
            "review_count": r.get("ratings"),
            "icon_url": r.get("icon"),
            "country": country.lower(),
            "store": "android",
        })
    return rows


def keyword_to_apps_bulk(keywords: list[str], country: str = "us", limit: int = 20) -> list[dict]:
    rows = []
    for kw in keywords:
        rows.extend(keyword_to_apps(kw, country=country, limit=limit))
    return rows


def lookup_apps(app_ids: list[str], country: str = "us") -> list[dict]:
    """Fetch metadata for a list of Google Play app IDs (package names)."""
    _, gplay_app = _import_gplay()
    lang, cc = _COUNTRY_LANG.get(country.lower(), ("en", country.lower()))
    rows = []
    for aid in app_ids:
        try:
            a = gplay_app(aid, lang=lang, country=cc)
        except Exception:
            continue
        rows.append({
            "app_id": a.get("appId"),
            "name": a.get("title"),
            "subtitle": None,
            "description": (a.get("description") or "")[:500],
            "developer": a.get("developer"),
            "rating": a.get("score"),
            "review_count": a.get("ratings"),
            "est_downloads_low": _parse_installs_low(a.get("installs")),
            "est_downloads_high": _parse_installs_high(a.get("installs")),
            "price": "Free" if a.get("free") else str(a.get("price", "")),
            "category": a.get("genre"),
            "bundle_id": a.get("appId"),
            "icon_url": a.get("icon"),
            "url": a.get("url"),
            "store": "android",
        })
    return rows


def _parse_installs_low(installs_str: Optional[str]) -> Optional[int]:
    if not installs_str:
        return None
    try:
        return int(installs_str.replace("+", "").replace(",", "").strip())
    except Exception:
        return None


def _parse_installs_high(installs_str: Optional[str]) -> Optional[int]:
    low = _parse_installs_low(installs_str)
    if low is None:
        return None
    if low == 0:
        return 100
    return low * 10
