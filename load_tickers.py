"""
load_tickers.py — One-time loader for the ticker universe.

Uses FMP's current (post-Aug 2025) v4 API + Wikipedia fallbacks.
The old v3 sp500_constituent endpoint is retired.

Strategy (in priority order):
  1. FMP v4 ETF holdings: SPY=S&P500, QQQ=NDX100, DIA=Dow30, MDY=SP400, IJR=SP600
  2. Wikipedia fallback if ETF holdings return too few symbols
  3. Hardcoded Dow 30 as last resort

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
BASE_V3 = "https://financialmodelingprep.com/api/v3"
BASE_V4 = "https://financialmodelingprep.com/api/v4"
MIN_MARKET_CAP = 500_000_000


def fmp_get(url, params=None):
    params = params or {}
    params["apikey"] = FMP_KEY
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "Error Message" in data:
                    raise RuntimeError(f"FMP error: {data['Error Message'][:200]}")
                return data
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (401, 403):
                raise RuntimeError(f"FMP auth error {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"FMP failed after retries: {url}")


def fetch_etf_holdings(etf_symbol):
    """FMP v4 ETF holdings as index proxy."""
    try:
        data = fmp_get(f"{BASE_V4}/etf-holdings", params={"symbol": etf_symbol})
        if not data or not isinstance(data, list):
            return set()
        symbols = {row.get("asset", "").upper() for row in data if row.get("asset")}
        print(f"    {etf_symbol}: {len(symbols)} holdings")
        return symbols
    except Exception as e:
        print(f"    {etf_symbol} failed: {e}")
        return set()


def fetch_sp500_wikipedia():
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            attrs={"id": "constituents"},
        )
        df = tables[0]
        col = next((c for c in df.columns if "symbol" in c.lower()), df.columns[0])
        syms = set(df[col].str.upper().str.replace(".", "-", regex=False))
        print(f"    Wikipedia S&P500: {len(syms)} symbols")
        return syms
    except Exception as e:
        print(f"    Wikipedia S&P500 failed: {e}")
        return set()


def fetch_ndx_wikipedia():
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
        for t in tables:
            cols_lower = [c.lower() for c in t.columns]
            if any("ticker" in c or "symbol" in c for c in cols_lower):
                col = next(c for c in t.columns if "ticker" in c.lower() or "symbol" in c.lower())
                syms = set(t[col].dropna().astype(str).str.upper())
                if len(syms) > 80:
                    print(f"    Wikipedia NDX: {len(syms)} symbols")
                    return syms
        return set()
    except Exception as e:
        print(f"    Wikipedia NDX failed: {e}")
        return set()


DOW30 = {
    "AAPL","AMGN","AXP","BA","CAT","CRM","CSCO","CVX","DIS","DOW",
    "GS","HD","HON","IBM","INTC","JNJ","JPM","KO","MCD","MMM",
    "MRK","MSFT","NKE","PG","TRV","UNH","V","VZ","WBA","WMT",
}


def fetch_profiles_batch(symbols):
    profiles = {}
    symbols = sorted(symbols)
    BATCH = 50
    total = len(symbols)
    for i in range(0, total, BATCH):
        chunk = symbols[i:i + BATCH]
        try:
            data = fmp_get(f"{BASE_V3}/profile/{','.join(chunk)}")
            if isinstance(data, list):
                for row in data:
                    sym = row.get("symbol", "").upper()
                    if sym:
                        profiles[sym] = row
        except Exception as e:
            print(f"  profile batch {i//BATCH+1} failed: {e}")
        if i > 0 and i % 500 == 0:
            print(f"  profiles: {i}/{total}...")
        time.sleep(0.15)
    return profiles


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


def main():
    print("Loading ticker universe...\n")

    # Step 1: Index constituents
    print("Step 1: Fetching index constituents...")
    print("  trying FMP v4 ETF holdings...")
    sp500  = fetch_etf_holdings("SPY")
    ndx    = fetch_etf_holdings("QQQ")
    dow    = fetch_etf_holdings("DIA")
    sp400  = fetch_etf_holdings("MDY")
    sp600  = fetch_etf_holdings("IJR")

    if len(sp500) < 400:
        print("  SPY sparse — trying Wikipedia S&P 500...")
        sp500 = fetch_sp500_wikipedia()
    if len(ndx) < 90:
        print("  QQQ sparse — trying Wikipedia NDX...")
        ndx = fetch_ndx_wikipedia()
    if len(dow) < 25:
        print("  DIA sparse — using hardcoded Dow 30...")
        dow = DOW30

    universe = sp500 | ndx | dow | sp400 | sp600
    if len(universe) < 200:
        print("  All ETF methods sparse — building from Wikipedia only...")
        sp500 = fetch_sp500_wikipedia()
        ndx   = fetch_ndx_wikipedia()
        dow   = DOW30
        universe = sp500 | ndx | dow
        sp400, sp600 = set(), set()

    print(f"\nUnion universe: {len(universe)} unique symbols")

    # Step 2: Profiles
    print("\nStep 2: Fetching company profiles...")
    profiles = fetch_profiles_batch(sorted(universe))
    print(f"  Got {len(profiles)} / {len(universe)} profiles")

    # Step 3: Filter
    print(f"\nStep 3: Applying ${MIN_MARKET_CAP/1e6:.0f}M market cap filter...")
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
        mcap = p.get("mktCap") or 0
        if mcap < MIN_MARKET_CAP:
            skipped["cap"] += 1
            continue
        rows.append({
            "ticker":        sym,
            "name":          (p.get("companyName") or "")[:255],
            "exchange":      (p.get("exchangeShortName") or "")[:20],
            "sector":        p.get("sector") or None,
            "industry":      p.get("industry") or None,
            "country":       (p.get("country") or "US")[:40],
            "currency":      (p.get("currency") or "USD")[:10],
            "in_sp500":      sym in sp500,
            "in_sp400":      sym in sp400,
            "in_sp600":      sym in sp600,
            "in_nasdaq100":  sym in ndx,
            "in_dow30":      sym in dow,
            "market_cap_usd": float(mcap),
        })
    print(f"  Kept {len(rows)} | skipped: cap={skipped['cap']}, "
          f"inactive={skipped['inactive']}, no_profile={skipped['no_profile']}")

    # Step 4: Upsert
    print(f"\nStep 4: Upserting to database...")
    n = upsert_tickers(rows)
    print(f"  Upserted {n} tickers.")

    # Summary
    with engine.connect() as con:
        counts = con.execute(text("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE in_sp500) AS sp500,
                   COUNT(*) FILTER (WHERE in_nasdaq100) AS ndx,
                   COUNT(*) FILTER (WHERE in_dow30) AS dow
            FROM tickers WHERE is_active
        """)).fetchone()
        by_sector = pd.read_sql(
            text("SELECT sector, COUNT(*) n FROM tickers WHERE is_active AND sector IS NOT NULL "
                 "GROUP BY sector ORDER BY n DESC LIMIT 10"),
            con,
        )
    print(f"\nFinal: {counts[0]} tickers | {counts[1]} S&P500 | {counts[2]} NDX | {counts[3]} Dow")
    print("\nTop sectors:")
    print(by_sector.to_string(index=False))


if __name__ == "__main__":
    main()
