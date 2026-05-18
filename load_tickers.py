"""
load_tickers.py — Loads the full ticker universe for Premium FMP accounts.

US indices (via stable constituent endpoints):
    S&P 500, NASDAQ 100, Dow 30

European indices (via stable constituent endpoints, Premium only):
    FTSE 100, DAX 40, CAC 40, AEX, IBEX 35, FTSE MIB, SMI

Filtered to $500M+ market cap.

Environment:
    FMP_API_KEY    — FMP Premium API key
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
                raise RuntimeError(f"Not found: {path}")
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
    raise RuntimeError(f"FMP failed: {path}")


def fetch_constituent(endpoint, symbol_key="symbol"):
    """Fetch index constituents. Returns set of ticker symbols."""
    try:
        data = fmp_get(endpoint)
        if data and isinstance(data, list) and len(data) > 5:
            syms = {str(row.get(symbol_key, "")).upper()
                    for row in data if row.get(symbol_key)}
            print(f"    {endpoint}: {len(syms)} symbols")
            return syms
    except Exception as e:
        print(f"    {endpoint} failed: {e}")
    return set()


def get_market_cap(p):
    for key in ("marketCap", "mktCap", "market_cap", "MarketCap"):
        v = p.get(key)
        if v and float(v) > 0:
            return float(v)
    return 0.0


def fetch_profile(symbol):
    try:
        data = fmp_get("profile", params={"symbol": symbol})
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict) and data.get("symbol"):
            return data
    except Exception:
        pass
    return None


def fetch_profiles(symbols):
    profiles = {}
    symbols = sorted(symbols)
    total = len(symbols)
    print(f"  Fetching {total} profiles (~{total*0.15/60:.1f} min)...")
    for i, sym in enumerate(symbols, 1):
        p = fetch_profile(sym)
        if p:
            profiles[sym] = p
        if i % 100 == 0:
            print(f"  {i}/{total} ({len(profiles)} valid)...")
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
                    in_ftse100, in_ftse250, in_dax40, in_cac40,
                    in_aex, in_ibex35, in_mib40, in_smi,
                    market_cap_usd, is_active, last_updated
                ) VALUES (
                    :ticker, :name, :exchange, :sector, :industry, :country, :currency,
                    :in_sp500, :in_sp400, :in_sp600, :in_nasdaq100, :in_dow30,
                    :in_ftse100, :in_ftse250, :in_dax40, :in_cac40,
                    :in_aex, :in_ibex35, :in_mib40, :in_smi,
                    :market_cap_usd, TRUE, NOW()
                )
                ON CONFLICT (ticker) DO UPDATE SET
                    name=EXCLUDED.name, exchange=EXCLUDED.exchange,
                    sector=EXCLUDED.sector, industry=EXCLUDED.industry,
                    country=EXCLUDED.country, currency=EXCLUDED.currency,
                    in_sp500=EXCLUDED.in_sp500, in_sp400=EXCLUDED.in_sp400,
                    in_sp600=EXCLUDED.in_sp600, in_nasdaq100=EXCLUDED.in_nasdaq100,
                    in_dow30=EXCLUDED.in_dow30,
                    in_ftse100=EXCLUDED.in_ftse100, in_ftse250=EXCLUDED.in_ftse250,
                    in_dax40=EXCLUDED.in_dax40, in_cac40=EXCLUDED.in_cac40,
                    in_aex=EXCLUDED.in_aex, in_ibex35=EXCLUDED.in_ibex35,
                    in_mib40=EXCLUDED.in_mib40, in_smi=EXCLUDED.in_smi,
                    market_cap_usd=EXCLUDED.market_cap_usd,
                    is_active=TRUE, last_updated=NOW()
            """), r)
    return len(rows)


def main():
    print("=" * 60)
    print("Loading ticker universe (FMP Premium)")
    print("=" * 60)

    # ── US indices ──────────────────────────────────────────────
    print("\nUS indices:")
    sp500 = fetch_constituent("sp500-constituent")
    ndx   = fetch_constituent("nasdaq-constituent")
    dow   = fetch_constituent("dowjones-constituent")

    if not dow:
        dow = {
            "AAPL","AMGN","AXP","BA","CAT","CRM","CSCO","CVX","DIS","DOW",
            "GS","HD","HON","IBM","INTC","JNJ","JPM","KO","MCD","MMM",
            "MRK","MSFT","NKE","PG","TRV","UNH","V","VZ","WBA","WMT",
        }

    # ── European indices (Premium) ──────────────────────────────
    print("\nEuropean indices (Premium):")
    ftse100 = fetch_constituent("ftse-constituent")
    ftse250 = fetch_constituent("ftse250-constituent")
    dax40   = fetch_constituent("dax-constituent")
    cac40   = fetch_constituent("cac-constituent")
    aex     = fetch_constituent("aex-constituent")
    ibex35  = fetch_constituent("ibex-constituent")
    mib40   = fetch_constituent("euronext-constituent")   # FTSE MIB proxy
    smi     = fetch_constituent("smi-constituent")

    # Union
    universe = sp500 | ndx | dow | ftse100 | ftse250 | dax40 | cac40 | aex | ibex35 | mib40 | smi
    print(f"\nTotal universe: {len(universe)} symbols")
    print(f"  US: SP500={len(sp500)} NDX={len(ndx)} DOW={len(dow)}")
    print(f"  EU: FTSE100={len(ftse100)} FTSE250={len(ftse250)} DAX={len(dax40)} "
          f"CAC={len(cac40)} AEX={len(aex)} IBEX={len(ibex35)} SMI={len(smi)}")

    # ── Profiles ────────────────────────────────────────────────
    print("\nFetching company profiles...")
    profiles = fetch_profiles(universe)
    print(f"  Got {len(profiles)} / {len(universe)} profiles")

    # ── Filter & build rows ─────────────────────────────────────
    print(f"\nFiltering to ${MIN_MARKET_CAP/1e6:.0f}M+ market cap...")
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
            "ticker":       sym,
            "name":         (p.get("companyName") or p.get("name") or "")[:255],
            "exchange":     (p.get("exchangeShortName") or p.get("exchange") or "")[:20],
            "sector":       p.get("sector") or None,
            "industry":     p.get("industry") or None,
            "country":      (p.get("country") or "US")[:40],
            "currency":     (p.get("currency") or "USD")[:10],
            "in_sp500":     sym in sp500,
            "in_sp400":     False,
            "in_sp600":     False,
            "in_nasdaq100": sym in ndx,
            "in_dow30":     sym in dow,
            "in_ftse100":   sym in ftse100,
            "in_ftse250":   sym in ftse250,
            "in_dax40":     sym in dax40,
            "in_cac40":     sym in cac40,
            "in_aex":       sym in aex,
            "in_ibex35":    sym in ibex35,
            "in_mib40":     sym in mib40,
            "in_smi":       sym in smi,
            "market_cap_usd": mcap,
        })

    print(f"  Kept {len(rows)} | "
          f"cap={skipped['cap']} inactive={skipped['inactive']} "
          f"no_profile={skipped['no_profile']}")

    # ── Upsert ──────────────────────────────────────────────────
    # Schema needs EU columns — add them if missing
    with engine.begin() as con:
        for col in ["in_ftse100","in_ftse250","in_dax40","in_cac40",
                    "in_aex","in_ibex35","in_mib40","in_smi"]:
            try:
                con.execute(text(
                    f"ALTER TABLE tickers ADD COLUMN IF NOT EXISTS "
                    f"{col} BOOLEAN DEFAULT FALSE"
                ))
            except Exception:
                pass

    print(f"\nUpserting {len(rows)} tickers...")
    n = upsert_tickers(rows)
    print(f"  Done. {n} rows.")

    # ── Summary ─────────────────────────────────────────────────
    with engine.connect() as con:
        counts = con.execute(text("""
            SELECT COUNT(*) total,
                COUNT(*) FILTER (WHERE in_sp500)    sp500,
                COUNT(*) FILTER (WHERE in_nasdaq100) ndx,
                COUNT(*) FILTER (WHERE in_ftse100)  ftse100,
                COUNT(*) FILTER (WHERE in_dax40)    dax,
                COUNT(*) FILTER (WHERE in_cac40)    cac,
                COUNT(*) FILTER (WHERE country != 'US' AND country IS NOT NULL) eu_count
            FROM tickers WHERE is_active
        """)).fetchone()
        top_sectors = pd.read_sql(
            text("SELECT sector, COUNT(*) n FROM tickers "
                 "WHERE is_active AND sector IS NOT NULL "
                 "GROUP BY sector ORDER BY n DESC LIMIT 12"),
            con,
        )

    print(f"\n{'='*60}")
    print(f"FINAL: {counts[0]} tickers")
    print(f"  US:  SP500={counts[1]} | NDX={counts[2]}")
    print(f"  EU:  FTSE100={counts[3]} | DAX={counts[4]} | CAC={counts[5]}")
    print(f"  Non-US total: {counts[6]}")
    print(f"\nTop sectors:\n{top_sectors.to_string(index=False)}")


if __name__ == "__main__":
    main()
