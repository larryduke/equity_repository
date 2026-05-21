"""
enrich_fundamentals.py — Backfills fundamental ratios into the `fundamentals` table.

Based on actual FMP stable endpoint responses (verified via fmp_field_diagnostic.py):

  stable/ratios-ttm       — returns priceToEarningsRatioTTM, priceToBookRatioTTM,
                           netProfitMarginTTM, etc. with `TTM` suffix.
  stable/key-metrics-ttm  — returns enterpriseValueTTM, evToEBITDATTM,
                           returnOnEquityTTM, etc.
  stable/price-target-consensus — returns targetConsensus, targetHigh, targetLow
  stable/income-statement — quarterly actuals (used elsewhere to derive surprises)

Run nightly or on-demand. Idempotent.

Environment:
    FMP_API_KEY
    DATABASE_URL
"""
import os
import sys
import time
import requests
import pandas as pd
from datetime import date
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
            r = requests.get(url, params=params, timeout=20)
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
        if f != f or abs(f) > 1e15:
            return None
        return f
    except (TypeError, ValueError):
        return None


def fetch_enrichment(ticker):
    """Pull ratios-ttm + key-metrics-ttm + price-target-consensus,
    merge into one flat dict with our DB column names."""
    out = {}

    # --- ratios-ttm: the verified field names from diagnostic ---
    r = fmp_get("ratios-ttm", params={"symbol": ticker})
    if r and isinstance(r, list) and r:
        row = r[0]
        out["pe_trailing"]      = safe_float(row.get("priceToEarningsRatioTTM"))
        out["price_to_sales"]   = safe_float(row.get("priceToSalesRatioTTM"))
        out["price_to_book"]    = safe_float(row.get("priceToBookRatioTTM"))
        out["peg_ratio"]        = safe_float(row.get("priceToEarningsGrowthRatioTTM"))
        out["profit_margin"]    = safe_float(row.get("netProfitMarginTTM"))
        out["operating_margin"] = safe_float(row.get("operatingProfitMarginTTM"))
        out["gross_margin"]     = safe_float(row.get("grossProfitMarginTTM"))
        out["debt_to_equity"]   = safe_float(row.get("debtToEquityRatioTTM"))
        out["current_ratio"]    = safe_float(row.get("currentRatioTTM"))
        out["quick_ratio"]      = safe_float(row.get("quickRatioTTM"))
        out["dividend_yield"]   = safe_float(row.get("dividendYieldTTM"))
        out["payout_ratio"]     = safe_float(row.get("dividendPayoutRatioTTM"))

    # --- key-metrics-ttm: enterprise value and yields ---
    k = fmp_get("key-metrics-ttm", params={"symbol": ticker})
    if k and isinstance(k, list) and k:
        row = k[0]
        out["enterprise_value"]      = safe_float(row.get("enterpriseValueTTM"))
        out["ev_ebitda"]             = safe_float(row.get("evToEBITDATTM"))
        out["ev_sales"]              = safe_float(row.get("evToSalesTTM"))
        out["return_on_equity"]      = safe_float(row.get("returnOnEquityTTM"))
        out["return_on_assets"]      = safe_float(row.get("returnOnAssetsTTM"))
        out["return_on_invested_cap"] = safe_float(row.get("returnOnInvestedCapitalTTM"))
        out["fcf_yield"]             = safe_float(row.get("freeCashFlowYieldTTM"))
        out["earnings_yield"]        = safe_float(row.get("earningsYieldTTM"))
        out["net_debt_to_ebitda"]    = safe_float(row.get("netDebtToEBITDATTM"))

    # --- price-target-consensus: analyst price targets ---
    pt = fmp_get("price-target-consensus", params={"symbol": ticker})
    if pt and isinstance(pt, list) and pt:
        row = pt[0]
        out["analyst_target_price"] = safe_float(row.get("targetConsensus"))
        out["analyst_target_high"]  = safe_float(row.get("targetHigh"))
        out["analyst_target_low"]   = safe_float(row.get("targetLow"))

    return out


def main():
    print("=" * 60)
    print("Enriching fundamentals (verified field names)")
    print("=" * 60)

    # Add columns that don't yet exist on the table
    print("\nEnsuring all enrichment columns exist on fundamentals table...")
    new_cols = [
        ("gross_margin",          "DOUBLE PRECISION"),
        ("quick_ratio",           "DOUBLE PRECISION"),
        ("ev_sales",              "DOUBLE PRECISION"),
        ("return_on_assets",      "DOUBLE PRECISION"),
        ("return_on_invested_cap", "DOUBLE PRECISION"),
        ("fcf_yield",             "DOUBLE PRECISION"),
        ("earnings_yield",        "DOUBLE PRECISION"),
        ("net_debt_to_ebitda",    "DOUBLE PRECISION"),
        ("analyst_target_high",   "DOUBLE PRECISION"),
        ("analyst_target_low",    "DOUBLE PRECISION"),
    ]
    with engine.begin() as con:
        for col, typ in new_cols:
            con.execute(text(
                f"ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS {col} {typ}"
            ))
    print("  columns ready")

    # Get list of active tickers
    with engine.connect() as con:
        tickers = [r[0] for r in con.execute(text(
            "SELECT ticker FROM tickers WHERE is_active = TRUE ORDER BY ticker"
        )).fetchall()]

    if not tickers:
        sys.exit("No active tickers")

    print(f"\nProcessing {len(tickers)} tickers (~{len(tickers)*0.5/60:.1f} min)...\n")

    today = date.today()
    n_ok = n_skip = n_fail = 0

    for i, ticker in enumerate(tickers, 1):
        try:
            metrics = fetch_enrichment(ticker)
            if not metrics or all(v is None for v in metrics.values()):
                n_skip += 1
                if i <= 3:
                    print(f"  [{i}/{len(tickers)}] {ticker}: no data", flush=True)
                time.sleep(0.15)
                continue

            with engine.begin() as con:
                set_clauses = []
                params = {"ticker": ticker, "date": today}
                for col, val in metrics.items():
                    if val is None:
                        continue
                    set_clauses.append(f"{col} = :{col}")
                    params[col] = val

                if not set_clauses:
                    n_skip += 1
                    continue

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

    # Verify coverage
    with engine.connect() as con:
        verify = pd.read_sql(text("""
            SELECT
                COUNT(*) FILTER (WHERE pe_trailing IS NOT NULL) AS has_pe_trailing,
                COUNT(*) FILTER (WHERE ev_ebitda IS NOT NULL) AS has_ev_ebitda,
                COUNT(*) FILTER (WHERE profit_margin IS NOT NULL) AS has_profit_margin,
                COUNT(*) FILTER (WHERE return_on_equity IS NOT NULL) AS has_roe,
                COUNT(*) FILTER (WHERE analyst_target_price IS NOT NULL) AS has_target,
                COUNT(*) FILTER (WHERE peg_ratio IS NOT NULL) AS has_peg,
                COUNT(*) AS total
            FROM fundamentals
            WHERE date = (SELECT MAX(date) FROM fundamentals)
        """), con)

    print(f"\nLatest snapshot coverage:")
    print(verify.to_string(index=False))


if __name__ == "__main__":
    main()
