"""
cftc_client.py — Official CFTC Public Reporting API client
===========================================================
Replaces the old Tradingster scraping and the old cot_history module with
a single, robust source: the official CFTC Socrata API.

Endpoint: https://publicreporting.cftc.gov/resource/6dca-aqww.json
Dataset:  Commitments of Traders — Legacy Futures Only (6dca-aqww)

Field naming convention in this API: lowercase with underscores.
  Examples we care about:
    report_date_as_yyyy_mm_dd
    cftc_contract_market_code
    noncomm_positions_long_all
    noncomm_positions_short_all
    open_interest_all

The API is free, no auth required, and accepts SoQL-style query parameters.
"""

import sys
import time
from datetime import date, datetime

import requests


CFTC_BASE_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
DEFAULT_TIMEOUT = 30
PAGE_LIMIT = 5000  # Socrata allows up to 50_000, 5000 is generous and safe


# ---------------------------------------------------------------------------
# URL building (handles the '+' edge case for S&P 500 code '13874+')
# ---------------------------------------------------------------------------
def _build_url(cftc_code: str, since_date: date | None) -> str:
    """Build the CFTC API URL for one instrument.

    The '+' in '13874+' (S&P 500 Consolidated) needs special handling:
      - requests's params= auto-encodes '+' to '%2B' incorrectly for SoQL
      - but a literal '+' in the URL gets parsed by Socrata as a space
      - the only safe form is the explicit URL-encoded '%2B'
    So we encode '+' → '%2B' ourselves before building the URL.
    """
    # Manually encode '+' which has special meaning in URL query strings
    encoded_code = cftc_code.replace("+", "%2B")

    select = (
        "report_date_as_yyyy_mm_dd,"
        "noncomm_positions_long_all,"
        "noncomm_positions_short_all,"
        "open_interest_all"
    )
    parts = [
        f"cftc_contract_market_code={encoded_code}",
        f"$select={select}",
        f"$order=report_date_as_yyyy_mm_dd ASC",
        f"$limit={PAGE_LIMIT}",
    ]
    if since_date is not None:
        # Strict > so we don't re-fetch a date we already have
        iso = since_date.isoformat()
        parts.append(f"$where=report_date_as_yyyy_mm_dd > '{iso}T00:00:00.000'")

    qs = "&".join(p.replace(" ", "%20") for p in parts)
    return f"{CFTC_BASE_URL}?{qs}"


# ---------------------------------------------------------------------------
# Fetch with retry
# ---------------------------------------------------------------------------
def _fetch_with_retry(url: str, max_attempts: int = 3) -> list[dict]:
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.get(url, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last_err = e
            if attempt < max_attempts:
                # Exponential backoff: 2s, 4s
                time.sleep(2 ** attempt)
    raise RuntimeError(f"CFTC API failed after {max_attempts} attempts: {last_err}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def fetch_reports(cftc_code: str,
                  since_date: date | None = None) -> list[dict]:
    """Return all CFTC reports for one instrument, optionally only those
    strictly after `since_date`.

    Each row in the returned list has the schema we care about for the DB:
        {
            "report_date": date,
            "nc_long":      int,
            "nc_short":     int,
            "open_interest": int,
        }

    Returns rows sorted oldest → newest. Empty list if no new data.
    Raises RuntimeError if the API is unreachable after retries.
    """
    url = _build_url(cftc_code, since_date)
    raw = _fetch_with_retry(url)

    parsed: list[dict] = []
    for row in raw:
        try:
            report_date = datetime.fromisoformat(
                row["report_date_as_yyyy_mm_dd"].replace("Z", "")
            ).date()
            parsed.append({
                "report_date":   report_date,
                "nc_long":       int(float(row["noncomm_positions_long_all"])),
                "nc_short":      int(float(row["noncomm_positions_short_all"])),
                "open_interest": int(float(row["open_interest_all"])),
            })
        except (KeyError, ValueError, TypeError) as e:
            # Skip malformed rows but log them — better to lose 1 row than
            # to abort the whole batch
            print(f"  ⚠ skipping malformed row for {cftc_code}: {e}",
                  file=sys.stderr)
            continue

    # Defensive sort: Socrata $order should already give us this, but make sure
    parsed.sort(key=lambda r: r["report_date"])
    return parsed


def fetch_latest_one(cftc_code: str) -> dict | None:
    """Fetch only the single most recent report for an instrument.
    Useful for ad-hoc verification (not used by the batch)."""
    encoded_code = cftc_code.replace("+", "%2B")
    url = (
        f"{CFTC_BASE_URL}?cftc_contract_market_code={encoded_code}"
        f"&$select=report_date_as_yyyy_mm_dd,noncomm_positions_long_all,"
        f"noncomm_positions_short_all,open_interest_all"
        f"&$order=report_date_as_yyyy_mm_dd%20DESC&$limit=1"
    )
    raw = _fetch_with_retry(url)
    if not raw:
        return None
    row = raw[0]
    return {
        "report_date": datetime.fromisoformat(
            row["report_date_as_yyyy_mm_dd"].replace("Z", "")
        ).date(),
        "nc_long":       int(float(row["noncomm_positions_long_all"])),
        "nc_short":      int(float(row["noncomm_positions_short_all"])),
        "open_interest": int(float(row["open_interest_all"])),
    }
