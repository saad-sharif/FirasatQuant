"""
Leveraged limit-order backtest for ThresholdStrategy.

Extends the existing threshold-based signal logic (buy/sell on pct move
over a lookback window) with:
  - Limit order simulation: an order only fills if price actually trades
    through the limit level within the candle (checked via high/low),
    and expires if not filled within `order_expiry_minutes`.
  - Leverage: position size scaled by leverage against available equity.
  - Simplified isolated-margin liquidation: a position is force-closed
    if the adverse move exceeds 1/leverage (before fees). This is a
    simplification of real maintenance-margin tiers, but stops the
    backtest from showing impossible results.
  - Maker fee on limit fills, taker fee on forced liquidations.

This is a separate, higher-risk simulation from src/backtest/backtest.py
(spot, fixed-notional, no leverage) -- it doesn't touch the live pipeline
or ThresholdStrategy's decision logic, which both live in
src/strategy/strategy.py and stay shared between the two.

ASSUMPTIONS -- adjust if your schema differs:
  - klines_1m table has: symbol, open_time, open_price, high_price,
    low_price, close_price (numeric/decimal types)

Usage:
    python -m src.backtest.backtest_leveraged_limit --symbol BTCUSDT \
        --start 2025-01-01 --end 2026-01-01 \
        --leverage 3 --limit-offset-pct 0.05 \
        --threshold-pct 0.5 --lookback-minutes 15
"""

import os
from dataclasses import dataclass
from datetime import datetime

import psycopg2
from dotenv import load_dotenv

from src.strategy.strategy import ThresholdStrategy

load_dotenv()


@dataclass
class Candle:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class Trade:
    side: str  # "LONG" or "SHORT"
    entry_time: datetime
    entry_price: float
    exit_time: datetime = None
    exit_price: float = None
    size: float = 0.0
    pnl: float = 0.0
    liquidated: bool = False
    fees_paid: float = 0.0


class LeveragedLimitBacktester:
    def __init__(
        self,
        symbol: str,
        strategy: ThresholdStrategy,
        leverage: float = 3.0,
        limit_offset_pct: float = 0.05,
        initial_capital: float = 10_000.0,
        maker_fee_pct: float = 0.02,
        taker_fee_pct: float = 0.04,
        order_expiry_minutes: int = 5,
        dsn: str | None = None,
    ):
        self.symbol = symbol.upper()
        self.strategy = strategy
        self.leverage = leverage
        self.limit_offset_pct = limit_offset_pct / 100
        self.equity = initial_capital
        self.maker_fee_pct = maker_fee_pct / 100
        self.taker_fee_pct = taker_fee_pct / 100
        self.order_expiry_minutes = order_expiry_minutes
        self.dsn = dsn or os.environ["DATABASE_URL"]

        self.position: Trade | None = None
        self.pending_order: dict | None = None
        self.trades: list[Trade] = []
        self.equity_curve: list[tuple[datetime, float]] = []
        self.candles: list[Candle] = []

    def _fetch_candles(self, start: str, end: str) -> list[Candle]:
        conn = psycopg2.connect(self.dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT open_time, open_price, high_price, low_price, close_price
                    FROM klines_1m
                    WHERE symbol = %s AND open_time BETWEEN %s AND %s
                    ORDER BY open_time ASC
                    """,
                    (self.symbol, start, end),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [Candle(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]

    def _place_limit_order(self, side: str, ref_price: float, ts: datetime):
        offset = ref_price * self.limit_offset_pct
        limit_price = ref_price - offset if side == "BUY" else ref_price + offset
        self.pending_order = {"side": side, "limit_price": limit_price, "placed_at": ts}

    def _try_fill_pending(self, candle: Candle):
        if not self.pending_order:
            return
        order = self.pending_order
        age_minutes = (candle.open_time - order["placed_at"]).total_seconds() / 60
        if age_minutes > self.order_expiry_minutes:
            self.pending_order = None  # expired, cancel
            return

        filled = (order["side"] == "BUY" and candle.low <= order["limit_price"]) or (
            order["side"] == "SELL" and candle.high >= order["limit_price"]
        )
        if not filled:
            return

        fill_price = order["limit_price"]

        if self.position is None:
            notional = self.equity * self.leverage
            size = notional / fill_price
            fee = fill_price * self.maker_fee_pct * size
            self.equity -= fee
            side = "LONG" if order["side"] == "BUY" else "SHORT"
            self.position = Trade(side=side, entry_time=candle.open_time, entry_price=fill_price, size=size)
        else:
            self._close_position(fill_price, candle.open_time, fee_pct=self.maker_fee_pct)

        self.pending_order = None

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
        self.equity += pnl
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

    def run(self, start: str, end: str) -> dict:
        candles = self._fetch_candles(start, end)
        if not candles:
            raise ValueError("No candles returned — check symbol/date range/table+column names.")
        self.candles = candles

        lookback = self.strategy.lookback_minutes
        closes_window: list[float] = []

        for candle in candles:
            closes_window.insert(0, candle.close)
            if len(closes_window) > lookback:
                closes_window.pop()

            self._check_liquidation(candle)
            self._try_fill_pending(candle)

            signal = self.strategy.signal(closes_window)

            if self.position is None and self.pending_order is None:
                if signal in ("BUY", "SELL"):
                    self._place_limit_order(signal, candle.close, candle.open_time)
            elif self.position is not None:
                opposite = "SELL" if self.position.side == "LONG" else "BUY"
                if signal == opposite and self.pending_order is None:
                    self._place_limit_order(opposite, candle.close, candle.open_time)

            self.equity_curve.append((candle.open_time, self.equity))

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
        return {
            "final_equity": round(self.equity, 2),
            "total_return_pct": round((self.equity / start_equity - 1) * 100, 2),
            "num_trades": len(self.trades),
            "win_rate_pct": round(len(wins) / len(self.trades) * 100, 2) if self.trades else 0,
            "liquidations": sum(1 for t in self.trades if t.liquidated),
            "max_drawdown_pct": round(max_dd * 100, 2),
        }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Leveraged limit-order backtest")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start", required=True, help="e.g. 2025-01-01")
    parser.add_argument("--end", required=True, help="e.g. 2026-01-01")
    parser.add_argument("--leverage", type=float, default=3.0)
    parser.add_argument("--limit-offset-pct", type=float, default=0.05, help="%% away from market price for the limit order")
    parser.add_argument("--threshold-pct", type=float, default=0.5)
    parser.add_argument("--lookback-minutes", type=int, default=15)
    parser.add_argument("--initial-capital", type=float, default=10_000.0)
    parser.add_argument("--order-expiry-minutes", type=int, default=5)
    args = parser.parse_args()

    strategy = ThresholdStrategy(args.threshold_pct, args.lookback_minutes)
    backtester = LeveragedLimitBacktester(
        symbol=args.symbol,
        strategy=strategy,
        leverage=args.leverage,
        limit_offset_pct=args.limit_offset_pct,
        initial_capital=args.initial_capital,
        order_expiry_minutes=args.order_expiry_minutes,
    )
    result = backtester.run(args.start, args.end)
    print(result)
