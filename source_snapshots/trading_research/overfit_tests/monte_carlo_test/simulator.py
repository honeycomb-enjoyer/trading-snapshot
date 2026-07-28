import random


def build_equity_curve(trade_returns):
    """
    Convert trade R list into equity curve.
    Example:
    [1, -1, 2] -> [0, 1, 0, 2]
    """
    equity = [0]
    current = 0

    for r in trade_returns:
        current += r
        equity.append(current)

    return equity


# ==========================================
# SHUFFLE MODE
# ==========================================
def run_shuffle_mc(trades, simulations=1000, seed=42):
    """
    Monte Carlo via reshuffling historical trade order.

    Input:
        trades = backtester trades list
                 each trade must contain trade["R"]

    Output:
        list of equity curves
    """
    rng = random.Random(seed)

    trade_returns = [t["R"] for t in trades]

    if not trade_returns:
        raise RuntimeError("No trades for shuffle Monte Carlo")

    equity_curves = []

    for _ in range(simulations):
        shuffled = trade_returns.copy()
        rng.shuffle(shuffled)

        curve = build_equity_curve(shuffled)
        equity_curves.append(curve)

    return equity_curves


# ==========================================
# SYNTHETIC MODE
# ==========================================
def sample_trade(winrate, be_rate, avg_win, avg_loss, rng=None):
    """
    Sample one synthetic trade.
    """
    r = (rng or random).random()

    if r < winrate:
        return avg_win

    elif r < (winrate + be_rate):
        return 0

    else:
        return avg_loss


def run_synthetic_mc(
    winrate,
    be_rate,
    avg_win,
    avg_loss,
    trades_per_run,
    simulations=1000,
    seed=42
):
    """
    Monte Carlo via synthetic sampling from probabilities.
    """
    rng = random.Random(seed)

    equity_curves = []

    for _ in range(simulations):
        returns = []

        for _ in range(trades_per_run):
            trade_r = sample_trade(
                winrate=winrate,
                be_rate=be_rate,
                avg_win=avg_win,
                avg_loss=avg_loss,
                rng=rng,
            )
            returns.append(trade_r)

        curve = build_equity_curve(returns)
        equity_curves.append(curve)

    return equity_curves
