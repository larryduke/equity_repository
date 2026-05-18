-- =============================================================================
-- Equity Interrogator — Complete Schema (Phase 1 + Phase 2)
-- Run this entire file in Supabase SQL Editor on a fresh database.
-- If upgrading from Phase 1, run the "ALTER TABLE ADD COLUMN IF NOT EXISTS"
-- sections safely — they are all idempotent.
-- =============================================================================

-- Clean slate (comment these out if upgrading, not fresh install)
DROP TABLE IF EXISTS daily_bars CASCADE;
DROP TABLE IF EXISTS earnings_dates CASCADE;
DROP TABLE IF EXISTS fundamentals CASCADE;
DROP TABLE IF EXISTS tickers CASCADE;
DROP TABLE IF EXISTS macro_events CASCADE;
DROP TABLE IF EXISTS market_indicators CASCADE;
DROP TABLE IF EXISTS sector_indicators CASCADE;
DROP TABLE IF EXISTS sector_relative_strength CASCADE;
DROP TABLE IF EXISTS political_calendar CASCADE;
DROP TABLE IF EXISTS earnings_revision_breadth CASCADE;
DROP TABLE IF EXISTS insider_transactions CASCADE;
DROP TABLE IF EXISTS short_interest CASCADE;
DROP TABLE IF EXISTS analyst_estimates CASCADE;
DROP TABLE IF EXISTS commodity_prices CASCADE;

-- =============================================================================
-- tickers
-- =============================================================================
CREATE TABLE tickers (
    ticker          VARCHAR(15) PRIMARY KEY,
    name            VARCHAR(255),
    exchange        VARCHAR(20),
    sector          VARCHAR(80),
    industry        VARCHAR(120),
    country         VARCHAR(40),
    currency        VARCHAR(10),

    -- US index membership
    in_sp500        BOOLEAN DEFAULT FALSE,
    in_sp400        BOOLEAN DEFAULT FALSE,
    in_sp600        BOOLEAN DEFAULT FALSE,
    in_nasdaq100    BOOLEAN DEFAULT FALSE,
    in_dow30        BOOLEAN DEFAULT FALSE,

    -- European index membership
    in_ftse100      BOOLEAN DEFAULT FALSE,
    in_ftse250      BOOLEAN DEFAULT FALSE,
    in_dax40        BOOLEAN DEFAULT FALSE,
    in_cac40        BOOLEAN DEFAULT FALSE,
    in_aex          BOOLEAN DEFAULT FALSE,
    in_ibex35       BOOLEAN DEFAULT FALSE,
    in_mib40        BOOLEAN DEFAULT FALSE,
    in_smi          BOOLEAN DEFAULT FALSE,
    in_stoxx50      BOOLEAN DEFAULT FALSE,

    market_cap_usd  DOUBLE PRECISION,
    is_active       BOOLEAN DEFAULT TRUE,
    last_updated    TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_tickers_sector    ON tickers(sector);
CREATE INDEX idx_tickers_sp500     ON tickers(in_sp500)    WHERE in_sp500 = TRUE;
CREATE INDEX idx_tickers_market_cap ON tickers(market_cap_usd);
CREATE INDEX idx_tickers_country   ON tickers(country);

-- =============================================================================
-- daily_bars — core price + all pre-calculated indicators
-- =============================================================================
CREATE TABLE daily_bars (
    ticker          VARCHAR(15) NOT NULL,
    date            DATE NOT NULL,
    open            DOUBLE PRECISION,
    high            DOUBLE PRECISION,
    low             DOUBLE PRECISION,
    close           DOUBLE PRECISION,
    volume          BIGINT,

    -- Returns
    daily_return    DOUBLE PRECISION,
    weekly_return   DOUBLE PRECISION,
    monthly_return  DOUBLE PRECISION,

    -- Moving averages
    ma_20           DOUBLE PRECISION,
    ma_50           DOUBLE PRECISION,
    ma_100          DOUBLE PRECISION,
    ma_200          DOUBLE PRECISION,
    pct_vs_ma20     DOUBLE PRECISION,
    pct_vs_ma50     DOUBLE PRECISION,
    pct_vs_ma100    DOUBLE PRECISION,
    pct_vs_ma200    DOUBLE PRECISION,

    -- 52-week range
    high_52w        DOUBLE PRECISION,
    low_52w         DOUBLE PRECISION,
    pct_vs_52w_high DOUBLE PRECISION,
    pct_vs_52w_low  DOUBLE PRECISION,

    -- Volume
    vol_20d_avg     DOUBLE PRECISION,
    rel_volume      DOUBLE PRECISION,

    -- Momentum
    rsi_14          DOUBLE PRECISION,
    rsi_5           DOUBLE PRECISION,
    macd_line       DOUBLE PRECISION,
    macd_signal     DOUBLE PRECISION,
    macd_histogram  DOUBLE PRECISION,

    -- Volatility / bands
    bb_upper        DOUBLE PRECISION,
    bb_lower        DOUBLE PRECISION,
    bb_pct          DOUBLE PRECISION,
    atr_14          DOUBLE PRECISION,
    gap_pct         DOUBLE PRECISION,

    PRIMARY KEY (ticker, date)
);

CREATE INDEX idx_bars_ticker_date ON daily_bars(ticker, date DESC);
CREATE INDEX idx_bars_date        ON daily_bars(date);
CREATE INDEX idx_bars_rsi         ON daily_bars(rsi_14) WHERE rsi_14 IS NOT NULL;

-- =============================================================================
-- fundamentals — weekly snapshot per ticker
-- =============================================================================
CREATE TABLE fundamentals (
    ticker              VARCHAR(15) NOT NULL,
    date                DATE NOT NULL,
    market_cap          DOUBLE PRECISION,
    enterprise_value    DOUBLE PRECISION,
    pe_trailing         DOUBLE PRECISION,
    pe_forward          DOUBLE PRECISION,
    price_to_sales      DOUBLE PRECISION,
    price_to_book       DOUBLE PRECISION,
    ev_ebitda           DOUBLE PRECISION,
    peg_ratio           DOUBLE PRECISION,
    revenue_growth_yoy  DOUBLE PRECISION,
    earnings_growth_yoy DOUBLE PRECISION,
    profit_margin       DOUBLE PRECISION,
    operating_margin    DOUBLE PRECISION,
    return_on_equity    DOUBLE PRECISION,
    debt_to_equity      DOUBLE PRECISION,
    current_ratio       DOUBLE PRECISION,
    dividend_yield      DOUBLE PRECISION,
    payout_ratio        DOUBLE PRECISION,
    shares_outstanding  DOUBLE PRECISION,
    float_shares        DOUBLE PRECISION,
    short_interest_pct  DOUBLE PRECISION,
    short_ratio_days    DOUBLE PRECISION,

    -- Analyst consensus
    analyst_target_price    DOUBLE PRECISION,
    analyst_target_upside   DOUBLE PRECISION,  -- % upside to consensus target
    analyst_buy_count       INTEGER,
    analyst_hold_count      INTEGER,
    analyst_sell_count      INTEGER,
    analyst_consensus       VARCHAR(10),        -- 'Buy','Hold','Sell','Strong Buy'

    -- Percentile vs own 5-year history
    pe_percentile_5yr       DOUBLE PRECISION,
    yield_percentile_5yr    DOUBLE PRECISION,
    ps_percentile_5yr       DOUBLE PRECISION,

    PRIMARY KEY (ticker, date)
);

CREATE INDEX idx_fund_ticker ON fundamentals(ticker, date DESC);

-- =============================================================================
-- earnings_dates
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
    fiscal_period       VARCHAR(10),
    reporting_time      VARCHAR(5),
    PRIMARY KEY (ticker, date)
);

CREATE INDEX idx_earnings_ticker_date ON earnings_dates(ticker, date DESC);

-- =============================================================================
-- market_indicators — daily market-wide + macro series
-- =============================================================================
CREATE TABLE market_indicators (
    date                DATE PRIMARY KEY,

    -- Volatility
    vix_close           DOUBLE PRECISION,
    vix_ma20            DOUBLE PRECISION,
    vix_ma50            DOUBLE PRECISION,

    -- Breadth
    pct_above_ma200     DOUBLE PRECISION,
    pct_above_ma50      DOUBLE PRECISION,
    new_highs_52w       INTEGER,
    new_lows_52w        INTEGER,
    advance_decline_pct DOUBLE PRECISION,

    -- Sentiment
    fear_greed_index    DOUBLE PRECISION,
    put_call_ratio      DOUBLE PRECISION,

    -- Fed / rates (FRED)
    fed_funds_rate      DOUBLE PRECISION,
    ten_year_yield      DOUBLE PRECISION,
    two_year_yield      DOUBLE PRECISION,
    yield_curve_10y_2y  DOUBLE PRECISION,
    breakeven_10y       DOUBLE PRECISION,  -- 10Y inflation breakeven

    -- Inflation
    cpi_yoy             DOUBLE PRECISION,
    ppi_yoy             DOUBLE PRECISION,  -- Producer Price Index YoY%

    -- Activity
    unemployment_rate   DOUBLE PRECISION,
    initial_claims      DOUBLE PRECISION,  -- weekly jobless claims (thousands)
    ism_manufacturing   DOUBLE PRECISION,  -- PMI, 50 = neutral
    ism_services        DOUBLE PRECISION,

    -- Credit
    credit_spread_hy    DOUBLE PRECISION,  -- High yield OAS in bps (wider = stress)
    credit_spread_ig    DOUBLE PRECISION,  -- Investment grade OAS

    -- Currency / commodities
    dxy                 DOUBLE PRECISION,  -- Dollar index
    gold_price          DOUBLE PRECISION,  -- Gold $/oz
    copper_price        DOUBLE PRECISION,  -- Copper $/lb
    oil_price_wti       DOUBLE PRECISION,  -- WTI crude $/barrel
    copper_gold_ratio   DOUBLE PRECISION,  -- Risk-on/off gauge

    -- Money supply
    m2_yoy              DOUBLE PRECISION   -- M2 money supply YoY%
);

CREATE INDEX idx_market_indicators_date ON market_indicators(date DESC);

-- =============================================================================
-- macro_events — discrete point-in-time events
-- =============================================================================
CREATE TABLE macro_events (
    id          SERIAL PRIMARY KEY,
    event_date  DATE NOT NULL,
    event_type  VARCHAR(40) NOT NULL,
    event_name  VARCHAR(255),
    country     VARCHAR(40) DEFAULT 'US',
    value       DOUBLE PRECISION,
    prior_value DOUBLE PRECISION,
    estimate    DOUBLE PRECISION,
    surprise    DOUBLE PRECISION,
    metadata    JSONB
);

CREATE INDEX idx_macro_events_date ON macro_events(event_date);
CREATE INDEX idx_macro_events_type ON macro_events(event_type);

-- =============================================================================
-- political_calendar — party control of government
-- =============================================================================
CREATE TABLE political_calendar (
    id              SERIAL PRIMARY KEY,
    start_date      DATE NOT NULL,
    end_date        DATE,
    country         VARCHAR(10) DEFAULT 'US',
    body            VARCHAR(30) NOT NULL,
    controlling_party VARCHAR(30),
    majority_seats  INTEGER,
    minority_seats  INTEGER,
    is_majority     BOOLEAN DEFAULT TRUE,
    is_divided_govt BOOLEAN DEFAULT FALSE,
    notes           VARCHAR(255)
);

CREATE INDEX idx_polcal_date  ON political_calendar(start_date, end_date);
CREATE INDEX idx_polcal_body  ON political_calendar(body, country);
CREATE INDEX idx_polcal_party ON political_calendar(controlling_party);

-- =============================================================================
-- sector_indicators — daily sector-level aggregates
-- =============================================================================
CREATE TABLE sector_indicators (
    date                DATE NOT NULL,
    sector              VARCHAR(80) NOT NULL,
    n_stocks            INTEGER,
    avg_rsi_14          DOUBLE PRECISION,
    avg_macd_histogram  DOUBLE PRECISION,
    pct_rsi_oversold    DOUBLE PRECISION,
    pct_rsi_overbought  DOUBLE PRECISION,
    pct_above_ma50      DOUBLE PRECISION,
    pct_above_ma200     DOUBLE PRECISION,
    avg_pct_vs_ma50     DOUBLE PRECISION,
    avg_pct_vs_ma200    DOUBLE PRECISION,
    avg_return_5d       DOUBLE PRECISION,
    avg_return_20d      DOUBLE PRECISION,
    avg_rel_volume      DOUBLE PRECISION,
    PRIMARY KEY (date, sector)
);

CREATE INDEX idx_sector_ind_date   ON sector_indicators(date DESC);
CREATE INDEX idx_sector_ind_sector ON sector_indicators(sector, date DESC);

-- =============================================================================
-- sector_relative_strength — pairwise rotation scores
-- =============================================================================
CREATE TABLE sector_relative_strength (
    date            DATE NOT NULL,
    sector_a        VARCHAR(80) NOT NULL,
    sector_b        VARCHAR(80) NOT NULL,
    rs_ratio_5d     DOUBLE PRECISION,
    rs_ratio_20d    DOUBLE PRECISION,
    rs_ratio_60d    DOUBLE PRECISION,
    rs_trend_20d    DOUBLE PRECISION,
    rotation_score  DOUBLE PRECISION,
    signal          VARCHAR(20),
    score_momentum  DOUBLE PRECISION,
    score_breadth   DOUBLE PRECISION,
    score_rsi       DOUBLE PRECISION,
    score_volume    DOUBLE PRECISION,
    score_macro     DOUBLE PRECISION,
    PRIMARY KEY (date, sector_a, sector_b)
);

CREATE INDEX idx_srs_date ON sector_relative_strength(date DESC);
CREATE INDEX idx_srs_pair ON sector_relative_strength(sector_a, sector_b, date DESC);

-- =============================================================================
-- earnings_revision_breadth — weekly per sector
-- =============================================================================
CREATE TABLE earnings_revision_breadth (
    date            DATE NOT NULL,
    sector          VARCHAR(80) NOT NULL,
    n_stocks        INTEGER,
    pct_raised_30d  DOUBLE PRECISION,
    pct_cut_30d     DOUBLE PRECISION,
    net_revision    DOUBLE PRECISION,
    avg_revision_pct DOUBLE PRECISION,
    PRIMARY KEY (date, sector)
);

CREATE INDEX idx_erb_date   ON earnings_revision_breadth(date DESC);
CREATE INDEX idx_erb_sector ON earnings_revision_breadth(sector, date DESC);

-- =============================================================================
-- insider_transactions — SEC EDGAR Form 4
-- =============================================================================
CREATE TABLE insider_transactions (
    id              SERIAL PRIMARY KEY,
    ticker          VARCHAR(15) NOT NULL,
    date            DATE NOT NULL,
    insider_name    VARCHAR(255),
    role            VARCHAR(50),   -- 'CEO', 'CFO', 'Director', 'VP', etc.
    transaction_type VARCHAR(10),  -- 'Buy', 'Sell', 'Grant'
    shares          DOUBLE PRECISION,
    price_per_share DOUBLE PRECISION,
    value_usd       DOUBLE PRECISION,
    shares_owned_after DOUBLE PRECISION,
    filing_date     DATE,
    UNIQUE (ticker, date, insider_name, transaction_type, shares)
);

CREATE INDEX idx_insider_ticker_date ON insider_transactions(ticker, date DESC);
CREATE INDEX idx_insider_date        ON insider_transactions(date DESC);
CREATE INDEX idx_insider_type        ON insider_transactions(transaction_type, date DESC);

-- =============================================================================
-- short_interest — FINRA biweekly + FMP weekly
-- =============================================================================
CREATE TABLE short_interest (
    ticker          VARCHAR(15) NOT NULL,
    date            DATE NOT NULL,
    short_interest  DOUBLE PRECISION,  -- shares short
    short_pct_float DOUBLE PRECISION,  -- % of float
    days_to_cover   DOUBLE PRECISION,  -- short ratio
    change_pct      DOUBLE PRECISION,  -- WoW change in short interest %
    PRIMARY KEY (ticker, date)
);

CREATE INDEX idx_short_ticker_date ON short_interest(ticker, date DESC);
CREATE INDEX idx_short_pct         ON short_interest(short_pct_float DESC, date DESC);

-- =============================================================================
-- analyst_estimates — price targets and consensus
-- =============================================================================
CREATE TABLE analyst_estimates (
    ticker          VARCHAR(15) NOT NULL,
    date            DATE NOT NULL,
    analyst_firm    VARCHAR(100),
    analyst_name    VARCHAR(100),
    action          VARCHAR(20),   -- 'Upgrade','Downgrade','Initiate','Reiterate'
    rating_new      VARCHAR(20),   -- 'Buy','Hold','Sell','Strong Buy','Outperform'
    rating_prior    VARCHAR(20),
    target_new      DOUBLE PRECISION,
    target_prior    DOUBLE PRECISION,
    PRIMARY KEY (ticker, date, analyst_firm)
);

CREATE INDEX idx_analyst_ticker_date ON analyst_estimates(ticker, date DESC);
CREATE INDEX idx_analyst_date        ON analyst_estimates(date DESC);

-- =============================================================================
-- commodity_prices — daily commodity closes
-- =============================================================================
CREATE TABLE commodity_prices (
    date        DATE NOT NULL,
    commodity   VARCHAR(20) NOT NULL,  -- 'gold','silver','copper','oil_wti','oil_brent',
                                        -- 'natural_gas','wheat','corn','soybeans'
    price       DOUBLE PRECISION,
    currency    VARCHAR(5) DEFAULT 'USD',
    unit        VARCHAR(20),           -- 'per_oz','per_lb','per_barrel','per_bushel'
    PRIMARY KEY (date, commodity)
);

CREATE INDEX idx_commodity_date      ON commodity_prices(date DESC);
CREATE INDEX idx_commodity_type_date ON commodity_prices(commodity, date DESC);

-- =============================================================================
-- Read-only query role
-- =============================================================================
DROP ROLE IF EXISTS query_reader;
CREATE ROLE query_reader WITH LOGIN PASSWORD 'CHANGE_ME_STRONG_PASSWORD';
GRANT CONNECT ON DATABASE postgres TO query_reader;
GRANT USAGE ON SCHEMA public TO query_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO query_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO query_reader;
ALTER ROLE query_reader SET statement_timeout = '15s';

-- =============================================================================
-- SEED DATA
-- =============================================================================

-- Macro events: elections
INSERT INTO macro_events (event_date, event_type, event_name, country, metadata) VALUES
('1998-11-03','us_election','US Midterm 1998','US','{"type":"midterm","year":1998}'::jsonb),
('2000-11-07','us_election','US Presidential 2000','US','{"type":"presidential","year":2000,"winner":"Bush","party":"Republican"}'::jsonb),
('2002-11-05','us_election','US Midterm 2002','US','{"type":"midterm","year":2002}'::jsonb),
('2004-11-02','us_election','US Presidential 2004','US','{"type":"presidential","year":2004,"winner":"Bush","party":"Republican"}'::jsonb),
('2006-11-07','us_election','US Midterm 2006','US','{"type":"midterm","year":2006}'::jsonb),
('2008-11-04','us_election','US Presidential 2008','US','{"type":"presidential","year":2008,"winner":"Obama","party":"Democratic"}'::jsonb),
('2010-11-02','us_election','US Midterm 2010','US','{"type":"midterm","year":2010}'::jsonb),
('2012-11-06','us_election','US Presidential 2012','US','{"type":"presidential","year":2012,"winner":"Obama","party":"Democratic"}'::jsonb),
('2014-11-04','us_election','US Midterm 2014','US','{"type":"midterm","year":2014}'::jsonb),
('2016-11-08','us_election','US Presidential 2016','US','{"type":"presidential","year":2016,"winner":"Trump","party":"Republican"}'::jsonb),
('2018-11-06','us_election','US Midterm 2018','US','{"type":"midterm","year":2018}'::jsonb),
('2020-11-03','us_election','US Presidential 2020','US','{"type":"presidential","year":2020,"winner":"Biden","party":"Democratic"}'::jsonb),
('2022-11-08','us_election','US Midterm 2022','US','{"type":"midterm","year":2022}'::jsonb),
('2024-11-05','us_election','US Presidential 2024','US','{"type":"presidential","year":2024,"winner":"Trump","party":"Republican"}'::jsonb)
ON CONFLICT DO NOTHING;

-- Macro events: recessions
INSERT INTO macro_events (event_date, event_type, event_name, country, metadata) VALUES
('2001-03-01','recession_start','Dot-com recession','US','{"nber":true,"severity":"moderate"}'::jsonb),
('2001-11-01','recession_end','Dot-com recession ends','US','{"nber":true}'::jsonb),
('2007-12-01','recession_start','Global Financial Crisis','US','{"nber":true,"severity":"severe"}'::jsonb),
('2009-06-01','recession_end','GFC ends','US','{"nber":true}'::jsonb),
('2020-02-01','recession_start','COVID recession','US','{"nber":true,"severity":"severe"}'::jsonb),
('2020-04-01','recession_end','COVID recession ends','US','{"nber":true}'::jsonb)
ON CONFLICT DO NOTHING;

-- Macro events: Fed cycles
INSERT INTO macro_events (event_date, event_type, event_name, country, metadata) VALUES
('1999-06-30','fed_cycle_start','Fed tightening 1999','US','{"direction":"tightening","starting_rate":5.0}'::jsonb),
('2000-05-16','fed_cycle_end','Fed peaks 2000','US','{"direction":"tightening","peak_rate":6.5}'::jsonb),
('2001-01-03','fed_cycle_start','Fed easing 2001','US','{"direction":"easing","starting_rate":6.5}'::jsonb),
('2004-06-30','fed_cycle_start','Fed tightening 2004','US','{"direction":"tightening","starting_rate":1.0}'::jsonb),
('2006-06-29','fed_cycle_end','Fed peaks 2006','US','{"direction":"tightening","peak_rate":5.25}'::jsonb),
('2007-09-18','fed_cycle_start','Fed easing 2007','US','{"direction":"easing","starting_rate":5.25}'::jsonb),
('2015-12-16','fed_cycle_start','Fed tightening 2015','US','{"direction":"tightening","starting_rate":0.25}'::jsonb),
('2018-12-19','fed_cycle_end','Fed peaks 2018','US','{"direction":"tightening","peak_rate":2.5}'::jsonb),
('2019-07-31','fed_cycle_start','Fed easing 2019','US','{"direction":"easing","starting_rate":2.5}'::jsonb),
('2022-03-16','fed_cycle_start','Fed tightening 2022','US','{"direction":"tightening","starting_rate":0.25}'::jsonb),
('2023-07-26','fed_cycle_end','Fed peaks 2023','US','{"direction":"tightening","peak_rate":5.5}'::jsonb),
('2024-09-18','fed_cycle_start','Fed easing 2024','US','{"direction":"easing","first_cut_bps":50}'::jsonb)
ON CONFLICT DO NOTHING;

-- Macro events: crises
INSERT INTO macro_events (event_date, event_type, event_name, country, metadata) VALUES
('2000-03-10','crisis_start','Dot-com peak','US','{"severity":"severe","drawdown_pct":-49}'::jsonb),
('2002-10-09','crisis_low','Dot-com bottom','US','{"severity":"severe"}'::jsonb),
('2007-10-09','crisis_start','GFC peak','US','{"severity":"severe","drawdown_pct":-57}'::jsonb),
('2009-03-09','crisis_low','GFC bottom','US','{"severity":"severe"}'::jsonb),
('2011-08-08','crisis_start','Debt ceiling crisis','US','{"severity":"moderate","drawdown_pct":-22}'::jsonb),
('2011-10-03','crisis_low','2011 bottom','US','{"severity":"moderate"}'::jsonb),
('2018-12-24','crisis_low','Q4 2018 bottom','US','{"severity":"mild","drawdown_pct":-20}'::jsonb),
('2020-02-19','crisis_start','COVID crash','US','{"severity":"severe","drawdown_pct":-34}'::jsonb),
('2020-03-23','crisis_low','COVID bottom','US','{"severity":"severe"}'::jsonb),
('2022-01-03','crisis_start','2022 bear market','US','{"severity":"moderate","drawdown_pct":-25}'::jsonb),
('2022-10-12','crisis_low','2022 bottom','US','{"severity":"moderate"}'::jsonb)
ON CONFLICT DO NOTHING;

-- Political calendar: PRESIDENCY
INSERT INTO political_calendar (start_date, end_date, country, body, controlling_party, notes) VALUES
('1993-01-20','2001-01-19','US','presidency','Democratic','Clinton'),
('2001-01-20','2009-01-19','US','presidency','Republican','Bush'),
('2009-01-20','2017-01-19','US','presidency','Democratic','Obama'),
('2017-01-20','2021-01-19','US','presidency','Republican','Trump'),
('2021-01-20','2025-01-19','US','presidency','Democratic','Biden'),
('2025-01-20',NULL,        'US','presidency','Republican','Trump 2')
ON CONFLICT DO NOTHING;

-- Political calendar: HOUSE
INSERT INTO political_calendar (start_date, end_date, country, body, controlling_party, majority_seats, minority_seats, notes) VALUES
('1993-01-03','1995-01-03','US','house','Democratic',258,176,'103rd'),
('1995-01-03','2007-01-03','US','house','Republican',230,204,'104th-109th (Rep wave)'),
('2007-01-03','2011-01-03','US','house','Democratic',233,202,'110th-111th (Dem wave)'),
('2011-01-03','2019-01-03','US','house','Republican',242,193,'112th-115th (Tea Party)'),
('2019-01-03','2023-01-03','US','house','Democratic',235,199,'116th-117th'),
('2023-01-03','2025-01-03','US','house','Republican',222,213,'118th (thin majority)'),
('2025-01-03',NULL,        'US','house','Republican',219,215,'119th')
ON CONFLICT DO NOTHING;

-- Political calendar: SENATE
INSERT INTO political_calendar (start_date, end_date, country, body, controlling_party, majority_seats, minority_seats, notes) VALUES
('1993-01-03','1995-01-03','US','senate','Democratic',57,43,'103rd'),
('1995-01-03','2001-06-06','US','senate','Republican',54,46,'104th-106th + early 107th'),
('2001-06-06','2003-01-03','US','senate','Democratic',50,49,'107th post-Jeffords'),
('2003-01-03','2007-01-03','US','senate','Republican',51,48,'108th-109th'),
('2007-01-03','2015-01-03','US','senate','Democratic',49,49,'110th-113th'),
('2015-01-03','2021-01-03','US','senate','Republican',54,46,'114th-116th'),
('2021-01-03','2023-01-03','US','senate','Democratic',50,50,'117th - VP tiebreak'),
('2023-01-03','2025-01-03','US','senate','Democratic',48,51,'118th'),
('2025-01-03',NULL,        'US','senate','Republican',53,47,'119th')
ON CONFLICT DO NOTHING;

-- Set divided government flag
UPDATE political_calendar pc
SET is_divided_govt = TRUE
WHERE body IN ('house','senate')
AND EXISTS (
    SELECT 1 FROM political_calendar pres
    WHERE pres.body = 'presidency'
      AND pres.controlling_party != pc.controlling_party
      AND pres.start_date <= COALESCE(pc.end_date, CURRENT_DATE)
      AND COALESCE(pres.end_date, CURRENT_DATE) >= pc.start_date
);

-- Verify everything landed
SELECT 'macro_events' AS tbl, COUNT(*) n FROM macro_events
UNION ALL SELECT 'political_calendar', COUNT(*) FROM political_calendar
UNION ALL SELECT 'tickers', COUNT(*) FROM tickers
UNION ALL SELECT 'daily_bars', COUNT(*) FROM daily_bars;
