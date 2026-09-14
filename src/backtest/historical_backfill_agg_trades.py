import io
import zipfile
from datetime import date, datetime, timedelta, timezone

import truststore

truststore.inject_into_ssl()  # use the OS certificate store instead of requests' bundled one

import requests

from src.config import load_config
from src.ingestion.stream_to_postgresql import PostgresStreamWriter

BATCH_SIZE = 5000


def backfill_agg_trades(symbol: str, days_back: int, writer: PostgresStreamWriter) -> int:
    """Pulls historical aggTrade ticks from Binance's public daily dump files
    (data.binance.vision) -- there's no practical REST way to pull a month+ of
    tick data (the live aggTrades REST endpoint caps time windows to under an
    hour per call). Ends yesterday: today's data is already covered by the
    live websocket pipeline, and today's dump file won't exist yet anyway."""
    symbol = symbol.upper()
    total = 0

    for i in range(days_back, 0, -1):
        day = date.today() - timedelta(days=i)
        url = f"https://data.binance.vision/data/spot/daily/aggTrades/{symbol}/{symbol}-aggTrades-{day:%Y-%m-%d}.zip"

        resp = requests.get(url, timeout=60)
        if resp.status_code == 404:
            print(f"{day}: no dump file published, skipping")
            continue
        resp.raise_for_status()

        z = zipfile.ZipFile(io.BytesIO(resp.content))
        day_count = 0
        batch = []
        with z.open(z.namelist()[0]) as f:
            for line in f:
                agg_id, price, qty, first_id, last_id, ts_us, is_buyer_maker = line.decode().strip().split(",")[:7]
                ts = datetime.fromtimestamp(int(ts_us) / 1_000_000, tz=timezone.utc)
                batch.append(
                    (int(agg_id), symbol, price, qty, int(first_id), int(last_id), ts, ts, is_buyer_maker.lower() == "true")
                )
                if len(batch) >= BATCH_SIZE:
                    writer.write_agg_trades_batch(batch)
                    day_count += len(batch)
                    batch = []
        if batch:
            writer.write_agg_trades_batch(batch)
            day_count += len(batch)

        total += day_count
        print(f"{day}: {day_count} trades (running total {total})")

    return total


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="How many days of aggTrade history to backfill")
    args = parser.parse_args()

    config = load_config()
    writer = PostgresStreamWriter()
    try:
        total = backfill_agg_trades(config["ingestion"]["symbol"], args.days, writer)
        print(f"Done. {total} agg trades backfilled.")
    finally:
        writer.close()
