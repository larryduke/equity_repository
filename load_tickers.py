"""
load_tickers.py — One-time loader for the ticker universe.

Populates the `tickers` table from FMP index constituent endpoints, applies the
$500M+ market cap filter, and tags index membership flags.

Run manually:
    python load_tickers.py

Re-run quarterly to pick up index reconstitutions.

Universe (Phase 1, US-only):
    S&P 500  + S&P 400 (mid-cap) + S&P 600 (small-cap) + NASDAQ 100 + Dow 30
    filtered to market cap >= $500M

Environment:
    FMP_API_KEY    — from FMP dashboard
    DATABASE_URL   — Supabase Transaction Pooler URL
"""
import os
import sys
import time
import requests
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

FMP_KEY = os.getenv("FMP_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

if not FMP_KEY:
    sys.exit("ERROR: FMP_API_KEY not set")
if not DATABASE_URL:
    sys.exit("ERROR: DATABASE_URL not set")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
BASE = "https://financialmodelingprep.com/api/v3"
MIN_MARKET_CAP = 500_000_000  # $500M filter


def fmp_get(path, params=None):
    """Wrapper for FMP GET with rate-limit friendly retries."""
    params = params or {}
    params["apikey"] = FMP_KEY
    url = f"{BASE}/{path}"
    for attempt in range(3):
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            wait = 2 ** attempt
            print(f"  rate-limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
    raise RuntimeError(f"FMP failed after retries: {url}")


def fetch_index_constituents(endpoint):
    """endpoint is 'sp500_constituent', 'nasdaq_constituent', 'dowjones_constituent'.
    FMP doesn't have a direct S&P 400/600 endpoint on every tier, so we handle
    those separately below."""
    data = fmp_get(endpoint)
    return {row["symbol"] for row in data if "symbol" in row}


def fetch_sp400_sp600():
    """S&P 400 (mid-cap) and S&P 600 (small-cap) constituents.
    FMP exposes these on Premium under 'historical/sp500_constituent' style paths;
    on Starter they may not be available, in which case we degrade gracefully."""
    sp400, sp600 = set(), set()
    try:
        data = fmp_get("sp400_constituent")
        sp400 = {row["symbol"] for row in data if "symbol" in row}
    except Exception as e:
        print(f"  S&P 400 not available on this FMP tier: {e}")
    try:
        data = fmp_get("sp600_constituent")
        sp600 = {row["symbol"] for row in data if "symbol" in row}
    except Exception as e:
        print(f"  S&P 600 not available on this FMP tier: {e}")
    return sp400, sp600


def fetch_company_profile(symbols):
    """Batched profile lookup — gets sector, industry, market cap, exchange.
    FMP supports up to ~50 symbols per call on /profile/{symbol1,symbol2,...}."""
    profiles = {}
    BATCH = 50
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        joined = ",".join(chunk)
        try:
            data = fmp_get(f"profile/{joined}")
            for row in data:
                sym = row.get("symbol")
                if sym:
                    profiles[sym] = row
        except Exception as e:
            print(f"  profile batch failed for {chunk[0]}..{chunk[-1]}: {e}")
        time.sleep(0.2)
    return profiles


def upsert_tickers(rows):
    """Insert/update tickers. ON CONFLICT updates the flags and market cap."""
    if not rows:
        return 0
    with engine.begin() as con:
        for r in rows:
            con.execute(text("""
                INSERT INTO tickers (
                    ticker, name, exchange, sector, industry, country, currency,
                    in_sp500, in_sp400, in_sp600, in_nasdaq100, in_dow30,
                    market_cap_usd, is_active, last_updated
                ) VALUES (
                    :ticker, :name, :exchange, :sector, :industry, :country, :currency,
                    :in_sp500, :in_sp400, :in_sp600, :in_nasdaq100, :in_dow30,
                    :market_cap_usd, TRUE, NOW()
                )
                ON CONFLICT (ticker) DO UPDATE SET
                    name = EXCLUDED.name,
                    exchange = EXCLUDED.exchange,
                    sector = EXCLUDED.sector,
                    industry = EXCLUDED.industry,
                    country = EXCLUDED.country,
                    currency = EXCLUDED.currency,
                    in_sp500 = EXCLUDED.in_sp500,
                    in_sp400 = EXCLUDED.in_sp400,
                    in_sp600 = EXCLUDED.in_sp600,
                    in_nasdaq100 = EXCLUDED.in_nasdaq100,
                    in_dow30 = EXCLUDED.in_dow30,
                    market_cap_usd = EXCLUDED.market_cap_usd,
                    is_active = TRUE,
                    last_updated = NOW()
            """), r)
    return len(rows)


def main():
    print("Loading ticker universe from FMP...")

    print("  fetching S&P 500...")
    sp500 = fetch_index_constituents("sp500_constituent")
    print(f"    {len(sp500)} symbols")

    print("  fetching NASDAQ 100...")
    ndx = fetch_index_constituents("nasdaq_constituent")
    print(f"    {len(ndx)} symbols")

    print("  fetching Dow 30...")
    dow = fetch_index_constituents("dowjones_constituent")
    print(f"    {len(dow)} symbols")

    print("  fetching S&P 400 + 600 (Premium-tier endpoints)...")
    sp400, sp600 = fetch_sp400_sp600()
    print(f"    S&P 400: {len(sp400)}, S&P 600: {len(sp600)}")

    universe = sp500 | ndx | dow | sp400 | sp600
    print(f"\nUnion universe: {len(universe)} unique symbols")

    print("\nFetching company profiles (sector, market cap, etc.)...")
    profiles = fetch_company_profile(sorted(universe))
    print(f"  got profiles for {len(profiles)} / {len(universe)}")

    rows = []
    skipped_cap = 0
    skipped_inactive = 0
    for sym in sorted(universe):
        p = profiles.get(sym)
        if not p:
            continue
        if p.get("isActivelyTrading") is False:
            skipped_inactive += 1
            continue
        mcap = p.get("mktCap") or 0
        if mcap < MIN_MARKET_CAP:
            skipped_cap += 1
            continue
        rows.append({
            "ticker":          sym,
            "name":            (p.get("companyName") or "")[:255],
            "exchange":        (p.get("exchangeShortName") or "")[:20],
            "sector":          (p.get("sector") or "")[:80] or None,
            "industry":        (p.get("industry") or "")[:120] or None,
            "country":         (p.get("country") or "US")[:40],
            "currency":        (p.get("currency") or "USD")[:10],
            "in_sp500":        sym in sp500,
            "in_sp400":        sym in sp400,
            "in_sp600":        sym in sp600,
            "in_nasdaq100":    sym in ndx,
            "in_dow30":        sym in dow,
            "market_cap_usd":  float(mcap),
        })

    print(f"\nFiltered: {len(rows)} kept, {skipped_cap} below ${MIN_MARKET_CAP/1e6:.0f}M, "
          f"{skipped_inactive} inactive")

    n = upsert_tickers(rows)
    print(f"\nUpserted {n} tickers into the database.")

    # Print quick breakdown
    with engine.connect() as con:
        by_sector = pd.read_sql(
            text("SELECT sector, COUNT(*) AS n FROM tickers GROUP BY sector ORDER BY n DESC"),
            con,
        )
    print("\nBy sector:")
    print(by_sector.to_string(index=False))


if __name__ == "__main__":
    main()
