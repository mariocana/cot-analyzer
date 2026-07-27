"""
sync.py — On-demand COT data sync
==================================
Replaces the old `batch.py` cron job with a function the web app can call
from a "Sync" button.

Two responsibilities:
  1. `data_status()` — a *cheap* check (one DB query, no network) that tells
     whether the DB already holds the latest CFTC report that should exist by
     now. The frontend uses this to show the freshness of the data.
  2. `run_sync()`   — the actual fetch. Incremental by default (only reports
     newer than what we already have); `force_backfill=True` re-downloads two
     years. All writes are idempotent (upsert), so re-running is safe.

COT release schedule:
  Each report reflects positions as of a Tuesday and is published the
  following Friday ~15:30 ET. We treat a report as reliably available on the
  Saturday after its Tuesday, which mirrors the old Saturday 09:00 UTC cron.
"""

import time
from datetime import date, datetime, timedelta, timezone

from instruments import INSTRUMENTS, Instrument
from cftc_client import fetch_reports
from db import (init_schema, upsert_rows, latest_date_for,
                available_report_dates, coverage_summary)


# Two years of weekly reports ≈ 104 rows per instrument
BACKFILL_DAYS = 2 * 365


# ---------------------------------------------------------------------------
# Freshness check (no network — safe to call on every page load)
# ---------------------------------------------------------------------------
def latest_expected_report_date(today: date | None = None) -> date:
    """The most recent COT report date that *should* already be published.

    Reports are as-of Tuesday and released the following Friday; we consider a
    report reliably available on the Saturday after its Tuesday (T+4 days).
    """
    today = today or date.today()
    # weekday(): Mon=0 … Sun=6, so Tuesday=1
    days_since_tue = (today.weekday() - 1) % 7
    last_tue = today - timedelta(days=days_since_tue)
    # If we haven't yet reached the Saturday after this Tuesday, the report
    # isn't out — fall back to the previous week's Tuesday.
    if today < last_tue + timedelta(days=4):
        last_tue -= timedelta(days=7)
    return last_tue


def data_status() -> dict:
    """Cheap freshness snapshot. No CFTC API calls.

    Returns:
        {
          "current":  bool,          # DB already has the latest expected report
          "newest":   "YYYY-MM-DD" | None,  # newest report_date in DB
          "expected": "YYYY-MM-DD",         # latest report that should exist
          "instruments": int,               # instruments with any data
        }
    """
    expected = latest_expected_report_date()
    dates = available_report_dates()
    newest = dates[0] if dates else None
    current = newest is not None and newest >= expected
    return {
        "current": current,
        "newest": newest.isoformat() if newest else None,
        "expected": expected.isoformat(),
        "instruments": len(coverage_summary()),
    }


# ---------------------------------------------------------------------------
# Fetch logic (moved verbatim from the old batch.py)
# ---------------------------------------------------------------------------
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
    Returns {'ticker', 'new_rows', 'latest_date', 'error'}."""
    since = _resolve_since(inst, force_backfill)
    try:
        reports = fetch_reports(inst.cftc_code, since_date=since)
    except Exception as e:
        return {"ticker": inst.ticker, "new_rows": 0,
                "latest_date": None, "error": str(e)}

    if not reports:
        return {"ticker": inst.ticker, "new_rows": 0,
                "latest_date": None, "error": None}

    rows = [{"instrument_code": inst.ticker, **r} for r in reports]
    try:
        affected = upsert_rows(rows)
    except Exception as e:
        return {"ticker": inst.ticker, "new_rows": 0,
                "latest_date": None, "error": str(e)}

    latest = reports[-1]["report_date"]
    return {"ticker": inst.ticker, "new_rows": affected,
            "latest_date": latest.isoformat(), "error": None}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_sync(force_backfill: bool = False,
             only_ticker: str | None = None,
             progress=None) -> dict:
    """Fetch new data for all instruments (or one). Returns a summary dict.

    `progress` is an optional callable(str) used to report status to the UI.
    """
    def _say(msg: str) -> None:
        if progress:
            progress(msg)

    start = time.time()
    _say("Ensuring schema…")
    init_schema()

    targets = (
        [INSTRUMENTS[only_ticker]] if only_ticker
        else list(INSTRUMENTS.values())
    )

    results: list[dict] = []
    for i, inst in enumerate(targets, 1):
        _say(f"Fetching {inst.ticker} ({i}/{len(targets)})…")
        results.append(process_one(inst, force_backfill=force_backfill))
        time.sleep(0.4)  # be polite with the CFTC API

    total_new = sum(r["new_rows"] for r in results)
    errors = [r for r in results if r["error"]]
    status = data_status()

    return {
        "ok": len(errors) == 0,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_s": round(time.time() - start, 1),
        "instruments": len(results),
        "new_rows": total_new,
        "errors": [{"ticker": e["ticker"], "error": e["error"]} for e in errors],
        "newest": status["newest"],
        "current": status["current"],
    }


# ---------------------------------------------------------------------------
# CLI entry point (kept so the same logic can still run from a cron or shell:
#     python sync.py                    # incremental sync of all instruments
#     python sync.py --backfill         # force 2-year re-download
#     python sync.py --instrument GC    # only one instrument
# )
# ---------------------------------------------------------------------------
def main() -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(description="COT data sync (manual/CLI)")
    p.add_argument("--backfill", action="store_true",
                   help="Force 2-year re-download (upserts overwrite existing)")
    p.add_argument("--instrument", type=str, default=None,
                   help="Sync only one instrument (ticker, e.g. GC)")
    args = p.parse_args()

    if args.instrument and args.instrument not in INSTRUMENTS:
        print(f"Unknown instrument: {args.instrument}")
        print(f"Available: {', '.join(sorted(INSTRUMENTS))}")
        return 2

    result = run_sync(force_backfill=args.backfill,
                      only_ticker=args.instrument,
                      progress=lambda m: print(f"  · {m}"))
    print(f"\nDone in {result['elapsed_s']}s — "
          f"{result['new_rows']} new rows across {result['instruments']} instruments. "
          f"Newest: {result['newest']}")
    if result["errors"]:
        print("Errors:")
        for e in result["errors"]:
            print(f"  - {e['ticker']}: {e['error']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
