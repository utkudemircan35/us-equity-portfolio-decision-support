from argparse import ArgumentParser
from dataclasses import dataclass
from math import sqrt
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import linprog, minimize

from database import (
    fetch_all,
    validate_database,
)
from risk_calculator import resolve_risk_free_rate


TRADING_DAYS_PER_YEAR = 252

DEFAULT_LOOKBACK_YEARS = 3
DEFAULT_MINIMUM_OBSERVATIONS = 252
DEFAULT_MIN_WEIGHT = 0.0
DEFAULT_MAX_WEIGHT = 0.20
DEFAULT_FRONTIER_POINTS = 40

NUMERICAL_TOLERANCE = 1e-10
OPTIMIZER_TOLERANCE = 1e-9


@dataclass
class PortfolioOptimizationResult:
    summary: dict[str, Any]
    allocation: pd.DataFrame
    strategy_metrics: pd.DataFrame
    efficient_frontier: pd.DataFrame
    efficient_frontier_weights: pd.DataFrame
    expected_returns: pd.Series
    covariance_matrix: pd.DataFrame


def query_to_dataframe(
    query: str,
    parameters: tuple[Any, ...] = (),
) -> pd.DataFrame:
    """
    Execute a database query and return a DataFrame.
    """

    rows = fetch_all(
        query,
        parameters,
    )

    return pd.DataFrame(rows)


def load_current_holdings() -> pd.DataFrame:
    """
    Load current open holdings with asset metadata.
    """

    holdings = query_to_dataframe(
        """
        SELECT
            h.ticker,
            h.status,
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

    if holdings.empty:
        raise RuntimeError(
            "No open holdings were found."
        )

    holdings["ticker"] = (
        holdings["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    holdings["market_value_usd"] = pd.to_numeric(
        holdings["market_value_usd"],
        errors="raise",
    )

    holdings["weight"] = pd.to_numeric(
        holdings["weight"],
        errors="raise",
    )

    total_weight = float(
        holdings["weight"].sum()
    )

    if not np.isclose(
        total_weight,
        1.0,
        atol=1e-6,
    ):
        raise RuntimeError(
            "Open holding weights do not sum to 100 percent. "
            f"Calculated total: {total_weight}"
        )

    return holdings


def load_adjusted_prices(
    tickers: list[str],
    lookback_years: int,
) -> pd.DataFrame:
    """
    Load adjusted prices for the selected lookback period.
    """

    placeholders = ", ".join(
        "?"
        for _ in tickers
    )

    prices = query_to_dataframe(
        f"""
        SELECT
            ticker,
            date,
            adjusted_close_usd
        FROM market_prices
        WHERE ticker IN ({placeholders})
        ORDER BY date, ticker
        """,
        tuple(tickers),
    )

    if prices.empty:
        raise RuntimeError(
            "No market prices were found "
            "for open positions."
        )

    prices["ticker"] = (
        prices["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    prices["date"] = pd.to_datetime(
        prices["date"],
        errors="raise",
    )

    prices["adjusted_close_usd"] = pd.to_numeric(
        prices["adjusted_close_usd"],
        errors="raise",
    )

    latest_date = prices["date"].max()

    earliest_date = (
        latest_date
        - pd.DateOffset(
            years=lookback_years
        )
    )

    prices = prices[
        prices["date"] >= earliest_date
    ].copy()

    price_matrix = prices.pivot_table(
        index="date",
        columns="ticker",
        values="adjusted_close_usd",
        aggfunc="last",
    )

    return price_matrix.sort_index()


def build_return_data(
    price_matrix: pd.DataFrame,
    requested_tickers: list[str],
    minimum_observations: int,
) -> tuple[
    pd.DataFrame,
    list[str],
    list[str],
    dict[str, int],
]:
    """
    Build a common return matrix and identify limited-history assets.
    """

    return_matrix = price_matrix.pct_change(
        fill_method=None
    )

    return_matrix = return_matrix.replace(
        [np.inf, -np.inf],
        np.nan,
    )

    observation_counts = {
        ticker: (
            int(return_matrix[ticker].count())
            if ticker in return_matrix.columns
            else 0
        )
        for ticker in requested_tickers
    }

    eligible_tickers = [
        ticker
        for ticker in requested_tickers
        if observation_counts[ticker]
        >= minimum_observations
    ]

    limited_tickers = [
        ticker
        for ticker in requested_tickers
        if ticker not in eligible_tickers
    ]

    # Remove the shortest-history asset until the common
    # return matrix satisfies the minimum observation limit.
    while len(eligible_tickers) > 1:
        common_returns = return_matrix[
            eligible_tickers
        ].dropna(how="any")

        if (
            len(common_returns)
            >= minimum_observations
        ):
            break

        removed_ticker = min(
            eligible_tickers,
            key=lambda ticker:
                observation_counts[ticker],
        )

        eligible_tickers.remove(
            removed_ticker
        )

        limited_tickers.append(
            removed_ticker
        )

    if len(eligible_tickers) < 2:
        raise RuntimeError(
            "At least two securities with sufficient "
            "common history are required."
        )

    common_returns = return_matrix[
        eligible_tickers
    ].dropna(how="any")

    if (
        len(common_returns)
        < minimum_observations
    ):
        raise RuntimeError(
            "Insufficient common observations "
            "for portfolio optimization."
        )

    return (
        common_returns,
        eligible_tickers,
        sorted(set(limited_tickers)),
        observation_counts,
    )


def validate_constraints(
    number_of_assets: int,
    eligible_capital: float,
    min_weight: float,
    max_weight: float,
) -> tuple[float, float]:
    """
    Validate total-portfolio weight constraints.

    Returned bounds are normalized for the optimizable sleeve.
    """

    if min_weight < 0:
        raise ValueError(
            "Minimum weight cannot be negative."
        )

    if max_weight <= 0:
        raise ValueError(
            "Maximum weight must be greater than zero."
        )

    if min_weight > max_weight:
        raise ValueError(
            "Minimum weight cannot exceed maximum weight."
        )

    if eligible_capital <= NUMERICAL_TOLERANCE:
        raise ValueError(
            "No capital remains for eligible securities."
        )

    if (
        number_of_assets * min_weight
        > eligible_capital
        + OPTIMIZER_TOLERANCE
    ):
        raise ValueError(
            "The minimum weight is too high for "
            "the number of eligible securities."
        )

    if (
        number_of_assets * max_weight
        < eligible_capital
        - OPTIMIZER_TOLERANCE
    ):
        required_maximum = (
            eligible_capital
            / number_of_assets
        )

        raise ValueError(
            "The maximum weight is too low for "
            "the number of eligible securities. "
            f"Use at least {required_maximum:.6%}."
        )

    sleeve_lower_bound = (
        min_weight / eligible_capital
    )

    sleeve_upper_bound = (
        max_weight / eligible_capital
    )

    return (
        sleeve_lower_bound,
        sleeve_upper_bound,
    )


def clean_solution(
    weights: np.ndarray,
) -> np.ndarray:
    """
    Remove numerical noise from optimized weights.
    """

    cleaned_weights = np.asarray(
        weights,
        dtype=float,
    ).copy()

    cleaned_weights[
        np.abs(cleaned_weights)
        <= NUMERICAL_TOLERANCE
    ] = 0.0

    cleaned_weights = np.clip(
        cleaned_weights,
        0.0,
        None,
    )

    total_weight = cleaned_weights.sum()

    if total_weight <= NUMERICAL_TOLERANCE:
        raise RuntimeError(
            "The optimizer returned invalid weights."
        )

    return cleaned_weights / total_weight


def calculate_portfolio_metrics(
    weights: np.ndarray,
    expected_returns: np.ndarray,
    covariance_matrix: np.ndarray,
    risk_free_rate: float,
) -> dict[str, float]:
    """
    Calculate expected return, volatility and Sharpe ratio.
    """

    expected_return = float(
        weights @ expected_returns
    )

    variance = float(
        weights.T
        @ covariance_matrix
        @ weights
    )

    volatility = sqrt(
        max(variance, 0.0)
    )

    if volatility > NUMERICAL_TOLERANCE:
        sharpe_ratio = (
            expected_return
            - risk_free_rate
        ) / volatility

    else:
        sharpe_ratio = float("nan")

    return {
        "expected_annual_return":
            expected_return,
        "annualized_volatility":
            volatility,
        "sharpe_ratio":
            sharpe_ratio,
    }


def solve_minimum_variance(
    covariance_matrix: np.ndarray,
    lower_bound: float,
    upper_bound: float,
    initial_weights: np.ndarray,
    expected_returns: np.ndarray | None = None,
    target_return: float | None = None,
) -> np.ndarray:
    """
    Solve the minimum-variance optimization problem.
    """

    constraints: list[
        dict[str, Any]
    ] = [
        {
            "type": "eq",
            "fun": (
                lambda weights:
                np.sum(weights) - 1.0
            ),
        }
    ]

    if (
        expected_returns is not None
        and target_return is not None
    ):
        constraints.append(
            {
                "type": "eq",
                "fun": (
                    lambda weights:
                    float(
                        weights
                        @ expected_returns
                        - target_return
                    )
                ),
            }
        )

    result = minimize(
        fun=(
            lambda weights:
            float(
                weights.T
                @ covariance_matrix
                @ weights
            )
        ),
        x0=initial_weights,
        method="SLSQP",
        bounds=[
            (
                lower_bound,
                upper_bound,
            )
            for _ in initial_weights
        ],
        constraints=constraints,
        options={
            "maxiter": 2000,
            "ftol": 1e-12,
        },
    )

    if not result.success:
        raise RuntimeError(
            "Minimum-variance optimization failed: "
            f"{result.message}"
        )

    return clean_solution(result.x)


def solve_maximum_sharpe(
    expected_returns: np.ndarray,
    covariance_matrix: np.ndarray,
    risk_free_rate: float,
    lower_bound: float,
    upper_bound: float,
    initial_weights: np.ndarray,
) -> np.ndarray:
    """
    Solve the maximum-Sharpe optimization problem.
    """

    def objective(
        weights: np.ndarray,
    ) -> float:
        metrics = calculate_portfolio_metrics(
            weights=weights,
            expected_returns=expected_returns,
            covariance_matrix=covariance_matrix,
            risk_free_rate=risk_free_rate,
        )

        sharpe_ratio = metrics[
            "sharpe_ratio"
        ]

        if not np.isfinite(sharpe_ratio):
            return 1e6

        return -sharpe_ratio

    result = minimize(
        fun=objective,
        x0=initial_weights,
        method="SLSQP",
        bounds=[
            (
                lower_bound,
                upper_bound,
            )
            for _ in initial_weights
        ],
        constraints=[
            {
                "type": "eq",
                "fun": (
                    lambda weights:
                    np.sum(weights) - 1.0
                ),
            }
        ],
        options={
            "maxiter": 2000,
            "ftol": 1e-12,
        },
    )

    if not result.success:
        raise RuntimeError(
            "Maximum-Sharpe optimization failed: "
            f"{result.message}"
        )

    return clean_solution(result.x)


def build_efficient_frontier(
    expected_returns: np.ndarray,
    covariance_matrix: np.ndarray,
    risk_free_rate: float,
    lower_bound: float,
    upper_bound: float,
    minimum_variance_weights: np.ndarray,
    number_of_points: int,
    tickers: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Build the deterministic efficient frontier
    and retain the weights of every frontier point.
    """

    asset_count = len(
        expected_returns
    )

    linear_bounds = [
        (
            lower_bound,
            upper_bound,
        )
        for _ in range(asset_count)
    ]

    maximum_return_result = linprog(
        c=-expected_returns,
        A_eq=np.ones(
            (
                1,
                asset_count,
            )
        ),
        b_eq=np.array([1.0]),
        bounds=linear_bounds,
        method="highs",
    )

    if not maximum_return_result.success:
        raise RuntimeError(
            "Unable to determine the maximum "
            "feasible portfolio return."
        )

    minimum_frontier_return = float(
        minimum_variance_weights
        @ expected_returns
    )

    maximum_frontier_return = float(
        maximum_return_result.x
        @ expected_returns
    )

    target_returns = np.linspace(
        minimum_frontier_return,
        maximum_frontier_return,
        number_of_points,
    )

    metric_records: list[
        dict[str, float | int]
    ] = []

    weight_records: list[
        dict[str, float | int]
    ] = []

    previous_weights = (
        minimum_variance_weights
    )

    for target_return in target_returns:
        try:
            weights = solve_minimum_variance(
                covariance_matrix=(
                    covariance_matrix
                ),
                lower_bound=lower_bound,
                upper_bound=upper_bound,
                initial_weights=(
                    previous_weights
                ),
                expected_returns=(
                    expected_returns
                ),
                target_return=float(
                    target_return
                ),
            )

        except RuntimeError:
            continue

        previous_weights = weights

        metrics = calculate_portfolio_metrics(
            weights=weights,
            expected_returns=expected_returns,
            covariance_matrix=(
                covariance_matrix
            ),
            risk_free_rate=risk_free_rate,
        )

        frontier_point = (
            len(metric_records) + 1
        )

        metric_records.append(
            {
                "frontier_point":
                    frontier_point,
                "target_return":
                    float(target_return),
                **metrics,
            }
        )

        weight_records.append(
            {
                "frontier_point":
                    frontier_point,
                **dict(
                    zip(
                        tickers,
                        weights,
                    )
                ),
            }
        )

    return (
        pd.DataFrame(metric_records),
        pd.DataFrame(weight_records),
    )

def build_portfolio_optimizer(
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
    selected_tickers: list[str] | None = None,
) -> PortfolioOptimizationResult:
    """
    Build current, equal-weight, minimum-variance
    and maximum-Sharpe portfolios.
    """

    validate_database()

    if lookback_years <= 0:
        raise ValueError(
            "Lookback years must be positive."
        )

    if minimum_observations < 2:
        raise ValueError(
            "Minimum observations must be at least 2."
        )

    if frontier_points < 2:
        raise ValueError(
            "Frontier points must be at least 2."
        )

    risk_free_details = (
        resolve_risk_free_rate(
            risk_free_rate
        )
    )

    annual_risk_free_rate = float(
        risk_free_details["rate"]
    )

    holdings = load_current_holdings()

    risky_holdings = holdings[
        holdings["asset_type"].isin(
            [
                "EQUITY",
                "ETF",
            ]
        )
    ].copy()

    cash_rows = holdings[
        holdings["asset_type"] == "CASH"
    ]

    current_cash_weight = float(
        cash_rows["weight"].sum()
    )

    if cash_reserve is None:
        target_cash_weight = (
            current_cash_weight
        )

    else:
        target_cash_weight = float(
            cash_reserve
        )

    if (
        target_cash_weight < 0
        or target_cash_weight >= 1
    ):
        raise ValueError(
            "Cash reserve must be at least 0 "
            "and less than 1."
        )

    available_tickers = (
        risky_holdings[
            "ticker"
        ].tolist()
    )

    if selected_tickers is None:
        requested_tickers = (
            available_tickers
        )

    else:
        requested_tickers = sorted(
            {
                str(ticker)
                .strip()
                .upper()
                for ticker in selected_tickers
            }
        )

        invalid_tickers = sorted(
            set(requested_tickers)
            - set(available_tickers)
        )

        if invalid_tickers:
            raise ValueError(
                "Selected tickers are not open "
                "EQUITY or ETF positions: "
                + ", ".join(invalid_tickers)
            )

    if len(requested_tickers) < 2:
        raise ValueError(
            "Select at least two open securities."
        )

    unselected_tickers = sorted(
        set(available_tickers)
        - set(requested_tickers)
    )

    price_matrix = load_adjusted_prices(
        tickers=requested_tickers,
        lookback_years=lookback_years,
    )

    (
        common_returns,
        eligible_tickers,
        limited_tickers,
        observation_counts,
    ) = build_return_data(
        price_matrix=price_matrix,
        requested_tickers=(
            requested_tickers
        ),
        minimum_observations=(
            minimum_observations
        ),
    )

    current_weight_series = (
        holdings
        .set_index("ticker")["weight"]
        .astype(float)
    )

    fixed_tickers = sorted(
        set(
            limited_tickers
            + unselected_tickers
        )
    )

    fixed_limited_weight = float(
        current_weight_series
        .reindex(limited_tickers)
        .fillna(0.0)
        .sum()
    )

    fixed_unselected_weight = float(
        current_weight_series
        .reindex(unselected_tickers)
        .fillna(0.0)
        .sum()
    )

    fixed_weight = (
        fixed_limited_weight
        + fixed_unselected_weight
    )

    eligible_capital = (
        1.0
        - target_cash_weight
        - fixed_weight
    )

    (
        sleeve_lower_bound,
        sleeve_upper_bound,
    ) = validate_constraints(
        number_of_assets=len(
            eligible_tickers
        ),
        eligible_capital=eligible_capital,
        min_weight=min_weight,
        max_weight=max_weight,
    )

    expected_returns = (
        common_returns.mean()
        * TRADING_DAYS_PER_YEAR
    )

    covariance_matrix = (
        common_returns.cov()
        * TRADING_DAYS_PER_YEAR
    )

    expected_returns = (
        expected_returns.reindex(
            eligible_tickers
        )
    )

    covariance_matrix = (
        covariance_matrix.reindex(
            index=eligible_tickers,
            columns=eligible_tickers,
        )
    )

    # Add a minimal diagonal value for numerical stability.
    covariance_matrix = (
        covariance_matrix
        + np.eye(
            len(covariance_matrix)
        )
        * 1e-12
    )

    expected_return_vector = (
        expected_returns.to_numpy(
            dtype=float
        )
    )

    covariance_array = (
        covariance_matrix.to_numpy(
            dtype=float
        )
    )

    equal_weights = np.full(
        len(eligible_tickers),
        1.0 / len(eligible_tickers),
    )

    current_sleeve_weights = (
        current_weight_series
        .reindex(eligible_tickers)
        .to_numpy(dtype=float)
    )

    current_sleeve_weights = (
        current_sleeve_weights
        / current_sleeve_weights.sum()
    )

    minimum_variance_weights = (
        solve_minimum_variance(
            covariance_matrix=(
                covariance_array
            ),
            lower_bound=(
                sleeve_lower_bound
            ),
            upper_bound=(
                sleeve_upper_bound
            ),
            initial_weights=(
                equal_weights
            ),
        )
    )

    maximum_sharpe_weights = (
        solve_maximum_sharpe(
            expected_returns=(
                expected_return_vector
            ),
            covariance_matrix=(
                covariance_array
            ),
            risk_free_rate=(
                annual_risk_free_rate
            ),
            lower_bound=(
                sleeve_lower_bound
            ),
            upper_bound=(
                sleeve_upper_bound
            ),
            initial_weights=(
                equal_weights
            ),
        )
    )

    strategies = {
        "CURRENT_SLEEVE":
            current_sleeve_weights,
        "EQUAL_WEIGHT":
            equal_weights,
        "MINIMUM_VARIANCE":
            minimum_variance_weights,
        "MAXIMUM_SHARPE":
            maximum_sharpe_weights,
    }

    metric_rows = []

    for (
        strategy_name,
        strategy_weights,
    ) in strategies.items():
        metrics = (
            calculate_portfolio_metrics(
                weights=strategy_weights,
                expected_returns=(
                    expected_return_vector
                ),
                covariance_matrix=(
                    covariance_array
                ),
                risk_free_rate=(
                    annual_risk_free_rate
                ),
            )
        )

        metric_rows.append(
            {
                "strategy": strategy_name,
                **metrics,
            }
        )

    strategy_metrics = pd.DataFrame(
        metric_rows
    )

    (
        efficient_frontier,
        frontier_sleeve_weights,
    ) = build_efficient_frontier(
        expected_returns=(
            expected_return_vector
        ),
        covariance_matrix=(
            covariance_array
        ),
        risk_free_rate=(
            annual_risk_free_rate
        ),
        lower_bound=(
            sleeve_lower_bound
        ),
        upper_bound=(
            sleeve_upper_bound
        ),
        minimum_variance_weights=(
            minimum_variance_weights
        ),
        number_of_points=(
            frontier_points
        ),
        tickers=eligible_tickers,
    )
    frontier_weights = (
        frontier_sleeve_weights.copy()
    )

    # Convert sleeve weights into total-portfolio weights.
    for ticker in eligible_tickers:
        frontier_weights[ticker] = (
            frontier_weights[ticker]
            * eligible_capital
        )

    # Preserve limited-history and unselected positions.
    for ticker in fixed_tickers:
        frontier_weights[ticker] = float(
            current_weight_series.get(
                ticker,
                0.0,
            )
        )

    frontier_weights["CASH"] = (
        target_cash_weight
    )

    frontier_weights = frontier_weights[
        [
            "frontier_point",
            *sorted(available_tickers),
            "CASH",
        ]
    ]

    allocation = holdings[
        [
            "ticker",
            "company_name",
            "asset_type",
            "weight",
        ]
    ].rename(
        columns={
            "weight": "current_weight",
        }
    )

    allocation[
        "history_observations"
    ] = allocation["ticker"].map(
        observation_counts
    )

    allocation[
        "optimization_status"
    ] = "FIXED"

    allocation.loc[
        allocation["ticker"].isin(
            eligible_tickers
        ),
        "optimization_status",
    ] = "OPTIMIZED"

    allocation.loc[
        allocation["ticker"].isin(
            limited_tickers
        ),
        "optimization_status",
    ] = "FIXED_LIMITED_HISTORY"

    allocation.loc[
        allocation["ticker"].isin(
            unselected_tickers
        ),
        "optimization_status",
    ] = "FIXED_NOT_SELECTED"


    allocation.loc[
        allocation["asset_type"] == "CASH",
        "optimization_status",
    ] = "CASH"

    target_columns = [
        "equal_weight",
        "minimum_variance_weight",
        "maximum_sharpe_weight",
    ]

    for column in target_columns:
        allocation[column] = (
            allocation["current_weight"]
        )

    allocation.loc[
        allocation["asset_type"] == "CASH",
        target_columns,
    ] = target_cash_weight

    optimized_weights = {
        "equal_weight":
            equal_weights,
        "minimum_variance_weight":
            minimum_variance_weights,
        "maximum_sharpe_weight":
            maximum_sharpe_weights,
    }

    for (
        column,
        sleeve_weights,
    ) in optimized_weights.items():
        total_portfolio_weights = (
            sleeve_weights
            * eligible_capital
        )

        target_weight_map = dict(
            zip(
                eligible_tickers,
                total_portfolio_weights,
            )
        )

        eligible_mask = (
            allocation["ticker"].isin(
                eligible_tickers
            )
        )

        allocation.loc[
            eligible_mask,
            column,
        ] = (
            allocation.loc[
                eligible_mask,
                "ticker",
            ]
            .map(target_weight_map)
        )

    for column in target_columns:
        target_total = float(
            allocation[column].sum()
        )

        if not np.isclose(
            target_total,
            1.0,
            atol=1e-6,
        ):
            raise RuntimeError(
                f"{column} does not sum to 100 percent. "
                f"Calculated total: {target_total}"
            )

    summary = {
        "analysis_start_date":
            common_returns.index.min()
            .strftime("%Y-%m-%d"),
        "analysis_end_date":
            common_returns.index.max()
            .strftime("%Y-%m-%d"),
        "common_observations":
            int(len(common_returns)),
        "eligible_tickers":
            eligible_tickers,
        "limited_history_tickers":
            limited_tickers,
        "current_cash_weight":
            current_cash_weight,
        "target_cash_weight":
            target_cash_weight,
        "fixed_limited_weight":
            fixed_limited_weight,
        "eligible_capital_weight":
            eligible_capital,
        "minimum_weight":
            min_weight,
        "maximum_weight":
            max_weight,
        "risk_free_rate":
            annual_risk_free_rate,
        "risk_free_rate_percent":
            risk_free_details[
                "percentage"
            ],
        "risk_free_rate_source":
            risk_free_details["source"],
        "risk_free_rate_observation_date":
            risk_free_details[
                "observation_date"
            ],
        "metrics_scope":
            "OPTIMIZABLE_SLEEVE",
        "selected_tickers":
            requested_tickers,
        "unselected_tickers":
            unselected_tickers,
        "fixed_unselected_weight":
            fixed_unselected_weight,
    }

    return PortfolioOptimizationResult(
        summary=summary,
        allocation=allocation,
        strategy_metrics=strategy_metrics,
        efficient_frontier=(
            efficient_frontier
        ),
        efficient_frontier_weights=(
            frontier_weights
        ),
        expected_returns=(
            expected_returns
        ),
        covariance_matrix=(
            covariance_matrix
        ),
    )


def build_argument_parser() -> ArgumentParser:
    """
    Build command-line arguments.
    """

    parser = ArgumentParser(
        description=(
            "Optimize the current portfolio."
        )
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
        help=(
            "Minimum total-portfolio weight "
            "as a decimal."
        ),
    )

    parser.add_argument(
        "--max-weight",
        type=float,
        default=DEFAULT_MAX_WEIGHT,
        help=(
            "Maximum total-portfolio weight "
            "as a decimal."
        ),
    )

    parser.add_argument(
        "--cash-reserve",
        type=float,
        default=None,
        help=(
            "Target cash weight as a decimal. "
            "Defaults to the current cash weight."
        ),
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
        help=(
            "Optional annual risk-free-rate override. "
            "Defaults to Yahoo Finance ^TNX."
        ),
    )

    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help=(
            "Open tickers to optimize. "
            "Unselected positions remain fixed."
        ),
    )

    return parser


def print_result(
    result: PortfolioOptimizationResult,
) -> None:
    """
    Print optimization results.
    """

    summary = result.summary

    print()
    print("PORTFOLIO OPTIMIZATION")
    print("=" * 80)

    print(
        f"Analysis period: "
        f"{summary['analysis_start_date']} to "
        f"{summary['analysis_end_date']}"
    )

    print(
        f"Common observations: "
        f"{summary['common_observations']}"
    )

    print(
        f"Eligible securities: "
        f"{len(summary['eligible_tickers'])}"
    )

    limited_tickers = summary[
        "limited_history_tickers"
    ]

    print(
        "Limited-history securities: "
        + (
            ", ".join(limited_tickers)
            if limited_tickers
            else "None"
        )
    )

    print(
        f"Current cash weight: "
        f"{summary['current_cash_weight']:.6%}"
    )

    print(
        f"Target cash weight: "
        f"{summary['target_cash_weight']:.6%}"
    )

    print(
        f"Eligible capital weight: "
        f"{summary['eligible_capital_weight']:.6%}"
    )

    print(
        f"Risk-free rate source: "
        f"{summary['risk_free_rate_source']}"
    )

    print(
        f"Risk-free rate observation date: "
        f"{summary['risk_free_rate_observation_date']}"
    )

    print(
        f"Risk-free rate used: "
        f"{summary['risk_free_rate_percent']:.6f}%"
    )

    with pd.option_context(
        "display.max_rows",
        None,
        "display.max_columns",
        None,
        "display.width",
        240,
        "display.float_format",
        lambda value: f"{value:.8f}",
    ):
        print()
        print("OPTIMIZABLE SLEEVE METRICS")
        print("-" * 80)

        print(
            result.strategy_metrics.to_string(
                index=False
            )
        )

        print()
        print("TARGET ALLOCATIONS")
        print("-" * 80)

        print(
            result.allocation.to_string(
                index=False
            )
        )

        print()
        print("EFFICIENT FRONTIER")
        print("-" * 80)

        print(
            result.efficient_frontier.to_string(
                index=False
            )
        )


def main() -> None:
    """
    Run portfolio optimization.
    """

    arguments = (
        build_argument_parser()
        .parse_args()
    )


    result = build_portfolio_optimizer(
        lookback_years=(
            arguments.lookback_years
        ),
        minimum_observations=(
            arguments.minimum_observations
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
        selected_tickers=arguments.tickers,
    )

    print_result(result)


if __name__ == "__main__":
    main()
