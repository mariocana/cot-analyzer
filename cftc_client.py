"""
cftc_client.py — Official CFTC Public Reporting API client
===========================================================
Single source for all COT data: the official CFTC Socrata API.

Endpoint: https://publicreporting.cftc.gov/resource/6dca-aqww.json
Dataset:  Commitments of Traders — Legacy Futures Only (6dca-aqww)

IMPORTANT — avoiding 403 Forbidden:
Socrata (which hosts the CFTC data) rejects requests that look like
anonymous bots, especially from datacenter IPs (Railway, AWS, etc.).
Two mitigations, both applied here:

  1. A browser-like User-Agent header (often enough on its own).
  2. An optional Socrata app token via the CFTC_APP_TOKEN env variable.
     Free to obtain: sign up at https://evergreen.data.socrata.com/signup
     then create a token at
     https://publicreporting.cftc.gov/profile/edit/developer_settings
     Set it in Railway as CFTC_APP_TOKEN. Without it the client still
     works, but a token raises rate limits and avoids most 403s from
     shared cloud IPs.

Field naming convention: lowercase with underscores.
"""

import os
import sys
import time
from datetime import date, datetime

import requests


CFTC_BASE_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
DEFAULT_TIMEOUT = 30
PAGE_LIMIT = 5000

# Browser-like UA — Socrata is much friendlier to requests that carry one.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _headers() -> dict:
    """Build request headers, adding the Socrata app token if configured."""
    h = dict(_HEADERS)
    token = os.environ.get("CFTC_APP_TOKEN")
    if token:
        h["X-App-Token"] = token
    return h


def _build_url(cftc_code: str, since_date: date | None) -> str:
    """Build the CFTC API URL for one instrument.

    The '+' in '13874+' (S&P 500 Consolidated) must be sent as '%2B'.
    """
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
        iso = since_date.isoformat()
        parts.append(f"$where=report_date_as_yyyy_mm_dd > '{iso}T00:00:00.000'")
    qs = "&".join(p.replace(" ", "%20") for p in parts)
    return f"{CFTC_BASE_URL}?{qs}"


def _fetch_with_retry(url: str, max_attempts: int = 4) -> list[dict]:
    """GET with retry + exponential backoff. Adds a jittered pause so that
    a burst of 403s from rate-limiting has a chance to clear."""
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.get(url, headers=_headers(), timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            last_err = e
            status = e.response.status_code if e.response is not None else None
            # 403/429 are the throttling codes — back off harder
            if status in (403, 429) and attempt < max_attempts:
                time.sleep(3 * attempt)
                continue
            if attempt < max_attempts:
                time.sleep(2 ** attempt)
                continue
            break
        except (requests.RequestException, ValueError) as e:
            last_err = e
            if attempt < max_attempts:
                time.sleep(2 ** attempt)
                continue
            break
    raise RuntimeError(f"CFTC API failed after {max_attempts} attempts: {last_err}")


def fetch_reports(cftc_code: str,
                  since_date: date | None = None) -> list[dict]:
    """Return all CFTC reports for one instrument, optionally only those
    strictly after `since_date`. Rows sorted oldest → newest."""
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
            print(f"  ⚠ skipping malformed row for {cftc_code}: {e}",
                  file=sys.stderr)
            continue

    parsed.sort(key=lambda r: r["report_date"])
    return parsed


def fetch_latest_one(cftc_code: str) -> dict | None:
    """Fetch only the single most recent report for an instrument."""
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
