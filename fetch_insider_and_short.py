"""
fetch_insider_and_short.py — Robust fetch for insider transactions and short
interest with visibility into why endpoints might be returning empty.

Replaces the silent-fail version inside refresh_macro.py.

Tries multiple endpoint variations because FMP renames endpoints periodically:
  Insider:  insider-trading, insider-trades, insider-trading-rss-feed
  Short:    short-float, short-interest, share-float

Logs the actual response shape on failure so we can diagnose.

Environment:
    FMP_API_KEY, DATABASE_URL
"""
import os
import sys
import time
import json
import requests
import pandas as pd
from datetime import date, timedelta
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

FMP_KEY = os.getenv("FMP_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

STABLE = "https://financialmodelingprep.com/stable"


def fmp_get_raw(path, params=None):
    """Returns (status_code, parsed_json_or_text)."""
    params = params or {}
    params["apikey"] = FMP_KEY
    url = f"{STABLE}/{path}"
    try:
        r = requests.get(url, params=params, timeout=20)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text[:200]
    except Exception as e:
        return 0, str(e)


def diagnose_endpoint(path, params=None):
    """Show exactly what an endpoint returns for the first ticker."""
    code, data = fmp_get_raw(path, params)
    if code != 200:
        print(f"    [{path}] HTTP {code}: {str(data)[:150]}")
        return None
    if isinstance(data, dict):
        if "Error Message" in data:
            print(f"    [{path}] FMP error: {data['Error Message'][:150]}")
            return None
        print(f"    [{path}] dict response with keys: {list(data.keys())[:6]}")
        return data
    if isinstance(data, list):
        if not data:
            print(f"    [{path}] empty list (endpoint works but no data for this query)")
            return []
        sample = data[0]
        print(f"    [{path}] {len(data)} rows. Sample keys: {list(sample.keys())[:8]}")
        return data
    print(f"    [{path}] unknown shape: {type(data)}")
    return None


# ============================================================================
# Discovery: find which endpoints work on your tier
# ============================================================================
def discover_endpoints():
    """Test all candidate endpoints for a sample ticker (AAPL) and report results."""
    print("\n" + "=" * 60)
    print("Discovering working endpoints for AAPL")
    print("=" * 60)

    print("\nInsider endpoints:")
    insider_paths = [
        ("insider-trading",         {"symbol": "AAPL", "limit": 10}),
        ("insider-trades",          {"symbol": "AAPL", "limit": 10}),
        ("insider-trading-rss-feed", {"limit": 10}),
        ("acquisition-of-beneficial-ownership", {"symbol": "AAPL"}),
    ]
    insider_working = None
    for path, params in insider_paths:
        data = diagnose_endpoint(path, params)
        if isinstance(data, list) and data:
            insider_working = path
            break
        time.sleep(0.3)

    print("\nShort interest endpoints:")
    short_paths = [
        ("short-float",             {"symbol": "AAPL"}),
        ("shares-float",            {"symbol": "AAPL"}),
        ("share-float",             {"symbol": "AAPL"}),
        ("share-float-all",         {"symbol": "AAPL"}),
        ("short-interest",          {"symbol": "AAPL"}),
        ("historical-short-interest", {"symbol": "AAPL"}),
    ]
    short_working = None
    for path, params in short_paths:
        data = diagnose_endpoint(path, params)
        if data and (isinstance(data, list) and data or isinstance(data, dict) and data):
            short_working = path
            break
        time.sleep(0.3)

    print(f"\n\nFOUND WORKING:")
    print(f"  Insider:  {insider_working or 'NONE — not available on your tier'}")
    print(f"  Short:    {short_working or 'NONE — not available on your tier'}")

    return insider_working, short_working


# ============================================================================
# Fetch and write
# ============================================================================
def fetch_insider(ticker, endpoint):
    if not endpoint:
        return []
    code, data = fmp_get_raw(endpoint, params={"symbol": ticker, "limit": 50})
    if code != 200 or not isinstance(data, list):
        return []
    return data


def parse_insider_row(item, ticker):
    """Normalize different FMP insider response shapes into our schema."""
    tx_date = (item.get("transactionDate")
               or item.get("filingDate")
               or item.get("date"))
    if not tx_date:
        return None
    try:
        tx_date = pd.to_datetime(tx_date).date()
    except Exception:
        return None

    tx_type_raw = (item.get("transactionType")
                   or item.get("type")
                   or "").upper()
    if "P" in tx_type_raw or "PURCHASE" in tx_type_raw or "BUY" in tx_type_raw:
        tx_type = "Buy"
    elif "S" in tx_type_raw or "SALE" in tx_type_raw or "SELL" in tx_type_raw:
        tx_type = "Sell"
    elif "A" in tx_type_raw or "GRANT" in tx_type_raw or "AWARD" in tx_type_raw:
        tx_type = "Grant"
    else:
        tx_type = "Other"

    def fnum(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "ticker": ticker,
        "date": tx_date,
        "insider_name": (item.get("reportingName")
                         or item.get("insiderName")
                         or "")[:255],
        "role": (item.get("typeOfOwner")
                 or item.get("reportingTitle")
                 or "")[:50],
        "transaction_type": tx_type,
        "shares": fnum(item.get("securitiesTransacted") or item.get("shares")),
        "price_per_share": fnum(item.get("price") or item.get("pricePerShare")),
        "value_usd": fnum(item.get("value") or item.get("transactionValue")),
        "shares_owned_after": fnum(item.get("securitiesOwned")
                                   or item.get("sharesOwnedAfter")),
        "filing_date": date.today(),
    }


def fetch_all_insider(endpoint):
    print(f"\nFetching insider transactions using: {endpoint}")
    with engine.connect() as con:
        tickers = [r[0] for r in con.execute(text(
            "SELECT ticker FROM tickers WHERE is_active AND in_sp500 ORDER BY ticker"
        )).fetchall()]

    n_inserted = 0
    cutoff = date.today() - timedelta(days=90)

    for i, ticker in enumerate(tickers, 1):
        raw = fetch_insider(ticker, endpoint)
        for item in raw:
            row = parse_insider_row(item, ticker)
            if not row:
                continue
            if row["date"] < cutoff:
                continue
            try:
                with engine.begin() as con:
                    con.execute(text("""
                        INSERT INTO insider_transactions
                          (ticker, date, insider_name, role, transaction_type,
                           shares, price_per_share, value_usd,
                           shares_owned_after, filing_date)
                        VALUES
                          (:ticker, :date, :insider_name, :role, :transaction_type,
                           :shares, :price_per_share, :value_usd,
                           :shares_owned_after, :filing_date)
                        ON CONFLICT DO NOTHING
                    """), row)
                    n_inserted += 1
            except Exception as e:
                if n_inserted < 3:
                    print(f"    {ticker} insert error: {e}")
        if i % 100 == 0:
            print(f"  [{i}/{len(tickers)}] {n_inserted} inserted", flush=True)
        time.sleep(0.15)

    print(f"  Total inserted: {n_inserted}")


def fetch_all_short(endpoint):
    print(f"\nFetching short interest using: {endpoint}")
    with engine.connect() as con:
        tickers = [r[0] for r in con.execute(text(
            "SELECT ticker FROM tickers WHERE is_active AND in_sp500 ORDER BY ticker"
        )).fetchall()]

    n_inserted = 0
    for i, ticker in enumerate(tickers, 1):
        code, data = fmp_get_raw(endpoint, params={"symbol": ticker})
        if code != 200:
            continue
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            continue

        short_pct = (data.get("shortPercent")
                     or data.get("shortPercentFloat")
                     or data.get("shortPercentOutstanding")
                     or data.get("shortFloat"))
        if short_pct is None:
            continue

        try:
            short_pct = float(short_pct)
            with engine.begin() as con:
                con.execute(text("""
                    INSERT INTO short_interest
                      (ticker, date, short_interest, short_pct_float, days_to_cover)
                    VALUES
                      (:ticker, :date, :short_interest, :short_pct_float, :days_to_cover)
                    ON CONFLICT (ticker, date) DO UPDATE SET
                        short_pct_float = EXCLUDED.short_pct_float,
                        days_to_cover   = EXCLUDED.days_to_cover
                """), {
                    "ticker": ticker, "date": date.today(),
                    "short_interest": float(data.get("shortInterest") or 0),
                    "short_pct_float": short_pct,
                    "days_to_cover": float(data.get("shortRatio") or 0),
                })
            n_inserted += 1
        except Exception:
            pass

        if i % 100 == 0:
            print(f"  [{i}/{len(tickers)}] {n_inserted} inserted", flush=True)
        time.sleep(0.15)

    print(f"  Total inserted: {n_inserted}")


def main():
    if not FMP_KEY:
        sys.exit("ERROR: FMP_API_KEY not set")

    print("=" * 60)
    print("Fixing insider + short interest pipelines")
    print("=" * 60)

    insider_ep, short_ep = discover_endpoints()

    if insider_ep:
        fetch_all_insider(insider_ep)
    else:
        print("\nInsider endpoint not available — skipping. Subscription tier may not include this.")

    if short_ep:
        fetch_all_short(short_ep)
    else:
        print("\nShort interest endpoint not available — skipping. Subscription tier may not include this.")

    print("\nDone.")


if __name__ == "__main__":
    main()
