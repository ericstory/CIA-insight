"""Circular competitor discovery loop — Web + App unified.

Each round runs TWO parallel legs:

  App leg  (iTunes + DataForSEO):
    unfetched apps → ASO keywords → new keywords → iTunes SERP → new apps

  Web leg  (Ahrefs):
    unfetched domains → organic-competitors → new domains
    new domains → organic-keywords → new keywords (→ back to pool)

Both legs share the same keyword pool. Stop when:
  new_competitors < min_new  OR  Jaccard(new_kw, pool) > max_jaccard  OR  rounds >= max

Usage:
    from pipeline.discover import run_loop
    stats = run_loop(db_path, country="us", max_rounds=3, budget_usd=5.0)
"""
from __future__ import annotations

import sys
import pathlib
from dataclasses import dataclass, field
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import db
import hub_client
from fetchers import dataforseo, itunes


@dataclass
class RoundStats:
    round_num: int
    new_apps: int
    new_domains: int
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
        print(f"\n{'RND':>3}  {'APPS':>5}  {'DOMS':>5}  {'NEW_KW':>7}  "
              f"{'TOTAL_KW':>9}  {'JACCARD':>7}  {'COST':>7}  NOTE")
        print("-" * 72)
        for r in self.rounds:
            print(f"{r.round_num:>3}  {r.new_apps:>5}  {r.new_domains:>5}  "
                  f"{r.new_keywords:>7}  {r.total_keywords:>9}  {r.jaccard:>7.3f}  "
                  f"${r.cost_usd:>6.4f}  {r.stop_reason}")
        print(f"\nTotal: ${self.total_cost:.4f}  Stop: {self.stop_reason}")


# ── Keyword pool helpers ───────────────────────────────────────────────────────

def _kw_pool(db_path: pathlib.Path) -> set[str]:
    kws: set[str] = set()
    for tbl, col in [("seeds", "keyword"), ("keywords_google", "keyword"),
                     ("appstore_keywords", "keyword"), ("competitor_organic_kw", "keyword")]:
        try:
            df = db.query_df(db_path, f"SELECT LOWER({col}) AS k FROM {tbl}")
            kws.update(df["k"].dropna().tolist())
        except Exception:
            pass
    return kws


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    i = len(a & b)
    u = len(a | b)
    return i / u if u else 1.0


# ── App leg helpers ────────────────────────────────────────────────────────────

def _unfetched_apps(db_path: pathlib.Path, limit: int) -> list[str]:
    df = db.query_df(db_path, """
        SELECT DISTINCT s.app_id FROM appstore_serp s
        LEFT JOIN appstore_keywords k ON k.app_id = s.app_id
        WHERE k.app_id IS NULL AND s.app_id != ''
        LIMIT ?
    """, (limit,))
    return df["app_id"].tolist()


# ── Web leg helpers ────────────────────────────────────────────────────────────

def _unfetched_domains(db_path: pathlib.Path, limit: int) -> list[str]:
    """Domains in competitors_web that haven't had organic-competitors fetched yet."""
    df = db.query_df(db_path, """
        SELECT w.domain FROM competitors_web w
        WHERE NOT EXISTS (
            SELECT 1 FROM competitor_clusters cc
            WHERE cc.competitor_type='web' AND cc.domain = w.domain
        )
        LIMIT ?
    """, (limit,))
    return df["domain"].tolist()


def _domains_without_kw(db_path: pathlib.Path, limit: int) -> list[str]:
    """Domains without any organic-keywords fetched."""
    df = db.query_df(db_path, """
        SELECT w.domain FROM competitors_web w
        WHERE NOT EXISTS (
            SELECT 1 FROM competitor_organic_kw ck WHERE ck.domain = w.domain
        )
        LIMIT ?
    """, (limit,))
    return df["domain"].tolist()


def _fetch_organic_competitors(domain: str, country: str) -> list[str]:
    """Returns list of competitor domains via Ahrefs (hub or direct)."""
    try:
        if hub_client.enabled():
            data = hub_client.fetch("ahrefs_organic_competitors",
                                    target=domain, country=country, limit=15)
        else:
            import requests as req, json as _json
            import config as _cfg
            # Direct Ahrefs REST
            token = _cfg.get("AHREFS_TOKEN") or ""
            r = req.get("https://api.ahrefs.com/v3/site-explorer/organic-competitors",
                headers={"Authorization": f"Bearer {token}"},
                params={"target": domain, "country": country, "mode": "subdomains",
                        "limit": 15, "date": date.today().isoformat(),
                        "select": "competitor_domain,traffic"},
                timeout=15)
            data = r.json()
        competitors = data.get("competitors", []) if isinstance(data, dict) else []
        return [c["competitor_domain"] for c in competitors if c.get("competitor_domain")]
    except Exception as e:
        print(f"    organic-competitors({domain}): {e}")
        return []


def _fetch_domain_kw(domain: str, country: str, limit: int = 50) -> list[dict]:
    """Fetch organic keywords for a domain."""
    try:
        if hub_client.enabled():
            raw = hub_client.fetch("ahrefs_organic_kw", target=domain,
                                   country=country, limit=limit)
        else:
            import requests as req
            import config as _cfg
            token = _cfg.get("AHREFS_TOKEN") or ""
            r = req.get("https://api.ahrefs.com/v3/site-explorer/organic-keywords",
                headers={"Authorization": f"Bearer {token}"},
                params={"target": domain, "country": country, "mode": "subdomains",
                        "limit": limit, "date": date.today().isoformat(),
                        "order_by": "volume:desc",
                        "select": "keyword,volume,difficulty,cpc,traffic,position"},
                timeout=15)
            raw = r.json()
        from fetchers.ahrefs_ingest import normalize_organic_keywords
        return normalize_organic_keywords(raw, domain=domain)
    except Exception as e:
        print(f"    organic-kw({domain}): {e}")
        return []


# ── Main loop ──────────────────────────────────────────────────────────────────

def run_loop(
    db_path: pathlib.Path,
    *,
    country: str = "us",
    max_rounds: int = 3,
    min_new_kw: int = 30,
    max_jaccard: float = 0.70,
    aso_limit: int = 200,
    itunes_serp_limit: int = 20,
    max_apps_per_round: int = 20,
    max_domains_per_round: int = 10,
    budget_usd: float = 5.0,
    dry_run: bool = False,
) -> LoopStats:
    stats = LoopStats()
    total_spent = 0.0

    for round_num in range(1, max_rounds + 1):
        print(f"\n── Round {round_num}/{max_rounds} ─────────────────────────────────────")
        round_cost = 0.0
        existing_kws = _kw_pool(db_path)
        new_kws: set[str] = set()
        new_apps = 0
        new_domains = 0

        # ── APP LEG ───────────────────────────────────────────────────────────
        unfetched_apps = _unfetched_apps(db_path, max_apps_per_round)
        if unfetched_apps:
            print(f"  App leg: {len(unfetched_apps)} apps → ASO keywords")
        for aid in unfetched_apps:
            if total_spent + round_cost >= budget_usd:
                print(f"  Budget limit ${budget_usd:.2f} reached, stopping app leg")
                break
            if dry_run:
                print(f"    [dry-run] would fetch ASO for {aid}")
                continue
            try:
                rows, cost = dataforseo.fetch_keywords_for_app(aid, limit=aso_limit)
                nm_df = db.query_df(db_path,
                    "SELECT name FROM competitors_app WHERE app_id=? "
                    "UNION SELECT app_name AS name FROM appstore_serp WHERE app_id=? LIMIT 1",
                    (aid, aid))
                nm = nm_df.iloc[0]["name"] if len(nm_df) else None
                for r in rows:
                    r["app_name"] = nm
                db.upsert_rows(db_path, "appstore_keywords", rows)
                db.log_fetch(db_path, "dataforseo", "aso/loop",
                             {"app_id": aid, "round": round_num}, rows=len(rows), cost_usd=cost)
                new_kws.update(r["keyword"].lower() for r in rows if r.get("keyword"))
                round_cost += cost
                print(f"    app {aid}: {len(rows)} kw  ${cost:.4f}")
            except Exception as e:
                print(f"    app {aid}: ERROR {e}")

        # iTunes SERP for top new keywords found this round
        new_app_kws = new_kws - existing_kws
        if new_app_kws and not dry_run:
            sv_df = db.query_df(db_path, """
                SELECT keyword, MAX(search_volume) AS sv FROM appstore_keywords
                WHERE LOWER(keyword) IN ({})
                GROUP BY keyword ORDER BY sv DESC LIMIT ?
            """.format(",".join("?" * min(len(new_app_kws), 100)), itunes_serp_limit),
                tuple(list(new_app_kws)[:100]) + (itunes_serp_limit,))
            top_kws = sv_df["keyword"].tolist() or list(new_app_kws)[:itunes_serp_limit]
            serp_rows = itunes.keyword_to_apps_bulk(top_kws, country=country, limit=20)
            n = db.upsert_rows(db_path, "appstore_serp", serp_rows)
            new_apps = n
            db.log_fetch(db_path, "itunes", "serp/loop", {"round": round_num, "n_kw": len(top_kws)}, rows=n)
            print(f"  iTunes SERP: {n} rows from {len(top_kws)} kw")

        # ── WEB LEG ───────────────────────────────────────────────────────────
        unfetched_domains = _unfetched_domains(db_path, max_domains_per_round)
        if unfetched_domains:
            print(f"  Web leg: {len(unfetched_domains)} domains → organic-competitors")
        for domain in unfetched_domains:
            if total_spent + round_cost >= budget_usd:
                print(f"  Budget limit reached, stopping web leg")
                break
            if dry_run:
                print(f"    [dry-run] would fetch organic-competitors for {domain}")
                continue
            comp_domains = _fetch_organic_competitors(domain, country)
            print(f"    {domain}: {len(comp_domains)} competitors found")
            for cd in comp_domains:
                # Add to competitors_web (minimal row, metrics fetched separately)
                try:
                    db.upsert_rows(db_path, "competitors_web",
                                   [{"domain": cd, "monthly_organic_traffic": None}],
                                   on_conflict="IGNORE")
                    new_domains += 1
                except Exception:
                    pass
            # Mark this domain as "organic-competitors fetched" via placeholder cluster entry
            db.upsert_rows(db_path, "competitor_clusters", [{
                "competitor_id": domain, "competitor_type": "web", "domain": domain,
                "cluster_id": -1,  # -1 = unfetched/unclustered
            }], on_conflict="IGNORE")

        # Fetch organic keywords for domains that don't have any yet
        domains_no_kw = _domains_without_kw(db_path, max_domains_per_round)
        if domains_no_kw:
            print(f"  Web leg: {len(domains_no_kw)} domains → organic-keywords")
        for domain in domains_no_kw:
            if total_spent + round_cost >= budget_usd:
                break
            if dry_run:
                print(f"    [dry-run] would fetch organic-kw for {domain}")
                continue
            kw_rows = _fetch_domain_kw(domain, country)
            if kw_rows:
                db.upsert_rows(db_path, "competitor_organic_kw", kw_rows)
                new_kws.update(r["keyword"].lower() for r in kw_rows if r.get("keyword"))
                print(f"    {domain}: {len(kw_rows)} organic kw")

        # ── Convergence check ─────────────────────────────────────────────────
        new_only = new_kws - existing_kws
        jaccard = _jaccard(new_kws, existing_kws)
        total_kws = len(existing_kws | new_kws)

        # Save new keywords as seeds for next round
        if new_only and not dry_run:
            seed_rows = [{"keyword": kw, "dimension": "loop", "side": "demand"}
                         for kw in list(new_only)[:300]]
            db.upsert_rows(db_path, "seeds", seed_rows, on_conflict="IGNORE")

        total_spent += round_cost
        stop_reason = ""
        total_new = len(new_only)
        if total_new < min_new_kw:
            stop_reason = f"new_kw={total_new}<{min_new_kw}"
        elif jaccard > max_jaccard:
            stop_reason = f"jaccard={jaccard:.3f}>{max_jaccard}"
        elif total_spent >= budget_usd:
            stop_reason = f"budget=${total_spent:.2f}>=${budget_usd:.2f}"

        r = RoundStats(round_num=round_num, new_apps=new_apps, new_domains=new_domains,
                       new_keywords=len(new_only), total_keywords=total_kws,
                       jaccard=jaccard, cost_usd=round_cost, stop_reason=stop_reason)
        stats.rounds.append(r)
        stats.total_cost = total_spent

        print(f"  → new_kw={len(new_only)}  new_apps={new_apps}  new_domains={new_domains}"
              f"  jaccard={jaccard:.3f}  cost=${round_cost:.4f}")

        if stop_reason:
            stats.stop_reason = stop_reason
            print(f"  Stopping: {stop_reason}")
            break

    if not stats.stop_reason:
        stats.stop_reason = f"max_rounds={max_rounds}"
    stats.print_summary()
    return stats
