"""
load_eu_tickers.py — Loads European index constituents.

FMP's stable API doesn't currently return EU constituent lists, so we build
the universe from three sources in priority order:
  1. Hardcoded current constituents (reliable, needs quarterly update)
  2. Wikipedia scrape (usually works, format changes occasionally)
  3. FMP profile lookup for any symbol we already know

Indices covered:
    FTSE 100  (UK)
    DAX 40    (Germany)
    CAC 40    (France)
    AEX 25    (Netherlands)
    IBEX 35   (Spain)
    SMI 20    (Switzerland)
    FTSE MIB 40 (Italy)
    STOXX 50  (Pan-European)

Run: python load_eu_tickers.py

Environment:
    FMP_API_KEY    — FMP Premium (needed for profile data)
    DATABASE_URL   — Supabase connection string
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


# =============================================================================
# Hardcoded current constituents (as of May 2026)
# FMP uses the local exchange ticker format for European stocks
# Update these quarterly via Wikipedia
# =============================================================================

# FTSE 100 — London Stock Exchange tickers (without .L suffix for FMP)
FTSE100 = {
    "AAL","ABF","ADM","AHT","ANTO","AUTO","AV","AZN","BA","BAB","BARC","BATS",
    "BHAG","BKG","BLND","BNS","BP","BRBY","BT","CCH","CEY","CNA","CPG","CRDA",
    "CRH","CTEC","DCC","DGE","DPLM","EDV","ELN","ENT","EXPN","EZJ","FCH","FCIT",
    "FLTR","FRAS","FRES","GLEN","GSK","HIK","HL","HLMA","HLN","HSBA","IAG","ICG",
    "ICP","IHG","III","IMB","INF","ITRK","ITV","JD","JMAT","KGF","LAND","LGEN",
    "LLOY","LMP","LSEG","MKS","MNDI","MNG","MRO","NG","NWG","NXT","OCDO","PHNX",
    "PRU","PSH","PSN","PSON","REL","RIO","RKT","RMV","RR","RS1","RTO","SBRY",
    "SDR","SGE","SGRO","SHEL","SKG","SMDS","SMIN","SMT","SN","SPX","SSE","STAN",
    "STJ","SVT","TSCO","TW","ULVR","UU","VOD","WEIR","WPP","WTB",
}

# DAX 40 — Frankfurt Stock Exchange
DAX40 = {
    "ADS","AIR","ALV","BAS","BAYN","BEI","BMW","BNR","CON","DB1","DBK","DHL",
    "DTE","DTG","EOAN","EVD","FRE","FME","HEI","HEN3","HOT","IFX","LIN","MBG",
    "MEO","MRK","MTX","MUV2","NDA","P911","PAH3","PLT","PUMA","QIA","RHM",
    "RWE","SAP","SHL","SIE","SIM","SRT3","SY1","SZG","TKA","VNA","VOW3",
    "WCH","ZAL","1COV",
}

# CAC 40 — Euronext Paris
CAC40 = {
    "AC","ACA","AF","AI","AIR","AKE","ALO","BN","BNP","CA","CAP","CS","DSY",
    "DG","ENGI","EL","ERF","FR","GLE","HO","ILD","KER","LHN","LR","MC","ML",
    "MT","ORA","PUB","RI","RMS","RNO","SAF","SAND","SGO","SU","SW","TTE","URW","VIE","VIV",
}

# AEX 25 — Amsterdam
AEX25 = {
    "ADYEN","AGN","AH","AKZA","ASM","ASML","AVP","BESI","EXOR","HEIA","IMCD",
    "INGA","KPN","NN","OCI","PHIA","PRX","RAND","REN","SBMO","SIF","UMG","UNA",
    "URW","WKL",
}

# IBEX 35 — Madrid
IBEX35 = {
    "ACS","ACX","AENA","AMS","ANA","BBVA","BKT","CABK","CIE","COL","ELE","ENG",
    "FDR","FER","GRF","IAG","IBE","IDR","INDRA","ITX","MAP","MEL","MRL","MTS",
    "NTGY","PHM","RED","REP","ROVI","SAB","SAN","SCYR","SGRE","SOL","TEF",
}

# SMI 20 — Switzerland
SMI20 = {
    "ABBN","ADEN","ALC","BALN","CFR","CSGN","GEBN","GIVN","HOLN","KUHN",
    "LONN","LHN","NESN","NOVN","PGHN","ROCG","ROG","SGSN","SLHN","SREN",
    "UBSG","UHR","ZURN",
}

# FTSE MIB 40 — Italy (Borsa Italiana)
FTSEMIB = {
    "A2A","AMP","ATL","BMPS","BPE","BPER","CPR","DIA","ENI","ENEL","ERG",
    "FCA","FCST","FI","G","GEO","HER","INW","ISP","IVG","LDO","MB","MONC",
    "NEXI","PIRC","POST","PRY","PST","REC","RACE","SRG","STM","TEN","TIT",
    "TRN","UCG","UNI","WBD",
}

# EURO STOXX 50 (pan-European, many overlap with above)
STOXX50 = {
    "ADYEN","AI","AIR","ALV","AMP","ASML","AXA","BAS","BAYN","BMW","BNP",
    "CRH","CS","DB1","DG","DTE","ENI","ENEL","ENGI","FRE","INGA","ITX",
    "KER","LIN","MC","MBG","MRK","MT","MUV2","NESN","NOVN","ORA","PHIA",
    "PRX","RMS","ROG","SAF","SAN","SAP","SHL","SIE","SU","TEF","TTE",
    "UCG","UNA","VOD","VOW3","WKL",
}

ALL_EU = {
    "FTSE100": FTSE100,
    "DAX40":   DAX40,
    "CAC40":   CAC40,
    "AEX25":   AEX25,
    "IBEX35":  IBEX35,
    "SMI20":   SMI20,
    "FTSEMIB": FTSEMIB,
    "STOXX50": STOXX50,
}


# =============================================================================
# Wikipedia fallback
# =============================================================================
def try_wikipedia(url, symbol_col_hint):
    try:
        tables = pd.read_html(url)
        for t in tables:
            for col in t.columns:
                if any(h in str(col).lower() for h in
                       ["ticker", "symbol", "epic", "isin", "code"]):
                    syms = set(t[col].dropna().astype(str).str.upper().str.strip())
                    syms = {s for s in syms if 1 < len(s) <= 8}
                    if len(syms) > 10:
                        return syms
    except Exception as e:
        print(f"    Wikipedia {url[:50]}... failed: {e}")
    return set()


# =============================================================================
# FMP profile lookup
# =============================================================================
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
                    raise RuntimeError(data["Error Message"][:100])
                return data
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            if attempt == 2:
                raise
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


# =============================================================================
# Ensure EU columns exist in tickers table
# =============================================================================
def ensure_eu_columns():
    cols = ["in_ftse100","in_ftse250","in_dax40","in_cac40",
            "in_aex","in_ibex35","in_mib40","in_smi","in_stoxx50"]
    with engine.begin() as con:
        for col in cols:
            con.execute(text(
                f"ALTER TABLE tickers ADD COLUMN IF NOT EXISTS "
                f"{col} BOOLEAN DEFAULT FALSE"
            ))


def upsert_eu_ticker(row):
    with engine.begin() as con:
        con.execute(text("""
            INSERT INTO tickers (
                ticker, name, exchange, sector, industry, country, currency,
                in_sp500, in_sp400, in_sp600, in_nasdaq100, in_dow30,
                in_ftse100, in_ftse250, in_dax40, in_cac40,
                in_aex, in_ibex35, in_mib40, in_smi, in_stoxx50,
                market_cap_usd, is_active, last_updated
            ) VALUES (
                :ticker, :name, :exchange, :sector, :industry, :country, :currency,
                FALSE, FALSE, FALSE, FALSE, FALSE,
                :in_ftse100, :in_ftse250, :in_dax40, :in_cac40,
                :in_aex, :in_ibex35, :in_mib40, :in_smi, :in_stoxx50,
                :market_cap_usd, TRUE, NOW()
            )
            ON CONFLICT (ticker) DO UPDATE SET
                name=EXCLUDED.name, exchange=EXCLUDED.exchange,
                sector=EXCLUDED.sector, industry=EXCLUDED.industry,
                country=EXCLUDED.country, currency=EXCLUDED.currency,
                in_ftse100=EXCLUDED.in_ftse100, in_dax40=EXCLUDED.in_dax40,
                in_cac40=EXCLUDED.in_cac40, in_aex=EXCLUDED.in_aex,
                in_ibex35=EXCLUDED.in_ibex35, in_mib40=EXCLUDED.in_mib40,
                in_smi=EXCLUDED.in_smi, in_stoxx50=EXCLUDED.in_stoxx50,
                market_cap_usd=EXCLUDED.market_cap_usd,
                is_active=TRUE, last_updated=NOW()
        """), row)


# =============================================================================
# Main
# =============================================================================
def main():
    print("=" * 60)
    print("Loading EU ticker universe")
    print("=" * 60)

    ensure_eu_columns()

    # Build universe from hardcoded lists
    universe = set()
    for index_name, syms in ALL_EU.items():
        universe |= syms
        print(f"  {index_name}: {len(syms)} symbols")

    print(f"\nTotal EU symbols: {len(universe)}")

    # Fetch profiles from FMP
    print(f"\nFetching FMP profiles (~{len(universe)*0.15/60:.1f} min)...")
    profiles = {}
    for i, sym in enumerate(sorted(universe), 1):
        p = fetch_profile(sym)
        if p:
            profiles[sym] = p
        if i % 50 == 0:
            print(f"  {i}/{len(universe)} ({len(profiles)} valid)...")
        time.sleep(0.15)

    print(f"  Got {len(profiles)} profiles")

    # Build rows
    n_ok = n_skip = 0
    for sym in sorted(universe):
        p = profiles.get(sym)
        if not p:
            # Still insert with minimal data if we know it's in an index
            mcap = 0.0
        else:
            if p.get("isActivelyTrading") is False:
                n_skip += 1
                continue
            mcap = get_market_cap(p)
            if mcap > 0 and mcap < MIN_MARKET_CAP:
                n_skip += 1
                continue

        row = {
            "ticker":       sym,
            "name":         (p.get("companyName") or sym)[:255] if p else sym,
            "exchange":     (p.get("exchangeShortName") or "")[:20] if p else "",
            "sector":       (p.get("sector") or None) if p else None,
            "industry":     (p.get("industry") or None) if p else None,
            "country":      (p.get("country") or "EU")[:40] if p else "EU",
            "currency":     (p.get("currency") or "EUR")[:10] if p else "EUR",
            "in_ftse100":   sym in FTSE100,
            "in_ftse250":   False,
            "in_dax40":     sym in DAX40,
            "in_cac40":     sym in CAC40,
            "in_aex":       sym in AEX25,
            "in_ibex35":    sym in IBEX35,
            "in_mib40":     sym in FTSEMIB,
            "in_smi":       sym in SMI20,
            "in_stoxx50":   sym in STOXX50,
            "market_cap_usd": float(mcap),
        }
        try:
            upsert_eu_ticker(row)
            n_ok += 1
        except Exception as e:
            print(f"  {sym}: upsert failed — {e}")

    print(f"\n  Upserted {n_ok} EU tickers ({n_skip} skipped)")

    # Summary
    with engine.connect() as con:
        counts = con.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE in_ftse100) ftse100,
                COUNT(*) FILTER (WHERE in_dax40)   dax,
                COUNT(*) FILTER (WHERE in_cac40)   cac,
                COUNT(*) FILTER (WHERE in_aex)     aex,
                COUNT(*) FILTER (WHERE in_ibex35)  ibex,
                COUNT(*) FILTER (WHERE in_smi)     smi,
                COUNT(*) FILTER (WHERE country != 'US' AND country IS NOT NULL) total_eu
            FROM tickers WHERE is_active
        """)).fetchone()
    print(f"\nEU in DB: FTSE100={counts[0]} DAX={counts[1]} CAC={counts[2]} "
          f"AEX={counts[3]} IBEX={counts[4]} SMI={counts[5]}")
    print(f"Total non-US tickers: {counts[6]}")


if __name__ == "__main__":
    main()
