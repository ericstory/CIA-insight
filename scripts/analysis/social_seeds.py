"""Extract seed keywords from high-engagement social content.

TikTok/Reddit reveal what language *converts* — users discover products through
content before they search. This module surfaces those phrases as demand-side seeds.

Usage:
    from analysis.social_seeds import extract_signal_phrases, save_to_seeds
    phrases = extract_signal_phrases(db_path, min_tiktok_share_rate=0.005, min_reddit_score=10)
    n = save_to_seeds(db_path, phrases)
"""
from __future__ import annotations

import re
import sys
import pathlib
from collections import Counter
from typing import NamedTuple

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import db

# n-gram window for phrase extraction
_MIN_N = 2
_MAX_N = 4
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "this", "that", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would", "can", "could",
    "should", "may", "might", "i", "you", "we", "they", "he", "she", "it",
    "my", "your", "our", "their", "its", "me", "him", "her", "us", "them",
    "just", "like", "so", "very", "really", "actually", "also", "more",
    "about", "what", "how", "when", "where", "who", "which", "why",
    "not", "no", "yes", "get", "got", "use", "using", "used",
}


class SignalPhrase(NamedTuple):
    phrase: str
    count: int
    source: str          # "tiktok" | "reddit" | "mixed"
    max_engagement: float  # share_rate for TT, score for Reddit (normalized)


def _tokenize(text: str) -> list[str]:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s'-]", " ", text)
    return [w.strip("-'") for w in text.split() if w.strip("-'")]


def _ngrams(tokens: list[str], n: int) -> list[str]:
    """Yield n-grams, skipping any that start/end with a stopword."""
    grams = []
    for i in range(len(tokens) - n + 1):
        window = tokens[i : i + n]
        if window[0] in _STOPWORDS or window[-1] in _STOPWORDS:
            continue
        if all(t in _STOPWORDS for t in window):
            continue
        grams.append(" ".join(window))
    return grams


def _extract_from_texts(texts: list[str]) -> Counter:
    counts: Counter = Counter()
    for text in texts:
        tokens = _tokenize(text)
        for n in range(_MIN_N, _MAX_N + 1):
            counts.update(_ngrams(tokens, n))
    return counts


def extract_signal_phrases(
    db_path: pathlib.Path,
    *,
    min_tiktok_share_rate: float = 0.005,
    min_reddit_score: int = 10,
    top_n: int = 80,
) -> list[SignalPhrase]:
    """Return top phrases from high-engagement social content."""
    results: list[SignalPhrase] = []

    # --- TikTok: high share-rate posts signal commercial intent ---
    tt_df = db.query_df(db_path, """
        SELECT text, plays, likes, shares,
               CASE WHEN plays > 0 THEN CAST(shares AS REAL) / plays ELSE 0 END AS share_rate
        FROM social_tiktok
        WHERE plays > 100
        ORDER BY share_rate DESC
    """)
    tt_high = tt_df[tt_df["share_rate"] >= min_tiktok_share_rate]
    tt_texts = tt_high["text"].dropna().tolist()
    tt_counts = _extract_from_texts(tt_texts)

    # Build per-phrase max engagement mapping
    tt_engagement: dict[str, float] = {}
    for _, row in tt_high.iterrows():
        for n in range(_MIN_N, _MAX_N + 1):
            tokens = _tokenize(str(row["text"]))
            for gram in _ngrams(tokens, n):
                tt_engagement[gram] = max(tt_engagement.get(gram, 0), float(row["share_rate"]))

    # --- Reddit: high-score posts signal pain points / awareness ---
    rd_df = db.query_df(db_path, """
        SELECT title || ' ' || COALESCE(body, '') AS text, score
        FROM social_reddit
        WHERE score >= ?
        ORDER BY score DESC
    """, (min_reddit_score,))
    rd_texts = rd_df["text"].dropna().tolist()
    rd_counts = _extract_from_texts(rd_texts)

    rd_engagement: dict[str, float] = {}
    max_score = float(rd_df["score"].max()) if len(rd_df) else 1.0
    for _, row in rd_df.iterrows():
        norm = float(row["score"]) / max_score
        for n in range(_MIN_N, _MAX_N + 1):
            tokens = _tokenize(str(row["text"]))
            for gram in _ngrams(tokens, n):
                rd_engagement[gram] = max(rd_engagement.get(gram, 0), norm)

    # Merge: phrases appearing in both sources get a boost
    all_phrases: set[str] = set(tt_counts) | set(rd_counts)
    scored: list[tuple[int, str, str, float]] = []
    for phrase in all_phrases:
        tt_c = tt_counts.get(phrase, 0)
        rd_c = rd_counts.get(phrase, 0)
        total = tt_c + rd_c
        if total < 2:
            continue
        if tt_c > 0 and rd_c > 0:
            source = "mixed"
        elif tt_c > 0:
            source = "tiktok"
        else:
            source = "reddit"
        eng = max(tt_engagement.get(phrase, 0), rd_engagement.get(phrase, 0))
        scored.append((total, phrase, source, eng))

    scored.sort(key=lambda x: (-x[0], -x[3]))
    for total, phrase, source, eng in scored[:top_n]:
        results.append(SignalPhrase(phrase=phrase, count=total, source=source, max_engagement=eng))

    return results


def save_to_seeds(
    db_path: pathlib.Path,
    phrases: list[SignalPhrase],
    *,
    min_count: int = 3,
) -> int:
    """Save high-confidence phrases to seeds table as demand|social_signal."""
    rows = [
        {"keyword": p.phrase, "dimension": "social_signal", "side": "demand"}
        for p in phrases
        if p.count >= min_count
    ]
    if not rows:
        return 0
    return db.upsert_rows(db_path, "seeds", rows, on_conflict="IGNORE")


def print_report(phrases: list[SignalPhrase], top: int = 30) -> None:
    """Print a quick summary table to stdout."""
    print(f"\n{'PHRASE':<35} {'COUNT':>5}  {'SOURCE':<7}  {'ENG':>6}")
    print("-" * 60)
    for p in phrases[:top]:
        print(f"{p.phrase:<35} {p.count:>5}  {p.source:<7}  {p.max_engagement:>6.4f}")
