"""
scenarios.py — The analytical core. Each function answers one class of question.
Add new scenarios here as you think of them. Each one takes a ticker plus
parameters and returns a dict with summary stats + per-instance detail.
"""
import duckdb
import pandas as pd
from datetime import timedelta

DB_PATH = "equity.duckdb"


def _conn():
    return duckdb.connect(DB_PATH, read_only=True)


# ---------------------------------------------------------------------------
# Scenario 1: Consecutive down periods
# ---------------------------------------------------------------------------
def consecutive_down_periods(
    ticker: str,
    n_periods: int = 3,
    freq: str = "W",  # "W" = weekly, "D" = daily, "M" = monthly
    lookforward: list = None,
) -> dict:
    """
    Find all instances where `ticker` has had `n_periods` consecutive lower
    closes at the given frequency, then measure forward returns.

    Returns: dict with summary stats and a list of instances.
    """
    if lookforward is None:
        lookforward = [1, 4, 13] if freq == "W" else [5, 20, 60]

    con = _conn()
    daily = con.execute(
        "SELECT date, close FROM daily_bars WHERE ticker = ? ORDER BY date",
        [ticker],
    ).df()
    con.close()

    if daily.empty:
        return {"error": f"No data for {ticker}"}

    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily.set_index("date")

    # Resample to weekly/monthly using last close of the period
    period = daily["close"].resample(freq).last().dropna()

    # Identify down periods and runs of length >= n_periods ending here
    is_down = period < period.shift(1)
    streak_lengths = []
    streak = 0
    for d in is_down:
        if d:
            streak += 1
        else:
            streak = 0
        streak_lengths.append(streak)
    period_df = pd.DataFrame({"close": period, "streak": streak_lengths})

    # Take non-overlapping signals: when streak hits n, record and skip ahead
    signals = []
    i = 0
    arr = period_df["streak"].values
    while i < len(arr):
        if arr[i] >= n_periods:
            signals.append(period_df.index[i])
            i += n_periods  # skip past this streak to avoid overlap
        else:
            i += 1

    # Compute forward returns
    rows = []
    period_index = period_df.index
    for sig_date in signals:
        sig_pos = period_index.get_loc(sig_date)
        base = period_df.loc[sig_date, "close"]
        for h in lookforward:
            future_pos = sig_pos + h
            if future_pos >= len(period_index):
                continue
            future_close = period_df.iloc[future_pos]["close"]
            rows.append({
                "signal_date": sig_date.strftime("%Y-%m-%d"),
                "horizon": h,
                "return_pct": round((future_close / base - 1) * 100, 2),
            })

    if not rows:
        return {"ticker": ticker, "n_signals": 0, "message": "No qualifying signals found."}

    results_df = pd.DataFrame(rows)
    summary = []
    for h in lookforward:
        sub = results_df[results_df["horizon"] == h]
        if sub.empty:
            continue
        summary.append({
            "horizon_periods": h,
            "n": len(sub),
            "mean_return_pct": round(sub["return_pct"].mean(), 2),
            "median_return_pct": round(sub["return_pct"].median(), 2),
            "pct_positive": round((sub["return_pct"] > 0).mean() * 100, 1),
        })

    return {
        "ticker": ticker,
        "scenario": f"{n_periods} consecutive down {freq}-periods",
        "n_signals": len(signals),
        "frequency": freq,
        "summary": summary,
        "instances": rows[:20],  # cap detail to keep response small
    }


# ---------------------------------------------------------------------------
# Scenario 2: Intraday drawdown recovery
# ---------------------------------------------------------------------------
def intraday_drawdown_recovery(ticker: str, threshold_pct: float = 5.0) -> dict:
    """
    Find all sessions where intraday low was >= threshold_pct below open,
    then measure how often the close was above the low (and by how much).
    """
    con = _conn()
    df = con.execute(
        """
        SELECT date, open, high, low, close
        FROM daily_bars
        WHERE ticker = ?
        ORDER BY date
        """,
        [ticker],
    ).df()
    con.close()

    if df.empty:
        return {"error": f"No data for {ticker}"}

    df["low_vs_open_pct"] = (df["low"] / df["open"] - 1) * 100
    df["close_vs_low_pct"] = (df["close"] / df["low"] - 1) * 100
    df["close_vs_open_pct"] = (df["close"] / df["open"] - 1) * 100

    triggered = df[df["low_vs_open_pct"] <= -threshold_pct].copy()

    if triggered.empty:
        return {
            "ticker": ticker,
            "threshold_pct": threshold_pct,
            "n_signals": 0,
            "message": f"No sessions found with intraday drop of {threshold_pct}%+ from open.",
        }

    closed_above_low = (triggered["close"] > triggered["low"]).sum()
    closed_green = (triggered["close"] > triggered["open"]).sum()

    instances = []
    for _, row in triggered.tail(15).iterrows():
        instances.append({
            "date": row["date"].strftime("%Y-%m-%d") if hasattr(row["date"], "strftime") else str(row["date"]),
            "low_vs_open_pct": round(row["low_vs_open_pct"], 2),
            "close_vs_low_pct": round(row["close_vs_low_pct"], 2),
            "close_vs_open_pct": round(row["close_vs_open_pct"], 2),
        })

    return {
        "ticker": ticker,
        "scenario": f"intraday drop ≥{threshold_pct}% from open",
        "n_signals": int(len(triggered)),
        "closed_above_low": int(closed_above_low),
        "pct_closed_above_low": round(closed_above_low / len(triggered) * 100, 1),
        "closed_green": int(closed_green),
        "pct_closed_green": round(closed_green / len(triggered) * 100, 1),
        "avg_recovery_from_low_pct": round(triggered["close_vs_low_pct"].mean(), 2),
        "avg_final_change_pct": round(triggered["close_vs_open_pct"].mean(), 2),
        "instances": instances,
    }


# ---------------------------------------------------------------------------
# Scenario 3: Post-earnings drop recovery
# ---------------------------------------------------------------------------
def post_earnings_drop_recovery(
    ticker: str,
    drop_threshold_pct: float = 5.0,
    recovery_threshold_pct: float = 15.0,
    max_days: int = 252,
) -> dict:
    """
    Find earnings events where the stock dropped >= drop_threshold_pct
    on the next trading day, then measure how many trading days until it
    recovered by recovery_threshold_pct from the post-earnings low.
    """
    con = _conn()
    bars = con.execute(
        "SELECT date, open, close FROM daily_bars WHERE ticker = ? ORDER BY date",
        [ticker],
    ).df()
    earnings = con.execute(
        "SELECT date FROM earnings_dates WHERE ticker = ? ORDER BY date",
        [ticker],
    ).df()
    con.close()

    if bars.empty:
        return {"error": f"No price data for {ticker}"}
    if earnings.empty:
        return {"error": f"No earnings data for {ticker}. yfinance doesn't always have it — try a more liquid name."}

    bars["date"] = pd.to_datetime(bars["date"])
    bars = bars.set_index("date").sort_index()
    earnings["date"] = pd.to_datetime(earnings["date"])

    events = []
    for ed in earnings["date"]:
        # Find the first trading day on or after the earnings date
        future_bars = bars[bars.index >= ed]
        if len(future_bars) < 2:
            continue
        day_before = bars[bars.index < ed]
        if day_before.empty:
            continue
        pre_close = day_before.iloc[-1]["close"]
        post_open_day = future_bars.iloc[0]
        post_close_day = future_bars.iloc[0]
        reaction_pct = (post_close_day["close"] / pre_close - 1) * 100

        if reaction_pct > -drop_threshold_pct:
            continue  # didn't drop enough

        # Walk forward to find the low and then time-to-recovery
        window = future_bars.head(max_days)
        post_low = window["close"].min()
        post_low_date = window["close"].idxmin()
        recovery_target = post_low * (1 + recovery_threshold_pct / 100)
        after_low = window[window.index >= post_low_date]
        recovery_hits = after_low[after_low["close"] >= recovery_target]

        if recovery_hits.empty:
            days_to_recover = None
        else:
            days_to_recover = (recovery_hits.index[0] - post_low_date).days

        events.append({
            "earnings_date": ed.strftime("%Y-%m-%d"),
            "reaction_pct": round(reaction_pct, 2),
            "post_low_date": post_low_date.strftime("%Y-%m-%d"),
            "days_low_to_recovery": days_to_recover,
            "recovered": days_to_recover is not None,
        })

    if not events:
        return {
            "ticker": ticker,
            "n_drops": 0,
            "message": f"No post-earnings drops of {drop_threshold_pct}%+ found.",
        }

    recovered_events = [e for e in events if e["recovered"]]
    return {
        "ticker": ticker,
        "scenario": f"earnings drop ≥{drop_threshold_pct}%, recovery threshold {recovery_threshold_pct}% from low",
        "n_drops": len(events),
        "n_recovered": len(recovered_events),
        "pct_recovered": round(len(recovered_events) / len(events) * 100, 1),
        "avg_days_to_recover": round(
            sum(e["days_low_to_recovery"] for e in recovered_events) / len(recovered_events), 1
        ) if recovered_events else None,
        "median_days_to_recover": int(
            pd.Series([e["days_low_to_recovery"] for e in recovered_events]).median()
        ) if recovered_events else None,
        "events": events,
    }


# Registry — used by app.py to expose these to Claude
SCENARIO_REGISTRY = {
    "consecutive_down_periods": consecutive_down_periods,
    "intraday_drawdown_recovery": intraday_drawdown_recovery,
    "post_earnings_drop_recovery": post_earnings_drop_recovery,
}
