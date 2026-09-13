import os
from datetime import datetime

import truststore

truststore.inject_into_ssl()  # use the OS certificate store instead of ccxt/requests' bundled one

import ccxt
import psycopg2
from dotenv import load_dotenv

load_dotenv()


class BinanceExecutor:
    """Places fixed-notional market orders on Binance based on a strategy
    signal. Tracks the last filled order per symbol in the `orders` table so
    it never buys while already holding a position, or sells without one.
    Defaults to dry-run (logs the intended order, sends nothing) unless
    live=True."""

    def __init__(self, symbol: str, trade_amount_usd: float, live: bool = False, dsn: str | None = None):
        self.symbol = symbol.upper()
        # BTCUSDT -> BTC/USDT; assumes a 4-letter quote asset like the rest of the config.
        self.market_symbol = f"{self.symbol[:-4]}/{self.symbol[-4:]}"
        self.trade_amount_usd = trade_amount_usd
        self.live = live

        self.conn = psycopg2.connect(dsn or os.environ["DATABASE_URL"])
        self.conn.autocommit = True

        self.exchange = ccxt.binance(
            {
                "apiKey": os.environ["BINANCE_API_KEY"],
                "secret": os.environ["BINANCE_API_SECRET"],
            }
        )

    def close(self):
        self.conn.close()

    def has_open_position(self) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT side FROM orders
                WHERE symbol = %s AND status IN ('filled', 'dry_run')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (self.symbol,),
            )
            row = cur.fetchone()
        return row is not None and row[0] == "BUY"

    def execute(self, signal: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        price = self.exchange.fetch_ticker(self.market_symbol)["last"]
        open_position = self.has_open_position()

        if signal == "BUY" and not open_position:
            self._place_order("buy", price, ts)
        elif signal == "SELL" and open_position:
            self._place_order("sell", price, ts)
        else:
            print(f"[{ts}] Signal={signal}, price={price}, open_position={open_position} -> no action")

    def _place_order(self, side: str, price: float, ts: str):
        quantity = round(self.trade_amount_usd / price, 6)

        if not self.live:
            print(f"[{ts}] [DRY RUN] Would {side.upper()} {quantity} {self.market_symbol} (~${self.trade_amount_usd}) at ~{price}")
            self._log_order(side.upper(), quantity, price, None, "dry_run")
            return

        order = self.exchange.create_market_order(self.market_symbol, side, quantity)
        fill_price = order.get("average") or price
        print(f"[{ts}] Placed {side.upper()} order {order['id']}: {quantity} {self.market_symbol} @ ~{fill_price}")
        self._log_order(side.upper(), quantity, fill_price, order["id"], "filled")

    def _log_order(self, side: str, quantity: float, price: float, exchange_order_id: str | None, status: str):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders (symbol, side, quantity, price, notional_usd, exchange_order_id, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (self.symbol, side, quantity, price, self.trade_amount_usd, exchange_order_id, status),
            )


if __name__ == "__main__":
    import argparse
    import time

    from src.config import load_config
    from src.strategy.strategy import LiveThresholdStrategy

    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Place real orders on Binance (default: dry run, logs only)")
    parser.add_argument("--interval", type=float, default=60.0, help="Seconds between signal checks")
    args = parser.parse_args()

    config = load_config()
    strategy = LiveThresholdStrategy.from_config(config)
    executor = BinanceExecutor(
        symbol=config["ingestion"]["symbol"],
        trade_amount_usd=config["strategy"]["trade_amount_usd"],
        live=args.live,
    )

    print(f"Running {'LIVE' if args.live else 'DRY RUN'}, checking every {args.interval}s. Ctrl+C to stop.")
    try:
        while True:
            try:
                executor.execute(strategy.latest_signal())
            except ccxt.NetworkError as e:
                # Transient connectivity/timeout hitting Binance -- skip this
                # check and try again next interval rather than crashing.
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{ts}] Network error, skipping this check: {e}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        strategy.close()
        executor.close()
