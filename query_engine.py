"""
query_engine.py — Text-to-SQL layer.

Takes a user question, sends it to Claude with the full schema in the system
prompt, gets SQL back, executes it safely against Supabase (read-only role,
statement timeout, row limit), returns the result as a DataFrame plus the
SQL that was run.

On query error, sends the error back to Claude for one retry.
"""
import os
import re
import time
import pandas as pd
from anthropic import Anthropic
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError


MODEL = "claude-sonnet-4-5"
MAX_ROWS = 5000
RETRY_LIMIT = 1


SCHEMA_DOC = """
You have read-only access to a Postgres database. Tables and columns:

-- One row per ticker per trading day. Includes pre-calculated indicators.
daily_bars (
    ticker          VARCHAR,
    date            DATE,
    open, high, low, close   DOUBLE PRECISION,
    volume          BIGINT,

    -- Returns (already in %, e.g. 1.5 means +1.5%)
    daily_return    DOUBLE,
    weekly_return   DOUBLE,    -- vs close 5 trading days ago
    monthly_return  DOUBLE,    -- vs close 21 trading days ago

    -- Moving averages on close
    ma_20, ma_50, ma_100, ma_200    DOUBLE,

    -- Pre-calculated % distance from each MA (negative = below)
    pct_vs_ma20, pct_vs_ma50, pct_vs_ma100, pct_vs_ma200   DOUBLE,

    -- 52-week range
    high_52w, low_52w    DOUBLE,
    pct_vs_52w_high      DOUBLE,    -- typically 0 or negative
    pct_vs_52w_low       DOUBLE,    -- typically 0 or positive

    -- Volume
    vol_20d_avg          DOUBLE,
    rel_volume           DOUBLE,    -- volume / vol_20d_avg

    -- Momentum
    rsi_14, rsi_5        DOUBLE,    -- 0-100, <30 oversold, >70 overbought
    macd_line, macd_signal, macd_histogram   DOUBLE,

    -- Volatility / bands
    bb_upper, bb_lower   DOUBLE,
    bb_pct               DOUBLE,    -- 0 = at lower band, 1 = at upper
    atr_14               DOUBLE,

    -- Structure
    gap_pct              DOUBLE     -- open vs prior close, in %
)
PRIMARY KEY (ticker, date)

-- Reference table for the ticker universe.
tickers (
    ticker          VARCHAR PRIMARY KEY,
    name            VARCHAR,
    exchange        VARCHAR,
    sector          VARCHAR,
    industry        VARCHAR,
    country         VARCHAR,
    currency        VARCHAR,
    in_sp500        BOOLEAN,
    in_sp400        BOOLEAN,
    in_sp600        BOOLEAN,
    in_nasdaq100    BOOLEAN,
    in_dow30        BOOLEAN,
    market_cap_usd  DOUBLE,
    is_active       BOOLEAN
)

-- Latest fundamentals snapshot. One row per ticker per date.
-- For "current P/E" queries, take the row with the most recent date for that ticker.
fundamentals (
    ticker              VARCHAR,
    date                DATE,
    market_cap          DOUBLE,
    enterprise_value    DOUBLE,
    pe_trailing         DOUBLE,
    pe_forward          DOUBLE,    -- may be NULL on Starter tier
    price_to_sales      DOUBLE,
    price_to_book       DOUBLE,
    ev_ebitda           DOUBLE,
    peg_ratio           DOUBLE,
    profit_margin       DOUBLE,
    operating_margin    DOUBLE,
    return_on_equity    DOUBLE,
    debt_to_equity      DOUBLE,
    current_ratio       DOUBLE,
    dividend_yield      DOUBLE,
    payout_ratio        DOUBLE,
    shares_outstanding  DOUBLE,
    short_interest_pct  DOUBLE     -- may be NULL on Starter tier
)
PRIMARY KEY (ticker, date)

-- Historical earnings events.
earnings_dates (
    ticker              VARCHAR,
    date                DATE,
    eps_estimate        DOUBLE,
    eps_actual          DOUBLE,
    eps_surprise_pct    DOUBLE,
    revenue_estimate    DOUBLE,
    revenue_actual      DOUBLE,
    revenue_surprise_pct DOUBLE,
    fiscal_period       VARCHAR,
    reporting_time      VARCHAR     -- 'bmo' (before market open) or 'amc' (after market close)
)
PRIMARY KEY (ticker, date)

-- Daily market-wide indicators.
market_indicators (
    date                DATE PRIMARY KEY,
    vix_close           DOUBLE,
    vix_ma20            DOUBLE,
    vix_ma50            DOUBLE,
    pct_above_ma200     DOUBLE,    -- % of S&P 500 stocks above their 200MA
    pct_above_ma50      DOUBLE,
    advance_decline_pct DOUBLE,    -- % of S&P 500 advancing today
    fed_funds_rate      DOUBLE,
    ten_year_yield      DOUBLE,
    two_year_yield      DOUBLE,
    yield_curve_10y_2y  DOUBLE,    -- 10Y minus 2Y; negative = inverted
    cpi_yoy             DOUBLE,
    unemployment_rate   DOUBLE
)

-- Discrete macro events (elections, Fed cycle markers, crises).
macro_events (
    id              INTEGER PRIMARY KEY,
    event_date      DATE,
    event_type      VARCHAR,     -- 'us_election', 'fed_cycle_start', 'fed_cycle_end',
                                 -- 'crisis_start', 'crisis_low'
    event_name      VARCHAR,
    country         VARCHAR,
    metadata        JSONB        -- flexible; e.g. {"winner": "Biden", "drawdown_pct": -34}
)
"""


SYSTEM_PROMPT = f"""You are a SQL generator for an equity research tool. The user asks a question about historical stock behavior or current market conditions. You generate ONE PostgreSQL query that answers it.

{SCHEMA_DOC}

# Rules

1. Output ONLY the SQL query. No markdown, no commentary, no ```sql fences. Just the raw SQL.
2. ALWAYS include `LIMIT {MAX_ROWS}` unless the query is a pure aggregate (no per-row results).
3. Read-only — no INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, GRANT, TRUNCATE.
4. Use uppercase ticker symbols ('AAPL' not 'aapl').
5. Prefer pre-calculated columns over recomputing. Use `pct_vs_ma200` rather than `(close/ma_200 - 1)*100`. Use `rsi_14` rather than recomputing RSI.
6. For "consecutive N down days/weeks" patterns, use window functions with LAG.
7. For "after event X, what happened?" patterns, use a CTE to find event dates, then LATERAL JOIN or correlated subquery to grab forward returns.
8. Show sample size in your results when possible — include COUNT(*) AS n.
9. Round numeric outputs to 2 decimals using ROUND(value::numeric, 2).
10. When the user mentions a ticker without specifying timeframe, default to last 5 years.
11. If the question is ambiguous, pick the most reasonable default and proceed. Don't ask for clarification.
12. If a question asks for visual data (e.g. "show the distribution of returns"), return the underlying rows — the frontend handles charting.

# Examples

Question: "AAPL after 3 down weeks in a row"
SQL:
WITH weekly AS (
  SELECT
    date_trunc('week', date)::date AS week_start,
    (array_agg(close ORDER BY date DESC))[1] AS week_close
  FROM daily_bars
  WHERE ticker = 'AAPL'
  GROUP BY date_trunc('week', date)
),
flagged AS (
  SELECT
    week_start,
    week_close,
    LAG(week_close, 1) OVER (ORDER BY week_start) AS w1,
    LAG(week_close, 2) OVER (ORDER BY week_start) AS w2,
    LAG(week_close, 3) OVER (ORDER BY week_start) AS w3
  FROM weekly
),
signals AS (
  SELECT week_start, week_close
  FROM flagged
  WHERE week_close < w1 AND w1 < w2 AND w2 < w3
)
SELECT
  s.week_start AS signal_date,
  ROUND(s.week_close::numeric, 2) AS signal_close,
  ROUND(((f1.week_close / s.week_close - 1) * 100)::numeric, 2) AS return_1w_pct,
  ROUND(((f4.week_close / s.week_close - 1) * 100)::numeric, 2) AS return_4w_pct,
  ROUND(((f13.week_close / s.week_close - 1) * 100)::numeric, 2) AS return_13w_pct
FROM signals s
LEFT JOIN weekly f1  ON f1.week_start  = s.week_start + INTERVAL '7 days'
LEFT JOIN weekly f4  ON f4.week_start  = s.week_start + INTERVAL '28 days'
LEFT JOIN weekly f13 ON f13.week_start = s.week_start + INTERVAL '91 days'
ORDER BY s.week_start DESC
LIMIT {MAX_ROWS};

Question: "tech stocks oversold right now"
SQL:
SELECT
  d.ticker,
  t.name,
  ROUND(d.close::numeric, 2) AS close,
  ROUND(d.rsi_14::numeric, 1) AS rsi_14,
  ROUND(d.pct_vs_ma200::numeric, 1) AS pct_vs_ma200,
  ROUND(d.pct_vs_52w_high::numeric, 1) AS pct_vs_52w_high
FROM daily_bars d
JOIN tickers t ON t.ticker = d.ticker
WHERE d.date = (SELECT MAX(date) FROM daily_bars)
  AND t.sector = 'Technology'
  AND d.rsi_14 < 35
ORDER BY d.rsi_14 ASC
LIMIT {MAX_ROWS};

Question: "current VIX and yield curve"
SQL:
SELECT
  date,
  ROUND(vix_close::numeric, 2) AS vix,
  ROUND(yield_curve_10y_2y::numeric, 2) AS yield_curve,
  ROUND(fed_funds_rate::numeric, 2) AS fed_funds,
  ROUND(pct_above_ma200::numeric, 1) AS pct_sp500_above_200ma
FROM market_indicators
WHERE date = (SELECT MAX(date) FROM market_indicators);

-- Sector-level daily aggregates (calculated from daily_bars + tickers).
-- Use for rotation analysis and sector health queries.
sector_indicators (
    date                DATE,
    sector              VARCHAR,
    n_stocks            INTEGER,       -- stocks in the aggregate
    avg_rsi_14          DOUBLE,        -- avg RSI across sector
    avg_macd_histogram  DOUBLE,        -- positive = momentum building
    pct_rsi_oversold    DOUBLE,        -- % of stocks RSI < 35
    pct_rsi_overbought  DOUBLE,        -- % of stocks RSI > 65
    pct_above_ma50      DOUBLE,        -- breadth: % above 50MA
    pct_above_ma200     DOUBLE,
    avg_pct_vs_ma50     DOUBLE,        -- avg distance from 50MA
    avg_pct_vs_ma200    DOUBLE,
    avg_return_5d       DOUBLE,        -- avg 5-day return across sector
    avg_return_20d      DOUBLE,
    avg_rel_volume      DOUBLE         -- > 1.2 = volume expansion
)
PRIMARY KEY (date, sector)

-- Pairwise sector rotation scores. sector_a is the candidate "rotating into" sector.
-- rotation_score 0-100: >= 65 strong signal, 45-65 early signal, < 45 noise.
sector_relative_strength (
    date            DATE,
    sector_a        VARCHAR,           -- potential rotation destination
    sector_b        VARCHAR,           -- potential rotation source
    rs_ratio_5d     DOUBLE,            -- sector_a minus sector_b 5d return
    rs_ratio_20d    DOUBLE,
    rs_trend_20d    DOUBLE,            -- positive = A accelerating vs B
    rotation_score  DOUBLE,            -- 0-100 composite confidence score
    signal          VARCHAR,           -- 'strong_into_a', 'early_into_a',
                                       -- 'neutral', 'early_into_b', 'strong_into_b'
    score_momentum  DOUBLE,            -- RS ratio trend (max 25)
    score_breadth   DOUBLE,            -- breadth divergence (max 20)
    score_rsi       DOUBLE,            -- RSI divergence (max 20)
    score_volume    DOUBLE,            -- volume expansion (max 15)
    score_macro     DOUBLE             -- macro regime alignment (max 20)
)
PRIMARY KEY (date, sector_a, sector_b)

Now generate SQL for the user's question. Remember: SQL only, no commentary."""


_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|truncate|alter|create|grant|revoke|"
    r"vacuum|analyze|copy|comment\s+on|reindex)\b",
    re.IGNORECASE,
)


def _strip_fences(s):
    """Remove markdown code fences if Claude added them despite instructions."""
    s = s.strip()
    if s.startswith("```"):
        # Drop the first line (```sql or ```) and any trailing ```
        lines = s.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines)
    return s.strip()


def _validate(sql):
    """Cheap safety guard. Real safety comes from the read-only DB role."""
    if _FORBIDDEN.search(sql):
        raise ValueError("Query contains forbidden keyword")
    if sql.count(";") > 1:
        raise ValueError("Multiple statements not allowed")


def generate_sql(client, question, error_context=None):
    """Ask Claude for SQL. Returns the cleaned SQL string."""
    user_msg = question
    if error_context:
        user_msg = (
            f"Previous SQL attempt:\n{error_context['sql']}\n\n"
            f"It failed with this error:\n{error_context['error']}\n\n"
            f"Original question: {question}\n\n"
            f"Generate a corrected SQL query."
        )

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = "".join(b.text for b in response.content if b.type == "text")
    return _strip_fences(raw)


def execute_sql(engine, sql, timeout_s=20):
    """Run the SQL and return a DataFrame. Raises on error."""
    _validate(sql)
    with engine.connect() as con:
        # Per-query statement timeout as a belt-and-suspenders defense.
        con.execute(text(f"SET LOCAL statement_timeout = '{int(timeout_s * 1000)}ms'"))
        df = pd.read_sql(text(sql), con)
    return df


def answer(question, client, engine):
    """The full loop. Returns dict with: question, sql, df, error (if any)."""
    sql = generate_sql(client, question)
    started = time.time()
    try:
        df = execute_sql(engine, sql)
        return {
            "question": question,
            "sql": sql,
            "df": df,
            "error": None,
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    except (SQLAlchemyError, ValueError) as e:
        # One retry
        try:
            sql2 = generate_sql(client, question,
                                error_context={"sql": sql, "error": str(e)[:500]})
            df = execute_sql(engine, sql2)
            return {
                "question": question,
                "sql": sql2,
                "df": df,
                "error": None,
                "elapsed_ms": int((time.time() - started) * 1000),
                "retried": True,
            }
        except Exception as e2:
            return {
                "question": question,
                "sql": sql,
                "df": None,
                "error": f"Query failed: {e2}",
                "elapsed_ms": int((time.time() - started) * 1000),
            }
