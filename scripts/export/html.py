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
        SELECT DISTINCT cc.cluster_id, cc.cluster_label, cc.top_keywords
        FROM competitor_clusters cc ORDER BY cc.cluster_id
    """)
    tracks = []
    for c in clusters:
        cid = c["cluster_id"]
        apps = _q(con, """
            SELECT ca.app_id, ca.name, ca.developer, ca.rating, ca.review_count,
                   ca.price, ca.category, ca.url
            FROM competitor_clusters cc
            JOIN competitors_app ca ON ca.app_id = cc.app_id
            WHERE cc.cluster_id = ?
            ORDER BY ca.review_count DESC NULLS LAST
        """, [cid])
        keywords = _q(con, """
            SELECT ak.keyword, ak.search_volume, ak.rank, ak.app_id, ak.app_name
            FROM competitor_clusters cc
            JOIN appstore_keywords ak ON ak.app_id = cc.app_id
            WHERE cc.cluster_id = ?
            ORDER BY ak.search_volume DESC NULLS LAST
            LIMIT 30
        """, [cid])
        reviews = _q(con, """
            SELECT ar.body, ar.rating, ar.app_id, ar.posted_at
            FROM competitor_clusters cc
            JOIN app_reviews ar ON ar.app_id = cc.app_id
            WHERE cc.cluster_id = ? AND ar.rating <= 3
            ORDER BY ar.posted_at DESC
            LIMIT 20
        """, [cid])
        tracks.append({
            "id": cid,
            "label": c.get("cluster_label") or f"Cluster {cid}",
            "top_keywords": str(c.get("top_keywords") or "").split(","),
            "apps": apps,
            "keywords": keywords,
            "reviews": reviews,
            "n_apps": len(apps),
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
