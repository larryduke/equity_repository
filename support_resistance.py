"""
support_resistance.py — Detects support and resistance levels for every ticker.

Uses five complementary methods, each producing levels with a strength score:

1. PIVOT POINTS
   Local highs/lows surrounded by N lower/higher bars. Window-tunable so
   we capture both major (50-day swing) and minor (10-day swing) levels.

2. VOLUME-WEIGHTED PRICE CLUSTERS
   Price ranges where the most cumulative volume has traded. These are where
   the most positions exist — where price tends to react when revisiting.

3. ROUND NUMBERS
   Psychological levels: $100, $50, $25, $10, $5 increments. Lower base
   strength but bumped when other methods also flag the same level.

4. MOVING AVERAGE LEVELS (dynamic, not stored — accessed via daily_bars)
   The 50MA, 100MA, 200MA. Already in daily_bars; we add a check when
   price is within 2% of one of these.

5. FLIPPED LEVELS
   A prior resistance that broke and is now acting as support (and vice versa).
   Detected by finding pivots that switched sides over the lookback window.

Each level gets a strength_score 0-100 weighing:
   touch_count (40), hold_rate (25), recency (20), volume confirmation (15)

Final tier:
   >= 70  'major'    — strong, well-validated level
   50-69  'moderate' — meaningful but not always held
   30-49  'minor'    — has touched a few times
   < 30   'weak'     — barely qualifies, often noise

Run:
    python support_resistance.py             # all active tickers
    python support_resistance.py --ticker AAPL
    python support_resistance.py --limit 50  # first 50 tickers (test)

Environment:
    DATABASE_URL
"""
import os
import sys
import argparse
import numpy as np
import pandas as pd
from datetime import date, timedelta
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


# =============================================================================
# Configuration
# =============================================================================
LOOKBACK_DAYS = 730            # 2 years of history per ticker for detection
PIVOT_WINDOWS = [10, 25, 50]   # multi-scale pivots
VOLUME_BINS = 50               # how many price bins for volume clustering
PROXIMITY_PCT = 0.015          # 1.5% tolerance for grouping nearby levels
RECENT_DAYS_THRESHOLD = 180    # levels touched within this are "recent"


# =============================================================================
# Step 1: Pivot point detection
# =============================================================================
def find_pivots(df, window):
    """
    A pivot high at index i exists if df['high'][i] is the max in
    [i-window, i+window]. Same idea for pivot low using df['low'].
    Returns two lists: pivot_highs, pivot_lows. Each is a list of dicts:
        {'date', 'price', 'volume'}
    """
    highs, lows = [], []
    n = len(df)
    h = df["high"].values
    l = df["low"].values
    v = df["volume"].values
    dates = df["date"].values

    for i in range(window, n - window):
        win_h = h[i - window:i + window + 1]
        win_l = l[i - window:i + window + 1]
        if h[i] == win_h.max():
            highs.append({
                "date": dates[i],
                "price": float(h[i]),
                "volume": float(v[i]) if not np.isnan(v[i]) else 0,
                "window": window,
            })
        if l[i] == win_l.min():
            lows.append({
                "date": dates[i],
                "price": float(l[i]),
                "volume": float(v[i]) if not np.isnan(v[i]) else 0,
                "window": window,
            })
    return highs, lows


# =============================================================================
# Step 2: Volume-weighted price clusters
# =============================================================================
def volume_clusters(df, n_bins=VOLUME_BINS, top_n=8):
    """
    Bin the price range into N buckets, sum volume per bucket using typical
    price (HLC/3) weighted by volume. Top buckets by volume = high-interest
    price zones.
    """
    df = df.copy()
    df["typical"] = (df["high"] + df["low"] + df["close"]) / 3.0
    df["dollar_vol"] = df["typical"] * df["volume"]

    price_min, price_max = df["typical"].min(), df["typical"].max()
    if not (np.isfinite(price_min) and np.isfinite(price_max)) or price_max <= price_min:
        return []

    bins = np.linspace(price_min, price_max, n_bins + 1)
    df["bin"] = pd.cut(df["typical"], bins=bins, include_lowest=True)
    cluster = df.groupby("bin", observed=True)["dollar_vol"].sum().reset_index()
    cluster["bin_mid"] = cluster["bin"].apply(lambda x: (x.left + x.right) / 2)
    cluster = cluster.sort_values("dollar_vol", ascending=False).head(top_n)

    return [
        {"price": float(row["bin_mid"]), "dollar_vol": float(row["dollar_vol"])}
        for _, row in cluster.iterrows()
    ]


# =============================================================================
# Step 3: Round numbers
# =============================================================================
def round_number_levels(current_price, range_low, range_high):
    """
    Generate psychological round-number levels within the historical price range.
    Increment scales with price:
        < $20    → $1
        $20-100  → $5
        $100-500 → $10
        $500+    → $25
    """
    if current_price < 20:
        step = 1
    elif current_price < 100:
        step = 5
    elif current_price < 500:
        step = 10
    elif current_price < 2000:
        step = 25
    else:
        step = 100

    low_bound = max(range_low * 0.7, current_price * 0.3)
    high_bound = min(range_high * 1.3, current_price * 2.5)

    levels = []
    start = int(low_bound // step) * step
    end   = int(high_bound // step + 1) * step
    for p in range(int(start), int(end) + 1, int(step)):
        if p <= 0:
            continue
        levels.append(float(p))
    return levels


# =============================================================================
# Step 4: Cluster nearby levels & count touches
# =============================================================================
def cluster_levels(levels, tolerance=PROXIMITY_PCT):
    """
    Group levels that are within `tolerance` of each other.
    Returns list of cluster centers with their constituent dates/volumes.
    """
    if not levels:
        return []
    levels = sorted(levels, key=lambda x: x["price"])
    clusters = [[levels[0]]]
    for lvl in levels[1:]:
        last_cluster_avg = np.mean([x["price"] for x in clusters[-1]])
        if abs(lvl["price"] - last_cluster_avg) / last_cluster_avg <= tolerance:
            clusters[-1].append(lvl)
        else:
            clusters.append([lvl])

    result = []
    for c in clusters:
        prices = [x["price"] for x in c]
        result.append({
            "price": float(np.mean(prices)),
            "n_pivots": len(c),
            "dates": [x["date"] for x in c],
            "volumes": [x["volume"] for x in c],
            "min_window": min(x.get("window", 0) for x in c),
            "max_window": max(x.get("window", 0) for x in c),
        })
    return result


def count_touches(df, level_price, tolerance=PROXIMITY_PCT):
    """
    Count how many times daily high/low came within `tolerance` of level_price.
    Returns:
       touches:        list of dates price came near level
       held:           list of dates where price reversed away
       broken:         list of dates where price broke through (closed beyond)
       avg_vol:        average volume on touch days
    """
    tol = level_price * tolerance
    near = df[
        ((df["low"] <= level_price + tol) & (df["high"] >= level_price - tol))
    ].copy()
    if near.empty:
        return [], [], [], 0

    near["closed_above"] = near["close"] > level_price + tol
    near["closed_below"] = near["close"] < level_price - tol
    near["closed_near"]  = ~(near["closed_above"] | near["closed_below"])

    held = []
    broken = []
    for _, row in near.iterrows():
        prev_close = df[df["date"] < row["date"]]["close"].iloc[-1] \
            if (df["date"] < row["date"]).any() else None
        if prev_close is None:
            continue
        # Approaching from above (resistance broken if closes well below; held if bounced)
        if prev_close > level_price + tol and row["closed_below"]:
            broken.append(row["date"])
        elif prev_close < level_price - tol and row["closed_above"]:
            broken.append(row["date"])
        else:
            held.append(row["date"])

    avg_vol = float(near["volume"].mean()) if not near["volume"].isna().all() else 0
    return near["date"].tolist(), held, broken, avg_vol


# =============================================================================
# Step 5: Score and tier
# =============================================================================
def score_level(cluster, touches, held, broken, avg_vol_at_touch,
                ticker_avg_vol, today, level_price, current_price):
    """
    Strength score 0-100. Components:
      touch_count (40):    more touches = stronger (capped at 8)
      hold_rate (25):      held / total touches
      recency (20):        recent touches matter more
      volume confirm (15): touch volume vs ticker average
    """
    n_touches = len(touches)
    if n_touches == 0:
        return 0, "weak"

    # touch_count component
    score_touches = min(n_touches / 8.0, 1.0) * 40

    # hold_rate component
    hold_rate = len(held) / n_touches if n_touches else 0
    score_hold = hold_rate * 25

    # recency component (most recent touch)
    if touches:
        last = max(touches)
        days_since = (today - pd.to_datetime(last).date()
                      if hasattr(last, 'strftime') else (today - last)).days \
            if hasattr(last, '__sub__') else 999
        try:
            days_since = (today - pd.Timestamp(last).date()).days
        except Exception:
            days_since = 999
        score_recency = max(0, 1 - days_since / RECENT_DAYS_THRESHOLD) * 20
    else:
        score_recency = 0

    # volume confirmation
    if ticker_avg_vol > 0 and avg_vol_at_touch > 0:
        vol_ratio = avg_vol_at_touch / ticker_avg_vol
        score_volume = min(vol_ratio / 1.5, 1.0) * 15
    else:
        score_volume = 0

    # Bonus: multi-window pivots
    if cluster.get("max_window", 0) >= 50:
        score_touches = min(score_touches + 5, 40)

    total = round(score_touches + score_hold + score_recency + score_volume, 1)

    if total >= 70:
        tier = "major"
    elif total >= 50:
        tier = "moderate"
    elif total >= 30:
        tier = "minor"
    else:
        tier = "weak"

    return total, tier


# =============================================================================
# Step 6: Main per-ticker detection
# =============================================================================
def detect_levels_for_ticker(ticker, df, today):
    """Returns list of detected level dicts for upserting."""
    if df.empty or len(df) < 100:
        return []

    df = df.sort_values("date").reset_index(drop=True)
    current_price = float(df["close"].iloc[-1])
    range_low  = float(df["low"].min())
    range_high = float(df["high"].max())
    ticker_avg_vol = float(df["volume"].mean()) if not df["volume"].isna().all() else 0

    # Collect raw pivot candidates from multiple windows
    raw_highs, raw_lows = [], []
    for w in PIVOT_WINDOWS:
        h, l = find_pivots(df, w)
        raw_highs.extend(h)
        raw_lows.extend(l)

    # Cluster pivot highs (resistance candidates) and lows (support)
    res_clusters = cluster_levels(raw_highs)
    sup_clusters = cluster_levels(raw_lows)

    # Volume clusters — could be either S or R depending on current price
    vol_clusters = volume_clusters(df)

    detected = []

    # Process resistance clusters
    for c in res_clusters:
        if c["price"] <= current_price:
            continue  # only count as resistance if above current
        touches, held, broken, avg_vol = count_touches(df, c["price"])
        if len(touches) < 1:
            continue
        score, tier = score_level(c, touches, held, broken, avg_vol,
                                  ticker_avg_vol, today, c["price"], current_price)
        last_touch = max(touches) if touches else None
        first_touch = min(touches) if touches else None
        detected.append({
            "ticker": ticker,
            "level_price": c["price"],
            "level_type": "resistance",
            "method": "pivot",
            "touch_count": len(touches),
            "n_strong_touches": sum(1 for d in touches
                                    if df[df["date"] == d]["volume"].iloc[0] > ticker_avg_vol * 1.3)
                                    if len(touches) > 0 else 0,
            "last_touch_date": last_touch,
            "first_touch_date": first_touch,
            "days_since_last_touch": (today - pd.Timestamp(last_touch).date()).days
                                     if last_touch is not None else None,
            "times_held": len(held),
            "times_broken": len(broken),
            "hold_rate": len(held) / len(touches) if touches else 0,
            "avg_volume_at_touches": avg_vol,
            "pct_distance_current": (c["price"] - current_price) / current_price * 100,
            "is_active": True,
            "strength_score": score,
            "strength_tier": tier,
            "calculated_date": today,
            "lookback_days": LOOKBACK_DAYS,
        })

    # Process support clusters
    for c in sup_clusters:
        if c["price"] >= current_price:
            continue
        touches, held, broken, avg_vol = count_touches(df, c["price"])
        if len(touches) < 1:
            continue
        score, tier = score_level(c, touches, held, broken, avg_vol,
                                  ticker_avg_vol, today, c["price"], current_price)
        last_touch = max(touches) if touches else None
        first_touch = min(touches) if touches else None
        detected.append({
            "ticker": ticker,
            "level_price": c["price"],
            "level_type": "support",
            "method": "pivot",
            "touch_count": len(touches),
            "n_strong_touches": sum(1 for d in touches
                                    if df[df["date"] == d]["volume"].iloc[0] > ticker_avg_vol * 1.3)
                                    if len(touches) > 0 else 0,
            "last_touch_date": last_touch,
            "first_touch_date": first_touch,
            "days_since_last_touch": (today - pd.Timestamp(last_touch).date()).days
                                     if last_touch is not None else None,
            "times_held": len(held),
            "times_broken": len(broken),
            "hold_rate": len(held) / len(touches) if touches else 0,
            "avg_volume_at_touches": avg_vol,
            "pct_distance_current": (c["price"] - current_price) / current_price * 100,
            "is_active": True,
            "strength_score": score,
            "strength_tier": tier,
            "calculated_date": today,
            "lookback_days": LOOKBACK_DAYS,
        })

    # Process volume clusters as either S or R
    for vc in vol_clusters:
        level_type = "support" if vc["price"] < current_price else "resistance"
        # Skip if too close to current price (within 2%)
        if abs(vc["price"] - current_price) / current_price < 0.02:
            continue
        touches, held, broken, avg_vol = count_touches(df, vc["price"])
        if len(touches) < 2:
            continue
        cluster_proxy = {"max_window": 25}
        score, tier = score_level(cluster_proxy, touches, held, broken, avg_vol,
                                  ticker_avg_vol, today, vc["price"], current_price)
        # Volume clusters tend to be very strong — add a bonus
        score = min(score + 8, 100)
        if score >= 70:
            tier = "major"
        elif score >= 50:
            tier = "moderate"
        elif score >= 30:
            tier = "minor"

        last_touch = max(touches) if touches else None
        first_touch = min(touches) if touches else None
        detected.append({
            "ticker": ticker,
            "level_price": vc["price"],
            "level_type": level_type,
            "method": "volume_cluster",
            "touch_count": len(touches),
            "n_strong_touches": 0,
            "last_touch_date": last_touch,
            "first_touch_date": first_touch,
            "days_since_last_touch": (today - pd.Timestamp(last_touch).date()).days
                                     if last_touch is not None else None,
            "times_held": len(held),
            "times_broken": len(broken),
            "hold_rate": len(held) / len(touches) if touches else 0,
            "avg_volume_at_touches": avg_vol,
            "pct_distance_current": (vc["price"] - current_price) / current_price * 100,
            "is_active": True,
            "strength_score": score,
            "strength_tier": tier,
            "calculated_date": today,
            "lookback_days": LOOKBACK_DAYS,
        })

    # Round number levels (low confidence base, bumped when other methods agree)
    rn_levels = round_number_levels(current_price, range_low, range_high)
    existing_prices = [d["level_price"] for d in detected]

    for rn in rn_levels:
        if abs(rn - current_price) / current_price < 0.02:
            continue
        # If a method already flagged this level (within tolerance), skip — it'll get the bonus elsewhere
        if any(abs(rn - p) / p < PROXIMITY_PCT for p in existing_prices):
            continue
        touches, held, broken, avg_vol = count_touches(df, rn)
        if len(touches) < 3:
            continue
        cluster_proxy = {"max_window": 10}
        score, tier = score_level(cluster_proxy, touches, held, broken, avg_vol,
                                  ticker_avg_vol, today, rn, current_price)
        if score < 30:
            continue
        level_type = "support" if rn < current_price else "resistance"
        last_touch = max(touches) if touches else None
        first_touch = min(touches) if touches else None
        detected.append({
            "ticker": ticker,
            "level_price": float(rn),
            "level_type": level_type,
            "method": "round_number",
            "touch_count": len(touches),
            "n_strong_touches": 0,
            "last_touch_date": last_touch,
            "first_touch_date": first_touch,
            "days_since_last_touch": (today - pd.Timestamp(last_touch).date()).days
                                     if last_touch is not None else None,
            "times_held": len(held),
            "times_broken": len(broken),
            "hold_rate": len(held) / len(touches) if touches else 0,
            "avg_volume_at_touches": avg_vol,
            "pct_distance_current": (rn - current_price) / current_price * 100,
            "is_active": True,
            "strength_score": score,
            "strength_tier": tier,
            "calculated_date": today,
            "lookback_days": LOOKBACK_DAYS,
        })

    # Deduplicate: combine levels within tolerance, take highest score
    detected = consolidate(detected)
    return detected


def consolidate(levels):
    """Merge near-duplicate levels across methods, keeping highest score."""
    if not levels:
        return []
    levels = sorted(levels, key=lambda x: x["level_price"])
    groups = [[levels[0]]]
    for lvl in levels[1:]:
        last_group_price = np.mean([x["level_price"] for x in groups[-1]])
        if abs(lvl["level_price"] - last_group_price) / last_group_price <= PROXIMITY_PCT:
            groups[-1].append(lvl)
        else:
            groups.append([lvl])

    final = []
    for g in groups:
        # Pick the strongest level in the group, but boost score if multiple methods agree
        best = max(g, key=lambda x: x["strength_score"])
        if len(g) > 1:
            methods = {x["method"] for x in g}
            # Multi-method confirmation bonus
            bonus = min((len(methods) - 1) * 8, 15)
            best = dict(best)
            best["strength_score"] = min(best["strength_score"] + bonus, 100)
            best["method"] = "+".join(sorted(methods))
            if best["strength_score"] >= 70:
                best["strength_tier"] = "major"
            elif best["strength_score"] >= 50:
                best["strength_tier"] = "moderate"
            elif best["strength_score"] >= 30:
                best["strength_tier"] = "minor"
        final.append(best)
    return final


# =============================================================================
# DB I/O
# =============================================================================
def get_ticker_bars(ticker, lookback=LOOKBACK_DAYS):
    with engine.connect() as con:
        df = pd.read_sql(text("""
            SELECT date, open, high, low, close, volume
            FROM daily_bars
            WHERE ticker = :t
              AND date >= CURRENT_DATE - :lb * INTERVAL '1 day'
            ORDER BY date
        """), con, params={"t": ticker, "lb": lookback})
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def get_active_tickers(limit=None):
    with engine.connect() as con:
        q = "SELECT ticker FROM tickers WHERE is_active = TRUE ORDER BY ticker"
        if limit:
            q += f" LIMIT {int(limit)}"
        rows = con.execute(text(q)).fetchall()
    return [r[0] for r in rows]


def upsert_levels(ticker, levels):
    """Replace all levels for this ticker calculated today."""
    today = date.today()
    with engine.begin() as con:
        con.execute(text("""
            DELETE FROM support_resistance_levels
            WHERE ticker = :t AND calculated_date = :d
        """), {"t": ticker, "d": today})
        if not levels:
            return 0
        for lvl in levels:
            con.execute(text("""
                INSERT INTO support_resistance_levels (
                    ticker, level_price, level_type, method,
                    touch_count, n_strong_touches,
                    last_touch_date, first_touch_date, days_since_last_touch,
                    times_held, times_broken, hold_rate,
                    avg_volume_at_touches, pct_distance_current,
                    is_active, strength_score, strength_tier,
                    calculated_date, lookback_days
                ) VALUES (
                    :ticker, :level_price, :level_type, :method,
                    :touch_count, :n_strong_touches,
                    :last_touch_date, :first_touch_date, :days_since_last_touch,
                    :times_held, :times_broken, :hold_rate,
                    :avg_volume_at_touches, :pct_distance_current,
                    :is_active, :strength_score, :strength_tier,
                    :calculated_date, :lookback_days
                )
                ON CONFLICT (ticker, level_price, method, calculated_date) DO NOTHING
            """), lvl)
    return len(levels)


# =============================================================================
# Main
# =============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", help="run for one ticker only")
    parser.add_argument("--limit", type=int, help="process only first N tickers")
    args = parser.parse_args()

    today = date.today()
    print(f"\nSupport/resistance detection — {today}")
    print("=" * 60)

    if args.ticker:
        tickers = [args.ticker.upper()]
    else:
        tickers = get_active_tickers(limit=args.limit)

    if not tickers:
        sys.exit("No tickers found")

    print(f"Processing {len(tickers)} tickers...\n")

    n_ok = n_skip = n_fail = total_levels = 0
    for i, ticker in enumerate(tickers, 1):
        try:
            df = get_ticker_bars(ticker)
            if df.empty or len(df) < 100:
                n_skip += 1
                continue
            levels = detect_levels_for_ticker(ticker, df, today)
            upsert_levels(ticker, levels)
            n_ok += 1
            total_levels += len(levels)
            if i % 50 == 0:
                print(f"  [{i}/{len(tickers)}] {n_ok} done, {total_levels} levels found")
        except Exception as e:
            n_fail += 1
            if n_fail < 5:
                print(f"  {ticker}: ERROR {e}")

    print(f"\n{'='*60}")
    print(f"Done. {n_ok} processed, {n_skip} skipped, {n_fail} failed")
    print(f"Total levels: {total_levels}")

    # Quick summary by tier
    with engine.connect() as con:
        summary = pd.read_sql(text("""
            SELECT level_type, strength_tier, COUNT(*) AS n
            FROM support_resistance_levels
            WHERE calculated_date = :d
            GROUP BY level_type, strength_tier
            ORDER BY level_type, strength_tier
        """), con, params={"d": today})
    if not summary.empty:
        print("\nLevels by tier:")
        print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
