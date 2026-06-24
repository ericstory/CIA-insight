"""Generic geo / language resolver — topic-agnostic.

Single source of truth mapping an ISO country code to the per-source codes each
fetcher needs (DataForSEO location_code/language_code, Ahrefs country, app-store
country, YouTube regionCode). Lets any analysis fan out by market without
hardcoding country tables in every fetcher.
"""
from __future__ import annotations

# country -> (dataforseo_location_code, default_language_code, ahrefs_country,
#             store_country, youtube_region)
_GEO = {
    "us": (2840, "en", "us", "us", "US"),
    "gb": (2826, "en", "gb", "gb", "GB"),
    "uk": (2826, "en", "gb", "gb", "GB"),  # alias
    "ca": (2124, "en", "ca", "ca", "CA"),
    "au": (2036, "en", "au", "au", "AU"),
    "de": (2276, "de", "de", "de", "DE"),
    "fr": (2250, "fr", "fr", "fr", "FR"),
    "es": (2724, "es", "es", "es", "ES"),
    "it": (2380, "it", "it", "it", "IT"),
    "jp": (2392, "ja", "jp", "jp", "JP"),
    "kr": (2410, "ko", "kr", "kr", "KR"),
    "in": (2356, "en", "in", "in", "IN"),
    "id": (2360, "id", "id", "id", "ID"),
    "br": (2076, "pt", "br", "br", "BR"),
    "mx": (2484, "es", "mx", "mx", "MX"),
    "ru": (2643, "ru", "ru", "ru", "RU"),
    "tr": (2792, "tr", "tr", "tr", "TR"),
    "nl": (2528, "nl", "nl", "nl", "NL"),
}


def resolve(country: str = "us", lang: str | None = None) -> dict:
    """ISO country -> per-source codes. Unknown country falls back to US."""
    c = (country or "us").lower()
    loc, dlang, ahrefs, store, yt = _GEO.get(c, _GEO["us"])
    return {
        "country": c,
        "location_code": loc,
        "language_code": lang or dlang,
        "ahrefs_country": ahrefs,
        "store_country": store,
        "youtube_region": yt,
    }


def markets(countries: list[str]) -> list[dict]:
    """Resolve a list of countries for multi-market fan-out."""
    return [resolve(c) for c in (countries or ["us"])]


def supported() -> list[str]:
    return sorted(k for k in _GEO if k != "uk")
