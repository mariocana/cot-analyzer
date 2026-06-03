"""
batch.py — Weekly COT data batch job
=====================================
Strategy:
  - First run for an instrument (no rows in DB): fetch 2 years of history
  - Subsequent runs: fetch only reports after the latest date we have
  - All writes are idempotent (upsert), so re-running is safe.

Run modes:
  python batch.py              # all instruments, incremental
  python batch.py --backfill   # force re-download of 2 years (does not delete
                                 existing data; upserts overwrite)
  python batch.py --instrument GC   # only one instrument

Designed for Railway cron: just point a cron service at `python batch.py`.
"""

import argparse
import sys
import time
from datetime import date, timedelta

from instruments import INSTRUMENTS, Instrument
from cftc_client import fetch_reports
from db import init_schema, upsert_rows, latest_date_for, row_count, coverage_summary


# Two years of weekly reports ≈ 104 rows per instrument
BACKFILL_DAYS = 2 * 365


def _resolve_since(inst: Instrument, force_backfill: bool) -> date | None:
    """Decide the 'since_date' for this instrument:
       - backfill mode → 2 years ago
       - has rows in DB → latest date in DB (incremental)
       - no rows in DB → 2 years ago (first-time backfill)
    """
    if force_backfill:
        return date.today() - timedelta(days=BACKFILL_DAYS)

    latest = latest_date_for(inst.ticker)
    if latest is None:
        return date.today() - timedelta(days=BACKFILL_DAYS)
    return latest


def process_one(inst: Instrument, force_backfill: bool = False) -> dict:
    """Fetch and store data for a single instrument.
    Returns a small dict with stats: {'ticker', 'new_rows', 'latest_date', 'error'}."""
    since = _resolve_since(inst, force_backfill)
    mode = "BACKFILL" if since is None or (date.today() - since).days > 30 else "INCR"

    print(f"  · {inst.ticker:<10} ({inst.name[:35]:<35}) "
          f"[{mode}] since {since}...", end="", flush=True)

    try:
        reports = fetch_reports(inst.cftc_code, since_date=since)
    except Exception as e:
        print(f" ✗ API error: {e}")
        return {"ticker": inst.ticker, "new_rows": 0,
                "latest_date": None, "error": str(e)}

    if not reports:
        print(" (no new data)")
        return {"ticker": inst.ticker, "new_rows": 0,
                "latest_date": None, "error": None}

    # Stamp each row with the ticker for DB insert
    rows = [
        {"instrument_code": inst.ticker, **r}
        for r in reports
    ]
    try:
        affected = upsert_rows(rows)
    except Exception as e:
        print(f" ✗ DB error: {e}")
        return {"ticker": inst.ticker, "new_rows": 0,
                "latest_date": None, "error": str(e)}

    latest = reports[-1]["report_date"]
    print(f" ✓ {affected} rows  (latest: {latest})")
    return {"ticker": inst.ticker, "new_rows": affected,
            "latest_date": latest, "error": None}


def run(only_ticker: str | None = None, force_backfill: bool = False) -> None:
    """Main entry point. Iterates all (or one) instruments and writes to DB."""
    print(f"\n=== COT Batch Job — {date.today().isoformat()} ===")
    print(f"Mode: {'BACKFILL (2y)' if force_backfill else 'incremental'}")

    print("\n[1/3] Ensuring schema...")
    init_schema()
    print(f"      Schema ready. Current row count: {row_count():,}")

    print("\n[2/3] Fetching data...")
    targets = (
        [INSTRUMENTS[only_ticker]] if only_ticker
        else list(INSTRUMENTS.values())
    )

    stats: list[dict] = []
    for inst in targets:
        result = process_one(inst, force_backfill=force_backfill)
        stats.append(result)
        # Be polite with the CFTC API
        time.sleep(0.4)

    # Summary
    print("\n[3/3] Summary")
    total_new = sum(s["new_rows"] for s in stats)
    errors    = [s for s in stats if s["error"]]
    print(f"      Instruments processed: {len(stats)}")
    print(f"      Total rows written:    {total_new:,}")
    print(f"      Errors:                {len(errors)}")
    if errors:
        print("      Failed instruments:")
        for e in errors:
            print(f"        - {e['ticker']}: {e['error']}")

    print(f"\n      DB row count after batch: {row_count():,}")
    print("\n      Coverage by instrument:")
    print(f"      {'Ticker':<10} {'Oldest':<12} {'Newest':<12} {'Weeks':>6}")
    print(f"      {'-'*10} {'-'*12} {'-'*12} {'-'*6}")
    for row in coverage_summary():
        print(f"      {row['instrument_code']:<10} "
              f"{row['oldest'].isoformat():<12} "
              f"{row['newest'].isoformat():<12} "
              f"{row['weeks']:>6}")

    if errors:
        sys.exit(1)
    print("\n=== Done ===\n")


def main():
    p = argparse.ArgumentParser(description="COT weekly batch job")
    p.add_argument("--backfill", action="store_true",
                   help="Force 2-year re-download (upserts overwrite existing)")
    p.add_argument("--instrument", type=str, default=None,
                   help="Process only one instrument (ticker, e.g. GC)")
    args = p.parse_args()

    if args.instrument and args.instrument not in INSTRUMENTS:
        print(f"Unknown instrument: {args.instrument}")
        print(f"Available: {', '.join(sorted(INSTRUMENTS))}")
        sys.exit(2)

    run(only_ticker=args.instrument, force_backfill=args.backfill)


if __name__ == "__main__":
    main()
