"""
instruments.py — Single source of truth for the 32 tracked instruments.

Each entry maps a short ticker (used in DB, URLs, UI) to:
  - cftc_code: the official CFTC contract market code
  - name: human-readable CFTC contract name
  - category: 'FX' | 'Crypto' | 'Index' | 'Rates' | 'Commodity'

When you add/remove an instrument, change it here only — everything else
(db, batch, web) reads from this dict.

NOTE on codes: some of the new indices and rates codes are best-effort
estimates. If the first batch run shows "(no new data)" for an instrument,
the CFTC code is likely wrong — verify on
  https://publicreporting.cftc.gov/resource/6dca-aqww.json?contract_market_name=...
and update the code below.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    ticker: str       # short code used internally (EUR, BTC, CL_NYMEX, ...)
    cftc_code: str    # official CFTC contract market code
    name: str         # human-readable name
    category: str     # FX | Crypto | Index | Rates | Commodity


INSTRUMENTS: dict[str, Instrument] = {
    # --- Forex (8) ---
    "AUD":      Instrument("AUD",      "232741", "AUSTRALIAN DOLLAR",       "FX"),
    "GBP":      Instrument("GBP",      "096742", "BRITISH POUND",            "FX"),
    "CAD":      Instrument("CAD",      "090741", "CANADIAN DOLLAR",          "FX"),
    "EUR":      Instrument("EUR",      "099741", "EURO FX",                  "FX"),
    "JPY":      Instrument("JPY",      "097741", "JAPANESE YEN",             "FX"),
    "CHF":      Instrument("CHF",      "092741", "SWISS FRANC",              "FX"),
    "NZD":      Instrument("NZD",      "112741", "NEW ZEALAND DOLLAR",       "FX"),
    "USD":      Instrument("USD",      "098662", "U.S. DOLLAR INDEX",        "FX"),

    # --- Crypto (2) ---
    "BTC":      Instrument("BTC",      "133741", "BITCOIN",                  "Crypto"),
    "ETH":      Instrument("ETH",      "146021", "ETHER CASH SETTLED",       "Crypto"),

    # --- Index (7) ---
    "SPX":      Instrument("SPX",      "13874+", "S&P 500 CONSOLIDATED",     "Index"),
    "ES":       Instrument("ES",       "13874A", "E-MINI S&P 500",           "Index"),
    "NQ":       Instrument("NQ",       "209742", "NASDAQ-100 MINI",          "Index"),
    "YM":       Instrument("YM",       "124603", "DOW JONES MINI ($5)",      "Index"),
    "RTY":      Instrument("RTY",      "239742", "RUSSELL 2000 MINI",        "Index"),
    "EMD":      Instrument("EMD",      "33874A", "E-MINI S&P 400",           "Index"),
    "VX":       Instrument("VX",       "1170E1", "VIX FUTURES",              "Index"),

    # --- Rates (6) ---
    "ZQ":       Instrument("ZQ",       "045601", "30-DAY FED FUNDS",         "Rates"),
    "ZT":       Instrument("ZT",       "042601", "2-YEAR T-NOTES",           "Rates"),
    "ZF":       Instrument("ZF",       "044601", "5-YEAR T-NOTES",           "Rates"),
    "ZN":       Instrument("ZN",       "043602", "10-YEAR T-NOTES",          "Rates"),
    "ZB":       Instrument("ZB",       "020601", "30-YEAR T-BOND",           "Rates"),
    "UB":       Instrument("UB",       "020604", "ULTRA T-BOND",             "Rates"),

    # --- Commodities (9) ---
    "CL_NYMEX": Instrument("CL_NYMEX", "067651", "CRUDE OIL WTI (NYMEX)",    "Commodity"),
    "CL_ICE":   Instrument("CL_ICE",   "067411", "CRUDE OIL WTI (ICE)",      "Commodity"),
    "NG":       Instrument("NG",       "023651", "NATURAL GAS",              "Commodity"),
    "GC":       Instrument("GC",       "088691", "GOLD",                     "Commodity"),
    "SI":       Instrument("SI",       "084691", "SILVER",                   "Commodity"),
    "HG":       Instrument("HG",       "085692", "COPPER #1",                "Commodity"),
    "PA":       Instrument("PA",       "075651", "PALLADIUM",                "Commodity"),
    "PL":       Instrument("PL",       "076651", "PLATINUM",                 "Commodity"),
    "AL":       Instrument("AL",       "191693", "ALUMINUM MWP",             "Commodity"),
}


def by_cftc_code(code: str) -> "Instrument | None":
    """Reverse lookup: given a CFTC code, find the instrument."""
    for inst in INSTRUMENTS.values():
        if inst.cftc_code == code:
            return inst
    return None


# Sanity check at import time
assert len(INSTRUMENTS) == 32, f"Expected 32 instruments, got {len(INSTRUMENTS)}"
