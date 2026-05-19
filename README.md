# COT Multi-Asset Bias Analyzer + AI Review

Tool a riga di comando che scarica i dati COT Legacy (Futures Only) da
Tradingster.com per **19 strumenti** (8 valute + 11 asset tra crypto, indice
e commodities), estrae il posizionamento Non-Commercial e produce un report
completo con valutazione AI dei 3 setup più puliti.

## Strumenti monitorati

| Categoria | Asset |
|---|---|
| **Forex** | AUD, GBP, CAD, EUR, JPY, CHF, NZD, USD Index |
| **Crypto** | Bitcoin, Ether (cash settled) |
| **Indice** | S&P 500 Consolidated |
| **Commodities** | Crude Oil WTI, Natural Gas, Gold, Silver, Copper, Palladium, Platinum, Aluminum MWP |

## Setup con conda

### 1. Crea l'environment

```bash
conda env create -f environment.yml
conda activate cot-fx
```

### 2. Imposta la chiave API Anthropic

```bash
# Linux / macOS
export ANTHROPIC_API_KEY='sk-ant-...'

# Windows PowerShell
$env:ANTHROPIC_API_KEY='sk-ant-...'

# Windows cmd
set ANTHROPIC_API_KEY=sk-ant-...
```

Per renderla persistente su Linux/macOS, aggiungila a `~/.bashrc` o `~/.zshrc`.
Su Windows usa "Variabili d'ambiente" nelle impostazioni di sistema.

> Senza la chiave lo script gira ugualmente, ma salta la sezione AI finale
> mostrando quali setup *sarebbero stati* analizzati.

### 3. Lancia lo script

```bash
python cot_fx_analyzer.py
```

## Cosa produce in output

1. **Tabella valute** ordinate per net decrescente (più long → più short)
2. **Due matrici 8×8** forex: bias di lungo e momentum settimanale
3. **Ranking completo** delle 28 coppie forex con etichette bias + allineamento
4. **Tabella crypto/indici/commodities** ordinata per categoria e %OI
5. **🤖 Analisi AI** dei 3 setup più puliti (bias forte + momentum allineato)

### Logica

Per ogni asset estraiamo **solo i dati Non-Commercial** (speculatori):

- `NET = Long - Short` → posizionamento corrente (bias di lungo)
- `ΔNET = ΔLong - ΔShort` → variazione settimanale (momentum)

**Per le coppie forex:**
- `Net_pair = Net(BASE) − Net(QUOTE)`
- `ΔNet_pair = ΔNet(BASE) − ΔNet(QUOTE)`

**Per gli asset singoli** (crypto/indici/commodity), il net viene normalizzato
sul net%OI per essere confrontabile (un net di 200k su Gold = +45% di OI è
molto più estremo di un net di 200k su Natural Gas che ha OI 4x più grande).

**Selezione top 3 per l'AI:** competono coppie FX e asset singoli con uno
scoring 0–100 (70% bias di lungo, 30% momentum). Solo i setup con bias +
momentum allineati sono candidati.

## L'analisi AI

Lo script invia un prompt strutturato a Claude (modello `claude-sonnet-4-6`)
con i dati JSON dei 3 setup, chiedendo per ognuno:

1. **Cosa dice il posizionamento** (è affollato? è un estremo?)
2. **Cosa dice il momentum** (conferma o smentisce?)
3. **Rischi e blind spot** (unwind, divergenze nascoste, eventi macro, bias cognitivi)
4. **Note operative** (cosa cercare sul grafico, orizzonte temporale)

Più una sezione finale di contesto incrociato che lega i 3 setup.

> Il prompt chiede esplicitamente all'AI di NON dare entry/stop/target.
> L'output è feedback critico, non advisory.

## Costo per esecuzione

Il prompt è circa **700 token in input** e **~2000 token in output**.
Con Sonnet 4.6 ($3 input / $15 output per MTok):

- Input:  700 × $3 / 1M  ≈ **$0.0021**
- Output: 2000 × $15 / 1M  ≈ **$0.030**
- **Totale: ~3 centesimi per esecuzione**

Lanciando lo script ogni sabato (quando esce il nuovo COT), spendi **circa
$1.50 all'anno**.

## Note operative

- I report COT escono il **venerdì sera** (orario USA) con dati al **martedì
  precedente**. Lanciare lo script sabato/domenica è il momento giusto.
- Soglie di classificazione attuali (in `cot_fx_analyzer.py`):
  - Bias coppia FX: `±25k` moderato, `±80k` forte
  - Bias asset singolo (net%OI): `±10%` moderato, `±25%` estremo
  - Momentum: rispettivamente `±3k` e `±0.5% OI`
- Sono soglie ragionevoli ma non calibrate storicamente. Si possono tarare
  modificando le funzioni `classify_*` nello script.

## Manutenzione conda

```bash
# Aggiorna le dipendenze
conda env update -f environment.yml --prune

# Rimuovi l'environment
conda env remove -n cot-fx
```

## Disclaimer

Strumento di supporto all'analisi del posizionamento speculativo, **non
genera segnali di trading**. Il COT è un dato di contesto (medio periodo),
non un trigger di entry. Il report ha sempre un ritardo strutturale di
3-7 giorni rispetto al mercato. L'analisi AI è un sounding board critico,
non advisory finanziaria.
