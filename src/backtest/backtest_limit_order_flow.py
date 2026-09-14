import os
from datetime import timedelta

import psycopg2
from dotenv import load_dotenv

from src.strategy.strategy import OrderFlowConfirmedStrategy

load_dotenv()

FEE_PCT = 0.001  # 0.1% per trade, same as backtest.py -- Binance spot doesn't split maker/taker like futures does


def fetch_minute_flow(symbol: str, dsn: str | None = None) -> dict:
    """One aggregation query over agg_trades, bucketed to the minute so it
    lines up with klines_1m -- far cheaper than querying agg_trades per
    candle. is_buyer_maker=False means the taker was a buyer (aggressive
    buy); True means the taker was a seller (aggressive sell)."""
    conn = psycopg2.connect(dsn or os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT date_trunc('minute', trade_time) AS minute,
                       SUM(CASE WHEN is_buyer_maker = FALSE THEN quantity ELSE 0 END) AS buy_vol,
                       SUM(CASE WHEN is_buyer_maker = TRUE THEN quantity ELSE 0 END) AS sell_vol
                FROM agg_trades
                WHERE symbol = %s
                GROUP BY minute
                ORDER BY minute
                """,
                (symbol.upper(),),
            )
            return {row[0]: (float(row[1]), float(row[2])) for row in cur.fetchall()}
    finally:
        conn.close()


def try_fill(conn, symbol: str, side: str, limit_price: float, placed_at, expiry_minutes: int):
    """Looks for the first actual trade tick that crosses the limit price
    within the order's expiry window -- more realistic than checking candle
    high/low, since it's the exact price path, not just the extremes."""
    end = placed_at + timedelta(minutes=expiry_minutes)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_time, price FROM agg_trades
            WHERE symbol = %s AND trade_time > %s AND trade_time <= %s
            ORDER BY trade_time ASC
            """,
            (symbol.upper(), placed_at, end),
        )
        for trade_time, price in cur.fetchall():
            price = float(price)
            if (side == "buy" and price <= limit_price) or (side == "sell" and price >= limit_price):
                return trade_time, price
    return None, None


def run_limit_order_flow_backtest(
    rows: list[tuple],
    strategy: OrderFlowConfirmedStrategy,
    trade_amount_usd: float,
    symbol: str,
    limit_offset_pct: float = 0.05,
    order_expiry_minutes: int = 5,
    fee_pct: float = FEE_PCT,
    dsn: str | None = None,
):
    """Same shape as backtest.run_backtest's return (list of trade dicts with
    pnl/fees_paid/gross_pnl) so the existing summarize()/plot_price_and_portfolio()
    work unchanged. Spot only -- long entries on BUY while flat, exit on SELL
    while holding; no shorting, matching what the live account can actually do."""
    dsn = dsn or os.environ["DATABASE_URL"]
    conn = psycopg2.connect(dsn)
    minute_flow = fetch_minute_flow(symbol, dsn)

    # klines_1m usually has far more history than agg_trades -- without this,
    # minutes with no agg_trades coverage default to (0, 0) buy/sell volume,
    # which never confirms a signal, so the strategy silently sits flat for
    # the whole gap. Trim rows to only where both datasets overlap.
    if minute_flow:
        agg_trades_start = min(minute_flow)
        rows = [r for r in rows if r[0] >= agg_trades_start]

    flow_series = [minute_flow.get(ts, (0.0, 0.0)) for ts, _ in rows]

    lookback = strategy.lookback_minutes
    trades = []
    in_position = False
    entry_price = entry_time = quantity = None
    closes_window: list[float] = []

    try:
        for i, (ts, close) in enumerate(rows):
            close = float(close)
            closes_window.insert(0, close)
            if len(closes_window) > lookback:
                closes_window.pop()

            start_idx = max(0, i - lookback + 1)
            buy_vol = sum(b for b, _ in flow_series[start_idx : i + 1])
            sell_vol = sum(s for _, s in flow_series[start_idx : i + 1])

            signal = strategy.signal(closes_window, buy_vol, sell_vol)

            if not in_position and signal == "BUY":
                limit_price = close * (1 - limit_offset_pct / 100)
                fill_time, fill_price = try_fill(conn, symbol, "buy", limit_price, ts, order_expiry_minutes)
                if fill_price is not None:
                    buy_fee = trade_amount_usd * fee_pct
                    quantity = (trade_amount_usd - buy_fee) / fill_price
                    entry_price, entry_time = fill_price, fill_time
                    in_position = True

            elif in_position and signal == "SELL":
                limit_price = close * (1 + limit_offset_pct / 100)
                fill_time, fill_price = try_fill(conn, symbol, "sell", limit_price, ts, order_expiry_minutes)
                if fill_price is not None:
                    buy_fee = trade_amount_usd * fee_pct
                    gross = quantity * fill_price
                    sell_fee = gross * fee_pct
                    net = gross - sell_fee
                    pnl = net - trade_amount_usd
                    fees_paid = buy_fee + sell_fee
                    trades.append(
                        {
                            "entry_time": entry_time,
                            "exit_time": fill_time,
                            "entry_price": entry_price,
                            "exit_price": fill_price,
                            "pnl": pnl,
                            "fees_paid": fees_paid,
                            "gross_pnl": pnl + fees_paid,
                        }
                    )
                    in_position = False
    finally:
        conn.close()

    return trades


if __name__ == "__main__":
    import argparse

    from src.backtest.backtest_klines import fetch_closes, summarize
    from src.config import load_config

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit-offset-pct", type=float, default=0.05, help="%% away from signal price for the limit order")
    parser.add_argument("--order-expiry-minutes", type=int, default=5)
    parser.add_argument("--balance", type=float, default=300.0)
    args = parser.parse_args()

    config = load_config()
    strategy_cfg = config["strategy"]
    symbol = config["ingestion"]["symbol"]
    strategy = OrderFlowConfirmedStrategy(strategy_cfg["threshold_pct"], strategy_cfg["lookback_minutes"])

    rows = fetch_closes(symbol)
    print(f"Backtesting {len(rows)} 1-minute candles: {rows[0][0]} to {rows[-1][0]}")

    trades = run_limit_order_flow_backtest(
        rows,
        strategy,
        strategy_cfg["trade_amount_usd"],
        symbol,
        limit_offset_pct=args.limit_offset_pct,
        order_expiry_minutes=args.order_expiry_minutes,
    )
    summarize(trades, len(rows), args.balance)
