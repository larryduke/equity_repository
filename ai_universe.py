"""
ai_universe.py — The definitive AI ecosystem ticker universe.

~250 names organized by stack layer. Imported by build_dashboard.py
for scoring and by load_extra_tickers.py for database loading.

To add a name: put it in the right layer dict, run load_extra_tickers.py
to add it to the DB, then refresh_data.py --incremental to get its data.

Layers:
  1. Silicon         — GPUs, custom ASICs, memory, EDA, equipment, foundry
  2. Optical         — transceivers, photonics, fiber, components
  3. Networking      — switches, routers, high-speed interconnect
  4. Power           — transformers, switchgear, UPS, generators, grid infra
  5. Cooling         — liquid cooling, thermal management, HVAC
  6. Data Centers    — REITs, colocation, tower infrastructure
  7. Connectors      — physical interconnects, cables, fiber, PCBs
  8. Manufacturing   — contract manufacturing, assembly, test
  9. Cloud           — hyperscalers, IaaS/PaaS
  10. AI Platform    — MLOps, data platforms, developer tools
  11. Enterprise AI  — AI-enabled SaaS, productivity, vertical
  12. Cybersecurity  — AI-powered security tools
  13. AI Application — consumer platforms, adtech, fintech
  14. Robotics       — physical AI, autonomy, surgical, industrial
  15. Defense AI     — military AI, autonomous systems, ISR
  16. Healthcare AI  — diagnostics, drug discovery, medtech
  17. Autonomous     — self-driving, ADAS, lidar, sensors
  18. Edge AI        — IoT, embedded vision, edge compute
  19. AI Services    — consulting, implementation, outsourcing
"""

AI_LAYERS = {
    # ══════════════════════════════════════════════════════════════
    # HARDWARE STACK
    # ══════════════════════════════════════════════════════════════

    "Silicon": {
        # GPUs / Accelerators
        "NVDA", "AMD", "INTC", "QCOM",
        # Custom ASIC / networking silicon
        "AVGO", "MRVL", "ARM",
        # Memory (HBM is the bottleneck for AI training)
        "MU", "WDC",
        # EDA (every chip starts here)
        "SNPS", "CDNS",
        # Semiconductor equipment (building the fabs)
        "ASML", "KLAC", "LRCX", "AMAT", "ONTO", "ACLS",
        "ENTG", "MKSI", "AMKR", "COHU", "FORM",
        # Foundry
        "TSM", "GFS", "UMC",
        # Analog / mixed-signal / power management
        "ADI", "TXN", "MCHP", "MPWR", "ON", "SWKS", "QRVO",
        # AI server platforms
        "SMCI",
    },

    "Optical": {
        # Transceivers (the 800G/1.6T links between GPUs)
        "AAOI", "LITE", "COHR", "CIEN",
        # High-speed connectivity silicon
        "CRDO", "MTSI",
        # Test & measurement for optical
        "VIAV",
        # Optical networking systems
        "INFN", "CALX",
    },

    "Networking": {
        # Data center switches (spine/leaf for AI clusters)
        "ANET", "CSCO", "JNPR",
        # Storage networking
        "NTAP", "PSTG", "NTNX",
        # Wireless infrastructure
        "SYNA",
    },

    "Power": {
        # Power generation & grid (AI data centers need GW-scale)
        "GEV",   # GE Vernova — turbines, grid solutions
        "ETN",   # Eaton — power management, UPS, switchgear
        "VRT",   # Vertiv — data center power & thermal
        "EMR",   # Emerson — automation + power
        "GE",    # GE Aerospace (also power legacy)
        # Electrical infrastructure
        "PWR",   # Quanta Services — grid construction
        "HUBB",  # Hubbell — electrical products
        "AME",   # AMETEK — electronic instruments, power
        "GNRC",  # Generac — backup generators for DC
        "POWL",  # Powell Industries — switchgear, bus duct
        "ENS",   # EnerSys — industrial batteries/UPS
        "RRX",   # Regal Rexnord — motors, drives
        "AYI",   # Acuity Brands — lighting + controls
        "BDC",   # Belden — signal transmission
        "APTV",  # Aptiv — electrical architecture
        # Power electronics & solar (DC power)
        "ENPH",  # Enphase — power electronics
        "FSLR",  # First Solar — utility-scale power for DC
        "FLNC",  # Fluence Energy — energy storage
        "NEE",   # NextEra — renewable power generation
        # Fuel cells / backup
        "PLUG",  # Plug Power
        "FCEL",  # FuelCell Energy
        "BE",    # Bloom Energy — solid oxide fuel cells
    },

    "Cooling": {
        "CARR",  # Carrier — HVAC, liquid cooling for DC
        "TT",    # Trane Technologies — climate solutions
        "JCI",   # Johnson Controls — building automation + cooling
        "AAON",  # AAON — precision cooling units
        "WSO",   # Watsco — HVAC distribution
    },

    "Data Centers": {
        "DLR",   # Digital Realty — DC REIT
        "EQIX",  # Equinix — colocation + interconnection
        "AMT",   # American Tower
        "CCI",   # Crown Castle
        "SBAC",  # SBA Communications
        "IRM",   # Iron Mountain — DC + storage
    },

    "Connectors": {
        "APH",   # Amphenol — connectors, fiber
        "TEL",   # TE Connectivity — connectors, sensors
        "GLW",   # Corning — fiber optic cable
        "KEYS",  # Keysight — test & measurement
    },

    "Manufacturing": {
        "JBL",   # Jabil — contract manufacturing
        "FLEX",  # Flex Ltd — contract manufacturing
        "CLS",   # Celestica — server/networking assembly
        "SANM",  # Sanmina — electronics manufacturing
        "DELL",  # Dell — enterprise hardware
        "HPE",   # HPE — servers, storage, networking
        "IBM",   # IBM — hybrid cloud + AI hardware
    },

    # ══════════════════════════════════════════════════════════════
    # SOFTWARE STACK
    # ══════════════════════════════════════════════════════════════

    "Cloud": {
        "AMZN",  # AWS
        "MSFT",  # Azure
        "GOOGL", # GCP
        "META",  # AI research + inference at scale
        "ORCL",  # OCI + autonomous DB
        "CRM",   # Salesforce platform
    },

    "AI Platform": {
        "PLTR",  # Palantir — AIP, Foundry, Gotham
        "SNOW",  # Snowflake — data cloud
        "DDOG",  # Datadog — observability for AI workloads
        "MDB",   # MongoDB — document DB for AI apps
        "CFLT",  # Confluent — real-time streaming
        "GTLB",  # GitLab — DevSecOps
        "ESTC",  # Elastic — search + vector DB
        "AI",    # C3.ai — enterprise AI platform
        "BBAI",  # BigBear.ai — decision intelligence
        "SOUN",  # SoundHound — voice AI
        "PATH",  # UiPath — automation + AI agents
    },

    "Enterprise AI": {
        "NOW",   # ServiceNow — IT workflow AI
        "HUBS",  # HubSpot — marketing/sales AI
        "WDAY",  # Workday — HR/finance AI
        "ADBE",  # Adobe — creative AI (Firefly)
        "INTU",  # Intuit — financial AI
        "TEAM",  # Atlassian — dev collaboration
        "VEEV",  # Veeva — life sciences cloud
        "PAYC",  # Paycom — HR tech
        "BILL",  # Bill.com — AP/AR automation
        "DOCS",  # Doximity — physician AI platform
        "DOCU",  # DocuSign — agreement AI
        "ZM",    # Zoom — AI companion
        "TWLO",  # Twilio — communications AI
        "RNG",   # RingCentral — comms platform
    },

    "Cybersecurity": {
        "CRWD",  # CrowdStrike — AI-native endpoint
        "PANW",  # Palo Alto — platform security
        "ZS",    # Zscaler — zero trust
        "FTNT",  # Fortinet — network security
        "S",     # SentinelOne — autonomous AI security
        "CYBR",  # CyberArk — identity security
        "OKTA",  # Okta — identity
        "NET",   # Cloudflare — edge security + AI gateway
        "VRNS",  # Varonis — data security AI
    },

    # ══════════════════════════════════════════════════════════════
    # APPLICATION LAYER
    # ══════════════════════════════════════════════════════════════

    "AI Application": {
        # Consumer platforms (AI-driven matching/recommendations)
        "SHOP",  # Shopify — AI commerce
        "UBER",  # Uber — routing, pricing AI
        "ABNB",  # Airbnb — recommendation AI
        "DASH",  # DoorDash — logistics AI
        "COIN",  # Coinbase — crypto + AI trading
        "RBLX",  # Roblox — generative AI gaming
        "U",     # Unity — real-time 3D + AI tools
        # Adtech / data (AI-powered targeting)
        "TTD",   # The Trade Desk — programmatic AI
        "APP",   # AppLovin — AI-powered mobile ads
        "SNAP",  # Snap — AR/AI features
        "PINS",  # Pinterest — visual AI search
        "SPOT",  # Spotify — recommendation AI
        "DUOL",  # Duolingo — AI-powered language learning
        # Fintech (AI-driven)
        "SQ",    # Block — fintech AI
        "AFRM",  # Affirm — credit AI
        "UPST",  # Upstart — lending AI
        "HOOD",  # Robinhood — trading AI
        "SOFI",  # SoFi — financial AI
    },

    # ══════════════════════════════════════════════════════════════
    # PHYSICAL AI
    # ══════════════════════════════════════════════════════════════

    "Robotics": {
        "TSLA",  # Tesla — Optimus, FSD
        "ISRG",  # Intuitive Surgical — surgical robotics
        "ROK",   # Rockwell — industrial automation
        "TER",   # Teradyne — collaborative robots (UR)
        "IRBT",  # iRobot — consumer robotics
    },

    "Autonomous": {
        "RIVN",  # Rivian — autonomous EV
        "LCID",  # Lucid — autonomous EV
        "MBLY",  # Mobileye — ADAS + self-driving
        "LAZR",  # Luminar — lidar
        "INVZ",  # Innoviz — lidar
        "AMBA",  # Ambarella — edge AI vision
        "PI",    # Impinj — RAIN RFID / IoT
    },

    "Defense AI": {
        "LMT",   # Lockheed — autonomous systems
        "RTX",   # Raytheon — AI-guided munitions, radar
        "NOC",   # Northrop — autonomous aircraft, ISR
        "GD",    # General Dynamics — C4ISR
        "LDOS",  # Leidos — defense IT + AI
        "KTOS",  # Kratos — autonomous drones
        "BWXT",  # BWX Technologies — nuclear + defense tech
    },

    "Healthcare AI": {
        "DXCM",  # Dexcom — continuous glucose AI
        "ILMN",  # Illumina — genomic AI
        "TDOC",  # Teladoc — virtual care AI
        "HIMS",  # Hims & Hers — telehealth AI
        "NVCR",  # NovoCure — AI-guided tumor treatment
    },

    "AI Services": {
        "ACN",   # Accenture — AI consulting + implementation
        "EPAM",  # EPAM — engineering + AI services
        "GLOB",  # Globant — AI-powered digital transformation
    },
}


# Flat set of all tickers for quick membership checks
AI_UNIVERSE = set()
for layer_tickers in AI_LAYERS.values():
    AI_UNIVERSE.update(layer_tickers)

# Reverse map: ticker → layer name
AI_LAYER_MAP = {}
for layer_name, tickers in AI_LAYERS.items():
    for ticker in tickers:
        AI_LAYER_MAP[ticker] = layer_name


if __name__ == "__main__":
    print(f"Total AI universe: {len(AI_UNIVERSE)} names\n")
    for layer, tickers in AI_LAYERS.items():
        print(f"  {layer:20s}  {len(tickers):3d}  {', '.join(sorted(tickers)[:8])}{'...' if len(tickers) > 8 else ''}")
    print(f"\n  {'TOTAL':20s}  {len(AI_UNIVERSE):3d}")
