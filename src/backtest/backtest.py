import os

import psycopg2
from dotenv import load_dotenv

from src.config import load_config
from src.strategy.strategy import ThresholdStrategy

load_dotenv()

FEE_PCT = 0.001  # 0.1% per trade -- confirm your actual rate at Binance: Wallet > Fee > Trading Fee


def fetch_closes(symbol: str, dsn: str | None = None) -> list[tuple]:
    conn = psycopg2.connect(dsn or os.environ["DATABASE_URL"])
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT open_time, close_price FROM klines_1m
            WHERE symbol = %s
            ORDER BY open_time ASC
            """,
            (symbol.upper(),),
        )
        rows = cur.fetchall()
    conn.close()
    return rows


def run_backtest(rows: list[tuple], strategy: ThresholdStrategy, trade_amount_usd: float, fee_pct: float = FEE_PCT):
    """Replays the strategy bar-by-bar over historical closes, simulating a
    fixed-notional position sized like BinanceExecutor's real orders. Bar-by-bar
    on 1m closes only -- no slippage or partial fills, so treat results as
    directional, not exact."""
    trades = []
    in_position = False
    quantity = 0.0
    entry_price = 0.0
    entry_time = None

    for i in range(strategy.lookback_minutes, len(rows)):
        window = [float(price) for _, price in rows[i - strategy.lookback_minutes + 1 : i + 1]][::-1]
        ts, price = rows[i]
        price = float(price)

        signal = strategy.signal(window)

        if signal == "BUY" and not in_position:
            buy_fee = trade_amount_usd * fee_pct
            quantity = (trade_amount_usd - buy_fee) / price
            entry_price = price
            entry_time = ts
            in_position = True
        elif signal == "SELL" and in_position:
            gross = quantity * price
            sell_fee = gross * fee_pct
            net = gross - sell_fee
            pnl = net - trade_amount_usd
            fees_paid = buy_fee + sell_fee
            trades.append(
                {
                    "entry_time": entry_time,
                    "exit_time": ts,
                    "entry_price": entry_price,
                    "exit_price": price,
                    "pnl": pnl,
                    "fees_paid": fees_paid,
                    "gross_pnl": pnl + fees_paid,
                }
            )
            in_position = False

    return trades


def summarize(trades: list[dict], num_candles: int, account_balance_usd: float):
    if not trades:
        print("No trades were triggered over this period.")
        return

    total_pnl = sum(t["pnl"] for t in trades)
    total_gross_pnl = sum(t["gross_pnl"] for t in trades)
    total_fees = sum(t["fees_paid"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]
    win_rate = len(wins) / len(trades) * 100
    trades_per_day = len(trades) / (num_candles / (24 * 60))

    print(f"Trades: {len(trades)} ({trades_per_day:.2f}/day)")
    print(f"Win rate: {win_rate:.1f}%")
    print(f"Gross PnL (before fees): ${total_gross_pnl:.2f}")
    print(f"Fees paid: ${total_fees:.2f}")
    print(f"Net PnL: ${total_pnl:.2f}")
    print(f"Return on ${account_balance_usd:.2f} starting balance: {total_pnl / account_balance_usd * 100:.2f}%")
    print(f"Avg PnL per trade: ${total_pnl / len(trades):.2f}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--balance", type=float, default=300.0, help="Starting account balance, for return %%")
    args = parser.parse_args()

    config = load_config()
    strategy_cfg = config["strategy"]
    strategy = ThresholdStrategy(
        threshold_pct=strategy_cfg["threshold_pct"],
        lookback_minutes=strategy_cfg["lookback_minutes"],
    )

    rows = fetch_closes(config["ingestion"]["symbol"])
    if len(rows) <= strategy.lookback_minutes:
        raise SystemExit(f"Only {len(rows)} candles available -- run historical_backfill.py first.")

    print(f"Backtesting {len(rows)} 1-minute candles: {rows[0][0]} to {rows[-1][0]}")
    trades = run_backtest(rows, strategy, strategy_cfg["trade_amount_usd"])
    summarize(trades, len(rows), args.balance)
