"""SQLite helpers — one DB per topic."""
import sqlite3
import pathlib
import json
import time
from contextlib import contextmanager
from typing import Iterable, Mapping, Optional

import pandas as pd

SCHEMA_PATH = pathlib.Path(__file__).parent / "sql" / "schema.sql"


def init(db_path: pathlib.Path) -> None:
    """Create DB if not exists and apply schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        con.executescript(SCHEMA_PATH.read_text())
    _migrate(db_path)


def _migrate(db_path: pathlib.Path) -> None:
    """Apply non-destructive schema migrations for existing DBs."""
    migrations = [
        # v2: unified web+app clustering
        "ALTER TABLE competitor_clusters ADD COLUMN competitor_type TEXT DEFAULT 'app'",
        "ALTER TABLE competitor_clusters ADD COLUMN domain TEXT",
        # v2: rename app_id → competitor_id (can't rename in SQLite, add alias column)
        "ALTER TABLE competitor_clusters ADD COLUMN competitor_id_alias TEXT",
        # v2: social cluster map
        """CREATE TABLE IF NOT EXISTS social_cluster_map (
            source TEXT NOT NULL,
            item_id TEXT NOT NULL,
            cluster_id INTEGER,
            score REAL,
            assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (source, item_id)
        )""",
        # v3: LLM-defined track taxonomy
        """CREATE TABLE IF NOT EXISTS tracks (
            track_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            name_en TEXT,
            description TEXT,
            keywords TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        # v3: add cluster_label + competitor_id columns to old DBs
        "ALTER TABLE competitor_clusters ADD COLUMN cluster_label TEXT",
        # competitor_id mirrors app_id for backward compat with new queries
        "ALTER TABLE competitor_clusters ADD COLUMN competitor_id TEXT",
        # v4: add store field to appstore_serp and competitors_app (ios/android)
        "ALTER TABLE appstore_serp ADD COLUMN store TEXT DEFAULT 'ios'",
        "ALTER TABLE competitors_app ADD COLUMN store TEXT DEFAULT 'ios'",
    ]
    with sqlite3.connect(db_path) as con:
        for sql in migrations:
            try:
                con.execute(sql)
            except sqlite3.OperationalError:
                pass  # Column/table already exists


@contextmanager
def connect(db_path: pathlib.Path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def set_meta(db_path: pathlib.Path, key: str, value: str) -> None:
    with connect(db_path) as con:
        con.execute(
            "INSERT INTO topic_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_meta(db_path: pathlib.Path, key: str, default: Optional[str] = None) -> Optional[str]:
    with connect(db_path) as con:
        row = con.execute("SELECT value FROM topic_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def upsert_rows(
    db_path: pathlib.Path,
    table: str,
    rows: Iterable[Mapping],
    on_conflict: str = "REPLACE",
) -> int:
    """Bulk upsert. Uses INSERT OR REPLACE by default (relies on UNIQUE constraints)."""
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ",".join("?" * len(cols))
    sql = f"INSERT OR {on_conflict} INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    with connect(db_path) as con:
        con.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
    return len(rows)


def log_fetch(
    db_path: pathlib.Path,
    source: str,
    op: str,
    args: Mapping,
    rows: int = 0,
    cost_usd: float = 0.0,
    error: Optional[str] = None,
    duration_ms: int = 0,
) -> None:
    with connect(db_path) as con:
        con.execute(
            "INSERT INTO fetch_log(source,op,args,rows,cost_usd,error,duration_ms) "
            "VALUES(?,?,?,?,?,?,?)",
            (source, op, json.dumps(args, ensure_ascii=False), rows, cost_usd, error, duration_ms),
        )


def query_df(db_path: pathlib.Path, sql: str, params: tuple = ()) -> pd.DataFrame:
    with sqlite3.connect(db_path) as con:
        return pd.read_sql_query(sql, con, params=params)


def total_cost(db_path: pathlib.Path) -> float:
    with connect(db_path) as con:
        row = con.execute("SELECT COALESCE(SUM(cost_usd),0) AS t FROM fetch_log").fetchone()
    return float(row["t"])


class Timer:
    def __enter__(self):
        self.t0 = time.time()
        return self

    def __exit__(self, *a):
        self.ms = int((time.time() - self.t0) * 1000)
