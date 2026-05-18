"""LLM-defined market track taxonomy + keyword-overlap competitor assignment.

Two-phase flow (preferred):
  1. propose(db_path, n, hints) → saves proposed_tracks.json, returns track list for human review
  2. apply(db_path)             → reads proposed_tracks.json, commits to DB + assigns competitors

One-shot flow (no pause):
  run(db_path, n, hints)        → propose + apply in one call
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import db
import config
import hub_client

REPORTS_BASE = pathlib.Path.home() / "workspace/CIA/reports"

# ── Path helpers ───────────────────────────────────────────────────────────────

def _topic_dir(db_path: pathlib.Path) -> pathlib.Path:
    return db_path.parent


def _proposal_path(db_path: pathlib.Path) -> pathlib.Path:
    return _topic_dir(db_path) / "proposed_tracks.json"


# ── Context builder ────────────────────────────────────────────────────────────

def pull_context(db_path: pathlib.Path) -> dict:
    topic = db.get_meta(db_path, "topic", "") or ""

    google_kw = db.query_df(db_path,
        "SELECT keyword FROM keywords_google ORDER BY volume DESC NULLS LAST LIMIT 60")
    aso_kw = db.query_df(db_path,
        "SELECT keyword, MAX(search_volume) AS sv FROM appstore_keywords "
        "GROUP BY keyword ORDER BY sv DESC NULLS LAST LIMIT 60")
    # ★ Include review counts so LLM sees actual market size, not just topic relevance
    apps = db.query_df(db_path,
        "SELECT name, category, review_count FROM competitors_app "
        "ORDER BY review_count DESC NULLS LAST LIMIT 40")

    apps_ranked = []
    for _, row in apps.iterrows():
        name = str(row.get("name") or "")
        rc = int(row.get("review_count") or 0)
        cat = str(row.get("category") or "")
        apps_ranked.append(f"{name} ({rc:,} reviews, {cat})")

    return {
        "topic": topic,
        "google_kw": google_kw["keyword"].tolist() if not google_kw.empty else [],
        "aso_kw": aso_kw["keyword"].tolist() if not aso_kw.empty else [],
        "apps_ranked": apps_ranked,
    }


# ── LLM call ──────────────────────────────────────────────────────────────────

# Generic AI chatbot names to exclude from keywords (not market-specific)
_GENERIC_EXCLUDES = (
    "chatgpt, gpt-4, openai, claude, gemini, perplexity, copilot, bard, "
    "chat ai, ai chatbot, ai assistant, generative ai, llm, large language model"
)

_PROMPT = """\
You are a CIA-style market analyst. Your job is to discover ALL significant market segments in a competitive landscape — especially ones that may surprise the person who gave you the topic name.

ANTI-BIAS RULE: The topic "{topic}" is just the entry point. Look at the actual apps sorted by review count to find where the REAL market volume is. An app with 300K reviews represents a larger market than one with 5K reviews, even if it seems less related to the topic name.

## Competitor apps ranked by review count (actual market size signal):
{apps_ranked}

## Top Google search keywords:
{google_kw}

## Top App Store ASO keywords:
{aso_kw}

## User expects these tracks (include if data supports):
{hints}

## Define exactly {n} market tracks.

Rules:
1. Name each track by what USERS actually do, not by technology (e.g., "实时翻译类" not "NLP Tools")
2. Chinese name: 4-8 chars, ending in 类 (e.g., "实时翻译类", "虚拟号码类", "外呼自动化")
3. Do NOT let the topic name "{topic}" bias you toward similar niches — check the review counts
4. Include 12-20 lowercase keywords. These must be SPECIFIC to this track. Exclude generic AI terms like: {excludes}
5. If a track appears in the user hints list above, make sure to include it (if you see supporting evidence in the data)
6. Each track should be distinct with minimal keyword overlap

Return ONLY a valid JSON array, nothing else:
[
  {{
    "name": "赛道中文名（4-8字）",
    "name_en": "Track English Name",
    "description": "One sentence. What does the user DO with this type of product?",
    "keywords": ["keyword1", "keyword2", ...]
  }}
]
"""


def define_tracks_llm(topic: str, context: dict, n: int = 6,
                      hints: list[str] | None = None) -> list[dict]:
    """Call LLM (Hub or direct Anthropic) → return list of normalized track dicts."""
    hints_str = (", ".join(hints) if hints else "（无特殊要求）")
    prompt = _PROMPT.format(
        topic=topic,
        n=n,
        apps_ranked="\n".join(context["apps_ranked"][:30]),
        google_kw=", ".join(context["google_kw"][:40]),
        aso_kw=", ".join(context["aso_kw"][:40]),
        hints=hints_str,
        excludes=_GENERIC_EXCLUDES,
    )

    print(f"Calling LLM (haiku) — defining {n} tracks for '{topic}'...")

    if hub_client.enabled():
        raw = hub_client.llm([{"role": "user", "content": prompt}])
    else:
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("pip install anthropic")
        api_key = config.get("ANTHROPIC_AUTH_TOKEN") or config.get("ANTHROPIC_API_KEY")
        base_url = config.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        if not api_key:
            raise RuntimeError("ANTHROPIC_AUTH_TOKEN not set in ~/.claude/settings.json")
        client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()

    m = re.search(r'\[.*\]', raw, re.DOTALL)
    if not m:
        raise ValueError(f"LLM did not return a JSON array.\nRaw:\n{raw[:400]}")

    tracks_raw = json.loads(m.group(0))
    if not isinstance(tracks_raw, list) or not tracks_raw:
        raise ValueError("LLM response is not a non-empty list")

    result = []
    for i, t in enumerate(tracks_raw[:n]):
        kws = [str(k).lower().strip() for k in t.get("keywords", []) if k]
        result.append({
            "track_id": i,
            "name": t.get("name") or f"Track {i}",
            "name_en": t.get("name_en") or "",
            "description": t.get("description") or "",
            "keywords": ",".join(kws),
        })
    return result


# ── Phase 1: Propose ───────────────────────────────────────────────────────────

def propose(db_path: pathlib.Path, n: int = 6,
            hints: list[str] | None = None) -> list[dict]:
    """
    Phase 1: LLM generates track proposals → saves to proposed_tracks.json.
    Does NOT write to the DB. Returns the track list for display/review.
    """
    ctx = pull_context(db_path)
    topic = ctx["topic"] or "(unknown)"

    if not ctx["apps_ranked"] and not ctx["google_kw"]:
        print("⚠ Warning: DB has no competitor or keyword data. Track quality will be low.")

    tracks = define_tracks_llm(topic, ctx, n=n, hints=hints)

    out = _proposal_path(db_path)
    out.write_text(json.dumps(tracks, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n── 赛道提案 ({len(tracks)} 条) ─────────────────────────────────────")
    for t in tracks:
        kw_preview = (t["keywords"] or "")[:80]
        print(f"  [{t['track_id']}] {t['name']}  ({t['name_en']})")
        print(f"       {t['description']}")
        print(f"       词: {kw_preview}…")
    print(f"\n提案已保存到: {out}")
    print("请确认或编辑后执行 apply-tracks。")

    return tracks


# ── Phase 2: Apply ────────────────────────────────────────────────────────────

def apply(db_path: pathlib.Path) -> None:
    """
    Phase 2: Read proposed_tracks.json → save to DB → assign competitors.
    """
    out = _proposal_path(db_path)
    if not out.exists():
        raise FileNotFoundError(
            f"No proposal found at {out}. Run propose-tracks first.")

    tracks = json.loads(out.read_text(encoding="utf-8"))
    print(f"Applying {len(tracks)} tracks from {out.name}...")
    save_tracks(db_path, tracks)
    print("\nAssigning competitors to tracks...")
    assign_competitors(db_path)


# ── Persist tracks ─────────────────────────────────────────────────────────────

def save_tracks(db_path: pathlib.Path, tracks: list[dict]) -> None:
    with db.connect(db_path) as con:
        con.execute("DELETE FROM tracks")
    db.upsert_rows(db_path, "tracks", tracks)
    print(f"Saved {len(tracks)} tracks to DB:")
    for t in tracks:
        print(f"  [{t['track_id']}] {t['name']} ({t['name_en']})")


# ── Competitor assignment ──────────────────────────────────────────────────────

def assign_competitors(db_path: pathlib.Path) -> int:
    """
    Score each App + Web competitor against track keyword sets.
    Writes to competitor_clusters (cluster_id=track_id, cluster_label=track name).
    Populates top_keywords per row so assign_social_to_clusters works unchanged.
    """
    tracks_df = db.query_df(db_path, "SELECT track_id, name, keywords FROM tracks")
    if tracks_df.empty:
        print("No tracks defined — run propose-tracks + apply-tracks first.")
        return 0

    track_kws: list[tuple[int, str, list[str]]] = []
    for _, row in tracks_df.iterrows():
        kws = [k.strip() for k in (row["keywords"] or "").split(",") if k.strip()]
        track_kws.append((int(row["track_id"]), str(row["name"]), kws))

    def best_track(text: str) -> tuple[int | None, str, str]:
        """Return (track_id, name, top_kws) or (None, '', '') if no keyword matches."""
        text_lower = text.lower()
        best_id: int | None = None
        best_name, best_score = "", 0
        for tid, tname, kws in track_kws:
            score = sum(1 for k in kws if k in text_lower)
            if score > best_score:
                best_score, best_id, best_name = score, tid, tname
        if best_id is None or best_score == 0:
            return None, "", ""  # unmatched — skip, don't pollute track 0
        winning_kws = track_kws[best_id][2]
        return best_id, best_name, ",".join(winning_kws[:20])

    rows = []

    apps_df = db.query_df(db_path, """
        SELECT a.app_id, a.name, a.category, a.description,
               GROUP_CONCAT(k.keyword, ' ') AS kw_text
        FROM competitors_app a
        LEFT JOIN appstore_keywords k ON k.app_id = a.app_id
        GROUP BY a.app_id
    """)
    for _, a in apps_df.iterrows():
        text = " ".join(filter(None, [
            str(a.get("name") or ""),
            str(a.get("category") or ""),
            str(a.get("description") or "")[:300],
            str(a.get("kw_text") or ""),
        ]))
        tid, tname, top_kws = best_track(text)
        if tid is None:
            continue  # no track match — stays in global competitors page only
        cid = str(a["app_id"])
        rows.append({
            "app_id": cid,
            "competitor_id": cid,
            "competitor_type": "app",
            "domain": None,
            "cluster_id": tid,
            "cluster_label": tname,
            "top_keywords": top_kws,
        })

    web_df = db.query_df(db_path, """
        SELECT w.domain, GROUP_CONCAT(k.keyword, ' ') AS kw_text
        FROM competitors_web w
        LEFT JOIN competitor_organic_kw k ON k.domain = w.domain
        GROUP BY w.domain
    """)
    for _, w in web_df.iterrows():
        text = " ".join(filter(None, [
            str(w.get("domain") or ""),
            str(w.get("kw_text") or ""),
        ]))
        tid, tname, top_kws = best_track(text)
        if tid is None:
            continue
        domain = str(w["domain"])
        rows.append({
            "app_id": domain,
            "competitor_id": domain,
            "competitor_type": "web",
            "domain": domain,
            "cluster_id": tid,
            "cluster_label": tname,
            "top_keywords": top_kws,
        })

    if not rows:
        print("No competitors found to assign.")
        return 0

    # Clear ALL existing assignments before writing fresh ones (prevents stale rows)
    with db.connect(db_path) as con:
        con.execute("DELETE FROM competitor_clusters")

    n = db.upsert_rows(db_path, "competitor_clusters", rows)
    dist = Counter(r["cluster_label"] for r in rows)
    print(f"Assigned {n} competitors:")
    for label, count in sorted(dist.items(), key=lambda x: -x[1]):
        print(f"  {label}: {count}")
    return n


# ── SV补采：iTunes SERP → DataForSEO ASO ──────────────────────────────────────

def fetch_missing_svs(db_path: pathlib.Path, limit: int = 1000) -> float:
    """
    For each track, fetch keywords_for_app (limit=1000) for the top 3 apps by review count.
    This is more reliable than iTunes SERP (no rate limiting) and gives deeper keyword coverage.
    Returns total cost spent.
    """
    from fetchers import dataforseo

    # Load all track keywords for coverage reporting
    tracks_df = db.query_df(db_path, "SELECT track_id, keywords FROM tracks")
    all_track_kws: set[str] = set()
    for _, row in tracks_df.iterrows():
        for kw in (row["keywords"] or "").split(","):
            if kw.strip():
                all_track_kws.add(kw.strip().lower())

    # Top 3 apps per track by review count (deduplicated across tracks)
    apps_df = db.query_df(db_path, """
        SELECT DISTINCT cc.competitor_id AS app_id, ca.name, ca.review_count,
               cc.cluster_label, cc.cluster_id,
               ROW_NUMBER() OVER (
                   PARTITION BY cc.cluster_id
                   ORDER BY ca.review_count DESC NULLS LAST
               ) AS rank_in_track
        FROM competitor_clusters cc
        JOIN competitors_app ca ON ca.app_id = cc.competitor_id
        WHERE cc.competitor_type = 'app'
    """)

    if apps_df.empty:
        print("No app competitors found.")
        return 0.0

    top_apps = apps_df[apps_df["rank_in_track"] <= 3]["app_id"].unique().tolist()
    print(f"\nFetching {limit} keywords for {len(top_apps)} top apps across all tracks...")

    total_cost = 0.0
    for app_id in top_apps:
        try:
            rows, cost = dataforseo.fetch_keywords_for_app(app_id, limit=limit)
            if rows:
                db.upsert_rows(db_path, "appstore_keywords", rows)
                db.log_fetch(db_path, "dataforseo", "aso/track_sv_fill",
                             {"app_id": app_id, "limit": limit},
                             rows=len(rows), cost_usd=cost)
                total_cost += cost
                covered = sum(1 for r in rows
                              if r.get("keyword", "").lower() in all_track_kws)
                app_name = apps_df[apps_df["app_id"] == app_id]["name"].iloc[0] \
                    if not apps_df[apps_df["app_id"] == app_id].empty else app_id
                print(f"  {app_name[:30]}: {len(rows)} kw "
                      f"({covered} track kw covered)  ${cost:.4f}")
        except Exception as e:
            print(f"  App {app_id}: {e}")

    print(f"\nSV fetch done. Total cost: ${total_cost:.4f}")
    return total_cost


# ── One-shot shortcut ──────────────────────────────────────────────────────────

def run(db_path: pathlib.Path, n: int = 6,
        hints: list[str] | None = None) -> None:
    """propose + apply without pause (use only when no confirmation needed)."""
    propose(db_path, n=n, hints=hints)
    apply(db_path)
