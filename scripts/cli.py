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
from export import excel, html as html_exp
from analysis import social_seeds as _social_seeds
from analysis import cluster as _cluster
from analysis import define_tracks as _define_tracks
from pipeline import discover as _discover


REPORTS_BASE = pathlib.Path.home() / "workspace/analytics/reports"


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
        df = db.query_df(p, "SELECT DISTINCT app_id FROM appstore_serp WHERE app_id != ''")
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

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
