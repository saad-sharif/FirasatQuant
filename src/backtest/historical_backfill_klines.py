import time

import truststore

truststore.inject_into_ssl()  # use the OS certificate store instead of ccxt/requests' bundled one

import ccxt

from src.config import load_config
from src.ingestion.stream_to_postgresql import PostgresStreamWriter

INTERVAL = "1m"
MS_PER_DAY = 24 * 60 * 60 * 1000


def backfill_klines(symbol: str, months_back: int, writer: PostgresStreamWriter) -> int:
    """Pulls historical 1m candles from Binance's REST klines endpoint (not
    the websocket, which only sees data from when it's started) and upserts
    them into klines_1m."""
    exchange = ccxt.binance()
    symbol = symbol.upper()
    since = exchange.milliseconds() - months_back * 30 * MS_PER_DAY
    total = 0

    while True:
        candles = exchange.publicGetKlines(
            {"symbol": symbol, "interval": INTERVAL, "startTime": since, "limit": 1000}
        )
        if not candles:
            break

        for candle in candles:
            writer.write_kline_from_rest(symbol, INTERVAL, candle)
        total += len(candles)

        last_close_time = candles[-1][6]
        print(f"Backfilled {total} candles, up to {exchange.iso8601(last_close_time)}")

        if len(candles) < 1000:
            break
        since = last_close_time + 1
        time.sleep(exchange.rateLimit / 1000)

    return total


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=12, help="How many months of history to backfill")
    args = parser.parse_args()

    config = load_config()
    writer = PostgresStreamWriter()
    try:
        total = backfill_klines(config["ingestion"]["symbol"], args.months, writer)
        print(f"Done. {total} candles backfilled.")
    finally:
        writer.close()
