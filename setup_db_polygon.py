"""
setup_db_polygon.py — Polygon.io version of setup_db.py.

Pulls daily OHLCV bars for a watchlist of tickers into DuckDB using Polygon.
Polygon's Starter tier ($29/mo) gives unlimited API calls.

Usage:
    1. Sign up at polygon.io, copy your API key
    2. Add POLYGON_API_KEY to Replit Secrets (or your .env)
    3. python setup_db_polygon.py
"""
import os
import duckdb
import pandas as pd
from datetime import datetime, timedelta
from polygon import RESTClient
from dotenv import load_dotenv

load_dotenv()

DB_PATH = "equity.duckdb"
POLYGON_KEY = os.getenv("POLYGON_API_KEY")
if not POLYGON_KEY:
    raise RuntimeError("POLYGON_API_KEY not set. Add it to Replit Secrets or your .env file.")

client = RESTClient(POLYGON_KEY)

# Edit this list freely — Polygon has no per-call limit, so you can put the
# entire S&P 500 here if you want. ~500 tickers takes about 10 minutes.
WATCHLIST = [
    "AAPL", "MSFT", "GOOG", "AMZN", "NVDA", "META",
    "TSLA", "QCOM", "CRM", "NEE", "CEG",
    "QQQ", "SPY", "IWM",
]

YEARS_OF_HISTORY = 5  # Starter tier gives 5 years; Developer gives 10


def init_schema(con):
    """Create tables if they don't exist. Same schema as the yfinance version
    so scenarios.py and app.py don't need to change."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS daily_bars (
            ticker VARCHAR,
            date DATE,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            adj_close DOUBLE,
            volume BIGINT,
            PRIMARY KEY (ticker, date)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS earnings_dates (
            ticker VARCHAR,
            date DATE,
            eps_estimate DOUBLE,
            eps_actual DOUBLE,
            surprise_pct DOUBLE,
            PRIMARY KEY (ticker, date)
        )
    """)


def fetch_bars(ticker, start_date, end_date):
    """Pull daily bars from Polygon. Returns a DataFrame in our schema."""
    aggs = client.list_aggs(
        ticker=ticker,
        multiplier=1,
        timespan="day",
        from_=start_date,
        to=end_date,
        adjusted=True,  # adjusted for splits
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
            "adj_close": a.close,  # Polygon adjusted=True already adjusts close
            "volume": int(a.volume) if a.volume else 0,
        })
    return pd.DataFrame(rows)


def fetch_earnings(ticker):
    """
    Polygon's earnings data is on the Stocks Advanced tier ($199/mo).
    On Starter/Developer, you can derive earnings dates from the 'financials'
    vx endpoint, but it's less clean. Easiest path: keep yfinance for earnings
    dates only (it's the one thing it does fine), or upgrade to Advanced.

    For now this function returns empty — post_earnings_drop_recovery scenario
    won't work until you either:
      (a) upgrade Polygon to Advanced, or
      (b) layer in yfinance for the earnings_dates table only, or
      (c) buy earnings data separately from a provider like Zacks or Estimize.

    See the comment block at the bottom of the file for option (b) snippet.
    """
    return pd.DataFrame()


def main():
    print(f"Initializing database at {DB_PATH}")
    con = duckdb.connect(DB_PATH)
    init_schema(con)

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=365 * YEARS_OF_HISTORY)).strftime("%Y-%m-%d")
    print(f"Fetching {start_date} to {end_date} for {len(WATCHLIST)} tickers via Polygon\n")

    for ticker in WATCHLIST:
        print(f"{ticker}: ", end="", flush=True)
        try:
            df = fetch_bars(ticker, start_date, end_date)
            if df.empty:
                print("no data")
                continue
            con.execute("DELETE FROM daily_bars WHERE ticker = ?", [ticker])
            con.register("df_temp", df)
            con.execute("INSERT INTO daily_bars SELECT * FROM df_temp")
            con.unregister("df_temp")
            print(f"{len(df)} daily bars")
        except Exception as e:
            print(f"error: {e}")

    # Earnings — see fetch_earnings() docstring
    print("\nSkipping earnings (see fetch_earnings docstring for options).")

    total = con.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
    print(f"\nDone. {total} total daily bars stored.")
    con.close()


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# OPTIONAL: hybrid earnings approach.
# Polygon Starter doesn't include earnings dates. Easiest fix is to use
# yfinance for earnings only — it's free and the dates are usually correct.
# Uncomment and call this from main() if you want earnings data:
#
# import yfinance as yf
# def fetch_earnings_via_yfinance(con, ticker):
#     try:
#         tk = yf.Ticker(ticker)
#         edf = tk.get_earnings_dates(limit=40)
#         if edf is None or edf.empty:
#             return
#         edf = edf.reset_index()
#         edf["ticker"] = ticker
#         edf = edf.rename(columns={
#             "Earnings Date": "date",
#             "EPS Estimate": "eps_estimate",
#             "Reported EPS": "eps_actual",
#             "Surprise(%)": "surprise_pct",
#         })
#         edf["date"] = pd.to_datetime(edf["date"]).dt.date
#         edf = edf[["ticker", "date", "eps_estimate", "eps_actual", "surprise_pct"]]
#         edf = edf.drop_duplicates(subset=["ticker", "date"])
#         con.execute("DELETE FROM earnings_dates WHERE ticker = ?", [ticker])
#         con.register("e_temp", edf)
#         con.execute("INSERT INTO earnings_dates SELECT * FROM e_temp")
#         con.unregister("e_temp")
#     except Exception as e:
#         print(f"  earnings fetch failed for {ticker}: {e}")
# ---------------------------------------------------------------------------
