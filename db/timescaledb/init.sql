-- TimescaleDB initialization script
-- Runs once on first container start via docker-entrypoint-initdb.d

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ─── OHLCV Bars ──────────────────────────────────────────────────────────────
-- Stores aggregated bars at any frequency (1m, 5m, 1h, 1d, etc.)
-- Mirrors ib_insync BarData fields exactly.
CREATE TABLE IF NOT EXISTS market_bars (
    time            TIMESTAMPTZ     NOT NULL,
    symbol          VARCHAR(20)     NOT NULL,
    frequency       VARCHAR(5)      NOT NULL,   -- '1m', '5m', '1h', '1d'
    open            DOUBLE PRECISION,
    high            DOUBLE PRECISION,
    low             DOUBLE PRECISION,
    close           DOUBLE PRECISION,
    volume          BIGINT,
    average         DOUBLE PRECISION,           -- VWAP for the bar (ib_insync: average)
    bar_count       INTEGER,                    -- number of trades in bar (ib_insync: barCount)
    source          VARCHAR(20) DEFAULT 'IBKR'  -- 'IBKR', 'CSV', 'POLYGON', etc.
);

SELECT create_hypertable(
    'market_bars',
    'time',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '1 day'
);

-- Unique constraint: one bar per (symbol, frequency, time)
CREATE UNIQUE INDEX IF NOT EXISTS idx_market_bars_uniq
    ON market_bars (symbol, frequency, time DESC);

-- Fast lookups by symbol + time range
CREATE INDEX IF NOT EXISTS idx_market_bars_symbol_time
    ON market_bars (symbol, time DESC);

-- Compression: bars older than 7 days are compressed (saves ~90% space)
ALTER TABLE market_bars SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, frequency'
);
SELECT add_compression_policy('market_bars', INTERVAL '7 days');


-- ─── Live Ticks ──────────────────────────────────────────────────────────────
-- Raw tick-by-tick data from ib_insync reqTickByTickData / Ticker events.
CREATE TABLE IF NOT EXISTS market_ticks (
    time            TIMESTAMPTZ     NOT NULL,
    symbol          VARCHAR(20)     NOT NULL,
    bid             DOUBLE PRECISION,
    ask             DOUBLE PRECISION,
    last            DOUBLE PRECISION,
    bid_size        DOUBLE PRECISION,
    ask_size        DOUBLE PRECISION,
    last_size       DOUBLE PRECISION,
    volume          BIGINT,
    open            DOUBLE PRECISION,
    high            DOUBLE PRECISION,
    low             DOUBLE PRECISION,
    close           DOUBLE PRECISION,           -- previous close
    halted          BOOLEAN DEFAULT FALSE
);

SELECT create_hypertable(
    'market_ticks',
    'time',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '1 hour'
);

CREATE INDEX IF NOT EXISTS idx_market_ticks_symbol_time
    ON market_ticks (symbol, time DESC);

-- Ticks are short-lived in time-series; compress after 1 hour
ALTER TABLE market_ticks SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol'
);
SELECT add_compression_policy('market_ticks', INTERVAL '1 hour');


-- ─── Continuous Aggregates (materialized views) ───────────────────────────────
-- Automatically roll up 1-minute bars into hourly bars.
-- Add more as needed (daily, weekly).
CREATE MATERIALIZED VIEW IF NOT EXISTS market_bars_1h
    WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time) AS time,
    symbol,
    '1h'                        AS frequency,
    first(open, time)           AS open,
    max(high)                   AS high,
    min(low)                    AS low,
    last(close, time)           AS close,
    sum(volume)                 AS volume
FROM market_bars
WHERE frequency = '1m'
GROUP BY time_bucket('1 hour', time), symbol
WITH NO DATA;

SELECT add_continuous_aggregate_policy('market_bars_1h',
    start_offset => INTERVAL '3 hours',
    end_offset   => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute');
