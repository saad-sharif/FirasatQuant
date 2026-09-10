import json
import os
import threading
import time

import psycopg2
from dotenv import load_dotenv

from src.config import PROJECT_ROOT

load_dotenv()


class PostgresStreamWriter:
    """Writes parsed Binance stream payloads (trade / bookTicker / kline_1m)
    to their matching Postgres table. Can be used as a live message handler
    or to backfill from the JSONL files BinanceStreamIngestor writes."""

    def __init__(self, dsn: str | None = None):
        self.conn = psycopg2.connect(dsn or os.environ["DATABASE_URL"])
        self.conn.autocommit = True

    def close(self):
        self.conn.close()

    def write_trade(self, data: dict):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trades (trade_id, symbol, price, quantity, trade_time, event_time, is_buyer_maker)
                VALUES (%s, %s, %s, %s, to_timestamp(%s / 1000.0), to_timestamp(%s / 1000.0), %s)
                ON CONFLICT (trade_id) DO NOTHING
                """,
                (data["t"], data["s"], data["p"], data["q"], data["T"], data["E"], data["m"]),
            )

    def write_book_ticker(self, data: dict):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO book_ticker (update_id, symbol, best_bid_price, best_bid_qty, best_ask_price, best_ask_qty)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (update_id) DO NOTHING
                """,
                (data["u"], data["s"], data["b"], data["B"], data["a"], data["A"]),
            )

    def write_kline(self, data: dict):
        k = data["k"]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO klines_1m (
                    symbol, interval, open_time, close_time, open_price, close_price,
                    high_price, low_price, base_volume, quote_volume,
                    taker_buy_base_volume, taker_buy_quote_volume, num_trades,
                    is_closed, first_trade_id, last_trade_id
                )
                VALUES (
                    %s, %s, to_timestamp(%s / 1000.0), to_timestamp(%s / 1000.0), %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (symbol, interval, open_time) DO UPDATE SET
                    close_time = EXCLUDED.close_time,
                    close_price = EXCLUDED.close_price,
                    high_price = EXCLUDED.high_price,
                    low_price = EXCLUDED.low_price,
                    base_volume = EXCLUDED.base_volume,
                    quote_volume = EXCLUDED.quote_volume,
                    taker_buy_base_volume = EXCLUDED.taker_buy_base_volume,
                    taker_buy_quote_volume = EXCLUDED.taker_buy_quote_volume,
                    num_trades = EXCLUDED.num_trades,
                    is_closed = EXCLUDED.is_closed,
                    last_trade_id = EXCLUDED.last_trade_id
                """,
                (
                    k["s"], k["i"], k["t"], k["T"], k["o"], k["c"],
                    k["h"], k["l"], k["v"], k["q"],
                    k["V"], k["Q"], k["n"], k["x"], k["f"], k["L"],
                ),
            )

    WRITERS = {
        "trade": write_trade,
        "bookTicker": write_book_ticker,
        "kline_1m": write_kline,
    }

    def write(self, stream: str, data: dict):
        handler = self.WRITERS.get(stream)
        if handler:
            handler(self, data)

    def load_jsonl(self, path: str, stream: str):
        with open(path, "r") as f:
            for line in f:
                self.write(stream, json.loads(line))

    def backfill_from_config(self, config: dict):
        ingestion_cfg = config["ingestion"]
        symbol = ingestion_cfg["symbol"].lower()
        data_dir = ingestion_cfg["data_dir"]
        for stream in ingestion_cfg["streams"]:
            path = os.path.join(PROJECT_ROOT, data_dir, f"{symbol}_{stream}.jsonl")
            if os.path.exists(path):
                print(f"Loading {path} into {stream}...")
                self.load_jsonl(path, stream)

    def tail_jsonl(self, path: str, stream: str, poll_interval: float = 1.0):
        """Follow a growing JSONL file, writing each new line to Postgres as
        BinanceStreamIngestor appends it."""
        with open(path, "r") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(poll_interval)
                    continue
                self.write(stream, json.loads(line))

    def follow_from_config(self, config: dict, poll_interval: float = 1.0):
        ingestion_cfg = config["ingestion"]
        symbol = ingestion_cfg["symbol"].lower()
        data_dir = ingestion_cfg["data_dir"]
        threads = [
            threading.Thread(
                target=self.tail_jsonl,
                args=(
                    os.path.join(PROJECT_ROOT, data_dir, f"{symbol}_{stream}.jsonl"),
                    stream,
                    poll_interval,
                ),
                daemon=True,
            )
            for stream in ingestion_cfg["streams"]
        ]
        for t in threads:
            t.start()
        print("Following JSONL files for new rows... (Ctrl+C to stop)")
        for t in threads:
            t.join()


if __name__ == "__main__":
    import argparse

    from src.config import load_config

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Keep tailing the JSONL files for new rows instead of exiting after backfill",
    )
    args = parser.parse_args()

    writer = PostgresStreamWriter()
    try:
        if args.follow:
            writer.follow_from_config(load_config())
        else:
            writer.backfill_from_config(load_config())
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        writer.close()
