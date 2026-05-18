"""
refresh_data.py — Pulls prices, fundamentals, earnings from FMP stable API
and macro from FRED + yfinance into Supabase. Calculates all technical
indicators in pandas and stores pre-computed in daily_bars.

Two modes:
    python refresh_data.py --full          # initial load (5+ years per ticker)
    python refresh_data.py --incremental   # nightly: last ~10 days only
    python refresh_data.py --full --limit 50  # test run on first 50 tickers

Environment:
    FMP_API_KEY     — FMP dashboard
    DATABASE_URL    — Supabase Transaction Pooler URL
    FRED_API_KEY    — optional, https://fred.stlouisfed.org/docs/api/api_key.html
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
FRED_KEY = os.getenv("FRED_API_KEY")

if not FMP_KEY:
    sys.exit("ERROR: FMP_API_KEY not set")
if not DATABASE_URL:
    sys.exit("ERROR: DATABASE_URL not set")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
STABLE = "https://financialmodelingprep.com/stable"


# =============================================================================
# FMP stable API helper
# =============================================================================
def fmp_get(path, params=None):
    """GET from FMP stable API. All params as query string (no path params)."""
    params = params or {}
    params["apikey"] = FMP_KEY
    url = f"{STABLE}/{path}"
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "Error Message" in data:
                    msg = data["Error Message"]
                    if "Legacy" in msg or "legacy" in msg:
                        raise RuntimeError(f"Legacy endpoint: {path}")
                    raise RuntimeError(f"FMP error {path}: {msg[:150]}")
                return data
            if r.status_code == 429:
                wait = 2 ** attempt
                time.sleep(wait)
                continue
            if r.status_code in (401, 403):
                raise RuntimeError(f"FMP auth {r.status_code}: {path}")
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"FMP failed after retries: {path}")


# =============================================================================
# FMP data fetchers
# =============================================================================
def fetch_prices(ticker, start_date):
    """
    Returns DataFrame: date, open, high, low, close (adj), volume.
    Uses stable/historical-price-eod/full for adjusted prices.
    """
    try:
        data = fmp_get("historical-price-eod/full", params={
            "symbol": ticker,
            "from": start_date.strftime("%Y-%m-%d"),
        })
    except RuntimeError:
        # Fallback to non-adjusted if full not available on tier
        try:
            data = fmp_get("historical-price-eod", params={
                "symbol": ticker,
                "from": start_date.strftime("%Y-%m-%d"),
            })
        except Exception:
            return pd.DataFrame()

    if not data:
        return pd.DataFrame()

    # FMP returns either {"historical": [...]} or a plain list
    if isinstance(data, dict) and "historical" in data:
        records = data["historical"]
    elif isinstance(data, list):
        records = data
    else:
        return pd.DataFrame()

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    if df.empty:
        return df

    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df = df.dropna(subset=["date"])

    # Use adjClose if available, else close
    close_col = "adjClose" if "adjClose" in df.columns else "close"

    out = pd.DataFrame({
        "ticker": ticker,
        "date":   df["date"],
        "open":   pd.to_numeric(df.get("open"), errors="coerce"),
        "high":   pd.to_numeric(df.get("high"), errors="coerce"),
        "low":    pd.to_numeric(df.get("low"), errors="coerce"),
        "close":  pd.to_numeric(df[close_col], errors="coerce"),
        "volume": pd.to_numeric(df.get("volume"), errors="coerce").fillna(0).astype("Int64"),
    })
    return out.sort_values("date").reset_index(drop=True)


def fetch_earnings(ticker):
    """Historical earnings via stable/earnings."""
    try:
        data = fmp_get("earnings", params={"symbol": ticker})
    except Exception:
        return pd.DataFrame()

    if not data or not isinstance(data, list):
        return pd.DataFrame()

    df = pd.DataFrame(data)
    if df.empty:
        return df

    df["date"] = pd.to_datetime(df.get("date"), errors="coerce").dt.date
    df = df.dropna(subset=["date"])
    df["ticker"] = ticker

    rename = {
        "epsEstimated":       "eps_estimate",
        "eps":                "eps_actual",
        "revenueEstimated":   "revenue_estimate",
        "revenue":            "revenue_actual",
        "time":               "reporting_time",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    for col in ["eps_estimate", "eps_actual", "revenue_estimate", "revenue_actual"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "eps_actual" in df.columns and "eps_estimate" in df.columns:
        with pd.option_context("mode.use_inf_as_na", True):
            df["eps_surprise_pct"] = (
                (df["eps_actual"] - df["eps_estimate"])
                / df["eps_estimate"].abs() * 100
            ).replace([np.inf, -np.inf], np.nan)

    if "revenue_actual" in df.columns and "revenue_estimate" in df.columns:
        with pd.option_context("mode.use_inf_as_na", True):
            df["revenue_surprise_pct"] = (
                (df["revenue_actual"] - df["revenue_estimate"])
                / df["revenue_estimate"].abs() * 100
            ).replace([np.inf, -np.inf], np.nan)

    df["fiscal_period"] = None

    keep = ["ticker", "date", "eps_estimate", "eps_actual", "eps_surprise_pct",
            "revenue_estimate", "revenue_actual", "revenue_surprise_pct",
            "fiscal_period", "reporting_time"]
    return df[[c for c in keep if c in df.columns]]


def fetch_fundamentals_snapshot(ticker):
    """Latest fundamentals via stable/key-metrics-ttm, ratios-ttm, quote."""
    try:
        km     = fmp_get("key-metrics-ttm",  params={"symbol": ticker})
        ratios = fmp_get("ratios-ttm",        params={"symbol": ticker})
        quote  = fmp_get("quote",             params={"symbol": ticker})
    except Exception:
        return None

    km     = (km[0]     if isinstance(km, list)     and km     else km     if isinstance(km, dict)     else {})
    ratios = (ratios[0] if isinstance(ratios, list) and ratios else ratios if isinstance(ratios, dict) else {})
    quote  = (quote[0]  if isinstance(quote, list)  and quote  else quote  if isinstance(quote, dict)  else {})

    if not (km or ratios or quote):
        return None

    return {
        "ticker":            ticker,
        "date":              date.today(),
        "market_cap":        quote.get("marketCap"),
        "enterprise_value":  km.get("enterpriseValueTTM"),
        "pe_trailing":       quote.get("pe") or km.get("peRatioTTM"),
        "pe_forward":        km.get("forwardPERatio"),
        "price_to_sales":    km.get("priceToSalesRatioTTM"),
        "price_to_book":     km.get("pbRatioTTM"),
        "ev_ebitda":         km.get("enterpriseValueOverEBITDATTM"),
        "peg_ratio":         ratios.get("pegRatioTTM"),
        "revenue_growth_yoy":   None,
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
        "short_interest_pct":   None,
        "short_ratio_days":     None,
    }


# =============================================================================
# Technical indicator calculations
# =============================================================================
def _rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_indicators(df):
    if df.empty or len(df) < 2:
        return df
    df = df.copy().sort_values("date").reset_index(drop=True)
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    v = df["volume"].astype(float)
    prev = c.shift(1)

    df["daily_return"]   = (c / prev - 1) * 100
    df["weekly_return"]  = (c / c.shift(5) - 1) * 100
    df["monthly_return"] = (c / c.shift(21) - 1) * 100

    df["ma_20"]  = c.rolling(20).mean()
    df["ma_50"]  = c.rolling(50).mean()
    df["ma_100"] = c.rolling(100).mean()
    df["ma_200"] = c.rolling(200).mean()

    df["pct_vs_ma20"]  = (c / df["ma_20"]  - 1) * 100
    df["pct_vs_ma50"]  = (c / df["ma_50"]  - 1) * 100
    df["pct_vs_ma100"] = (c / df["ma_100"] - 1) * 100
    df["pct_vs_ma200"] = (c / df["ma_200"] - 1) * 100

    df["high_52w"]        = c.rolling(252, min_periods=20).max()
    df["low_52w"]         = c.rolling(252, min_periods=20).min()
    df["pct_vs_52w_high"] = (c / df["high_52w"] - 1) * 100
    df["pct_vs_52w_low"]  = (c / df["low_52w"]  - 1) * 100

    df["vol_20d_avg"] = v.rolling(20).mean()
    df["rel_volume"]  = v / df["vol_20d_avg"]

    df["rsi_14"] = _rsi(c, 14)
    df["rsi_5"]  = _rsi(c, 5)

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["macd_line"]      = ema12 - ema26
    df["macd_signal"]    = df["macd_line"].ewm(span=9, adjust=False).mean()
    df["macd_histogram"] = df["macd_line"] - df["macd_signal"]

    bb_ma = c.rolling(20).mean()
    bb_sd = c.rolling(20).std()
    df["bb_upper"] = bb_ma + 2 * bb_sd
    df["bb_lower"] = bb_ma - 2 * bb_sd
    width = (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)
    df["bb_pct"]   = (c - df["bb_lower"]) / width

    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    df["atr_14"]  = tr.rolling(14).mean()
    df["gap_pct"] = (df["open"].astype(float) / prev - 1) * 100

    return df.replace([np.inf, -np.inf], np.nan)


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
    # Keep only schema columns, convert NaN → None
    for col in BAR_COLS:
        if col not in df.columns:
            df[col] = None
    df = df[BAR_COLS].copy()
    df = df.where(pd.notnull(df), None)

    with engine.begin() as con:
        if full_replace:
            con.execute(text("DELETE FROM daily_bars WHERE ticker = :t"), {"t": ticker})
            df.to_sql("daily_bars", con, if_exists="append",
                      index=False, method="multi", chunksize=500)
        else:
            min_d, max_d = df["date"].min(), df["date"].max()
            con.execute(
                text("DELETE FROM daily_bars WHERE ticker=:t AND date BETWEEN :a AND :b"),
                {"t": ticker, "a": min_d, "b": max_d},
            )
            df.to_sql("daily_bars", con, if_exists="append",
                      index=False, method="multi", chunksize=500)
    return len(df)


def upsert_earnings(df, ticker):
    if df is None or df.empty:
        return 0
    # Deduplicate on (ticker, date) — FMP sometimes returns the same event twice
    df = df.drop_duplicates(subset=["ticker", "date"], keep="first")
    with engine.begin() as con:
        con.execute(text("DELETE FROM earnings_dates WHERE ticker=:t"), {"t": ticker})
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
                ticker, date, market_cap, enterprise_value,
                pe_trailing, pe_forward, price_to_sales, price_to_book,
                ev_ebitda, peg_ratio, revenue_growth_yoy, earnings_growth_yoy,
                profit_margin, operating_margin, return_on_equity,
                debt_to_equity, current_ratio, dividend_yield, payout_ratio,
                shares_outstanding, float_shares, short_interest_pct, short_ratio_days
            ) VALUES (
                :ticker, :date, :market_cap, :enterprise_value,
                :pe_trailing, :pe_forward, :price_to_sales, :price_to_book,
                :ev_ebitda, :peg_ratio, :revenue_growth_yoy, :earnings_growth_yoy,
                :profit_margin, :operating_margin, :return_on_equity,
                :debt_to_equity, :current_ratio, :dividend_yield, :payout_ratio,
                :shares_outstanding, :float_shares, :short_interest_pct, :short_ratio_days
            )
            ON CONFLICT (ticker, date) DO UPDATE SET
                market_cap=EXCLUDED.market_cap, pe_trailing=EXCLUDED.pe_trailing,
                pe_forward=EXCLUDED.pe_forward, price_to_sales=EXCLUDED.price_to_sales,
                price_to_book=EXCLUDED.price_to_book, ev_ebitda=EXCLUDED.ev_ebitda,
                profit_margin=EXCLUDED.profit_margin, operating_margin=EXCLUDED.operating_margin,
                return_on_equity=EXCLUDED.return_on_equity, debt_to_equity=EXCLUDED.debt_to_equity,
                current_ratio=EXCLUDED.current_ratio, dividend_yield=EXCLUDED.dividend_yield,
                payout_ratio=EXCLUDED.payout_ratio, shares_outstanding=EXCLUDED.shares_outstanding
        """), row)
    return 1


# =============================================================================
# Macro / market indicators
# =============================================================================
def refresh_vix(start_date):
    print("  fetching VIX (yfinance)...")
    try:
        vix = yf.download("^VIX", start=start_date.strftime("%Y-%m-%d"),
                          progress=False, auto_adjust=True)
        if vix.empty:
            print("    no VIX data")
            return
        if isinstance(vix.columns, pd.MultiIndex):
            vix.columns = vix.columns.get_level_values(0)
        vix = vix.reset_index()
        vix["date"]     = pd.to_datetime(vix["Date"]).dt.date
        vix["vix_close"] = pd.to_numeric(vix["Close"], errors="coerce")
        vix["vix_ma20"]  = vix["vix_close"].rolling(20).mean()
        vix["vix_ma50"]  = vix["vix_close"].rolling(50).mean()
        out = vix[["date", "vix_close", "vix_ma20", "vix_ma50"]].copy()
        out = out.where(pd.notnull(out), None)
        with engine.begin() as con:
            for _, row in out.iterrows():
                con.execute(text("""
                    INSERT INTO market_indicators (date, vix_close, vix_ma20, vix_ma50)
                    VALUES (:date, :vix_close, :vix_ma20, :vix_ma50)
                    ON CONFLICT (date) DO UPDATE SET
                        vix_close=EXCLUDED.vix_close,
                        vix_ma20=EXCLUDED.vix_ma20,
                        vix_ma50=EXCLUDED.vix_ma50
                """), row.to_dict())
        print(f"    {len(out)} VIX rows")
    except Exception as e:
        print(f"    VIX failed: {e}")


def refresh_fred(start_date):
    if not FRED_KEY:
        print("  FRED_API_KEY not set — skipping macro series (VIX still loaded)")
        return
    print("  fetching FRED macro series...")
    series_map = {
        "DFF":      "fed_funds_rate",
        "DGS10":    "ten_year_yield",
        "DGS2":     "two_year_yield",
        "CPIAUCSL": "cpi_yoy",
        "UNRATE":   "unemployment_rate",
    }
    frames = {}
    for code, col in series_map.items():
        try:
            r = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={"series_id": code, "api_key": FRED_KEY,
                        "file_type": "json",
                        "observation_start": start_date.strftime("%Y-%m-%d")},
                timeout=30,
            )
            r.raise_for_status()
            obs = r.json().get("observations", [])
            df = pd.DataFrame(obs)
            if df.empty:
                continue
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df["val"]  = pd.to_numeric(df["value"], errors="coerce")
            df = df[["date", "val"]].rename(columns={"val": col})
            if code == "CPIAUCSL":
                df = df.set_index("date")
                df.index = pd.to_datetime(df.index)
                df = df.resample("MS").last()
                df[col] = (df[col] / df[col].shift(12) - 1) * 100
                df = df.reset_index()
                df["date"] = df["date"].dt.date
            frames[col] = df
            time.sleep(0.2)
        except Exception as e:
            print(f"    {code} failed: {e}")

    if not frames:
        return

    merged = pd.DataFrame({"date": pd.date_range(start_date, datetime.today()).date})
    for col, df in frames.items():
        merged = merged.merge(df, on="date", how="left")
    for col in series_map.values():
        if col in merged.columns:
            merged[col] = merged[col].ffill()

    if "ten_year_yield" in merged.columns and "two_year_yield" in merged.columns:
        merged["yield_curve_10y_2y"] = merged["ten_year_yield"] - merged["two_year_yield"]

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
                    fed_funds_rate=COALESCE(EXCLUDED.fed_funds_rate, market_indicators.fed_funds_rate),
                    ten_year_yield=COALESCE(EXCLUDED.ten_year_yield, market_indicators.ten_year_yield),
                    two_year_yield=COALESCE(EXCLUDED.two_year_yield, market_indicators.two_year_yield),
                    yield_curve_10y_2y=COALESCE(EXCLUDED.yield_curve_10y_2y, market_indicators.yield_curve_10y_2y),
                    cpi_yoy=COALESCE(EXCLUDED.cpi_yoy, market_indicators.cpi_yoy),
                    unemployment_rate=COALESCE(EXCLUDED.unemployment_rate, market_indicators.unemployment_rate)
            """), {k: row.get(k) for k in [
                "date", "fed_funds_rate", "ten_year_yield", "two_year_yield",
                "yield_curve_10y_2y", "cpi_yoy", "unemployment_rate",
            ]})
    print(f"    {len(merged)} macro rows")


def refresh_breadth():
    print("  calculating S&P 500 breadth from daily_bars...")
    with engine.begin() as con:
        con.execute(text("""
            INSERT INTO market_indicators (
                date, pct_above_ma200, pct_above_ma50, advance_decline_pct
            )
            SELECT
                d.date,
                100.0 * AVG(CASE WHEN d.close > d.ma_200 AND d.ma_200 IS NOT NULL
                                 THEN 1.0 ELSE 0.0 END) AS pct_above_ma200,
                100.0 * AVG(CASE WHEN d.close > d.ma_50  AND d.ma_50  IS NOT NULL
                                 THEN 1.0 ELSE 0.0 END) AS pct_above_ma50,
                100.0 * AVG(CASE WHEN d.daily_return > 0
                                 THEN 1.0 ELSE 0.0 END) AS adv_dec
            FROM daily_bars d
            JOIN tickers t ON t.ticker = d.ticker
            WHERE t.in_sp500 = TRUE
              AND d.date >= (CURRENT_DATE - INTERVAL '400 days')
            GROUP BY d.date
            ON CONFLICT (date) DO UPDATE SET
                pct_above_ma200=EXCLUDED.pct_above_ma200,
                pct_above_ma50=EXCLUDED.pct_above_ma50,
                advance_decline_pct=EXCLUDED.advance_decline_pct
        """))
    print("    breadth updated")


# =============================================================================
# Main
# =============================================================================
def get_watchlist():
    with engine.connect() as con:
        rows = con.execute(
            text("SELECT ticker FROM tickers WHERE is_active=TRUE ORDER BY ticker")
        ).fetchall()
    return [r[0] for r in rows]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full",        action="store_true")
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument("--years",  type=int, default=5,
                        help='Years of history. Use 20 for full election analysis.')
    parser.add_argument("--limit",  type=int, default=None)
    args = parser.parse_args()

    if not args.full and not args.incremental:
        args.incremental = True

    watchlist = get_watchlist()
    if not watchlist:
        sys.exit("No tickers in DB. Run load_tickers.py first.")
    if args.limit:
        watchlist = watchlist[:args.limit]

    mode = "FULL" if args.full else "INCREMENTAL"
    print(f"\n{'='*60}")
    print(f"Refresh: {mode} | {len(watchlist)} tickers | {date.today()}")
    print(f"{'='*60}\n")

    if args.full:
        # Extra days for MA warmup (200-day MA needs 200 trading days)
        price_start = date.today() - timedelta(days=365 * args.years + 210)
        trim_to     = date.today() - timedelta(days=365 * args.years)
    else:
        price_start = date.today() - timedelta(days=400)
        trim_to     = date.today() - timedelta(days=15)

    n_ok = n_fail = n_bars = n_earn = n_fund = 0

    for i, ticker in enumerate(watchlist, 1):
        try:
            fresh = fetch_prices(ticker, price_start)
            if fresh.empty:
                print(f"[{i}/{len(watchlist)}] {ticker}: no price data")
                n_fail += 1
                continue

            augmented = calc_indicators(fresh)

            if args.full:
                augmented = augmented[augmented["date"] >= trim_to]
                n_bars += upsert_bars(augmented, ticker, full_replace=True)
            else:
                augmented = augmented[augmented["date"] >= trim_to]
                n_bars += upsert_bars(augmented, ticker, full_replace=False)

            edf  = fetch_earnings(ticker)
            n_earn += upsert_earnings(edf, ticker)

            fund = fetch_fundamentals_snapshot(ticker)
            n_fund += upsert_fundamentals(fund)

            n_ok += 1
            if i % 50 == 0:
                print(f"[{i}/{len(watchlist)}] {ticker}: "
                      f"{n_ok} ok, {n_fail} failed so far")

            time.sleep(0.15)

        except Exception as e:
            print(f"[{i}/{len(watchlist)}] {ticker}: ERROR — {e}")
            n_fail += 1

    print(f"\nTicker work complete:")
    print(f"  {n_ok} ok | {n_fail} failed")
    print(f"  {n_bars:,} bar rows | {n_earn:,} earnings | {n_fund:,} fundamentals")

    print("\nRefreshing market indicators...")
    start_market = date.today() - timedelta(
        days=365 * (args.years if args.full else 1) + 60
    )
    refresh_vix(start_market)
    refresh_fred(start_market)
    refresh_breadth()

    # Sector rotation signals
    try:
        from rotation import run_rotation_refresh
        run_rotation_refresh(engine)
    except Exception as e:
        print(f"\nRotation refresh failed (non-fatal): {e}")

    # Phase 2: macro indicators, commodities, insider data, short interest, analysts
    try:
        from refresh_macro import run_macro_refresh
        run_macro_refresh(full=args.full)
    except Exception as e:
        print(f"\nPhase 2 macro refresh failed (non-fatal): {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
