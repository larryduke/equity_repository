"""
import streamlit as st
app.py — Streamlit chat UI. Routes user questions to Claude, which picks
the right scenario function, runs it, and writes the answer.

Usage:
    streamlit run app.py
"""
import json
import os
import streamlit as st
from anthropic import Anthropic
from dotenv import load_dotenv
from scenarios import SCENARIO_REGISTRY

load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY") or st.secrets.get("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-5"  # fast + cheap; bump to opus if you want more nuanced replies

# Describe each scenario function as a tool that Claude can call
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
                "n_periods": {"type": "integer", "description": "Number of consecutive down periods", "default": 3},
                "freq": {
                    "type": "string",
                    "enum": ["D", "W", "M"],
                    "description": "Period frequency: D=daily, W=weekly, M=monthly",
                    "default": "W",
                },
                "lookforward": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Forward horizons in periods of the chosen frequency. Default: [1,4,13] for weekly.",
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
                "threshold_pct": {"type": "number", "description": "Min intraday drop %", "default": 5.0},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "post_earnings_drop_recovery",
        "description": (
            "Find earnings events where the stock dropped at least X% on the "
            "reaction day, then measure how long until it recovered Y% from its "
            "post-earnings low. Use for 'how long does MSFT take to recover "
            "after an earnings drop' type questions."
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

SYSTEM_PROMPT = """You are an equity research assistant. The user asks questions about historical stock behavior. You have access to scenario functions that query a local database of daily OHLCV bars and earnings dates.

When a user asks a question:
1. Identify which scenario function fits best
2. Extract the ticker and parameters from their question
3. Call the function
4. Write a clear, concise answer using the results — show key stats, mention sample size, and add brief context (e.g. "sample is small, don't over-anchor")
5. Suggest 1-2 natural follow-up questions

Be honest about limitations: small sample sizes, definitional choices, survivorship effects. Don't make trading recommendations — describe what the data shows."""


def run_tool(name, args):
    """Execute a scenario function and return JSON-serializable result."""
    fn = SCENARIO_REGISTRY.get(name)
    if not fn:
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn(**args)
    except Exception as e:
        return {"error": str(e)}


def chat(user_message, history):
    """Send a message to Claude with tool use, return the final text response."""
    messages = history + [{"role": "user", "content": user_message}]

    # Loop until Claude returns a final text answer (no more tool calls)
    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            # Claude wants to call a tool; run it and feed the result back
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

        # Final response
        text_blocks = [b.text for b in response.content if b.type == "text"]
        final = "\n".join(text_blocks)
        messages.append({"role": "assistant", "content": final})
        return final, messages


# --- Streamlit UI ---
st.set_page_config(page_title="Equity Interrogator", page_icon="📈")
st.title("📈 Equity Interrogator")
st.caption("Ask questions about historical stock behavior. Powered by your local DuckDB + Claude.")

if "history" not in st.session_state:
    st.session_state.history = []
if "display" not in st.session_state:
    st.session_state.display = []

# Show conversation
for msg in st.session_state.display:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Input
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

# Sidebar with examples
with st.sidebar:
    st.header("Example questions")
    st.markdown("""
- AAPL after 3 down weeks in a row
- When GOOG drops 5% intraday, how often does it close above the low?
- MSFT post-earnings drops of 5%+, how long to recover 15%?
- NVDA after 5 down days in a row
- How does QCOM behave after big intraday drops?
    """)
    st.header("Tickers in DB")
    st.caption("Edit setup_db.py to add more, then re-run it.")
    try:
        import duckdb
        con = duckdb.connect("equity.duckdb", read_only=True)
        tickers = con.execute("SELECT DISTINCT ticker FROM daily_bars ORDER BY ticker").fetchall()
        st.code("\n".join(t[0] for t in tickers))
        con.close()
    except Exception as e:
        st.warning("Run `python setup_db.py` first to populate the database.")
