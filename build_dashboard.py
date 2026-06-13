"""
build_dashboard.py — Nightly job that pre-computes all dashboard content.

Runs after refresh_data.py completes. Produces:

  1. Composite scores for every ticker (bullish + bearish)
  2. Top 10 lists for 6 categories
  3. Historical analogue matches (today vs every prior day)
  4. Daily dashboard sections with Claude-written prose
  5. Calendar effect tables (midterm year returns, post-Fed cuts, etc.)

Environment:
    DATABASE_URL    — Supabase admin connection
    ANTHROPIC_API_KEY — for prose generation
"""
import os
import sys
import json
import numpy as np
import pandas as pd
from datetime import date, timedelta
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-5"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def _json_safe(obj):
    """Recursively replace NaN/Inf with None so JSON serialization is valid.
    Postgres JSON columns reject NaN."""
    import math
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    return obj


# Lazy import — only when prose generation is needed
def get_claude_client():
    if not ANTHROPIC_KEY:
        return None
    try:
        from anthropic import Anthropic
        return Anthropic(api_key=ANTHROPIC_KEY)
    except ImportError:
        return None


# =============================================================================
# Step 1: Composite scoring
# =============================================================================
def calculate_bullish_score(row):
    """
    Composite bullish score 0-100 per ticker.
    Weights:
      momentum (room to run)     25
      proximity to major support 20
      sector rotation tailwind   20
      volume confirmation        15
      analyst consensus          10
      recent insider buying      10
    """
    score = 0
    components = {}

    # Momentum: RSI between 35-60 = sweet spot (not oversold, not overbought)
    rsi = row.get("rsi_14")
    if rsi is not None:
        if 35 <= rsi <= 60:
            v = 25 * (1 - abs(rsi - 47.5) / 12.5)
            score += v
            components["momentum"] = round(v, 1)
        elif 60 < rsi <= 70:
            v = 15
            score += v
            components["momentum"] = v
        else:
            components["momentum"] = 0

    # Support proximity
    pct_to_support = row.get("pct_to_nearest_support")
    support_tier = row.get("support_tier")
    if pct_to_support is not None:
        if abs(pct_to_support) < 5 and support_tier in ("major", "moderate"):
            v = 20 * (1 - abs(pct_to_support) / 5)
            score += v
            components["support"] = round(v, 1)
        else:
            components["support"] = 0

    # Sector rotation tailwind
    rotation = row.get("sector_rotation_into")
    if rotation is not None and rotation >= 50:
        v = 20 * (min(rotation, 80) - 50) / 30
        score += v
        components["rotation"] = round(v, 1)
    else:
        components["rotation"] = 0

    # Volume confirmation: rel_volume > 1.2
    rel_vol = row.get("rel_volume")
    if rel_vol is not None and rel_vol > 1.0:
        v = min(15 * (rel_vol - 1.0) / 0.5, 15)
        score += v
        components["volume"] = round(v, 1)
    else:
        components["volume"] = 0

    # Analyst consensus
    consensus = row.get("analyst_consensus")
    target_upside = row.get("analyst_target_upside")
    if consensus in ("Buy", "Strong Buy") and target_upside and target_upside > 5:
        v = min(10 * target_upside / 20, 10)
        score += v
        components["analyst"] = round(v, 1)
    else:
        components["analyst"] = 0

    # Insider buying in last 30 days
    insider_buys = row.get("insider_buys_30d", 0) or 0
    if insider_buys >= 2:
        v = min(10, insider_buys * 3)
        score += v
        components["insider"] = round(v, 1)
    else:
        components["insider"] = 0

    return round(score, 1), components


def calculate_bearish_score(row):
    """Mirror of bullish — high score = bearish setup."""
    score = 0
    components = {}

    rsi = row.get("rsi_14")
    if rsi is not None and rsi > 65:
        v = min(25 * (rsi - 65) / 15, 25)
        score += v
        components["overbought"] = round(v, 1)

    pct_to_resist = row.get("pct_to_nearest_resistance")
    resist_tier = row.get("resistance_tier")
    if pct_to_resist is not None:
        if abs(pct_to_resist) < 5 and resist_tier in ("major", "moderate"):
            v = 20 * (1 - abs(pct_to_resist) / 5)
            score += v
            components["resistance"] = round(v, 1)

    rotation_out = row.get("sector_rotation_out")
    if rotation_out is not None and rotation_out >= 50:
        v = 20 * (min(rotation_out, 80) - 50) / 30
        score += v
        components["rotation_out"] = round(v, 1)

    # Distribution: high volume on down days
    rel_vol = row.get("rel_volume")
    daily_return = row.get("daily_return")
    if rel_vol and daily_return and rel_vol > 1.3 and daily_return < -1:
        score += 15
        components["distribution"] = 15

    consensus = row.get("analyst_consensus")
    if consensus in ("Sell", "Underperform"):
        score += 10
        components["analyst_negative"] = 10

    return round(score, 1), components


# =============================================================================
# Step 2: Load all the data we need to score
# =============================================================================
def load_scoring_universe():
    """
    Returns a DataFrame with one row per active S&P 500 ticker containing
    all the columns needed for both bullish and bearish scoring.
    """
    print("  loading scoring universe...")

    sql = """
    WITH latest_bars AS (
        SELECT DISTINCT ON (ticker) ticker, date, close,
            rsi_14, rel_volume, daily_return, macd_histogram,
            pct_vs_ma50, pct_vs_ma200, ma_50, ma_200
        FROM daily_bars
        WHERE date >= CURRENT_DATE - 7
        ORDER BY ticker, date DESC
    ),
    nearest_support AS (
        SELECT DISTINCT ON (lb.ticker)
            lb.ticker,
            srl.level_price AS nearest_support_price,
            srl.strength_tier AS support_tier,
            ((srl.level_price - lb.close) / lb.close * 100) AS pct_to_nearest_support
        FROM latest_bars lb
        JOIN support_resistance_levels srl
          ON srl.ticker = lb.ticker
         AND srl.level_type = 'support'
         AND srl.level_price < lb.close
         AND srl.calculated_date = (SELECT MAX(calculated_date)
                                    FROM support_resistance_levels
                                    WHERE ticker = lb.ticker)
        ORDER BY lb.ticker, srl.level_price DESC
    ),
    nearest_resistance AS (
        SELECT DISTINCT ON (lb.ticker)
            lb.ticker,
            srl.level_price AS nearest_resistance_price,
            srl.strength_tier AS resistance_tier,
            ((srl.level_price - lb.close) / lb.close * 100) AS pct_to_nearest_resistance
        FROM latest_bars lb
        JOIN support_resistance_levels srl
          ON srl.ticker = lb.ticker
         AND srl.level_type = 'resistance'
         AND srl.level_price > lb.close
         AND srl.calculated_date = (SELECT MAX(calculated_date)
                                    FROM support_resistance_levels
                                    WHERE ticker = lb.ticker)
        ORDER BY lb.ticker, srl.level_price ASC
    ),
    latest_rotation AS (
        SELECT sector_a AS sector, MAX(rotation_score) AS sector_rotation_into
        FROM sector_relative_strength
        WHERE date = (SELECT MAX(date) FROM sector_relative_strength)
        GROUP BY sector_a
    ),
    latest_rotation_out AS (
        SELECT sector_b AS sector, MAX(rotation_score) AS sector_rotation_out
        FROM sector_relative_strength
        WHERE date = (SELECT MAX(date) FROM sector_relative_strength)
        GROUP BY sector_b
    ),
    insider_recent AS (
        SELECT ticker, COUNT(*) AS insider_buys_30d
        FROM insider_transactions
        WHERE transaction_type = 'Buy'
          AND date >= CURRENT_DATE - 30
        GROUP BY ticker
    ),
    latest_fund AS (
        SELECT DISTINCT ON (ticker) ticker,
            analyst_consensus, analyst_target_price, analyst_target_upside,
            pe_forward, market_cap
        FROM fundamentals
        ORDER BY ticker, date DESC
    )
    SELECT
        t.ticker, t.name AS company_name, t.sector,
        lb.close AS current_price, lb.rsi_14, lb.rel_volume, lb.daily_return,
        lb.macd_histogram, lb.pct_vs_ma50, lb.pct_vs_ma200,
        ns.nearest_support_price, ns.support_tier, ns.pct_to_nearest_support,
        nr.nearest_resistance_price, nr.resistance_tier, nr.pct_to_nearest_resistance,
        lr.sector_rotation_into,
        lro.sector_rotation_out,
        ir.insider_buys_30d,
        lf.analyst_consensus, lf.analyst_target_price, lf.analyst_target_upside,
        lf.pe_forward, lf.market_cap
    FROM tickers t
    JOIN latest_bars lb         ON lb.ticker = t.ticker
    LEFT JOIN nearest_support ns ON ns.ticker = t.ticker
    LEFT JOIN nearest_resistance nr ON nr.ticker = t.ticker
    LEFT JOIN latest_rotation lr ON lr.sector  = t.sector
    LEFT JOIN latest_rotation_out lro ON lro.sector = t.sector
    LEFT JOIN insider_recent ir ON ir.ticker = t.ticker
    LEFT JOIN latest_fund lf    ON lf.ticker = t.ticker
    WHERE t.is_active = TRUE
      AND t.in_sp500 = TRUE
    """

    with engine.connect() as con:
        df = pd.read_sql(text(sql), con)

    print(f"    {len(df)} tickers loaded for scoring")
    return df


# =============================================================================
# Step 3: Build the Top 10 lists
# =============================================================================
def build_lists(today=None):
    today = today or date.today()
    df = load_scoring_universe()

    if df.empty:
        print("  no scoring universe — aborting list build")
        return

    # Calculate bullish + bearish scores
    bull_scores = df.apply(calculate_bullish_score, axis=1)
    bear_scores = df.apply(calculate_bearish_score, axis=1)
    df["bull_score"] = [s[0] for s in bull_scores]
    df["bull_components"] = [s[1] for s in bull_scores]
    df["bear_score"] = [s[0] for s in bear_scores]
    df["bear_components"] = [s[1] for s in bear_scores]

    def insert_list(list_type, scored_df, score_col, components_col, threshold=40):
        ranked = scored_df[scored_df[score_col] >= threshold].sort_values(
            score_col, ascending=False
        ).head(30).reset_index(drop=True)

        if ranked.empty:
            print(f"    {list_type}: 0 candidates above threshold {threshold}")
            return

        with engine.begin() as con:
            # Clear today's rows for this list
            con.execute(text(
                "DELETE FROM dashboard_lists WHERE list_date=:d AND list_type=:t"
            ), {"d": today, "t": list_type})

            for i, row in ranked.iterrows():
                comps = row[components_col] or {}
                signals = [k for k, v in comps.items() if v > 0]
                reason = build_reason_sentence(row, list_type, comps)
                tier = ("major" if row[score_col] >= 75
                        else "moderate" if row[score_col] >= 60
                        else "minor")

                con.execute(text("""
                    INSERT INTO dashboard_lists (
                        list_date, list_type, rank, ticker, sector, company_name,
                        composite_score, strength_tier, reason,
                        components_json, signals_json,
                        current_price, market_cap, days_on_list, first_appeared
                    ) VALUES (
                        :d, :t, :r, :tk, :sec, :cn,
                        :sc, :st, :rs,
                        :cj, :sj,
                        :cp, :mc, 1, :d
                    )
                """), {
                    "d": today, "t": list_type, "r": i + 1,
                    "tk": row["ticker"], "sec": row.get("sector"),
                    "cn": row.get("company_name"),
                    "sc": float(row[score_col]), "st": tier,
                    "rs": reason,
                    "cj": json.dumps(_json_safe(comps)),
                    "sj": json.dumps(_json_safe(signals)),
                    "cp": float(row["current_price"]) if pd.notnull(row.get("current_price")) else None,
                    "mc": float(row["market_cap"]) if pd.notnull(row.get("market_cap")) else None,
                })

            # Update days_on_list for tickers that were on yesterday's list
            con.execute(text("""
                UPDATE dashboard_lists today
                SET days_on_list = COALESCE(yest.days_on_list, 0) + 1,
                    first_appeared = COALESCE(yest.first_appeared, today.list_date)
                FROM dashboard_lists yest
                WHERE today.list_date = :d
                  AND today.list_type = :t
                  AND yest.list_date = :d - INTERVAL '1 day'
                  AND yest.list_type = :t
                  AND yest.ticker = today.ticker
            """), {"d": today, "t": list_type})

        print(f"    {list_type}: top {len(ranked)} stored")

    print("  building Top 10 lists...")
    insert_list("bullish", df, "bull_score", "bull_components", threshold=40)
    insert_list("bearish", df, "bear_score", "bear_components", threshold=40)


def build_reason_sentence(row, list_type, components):
    """Build a one-line analytical reason. Used as the row description."""
    parts = []
    if list_type == "bullish":
        if components.get("rotation", 0) > 10:
            parts.append("Strong sector rotation tailwind")
        if components.get("support", 0) > 10:
            d = row.get("pct_to_nearest_support", 0)
            parts.append(f"Within {abs(d):.1f}% of major support")
        if components.get("momentum", 0) > 10:
            r = row.get("rsi_14", 0)
            parts.append(f"RSI {r:.0f} — room to run")
        if components.get("insider", 0) > 0:
            n = row.get("insider_buys_30d", 0)
            parts.append(f"{int(n)} insiders buying")
        if components.get("volume", 0) > 5:
            v = row.get("rel_volume", 1)
            parts.append(f"Volume {v:.1f}x average")
        if components.get("analyst", 0) > 5:
            u = row.get("analyst_target_upside", 0)
            parts.append(f"{u:.0f}% upside to consensus target")
    else:  # bearish
        if components.get("overbought", 0) > 10:
            r = row.get("rsi_14", 0)
            parts.append(f"RSI {r:.0f} — overbought")
        if components.get("resistance", 0) > 10:
            d = row.get("pct_to_nearest_resistance", 0)
            parts.append(f"Within {abs(d):.1f}% of major resistance")
        if components.get("rotation_out", 0) > 10:
            parts.append("Sector rotating out")
        if components.get("distribution", 0) > 0:
            parts.append("Distribution day on heavy volume")

    if not parts:
        return f"{row['ticker']} flagged by composite scoring"
    return ". ".join(parts) + "."


# =============================================================================
# Step 4: Macro vector + historical analogue matching
# =============================================================================
VECTOR_COMPONENTS = [
    ("vix_close",          15),
    ("pct_above_ma50",     15),
    ("yield_curve_10y_2y", 15),
    ("credit_spread_hy",   12),
    ("fed_funds_rate",     10),
    ("copper_gold_ratio",  10),
    ("dxy",                 8),
    ("cpi_yoy",            15),
]


def load_macro_history():
    cols = [c[0] for c in VECTOR_COMPONENTS]
    sql = f"""
        SELECT date, {','.join(cols)}
        FROM market_indicators
        WHERE date IS NOT NULL
        ORDER BY date
    """
    with engine.connect() as con:
        df = pd.read_sql(text(sql), con)
    # Require AT LEAST 5 of the 8 components to be present (not all 8)
    # so we can match across the full history despite credit spreads / DXY / etc
    # starting later.
    df["_present_count"] = df[cols].notna().sum(axis=1)
    df = df[df["_present_count"] >= 5].copy()
    df = df.drop(columns=["_present_count"])
    return df


def normalize_vectors(df):
    """Z-score each column using its historical median and IQR (robust).
    Missing values stay as NaN — distance calc skips them per-pair."""
    cols = [c[0] for c in VECTOR_COMPONENTS]
    normed = df.copy()
    for c in cols:
        valid = df[c].dropna()
        if len(valid) == 0:
            continue
        med = valid.median()
        q75, q25 = valid.quantile(0.75), valid.quantile(0.25)
        iqr = max(q75 - q25, 1e-6)
        normed[c] = (df[c] - med) / iqr
    return normed


def find_analogues(today=None, top_n=10):
    print("  finding historical analogues...")
    df = load_macro_history()
    if df.empty or len(df) < 100:
        print("    not enough macro history for analogue matching")
        return

    today = today or df["date"].max()
    today_row = df[df["date"] == today]
    if today_row.empty:
        today = df["date"].max()
        today_row = df[df["date"] == today]

    normed = normalize_vectors(df)
    today_norm = normed[normed["date"] == today].iloc[0]
    cols = [c[0] for c in VECTOR_COMPONENTS]
    weights = np.array([c[1] for c in VECTOR_COMPONENTS]) / 100.0

    today_vec = today_norm[cols].values.astype(float)

    # Compute weighted Euclidean distance to every other date
    history = normed[normed["date"] < today].copy()
    if history.empty:
        print("    no historical dates to compare to")
        return

    other_vecs = history[cols].values.astype(float)
    diffs = other_vecs - today_vec
    # Mask NaN pairs: if either today or analogue is missing a dimension, skip it
    nan_mask = np.isnan(diffs)
    diffs_clean = np.where(nan_mask, 0, diffs)
    # Per-row, only weight the dimensions actually present
    weight_matrix = np.broadcast_to(weights, diffs.shape).copy()
    weight_matrix[nan_mask] = 0
    # Reweight: scale up to compensate for missing dims (so distance is comparable)
    row_weight_sums = weight_matrix.sum(axis=1, keepdims=True)
    row_weight_sums = np.where(row_weight_sums < 0.5, 1.0, row_weight_sums)  # avoid div0
    weight_matrix_norm = weight_matrix / row_weight_sums
    weighted_sq = (diffs_clean ** 2) * weight_matrix_norm
    distances = np.sqrt(weighted_sq.sum(axis=1))

    # Convert distance to similarity score 0-100
    # A distance of 0 = 100, a distance of ~3+ = 0
    similarities = np.clip(100 * (1 - distances / 3.0), 0, 100)

    history["similarity"] = similarities
    top = history.sort_values("similarity", ascending=False).head(top_n)

    # For each analogue, compute forward S&P 500 returns
    spy_sql = """
        SELECT date, close
        FROM daily_bars
        WHERE ticker = 'SPY'
        ORDER BY date
    """
    with engine.connect() as con:
        spy = pd.read_sql(text(spy_sql), con)
    spy["date"] = pd.to_datetime(spy["date"]).dt.date
    spy = spy.set_index("date")["close"].to_dict()

    # Insert
    today_vec_raw = df[df["date"] == today].iloc[0]
    today_vector_json = {c: float(today_vec_raw[c]) for c in cols}

    with engine.begin() as con:
        con.execute(text(
            "DELETE FROM dashboard_analogues WHERE today_date = :d"
        ), {"d": today})

        for i, (_, row) in enumerate(top.iterrows()):
            a_date = row["date"]
            a_vec_raw = df[df["date"] == a_date].iloc[0]
            analogue_vector_json = {c: float(a_vec_raw[c]) for c in cols}

            # Forward returns
            a_price = spy.get(a_date)
            r30 = r90 = r180 = None
            if a_price:
                for offset, key in [(30, "r30"), (90, "r90"), (180, "r180")]:
                    target_date = a_date + timedelta(days=offset)
                    # Find nearest trading day
                    nearest = min(
                        (d for d in spy.keys() if d >= target_date),
                        default=None
                    )
                    if nearest and nearest <= a_date + timedelta(days=offset + 14):
                        if key == "r30":   r30  = (spy[nearest] / a_price - 1) * 100
                        if key == "r90":   r90  = (spy[nearest] / a_price - 1) * 100
                        if key == "r180":  r180 = (spy[nearest] / a_price - 1) * 100

            label = a_date.strftime("%b %Y")
            regime = classify_regime(a_vec_raw)

            con.execute(text("""
                INSERT INTO dashboard_analogues (
                    today_date, analogue_date, similarity_score,
                    today_vector_json, analogue_vector_json,
                    spx_return_30d, spx_return_90d, spx_return_180d,
                    analogue_label, regime_label, rank
                ) VALUES (
                    :td, :ad, :ss,
                    :tvj, :avj,
                    :r30, :r90, :r180,
                    :al, :rl, :rk
                )
            """), {
                "td": today, "ad": a_date, "ss": float(row["similarity"]),
                "tvj": json.dumps(_json_safe(today_vector_json)),
                "avj": json.dumps(_json_safe(analogue_vector_json)),
                "r30": r30, "r90": r90, "r180": r180,
                "al": label, "rl": regime, "rk": i + 1,
            })

    print(f"    {len(top)} analogues stored for {today}")


def classify_regime(row):
    """Quick regime label based on the vector."""
    yc = row.get("yield_curve_10y_2y", 0) or 0
    vix = row.get("vix_close", 20) or 20
    cs  = row.get("credit_spread_hy", 350) or 350
    if vix > 28 or cs > 600:
        return "Risk-off / stress"
    if yc < -0.2 and vix < 18:
        return "Late-cycle expansion"
    if yc > 1.0:
        return "Steepening / cyclical recovery"
    if vix < 13:
        return "Low-vol expansion"
    return "Mid-cycle"


# =============================================================================
# Step 5: Build Daily Sections (Today page cards) with Claude prose
# =============================================================================
SECTION_PROMPT = """You write daily market analysis for a finance product modeled
on Stratechery's hedged-observational tone. The audience is smart retail
investors. You are looking at TODAY'S market data and need to write ONE section
for the dashboard.

WRITING RULES (mandatory):
- Hedged-observational voice. "Conditions match X — historically that's preceded Y."
  Never "the market will" or "buy this."
- Headline: 6-12 words, sentence case, no clickbait, no emojis.
- Body: 2-3 short paragraphs, ~80-120 words total. Use specific numbers from the data.
- "Watch:" sentence: one observable condition that would change the analysis.
- Inline caveats for small samples: "n=4" or "small sample" if applicable.
- No bullet points. No bold. No markdown. Just paragraphs.

Return JSON only, no preamble:
{
  "headline": "...",
  "body": "...",
  "watch_signal": "Watch: ...",
  "confidence_score": <integer 0-100>,
  "historical_n": <integer or null>,
  "hit_rate_pct": <number or null>,
  "methodology_note": "<one-line explanation of how this was scored>"
}
"""


def call_claude_for_prose(client, section_type, data_context):
    """Call Claude to write one section based on the data context dict."""
    if not client:
        return None

    user_msg = (
        f"Section type: {section_type}\n\n"
        f"Data context:\n{json.dumps(_json_safe(data_context), default=str, indent=2)}\n\n"
        f"Write the section now."
    )

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            system=SECTION_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = "".join(b.text for b in resp.content if b.type == "text")
        # Strip code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        print(f"    prose generation failed for {section_type}: {e}")
        return None


def build_market_regime_section(client, today):
    """Section 1: Market Regime — characterize the current macro state."""
    print("  building Market Regime section...")
    with engine.connect() as con:
        # Get recent macro context
        mi = pd.read_sql(text("""
            SELECT * FROM market_indicators
            WHERE date >= CURRENT_DATE - 90
            ORDER BY date
        """), con)
        # Most recent rotation
        rot = pd.read_sql(text("""
            SELECT sector_a, sector_b, rotation_score
            FROM sector_relative_strength
            WHERE date = (SELECT MAX(date) FROM sector_relative_strength)
            ORDER BY rotation_score DESC
            LIMIT 5
        """), con)

    if mi.empty:
        return

    latest = mi.iloc[-1]
    prior  = mi.iloc[-21] if len(mi) >= 22 else mi.iloc[0]

    context = {
        "today": {
            "vix": float(latest.get("vix_close") or 0),
            "credit_spread_hy_bps": float(latest.get("credit_spread_hy") or 0),
            "copper_gold_ratio": float(latest.get("copper_gold_ratio") or 0),
            "yield_curve_10y_2y": float(latest.get("yield_curve_10y_2y") or 0),
            "dxy": float(latest.get("dxy") or 0),
            "put_call_ratio": float(latest.get("put_call_ratio") or 0) if latest.get("put_call_ratio") else None,
        },
        "20_day_changes": {
            "vix_chg": float((latest.get("vix_close") or 0) - (prior.get("vix_close") or 0)),
            "credit_spread_chg_bps": float((latest.get("credit_spread_hy") or 0) - (prior.get("credit_spread_hy") or 0)),
            "copper_gold_pct_chg": float(((latest.get("copper_gold_ratio") or 0) / (prior.get("copper_gold_ratio") or 1) - 1) * 100) if prior.get("copper_gold_ratio") else None,
        },
        "top_rotations": rot.to_dict(orient="records"),
    }

    prose = call_claude_for_prose(client, "market_regime", context)
    if not prose:
        print(f"    market regime prose generation returned None", flush=True)
        print(f"    context was: {json.dumps(_json_safe(context), default=str)[:500]}", flush=True)
        return
    print(f"    market regime headline: {prose.get('headline', '(none)')}", flush=True)

    with engine.begin() as con:
        con.execute(text("""
            INSERT INTO daily_dashboard_sections (
                section_date, section_type, section_order, overline,
                headline, body, watch_signal,
                confidence_score, historical_n, hit_rate_pct,
                metrics_json, methodology_note
            ) VALUES (
                :d, 'market_regime', 1, 'Market regime',
                :hl, :bd, :ws,
                :cs, :hn, :hr,
                :mj, :mn
            )
            ON CONFLICT (section_date, section_type) DO UPDATE SET
                headline=EXCLUDED.headline, body=EXCLUDED.body,
                watch_signal=EXCLUDED.watch_signal,
                confidence_score=EXCLUDED.confidence_score,
                historical_n=EXCLUDED.historical_n, hit_rate_pct=EXCLUDED.hit_rate_pct,
                metrics_json=EXCLUDED.metrics_json,
                methodology_note=EXCLUDED.methodology_note,
                created_at=NOW()
        """), {
            "d": today,
            "hl": prose.get("headline"),
            "bd": prose.get("body"),
            "ws": prose.get("watch_signal"),
            "cs": prose.get("confidence_score"),
            "hn": prose.get("historical_n"),
            "hr": prose.get("hit_rate_pct"),
            "mj": json.dumps(_json_safe(context)),
            "mn": prose.get("methodology_note"),
        })
    print(f"    written: {prose.get('headline')}")


def build_setup_of_day_section(client, today):
    """Section 2: Setup of the Day — highest-scoring single name."""
    print("  building Setup of the Day section...")
    with engine.connect() as con:
        # Try bullish first
        top = pd.read_sql(text("""
            SELECT * FROM dashboard_lists
            WHERE list_date = :d AND list_type = 'bullish'
            ORDER BY composite_score DESC LIMIT 1
        """), con, params={"d": today})
        if top.empty or top.iloc[0]["composite_score"] < 50:
            top = pd.read_sql(text("""
                SELECT * FROM dashboard_lists
                WHERE list_date = :d AND list_type = 'bearish'
                ORDER BY composite_score DESC LIMIT 1
            """), con, params={"d": today})
        if top.empty:
            print("    no setup of day candidates")
            return
        # log which we picked
        chosen_score = top.iloc[0]["composite_score"]
        print(f"    picking {top.iloc[0]['ticker']} with score {chosen_score}", flush=True)

        # Get S/R for context
        ticker = top.iloc[0]["ticker"]
        levels = pd.read_sql(text("""
            SELECT level_price, level_type, strength_tier,
                   touch_count, hold_rate, pct_distance_current
            FROM support_resistance_levels
            WHERE ticker = :t
              AND strength_tier IN ('major','moderate')
              AND calculated_date = (SELECT MAX(calculated_date)
                                     FROM support_resistance_levels
                                     WHERE ticker = :t)
            ORDER BY ABS(pct_distance_current)
            LIMIT 4
        """), con, params={"t": ticker})

    setup_row = top.iloc[0]
    list_type = setup_row.get("list_type", "bullish")
    context = {
        "list_type": list_type,
        "ticker": ticker,
        "company_name": setup_row.get("company_name"),
        "sector": setup_row.get("sector"),
        "current_price": float(setup_row["current_price"]) if pd.notnull(setup_row.get("current_price")) else None,
        "composite_score": float(setup_row["composite_score"]),
        "reason": setup_row.get("reason"),
        "components": (
            setup_row.get("components_json")
            if isinstance(setup_row.get("components_json"), dict)
            else (json.loads(setup_row.get("components_json") or "{}")
                  if setup_row.get("components_json") else {})
        ),
        "key_levels": levels.to_dict(orient="records") if not levels.empty else [],
    }

    prose = call_claude_for_prose(client, "setup_of_day", context)
    if not prose:
        print(f"    setup_of_day prose generation returned None", flush=True)
        return
    print(f"    setup headline: {prose.get('headline', '(none)')}", flush=True)

    levels_json = json.dumps(_json_safe(context["key_levels"]), default=str)

    with engine.begin() as con:
        con.execute(text("""
            INSERT INTO daily_dashboard_sections (
                section_date, section_type, section_order, overline,
                headline, body, watch_signal,
                confidence_score, primary_ticker, primary_sector,
                levels_json, metrics_json, methodology_note
            ) VALUES (
                :d, 'setup_of_day', 2, 'Setup of the day',
                :hl, :bd, :ws,
                :cs, :pt, :ps,
                :lj, :mj, :mn
            )
            ON CONFLICT (section_date, section_type) DO UPDATE SET
                headline=EXCLUDED.headline, body=EXCLUDED.body,
                watch_signal=EXCLUDED.watch_signal,
                confidence_score=EXCLUDED.confidence_score,
                primary_ticker=EXCLUDED.primary_ticker,
                primary_sector=EXCLUDED.primary_sector,
                levels_json=EXCLUDED.levels_json,
                metrics_json=EXCLUDED.metrics_json,
                methodology_note=EXCLUDED.methodology_note,
                created_at=NOW()
        """), {
            "d": today,
            "hl": prose.get("headline"),
            "bd": prose.get("body"),
            "ws": prose.get("watch_signal"),
            "cs": prose.get("confidence_score"),
            "pt": ticker,
            "ps": context["sector"],
            "lj": levels_json,
            "mj": json.dumps(_json_safe(context), default=str),
            "mn": prose.get("methodology_note"),
        })
    print(f"    written: {prose.get('headline')}")


def build_rotation_section(client, today):
    """Section 3: Rotation in Motion — pick top rotation."""
    print("  building Rotation in Motion section...")
    with engine.connect() as con:
        top = pd.read_sql(text("""
            SELECT sector_a, sector_b, rotation_score,
                   score_momentum, score_breadth, score_rsi,
                   score_volume, score_macro, signal
            FROM sector_relative_strength
            WHERE date = (SELECT MAX(date) FROM sector_relative_strength)
            ORDER BY rotation_score DESC LIMIT 1
        """), con)

    if top.empty:
        print("    no rotation data available")
        return
    rot_score = top.iloc[0]["rotation_score"] or 0
    print(f"    top rotation: {top.iloc[0]['sector_a']} into {top.iloc[0]['sector_b']} at {rot_score}", flush=True)
    if rot_score < 45:
        print("    rotation score below 45 — skipping section")
        return

    row = top.iloc[0]
    context = {
        "sector_into": row["sector_a"],
        "sector_out": row["sector_b"],
        "score": float(row["rotation_score"]),
        "components": {
            "momentum": float(row["score_momentum"] or 0),
            "breadth":  float(row["score_breadth"]  or 0),
            "rsi":      float(row["score_rsi"]      or 0),
            "volume":   float(row["score_volume"]   or 0),
            "macro":    float(row["score_macro"]    or 0),
        },
        "signal_strength": row["signal"],
    }
    prose = call_claude_for_prose(client, "rotation_in_motion", context)
    if not prose:
        print(f"    rotation prose generation returned None", flush=True)
        return
    print(f"    rotation headline: {prose.get('headline', '(none)')}", flush=True)

    with engine.begin() as con:
        con.execute(text("""
            INSERT INTO daily_dashboard_sections (
                section_date, section_type, section_order, overline,
                headline, body, watch_signal,
                confidence_score, primary_sector,
                metrics_json, methodology_note
            ) VALUES (
                :d, 'rotation_in_motion', 3, 'Rotation in motion',
                :hl, :bd, :ws,
                :cs, :ps,
                :mj, :mn
            )
            ON CONFLICT (section_date, section_type) DO UPDATE SET
                headline=EXCLUDED.headline, body=EXCLUDED.body,
                watch_signal=EXCLUDED.watch_signal,
                confidence_score=EXCLUDED.confidence_score,
                primary_sector=EXCLUDED.primary_sector,
                metrics_json=EXCLUDED.metrics_json,
                methodology_note=EXCLUDED.methodology_note,
                created_at=NOW()
        """), {
            "d": today,
            "hl": prose.get("headline"),
            "bd": prose.get("body"),
            "ws": prose.get("watch_signal"),
            "cs": prose.get("confidence_score"),
            "ps": row["sector_a"],
            "mj": json.dumps(_json_safe(context)),
            "mn": prose.get("methodology_note"),
        })
    print(f"    written: {prose.get('headline')}")


# =============================================================================
# Main entry point
# =============================================================================
def run_build(today=None):
    today = today or date.today()
    print("\n" + "=" * 60)
    print(f"Building dashboard for {today}")
    print("=" * 60)

    # Lists first — Setup of the Day depends on them
    build_lists(today)

    # Analogues for the Patterns page
    find_analogues(today)

    # Sections (prose via Claude)
    client = get_claude_client()
    if not client:
        print("  ANTHROPIC_API_KEY missing — skipping prose sections")
    else:
        build_market_regime_section(client, today)
        build_setup_of_day_section(client, today)
        build_rotation_section(client, today)

    print("\nDashboard build complete")


if __name__ == "__main__":
    run_build()
