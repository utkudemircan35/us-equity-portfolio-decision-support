from argparse import ArgumentParser
from dataclasses import dataclass
from math import floor
from typing import Any

import numpy as np
import pandas as pd

from database import (
    fetch_all,
    validate_database,
)
from portfolio_optimizer import (
    DEFAULT_FRONTIER_POINTS,
    DEFAULT_LOOKBACK_YEARS,
    DEFAULT_MAX_WEIGHT,
    DEFAULT_MINIMUM_OBSERVATIONS,
    DEFAULT_MIN_WEIGHT,
    PortfolioOptimizationResult,
    build_portfolio_optimizer,
)


DEFAULT_STRATEGY = "maximum_sharpe"
DEFAULT_COMMISSION_PER_TRADE = 0.0
DEFAULT_MINIMUM_TRADE_AMOUNT = 5.0

VALUE_TOLERANCE = 1e-10


STRATEGY_COLUMNS = {
    "equal_weight":
        "equal_weight",
    "minimum_variance":
        "minimum_variance_weight",
    "maximum_sharpe":
        "maximum_sharpe_weight",
}


@dataclass
class RebalancingResult:
    summary: dict[str, Any]
    recommendations: pd.DataFrame
    optimization: PortfolioOptimizationResult


def load_current_positions() -> pd.DataFrame:
    """
    Load current open positions and asset metadata.
    """

    rows = fetch_all(
        """
        SELECT
            h.ticker,
            h.shares,
            h.price_usd,
            h.market_value_usd,
            h.weight,
            a.company_name,
            a.asset_type
        FROM holdings_summary AS h
        JOIN assets AS a
          ON a.ticker = h.ticker
        WHERE h.status = 'OPEN'
        ORDER BY h.ticker
        """
    )

    positions = pd.DataFrame(rows)

    if positions.empty:
        raise RuntimeError(
            "No open positions were found."
        )

    positions["ticker"] = (
        positions["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    numeric_columns = [
        "shares",
        "price_usd",
        "market_value_usd",
        "weight",
    ]

    for column in numeric_columns:
        positions[column] = pd.to_numeric(
            positions[column],
            errors="raise",
        )

    return positions


def select_target_weights(
    optimization: PortfolioOptimizationResult,
    strategy: str,
    frontier_point: int | None,
) -> pd.Series:
    """
    Select target weights from an optimization strategy.
    """

    normalized_strategy = (
        strategy.strip().lower()
    )

    if normalized_strategy == "frontier":
        if frontier_point is None:
            raise ValueError(
                "A frontier point must be selected "
                "for the frontier strategy."
            )

        frontier_rows = (
            optimization
            .efficient_frontier_weights[
                optimization
                .efficient_frontier_weights[
                    "frontier_point"
                ]
                == frontier_point
            ]
        )

        if frontier_rows.empty:
            available_points = (
                optimization
                .efficient_frontier_weights[
                    "frontier_point"
                ]
                .tolist()
            )

            raise ValueError(
                f"Frontier point {frontier_point} "
                "does not exist. "
                f"Available points: {available_points}"
            )

        selected_row = (
            frontier_rows
            .iloc[0]
            .drop(
                labels="frontier_point"
            )
        )

        return pd.to_numeric(
            selected_row,
            errors="raise",
        ).astype(float)

    if (
        normalized_strategy
        not in STRATEGY_COLUMNS
    ):
        raise ValueError(
            "Unknown strategy. Use equal_weight, "
            "minimum_variance, maximum_sharpe "
            "or frontier."
        )

    target_column = (
        STRATEGY_COLUMNS[
            normalized_strategy
        ]
    )

    return (
        optimization
        .allocation
        .set_index("ticker")[
            target_column
        ]
        .astype(float)
    )


def calculate_trade_details(
    current_shares: float,
    current_value: float,
    price: float,
    target_value: float,
    minimum_trade_amount: float,
    commission_per_trade: float,
    fractional_shares: bool,
) -> dict[str, float | str]:
    """
    Calculate one security's rebalancing recommendation.
    """

    raw_difference = (
        target_value
        - current_value
    )

    action = "HOLD"
    share_change = 0.0
    gross_trade_value = 0.0
    commission = 0.0
    cash_impact = 0.0

    if (
        abs(raw_difference)
        >= minimum_trade_amount
    ):
        action = (
            "BUY"
            if raw_difference > 0
            else "SELL"
        )

        requested_value = abs(
            raw_difference
        )

        if price <= VALUE_TOLERANCE:
            raise RuntimeError(
                "A positive market price is required "
                "for rebalancing."
            )

        if fractional_shares:
            trade_shares = (
                requested_value / price
            )

        else:
            trade_shares = float(
                floor(
                    requested_value / price
                )
            )

        if trade_shares > VALUE_TOLERANCE:
            if action == "SELL":
                trade_shares = min(
                    trade_shares,
                    current_shares,
                )

            gross_trade_value = (
                trade_shares * price
            )

            commission = (
                commission_per_trade
            )

            if action == "BUY":
                share_change = (
                    trade_shares
                )

                cash_impact = -(
                    gross_trade_value
                    + commission
                )

            else:
                share_change = -(
                    trade_shares
                )

                cash_impact = (
                    gross_trade_value
                    - commission
                )

        else:
            action = "HOLD"

    post_trade_shares = (
        current_shares
        + share_change
    )

    if action == "BUY":
        post_trade_value = (
            current_value
            + gross_trade_value
        )

    elif action == "SELL":
        post_trade_value = (
            current_value
            - gross_trade_value
        )

    else:
        post_trade_value = (
            current_value
        )

    return {
        "action": action,
        "recommended_share_change":
            share_change,
        "gross_trade_value_usd":
            gross_trade_value,
        "estimated_commission_usd":
            commission,
        "cash_impact_usd":
            cash_impact,
        "post_trade_shares":
            post_trade_shares,
        "post_trade_value_usd":
            post_trade_value,
        "remaining_difference_usd":
            target_value
            - post_trade_value,
    }


def build_rebalancing_plan(
    strategy: str = DEFAULT_STRATEGY,
    frontier_point: int | None = None,
    commission_per_trade: float = (
        DEFAULT_COMMISSION_PER_TRADE
    ),
    minimum_trade_amount: float = (
        DEFAULT_MINIMUM_TRADE_AMOUNT
    ),
    fractional_shares: bool = True,
    selected_tickers: list[str] | None = None,
    lookback_years: int = (
        DEFAULT_LOOKBACK_YEARS
    ),
    minimum_observations: int = (
        DEFAULT_MINIMUM_OBSERVATIONS
    ),
    min_weight: float = (
        DEFAULT_MIN_WEIGHT
    ),
    max_weight: float = (
        DEFAULT_MAX_WEIGHT
    ),
    cash_reserve: float | None = None,
    frontier_points: int = (
        DEFAULT_FRONTIER_POINTS
    ),
    risk_free_rate: float | None = None,
) -> RebalancingResult:
    """
    Build a rebalancing plan from an optimizer strategy.
    """

    validate_database()

    if commission_per_trade < 0:
        raise ValueError(
            "Commission per trade cannot be negative."
        )

    if minimum_trade_amount < 0:
        raise ValueError(
            "Minimum trade amount cannot be negative."
        )

    optimization = (
        build_portfolio_optimizer(
            lookback_years=(
                lookback_years
            ),
            minimum_observations=(
                minimum_observations
            ),
            min_weight=min_weight,
            max_weight=max_weight,
            cash_reserve=cash_reserve,
            frontier_points=(
                frontier_points
            ),
            risk_free_rate=(
                risk_free_rate
            ),
            selected_tickers=(
                selected_tickers
            ),
        )
    )

    positions = load_current_positions()

    total_portfolio_value = float(
        positions[
            "market_value_usd"
        ].sum()
    )

    if (
        total_portfolio_value
        <= VALUE_TOLERANCE
    ):
        raise RuntimeError(
            "Portfolio value must be "
            "greater than zero."
        )

    target_weights = select_target_weights(
        optimization=optimization,
        strategy=strategy,
        frontier_point=frontier_point,
    )

    missing_tickers = sorted(
        set(positions["ticker"])
        - set(target_weights.index)
    )

    if missing_tickers:
        raise RuntimeError(
            "Target weights are missing for: "
            + ", ".join(missing_tickers)
        )

    status_map = (
        optimization
        .allocation
        .set_index("ticker")[
            "optimization_status"
        ]
        .to_dict()
    )

    security_positions = positions[
        positions["asset_type"]
        != "CASH"
    ].copy()

    records: list[
        dict[str, Any]
    ] = []

    total_commission = 0.0
    gross_buy_value = 0.0
    gross_sell_value = 0.0
    total_cash_impact = 0.0

    for row in (
        security_positions.itertuples(
            index=False
        )
    ):
        target_weight = float(
            target_weights.loc[
                row.ticker
            ]
        )

        target_value = (
            total_portfolio_value
            * target_weight
        )

        trade_details = (
            calculate_trade_details(
                current_shares=float(
                    row.shares
                ),
                current_value=float(
                    row.market_value_usd
                ),
                price=float(
                    row.price_usd
                ),
                target_value=target_value,
                minimum_trade_amount=(
                    minimum_trade_amount
                ),
                commission_per_trade=(
                    commission_per_trade
                ),
                fractional_shares=(
                    fractional_shares
                ),
            )
        )

        action = trade_details[
            "action"
        ]

        gross_trade_value = float(
            trade_details[
                "gross_trade_value_usd"
            ]
        )

        commission = float(
            trade_details[
                "estimated_commission_usd"
            ]
        )

        cash_impact = float(
            trade_details[
                "cash_impact_usd"
            ]
        )

        if action == "BUY":
            gross_buy_value += (
                gross_trade_value
            )

        elif action == "SELL":
            gross_sell_value += (
                gross_trade_value
            )

        total_commission += commission
        total_cash_impact += cash_impact

        records.append(
            {
                "ticker":
                    row.ticker,
                "company_name":
                    row.company_name,
                "asset_type":
                    row.asset_type,
                "optimization_status":
                    status_map.get(
                        row.ticker,
                        "UNKNOWN",
                    ),
                "current_shares":
                    float(row.shares),
                "price_usd":
                    float(row.price_usd),
                "current_value_usd":
                    float(
                        row.market_value_usd
                    ),
                "current_weight":
                    float(row.weight),
                "target_weight":
                    target_weight,
                "target_value_usd":
                    target_value,
                **trade_details,
            }
        )

    cash_position = positions[
        positions["asset_type"]
        == "CASH"
    ]

    current_cash = float(
        cash_position[
            "market_value_usd"
        ].sum()
    )

    target_cash_weight = float(
        target_weights.get(
            "CASH",
            0.0,
        )
    )

    target_cash = (
        total_portfolio_value
        * target_cash_weight
    )

    projected_cash = (
        current_cash
        + total_cash_impact
    )

    records.append(
        {
            "ticker": "CASH",
            "company_name":
                "US Dollar Cash",
            "asset_type": "CASH",
            "optimization_status": "CASH",
            "current_shares":
                current_cash,
            "price_usd": 1.0,
            "current_value_usd":
                current_cash,
            "current_weight":
                current_cash
                / total_portfolio_value,
            "target_weight":
                target_cash_weight,
            "target_value_usd":
                target_cash,
            "action":
                "CASH_RESERVE",
            "recommended_share_change":
                projected_cash
                - current_cash,
            "gross_trade_value_usd":
                0.0,
            "estimated_commission_usd":
                0.0,
            "cash_impact_usd":
                total_cash_impact,
            "post_trade_shares":
                projected_cash,
            "post_trade_value_usd":
                projected_cash,
            "remaining_difference_usd":
                target_cash
                - projected_cash,
        }
    )

    recommendations = pd.DataFrame(
        records
    )

    recommendations[
        "post_trade_weight"
    ] = (
        recommendations[
            "post_trade_value_usd"
        ]
        / total_portfolio_value
    )

    security_recommendations = (
        recommendations[
            recommendations[
                "asset_type"
            ]
            != "CASH"
        ]
    )

    action_counts = (
        security_recommendations[
            "action"
        ]
        .value_counts()
    )

    target_turnover = 0.5 * float(
        np.abs(
            recommendations[
                "target_weight"
            ]
            - recommendations[
                "current_weight"
            ]
        ).sum()
    )

    is_cash_feasible = (
        projected_cash
        >= -VALUE_TOLERANCE
    )

    summary = {
        "strategy":
            strategy.strip().lower(),
        "frontier_point":
            frontier_point,
        "total_portfolio_value_usd":
            total_portfolio_value,
        "current_cash_usd":
            current_cash,
        "target_cash_usd":
            target_cash,
        "projected_cash_usd":
            projected_cash,
        "cash_difference_usd":
            target_cash
            - projected_cash,
        "is_cash_feasible":
            is_cash_feasible,
        "gross_buy_value_usd":
            gross_buy_value,
        "gross_sell_value_usd":
            gross_sell_value,
        "estimated_commission_usd":
            total_commission,
        "target_turnover":
            target_turnover,
        "buy_count":
            int(
                action_counts.get(
                    "BUY",
                    0,
                )
            ),
        "sell_count":
            int(
                action_counts.get(
                    "SELL",
                    0,
                )
            ),
        "hold_count":
            int(
                action_counts.get(
                    "HOLD",
                    0,
                )
            ),
        "minimum_trade_amount_usd":
            minimum_trade_amount,
        "commission_per_trade_usd":
            commission_per_trade,
        "fractional_shares":
            fractional_shares,
    }

    return RebalancingResult(
        summary=summary,
        recommendations=(
            recommendations
        ),
        optimization=optimization,
    )


def build_argument_parser() -> ArgumentParser:
    """
    Build command-line arguments.
    """

    parser = ArgumentParser(
        description=(
            "Build a portfolio "
            "rebalancing plan."
        )
    )

    parser.add_argument(
        "--strategy",
        choices=[
            "equal_weight",
            "minimum_variance",
            "maximum_sharpe",
            "frontier",
        ],
        default=DEFAULT_STRATEGY,
    )

    parser.add_argument(
        "--frontier-point",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--commission-per-trade",
        type=float,
        default=(
            DEFAULT_COMMISSION_PER_TRADE
        ),
    )

    parser.add_argument(
        "--minimum-trade-amount",
        type=float,
        default=(
            DEFAULT_MINIMUM_TRADE_AMOUNT
        ),
    )

    parser.add_argument(
        "--whole-shares",
        action="store_true",
    )

    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
    )

    parser.add_argument(
        "--lookback-years",
        type=int,
        default=DEFAULT_LOOKBACK_YEARS,
    )

    parser.add_argument(
        "--minimum-observations",
        type=int,
        default=(
            DEFAULT_MINIMUM_OBSERVATIONS
        ),
    )

    parser.add_argument(
        "--min-weight",
        type=float,
        default=DEFAULT_MIN_WEIGHT,
    )

    parser.add_argument(
        "--max-weight",
        type=float,
        default=DEFAULT_MAX_WEIGHT,
    )

    parser.add_argument(
        "--cash-reserve",
        type=float,
        default=None,
    )

    parser.add_argument(
        "--frontier-points",
        type=int,
        default=DEFAULT_FRONTIER_POINTS,
    )

    parser.add_argument(
        "--risk-free-rate",
        type=float,
        default=None,
    )

    return parser


def print_result(
    result: RebalancingResult,
) -> None:
    """
    Print rebalancing results.
    """

    summary = result.summary

    print()
    print("REBALANCING PLAN")
    print("=" * 90)

    print(
        f"Strategy: "
        f"{summary['strategy']}"
    )

    if (
        summary["frontier_point"]
        is not None
    ):
        print(
            f"Frontier point: "
            f"{summary['frontier_point']}"
        )

    print(
        f"Portfolio value: "
        f"${summary['total_portfolio_value_usd']:.6f}"
    )

    print(
        f"Current cash: "
        f"${summary['current_cash_usd']:.6f}"
    )

    print(
        f"Target cash: "
        f"${summary['target_cash_usd']:.6f}"
    )

    print(
        f"Projected cash: "
        f"${summary['projected_cash_usd']:.6f}"
    )

    print(
        f"Cash difference: "
        f"${summary['cash_difference_usd']:.6f}"
    )

    print(
        f"Cash feasible: "
        f"{summary['is_cash_feasible']}"
    )

    print(
        f"Gross buys: "
        f"${summary['gross_buy_value_usd']:.6f}"
    )

    print(
        f"Gross sells: "
        f"${summary['gross_sell_value_usd']:.6f}"
    )

    print(
        f"Estimated commission: "
        f"${summary['estimated_commission_usd']:.6f}"
    )

    print(
        f"Target turnover: "
        f"{summary['target_turnover']:.6%}"
    )

    print(
        f"Actions: "
        f"{summary['buy_count']} buys, "
        f"{summary['sell_count']} sells, "
        f"{summary['hold_count']} holds"
    )

    if not summary["is_cash_feasible"]:
        print(
            "Warning: The proposed transactions "
            "would result in a negative cash balance."
        )

    with pd.option_context(
        "display.max_rows",
        None,
        "display.max_columns",
        None,
        "display.width",
        280,
        "display.float_format",
        lambda value: f"{value:.8f}",
    ):
        print()
        print("RECOMMENDATIONS")
        print("-" * 90)

        print(
            result
            .recommendations
            .to_string(index=False)
        )


def main() -> None:
    """
    Run the rebalancing calculator.
    """

    arguments = (
        build_argument_parser()
        .parse_args()
    )

    result = build_rebalancing_plan(
        strategy=arguments.strategy,
        frontier_point=(
            arguments.frontier_point
        ),
        commission_per_trade=(
            arguments
            .commission_per_trade
        ),
        minimum_trade_amount=(
            arguments
            .minimum_trade_amount
        ),
        fractional_shares=(
            not arguments.whole_shares
        ),
        selected_tickers=(
            arguments.tickers
        ),
        lookback_years=(
            arguments.lookback_years
        ),
        minimum_observations=(
            arguments
            .minimum_observations
        ),
        min_weight=(
            arguments.min_weight
        ),
        max_weight=(
            arguments.max_weight
        ),
        cash_reserve=(
            arguments.cash_reserve
        ),
        frontier_points=(
            arguments.frontier_points
        ),
        risk_free_rate=(
            arguments.risk_free_rate
        ),
    )

    print_result(result)


if __name__ == "__main__":
    main()
