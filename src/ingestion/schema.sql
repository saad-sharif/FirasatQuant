-- Run this against the `firasatquant` database (public schema).

CREATE TABLE IF NOT EXISTS agg_trades (
    agg_trade_id    BIGINT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    price           NUMERIC NOT NULL,
    quantity        NUMERIC NOT NULL,
    first_trade_id  BIGINT NOT NULL,
    last_trade_id   BIGINT NOT NULL,
    trade_time      TIMESTAMPTZ NOT NULL,
    event_time      TIMESTAMPTZ NOT NULL,
    is_buyer_maker  BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS book_ticker (
    update_id       BIGINT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    best_bid_price  NUMERIC NOT NULL,
    best_bid_qty    NUMERIC NOT NULL,
    best_ask_price  NUMERIC NOT NULL,
    best_ask_qty    NUMERIC NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS klines_1m (
    symbol                  TEXT NOT NULL,
    interval                TEXT NOT NULL,
    open_time               TIMESTAMPTZ NOT NULL,
    close_time              TIMESTAMPTZ NOT NULL,
    open_price              NUMERIC NOT NULL,
    close_price             NUMERIC NOT NULL,
    high_price              NUMERIC NOT NULL,
    low_price               NUMERIC NOT NULL,
    base_volume             NUMERIC NOT NULL,
    quote_volume            NUMERIC NOT NULL,
    taker_buy_base_volume   NUMERIC NOT NULL,
    taker_buy_quote_volume  NUMERIC NOT NULL,
    num_trades              INT NOT NULL,
    is_closed               BOOLEAN NOT NULL,
    first_trade_id          BIGINT, -- NULL for candles backfilled from the REST klines endpoint
    last_trade_id           BIGINT, -- NULL for candles backfilled from the REST klines endpoint
    PRIMARY KEY (symbol, interval, open_time)
);

CREATE TABLE IF NOT EXISTS depth_snapshots (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol          TEXT NOT NULL,
    last_update_id  BIGINT NOT NULL,
    bids            JSONB NOT NULL,
    asks            JSONB NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,
    quantity            NUMERIC NOT NULL,
    price               NUMERIC NOT NULL,
    notional_usd        NUMERIC NOT NULL,
    exchange_order_id   TEXT,
    status              TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
