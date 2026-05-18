-- =============================================================================
-- Equity Interrogator — Phase 1 schema
-- Target: Supabase Postgres (free tier handles this volume comfortably)
-- Run order: drop old tables, then this whole file in the Supabase SQL editor.
-- =============================================================================

-- Clean slate. Drop anything from the prototype.
DROP TABLE IF EXISTS daily_bars CASCADE;
DROP TABLE IF EXISTS earnings_dates CASCADE;
DROP TABLE IF EXISTS fundamentals CASCADE;
DROP TABLE IF EXISTS tickers CASCADE;
DROP TABLE IF EXISTS macro_events CASCADE;
DROP TABLE IF EXISTS market_indicators CASCADE;

-- =============================================================================
-- tickers: reference table. Static-ish, refreshed quarterly.
-- =============================================================================
CREATE TABLE tickers (
    ticker          VARCHAR(15) PRIMARY KEY,
    name            VARCHAR(255),
    exchange        VARCHAR(20),     -- NYSE, NASDAQ, AMEX
    sector          VARCHAR(80),
    industry        VARCHAR(120),
    country         VARCHAR(40),
    currency        VARCHAR(10),

    -- Index membership flags
    in_sp500        BOOLEAN DEFAULT FALSE,
    in_sp400        BOOLEAN DEFAULT FALSE,   -- mid-cap
    in_sp600        BOOLEAN DEFAULT FALSE,   -- small-cap
    in_nasdaq100    BOOLEAN DEFAULT FALSE,
    in_dow30        BOOLEAN DEFAULT FALSE,

    market_cap_usd  DOUBLE PRECISION,
    is_active       BOOLEAN DEFAULT TRUE,
    last_updated    TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_tickers_sector ON tickers(sector);
CREATE INDEX idx_tickers_sp500 ON tickers(in_sp500) WHERE in_sp500 = TRUE;
CREATE INDEX idx_tickers_market_cap ON tickers(market_cap_usd);

-- =============================================================================
-- daily_bars: the core table. Prices + all calculated technical indicators.
-- Indicators are computed during refresh_data.py, NOT at query time, so Claude
-- can write simple SQL like `WHERE close < ma_200`.
-- =============================================================================
CREATE TABLE daily_bars (
    ticker          VARCHAR(15) NOT NULL,
    date            DATE NOT NULL,

    -- Raw OHLCV (split/dividend adjusted from FMP)
    open            DOUBLE PRECISION,
    high            DOUBLE PRECISION,
    low             DOUBLE PRECISION,
    close           DOUBLE PRECISION,
    volume          BIGINT,

    -- Returns
    daily_return    DOUBLE PRECISION,    -- (close / prev_close - 1) * 100
    weekly_return   DOUBLE PRECISION,    -- vs close 5 trading days ago
    monthly_return  DOUBLE PRECISION,    -- vs close 21 trading days ago

    -- Moving averages
    ma_20           DOUBLE PRECISION,
    ma_50           DOUBLE PRECISION,
    ma_100          DOUBLE PRECISION,
    ma_200          DOUBLE PRECISION,

    -- Distance from MAs (% — negative means below)
    pct_vs_ma20     DOUBLE PRECISION,
    pct_vs_ma50     DOUBLE PRECISION,
    pct_vs_ma100    DOUBLE PRECISION,
    pct_vs_ma200    DOUBLE PRECISION,

    -- 52-week range
    high_52w        DOUBLE PRECISION,
    low_52w         DOUBLE PRECISION,
    pct_vs_52w_high DOUBLE PRECISION,    -- how far below 52w high (negative or zero)
    pct_vs_52w_low  DOUBLE PRECISION,    -- how far above 52w low (positive or zero)

    -- Volume context
    vol_20d_avg     DOUBLE PRECISION,
    rel_volume      DOUBLE PRECISION,    -- volume / vol_20d_avg

    -- Momentum
    rsi_14          DOUBLE PRECISION,    -- classic 14-day RSI
    rsi_5           DOUBLE PRECISION,    -- short-term RSI

    -- MACD (12/26/9)
    macd_line       DOUBLE PRECISION,
    macd_signal     DOUBLE PRECISION,
    macd_histogram  DOUBLE PRECISION,

    -- Bollinger Bands (20, 2 stddev)
    bb_upper        DOUBLE PRECISION,
    bb_lower        DOUBLE PRECISION,
    bb_pct          DOUBLE PRECISION,    -- where price sits in band, 0=at lower, 1=at upper

    -- Volatility
    atr_14          DOUBLE PRECISION,    -- Average True Range

    -- Structure
    gap_pct         DOUBLE PRECISION,    -- (today_open / prev_close - 1) * 100

    PRIMARY KEY (ticker, date)
);

CREATE INDEX idx_bars_ticker_date ON daily_bars(ticker, date DESC);
CREATE INDEX idx_bars_date ON daily_bars(date);
CREATE INDEX idx_bars_rsi ON daily_bars(rsi_14) WHERE rsi_14 IS NOT NULL;

-- =============================================================================
-- fundamentals: refreshed weekly (slower-changing data).
-- One row per ticker per snapshot date.
-- =============================================================================
CREATE TABLE fundamentals (
    ticker              VARCHAR(15) NOT NULL,
    date                DATE NOT NULL,        -- as-of date

    market_cap          DOUBLE PRECISION,
    enterprise_value    DOUBLE PRECISION,

    -- Valuation
    pe_trailing         DOUBLE PRECISION,
    pe_forward          DOUBLE PRECISION,
    price_to_sales      DOUBLE PRECISION,
    price_to_book       DOUBLE PRECISION,
    ev_ebitda           DOUBLE PRECISION,
    peg_ratio           DOUBLE PRECISION,

    -- Growth
    revenue_growth_yoy  DOUBLE PRECISION,
    earnings_growth_yoy DOUBLE PRECISION,

    -- Profitability
    profit_margin       DOUBLE PRECISION,
    operating_margin    DOUBLE PRECISION,
    return_on_equity    DOUBLE PRECISION,

    -- Balance sheet
    debt_to_equity      DOUBLE PRECISION,
    current_ratio       DOUBLE PRECISION,

    -- Yield
    dividend_yield      DOUBLE PRECISION,
    payout_ratio        DOUBLE PRECISION,

    -- Shares
    shares_outstanding  DOUBLE PRECISION,
    float_shares        DOUBLE PRECISION,
    short_interest_pct  DOUBLE PRECISION,
    short_ratio_days    DOUBLE PRECISION,

    -- Calculated: percentile of current value vs this ticker's own 5-year history.
    -- Populated during refresh. NULL until enough history exists.
    pe_percentile_5yr           DOUBLE PRECISION,
    yield_percentile_5yr        DOUBLE PRECISION,
    price_to_sales_pct_5yr      DOUBLE PRECISION,

    PRIMARY KEY (ticker, date)
);

CREATE INDEX idx_fundamentals_ticker ON fundamentals(ticker, date DESC);
CREATE INDEX idx_fundamentals_pe ON fundamentals(pe_trailing) WHERE pe_trailing IS NOT NULL;

-- =============================================================================
-- earnings_dates: historical earnings events
-- =============================================================================
CREATE TABLE earnings_dates (
    ticker              VARCHAR(15) NOT NULL,
    date                DATE NOT NULL,

    eps_estimate        DOUBLE PRECISION,
    eps_actual          DOUBLE PRECISION,
    eps_surprise_pct    DOUBLE PRECISION,

    revenue_estimate    DOUBLE PRECISION,
    revenue_actual      DOUBLE PRECISION,
    revenue_surprise_pct DOUBLE PRECISION,

    fiscal_period       VARCHAR(10),         -- 'Q1', 'Q2', 'Q3', 'Q4', 'FY'
    reporting_time      VARCHAR(5),          -- 'bmo' or 'amc' if known

    PRIMARY KEY (ticker, date)
);

CREATE INDEX idx_earnings_ticker_date ON earnings_dates(ticker, date DESC);

-- =============================================================================
-- market_indicators: market-wide series. One row per date.
-- VIX, breadth, fear/greed, etc.
-- =============================================================================
CREATE TABLE market_indicators (
    date                DATE PRIMARY KEY,

    -- Volatility
    vix_close           DOUBLE PRECISION,
    vix_ma20            DOUBLE PRECISION,
    vix_ma50            DOUBLE PRECISION,

    -- Breadth (calculated from daily_bars during refresh)
    pct_above_ma200     DOUBLE PRECISION,    -- % of S&P 500 above 200MA
    pct_above_ma50      DOUBLE PRECISION,    -- % of S&P 500 above 50MA
    new_highs_52w       INTEGER,             -- count in S&P 500
    new_lows_52w        INTEGER,
    advance_decline_pct DOUBLE PRECISION,    -- % advancing in S&P 500

    -- Sentiment (optional, populated when source available)
    fear_greed_index    DOUBLE PRECISION,    -- 0-100, CNN scrape

    -- Macro from FRED
    fed_funds_rate      DOUBLE PRECISION,
    ten_year_yield      DOUBLE PRECISION,
    two_year_yield      DOUBLE PRECISION,
    yield_curve_10y_2y  DOUBLE PRECISION,    -- 10Y minus 2Y; negative = inverted
    cpi_yoy             DOUBLE PRECISION,    -- year-over-year CPI %
    unemployment_rate   DOUBLE PRECISION
);

CREATE INDEX idx_market_indicators_date ON market_indicators(date DESC);

-- =============================================================================
-- macro_events: discrete events with optional point-in-time data.
-- Mostly seeded once; new entries added when FRED publishes new readings.
-- =============================================================================
CREATE TABLE macro_events (
    id              SERIAL PRIMARY KEY,
    event_date      DATE NOT NULL,
    event_type      VARCHAR(40) NOT NULL,   -- 'us_election', 'fed_meeting',
                                            --  'cpi_release', 'recession_start',
                                            --  'recession_end', 'crisis'
    event_name      VARCHAR(255),
    country         VARCHAR(40) DEFAULT 'US',
    value           DOUBLE PRECISION,        -- the reading, if applicable
    prior_value     DOUBLE PRECISION,
    estimate        DOUBLE PRECISION,
    surprise        DOUBLE PRECISION,
    metadata        JSONB                    -- flexible: winner, party, severity
);

CREATE INDEX idx_macro_events_date ON macro_events(event_date);
CREATE INDEX idx_macro_events_type ON macro_events(event_type);

-- =============================================================================
-- Seed data: US election dates and known regime markers.
-- These are static historical facts. Update annually.
-- =============================================================================

-- US presidential elections (Tuesday after first Monday of November)
INSERT INTO macro_events (event_date, event_type, event_name, metadata) VALUES
    ('2020-11-03', 'us_election', 'US Presidential Election 2020',
        '{"winner": "Biden", "party": "Democratic", "incumbent_lost": true}'::jsonb),
    ('2022-11-08', 'us_election', 'US Midterm Election 2022',
        '{"type": "midterm"}'::jsonb),
    ('2024-11-05', 'us_election', 'US Presidential Election 2024',
        '{"winner": "Trump", "party": "Republican", "incumbent_lost": true}'::jsonb);

-- Known Fed regime markers (start/end dates of hiking/cutting cycles)
INSERT INTO macro_events (event_date, event_type, event_name, metadata) VALUES
    ('2022-03-16', 'fed_cycle_start', 'Fed tightening cycle begins',
        '{"direction": "tightening", "starting_rate": 0.25}'::jsonb),
    ('2023-07-26', 'fed_cycle_end', 'Fed tightening cycle peaks',
        '{"direction": "tightening", "peak_rate": 5.50}'::jsonb),
    ('2024-09-18', 'fed_cycle_start', 'Fed easing cycle begins',
        '{"direction": "easing", "first_cut_bps": 50}'::jsonb);

-- Known crisis / regime markers (5-year window)
INSERT INTO macro_events (event_date, event_type, event_name, metadata) VALUES
    ('2020-02-19', 'crisis_start', 'COVID-19 market crash begins',
        '{"severity": "severe", "drawdown_pct": -34}'::jsonb),
    ('2020-03-23', 'crisis_low', 'COVID-19 market bottom',
        '{"severity": "severe"}'::jsonb),
    ('2022-01-03', 'crisis_start', '2022 bear market begins',
        '{"severity": "moderate", "drawdown_pct": -25}'::jsonb),
    ('2022-10-12', 'crisis_low', '2022 bear market bottom',
        '{"severity": "moderate"}'::jsonb);

-- =============================================================================
-- Read-only role for query engine.
-- The text-to-SQL layer connects as this role so Claude can only SELECT.
-- =============================================================================
DROP ROLE IF EXISTS query_reader;
CREATE ROLE query_reader WITH LOGIN PASSWORD 'CHANGE_ME_IN_SUPABASE';
GRANT CONNECT ON DATABASE postgres TO query_reader;
GRANT USAGE ON SCHEMA public TO query_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO query_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO query_reader;

-- Enforce statement timeout for this role so a runaway query can't lock things up.
ALTER ROLE query_reader SET statement_timeout = '15s';
