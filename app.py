"""
app.py — Streamlit chat UI. Takes a user question, routes to Claude with
tool-use, runs the chosen scenario function against Supabase, returns answer.

Run locally: streamlit run app.py
Deployed on Streamlit Cloud automatically when pushed to GitHub.
"""
import json
import os
import streamlit as st
from anthropic import Anthropic
from sqlalchemy import create_engine, text
from scenarios import SCENARIO_REGISTRY


def get_secret(key):
    """Read from env var first, fall back to Streamlit Secrets."""
    val = os.getenv(key)
    if val:
        return val
    try:
        return st.secrets[key]
    except Exception:
        return None


ANTHROPIC_KEY = get_secret("ANTHROPIC_API_KEY")
DATABASE_URL = get_secret("DATABASE_URL")

if not ANTHROPIC_KEY:
    st.error("ANTHROPIC_API_KEY is not set. Add it in Streamlit Cloud → Settings → Secrets.")
    st.stop()
if not DATABASE_URL:
    st.error("DATABASE_URL is not set. Add it in Streamlit Cloud → Settings → Secrets.")
    st.stop()

client = Anthropic(api_key=ANTHROPIC_KEY)
MODEL = "claude-sonnet-4-5"


TOOLS = [
    {
        "name": "consecutive_down_periods",
        "description": (
            "Find historical instances where a ticker had N consecutive down "
            "periods (daily/weekly/monthly) and measure forward returns. "
            "Use this for questions like 'after 3 down weeks in a row', "
            "'after a 5-day losing streak', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol, uppercase"},
                "n_periods": {"type": "integer", "default": 3},
                "freq": {
                    "type": "string",
                    "enum": ["D", "W", "M"],
                    "description": "D=daily, W=weekly, M=monthly",
                    "default": "W",
                },
                "lookforward": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Forward horizons in periods. Default [1,4,13] for weekly.",
                },
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "intraday_drawdown_recovery",
        "description": (
            "Find sessions where a ticker dropped at least N% intraday from open, "
            "then measure how often it closed above the low. Use for questions like "
            "'when GOOG drops 5% intraday how often does it recover'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "threshold_pct": {"type": "number", "default": 5.0},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "post_earnings_drop_recovery",
        "description": (
            "Find earnings events where the stock dropped at least X% on the reaction "
            "day, then measure how long until it recovered Y% from its post-earnings low."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "drop_threshold_pct": {"type": "number", "default": 5.0},
                "recovery_threshold_pct": {"type": "number", "default": 15.0},
                "max_days": {"type": "integer", "default": 252},
            },
            "required": ["ticker"],
        },
    },
]

SYSTEM_PROMPT = """You are an equity research assistant. The user asks about historical stock behavior. You have scenario functions that query a Postgres database of daily OHLCV bars and earnings dates.

For each question:
1. Identify the right scenario function
2. Extract ticker and parameters
3. Call the function
4. Write a clear, concise answer — show key stats, mention sample size, add context (e.g. "sample is small, don't over-anchor")
5. Suggest 1-2 natural follow-up questions

Be honest about limitations: small samples, definitional choices, survivorship effects. Don't make trading recommendations — describe what the data shows."""


def run_tool(name, args):
    fn = SCENARIO_REGISTRY.get(name)
    if not fn:
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn(**args)
    except Exception as e:
        return {"error": str(e)}


def chat(user_message, history):
    messages = history + [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "tool_use":
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for tu in tool_uses:
                result = run_tool(tu.name, tu.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(result, default=str),
                })
            messages.append({"role": "user", "content": tool_results})
            continue
        text_blocks = [b.text for b in response.content if b.type == "text"]
        final = "\n".join(text_blocks)
        messages.append({"role": "assistant", "content": final})
        return final, messages


# --- UI ---
st.set_page_config(page_title="Equity Interrogator", page_icon="📈")
st.title("📈 Equity Interrogator")
st.caption("Historical conditional analysis. Powered by Supabase + Claude.")

if "history" not in st.session_state:
    st.session_state.history = []
if "display" not in st.session_state:
    st.session_state.display = []

for msg in st.session_state.display:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if user_input := st.chat_input("e.g. AAPL after 3 down weeks in a row"):
    st.session_state.display.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)
    with st.chat_message("assistant"):
        with st.spinner("Querying the database..."):
            reply, new_history = chat(user_input, st.session_state.history)
        st.markdown(reply)
        st.session_state.history = new_history
        st.session_state.display.append({"role": "assistant", "content": reply})

with st.sidebar:
    st.header("Example questions")
    st.markdown("""
- AAPL after 3 down weeks in a row
- When GOOG drops 5% intraday, how often does it close above the low?
- MSFT post-earnings drops of 5%+, how long to recover 15%?
- NVDA after 5 down days in a row
- How does QCOM behave after big intraday drops?
    """)
    st.header("Tickers available")
    try:
        engine = create_engine(DATABASE_URL.replace("postgres://", "postgresql://", 1), pool_pre_ping=True)
        with engine.connect() as con:
            result = con.execute(text("SELECT DISTINCT ticker FROM daily_bars ORDER BY ticker"))
            tickers = [r[0] for r in result]
        if tickers:
            st.code("\n".join(tickers))
        else:
            st.warning("No data yet. Run refresh_data.py.")
    except Exception as e:
        st.warning(f"Database not reachable: {e}")
