# Equity Interrogator

A chat app that answers conditional historical questions about stock behavior.
Ask things like "AAPL after 3 down weeks in a row" or "how often does GOOG
close above its intraday low when it drops 5%+", and the app queries a local
database of daily OHLCV data and writes you an answer.

## How it works

```
You type a question
   ↓
Streamlit UI
   ↓
Claude API (with tool use)
   ↓
One of three scenario functions in scenarios.py
   ↓
DuckDB query on equity.duckdb (10 years of daily bars)
   ↓
Results back to Claude
   ↓
Natural-language answer back to you
```

Three files do the real work:

- `setup_db.py` — runs ONCE to pull data from yfinance into DuckDB
- `scenarios.py` — the three analytical functions; add more here as you think of them
- `app.py` — Streamlit chat UI that wires it all together via Claude tool-use

---

## Path A: Run on Replit (recommended)

1. Go to [replit.com](https://replit.com), sign up, create a new Python Repl.
2. In the Repl, upload all the files from this folder (drag and drop into the file tree).
3. Click the "Secrets" tab (lock icon) in the left sidebar. Add a secret:
   - Key: `ANTHROPIC_API_KEY`
   - Value: your real key from [console.anthropic.com](https://console.anthropic.com)
4. Open the Replit shell (Tools → Shell) and run:
   ```
   pip install -r requirements.txt
   python setup_db.py
   ```
   The setup step takes about a minute and creates `equity.duckdb`.
5. In the shell, run:
   ```
   streamlit run app.py --server.port 8080 --server.address 0.0.0.0
   ```
6. Replit will show you a webview URL — that's your app. Bookmark it.

**Cost on Replit:** Free tier works for testing but the app sleeps when idle.
Replit Core ($20/mo) keeps it always-on with a public URL. Anthropic API costs
are pennies per query (Sonnet is ~$3 per million input tokens).

---

## Path B: Run locally

You'll need Python 3.10+ installed.

```bash
cd equity_app
python -m venv venv
source venv/bin/activate          # on Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env              # then edit .env and paste your real API key

python setup_db.py                # pulls ~10 years of data, takes ~1 minute
streamlit run app.py              # opens in your browser at localhost:8501
```

---

## Adding more tickers

Edit the `WATCHLIST` in `setup_db.py`, then re-run `python setup_db.py`.

## Adding more scenarios

Each scenario is one function in `scenarios.py` that takes a ticker plus
parameters and returns a dict. Then register it in two places:

1. Add it to `SCENARIO_REGISTRY` at the bottom of `scenarios.py`
2. Add a tool definition for it in the `TOOLS` list in `app.py`

That's it — Claude will start using it automatically when relevant questions come in.

## Refreshing data

Run `python setup_db.py` again. It re-fetches everything in the watchlist
and overwrites the existing rows. Run it nightly via a cron job if you want
fresh data.

## Limitations to keep in mind

- **yfinance is free but unofficial.** Quality is generally fine for daily
  bars, but earnings dates are spottier — small-caps and older history may
  be missing. For a production system upgrade to Polygon or EOD Historical Data.
- **Survivorship.** The watchlist is whatever you put in. You won't see
  delisted tickers unless you add them explicitly.
- **Adjusted vs unadjusted.** Both columns are stored; scenario functions
  use `close` (unadjusted) by default. For long-horizon return math, switch
  to `adj_close`.
- **Sample sizes are small.** "3 down weeks in a row" might only have ~15
  instances per ticker per decade. The app surfaces n so you can judge.
