"""
Historical Z-Score Module
==========================
Downloads historical Non-Commercial net positioning from the official CFTC
Public Reporting API (Socrata) and computes z-scores over 26 and 52 weeks.

Why z-score matters: an absolute net of +170,000 contracts means nothing on
its own. It could be a historical extreme (signal: positioning is crowded,
unwind risk is high) or perfectly normal range (signal: ignore the number).
The z-score answers: "how unusual is today's positioning compared to recent
history?"

Interpretation:
  |z| > 2.0  → statistical extreme (top/bottom ~2.5% of recent history)
  |z| > 1.5  → notable extreme
  |z| > 1.0  → above-average positioning
  |z| < 1.0  → within normal range

We use a simple disk cache (JSON file) to avoid re-downloading the full
history on every run. The cache is refreshed weekly automatically.
"""

import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

# CFTC Public Reporting Environment — Legacy Futures Only dataset
CFTC_API_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"

# Cache file (stored next to the script)
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".cot_history_cache.json")
CACHE_MAX_AGE_DAYS = 6   # refresh weekly (slightly less than 7 to be safe)

# Lookback windows
LOOKBACK_WEEKS_PRIMARY = 26    # ~6 months — main reference
LOOKBACK_WEEKS_SECONDARY = 52  # 1 year — long-term context


# ===========================================================================
#  DATA MODEL
# ===========================================================================
@dataclass
class ZScoreResult:
    """Z-score analysis for one instrument."""
    code: str                # short ticker
    current_net: int
    z26: float | None        # z-score on 26-week window
    z52: float | None        # z-score on 52-week window
    pct_rank_26w: float | None  # percentile rank within 26w (0-100)
    pct_rank_52w: float | None
    mean_26w: float | None
    std_26w: float | None
    n_obs_26w: int           # how many weekly observations were used
    n_obs_52w: int

    @property
    def label_26w(self) -> str:
        """Human-readable label for the 26-week z-score."""
        if self.z26 is None:
            return "n/a"
        z = self.z26
        if z > 2.0:   return f"📈📈 EXTREME long ({z:+.2f}σ)"
        if z > 1.5:   return f"📈 stretched long ({z:+.2f}σ)"
        if z > 1.0:   return f"↑ above-avg long ({z:+.2f}σ)"
        if z < -2.0:  return f"📉📉 EXTREME short ({z:+.2f}σ)"
        if z < -1.5:  return f"📉 stretched short ({z:+.2f}σ)"
        if z < -1.0:  return f"↓ above-avg short ({z:+.2f}σ)"
        return f"normal range ({z:+.2f}σ)"

    @property
    def is_extreme(self) -> bool:
        return self.z26 is not None and abs(self.z26) > 1.5


# ===========================================================================
#  CACHE I/O
# ===========================================================================
def _load_cache() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r") as f:
            data = json.load(f)
        # Check age
        cached_at = data.get("_cached_at", 0)
        age_days = (time.time() - cached_at) / 86400
        if age_days > CACHE_MAX_AGE_DAYS:
            print(f"  · history cache is {age_days:.1f} days old, will refresh")
            return {}
        return data.get("history", {})
    except Exception:
        return {}


def _save_cache(history: dict) -> None:
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"_cached_at": time.time(), "history": history}, f)
    except Exception as e:
        print(f"  ! Could not save cache: {e}", file=sys.stderr)


# ===========================================================================
#  CFTC API DOWNLOAD
# ===========================================================================
def _normalize_cot_code(code: str) -> str:
    """Tradingster uses codes like '13874%2B' (URL-encoded '+'), while CFTC
    API expects '13874+'. Some codes also have variants. Normalize."""
    return code.replace("%2B", "+")


def fetch_history(cot_code: str, weeks: int = 60) -> list[tuple[str, int]] | None:
    """Download `weeks` most recent reports for a single contract code.
    Returns list of (report_date, net_NC) tuples, sorted oldest→newest.
    Returns None on failure."""
    code = _normalize_cot_code(cot_code)
    # The CFTC API treats '+' in query params as a literal char, but requests
    # auto-encodes it as %2B. We bypass this by building the URL manually for
    # codes containing '+'.
    if "+" in code:
        # Manual URL with the + preserved
        url = (f"{CFTC_API_URL}?cftc_contract_market_code={code}"
               f"&$select=report_date_as_yyyy_mm_dd,noncomm_positions_long_all,"
               f"noncomm_positions_short_all"
               f"&$order=report_date_as_yyyy_mm_dd DESC"
               f"&$limit={weeks + 5}")
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            rows = r.json()
        except Exception as e:
            print(f"  ! CFTC history fetch failed for {code}: {e}", file=sys.stderr)
            return None
    else:
        params = {
            "cftc_contract_market_code": code,
            "$select": ("report_date_as_yyyy_mm_dd, "
                        "noncomm_positions_long_all, "
                        "noncomm_positions_short_all"),
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": str(weeks + 5),
        }
        try:
            r = requests.get(CFTC_API_URL, params=params, timeout=30)
            r.raise_for_status()
            rows = r.json()
        except Exception as e:
            print(f"  ! CFTC history fetch failed for {code}: {e}", file=sys.stderr)
            return None

    if not rows:
        return None

    out: list[tuple[str, int]] = []
    for row in rows:
        try:
            date = row["report_date_as_yyyy_mm_dd"][:10]
            long_pos = int(float(row["noncomm_positions_long_all"]))
            short_pos = int(float(row["noncomm_positions_short_all"]))
            out.append((date, long_pos - short_pos))
        except (KeyError, ValueError, TypeError):
            continue

    out.sort(key=lambda x: x[0])
    return out


# ===========================================================================
#  Z-SCORE COMPUTATION
# ===========================================================================
def _compute_zscore(history_nets: list[int], current_net: int,
                    lookback: int) -> tuple[float | None, float | None,
                                            float | None, float | None, int]:
    """Compute (z_score, pct_rank, mean, std, n_obs) on the most recent
    `lookback` observations, EXCLUDING the current value (so the z-score
    measures how today compares to the recent past, not to itself)."""
    # Exclude current value if it matches the most recent history entry
    past = history_nets[-lookback:] if len(history_nets) >= lookback else history_nets
    # We want at least 10 observations for a meaningful z-score
    if len(past) < 10:
        return None, None, None, None, len(past)

    mean = statistics.mean(past)
    # Use stdev (sample standard deviation)
    try:
        std = statistics.stdev(past)
    except statistics.StatisticsError:
        return None, None, None, None, len(past)

    if std == 0:
        return None, None, mean, 0.0, len(past)

    z = (current_net - mean) / std

    # Percentile rank: % of past observations strictly below current
    below = sum(1 for v in past if v < current_net)
    pct_rank = 100 * below / len(past)

    return z, pct_rank, mean, std, len(past)


def compute_zscores(code: str, cot_code: str,
                    current_net: int,
                    use_cache: bool = True) -> ZScoreResult:
    """Main entry point. Returns z-score analysis for one instrument."""
    cache = _load_cache() if use_cache else {}
    history: list[tuple[str, int]] | None = None

    if cot_code in cache:
        history = [(d, n) for d, n in cache[cot_code]]
    else:
        history = fetch_history(cot_code, weeks=60)
        if history:
            cache[cot_code] = history
            _save_cache(cache)

    if not history:
        return ZScoreResult(code=code, current_net=current_net,
                            z26=None, z52=None,
                            pct_rank_26w=None, pct_rank_52w=None,
                            mean_26w=None, std_26w=None,
                            n_obs_26w=0, n_obs_52w=0)

    # The current_net we receive from Tradingster might already be the latest
    # entry in the CFTC history. To compute "how unusual is today vs the past",
    # we want a window that ends BEFORE today. Drop the most recent observation
    # if it matches current_net within a small tolerance.
    if history and abs(history[-1][1] - current_net) < 100:
        past_nets = [n for _, n in history[:-1]]
    else:
        past_nets = [n for _, n in history]

    z26, pct26, mean26, std26, n26 = _compute_zscore(
        past_nets, current_net, LOOKBACK_WEEKS_PRIMARY)
    z52, pct52, _, _, n52 = _compute_zscore(
        past_nets, current_net, LOOKBACK_WEEKS_SECONDARY)

    return ZScoreResult(
        code=code, current_net=current_net,
        z26=z26, z52=z52,
        pct_rank_26w=pct26, pct_rank_52w=pct52,
        mean_26w=mean26, std_26w=std26,
        n_obs_26w=n26, n_obs_52w=n52,
    )


def fetch_all_zscores(instruments: dict[str, tuple[str, str]],
                      current_nets: dict[str, int]) -> dict[str, ZScoreResult]:
    """Fetch z-scores for all instruments. `instruments` is {code: (cot_code, label)}
    and `current_nets` is {code: net_NC}."""
    results: dict[str, ZScoreResult] = {}
    for code, (cot_code, _label) in instruments.items():
        if code not in current_nets:
            continue
        print(f"  · {code}...", end="", flush=True)
        r = compute_zscores(code, cot_code, current_nets[code])
        results[code] = r
        if r.z26 is not None:
            print(f" z26={r.z26:+.2f} ({r.n_obs_26w}w)")
        else:
            print(f" no data")
        time.sleep(0.15)  # be polite with CFTC API
    return results
