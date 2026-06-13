"""
pages/02_lists.py — Stock shortlist page v2.
Uses native Streamlit components instead of raw HTML for reliability.
"""
import os
import json
import math
import streamlit as st
import pandas as pd
import numpy as np
from datetime import date, timedelta
from sqlalchemy import create_engine, text

st.set_page_config(page_title="Lists", layout="centered")

DATABASE_URL = os.getenv("DATABASE_URL", "")
QUERY_DB_URL = os.getenv("QUERY_DATABASE_URL", DATABASE_URL)
if QUERY_DB_URL.startswith("postgres://"):
    QUERY_DB_URL = QUERY_DB_URL.replace("postgres://", "postgresql://", 1)
engine = create_engine(QUERY_DB_URL, pool_pre_ping=True)

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── Minimal CSS — theme-aware only ─────────────────────────────────
st.markdown("""
<style>
    [data-testid="stSidebar"] { display: none; }
    .block-container { max-width: 740px; padding-top: 1.5rem; }
    .signal-track {
        height: 4px; border-radius: 2px; position: relative;
        background: rgba(128,128,128,0.15); margin-top: 3px;
    }
    .signal-fill {
        position: absolute; left: 0; top: 0; height: 100%;
        background: #4a90d9; border-radius: 2px;
    }
</style>
""", unsafe_allow_html=True)

if "selected_ticker" not in st.session_state:
    st.session_state.selected_ticker = None
if "list_type" not in st.session_state:
    st.session_state.list_type = "bullish"
if "show_all" not in st.session_state:
    st.session_state.show_all = False


# ── Data ───────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def load_lists(list_type):
    with engine.connect() as con:
        return pd.read_sql(text("""
            SELECT * FROM dashboard_lists
            WHERE list_date = (SELECT MAX(list_date) FROM dashboard_lists)
              AND list_type = :lt ORDER BY rank
        """), con, params={"lt": list_type})


@st.cache_data(ttl=300)
def load_detail(ticker):
    with engine.connect() as con:
        bar = pd.read_sql(text("""
            SELECT * FROM daily_bars WHERE ticker=:t AND date >= CURRENT_DATE - 7
            ORDER BY date DESC LIMIT 1
        """), con, params={"t": ticker})

        levels = pd.read_sql(text("""
            SELECT level_price, level_type, strength_tier,
                   touch_count, hold_rate, pct_distance_current
            FROM support_resistance_levels WHERE ticker=:t
              AND calculated_date=(SELECT MAX(calculated_date)
                   FROM support_resistance_levels WHERE ticker=:t)
            ORDER BY ABS(pct_distance_current)
        """), con, params={"t": ticker})

        fund = pd.read_sql(text("""
            SELECT * FROM fundamentals WHERE ticker=:t ORDER BY date DESC LIMIT 1
        """), con, params={"t": ticker})

        major_support = pd.read_sql(text("""
            SELECT level_price, touch_count, hold_rate
            FROM support_resistance_levels WHERE ticker=:t
              AND level_type='support' AND strength_tier IN ('major','moderate')
              AND calculated_date=(SELECT MAX(calculated_date)
                   FROM support_resistance_levels WHERE ticker=:t)
            ORDER BY level_price DESC LIMIT 1
        """), con, params={"t": ticker})

        hist = pd.read_sql(text("""
            SELECT date, close, rsi_14, rel_volume, daily_return
            FROM daily_bars WHERE ticker=:t ORDER BY date
        """), con, params={"t": ticker})

        macro = pd.read_sql(text("""
            SELECT date, vix_close, credit_spread_hy, yield_curve_10y_2y
            FROM market_indicators WHERE date IS NOT NULL ORDER BY date
        """), con)

    return bar, levels, fund, major_support, hist, macro


def find_bounces(hist, support_price, tol=2.0):
    if hist.empty or support_price is None:
        return []
    touches = []
    in_zone = False
    entry = None
    for _, r in hist.iterrows():
        pct = (r["close"] - support_price) / support_price * 100
        if -tol <= pct <= tol:
            if not in_zone:
                in_zone = True
                entry = r
        else:
            if in_zone:
                touches.append({
                    "date": entry["date"],
                    "price": float(entry["close"]),
                    "rsi": float(entry["rsi_14"]) if pd.notnull(entry.get("rsi_14")) else None,
                    "volume": float(entry["rel_volume"]) if pd.notnull(entry.get("rel_volume")) else None,
                    "held": r["close"] > support_price,
                })
                in_zone = False
    return touches


def fwd_return(hist, touch_date, days):
    hist = hist.copy()
    hist["date"] = pd.to_datetime(hist["date"])
    td = pd.to_datetime(touch_date)
    after = hist[hist["date"] >= td]
    if after.empty:
        return None
    base = after.iloc[0]["close"]
    target = td + timedelta(days=days)
    window = hist[(hist["date"] >= target) & (hist["date"] <= target + timedelta(days=14))]
    if window.empty:
        return None
    return round((window.iloc[0]["close"] / base - 1) * 100, 1)


# ── Signal bars using native Streamlit ─────────────────────────────
def render_signals(comps):
    labels = ["momentum", "support", "rotation", "volume", "analyst", "insider"]
    display = ["MOM", "SUPP", "ROT", "VOL", "ANL", "INS"]
    maxes = [25, 20, 20, 15, 10, 10]
    cols = st.columns(6)
    for i, (lbl, disp, mx) in enumerate(zip(labels, display, maxes)):
        val = comps.get(lbl, 0) or 0
        pct = min(val / mx * 100, 100) if mx > 0 else 0
        with cols[i]:
            st.caption(disp)
            st.markdown(
                f'<div class="signal-track">'
                f'<div class="signal-fill" style="width:{pct:.0f}%"></div>'
                f'</div>', unsafe_allow_html=True)


# ── List row ───────────────────────────────────────────────────────
def render_row(row, rank, rich=True):
    ticker = row["ticker"]
    comps = row.get("components_json")
    if isinstance(comps, str):
        comps = json.loads(comps)
    elif not isinstance(comps, dict):
        comps = {}

    score = row.get("composite_score", 0)
    reason = row.get("reason", "")
    days = row.get("days_on_list", 1)
    company = row.get("company_name", "")
    sector = row.get("sector", "")

    tag = ""
    if days and days <= 1:
        tag = " · 🆕"
    elif days and days >= 5:
        tag = f" · {days}d streak"

    with st.container(border=True):
        c1, c2 = st.columns([5, 1])
        with c1:
            st.markdown(f"**{rank:02d} &nbsp; {ticker}** &nbsp; "
                        f"<span style='font-size:12px;opacity:0.6'>"
                        f"{company} · {sector}{tag}</span>",
                        unsafe_allow_html=True)
        with c2:
            st.markdown(f"<div style='text-align:right;font-size:18px;"
                        f"font-weight:500;color:#4a90d9'>{score:.1f}</div>",
                        unsafe_allow_html=True)

        if rich and reason:
            st.caption(reason)

        if rich:
            render_signals(comps)

        if st.button(f"View {ticker} →", key=f"go_{ticker}_{rank}"):
            st.session_state.selected_ticker = ticker
            st.rerun()


# ── Detail view ────────────────────────────────────────────────────
def render_detail(ticker, list_row):
    if st.button("← Back to list"):
        st.session_state.selected_ticker = None
        st.rerun()

    st.header(f"{ticker} — full analysis")

    bar, levels, fund, major_support, hist, macro = load_detail(ticker)
    if bar.empty:
        st.warning(f"No recent data for {ticker}")
        return

    price = float(bar.iloc[0]["close"])

    # ── Current position ───────────────────────────────────────────
    st.subheader("Current position")

    supports = levels[(levels["level_type"] == "support") &
                      (levels["level_price"] < price)]
    resists = levels[(levels["level_type"] == "resistance") &
                     (levels["level_price"] > price)]

    near_s = float(supports.iloc[0]["level_price"]) if not supports.empty else None
    near_r = float(resists.iloc[0]["level_price"]) if not resists.empty else None

    # Price band
    if near_s and near_r and near_r > near_s:
        rng = near_r - near_s
        pct = max(0, min(100, (price - near_s) / rng * 100))
        ratio = round((near_r - price) / max(price - near_s, 0.01), 1)

        st.markdown(
            f"<div style='display:flex;justify-content:space-between;"
            f"font-size:11px;opacity:0.5;margin-bottom:2px'>"
            f"<span>${near_s:,.0f} support</span>"
            f"<span>${price:,.2f} current</span>"
            f"<span>${near_r:,.0f} resistance</span></div>"
            f"<div style='position:relative;height:8px;"
            f"background:rgba(128,128,128,0.15);border-radius:4px;margin-bottom:16px'>"
            f"<div style='position:absolute;left:0;top:0;width:3px;height:100%;"
            f"background:#4a90d9;border-radius:2px'></div>"
            f"<div style='position:absolute;right:0;top:0;width:3px;height:100%;"
            f"background:#d94a4a;border-radius:2px'></div>"
            f"<div style='position:absolute;left:{pct:.0f}%;top:-2px;width:4px;"
            f"height:12px;background:currentColor;border-radius:2px;"
            f"transform:translateX(-2px)'></div></div>",
            unsafe_allow_html=True)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Current", f"${price:,.2f}")
        c2.metric("Up : Down", f"{ratio} : 1")
        hold = supports.iloc[0].get("hold_rate") if not supports.empty else None
        c3.metric("Support hold", f"{hold:.0%}" if pd.notnull(hold) else "—")
        pe = fund.iloc[0].get("pe_trailing") if not fund.empty else None
        c4.metric("P/E", f"{pe:.1f}" if pd.notnull(pe) else "—")

    # ── Signal decomposition ───────────────────────────────────────
    st.subheader("Signal decomposition")
    comps = list_row.get("components_json")
    if isinstance(comps, str):
        comps = json.loads(comps)
    elif not isinstance(comps, dict):
        comps = {}
    render_signals(comps)

    # ── Pattern comparison ─────────────────────────────────────────
    if not major_support.empty and not hist.empty:
        sp = float(major_support.iloc[0]["level_price"])
        touches = find_bounces(hist, sp)

        if touches and len(touches) >= 2:
            st.subheader(f"Pattern comparison — ${sp:,.0f} support")

            n_total = len(touches)
            n_held = sum(1 for t in touches if t["held"])

            # Forward returns
            returns_30 = [fwd_return(hist, t["date"], 30) for t in touches]
            returns_90 = [fwd_return(hist, t["date"], 90) for t in touches]
            returns_180 = [fwd_return(hist, t["date"], 180) for t in touches]
            returns_30 = [r for r in returns_30 if r is not None]
            returns_90 = [r for r in returns_90 if r is not None]
            returns_180 = [r for r in returns_180 if r is not None]

            c1, c2, c3 = st.columns(3)
            c1.metric("Touches", str(n_total))
            c2.metric("Held", f"{n_held} / {n_total}")
            if returns_90:
                c3.metric("Avg 90d return", f"{np.mean(returns_90):+.1f}%")

            # ── Condition comparison ───────────────────────────────
            st.markdown("##### How today compares")

            current_rsi = float(bar.iloc[0].get("rsi_14", 0)) if pd.notnull(
                bar.iloc[0].get("rsi_14")) else None
            current_vol = float(bar.iloc[0].get("rel_volume", 0)) if pd.notnull(
                bar.iloc[0].get("rel_volume")) else None
            latest_m = macro.dropna(subset=["vix_close"])
            current_vix = float(latest_m.iloc[-1]["vix_close"]) if not latest_m.empty else None

            # Build comparison as a DataFrame — Streamlit renders this natively
            comp_data = []
            n_match = 0
            n_amber = 0
            n_miss = 0

            if current_vix is not None:
                if current_vix < 20:
                    verdict = "✅ Recovery zone"
                    n_match += 1
                elif current_vix < 25:
                    verdict = "⚠️ Amber"
                    n_amber += 1
                else:
                    verdict = "❌ Stress zone"
                    n_miss += 1
                comp_data.append({
                    "Condition": "VIX level",
                    "Recoveries": "14 – 19",
                    "Breakdowns": "> 25",
                    "Today": f"{current_vix:.1f}",
                    "Verdict": verdict,
                })

            if current_rsi is not None:
                if 25 <= current_rsi <= 45:
                    verdict = "✅ Recovery zone"
                    n_match += 1
                elif current_rsi < 25:
                    verdict = "⚠️ Very oversold"
                    n_amber += 1
                else:
                    verdict = "✅ Past bottom"
                    n_match += 1
                comp_data.append({
                    "Condition": "RSI at touch",
                    "Recoveries": "28 – 42",
                    "Breakdowns": "< 25",
                    "Today": f"{current_rsi:.0f}",
                    "Verdict": verdict,
                })

            if current_vol is not None:
                if current_vol >= 1.5:
                    verdict = "✅ Volume confirmed"
                    n_match += 1
                elif current_vol >= 1.0:
                    verdict = "⚠️ Below threshold"
                    n_amber += 1
                else:
                    verdict = "❌ Weak volume"
                    n_miss += 1
                comp_data.append({
                    "Condition": "Bounce volume",
                    "Recoveries": "> 1.5x",
                    "Breakdowns": "< 1.0x",
                    "Today": f"{current_vol:.1f}x",
                    "Verdict": verdict,
                })

            latest_cs = macro.dropna(subset=["credit_spread_hy"])
            if not latest_cs.empty:
                cs = float(latest_cs.iloc[-1]["credit_spread_hy"])
                if cs < 400:
                    verdict = "✅ Spreads tight"
                    n_match += 1
                elif cs < 500:
                    verdict = "⚠️ Widening"
                    n_amber += 1
                else:
                    verdict = "❌ Stress"
                    n_miss += 1
                comp_data.append({
                    "Condition": "Credit spreads",
                    "Recoveries": "< 400 bps",
                    "Breakdowns": "> 500 bps",
                    "Today": f"{cs:.0f} bps",
                    "Verdict": verdict,
                })

            if comp_data:
                df_comp = pd.DataFrame(comp_data)
                st.dataframe(df_comp, use_container_width=True, hide_index=True)

                # Summary
                total_conds = n_match + n_amber + n_miss
                st.markdown(
                    f"**Match: {n_match}** · Amber: {n_amber} · Miss: {n_miss} · "
                    f"Closest to: **{'recovery' if n_match >= n_miss else 'breakdown'}** pattern")

            # ── Forward returns ────────────────────────────────────
            if returns_30 or returns_90 or returns_180:
                st.markdown("##### Forward returns from prior touches")
                c1, c2, c3 = st.columns(3)
                if returns_30:
                    avg = np.mean(returns_30)
                    wins = sum(1 for r in returns_30 if r > 0)
                    c1.metric("30-day avg", f"{avg:+.1f}%",
                              delta=f"Win {wins}/{len(returns_30)}")
                if returns_90:
                    avg = np.mean(returns_90)
                    wins = sum(1 for r in returns_90 if r > 0)
                    c2.metric("90-day avg", f"{avg:+.1f}%",
                              delta=f"Win {wins}/{len(returns_90)}")
                if returns_180:
                    avg = np.mean(returns_180)
                    wins = sum(1 for r in returns_180 if r > 0)
                    c3.metric("180-day avg", f"{avg:+.1f}%",
                              delta=f"Win {wins}/{len(returns_180)}")

    # ── Bottom line ────────────────────────────────────────────────
    if ANTHROPIC_KEY:
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=ANTHROPIC_KEY)
            context = {
                "ticker": ticker, "price": price,
                "support": near_s, "resistance": near_r,
                "rsi": current_rsi, "vix": current_vix,
                "score": float(list_row.get("composite_score", 0)),
                "reason": list_row.get("reason", ""),
                "touches": len(touches) if 'touches' in dir() else 0,
                "held": n_held if 'n_held' in dir() else 0,
            }
            with st.spinner("Writing synthesis..."):
                resp = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=350,
                    system="Write a 3-4 sentence bottom-line synthesis for this stock setup. "
                           "Hedged-observational tone. Never say buy/sell/hold. Use numbers. "
                           "End with what would change the picture. Plain text only.",
                    messages=[{"role": "user", "content": json.dumps(context, default=str)}],
                )
                synthesis = resp.content[0].text
                st.info(f"**Bottom line**\n\n{synthesis}")
        except Exception as e:
            st.caption(f"Synthesis unavailable: {e}")

    # ── Fundamentals snapshot ──────────────────────────────────────
    if not fund.empty:
        st.markdown("##### Fundamentals")
        f = fund.iloc[0]
        fc1, fc2, fc3, fc4 = st.columns(4)
        pe = f.get("pe_trailing")
        ev = f.get("ev_ebitda")
        roe = f.get("return_on_equity")
        pm = f.get("profit_margin")
        fc1.metric("P/E", f"{pe:.1f}" if pd.notnull(pe) else "—")
        fc2.metric("EV/EBITDA", f"{ev:.1f}" if pd.notnull(ev) else "—")
        fc3.metric("ROE", f"{roe:.1%}" if pd.notnull(roe) else "—")
        fc4.metric("Margin", f"{pm:.1%}" if pd.notnull(pm) else "—")

    st.divider()
    st.caption("Not financial advice · Historical patterns are not predictive")


# ── Main ───────────────────────────────────────────────────────────
if st.session_state.selected_ticker:
    df = load_lists(st.session_state.list_type)
    row = df[df["ticker"] == st.session_state.selected_ticker]
    if not row.empty:
        render_detail(st.session_state.selected_ticker, row.iloc[0])
    else:
        st.warning("Ticker not found")
        if st.button("← Back"):
            st.session_state.selected_ticker = None
            st.rerun()
else:
    # ── List view ──────────────────────────────────────────────────
    col_t, col_tog = st.columns([3, 1])
    with col_t:
        st.title(f"{'Bullish' if st.session_state.list_type == 'bullish' else 'Bearish'} shortlist")
    with col_tog:
        toggle = st.radio("", ["Bullish", "Bearish"],
                          index=0 if st.session_state.list_type == "bullish" else 1,
                          horizontal=True, label_visibility="collapsed")
        if toggle.lower() != st.session_state.list_type:
            st.session_state.list_type = toggle.lower()
            st.session_state.show_all = False
            st.rerun()

    df = load_lists(st.session_state.list_type)

    if df.empty:
        st.info("No stocks on this list today. Run build_dashboard.py to populate.")
    else:
        st.caption(f"{len(df)} names · {df.iloc[0]['list_date']}")

        # Top 10 rich
        for _, row in df.head(10).iterrows():
            render_row(row, int(row.get("rank", 0)), rich=True)

        # Expand
        if len(df) > 10:
            if not st.session_state.show_all:
                if st.button(f"Show {len(df) - 10} more →", use_container_width=True):
                    st.session_state.show_all = True
                    st.rerun()
            else:
                st.divider()
                st.caption("11–30")
                for _, row in df.iloc[10:].iterrows():
                    render_row(row, int(row.get("rank", 0)), rich=False)

    st.divider()
    st.caption("Not financial advice · Historical patterns are not predictive · Do your own analysis")
