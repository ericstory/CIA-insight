"""Cluster competitors by profile similarity.

Uses sklearn KMeans on a feature matrix built from:
  - log(review_count + 1) normalized
  - rating / 5.0
  - category one-hot (top 10 categories)
  - ASO keyword TF-IDF (top 50 terms across all apps)

Determines k automatically via elbow method (inertia delta, max k=8).

Usage:
    from analysis.cluster import cluster_competitors, save_clusters
    clusters = cluster_competitors(db_path)
    n = save_clusters(db_path, clusters)
"""
from __future__ import annotations

import sys
import math
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
class CompetitorCluster:
    app_id: str
    app_name: str
    cluster_id: int
    review_count: int
    rating: float
    category: str
    # Top keywords for this app (for cluster naming hint)
    top_keywords: str


def _auto_k(X: np.ndarray, max_k: int = 8) -> int:
    """Pick k via elbow: stop when inertia improvement drops below 15%."""
    if len(X) <= 2:
        return 1
    max_k = min(max_k, len(X) - 1)
    inertias = []
    for k in range(1, max_k + 1):
        km = KMeans(n_clusters=k, random_state=42, n_init=10)
        km.fit(X)
        inertias.append(km.inertia_)
    best_k = 1
    for i in range(1, len(inertias)):
        prev = inertias[i - 1]
        curr = inertias[i]
        if prev > 0 and (prev - curr) / prev < 0.15:
            break
        best_k = i + 1
    return max(2, min(best_k, max_k))


# Noise-app bundle IDs to exclude from clustering (generic AI chat apps)
_NOISE_BUNDLES = {
    "com.openai.chat", "com.google.LLM", "com.anthropic.claude",
    "ai.perplexity.app.ios", "com.character.ai",
}
_NOISE_NAME_FRAGMENTS = {
    "chatgpt", "replika", "character ai", "perplexity", "genie", "poe ",
    "kindroid", "synclub", "bala", "math solver",
}


def _is_noise(row: pd.Series) -> bool:
    bundle = str(row.get("bundle_id", "") or "").lower()
    name = str(row.get("name", "") or "").lower()
    if bundle in _NOISE_BUNDLES:
        return True
    return any(frag in name for frag in _NOISE_NAME_FRAGMENTS)


def cluster_competitors(
    db_path: pathlib.Path,
    *,
    n_clusters: int | None = None,
    max_k: int = 8,
) -> list[CompetitorCluster]:
    if not _HAS_SKLEARN:
        raise RuntimeError(
            "scikit-learn is required for clustering. "
            "Run: pip install scikit-learn"
        )

    apps_df = db.query_df(db_path, """
        SELECT app_id, name, subtitle, bundle_id, rating, review_count, category
        FROM competitors_app
        WHERE review_count IS NOT NULL AND review_count > 0
    """)
    if len(apps_df) < 2:
        return []

    # Filter noise apps
    apps_df = apps_df[~apps_df.apply(_is_noise, axis=1)].copy()
    if len(apps_df) < 2:
        return []

    # ASO keywords per app (concatenated for TF-IDF)
    kw_df = db.query_df(db_path, """
        SELECT app_id, GROUP_CONCAT(keyword, ' ') AS kw_text
        FROM appstore_keywords
        GROUP BY app_id
    """)
    apps_df = apps_df.merge(kw_df, on="app_id", how="left")
    apps_df["kw_text"] = apps_df["kw_text"].fillna("")

    # --- Feature matrix ---
    # 1) Numerical: log_reviews, rating
    log_rev = np.log1p(apps_df["review_count"].fillna(0).values.astype(float))
    max_lr = log_rev.max() or 1.0
    log_rev = log_rev / max_lr

    rating = apps_df["rating"].fillna(3.0).values.astype(float) / 5.0

    num_feats = np.column_stack([log_rev, rating])  # (n, 2)

    # 2) Category one-hot (top 10 categories by frequency)
    top_cats = (
        apps_df["category"].dropna().value_counts().head(10).index.tolist()
    )
    cat_ohe = np.zeros((len(apps_df), len(top_cats)), dtype=float)
    for i, cat in enumerate(apps_df["category"].fillna("").tolist()):
        if cat in top_cats:
            cat_ohe[i, top_cats.index(cat)] = 1.0

    # 3) TF-IDF on ASO keyword text (max 50 features)
    kw_texts = apps_df["kw_text"].tolist()
    if any(t.strip() for t in kw_texts):
        tfidf = TfidfVectorizer(max_features=50, ngram_range=(1, 2),
                                stop_words="english", min_df=2)
        try:
            tfidf_mat = tfidf.fit_transform(kw_texts).toarray()
            feature_names = tfidf.get_feature_names_out()
        except ValueError:
            tfidf_mat = np.zeros((len(apps_df), 0))
            feature_names = np.array([])
    else:
        tfidf_mat = np.zeros((len(apps_df), 0))
        feature_names = np.array([])

    # Weight: num(2) × 3, cat × 1, tfidf × 2
    X = np.hstack([
        num_feats * 3,
        cat_ohe * 1,
        tfidf_mat * 2,
    ])
    X = normalize(X, norm="l2")

    # --- Cluster ---
    k = n_clusters if n_clusters else _auto_k(X, max_k=max_k)
    km = KMeans(n_clusters=k, random_state=42, n_init=10)
    labels = km.fit_predict(X)

    # Top TF-IDF terms per app for cluster naming hints
    top_kw_per_app: dict[str, str] = {}
    if len(feature_names) > 0:
        for i, app_id in enumerate(apps_df["app_id"].tolist()):
            row_tfidf = tfidf_mat[i]
            top_idx = row_tfidf.argsort()[::-1][:5]
            terms = [feature_names[j] for j in top_idx if row_tfidf[j] > 0]
            top_kw_per_app[app_id] = ", ".join(terms)

    results: list[CompetitorCluster] = []
    for i, (_, row) in enumerate(apps_df.iterrows()):
        results.append(CompetitorCluster(
            app_id=str(row["app_id"]),
            app_name=str(row["name"] or ""),
            cluster_id=int(labels[i]),
            review_count=int(row["review_count"] or 0),
            rating=float(row["rating"] or 0),
            category=str(row["category"] or ""),
            top_keywords=top_kw_per_app.get(str(row["app_id"]), ""),
        ))

    return sorted(results, key=lambda x: (x.cluster_id, -x.review_count))


def save_clusters(db_path: pathlib.Path, clusters: list[CompetitorCluster]) -> int:
    """Persist cluster assignments to competitor_clusters table."""
    rows = [
        {
            "app_id": c.app_id,
            "cluster_id": c.cluster_id,
            "top_keywords": c.top_keywords,
        }
        for c in clusters
    ]
    return db.upsert_rows(db_path, "competitor_clusters", rows)


def print_clusters(clusters: list[CompetitorCluster]) -> None:
    """Print cluster summary grouped by cluster_id."""
    by_cluster: dict[int, list[CompetitorCluster]] = {}
    for c in clusters:
        by_cluster.setdefault(c.cluster_id, []).append(c)

    for cid, members in sorted(by_cluster.items()):
        total_rev = sum(m.review_count for m in members)
        kw_hint = members[0].top_keywords if members else ""
        print(f"\nCluster {cid}  ({len(members)} apps, {total_rev:,} total reviews)")
        print(f"  Keywords: {kw_hint}")
        for m in members[:8]:
            print(f"    [{m.rating:.1f}★ {m.review_count:>7,}] {m.app_name}  ({m.category})")
