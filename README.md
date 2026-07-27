# COT Positioning Desk

A self-hosted analytical tool for the weekly **Commitments of Traders**
(CFTC) report. Tracks **Non-Commercial** speculator positioning across
**32 instruments**, computes historical z-scores, identifies the cleanest
setups each week, and serves everything through a fast web interface.

Designed as a **sounding board for swing traders** — positioning context,
not a signal generator.

---

## Tracked instruments (32)

| Category | Tickers |
|---|---|
| **Forex** (8) | AUD, GBP, CAD, EUR, JPY, CHF, NZD, USD Index |
| **Crypto** (2) | BTC, ETH |
| **Index** (7) | SPX (Consolidated), ES (E-mini S&P 500), NQ (Nasdaq-100), YM (Dow $5), RTY (Russell 2000), EMD (E-mini S&P 400), VX (VIX) |
| **Rates** (6) | ZQ (Fed Funds), ZT (2Y), ZF (5Y), ZN (10Y), ZB (30Y), UB (Ultra) |
| **Commodities** (9) | Crude WTI (NYMEX + ICE), Natural Gas, Gold, Silver, Copper, Palladium, Platinum, Aluminum MWP |

---

## Architecture

```
┌───────────────────┐    ┌────────────────────┐    ┌──────────────────┐
│  CFTC API         │──▶ │  sync.py           │──▶ │  PostgreSQL      │
│  (Socrata, free)  │    │  manual SYNC button│    │  cot_weekly      │
└───────────────────┘    │  (or python sync.py)│   └────────┬─────────┘
                         └────────────────────┘             │
                                                            ▼ (millisecond reads)
                                                   ┌──────────────────┐
                                                   │  Flask web app   │
                                                   │  + Chart.js UI   │
                                                   └──────────────────┘
```

- **Single source**: official CFTC Public Reporting API (Socrata). No scraping.
- **Persistent storage**: PostgreSQL accumulates the weekly history forever.
- **Web reads from DB**: clicking "Run Analysis" is instant — no API calls.
- **Manual sync**: the **SYNC** button first checks whether the DB is already
  up to date (no API call) and hits the CFTC API only when it's stale.
- **Idempotent sync**: re-runs are safe, upserts never duplicate.

---

## Features

- **Latest positioning** for all 32 instruments with one click
- **Historical z-scores** (26w + 52w) computed on the prior weeks only — no look-ahead bias
- **28 FX cross-pair biases** with momentum and alignment flags
- **Cross-pair matrices** (long-term bias + weekly momentum)
- **Top-3 cleanest setups** auto-selected (strong bias + aligned momentum, z-score bonus for extremes)
- **Historical charts** for any instrument (modal or full-page), 3M / 1Y / 2Y zoom, with Open Interest below
- **Backtest mode**: pick any past report date from the dropdown to recompute the entire analysis as of that date — z-scores honor the historical window
- **Optional AI review** of the top-3 setups via the Anthropic API (Claude Sonnet 4.6)

---

## Quick start (local)

### 1. Set up the environment

Using conda (recommended):
```bash
conda env create -f environment.yml
conda activate cot-fx
```

Or with pip:
```bash
pip install -r requirements.txt
```

### 2. Set the DB URL

```bash
# Linux/macOS
export DATABASE_URL='postgresql://user:pass@host:5432/dbname'

# Windows cmd
set DATABASE_URL=postgresql://user:pass@host:5432/dbname

# Windows PowerShell
$env:DATABASE_URL='postgresql://user:pass@host:5432/dbname'
```

### 3. Launch the web app

```bash
python app.py
```

Open <http://localhost:5000>.

### 4. Populate the database

Click the **⟳ SYNC** button in the header. The first sync downloads 2 years
of history for every instrument (~30 seconds); afterwards it's incremental —
SYNC first checks whether the DB is already up to date and only calls the
CFTC API when new reports exist.

You can also populate from the CLI (handy for a first backfill or a cron):

```bash
python sync.py
```

Then click **RUN ANALYSIS**.

### Optional: enable AI review

```bash
export ANTHROPIC_API_KEY='sk-ant-...'
```

Without it, the AI review section shows "skipped" and everything else works
normally.

---

## Data sync

The primary way to sync is the **⟳ SYNC** button in the web UI. The same
logic is also available from the CLI:

```bash
python sync.py                       # incremental (run after every weekly CFTC release)
python sync.py --backfill            # force re-download of 2 years for all instruments
python sync.py --instrument GC       # only one instrument (useful for debugging)
python sync.py --instrument ZB --backfill   # backfill a newly added instrument
```

Exit codes: `0` = success, `1` = partial failure, `2` = bad arguments.

---

## Project structure

```
cot_v5/
├── instruments.py        ← Single source of truth for the 32 instruments
├── db.py                 ← PostgreSQL layer (schema, upserts, queries)
├── cftc_client.py        ← CFTC API client (handles the '+' in S&P codes)
├── sync.py               ← Manual sync + freshness check (CFTC → DB)
├── core.py               ← Analysis: snapshots, z-scores, top-3, AI prompt
├── app.py                ← Flask web app
├── templates/
│   ├── index.html        ← Main dashboard (clickable tables + modal charts)
│   └── history.html      ← Full-page history view per instrument
├── environment.yml       ← Conda environment
├── requirements.txt      ← pip dependencies
├── Procfile              ← Heroku/Railway start command
├── gunicorn.conf.py      ← Production server config
├── railway.json          ← Railway web service config
├── railway-cron.json     ← Optional Railway cron config (runs python sync.py)
└── DEPLOY.md             ← Step-by-step Railway deployment guide
```

---

## Deploy on Railway

Full step-by-step instructions in **[DEPLOY.md](DEPLOY.md)**. Summary:

1. Push the code to GitHub
2. Create a **PostgreSQL** service in Railway
3. Create a **web service** from the GitHub repo
   - reference `DATABASE_URL` from Postgres
   - generate public domain

After deploy, open the app and hit the **⟳ SYNC** button to populate the DB
and to pull fresh CFTC data whenever a new report is out.

### Optional: automated weekly sync

If you'd rather not click SYNC manually, add a **cron service** from the same
repo using [railway-cron.json](railway-cron.json):

- start command: `python sync.py`
- cron schedule: `0 9 * * 6` (Saturday 09:00 UTC)
- restart policy: Never
- same `DATABASE_URL` reference

---

## How positions and signals are computed

For every instrument:

- `NET = NonComm_Long − NonComm_Short` (current bias)
- `ΔNET = ΔLong − ΔShort` (weekly momentum vs the prior report)
- `z(26w) = (NET − mean_26w) / stdev_26w` against the prior 26 weeks **excluding** the current observation
- `pct_rank_26w` = percentile rank of current NET within the prior 26 weeks

**Z-score interpretation**:

| \|z26\| | Meaning |
|---|---|
| > 2.0 | Statistical extreme (top/bottom ~2.5% of recent history) |
| > 1.5 | Stretched, watch for unwind |
| > 1.0 | Above-average positioning |
| < 1.0 | Within normal range |

**For FX pairs** (BASE/QUOTE): everything is computed as a difference
between the two legs (`net_BASE − net_QUOTE`). The same z-score logic
applies per currency, and the top-3 selector gives a bonus if either leg
is at a statistical extreme.

---

## Cost expectations

| Component | Resource use | Cost |
|---|---|---|
| Railway Postgres | <100 MB | Free tier |
| Railway Web | ~50 MB RAM idle | Free tier |
| Railway Cron (optional) | ~60 s/week | Almost zero |
| Anthropic AI review (Claude Sonnet 4.6) | ~3000 tokens/run | ~$0.04 per run, ~$2/year if weekly |

---

## Caveats and honest limitations

1. **The COT is a context indicator, not a leading signal.** It lags the
   market by 3-7 days. Use it for medium-term positioning context, never
   as an entry trigger.

2. **Z-score thresholds (±1.5, ±2.0) are conventional, not statistically
   calibrated** for each instrument. Different markets have different
   "normal" volatilities of positioning. After observing for a few months,
   you may want to recalibrate.

3. **Backtest mode is for studying past positioning, not for finding
   patterns that predict returns.** Cherry-picking dates where the COT
   "called" a move is a recipe for self-deception. The z-score is
   look-ahead-free, but a backtest run on a handful of hand-picked dates
   isn't a strategy validation.

4. **The Top-3 selector with 32 instruments tends to favor large-OI assets**
   (Treasuries, big indices). If you notice it gets monothematic, the
   scoring formula in `core.py` can be tuned to balance categories.

5. **The web app has no authentication.** Anyone who knows the URL can
   trigger analyses. If you deploy publicly and enable the AI review, also
   add basic auth or rate-limiting.

---

## Data source

All data comes from the official **CFTC Public Reporting Environment**
(Socrata API), dataset `6dca-aqww` — Commitments of Traders, Legacy Futures
Only. No authentication required, no rate limits in practice. The CFTC
publishes new reports every **Friday evening US time**, with data as of
the previous **Tuesday**.

---

## License & disclaimer

Personal-use analytical tool. Provided as-is, no warranty.

**This is not financial advice.** The output of this tool — including the
AI review — is informational only and should not be the sole basis for
trading decisions.
