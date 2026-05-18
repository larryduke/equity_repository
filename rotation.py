"""
rotation.py — Sector rotation detection engine.

Calculates:
  1. sector_indicators: daily aggregated technicals per sector
  2. sector_relative_strength: pairwise RS ratios + confidence scores

Called from refresh_data.py nightly after daily_bars is updated.
Can also be run standalone:
    python rotation.py

The confidence score model (0-100):
  Momentum (RS ratio trend)   25pts  — is A accelerating vs B?
  RSI divergence              20pts  — A rising, B falling from elevated levels
  Breadth divergence          20pts  — % above 50MA expanding in A, contracting in B
  Volume expansion            15pts  — avg rel_volume > 1.2 in A
  Macro alignment             20pts  — yield curve / rate direction favors A

Score interpretation:
  >= 65  Strong rotation signal into sector A
  45-64  Early / weak signal, worth watching
  < 45   Noise, no clear rotation

IMPORTANT: scores are based on historical signal correlations, not causal
prediction. Always shown with sample size and historical base rate.
"""
import os
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


def get_engine():
    return create_engine(DATABASE_URL, pool_pre_ping=True)


# Sectors we track for rotation analysis
TRACKED_SECTORS = [
    "Technology",
    "Financial Services",
    "Healthcare",
    "Industrials",
    "Consumer Cyclical",
    "Consumer Defensive",
    "Energy",
    "Utilities",
    "Real Estate",
    "Communication Services",
    "Basic Materials",
]

# Macro alignment rules: which macro conditions favor which sector
# Format: (condition_fn, favored_sector, opposed_sector, weight_multiplier)
MACRO_RULES = [
    # Steepening yield curve (10Y-2Y rising) favors financials
    ("yield_curve_rising",    "Financial Services", "Technology",        1.0),
    ("yield_curve_rising",    "Financial Services", "Consumer Cyclical", 0.7),
    # Falling rates favor growth / tech
    ("fed_funds_falling",     "Technology",         "Financial Services", 1.0),
    ("fed_funds_falling",     "Consumer Cyclical",  "Financial Services", 0.7),
    # High VIX (>25) favors defensives
    ("vix_elevated",          "Consumer Defensive", "Technology",         0.8),
    ("vix_elevated",          "Healthcare",         "Consumer Cyclical",  0.8),
    ("vix_elevated",          "Utilities",          "Technology",         0.7),
    # Low VIX (<15) favors risk-on
    ("vix_low",               "Technology",         "Utilities",          0.8),
    ("vix_low",               "Consumer Cyclical",  "Consumer Defensive", 0.7),
]


# =============================================================================
# Step 1: Calculate sector_indicators
# =============================================================================
def calc_sector_indicators(engine, lookback_days=400):
    """Aggregate daily_bars by sector for the last N days."""
    print("  calculating sector indicators...")

    sql = text("""
        SELECT
            d.date,
            t.sector,
            COUNT(DISTINCT d.ticker)                                    AS n_stocks,

            -- Momentum
            AVG(d.rsi_14)                                               AS avg_rsi_14,
            AVG(d.macd_histogram)                                       AS avg_macd_histogram,
            100.0 * AVG(CASE WHEN d.rsi_14 < 35  THEN 1.0 ELSE 0.0 END)
                FILTER (WHERE d.rsi_14 IS NOT NULL)                     AS pct_rsi_oversold,
            100.0 * AVG(CASE WHEN d.rsi_14 > 65  THEN 1.0 ELSE 0.0 END)
                FILTER (WHERE d.rsi_14 IS NOT NULL)                     AS pct_rsi_overbought,

            -- Trend
            100.0 * AVG(CASE WHEN d.close > d.ma_50  AND d.ma_50  IS NOT NULL
                              THEN 1.0 ELSE 0.0 END)                    AS pct_above_ma50,
            100.0 * AVG(CASE WHEN d.close > d.ma_200 AND d.ma_200 IS NOT NULL
                              THEN 1.0 ELSE 0.0 END)                    AS pct_above_ma200,
            AVG(d.pct_vs_ma50)                                          AS avg_pct_vs_ma50,
            AVG(d.pct_vs_ma200)                                         AS avg_pct_vs_ma200,

            -- Returns
            AVG(d.weekly_return)                                        AS avg_return_5d,
            AVG(d.monthly_return)                                       AS avg_return_20d,

            -- Volume
            AVG(d.rel_volume)                                           AS avg_rel_volume

        FROM daily_bars d
        JOIN tickers t ON t.ticker = d.ticker
        WHERE t.sector IS NOT NULL
          AND t.is_active = TRUE
          AND t.in_sp500 = TRUE
          AND d.date >= CURRENT_DATE - :lookback * INTERVAL '1 day'
        GROUP BY d.date, t.sector
        HAVING COUNT(DISTINCT d.ticker) >= 5
        ORDER BY d.date, t.sector
    """)

    with engine.connect() as con:
        df = pd.read_sql(sql, con, params={"lookback": lookback_days})

    if df.empty:
        print("    no sector data found — daily_bars may be empty")
        return df

    # Calculate 60-day return from rolling sector data
    df = df.sort_values(["sector", "date"])
    # We'll approximate 60d return by looking at avg_pct_vs_ma200 trend
    # True 60d return requires a self-join; we'll add it as a derived column later

    print(f"    {len(df)} sector-day rows across {df['sector'].nunique()} sectors")
    return df


def write_sector_indicators(engine, df):
    if df.empty:
        return
    cols = [
        "date", "sector", "n_stocks",
        "avg_rsi_14", "avg_macd_histogram", "pct_rsi_oversold", "pct_rsi_overbought",
        "pct_above_ma50", "pct_above_ma200", "avg_pct_vs_ma50", "avg_pct_vs_ma200",
        "avg_return_5d", "avg_return_20d", "avg_rel_volume",
    ]
    out = df[[c for c in cols if c in df.columns]].copy()
    out = out.where(pd.notnull(out), None)

    with engine.begin() as con:
        # Delete recent window and rewrite (cleaner than per-row upsert)
        con.execute(text("""
            DELETE FROM sector_indicators
            WHERE date >= CURRENT_DATE - INTERVAL '410 days'
        """))
        out.to_sql("sector_indicators", con, if_exists="append",
                   index=False, method="multi", chunksize=500)
    print(f"    wrote {len(out)} rows to sector_indicators")


# =============================================================================
# Step 2: Calculate rotation scores
# =============================================================================
def get_macro_conditions(engine):
    """Read the most recent macro indicators."""
    with engine.connect() as con:
        row = con.execute(text("""
            SELECT
                yield_curve_10y_2y,
                fed_funds_rate,
                vix_close,
                date
            FROM market_indicators
            WHERE yield_curve_10y_2y IS NOT NULL
               OR vix_close IS NOT NULL
            ORDER BY date DESC
            LIMIT 1
        """)).fetchone()
        # Also get yield curve trend (is it rising?)
        trend_row = con.execute(text("""
            SELECT
                AVG(yield_curve_10y_2y) FILTER (WHERE date >= CURRENT_DATE - 10)
                    AS recent_yc,
                AVG(yield_curve_10y_2y) FILTER (WHERE date BETWEEN
                    CURRENT_DATE - 40 AND CURRENT_DATE - 11)
                    AS prior_yc,
                AVG(fed_funds_rate) FILTER (WHERE date >= CURRENT_DATE - 10)
                    AS recent_ffr,
                AVG(fed_funds_rate) FILTER (WHERE date BETWEEN
                    CURRENT_DATE - 70 AND CURRENT_DATE - 11)
                    AS prior_ffr
            FROM market_indicators
            WHERE date >= CURRENT_DATE - 70
        """)).fetchone()

    conditions = set()
    if row:
        yc    = row[0]
        ffr   = row[1]
        vix   = row[2]
        if vix and vix > 25:
            conditions.add("vix_elevated")
        if vix and vix < 15:
            conditions.add("vix_low")

    if trend_row:
        recent_yc, prior_yc = trend_row[0], trend_row[1]
        recent_ffr, prior_ffr = trend_row[2], trend_row[3]
        if recent_yc and prior_yc and recent_yc > prior_yc:
            conditions.add("yield_curve_rising")
        if recent_yc and prior_yc and recent_yc < prior_yc:
            conditions.add("yield_curve_falling")
        if recent_ffr and prior_ffr and recent_ffr < prior_ffr:
            conditions.add("fed_funds_falling")
        if recent_ffr and prior_ffr and recent_ffr > prior_ffr:
            conditions.add("fed_funds_rising")

    return conditions


def calc_macro_score(sector_a, sector_b, macro_conditions):
    """Return 0-20 macro alignment score for rotating INTO sector_a FROM sector_b."""
    score = 0.0
    for condition, favored, opposed, weight in MACRO_RULES:
        if condition not in macro_conditions:
            continue
        if favored == sector_a and opposed == sector_b:
            score += 20 * weight
        elif favored == sector_b and opposed == sector_a:
            score -= 20 * weight
    return max(0.0, min(20.0, score))


def calc_rotation_scores(si_df, macro_conditions):
    """
    Given sector_indicators DataFrame, compute pairwise rotation scores
    for the most recent date.

    Returns DataFrame with one row per (sector_a, sector_b) pair.
    """
    if si_df.empty:
        return pd.DataFrame()

    latest_date = si_df["date"].max()
    # Use last 60 days of sector data for trend calculations
    recent = si_df[si_df["date"] >= latest_date - pd.Timedelta(days=65)].copy()
    today  = si_df[si_df["date"] == latest_date].copy()

    if today.empty:
        return pd.DataFrame()

    today = today.set_index("sector")
    sectors = [s for s in today.index if s in TRACKED_SECTORS]

    rows = []
    for sector_a in sectors:
        for sector_b in sectors:
            if sector_a == sector_b:
                continue

            a = today.loc[sector_a]
            b = today.loc[sector_b]

            # ── Momentum score (25 pts) ──────────────────────────────
            # RS ratio trend: is sector_a's 20d return > sector_b's?
            ret_a = a.get("avg_return_20d") or 0
            ret_b = b.get("avg_return_20d") or 0
            rs_20d = (ret_a - ret_b)  # simple difference in %

            # Trend of RS over last 20 days
            a_series = (recent[recent["sector"] == sector_a]
                        .sort_values("date")["avg_return_20d"].dropna())
            b_series = (recent[recent["sector"] == sector_b]
                        .sort_values("date")["avg_return_20d"].dropna())

            rs_trend = 0.0
            if len(a_series) >= 10 and len(b_series) >= 10:
                min_len = min(len(a_series), len(b_series))
                rs_series = a_series.values[-min_len:] - b_series.values[-min_len:]
                if len(rs_series) >= 5:
                    x = np.arange(len(rs_series))
                    slope = np.polyfit(x, rs_series, 1)[0]
                    # Normalise: slope of 0.1 per day = full 25 pts
                    rs_trend = float(np.clip(slope / 0.1, -1, 1))

            score_momentum = max(0.0, 12.5 + rs_trend * 12.5)  # 0-25

            # ── RSI divergence score (20 pts) ────────────────────────
            rsi_a = a.get("avg_rsi_14") or 50
            rsi_b = b.get("avg_rsi_14") or 50
            rsi_diff = rsi_a - rsi_b  # positive = A more bullish momentum
            # Max divergence ~30 points → full score
            score_rsi = max(0.0, min(20.0, 10.0 + (rsi_diff / 30.0) * 10.0))

            # ── Breadth divergence score (20 pts) ────────────────────
            breadth_a = a.get("pct_above_ma50") or 50
            breadth_b = b.get("pct_above_ma50") or 50
            breadth_diff = breadth_a - breadth_b
            score_breadth = max(0.0, min(20.0, 10.0 + (breadth_diff / 40.0) * 10.0))

            # ── Volume expansion score (15 pts) ──────────────────────
            rvol_a = a.get("avg_rel_volume") or 1.0
            rvol_b = b.get("avg_rel_volume") or 1.0
            # A expanding (>1.1) and B contracting (<0.9) = full score
            vol_score_raw = (rvol_a - 1.0) - (rvol_b - 1.0)
            score_volume = max(0.0, min(15.0, 7.5 + vol_score_raw * 15.0))

            # ── Macro alignment score (20 pts) ───────────────────────
            score_macro = calc_macro_score(sector_a, sector_b, macro_conditions)

            # ── Total ─────────────────────────────────────────────────
            total = score_momentum + score_rsi + score_breadth + score_volume + score_macro

            # Signal label
            if total >= 65:
                signal = "strong_into_a"
            elif total >= 50:
                signal = "early_into_a"
            elif total <= 35:
                signal = "strong_into_b"
            elif total <= 50 and total > 35:
                signal = "early_into_b"
            else:
                signal = "neutral"

            rows.append({
                "date":           latest_date,
                "sector_a":       sector_a,
                "sector_b":       sector_b,
                "rs_ratio_5d":    round((a.get("avg_return_5d") or 0) -
                                        (b.get("avg_return_5d") or 0), 3),
                "rs_ratio_20d":   round(rs_20d, 3),
                "rs_ratio_60d":   None,
                "rs_trend_20d":   round(rs_trend, 3),
                "rotation_score": round(total, 1),
                "signal":         signal,
                "score_momentum": round(score_momentum, 1),
                "score_breadth":  round(score_breadth, 1),
                "score_rsi":      round(score_rsi, 1),
                "score_volume":   round(score_volume, 1),
                "score_macro":    round(score_macro, 1),
            })

    return pd.DataFrame(rows)


def write_rotation_scores(engine, df):
    if df.empty:
        return
    df = df.where(pd.notnull(df), None)
    with engine.begin() as con:
        con.execute(text("""
            DELETE FROM sector_relative_strength
            WHERE date >= CURRENT_DATE - INTERVAL '2 days'
        """))
        df.to_sql("sector_relative_strength", con, if_exists="append",
                  index=False, method="multi", chunksize=500)
    print(f"    wrote {len(df)} rotation scores")


# =============================================================================
# Main entry point (called from refresh_data.py or standalone)
# =============================================================================
def run_rotation_refresh(engine=None):
    if engine is None:
        engine = get_engine()

    print("\nRefreshing rotation signals...")

    # Step 1: sector indicators
    si_df = calc_sector_indicators(engine)
    if si_df.empty:
        print("  no data yet — skipping rotation scores")
        return
    write_sector_indicators(engine, si_df)

    # Step 2: macro conditions
    macro_conditions = get_macro_conditions(engine)
    print(f"  macro conditions: {macro_conditions or 'none detected'}")

    # Step 3: rotation scores
    si_df["date"] = pd.to_datetime(si_df["date"])
    scores_df = calc_rotation_scores(si_df, macro_conditions)
    write_rotation_scores(engine, scores_df)

    # Print top signals for today
    if not scores_df.empty:
        top = (scores_df[scores_df["rotation_score"] >= 50]
               .sort_values("rotation_score", ascending=False)
               .head(5))
        if not top.empty:
            print("\n  Top rotation signals today:")
            for _, row in top.iterrows():
                print(f"    {row['sector_a']:25s} vs {row['sector_b']:25s} "
                      f"score={row['rotation_score']:.0f} [{row['signal']}]")
        else:
            print("  No strong rotation signals today (all scores < 50)")

    print("  rotation refresh complete")


if __name__ == "__main__":
    run_rotation_refresh()
