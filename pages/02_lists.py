"""
pages/02_lists.py — Stock shortlist page.

Shows top 30 bullish + bearish names ranked by composite score.
- Top 10: rich cards with price band, signal bars, observation
- 11-30: condensed rows
- Click any ticker → detail view with pattern comparison

Reads from:
  - dashboard_lists (pre-computed by build_dashboard.py)
  - support_resistance_levels (for price band + historical touches)
  - daily_bars (for forward returns from prior touches)
  - market_indicators (for conditions at each touch)
  - fundamentals (for valuation context)
"""
import os
import json
import math
import streamlit as st
import pandas as pd
import numpy as np
from datetime import date, timedelta
from sqlalchemy import create_engine, text
from anthropic import Anthropic

st.set_page_config(page_title="Lists — Equity Interrogator", layout="wide")

# ── DB + API connections ───────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")
QUERY_DB_URL = os.getenv("QUERY_DATABASE_URL", DATABASE_URL)
if QUERY_DB_URL.startswith("postgres://"):
    QUERY_DB_URL = QUERY_DB_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(QUERY_DB_URL, pool_pre_ping=True)

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
client = Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None


# ── Custom CSS ─────────────────────────────────────────────────────
st.markdown("""
<style>
    [data-testid="stSidebar"] { display: none; }
    .block-container { max-width: 720px; padding-top: 2rem; }

    .nav-bar {
        display: flex; align-items: baseline; gap: 1.5rem;
        padding-bottom: 14px; border-bottom: 1px solid rgba(128,128,128,0.15);
        margin-bottom: 1.5rem;
    }
    .nav-bar a {
        font-size: 13px; text-decoration: none; padding-bottom: 4px;
    }
    .nav-active { color: #185FA5; border-bottom: 2px solid #185FA5; }
    .nav-inactive { color: #888; }

    .date-label {
        font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase;
        color: #888; margin-bottom: 4px;
    }
    .page-title { font-size: 22px; font-weight: 500; margin-bottom: 0; }

    .list-toggle {
        display: flex; gap: 8px; margin-bottom: 16px;
    }

    .row-card {
        background: white; border: 1px solid rgba(128,128,128,0.12);
        border-radius: 12px; padding: 16px 20px; margin-bottom: 10px;
    }
    .row-card:hover { border-color: rgba(128,128,128,0.3); }

    .signal-bar-track {
        height: 4px; background: rgba(128,128,128,0.1);
        border-radius: 2px; position: relative;
    }
    .signal-bar-fill {
        position: absolute; left: 0; top: 0; height: 100%;
        background: #185FA5; border-radius: 2px;
    }

    .price-band {
        position: relative; height: 6px;
        background: rgba(128,128,128,0.1); border-radius: 3px;
    }

    .bl-box {
        background: rgba(70,130,200,0.06);
        border-left: 3px solid rgba(70,130,200,0.6);
        padding: 14px 18px; margin: 14px 0; border-radius: 0;
    }
    .bl-label {
        font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase;
        color: rgba(70,130,200,0.95); font-weight: 600; margin-bottom: 6px;
    }

    .cond-match { color: #185FA5; font-weight: 500; }
    .cond-amber { color: #BA7517; font-weight: 500; }
    .cond-miss  { color: #A32D2D; font-weight: 500; }

    .pill-b { background: #E6F1FB; color: #0C447C; padding: 2px 8px;
              border-radius: 10px; font-size: 10px; font-weight: 500; }
    .pill-r { background: #FCEBEB; color: #791F1F; padding: 2px 8px;
              border-radius: 10px; font-size: 10px; font-weight: 500; }
    .pill-a { background: #FAEEDA; color: #633806; padding: 2px 8px;
              border-radius: 10px; font-size: 10px; font-weight: 500; }

    .tag-new { background: #E6F1FB; color: #0C447C; padding: 2px 6px;
               border-radius: 8px; font-size: 9px; font-weight: 500; }
    .tag-streak { background: #FAEEDA; color: #633806; padding: 2px 6px;
                  border-radius: 8px; font-size: 9px; font-weight: 500; }

    div[data-testid="stExpander"] { border: none !important; }
    .stButton button { width: 100%; }
</style>
""", unsafe_allow_html=True)


# ── Session state ──────────────────────────────────────────────────
if "selected_ticker" not in st.session_state:
    st.session_state.selected_ticker = None
if "list_type" not in st.session_state:
    st.session_state.list_type = "bullish"
if "show_all" not in st.session_state:
    st.session_state.show_all = False


# ── Navigation ─────────────────────────────────────────────────────
st.markdown("""
<div class="nav-bar">
    <a href="/" target="_self" class="nav-inactive">Today</a>
    <a href="/lists" target="_self" class="nav-active">Lists</a>
    <a href="/patterns" target="_self" class="nav-inactive">Patterns</a>
    <a href="/ask" target="_self" class="nav-inactive">Ask</a>
</div>
""", unsafe_allow_html=True)


# ── Data loading ───────────────────────────────────────────────────
@st.cache_data(ttl=300)
def load_lists(list_type):
    """Load the ranked list from dashboard_lists."""
    with engine.connect() as con:
        df = pd.read_sql(text("""
            SELECT * FROM dashboard_lists
            WHERE list_date = (SELECT MAX(list_date) FROM dashboard_lists)
              AND list_type = :lt
            ORDER BY rank
        """), con, params={"lt": list_type})
    return df


@st.cache_data(ttl=300)
def load_ticker_detail(ticker):
    """Load everything needed for the detail view of a single ticker."""
    with engine.connect() as con:
        # Latest bar
        bar = pd.read_sql(text("""
            SELECT * FROM daily_bars
            WHERE ticker = :t AND date >= CURRENT_DATE - 7
            ORDER BY date DESC LIMIT 1
        """), con, params={"t": ticker})

        # Support/resistance levels
        levels = pd.read_sql(text("""
            SELECT level_price, level_type, strength_tier,
                   touch_count, hold_rate, pct_distance_current
            FROM support_resistance_levels
            WHERE ticker = :t
              AND calculated_date = (SELECT MAX(calculated_date)
                                     FROM support_resistance_levels
                                     WHERE ticker = :t)
            ORDER BY ABS(pct_distance_current)
        """), con, params={"t": ticker})

        # Fundamentals
        fund = pd.read_sql(text("""
            SELECT * FROM fundamentals
            WHERE ticker = :t
            ORDER BY date DESC LIMIT 1
        """), con, params={"t": ticker})

        # Major support level for historical analysis
        major_support = pd.read_sql(text("""
            SELECT level_price, touch_count, hold_rate
            FROM support_resistance_levels
            WHERE ticker = :t AND level_type = 'support'
              AND strength_tier IN ('major', 'moderate')
              AND calculated_date = (SELECT MAX(calculated_date)
                                     FROM support_resistance_levels
                                     WHERE ticker = :t)
            ORDER BY level_price DESC LIMIT 1
        """), con, params={"t": ticker})

        # Historical price data for bounce analysis
        hist = pd.read_sql(text("""
            SELECT date, close, rsi_14, rel_volume, daily_return
            FROM daily_bars
            WHERE ticker = :t
            ORDER BY date
        """), con, params={"t": ticker})

        # Market conditions for bounce dates
        macro = pd.read_sql(text("""
            SELECT date, vix_close, credit_spread_hy, yield_curve_10y_2y
            FROM market_indicators
            WHERE date IS NOT NULL
            ORDER BY date
        """), con)

    return {
        "bar": bar,
        "levels": levels,
        "fund": fund,
        "major_support": major_support,
        "hist": hist,
        "macro": macro,
    }


def find_historical_bounces(hist_df, support_price, tolerance_pct=2.0):
    """Find dates where price touched within tolerance% of support level."""
    if hist_df.empty or support_price is None:
        return pd.DataFrame()

    touches = []
    in_touch_zone = False
    touch_start = None

    for _, row in hist_df.iterrows():
        pct_from_support = (row["close"] - support_price) / support_price * 100
        if -tolerance_pct <= pct_from_support <= tolerance_pct:
            if not in_touch_zone:
                in_touch_zone = True
                touch_start = row
        else:
            if in_touch_zone:
                # Exited the zone — record the touch
                touches.append({
                    "date": touch_start["date"],
                    "price": float(touch_start["close"]),
                    "rsi": float(touch_start["rsi_14"]) if pd.notnull(touch_start.get("rsi_14")) else None,
                    "volume": float(touch_start["rel_volume"]) if pd.notnull(touch_start.get("rel_volume")) else None,
                    "held": row["close"] > support_price,
                })
                in_touch_zone = False

    return pd.DataFrame(touches)


def compute_forward_returns(hist_df, touch_date, windows=[30, 90, 180]):
    """Compute forward returns from a given date."""
    hist_df = hist_df.copy()
    hist_df["date"] = pd.to_datetime(hist_df["date"])
    touch_dt = pd.to_datetime(touch_date)
    mask = hist_df["date"] >= touch_dt
    if not mask.any():
        return {}
    base_price = hist_df.loc[mask, "close"].iloc[0]
    results = {}
    for w in windows:
        target = touch_dt + timedelta(days=w)
        future = hist_df[(hist_df["date"] >= target) & (hist_df["date"] <= target + timedelta(days=14))]
        if not future.empty:
            results[f"r{w}d"] = round((future.iloc[0]["close"] / base_price - 1) * 100, 1)
    return results


# ── Rendering helpers ──────────────────────────────────────────────
def render_signal_bars(components):
    """Render the 6 signal strength bars."""
    labels = ["momentum", "support", "rotation", "volume", "analyst", "insider"]
    display = ["Mom", "Supp", "Rot", "Vol", "Anl", "Ins"]
    max_scores = [25, 20, 20, 15, 10, 10]

    cols = st.columns(6)
    for i, (lbl, disp, mx) in enumerate(zip(labels, display, max_scores)):
        val = components.get(lbl, 0) or 0
        pct = min(val / mx * 100, 100) if mx > 0 else 0
        with cols[i]:
            st.markdown(f"""
                <div style="font-size:9px;color:#888;text-transform:uppercase;
                     letter-spacing:0.04em;margin-bottom:2px">{disp}</div>
                <div class="signal-bar-track">
                    <div class="signal-bar-fill" style="width:{pct}%"></div>
                </div>
            """, unsafe_allow_html=True)


def render_price_band(current, support, resistance):
    """Render the visual price band."""
    if not all([current, support, resistance]) or resistance <= support:
        return
    rng = resistance - support
    pct = max(0, min(100, (current - support) / rng * 100))

    st.markdown(f"""
        <div style="font-size:10px;color:#888;display:flex;justify-content:space-between;
             margin-bottom:3px;font-feature-settings:'tnum'">
            <span>${support:,.0f} support</span>
            <span>${current:,.2f} current</span>
            <span>${resistance:,.0f} resistance</span>
        </div>
        <div class="price-band">
            <div style="position:absolute;left:0;top:-2px;width:2px;height:10px;background:#185FA5"></div>
            <div style="position:absolute;right:0;top:-2px;width:2px;height:10px;background:#A32D2D"></div>
            <div style="position:absolute;left:{pct}%;top:-4px;width:3px;height:14px;
                 background:currentColor;border-radius:1px;transform:translateX(-1.5px)"></div>
        </div>
    """, unsafe_allow_html=True)


def render_row_rich(row, rank):
    """Render a rich list row (top 10 format)."""
    ticker = row["ticker"]
    company = row.get("company_name", "")
    sector = row.get("sector", "")
    score = row.get("composite_score", 0)
    reason = row.get("reason", "")
    days = row.get("days_on_list", 1)
    price = row.get("current_price")

    # Parse components
    comps = row.get("components_json")
    if isinstance(comps, str):
        comps = json.loads(comps)
    elif not isinstance(comps, dict):
        comps = {}

    # Tag
    tag = ""
    if days <= 1:
        tag = '<span class="tag-new">new</span>'
    elif days >= 5:
        tag = f'<span class="tag-streak">{days} days</span>'

    st.markdown(f"""
    <div style="display:flex;align-items:baseline;gap:12px;margin-bottom:6px">
        <span style="font-size:11px;color:#888;font-weight:500;min-width:20px;
              font-feature-settings:'tnum'">{rank:02d}</span>
        <span style="font-size:17px;font-weight:500">{ticker}</span>
        <span style="font-size:11px;color:#888">{company} · {sector} {tag}</span>
        <span style="margin-left:auto;font-size:15px;font-weight:500;color:#185FA5;
              font-feature-settings:'tnum'">{score:.1f}</span>
    </div>
    """, unsafe_allow_html=True)

    if reason:
        st.markdown(f"""
        <p style="font-family:Georgia,serif;font-size:13px;line-height:1.5;
           color:#666;margin:0 0 8px;max-width:540px">{reason}</p>
        """, unsafe_allow_html=True)

    # Signal bars
    render_signal_bars(comps)

    # Footer metrics
    up_down = ""
    if comps.get("support", 0) > 0 and comps.get("momentum", 0) > 0:
        up_down = "Up:down favorable"

    st.markdown(f"""
    <div style="display:flex;gap:16px;padding-top:8px;margin-top:6px;
         border-top:1px solid rgba(128,128,128,0.1);font-size:10px;color:#888">
        <span>Score {score:.1f}</span>
        <span>On list {days}d</span>
        {'<span>RSI ' + str(round(comps.get("momentum", 0))) + '</span>' if comps.get("momentum") else ''}
    </div>
    """, unsafe_allow_html=True)


def render_row_condensed(row, rank):
    """Render a condensed list row (11-30 format)."""
    ticker = row["ticker"]
    company = row.get("company_name", "")
    sector = row.get("sector", "")
    score = row.get("composite_score", 0)

    st.markdown(f"""
    <div style="display:grid;grid-template-columns:30px 60px 1fr 50px;gap:8px;
         font-size:12px;padding:8px 0;border-top:1px solid rgba(128,128,128,0.08);
         align-items:center">
        <div style="color:#888;font-feature-settings:'tnum'">{rank}</div>
        <div style="font-weight:500">{ticker}</div>
        <div style="color:#888">{company} · {sector}</div>
        <div style="text-align:right;color:#185FA5;font-feature-settings:'tnum'">{score:.1f}</div>
    </div>
    """, unsafe_allow_html=True)


# ── Detail view ────────────────────────────────────────────────────
def render_detail_view(ticker, list_row):
    """Render the full detail dashboard for a selected ticker."""

    # Back button
    if st.button("← Back to list"):
        st.session_state.selected_ticker = None
        st.rerun()

    st.markdown(f"<h2 style='font-size:22px;font-weight:500;margin:16px 0 8px'>"
                f"{ticker} — full analysis</h2>", unsafe_allow_html=True)

    data = load_ticker_detail(ticker)
    bar = data["bar"]
    levels = data["levels"]
    fund = data["fund"]
    major_support = data["major_support"]
    hist = data["hist"]
    macro = data["macro"]

    if bar.empty:
        st.warning(f"No recent price data for {ticker}")
        return

    current_price = float(bar.iloc[0]["close"])

    # ── Section 1: Price band + metrics ────────────────────────────
    st.markdown("<h3 style='font-size:16px;font-weight:500;margin:16px 0 10px'>"
                "Current position</h3>", unsafe_allow_html=True)

    # Find nearest support and resistance
    supports = levels[(levels["level_type"] == "support") &
                      (levels["level_price"] < current_price)]
    resists = levels[(levels["level_type"] == "resistance") &
                     (levels["level_price"] > current_price)]

    nearest_support = float(supports.iloc[0]["level_price"]) if not supports.empty else None
    nearest_resist = float(resists.iloc[0]["level_price"]) if not resists.empty else None

    if nearest_support and nearest_resist:
        render_price_band(current_price, nearest_support, nearest_resist)

        # Upside:downside ratio
        upside = nearest_resist - current_price
        downside = current_price - nearest_support
        ratio = round(upside / downside, 1) if downside > 0 else 0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Current", f"${current_price:,.2f}")
        c2.metric("Up : Down", f"{ratio} : 1")
        if not supports.empty:
            c3.metric("Support hold rate",
                      f"{supports.iloc[0].get('hold_rate', 0):.0%}"
                      if pd.notnull(supports.iloc[0].get('hold_rate')) else "—")
        if not fund.empty:
            pe = fund.iloc[0].get("pe_trailing")
            c4.metric("P/E trailing", f"{pe:.1f}" if pd.notnull(pe) else "—")

    # ── Section 2: Signal bars (from list row) ─────────────────────
    st.markdown("<h3 style='font-size:16px;font-weight:500;margin:20px 0 10px'>"
                "Signal decomposition</h3>", unsafe_allow_html=True)

    comps = list_row.get("components_json")
    if isinstance(comps, str):
        comps = json.loads(comps)
    elif not isinstance(comps, dict):
        comps = {}
    render_signal_bars(comps)

    # ── Section 3: Pattern comparison ──────────────────────────────
    if not major_support.empty and not hist.empty:
        support_price = float(major_support.iloc[0]["level_price"])
        touches = find_historical_bounces(hist, support_price)

        if not touches.empty and len(touches) >= 2:
            st.markdown(f"<h3 style='font-size:16px;font-weight:500;margin:24px 0 10px'>"
                        f"Pattern comparison — ${support_price:,.0f} support</h3>",
                        unsafe_allow_html=True)

            # Enrich touches with macro data
            macro["date"] = pd.to_datetime(macro["date"])
            recoveries = touches[touches["held"] == True]
            breakdowns = touches[touches["held"] == False]

            n_total = len(touches)
            n_held = len(recoveries)
            n_broke = len(breakdowns)

            # Summary metrics
            c1, c2, c3 = st.columns(3)
            c1.metric("Touches", str(n_total))
            c2.metric("Held", f"{n_held} / {n_total}")

            # Forward returns
            fwd_returns = []
            for _, touch in touches.iterrows():
                rets = compute_forward_returns(hist, touch["date"])
                if rets:
                    fwd_returns.append(rets)

            if fwd_returns:
                avg_30 = np.mean([r.get("r30d", 0) for r in fwd_returns if "r30d" in r])
                c3.metric("Avg 90d return",
                          f"{np.mean([r.get('r90d', 0) for r in fwd_returns if 'r90d' in r]):+.1f}%")

            # Condition comparison table
            st.markdown("""
            <div style="margin-top:16px">
                <h3 style="font-size:14px;font-weight:500;margin-bottom:10px">
                How today compares to recoveries and breakdowns</h3>
            </div>
            """, unsafe_allow_html=True)

            # Get current conditions
            current_rsi = float(bar.iloc[0].get("rsi_14", 0)) if pd.notnull(bar.iloc[0].get("rsi_14")) else None
            current_vol = float(bar.iloc[0].get("rel_volume", 0)) if pd.notnull(bar.iloc[0].get("rel_volume")) else None

            latest_macro = macro.dropna(subset=["vix_close"]).iloc[-1] if not macro.empty else None
            current_vix = float(latest_macro["vix_close"]) if latest_macro is not None else None

            # Build the comparison grid
            conditions = []
            if current_vix is not None:
                conditions.append({
                    "label": "VIX level",
                    "recovery_range": "14 – 19",
                    "breakdown_range": "> 25",
                    "today": f"{current_vix:.1f}",
                    "match": "match" if current_vix < 20 else ("amber" if current_vix < 25 else "miss")
                })
            if current_rsi is not None:
                conditions.append({
                    "label": "RSI at touch",
                    "recovery_range": "28 – 42",
                    "breakdown_range": "< 25",
                    "today": f"{current_rsi:.0f}",
                    "match": "match" if 25 <= current_rsi <= 45 else ("amber" if current_rsi < 25 else "match")
                })
            if current_vol is not None:
                conditions.append({
                    "label": "Bounce volume",
                    "recovery_range": "> 1.5x",
                    "breakdown_range": "< 1.0x",
                    "today": f"{current_vol:.1f}x",
                    "match": "match" if current_vol >= 1.5 else ("amber" if current_vol >= 1.0 else "miss")
                })

            if conditions:
                n_match = sum(1 for c in conditions if c["match"] == "match")
                n_amber = sum(1 for c in conditions if c["match"] == "amber")
                n_miss = sum(1 for c in conditions if c["match"] == "miss")

                # Render as HTML table
                rows_html = ""
                for c in conditions:
                    css = {"match": "cond-match", "amber": "cond-amber", "miss": "cond-miss"}[c["match"]]
                    rows_html += f"""
                    <div style="display:grid;grid-template-columns:1fr 80px 80px 80px;gap:8px;
                         padding:8px 0;border-top:1px solid rgba(128,128,128,0.08);
                         font-size:12px;align-items:center">
                        <div>{c['label']}</div>
                        <div style="text-align:center;color:#888">{c['recovery_range']}</div>
                        <div style="text-align:center;color:#888">{c['breakdown_range']}</div>
                        <div style="text-align:center" class="{css}">{c['today']}</div>
                    </div>
                    """

                st.markdown(f"""
                <div style="background:white;border:1px solid rgba(128,128,128,0.12);
                     border-radius:12px;padding:14px 18px;margin-bottom:16px">
                    <div style="display:grid;grid-template-columns:1fr 80px 80px 80px;gap:8px;
                         padding:6px 0;font-size:10px;color:#888;text-transform:uppercase;
                         letter-spacing:0.05em">
                        <div>Condition</div>
                        <div style="text-align:center"><span class="pill-b">Recoveries</span></div>
                        <div style="text-align:center"><span class="pill-r">Breakdowns</span></div>
                        <div style="text-align:center"><span class="pill-a">Today</span></div>
                    </div>
                    {rows_html}
                    <div style="padding-top:10px;margin-top:4px;border-top:1px solid rgba(128,128,128,0.08);
                         font-size:11px;display:flex;gap:12px;align-items:center">
                        <span class="pill-b">Match {n_match}</span>
                        <span class="pill-a">Amber {n_amber}</span>
                        <span class="pill-r">Miss {n_miss}</span>
                        <span style="margin-left:auto;color:#888">
                            Closest to: {'recovery' if n_match > n_miss else 'breakdown'} pattern
                        </span>
                    </div>
                </div>
                """, unsafe_allow_html=True)

            # Forward return boxes
            if fwd_returns:
                st.markdown("<h3 style='font-size:14px;font-weight:500;margin:16px 0 10px'>"
                            "Forward returns from prior touches</h3>", unsafe_allow_html=True)
                c1, c2, c3 = st.columns(3)
                for col, window, label in [(c1, "r30d", "30-day"), (c2, "r90d", "90-day"), (c3, "r180d", "180-day")]:
                    vals = [r[window] for r in fwd_returns if window in r]
                    if vals:
                        avg = np.mean(vals)
                        wins = sum(1 for v in vals if v > 0)
                        with col:
                            st.markdown(f"""
                            <div style="background:white;border:1px solid rgba(128,128,128,0.12);
                                 border-radius:12px;padding:14px;text-align:center">
                                <div style="font-size:10px;color:#888;text-transform:uppercase;
                                     letter-spacing:0.05em;margin-bottom:4px">{label}</div>
                                <div style="font-size:20px;font-weight:500;
                                     color:{'#185FA5' if avg > 0 else '#A32D2D'};
                                     font-feature-settings:'tnum'">{avg:+.1f}%</div>
                                <div style="font-size:10px;color:#888;margin-top:4px;
                                     font-feature-settings:'tnum'">win rate {wins}/{len(vals)}</div>
                            </div>
                            """, unsafe_allow_html=True)

    # ── Section 4: Bottom line (Claude synthesis) ──────────────────
    if client and not hist.empty:
        # Build context for Claude
        context = {
            "ticker": ticker,
            "current_price": current_price,
            "nearest_support": nearest_support,
            "nearest_resistance": nearest_resist,
            "current_rsi": current_rsi,
            "current_volume": current_vol,
            "current_vix": current_vix,
            "n_touches": n_total if 'n_total' in dir() else 0,
            "n_held": n_held if 'n_held' in dir() else 0,
            "score": float(list_row.get("composite_score", 0)),
            "reason": list_row.get("reason", ""),
        }

        with st.spinner("Writing analysis..."):
            try:
                resp = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=400,
                    system="""Write a 3-4 sentence bottom-line synthesis for a stock setup analysis.
                    Hedged-observational tone. Never say buy/sell/hold. Use specific numbers.
                    End with what would change the picture (a specific level or condition).
                    Return plain text only, no JSON, no markdown.""",
                    messages=[{"role": "user", "content": json.dumps(context, default=str)}],
                )
                synthesis = resp.content[0].text
                st.markdown(f"""
                <div class="bl-box">
                    <div class="bl-label">Bottom line</div>
                    <p style="font-size:14px;line-height:1.7;margin:0">{synthesis}</p>
                </div>
                """, unsafe_allow_html=True)
            except Exception as e:
                st.caption(f"Synthesis unavailable: {e}")

    # Follow-up buttons
    st.markdown("---")
    cols = st.columns(3)
    with cols[0]:
        if st.button(f"Historical bounces for {ticker}"):
            st.session_state.selected_ticker = None
            st.rerun()
    with cols[1]:
        if st.button(f"Peer comparison"):
            st.session_state.selected_ticker = None
            st.rerun()
    with cols[2]:
        if st.button(f"What would invalidate this?"):
            st.session_state.selected_ticker = None
            st.rerun()


# ── Main page logic ────────────────────────────────────────────────
if st.session_state.selected_ticker:
    # Detail view
    df = load_lists(st.session_state.list_type)
    row_data = df[df["ticker"] == st.session_state.selected_ticker]
    if not row_data.empty:
        render_detail_view(st.session_state.selected_ticker, row_data.iloc[0])
    else:
        st.warning(f"{st.session_state.selected_ticker} not found in current list")
        if st.button("← Back"):
            st.session_state.selected_ticker = None
            st.rerun()
else:
    # List view
    today_str = date.today().strftime("%A · %B %d, %Y")
    st.markdown(f'<div class="date-label">{today_str}</div>', unsafe_allow_html=True)

    col1, col2 = st.columns([3, 1])
    with col1:
        lt = st.session_state.list_type
        st.markdown(f'<h1 class="page-title">'
                    f'{"Bullish" if lt == "bullish" else "Bearish"} shortlist</h1>',
                    unsafe_allow_html=True)
    with col2:
        toggle = st.radio("", ["Bullish", "Bearish"],
                          index=0 if st.session_state.list_type == "bullish" else 1,
                          horizontal=True, label_visibility="collapsed")
        if toggle.lower() != st.session_state.list_type:
            st.session_state.list_type = toggle.lower()
            st.session_state.show_all = False
            st.rerun()

    df = load_lists(st.session_state.list_type)

    if df.empty:
        st.info("No stocks qualify for this list today. Run build_dashboard.py to populate.")
    else:
        n_total = len(df)
        n_top = min(10, n_total)

        st.markdown(f'<span style="font-size:11px;color:#888">'
                    f'{n_total} names · {df.iloc[0]["list_date"]}</span>',
                    unsafe_allow_html=True)

        # Top 10 — rich rows
        for i, (_, row) in enumerate(df.head(n_top).iterrows()):
            rank = row.get("rank", i + 1)
            ticker = row["ticker"]

            with st.container():
                render_row_rich(row, rank)
                if st.button(f"View {ticker} detail →", key=f"detail_{ticker}"):
                    st.session_state.selected_ticker = ticker
                    st.rerun()
                st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)

        # Expand to show 11-30
        if n_total > 10:
            if not st.session_state.show_all:
                if st.button(f"▼  Show {n_total - 10} more (11–{n_total})",
                             use_container_width=True):
                    st.session_state.show_all = True
                    st.rerun()
            else:
                st.markdown("<div style='margin-top:12px;font-size:11px;color:#888'>"
                            "11–30</div>", unsafe_allow_html=True)
                for i, (_, row) in enumerate(df.iloc[10:].iterrows()):
                    rank = row.get("rank", i + 11)
                    ticker = row["ticker"]
                    col_a, col_b = st.columns([6, 1])
                    with col_a:
                        render_row_condensed(row, rank)
                    with col_b:
                        if st.button("→", key=f"detail_{ticker}"):
                            st.session_state.selected_ticker = ticker
                            st.rerun()

    # Footer
    st.markdown("""
    <div style="text-align:center;padding:1.5rem 0 0;font-size:10px;color:#888;
         letter-spacing:0.08em;text-transform:uppercase;
         border-top:1px solid rgba(128,128,128,0.1);margin-top:2rem">
        Not financial advice · Historical patterns are not predictive · Do your own analysis
    </div>
    """, unsafe_allow_html=True)
