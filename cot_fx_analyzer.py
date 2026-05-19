"""
COT Forex Cross Bias Analyzer
==============================
Scarica i dati COT Legacy (Futures Only) per le 8 valute principali dal sito
Tradingster.com, estrae SOLO i dati Non-Commercial (speculatori) e poi calcola
il bias di TUTTE le coppie incrociate possibili.

Logica:
  - Net NC = Long - Short  → bias direzionale di lungo periodo
  - ΔNet = ΔLong - ΔShort  → momentum settimanale (come è cambiato dall'ultimo report)
  - Per una coppia BASE/QUOTE:
        Net_pair  = Net(BASE) - Net(QUOTE)
        ΔNet_pair = ΔNet(BASE) - ΔNet(QUOTE)
    Positivo → bias bullish sulla coppia.

Nota sul USD: il COT non riporta direttamente la posizione speculativa su USD,
ma riporta il "U.S. Dollar Index" (DXY), che la usiamo come proxy per il dollaro
nelle coppie XXX/USD e USD/XXX.
"""

import re
import sys
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup


# --- Valute monitorate (codici COT Legacy) ---------------------------------
CURRENCIES = {
    "AUD": ("AUSTRALIAN DOLLAR",     "232741"),
    "GBP": ("BRITISH POUND",          "096742"),
    "CAD": ("CANADIAN DOLLAR",        "090741"),
    "EUR": ("EURO FX",                "099741"),
    "JPY": ("JAPANESE YEN",           "097741"),
    "CHF": ("SWISS FRANC",            "092741"),
    "NZD": ("NEW ZEALAND DOLLAR",     "112741"),
    "USD": ("U.S. DOLLAR INDEX",      "098662"),
}

BASE_URL = "https://www.tradingster.com/cot/legacy-futures/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


# --- Modello dati -----------------------------------------------------------
@dataclass
class Currency:
    code: str            # es. "EUR"
    name: str            # es. "EURO FX"
    report_date: str
    nc_long: int
    nc_short: int
    nc_long_chg: int     # variazione long dal report precedente
    nc_short_chg: int    # variazione short dal report precedente

    @property
    def net(self) -> int:
        """Net Non-Commercial: positivo = speculatori net long la valuta."""
        return self.nc_long - self.nc_short

    @property
    def net_chg(self) -> int:
        """Variazione settimanale del net = ΔLong - ΔShort.
        Positivo = la settimana ha aggiunto bias rialzista sulla valuta."""
        return self.nc_long_chg - self.nc_short_chg


# --- Scraper ----------------------------------------------------------------
def _to_int(token: str) -> int:
    token = token.replace(",", "").replace("+", "").strip()
    if token in ("", "-"):
        return 0
    return int(token)


def fetch_currency(code: str, name: str, cot_id: str, retries: int = 2) -> Currency | None:
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

    # Estraggo le righe della tabella con 9 celle numeriche
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

    # Schema 9 colonne: NC_L | NC_S | NC_Sprd | C_L | C_S | T_L | T_S | NR_L | NR_S
    return Currency(
        code=code,
        name=name,
        report_date=report_date,
        nc_long=positions[0],
        nc_short=positions[1],
        nc_long_chg=changes[0],
        nc_short_chg=changes[1],
    )


# --- Analisi singola valuta -------------------------------------------------
def classify_currency(c: Currency) -> str:
    """Etichetta sintetica del bias sulla valuta."""
    # Combino segno del net (lungo) con direzione del momentum (corto)
    if c.net > 0:
        long_bias = "Long"
    elif c.net < 0:
        long_bias = "Short"
    else:
        long_bias = "Flat"

    if c.net_chg > 0:
        momentum = "↑"   # accumulando long / scoprendo short
    elif c.net_chg < 0:
        momentum = "↓"   # scaricando long / aggiungendo short
    else:
        momentum = "→"

    return f"{long_bias} {momentum}"


# --- Analisi di coppia ------------------------------------------------------
@dataclass
class PairBias:
    pair: str           # es. "EUR/JPY"
    net_diff: int       # net(BASE) - net(QUOTE)
    chg_diff: int       # Δnet(BASE) - Δnet(QUOTE)

    @property
    def long_bias_label(self) -> str:
        """Bias di lungo basato sul net assoluto."""
        # Soglie tarate: il "rumore" tipico tra due valute è ~20-30k contratti
        if self.net_diff > 80_000:
            return "🟢🟢 BULL forte"
        if self.net_diff > 25_000:
            return "🟢 Bull"
        if self.net_diff < -80_000:
            return "🔴🔴 BEAR forte"
        if self.net_diff < -25_000:
            return "🔴 Bear"
        return "⚪ Neutro"

    @property
    def momentum_label(self) -> str:
        """Momentum settimanale: dove sta andando la differenza di posizionamento."""
        if self.chg_diff > 15_000:
            return "↑↑ accelera bull"
        if self.chg_diff > 3_000:
            return "↑ verso bull"
        if self.chg_diff < -15_000:
            return "↓↓ accelera bear"
        if self.chg_diff < -3_000:
            return "↓ verso bear"
        return "→ stabile"

    @property
    def alignment(self) -> str:
        """Coerenza tra bias di lungo e momentum: il segnale più forte è quando concordano."""
        long_pos = self.net_diff > 25_000
        long_neg = self.net_diff < -25_000
        mom_pos  = self.chg_diff > 3_000
        mom_neg  = self.chg_diff < -3_000

        if (long_pos and mom_pos) or (long_neg and mom_neg):
            return "✓ allineato"
        if (long_pos and mom_neg) or (long_neg and mom_pos):
            return "⚠ divergente"
        return "·"


def compute_pair(base: Currency, quote: Currency) -> PairBias:
    return PairBias(
        pair=f"{base.code}/{quote.code}",
        net_diff=base.net - quote.net,
        chg_diff=base.net_chg - quote.net_chg,
    )


# --- Report -----------------------------------------------------------------
def print_currencies(currencies: dict[str, Currency]) -> None:
    print()
    print("=" * 95)
    print(f"  COT NON-COMMERCIAL — Posizionamento singole valute")
    dates = {c.report_date for c in currencies.values()}
    print(f"  Data report: {', '.join(sorted(dates))}")
    print("=" * 95)
    print()
    print(f"{'Val':<6}{'Long':>12}{'Short':>12}{'NET':>14}"
          f"{'ΔLong':>12}{'ΔShort':>12}{'ΔNET':>12}   Bias")
    print("-" * 95)
    # Ordino per net decrescente per vedere subito la gerarchia di forza
    for c in sorted(currencies.values(), key=lambda x: x.net, reverse=True):
        bias = classify_currency(c)
        print(f"{c.code:<6}{c.nc_long:>12,}{c.nc_short:>12,}{c.net:>+14,}"
              f"{c.nc_long_chg:>+12,}{c.nc_short_chg:>+12,}{c.net_chg:>+12,}   {bias}")
    print()
    print("  Legenda:")
    print("    NET    = Long - Short (posizionamento corrente speculatori)")
    print("    ΔNET   = ΔLong - ΔShort (variazione dal report precedente)")
    print("    ↑ ↓ →  = direzione del momentum settimanale")


def print_pair_matrix(currencies: dict[str, Currency]) -> None:
    """Matrice di tutte le coppie incrociate: bias di lungo + momentum."""
    codes = ["EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "JPY", "USD"]
    codes = [c for c in codes if c in currencies]

    print()
    print("=" * 95)
    print("  MATRICE COPPIE INCROCIATE — Bias di lungo (Net NC base - Net NC quote)")
    print("=" * 95)
    print("  Lettura: riga / colonna. Valore = quanto la BASE è più forte della QUOTE")
    print("  in termini di posizionamento speculativo (in migliaia di contratti).")
    print()

    # Header
    print(f"  {'':<6}", end="")
    for q in codes:
        print(f"{q:>10}", end="")
    print()
    print("  " + "-" * (6 + 10 * len(codes)))

    for b in codes:
        print(f"  {b:<6}", end="")
        for q in codes:
            if b == q:
                print(f"{'·':>10}", end="")
            else:
                pair = compute_pair(currencies[b], currencies[q])
                # Valori in migliaia per leggibilità
                print(f"{pair.net_diff / 1000:>+10.1f}", end="")
        print()

    print()
    print("=" * 95)
    print("  MATRICE COPPIE INCROCIATE — Momentum settimanale (ΔNet base - ΔNet quote)")
    print("=" * 95)
    print()
    print(f"  {'':<6}", end="")
    for q in codes:
        print(f"{q:>10}", end="")
    print()
    print("  " + "-" * (6 + 10 * len(codes)))

    for b in codes:
        print(f"  {b:<6}", end="")
        for q in codes:
            if b == q:
                print(f"{'·':>10}", end="")
            else:
                pair = compute_pair(currencies[b], currencies[q])
                print(f"{pair.chg_diff / 1000:>+10.1f}", end="")
        print()


def print_pair_ranking(currencies: dict[str, Currency]) -> None:
    """Ranking delle coppie più forti/deboli con interpretazione."""
    codes = list(currencies.keys())
    pairs: list[PairBias] = []

    # Genero solo coppie uniche (evito EUR/USD e USD/EUR insieme)
    # Ordino le valute per "convenzione forex": EUR > GBP > AUD > NZD > USD > CAD > CHF > JPY
    fx_priority = ["EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY"]
    ordered = [c for c in fx_priority if c in codes]

    for i, b in enumerate(ordered):
        for q in ordered[i+1:]:
            pairs.append(compute_pair(currencies[b], currencies[q]))

    print()
    print("=" * 95)
    print("  RANKING COPPIE — Ordinate per intensità di bias (|net_diff|)")
    print("=" * 95)
    print()
    print(f"  {'Coppia':<10}{'Net diff':>12}{'ΔNet diff':>12}   "
          f"{'Bias lungo':<18}{'Momentum':<22}{'Allineamento'}")
    print("  " + "-" * 90)

    for p in sorted(pairs, key=lambda x: abs(x.net_diff), reverse=True):
        print(f"  {p.pair:<10}{p.net_diff:>+12,}{p.chg_diff:>+12,}   "
              f"{p.long_bias_label:<18}{p.momentum_label:<22}{p.alignment}")

    print()
    print("  Lettura:")
    print("    Net diff positivo grande → la BASE è molto più long della QUOTE → bull sulla coppia")
    print("    ΔNet diff stesso segno del net → momentum conferma il bias di lungo (✓ allineato)")
    print("    ΔNet diff segno opposto → smart money sta cambiando idea (⚠ divergente)")


def print_top_opportunities(currencies: dict[str, Currency]) -> None:
    """Trade setups più puliti: bias forte + momentum allineato."""
    fx_priority = ["EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY"]
    ordered = [c for c in fx_priority if c in currencies]
    pairs = [compute_pair(currencies[b], currencies[q])
             for i, b in enumerate(ordered) for q in ordered[i+1:]]

    aligned = [p for p in pairs
               if p.alignment == "✓ allineato" and abs(p.net_diff) > 25_000]
    aligned.sort(key=lambda x: abs(x.net_diff) + abs(x.chg_diff), reverse=True)

    divergent = [p for p in pairs
                 if p.alignment == "⚠ divergente" and abs(p.net_diff) > 25_000]
    divergent.sort(key=lambda x: abs(x.chg_diff), reverse=True)

    print()
    print("=" * 95)
    print("  COPPIE CON BIAS + MOMENTUM ALLINEATI (i setup più puliti)")
    print("=" * 95)
    if aligned:
        for p in aligned:
            direction = "LONG" if p.net_diff > 0 else "SHORT"
            print(f"  {direction:<6} {p.pair:<10}  "
                  f"net {p.net_diff:>+8,}  Δ {p.chg_diff:>+7,}  → "
                  f"{p.long_bias_label} + {p.momentum_label}")
    else:
        print("  Nessuna coppia con allineamento forte questa settimana.")

    print()
    print("=" * 95)
    print("  COPPIE DIVERGENTI (attenzione: lo speculatore sta cambiando idea)")
    print("=" * 95)
    if divergent:
        for p in divergent:
            print(f"  {p.pair:<10}  net {p.net_diff:>+8,}  Δ {p.chg_diff:>+7,}  → "
                  f"{p.long_bias_label} ma {p.momentum_label}")
    else:
        print("  Nessuna divergenza significativa questa settimana.")


# --- Main -------------------------------------------------------------------
def main():
    print("Scaricamento dati COT da Tradingster.com...")
    currencies: dict[str, Currency] = {}
    for code, (name, cot_id) in CURRENCIES.items():
        print(f"  · {code} ({name})...", end="", flush=True)
        c = fetch_currency(code, name, cot_id)
        if c:
            currencies[code] = c
            print(f" ok  [net {c.net:+,}, Δ {c.net_chg:+,}]")
        else:
            print(" FALLITO")
        time.sleep(0.4)

    if not currencies:
        print("\nNessun dato scaricato.")
        return

    print_currencies(currencies)
    print_pair_matrix(currencies)
    print_pair_ranking(currencies)
    print_top_opportunities(currencies)


if __name__ == "__main__":
    main()
