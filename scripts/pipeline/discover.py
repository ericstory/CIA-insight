"""Circular competitor discovery loop.

Each round:
  1. Get un-fetched apps from appstore_serp (no entry in appstore_keywords yet)
  2. Fetch their ASO keywords (DataForSEO)
  3. Extract new keywords not yet in the keyword pool
  4. Run iTunes SERP for new keywords → may surface new competitors
  5. Compute Jaccard similarity of new vs existing keyword set
  6. Stop if: new_kw < min_new_kw  OR  Jaccard > max_jaccard  OR  round >= max_rounds

Usage:
    from pipeline.discover import run_loop
    stats = run_loop(db_path, country="us", max_rounds=3, min_new_kw=50, max_jaccard=0.70,
                     aso_limit=200, itunes_serp_limit=20, dry_run=False)
"""
from __future__ import annotations

import sys
import pathlib
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import db
from fetchers import dataforseo, itunes


@dataclass
class RoundStats:
    round_num: int
    new_apps: int
    aso_fetched: int
    new_keywords: int
    total_keywords: int
    jaccard: float
    cost_usd: float
    stop_reason: str = ""


@dataclass
class LoopStats:
    rounds: list[RoundStats] = field(default_factory=list)
    total_cost: float = 0.0
    stop_reason: str = ""

    def print_summary(self) -> None:
        print(f"\n{'RND':>3}  {'NEW_APPS':>8}  {'ASO_FETCH':>9}  {'NEW_KW':>7}  "
              f"{'TOTAL_KW':>9}  {'JACCARD':>7}  {'COST':>7}  NOTE")
        print("-" * 75)
        for r in self.rounds:
            print(f"{r.round_num:>3}  {r.new_apps:>8}  {r.aso_fetched:>9}  "
                  f"{r.new_keywords:>7}  {r.total_keywords:>9}  {r.jaccard:>7.3f}  "
                  f"${r.cost_usd:>6.4f}  {r.stop_reason}")
        print(f"\nTotal cost: ${self.total_cost:.4f}  Stop: {self.stop_reason}")


def _existing_keyword_set(db_path: pathlib.Path) -> set[str]:
    """All keywords currently in the pool: seeds + google + ASO."""
    rows = db.query_df(db_path, "SELECT LOWER(keyword) AS kw FROM seeds")
    kws: set[str] = set(rows["kw"].tolist())
    for tbl in ("keywords_google", "appstore_keywords"):
        try:
            r = db.query_df(db_path, f"SELECT LOWER(keyword) AS kw FROM {tbl}")
            kws.update(r["kw"].tolist())
        except Exception:
            pass
    return kws


def _unfetched_apps(db_path: pathlib.Path, limit: int) -> list[str]:
    """App IDs present in appstore_serp but not yet in appstore_keywords."""
    df = db.query_df(db_path, """
        SELECT DISTINCT s.app_id
        FROM appstore_serp s
        LEFT JOIN (
            SELECT DISTINCT app_id FROM appstore_keywords
        ) k ON k.app_id = s.app_id
        WHERE k.app_id IS NULL AND s.app_id != ''
        LIMIT ?
    """, (limit,))
    return df["app_id"].tolist()


def _jaccard(set_a: set[str], set_b: set[str]) -> float:
    if not set_a and not set_b:
        return 1.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union else 1.0


def run_loop(
    db_path: pathlib.Path,
    *,
    country: str = "us",
    max_rounds: int = 3,
    min_new_kw: int = 50,
    max_jaccard: float = 0.70,
    aso_limit: int = 200,
    itunes_serp_limit: int = 20,
    max_apps_per_round: int = 20,
    dry_run: bool = False,
) -> LoopStats:
    """Run the discovery loop. Returns stats for each round."""
    stats = LoopStats()

    for round_num in range(1, max_rounds + 1):
        print(f"\n── Discovery round {round_num}/{max_rounds} ──────────────────────────")

        existing_kws = _existing_keyword_set(db_path)
        unfetched = _unfetched_apps(db_path, max_apps_per_round)

        if not unfetched:
            print("  No new apps to fetch ASO for — stopping.")
            stats.stop_reason = "no_new_apps"
            break

        print(f"  Found {len(unfetched)} apps without ASO keywords yet")

        round_cost = 0.0
        new_aso_kws: set[str] = set()

        if not dry_run:
            for aid in unfetched:
                try:
                    rows, cost = dataforseo.fetch_keywords_for_app(aid, limit=aso_limit)
                    # Hydrate app_name
                    nm_df = db.query_df(db_path,
                        "SELECT name FROM competitors_app WHERE app_id=? UNION "
                        "SELECT app_name AS name FROM appstore_serp WHERE app_id=? LIMIT 1",
                        (aid, aid))
                    nm = nm_df.iloc[0]["name"] if len(nm_df) else None
                    for r in rows:
                        r["app_name"] = nm
                    db.upsert_rows(db_path, "appstore_keywords", rows)
                    db.log_fetch(db_path, "dataforseo", "keywords_for_app/loop",
                                 {"app_id": aid, "round": round_num}, rows=len(rows), cost_usd=cost)
                    new_aso_kws.update(r["keyword"].lower() for r in rows if r.get("keyword"))
                    round_cost += cost
                    print(f"    {aid}: {len(rows)} kw  ${cost:.4f}")
                except Exception as e:
                    print(f"    {aid}: ERROR {e}")
        else:
            print(f"  [dry-run] would fetch {len(unfetched)} apps")

        # Determine truly new keywords (not in existing set before this round)
        new_only = new_aso_kws - existing_kws
        jaccard = _jaccard(new_aso_kws, existing_kws)
        updated_kws = existing_kws | new_aso_kws
        print(f"  New keywords: {len(new_only)}  Jaccard vs pool: {jaccard:.3f}")

        # Save new keywords as seeds for next SERP round
        if new_only and not dry_run:
            seed_rows = [
                {"keyword": kw, "dimension": "aso_loop", "side": "demand"}
                for kw in list(new_only)[:200]  # cap to avoid seed explosion
            ]
            db.upsert_rows(db_path, "seeds", seed_rows, on_conflict="IGNORE")

        # Run iTunes SERP for top new keywords (by search_volume if available)
        if new_only and not dry_run:
            # Prefer keywords that already have a search_volume in appstore_keywords
            sv_df = db.query_df(db_path, """
                SELECT keyword, MAX(search_volume) AS sv
                FROM appstore_keywords
                WHERE LOWER(keyword) IN ({})
                GROUP BY keyword
                ORDER BY sv DESC NULLS LAST LIMIT ?
            """.format(",".join("?" * min(len(new_only), 100)),
                       itunes_serp_limit),
                tuple(list(new_only)[:100]) + (itunes_serp_limit,)
            )
            top_new_kws = sv_df["keyword"].tolist() if len(sv_df) else list(new_only)[:itunes_serp_limit]
            if top_new_kws:
                serp_rows = itunes.keyword_to_apps_bulk(top_new_kws, country=country,
                                                        limit=itunes_serp_limit)
                n_serp = db.upsert_rows(db_path, "appstore_serp", serp_rows)
                db.log_fetch(db_path, "itunes", "keyword_to_apps_bulk/loop",
                             {"round": round_num, "n_kw": len(top_new_kws)}, rows=n_serp)
                print(f"  iTunes SERP: {n_serp} rows from {len(top_new_kws)} new keywords")

        stop_reason = ""
        if len(new_only) < min_new_kw:
            stop_reason = f"new_kw={len(new_only)}<{min_new_kw}"
        elif jaccard > max_jaccard:
            stop_reason = f"jaccard={jaccard:.3f}>{max_jaccard}"

        r_stats = RoundStats(
            round_num=round_num,
            new_apps=len(unfetched),
            aso_fetched=len(unfetched) if not dry_run else 0,
            new_keywords=len(new_only),
            total_keywords=len(updated_kws),
            jaccard=jaccard,
            cost_usd=round_cost,
            stop_reason=stop_reason,
        )
        stats.rounds.append(r_stats)
        stats.total_cost += round_cost

        if stop_reason:
            stats.stop_reason = stop_reason
            print(f"  Stopping: {stop_reason}")
            break

    if not stats.stop_reason:
        stats.stop_reason = f"max_rounds={max_rounds}"

    stats.print_summary()
    return stats
