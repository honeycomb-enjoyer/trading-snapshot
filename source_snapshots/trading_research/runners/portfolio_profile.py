"""Generate a portfolio profile from existing isolated strategy profiles."""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path

from data.schema import project_root
from master_config import PORTFOLIO_PROFILE_CONFIG
from portfolio_profile.analyzer import build_portfolio_profile
from portfolio_profile.reporting import write_portfolio_profile
from runners.common import report_dir


def _risk_override(value: str) -> tuple[str, float]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("Risk override must use LABEL=RISK_PCT")
    label, risk = value.split("=", 1)
    label = label.strip()
    if not label:
        raise argparse.ArgumentTypeError("Risk override label cannot be empty")
    try:
        risk_pct = float(risk)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid risk percent: {risk!r}") from exc
    if risk_pct <= 0:
        raise argparse.ArgumentTypeError("Risk percent must be positive")
    return label, risk_pct


def _config_with_overrides(
    config: dict,
    *,
    risk_overrides: list[tuple[str, float]] | None = None,
    name: str | None = None,
) -> dict:
    output = deepcopy(config)
    components = output["components"]
    by_label = {str(component["label"]): component for component in components}
    overrides = dict(risk_overrides or [])
    unknown = sorted(set(overrides).difference(by_label))
    if unknown:
        known = ", ".join(sorted(by_label))
        raise ValueError(f"Unknown portfolio component label(s): {unknown}. Known labels: {known}")
    for label, risk_pct in overrides.items():
        by_label[label]["risk_pct"] = risk_pct
    if name is not None:
        output["name"] = name
    elif overrides:
        tokens = []
        for component in components:
            label = str(component["label"])
            if label in overrides:
                value = f"{float(component['risk_pct']):g}".replace(".", "p")
                tokens.append(f"{label}{value}")
        output["name"] = f"{output['name']}_{'_'.join(tokens)}"
    return output


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a portfolio profile from existing strategy_profile artifacts.",
    )
    parser.add_argument(
        "--risk",
        action="append",
        default=[],
        type=_risk_override,
        metavar="LABEL=RISK_PCT",
        help="Override component risk percent, for example --risk B=0.75 --risk E=0.60.",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Override portfolio report folder name. Useful to keep several risk profiles.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> Path:
    args = parse_args(argv)
    config = _config_with_overrides(
        PORTFOLIO_PROFILE_CONFIG,
        risk_overrides=args.risk,
        name=args.name,
    )
    profile_root = report_dir("strategy_profile")
    profile = build_portfolio_profile(
        profile_root=profile_root,
        config=config,
    )
    output = report_dir("portfolio_profile") / profile["summary"]["portfolio"]["name"]
    summary_path = write_portfolio_profile(profile, output)
    perf = profile["summary"]["performance"]
    challenge = profile["summary"]["challenge"]
    print("\n========== PORTFOLIO PROFILE ==========")
    print(f"Portfolio: {profile['summary']['portfolio']['name']}")
    print(f"Components: {len(profile['summary']['portfolio']['components'])}")
    print(f"Final balance: {perf['final_balance_pct']:.2f}%")
    print(f"Intraday MTM DD: {perf['intraday_mtm_drawdown_pct']:.2f}%")
    for horizon, values in challenge.items():
        print(
            f"Challenge {horizon}d: pass {values['pass_pct']:.1f}% | "
            f"fail {values['fail_pct']:.1f}% | unresolved {values['unresolved_pct']:.1f}%"
        )
    print(f"Report: {summary_path.relative_to(project_root())}")
    print("=======================================")
    return summary_path


if __name__ == "__main__":
    main()
