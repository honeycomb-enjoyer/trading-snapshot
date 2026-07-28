DEFAULT_SCORING_CONFIG = {
    "min_trades": 200,
    "min_profit_factor": 1.07,
    "min_net_r": 20,
    "max_drawdown": 50,
}


def is_valid_result(metrics, scoring_config=None):
    config = {**DEFAULT_SCORING_CONFIG, **(scoring_config or {})}
    if config["min_trades"] is not None and metrics["total_trades"] < config["min_trades"]:
        return False
    if config["min_profit_factor"] is not None and metrics["profit_factor"] < config["min_profit_factor"]:
        return False
    if config["min_net_r"] is not None and metrics["net_r"] < config["min_net_r"]:
        return False
    if config["max_drawdown"] is not None and metrics["max_drawdown"] > config["max_drawdown"]:
        return False
    return True


def score_result(metrics, scoring_config=None):
    if not is_valid_result(metrics, scoring_config):
        return None

    net_r = metrics["net_r"]
    pf = metrics["profit_factor"]
    dd = metrics["max_drawdown"]
    trades = metrics["total_trades"]
    expectancy = metrics["expectancy"]

    # Main components
    profit_component = net_r * 0.45
    pf_component = (pf - 1.0) * 300
    expectancy_component = expectancy * 400
    trade_component = min(trades, 3000) * 0.05

    # DD penalty softer than breakout scorer
    dd_penalty = dd * 7

    score = (
        profit_component
        + pf_component
        + expectancy_component
        + trade_component
        - dd_penalty
    )

    return round(score, 2)
