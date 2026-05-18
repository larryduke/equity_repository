"""
scenarios.py — Analytical core. Each function queries the Supabase Postgres
database and returns a dict of summary stats + per-instance detail.

Add new scenarios here as you think of them. Register each in SCENARIO_REGISTRY
at the bottom, then add a tool definition for it in app.py.
"""
import os
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text


def _get_database_url():
    """Read DATABASE_URL from env or Streamlit Secrets."""
    url = os.getenv("DATABASE_URL")
    if not url:
        try:
            url = st.secrets["DATABASE_URL"]
        except Exception:
            pass
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


# Cached engine — connection pool reused across queries
_engine = None
def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(_get_database_url(), pool_pre_ping=True)
    return _engine


# ---------------------------------------------------------------------------
# Scenario 1: Consecutive down periods
# ---------------------------------------------------------------------------
def consecutive_down_periods(
    ticker: str,
    n_periods: int = 3,
    freq: str = "W",
    lookforward: list = None,
) -> dict:
    """Find instances of N consecutive lower closes at given frequency,
    measure forward returns."""
    if lookforward is None:
        lookforward = [1, 4, 13] if freq == "W" else [5, 20, 60]

    engine = _get_engine()
    with engine.connect() as con:
        daily = pd.read_sql(
            text("SELECT date, close FROM daily_bars WHERE ticker = :t ORDER BY date"),
            con, params={"t": ticker.upper()},
        )

    if daily.empty:
        return {"error": f"No data for {ticker}"}

    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily.set_index("date")
    period = daily["close"].resample(freq).last().dropna()

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

    # Non-overlapping signals
    signals = []
    i = 0
    arr = period_df["streak"].values
    while i < len(arr):
        if arr[i] >= n_periods:
            signals.append(period_df.index[i])
            i += n_periods
        else:
            i += 1

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
        "instances": rows[:20],
    }


# ---------------------------------------------------------------------------
# Scenario 2: Intraday drawdown recovery
# ---------------------------------------------------------------------------
def intraday_drawdown_recovery(ticker: str, threshold_pct: float = 5.0) -> dict:
    engine = _get_engine()
    with engine.connect() as con:
        df = pd.read_sql(
            text("SELECT date, open, high, low, close FROM daily_bars WHERE ticker = :t ORDER BY date"),
            con, params={"t": ticker.upper()},
        )

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
            "message": f"No sessions with intraday drop of {threshold_pct}%+ from open.",
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
    engine = _get_engine()
    with engine.connect() as con:
        bars = pd.read_sql(
            text("SELECT date, open, close FROM daily_bars WHERE ticker = :t ORDER BY date"),
            con, params={"t": ticker.upper()},
        )
        earnings = pd.read_sql(
            text("SELECT date FROM earnings_dates WHERE ticker = :t ORDER BY date"),
            con, params={"t": ticker.upper()},
        )

    if bars.empty:
        return {"error": f"No price data for {ticker}"}
    if earnings.empty:
        return {"error": f"No earnings data for {ticker}. yfinance may not have it for this ticker."}

    bars["date"] = pd.to_datetime(bars["date"])
    bars = bars.set_index("date").sort_index()
    earnings["date"] = pd.to_datetime(earnings["date"])

    events = []
    for ed in earnings["date"]:
        future_bars = bars[bars.index >= ed]
        if len(future_bars) < 2:
            continue
        day_before = bars[bars.index < ed]
        if day_before.empty:
            continue
        pre_close = day_before.iloc[-1]["close"]
        post_close_day = future_bars.iloc[0]
        reaction_pct = (post_close_day["close"] / pre_close - 1) * 100

        if reaction_pct > -drop_threshold_pct:
            continue

        window = future_bars.head(max_days)
        post_low = window["close"].min()
        post_low_date = window["close"].idxmin()
        recovery_target = post_low * (1 + recovery_threshold_pct / 100)
        after_low = window[window.index >= post_low_date]
        recovery_hits = after_low[after_low["close"] >= recovery_target]

        days_to_recover = None
        if not recovery_hits.empty:
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

    recovered = [e for e in events if e["recovered"]]
    return {
        "ticker": ticker,
        "scenario": f"earnings drop ≥{drop_threshold_pct}%, recovery {recovery_threshold_pct}% from low",
        "n_drops": len(events),
        "n_recovered": len(recovered),
        "pct_recovered": round(len(recovered) / len(events) * 100, 1),
        "avg_days_to_recover": round(
            sum(e["days_low_to_recovery"] for e in recovered) / len(recovered), 1
        ) if recovered else None,
        "median_days_to_recover": int(
            pd.Series([e["days_low_to_recovery"] for e in recovered]).median()
        ) if recovered else None,
        "events": events,
    }


SCENARIO_REGISTRY = {
    "consecutive_down_periods": consecutive_down_periods,
    "intraday_drawdown_recovery": intraday_drawdown_recovery,
    "post_earnings_drop_recovery": post_earnings_drop_recovery,
}
