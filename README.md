# COT Forex Cross Bias Analyzer

Tool a riga di comando che scarica i dati COT Legacy (Futures Only) da
Tradingster.com per le 8 valute principali, estrae il posizionamento
Non-Commercial e calcola la bias di tutte le 28 coppie incrociate.

## Setup con conda

### 1. Creazione dell'ambiente

```bash
conda env create -f environment.yml
```

Questo crea un environment chiamato `cot-fx` con Python 3.11, requests,
beautifulsoup4 e lxml.

### 2. Attivazione

```bash
conda activate cot-fx
```

### 3. Esecuzione

```bash
python cot_fx_analyzer.py
```

Lo script scarica i dati di tutte le valute (8 richieste HTTP, ~5 secondi
totali con il rate limit di cortesia) e stampa a video l'analisi completa.

## Cosa fa

Per ogni valuta (AUD, GBP, CAD, EUR, JPY, CHF, NZD, USD-Index) estrae solo
i dati **Non-Commercial** (speculatori):

- `NET = Long - Short` → posizionamento corrente (bias di lungo)
- `ΔNET = ΔLong - ΔShort` → variazione settimanale (momentum)

Poi per ogni coppia BASE/QUOTE calcola:

- `Net_pair = Net(BASE) - Net(QUOTE)`
- `ΔNet_pair = ΔNet(BASE) - ΔNet(QUOTE)`

Output in 4 sezioni:

1. **Tabella valute** ordinate per net decrescente
2. **Matrici 8×8** (bias di lungo + momentum) per ogni cross
3. **Ranking completo delle 28 coppie** con etichette di bias + allineamento
4. **Setup puliti** (bias e momentum concordi) e **divergenti**

## Note operative

- I report COT escono il **venerdì sera** (orario USA) con dati al martedì
  precedente. Lanciare lo script sabato/domenica è il momento più utile.
- Le soglie di classificazione sono in `cot_fx_analyzer.py`:
  - Bias coppia: `±25k` (moderato), `±80k` (forte)
  - Momentum: `±3k` (verso), `±15k` (accelera)
- Sono soglie ragionevoli ma non calibrate storicamente. Vanno tarate
  sull'asset se si nota troppo o troppo poco rumore.

## Aggiornare l'environment

```bash
conda env update -f environment.yml --prune
```

## Rimuovere l'environment

```bash
conda env remove -n cot-fx
```

## Disclaimer

Strumento di supporto all'analisi del posizionamento speculativo, non
genera segnali di trading. Il COT è un dato di **contesto** (medio periodo),
non un trigger di entry. Il report ha sempre un ritardo strutturale di
3-7 giorni rispetto al mercato.
