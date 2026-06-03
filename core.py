"""
core.py — Analysis orchestration (Phase 2)
============================================
Reads positioning data from the PostgreSQL database (populated by batch.py)
and produces the structured JSON consumed by the web UI.

Key change vs Phase 1: NO MORE LIVE SCRAPING.
The web app now answers in milliseconds because everything is computed from
the local DB. Tradingster is no longer involved anywhere in the pipeline.

Functions:
  run_analysis(run_ai=True) → dict
      Full analysis: currencies, pairs, matrices, single assets, top setups,
      and optional AI review. Drop-in replacement for the old function.

  history_for(ticker) → dict
      Historical series for one instrument, used by the /history/<asset>
      endpoint to power the chart in Phase 3.
"""

import os
import statistics
import time
from dataclasses import dataclass, field
from datetime import date

from instruments import INSTRUMENTS, Instrument
from db import fetch_history, fetch_latest_all

# Z-score lookback windows (in weeks)
Z_LOOKBACK_PRIMARY = 26    # ~6 months
Z_LOOKBACK_SECONDARY = 52  # 1 year


# ---------------------------------------------------------------------------
# Data model (in-memory representation, like the old CotData)
# ---------------------------------------------------------------------------
@dataclass
class AssetSnapshot:
    """Latest weekly snapshot for one instrument, with derived metrics."""
    ticker: str
    name: str
    category: str
    report_date: date
    # Current week
    nc_long: int
    nc_short: int
    open_interest: int
    # Derived from current week
    @property
    def net(self) -> int:
        return self.nc_long - self.nc_short
    @property
    def net_pct_oi(self) -> float:
        return 100 * self.net / self.open_interest if self.open_interest else 0.0
    # Previous week (filled in by build_snapshots)
    prev_nc_long: int = 0
    prev_nc_short: int = 0
    # Derived: weekly changes
    @property
    def nc_long_chg(self) -> int:
        return self.nc_long - self.prev_nc_long
    @property
    def nc_short_chg(self) -> int:
        return self.nc_short - self.prev_nc_short
    @property
    def net_chg(self) -> int:
        return self.nc_long_chg - self.nc_short_chg
    # Historical context (z-score)
    z26: float | None = None
    pct_rank_26w: float | None = None
    z52: float | None = None


# ---------------------------------------------------------------------------
# Build snapshots from DB
# ---------------------------------------------------------------------------
def _zscore(history_nets: list[int], current: int, lookback: int
            ) -> tuple[float | None, float | None]:
    """Return (z_score, pct_rank) of `current` vs the prior `lookback` weeks.
    Returns (None, None) if not enough history."""
    if len(history_nets) < 10:
        return None, None
    past = history_nets[-lookback:] if len(history_nets) >= lookback else history_nets
    try:
        mean = statistics.mean(past)
        std = statistics.stdev(past)
    except statistics.StatisticsError:
        return None, None
    if std == 0:
        return None, None
    z = (current - mean) / std
    below = sum(1 for v in past if v < current)
    pct_rank = 100 * below / len(past)
    return z, pct_rank


def build_snapshots() -> dict[str, AssetSnapshot]:
    """Read all instruments from DB and build a complete snapshot per asset.

    For each instrument we:
      1. Pull the full history (≤104 rows usually)
      2. Pick the latest row as "current"
      3. Pick the second-latest as "previous" (for ΔNet)
      4. Compute z26 / z52 against the prior weeks (excluding current)
    """
    snapshots: dict[str, AssetSnapshot] = {}

    for ticker, inst in INSTRUMENTS.items():
        history = fetch_history(ticker)  # oldest → newest
        if not history:
            continue

        latest = history[-1]
        prev = history[-2] if len(history) >= 2 else None

        # Build the past-nets series excluding the current observation —
        # this way z-score answers "how does today compare to recent past?"
        past_nets = [r["nc_long"] - r["nc_short"] for r in history[:-1]]
        current_net = latest["nc_long"] - latest["nc_short"]

        z26, pct26 = _zscore(past_nets, current_net, Z_LOOKBACK_PRIMARY)
        z52, _     = _zscore(past_nets, current_net, Z_LOOKBACK_SECONDARY)

        snap = AssetSnapshot(
            ticker=ticker,
            name=inst.name,
            category=inst.category,
            report_date=latest["report_date"],
            nc_long=latest["nc_long"],
            nc_short=latest["nc_short"],
            open_interest=latest["open_interest"],
            prev_nc_long=prev["nc_long"] if prev else latest["nc_long"],
            prev_nc_short=prev["nc_short"] if prev else latest["nc_short"],
            z26=z26,
            pct_rank_26w=pct26,
            z52=z52,
        )
        snapshots[ticker] = snap

    return snapshots


# ---------------------------------------------------------------------------
# FX pair analysis
# ---------------------------------------------------------------------------
@dataclass
class PairBias:
    pair: str           # e.g. "EUR/JPY"
    net_diff: int       # net(base) - net(quote)
    chg_diff: int       # net_chg(base) - net_chg(quote)

    @property
    def long_bias_label(self) -> str:
        if self.net_diff > 80_000:   return "🟢🟢 BULL strong"
        if self.net_diff > 25_000:   return "🟢 Bull"
        if self.net_diff < -80_000:  return "🔴🔴 BEAR strong"
        if self.net_diff < -25_000:  return "🔴 Bear"
        return "⚪ Neutral"

    @property
    def momentum_label(self) -> str:
        if self.chg_diff > 15_000:   return "↑↑ bull accelerating"
        if self.chg_diff > 3_000:    return "↑ toward bull"
        if self.chg_diff < -15_000:  return "↓↓ bear accelerating"
        if self.chg_diff < -3_000:   return "↓ toward bear"
        return "→ stable"

    @property
    def alignment(self) -> str:
        long_pos = self.net_diff > 25_000
        long_neg = self.net_diff < -25_000
        mom_pos  = self.chg_diff > 3_000
        mom_neg  = self.chg_diff < -3_000
        if (long_pos and mom_pos) or (long_neg and mom_neg):
            return "✓ aligned"
        if (long_pos and mom_neg) or (long_neg and mom_pos):
            return "⚠ divergent"
        return "·"

    @property
    def is_clean_setup(self) -> bool:
        return self.alignment == "✓ aligned" and abs(self.net_diff) > 25_000


def compute_all_pairs(currencies: dict[str, AssetSnapshot]) -> list[PairBias]:
    """Build all unique FX cross-pairs from the 8 currencies."""
    fx_priority = ["EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY"]
    ordered = [c for c in fx_priority if c in currencies]
    pairs: list[PairBias] = []
    for i, b in enumerate(ordered):
        base = currencies[b]
        for q in ordered[i+1:]:
            quote = currencies[q]
            pairs.append(PairBias(
                pair=f"{b}/{q}",
                net_diff=base.net - quote.net,
                chg_diff=base.net_chg - quote.net_chg,
            ))
    return pairs


# ---------------------------------------------------------------------------
# Single-asset classification
# ---------------------------------------------------------------------------
def classify_single_asset(a: AssetSnapshot) -> tuple[str, str, bool]:
    """Return (bias_label, momentum_label, is_clean_setup) for one asset.
    Thresholds based on net%OI so different OI sizes are comparable."""
    pct = a.net_pct_oi
    if pct > 25:    bias = "🟢🟢 BULL extreme"
    elif pct > 10:  bias = "🟢 Bull"
    elif pct < -25: bias = "🔴🔴 BEAR extreme"
    elif pct < -10: bias = "🔴 Bear"
    else:           bias = "⚪ Neutral"

    chg_pct = 100 * a.net_chg / a.open_interest if a.open_interest else 0
    if chg_pct > 2:      momentum = "↑↑ bull accelerating"
    elif chg_pct > 0.5:  momentum = "↑ toward bull"
    elif chg_pct < -2:   momentum = "↓↓ bear accelerating"
    elif chg_pct < -0.5: momentum = "↓ toward bear"
    else:                momentum = "→ stable"

    is_clean = (
        (pct > 10 and chg_pct > 0.5) or
        (pct < -10 and chg_pct < -0.5)
    )
    return bias, momentum, is_clean


# ---------------------------------------------------------------------------
# Top setup selection
# ---------------------------------------------------------------------------
@dataclass
class Setup:
    label: str           # "EUR/JPY" or "GC (GOLD)"
    asset_type: str      # "FX_PAIR" or "SINGLE"
    bias_label: str
    momentum_label: str
    net_value: int
    chg_value: int
    strength_score: float
    z26: float | None = None
    z26_quote: float | None = None
    details: dict = field(default_factory=dict)


def _score_pair(p: PairBias) -> float:
    bias_score = min(abs(p.net_diff) / 2000, 100)
    mom_score  = min(abs(p.chg_diff) / 300, 100)
    return bias_score * 0.7 + mom_score * 0.3


def _score_single(a: AssetSnapshot) -> float:
    bias_score = min(abs(a.net_pct_oi) * 2.5, 100)
    chg_pct = abs(a.net_chg) / a.open_interest * 100 if a.open_interest else 0
    mom_score = min(chg_pct * 33, 100)
    return bias_score * 0.7 + mom_score * 0.3


def select_top_3_setups(pairs: list[PairBias],
                        assets: dict[str, AssetSnapshot],
                        currencies: dict[str, AssetSnapshot]) -> list[Setup]:
    """Return the top 3 setups by composite score (bias + momentum + z bonus)."""
    setups: list[Setup] = []

    for p in pairs:
        if not p.is_clean_setup:
            continue
        base_code, quote_code = p.pair.split("/")
        base = currencies[base_code]
        quote = currencies[quote_code]
        score = _score_pair(p)
        # Z bonus
        z_bonus = 0.0
        if base.z26 is not None and abs(base.z26) > 1.5:
            z_bonus += min(abs(base.z26) * 5, 15)
        if quote.z26 is not None and abs(quote.z26) > 1.5:
            z_bonus += min(abs(quote.z26) * 5, 15)
        setups.append(Setup(
            label=p.pair, asset_type="FX_PAIR",
            bias_label=p.long_bias_label, momentum_label=p.momentum_label,
            net_value=p.net_diff, chg_value=p.chg_diff,
            strength_score=score + z_bonus,
            z26=base.z26, z26_quote=quote.z26,
            details={
                "pair": p.pair,
                "net_diff": p.net_diff, "chg_diff": p.chg_diff,
                "base": {
                    "code": base.ticker, "long": base.nc_long, "short": base.nc_short,
                    "net": base.net, "net_chg": base.net_chg,
                    "z26": round(base.z26, 2) if base.z26 is not None else None,
                    "pct_rank_26w": round(base.pct_rank_26w, 0) if base.pct_rank_26w is not None else None,
                },
                "quote": {
                    "code": quote.ticker, "long": quote.nc_long, "short": quote.nc_short,
                    "net": quote.net, "net_chg": quote.net_chg,
                    "z26": round(quote.z26, 2) if quote.z26 is not None else None,
                    "pct_rank_26w": round(quote.pct_rank_26w, 0) if quote.pct_rank_26w is not None else None,
                },
            }
        ))

    for a in assets.values():
        bias, momentum, is_clean = classify_single_asset(a)
        if not is_clean:
            continue
        score = _score_single(a)
        z_bonus = 0.0
        if a.z26 is not None and abs(a.z26) > 1.5:
            z_bonus = min(abs(a.z26) * 5, 15)
        setups.append(Setup(
            label=f"{a.ticker} ({a.name})", asset_type="SINGLE",
            bias_label=bias, momentum_label=momentum,
            net_value=a.net, chg_value=a.net_chg,
            strength_score=score + z_bonus,
            z26=a.z26,
            details={
                "code": a.ticker, "name": a.name, "category": a.category,
                "long": a.nc_long, "short": a.nc_short,
                "net": a.net, "net_pct_oi": round(a.net_pct_oi, 2),
                "long_chg": a.nc_long_chg, "short_chg": a.nc_short_chg,
                "net_chg": a.net_chg, "open_interest": a.open_interest,
                "z26": round(a.z26, 2) if a.z26 is not None else None,
                "z52": round(a.z52, 2) if a.z52 is not None else None,
                "pct_rank_26w": round(a.pct_rank_26w, 0) if a.pct_rank_26w is not None else None,
            }
        ))

    setups.sort(key=lambda x: x.strength_score, reverse=True)
    return setups[:3]


# ---------------------------------------------------------------------------
# JSON serialization helpers
# ---------------------------------------------------------------------------
def _currency_row(c: AssetSnapshot) -> dict:
    return {
        "code": c.ticker,
        "name": c.name,
        "report_date": c.report_date.isoformat(),
        "long": c.nc_long,
        "short": c.nc_short,
        "net": c.net,
        "long_chg": c.nc_long_chg,
        "short_chg": c.nc_short_chg,
        "net_chg": c.net_chg,
        "z26": round(c.z26, 2) if c.z26 is not None else None,
        "pct_rank_26w": round(c.pct_rank_26w, 0) if c.pct_rank_26w is not None else None,
        "z52": round(c.z52, 2) if c.z52 is not None else None,
    }


def _asset_row(a: AssetSnapshot) -> dict:
    bias, momentum, is_clean = classify_single_asset(a)
    return {
        "code": a.ticker,
        "name": a.name,
        "category": a.category,
        "report_date": a.report_date.isoformat(),
        "net": a.net,
        "net_pct_oi": round(a.net_pct_oi, 1),
        "net_chg": a.net_chg,
        "open_interest": a.open_interest,
        "bias": bias,
        "momentum": momentum,
        "is_clean": is_clean,
        "z26": round(a.z26, 2) if a.z26 is not None else None,
        "pct_rank_26w": round(a.pct_rank_26w, 0) if a.pct_rank_26w is not None else None,
        "z52": round(a.z52, 2) if a.z52 is not None else None,
    }


def _pair_row(p: PairBias) -> dict:
    return {
        "pair": p.pair,
        "net_diff": p.net_diff,
        "chg_diff": p.chg_diff,
        "bias": p.long_bias_label,
        "momentum": p.momentum_label,
        "alignment": p.alignment,
        "is_clean": p.is_clean_setup,
    }


# ---------------------------------------------------------------------------
# AI review (unchanged logic, prompt stays in Italian as before)
# ---------------------------------------------------------------------------
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL   = "claude-sonnet-4-6"
ANTHROPIC_VERSION = "2023-06-01"


def _build_ai_prompt(top_setups: list[Setup], report_date: str) -> str:
    import json as _json
    setups_json = _json.dumps(
        [{"label": s.label, "type": s.asset_type, "bias": s.bias_label,
          "momentum": s.momentum_label, **s.details} for s in top_setups],
        indent=2, ensure_ascii=False
    )
    return f"""Sei un analista esperto di posizionamento COT (Commitment of Traders) che assiste un trader prop swing. Stai facendo da sounding board critico, NON da advisor.

DATI: COT Legacy Report Non-Commercial al {report_date}, i 3 setup con bias di lungo + momentum settimanale allineati. Ogni setup include lo z-score a 26 settimane (z26) e il percentile rank, calcolati sui dati storici ufficiali CFTC:

```json
{setups_json}
```

INTERPRETAZIONE Z-SCORE:
- |z26| > 2.0  → ESTREMO statistico (top/bottom ~2.5% degli ultimi 6 mesi)
- |z26| > 1.5  → stretched, attenzione a unwind
- |z26| > 1.0  → posizionamento sopra la media
- |z26| < 1.0  → nella norma

Per OGNUNO dei 3 setup, fornisci un'analisi di ~180 parole strutturata così:

**[NOME SETUP]** — Bias COT: [direzione]

1. **Cosa dice il posizionamento**: usa z26 e percentile rank per quantificare quanto è affollato il trade.
2. **Cosa dice il momentum**: ΔNet conferma o smentisce?
3. **Rischi e blind spot**: affollamento, divergenze, eventi macro, bias cognitivi.
4. **Note operative**: NON dare entry/stop/target. Cosa cercare sul grafico, orizzonte temporale.

Tono: diretto, professionale, critico. Devil's advocate da collega esperto.

Chiudi con ~100 parole di **CONTESTO INCROCIATO** — tema macro, coerenza interna, quale dei 3 è più solido e quale più rischioso.
"""


def _call_anthropic_api(prompt: str, api_key: str) -> str | None:
    import requests
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 2500,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        r = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=60)
    except Exception as e:
        print(f"  ! AI API call failed: {e}")
        return None
    if r.status_code != 200:
        print(f"  ! AI API HTTP {r.status_code}: {r.text[:300]}")
        return None
    try:
        data = r.json()
        parts = [b.get("text", "") for b in data.get("content", [])
                 if b.get("type") == "text"]
        return "\n".join(parts).strip() or None
    except Exception as e:
        print(f"  ! AI response parse error: {e}")
        return None


# ---------------------------------------------------------------------------
# Public API: run_analysis (DB-backed, fast)
# ---------------------------------------------------------------------------
def run_analysis(progress=None, run_ai: bool = True) -> dict:
    """Run the full analysis using only the local DB. No scraping.

    Returns a JSON-serializable dict with the same shape as the old version.
    """
    def log(msg):
        if progress:
            progress(msg)

    log("Loading positioning data from DB...")
    snapshots = build_snapshots()

    if not snapshots:
        return {
            "ok": False,
            "error": ("No data in DB. Run `python batch.py` first to populate "
                      "the database from the CFTC API."),
        }

    # Split FX from single assets
    currencies = {t: s for t, s in snapshots.items() if s.category == "FX"}
    assets     = {t: s for t, s in snapshots.items() if s.category != "FX"}

    log("Computing pair biases...")
    pairs = compute_all_pairs(currencies)

    # Build cross-pair matrices (in thousands of contracts)
    matrix_codes = [c for c in ["EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "JPY", "USD"]
                    if c in currencies]
    bias_matrix, mom_matrix = [], []
    for b in matrix_codes:
        bias_row = {"base": b, "cells": {}}
        mom_row  = {"base": b, "cells": {}}
        for q in matrix_codes:
            if b == q:
                bias_row["cells"][q] = None
                mom_row["cells"][q]  = None
            else:
                bias_row["cells"][q] = round((currencies[b].net - currencies[q].net) / 1000, 1)
                mom_row["cells"][q]  = round((currencies[b].net_chg - currencies[q].net_chg) / 1000, 1)
        bias_matrix.append(bias_row)
        mom_matrix.append(mom_row)

    # Sort outputs
    ccy_rows = sorted([_currency_row(c) for c in currencies.values()],
                     key=lambda x: x["net"], reverse=True)
    pair_rows = sorted([_pair_row(p) for p in pairs],
                      key=lambda x: abs(x["net_diff"]), reverse=True)
    asset_rows = sorted([_asset_row(a) for a in assets.values()],
                       key=lambda x: (x["category"], -x["net_pct_oi"]))

    # Top 3
    top = select_top_3_setups(pairs, assets, currencies)
    top_setups = [{
        "label": s.label, "type": s.asset_type,
        "bias": s.bias_label, "momentum": s.momentum_label,
        "net_value": s.net_value, "chg_value": s.chg_value,
        "score": round(s.strength_score, 1),
        "z26": round(s.z26, 2) if s.z26 is not None else None,
    } for s in top]

    # Report date = max date across snapshots
    report_date = max(s.report_date for s in snapshots.values())

    # AI review
    ai_result = {"status": "skipped", "text": None, "model": None}
    if run_ai:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            ai_result["status"] = "no_key"
        elif not top:
            ai_result["status"] = "no_setups"
        else:
            log("Running AI review...")
            try:
                prompt = _build_ai_prompt(top, report_date.isoformat())
                text = _call_anthropic_api(prompt, api_key)
                if text:
                    ai_result = {"status": "ok", "text": text, "model": ANTHROPIC_MODEL}
                else:
                    ai_result["status"] = "error"
            except Exception as e:
                ai_result["status"] = "error"
                ai_result["text"] = str(e)

    log("Done.")
    return {
        "ok": True,
        "report_date": report_date.isoformat(),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "currencies": ccy_rows,
        "pairs": pair_rows,
        "bias_matrix": bias_matrix,
        "momentum_matrix": mom_matrix,
        "matrix_codes": matrix_codes,
        "assets": asset_rows,
        "top_setups": top_setups,
        "ai": ai_result,
        "counts": {
            "currencies": len(currencies),
            "assets":     len(assets),
            "pairs":      len(pairs),
        },
    }


# ---------------------------------------------------------------------------
# Public API: history_for (for the /history/<asset> endpoint in Phase 3)
# ---------------------------------------------------------------------------
def history_for(ticker: str) -> dict:
    """Return the full historical series for one instrument, formatted for
    the chart frontend."""
    if ticker not in INSTRUMENTS:
        return {"ok": False, "error": f"Unknown instrument: {ticker}"}

    inst = INSTRUMENTS[ticker]
    rows = fetch_history(ticker)
    if not rows:
        return {
            "ok": False,
            "error": f"No data in DB for {ticker}. Run the batch first.",
        }

    series = [{
        "date":          r["report_date"].isoformat(),
        "nc_long":       r["nc_long"],
        "nc_short":      r["nc_short"],
        "net":           r["nc_long"] - r["nc_short"],
        "open_interest": r["open_interest"],
    } for r in rows]

    return {
        "ok": True,
        "ticker": ticker,
        "name": inst.name,
        "category": inst.category,
        "n_weeks": len(series),
        "oldest": series[0]["date"],
        "newest": series[-1]["date"],
        "series": series,
    }
