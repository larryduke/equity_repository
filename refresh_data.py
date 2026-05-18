"""
refresh_data.py — Pulls daily OHLCV from Polygon and earnings dates from
yfinance into Supabase (cloud Postgres). Designed to be run by a nightly
GitHub Actions cron job, but you can also run it manually.

Environment variables required:
    POLYGON_API_KEY    — from polygon.io dashboard
    DATABASE_URL       — from Supabase Project Settings → Database → Connection string
                         (use the "Transaction pooler" URL, port 6543)

Usage (locally for testing):
    python refresh_data.py
"""
import os
import sys
import time
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from polygon import RESTClient
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

POLYGON_KEY = os.getenv("POLYGON_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

if not POLYGON_KEY:
    sys.exit("ERROR: POLYGON_API_KEY not set")
if not DATABASE_URL:
    sys.exit("ERROR: DATABASE_URL not set")

# SQLAlchemy needs postgresql:// not postgres://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

client = RESTClient(POLYGON_KEY)
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# Edit this list freely. Polygon Starter has no per-call limit.
WATCHLIST = [
    "AAPL", "MSFT", "GOOG", "AMZN", "NVDA", "META",
    "TSLA", "QCOM", "CRM", "NEE", "CEG",
    "QQQ", "SPY", "IWM",
]

YEARS_OF_HISTORY = 5  # Polygon Starter gives 5 years


def init_schema():
    """Create tables if they don't exist."""
    with engine.begin() as con:
        con.execute(text("""
            CREATE TABLE IF NOT EXISTS daily_bars (
                ticker VARCHAR(10) NOT NULL,
                date DATE NOT NULL,
                open DOUBLE PRECISION,
                high DOUBLE PRECISION,
                low DOUBLE PRECISION,
                close DOUBLE PRECISION,
                adj_close DOUBLE PRECISION,
                volume BIGINT,
                PRIMARY KEY (ticker, date)
            )
        """))
        con.execute(text("""
            CREATE TABLE IF NOT EXISTS earnings_dates (
                ticker VARCHAR(10) NOT NULL,
                date DATE NOT NULL,
                eps_estimate DOUBLE PRECISION,
                eps_actual DOUBLE PRECISION,
                surprise_pct DOUBLE PRECISION,
                PRIMARY KEY (ticker, date)
            )
        """))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_bars_ticker_date ON daily_bars(ticker, date)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_earn_ticker_date ON earnings_dates(ticker, date)"))


def fetch_bars_from_polygon(ticker, start_date, end_date):
    aggs = client.list_aggs(
        ticker=ticker,
        multiplier=1,
        timespan="day",
        from_=start_date,
        to=end_date,
        adjusted=True,
        limit=50000,
    )
    rows = []
    for a in aggs:
        rows.append({
            "ticker": ticker,
            "date": datetime.fromtimestamp(a.timestamp / 1000).date(),
            "open": a.open,
            "high": a.high,
            "low": a.low,
            "close": a.close,
            "adj_close": a.close,  # adjusted=True means close is already adjusted
            "volume": int(a.volume) if a.volume else 0,
        })
    return pd.DataFrame(rows)


def upsert_bars(df, ticker):
    """Replace all rows for this ticker. Simple and safe for daily refresh."""
    with engine.begin() as con:
        con.execute(text("DELETE FROM daily_bars WHERE ticker = :t"), {"t": ticker})
        df.to_sql("daily_bars", con, if_exists="append", index=False, method="multi", chunksize=500)


def fetch_earnings_from_yfinance(ticker):
    """yfinance is free and earnings dates are usually correct."""
    try:
        tk = yf.Ticker(ticker)
        edf = tk.get_earnings_dates(limit=40)
        if edf is None or edf.empty:
            return None
        edf = edf.reset_index()
        edf["ticker"] = ticker
        edf = edf.rename(columns={
            "Earnings Date": "date",
            "EPS Estimate": "eps_estimate",
            "Reported EPS": "eps_actual",
            "Surprise(%)": "surprise_pct",
        })
        edf["date"] = pd.to_datetime(edf["date"]).dt.date
        edf = edf[["ticker", "date", "eps_estimate", "eps_actual", "surprise_pct"]]
        edf = edf.drop_duplicates(subset=["ticker", "date"])
        return edf
    except Exception as e:
        print(f"  earnings fetch failed for {ticker}: {e}")
        return None


def upsert_earnings(df, ticker):
    if df is None or df.empty:
        return 0
    with engine.begin() as con:
        con.execute(text("DELETE FROM earnings_dates WHERE ticker = :t"), {"t": ticker})
        df.to_sql("earnings_dates", con, if_exists="append", index=False, method="multi", chunksize=500)
    return len(df)


def main():
    print(f"Connecting to database...")
    init_schema()

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=365 * YEARS_OF_HISTORY)).strftime("%Y-%m-%d")
    print(f"Refreshing {start_date} to {end_date} for {len(WATCHLIST)} tickers\n")

    for ticker in WATCHLIST:
        print(f"{ticker}: ", end="", flush=True)
        try:
            bars_df = fetch_bars_from_polygon(ticker, start_date, end_date)
            if not bars_df.empty:
                upsert_bars(bars_df, ticker)
                print(f"{len(bars_df)} bars", end="")
            else:
                print("no price data", end="")

            earn_df = fetch_earnings_from_yfinance(ticker)
            n = upsert_earnings(earn_df, ticker)
            print(f", {n} earnings dates")

            time.sleep(0.5)  # be gentle to yfinance
        except Exception as e:
            print(f"ERROR: {e}")

    with engine.connect() as con:
        bars_total = con.execute(text("SELECT COUNT(*) FROM daily_bars")).scalar()
        earn_total = con.execute(text("SELECT COUNT(*) FROM earnings_dates")).scalar()
    print(f"\nDone. {bars_total} daily bars and {earn_total} earnings dates in database.")


if __name__ == "__main__":
    main()
