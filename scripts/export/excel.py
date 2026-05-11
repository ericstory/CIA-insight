"""Export SQLite -> multi-sheet Excel for ad/ASO teams to consume directly."""
from __future__ import annotations

import pathlib
import sqlite3

import pandas as pd

# Sheets in the order they should appear in the workbook.
# Each entry: (sheet_name, sql, optional_human_columns_rename)
SHEETS = [
    ("Google Keywords", """
        SELECT keyword, volume, kd, cpc_usd, intent, traffic_potential,
               parent_topic, country, source_seed, fetched_at
        FROM keywords_google
        ORDER BY volume DESC NULLS LAST
    """, None),
    ("Golden Keywords", """
        SELECT keyword, volume, kd, cpc_usd, intent, traffic_potential, country
        FROM keywords_google
        WHERE volume >= 200 AND kd <= 35 AND cpc_usd >= 1
              AND (intent LIKE '%commercial%' OR intent IS NULL)
        ORDER BY volume DESC
    """, None),
    ("App Store SERP", """
        SELECT keyword, rank, app_name, developer, rating, review_count, app_id, country
        FROM appstore_serp
        ORDER BY keyword, rank
    """, None),
    ("App Keywords (ASO)", """
        SELECT app_name, app_id, keyword, rank, search_volume, country
        FROM appstore_keywords
        ORDER BY search_volume DESC NULLS LAST
    """, None),
    ("Top App Keyword Gaps", """
        SELECT k.keyword, k.search_volume,
               COUNT(DISTINCT k.app_id) AS apps_ranking,
               GROUP_CONCAT(DISTINCT k.app_name) AS competing_apps
        FROM appstore_keywords k
        WHERE k.search_volume >= 500
        GROUP BY k.keyword
        HAVING apps_ranking <= 3
        ORDER BY k.search_volume DESC
        LIMIT 200
    """, None),
    ("Competitor Apps", """
        SELECT name, developer, rating, review_count,
               est_downloads_low, est_downloads_high, price, category, app_id, url
        FROM competitors_app
        ORDER BY review_count DESC NULLS LAST
    """, None),
    ("App Reviews (Pain)", """
        SELECT r.app_id,
               (SELECT name FROM competitors_app WHERE app_id = r.app_id) AS app_name,
               r.rating, r.title, r.body, r.author, r.posted_at
        FROM app_reviews r
        WHERE r.rating <= 3
        ORDER BY r.posted_at DESC
    """, None),
    ("Competitor Web", """
        SELECT domain, monthly_organic_traffic, domain_rating,
               organic_keywords_count, paid_keywords_count,
               backlinks_count, refdomains_count, fetched_at
        FROM competitors_web
        ORDER BY monthly_organic_traffic DESC NULLS LAST
    """, None),
    ("Competitor Organic KW", """
        SELECT domain, keyword, rank, volume, cpc_usd, url
        FROM competitor_organic_kw
        ORDER BY volume DESC NULLS LAST
    """, None),
    ("AI Visibility", """
        SELECT brand, platform, impressions, mentions, sov_pct, citation_url, fetched_at
        FROM ai_visibility
        ORDER BY sov_pct DESC NULLS LAST
    """, None),
    ("TikTok", """
        SELECT plays, likes, shares, comments,
               CASE WHEN plays > 0 THEN ROUND(CAST(shares AS REAL) / plays, 5) ELSE 0 END AS share_rate,
               author, text, url, posted_at, query
        FROM social_tiktok
        WHERE plays > 0
        ORDER BY share_rate DESC NULLS LAST
    """, None),
    ("Reddit", """
        SELECT score, num_comments, subreddit, title, body, author, url, posted_at, query
        FROM social_reddit
        ORDER BY score DESC NULLS LAST
    """, None),
    ("YouTube", """
        SELECT views, likes, comments, channel, title, description,
               duration_seconds, url, posted_at, query
        FROM social_youtube
        ORDER BY views DESC NULLS LAST
    """, None),
    ("Competitor Clusters", """
        SELECT cc.cluster_id, cc.cluster_label,
               ca.name, ca.developer, ca.rating, ca.review_count,
               ca.category, ca.price, cc.top_keywords, ca.app_id
        FROM competitor_clusters cc
        JOIN competitors_app ca ON ca.app_id = cc.app_id
        ORDER BY cc.cluster_id, ca.review_count DESC NULLS LAST
    """, None),
    ("Seeds", "SELECT * FROM seeds ORDER BY side, dimension, keyword", None),
    ("Fetch Log", """
        SELECT source, op, rows, ROUND(cost_usd, 5) AS cost_usd,
               error, started_at, duration_ms
        FROM fetch_log
        ORDER BY started_at DESC
    """, None),
]


def export(db_path: pathlib.Path, xlsx_path: pathlib.Path) -> dict:
    """Write all sheets. Returns {sheet: row_count}."""
    counts: dict[str, int] = {}
    with sqlite3.connect(db_path) as con:
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            for sheet, sql, _ in SHEETS:
                try:
                    df = pd.read_sql_query(sql, con)
                except Exception as e:
                    df = pd.DataFrame({"error": [str(e)]})
                df.to_excel(writer, sheet_name=sheet[:31], index=False)
                counts[sheet] = len(df)
                # Auto-fit columns (rough: first 200 chars)
                ws = writer.sheets[sheet[:31]]
                for col_idx, col in enumerate(df.columns, start=1):
                    max_len = max(
                        [len(str(col))]
                        + [min(len(str(v)), 80) for v in df[col].head(200).tolist()]
                    )
                    ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = min(max_len + 2, 60)
    return counts
