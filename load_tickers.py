"""
load_tickers.py — One-time loader for the ticker universe.

Uses FMP's current stable API (post-Aug 2025) + Wikipedia fallbacks.
Legacy v3 batch endpoints are retired; we use stable/ single-symbol calls.

Strategy:
  Index membership: ETF holdings (SPY/QQQ/DIA/MDY/IJR) via stable/etf-holder
  Company profiles: stable/profile?symbol=X (one call per ticker, rate-paced)
  Fallback for index lists: Wikipedia (no API needed)

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


def fmp_get(path, params=None, base=STABLE):
    """GET from FMP. Returns parsed JSON or raises."""
    params = params or {}
    params["apikey"] = FMP_KEY
    url = f"{base}/{path}"
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "Error Message" in data:
                    msg = data["Error Message"]
                    if "Legacy" in msg or "legacy" in msg:
                        raise RuntimeError(f"Legacy endpoint: {path}")
                    raise RuntimeError(f"FMP error on {path}: {msg[:150]}")
                return data
            if r.status_code == 429:
                wait = 2 ** attempt
                print(f"    rate-limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            if r.status_code in (401, 403):
                raise RuntimeError(f"FMP auth {r.status_code} on {path}")
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"FMP failed after retries: {path}")


# ---------------------------------------------------------------------------
# Index constituents via ETF holders
# ---------------------------------------------------------------------------
def fetch_etf_symbols(etf_symbol):
    """stable/etf-holder returns list of {asset, ...}."""
    try:
        data = fmp_get("etf-holder", params={"symbol": etf_symbol})
        if not data or not isinstance(data, list):
            return set()
        syms = {row.get("asset", "").upper() for row in data
                if row.get("asset") and len(row.get("asset", "")) <= 6}
        print(f"    {etf_symbol}: {len(syms)} holdings")
        return syms
    except Exception as e:
        print(f"    {etf_symbol} failed: {e}")
        return set()


# ---------------------------------------------------------------------------
# Wikipedia fallbacks (zero API calls)
# ---------------------------------------------------------------------------
def wiki_sp500():
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            attrs={"id": "constituents"},
        )
        df = tables[0]
        col = next((c for c in df.columns if "symbol" in c.lower()), df.columns[0])
        syms = set(df[col].str.upper().str.replace(".", "-", regex=False))
        print(f"    Wikipedia S&P500: {len(syms)}")
        return syms
    except Exception as e:
        print(f"    Wikipedia S&P500 failed: {e}")
        return set()


def wiki_ndx():
    try:
        # Try multiple table structures Wikipedia uses
        tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
        for t in tables:
            for col in t.columns:
                if "ticker" in str(col).lower() or "symbol" in str(col).lower():
                    syms = set(t[col].dropna().astype(str).str.upper().str.strip())
                    syms = {s for s in syms if 1 < len(s) <= 6 and s.isalpha()}
                    if len(syms) > 80:
                        print(f"    Wikipedia NDX: {len(syms)}")
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


# ---------------------------------------------------------------------------
# Profile fetching — one ticker at a time via stable/profile
# ---------------------------------------------------------------------------
def fetch_profile(symbol):
    """Fetch a single ticker's profile. Returns dict or None."""
    try:
        data = fmp_get("profile", params={"symbol": symbol})
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict) and "symbol" in data:
            return data
        return None
    except Exception:
        return None


def fetch_profiles(symbols, sample_size=None):
    """Fetch profiles for all symbols. If sample_size set, only fetch that many
    (for testing). Returns dict of symbol -> profile."""
    symbols = sorted(symbols)
    if sample_size:
        symbols = symbols[:sample_size]

    profiles = {}
    total = len(symbols)
    print(f"  Fetching {total} profiles (this takes ~{total*0.15/60:.1f} min)...")

    for i, sym in enumerate(symbols, 1):
        p = fetch_profile(sym)
        if p:
            profiles[sym] = p
        if i % 100 == 0:
            print(f"  {i}/{total} profiles fetched ({len(profiles)} valid)...")
        time.sleep(0.15)  # ~6-7 req/sec, well within rate limits

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

    # Step 1: Build index membership sets
    print("\nStep 1: Fetching index constituents...")

    print("  ETF holder approach (stable/etf-holder):")
    sp500 = fetch_etf_symbols("SPY")
    ndx   = fetch_etf_symbols("QQQ")
    dow   = fetch_etf_symbols("DIA")
    sp400 = fetch_etf_symbols("MDY")
    sp600 = fetch_etf_symbols("IJR")

    # Fallback to Wikipedia if ETF holdings too sparse
    if len(sp500) < 400:
        print("  SPY sparse — Wikipedia fallback...")
        sp500 = wiki_sp500()

    if len(ndx) < 90:
        print("  QQQ sparse — Wikipedia fallback...")
        ndx = wiki_ndx()

    if len(dow) < 25:
        print("  DIA sparse — using hardcoded Dow 30...")
        dow = DOW30

    universe = sp500 | ndx | dow | sp400 | sp600

    # Last resort: if everything failed, use Wikipedia + hardcoded Dow
    if len(universe) < 200:
        print("  All ETF methods failed — building from Wikipedia only...")
        sp500 = wiki_sp500()
        ndx   = wiki_ndx()
        dow   = DOW30
        universe = sp500 | ndx | dow
        sp400, sp600 = set(), set()

    print(f"\n  Universe: {len(universe)} unique symbols "
          f"(SP500={len(sp500)}, NDX={len(ndx)}, DOW={len(dow)}, "
          f"SP400={len(sp400)}, SP600={len(sp600)})")

    # Step 2: Fetch profiles (one per symbol)
    print("\nStep 2: Fetching company profiles (stable/profile)...")
    profiles = fetch_profiles(universe)
    print(f"  Got {len(profiles)} / {len(universe)} profiles")

    # Step 3: Filter and build rows
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
                   COUNT(*) FILTER (WHERE in_sp500)    AS sp500,
                   COUNT(*) FILTER (WHERE in_nasdaq100) AS ndx,
                   COUNT(*) FILTER (WHERE in_dow30)    AS dow,
                   COUNT(*) FILTER (WHERE in_sp400)    AS sp400,
                   COUNT(*) FILTER (WHERE in_sp600)    AS sp600
            FROM tickers WHERE is_active
        """)).fetchone()
        top_sectors = pd.read_sql(
            text("SELECT sector, COUNT(*) n FROM tickers "
                 "WHERE is_active AND sector IS NOT NULL "
                 "GROUP BY sector ORDER BY n DESC LIMIT 10"),
            con,
        )

    print(f"\n{'='*60}")
    print(f"FINAL: {counts[0]} tickers")
    print(f"  S&P 500: {counts[1]} | NDX: {counts[2]} | Dow: {counts[3]}")
    print(f"  S&P 400: {counts[4]} | S&P 600: {counts[5]}")
    print(f"\nTop sectors:")
    print(top_sectors.to_string(index=False))


if __name__ == "__main__":
    main()
