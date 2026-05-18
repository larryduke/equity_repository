# Equity Interrogator (Cloud Edition)

A chat app for answering conditional historical questions about stock behavior.
Cloud-hosted, free to run, refreshes data automatically every night.

## Stack
- **Polygon.io** ($29/mo) — daily OHLCV data, unlimited API calls
- **yfinance** (free) — earnings dates only
- **Supabase** (free tier) — cloud Postgres database
- **Streamlit Cloud** (free) — hosts the chat UI, gives you a public URL
- **GitHub Actions** (free) — runs the nightly data refresh
- **Anthropic API** (~$1–5/mo) — powers the natural language chat

**Total cost: $29/mo + a few dollars in API usage.**

## Files
- `app.py` — Streamlit chat UI
- `scenarios.py` — analytical functions (consecutive down periods, intraday drawdown, post-earnings recovery)
- `refresh_data.py` — pulls data from Polygon + yfinance into Supabase
- `.github/workflows/refresh.yml` — nightly cron job
- `requirements.txt` — Python deps

## First-time setup

You'll need accounts at: GitHub, Anthropic, Polygon, Supabase, Streamlit Cloud.
Step-by-step deployment instructions are in the conversation that produced this code.

## Adding scenarios

Each scenario is one function in `scenarios.py` that queries Postgres and
returns a dict. Then:
1. Add it to `SCENARIO_REGISTRY` at the bottom of `scenarios.py`
2. Add a tool definition for it in the `TOOLS` list in `app.py`
3. Commit and push — Streamlit Cloud auto-redeploys

## Adding tickers

Edit `WATCHLIST` in `refresh_data.py`, commit and push. The next nightly run
(or manual trigger via GitHub Actions) will fetch them.

## Manual data refresh

Either trigger the GitHub Action manually (Actions tab → Nightly data refresh
→ Run workflow), or run locally:
```bash
export POLYGON_API_KEY=...
export DATABASE_URL=...
python refresh_data.py
```

## Limitations
- Polygon Starter gives 5 years of history. Developer tier ($79) gives 10.
- yfinance earnings dates can be missing for less-liquid tickers.
- Sample sizes for streak/drawdown scenarios are typically 10–30 instances
  per ticker. The app surfaces n so you can judge significance.
