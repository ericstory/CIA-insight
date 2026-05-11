"""Render canonical SQLite -> single-file interactive HTML report."""
from __future__ import annotations

import pathlib
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


def render(db_path: pathlib.Path, html_path: pathlib.Path, *, synthesis: str | None = None) -> int:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(),
    )
    tmpl = env.get_template("report.html.j2")

    sections = []
    stats = {}
    with sqlite3.connect(db_path) as con:
        for title, sql, _ in SHEETS:
            try:
                df = pd.read_sql_query(sql, con)
            except Exception as e:
                df = pd.DataFrame({"error": [str(e)]})
            sections.append({
                "title": title,
                "row_count": len(df),
                "open": title in {"Golden Keywords", "Top App Keyword Gaps"},
                "note": _section_note(title),
                "table_html": _df_to_html(df),
            })
            stats[title] = len(df)

    topic = get_meta(db_path, "topic", "(unknown)") or "(unknown)"
    synthesis_html = (
        md.markdown(synthesis, extensions=MD_EXTENSIONS, output_format="html5")
        if synthesis else None
    )
    rendered = tmpl.render(
        topic=topic,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        db_path=str(db_path),
        total_cost=total_cost(db_path),
        synthesis_html=synthesis_html,
        sections=sections,
        stats=stats,
    )
    html_path.write_text(rendered, encoding="utf-8")
    return sum(stats.values())


def _df_to_html(df: pd.DataFrame) -> str:
    if df.empty:
        return '<div style="color:#57606a; font-size:12px;">— no rows —</div>'
    return df.to_html(
        classes="cia-table display compact",
        index=False,
        escape=True,
        border=0,
        na_rep="",
    )


def _section_note(title: str) -> str | None:
    notes = {
        "Golden Keywords": "Vol≥200 + KD≤35 + CPC≥$1 + commercial intent — 立即可投/可做的高价值词",
        "Top App Keyword Gaps": "App Store 上搜索量 ≥500 但只有 ≤3 个 App 在排名 — ASO 蓝海词",
        "App Reviews (Pain)": "1-3 星差评，按时间倒序 — 直接挖痛点和不满",
        "App Keywords (ASO)": "竞品 App 实际排名词 + Apple 搜索量 — 投放/ASO 直接用",
        "Competitor Web": "竞品官网 SEO 体量 — 从 Ahrefs Site Explorer",
        "AI Visibility": "竞品在 ChatGPT/Gemini/Perplexity 的引用份额 — Brand Radar",
        "Competitor Clusters": "Python KMeans 自动分群（v3）— 按 review/category/ASO词 聚类，LLM 看 top_keywords 后填 cluster_label",
        "TikTok": "share_rate = shares/plays，>1% 是爆款门槛；高 share_rate 代表有商业转化力的内容钩子（leading indicator）",
    }
    return notes.get(title)
