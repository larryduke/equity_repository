"""
app.py — Streamlit frontend.

Replaces the old chat-with-markdown UI. Now:
  1. User asks a question
  2. query_engine generates SQL, runs it
  3. response_formatter produces structured JSON
  4. This file renders each component type properly (cards, charts, tables)

This is Phase 1 — Streamlit-native. Phase 2 will swap this for React+FastAPI
with the same JSON contract, so component types stay stable.
"""
import os
import pandas as pd
import streamlit as st
from anthropic import Anthropic
from sqlalchemy import create_engine, text

from query_engine import answer as run_query
from response_formatter import format_response


# -----------------------------------------------------------------------------
# Config / secrets
# -----------------------------------------------------------------------------
def get_secret(key):
    val = os.getenv(key)
    if val:
        return val
    try:
        return st.secrets[key]
    except Exception:
        return None


ANTHROPIC_KEY = get_secret("ANTHROPIC_API_KEY")
DATABASE_URL = get_secret("DATABASE_URL")
QUERY_DATABASE_URL = get_secret("QUERY_DATABASE_URL") or DATABASE_URL  # read-only role URL

if not ANTHROPIC_KEY:
    st.error("ANTHROPIC_API_KEY missing. Add it in Streamlit Cloud → Settings → Secrets.")
    st.stop()
if not DATABASE_URL:
    st.error("DATABASE_URL missing. Add it in Streamlit Cloud → Settings → Secrets.")
    st.stop()

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if QUERY_DATABASE_URL.startswith("postgres://"):
    QUERY_DATABASE_URL = QUERY_DATABASE_URL.replace("postgres://", "postgresql://", 1)


@st.cache_resource
def get_clients():
    client = Anthropic(api_key=ANTHROPIC_KEY)
    engine = create_engine(QUERY_DATABASE_URL, pool_pre_ping=True)
    full_engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    return client, engine, full_engine


client, engine, full_engine = get_clients()


# -----------------------------------------------------------------------------
# Page setup + styling
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Equity Interrogator",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    /* Reduce default Streamlit chrome */
    .block-container { padding-top: 1.5rem; padding-bottom: 5rem; max-width: 1200px; }
    h1, h2, h3 { font-weight: 600; letter-spacing: -0.01em; }

    /* Headline */
    .insight-headline {
        font-size: 1.35rem;
        font-weight: 600;
        line-height: 1.4;
        margin: 1rem 0 1.5rem 0;
        color: var(--text-color);
    }

    /* Metric card grid */
    .metric-card {
        background: rgba(255,255,255,0.03);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 10px;
        padding: 1rem 1.2rem;
        margin: 0.25rem;
        min-height: 5.5rem;
        display: flex;
        flex-direction: column;
        justify-content: center;
    }
    .metric-card .label {
        font-size: 0.75rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        opacity: 0.65;
        margin-bottom: 0.3rem;
    }
    .metric-card .value {
        font-size: 1.6rem;
        font-weight: 600;
        line-height: 1;
    }
    .metric-card.positive .value { color: #22c55e; }
    .metric-card.negative .value { color: #ef4444; }
    .metric-card.warning  .value { color: #f59e0b; }

    /* Signal grid pills */
    .signal-pill {
        display: inline-block;
        padding: 0.2rem 0.6rem;
        border-radius: 12px;
        font-size: 0.75rem;
        font-weight: 600;
        text-transform: uppercase;
    }
    .signal-green  { background: rgba(34,197,94,0.15);  color: #22c55e; }
    .signal-yellow { background: rgba(245,158,11,0.15); color: #f59e0b; }
    .signal-red    { background: rgba(239,68,68,0.15);  color: #ef4444; }

    /* Caveat */
    .caveat {
        font-size: 0.85rem;
        opacity: 0.7;
        font-style: italic;
        border-left: 2px solid rgba(255,255,255,0.2);
        padding: 0.4rem 0 0.4rem 0.8rem;
        margin: 1.5rem 0 1rem 0;
    }

    /* Disclaimer */
    .disclaimer {
        font-size: 0.7rem;
        opacity: 0.5;
        text-align: center;
        padding: 2rem 0 1rem 0;
        letter-spacing: 0.03em;
    }

    /* Section dividers between past Q&A */
    .qa-divider {
        border-top: 1px solid rgba(255,255,255,0.08);
        margin: 2.5rem 0 1rem 0;
    }
</style>
""", unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# Component renderers
# -----------------------------------------------------------------------------
def render_metric_cards(comp):
    items = comp.get("items", [])
    if not items:
        return
    cols = st.columns(min(len(items), 6))
    for col, item in zip(cols, items):
        tone = item.get("tone", "neutral")
        with col:
            st.markdown(f"""
                <div class="metric-card {tone}">
                    <div class="label">{item.get('label', '')}</div>
                    <div class="value">{item.get('value', '')}</div>
                </div>
            """, unsafe_allow_html=True)


def render_bar_chart(comp):
    title = comp.get("title", "")
    data = comp.get("data", [])
    if not data:
        return
    if title:
        st.markdown(f"**{title}**")
    df = pd.DataFrame(data)
    if "label" in df.columns and "value" in df.columns:
        st.bar_chart(df.set_index("label")["value"], use_container_width=True, height=260)


def render_line_chart(comp):
    title = comp.get("title", "")
    series = comp.get("series", [])
    if not series:
        return
    if title:
        st.markdown(f"**{title}**")
    # Combine all series into wide DataFrame
    combined = None
    for s in series:
        pts = s.get("points", [])
        if not pts:
            continue
        df = pd.DataFrame(pts)
        if "x" not in df.columns or "y" not in df.columns:
            continue
        df = df.rename(columns={"y": s.get("name", "series")}).set_index("x")[[s.get("name", "series")]]
        combined = df if combined is None else combined.join(df, how="outer")
    if combined is not None:
        try:
            combined.index = pd.to_datetime(combined.index)
        except Exception:
            pass
        st.line_chart(combined, use_container_width=True, height=300)


def render_event_list(comp):
    title = comp.get("title", "")
    cols = comp.get("columns", [])
    rows = comp.get("rows", [])
    if not rows:
        return
    if title:
        st.markdown(f"**{title}**")
    df = pd.DataFrame([r.get("cells", []) for r in rows], columns=cols)
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_comparison_table(comp):
    title = comp.get("title", "")
    lh = comp.get("left_header", "A")
    rh = comp.get("right_header", "B")
    rows = comp.get("rows", [])
    if not rows:
        return
    if title:
        st.markdown(f"**{title}**")
    df = pd.DataFrame([
        {"": r.get("label", ""), lh: r.get("left", ""), rh: r.get("right", "")}
        for r in rows
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_signal_grid(comp):
    title = comp.get("title", "")
    signals = comp.get("signals", [])
    if not signals:
        return
    if title:
        st.markdown(f"**{title}**")
    # Lay out as 3 per row
    for i in range(0, len(signals), 3):
        cols = st.columns(3)
        for col, sig in zip(cols, signals[i:i + 3]):
            status = sig.get("status", "yellow")
            with col:
                st.markdown(f"""
                    <div class="metric-card">
                        <div style="display:flex; justify-content:space-between; align-items:start;">
                            <div class="label">{sig.get('name', '')}</div>
                            <span class="signal-pill signal-{status}">{status}</span>
                        </div>
                        <div class="value" style="font-size:1.4rem;">{sig.get('value', '')}</div>
                        <div style="font-size:0.78rem; opacity:0.65; margin-top:0.3rem;">
                            {sig.get('note', '')}
                        </div>
                    </div>
                """, unsafe_allow_html=True)


def render_ranking_list(comp):
    title = comp.get("title", "")
    items = comp.get("items", [])
    if not items:
        return
    if title:
        st.markdown(f"**{title}**")
    df = pd.DataFrame([
        {
            "#":         it.get("rank", ""),
            "Ticker":    it.get("ticker", ""),
            "Primary":   it.get("primary", ""),
            "Detail":    it.get("secondary", ""),
        }
        for it in items
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_plain_text(comp):
    st.markdown(comp.get("body", ""))


RENDERERS = {
    "metric_cards":      render_metric_cards,
    "bar_chart":         render_bar_chart,
    "line_chart":        render_line_chart,
    "event_list":        render_event_list,
    "comparison_table":  render_comparison_table,
    "signal_grid":       render_signal_grid,
    "ranking_list":      render_ranking_list,
    "plain_text":        render_plain_text,
}


def render_llm_fallback_block(llm):
    """Render the AI Knowledge fallback panel."""
    answer_text = llm.get("answer", "")
    what_data = llm.get("what_data_would_help", "")
    answer_type = llm.get("answer_type", "")
    type_labels = {
        "conceptual": "Conceptual question",
        "stock_specific": "Stock-specific",
        "macro_general": "Macro / general",
        "unanswerable": "Best effort",
    }
    type_label = type_labels.get(answer_type, "AI knowledge")
    extra = ""
    if what_data:
        extra = (
            '<div style="margin-top:14px;padding-top:12px;'
            'border-top:1px solid rgba(180,130,60,0.15);font-size:11px;'
            'color:var(--text-color,inherit);opacity:0.6;">'
            '<strong style="opacity:0.8;">What data would give a definitive '
            f'answer:</strong> {what_data}</div>'
        )
    st.markdown(f"""
        <div style="
            background: rgba(180, 130, 60, 0.05);
            border: 1px solid rgba(180, 130, 60, 0.25);
            border-radius: 12px;
            padding: 20px 22px;
            margin: 20px 0 12px;
        ">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;">
            <div style="font-size:10px;letter-spacing:0.12em;text-transform:uppercase;
                color:rgb(180,130,60);font-weight:600;
                background:rgba(180,130,60,0.12);padding:3px 8px;border-radius:4px;">
              AI Knowledge
            </div>
            <div style="font-size:11px;color:var(--text-color,inherit);opacity:0.6;">
              not from your database · {type_label}
            </div>
          </div>
          <div style="font-size:14px;line-height:1.7;
              color:var(--text-color,inherit);opacity:0.92;white-space:pre-wrap;">
            {answer_text}
          </div>
          {extra}
        </div>
    """, unsafe_allow_html=True)


def render_response(resp, question):
    st.markdown(f"<div class='insight-headline'>{resp.get('headline', '')}</div>",
                unsafe_allow_html=True)

    for comp in resp.get("components", []):
        ctype = comp.get("type")
        renderer = RENDERERS.get(ctype)
        if renderer:
            renderer(comp)
        else:
            st.warning(f"Unknown component type: {ctype}")

    # If response has an attached LLM fallback (e.g. empty SQL result),
    # render it below the components.
    llm = resp.get("llm_fallback")
    if llm and llm.get("answer"):
        render_llm_fallback_block(llm)

    # NEW: Conclusion synthesis — the most important part
    conclusion = resp.get("conclusion")
    if conclusion:
        st.markdown(f"""
            <div style="
                background: rgba(70, 130, 200, 0.06);
                border-left: 3px solid rgba(70, 130, 200, 0.6);
                border-radius: 6px;
                padding: 16px 20px;
                margin: 18px 0 8px;
                font-size: 15px;
                line-height: 1.65;
            ">
              <div style="
                  font-size: 10px;
                  letter-spacing: 0.12em;
                  text-transform: uppercase;
                  color: rgba(70, 130, 200, 0.95);
                  font-weight: 600;
                  margin-bottom: 8px;
              ">Bottom line</div>
              {conclusion}
            </div>
        """, unsafe_allow_html=True)

    caveat = resp.get("caveat")
    if caveat:
        st.markdown(f"<div class='caveat'>{caveat}</div>", unsafe_allow_html=True)

    follow_ups = resp.get("follow_ups") or []
    if follow_ups:
        st.markdown("**Try next**")
        cols = st.columns(len(follow_ups))
        for col, fu in zip(cols, follow_ups):
            with col:
                if st.button(fu, key=f"fu_{abs(hash(question + fu))}"):
                    st.session_state.pending_question = fu
                    st.rerun()


# -----------------------------------------------------------------------------
# Topbar — current market readings
# -----------------------------------------------------------------------------
@st.cache_data(ttl=300)
def get_topbar():
    try:
        with full_engine.connect() as con:
            row = con.execute(text("""
                SELECT date, vix_close, yield_curve_10y_2y, pct_above_ma200
                FROM market_indicators
                ORDER BY date DESC LIMIT 1
            """)).fetchone()
            spy = con.execute(text("""
                SELECT close, daily_return FROM daily_bars
                WHERE ticker = 'SPY' ORDER BY date DESC LIMIT 1
            """)).fetchone()
        return {
            "date": row[0] if row else None,
            "vix": row[1] if row else None,
            "yield_curve": row[2] if row else None,
            "breadth": row[3] if row else None,
            "spy_close": spy[0] if spy else None,
            "spy_change": spy[1] if spy else None,
        }
    except Exception:
        return {}


tb = get_topbar()


def topbar_metric(label, value, color=None):
    color_css = f"color: {color};" if color else ""
    return f"""
        <div style="display:flex; flex-direction:column; align-items:flex-start; padding: 0 1rem;">
            <div style="font-size: 0.65rem; opacity: 0.6; text-transform: uppercase; letter-spacing: 0.06em;">{label}</div>
            <div style="font-size: 1.05rem; font-weight: 600; {color_css}">{value}</div>
        </div>
    """


if tb:
    cols = st.columns([3, 1, 1, 1, 1])
    with cols[0]:
        st.markdown("### 📈 Equity Interrogator")
        st.caption(f"As of {tb.get('date', '—')}")
    with cols[1]:
        spy_chg = tb.get("spy_change")
        c = "#22c55e" if spy_chg and spy_chg > 0 else "#ef4444" if spy_chg and spy_chg < 0 else None
        st.markdown(topbar_metric(
            "SPY",
            f"${tb.get('spy_close', 0):.2f}  {spy_chg:+.2f}%" if spy_chg is not None and tb.get('spy_close') else "—",
            c,
        ), unsafe_allow_html=True)
    with cols[2]:
        v = tb.get("vix")
        c = "#ef4444" if v and v > 25 else "#f59e0b" if v and v > 18 else "#22c55e"
        st.markdown(topbar_metric("VIX", f"{v:.2f}" if v else "—", c), unsafe_allow_html=True)
    with cols[3]:
        yc = tb.get("yield_curve")
        c = "#ef4444" if yc is not None and yc < 0 else "#22c55e"
        st.markdown(topbar_metric("10Y-2Y", f"{yc:+.2f}" if yc is not None else "—", c),
                    unsafe_allow_html=True)
    with cols[4]:
        b = tb.get("breadth")
        c = "#22c55e" if b and b > 60 else "#f59e0b" if b and b > 35 else "#ef4444"
        st.markdown(topbar_metric("% SPX >200MA", f"{b:.0f}%" if b else "—", c),
                    unsafe_allow_html=True)


st.divider()


# -----------------------------------------------------------------------------
# Sidebar
# -----------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### Example questions")
    examples = [
        "AAPL after 3 down weeks in a row",
        "Tech stocks with RSI below 35 today",
        "When GOOG drops 5% intraday, how often does it close above the low?",
        "MSFT post-earnings drops of 5%, how long until 15% recovery?",
        "Show me all stocks more than 30% below their 200MA today",
        "Current market signals — are we oversold?",
        "Compare NVDA and AMD volatility over 2 years",
    ]
    for ex in examples:
        if st.button(ex, key=f"ex_{abs(hash(ex))}", use_container_width=True):
            st.session_state.pending_question = ex
            st.rerun()

    st.markdown("---")
    st.markdown("### Universe")
    try:
        with full_engine.connect() as con:
            row = con.execute(text("""
                SELECT COUNT(*) AS n,
                       COUNT(*) FILTER (WHERE in_sp500) AS sp500,
                       COUNT(*) FILTER (WHERE in_nasdaq100) AS ndx
                FROM tickers WHERE is_active
            """)).fetchone()
        if row:
            st.caption(f"{row[0]} tickers · {row[1]} in S&P 500 · {row[2]} in NDX")
    except Exception:
        pass

    st.markdown("---")
    st.markdown("### About")
    st.caption(
        "Phase 1 — text-to-SQL research tool. Ask any question the data can "
        "answer. Responses render as structured components, not text blocks."
    )


# -----------------------------------------------------------------------------
# History + input
# -----------------------------------------------------------------------------
if "history" not in st.session_state:
    st.session_state.history = []

if "pending_question" in st.session_state:
    pending = st.session_state.pop("pending_question")
else:
    pending = None

def render_friendly_error(error_info, question, sql):
    """Render a clean, styled error message — with rephrasing suggestions if available."""
    headline    = error_info.get("headline", "Something went wrong")
    body        = error_info.get("body", "")
    err_type    = error_info.get("type", "generic")
    diagnosis   = error_info.get("diagnosis")
    rephrasings = error_info.get("rephrasings") or []

    type_label = {
        "timeout": "Query timeout",
        "sql": "Query couldn't be built",
        "connection": "Connection issue",
        "generic": "Unable to answer",
    }.get(err_type, "Issue")

    # Use diagnosis as the headline if we have a smart one, otherwise the generic
    display_headline = diagnosis if diagnosis else headline

    html = f"""
    <div style="
        background: rgba(218, 80, 80, 0.06);
        border: 1px solid rgba(218, 80, 80, 0.25);
        border-radius: 12px;
        padding: 20px 22px;
        margin: 12px 0;
    ">
      <div style="
          font-size: 11px;
          letter-spacing: 0.1em;
          text-transform: uppercase;
          color: #DA5050;
          font-weight: 500;
          margin-bottom: 8px;
      ">{type_label}</div>
      <div style="
          font-size: 16px;
          font-weight: 500;
          color: var(--text-color, inherit);
          margin-bottom: 6px;
          line-height: 1.4;
      ">{display_headline}</div>
      <div style="
          font-size: 13px;
          line-height: 1.55;
          color: var(--text-color, inherit);
          opacity: 0.7;
      ">{body if not diagnosis else "Try one of the rephrased questions below — they're more likely to return results."}</div>
    </div>
    """
    st.markdown(html, unsafe_allow_html=True)

    # If we have rephrased suggestions, show them as clickable buttons
    if rephrasings:
        st.markdown("""
            <div style="
                font-size: 11px;
                letter-spacing: 0.08em;
                text-transform: uppercase;
                color: var(--text-color, inherit);
                opacity: 0.6;
                margin: 16px 0 8px;
                font-weight: 500;
            ">Try one of these instead</div>
        """, unsafe_allow_html=True)

        for i, rephrased in enumerate(rephrasings):
            key = f"reph_{abs(hash(question + rephrased))}_{i}"
            if st.button(rephrased, key=key, use_container_width=True):
                st.session_state.pending_question = rephrased
                st.rerun()

    # If we have an LLM fallback answer, show it CLEARLY LABELED as AI knowledge
    llm = error_info.get("llm_fallback")
    if llm and llm.get("answer"):
        answer_text = llm["answer"]
        what_data = llm.get("what_data_would_help", "")
        answer_type = llm.get("answer_type", "")

        type_labels = {
            "conceptual": "Conceptual question",
            "stock_specific": "Stock-specific",
            "macro_general": "Macro / general",
            "unanswerable": "Best effort",
        }
        type_label = type_labels.get(answer_type, "AI knowledge")

        st.markdown(f"""
            <div style="
                background: rgba(180, 130, 60, 0.05);
                border: 1px solid rgba(180, 130, 60, 0.25);
                border-radius: 12px;
                padding: 20px 22px;
                margin: 20px 0 12px;
            ">
              <div style="
                  display: flex;
                  align-items: center;
                  gap: 8px;
                  margin-bottom: 12px;
              ">
                <div style="
                    font-size: 10px;
                    letter-spacing: 0.12em;
                    text-transform: uppercase;
                    color: rgb(180, 130, 60);
                    font-weight: 600;
                    background: rgba(180, 130, 60, 0.12);
                    padding: 3px 8px;
                    border-radius: 4px;
                ">AI Knowledge</div>
                <div style="
                    font-size: 11px;
                    color: var(--text-color, inherit);
                    opacity: 0.6;
                ">not from your database · {type_label}</div>
              </div>
              <div style="
                  font-size: 14px;
                  line-height: 1.7;
                  color: var(--text-color, inherit);
                  opacity: 0.92;
                  white-space: pre-wrap;
              ">{answer_text}</div>
              {'<div style="margin-top:14px;padding-top:12px;border-top:1px solid rgba(180,130,60,0.15);font-size:11px;color:var(--text-color,inherit);opacity:0.6;"><strong style="opacity:0.8;">What data would give a definitive answer:</strong> ' + what_data + '</div>' if what_data else ''}
            </div>
        """, unsafe_allow_html=True)


# Render past Q&A
for entry in st.session_state.history:
    st.markdown(f"<div class='qa-divider'></div>", unsafe_allow_html=True)
    st.markdown(f"**You asked:** _{entry['question']}_")

    # Render either the successful response or a styled error
    if entry.get("error"):
        render_friendly_error(entry["error"], entry["question"], entry.get("sql", ""))
    elif entry.get("response"):
        render_response(entry["response"], entry["question"])

    # Only show SQL expander if there was SQL to show
    if entry.get("sql"):
        with st.expander("SQL"):
            st.code(entry.get("sql", ""), language="sql")
            if entry.get("elapsed_ms"):
                st.caption(f"{entry['elapsed_ms']} ms")

# Chat input
typed = st.chat_input("Ask anything about historical stock behavior…")
question = pending or typed

if question:
    error_for_history = None
    response = None
    sql = ""

    try:
        with st.spinner("Running query…"):
            result = run_query(question, client, engine)
        sql = result.get("sql", "")
        df = result.get("df")
        err = result.get("error")
        rephrasings_from_engine = result.get("rephrasings") or []
        diagnosis_from_engine = result.get("diagnosis")

        # If the query engine returned rephrasings, SQL couldn't be built.
        # Show diagnosis + rephrasings + LLM fallback all together.
        llm_fallback_data = result.get("llm_fallback")
        if rephrasings_from_engine or llm_fallback_data:
            error_for_history = {
                "type": "sql",
                "headline": "I couldn't write valid SQL for that",
                "body": "Try one of the rephrased questions below.",
                "diagnosis": diagnosis_from_engine,
                "rephrasings": rephrasings_from_engine,
                "llm_fallback": llm_fallback_data,
            }
            response = None
        else:
            llm_fb = result.get("llm_fallback")  # may be set when df is empty
            with st.spinner("Formatting response…"):
                response = format_response(
                    client, question, sql, df, error=err, llm_fallback=llm_fb
                )

    except Exception as e:
        # Translate raw exceptions into clean error responses
        msg = str(e)
        # Detect common error types and give helpful messages
        if "timeout" in msg.lower() or "statement_timeout" in msg.lower():
            error_for_history = {
                "type": "timeout",
                "headline": "That query took too long",
                "body": "The database timed out before completing this query. "
                        "Try narrowing the scope — fewer tickers, a shorter "
                        "date range, or a more specific question.",
            }
        elif "syntax error" in msg.lower() or "does not exist" in msg.lower():
            error_for_history = {
                "type": "sql",
                "headline": "I couldn't write valid SQL for that",
                "body": "The query I tried to generate didn't compile against "
                        "the database. This sometimes happens with ambiguous "
                        "questions — try rephrasing more specifically.",
            }
        elif "connection" in msg.lower() or "operationalerror" in msg.lower():
            error_for_history = {
                "type": "connection",
                "headline": "Couldn't reach the database",
                "body": "The connection to the data store failed. This is "
                        "usually temporary — retry in a moment.",
            }
        else:
            error_for_history = {
                "type": "generic",
                "headline": "Something went wrong with that question",
                "body": "I couldn't complete this analysis. The most common "
                        "fixes: try rephrasing the question more specifically, "
                        "or simplify the time range or ticker set.",
            }

    # If run_query came back with rephrasings (SQL build failed cleanly),
    # promote them into the error_for_history so the renderer can show them.
    if response is None and 'result' in dir() and result and result.get("rephrasings"):
        if not error_for_history:
            error_for_history = {"type": "sql"}
        error_for_history["diagnosis"]   = result.get("diagnosis")
        error_for_history["rephrasings"] = result.get("rephrasings")

    st.session_state.history.append({
        "question": question,
        "response": response,
        "error": error_for_history,
        "sql": sql,
        "elapsed_ms": result.get("elapsed_ms") if response else None,
    })
    st.rerun()


# Disclaimer
st.markdown(
    "<div class='disclaimer'>NOT FINANCIAL ADVICE — this is a research tool. "
    "Historical patterns are not predictive. Sample sizes are often small. "
    "Do your own analysis.</div>",
    unsafe_allow_html=True,
)
