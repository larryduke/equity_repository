"""
load_tickers.py — One-time loader for the ticker universe.

Uses FMP stable API (post-Aug 2025). All endpoints under /stable/.

Index constituents:
    stable/sp500-constituent      — S&P 500
    stable/nasdaq-constituent     — NASDAQ 100
    stable/dowjones-constituent   — Dow 30
    stable/sp500-companies        — alternate S&P 500 endpoint
    (S&P 400 / 600 via stock screener if available)

Company profiles:
    stable/profile?symbol=X       — one call per ticker
    Fields: mktCap (market cap), companyName, sector, etc.

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
STABLE = "https://financialmodelingprep.com/stable"
MIN_MARKET_CAP = 500_000_000


def fmp_get(path, params=None):
    params = params or {}
    params["apikey"] = FMP_KEY
    url = f"{STABLE}/{path}"
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "Error Message" in data:
                    raise RuntimeError(f"FMP error {path}: {data['Error Message'][:150]}")
                return data
            if r.status_code == 404:
                raise RuntimeError(f"Endpoint not found: {path}")
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (401, 403):
                raise RuntimeError(f"FMP auth {r.status_code}: {path}")
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"FMP failed after retries: {path}")


# ---------------------------------------------------------------------------
# Index constituents — try multiple endpoint names, take first that works
# ---------------------------------------------------------------------------
def fetch_constituent(endpoint_names, symbol_key="symbol"):
    """Try a list of endpoint names, return set of symbols from first that works."""
    for name in endpoint_names:
        try:
            data = fmp_get(name)
            if data and isinstance(data, list) and len(data) > 10:
                syms = {row.get(symbol_key, "").upper()
                        for row in data if row.get(symbol_key)}
                print(f"    {name}: {len(syms)} symbols")
                return syms
        except Exception as e:
            print(f"    {name} failed: {e}")
    return set()


def fetch_sp500():
    return fetch_constituent([
        "sp500-constituent",
        "sp500-companies",
        "historical/sp500_constituent",   # may work on some tiers
    ])


def fetch_nasdaq100():
    return fetch_constituent([
        "nasdaq-constituent",
        "nasdaq100-constituent",
        "historical/nasdaq_constituent",
    ])


def fetch_dow30():
    return fetch_constituent([
        "dowjones-constituent",
        "dow-jones-constituent",
        "historical/dowjones_constituent",
    ])


def fetch_mid_small_cap():
    """Try to get S&P 400 + 600 via stock screener."""
    sp400, sp600 = set(), set()
    try:
        # Screen for $500M-$10B market cap in major US exchanges as a proxy for mid/small cap
        data = fmp_get("stock-screener", params={
            "marketCapMoreThan": 500_000_000,
            "marketCapLessThan": 10_000_000_000,
            "exchange": "NYSE,NASDAQ",
            "limit": 2000,
        })
        if data and isinstance(data, list):
            syms = {row.get("symbol", "").upper() for row in data if row.get("symbol")}
            print(f"    stock-screener (mid/small proxy): {len(syms)} symbols")
            return syms
    except Exception as e:
        print(f"    stock-screener failed: {e}")
    return set()


# ---------------------------------------------------------------------------
# Profile fetching
# ---------------------------------------------------------------------------
def get_market_cap(profile):
    """Extract market cap from profile — FMP uses different key names."""
    for key in ("mktCap", "marketCap", "market_cap", "MarketCap"):
        v = profile.get(key)
        if v and float(v) > 0:
            return float(v)
    return 0.0


def fetch_profile(symbol):
    """Fetch a single ticker profile."""
    try:
        data = fmp_get("profile", params={"symbol": symbol})
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict) and data.get("symbol"):
            return data
        return None
    except Exception:
        return None


def fetch_profiles(symbols):
    profiles = {}
    symbols = sorted(symbols)
    total = len(symbols)
    mins = total * 0.15 / 60
    print(f"  Fetching {total} profiles (~{mins:.1f} min)...")
    for i, sym in enumerate(symbols, 1):
        p = fetch_profile(sym)
        if p:
            profiles[sym] = p
        if i % 100 == 0:
            print(f"  {i}/{total} ({len(profiles)} valid)...")
        time.sleep(0.15)
    return profiles


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------
def upsert_tickers(rows):
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
                    name=EXCLUDED.name, exchange=EXCLUDED.exchange,
                    sector=EXCLUDED.sector, industry=EXCLUDED.industry,
                    country=EXCLUDED.country, currency=EXCLUDED.currency,
                    in_sp500=EXCLUDED.in_sp500, in_sp400=EXCLUDED.in_sp400,
                    in_sp600=EXCLUDED.in_sp600, in_nasdaq100=EXCLUDED.in_nasdaq100,
                    in_dow30=EXCLUDED.in_dow30, market_cap_usd=EXCLUDED.market_cap_usd,
                    is_active=TRUE, last_updated=NOW()
            """), r)
    return len(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 60)
    print("Loading ticker universe")
    print("=" * 60)

    # Step 1: Index constituents
    print("\nStep 1: Fetching index constituents (stable API)...")
    sp500 = fetch_sp500()
    ndx   = fetch_nasdaq100()
    dow   = fetch_dow30()

    # Mid/small cap via screener
    print("  Fetching mid/small cap via screener...")
    mid_small = fetch_mid_small_cap()

    # If all FMP constituent endpoints fail, use hardcoded Dow 30 minimum
    if not dow:
        dow = {
            "AAPL","AMGN","AXP","BA","CAT","CRM","CSCO","CVX","DIS","DOW",
            "GS","HD","HON","IBM","INTC","JNJ","JPM","KO","MCD","MMM",
            "MRK","MSFT","NKE","PG","TRV","UNH","V","VZ","WBA","WMT",
        }
        print(f"  Using hardcoded Dow 30 ({len(dow)} symbols)")

    universe = sp500 | ndx | dow | mid_small
    print(f"\n  Universe: {len(universe)} symbols "
          f"(SP500={len(sp500)}, NDX={len(ndx)}, DOW={len(dow)}, "
          f"screener={len(mid_small)})")

    if len(universe) < 30:
        print("\nWARNING: Universe is very small. Check that FMP constituent "
              "endpoints are accessible on your tier.")
        print("Proceeding with available symbols...")

    # Step 2: Profiles
    print("\nStep 2: Fetching company profiles...")
    profiles = fetch_profiles(universe)

    # DEBUG: show what keys are in a sample profile
    if profiles:
        sample = next(iter(profiles.values()))
        cap_keys = {k: v for k, v in sample.items()
                    if "cap" in k.lower() or "market" in k.lower() or "mkt" in k.lower()}
        print(f"  Sample profile keys for market cap: {cap_keys}")

    # Step 3: Filter
    print(f"\nStep 3: Filtering to ${MIN_MARKET_CAP/1e6:.0f}M+ market cap...")
    rows = []
    skipped = {"cap": 0, "inactive": 0, "no_profile": 0}

    for sym in sorted(universe):
        p = profiles.get(sym)
        if not p:
            skipped["no_profile"] += 1
            continue
        if p.get("isActivelyTrading") is False:
            skipped["inactive"] += 1
            continue
        mcap = get_market_cap(p)
        if mcap < MIN_MARKET_CAP:
            skipped["cap"] += 1
            continue
        rows.append({
            "ticker":        sym,
            "name":          (p.get("companyName") or p.get("name") or "")[:255],
            "exchange":      (p.get("exchangeShortName") or p.get("exchange") or "")[:20],
            "sector":        p.get("sector") or None,
            "industry":      p.get("industry") or None,
            "country":       (p.get("country") or "US")[:40],
            "currency":      (p.get("currency") or "USD")[:10],
            "in_sp500":      sym in sp500,
            "in_sp400":      sym in mid_small and sym not in sp500,
            "in_sp600":      False,
            "in_nasdaq100":  sym in ndx,
            "in_dow30":      sym in dow,
            "market_cap_usd": mcap,
        })

    print(f"  Kept {len(rows)} | "
          f"skipped cap={skipped['cap']}, "
          f"inactive={skipped['inactive']}, "
          f"no_profile={skipped['no_profile']}")

    # Step 4: Upsert
    print(f"\nStep 4: Writing {len(rows)} tickers to database...")
    n = upsert_tickers(rows)
    print(f"  Done. {n} rows upserted.")

    # Summary
    with engine.connect() as con:
        counts = con.execute(text("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE in_sp500)     AS sp500,
                   COUNT(*) FILTER (WHERE in_nasdaq100)  AS ndx,
                   COUNT(*) FILTER (WHERE in_dow30)      AS dow
            FROM tickers WHERE is_active
        """)).fetchone()
        top_sectors = pd.read_sql(
            text("SELECT sector, COUNT(*) n FROM tickers "
                 "WHERE is_active AND sector IS NOT NULL "
                 "GROUP BY sector ORDER BY n DESC LIMIT 10"),
            con,
        )

    print(f"\n{'='*60}")
    print(f"FINAL: {counts[0]} tickers | "
          f"S&P500={counts[1]} | NDX={counts[2]} | Dow={counts[3]}")
    print("\nTop sectors:")
    print(top_sectors.to_string(index=False))


if __name__ == "__main__":
    main()
