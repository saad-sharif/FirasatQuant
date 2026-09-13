import matplotlib.pyplot as plt


def build_equity_curve(rows: list[tuple], trades: list[dict], account_balance_usd: float):
    """Steps a starting balance up/down by each trade's pnl at its exit time,
    aligned to the same timestamps as `rows` so it can share an x-axis with price."""
    pnl_by_exit_time: dict = {}
    for t in trades:
        pnl_by_exit_time[t["exit_time"]] = pnl_by_exit_time.get(t["exit_time"], 0.0) + t["pnl"]

    equity = account_balance_usd
    times, values = [], []
    for ts, _ in rows:
        if ts in pnl_by_exit_time:
            equity += pnl_by_exit_time[ts]
        times.append(ts)
        values.append(equity)
    return times, values


def plot_price_and_portfolio(rows: list[tuple], trades: list[dict], account_balance_usd: float, symbol: str = "BTCUSDT"):
    """Returns a matplotlib Figure with symbol price and portfolio value plotted
    against the same time axis on separate y-axes (their scales differ by orders
    of magnitude)."""
    price_times = [ts for ts, _ in rows]
    price_values = [float(p) for _, p in rows]
    equity_times, equity_values = build_equity_curve(rows, trades, account_balance_usd)

    fig, ax1 = plt.subplots(figsize=(14, 6))

    ax1.plot(price_times, price_values, color="tab:blue", label=f"{symbol} price")
    ax1.set_xlabel("Time")
    ax1.set_ylabel(f"{symbol} price (USD)", color="tab:blue")
    ax1.tick_params(axis="y", labelcolor="tab:blue")

    ax2 = ax1.twinx()
    ax2.plot(equity_times, equity_values, color="tab:orange", label="Portfolio value")
    ax2.set_ylabel("Portfolio value (USD)", color="tab:orange")
    ax2.tick_params(axis="y", labelcolor="tab:orange")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")

    fig.suptitle(f"{symbol} price vs. portfolio value")
    fig.tight_layout()
    return fig
