"""Render canonical SQLite -> single-file interactive HTML report.

v1: original flat DataTables report (backward compatible)
v2: sidebar + hash-router + drilldown cards (YouTube thumbnails, TikTok cards, etc.)
"""
from __future__ import annotations

import pathlib
import re
import sqlite3
from datetime import datetime

import pandas as pd
import markdown as md
from jinja2 import Environment, FileSystemLoader, select_autoescape

import sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from db import total_cost, get_meta
from export.excel import SHEETS

MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists", "attr_list", "toc"]
TEMPLATE_DIR = pathlib.Path(__file__).parent.parent / "templates"

_YT_RE = re.compile(r'(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})')


# ── Jinja filters ─────────────────────────────────────────────────────────────

def _num(n) -> str:
    try:
        return f"{int(n):,}" if n is not None else "—"
    except (ValueError, TypeError):
        return str(n) if n else "—"

def _hms(s) -> str:
    try:
        s = int(s)
        return f"{s // 60}:{s % 60:02d}"
    except (ValueError, TypeError):
        return ""

def _kd_bucket(k) -> str:
    try:
        k = float(k)
        return "easy" if k <= 25 else "hard" if k >= 60 else "med"
    except (ValueError, TypeError):
        return "med"

def _yt_id(url: str) -> str:
    m = _YT_RE.search(url or "")
    return m.group(1) if m else ""

def _truncate(s, n=120) -> str:
    s = (s or "").strip()
    return s[:n] + "…" if len(s) > n else s


# ── Data builders ──────────────────────────────────────────────────────────────

def _q(con, sql, params=()):
    try:
        return pd.read_sql_query(sql, con, params=params).to_dict("records")
    except Exception:
        return []


def _build_stats(con) -> dict:
    stats = {}
    tables = ["keywords_google", "social_tiktok", "social_youtube", "social_reddit",
              "appstore_serp", "appstore_keywords", "competitors_app", "competitors_web",
              "app_reviews", "ai_visibility", "competitor_clusters"]
    for t in tables:
        try:
            n = pd.read_sql_query(f"SELECT COUNT(*) AS n FROM {t}", con).iloc[0]["n"]
        except Exception:
            n = 0
        stats[t] = int(n)
    return stats


# Tokens so generic they appear in almost every keyword in a phone/app category.
# A keyword that only matches via these tokens is likely a brand KW, not an intent KW.
_GENERIC_TOKENS: frozenset[str] = frozenset({
    "phone", "number", "call", "calls", "free", "app", "apps", "best",
    "mobile", "online", "sim", "card", "service", "services", "line",
    "text", "chat", "send", "receive", "get", "use", "make",
})

# Well-known brand / platform names whose keywords pollute intent analysis
_PLATFORM_BRANDS: frozenset[str] = frozenset({
    "google", "gmail", "facebook", "apple", "samsung", "microsoft",
    "whatsapp", "skype", "zoom", "amazon", "netflix", "youtube",
    "android", "iphone", "ios", "windows",
})


def _brand_dominated(kw: str, app_name_tokens: frozenset[str]) -> bool:
    """True if every meaningful token in the keyword is a brand/platform/generic term."""
    tokens = [t for t in kw.lower().split() if len(t) >= 3]
    if not tokens:
        return True
    bad = _PLATFORM_BRANDS | _GENERIC_TOKENS | app_name_tokens
    return all(t in bad for t in tokens)


def _build_tracks(con) -> list[dict]:
    clusters = _q(con, """
        SELECT DISTINCT cc.cluster_id, cc.cluster_label, cc.top_keywords,
               t.description, t.keywords AS track_keywords, t.name_en
        FROM competitor_clusters cc
        LEFT JOIN tracks t ON t.track_id = cc.cluster_id
        WHERE cc.cluster_id >= 0
        ORDER BY cc.cluster_id
    """)

    # Load all google KWs once; distributed to clusters via token soft-match at render time
    all_google_kw = _q(con, """
        SELECT keyword, volume, kd, cpc_usd, intent, country, source_seed
        FROM keywords_google ORDER BY volume DESC NULLS LAST
    """)

    tracks = []
    for c in clusters:
        cid = c["cluster_id"]
        _kw_raw = c.get("top_keywords")
        _kw_str = "" if not _kw_raw or isinstance(_kw_raw, float) else str(_kw_raw)
        cluster_tokens = {t.strip().lower() for t in _kw_str.split(",") if t.strip()}

        # Supply: Apps
        apps = _q(con, """
            SELECT ca.app_id, ca.name, ca.developer, ca.rating, ca.review_count,
                   ca.price, ca.category, ca.url
            FROM competitor_clusters cc
            JOIN competitors_app ca ON ca.app_id = cc.competitor_id
            WHERE cc.cluster_id = ? AND cc.competitor_type = 'app'
            ORDER BY ca.review_count DESC NULLS LAST
        """, [cid])

        # Supply: Web competitors
        web_comps = _q(con, """
            SELECT w.domain, w.monthly_organic_traffic, w.organic_keywords_count
            FROM competitor_clusters cc
            JOIN competitors_web w ON w.domain = cc.domain
            WHERE cc.cluster_id = ? AND cc.competitor_type = 'web'
            ORDER BY w.monthly_organic_traffic DESC NULLS LAST
        """, [cid])

        # Supply: Low-rated reviews for apps in this cluster
        reviews = _q(con, """
            SELECT ar.body, ar.rating, ar.app_id, ar.posted_at
            FROM competitor_clusters cc
            JOIN app_reviews ar ON ar.app_id = cc.competitor_id
            WHERE cc.cluster_id = ? AND cc.competitor_type = 'app' AND ar.rating <= 3
            ORDER BY ar.posted_at DESC LIMIT 20
        """, [cid])

        # Demand: ASO keywords — apply track-token filter IN SQL before LIMIT so that
        # intent keywords (burner phone SV=3K) aren't crowded out by generic App Store
        # megakeywords (google 37M, gmail 23M) from large apps that co-exist in the cluster.
        # GROUP BY keyword so each term appears once with the MAX search_volume across apps.
        specific_tokens = cluster_tokens - _GENERIC_TOKENS - _PLATFORM_BRANDS
        _filter_tokens = specific_tokens if specific_tokens else cluster_tokens
        if _filter_tokens:
            like_clauses = " OR ".join("ak.keyword LIKE ?" for _ in _filter_tokens)
            like_params: list = [f"%{tok}%" for tok in _filter_tokens] + [cid]
            keywords_raw = _q(con, f"""
                SELECT ak.keyword,
                       MAX(ak.search_volume) AS search_volume,
                       MIN(ak.rank)          AS rank,
                       MIN(ak.app_name)      AS app_name,
                       MIN(ak.app_id)        AS app_id
                FROM competitor_clusters cc
                JOIN appstore_keywords ak ON ak.app_id = cc.competitor_id
                WHERE ({like_clauses})
                  AND cc.cluster_id = ? AND cc.competitor_type = 'app'
                GROUP BY ak.keyword
                ORDER BY MAX(ak.search_volume) DESC NULLS LAST LIMIT 500
            """, like_params)
        else:
            keywords_raw = _q(con, """
                SELECT ak.keyword,
                       MAX(ak.search_volume) AS search_volume,
                       MIN(ak.rank)          AS rank,
                       MIN(ak.app_name)      AS app_name,
                       MIN(ak.app_id)        AS app_id
                FROM competitor_clusters cc
                JOIN appstore_keywords ak ON ak.app_id = cc.competitor_id
                WHERE cc.cluster_id = ? AND cc.competitor_type = 'app'
                GROUP BY ak.keyword
                ORDER BY MAX(ak.search_volume) DESC NULLS LAST LIMIT 500
            """, [cid])

        # Python-side filter is now a lightweight dedup pass (SQL LIKE already handles relevance)
        keywords = keywords_raw[:30]

        # Demand: Google KWs matched by cluster token overlap
        if cluster_tokens:
            google_kw = [k for k in all_google_kw
                         if any(tok in k["keyword"].lower() for tok in cluster_tokens)][:30]
        else:
            google_kw = []

        # Social: TikTok via social_cluster_map
        tiktok = _q(con, """
            SELECT tt.id, tt.query, tt.author, tt.plays, tt.likes, tt.shares, tt.text, tt.url,
                   CASE WHEN tt.plays > 0
                        THEN ROUND(CAST(tt.shares AS REAL) / tt.plays, 5) ELSE 0 END AS share_rate
            FROM social_tiktok tt
            JOIN social_cluster_map scm ON scm.source = 'tiktok' AND scm.item_id = tt.id
            WHERE scm.cluster_id = ?
            ORDER BY tt.plays DESC LIMIT 20
        """, [cid])
        for r in tiktok:
            r["text_short"] = _truncate(r.get("text") or "", 120)
            r["is_hot"] = (r.get("share_rate") or 0) >= 0.01

        # Social: YouTube via social_cluster_map
        youtube = _q(con, """
            SELECT yt.id, yt.query, yt.title, yt.channel, yt.views, yt.likes,
                   yt.comments, yt.url, yt.duration_seconds
            FROM social_youtube yt
            JOIN social_cluster_map scm ON scm.source = 'youtube' AND scm.item_id = yt.id
            WHERE scm.cluster_id = ?
            ORDER BY yt.views DESC LIMIT 20
        """, [cid])
        for r in youtube:
            r["video_id"] = _yt_id(r.get("url") or "")

        # Social: Reddit via social_cluster_map
        reddit_track = _q(con, """
            SELECT r.id, r.subreddit, r.query, r.title, r.body, r.score,
                   r.num_comments, r.author, r.url
            FROM social_reddit r
            JOIN social_cluster_map scm ON scm.source = 'reddit' AND scm.item_id = r.id
            WHERE scm.cluster_id = ?
            ORDER BY r.score DESC LIMIT 20
        """, [cid])

        avg_rating = (sum(a.get("rating") or 0 for a in apps) / len(apps)) if apps else 0
        total_reviews = sum(a.get("review_count") or 0 for a in apps)

        # Track-level description + LLM-defined keywords (from tracks table)
        _tk_raw = c.get("track_keywords")
        _tk_str = "" if not _tk_raw or isinstance(_tk_raw, float) else str(_tk_raw)
        track_keywords = [k.strip() for k in _tk_str.split(",") if k.strip()][:10]

        # Build SV lookup from UNFILTERED raw ASO keywords (max coverage for chip matching)
        kw_sv_map: dict[str, dict] = {}
        for k in keywords_raw:
            kw_sv_map[k["keyword"].lower()] = {
                "sv": k.get("search_volume") or 0, "channel": "app"}
        for k in all_google_kw:
            kw_lower = k["keyword"].lower()
            if kw_lower not in kw_sv_map:
                kw_sv_map[kw_lower] = {
                    "sv": k.get("volume") or 0, "channel": "web"}

        # ── Overview demand chips ─────────────────────────────────────────────
        # Source: the already-filtered `keywords` list (specific_tokens filtered, 30 items).
        # Sort: exact cluster keyword matches first (most "intent-pure"), then by SV desc.
        # Only strip _PLATFORM_BRANDS (google/facebook etc.) — do NOT use app_name_tokens
        # here because for phone/number categories the app names ARE the intent keywords.
        _platform_brand_tokens = _PLATFORM_BRANDS  # e.g. google, facebook, apple

        def _starts_with_platform_brand(kw: str) -> bool:
            first = kw.lower().split()[0] if kw.strip() else ""
            return first in _platform_brand_tokens

        def _overview_sort_key(k):
            kw_lower = k.get("keyword", "").lower()
            # Exact cluster keyword → tier 0 (most specific intent)
            is_exact = kw_lower in cluster_tokens
            # Starts with platform brand (google, facebook…) → tier 2
            is_platform = _starts_with_platform_brand(kw_lower)
            sv = k.get("search_volume") or 0
            # Platform brands (google voice, facebook…) → lowest tier even if exact cluster match
            tier = 2 if is_platform else (0 if is_exact else 1)
            return (tier, -sv)

        demand_overview: list[dict] = []
        seen_kws: set[str] = set()
        for k in sorted(keywords, key=_overview_sort_key):
            kw = k.get("keyword", "")
            if kw.lower() in seen_kws:
                continue
            demand_overview.append({
                "keyword": kw,
                "sv": k.get("search_volume") or 0,
                "channel": "app",
            })
            seen_kws.add(kw.lower())
            if len(demand_overview) >= 5:
                break

        # Supplement with Google KWs if ASO is sparse
        for k in sorted(all_google_kw, key=lambda x: x.get("volume") or 0, reverse=True):
            if len(demand_overview) >= 5:
                break
            kw = k.get("keyword", "")
            if kw.lower() in seen_kws:
                continue
            if not cluster_tokens or any(tok in kw.lower() for tok in cluster_tokens):
                if not _starts_with_platform_brand(kw):
                    demand_overview.append({
                        "keyword": kw,
                        "sv": k.get("volume") or 0,
                        "channel": "web",
                    })
                    seen_kws.add(kw.lower())

        # Fallback: LLM track keywords with fuzzy SV lookup (original logic)
        if not demand_overview:
            def _best_sv(kw: str) -> dict:
                """Exact → forward token match → backward token match (min len 3)."""
                kw_l = kw.lower()
                if kw_l in kw_sv_map:
                    return kw_sv_map[kw_l]
                tokens = [t for t in kw_l.split() if len(t) >= 3]
                if not tokens:
                    return {"sv": 0, "channel": None}
                best: dict = {"sv": 0, "channel": None}
                for db_kw, info in kw_sv_map.items():
                    sv = info.get("sv") or 0
                    if sv <= (best.get("sv") or 0):
                        continue
                    db_tokens = [t for t in db_kw.split() if len(t) >= 3]
                    if len(tokens) >= 2 and all(t in db_kw for t in tokens):
                        best = info
                    elif len(db_tokens) >= 2 and all(t in kw_l for t in db_tokens):
                        best = info
                return best

            track_keywords_for_fallback = [k.strip() for k in _tk_str.split(",") if k.strip()][:10]
            for kw in track_keywords_for_fallback:
                info = _best_sv(kw)
                demand_overview.append({"keyword": kw, "sv": info.get("sv") or 0, "channel": info.get("channel")})

        track_keywords_enriched = demand_overview

        aso_total_sv = sum(k.get("search_volume") or 0 for k in keywords)
        google_total_sv = sum(k.get("volume") or 0 for k in google_kw)

        tracks.append({
            "id": cid,
            "label": c.get("cluster_label") or f"Cluster {cid}",
            "description": (c.get("description") or "") if not isinstance(c.get("description"), float) else "",
            "name_en": (c.get("name_en") or "") if not isinstance(c.get("name_en"), float) else "",
            "track_keywords": track_keywords,
            "track_keywords_enriched": track_keywords_enriched,
            "aso_total_sv": aso_total_sv,
            "google_total_sv": google_total_sv,
            "top_keywords": _kw_str.split(",") if _kw_str else [],
            # Supply
            "apps": apps,
            "web_comps": web_comps,
            "reviews": reviews,
            # Demand
            "keywords": keywords,
            "google_kw": google_kw,
            # Social
            "tiktok": tiktok,
            "youtube": youtube,
            "reddit": reddit_track,
            # Summary stats for comparison matrix
            "n_apps": len(apps),
            "n_web": len(web_comps),
            "n_tiktok": len(tiktok),
            "n_youtube": len(youtube),
            "n_reddit": len(reddit_track),
            "n_aso_kw": len(keywords),
            "n_google_kw": len(google_kw),
            "avg_rating": round(avg_rating, 1),
            "total_reviews": total_reviews,
        })
    return tracks


def _build_tiktok(con) -> list[dict]:
    rows = _q(con, """
        SELECT id, query, author, plays, likes, shares, comments, text, url, posted_at,
               CASE WHEN plays > 0 THEN ROUND(CAST(shares AS REAL)/plays, 5) ELSE 0 END AS share_rate
        FROM social_tiktok
        WHERE plays IS NOT NULL
        ORDER BY share_rate DESC, plays DESC
    """)
    for r in rows:
        r["text_short"] = _truncate(r.get("text") or "", 120)
        r["is_hot"] = (r.get("share_rate") or 0) >= 0.01
    return rows


def _build_youtube(con) -> list[dict]:
    rows = _q(con, """
        SELECT id, query, title, channel, views, likes, comments,
               description, url, posted_at, duration_seconds
        FROM social_youtube
        WHERE views IS NOT NULL
        ORDER BY views DESC
    """)
    for r in rows:
        r["video_id"] = _yt_id(r.get("url") or "")
        r["desc_short"] = _truncate(r.get("description") or "", 200)
    return rows


def _build_keywords(con) -> dict:
    all_kw = _q(con, """
        SELECT keyword, volume, kd, cpc_usd, intent, traffic_potential, country, source_seed
        FROM keywords_google ORDER BY volume DESC NULLS LAST
    """)
    golden = [k for k in all_kw if
              (k.get("volume") or 0) >= 200 and
              (k.get("kd") is None or (k.get("kd") or 999) <= 35) and
              (k.get("cpc_usd") or 0) >= 1]

    # Load track keyword phrases for ASO matching (from competitor_clusters)
    _track_phrases: list[tuple[int, str, list[str]]] = []
    try:
        clusters = _q(con, """
            SELECT DISTINCT cluster_id, cluster_label, top_keywords
            FROM competitor_clusters
            WHERE cluster_id >= 0 AND top_keywords IS NOT NULL AND top_keywords != ''
        """)
        for c in clusters:
            phrases = [p.strip().lower() for p in (c.get("top_keywords") or "").split(",") if p.strip()]
            if phrases:
                _track_phrases.append((
                    int(c["cluster_id"]),
                    str(c.get("cluster_label") or f"Track {c['cluster_id']}"),
                    phrases,
                ))
    except Exception:
        pass

    def _assign_track(kw: str) -> tuple[int | None, str]:
        kw_lower = kw.lower()
        for tid, tlabel, phrases in _track_phrases:
            if any(p in kw_lower for p in phrases):
                return tid, tlabel
        return None, ""

    # ASO: only show keywords that match at least one track; add track_label badge
    aso_raw = _q(con, """
        SELECT ak.keyword, ak.search_volume, ak.rank, ak.app_name, ak.app_id
        FROM appstore_keywords ak
        ORDER BY ak.search_volume DESC NULLS LAST
        LIMIT 2000
    """)
    aso: list[dict] = []
    seen_aso: set[str] = set()
    for k in aso_raw:
        kw = k.get("keyword") or ""
        if kw.lower() in seen_aso:
            continue
        seen_aso.add(kw.lower())
        tid, tlabel = _assign_track(kw)
        if tid is not None:
            k["track_id"] = tid
            k["track_label"] = tlabel
            aso.append(k)
        if len(aso) >= 300:
            break

    gaps = _q(con, """
        SELECT keyword, search_volume, COUNT(DISTINCT app_id) AS n_apps
        FROM appstore_keywords WHERE search_volume >= 200
        GROUP BY keyword HAVING n_apps <= 3
        ORDER BY search_volume DESC LIMIT 50
    """)
    # Add track labels to gaps too
    for k in gaps:
        _, tlabel = _assign_track(k.get("keyword") or "")
        k["track_label"] = tlabel

    return {"all": all_kw, "golden": golden, "aso": aso, "gaps": gaps}


# ── Social translation ────────────────────────────────────────────────────────

def _translate_batch(texts: list[str]) -> list[str]:
    """Translate a batch of texts to Chinese (Simplified) via Haiku."""
    if not texts:
        return []
    try:
        import anthropic
        import json as _json
        sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
        import config
        api_key = config.get("ANTHROPIC_AUTH_TOKEN") or config.get("ANTHROPIC_API_KEY")
        base_url = config.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        if not api_key:
            return [""] * len(texts)
        client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
        prompt = (
            "将下列每条内容翻译成简体中文摘要（每条不超过50字，保留品牌名/数字/标签）。"
            "仅返回 JSON 字符串数组，与输入等长，无其他文字。\n\n" + numbered
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Try full JSON array first
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if m:
            try:
                result = _json.loads(m.group(0))
                if isinstance(result, list) and len(result) == len(texts):
                    return [str(r) for r in result]
            except Exception:
                pass
        # Fallback: extract quoted strings line by line
        lines = [ln.strip().strip('",') for ln in raw.splitlines()
                 if ln.strip() and ln.strip() not in ('[', ']')]
        lines = [ln for ln in lines if ln]
        if len(lines) == len(texts):
            return lines
    except Exception as e:
        print(f"  [translate] {e}")
    return [""] * len(texts)


def _translate_social(
    tracks: list[dict],
    global_tiktok: list[dict] | None = None,
    global_youtube: list[dict] | None = None,
    global_reddit: list[dict] | None = None,
) -> None:
    """Translate TikTok captions, YouTube titles, Reddit titles in-place.

    Covers both per-track items and the global listing pages in one shared LLM call
    so duplicates are translated only once.
    """
    def _needs_zh(text: str) -> bool:
        if not text:
            return False
        cjk = sum(1 for c in text if '一' <= c <= '鿿')
        return cjk / max(len(text), 1) < 0.3

    # Collect unique texts from per-track AND global lists
    text_set: set[str] = set()
    for t in tracks:
        for v in t.get("tiktok", []):
            s = v.get("text_short") or ""
            if _needs_zh(s): text_set.add(s)
        for v in t.get("youtube", []):
            s = v.get("title") or ""
            if _needs_zh(s): text_set.add(s)
        for r in t.get("reddit", []):
            s = r.get("title") or ""
            if _needs_zh(s): text_set.add(s)
    for v in (global_tiktok or []):
        s = v.get("text_short") or ""
        if _needs_zh(s): text_set.add(s)
    for v in (global_youtube or []):
        s = v.get("title") or ""
        if _needs_zh(s): text_set.add(s)
    for r in (global_reddit or []):
        s = r.get("title") or ""
        if _needs_zh(s): text_set.add(s)

    if not text_set:
        return

    to_translate = sorted(text_set)
    print(f"  Translating {len(to_translate)} social items to Chinese (Haiku)…")

    batch_size = 25
    trans_map: dict[str, str] = {}
    for i in range(0, len(to_translate), batch_size):
        batch = to_translate[i : i + batch_size]
        results = _translate_batch(batch)
        trans_map.update(zip(batch, results))

    # Apply to per-track items
    for t in tracks:
        for v in t.get("tiktok", []):
            v["text_zh"] = trans_map.get(v.get("text_short") or "", "")
        for v in t.get("youtube", []):
            v["title_zh"] = trans_map.get(v.get("title") or "", "")
        for r in t.get("reddit", []):
            r["title_zh"] = trans_map.get(r.get("title") or "", "")
    # Apply to global list pages
    for v in (global_tiktok or []):
        v["text_zh"] = trans_map.get(v.get("text_short") or "", "")
    for v in (global_youtube or []):
        v["title_zh"] = trans_map.get(v.get("title") or "", "")
    for r in (global_reddit or []):
        r["title_zh"] = trans_map.get(r.get("title") or "", "")


# ── Briefs loader ──────────────────────────────────────────────────────────────

def _load_briefs(db_path: pathlib.Path) -> list[dict]:
    """Load briefs from briefs/briefs.json and attach md_content from each .md file."""
    briefs_json = db_path.parent / "briefs" / "briefs.json"
    if not briefs_json.exists():
        return []
    import json as _json
    import re as _re
    data = _json.loads(briefs_json.read_text(encoding="utf-8"))
    tracks = data.get("tracks", [])
    briefs_dir = briefs_json.parent
    for i, t in enumerate(tracks, 1):
        slug = f"{i:02d}-" + _re.sub(r"[^a-z0-9]+", "-", (t.get("name_en") or "").lower()).strip("-")
        md_path = briefs_dir / f"{slug}.md"
        t["md_content"] = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        t["md_filename"] = md_path.name
    return tracks


# ── v1 render (backward compat) ────────────────────────────────────────────────

def render(db_path: pathlib.Path, html_path: pathlib.Path, *, synthesis: str | None = None) -> int:
    return render_v2(db_path, html_path, synthesis=synthesis)


# ── v2 render ─────────────────────────────────────────────────────────────────

def render_v2(db_path: pathlib.Path, html_path: pathlib.Path, *, synthesis: str | None = None) -> int:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["num"] = _num
    env.filters["hms"] = _hms
    env.filters["kd_bucket"] = _kd_bucket
    env.filters["yt_id"] = _yt_id
    env.filters["truncate80"] = lambda s: _truncate(s, 80)

    with sqlite3.connect(db_path) as con:
        stats = _build_stats(con)
        tracks = _build_tracks(con)
        tiktok = _build_tiktok(con)
        youtube = _build_youtube(con)
        reddit = _q(con, "SELECT * FROM social_reddit ORDER BY score DESC NULLS LAST LIMIT 100")
        _translate_social(tracks, tiktok, youtube, reddit)  # one shared LLM call for all
        keywords = _build_keywords(con)
        comp_web = _q(con, """
            SELECT domain, monthly_organic_traffic, organic_keywords_count,
                   paid_keywords_count, fetched_at
            FROM competitors_web ORDER BY monthly_organic_traffic DESC NULLS LAST
        """)
        comp_apps = _q(con, """
            SELECT app_id, name, developer, rating, review_count, price, category, url
            FROM competitors_app ORDER BY review_count DESC NULLS LAST LIMIT 100
        """)
        reviews = _q(con, """
            SELECT body, rating, app_id, posted_at
            FROM app_reviews WHERE rating <= 3 AND length(body) > 30
            ORDER BY posted_at DESC LIMIT 200
        """)
        ai_vis = _q(con, "SELECT * FROM ai_visibility ORDER BY sov_pct DESC NULLS LAST")
        fetch_log = _q(con, "SELECT * FROM fetch_log ORDER BY fetched_at DESC LIMIT 100")

    topic = get_meta(db_path, "topic", "(unknown)") or "(unknown)"
    synthesis_html = (
        md.markdown(synthesis, extensions=MD_EXTENSIONS, output_format="html5")
        if synthesis else None
    )

    briefs = _load_briefs(db_path)

    ctx = dict(
        topic=topic,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        db_path=str(db_path),
        total_cost=total_cost(db_path),
        synthesis_html=synthesis_html,
        stats=stats,
        tracks=tracks,
        tiktok=tiktok,
        youtube=youtube,
        reddit=reddit,
        kw_all=keywords["all"],
        kw_golden=keywords["golden"],
        kw_aso=keywords["aso"],
        kw_gaps=keywords["gaps"],
        comp_web=comp_web,
        comp_apps=comp_apps,
        reviews=reviews,
        ai_vis=ai_vis,
        fetch_log=fetch_log,
        briefs=briefs,
    )

    tmpl = env.get_template("report_v2.html.j2")
    html_path.write_text(tmpl.render(**ctx), encoding="utf-8")
    return sum(stats.values())


# ── section note (v1 compat) ───────────────────────────────────────────────────

def _df_to_html(df: pd.DataFrame) -> str:
    if df.empty:
        return '<div style="color:#57606a; font-size:12px;">— no rows —</div>'
    return df.to_html(classes="cia-table display compact", index=False,
                      escape=True, border=0, na_rep="")
