"""
db.py — PostgreSQL data layer
==============================
- Connects via DATABASE_URL (Railway provides this automatically)
- Creates the cot_weekly table if it doesn't exist
- All writes are idempotent (ON CONFLICT DO UPDATE), so re-running the
  batch is safe — it won't duplicate or corrupt existing rows.

Schema design rationale:
  - Primary key (instrument_code, report_date) prevents duplicates
  - We store ONLY raw NonCommercial long/short + open interest
  - All derived metrics (net, momentum, z-score) are computed at query time
    from these raw values, so changing the logic later doesn't require
    rewriting the DB.
"""

import os
from contextlib import contextmanager
from datetime import date
from typing import Iterator

import psycopg
from psycopg.rows import dict_row


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cot_weekly (
    instrument_code  TEXT    NOT NULL,
    report_date      DATE    NOT NULL,
    nc_long          INTEGER NOT NULL,
    nc_short         INTEGER NOT NULL,
    open_interest    INTEGER NOT NULL,
    inserted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (instrument_code, report_date)
);

-- Index for fast "history of one instrument" queries (used by /history/<asset>)
CREATE INDEX IF NOT EXISTS idx_cot_weekly_code_date
    ON cot_weekly (instrument_code, report_date DESC);
"""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def _get_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL env variable not set. On Railway it's provided "
            "automatically once you add the PostgreSQL service. Locally, "
            "set it to e.g. postgresql://user:pass@localhost:5432/cot"
        )
    # Railway sometimes uses postgres:// prefix; psycopg wants postgresql://
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]
    return dsn


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    """Context manager that yields a connection and ensures it's closed."""
    conn = psycopg.connect(_get_dsn(), autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------
def init_schema() -> None:
    """Create the table and index if they don't exist. Idempotent — safe to
    call every time the batch starts."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def upsert_rows(rows: list[dict]) -> int:
    """Insert COT rows. If (instrument_code, report_date) already exists,
    update with the new values (idempotent re-runs).

    Each row dict must contain:
        instrument_code, report_date, nc_long, nc_short, open_interest

    Returns the number of rows affected.
    """
    if not rows:
        return 0

    sql = """
        INSERT INTO cot_weekly
            (instrument_code, report_date, nc_long, nc_short, open_interest)
        VALUES
            (%(instrument_code)s, %(report_date)s, %(nc_long)s, %(nc_short)s, %(open_interest)s)
        ON CONFLICT (instrument_code, report_date) DO UPDATE SET
            nc_long       = EXCLUDED.nc_long,
            nc_short      = EXCLUDED.nc_short,
            open_interest = EXCLUDED.open_interest,
            inserted_at   = NOW();
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
            affected = cur.rowcount
        conn.commit()
    return affected


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def latest_date_for(instrument_code: str) -> date | None:
    """Return the most recent report_date stored for an instrument, or None
    if no data yet. Used by the batch to know where to resume."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(report_date) FROM cot_weekly "
                "WHERE instrument_code = %s",
                (instrument_code,),
            )
            row = cur.fetchone()
    return row[0] if row else None


def fetch_history(instrument_code: str, limit_weeks: int | None = None) -> list[dict]:
    """Return the historical series for one instrument, oldest first.

    Used by the web layer (/history/<asset>) and the z-score computation.
    """
    sql = """
        SELECT report_date, nc_long, nc_short, open_interest
        FROM cot_weekly
        WHERE instrument_code = %s
        ORDER BY report_date ASC
    """
    params: tuple = (instrument_code,)
    if limit_weeks is not None:
        # Get the most recent N then re-sort ascending — done in SQL for clarity
        sql = f"""
            SELECT * FROM (
                SELECT report_date, nc_long, nc_short, open_interest
                FROM cot_weekly
                WHERE instrument_code = %s
                ORDER BY report_date DESC
                LIMIT %s
            ) AS t
            ORDER BY report_date ASC
        """
        params = (instrument_code, limit_weeks)

    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


def fetch_latest_all() -> list[dict]:
    """Return the most recent row for every instrument. Used by the web UI
    to render the current positioning table without scraping anything."""
    sql = """
        SELECT DISTINCT ON (instrument_code)
            instrument_code, report_date, nc_long, nc_short, open_interest
        FROM cot_weekly
        ORDER BY instrument_code, report_date DESC;
    """
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            return list(cur.fetchall())


def row_count() -> int:
    """Total rows in cot_weekly (sanity check / monitoring)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM cot_weekly;")
            return cur.fetchone()[0]


def coverage_summary() -> list[dict]:
    """One row per instrument: oldest date, newest date, total weeks.
    Useful to verify the batch worked correctly."""
    sql = """
        SELECT
            instrument_code,
            MIN(report_date) AS oldest,
            MAX(report_date) AS newest,
            COUNT(*) AS weeks
        FROM cot_weekly
        GROUP BY instrument_code
        ORDER BY instrument_code;
    """
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            return list(cur.fetchall())
