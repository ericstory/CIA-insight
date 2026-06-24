"""Ingest Ahrefs MCP results (JSON) into SQLite.

Claude calls the MCP tools, saves response JSON to disk, then invokes:
  python3 cli.py ingest --source <name> --file path/to/response.json

Supported sources:
  - ahrefs-keywords-overview      → keywords_google
  - ahrefs-matching-terms         → keywords_google
  - ahrefs-related-terms          → keywords_google
  - ahrefs-site-explorer-metrics  → competitors_web
  - ahrefs-organic-keywords       → competitor_organic_kw
  - ahrefs-brand-radar-sov        → ai_visibility
  - ahrefs-brand-radar-mentions   → ai_visibility
"""
from __future__ import annotations

import json
import pathlib
from typing import Any


def normalize_keywords(raw: dict, *, source_seed: str | None = None, country: str = "us") -> list[dict]:
    """Normalize Ahrefs keywords-explorer responses.

    Handles both `keywords-explorer-overview` (rows under `metrics`/`keywords`)
    and `matching-terms`/`related-terms` (rows under `keywords`).
    Field names are tolerant since MCP responses sometimes nest differently.
    """
    rows: list[dict] = []
    items = _items(raw)
    for it in items:
        kw = it.get("keyword") or it.get("term") or it.get("query")
        if not kw:
            continue
        cpc = _num(it.get("cpc"))
        # Ahrefs returns CPC in cents
        cpc_usd = round(cpc / 100, 2) if cpc and cpc > 0 else None
        rows.append({
            "keyword": kw,
            "volume": _num(it.get("volume") or it.get("search_volume")),
            "kd": _num(it.get("difficulty") or it.get("kd")),
            "cpc_usd": cpc_usd,
            "intent": _intent(it.get("intents") or it.get("intent")),
            "traffic_potential": _num(it.get("traffic_potential") or it.get("traffic")),
            "parent_topic": it.get("parent_topic") or it.get("parent_keyword"),
            "vol_mobile_pct": _pct(it.get("volume_mobile_pct")),
            "vol_desktop_pct": _pct(it.get("volume_desktop_pct")),
            "country": country,
            "source_seed": source_seed,
        })
    return rows


def _pct(x) -> float | None:
    """Keep mobile/desktop split as a float in [0,1] (unlike _num which floors)."""
    try:
        return round(float(x), 2) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


def normalize_site_explorer_metrics(raw: dict, *, domain: str) -> dict | None:
    items = _items(raw)
    if not items:
        return None
    m = items[0]
    return {
        "domain": domain,
        "monthly_organic_traffic": _num(m.get("org_traffic") or m.get("traffic") or m.get("organic_traffic")),
        "domain_rating": _num(m.get("domain_rating") or m.get("dr")),
        "backlinks_count": _num(m.get("backlinks")),
        "refdomains_count": _num(m.get("refdomains") or m.get("ref_domains")),
        "organic_keywords_count": _num(m.get("org_keywords") or m.get("organic_keywords") or m.get("keywords")),
        "paid_keywords_count": _num(m.get("paid_keywords")),
    }


def normalize_organic_keywords(raw: dict, *, domain: str) -> list[dict]:
    out = []
    for it in _items(raw):
        cpc = _num(it.get("cpc"))
        out.append({
            "domain": domain,
            "keyword": it.get("keyword"),
            "rank": _num(it.get("best_position") or it.get("position") or it.get("rank")),
            "volume": _num(it.get("volume") or it.get("search_volume")),
            "cpc_usd": round(cpc / 100, 2) if cpc and cpc > 0 else None,
            "url": it.get("best_position_url") or it.get("url"),
        })
    return [r for r in out if r["keyword"]]


def normalize_brand_radar(raw: dict, *, brand: str | None = None) -> list[dict]:
    """Normalize Brand Radar SOV / mentions responses."""
    out = []
    for it in _items(raw):
        out.append({
            "brand": brand or it.get("brand") or it.get("entity") or it.get("domain"),
            "platform": it.get("platform") or it.get("ai_platform") or it.get("source"),
            "impressions": _num(it.get("impressions")),
            "mentions": _num(it.get("mentions") or it.get("count")),
            "sov_pct": _float(it.get("sov") or it.get("share_of_voice")),
            "citation_url": it.get("url") or it.get("citation_url"),
        })
    return [r for r in out if r["brand"] or r["citation_url"]]


def load_json(path: str | pathlib.Path) -> dict:
    return json.loads(pathlib.Path(path).read_text())


# ---------- helpers ----------

def _items(raw: Any) -> list[dict]:
    """Extract row-like items from a variety of nesting shapes."""
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if not isinstance(raw, dict):
        return []
    for key in ("rows", "items", "results", "data", "keywords", "terms", "metrics", "entities"):
        v = raw.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict):
            return [v]
    # Some MCP responses wrap in { "result": { "items": [...] } }
    if "result" in raw:
        return _items(raw["result"])
    return []


def _num(x) -> int | None:
    try:
        return int(float(x)) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


def _float(x) -> float | None:
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


def _intent(x) -> str | None:
    if x is None:
        return None
    if isinstance(x, list):
        return "|".join(str(v) for v in x)
    return str(x)
