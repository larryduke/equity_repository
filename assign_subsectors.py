"""
assign_subsectors.py — Adds a curated `subsector` column to the tickers table
and populates it via a deterministic mapping from FMP's `industry` field.

Why this exists:
  Standard GICS "Technology" is too coarse for analysis. NVDA (semis) and
  HUBS (SaaS) behave nothing alike. This script bucketizes every ticker
  into ~30 actionable subsectors so queries can distinguish them.

Run: python assign_subsectors.py
Idempotent — safe to re-run.

Environment:
    DATABASE_URL
"""
import os
import sys
import re
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


# =============================================================================
# Subsector taxonomy
# Each entry maps a regex pattern (matched against FMP industry) to a subsector.
# Order matters — first match wins. Most specific first.
# =============================================================================
SUBSECTOR_RULES = [
    # ── Technology splits ──────────────────────────────────────────────
    (r"semiconductor",                              "Semiconductors"),
    (r"software.*application|application software", "Software – Application"),
    (r"software.*infrastructure|infrastructure software|systems software", "Software – Infrastructure"),
    (r"information technology services|it services", "IT Services"),
    (r"computer hardware|electronic equipment",     "Hardware / Components"),
    (r"communication equipment",                    "Communications Equipment"),
    (r"consumer electronics",                       "Consumer Electronics"),
    (r"scientific.*instruments|technology hardware","Tech Instruments"),

    # ── Financials splits ──────────────────────────────────────────────
    (r"banks.*diversified|diversified banks",       "Banks – Money Center"),
    (r"banks.*regional|regional banks",             "Banks – Regional"),
    (r"capital markets|investment banking",         "Capital Markets / Brokers"),
    (r"asset management",                           "Asset Management"),
    (r"insurance.*property|property.*casualty",     "Insurance – P&C"),
    (r"insurance.*life",                            "Insurance – Life"),
    (r"insurance.*specialty|insurance.*brokers",    "Insurance – Specialty"),
    (r"credit services|consumer finance",           "Credit / Payments"),
    (r"financial.*conglomerate|financial.*data",    "Financial Conglomerate"),
    (r"mortgage finance|mortgage",                  "Mortgage / REIT Mtge"),

    # ── Healthcare splits ──────────────────────────────────────────────
    (r"biotechnology",                              "Biotechnology"),
    (r"drug manufacturers.*general|pharmaceutical", "Pharmaceuticals – Large"),
    (r"drug manufacturers.*specialty",              "Pharmaceuticals – Specialty"),
    (r"medical devices",                            "Medical Devices"),
    (r"medical instruments.*supplies",              "Medical Supplies"),
    (r"diagnostics.*research|medical care",         "Diagnostics / Care"),
    (r"health.*insurance|healthcare.*plans",        "Health Insurance"),
    (r"healthcare.*equipment",                      "Healthcare Equipment"),
    (r"healthcare.*services",                       "Healthcare Services"),

    # ── Consumer Cyclical splits ───────────────────────────────────────
    (r"internet.*retail|specialty retail",          "E-commerce / Specialty Retail"),
    (r"apparel.*retail|footwear",                   "Apparel / Footwear"),
    (r"auto manufacturers|auto.*truck",             "Autos – Manufacturers"),
    (r"auto parts",                                 "Auto Parts"),
    (r"restaurants",                                "Restaurants"),
    (r"travel.*services|lodging|leisure",           "Travel & Leisure"),
    (r"home improvement|household durables",        "Home / Durables"),
    (r"luxury goods",                               "Luxury Goods"),
    (r"gambling",                                   "Gaming / Gambling"),

    # ── Consumer Defensive splits ──────────────────────────────────────
    (r"beverages.*non-alcoholic",                   "Beverages – Soft"),
    (r"beverages.*brewers|wineries|alcoholic",      "Beverages – Alcohol"),
    (r"packaged foods|food.*products",              "Packaged Foods"),
    (r"tobacco",                                    "Tobacco"),
    (r"household.*products|personal.*products",     "Household / Personal Products"),
    (r"discount stores|grocery|food.*staples retail","Discount / Grocery"),

    # ── Energy splits ──────────────────────────────────────────────────
    (r"oil.*gas.*integrated",                       "Oil & Gas – Integrated"),
    (r"oil.*gas.*e\&p|oil.*gas.*exploration",       "Oil & Gas – E&P"),
    (r"oil.*gas.*midstream|pipelines",              "Pipelines / Midstream"),
    (r"oil.*gas.*refining",                         "Oil & Gas – Refining"),
    (r"oil.*gas.*equipment|drilling",               "Oil Services / Drilling"),
    (r"uranium|coal",                               "Uranium / Coal"),
    (r"renewable.*energy|solar",                    "Renewable Energy / Solar"),

    # ── Industrials splits ─────────────────────────────────────────────
    (r"aerospace.*defense",                         "Aerospace & Defense"),
    (r"railroads",                                  "Railroads"),
    (r"airlines",                                   "Airlines"),
    (r"trucking",                                   "Trucking"),
    (r"integrated freight.*logistics|marine.*shipping","Freight / Logistics"),
    (r"building materials|building products",       "Building Materials / Products"),
    (r"engineering.*construction|infrastructure",   "Engineering & Construction"),
    (r"electrical equipment",                       "Electrical Equipment"),
    (r"farm.*heavy machinery|machinery",            "Industrial Machinery"),
    (r"industrial.*conglomerate",                   "Industrial Conglomerates"),
    (r"specialty industrial",                       "Specialty Industrial"),
    (r"business services|staffing",                 "Business Services"),
    (r"rental.*leasing",                            "Rental / Leasing"),
    (r"waste management",                           "Waste Management"),

    # ── Communication Services splits ──────────────────────────────────
    (r"internet content.*information",              "Internet / Digital Media"),
    (r"interactive media|social media",             "Interactive Media"),
    (r"telecom services|wireless",                  "Telecom Services"),
    (r"entertainment.*media|broadcasting",          "Media / Entertainment"),
    (r"electronic gaming.*multimedia",              "Gaming / Multimedia"),
    (r"advertising agencies",                       "Advertising"),
    (r"publishing",                                 "Publishing"),

    # ── Utilities splits ───────────────────────────────────────────────
    (r"utilities.*regulated electric|electric",     "Utilities – Electric"),
    (r"utilities.*regulated gas|gas",               "Utilities – Gas"),
    (r"utilities.*regulated water|water",           "Utilities – Water"),
    (r"utilities.*renewable",                       "Utilities – Renewable"),
    (r"utilities.*diversified",                     "Utilities – Diversified"),

    # ── Real Estate splits ─────────────────────────────────────────────
    (r"reit.*residential",                          "REIT – Residential"),
    (r"reit.*industrial",                           "REIT – Industrial"),
    (r"reit.*retail",                               "REIT – Retail"),
    (r"reit.*office",                               "REIT – Office"),
    (r"reit.*healthcare",                           "REIT – Healthcare"),
    (r"reit.*specialty|reit.*hotel|reit.*diversified","REIT – Specialty"),
    (r"real estate services",                       "Real Estate Services"),

    # ── Basic Materials splits ─────────────────────────────────────────
    (r"gold",                                       "Gold"),
    (r"silver",                                     "Silver"),
    (r"copper|industrial metals",                   "Industrial Metals"),
    (r"steel|iron",                                 "Steel"),
    (r"specialty chemicals",                        "Specialty Chemicals"),
    (r"chemicals",                                  "Chemicals"),
    (r"agricultural.*inputs|fertilizer",            "Agricultural Inputs"),
    (r"paper.*forest|lumber",                       "Forest Products"),
    (r"construction.*materials|aggregates",         "Construction Materials"),
]


def assign_subsector(industry: str | None) -> str | None:
    """Map an FMP industry string to one of our curated subsectors."""
    if not industry:
        return None
    ind = industry.lower()
    for pattern, label in SUBSECTOR_RULES:
        if re.search(pattern, ind):
            return label
    return "Other"


def main():
    print("=" * 60)
    print("Assigning subsectors")
    print("=" * 60)

    # 1. Add subsector column if missing (idempotent)
    print("\nEnsuring `subsector` column exists on tickers table...")
    with engine.begin() as con:
        con.execute(text(
            "ALTER TABLE tickers ADD COLUMN IF NOT EXISTS subsector VARCHAR(120)"
        ))
        con.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_tickers_subsector ON tickers(subsector)"
        ))

    # 2. Load all tickers with their industry
    with engine.connect() as con:
        df = pd.read_sql(
            text("SELECT ticker, industry, sector, subsector FROM tickers "
                 "WHERE is_active = TRUE"),
            con
        )

    if df.empty:
        sys.exit("No tickers found.")

    print(f"\nProcessing {len(df)} tickers...")

    # 3. Compute new subsectors
    df["new_subsector"] = df["industry"].apply(assign_subsector)

    # 4. Show summary before writing
    summary = df.groupby("new_subsector", dropna=False)["ticker"].count().reset_index()
    summary = summary.sort_values("ticker", ascending=False)
    summary.columns = ["subsector", "n"]
    print("\nSubsector distribution:")
    print(summary.to_string(index=False))

    other_count = (df["new_subsector"] == "Other").sum()
    null_industry = df["industry"].isna().sum()
    print(f"\n  {other_count} fell into 'Other' bucket (industry didn't match any rule)")
    print(f"  {null_industry} have NULL industry (FMP didn't return one)")

    # 5. Show what's in "Other" so we can refine rules over time
    if other_count > 0:
        other_df = df[df["new_subsector"] == "Other"]
        sample_industries = other_df["industry"].value_counts().head(20)
        print("\n  Top 'Other' industries (refine SUBSECTOR_RULES to capture):")
        for ind, n in sample_industries.items():
            print(f"    {ind} ({n})")

    # 6. Write subsectors
    print("\nWriting subsectors...")
    with engine.begin() as con:
        for _, row in df.iterrows():
            con.execute(
                text("UPDATE tickers SET subsector = :ss WHERE ticker = :t"),
                {"ss": row["new_subsector"], "t": row["ticker"]}
            )
    print(f"  {len(df)} tickers updated")

    # 7. Verify
    with engine.connect() as con:
        counts = pd.read_sql(
            text("SELECT sector, subsector, COUNT(*) AS n FROM tickers "
                 "WHERE is_active = TRUE AND subsector IS NOT NULL "
                 "GROUP BY sector, subsector ORDER BY sector, n DESC"),
            con
        )

    print("\nSubsectors by sector:")
    for sector in counts["sector"].dropna().unique():
        sub = counts[counts["sector"] == sector].head(8)
        print(f"\n  {sector}:")
        for _, r in sub.iterrows():
            print(f"    {r['subsector']:40s} {r['n']:>4d}")

    print("\nDone.")


if __name__ == "__main__":
    main()
