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

-- Political calendar: which party controls each branch of government.
-- Use for "under Republican/Democratic control" type questions.
political_calendar (
    start_date        DATE,
    end_date          DATE,          -- NULL = currently in effect
    country           VARCHAR,       -- 'US'
    body              VARCHAR,       -- 'presidency', 'house', 'senate'
    controlling_party VARCHAR,       -- 'Republican', 'Democratic'
    majority_seats    INTEGER,
    minority_seats    INTEGER,
    is_divided_govt   BOOLEAN,       -- presidency vs congress split party
    notes             VARCHAR
)

-- Earnings revision breadth per sector (weekly snapshot).
-- net_revision > 0 = more upgrades than downgrades = bullish earnings momentum.
earnings_revision_breadth (
    date            DATE,
    sector          VARCHAR,
    n_stocks        INTEGER,
    pct_raised_30d  DOUBLE,    -- % of stocks with EPS estimate raised
    pct_cut_30d     DOUBLE,    -- % of stocks with EPS estimate cut
    net_revision    DOUBLE,    -- pct_raised - pct_cut
    avg_revision_pct DOUBLE
)
PRIMARY KEY (date, sector)

-- Additional columns now available in market_indicators:
--   ppi_yoy          DOUBLE  -- Producer Price Index YoY%
--   ism_manufacturing DOUBLE -- ISM Manufacturing PMI (50 = neutral)
--   ism_services      DOUBLE -- ISM Services PMI
--   credit_spread_hy  DOUBLE -- High yield credit spread in basis points (wider = stress)
--   dxy               DOUBLE -- US Dollar Index (higher = stronger dollar)
--   initial_claims    DOUBLE -- Weekly initial jobless claims (thousands)
--   breakeven_10y     DOUBLE -- 10Y inflation breakeven rate %
--   put_call_ratio    DOUBLE -- CBOE put/call ratio (>1.2 = fear, <0.7 = complacency)
--   m2_yoy            DOUBLE -- M2 money supply YoY% growth

-- =============================================================================
-- PHASE 2 TABLES
-- =============================================================================

-- Political calendar: party control of US government branches 1993-present.
-- Use for: "under Republican/Democratic control", "divided government" queries.
political_calendar (
    start_date        DATE,
    end_date          DATE,          -- NULL = currently in effect
    country           VARCHAR,       -- 'US'
    body              VARCHAR,       -- 'presidency', 'house', 'senate'
    controlling_party VARCHAR,       -- 'Republican', 'Democratic'
    majority_seats    INTEGER,
    minority_seats    INTEGER,
    is_divided_govt   BOOLEAN,       -- TRUE when presidency + congress split
    notes             VARCHAR        -- e.g. 'Clinton', '104th Congress'
)

-- Insider transactions: SEC Form 4 filings (buys and sells by executives/directors).
-- Cluster buying (multiple insiders buying same stock) is historically significant.
insider_transactions (
    ticker            VARCHAR,
    date              DATE,
    insider_name      VARCHAR,
    role              VARCHAR,       -- 'CEO', 'CFO', 'Director', '10% Owner'
    transaction_type  VARCHAR,       -- 'Buy', 'Sell', 'Other'
    shares            DOUBLE,
    price_per_share   DOUBLE,
    value_usd         DOUBLE,
    shares_owned_after DOUBLE,
    filing_date       DATE
)

-- Short interest: biweekly/weekly snapshot of short positions.
-- short_pct_float > 15% = heavily shorted. days_to_cover > 5 = squeeze risk.
short_interest (
    ticker          VARCHAR,
    date            DATE,
    short_interest  DOUBLE,        -- total shares short
    short_pct_float DOUBLE,        -- % of float sold short
    days_to_cover   DOUBLE,        -- short ratio (days to cover at avg volume)
    change_pct      DOUBLE         -- WoW change in short interest %
)
PRIMARY KEY (ticker, date)

-- Analyst estimates: individual analyst ratings and price targets.
-- For consensus, use fundamentals.analyst_consensus, analyst_target_price.
analyst_estimates (
    ticker          VARCHAR,
    date            DATE,
    analyst_firm    VARCHAR,
    action          VARCHAR,       -- 'Upgrade', 'Downgrade', 'Initiate', 'Reiterate'
    rating_new      VARCHAR,       -- 'Buy', 'Hold', 'Sell', 'Outperform'
    rating_prior    VARCHAR,
    target_new      DOUBLE,
    target_prior    DOUBLE
)
PRIMARY KEY (ticker, date, analyst_firm)

-- Commodity prices: daily closes for major commodities.
commodity_prices (
    date        DATE,
    commodity   VARCHAR,           -- 'gold','silver','copper','oil_wti','natural_gas','wheat','corn'
    price       DOUBLE,
    currency    VARCHAR,           -- 'USD'
    unit        VARCHAR            -- 'per_oz','per_lb','per_barrel','per_bushel'
)
PRIMARY KEY (date, commodity)

-- Earnings revision breadth: weekly sector-level estimate momentum.
-- net_revision > 0 = upgrades dominating = bullish earnings momentum.
earnings_revision_breadth (
    date            DATE,
    sector          VARCHAR,
    n_stocks        INTEGER,
    pct_raised_30d  DOUBLE,        -- % of stocks with EPS estimate raised
    pct_cut_30d     DOUBLE,        -- % of stocks with EPS estimate cut
    net_revision    DOUBLE,        -- pct_raised - pct_cut (positive = bullish)
    avg_revision_pct DOUBLE
)
PRIMARY KEY (date, sector)

-- Sector rotation scores (pairwise). sector_a = destination, sector_b = source.
-- rotation_score: >= 65 strong signal, 45-65 early signal, < 45 noise.
sector_relative_strength (
    date            DATE,
    sector_a        VARCHAR,
    sector_b        VARCHAR,
    rs_ratio_5d     DOUBLE,
    rs_ratio_20d    DOUBLE,
    rs_trend_20d    DOUBLE,        -- positive = A accelerating vs B
    rotation_score  DOUBLE,        -- 0-100 composite
    signal          VARCHAR,       -- 'strong_into_a','early_into_a','neutral','early_into_b','strong_into_b'
    score_momentum  DOUBLE,        -- component scores (max: 25,20,20,15,20)
    score_breadth   DOUBLE,
    score_rsi       DOUBLE,
    score_volume    DOUBLE,
    score_macro     DOUBLE
)
PRIMARY KEY (date, sector_a, sector_b)

-- =============================================================================
-- EXTENDED market_indicators COLUMNS (Phase 2 additions):
-- ppi_yoy          DOUBLE  -- Producer Price Index YoY% (leads CPI by 2-3 months)
-- ism_manufacturing DOUBLE -- ISM Manufacturing PMI (>50 = expansion, <50 = contraction)
-- ism_services      DOUBLE -- ISM Services PMI
-- credit_spread_hy  DOUBLE -- High yield credit spread bps (>500 = stress, <300 = healthy)
-- credit_spread_ig  DOUBLE -- Investment grade credit spread bps
-- dxy               DOUBLE -- US Dollar Index (higher = stronger dollar)
-- initial_claims    DOUBLE -- Weekly initial jobless claims in thousands (leading indicator)
-- breakeven_10y     DOUBLE -- 10Y inflation breakeven % (market's inflation expectation)
-- put_call_ratio    DOUBLE -- CBOE total put/call (>1.2 = fear, <0.7 = complacency)
-- m2_yoy            DOUBLE -- M2 money supply YoY% growth
-- gold_price        DOUBLE -- Gold $/oz (safe haven)
-- copper_price      DOUBLE -- Copper $/lb (economic activity indicator)
-- oil_price_wti     DOUBLE -- WTI crude $/barrel
-- copper_gold_ratio DOUBLE -- copper/gold ratio (rising = risk-on, falling = risk-off)
-- =============================================================================

-- =============================================================================
-- EXTENDED fundamentals COLUMNS (Phase 2 additions):
-- analyst_target_price  DOUBLE  -- consensus price target
-- analyst_target_upside DOUBLE  -- % upside to consensus target
-- analyst_buy_count     INTEGER -- number of Buy/Outperform ratings
-- analyst_hold_count    INTEGER -- number of Hold/Neutral ratings
-- analyst_sell_count    INTEGER -- number of Sell/Underperform ratings
-- analyst_consensus     VARCHAR -- 'Buy', 'Hold', or 'Sell'
-- =============================================================================

-- Support / resistance levels detected via 5 methods (pivot, volume cluster,
-- round number, MA, flipped). Each level has a strength_score 0-100.
-- strength_tier: 'major' >= 70, 'moderate' 50-69, 'minor' 30-49, 'weak' < 30.
support_resistance_levels (
    ticker          VARCHAR,
    level_price     DOUBLE,
    level_type      VARCHAR,        -- 'support' or 'resistance'
    method          VARCHAR,        -- 'pivot','volume_cluster','round_number',
                                    -- 'moving_avg','flipped_level',
                                    -- or combos like 'pivot+volume_cluster'
    touch_count     INTEGER,        -- times price reversed near this level
    n_strong_touches INTEGER,       -- reversals on above-avg volume
    last_touch_date DATE,
    first_touch_date DATE,
    days_since_last_touch INTEGER,
    times_held      INTEGER,        -- touches where price reversed away
    times_broken    INTEGER,        -- touches where price broke through
    hold_rate       DOUBLE,         -- times_held / touch_count (0-1)
    avg_volume_at_touches DOUBLE,
    pct_distance_current DOUBLE,    -- % distance from current price (negative if below)
    is_active       BOOLEAN,        -- still in play?
    strength_score  DOUBLE,         -- 0-100 composite confidence
    strength_tier   VARCHAR,        -- 'major','moderate','minor','weak'
    calculated_date DATE,
    lookback_days   INTEGER
)


-- =============================================================================
-- DATA COVERAGE NOTES (READ CAREFULLY before writing queries)
-- =============================================================================
-- Different macro series have different historical depth on FRED:
--   * fed_funds_rate, ten_year_yield, two_year_yield, cpi_yoy, unemployment:
--     daily/monthly from 1990s — deep history available
--   * ppi_yoy: daily back to 1990s
--   * dxy, breakeven_10y: daily back to 2003-2006
--   * credit_spread_hy (BAMLH0A0HYM2): daily back to ~2008 only
--   * credit_spread_ig (BAMLC0A0CM): daily back to ~2008 only
--   * initial_claims: weekly back to 1967
--   * m2_yoy: monthly back to 1980s
--   * ism_manufacturing, ism_services: proxies via MANEMP/PAYEMS, deep history
--   * put_call_ratio: daily back to ~2007
--   * commodities (gold/silver/copper/oil/etc): daily back to ~2000
--   * vix_close: daily back to 1990
--
-- Stock price data (daily_bars) goes back to 2006 on average.
-- Earnings_dates back to ~2010.
-- Fundamentals are current snapshots (rolling, not historical depth).
--
-- WHEN A USER ASKS for historical patterns requiring data that may not be
-- present (e.g. "credit spreads above 500bps historically"), the SQL should
-- ALWAYS return any matching rows even if very few, and the response should
-- honestly note when the sample is small or the data window is limited
-- rather than claiming "no matching data."

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


def _clean_sql(sql):
    """Strip trailing semicolons and whitespace. Claude sometimes adds them."""
    # Remove trailing semicolons (one is fine, just strip it)
    return sql.strip().rstrip(";").strip()


def _validate(sql):
    """Cheap safety guard. Real safety comes from the read-only DB role."""
    if _FORBIDDEN.search(sql):
        raise ValueError("Query contains forbidden keyword")
    # After stripping the trailing semicolon, there should be no more
    if sql.count(";") > 0:
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
    sql = _clean_sql(sql)   # strip trailing semicolons before validation
    _validate(sql)
    with engine.connect() as con:
        # Per-query statement timeout as a belt-and-suspenders defense.
        con.execute(text(f"SET LOCAL statement_timeout = '{int(timeout_s * 1000)}ms'"))
        df = pd.read_sql(text(sql), con)
    return df


REPHRASE_PROMPT = """The user asked a finance database a question, but the
SQL generator couldn't produce a working query. Look at:

1. WHAT the user wanted (the question)
2. WHAT was tried (the failed SQL)
3. WHY it failed (the error)

Then propose 3 alternative phrasings that the user could ask instead.
The rephrasings must:
- Stay close to the user's actual intent
- Be more specific or narrower in scope where the original was too broad
- Use entities (tickers, sectors, dates) that exist in the schema
- Each be a complete question the user could click and re-ask

The database holds:
- US stocks (S&P 500, NDX, Dow, ~100 popular mid-caps) plus some EU indices
- 20 years of daily prices and indicators (RSI, MACD, MAs, etc.)
- Earnings history back to ~2010
- Sector rotation scores, support/resistance levels
- Macro indicators (VIX, FRED data) back to 2000
- Election dates, recessions, Fed cycles
- Composite scores for bullish/bearish setups

Return JSON only, no preamble:
{
  "diagnosis": "<one sentence on why the original failed in plain English>",
  "rephrasings": [
    "<rephrased question 1>",
    "<rephrased question 2>",
    "<rephrased question 3>"
  ]
}
"""


LLM_FALLBACK_PROMPT = """The user asked a finance question that the
database couldn't answer (either no data exists, or SQL couldn't be built).
You're now answering from general knowledge as a final fallback.

CRITICAL: This response will be clearly labeled to the user as "AI knowledge
(not from the database)." Be honest about that limitation.

Rules:
- Hedged-observational tone. Never recommend buy/sell/hold/DCA.
- 2-3 short paragraphs maximum.
- If the question is about a SPECIFIC TICKER and you don't have current/recent
  data, say so explicitly. Give general context only.
- If the question is conceptual or educational (e.g. "what is the put/call ratio"),
  answer it directly and clearly.
- Always end by noting what specific data WOULD have answered this in the
  database if it existed (helps the user understand what's possible).

Return JSON only:
{
  "answer": "<your 2-3 paragraph answer>",
  "answer_type": "conceptual" | "stock_specific" | "macro_general" | "unanswerable",
  "what_data_would_help": "<short note on what database data would give a definitive answer>"
}
"""


def llm_fallback(client, question, sql_attempted=None, error=None):
    """When SQL fails or returns nothing, fall back to general LLM knowledge."""
    context = (f"USER QUESTION:\n{question}\n\n"
               f"SQL TRIED (failed): {sql_attempted[:300] if sql_attempted else 'none'}\n"
               f"ERROR: {error[:200] if error else 'no data matched'}\n\n"
               f"Now answer from general knowledge.")
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            system=LLM_FALLBACK_PROMPT,
            messages=[{"role": "user", "content": context}],
        )
        raw = "".join(b.text for b in resp.content if b.type == "text").strip()
        if raw.startswith("```"):
            parts = raw.split("```", 2)
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        import json as _json
        parsed = _json.loads(raw)
        return {
            "answer":              parsed.get("answer", ""),
            "answer_type":         parsed.get("answer_type", "unanswerable"),
            "what_data_would_help": parsed.get("what_data_would_help", ""),
            "is_llm_fallback":     True,
        }
    except Exception as e:
        return {
            "answer": f"I couldn't answer this from the database or from general knowledge. ({e})",
            "answer_type": "unanswerable",
            "what_data_would_help": "",
            "is_llm_fallback": True,
        }


def suggest_rephrasings(client, question, failed_sql, error):
    """When SQL generation fails, suggest 3 alternative phrasings."""
    user_msg = (
        f"USER ASKED:\n{question}\n\n"
        f"FAILED SQL:\n{failed_sql}\n\n"
        f"ERROR:\n{error[:500]}\n\n"
        f"Now produce the JSON with diagnosis and 3 rephrasings."
    )
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=600,
            system=REPHRASE_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = "".join(b.text for b in resp.content if b.type == "text").strip()
        # Strip code fences
        if raw.startswith("```"):
            parts = raw.split("```", 2)
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        import json as _json
        parsed = _json.loads(raw)
        return {
            "diagnosis": parsed.get("diagnosis", "The query couldn't be built."),
            "rephrasings": parsed.get("rephrasings", [])[:3],
        }
    except Exception as e:
        return {
            "diagnosis": "The query couldn't be built and a rephrasing couldn't "
                         "be generated either. Try a more specific question.",
            "rephrasings": [],
        }


def answer(question, client, engine):
    """The full loop. Returns dict with: question, sql, df, error (if any)."""
    sql = generate_sql(client, question)
    started = time.time()
    try:
        df = execute_sql(engine, sql)
        # If SQL succeeded but returned no rows, attach an LLM fallback
        # so the user gets a useful response anyway.
        result = {
            "question": question,
            "sql": sql,
            "df": df,
            "error": None,
            "elapsed_ms": int((time.time() - started) * 1000),
        }
        if df is not None and df.empty:
            result["llm_fallback"] = llm_fallback(client, question, sql, "no rows matched")
        return result
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
            # SQL retry also failed.
            # 1. Get rephrasings to offer the user
            suggestions = suggest_rephrasings(client, question, sql, str(e2))
            # 2. Also get a best-effort LLM fallback answer
            fallback = llm_fallback(client, question, sql, str(e2))
            return {
                "question": question,
                "sql": sql,
                "df": None,
                "error": f"Query failed: {e2}",
                "diagnosis": suggestions["diagnosis"],
                "rephrasings": suggestions["rephrasings"],
                "llm_fallback": fallback,
                "elapsed_ms": int((time.time() - started) * 1000),
            }
