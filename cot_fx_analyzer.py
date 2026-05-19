"""
COT Multi-Asset Bias Analyzer + AI Review
==========================================
Scarica i dati COT Legacy (Futures Only) da Tradingster.com per:
  - Forex: AUD, GBP, CAD, EUR, JPY, CHF, NZD, USD Index
  - Crypto: BITCOIN, ETHER (cash settled)
  - Indici: S&P 500 Consolidated
  - Commodities: Crude Oil, Natural Gas, Gold, Silver, Copper,
                 Palladium, Platinum, Aluminum MWP

Estrae SOLO i dati Non-Commercial (speculatori) e produce:
  1. Tabella posizionamento di ogni singolo asset/valuta
  2. Matrici 8x8 di tutte le coppie forex incrociate
  3. Ranking completo delle 28 coppie forex
  4. Top setup forex (bias + momentum allineati)
  5. Classifica singoli asset (crypto/indici/commodities)
  6. ⭐ Analisi AI dei 3 setup più puliti (via Anthropic API)

Per l'analisi AI serve la variabile d'ambiente ANTHROPIC_API_KEY.
Se non è impostata, lo script gira lo stesso ma salta la sezione AI.
"""

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup


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
        if self.net_diff > 80_000:   return "🟢🟢 BULL forte"
        if self.net_diff > 25_000:   return "🟢 Bull"
        if self.net_diff < -80_000:  return "🔴🔴 BEAR forte"
        if self.net_diff < -25_000:  return "🔴 Bear"
        return "⚪ Neutro"

    @property
    def momentum_label(self) -> str:
        if self.chg_diff > 15_000:   return "↑↑ accelera bull"
        if self.chg_diff > 3_000:    return "↑ verso bull"
        if self.chg_diff < -15_000:  return "↓↓ accelera bear"
        if self.chg_diff < -3_000:   return "↓ verso bear"
        return "→ stabile"

    @property
    def alignment(self) -> str:
        long_pos = self.net_diff > 25_000
        long_neg = self.net_diff < -25_000
        mom_pos  = self.chg_diff > 3_000
        mom_neg  = self.chg_diff < -3_000
        if (long_pos and mom_pos) or (long_neg and mom_neg):
            return "✓ allineato"
        if (long_pos and mom_neg) or (long_neg and mom_pos):
            return "⚠ divergente"
        return "·"

    @property
    def is_clean_setup(self) -> bool:
        return self.alignment == "✓ allineato" and abs(self.net_diff) > 25_000


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
    """
    Per asset singoli usiamo soglie sul net% di OI perché i volumi sono molto
    diversi (l'OI di Crude oil è 30x quello del Palladio).
    Ritorna (bias_label, momentum_label, is_clean_setup).
    """
    pct = a.net_pct_oi

    # Bias di lungo (% dell'OI)
    if pct > 25:    bias = "🟢🟢 BULL estremo"
    elif pct > 10:  bias = "🟢 Bull"
    elif pct < -25: bias = "🔴🔴 BEAR estremo"
    elif pct < -10: bias = "🔴 Bear"
    else:           bias = "⚪ Neutro"

    # Momentum (Δnet relativizzato all'OI)
    chg_pct = 100 * a.net_chg / a.open_interest if a.open_interest else 0
    if chg_pct > 2:     momentum = "↑↑ accelera bull"
    elif chg_pct > 0.5: momentum = "↑ verso bull"
    elif chg_pct < -2:  momentum = "↓↓ accelera bear"
    elif chg_pct < -0.5: momentum = "↓ verso bear"
    else:               momentum = "→ stabile"

    # Setup pulito: bias significativo + momentum nella stessa direzione
    is_clean = (
        (pct > 10 and chg_pct > 0.5) or
        (pct < -10 and chg_pct < -0.5)
    )

    return bias, momentum, is_clean


# ===========================================================================
#  OUTPUT TESTUALE
# ===========================================================================
def print_currencies(currencies: dict[str, CotData]) -> None:
    print()
    print("=" * 95)
    print(f"  COT NON-COMMERCIAL — Posizionamento singole valute")
    dates = {c.report_date for c in currencies.values()}
    print(f"  Data report: {', '.join(sorted(dates))}")
    print("=" * 95)
    print()
    print(f"{'Val':<6}{'Long':>12}{'Short':>12}{'NET':>14}"
          f"{'ΔLong':>12}{'ΔShort':>12}{'ΔNET':>12}")
    print("-" * 80)
    for c in sorted(currencies.values(), key=lambda x: x.net, reverse=True):
        print(f"{c.code:<6}{c.nc_long:>12,}{c.nc_short:>12,}{c.net:>+14,}"
              f"{c.nc_long_chg:>+12,}{c.nc_short_chg:>+12,}{c.net_chg:>+12,}")
    print()
    print("  NET = Long - Short (bias di lungo) | ΔNET = ΔLong - ΔShort (momentum)")


def print_pair_matrix(currencies: dict[str, CotData]) -> None:
    codes = [c for c in ["EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "JPY", "USD"]
             if c in currencies]

    print()
    print("=" * 95)
    print("  MATRICE COPPIE FOREX — Bias di lungo (Net base - Net quote, x1000 contratti)")
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
    print("  MATRICE COPPIE FOREX — Momentum settimanale (ΔNet base - ΔNet quote, x1000)")
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
    print("  RANKING COPPIE FOREX — Ordinate per intensità di bias")
    print("=" * 95)
    print()
    print(f"  {'Coppia':<10}{'Net diff':>12}{'ΔNet diff':>12}   "
          f"{'Bias lungo':<18}{'Momentum':<22}{'Allinea'}")
    print("  " + "-" * 88)
    for p in sorted(pairs, key=lambda x: abs(x.net_diff), reverse=True):
        print(f"  {p.pair:<10}{p.net_diff:>+12,}{p.chg_diff:>+12,}   "
              f"{p.long_bias_label:<18}{p.momentum_label:<22}{p.alignment}")


def print_single_assets(assets: dict[str, CotData]) -> None:
    print()
    print("=" * 95)
    print("  COT NON-COMMERCIAL — Crypto / Indici / Commodities")
    print("=" * 95)
    print()
    print(f"  {'Asset':<28}{'NET':>12}{'%OI':>8}{'ΔNET':>12}   "
          f"{'Bias':<22}{'Momentum'}")
    print("  " + "-" * 90)
    # Ordino per categoria poi per net% decrescente
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
            print(f"  {label:<28}{a.net:>+12,}{a.net_pct_oi:>+7.1f}%{a.net_chg:>+12,}   "
                  f"{bias:<22}{momentum}")


# ===========================================================================
#  SELEZIONE TOP 3 SETUP
# ===========================================================================
@dataclass
class Setup:
    label: str           # es. "EUR/JPY" o "GOLD"
    asset_type: str      # "FX_PAIR" o "SINGLE"
    bias_label: str
    momentum_label: str
    net_value: int       # net_diff per le coppie, net assoluto per asset singoli
    chg_value: int       # chg_diff per le coppie, net_chg per asset singoli
    strength_score: float  # per il ranking
    details: dict        # tutti i dati per il prompt AI


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
                        currencies: dict[str, CotData]) -> list[Setup]:
    """Seleziona i 3 setup più puliti (bias forte + momentum allineato).
    Coppie forex e asset singoli competono con scoring normalizzato 0-100."""
    setups: list[Setup] = []

    # Coppie forex
    for p in pairs:
        if not p.is_clean_setup:
            continue
        base_code, quote_code = p.pair.split("/")
        base, quote = currencies[base_code], currencies[quote_code]
        setups.append(Setup(
            label=p.pair, asset_type="FX_PAIR",
            bias_label=p.long_bias_label, momentum_label=p.momentum_label,
            net_value=p.net_diff, chg_value=p.chg_diff,
            strength_score=_score_pair(p),
            details={
                "pair": p.pair,
                "net_diff": p.net_diff, "chg_diff": p.chg_diff,
                "base": {"code": base.code, "long": base.nc_long, "short": base.nc_short,
                         "net": base.net, "net_chg": base.net_chg},
                "quote": {"code": quote.code, "long": quote.nc_long, "short": quote.nc_short,
                          "net": quote.net, "net_chg": quote.net_chg},
            }
        ))

    # Asset singoli
    for a in assets.values():
        bias, momentum, is_clean = classify_single_asset(a)
        if not is_clean:
            continue
        setups.append(Setup(
            label=f"{a.code} ({a.name})", asset_type="SINGLE",
            bias_label=bias, momentum_label=momentum,
            net_value=a.net, chg_value=a.net_chg,
            strength_score=_score_single(a),
            details={
                "code": a.code, "name": a.name, "category": a.category,
                "long": a.nc_long, "short": a.nc_short,
                "net": a.net, "net_pct_oi": round(a.net_pct_oi, 2),
                "long_chg": a.nc_long_chg, "short_chg": a.nc_short_chg,
                "net_chg": a.net_chg, "open_interest": a.open_interest,
            }
        ))

    setups.sort(key=lambda x: x.strength_score, reverse=True)
    return setups[:3]


# ===========================================================================
#  ANALISI AI VIA ANTHROPIC API
# ===========================================================================
def build_ai_prompt(top_setups: list[Setup], report_date: str) -> str:
    """Costruisce il prompt per Claude con i dati strutturati dei 3 setup."""
    setups_json = json.dumps(
        [{"label": s.label, "type": s.asset_type, "bias": s.bias_label,
          "momentum": s.momentum_label, **s.details} for s in top_setups],
        indent=2, ensure_ascii=False
    )

    return f"""Sei un analista esperto di posizionamento COT (Commitment of Traders) che assiste un trader prop swing. Stai facendo da sounding board critico, NON da advisor.

DATI: COT Legacy Report Non-Commercial al {report_date}, i 3 setup con bias di lungo + momentum settimanale allineati:

```json
{setups_json}
```

Per OGNUNO dei 3 setup, fornisci un'analisi di ~150 parole strutturata così:

**[NOME SETUP]** — Bias COT: [direzione]

1. **Cosa dice il posizionamento**: spiega in 2 frasi cosa significa concretamente il net (es. quanto sono affollati gli speculatori, se è un estremo storico, ecc).

2. **Cosa dice il momentum**: spiega come il ΔNet sta confermando o smentendo il posizionamento, e cosa implica per la prossima settimana.

3. **Rischi e blind spot**: identifica:
   - se il setup è AFFOLLATO (rischio di unwind violento)
   - se ci sono divergenze nascoste (es. il momentum sta rallentando anche se ancora positivo)
   - eventi macro noti che potrebbero ribaltare il quadro
   - bias cognitivi tipici in cui un trader potrebbe cadere su questo setup

4. **Note operative**: NON dare entry/stop/target. Suggerisci invece *cosa cercare* sul grafico per validare il bias COT (es. "cerca rotture di range con volume", "attendi pullback su EMA"), e quale orizzonte temporale ha senso per uno swing trade su questo asset.

Tono: diretto, professionale, critico. Niente disclaimer generici. Non dire "considera di consultare un advisor". Parla come un collega esperto che fa devil's advocate.

Chiudi con una sezione finale di 80 parole: **CONTESTO INCROCIATO** — collega i 3 setup: c'è un tema macro che li unifica? Sono mutuamente coerenti o c'è qualche contraddizione? Quale dei 3 ti sembra più solido e perché?
"""


def call_anthropic_api(prompt: str, api_key: str) -> str | None:
    """Chiama l'API Claude e ritorna la risposta testuale."""
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
        r.raise_for_status()
        data = r.json()
        # data.content è una lista di blocchi, prendiamo i text
        parts = [b.get("text", "") for b in data.get("content", [])
                 if b.get("type") == "text"]
        return "\n".join(parts).strip()
    except requests.HTTPError as e:
        print(f"\n  ! API error {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"\n  ! Errore chiamata API: {e}", file=sys.stderr)
        return None


def print_ai_review(top_setups: list[Setup], report_date: str) -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    print()
    print("=" * 95)
    print("  🤖 ANALISI AI — Top 3 setup più puliti")
    print("=" * 95)

    if not api_key:
        print()
        print("  ⚠ Variabile ANTHROPIC_API_KEY non impostata. Salto l'analisi AI.")
        print("    Esporta la chiave con:  export ANTHROPIC_API_KEY='sk-ant-...'")
        print()
        print("  I 3 setup selezionati per l'analisi sarebbero stati:")
        for i, s in enumerate(top_setups, 1):
            print(f"    {i}. {s.label}  ({s.bias_label} | {s.momentum_label})")
        return

    if not top_setups:
        print()
        print("  Nessun setup con bias + momentum allineati questa settimana.")
        return

    print()
    print(f"  Modello: {ANTHROPIC_MODEL}")
    print(f"  Setup analizzati: {', '.join(s.label for s in top_setups)}")
    print("  Generazione in corso...", end="", flush=True)

    prompt = build_ai_prompt(top_setups, report_date)
    response = call_anthropic_api(prompt, api_key)

    if not response:
        print(" FALLITO")
        return

    print(" ok\n")
    print(response)
    print()


# ===========================================================================
#  MAIN
# ===========================================================================
def main():
    print("Scaricamento dati COT da Tradingster.com...")

    # Valute
    currencies: dict[str, CotData] = {}
    print("\n[Valute]")
    for code, (name, cot_id) in CURRENCIES.items():
        print(f"  · {code} ({name})...", end="", flush=True)
        d = fetch_cot(code, name, cot_id, "FX")
        if d:
            currencies[code] = d
            print(f" ok  [net {d.net:+,}]")
        else:
            print(" FALLITO")
        time.sleep(0.3)

    # Asset singoli
    assets: dict[str, CotData] = {}
    print("\n[Crypto / Indici / Commodities]")
    for code, (name, cot_id, category) in SINGLE_ASSETS.items():
        print(f"  · {code} ({name})...", end="", flush=True)
        d = fetch_cot(code, name, cot_id, category)
        if d:
            assets[code] = d
            print(f" ok  [net {d.net:+,}, %OI {d.net_pct_oi:+.1f}]")
        else:
            print(" FALLITO")
        time.sleep(0.3)

    if not currencies and not assets:
        print("\nNessun dato scaricato.")
        return

    # Report testuale
    if currencies:
        print_currencies(currencies)
        print_pair_matrix(currencies)
        pairs = get_all_pairs(currencies)
        print_pair_ranking(pairs)
    else:
        pairs = []

    if assets:
        print_single_assets(assets)

    # Top 3 + analisi AI
    top = select_top_3_setups(pairs, assets, currencies)
    # Data del report più "fresca" tra tutti i dati
    all_data = list(currencies.values()) + list(assets.values())
    report_date = max(d.report_date for d in all_data) if all_data else "n/d"
    print_ai_review(top, report_date)


if __name__ == "__main__":
    main()
