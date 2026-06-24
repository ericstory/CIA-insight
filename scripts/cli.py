#!/usr/bin/env python3
"""CIA CLI — fixed analysis pipeline.

Usage examples:
  python3 cli.py init "ai phone receptionist" [--country us]
  python3 cli.py status --topic "ai phone receptionist"

  python3 cli.py fetch-itunes-serp --topic <t> --keywords "kw1,kw2,..."
  python3 cli.py fetch-aso-keywords --topic <t> --app-ids "1241817309,921879637"
  python3 cli.py fetch-app-reviews --topic <t> --app-ids "1241817309" --depth 200
  python3 cli.py fetch-tiktok  --topic <t> --queries "ai receptionist,ai phone answering"
  python3 cli.py fetch-reddit  --topic <t> --queries "ai answering service" --subreddits "smallbusiness,startups"
  python3 cli.py fetch-youtube --topic <t> --queries "ai receptionist demo" --per-query 15

  python3 cli.py ingest-ahrefs --topic <t> --kind keywords --file resp.json --source-seed "ai receptionist"
  python3 cli.py ingest-ahrefs --topic <t> --kind site-metrics --file resp.json --domain quo.com
  python3 cli.py ingest-ahrefs --topic <t> --kind organic-kw --file resp.json --domain quo.com
  python3 cli.py ingest-ahrefs --topic <t> --kind brand-radar --file resp.json --brand quo.com

  python3 cli.py export --topic <t> [--synthesis-file synthesis.md]
  python3 cli.py export-briefs --topic <t> [--min-items 3] [--top-tt 15] [--top-yt 10]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from datetime import date

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))

import db
import hub_client
from fetchers import dataforseo, itunes, apify, youtube, ahrefs_ingest
from export import excel, html as html_exp, briefs as briefs_exp
from analysis import social_seeds as _social_seeds
from analysis import cluster as _cluster
from analysis import define_tracks as _define_tracks
from pipeline import discover as _discover


REPORTS_BASE = pathlib.Path.home() / "workspace/CIA/reports"


def topic_dir(topic: str) -> pathlib.Path:
    """Return path to the topic's report dir, creating if needed.

    Re-uses today's dir for that topic; falls back to the most recent existing dir.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    today = date.today().isoformat()
    candidate = REPORTS_BASE / f"{today}-cia-{slug}"
    if candidate.exists():
        return candidate
    # Look for an existing dir for this topic
    if REPORTS_BASE.exists():
        existing = sorted(
            [p for p in REPORTS_BASE.iterdir() if p.is_dir() and p.name.endswith(f"-cia-{slug}")],
            reverse=True,
        )
        if existing:
            return existing[0]
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def db_path_for(topic: str) -> pathlib.Path:
    d = topic_dir(topic)
    p = d / "cia.db"
    if not p.exists():
        db.init(p)
        db.set_meta(p, "topic", topic)
        db.set_meta(p, "created_at", date.today().isoformat())
    return p


def _split(s: str | None) -> list[str]:
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


# ---------- commands ----------

def cmd_init(args):
    p = db_path_for(args.topic)
    db.set_meta(p, "country", args.country)
    print(f"OK  topic={args.topic}  db={p}")


def cmd_status(args):
    p = db_path_for(args.topic)
    summary = []
    for table in [
        "seeds", "keywords_google", "appstore_serp", "appstore_keywords",
        "competitors_app", "competitor_clusters", "app_reviews", "competitors_web",
        "competitor_organic_kw", "ai_visibility",
        "social_tiktok", "social_reddit", "social_youtube", "fetch_log",
    ]:
        try:
            n = db.query_df(p, f"SELECT COUNT(*) AS n FROM {table}").iloc[0]["n"]
        except Exception:
            n = "?"
        summary.append(f"  {table:30s} {n:>6}")
    print(f"db: {p}")
    print(f"total cost: ${db.total_cost(p):.4f}")
    print("rows:")
    print("\n".join(summary))


def cmd_fetch_itunes_serp(args):
    p = db_path_for(args.topic)
    kws = _split(args.keywords)
    rows = []
    with db.Timer() as t:
        if hub_client.enabled():
            rows = hub_client.fetch("itunes_serp", keywords=kws, country=args.country, limit=args.limit)
        else:
            rows = itunes.keyword_to_apps_bulk(kws, country=args.country, limit=args.limit)
    n = db.upsert_rows(p, "appstore_serp", rows)
    db.log_fetch(p, "itunes", "keyword_to_apps_bulk",
                 {"keywords": kws, "country": args.country}, rows=n, duration_ms=t.ms)
    print(f"itunes SERP: {n} rows from {len(kws)} keywords ({t.ms}ms)")


def cmd_fetch_gplay_serp(args):
    from fetchers import gplay
    p = db_path_for(args.topic)
    kws = _split(args.keywords)
    rows = []
    with db.Timer() as t:
        if hub_client.enabled():
            rows = hub_client.fetch("gplay_serp", keywords=kws, country=args.country, limit=args.limit)
        else:
            rows = gplay.keyword_to_apps_bulk(kws, country=args.country, limit=args.limit)
    n = db.upsert_rows(p, "appstore_serp", rows)
    db.log_fetch(p, "gplay", "keyword_to_apps_bulk",
                 {"keywords": kws, "country": args.country}, rows=n, duration_ms=t.ms)
    print(f"gplay SERP: {n} rows from {len(kws)} keywords ({t.ms}ms)")


def cmd_fetch_gplay_meta(args):
    from fetchers import gplay
    p = db_path_for(args.topic)
    app_ids = _split(args.app_ids)
    if not app_ids:
        # Auto-pick from gplay SERP results
        df = db.query_df(p,
            "SELECT DISTINCT app_id FROM appstore_serp WHERE store='android' AND app_id IS NOT NULL "
            "ORDER BY rank ASC LIMIT ?", (args.top,))
        app_ids = df["app_id"].tolist() if not df.empty else []
    if not app_ids:
        print("No Google Play app IDs found. Run fetch-gplay-serp first.")
        return
    rows = []
    with db.Timer() as t:
        if hub_client.enabled():
            rows = hub_client.fetch("gplay_meta", app_ids=app_ids, country=args.country)
        else:
            rows = gplay.lookup_apps(app_ids, country=args.country)
    n = db.upsert_rows(p, "competitors_app", rows)
    db.log_fetch(p, "gplay", "lookup_apps",
                 {"app_ids": app_ids, "country": args.country}, rows=n, duration_ms=t.ms)
    print(f"gplay meta: {n} apps ({t.ms}ms)")


def cmd_fetch_aso_keywords(args):
    p = db_path_for(args.topic)
    app_ids = _split(args.app_ids)
    if not app_ids:
        # Auto-pick top competitor apps from appstore_serp.
        # Optionally filter by category whitelist (joins competitors_app).
        cats = _split(args.include_categories)
        if cats:
            placeholders = ",".join("?" * len(cats))
            df = db.query_df(p, f"""
                SELECT s.app_id, s.app_name, COUNT(*) AS n_kw, AVG(s.rank) AS avg_rank
                FROM appstore_serp s
                JOIN competitors_app c ON c.app_id = s.app_id
                WHERE c.category IN ({placeholders})
                GROUP BY s.app_id, s.app_name
                ORDER BY n_kw DESC, avg_rank ASC LIMIT ?
            """, tuple(cats) + (args.top,))
        else:
            df = db.query_df(p, """
                SELECT app_id, app_name, COUNT(*) AS n_kw, AVG(rank) AS avg_rank
                FROM appstore_serp GROUP BY app_id, app_name
                ORDER BY n_kw DESC, avg_rank ASC LIMIT ?
            """, (args.top,))
        app_ids = df["app_id"].tolist()
        cat_msg = f" (cat filter: {cats})" if cats else ""
        print(f"auto-picked top {len(app_ids)} apps from SERP{cat_msg}")
    total_cost = 0.0
    total_rows = 0
    for aid in app_ids:
        with db.Timer() as t:
            if hub_client.enabled():
                rows = hub_client.fetch("aso_keywords", app_id=aid, limit=args.limit)
                cost = 0.0
            else:
                rows, cost = dataforseo.fetch_keywords_for_app(aid, limit=args.limit)
        # Hydrate app_name from competitors_app or appstore_serp
        nm_df = db.query_df(p, "SELECT name FROM competitors_app WHERE app_id=?", (aid,))
        if len(nm_df) == 0:
            nm_df = db.query_df(p, "SELECT app_name AS name FROM appstore_serp WHERE app_id=? LIMIT 1", (aid,))
        nm = nm_df.iloc[0]["name"] if len(nm_df) else None
        for r in rows:
            r["app_name"] = nm
        n = db.upsert_rows(p, "appstore_keywords", rows)
        db.log_fetch(p, "dataforseo", "keywords_for_app",
                     {"app_id": aid, "limit": args.limit}, rows=n, cost_usd=cost, duration_ms=t.ms)
        total_cost += cost
        total_rows += n
        print(f"  app_id={aid:>14}  {n:>4} keywords  ${cost:.4f}  ({t.ms}ms)")
    print(f"ASO keywords: {total_rows} rows for {len(app_ids)} apps  total ${total_cost:.4f}")


def cmd_fetch_app_reviews(args):
    p = db_path_for(args.topic)
    app_ids = _split(args.app_ids)
    if not app_ids:
        df = db.query_df(p, """
            SELECT app_id FROM competitors_app
            WHERE review_count IS NOT NULL
            ORDER BY review_count DESC LIMIT ?
        """, (args.top,))
        app_ids = df["app_id"].tolist()
    total = 0
    total_cost = 0.0
    for aid in app_ids:
        with db.Timer() as t:
            if hub_client.enabled():
                rows = hub_client.fetch("app_reviews", app_id=aid, depth=args.depth)
                cost = 0.0
            else:
                rows, cost = dataforseo.fetch_app_reviews(aid, depth=args.depth)
        n = db.upsert_rows(p, "app_reviews", rows)
        db.log_fetch(p, "dataforseo", "app_reviews",
                     {"app_id": aid, "depth": args.depth}, rows=n, cost_usd=cost, duration_ms=t.ms)
        total += n
        total_cost += cost
        print(f"  app_id={aid:>14}  {n:>4} reviews  ${cost:.4f}  ({t.ms}ms)")
    print(f"app reviews: {total} rows  total ${total_cost:.4f}")


def cmd_fetch_competitors_meta(args):
    """Hydrate competitor_app from iTunes lookup using app_ids in appstore_serp."""
    p = db_path_for(args.topic)
    app_ids = _split(args.app_ids)
    if not app_ids:
        df = db.query_df(p,
            "SELECT DISTINCT app_id FROM appstore_serp WHERE app_id != '' AND store='ios'")
        app_ids = df["app_id"].tolist()
    if hub_client.enabled():
        rows = hub_client.fetch("itunes_meta", app_ids=app_ids, country=args.country)
    else:
        rows = itunes.lookup_apps(app_ids, country=args.country)
    n = db.upsert_rows(p, "competitors_app", rows)
    db.log_fetch(p, "itunes", "lookup_apps", {"n": len(app_ids)}, rows=n)
    print(f"competitor metadata: {n} apps hydrated")


def cmd_fetch_tiktok(args):
    p = db_path_for(args.topic)
    qs = _split(args.queries)
    with db.Timer() as t:
        if hub_client.enabled():
            rows = hub_client.fetch("tiktok", queries=qs, max_items=args.max_items)
        else:
            rows = apify.fetch_tiktok_search(qs, max_items=args.max_items)
    n = db.upsert_rows(p, "social_tiktok", rows)
    db.log_fetch(p, "apify", "tiktok_search",
                 {"queries": qs, "max_items": args.max_items}, rows=n, duration_ms=t.ms)
    print(f"tiktok: {n} videos ({t.ms}ms)")


def cmd_fetch_reddit(args):
    p = db_path_for(args.topic)
    qs = _split(args.queries)
    subs = _split(args.subreddits)
    with db.Timer() as t:
        if hub_client.enabled():
            rows = hub_client.fetch("reddit", queries=qs, subreddits=subs or None, max_items=args.max_items)
        else:
            rows = apify.fetch_reddit_search(qs, subreddits=subs or None, max_items=args.max_items)
    n = db.upsert_rows(p, "social_reddit", rows)
    db.log_fetch(p, "apify", "reddit_search",
                 {"queries": qs, "subs": subs}, rows=n, duration_ms=t.ms)
    print(f"reddit: {n} posts ({t.ms}ms)")


def cmd_fetch_youtube(args):
    p = db_path_for(args.topic)
    qs = _split(args.queries)
    with db.Timer() as t:
        if hub_client.enabled():
            rows = hub_client.fetch("youtube", queries=qs, per_query=args.per_query)
        else:
            rows = youtube.fetch_videos_for_queries(qs, per_query=args.per_query)
    n = db.upsert_rows(p, "social_youtube", rows)
    db.log_fetch(p, "youtube", "search+videos", {"queries": qs}, rows=n, duration_ms=t.ms)
    print(f"youtube: {n} videos ({t.ms}ms)")


def cmd_ingest_ahrefs(args):
    p = db_path_for(args.topic)
    if args.file == "-":
        raw = json.load(sys.stdin)
    else:
        raw = ahrefs_ingest.load_json(args.file)
    if args.kind == "keywords":
        rows = ahrefs_ingest.normalize_keywords(raw, source_seed=args.source_seed,
                                                country=args.country)
        n = db.upsert_rows(p, "keywords_google", rows)
        db.log_fetch(p, "ahrefs", "keywords:" + (args.source_seed or "?"),
                     {"file": args.file}, rows=n)
    elif args.kind == "site-metrics":
        if not args.domain:
            print("ERROR: --domain required for site-metrics", file=sys.stderr); sys.exit(2)
        row = ahrefs_ingest.normalize_site_explorer_metrics(raw, domain=args.domain)
        n = db.upsert_rows(p, "competitors_web", [row]) if row else 0
        db.log_fetch(p, "ahrefs", "site_metrics", {"domain": args.domain}, rows=n)
    elif args.kind == "organic-kw":
        if not args.domain:
            print("ERROR: --domain required for organic-kw", file=sys.stderr); sys.exit(2)
        rows = ahrefs_ingest.normalize_organic_keywords(raw, domain=args.domain)
        n = db.upsert_rows(p, "competitor_organic_kw", rows)
        db.log_fetch(p, "ahrefs", "organic_kw", {"domain": args.domain}, rows=n)
    elif args.kind == "brand-radar":
        rows = ahrefs_ingest.normalize_brand_radar(raw, brand=args.brand)
        n = db.upsert_rows(p, "ai_visibility", rows)
        db.log_fetch(p, "ahrefs", "brand_radar", {"brand": args.brand}, rows=n)
    else:
        print(f"unknown kind: {args.kind}", file=sys.stderr); sys.exit(2)
    print(f"ingested {n} rows ({args.kind}) into {p}")


def cmd_fetch_ahrefs_kw(args):
    """Fetch Ahrefs keyword data via Hub (no MCP needed)."""
    if not hub_client.enabled():
        print("ERROR: CIA_HUB_URL not set. This command requires hub mode.", file=sys.stderr); sys.exit(2)
    p = db_path_for(args.topic)
    kws = _split(args.keywords)
    with db.Timer() as t:
        if args.matching:
            raw = hub_client.fetch("ahrefs_kw_matching", keyword=kws[0],
                                   country=args.country, limit=args.limit)
        else:
            raw = hub_client.fetch("ahrefs_kw_overview", keywords=kws,
                                   country=args.country)
    rows = ahrefs_ingest.normalize_keywords(raw, source_seed=",".join(kws), country=args.country)
    n = db.upsert_rows(p, "keywords_google", rows)
    db.log_fetch(p, "ahrefs_hub", "keywords", {"keywords": kws}, rows=n, duration_ms=t.ms)
    print(f"ahrefs kw: {n} rows ({t.ms}ms)")


def cmd_fetch_ahrefs_site(args):
    """Fetch Ahrefs site metrics + organic kw via Hub (no MCP needed)."""
    if not hub_client.enabled():
        print("ERROR: CIA_HUB_URL not set. This command requires hub mode.", file=sys.stderr); sys.exit(2)
    p = db_path_for(args.topic)
    targets = _split(args.targets)
    for target in targets:
        with db.Timer() as t:
            raw_metrics = hub_client.fetch("ahrefs_site_metrics", target=target, country=args.country)
        row = ahrefs_ingest.normalize_site_explorer_metrics(raw_metrics, domain=target)
        if row:
            db.upsert_rows(p, "competitors_web", [row])
        if args.organic_kw:
            with db.Timer() as t2:
                raw_orgkw = hub_client.fetch("ahrefs_organic_kw", target=target,
                                             country=args.country, limit=args.limit)
            rows_kw = ahrefs_ingest.normalize_organic_keywords(raw_orgkw, domain=target)
            n_kw = db.upsert_rows(p, "competitor_organic_kw", rows_kw)
            print(f"  {target}: metrics OK + {n_kw} organic kw ({t.ms}+{t2.ms}ms)")
        else:
            print(f"  {target}: metrics OK ({t.ms}ms)")
    db.log_fetch(p, "ahrefs_hub", "site_metrics", {"targets": targets}, rows=len(targets))


def cmd_social_to_seeds(args):
    """Extract seed keywords from high-engagement social content and save to seeds table."""
    p = db_path_for(args.topic)
    phrases = _social_seeds.extract_signal_phrases(
        p,
        min_tiktok_share_rate=args.min_share_rate,
        min_reddit_score=args.min_reddit_score,
        top_n=args.top_n,
    )
    _social_seeds.print_report(phrases, top=args.top_n)
    if args.dry_run:
        print(f"\n[dry-run] would save {sum(1 for ph in phrases if ph.count >= args.min_count)} phrases as seeds")
        return
    n = _social_seeds.save_to_seeds(p, phrases, min_count=args.min_count)
    db.log_fetch(p, "analysis", "social_to_seeds",
                 {"min_share_rate": args.min_share_rate, "min_reddit_score": args.min_reddit_score},
                 rows=n)
    print(f"\nSaved {n} social signal seeds (min_count={args.min_count})")


def cmd_discover_loop(args):
    """Run circular competitor discovery loop: App (ASO) + Web (Ahrefs organic-competitors)."""
    p = db_path_for(args.topic)
    _discover.run_loop(
        p,
        country=args.country,
        max_rounds=args.max_rounds,
        min_new_kw=args.min_new_kw,
        max_jaccard=args.max_jaccard,
        aso_limit=args.aso_limit,
        itunes_serp_limit=args.itunes_limit,
        max_apps_per_round=args.max_apps,
        max_domains_per_round=args.max_domains,
        budget_usd=args.budget,
        dry_run=args.dry_run,
    )


def cmd_cluster_competitors(args):
    """Cluster competitors (App + Web unified) using KMeans + TF-IDF on keywords."""
    p = db_path_for(args.topic)
    clusters = _cluster.cluster_competitors(
        p,
        n_clusters=args.n_clusters if args.n_clusters > 0 else None,
        max_k=args.max_k,
    )
    if not clusters:
        print("Not enough competitor data to cluster.")
        return
    _cluster.print_clusters(clusters)
    if args.dry_run:
        print(f"\n[dry-run] would save {len(clusters)} cluster assignments")
        return
    n = _cluster.save_clusters(p, clusters)
    db.log_fetch(p, "analysis", "cluster_competitors",
                 {"n_clusters": args.n_clusters, "max_k": args.max_k}, rows=n)
    print(f"\nSaved {n} cluster assignments")
    # Auto-assign social data to clusters
    print("Assigning social data to clusters...")
    n_social = _cluster.assign_social_to_clusters(p)
    print(f"Assigned {n_social} social items → social_cluster_map")


def cmd_assign_clusters(args):
    """Score and assign social data (TikTok/YouTube/Reddit) to clusters."""
    p = db_path_for(args.topic)
    n = _cluster.assign_social_to_clusters(p)
    print(f"Assigned {n} social items → social_cluster_map")


def cmd_define_tracks(args):
    """One-shot: LLM defines tracks + assigns competitors (no confirmation pause)."""
    p = db_path_for(args.topic)
    db._migrate(p)
    hints = [h.strip() for h in args.hints.split(",") if h.strip()] if args.hints else None
    _define_tracks.run(p, n=args.n, hints=hints)
    print("\nAssigning social data to tracks...")
    n_social = _cluster.assign_social_to_clusters(p)
    print(f"Assigned {n_social} social items → social_cluster_map")


def cmd_propose_tracks(args):
    """Phase 1: LLM proposes tracks → saves proposed_tracks.json for review. Does NOT write DB."""
    p = db_path_for(args.topic)
    db._migrate(p)
    hints = [h.strip() for h in args.hints.split(",") if h.strip()] if args.hints else None
    _define_tracks.propose(p, n=args.n, hints=hints)


def cmd_apply_tracks(args):
    """Phase 2: Read confirmed proposed_tracks.json → write DB → assign competitors + social."""
    p = db_path_for(args.topic)
    _define_tracks.apply(p)
    if args.fetch_svs:
        _define_tracks.fetch_missing_svs(p)
    print("\nAssigning social data to tracks...")
    n_social = _cluster.assign_social_to_clusters(p)
    print(f"Assigned {n_social} social items → social_cluster_map")


def cmd_export(args):
    p = db_path_for(args.topic)
    d = topic_dir(args.topic)
    xlsx = d / "data.xlsx"
    htmlp = d / "report.html"
    counts = excel.export(p, xlsx)
    synthesis = None
    if args.synthesis_file:
        synthesis = pathlib.Path(args.synthesis_file).read_text()
    n = html_exp.render(p, htmlp, synthesis=synthesis)
    print(f"xlsx: {xlsx}")
    print(f"html: {htmlp}  (total {n} rows across {len(counts)} sections)")
    for sheet, c in counts.items():
        print(f"  {sheet:32s}  {c:>5}")


def cmd_export_briefs(args):
    p = db_path_for(args.topic)
    out = briefs_exp.export_briefs(
        p,
        min_items=args.min_items,
        top_tt=args.top_tt,
        top_yt=args.top_yt,
    )
    print(f"\nOutput dir: {out}")


def cmd_seed_save(args):
    """Save seed keywords (with dimension/side metadata) to DB."""
    p = db_path_for(args.topic)
    rows = []
    for line in pathlib.Path(args.file).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Format:  side|dimension|keyword  e.g.  demand|core|ai receptionist
        parts = line.split("|")
        if len(parts) == 3:
            side, dim, kw = parts
        elif len(parts) == 2:
            side, kw = parts; dim = "core"
        else:
            side = "demand"; dim = "core"; kw = line
        rows.append({"keyword": kw.strip(), "dimension": dim.strip(), "side": side.strip()})
    n = db.upsert_rows(p, "seeds", rows, on_conflict="IGNORE")
    print(f"saved {n} seeds")


# ---------- LLM helpers ----------

_SEED_EXPAND_PROMPT = """\
You are a market research analyst. Expand search seeds for competitive analysis of: "{topic}" (country: {country}).

Generate a keyword seed list across 20 market dimensions. Return ONLY a valid JSON array:
[
  {{"dim": "demand|core", "keywords": ["kw1", "kw2"]}},
  ...
]

Required dim values and guidance:
demand|core        - Core action/problem keywords (4-8 kws)
demand|audience    - Who uses this: job/role/industry segments (3-6 kws)
demand|scenario    - When/where needed: trigger scenarios (3-5 kws)
demand|pain        - Pain point phrases: "how to", "best way to" (3-6 kws)
demand|self_describe - How users describe their own need in plain language (3-5 kws)
demand|adjacent    - Adjacent categories this user also searches (3-5 kws)
demand|platform    - Platform-specific: "for iPhone", "for small business" (2-4 kws)
supply|solutions   - Existing solution types (3-6 kws)
supply|purchase    - Purchase intent: "pricing", "plans", "vs", "alternative" (3-6 kws)
supply|channels    - Discovery: "best X", "X review", "X tutorial" (3-5 kws)
supply|traditional - Traditional/offline alternatives being displaced (3-5 kws)
supply|complaints  - Common complaints about existing solutions (3-5 kws)
gap|ignored_audience - Underserved segments: regions/industries/roles (3-5 kws)
gap|scenario       - Scenarios no product serves well yet (2-4 kws)
gap|geo            - Geographic/language variations, non-US markets (2-4 kws)
gap|feature        - Missing features users ask for (2-4 kws)
gap|pricing        - Pricing gaps: "free", "cheap", "affordable" (2-4 kws)
action|channel     - GTM signals: "template", "tutorial", "how to start" (2-4 kws)
action|monetize    - Monetization signals: "pricing", "enterprise", "API" (2-4 kws)
action|timing      - Market timing: new entrants, recent trend keywords (2-4 kws)

Rules: all keywords lowercase, 1-4 words each, 50-80 total across all dimensions.
"{topic}" seeds ONLY demand|core; derive all other dims independently from market knowledge.
{hints_line}
"""

_SYNTHESIZE_PROMPT = """\
You are a CIA-style market analyst. Write a synthesis report for the CIA market intelligence tool.
Follow this EXACT structure (v4 anti-bias template):

# 一、赛道全景（按 total_reviews 降序排列）
| # | 赛道名 | Apps数 | 总Reviews | 估算TAM | PLG得分 | 与用户方向关系 |

# 二、Top {n_tracks} 赛道详细卡片
For each track include:
## Track N: <name> (<name_en>)
### 传播侧 — top social hooks with share_rate/score
### 需求侧 — top keywords with vol/SV/KD/CPC
### 供给侧 — top 3 competitors with reviews + pricing
### PLG 体检 | TTV(s) | setup_cost | viral_loop | sales_dep | PLG得分 |
### 切入角度 (≤2条, based on review pain points or ASO gaps)

# 三、PLG 全赛道对照矩阵

# 四、传播侧跨赛道洞察 (cross-track TikTok/Reddit hook patterns)

# 五、跨赛道组合策略
## 5.1 单赛道推荐（by resource constraints）
## 5.2 双赛道最高ROI组合
## 5.3 3/6/12个月分阶段路线

# 六、对用户原始假设的批判性评估 ★
| 用户假设 | 数据排名 | 与最大赛道TAM差距 | 反向证据 | 建议 |
（Must state: "你假设的赛道在全体中排第几"）

# 七、行动建议（数据支持推翻时必须推翻，不允许hedge）

Anti-bias rules:
- Sort all tracks by total_reviews, NOT by topic relevance
- User's original topic goes in chapter 六, NOT chapter 一 position 1 (unless data supports it)
- Every PLG scorecard must have TTV/setup_cost/viral_loop/sales_dep

Here is the market data:

## Topic: {topic}

## Tracks (sorted by total reviews):
{tracks_summary}

## Top Google Keywords (golden: vol≥200, KD≤35, CPC≥1):
{google_kw}

## App Store Blue Ocean Keywords (SV≥500, ≤3 apps):
{aso_blue_ocean}

## Top Social Content:
{social_summary}

## Competitor App Pain Points (low-rated reviews):
{pain_points}
"""


def _call_llm(messages: list[dict], model: str = "claude-haiku-4-5-20251001",
              max_tokens: int = 5000) -> str:
    """Call LLM via Hub if available, otherwise direct Anthropic."""
    if hub_client.enabled():
        return hub_client.llm(messages, model=model, max_tokens=max_tokens)
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("pip install anthropic  (or configure CIA_HUB_URL)")
    api_key = config.get("ANTHROPIC_AUTH_TOKEN") or config.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_AUTH_TOKEN not set (or configure CIA_HUB_URL)")
    base_url = config.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
    msg = client.messages.create(model=model, max_tokens=max_tokens, messages=messages)
    return msg.content[0].text.strip()


def cmd_expand_seeds(args):
    """LLM-powered 20-dimension seed keyword expansion → saves to seeds table."""
    import json as _json, re as _re
    p = db_path_for(args.topic)
    hints_line = f"User hints (include if market data supports): {args.hints}" if args.hints else ""
    prompt = _SEED_EXPAND_PROMPT.format(
        topic=args.topic, country=args.country, hints_line=hints_line)
    print(f"Expanding seeds for '{args.topic}' via LLM...")
    raw = _call_llm([{"role": "user", "content": prompt}])
    m = _re.search(r'\[.*\]', raw, _re.DOTALL)
    if not m:
        print(f"ERROR: LLM did not return JSON array.\n{raw[:300]}", file=sys.stderr); sys.exit(1)
    items = _json.loads(m.group(0))
    rows = []
    for item in items:
        dim = item.get("dim", "demand|core")
        parts = dim.split("|", 1)
        side = parts[0] if len(parts) == 2 else "demand"
        dimension = parts[1] if len(parts) == 2 else dim
        for kw in item.get("keywords", []):
            kw = kw.strip().lower()
            if kw:
                rows.append({"keyword": kw, "dimension": dimension, "side": side})
    n = db.upsert_rows(p, "seeds", rows, on_conflict="IGNORE")
    print(f"Saved {n} seed keywords across {len(items)} dimensions")


def cmd_synthesize(args):
    """LLM-powered synthesis report → writes synthesis.md to report dir."""
    import json as _json
    p = db_path_for(args.topic)
    d = topic_dir(args.topic)

    # Pull data from DB
    tracks_df = db.query_df(p, """
        SELECT t.name, t.name_en, t.description,
               COUNT(DISTINCT cc.competitor_id) AS n_apps,
               SUM(ca.review_count) AS total_reviews,
               AVG(ca.rating) AS avg_rating
        FROM tracks t
        LEFT JOIN competitor_clusters cc ON cc.cluster_id = t.track_id
        LEFT JOIN competitors_app ca ON ca.app_id = cc.competitor_id AND cc.competitor_type='app'
        GROUP BY t.track_id ORDER BY total_reviews DESC NULLS LAST
    """)

    gkw_df = db.query_df(p, """
        SELECT keyword, volume, kd, cpc_usd, intent FROM keywords_google
        WHERE volume>=200 AND kd<=35 AND cpc_usd>=1
        ORDER BY volume DESC LIMIT 20
    """)

    aso_blue_df = db.query_df(p, """
        SELECT keyword, MAX(search_volume) AS sv, COUNT(DISTINCT app_id) AS n
        FROM appstore_keywords WHERE search_volume>=500
        GROUP BY keyword HAVING n<=3 ORDER BY sv DESC LIMIT 20
    """)

    social_df = db.query_df(p, """
        SELECT * FROM (
            SELECT 'tiktok' AS src, text AS content, plays AS score,
                   ROUND(CAST(shares AS REAL)/plays, 5) AS share_rate
            FROM social_tiktok WHERE plays>1000
            ORDER BY share_rate DESC LIMIT 8
        )
        UNION ALL
        SELECT * FROM (
            SELECT 'reddit', title, score, 0 FROM social_reddit
            ORDER BY score DESC LIMIT 5
        )
    """)

    pain_df = db.query_df(p, """
        SELECT body FROM app_reviews WHERE rating<=3 AND length(body)>50
        ORDER BY posted_at DESC LIMIT 20
    """)

    if tracks_df.empty:
        print("ERROR: No tracks defined. Run propose-tracks + apply-tracks first.", file=sys.stderr)
        sys.exit(1)

    def _nz(v):
        try:
            return 0 if v is None or (isinstance(v, float) and v != v) else int(v)
        except Exception:
            return 0

    tracks_summary = "\n".join(
        f"  {i+1}. {r['name']} ({r.get('name_en','')}) — {_nz(r.get('total_reviews')):,} reviews, "
        f"{_nz(r.get('n_apps'))} apps"
        for i, (_, r) in enumerate(tracks_df.iterrows())
    )
    google_kw = "\n".join(
        f"  {r['keyword']} vol={r.get('volume',0)} KD={r.get('kd','?')} CPC=${r.get('cpc_usd',0):.2f}"
        for _, r in gkw_df.iterrows()
    ) or "  (no golden keywords yet)"
    aso_blue = "\n".join(
        f"  {r['keyword']} SV={int(r.get('sv') or 0)} {int(r.get('n') or 0)} apps"
        for _, r in aso_blue_df.iterrows()
    ) or "  (no blue ocean ASO keywords yet)"
    social_summary = "\n".join(
        f"  [{r.get('src','?')}] {str(r.get('content',''))[:100]} (score={r.get('score',0)})"
        for _, r in social_df.iterrows()
    ) or "  (no social data yet)"
    pain_pts = "\n".join(
        f"  - {str(r.get('body',''))[:120]}"
        for _, r in pain_df.iterrows()
    ) or "  (no review data yet)"

    prompt = _SYNTHESIZE_PROMPT.format(
        topic=args.topic,
        n_tracks=min(7, len(tracks_df)),
        tracks_summary=tracks_summary,
        google_kw=google_kw,
        aso_blue_ocean=aso_blue,
        social_summary=social_summary,
        pain_points=pain_pts,
    )
    print(f"Generating synthesis report for '{args.topic}' via LLM (Sonnet)...")
    raw = _call_llm(
        [{"role": "user", "content": prompt}],
        model="claude-sonnet-4-6",
        max_tokens=8000,
    )
    out = d / "synthesis.md"
    out.write_text(raw, encoding="utf-8")
    print(f"synthesis.md written to: {out}")
    return out


# ---------- Pipeline orchestrator ----------

def _step(name: str) -> None:
    print(f"\n{'='*56}\n  [{name}]\n{'='*56}")


def _ns(**kwargs):
    """Build a minimal argparse.Namespace for calling cmd_* functions."""
    import argparse as _ap
    return _ap.Namespace(**kwargs)


def cmd_run(args):
    """Full pipeline: init → fetch → discover → propose tracks → apply → export → synthesize."""
    topic = args.topic
    country = args.country
    n_tracks = args.n_tracks
    hints = args.hints or ""

    print(f"\nCIA Pipeline — topic='{topic}' country={country}")
    if args.no_ahrefs:
        print("  [--no-ahrefs] Ahrefs steps will be skipped")
    if args.no_social:
        print("  [--no-social] Social fetch steps will be skipped")

    dbp = db_path_for(topic)

    # Step 0: init
    _step("0 init")
    cmd_init(_ns(topic=topic, country=country))

    # Step 1: LLM seed expansion
    if not args.no_seeds:
        _step("1 expand-seeds (LLM)")
        cmd_expand_seeds(_ns(topic=topic, country=country, hints=hints))

    # Step 2: Social fetch (TikTok + Reddit)
    if not args.no_social:
        seed_kws = db.query_df(dbp, "SELECT keyword FROM seeds LIMIT 20")
        kw_str = ",".join(seed_kws["keyword"].tolist()[:8]) if not seed_kws.empty else topic
        _step("2a fetch-tiktok")
        cmd_fetch_tiktok(_ns(topic=topic, queries=kw_str, max_items=80))
        _step("2b fetch-reddit")
        cmd_fetch_reddit(_ns(topic=topic, queries=kw_str, subreddits="", max_items=60))
        _step("2c social-to-seeds")
        cmd_social_to_seeds(_ns(topic=topic, min_share_rate=0.005, min_reddit_score=10,
                                top_n=80, min_count=3, dry_run=False))

    # Step 3: Ahrefs keyword fetch (seed keywords)
    if not args.no_ahrefs and hub_client.enabled():
        _step("3 fetch-ahrefs-kw (seed keywords)")
        seed_kws = db.query_df(dbp, "SELECT keyword FROM seeds WHERE side='demand' LIMIT 12")
        if not seed_kws.empty:
            kw_str = ",".join(seed_kws["keyword"].tolist()[:12])
            cmd_fetch_ahrefs_kw(_ns(topic=topic, keywords=kw_str, country=country,
                                    limit=100, matching=False))
    elif not args.no_ahrefs:
        print("  [skip] Ahrefs requires CIA_HUB_URL to be set")

    # Step 4: iTunes SERP
    _step("4 fetch-itunes-serp")
    seed_kws = db.query_df(dbp, "SELECT keyword FROM seeds WHERE side='demand' LIMIT 8")
    kw_str = ",".join(seed_kws["keyword"].tolist()[:8]) if not seed_kws.empty else topic
    cmd_fetch_itunes_serp(_ns(topic=topic, keywords=kw_str, country=country, limit=20))

    # Step 5: Competitor metadata
    _step("5 fetch-competitors-meta")
    cmd_fetch_competitors_meta(_ns(topic=topic, app_ids="", country=country))

    # Step 6: Discovery loop (App + Web)
    _step("6 discover-loop (max 3 rounds)")
    cmd_discover_loop(_ns(topic=topic, country=country, max_rounds=3, min_new_kw=30,
                          max_jaccard=0.70, aso_limit=200, itunes_limit=20,
                          max_apps=20, max_domains=10, budget=5.0, dry_run=False))

    # Step 7: App reviews (top 5)
    _step("7 fetch-app-reviews (top 5)")
    cmd_fetch_app_reviews(_ns(topic=topic, app_ids="", top=5, depth=200))

    # Step 8: YouTube
    if not args.no_social:
        _step("8 fetch-youtube")
        seed_kws = db.query_df(dbp, "SELECT keyword FROM seeds WHERE side='demand' LIMIT 4")
        kw_str = ",".join(seed_kws["keyword"].tolist()[:4]) if not seed_kws.empty else topic
        cmd_fetch_youtube(_ns(topic=topic, queries=kw_str, per_query=15))

    # Step 9: Ahrefs site metrics for discovered web competitors
    if not args.no_ahrefs and hub_client.enabled():
        _step("9 fetch-ahrefs-site (web competitors)")
        web_df = db.query_df(dbp, "SELECT domain FROM competitors_web LIMIT 15")
        if not web_df.empty:
            targets = ",".join(web_df["domain"].dropna().tolist()[:15])
            cmd_fetch_ahrefs_site(_ns(topic=topic, targets=targets, country=country,
                                      limit=100, organic_kw=True))

    # Step 10: Status
    _step("10 status")
    cmd_status(_ns(topic=topic))

    # PAUSE: propose tracks → human review → apply
    _step("10.5 propose-tracks (LLM)")
    cmd_propose_tracks(_ns(topic=topic, n=n_tracks, hints=hints))

    proposal_path = topic_dir(topic) / "proposed_tracks.json"
    print(f"\nReview and edit the track proposal at:\n  {proposal_path}")
    print("\nYou can edit track names, keywords, or reorder tracks.")
    try:
        input("\n[Press ENTER to apply tracks, or Ctrl+C to abort] ")
    except KeyboardInterrupt:
        print("\nAborted. Re-run 'cia apply-tracks --topic \"...\"' when ready.")
        return

    _step("11 apply-tracks")
    cmd_apply_tracks(_ns(topic=topic, fetch_svs=False))

    # Step 12: Export HTML + xlsx
    _step("12 export")
    cmd_export(_ns(topic=topic, synthesis_file=None))

    # Step 13: LLM synthesis → re-export with synthesis embedded
    _step("13 synthesize (LLM)")
    synth_out = cmd_synthesize(_ns(topic=topic))
    if synth_out and synth_out.exists():
        _step("13b re-export with synthesis")
        cmd_export(_ns(topic=topic, synthesis_file=str(synth_out)))

    print(f"\n{'='*56}")
    print(f"  CIA Pipeline COMPLETE")
    print(f"  Report dir: {topic_dir(topic)}")
    print(f"{'='*56}\n")


# ---------- argparse wiring ----------

def main():
    ap = argparse.ArgumentParser(prog="cia")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init"); sp.add_argument("topic"); sp.add_argument("--country", default="us")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("status"); sp.add_argument("--topic", required=True)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("seed-save"); sp.add_argument("--topic", required=True); sp.add_argument("--file", required=True)
    sp.set_defaults(func=cmd_seed_save)

    sp = sub.add_parser("fetch-itunes-serp"); sp.add_argument("--topic", required=True)
    sp.add_argument("--keywords", required=True); sp.add_argument("--country", default="us")
    sp.add_argument("--limit", type=int, default=20); sp.set_defaults(func=cmd_fetch_itunes_serp)

    sp = sub.add_parser("fetch-gplay-serp"); sp.add_argument("--topic", required=True)
    sp.add_argument("--keywords", required=True); sp.add_argument("--country", default="us")
    sp.add_argument("--limit", type=int, default=20); sp.set_defaults(func=cmd_fetch_gplay_serp)

    sp = sub.add_parser("fetch-gplay-meta"); sp.add_argument("--topic", required=True)
    sp.add_argument("--app-ids", default=""); sp.add_argument("--country", default="us")
    sp.add_argument("--top", type=int, default=50); sp.set_defaults(func=cmd_fetch_gplay_meta)

    sp = sub.add_parser("fetch-competitors-meta"); sp.add_argument("--topic", required=True)
    sp.add_argument("--app-ids", default=""); sp.add_argument("--country", default="us")
    sp.set_defaults(func=cmd_fetch_competitors_meta)

    sp = sub.add_parser("fetch-aso-keywords"); sp.add_argument("--topic", required=True)
    sp.add_argument("--app-ids", default=""); sp.add_argument("--top", type=int, default=20)
    sp.add_argument("--limit", type=int, default=200)
    sp.add_argument("--include-categories", default="",
                    help="Comma list of App Store categories to whitelist when auto-picking, e.g. 'Business,Productivity,Utilities'")
    sp.set_defaults(func=cmd_fetch_aso_keywords)

    sp = sub.add_parser("fetch-app-reviews"); sp.add_argument("--topic", required=True)
    sp.add_argument("--app-ids", default=""); sp.add_argument("--top", type=int, default=5)
    sp.add_argument("--depth", type=int, default=200); sp.set_defaults(func=cmd_fetch_app_reviews)

    sp = sub.add_parser("social-to-seeds",
                        help="Extract seed keywords from high-engagement TikTok/Reddit content")
    sp.add_argument("--topic", required=True)
    sp.add_argument("--min-share-rate", type=float, default=0.005,
                    help="TikTok shares/plays threshold (default 0.005 = 0.5%%)")
    sp.add_argument("--min-reddit-score", type=int, default=10)
    sp.add_argument("--top-n", type=int, default=80)
    sp.add_argument("--min-count", type=int, default=3,
                    help="Min phrase occurrence before saving to seeds")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_social_to_seeds)

    sp = sub.add_parser("discover-loop",
                        help="Circular discovery: App (ASO) + Web (Ahrefs organic-competitors)")
    sp.add_argument("--topic", required=True)
    sp.add_argument("--country", default="us")
    sp.add_argument("--max-rounds", type=int, default=3)
    sp.add_argument("--min-new-kw", type=int, default=30)
    sp.add_argument("--max-jaccard", type=float, default=0.70)
    sp.add_argument("--aso-limit", type=int, default=200)
    sp.add_argument("--itunes-limit", type=int, default=20)
    sp.add_argument("--max-apps", type=int, default=20)
    sp.add_argument("--max-domains", type=int, default=10,
                    help="Max new web domains to process per round (default 10)")
    sp.add_argument("--budget", type=float, default=5.0,
                    help="Max spend in USD before stopping (default $5)")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_discover_loop)

    sp = sub.add_parser("assign-to-clusters",
                        help="Score TikTok/YouTube/Reddit against cluster keywords → write cluster_id")
    sp.add_argument("--topic", required=True)
    sp.set_defaults(func=cmd_assign_clusters)

    sp = sub.add_parser("cluster-competitors",
                        help="Cluster competitors by profile (KMeans on review/category/ASO keywords)")
    sp.add_argument("--topic", required=True)
    sp.add_argument("--n-clusters", type=int, default=0,
                    help="Force k; 0 = auto-detect via elbow (default)")
    sp.add_argument("--max-k", type=int, default=8)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_cluster_competitors)

    sp = sub.add_parser("define-tracks",
                        help="One-shot: LLM defines tracks + assigns (no confirmation pause)")
    sp.add_argument("--topic", required=True)
    sp.add_argument("--n", type=int, default=6, help="Number of tracks (default 6)")
    sp.add_argument("--hints", default="",
                    help="Comma-separated track names user expects, e.g. '实时翻译类,外呼类'")
    sp.set_defaults(func=cmd_define_tracks)

    sp = sub.add_parser("propose-tracks",
                        help="Phase 1: LLM proposes tracks → saves JSON for review (no DB write)")
    sp.add_argument("--topic", required=True)
    sp.add_argument("--n", type=int, default=6, help="Number of tracks (default 6)")
    sp.add_argument("--hints", default="",
                    help="Comma-separated tracks user expects, e.g. '实时翻译类,外呼类'")
    sp.set_defaults(func=cmd_propose_tracks)

    sp = sub.add_parser("apply-tracks",
                        help="Phase 2: Commit proposed_tracks.json to DB + assign competitors")
    sp.add_argument("--topic", required=True)
    sp.add_argument("--fetch-svs", action="store_true",
                    help="Fetch missing SV for track keywords via iTunes+DataForSEO (~$0.10)")
    sp.set_defaults(func=cmd_apply_tracks)

    sp = sub.add_parser("fetch-tiktok"); sp.add_argument("--topic", required=True)
    sp.add_argument("--queries", required=True); sp.add_argument("--max-items", type=int, default=80)
    sp.set_defaults(func=cmd_fetch_tiktok)

    sp = sub.add_parser("fetch-reddit"); sp.add_argument("--topic", required=True)
    sp.add_argument("--queries", required=True); sp.add_argument("--subreddits", default="")
    sp.add_argument("--max-items", type=int, default=60); sp.set_defaults(func=cmd_fetch_reddit)

    sp = sub.add_parser("fetch-youtube"); sp.add_argument("--topic", required=True)
    sp.add_argument("--queries", required=True); sp.add_argument("--per-query", type=int, default=15)
    sp.set_defaults(func=cmd_fetch_youtube)

    sp = sub.add_parser("ingest-ahrefs"); sp.add_argument("--topic", required=True)
    sp.add_argument("--kind", required=True,
                    choices=["keywords", "site-metrics", "organic-kw", "brand-radar"])
    sp.add_argument("--file", required=True); sp.add_argument("--source-seed", default=None)
    sp.add_argument("--country", default="us"); sp.add_argument("--domain", default=None)
    sp.add_argument("--brand", default=None); sp.set_defaults(func=cmd_ingest_ahrefs)

    sp = sub.add_parser("fetch-ahrefs-kw", help="Fetch Ahrefs keywords via Hub")
    sp.add_argument("--topic", required=True); sp.add_argument("--keywords", required=True)
    sp.add_argument("--country", default="us"); sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--matching", action="store_true", help="Use matching-terms instead of overview")
    sp.set_defaults(func=cmd_fetch_ahrefs_kw)

    sp = sub.add_parser("fetch-ahrefs-site", help="Fetch Ahrefs site metrics via Hub")
    sp.add_argument("--topic", required=True); sp.add_argument("--targets", required=True)
    sp.add_argument("--country", default="us"); sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--organic-kw", action="store_true", help="Also fetch organic keywords")
    sp.set_defaults(func=cmd_fetch_ahrefs_site)

    sp = sub.add_parser("export"); sp.add_argument("--topic", required=True)
    sp.add_argument("--synthesis-file", default=None); sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("export-briefs", help="Export per-track content briefs for mobile-opr")
    sp.add_argument("--topic", required=True)
    sp.add_argument("--min-items", type=int, default=3, help="Skip tracks with fewer total social items")
    sp.add_argument("--top-tt", type=int, default=15, help="Top N TikTok videos per track")
    sp.add_argument("--top-yt", type=int, default=10, help="Top N YouTube videos per track")
    sp.set_defaults(func=cmd_export_briefs)

    sp = sub.add_parser("expand-seeds",
                        help="LLM 20-dimension seed expansion → saves to seeds table")
    sp.add_argument("--topic", required=True)
    sp.add_argument("--country", default="us")
    sp.add_argument("--hints", default="",
                    help="Comma-separated user hints, e.g. '实时翻译类,外呼类'")
    sp.set_defaults(func=cmd_expand_seeds)

    sp = sub.add_parser("synthesize",
                        help="LLM synthesis report → writes synthesis.md to report dir")
    sp.add_argument("--topic", required=True)
    sp.set_defaults(func=cmd_synthesize)

    sp = sub.add_parser("run",
                        help="Full pipeline: init→fetch→discover→propose tracks→apply→export→synthesize")
    sp.add_argument("topic")
    sp.add_argument("--country", default="us")
    sp.add_argument("--n-tracks", type=int, default=8, help="Number of tracks for LLM to propose (default 8)")
    sp.add_argument("--hints", default="",
                    help="Comma-separated track hints, e.g. '实时翻译类,外呼类'")
    sp.add_argument("--no-ahrefs", action="store_true", help="Skip Ahrefs steps (saves cost)")
    sp.add_argument("--no-social", action="store_true", help="Skip TikTok/Reddit/YouTube fetch")
    sp.add_argument("--no-seeds", action="store_true", help="Skip LLM seed expansion (use existing seeds)")
    sp.set_defaults(func=cmd_run)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
