"""
load_extra_tickers.py — Loads ~120 popular mid-cap names not in S&P 500/NDX/Dow.

These are the names retail investors and finance Twitter ask about most often,
filtered by current market cap > $5B and active trading.

Run: python load_extra_tickers.py

Update the EXTRA_TICKERS list as users request additions.
"""
import os
import sys
import time
import requests
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Import the comprehensive AI universe
try:
    from ai_universe import AI_UNIVERSE as _AI_TICKERS
except ImportError:
    _AI_TICKERS = set()
    print("WARNING: ai_universe.py not found — AI names won't be loaded")

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
MIN_MARKET_CAP = 500_000_000   # $500M — lower for curated AI names like AAOI


# Popular mid-cap and large-cap names not in S&P 500 / NDX / Dow
# Curated based on retail trading volume, finance media mentions, and search trends.
EXTRA_TICKERS = {
    # SaaS / Cloud
    "HUBS","NET","DDOG","SNOW","CRWD","ZS","OKTA","NOW","TEAM","DOCU",
    "ZM","TWLO","COUP","WDAY","PANW","FTNT","S","CYBR","RNG","BILL",
    "BL","ESTC","FROG","GTLB","CFLT","MDB","HCP","PD","ZI","SMAR",
    "ASAN","MNDY","WORK","NEWR","DT","SUMO",

    # Fintech
    "SOFI","AFRM","UPST","HOOD","COIN","SQ","BLK","SCHW","ICE",
    "FIS","FISV","MA","V","NVCR","LMND","ROOT","HIPO",

    # Consumer tech / disruptors
    "SHOP","MELI","SE","CPNG","BABA","JD","PDD","NIO","XPEV","LI",
    "RIVN","LCID","BYND","DOCN","FVRR","ETSY","CHWY","WAYFAIR","W",
    "DASH","UBER","LYFT","ABNB","BKNG",

    # Biotech / Healthcare
    "MRNA","BNTX","CRSP","NTLA","EDIT","BEAM","VRTX","REGN","ALNY",
    "ILMN","DXCM","TDOC","HIMS","PGY",

    # Semis (not in NDX)
    "ARM","ON","WOLF","AMBA","SMCI","AVGO","ADI","KLAC","LRCX","ASML",
    "TSM","UMC","ASX","ASM","MRVL","MCHP","MPWR","SWKS",

    # Energy / Utilities (popular)
    "FCEL","PLUG","BLDP","NEE","ENPH","SEDG","RUN","CWEN","NOVA",

    # Cyclicals / Industrials
    "CARR","OTIS","FAST","PAYX","ROL","CHRW","XPO","ODFL","SAIA",
    "JBHT","KNX",

    # Defense / Aerospace
    "RTX","NOC","LMT","GD","HII","BWXT","KTOS","LDOS","TDY",

    # Real estate / REITs popular
    "O","PLD","CCI","AMT","SPG","EQR","EXR","PSA","DLR","EQIX",
    "ARE","VTR","WELL","STAG","NNN","ESS",

    # Media / Comm
    "ROKU","FUBO","SPOT","WBD","PARA","FOX","FOXA","NWSA","CHTR","CMCSA",

    # Consumer brands
    "LULU","NKE","UAA","UA","DECK","ONON","CROX","DKS","BBY","TGT",
    "DG","DLTR","FIVE","OLLI","FND",

    # Crypto / blockchain
    "MSTR","MARA","RIOT","HUT","CLSK","CIFR","BITF","HIVE","BTBT",

    # AI Optical / Photonics / Connectivity (critical AI infra)
    "AAOI","LITE","COHR","CIEN","CRDO","VIAV","MTSI","IIVI",

    # AI Memory + Foundry
    "MU","WDC","GFS",

    # AI Edge + Robotics
    "AMBA","PI","TER",

    # Data center cooling
    "CARR",

    # AI platforms not already listed
    "PLTR","PATH","AI","DOCS","VEEV","PAYC",

    # Enterprise hardware for AI
    "DELL","HPE","NTAP","PSTG","NTNX",

    # Networking for AI
    "ANET","JNPR",

    # Additional semis
    "GFS","WOLF",

}

# Merge in AI universe names so they always get loaded
EXTRA_TICKERS.update(_AI_TICKERS)
print(f"  AI universe added: {len(_AI_TICKERS)} names")

# Subtract anything already in major US indices to avoid double-counting
ALREADY_IN_INDEX = set()  # populated from DB lookup below


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


def get_market_cap(p):
    for key in ("marketCap", "mktCap", "market_cap"):
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


def main():
    print("=" * 60)
    print("Loading extra mid-cap tickers")
    print("=" * 60)

    # Get tickers already in DB to avoid wasting profile calls
    with engine.connect() as con:
        existing = {r[0] for r in con.execute(text(
            "SELECT ticker FROM tickers WHERE is_active = TRUE"
        )).fetchall()}

    to_load = sorted(EXTRA_TICKERS - existing)
    print(f"\n{len(EXTRA_TICKERS)} candidates total")
    print(f"{len(EXTRA_TICKERS & existing)} already in DB (skipping)")
    print(f"{len(to_load)} new to load\n")

    if not to_load:
        print("Nothing new to load.")
        return

    print(f"Fetching profiles for {len(to_load)} tickers (~{len(to_load)*0.15/60:.1f} min)...")
    profiles = {}
    for i, sym in enumerate(to_load, 1):
        p = fetch_profile(sym)
        if p:
            profiles[sym] = p
        if i % 25 == 0:
            print(f"  {i}/{len(to_load)} ({len(profiles)} valid)", flush=True)
        time.sleep(0.15)

    print(f"Got {len(profiles)} profiles")

    # Filter and upsert
    print(f"\nFiltering to ${MIN_MARKET_CAP/1e9:.0f}B+ market cap...")
    n_ok = n_skip = 0

    with engine.begin() as con:
        for sym in sorted(to_load):
            p = profiles.get(sym)
            if not p:
                n_skip += 1
                continue
            if p.get("isActivelyTrading") is False:
                n_skip += 1
                continue
            mcap = get_market_cap(p)
            if mcap < MIN_MARKET_CAP:
                n_skip += 1
                continue

            con.execute(text("""
                INSERT INTO tickers (
                    ticker, name, exchange, sector, industry, country, currency,
                    in_sp500, in_sp400, in_sp600, in_nasdaq100, in_dow30,
                    market_cap_usd, is_active, last_updated
                ) VALUES (
                    :ticker, :name, :exchange, :sector, :industry, :country, :currency,
                    FALSE, FALSE, FALSE, FALSE, FALSE,
                    :market_cap_usd, TRUE, NOW()
                )
                ON CONFLICT (ticker) DO UPDATE SET
                    name=EXCLUDED.name, exchange=EXCLUDED.exchange,
                    sector=EXCLUDED.sector, industry=EXCLUDED.industry,
                    country=EXCLUDED.country, currency=EXCLUDED.currency,
                    market_cap_usd=EXCLUDED.market_cap_usd,
                    is_active=TRUE, last_updated=NOW()
            """), {
                "ticker": sym,
                "name": (p.get("companyName") or "")[:255],
                "exchange": (p.get("exchangeShortName") or "")[:20],
                "sector": p.get("sector"),
                "industry": p.get("industry"),
                "country": (p.get("country") or "US")[:40],
                "currency": (p.get("currency") or "USD")[:10],
                "market_cap_usd": mcap,
            })
            n_ok += 1

    print(f"\n  Loaded {n_ok} tickers ({n_skip} skipped)")

    # Print summary by sector
    with engine.connect() as con:
        summary = pd.read_sql(text("""
            SELECT sector, COUNT(*) n
            FROM tickers
            WHERE is_active AND ticker IN :syms
            GROUP BY sector ORDER BY n DESC
        """), con, params={"syms": tuple(to_load) if to_load else ("__none__",)})

    print("\nNew tickers by sector:")
    print(summary.to_string(index=False))
    print(f"\nNext step: run refresh_data.py --incremental to fetch their price history")


if __name__ == "__main__":
    main()
