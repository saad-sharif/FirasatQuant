import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()


class ThresholdStrategy:
    """Pure decision logic: given the last `lookback_minutes` closes (most
    recent first), emits BUY/SELL/HOLD based on the pct price move across
    that window. Shared by live execution and backtesting so both run the
    exact same rule."""

    def __init__(self, threshold_pct: float, lookback_minutes: int):
        self.threshold_pct = threshold_pct
        self.lookback_minutes = lookback_minutes

    def signal(self, closes: list[float]) -> str:
        if len(closes) < 2:
            return "HOLD"

        latest, oldest = closes[0], closes[-1]
        pct_change = (latest - oldest) / oldest * 100

        if pct_change >= self.threshold_pct:
            return "BUY"
        if pct_change <= -self.threshold_pct:
            return "SELL"
        return "HOLD"


class LiveThresholdStrategy(ThresholdStrategy):
    """Reads the latest closes for `symbol` from klines_1m in Postgres and
    applies ThresholdStrategy against them."""

    def __init__(self, symbol: str, threshold_pct: float, lookback_minutes: int, dsn: str | None = None):
        super().__init__(threshold_pct, lookback_minutes)
        self.symbol = symbol.upper()
        self.conn = psycopg2.connect(dsn or os.environ["DATABASE_URL"])
        self.conn.autocommit = True

    @classmethod
    def from_config(cls, config: dict) -> "LiveThresholdStrategy":
        strategy_cfg = config["strategy"]
        return cls(
            symbol=config["ingestion"]["symbol"],
            threshold_pct=strategy_cfg["threshold_pct"],
            lookback_minutes=strategy_cfg["lookback_minutes"],
        )

    def latest_signal(self) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT close_price FROM klines_1m
                WHERE symbol = %s
                ORDER BY open_time DESC
                LIMIT %s
                """,
                (self.symbol, self.lookback_minutes),
            )
            closes = [float(row[0]) for row in cur.fetchall()]
        return self.signal(closes)

    def close(self):
        self.conn.close()
