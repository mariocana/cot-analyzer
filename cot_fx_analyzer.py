"""
COT Multi-Asset Bias Analyzer + AI Review
==========================================
Scrapes COT Legacy Futures Only data from Tradingster.com for:
  - Forex: AUD, GBP, CAD, EUR, JPY, CHF, NZD, USD Index
  - Crypto: BITCOIN, ETHER (cash settled)
  - Indices: S&P 500 Consolidated
  - Commodities: Crude Oil, Natural Gas, Gold, Silver, Copper,
                 Palladium, Platinum, Aluminum MWP

Uses ONLY Non-Commercial data (speculators) and produces:
  1. Position table for each currency
  2. 8x8 cross-pair matrices (bias + momentum)
  3. Full ranking of 28 forex pairs
  4. Single-asset table (crypto/indices/commodities)
  5. Historical z-scores (26w / 52w) from official CFTC API
  6. AI review of top-3 cleanest setups (via Anthropic API)

If ANTHROPIC_API_KEY is not set, the script still completes the analysis
and logs that the AI review was skipped — it does NOT abort.
"""

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

from cot_history import fetch_all_zscores, ZScoreResult


# ===========================================================================
#  CONFIGURAZIONE
# ===========================================================================

# Valute (verranno usate per costruire le coppie incrociate)
CURRENCIES = {
    "AUD": ("AUSTRALIAN DOLLAR",     "232741"),
    "GBP": ("BRITISH POUND",         "096742"),
    "CAD": ("CANADIAN DOLLAR",       "090741"),
    "EUR": ("EURO FX",               "099741"),
    "JPY": ("JAPANESE YEN",          "097741"),
    "CHF": ("SWISS FRANC",           "092741"),
    "NZD": ("NEW ZEALAND DOLLAR",    "112741"),
    "USD": ("U.S. DOLLAR INDEX",     "098662"),
}

# Asset singoli (no coppie, sono già "indici a sé")
SINGLE_ASSETS = {
    "BTC":  ("BITCOIN",              "133741", "Crypto"),
    "ETH":  ("ETHER CASH SETTLED",   "146021", "Crypto"),
    "SPX":  ("S&P 500 CONSOLIDATED", "13874%2B", "Indice"),
    "CL":   ("CRUDE OIL WTI",        "067411", "Commodity"),
    "NG":   ("NATURAL GAS",          "023651", "Commodity"),
    "GC":   ("GOLD",                 "088691", "Commodity"),
    "SI":   ("SILVER",               "084691", "Commodity"),
    "HG":   ("COPPER #1",            "085692", "Commodity"),
    "PA":   ("PALLADIUM",            "075651", "Commodity"),
    "PL":   ("PLATINUM",             "076651", "Commodity"),
    "AL":   ("ALUMINUM MWP",         "191693", "Commodity"),
}

BASE_URL = "https://www.tradingster.com/cot/legacy-futures/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

# Anthropic API
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"   # bilanciato per analisi finanziaria
ANTHROPIC_VERSION = "2023-06-01"


# ===========================================================================
#  MODELLO DATI
# ===========================================================================
@dataclass
class CotData:
    code: str            # ticker breve (EUR, BTC, GC...)
    name: str            # nome completo (EURO FX, BITCOIN, GOLD...)
    category: str        # "FX", "Crypto", "Indice", "Commodity"
    report_date: str
    nc_long: int
    nc_short: int
    nc_long_chg: int
    nc_short_chg: int
    open_interest: int = 0

    @property
    def net(self) -> int:
        """Net Non-Commercial: positivo = speculatori net long."""
        return self.nc_long - self.nc_short

    @property
    def net_chg(self) -> int:
        """Variazione settimanale del net = ΔLong - ΔShort."""
        return self.nc_long_chg - self.nc_short_chg

    @property
    def net_pct_oi(self) -> float:
        """Net come % dell'open interest (utile per confrontare asset diversi)."""
        return 100 * self.net / self.open_interest if self.open_interest else 0.0


# ===========================================================================
#  SCRAPER
# ===========================================================================
def _to_int(token: str) -> int:
    token = token.replace(",", "").replace("+", "").strip()
    if token in ("", "-"):
        return 0
    return int(token)


def fetch_cot(code: str, name: str, cot_id: str, category: str,
              retries: int = 2) -> CotData | None:
    """Scarica e fa il parsing di una pagina COT Legacy."""
    url = f"{BASE_URL}{cot_id}"
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            break
        except Exception as e:
            if attempt == retries:
                print(f"  ! {code}: errore download — {e}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))

    soup = BeautifulSoup(r.text, "lxml")
    text = soup.get_text()

    date_match = re.search(r"AS OF:\s*([\d\-]+)", text)
    report_date = date_match.group(1) if date_match else "n/d"

    oi_match = re.search(r"Open Interest:\s*([\d,]+)", text)
    open_interest = _to_int(oi_match.group(1)) if oi_match else 0

    numeric_rows: list[list[str]] = []
    for row in soup.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) == 9 and all(re.fullmatch(r"[\-\+\d,\.]+", c) for c in cells):
            numeric_rows.append(cells)

    if len(numeric_rows) < 2:
        print(f"  ! {code}: parsing fallito", file=sys.stderr)
        return None

    positions = [_to_int(x) for x in numeric_rows[0]]
    changes   = [_to_int(x) for x in numeric_rows[1]]

    return CotData(
        code=code, name=name, category=category,
        report_date=report_date,
        nc_long=positions[0], nc_short=positions[1],
        nc_long_chg=changes[0], nc_short_chg=changes[1],
        open_interest=open_interest,
    )


# ===========================================================================
#  ANALISI COPPIE FOREX
# ===========================================================================
@dataclass
class PairBias:
    pair: str
    net_diff: int
    chg_diff: int

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


def compute_pair(base: CotData, quote: CotData) -> PairBias:
    return PairBias(
        pair=f"{base.code}/{quote.code}",
        net_diff=base.net - quote.net,
        chg_diff=base.net_chg - quote.net_chg,
    )


# ===========================================================================
#  ANALISI ASSET SINGOLI (crypto/indici/commodity)
# ===========================================================================
def classify_single_asset(a: CotData) -> tuple[str, str, bool]:
    """For single assets we use thresholds on net%OI because contract sizes
    vary widely (Crude oil OI is 30x Palladium's).
    Returns (bias_label, momentum_label, is_clean_setup)."""
    pct = a.net_pct_oi

    if pct > 25:    bias = "🟢🟢 BULL extreme"
    elif pct > 10:  bias = "🟢 Bull"
    elif pct < -25: bias = "🔴🔴 BEAR extreme"
    elif pct < -10: bias = "🔴 Bear"
    else:           bias = "⚪ Neutral"

    chg_pct = 100 * a.net_chg / a.open_interest if a.open_interest else 0
    if chg_pct > 2:     momentum = "↑↑ bull accelerating"
    elif chg_pct > 0.5: momentum = "↑ toward bull"
    elif chg_pct < -2:  momentum = "↓↓ bear accelerating"
    elif chg_pct < -0.5: momentum = "↓ toward bear"
    else:               momentum = "→ stable"

    is_clean = (
        (pct > 10 and chg_pct > 0.5) or
        (pct < -10 and chg_pct < -0.5)
    )

    return bias, momentum, is_clean


# ===========================================================================
#  OUTPUT TESTUALE
# ===========================================================================
def print_currencies(currencies: dict[str, CotData],
                     zscores: dict[str, ZScoreResult] | None = None) -> None:
    print()
    print("=" * 110)
    print(f"  COT NON-COMMERCIAL — Currency positioning")
    dates = {c.report_date for c in currencies.values()}
    print(f"  Report date: {', '.join(sorted(dates))}")
    print("=" * 110)
    print()
    header = (f"{'Ccy':<6}{'Long':>12}{'Short':>12}{'NET':>14}"
              f"{'ΔLong':>11}{'ΔShort':>11}{'ΔNET':>11}")
    if zscores:
        header += f"{'z(26w)':>10}{'%rank':>8}"
    print(header)
    print("-" * (len(header) + 2))
    for c in sorted(currencies.values(), key=lambda x: x.net, reverse=True):
        line = (f"{c.code:<6}{c.nc_long:>12,}{c.nc_short:>12,}{c.net:>+14,}"
                f"{c.nc_long_chg:>+11,}{c.nc_short_chg:>+11,}{c.net_chg:>+11,}")
        if zscores and c.code in zscores:
            z = zscores[c.code]
            if z.z26 is not None:
                line += f"{z.z26:>+10.2f}{z.pct_rank_26w:>7.0f}%"
            else:
                line += f"{'n/a':>10}{'':>8}"
        print(line)
    print()
    print("  NET = Long − Short (long-term bias) | ΔNET = ΔLong − ΔShort (weekly momentum)")
    if zscores:
        print("  z(26w) = z-score vs prior 26 weeks | %rank = percentile rank within 26w window")


def print_pair_matrix(currencies: dict[str, CotData]) -> None:
    codes = [c for c in ["EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "JPY", "USD"]
             if c in currencies]

    print()
    print("=" * 95)
    print("  FX CROSS-PAIR MATRIX — Long-term bias (Net base − Net quote, x1000 contracts)")
    print("=" * 95)
    print(f"  {'':<6}", end="")
    for q in codes: print(f"{q:>10}", end="")
    print()
    print("  " + "-" * (6 + 10 * len(codes)))
    for b in codes:
        print(f"  {b:<6}", end="")
        for q in codes:
            if b == q:
                print(f"{'·':>10}", end="")
            else:
                print(f"{(currencies[b].net - currencies[q].net) / 1000:>+10.1f}", end="")
        print()

    print()
    print("=" * 95)
    print("  FX CROSS-PAIR MATRIX — Weekly momentum (ΔNet base − ΔNet quote, x1000)")
    print("=" * 95)
    print(f"  {'':<6}", end="")
    for q in codes: print(f"{q:>10}", end="")
    print()
    print("  " + "-" * (6 + 10 * len(codes)))
    for b in codes:
        print(f"  {b:<6}", end="")
        for q in codes:
            if b == q:
                print(f"{'·':>10}", end="")
            else:
                print(f"{(currencies[b].net_chg - currencies[q].net_chg) / 1000:>+10.1f}", end="")
        print()


def get_all_pairs(currencies: dict[str, CotData]) -> list[PairBias]:
    fx_priority = ["EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY"]
    ordered = [c for c in fx_priority if c in currencies]
    return [compute_pair(currencies[b], currencies[q])
            for i, b in enumerate(ordered) for q in ordered[i+1:]]


def print_pair_ranking(pairs: list[PairBias]) -> None:
    print()
    print("=" * 95)
    print("  FX PAIR RANKING — Sorted by bias intensity")
    print("=" * 95)
    print()
    print(f"  {'Pair':<10}{'Net diff':>12}{'ΔNet diff':>12}   "
          f"{'Bias':<18}{'Momentum':<22}{'Aligned'}")
    print("  " + "-" * 88)
    for p in sorted(pairs, key=lambda x: abs(x.net_diff), reverse=True):
        print(f"  {p.pair:<10}{p.net_diff:>+12,}{p.chg_diff:>+12,}   "
              f"{p.long_bias_label:<18}{p.momentum_label:<22}{p.alignment}")


def print_single_assets(assets: dict[str, CotData],
                        zscores: dict[str, ZScoreResult] | None = None) -> None:
    print()
    print("=" * 110)
    print("  COT NON-COMMERCIAL — Crypto / Indices / Commodities")
    print("=" * 110)
    print()
    header = (f"  {'Asset':<28}{'NET':>12}{'%OI':>8}{'ΔNET':>11}   "
              f"{'Bias':<22}{'Momentum':<22}")
    if zscores:
        header += f"{'z(26w)':>9}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    by_cat: dict[str, list[CotData]] = {}
    for a in assets.values():
        by_cat.setdefault(a.category, []).append(a)
    for cat in ["Crypto", "Indice", "Commodity"]:
        if cat not in by_cat:
            continue
        print(f"  --- {cat} ---")
        for a in sorted(by_cat[cat], key=lambda x: x.net_pct_oi, reverse=True):
            bias, momentum, _ = classify_single_asset(a)
            label = f"{a.code} ({a.name})"[:28]
            line = (f"  {label:<28}{a.net:>+12,}{a.net_pct_oi:>+7.1f}%{a.net_chg:>+11,}   "
                    f"{bias:<22}{momentum:<22}")
            if zscores and a.code in zscores:
                z = zscores[a.code]
                if z.z26 is not None:
                    line += f"{z.z26:>+9.2f}"
                else:
                    line += f"{'n/a':>9}"
            print(line)


# ===========================================================================
#  SELEZIONE TOP 3 SETUP
# ===========================================================================
@dataclass
class Setup:
    label: str           # e.g. "EUR/JPY" or "GOLD"
    asset_type: str      # "FX_PAIR" or "SINGLE"
    bias_label: str
    momentum_label: str
    net_value: int       # net_diff for pairs, absolute net for single assets
    chg_value: int       # chg_diff for pairs, net_chg for single assets
    strength_score: float
    z26: float | None = None    # z-score on the underlying asset (for SINGLE)
                                # or on the base currency (for FX_PAIR)
    z26_quote: float | None = None  # z-score of quote currency (FX_PAIR only)
    details: dict = field(default_factory=dict)


def _score_pair(p: PairBias) -> float:
    """Score normalizzato per una coppia FX (0-100 scale)."""
    # Net diff: ~50k = soglia minima setup pulito, ~200k = estremo
    bias_score = min(abs(p.net_diff) / 2000, 100)   # cap a 100
    # Momentum: ~5k = decente, ~30k = molto forte
    mom_score  = min(abs(p.chg_diff) / 300, 100)
    return bias_score * 0.7 + mom_score * 0.3


def _score_single(a: CotData) -> float:
    """Score normalizzato per asset singolo (0-100 scale)."""
    # Net%OI: 10% = soglia, 40%+ = estremo
    bias_score = min(abs(a.net_pct_oi) * 2.5, 100)
    # ΔNet relativo all'OI: 1% = decente, 3%+ = forte
    chg_pct = abs(a.net_chg) / a.open_interest * 100 if a.open_interest else 0
    mom_score = min(chg_pct * 33, 100)
    return bias_score * 0.7 + mom_score * 0.3


def select_top_3_setups(pairs: list[PairBias],
                        assets: dict[str, CotData],
                        currencies: dict[str, CotData],
                        zscores: dict[str, ZScoreResult] | None = None
                        ) -> list[Setup]:
    """Select the 3 cleanest setups (strong bias + aligned momentum).
    Forex pairs and single assets compete with a normalized 0-100 score.
    If z-scores are available, setups with extreme z-scores get a bonus."""
    setups: list[Setup] = []
    zscores = zscores or {}

    for p in pairs:
        if not p.is_clean_setup:
            continue
        base_code, quote_code = p.pair.split("/")
        base, quote = currencies[base_code], currencies[quote_code]
        score = _score_pair(p)
        z_base = zscores.get(base_code)
        z_quote = zscores.get(quote_code)
        # Bonus if either leg is at a statistical extreme
        z_bonus = 0.0
        if z_base and z_base.z26 is not None and abs(z_base.z26) > 1.5:
            z_bonus += min(abs(z_base.z26) * 5, 15)
        if z_quote and z_quote.z26 is not None and abs(z_quote.z26) > 1.5:
            z_bonus += min(abs(z_quote.z26) * 5, 15)

        setups.append(Setup(
            label=p.pair, asset_type="FX_PAIR",
            bias_label=p.long_bias_label, momentum_label=p.momentum_label,
            net_value=p.net_diff, chg_value=p.chg_diff,
            strength_score=score + z_bonus,
            z26=z_base.z26 if z_base else None,
            z26_quote=z_quote.z26 if z_quote else None,
            details={
                "pair": p.pair,
                "net_diff": p.net_diff, "chg_diff": p.chg_diff,
                "base": {"code": base.code, "long": base.nc_long, "short": base.nc_short,
                         "net": base.net, "net_chg": base.net_chg,
                         "z26": round(z_base.z26, 2) if z_base and z_base.z26 is not None else None,
                         "pct_rank_26w": round(z_base.pct_rank_26w, 0) if z_base and z_base.pct_rank_26w is not None else None},
                "quote": {"code": quote.code, "long": quote.nc_long, "short": quote.nc_short,
                          "net": quote.net, "net_chg": quote.net_chg,
                          "z26": round(z_quote.z26, 2) if z_quote and z_quote.z26 is not None else None,
                          "pct_rank_26w": round(z_quote.pct_rank_26w, 0) if z_quote and z_quote.pct_rank_26w is not None else None},
            }
        ))

    for a in assets.values():
        bias, momentum, is_clean = classify_single_asset(a)
        if not is_clean:
            continue
        score = _score_single(a)
        z = zscores.get(a.code)
        z_bonus = 0.0
        if z and z.z26 is not None and abs(z.z26) > 1.5:
            z_bonus = min(abs(z.z26) * 5, 15)

        setups.append(Setup(
            label=f"{a.code} ({a.name})", asset_type="SINGLE",
            bias_label=bias, momentum_label=momentum,
            net_value=a.net, chg_value=a.net_chg,
            strength_score=score + z_bonus,
            z26=z.z26 if z else None,
            details={
                "code": a.code, "name": a.name, "category": a.category,
                "long": a.nc_long, "short": a.nc_short,
                "net": a.net, "net_pct_oi": round(a.net_pct_oi, 2),
                "long_chg": a.nc_long_chg, "short_chg": a.nc_short_chg,
                "net_chg": a.net_chg, "open_interest": a.open_interest,
                "z26": round(z.z26, 2) if z and z.z26 is not None else None,
                "z52": round(z.z52, 2) if z and z.z52 is not None else None,
                "pct_rank_26w": round(z.pct_rank_26w, 0) if z and z.pct_rank_26w is not None else None,
            }
        ))

    setups.sort(key=lambda x: x.strength_score, reverse=True)
    return setups[:3]


# ===========================================================================
#  ANALISI AI VIA ANTHROPIC API
# ===========================================================================
def build_ai_prompt(top_setups: list[Setup], report_date: str) -> str:
    """Build the prompt with structured JSON data for the 3 setups."""
    setups_json = json.dumps(
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
Lo z-score è il dato più importante per capire se il setup è "affollato" o no.

Per OGNUNO dei 3 setup, fornisci un'analisi di ~180 parole strutturata così:

**[NOME SETUP]** — Bias COT: [direzione]

1. **Cosa dice il posizionamento**: usa il z26 e il percentile rank per quantificare quanto è "affollato" il trade. Es. "z=+1.8 significa che il posizionamento è ai massimi degli ultimi 6 mesi". Distinguere chiaramente tra net assoluto (poco utile da solo) e z-score (informativo).

2. **Cosa dice il momentum**: ΔNet conferma o smentisce il posizionamento? Se z26 è già estremo MA il momentum sta accelerando, è un caso particolarmente delicato (squeeze potenziale).

3. **Rischi e blind spot**:
   - Se z26 > 1.5 in valore assoluto → il setup è AFFOLLATO, unwind potenzialmente violento
   - Divergenze tra net assoluto e z-score (es. net alto ma z basso = "normale" per quell'asset)
   - Eventi macro noti che potrebbero ribaltare il quadro
   - Bias cognitivi tipici (FOMO, conferma)

4. **Note operative**: NON dare entry/stop/target. Suggerisci cosa cercare sul grafico per validare/invalidare il bias COT, e l'orizzonte temporale tipico per uno swing su questo asset.

Tono: diretto, professionale, critico. Niente disclaimer generici. Niente "consulta un advisor". Devil's advocate da collega esperto.

Chiudi con una sezione finale di ~100 parole: **CONTESTO INCROCIATO** — collega i 3 setup. Tema macro unificante? Coerenza interna? Quale dei 3 è più solido considerando z-score, momentum e potenziale affollamento? Quale è il più rischioso e perché?
"""


def call_anthropic_api(prompt: str, api_key: str) -> str | None:
    """Call the Claude API and return the text response. Returns None on any
    failure (never raises)."""
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
        r = requests.post(ANTHROPIC_API_URL, headers=headers,
                          json=payload, timeout=60)
    except requests.exceptions.Timeout:
        print(f"\n  ! API timeout after 60s", file=sys.stderr)
        return None
    except requests.exceptions.ConnectionError as e:
        print(f"\n  ! API connection error: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"\n  ! API call failed: {e}", file=sys.stderr)
        return None

    if r.status_code != 200:
        print(f"\n  ! API HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return None

    try:
        data = r.json()
        parts = [b.get("text", "") for b in data.get("content", [])
                 if b.get("type") == "text"]
        return "\n".join(parts).strip() or None
    except Exception as e:
        print(f"\n  ! Could not parse API response: {e}", file=sys.stderr)
        return None


def print_ai_review(top_setups: list[Setup], report_date: str) -> None:
    """Run the AI review. Never raises — logs and returns gracefully on any error."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    print()
    print("=" * 95)
    print("  🤖 AI REVIEW — Top 3 cleanest setups")
    print("=" * 95)

    if not top_setups:
        print()
        print("  No setups with aligned bias + momentum this week.")
        return

    if not api_key:
        print()
        print("  ⚠  ANTHROPIC_API_KEY environment variable not set.")
        print("     AI review SKIPPED — the rest of the analysis above is complete.")
        print("     To enable, export the key:  export ANTHROPIC_API_KEY='sk-ant-...'")
        print()
        print("  Selected setups that would have been analyzed:")
        for i, s in enumerate(top_setups, 1):
            z_info = f"  z26={s.z26:+.2f}" if s.z26 is not None else ""
            print(f"    {i}. {s.label}  ({s.bias_label} | {s.momentum_label}){z_info}")
        return

    print()
    print(f"  Model: {ANTHROPIC_MODEL}")
    print(f"  Setups: {', '.join(s.label for s in top_setups)}")
    print("  Generating analysis...", end="", flush=True)

    try:
        prompt = build_ai_prompt(top_setups, report_date)
        response = call_anthropic_api(prompt, api_key)
    except Exception as e:
        print(" FAILED")
        print(f"  ! Unexpected error during AI review: {e}", file=sys.stderr)
        print("  The rest of the analysis above is complete.")
        return

    if not response:
        print(" FAILED — see error above")
        print("  The rest of the analysis above is complete.")
        return

    print(" ok\n")
    print(response)
    print()


# ===========================================================================
#  MAIN
# ===========================================================================
def main():
    print("Downloading COT data from Tradingster.com...")

    # Currencies
    currencies: dict[str, CotData] = {}
    print("\n[Currencies]")
    for code, (name, cot_id) in CURRENCIES.items():
        print(f"  · {code} ({name})...", end="", flush=True)
        d = fetch_cot(code, name, cot_id, "FX")
        if d:
            currencies[code] = d
            print(f" ok  [net {d.net:+,}]")
        else:
            print(" FAILED")
        time.sleep(0.3)

    # Single assets
    assets: dict[str, CotData] = {}
    print("\n[Crypto / Indices / Commodities]")
    for code, (name, cot_id, category) in SINGLE_ASSETS.items():
        print(f"  · {code} ({name})...", end="", flush=True)
        d = fetch_cot(code, name, cot_id, category)
        if d:
            assets[code] = d
            print(f" ok  [net {d.net:+,}, %OI {d.net_pct_oi:+.1f}]")
        else:
            print(" FAILED")
        time.sleep(0.3)

    if not currencies and not assets:
        print("\nNo data downloaded.")
        return

    # Historical z-scores from CFTC API
    print("\n[Historical z-scores from CFTC API]")
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
    except Exception as e:
        print(f"  ! z-score computation failed: {e}", file=sys.stderr)
        print("  Continuing without z-scores.")
        zscores = {}

    # Text report
    if currencies:
        print_currencies(currencies, zscores)
        print_pair_matrix(currencies)
        pairs = get_all_pairs(currencies)
        print_pair_ranking(pairs)
    else:
        pairs = []

    if assets:
        print_single_assets(assets, zscores)

    # Top 3 + AI review
    top = select_top_3_setups(pairs, assets, currencies, zscores)
    all_data = list(currencies.values()) + list(assets.values())
    report_date = max(d.report_date for d in all_data) if all_data else "n/d"
    print_ai_review(top, report_date)


if __name__ == "__main__":
    main()
