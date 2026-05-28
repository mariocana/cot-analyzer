"""
core.py — Analysis orchestration returning structured data
===========================================================
Wraps the scraping + z-score + setup-selection logic into a single
run_analysis() function that returns a JSON-serializable dict. Used by
both the CLI (cot_fx_analyzer.py) and the web app (app.py).
"""

import os
import time

from cot_fx_analyzer import (
    CURRENCIES, SINGLE_ASSETS,
    CotData, fetch_cot, get_all_pairs, compute_pair,
    classify_single_asset, select_top_3_setups,
    build_ai_prompt, call_anthropic_api, ANTHROPIC_MODEL,
)
from cot_history import fetch_all_zscores


def _currency_row(c: CotData, z) -> dict:
    return {
        "code": c.code,
        "name": c.name,
        "report_date": c.report_date,
        "long": c.nc_long,
        "short": c.nc_short,
        "net": c.net,
        "long_chg": c.nc_long_chg,
        "short_chg": c.nc_short_chg,
        "net_chg": c.net_chg,
        "z26": round(z.z26, 2) if z and z.z26 is not None else None,
        "pct_rank_26w": round(z.pct_rank_26w, 0) if z and z.pct_rank_26w is not None else None,
        "z52": round(z.z52, 2) if z and z.z52 is not None else None,
    }


def _asset_row(a: CotData, z) -> dict:
    bias, momentum, is_clean = classify_single_asset(a)
    return {
        "code": a.code,
        "name": a.name,
        "category": a.category,
        "net": a.net,
        "net_pct_oi": round(a.net_pct_oi, 1),
        "net_chg": a.net_chg,
        "open_interest": a.open_interest,
        "bias": bias,
        "momentum": momentum,
        "is_clean": is_clean,
        "z26": round(z.z26, 2) if z and z.z26 is not None else None,
        "pct_rank_26w": round(z.pct_rank_26w, 0) if z and z.pct_rank_26w is not None else None,
        "z52": round(z.z52, 2) if z and z.z52 is not None else None,
    }


def _pair_row(p) -> dict:
    return {
        "pair": p.pair,
        "net_diff": p.net_diff,
        "chg_diff": p.chg_diff,
        "bias": p.long_bias_label,
        "momentum": p.momentum_label,
        "alignment": p.alignment,
        "is_clean": p.is_clean_setup,
    }


def run_analysis(progress=None, run_ai=True) -> dict:
    """Run the full COT analysis pipeline.

    Args:
        progress: optional callable(str) for status updates (used by web UI).
        run_ai: whether to attempt the AI review (skipped if no API key).

    Returns a JSON-serializable dict with all results.
    """
    def log(msg):
        if progress:
            progress(msg)

    # --- Scrape currencies ---
    log("Fetching currency data...")
    currencies: dict[str, CotData] = {}
    for code, (name, cot_id) in CURRENCIES.items():
        d = fetch_cot(code, name, cot_id, "FX")
        if d:
            currencies[code] = d
        time.sleep(0.2)

    # --- Scrape single assets ---
    log("Fetching crypto / index / commodity data...")
    assets: dict[str, CotData] = {}
    for code, (name, cot_id, category) in SINGLE_ASSETS.items():
        d = fetch_cot(code, name, cot_id, category)
        if d:
            assets[code] = d
        time.sleep(0.2)

    if not currencies and not assets:
        return {"ok": False, "error": "No data could be fetched from Tradingster."}

    # --- Z-scores ---
    log("Computing historical z-scores from CFTC API...")
    all_instruments: dict[str, tuple[str, str]] = {}
    current_nets: dict[str, int] = {}
    for code, (name, cot_id) in CURRENCIES.items():
        if code in currencies:
            all_instruments[code] = (cot_id, name)
            current_nets[code] = currencies[code].net
    for code, (name, cot_id, _cat) in SINGLE_ASSETS.items():
        if code in assets:
            all_instruments[code] = (cot_id, name)
            current_nets[code] = assets[code].net

    try:
        zscores = fetch_all_zscores(all_instruments, current_nets)
    except Exception:
        zscores = {}

    # --- Build structured output ---
    log("Building report...")
    currency_rows = [_currency_row(c, zscores.get(c.code))
                     for c in sorted(currencies.values(),
                                     key=lambda x: x.net, reverse=True)]

    pairs = get_all_pairs(currencies)
    pair_rows = [_pair_row(p) for p in sorted(pairs,
                                              key=lambda x: abs(x.net_diff),
                                              reverse=True)]

    # Cross-pair matrices (bias + momentum), x1000 contracts
    matrix_codes = [c for c in ["EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "JPY", "USD"]
                    if c in currencies]
    bias_matrix = []
    mom_matrix = []
    for b in matrix_codes:
        bias_row = {"base": b, "cells": {}}
        mom_row = {"base": b, "cells": {}}
        for q in matrix_codes:
            if b == q:
                bias_row["cells"][q] = None
                mom_row["cells"][q] = None
            else:
                bias_row["cells"][q] = round((currencies[b].net - currencies[q].net) / 1000, 1)
                mom_row["cells"][q] = round((currencies[b].net_chg - currencies[q].net_chg) / 1000, 1)
        bias_matrix.append(bias_row)
        mom_matrix.append(mom_row)

    asset_rows = [_asset_row(a, zscores.get(a.code)) for a in assets.values()]
    asset_rows.sort(key=lambda x: (x["category"], -x["net_pct_oi"]))

    # --- Top 3 + AI ---
    top = select_top_3_setups(pairs, assets, currencies, zscores)
    all_data = list(currencies.values()) + list(assets.values())
    report_date = max(d.report_date for d in all_data) if all_data else "n/d"

    top_setups = [{
        "label": s.label,
        "type": s.asset_type,
        "bias": s.bias_label,
        "momentum": s.momentum_label,
        "net_value": s.net_value,
        "chg_value": s.chg_value,
        "score": round(s.strength_score, 1),
        "z26": round(s.z26, 2) if s.z26 is not None else None,
    } for s in top]

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
                prompt = build_ai_prompt(top, report_date)
                text = call_anthropic_api(prompt, api_key)
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
        "report_date": report_date,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "currencies": currency_rows,
        "pairs": pair_rows,
        "bias_matrix": bias_matrix,
        "momentum_matrix": mom_matrix,
        "matrix_codes": matrix_codes,
        "assets": asset_rows,
        "top_setups": top_setups,
        "ai": ai_result,
        "counts": {
            "currencies": len(currencies),
            "assets": len(assets),
            "pairs": len(pairs),
        },
    }
