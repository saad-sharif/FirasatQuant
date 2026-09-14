"""
Leveraged, limit-order backtest using OrderFlowConfirmedStrategy (klines
momentum signal + aggTrade order-flow confirmation).

Combines:
  - Signal: OrderFlowConfirmedStrategy (src/strategy/strategy.py) -- klines
    threshold signal confirmed by aggTrade net order flow.
  - Fills: checked against actual aggTrade tick prices within the order's
    expiry window (src/backtest/backtest_limit_order_flow.py's try_fill),
    not candle high/low.
  - Risk: leverage + simplified isolated-margin liquidation, same mechanics
    as backtest_leveraged_limit.py (a position is force-closed if the
    adverse move exceeds 1/leverage before fees).

This is a what-if exploration for a possible future leveraged/margin
strategy -- it is NOT wired into the live execution bot, which remains
spot-only with no leverage.

Usage:
    python -m src.backtest.backtest_leveraged_order_flow \
        --leverage 3 --limit-offset-pct 0.05 --order-expiry-minutes 5 \
        --initial-capital 300
"""

import os
from datetime import datetime, timedelta

import psycopg2
from dotenv import load_dotenv

from src.backtest.backtest_leveraged_limit import Candle, Trade
from src.backtest.backtest_limit_order_flow import fetch_minute_flow, try_fill
from src.strategy.strategy import OrderFlowConfirmedStrategy

load_dotenv()


class LeveragedOrderFlowBacktester:
    def __init__(
        self,
        symbol: str,
        strategy: OrderFlowConfirmedStrategy,
        trade_amount_usd: float,
        leverage: float = 3.0,
        limit_offset_pct: float = 0.05,
        initial_capital: float = 300.0,
        # Binance Margin fees mirror spot's flat 0.1%, not Futures' lower
        # maker/taker split -- use these defaults unless you're actually
        # trading Futures, and override both explicitly if so.
        maker_fee_pct: float = 0.1,
        taker_fee_pct: float = 0.1,
        order_expiry_minutes: int = 5,
        dsn: str | None = None,
    ):
        self.symbol = symbol.upper()
        self.strategy = strategy
        self.trade_amount_usd = trade_amount_usd
        self.leverage = leverage
        self.limit_offset_pct = limit_offset_pct / 100
        self.equity = initial_capital
        self.maker_fee_pct = maker_fee_pct / 100
        self.taker_fee_pct = taker_fee_pct / 100
        self.order_expiry_minutes = order_expiry_minutes
        self.dsn = dsn or os.environ["DATABASE_URL"]

        self.position: Trade | None = None
        self.trades: list[Trade] = []
        self.equity_curve: list[tuple[datetime, float]] = []
        self.gross_equity_curve: list[tuple[datetime, float]] = []
        self.candles: list[Candle] = []
        self.total_fees = 0.0
        self.gross_equity = initial_capital

    def _fetch_candles(self) -> list[Candle]:
        conn = psycopg2.connect(self.dsn)
        try:
            with conn.cursor() as cur:
                # klines_1m usually has far more history than agg_trades --
                # without this, minutes with no agg_trades coverage default
                # to (0, 0) buy/sell volume, which never confirms a signal,
                # so the strategy silently sits flat for the whole gap.
                cur.execute("SELECT min(trade_time) FROM agg_trades WHERE symbol = %s", (self.symbol,))
                (agg_trades_start,) = cur.fetchone()
                if agg_trades_start is None:
                    raise ValueError(f"No agg_trades data for {self.symbol} -- run historical_backfill_agg_trades.py first.")

                cur.execute(
                    """
                    SELECT open_time, open_price, high_price, low_price, close_price
                    FROM klines_1m
                    WHERE symbol = %s AND open_time >= %s
                    ORDER BY open_time ASC
                    """,
                    (self.symbol, agg_trades_start),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [Candle(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]

    def _open_position(self, side: str, fill_price: float, fill_time: datetime):
        # Fixed stake, not a pct of current equity -- matches the fixed
        # trade_amount_usd every other backtest in this project uses, and
        # what the live bot actually risks per trade.
        notional = self.trade_amount_usd * self.leverage
        size = notional / fill_price
        fee = fill_price * self.maker_fee_pct * size
        self.equity -= fee
        self.total_fees += fee
        self.position = Trade(side=side, entry_time=fill_time, entry_price=fill_price, size=size, fees_paid=fee)

    def _close_position(self, exit_price: float, exit_time: datetime, fee_pct: float, liquidated: bool = False):
        pos = self.position
        direction = 1 if pos.side == "LONG" else -1
        pnl = direction * (exit_price - pos.entry_price) * pos.size
        fee = exit_price * fee_pct * pos.size
        pnl -= fee

        pos.exit_price = exit_price
        pos.exit_time = exit_time
        pos.pnl = pnl
        pos.liquidated = liquidated
        pos.fees_paid += fee
        self.equity += pnl
        self.total_fees += fee
        self.gross_equity += pnl + pos.fees_paid
        self.trades.append(pos)
        self.position = None

    def _check_liquidation(self, candle: Candle):
        if not self.position:
            return
        pos = self.position
        direction = 1 if pos.side == "LONG" else -1
        worst_price = candle.low if pos.side == "LONG" else candle.high
        adverse_pct = direction * (worst_price - pos.entry_price) / pos.entry_price
        if adverse_pct <= -1 / self.leverage:
            self._close_position(worst_price, candle.open_time, fee_pct=self.taker_fee_pct, liquidated=True)

    def run(self) -> dict:
        candles = self._fetch_candles()
        if not candles:
            raise ValueError("No candles found -- run historical_backfill_klines.py first.")
        self.candles = candles

        minute_flow = fetch_minute_flow(self.symbol, self.dsn)
        flow_series = [minute_flow.get(c.open_time, (0.0, 0.0)) for c in candles]

        conn = psycopg2.connect(self.dsn)
        lookback = self.strategy.lookback_minutes
        closes_window: list[float] = []

        try:
            for i, candle in enumerate(candles):
                closes_window.insert(0, candle.close)
                if len(closes_window) > lookback:
                    closes_window.pop()

                start_idx = max(0, i - lookback + 1)
                buy_vol = sum(b for b, _ in flow_series[start_idx : i + 1])
                sell_vol = sum(s for _, s in flow_series[start_idx : i + 1])

                self._check_liquidation(candle)

                signal = self.strategy.signal(closes_window, buy_vol, sell_vol)

                if self.position is None and signal in ("BUY", "SELL"):
                    side = "buy" if signal == "BUY" else "sell"
                    offset = candle.close * self.limit_offset_pct
                    limit_price = candle.close - offset if side == "buy" else candle.close + offset
                    fill_time, fill_price = try_fill(conn, self.symbol, side, limit_price, candle.open_time, self.order_expiry_minutes)
                    if fill_price is not None:
                        self._open_position("LONG" if side == "buy" else "SHORT", fill_price, fill_time)

                elif self.position is not None:
                    opposite = "sell" if self.position.side == "LONG" else "buy"
                    opposite_signal = "SELL" if self.position.side == "LONG" else "BUY"
                    if signal == opposite_signal:
                        offset = candle.close * self.limit_offset_pct
                        limit_price = candle.close - offset if opposite == "buy" else candle.close + offset
                        fill_time, fill_price = try_fill(conn, self.symbol, opposite, limit_price, candle.open_time, self.order_expiry_minutes)
                        if fill_price is not None:
                            self._close_position(fill_price, fill_time, fee_pct=self.maker_fee_pct)

                self.equity_curve.append((candle.open_time, self.equity))
                self.gross_equity_curve.append((candle.open_time, self.gross_equity))
        finally:
            conn.close()

        return self.summary()

    def summary(self) -> dict:
        if not self.equity_curve:
            return {"error": "no candles processed"}

        wins = [t for t in self.trades if t.pnl > 0]
        peak = float("-inf")
        max_dd = 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)

        start_equity = self.equity_curve[0][1]
        net_pnl = self.equity - start_equity
        return {
            "final_equity": round(self.equity, 2),
            "total_return_pct": round((self.equity / start_equity - 1) * 100, 2),
            "num_trades": len(self.trades),
            "win_rate_pct": round(len(wins) / len(self.trades) * 100, 2) if self.trades else 0,
            "liquidations": sum(1 for t in self.trades if t.liquidated),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "gross_pnl": round(net_pnl + self.total_fees, 2),
            "fees_paid": round(self.total_fees, 2),
            "net_pnl": round(net_pnl, 2),
        }


if __name__ == "__main__":
    import argparse

    from src.config import load_config

    parser = argparse.ArgumentParser()
    parser.add_argument("--leverage", type=float, default=3.0)
    parser.add_argument("--limit-offset-pct", type=float, default=0.05, help="%% away from signal price for the limit order")
    parser.add_argument("--order-expiry-minutes", type=int, default=5)
    parser.add_argument("--initial-capital", type=float, default=300.0)
    args = parser.parse_args()

    config = load_config()
    strategy_cfg = config["strategy"]
    strategy = OrderFlowConfirmedStrategy(strategy_cfg["threshold_pct"], strategy_cfg["lookback_minutes"])

    backtester = LeveragedOrderFlowBacktester(
        symbol=config["ingestion"]["symbol"],
        strategy=strategy,
        trade_amount_usd=strategy_cfg["trade_amount_usd"],
        leverage=args.leverage,
        limit_offset_pct=args.limit_offset_pct,
        initial_capital=args.initial_capital,
        order_expiry_minutes=args.order_expiry_minutes,
    )
    result = backtester.run()
    print(result)
