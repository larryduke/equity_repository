# Phase 1 Migration — Polygon prototype → FMP text-to-SQL

This wipes the old prototype tables and stands up the new schema with all
calculated indicators, fundamentals, earnings, ticker reference, and macro.
Estimated time: ~3 hours total, most of which is the initial historical
data load running unattended.

## What's changing

| Before | After |
|---|---|
| Polygon ($29/mo) | FMP ($49/mo) — cancel Polygon at the end |
| 3 fixed scenarios | Text-to-SQL: any question the data can answer |
| Markdown text replies | Structured component-based UI |
| 14 tickers | ~1,400 US tickers, S&P 500 + 400 + 600 + NDX, $500M+ |
| Prices + earnings only | + fundamentals, technicals, VIX, FRED macro, breadth |

## Prerequisites — already done

- ✅ FMP Premium API key (in your notes)
- ✅ Anthropic API key (already in GitHub + Streamlit Secrets)
- ✅ Supabase project (we'll reuse it, but wipe the tables)
- ✅ GitHub repo (we'll replace the files)
- ✅ Streamlit Cloud app (will auto-redeploy)

## Step 1 — Get a FRED API key (free, 2 minutes)

The FRED API powers Fed Funds Rate, yield curve, CPI, unemployment.

1. Go to https://fred.stlouisfed.org/docs/api/api_key.html
2. Click "Request or view your API key"
3. Sign in (or create a free account)
4. Click "Request API Key" → fill in: app name "Equity Interrogator", description "personal research tool"
5. Submit. The key is granted instantly.
6. Copy the key to your notes app.

> FRED is run by the St. Louis Fed. No limits worth noting. Completely free.

## Step 2 — Wipe the old Supabase tables and run the new schema

1. Open your Supabase project
2. Left sidebar → **SQL Editor** (icon looks like </>)
3. Click **+ New query**
4. Open `schema.sql` (downloaded from this chat) and paste the entire contents
5. **Before clicking Run**, find this line near the bottom:
   ```sql
   CREATE ROLE query_reader WITH LOGIN PASSWORD 'CHANGE_ME_IN_SUPABASE';
   ```
   Replace `CHANGE_ME_IN_SUPABASE` with a random strong password. Save this
   password to your notes — you'll need it next step.
6. Click **Run** (or Cmd/Ctrl+Enter)
7. You should see "Success" and no errors. The left sidebar's Table Editor
   should now show 6 tables: `tickers`, `daily_bars`, `fundamentals`,
   `earnings_dates`, `market_indicators`, `macro_events`.

## Step 3 — Build the read-only query connection string

The text-to-SQL engine connects as `query_reader`, not the admin user, so
it physically cannot modify data even if Claude wrote something destructive.

Take your existing `DATABASE_URL`:
```
postgresql://postgres.xxxxx:ADMIN-PASSWORD@aws-1-us-west-1.pooler.supabase.com:6543/postgres
```

Build the read-only version by swapping the user/password:
```
postgresql://query_reader:READER-PASSWORD@aws-1-us-west-1.pooler.supabase.com:6543/postgres
```

Where `READER-PASSWORD` is what you set in Step 2.

> Note: Supabase pooler usernames look like `postgres.xxxxx` because it
> embeds the project ID. For custom roles, the format is just the role name.
> If you hit auth errors with the pooler URL, try the direct connection
> string instead (port 5432, host `db.xxxxx.supabase.co`).

## Step 4 — Update GitHub Secrets

Repo → Settings → Secrets and variables → Actions.

**Add:**
- `FMP_API_KEY` — your FMP Premium key
- `FRED_API_KEY` — the key from Step 1
- `QUERY_DATABASE_URL` — the read-only URL from Step 3 (Streamlit will use this for queries)

**Delete:**
- `POLYGON_API_KEY` — no longer needed

**Keep as-is:**
- `DATABASE_URL` — still the admin URL, used by refresh_data.py to write
- `ANTHROPIC_API_KEY`

## Step 5 — Update Streamlit Cloud secrets

Open your Streamlit Cloud app → Settings → Secrets. Replace the contents with:

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
DATABASE_URL = "postgresql://postgres.xxxxx:ADMIN-PASSWORD@aws-1-us-west-1.pooler.supabase.com:6543/postgres"
QUERY_DATABASE_URL = "postgresql://query_reader:READER-PASSWORD@aws-1-us-west-1.pooler.supabase.com:6543/postgres"
```

Save. Don't reboot yet — we're about to push new code anyway.

## Step 6 — Push the new code

Replace all the files in your GitHub repo with the new Phase 1 files:

- `app.py` (replaces old)
- `refresh_data.py` (replaces old)
- `query_engine.py` (NEW)
- `response_formatter.py` (NEW)
- `load_tickers.py` (NEW)
- `schema.sql` (NEW — for reference, you already ran it)
- `requirements.txt` (updated)
- `.github/workflows/refresh.yml` (updated)
- `MIGRATION.md` (this file, for reference)

**Delete from repo:**
- `scenarios.py`
- `setup_db_polygon.py` (if it's still there)

You can do this in two ways:

### Option A: Bulk upload via web UI (easier)
1. In your repo, delete the old files one by one (click file → trash icon → commit)
2. Then upload the new files: **Add file → Upload files** → drag the new ones
3. Commit

### Option B: Local clone + git push (faster if you have git installed)
```bash
git clone https://github.com/larryduke/equity_repository
cd equity_repository
# replace files
git add . && git commit -m "Phase 1: text-to-SQL rebuild on FMP"
git push
```

## Step 7 — Load the ticker universe (one-time, ~5 min)

This is the **only step you need a local Python install for**, OR you can
do it on a GitHub Actions manual workflow if you don't want Python on your
laptop.

### Easiest path: GitHub Actions one-shot

Create a temporary workflow file `.github/workflows/one_shot.yml`:

```yaml
name: One-shot scripts
on:
  workflow_dispatch:
    inputs:
      script:
        description: 'Script to run'
        required: true
        type: choice
        options: ['load_tickers.py', 'refresh_data.py --full', 'refresh_data.py --full --limit 50']
jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 360
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11', cache: 'pip' }
      - run: pip install -r requirements.txt
      - env:
          FMP_API_KEY:  ${{ secrets.FMP_API_KEY }}
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          FRED_API_KEY: ${{ secrets.FRED_API_KEY }}
        run: python ${{ inputs.script }}
```

Push it. Then:
1. Actions tab → "One-shot scripts" → Run workflow
2. Pick `load_tickers.py` → Run

Watch the log. Takes ~3 min. When it finishes, your `tickers` table will
have ~1,400 rows.

### Alternative: local run

If you have Python 3.11 installed:
```bash
export FMP_API_KEY=...
export DATABASE_URL=...
pip install -r requirements.txt
python load_tickers.py
```

## Step 8 — Sanity check with 50 tickers (~10 min)

Don't run the full historical load yet. Test the pipeline first.

Trigger the one-shot workflow with `refresh_data.py --full --limit 50`.

This pulls 5 years of daily prices, calculates indicators, and loads
earnings + fundamentals for 50 tickers. ~5-10 minutes.

Check the result:
- Supabase → Table Editor → `daily_bars` → should have ~60,000 rows
- Spot-check: pick AAPL, look at the most recent row — `rsi_14` should be
  a number between 0 and 100, `ma_200` should be in the same ballpark as
  the close price, `pct_vs_52w_high` should be 0 or negative.

If that all looks right, proceed.

## Step 9 — Full historical load (~30-45 min, unattended)

Trigger the one-shot workflow with `refresh_data.py --full`.

This loads all ~1,400 tickers with 5 years of indicator-augmented daily
data, plus fundamentals and earnings. Takes 30-45 minutes depending on
FMP response times. **You can close the browser** — GitHub keeps running.

While it runs, check progress occasionally in the Actions log. Look for:
```
[25/1400] AAPL: ok (25 done, 0 failed)
[50/1400] ABBV: ok (50 done, 0 failed)
...
```

A few failures (5-10) are normal — some tickers FMP doesn't cover well.

When done, the last lines will be:
```
Refreshing market indicators...
  fetching VIX from yfinance...
    1250 VIX rows
  fetching FRED macro series...
    1800 macro rows
  calculating market breadth from daily_bars...
    breadth updated
Done.
```

## Step 10 — Verify Streamlit Cloud auto-redeployed

Open your Streamlit app URL. It should already be running the new code
(Streamlit Cloud auto-deploys on git push).

Test queries:

1. **"AAPL after 3 down weeks in a row"** — should return metric cards +
   event list, plus follow-up suggestions
2. **"Tech stocks with RSI below 35 today"** — should return a ranking
   list of tickers with their RSI values
3. **"Current market signals — are we oversold?"** — should return a
   signal grid with VIX, breadth, yield curve, etc.

Each query takes ~5-15 seconds (Claude generates SQL, runs it, Claude
formats the response).

## Step 11 — Cancel Polygon

Once everything above works for a day or two, cancel your Polygon
subscription. Login at polygon.io → Account → Cancel.

You'll save $29/mo. New monthly cost:
- FMP Premium: $49
- Supabase: $0
- Streamlit Cloud: $0
- GitHub Actions: $0
- Anthropic API: ~$3-5
- **Total: ~$54/mo**

## Step 12 — Confirm nightly cron is configured

The new `.github/workflows/refresh.yml` will run automatically every night
at 06:00 UTC (11pm Pacific). To verify:

1. Actions tab → "Nightly data refresh" should be listed
2. Wait until the next run, or trigger manually to test

The incremental mode runs in ~5-10 minutes and only updates the most
recent days. The fundamentals snapshot is refreshed every night too, so
your P/E / market cap stays current.

---

## Common gotchas

**"role query_reader does not exist"** — you ran `schema.sql` but maybe
skipped the role section. Re-run just the bottom block of `schema.sql`
(from `DROP ROLE IF EXISTS query_reader` to the end).

**Streamlit shows "ANTHROPIC_API_KEY missing"** — secrets in Streamlit
Cloud take ~30 seconds to propagate. Reboot the app from the menu.

**Claude returns SQL that fails** — the query engine has one auto-retry
built in. If a question consistently fails, expand the "SQL" panel in
the UI to see what Claude wrote — usually it's a column-name typo or
a missing join. Drop me the question and the SQL and we'll fix the
schema doc in `query_engine.py`.

**FMP rate limit errors** — Premium gives you 750 req/min. If you hit
it, `refresh_data.py` has retries built in. If it's persistent, lower
the `time.sleep(0.12)` line to `time.sleep(0.5)` and re-run.

**Indicator values look wrong** — first 200 trading days of any ticker
have NULL for `ma_200` because that needs 200 days of warmup. The
refresh script keeps 200 extra days during the load specifically to
avoid this. If you still see NULLs in recent rows, the ticker may not
have enough history (recent IPO, etc.).

## What's next (Phase 2)

Once Phase 1 is humming for a couple of weeks and you've validated:
- Text-to-SQL handles the questions you actually ask
- The component library covers your needs
- Indicators are correct (spot-check against TradingView)

Then we build Phase 2:
- React frontend + FastAPI backend → Vercel + Railway (Robinhood-level UI)
- Top 10 Bullish daily page (confluence scoring)
- Market Pulse dashboard
- Macro calendar with historical context
- Eventually: EU tickers, longer history (20+ years)
