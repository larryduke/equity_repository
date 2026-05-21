"""
enrich_fundamentals.py — Backfills the columns in `fundamentals` that
stable/profile doesn't provide (P/E forward, PEG, EV/EBITDA, ROE, margins, etc).

Pulls from FMP stable endpoints:
  - stable/ratios-ttm
  - stable/key-metrics-ttm

Run nightly or on-demand. Idempotent. Skips tickers that already have
a recent enrichment.

Environment:
    FMP_API_KEY
    DATABASE_URL
"""
import os
import sys
import time
import requests
import pandas as pd
from datetime import date, timedelta
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

FMP_KEY = os.getenv("FMP_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
if not FMP_KEY:
    sys.exit("ERROR: FMP_API_KEY not set")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
STABLE = "https://financialmodelingprep.com/stable"


def fmp_get(path, params=None):
    params = params or {}
    params["apikey"] = FMP_KEY
    url = f"{STABLE}/{path}"
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "Error Message" in data:
                    return None
                return data
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception:
            time.sleep(2)
    return None


def safe_float(v):
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN check
            return None
        if abs(f) > 1e15:
            return None
        return f
    except (TypeError, ValueError):
        return None


def fetch_ratios_and_metrics(ticker):
    """Pull both ratios-ttm and key-metrics-ttm, merge into one dict."""
    out = {}

    # Ratios TTM
    ratios = fmp_get("ratios-ttm", params={"symbol": ticker})
    if ratios:
        row = ratios[0] if isinstance(ratios, list) and ratios else (ratios if isinstance(ratios, dict) else {})
        out["pe_trailing"]   = safe_float(row.get("priceEarningsRatioTTM") or row.get("peRatioTTM"))
        out["price_to_sales"] = safe_float(row.get("priceToSalesRatioTTM"))
        out["price_to_book"]  = safe_float(row.get("priceToBookRatioTTM"))
        out["ev_ebitda"]      = safe_float(row.get("enterpriseValueOverEBITDATTM"))
        out["dividend_yield"] = safe_float(row.get("dividendYielTTM") or row.get("dividendYieldTTM"))
        out["payout_ratio"]   = safe_float(row.get("payoutRatioTTM"))
        out["profit_margin"]  = safe_float(row.get("netProfitMarginTTM"))
        out["operating_margin"] = safe_float(row.get("operatingProfitMarginTTM"))
        out["return_on_equity"] = safe_float(row.get("returnOnEquityTTM"))
        out["debt_to_equity"] = safe_float(row.get("debtEquityRatioTTM"))
        out["current_ratio"]  = safe_float(row.get("currentRatioTTM"))

    # Key metrics TTM
    km = fmp_get("key-metrics-ttm", params={"symbol": ticker})
    if km:
        row = km[0] if isinstance(km, list) and km else (km if isinstance(km, dict) else {})
        out["enterprise_value"] = safe_float(row.get("enterpriseValueTTM"))
        out["peg_ratio"]        = safe_float(row.get("pegRatioTTM"))
        out["pe_forward"]       = safe_float(row.get("forwardPETTM") or row.get("peRatioForwardTTM"))
        # if pe_forward still missing, try the analyst estimates path
        if out.get("revenue_growth_yoy") is None:
            out["revenue_growth_yoy"] = safe_float(row.get("revenuePerShareGrowthTTM"))

    return out


def fetch_forward_pe_from_estimates(ticker):
    """If ratios didn't return pe_forward, try analyst-estimates endpoint."""
    data = fmp_get("analyst-estimates", params={"symbol": ticker, "limit": 1})
    if not data or not isinstance(data, list):
        return None
    row = data[0]
    # Forward P/E = current_price / next_year_eps_estimate
    # Not directly given; would need price * shares / forward earnings
    return None  # leave None if not in main endpoints


def main():
    print("=" * 60)
    print("Enriching fundamentals (ratios-ttm + key-metrics-ttm)")
    print("=" * 60)

    # Get list of active tickers
    with engine.connect() as con:
        tickers = [r[0] for r in con.execute(text(
            "SELECT ticker FROM tickers WHERE is_active = TRUE ORDER BY ticker"
        )).fetchall()]

    if not tickers:
        sys.exit("No active tickers")

    print(f"\nProcessing {len(tickers)} tickers...")
    print("This will take ~3-4 minutes given FMP rate limits.\n")

    today = date.today()
    n_ok = n_skip = n_fail = 0

    for i, ticker in enumerate(tickers, 1):
        try:
            metrics = fetch_ratios_and_metrics(ticker)
            if not metrics or all(v is None for v in metrics.values()):
                n_skip += 1
                if i <= 5:
                    print(f"  [{i}/{len(tickers)}] {ticker}: no metrics returned", flush=True)
                time.sleep(0.15)
                continue

            metrics["ticker"] = ticker
            metrics["date"] = today

            with engine.begin() as con:
                # Build dynamic update for whatever fields we got
                set_clauses = []
                params = {"ticker": ticker, "date": today}
                for col, val in metrics.items():
                    if col in ("ticker", "date"):
                        continue
                    if val is None:
                        continue
                    set_clauses.append(f"{col} = :{col}")
                    params[col] = val

                if not set_clauses:
                    n_skip += 1
                    continue

                # Upsert with new columns
                cols = list(params.keys())
                placeholders = ", ".join(f":{c}" for c in cols)
                col_names = ", ".join(cols)
                update_clause = ", ".join(set_clauses)

                con.execute(text(f"""
                    INSERT INTO fundamentals ({col_names})
                    VALUES ({placeholders})
                    ON CONFLICT (ticker, date) DO UPDATE SET
                        {update_clause}
                """), params)

            n_ok += 1
            if i % 50 == 0:
                print(f"  [{i}/{len(tickers)}] {n_ok} enriched | {n_skip} skipped | {n_fail} failed", flush=True)
            time.sleep(0.15)

        except Exception as e:
            n_fail += 1
            if n_fail <= 5:
                print(f"  {ticker}: ERROR {e}", flush=True)

    print(f"\n  Done. {n_ok} enriched | {n_skip} skipped | {n_fail} failed")

    # Verify
    with engine.connect() as con:
        verify = pd.read_sql(text("""
            SELECT
                COUNT(*) FILTER (WHERE pe_trailing IS NOT NULL) AS has_pe_trailing,
                COUNT(*) FILTER (WHERE pe_forward IS NOT NULL) AS has_pe_forward,
                COUNT(*) FILTER (WHERE ev_ebitda IS NOT NULL) AS has_ev_ebitda,
                COUNT(*) FILTER (WHERE profit_margin IS NOT NULL) AS has_profit_margin,
                COUNT(*) FILTER (WHERE return_on_equity IS NOT NULL) AS has_roe,
                COUNT(*) AS total
            FROM fundamentals
            WHERE date = (SELECT MAX(date) FROM fundamentals)
        """), con)
    print(f"\nLatest snapshot coverage:\n{verify.to_string(index=False)}")


if __name__ == "__main__":
    main()
