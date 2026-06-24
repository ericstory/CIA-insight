"""Unified competitor clustering — Web domains + App Store apps.

Feature matrix:
  - Keyword TF-IDF (ASO keywords for apps, organic-kw for web domains)
  - Numeric signals (review_count/traffic normalized, rating)
  - Type indicator (app vs web)

Output: competitor_clusters rows with competitor_id, competitor_type, domain, cluster_id, top_keywords
Also writes social_cluster_map by scoring TikTok/YouTube against cluster top_keywords.
"""
from __future__ import annotations

import sys
import pathlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import db

try:
    from sklearn.cluster import KMeans
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.preprocessing import normalize
    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False


@dataclass
class Competitor:
    competitor_id: str      # app_id or domain
    competitor_type: str    # 'app' | 'web'
    domain: str | None
    name: str
    cluster_id: int
    signal: float           # review_count or organic_traffic (normalized)
    top_keywords: str


# ── Noise filter (apps only) ──────────────────────────────────────────────────

_NOISE_BUNDLES = {"com.openai.chat", "com.google.LLM", "com.anthropic.claude",
                  "ai.perplexity.app.ios", "com.character.ai"}
_NOISE_NAMES = {"chatgpt", "replika", "character ai", "perplexity", "genie", "poe ",
                "kindroid", "synclub", "bala", "math solver"}

def _is_noise_app(row: pd.Series) -> bool:
    bundle = str(row.get("bundle_id", "") or "").lower()
    name = str(row.get("name", "") or "").lower()
    return bundle in _NOISE_BUNDLES or any(f in name for f in _NOISE_NAMES)


# ── Auto-k via elbow ──────────────────────────────────────────────────────────

def _auto_k(X: np.ndarray, max_k: int = 8) -> int:
    if len(X) <= 2:
        return 1
    max_k = min(max_k, len(X) - 1, 8)
    inertias = []
    for k in range(1, max_k + 1):
        km = KMeans(n_clusters=k, random_state=42, n_init=10)
        km.fit(X)
        inertias.append(km.inertia_)
    best_k = 1
    for i in range(1, len(inertias)):
        if inertias[i - 1] > 0 and (inertias[i - 1] - inertias[i]) / inertias[i - 1] < 0.15:
            break
        best_k = i + 1
    return max(2, min(best_k, max_k))


# ── Data loaders ──────────────────────────────────────────────────────────────

def _load_apps(db_path: pathlib.Path) -> pd.DataFrame:
    df = db.query_df(db_path, """
        SELECT a.app_id, a.name, a.bundle_id, a.rating, a.review_count, a.category,
               GROUP_CONCAT(k.keyword, ' ') AS kw_text
        FROM competitors_app a
        LEFT JOIN appstore_keywords k ON k.app_id = a.app_id
        WHERE a.review_count IS NOT NULL AND a.review_count > 0
        GROUP BY a.app_id
    """)
    df = df[~df.apply(_is_noise_app, axis=1)].copy()
    df["competitor_type"] = "app"
    df["competitor_id"] = df["app_id"].astype(str)
    df["domain"] = None
    df["signal"] = np.log1p(df["review_count"].fillna(0))
    df["kw_text"] = df["kw_text"].fillna("")
    return df


def _load_web(db_path: pathlib.Path) -> pd.DataFrame:
    df = db.query_df(db_path, """
        SELECT w.domain,
               w.monthly_organic_traffic,
               GROUP_CONCAT(k.keyword, ' ') AS kw_text
        FROM competitors_web w
        LEFT JOIN competitor_organic_kw k ON k.domain = w.domain
        WHERE w.monthly_organic_traffic IS NOT NULL
           OR EXISTS (SELECT 1 FROM competitor_organic_kw ck WHERE ck.domain = w.domain)
        GROUP BY w.domain
    """)
    if df.empty:
        return df
    df["competitor_type"] = "web"
    df["competitor_id"] = df["domain"]
    df["app_id"] = None
    df["name"] = df["domain"]
    df["bundle_id"] = None
    df["rating"] = 0.0
    df["review_count"] = 0
    df["category"] = "web"
    df["signal"] = np.log1p(df["monthly_organic_traffic"].fillna(0))
    df["kw_text"] = df["kw_text"].fillna("")
    return df


# ── Main clustering function ───────────────────────────────────────────────────

def cluster_competitors(
    db_path: pathlib.Path,
    *,
    n_clusters: int | None = None,
    max_k: int = 8,
) -> list[Competitor]:
    if not _HAS_SKLEARN:
        raise RuntimeError("pip install scikit-learn")

    apps_df = _load_apps(db_path)
    web_df = _load_web(db_path)

    # Build unified dataframe
    common_cols = ["competitor_id", "competitor_type", "domain", "name",
                   "signal", "kw_text", "rating", "review_count", "category"]
    frames = []
    if not apps_df.empty:
        frames.append(apps_df[common_cols])
    if not web_df.empty:
        frames.append(web_df[common_cols])

    if not frames:
        print("No competitors to cluster.")
        return []

    all_df = pd.concat(frames, ignore_index=True)
    if len(all_df) < 2:
        print("Too few competitors to cluster.")
        return []

    print(f"Clustering {len(all_df)} competitors "
          f"({len(apps_df) if not apps_df.empty else 0} apps, "
          f"{len(web_df) if not web_df.empty else 0} web)...")

    # ── Feature matrix ────────────────────────────────────────────────────────
    # 1. Signal (log reviews or log traffic)
    sig = all_df["signal"].fillna(0).values.astype(float)
    sig_max = sig.max() or 1.0
    sig = sig / sig_max  # (n,)

    # 2. Type indicator (app=1, web=0)
    type_ind = (all_df["competitor_type"] == "app").astype(float).values  # (n,)

    # 3. TF-IDF on keyword text
    kw_texts = all_df["kw_text"].tolist()
    has_kw = any(t.strip() for t in kw_texts)
    if has_kw:
        tfidf = TfidfVectorizer(max_features=80, ngram_range=(1, 2),
                                stop_words="english", min_df=1)
        try:
            tfidf_mat = tfidf.fit_transform(kw_texts).toarray()
            feature_names = tfidf.get_feature_names_out()
        except ValueError:
            tfidf_mat = np.zeros((len(all_df), 0))
            feature_names = np.array([])
    else:
        tfidf_mat = np.zeros((len(all_df), 0))
        feature_names = np.array([])

    # Weights: signal × 2, type × 0.5, tfidf × 3
    X = np.hstack([
        sig.reshape(-1, 1) * 2,
        type_ind.reshape(-1, 1) * 0.5,
        tfidf_mat * 3,
    ])
    X = normalize(X, norm="l2")

    # ── KMeans ───────────────────────────────────────────────────────────────
    k = n_clusters if n_clusters else _auto_k(X, max_k=max_k)
    km = KMeans(n_clusters=k, random_state=42, n_init=10)
    labels = km.fit_predict(X)
    print(f"  → k={k} clusters")

    # Top TF-IDF terms per competitor
    top_kw_per_id: dict[str, str] = {}
    if len(feature_names) > 0:
        for i, cid in enumerate(all_df["competitor_id"].tolist()):
            row_v = tfidf_mat[i]
            top_idx = row_v.argsort()[::-1][:6]
            terms = [feature_names[j] for j in top_idx if row_v[j] > 0]
            top_kw_per_id[cid] = ", ".join(terms)

    results: list[Competitor] = []
    for i, row in all_df.iterrows():
        cid = row["competitor_id"]
        results.append(Competitor(
            competitor_id=cid,
            competitor_type=row["competitor_type"],
            domain=row["domain"],
            name=row["name"],
            cluster_id=int(labels[all_df.index.get_loc(i)]),
            signal=float(row["signal"]),
            top_keywords=top_kw_per_id.get(cid, ""),
        ))

    return sorted(results, key=lambda x: (x.cluster_id, -x.signal))


def save_clusters(db_path: pathlib.Path, clusters: list[Competitor]) -> int:
    rows = []
    for c in clusters:
        rows.append({
            "competitor_id": c.competitor_id,
            "competitor_type": c.competitor_type,
            "domain": c.domain,
            "cluster_id": c.cluster_id,
            "top_keywords": c.top_keywords,
        })
    return db.upsert_rows(db_path, "competitor_clusters", rows)


# ── Social data assignment ─────────────────────────────────────────────────────

def assign_social_to_clusters(db_path: pathlib.Path) -> int:
    """Score TikTok/YouTube/Reddit against cluster top_keywords → write cluster_id.

    Matching uses content text ONLY (not the search query used to fetch the item).
    The query string reflects what we searched for, not what the content is about —
    using it causes all videos from a "virtual phone number" query to trivially match
    that cluster even if the video is about character.ai or something unrelated.

    Minimum score thresholds:
      TikTok  → 1  (captions are short, single keyword match is meaningful)
      YouTube → 2  (titles are precise; require 2 keyword matches to reduce noise)
      Reddit  → 2  (same reasoning as YouTube)
    """
    # Build cluster keyword sets
    clusters_df = db.query_df(db_path,
        "SELECT cluster_id, top_keywords FROM competitor_clusters "
        "WHERE top_keywords IS NOT NULL AND cluster_id >= 0 "
        "GROUP BY cluster_id")
    if clusters_df.empty:
        print("No clusters found.")
        return 0

    cluster_kws: dict[int, set[str]] = {}
    for _, row in clusters_df.iterrows():
        kws = {k.strip().lower() for k in (row["top_keywords"] or "").split(",") if k.strip()}
        if kws:
            cluster_kws[int(row["cluster_id"])] = kws

    if not cluster_kws:
        return 0

    # Min keyword-hit count required before assigning an item to a cluster.
    # Title-only matching (no query contamination) is strict enough; keep threshold at 1.
    _MIN_SCORE = {"tiktok": 1, "youtube": 1, "reddit": 1}

    total = 0
    for source, table, text_col in [
        ("tiktok", "social_tiktok", "text"),
        ("youtube", "social_youtube", "title"),
        ("reddit", "social_reddit", "title"),
    ]:
        rows_df = db.query_df(db_path,
            f"SELECT id, {text_col} FROM {table}")
        if rows_df.empty:
            continue

        min_score = _MIN_SCORE.get(source, 1)
        map_rows = []
        for _, row in rows_df.iterrows():
            # Use content text only — not the fetch query
            combined = str(row.get(text_col) or "").lower()

            best_cid, best_score = -1, 0
            for cid, kws in cluster_kws.items():
                score = sum(1 for k in kws if k in combined)
                if score > best_score:
                    best_score, best_cid = score, cid

            if best_score >= min_score:
                map_rows.append({
                    "source": source,
                    "item_id": str(row["id"]),
                    "cluster_id": best_cid,
                    "score": float(best_score),
                })

        if map_rows:
            n = db.upsert_rows(db_path, "social_cluster_map", map_rows)
            total += n
            print(f"  {source}: {n} items assigned to clusters")

    return total


def print_clusters(clusters: list[Competitor]) -> None:
    by_cluster: dict[int, list[Competitor]] = {}
    for c in clusters:
        by_cluster.setdefault(c.cluster_id, []).append(c)

    for cid, members in sorted(by_cluster.items()):
        apps = [m for m in members if m.competitor_type == "app"]
        webs = [m for m in members if m.competitor_type == "web"]
        kw_hint = members[0].top_keywords if members else ""
        print(f"\nCluster {cid}  ({len(apps)} apps, {len(webs)} web sites)")
        print(f"  Keywords: {kw_hint}")
        for m in members[:6]:
            icon = "📱" if m.competitor_type == "app" else "🌐"
            print(f"    {icon} {m.name}")
