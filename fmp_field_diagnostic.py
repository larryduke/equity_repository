"""
fmp_field_diagnostic.py — Discovers actual field names in FMP responses.

We've had multiple bugs where FMP returns data but with field names different
from what our code reads. This script systematically tests every endpoint we
need and prints the EXACT response shape + field names for AAPL.

Use the output to update the field-mapping code in:
  - enrich_fundamentals.py  (ratios + key-metrics)
  - refresh_data.py         (earnings, profile)
  - fetch_insider_and_short (already self-diagnoses)

Run: python fmp_field_diagnostic.py
"""
import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

FMP_KEY = os.getenv("FMP_API_KEY")
STABLE = "https://financialmodelingprep.com/stable"

if not FMP_KEY:
    print("ERROR: FMP_API_KEY not set")
    exit(1)


def probe(path, params=None):
    params = params or {}
    params["apikey"] = FMP_KEY
    full = f"{STABLE}/{path}"
    try:
        r = requests.get(full, params=params, timeout=20)
        print(f"\n{'='*70}")
        print(f"ENDPOINT: {path}")
        print(f"PARAMS:   {dict((k,v) for k,v in params.items() if k != 'apikey')}")
        print(f"STATUS:   {r.status_code}")
        if r.status_code != 200:
            print(f"BODY:     {r.text[:300]}")
            return None
        data = r.json()
        if isinstance(data, dict):
            if "Error Message" in data:
                print(f"FMP error: {data['Error Message'][:200]}")
                return None
            print(f"SHAPE:    dict with {len(data)} keys")
            print(f"KEYS:     {list(data.keys())[:30]}")
            print(f"SAMPLE:")
            print(json.dumps(data, indent=2, default=str)[:1500])
            return data
        if isinstance(data, list):
            if not data:
                print(f"SHAPE:    empty list")
                return []
            print(f"SHAPE:    list of {len(data)} items")
            sample = data[0]
            print(f"FIRST ITEM keys ({len(sample)} total):")
            for k in sorted(sample.keys()):
                v = sample[k]
                vstr = str(v)[:80]
                print(f"  {k:40s} = {vstr}")
            return data
        print(f"SHAPE:    {type(data).__name__}")
        return data
    except Exception as e:
        print(f"EXCEPTION: {e}")
        return None


print("=" * 70)
print("FMP FIELD DIAGNOSTIC — testing endpoints for AAPL")
print("=" * 70)

# ────────────────────────────────────────────────────────────────────
# Fundamentals candidates
# ────────────────────────────────────────────────────────────────────
print("\n\n### FUNDAMENTALS — RATIOS ###")
probe("ratios-ttm",       {"symbol": "AAPL"})
probe("ratios",           {"symbol": "AAPL", "limit": 1})
probe("financial-ratios", {"symbol": "AAPL"})

print("\n\n### FUNDAMENTALS — KEY METRICS ###")
probe("key-metrics-ttm", {"symbol": "AAPL"})
probe("key-metrics",     {"symbol": "AAPL", "limit": 1})

print("\n\n### FUNDAMENTALS — INCOME / METRICS ###")
probe("income-statement", {"symbol": "AAPL", "limit": 1, "period": "annual"})
probe("financial-growth", {"symbol": "AAPL", "limit": 1})
probe("enterprise-values", {"symbol": "AAPL", "limit": 1})

# ────────────────────────────────────────────────────────────────────
# Earnings candidates
# ────────────────────────────────────────────────────────────────────
print("\n\n### EARNINGS — UPCOMING / SCHEDULE ###")
probe("earnings",                  {"symbol": "AAPL", "limit": 5})
probe("earnings-calendar",         {"symbol": "AAPL"})

print("\n\n### EARNINGS — HISTORICAL WITH ACTUALS ###")
probe("earnings-surprises",        {"symbol": "AAPL", "limit": 5})
probe("earnings-historical",       {"symbol": "AAPL", "limit": 5})
probe("historical-earnings",       {"symbol": "AAPL", "limit": 5})
probe("earnings-call-transcript",  {"symbol": "AAPL", "limit": 1})  # to confirm earnings access

# ────────────────────────────────────────────────────────────────────
# Profile (control test — we know this works)
# ────────────────────────────────────────────────────────────────────
print("\n\n### PROFILE (control test) ###")
probe("profile", {"symbol": "AAPL"})

# ────────────────────────────────────────────────────────────────────
# Analyst estimates
# ────────────────────────────────────────────────────────────────────
print("\n\n### ANALYST ESTIMATES ###")
probe("analyst-estimates",            {"symbol": "AAPL", "limit": 1})
probe("price-target",                 {"symbol": "AAPL"})
probe("price-target-summary",         {"symbol": "AAPL"})
probe("price-target-consensus",       {"symbol": "AAPL"})
probe("analyst-stock-recommendations", {"symbol": "AAPL", "limit": 5})

print("\n\nDone. Review the output and find which endpoints + field names work.")
