"""
refresh_data.py — Pulls prices, fundamentals, earnings from FMP and macro from
FRED + yfinance into Supabase. Calculates all technical indicators in pandas
and stores them as columns in daily_bars.

Two modes:
    python refresh_data.py --full          # initial load (5+ years per ticker)
    python refresh_data.py --incremental   # nightly: last ~10 days only

GitHub Actions runs --incremental nightly. You run --full manually once after
setup, or after expanding the ticker universe.

Environment:
    FMP_API_KEY     — FMP dashboard
    DATABASE_URL    — Supabase Transaction Pooler URL
    FRED_API_KEY    — optional, https://fred.stlouisfed.org/docs/api/api_key.html
                      (without this, macro_indicators columns from FRED are skipped;
                       VIX still works via yfinance)
"""
import os
import sys
import time
import argparse
import requests
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta, date
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

FMP_KEY = os.getenv("FMP_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
FRED_KEY = os.getenv("FRED_API_KEY")  # optional

if not FMP_KEY:
    sys.exit("ERROR: FMP_API_KEY not set")
if not DATABASE_URL:
    sys.exit("ERROR: DATABASE_URL not set")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
FMP_BASE = "https://financialmodelingprep.com/api/v3"


# =============================================================================
# FMP helpers
# =============================================================================
def fmp_get(path, params=None):
    params = params or {}
    params["apikey"] = FMP_KEY
    url = f"{FMP_BASE}/{path}"
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = 2 ** attempt
                time.sleep(wait)
                continue
            if r.status_code in (401, 403):
                raise RuntimeError(f"FMP auth/permission error on {path}: {r.text[:200]}")
            r.raise_for_status()
        except requests.exceptions.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"FMP failed: {url}")


def fetch_prices(ticker, start_date):
    """Returns DataFrame with columns: date, open, high, low, close, volume.
    FMP returns split-and-dividend-adjusted close as `adjClose`; we use that as `close`
    so all queries are adjustment-consistent."""
    data = fmp_get(f"historical-price-full/{ticker}",
                   params={"from": start_date.strftime("%Y-%m-%d")})
    if not data or "historical" not in data or not data["historical"]:
        return pd.DataFrame()
    df = pd.DataFrame(data["historical"])
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.rename(columns={"adjClose": "adj_close"})
    # Use adjusted close as the primary close so MAs and returns are adjustment-aware.
    out = pd.DataFrame({
        "ticker": ticker,
        "date":   df["date"],
        "open":   df.get("open"),
        "high":   df.get("high"),
        "low":    df.get("low"),
        "close":  df.get("adj_close", df.get("close")),
        "volume": df.get("volume"),
    })
    return out.sort_values("date").reset_index(drop=True)


def fetch_earnings(ticker):
    """Historical earnings with estimates and actuals."""
    try:
        data = fmp_get(f"historical/earning_calendar/{ticker}")
    except Exception:
        return pd.DataFrame()
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df = df.dropna(subset=["date"])
    df["ticker"] = ticker
    keep = {
        "ticker": "ticker",
        "date": "date",
        "epsEstimated": "eps_estimate",
        "eps": "eps_actual",
        "revenueEstimated": "revenue_estimate",
        "revenue": "revenue_actual",
        "fiscalDateEnding": "_fiscal_period",
        "time": "reporting_time",
    }
    cols = {src: dst for src, dst in keep.items() if src in df.columns}
    df = df.rename(columns=cols)[list(cols.values())]
    if "eps_actual" in df and "eps_estimate" in df:
        df["eps_surprise_pct"] = (
            (df["eps_actual"] - df["eps_estimate"]) / df["eps_estimate"].abs() * 100
        )
    if "revenue_actual" in df and "revenue_estimate" in df:
        df["revenue_surprise_pct"] = (
            (df["revenue_actual"] - df["revenue_estimate"]) / df["revenue_estimate"].abs() * 100
        )
    # We don't reliably get fiscal period as Q1/Q2; leave null for now.
    df["fiscal_period"] = None
    df = df.drop(columns=[c for c in ["_fiscal_period"] if c in df.columns])
    return df


def fetch_fundamentals_snapshot(ticker):
    """Latest fundamentals snapshot via /key-metrics-ttm and /ratios-ttm."""
    try:
        km = fmp_get(f"key-metrics-ttm/{ticker}")
        ratios = fmp_get(f"ratios-ttm/{ticker}")
        quote = fmp_get(f"quote/{ticker}")
    except Exception:
        return None
    km = km[0] if isinstance(km, list) and km else {}
    ratios = ratios[0] if isinstance(ratios, list) and ratios else {}
    quote = quote[0] if isinstance(quote, list) and quote else {}

    if not (km or ratios or quote):
        return None

    return {
        "ticker": ticker,
        "date": date.today(),
        "market_cap":           quote.get("marketCap"),
        "enterprise_value":     km.get("enterpriseValueTTM"),
        "pe_trailing":          quote.get("pe") or km.get("peRatioTTM"),
        "pe_forward":           km.get("forwardPERatio"),       # may be null on Starter
        "price_to_sales":       km.get("priceToSalesRatioTTM"),
        "price_to_book":        km.get("pbRatioTTM"),
        "ev_ebitda":            km.get("enterpriseValueOverEBITDATTM"),
        "peg_ratio":            ratios.get("pegRatioTTM"),
        "revenue_growth_yoy":   None,   # would need /financial-growth, skip in snapshot
        "earnings_growth_yoy":  None,
        "profit_margin":        ratios.get("netProfitMarginTTM"),
        "operating_margin":     ratios.get("operatingProfitMarginTTM"),
        "return_on_equity":     ratios.get("returnOnEquityTTM"),
        "debt_to_equity":       ratios.get("debtEquityRatioTTM"),
        "current_ratio":        ratios.get("currentRatioTTM"),
        "dividend_yield":       ratios.get("dividendYieldTTM"),
        "payout_ratio":         ratios.get("payoutRatioTTM"),
        "shares_outstanding":   quote.get("sharesOutstanding"),
        "float_shares":         None,
        "short_interest_pct":   None,   # Premium-gated field
        "short_ratio_days":     None,
    }


# =============================================================================
# Indicator calculations
# =============================================================================
def calc_indicators(df):
    """Takes a DataFrame for ONE ticker sorted by date ascending. Adds all the
    indicator columns. Returns the augmented DataFrame."""
    if df.empty or len(df) < 2:
        return df
    df = df.copy().sort_values("date").reset_index(drop=True)
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    # Returns
    prev = c.shift(1)
    df["daily_return"]   = (c / prev - 1) * 100
    df["weekly_return"]  = (c / c.shift(5) - 1) * 100
    df["monthly_return"] = (c / c.shift(21) - 1) * 100

    # Moving averages
    df["ma_20"]  = c.rolling(20).mean()
    df["ma_50"]  = c.rolling(50).mean()
    df["ma_100"] = c.rolling(100).mean()
    df["ma_200"] = c.rolling(200).mean()

    df["pct_vs_ma20"]  = (c / df["ma_20"] - 1) * 100
    df["pct_vs_ma50"]  = (c / df["ma_50"] - 1) * 100
    df["pct_vs_ma100"] = (c / df["ma_100"] - 1) * 100
    df["pct_vs_ma200"] = (c / df["ma_200"] - 1) * 100

    # 52-week range (252 trading days)
    df["high_52w"] = c.rolling(252, min_periods=20).max()
    df["low_52w"]  = c.rolling(252, min_periods=20).min()
    df["pct_vs_52w_high"] = (c / df["high_52w"] - 1) * 100
    df["pct_vs_52w_low"]  = (c / df["low_52w"] - 1) * 100

    # Volume context
    df["vol_20d_avg"] = v.rolling(20).mean()
    df["rel_volume"]  = v / df["vol_20d_avg"]

    # RSI (Wilder)
    df["rsi_14"] = _rsi(c, 14)
    df["rsi_5"]  = _rsi(c, 5)

    # MACD (12, 26, 9)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["macd_line"]      = ema12 - ema26
    df["macd_signal"]    = df["macd_line"].ewm(span=9, adjust=False).mean()
    df["macd_histogram"] = df["macd_line"] - df["macd_signal"]

    # Bollinger Bands (20, 2)
    bb_ma = c.rolling(20).mean()
    bb_sd = c.rolling(20).std()
    df["bb_upper"] = bb_ma + 2 * bb_sd
    df["bb_lower"] = bb_ma - 2 * bb_sd
    width = (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)
    df["bb_pct"]   = (c - df["bb_lower"]) / width

    # ATR (14)
    tr1 = h - l
    tr2 = (h - prev).abs()
    tr3 = (l - prev).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(14).mean()

    # Gap
    df["gap_pct"] = (df["open"] / prev - 1) * 100

    # Replace inf with NaN so Postgres doesn't choke
    df = df.replace([np.inf, -np.inf], np.nan)
    return df


def _rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# =============================================================================
# DB writes
# =============================================================================
BAR_COLS = [
    "ticker", "date", "open", "high", "low", "close", "volume",
    "daily_return", "weekly_return", "monthly_return",
    "ma_20", "ma_50", "ma_100", "ma_200",
    "pct_vs_ma20", "pct_vs_ma50", "pct_vs_ma100", "pct_vs_ma200",
    "high_52w", "low_52w", "pct_vs_52w_high", "pct_vs_52w_low",
    "vol_20d_avg", "rel_volume",
    "rsi_14", "rsi_5",
    "macd_line", "macd_signal", "macd_histogram",
    "bb_upper", "bb_lower", "bb_pct",
    "atr_14", "gap_pct",
]


def upsert_bars(df, ticker, full_replace=False):
    if df.empty:
        return 0
    df = df[BAR_COLS].copy()
    # Convert NaN to None for psycopg
    df = df.where(pd.notnull(df), None)

    with engine.begin() as con:
        if full_replace:
            con.execute(text("DELETE FROM daily_bars WHERE ticker = :t"), {"t": ticker})
            df.to_sql("daily_bars", con, if_exists="append",
                      index=False, method="multi", chunksize=500)
        else:
            # Incremental: delete just the date range we're inserting, then append.
            min_d, max_d = df["date"].min(), df["date"].max()
            con.execute(
                text("DELETE FROM daily_bars WHERE ticker = :t AND date BETWEEN :a AND :b"),
                {"t": ticker, "a": min_d, "b": max_d},
            )
            df.to_sql("daily_bars", con, if_exists="append",
                      index=False, method="multi", chunksize=500)
    return len(df)


def upsert_earnings(df, ticker):
    if df.empty:
        return 0
    with engine.begin() as con:
        con.execute(text("DELETE FROM earnings_dates WHERE ticker = :t"), {"t": ticker})
        df = df.where(pd.notnull(df), None)
        df.to_sql("earnings_dates", con, if_exists="append",
                  index=False, method="multi", chunksize=500)
    return len(df)


def upsert_fundamentals(row):
    if not row:
        return 0
    with engine.begin() as con:
        con.execute(text("""
            INSERT INTO fundamentals (
                ticker, date,
                market_cap, enterprise_value,
                pe_trailing, pe_forward, price_to_sales, price_to_book, ev_ebitda, peg_ratio,
                revenue_growth_yoy, earnings_growth_yoy,
                profit_margin, operating_margin, return_on_equity,
                debt_to_equity, current_ratio,
                dividend_yield, payout_ratio,
                shares_outstanding, float_shares, short_interest_pct, short_ratio_days
            ) VALUES (
                :ticker, :date,
                :market_cap, :enterprise_value,
                :pe_trailing, :pe_forward, :price_to_sales, :price_to_book, :ev_ebitda, :peg_ratio,
                :revenue_growth_yoy, :earnings_growth_yoy,
                :profit_margin, :operating_margin, :return_on_equity,
                :debt_to_equity, :current_ratio,
                :dividend_yield, :payout_ratio,
                :shares_outstanding, :float_shares, :short_interest_pct, :short_ratio_days
            )
            ON CONFLICT (ticker, date) DO UPDATE SET
                market_cap = EXCLUDED.market_cap,
                pe_trailing = EXCLUDED.pe_trailing,
                pe_forward = EXCLUDED.pe_forward,
                price_to_sales = EXCLUDED.price_to_sales,
                price_to_book = EXCLUDED.price_to_book,
                ev_ebitda = EXCLUDED.ev_ebitda,
                peg_ratio = EXCLUDED.peg_ratio,
                profit_margin = EXCLUDED.profit_margin,
                operating_margin = EXCLUDED.operating_margin,
                return_on_equity = EXCLUDED.return_on_equity,
                debt_to_equity = EXCLUDED.debt_to_equity,
                current_ratio = EXCLUDED.current_ratio,
                dividend_yield = EXCLUDED.dividend_yield,
                payout_ratio = EXCLUDED.payout_ratio,
                shares_outstanding = EXCLUDED.shares_outstanding
        """), row)
    return 1


# =============================================================================
# Macro / market indicators
# =============================================================================
def refresh_vix_and_market(start_date):
    """Pull VIX from yfinance and write to market_indicators."""
    print("  fetching VIX from yfinance...")
    try:
        vix = yf.download("^VIX", start=start_date.strftime("%Y-%m-%d"),
                          progress=False, auto_adjust=True)
        if vix.empty:
            print("    no VIX data")
            return
        if isinstance(vix.columns, pd.MultiIndex):
            vix.columns = vix.columns.get_level_values(0)
        vix = vix.reset_index()
        vix["date"] = pd.to_datetime(vix["Date"]).dt.date
        vix["vix_close"] = vix["Close"]
        vix["vix_ma20"] = vix["vix_close"].rolling(20).mean()
        vix["vix_ma50"] = vix["vix_close"].rolling(50).mean()
        out = vix[["date", "vix_close", "vix_ma20", "vix_ma50"]].copy()
        out = out.where(pd.notnull(out), None)

        with engine.begin() as con:
            for _, row in out.iterrows():
                con.execute(text("""
                    INSERT INTO market_indicators (date, vix_close, vix_ma20, vix_ma50)
                    VALUES (:date, :vix_close, :vix_ma20, :vix_ma50)
                    ON CONFLICT (date) DO UPDATE SET
                        vix_close = EXCLUDED.vix_close,
                        vix_ma20  = EXCLUDED.vix_ma20,
                        vix_ma50  = EXCLUDED.vix_ma50
                """), row.to_dict())
        print(f"    {len(out)} VIX rows")
    except Exception as e:
        print(f"    VIX fetch failed: {e}")


def refresh_fred(start_date):
    """Pull Fed Funds, 10Y, 2Y, CPI, unemployment from FRED.
    Requires FRED_API_KEY env var. Skips gracefully if not set."""
    if not FRED_KEY:
        print("  FRED_API_KEY not set, skipping macro series")
        return
    print("  fetching FRED macro series...")
    series_map = {
        "DFF":      "fed_funds_rate",        # Daily Federal Funds Effective Rate
        "DGS10":    "ten_year_yield",
        "DGS2":     "two_year_yield",
        "CPIAUCSL": "cpi_yoy",               # CPI level — we'll convert to YoY
        "UNRATE":   "unemployment_rate",
    }
    frames = {}
    for code, col in series_map.items():
        try:
            url = "https://api.stlouisfed.org/fred/series/observations"
            r = requests.get(url, params={
                "series_id": code, "api_key": FRED_KEY, "file_type": "json",
                "observation_start": start_date.strftime("%Y-%m-%d"),
            }, timeout=30)
            r.raise_for_status()
            data = r.json().get("observations", [])
            df = pd.DataFrame(data)
            if df.empty:
                continue
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df["val"]  = pd.to_numeric(df["value"], errors="coerce")
            df = df[["date", "val"]].rename(columns={"val": col})
            if code == "CPIAUCSL":
                df = df.set_index("date").asfreq("MS").ffill()
                df[col] = (df[col] / df[col].shift(12) - 1) * 100
                df = df.reset_index()
            frames[col] = df
            time.sleep(0.2)
        except Exception as e:
            print(f"    {code} failed: {e}")

    if not frames:
        return

    # Merge all on date, forward-fill onto daily index
    base = pd.DataFrame({"date": pd.date_range(start_date, datetime.today()).date})
    merged = base.copy()
    for col, df in frames.items():
        merged = merged.merge(df, on="date", how="left")
    for col in series_map.values():
        if col in merged.columns:
            merged[col] = merged[col].ffill()

    merged["yield_curve_10y_2y"] = (
        merged.get("ten_year_yield") - merged.get("two_year_yield")
        if "ten_year_yield" in merged.columns and "two_year_yield" in merged.columns
        else None
    )
    merged = merged.where(pd.notnull(merged), None)

    with engine.begin() as con:
        for _, row in merged.iterrows():
            con.execute(text("""
                INSERT INTO market_indicators (
                    date, fed_funds_rate, ten_year_yield, two_year_yield,
                    yield_curve_10y_2y, cpi_yoy, unemployment_rate
                ) VALUES (
                    :date, :fed_funds_rate, :ten_year_yield, :two_year_yield,
                    :yield_curve_10y_2y, :cpi_yoy, :unemployment_rate
                )
                ON CONFLICT (date) DO UPDATE SET
                    fed_funds_rate     = COALESCE(EXCLUDED.fed_funds_rate, market_indicators.fed_funds_rate),
                    ten_year_yield     = COALESCE(EXCLUDED.ten_year_yield, market_indicators.ten_year_yield),
                    two_year_yield     = COALESCE(EXCLUDED.two_year_yield, market_indicators.two_year_yield),
                    yield_curve_10y_2y = COALESCE(EXCLUDED.yield_curve_10y_2y, market_indicators.yield_curve_10y_2y),
                    cpi_yoy            = COALESCE(EXCLUDED.cpi_yoy, market_indicators.cpi_yoy),
                    unemployment_rate  = COALESCE(EXCLUDED.unemployment_rate, market_indicators.unemployment_rate)
            """), {k: row.get(k) for k in [
                "date", "fed_funds_rate", "ten_year_yield", "two_year_yield",
                "yield_curve_10y_2y", "cpi_yoy", "unemployment_rate",
            ]})
    print(f"    {len(merged)} macro rows")


def refresh_breadth():
    """Calculate % of S&P 500 above 200MA / 50MA, advancing %, etc.
    Done from daily_bars + tickers joined together."""
    print("  calculating market breadth from daily_bars...")
    with engine.begin() as con:
        con.execute(text("""
            INSERT INTO market_indicators (
                date, pct_above_ma200, pct_above_ma50, advance_decline_pct
            )
            SELECT
                d.date,
                100.0 * AVG(CASE WHEN d.close > d.ma_200 THEN 1.0 ELSE 0.0 END)
                    FILTER (WHERE d.ma_200 IS NOT NULL) AS pct_above_ma200,
                100.0 * AVG(CASE WHEN d.close > d.ma_50 THEN 1.0 ELSE 0.0 END)
                    FILTER (WHERE d.ma_50 IS NOT NULL) AS pct_above_ma50,
                100.0 * AVG(CASE WHEN d.daily_return > 0 THEN 1.0 ELSE 0.0 END)
                    FILTER (WHERE d.daily_return IS NOT NULL) AS adv_dec
            FROM daily_bars d
            JOIN tickers t ON t.ticker = d.ticker
            WHERE t.in_sp500 = TRUE
              AND d.date >= (CURRENT_DATE - INTERVAL '400 days')
            GROUP BY d.date
            ON CONFLICT (date) DO UPDATE SET
                pct_above_ma200     = EXCLUDED.pct_above_ma200,
                pct_above_ma50      = EXCLUDED.pct_above_ma50,
                advance_decline_pct = EXCLUDED.advance_decline_pct
        """))
    print("    breadth updated")


# =============================================================================
# Main
# =============================================================================
def get_watchlist():
    """All active tickers from the tickers table."""
    with engine.connect() as con:
        rows = con.execute(text(
            "SELECT ticker FROM tickers WHERE is_active = TRUE ORDER BY ticker"
        )).fetchall()
    return [r[0] for r in rows]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true",
                        help="Initial load: pull 5+ years per ticker, replace all rows")
    parser.add_argument("--incremental", action="store_true",
                        help="Nightly: pull last ~10 trading days, merge")
    parser.add_argument("--years", type=int, default=5,
                        help="Years of history for --full (default 5; 20 on FMP Premium)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process first N tickers (for testing)")
    args = parser.parse_args()

    if not args.full and not args.incremental:
        # Default to incremental for cron runs
        args.incremental = True

    watchlist = get_watchlist()
    if args.limit:
        watchlist = watchlist[:args.limit]

    if not watchlist:
        sys.exit("No tickers in DB. Run load_tickers.py first.")

    mode = "FULL" if args.full else "INCREMENTAL"
    print(f"\n=== Refresh: {mode} | {len(watchlist)} tickers ===\n")

    if args.full:
        start = date.today() - timedelta(days=365 * args.years + 200)  # +200 for MA warmup
    else:
        start = date.today() - timedelta(days=20)   # ~10 trading days + buffer
        # For incremental, we still need historical context to recompute indicators.
        # We pull from DB any existing bars in the last 250 days, append the new ones,
        # recompute indicators, then upsert only the new date range.
        pass

    n_ok, n_fail = 0, 0
    n_bars, n_earn, n_fund = 0, 0, 0

    for i, ticker in enumerate(watchlist, 1):
        try:
            if args.full:
                fresh = fetch_prices(ticker, start)
                if fresh.empty:
                    print(f"[{i}/{len(watchlist)}] {ticker}: no price data")
                    n_fail += 1
                    continue
                augmented = calc_indicators(fresh)
                # Trim warmup period: keep only dates within `years`
                cutoff = date.today() - timedelta(days=365 * args.years)
                augmented = augmented[augmented["date"] >= cutoff]
                n_bars += upsert_bars(augmented, ticker, full_replace=True)
            else:
                # Incremental: fetch last ~250 days fresh from FMP, recompute
                # indicators using that as the only context, upsert just the
                # most recent few rows.
                recent = fetch_prices(ticker, date.today() - timedelta(days=400))
                if recent.empty:
                    n_fail += 1
                    continue
                augmented = calc_indicators(recent)
                cutoff = date.today() - timedelta(days=15)
                augmented = augmented[augmented["date"] >= cutoff]
                n_bars += upsert_bars(augmented, ticker, full_replace=False)

            # Earnings + fundamentals are cheap to refresh fully every time
            edf = fetch_earnings(ticker)
            n_earn += upsert_earnings(edf, ticker)

            fund = fetch_fundamentals_snapshot(ticker)
            n_fund += upsert_fundamentals(fund)

            n_ok += 1
            if i % 25 == 0:
                print(f"[{i}/{len(watchlist)}] {ticker}: ok ({n_ok} done, {n_fail} failed)")

            # Gentle pacing: ~10 req/sec across all endpoints
            time.sleep(0.12)
        except Exception as e:
            print(f"[{i}/{len(watchlist)}] {ticker}: ERROR {e}")
            n_fail += 1

    print(f"\nTicker work done: {n_ok} ok, {n_fail} failed, "
          f"{n_bars} bar rows, {n_earn} earnings, {n_fund} fundamentals")

    # Market-wide refresh
    print("\nRefreshing market indicators...")
    start_market = date.today() - timedelta(days=365 * (args.years if args.full else 1) + 60)
    refresh_vix_and_market(start_market)
    refresh_fred(start_market)
    refresh_breadth()

    print("\nDone.")


if __name__ == "__main__":
    main()
