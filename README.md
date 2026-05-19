# COT Multi-Asset Bias Analyzer + AI Review

A command-line tool that scrapes COT Legacy (Futures Only) data from
Tradingster.com for **19 instruments** (8 currencies + 11 single assets
across crypto, indices and commodities), pulls **historical z-scores** from
the official CFTC API, and produces a full positioning report with an
optional AI review of the 3 cleanest setups.

## Tracked instruments

| Category | Assets |
|---|---|
| **Forex** | AUD, GBP, CAD, EUR, JPY, CHF, NZD, USD Index |
| **Crypto** | Bitcoin, Ether (cash settled) |
| **Index** | S&P 500 Consolidated |
| **Commodities** | Crude Oil WTI, Natural Gas, Gold, Silver, Copper, Palladium, Platinum, Aluminum MWP |

## What it does

For every instrument the tool extracts **Non-Commercial** (speculator) data
only and computes three independent signals:

- **NET = Long − Short** → current positioning (long-term bias)
- **ΔNET = ΔLong − ΔShort** → weekly change (short-term momentum)
- **z(26w)** → z-score of current net vs the prior 26-week distribution
  (from the official CFTC Public Reporting API)

The z-score is the key innovation: an absolute net of +170k contracts means
nothing on its own. Is +170k a historical extreme (signal: crowded, unwind
risk) or perfectly normal range (signal: ignore the headline number)? The
z-score answers that question.

**Interpretation:**

| z-score | Meaning |
|---|---|
| `\|z\| > 2.0` | Statistical extreme (top/bottom ~2.5% of recent history) |
| `\|z\| > 1.5` | Stretched positioning, watch for unwind |
| `\|z\| > 1.0` | Above-average positioning |
| `\|z\| < 1.0` | Normal range |

## Setup with conda

### 1. Create the environment

```bash
conda env create -f environment.yml
conda activate cot-fx
```

### 2. (Optional) Set the Anthropic API key

```bash
# Linux / macOS
export ANTHROPIC_API_KEY='sk-ant-...'

# Windows PowerShell
$env:ANTHROPIC_API_KEY='sk-ant-...'

# Windows cmd
set ANTHROPIC_API_KEY=sk-ant-...
```

To persist on Linux/macOS, add the line to `~/.bashrc` or `~/.zshrc`. On
Windows use Settings → Environment Variables.

> **Without the key the script still runs in full.** The AI review section
> is skipped and a clear log message tells you why. All other analysis
> (scraping, z-scores, ranking, top-3 selection) completes normally.

### 3. Run

```bash
python cot_fx_analyzer.py
```

## Output sections

1. **Currency table** sorted by net descending, with z-scores and percentile ranks
2. **Two 8×8 forex matrices**: long-term bias and weekly momentum
3. **Full ranking** of the 28 forex pairs with bias + alignment labels
4. **Single-asset table** grouped by category, with z-scores
5. **🤖 AI review** of the top-3 setups (if API key is set)

## Top-3 selection logic

A "clean setup" requires both:
- significant bias (≥ |25k| for FX pairs, ≥ |10%| of OI for single assets)
- aligned momentum (ΔNet pushing in the same direction)

Selected setups are scored 0–100 (70% bias, 30% momentum). Setups with
extreme z-scores (`|z26| > 1.5`) get a score bonus of up to +15 because
historical context matters: a strong bias that is ALSO a statistical extreme
is operationally more interesting than a strong bias at "normal" levels.

## AI review

When the API key is set, the script sends a structured prompt to Claude
(`claude-sonnet-4-6`) with the JSON data of the 3 setups, **including their
z-scores**. For each setup the model produces ~180 words covering:

1. **What the positioning says** — quantified via z-score, not just headline net
2. **What the momentum says** — confirming or contradicting?
3. **Risks and blind spots** — crowding, hidden divergences, macro events, cognitive biases
4. **Operational notes** — what to look for on the chart, typical timeframe

Plus a final "cross-context" section linking the 3 setups: macro theme,
internal consistency, and which one is most/least solid given the z-scores.

The prompt explicitly bans entry/stop/target suggestions and generic
disclaimers. The output is critical feedback, not advisory.

## Cost per run

Typical prompt: ~800 input tokens, ~2400 output tokens. With Sonnet 4.6
($3 input / $15 output per MTok):

- Input:  800 × $3 / 1M  ≈ **$0.0024**
- Output: 2400 × $15 / 1M  ≈ **$0.036**
- **Total: ~4 cents per run**

Running once a week (Saturday, after the Friday CFTC release) → roughly
**$2/year**.

## Historical data cache

The CFTC API is queried once per instrument to download the last 60 weeks
of data. Results are cached locally in `.cot_history_cache.json` (next to
the script) and automatically refreshed every 6 days. First run downloads
~60 KB total; subsequent runs hit the cache instantly.

To force a refresh, delete the cache file:
```bash
rm .cot_history_cache.json
```

## Tweaking thresholds

All thresholds live in `cot_fx_analyzer.py`. Current defaults:

- **FX pair bias:** ±25k moderate, ±80k strong
- **Single-asset bias (net%OI):** ±10% moderate, ±25% extreme
- **Momentum FX:** ±3k weak, ±15k strong
- **Momentum single asset:** ±0.5% OI weak, ±2% OI strong
- **Z-score extreme:** |z| > 1.5

These are reasonable but not statistically calibrated. After a few weeks of
observation you may want to retune for your asset class.

## Operational notes

- COT reports are released **Friday evening US time** with data as of the
  previous **Tuesday**. Running this script Saturday/Sunday is the right
  cadence.
- Some assets (Bitcoin in particular) are released with variable delay by
  the CFTC. The script handles asymmetric report dates gracefully.
- The z-score uses the prior 26 weeks (excluding the current observation)
  so it measures "how unusual is today vs the past 6 months."

## Conda maintenance

```bash
# Update dependencies
conda env update -f environment.yml --prune

# Remove the environment
conda env remove -n cot-fx
```

## File structure

```
cot_fx_analyzer/
├── cot_fx_analyzer.py      # main script
├── cot_history.py          # CFTC API + z-score module
├── environment.yml         # conda environment definition
├── README.md               # this file
└── .cot_history_cache.json # (created on first run, ~60 KB)
```

## Disclaimer

Positioning analysis tool, **not a trading signal generator**. The COT
report is medium-term context, not an entry trigger. It always lags the
market by 3–7 days. The AI review is a critical sounding board, not
financial advice.
