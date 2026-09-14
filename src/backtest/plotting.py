import matplotlib.pyplot as plt


def build_equity_curve(rows: list[tuple], trades: list[dict], account_balance_usd: float, pnl_key: str = "pnl"):
    """Steps a starting balance up/down by each trade's pnl at its exit time,
    aligned to the same timestamps as `rows` so it can share an x-axis with
    price. Pass pnl_key="gross_pnl" to build the pre-fee curve instead."""
    pnl_by_exit_time: dict = {}
    for t in trades:
        pnl_by_exit_time[t["exit_time"]] = pnl_by_exit_time.get(t["exit_time"], 0.0) + t[pnl_key]

    equity = account_balance_usd
    times, values = [], []
    for ts, _ in rows:
        if ts in pnl_by_exit_time:
            equity += pnl_by_exit_time[ts]
        times.append(ts)
        values.append(equity)
    return times, values


def _pct_change(values: list[float]) -> list[float]:
    base = values[0]
    return [(v / base - 1) * 100 for v in values]


def _plot_pct_change(price_times, price_values, equity_times, equity_values, symbol: str, title: str, gross_times=None, gross_values=None):
    """All series plotted as % change from the start of the period, on one
    shared axis -- price and portfolio value differ by orders of magnitude in
    raw dollars, so that's the only way to actually compare their movements
    rather than one line dwarfing the other. The optional gross curve (before
    fees) shows how much of the strategy's real edge fee drag is eating."""
    fig, ax = plt.subplots(figsize=(14, 6))

    ax.plot(price_times, _pct_change(price_values), color="tab:blue", label=f"{symbol} price")
    ax.plot(equity_times, _pct_change(equity_values), color="tab:orange", label="Portfolio value (net)")
    if gross_times is not None:
        ax.plot(gross_times, _pct_change(gross_values), color="tab:green", linestyle="--", label="Portfolio value (gross, before fees)")
    ax.axhline(0, color="gray", linewidth=0.8, linestyle="--")

    ax.set_xlabel("Time")
    ax.set_ylabel("% change from start")
    ax.legend(loc="upper left")

    fig.suptitle(title)
    fig.tight_layout()
    return fig


def plot_price_and_portfolio(rows: list[tuple], trades: list[dict], account_balance_usd: float, symbol: str = "BTCUSDT"):
    """For the plain fixed-notional backtest in src/backtest/backtest_klines.py."""
    price_times = [ts for ts, _ in rows]
    price_values = [float(p) for _, p in rows]
    equity_times, equity_values = build_equity_curve(rows, trades, account_balance_usd)
    gross_times, gross_values = build_equity_curve(rows, trades, account_balance_usd, pnl_key="gross_pnl")

    return _plot_pct_change(
        price_times,
        price_values,
        equity_times,
        equity_values,
        symbol,
        f"{symbol} price vs. portfolio value (% change from start)",
        gross_times,
        gross_values,
    )


def plot_leveraged_backtest(backtester, symbol: str = "BTCUSDT"):
    """Same idea, for a backtester (LeveragedLimitBacktester or
    LeveragedOrderFlowBacktester) that already tracks its own candles and
    equity_curve. Only LeveragedOrderFlowBacktester tracks gross_equity_curve
    -- the gross line is omitted if it's not present."""
    price_times = [c.open_time for c in backtester.candles]
    price_values = [c.close for c in backtester.candles]
    equity_times = [ts for ts, _ in backtester.equity_curve]
    equity_values = [eq for _, eq in backtester.equity_curve]

    gross_times = gross_values = None
    gross_curve = getattr(backtester, "gross_equity_curve", None)
    if gross_curve:
        gross_times = [ts for ts, _ in gross_curve]
        gross_values = [eq for _, eq in gross_curve]

    return _plot_pct_change(
        price_times,
        price_values,
        equity_times,
        equity_values,
        symbol,
        f"{symbol} price vs. portfolio value (% change from start)",
        gross_times,
        gross_values,
    )
