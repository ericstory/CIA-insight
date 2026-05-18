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

        # Demand: ASO keywords — fetch wider pool, then filter to track-relevant terms only
        # (apps like Google Voice rank for "google"/"gmail" with huge SV — exclude that noise)
        keywords_raw = _q(con, """
            SELECT ak.keyword, ak.search_volume, ak.rank, ak.app_name, ak.app_id
            FROM competitor_clusters cc
            JOIN appstore_keywords ak ON ak.app_id = cc.competitor_id
            WHERE cc.cluster_id = ? AND cc.competitor_type = 'app'
            ORDER BY ak.search_volume DESC NULLS LAST LIMIT 500
        """, [cid])
        if cluster_tokens:
            keywords = [k for k in keywords_raw
                        if any(tok in k["keyword"].lower() for tok in cluster_tokens)][:30]
        else:
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

        def _best_sv(kw: str) -> dict:
            """Exact → forward token match → backward token match (min len 3)."""
            kw_l = kw.lower()
            if kw_l in kw_sv_map:
                return kw_sv_map[kw_l]
            # Only use tokens ≥ 3 chars to avoid "on"/"e"/"a" false matches
            tokens = [t for t in kw_l.split() if len(t) >= 3]
            if not tokens:
                return {"sv": 0, "channel": None}
            best: dict = {"sv": 0, "channel": None}
            for db_kw, info in kw_sv_map.items():
                sv = info.get("sv") or 0
                if sv <= (best.get("sv") or 0):
                    continue
                db_tokens = [t for t in db_kw.split() if len(t) >= 3]
                # Forward: all our tokens appear in the DB keyword
                # e.g. ["virtual","number"] both in "virtual phone number" → match
                if len(tokens) >= 2 and all(t in db_kw for t in tokens):
                    best = info
                # Backward: all DB keyword tokens appear in our keyword
                # e.g. ["second","phone"] both in "second phone number" → match
                elif len(db_tokens) >= 2 and all(t in kw_l for t in db_tokens):
                    best = info
            return best

        # Enrich track keywords with SV + channel (fuzzy match)
        track_keywords_enriched = []
        for kw in track_keywords:
            info = _best_sv(kw)
            track_keywords_enriched.append({
                "keyword": kw,
                "sv": info.get("sv") or 0,
                "channel": info.get("channel"),
            })

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
    aso = _q(con, """
        SELECT ak.keyword, ak.search_volume, ak.rank, ak.app_name, ak.app_id
        FROM appstore_keywords ak
        ORDER BY ak.search_volume DESC NULLS LAST
        LIMIT 200
    """)
    gaps = _q(con, """
        SELECT keyword, search_volume, COUNT(DISTINCT app_id) AS n_apps
        FROM appstore_keywords WHERE search_volume >= 200
        GROUP BY keyword HAVING n_apps <= 3
        ORDER BY search_volume DESC LIMIT 50
    """)
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


def _translate_social(tracks: list[dict]) -> None:
    """Translate TikTok captions, YouTube titles, Reddit titles in-place."""
    def _needs_zh(text: str) -> bool:
        if not text:
            return False
        cjk = sum(1 for c in text if '一' <= c <= '鿿')
        return cjk / max(len(text), 1) < 0.3

    # Collect unique texts
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

    for t in tracks:
        for v in t.get("tiktok", []):
            v["text_zh"] = trans_map.get(v.get("text_short") or "", "")
        for v in t.get("youtube", []):
            v["title_zh"] = trans_map.get(v.get("title") or "", "")
        for r in t.get("reddit", []):
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
        _translate_social(tracks)   # adds text_zh / title_zh in-place
        tiktok = _build_tiktok(con)
        youtube = _build_youtube(con)
        reddit = _q(con, "SELECT * FROM social_reddit ORDER BY score DESC NULLS LAST LIMIT 100")
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
